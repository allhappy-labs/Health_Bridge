"""Replayable external statistics backed by archived originals.

Ordinary means replace only the affected hour. Additive series use absolute
cumulative sums from originals and replace the affected tail (never a delta,
which could double-apply after a crash). Source pages and recorder writes are
bounded. The archive prefix is scanned to calculate the cumulative baseline.

HA has no public per-hour delete API. An empty previously visible hour queues
a durable full rebuild; its sentinel job survives a crash between clear/import.
Only integration-owned external IDs are cleared, never live sensor history.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
from itertools import islice
import math

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    get_last_statistics,
    statistic_during_period,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant

from .archive_protocol import ArchiveSample, CategoryPayload, QuantityPayload
from .archive_store import (
    ArchiveQuery,
    ArchiveStore,
    ProjectionJob,
    FULL_REBUILD_HOUR,
    RECONCILE_HOUR,
)
from .statistic_rules import RULES, TYPE_METRICS, StatisticRule

HOUR = timedelta(hours=1)
END = datetime.max.replace(tzinfo=timezone.utc)
CHUNK = 168


class StatisticsNeedRebuild(Exception):
    """Recorder contains a formerly valid hour absent from the archive."""


def statistic_id(metric: str, user_id: str) -> str:
    """Stable opaque user namespace, independent of entity registry IDs."""
    digest = hashlib.sha256(user_id.encode()).hexdigest()[:32]
    return f"health_bridge:{metric}_{digest}"


def project_hour(
    samples: Iterable[ArchiveSample], rule: StatisticRule, hour: datetime
) -> StatisticData | None:
    """Project one UTC hour, independent of input order and local DST.

    Instantaneous quantities use sample-start arithmetic mean/min/max. For
    additive intervals, partition at every boundary and select the lexically
    first source bundle per segment; preserve distinct UUIDs within that source.
    This explicit priority is not Apple's private Health source preference.
    Sleep uses interval unions, not sums of overlapping stage records.
    """
    if hour.tzinfo is None:
        raise ValueError("invalid_hour")
    hour = hour.astimezone(timezone.utc)
    if hour.minute or hour.second or hour.microsecond:
        raise ValueError("invalid_hour")
    if rule.mode == "timeline":
        return None
    end = hour + HOUR
    points = []
    for sample in {s.uuid: s for s in samples}.values():
        start, stop = (
            sample.start.astimezone(timezone.utc),
            sample.end.astimezone(timezone.utc),
        )
        if not (start < end and (stop > hour or start == stop >= hour)):
            continue
        payload = sample.payload
        if rule.mode == "duration":
            if not isinstance(payload, CategoryPayload):
                raise ValueError("invalid_payload_kind")
            if payload.value not in range(6):
                raise ValueError("unsupported_sleep_category")
            if payload.value == 0:  # In-bed is not a sleep stage.
                continue
        else:
            if not isinstance(payload, QuantityPayload):
                raise ValueError("invalid_payload_kind")
            if payload.canonical_unit != rule.canonical_unit:
                raise ValueError("invalid_unit")
            if (
                not math.isfinite(payload.canonical_value)
                or payload.canonical_value < 0
            ):
                raise ValueError("invalid_quantity")
        points.append((sample, start, stop))
    if rule.mode == "mean":
        values = [
            s.payload.canonical_value for s, start, _ in points if hour <= start < end
        ]
        if not values:
            return None
        return {
            "start": hour,
            "mean": math.fsum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }
    boundaries = sorted(
        {
            hour,
            end,
            *(max(hour, start) for _, start, _ in points),
            *(min(end, stop) for _, _, stop in points),
        }
    )
    contributions = []
    for left, right in zip(boundaries, boundaries[1:]):
        active = [
            (s, start, stop)
            for s, start, stop in points
            if start <= left and stop >= right and start < stop
        ]
        if not active:
            continue
        source = min(s.source.bundle_id for s, _, _ in active)
        selected = [
            (s, start, stop)
            for s, start, stop in active
            if s.source.bundle_id == source
        ]
        seconds = (right - left).total_seconds()
        if rule.mode == "duration":
            if any(s.payload.value in rule.categories for s, _, _ in selected):
                contributions.append(seconds / 3600)
        else:
            contributions.extend(
                s.payload.canonical_value * seconds / (stop - start).total_seconds()
                for s, start, stop in selected
            )
    # Zero-duration quantities are counted at their instant, with the same
    # source selection for timestamp ties; different UUIDs remain distinct.
    for instant in sorted({start for _, start, stop in points if start == stop}):
        instant_points = [s for s, start, stop in points if start == stop == instant]
        # Intervals use half-open coverage at this instant too. Otherwise a
        # lower-priority point could add on top of a preferred device's total.
        source = min(
            s.source.bundle_id
            for s, start, stop in points
            if start <= instant < stop or start == stop == instant
        )
        if rule.mode == "total":
            contributions.extend(
                s.payload.canonical_value
                for s in instant_points
                if s.source.bundle_id == source
            )
    if not contributions:
        return None
    total = math.fsum(contributions)
    return {"start": hour, "state": total, "sum": total}


def _samples(store: ArchiveStore, job: ProjectionJob) -> Iterator[ArchiveSample]:
    query = ArchiveQuery(
        job.user_id, job.sample_type, FULL_REBUILD_HOUR, END, limit=500
    )
    while True:
        page = store.query_samples(query)
        yield from page.samples
        if page.next_cursor is None:
            return
        query = replace(query, cursor=page.next_cursor)


def _series(store: ArchiveStore, job: ProjectionJob, rules: list[StatisticRule]):
    """Sweep sorted pages, retaining originals for only the current hour."""
    source = iter(_samples(store, job))
    upcoming = next(source, None)
    active = []
    totals = {r.metric: 0.0 for r in rules}
    hour = (
        upcoming.start.replace(minute=0, second=0, microsecond=0) if upcoming else None
    )
    while hour is not None:
        end = hour + HOUR
        while upcoming is not None and upcoming.start < end:
            active.append(upcoming)
            upcoming = next(source, None)
        for rule in rules:
            if row := project_hour(active, rule, hour):
                if rule.additive:
                    totals[rule.metric] += row["state"]
                    row["sum"] = totals[rule.metric]
                if hour >= job.hour_start:
                    yield rule, row
        active = [s for s in active if s.end > end]
        if active:
            hour = end
        elif upcoming:
            hour = upcoming.start.replace(minute=0, second=0, microsecond=0)
        else:
            hour = None


def _metadata(rule: StatisticRule, user_id: str):
    return {
        "statistic_id": statistic_id(rule.metric, user_id),
        "source": "health_bridge",
        "name": f"Health Bridge {rule.metric.replace('_', ' ')}",
        "unit_of_measurement": rule.unit,
        "unit_class": rule.unit_class,
        "mean_type": StatisticMeanType.NONE
        if rule.additive
        else StatisticMeanType.ARITHMETIC,
        "has_sum": rule.additive,
    }


class ArchiveProjectionWorker:
    """One cancellable worker per integration; outbox owns durable progress."""

    def __init__(self, hass: HomeAssistant, store: ArchiveStore):
        self.hass, self.store = hass, store
        self._task = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def available(self) -> bool:
        try:
            get_instance(self.hass)
        except KeyError:
            return False
        return True

    async def async_start(self) -> None:
        if not self.running:
            self._task = self.hass.async_create_background_task(
                self._run(), "health_bridge archive projections"
            )

    async def async_stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self):
        while True:
            try:
                if self.available:
                    jobs = await self.hass.async_add_executor_job(
                        partial(self.store.claim_projection_jobs, 1, retry_failed=False)
                    )
                    if jobs:
                        await self.async_rebuild(jobs[0])
                        continue
            except Exception:
                # Database/recorder outages leave durable work recoverable.
                # Never log exceptions containing health records.
                pass
            await asyncio.sleep(30)

    async def async_retry(self, user_id: str) -> None:
        await self.hass.async_add_executor_job(
            self.store.retry_failed_projections, user_id
        )

    async def async_delete_archive(self, user_id: str) -> None:
        """Delete originals while preserving recorder copies and other users."""
        async with self._lock:
            await self.hass.async_add_executor_job(
                self.store.delete_user_archive, user_id
            )

    async def _read(self, recorder, sid, start, end):
        if end - start > CHUNK * HOUR:
            raise ValueError("statistics_window_too_large")
        result = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {sid},
            "hour",
            None,
            {"mean", "min", "max", "state", "sum"},
        )
        return result.get(sid, [])

    async def _has_since(self, recorder, sid, start):
        """An ordered LIMIT 1 query proves tail/all-series existence."""
        result = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, sid, False, {"mean", "state", "sum"}
        )
        return any(row["start"] >= start.timestamp() for row in result.get(sid, []))

    async def _has_before(self, recorder, sid, end, rule):
        """One SQL aggregate result detects a restored stale prefix, even zero."""
        result = await recorder.async_add_executor_job(
            statistic_during_period,
            self.hass,
            None,
            end,
            sid,
            {"change"} if rule.additive else {"mean"},
            None,
        )
        return any(value is not None for value in result.values())

    async def _matches(self, recorder, sid, data, start=None, *, exact=True):
        """Compare values and exact hour inventory in bounded UTC windows."""
        start = start if start is not None else data[0]["start"]
        end = data[-1]["start"] + HOUR
        while start < end:
            stop = min(start + CHUNK * HOUR, end)
            expected = {
                row["start"].timestamp(): row
                for row in data
                if start <= row["start"] < stop
            }
            actual = {
                row["start"]: row
                for row in await self._read(recorder, sid, start, stop)
            }
            if (
                exact and actual.keys() != expected.keys()
            ) or not expected.keys() <= actual.keys():
                return False
            for timestamp, row in expected.items():
                if any(
                    not isinstance(actual[timestamp].get(key), (int, float))
                    or not math.isclose(
                        actual[timestamp][key], value, rel_tol=1e-9, abs_tol=1e-9
                    )
                    for key, value in row.items()
                    if key != "start"
                ):
                    return False
            start = stop
        return True

    async def _metadata_matches(self, recorder, rule, user):
        metadata = _metadata(rule, user)
        sid = metadata["statistic_id"]
        actual = await recorder.async_add_executor_job(
            partial(get_metadata, self.hass, statistic_ids={sid})
        )
        return sid in actual and all(
            actual[sid][1].get(key) == metadata[key]
            for key in (
                "unit_class",
                "unit_of_measurement",
                "mean_type",
                "has_sum",
                "source",
            )
        )

    async def async_projection_status(self, user_id):
        """Reconcile current claims with recorder before exposing them to users.

        Recorder can be restored/purged independently of the archive. This is a
        read-only comparison unless a mismatch queues durable replay; archive
        acknowledgements never wait for this full, bounded-page comparison.
        """
        async with self._lock:
            states = await self.hass.async_add_executor_job(
                self.store.projection_status, user_id
            )
            for sample_type, (state, _) in states.items():
                if state != "current":
                    continue
                rules = [
                    RULES[metric]
                    for metric in TYPE_METRICS.get(sample_type, ())
                    if RULES[metric].mode != "timeline"
                ]
                if not rules:
                    continue
                try:
                    matches = await self._series_matches(user_id, sample_type, rules)
                except StatisticsNeedRebuild:
                    await self.hass.async_add_executor_job(
                        self.store.request_full_projection_rebuild,
                        ProjectionJob(
                            "reconcile", user_id, sample_type, RECONCILE_HOUR, 0, None
                        ),
                    )
                    continue
                except Exception:
                    matches = False
                if not matches:
                    await self.hass.async_add_executor_job(
                        self.store.request_projection_reconciliation,
                        user_id,
                        sample_type,
                    )
            # Archive corrections can arrive during readback; re-read durable
            # status so their new jobs cannot be reported as current.
            return await self.hass.async_add_executor_job(
                self.store.projection_status, user_id
            )

    async def _series_matches(self, user_id, sample_type, rules):
        recorder = get_instance(self.hass)
        job = ProjectionJob("reconcile", user_id, sample_type, RECONCILE_HOUR, 0, None)
        series = _series(self.store, job, rules)
        checked_until = {rule.metric: None for rule in rules}
        seen = set()
        while chunk := await self.hass.async_add_executor_job(
            lambda: list(islice(series, CHUNK))
        ):
            for rule in rules:
                data = [row for candidate, row in chunk if candidate == rule]
                if not data:
                    continue
                sid = statistic_id(rule.metric, user_id)
                if rule.metric not in seen and await self._has_before(
                    recorder, sid, data[0]["start"], rule
                ):
                    raise StatisticsNeedRebuild
                if not await self._matches(
                    recorder, sid, data, checked_until[rule.metric]
                ):
                    return False
                if rule.metric not in seen and not await self._metadata_matches(
                    recorder, rule, user_id
                ):
                    return False
                seen.add(rule.metric)
                checked_until[rule.metric] = data[-1]["start"] + HOUR
        return not any(
            [
                await self._has_since(
                    recorder,
                    statistic_id(rule.metric, user_id),
                    checked_until[rule.metric] or RECONCILE_HOUR,
                )
                for rule in rules
            ]
        )

    async def _write_verified(self, recorder, rule, user, data):
        metadata = _metadata(rule, user)
        async_add_external_statistics(self.hass, metadata, data)
        await recorder.async_block_till_done()
        sid = metadata["statistic_id"]
        # async_block_till_done may observe an empty queue while the recorder
        # is executing the dequeued import. Only a matching read is a receipt.
        for attempt in range(20):
            if await self._matches(recorder, sid, data, exact=False):
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("statistics_readback_failed")
        if not await self._metadata_matches(recorder, rule, user):
            raise RuntimeError("statistics_metadata_failed")

    async def async_rebuild(self, job: ProjectionJob) -> None:
        async with self._lock:
            try:
                if not await self.hass.async_add_executor_job(
                    self.store.projection_claim_exists, job.job_id
                ):
                    return
                await self._rebuild(job)
            except ValueError as exc:
                code = (
                    str(exc)
                    if str(exc)
                    in {
                        "invalid_unit",
                        "invalid_payload_kind",
                        "invalid_quantity",
                        "unsupported_sleep_category",
                        "unsupported_sample_type",
                    }
                    else "projection_invalid"
                )
                await self.hass.async_add_executor_job(
                    self.store.fail_projection_job, job.job_id, code
                )
            except Exception:
                # Bounded exponential backoff, then explicit user retry.
                await self.hass.async_add_executor_job(
                    self.store.defer_projection_job,
                    job.job_id,
                    "statistics_unavailable",
                    min(300, 30 * 2 ** (job.attempts - 1))
                    if job.attempts < 5
                    else None,
                )

    async def _rebuild(self, job):
        if job.sample_type not in TYPE_METRICS:
            raise ValueError("unsupported_sample_type")
        rules = [
            RULES[m]
            for m in TYPE_METRICS[job.sample_type]
            if RULES[m].mode != "timeline"
        ]
        if not rules:  # Timeline-only records have no statistics obligation.
            await self.hass.async_add_executor_job(
                self.store.complete_projection_job, job.job_id
            )
            return
        recorder = get_instance(self.hass)
        full = job.hour_start == FULL_REBUILD_HOUR
        reconcile = job.hour_start == RECONCILE_HOUR
        coalesce = full or reconcile or all(r.additive for r in rules)
        snapshot = (
            await self.hass.async_add_executor_job(
                self.store.projection_tail_snapshot, job
            )
            if coalesce
            else (job.job_id,)
        )
        if full:
            ids = [statistic_id(r.metric, job.user_id) for r in rules]
            recorder.async_clear_statistics(ids)
            await recorder.async_block_till_done()
            for sid in ids:
                for attempt in range(20):
                    if not await self._has_since(recorder, sid, FULL_REBUILD_HOUR):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("statistics_clear_failed")
        elif not reconcile:
            query = ArchiveQuery(
                job.user_id,
                job.sample_type,
                job.hour_start,
                job.hour_start + HOUR,
                limit=500,
            )
            points = []
            while True:
                page = await self.hass.async_add_executor_job(
                    self.store.query_samples, query
                )
                points.extend(page.samples)
                if page.next_cursor is None:
                    break
                query = replace(query, cursor=page.next_cursor)
            projected = {
                r.metric: project_hour(points, r, job.hour_start) for r in rules
            }
            for rule in rules:
                if projected[rule.metric] is None and await self._read(
                    recorder,
                    statistic_id(rule.metric, job.user_id),
                    job.hour_start,
                    job.hour_start + HOUR,
                ):
                    await self.hass.async_add_executor_job(
                        self.store.request_full_projection_rebuild, job
                    )
                    await self.hass.async_add_executor_job(
                        self.store.complete_projection_snapshot, snapshot
                    )
                    return
            for rule in rules:
                if not rule.additive and (row := projected[rule.metric]):
                    await self._write_verified(recorder, rule, job.user_id, [row])
            rules = [r for r in rules if r.additive]
        if rules:
            series = _series(self.store, job, rules)
            checked_until = {
                r.metric: None if full or reconcile else job.hour_start for r in rules
            }
            while chunk := await self.hass.async_add_executor_job(
                lambda: list(islice(series, CHUNK))
            ):
                for rule in rules:
                    data = [row for candidate, row in chunk if candidate == rule]
                    if data:
                        await self._write_verified(recorder, rule, job.user_id, data)
                        end = data[-1]["start"] + HOUR
                        if not await self._matches(
                            recorder,
                            statistic_id(rule.metric, job.user_id),
                            data,
                            checked_until[rule.metric],
                        ):
                            await self.hass.async_add_executor_job(
                                self.store.request_full_projection_rebuild, job
                            )
                            return
                        checked_until[rule.metric] = end
            for rule in rules:
                if await self._has_since(
                    recorder,
                    statistic_id(rule.metric, job.user_id),
                    checked_until[rule.metric] or RECONCILE_HOUR,
                ):
                    await self.hass.async_add_executor_job(
                        self.store.request_full_projection_rebuild, job
                    )
                    return
        await self.hass.async_add_executor_job(
            self.store.complete_projection_snapshot, snapshot
        )
