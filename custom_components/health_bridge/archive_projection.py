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
    statistics_during_period,
)
from homeassistant.core import HomeAssistant

from .archive_protocol import ArchiveSample, CategoryPayload, QuantityPayload
from .archive_store import ArchiveQuery, ArchiveStore, ProjectionJob, FULL_REBUILD_HOUR
from .statistic_rules import RULES, TYPE_METRICS, StatisticRule

HOUR = timedelta(hours=1)
END = datetime.max.replace(tzinfo=timezone.utc)
CHUNK = 168


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
        source = min(s.source.bundle_id for s in instant_points)
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

    async def _read(self, recorder, sid, start, end):
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

    async def _write_verified(self, recorder, rule, user, data):
        metadata = _metadata(rule, user)
        async_add_external_statistics(self.hass, metadata, data)
        await recorder.async_block_till_done()
        sid = metadata["statistic_id"]
        # async_block_till_done may observe an empty queue while the recorder
        # is executing the dequeued import. Only a matching read is a receipt.
        for attempt in range(20):
            actual = await self._read(
                recorder, sid, data[0]["start"], data[-1]["start"] + HOUR
            )
            by_start = {r["start"]: r for r in actual}
            if all(
                all(
                    isinstance(
                        by_start.get(expected["start"].timestamp(), {}).get(k),
                        (int, float),
                    )
                    and math.isclose(
                        by_start[expected["start"].timestamp()][k],
                        v,
                        rel_tol=1e-9,
                        abs_tol=1e-9,
                    )
                    for k, v in expected.items()
                    if k != "start"
                )
                for expected in data
            ):
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("statistics_readback_failed")
        meta = await recorder.async_add_executor_job(
            partial(get_metadata, self.hass, statistic_ids={sid})
        )
        if sid not in meta or any(
            meta[sid][1].get(k) != metadata[k]
            for k in (
                "unit_class",
                "unit_of_measurement",
                "mean_type",
                "has_sum",
                "source",
            )
        ):
            raise RuntimeError("statistics_metadata_failed")

    async def async_rebuild(self, job: ProjectionJob) -> None:
        async with self._lock:
            try:
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
        coalesce = full or all(r.additive for r in rules)
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
                    if not await self._read(recorder, sid, FULL_REBUILD_HOUR, END):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("statistics_clear_failed")
        else:
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
            checked_until = {r.metric: job.hour_start for r in rules}
            while chunk := await self.hass.async_add_executor_job(
                lambda: list(islice(series, CHUNK))
            ):
                for rule in rules:
                    data = [row for candidate, row in chunk if candidate == rule]
                    if data:
                        await self._write_verified(recorder, rule, job.user_id, data)
                        end = data[-1]["start"] + HOUR
                        actual = await self._read(
                            recorder,
                            statistic_id(rule.metric, job.user_id),
                            checked_until[rule.metric],
                            end,
                        )
                        if {r["start"] for r in actual} != {
                            r["start"].timestamp() for r in data
                        }:
                            await self.hass.async_add_executor_job(
                                self.store.request_full_projection_rebuild, job
                            )
                            return
                        checked_until[rule.metric] = end
            for rule in rules:
                if await self._read(
                    recorder,
                    statistic_id(rule.metric, job.user_id),
                    checked_until[rule.metric],
                    END,
                ):
                    await self.hass.async_add_executor_job(
                        self.store.request_full_projection_rebuild, job
                    )
                    return
        await self.hass.async_add_executor_job(
            self.store.complete_projection_snapshot, snapshot
        )
