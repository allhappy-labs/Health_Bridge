"""Deterministic rules and real recorder visibility, including repair/restart."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

from homeassistant.components.recorder.statistics import (
    statistics_during_period,
    get_metadata,
)
from custom_components.health_bridge.archive_protocol import (
    ArchiveLimits,
    ArchiveSample,
    ArchiveSource,
    CategoryPayload,
    QuantityPayload,
    validate_archive_request,
)
from custom_components.health_bridge.archive_store import ArchiveStore

UTC = timezone.utc
HOUR = datetime(2024, 1, 1, 10, tzinfo=UTC)
STEP = "HKQuantityTypeIdentifierStepCount"
HEART = "HKQuantityTypeIdentifierHeartRate"


@pytest.fixture
def api():
    name = "custom_components.health_bridge.archive_projection"
    assert importlib.util.find_spec(name), "Archive projection is not implemented"
    return importlib.import_module(name)


@pytest.fixture
def rules(api):
    return importlib.import_module(
        "custom_components.health_bridge.statistic_rules"
    ).RULES


def sample(
    value=60,
    start=HOUR,
    end=None,
    source="a.watch",
    unit="count",
    category=False,
    uuid="sample-1",
):
    return ArchiveSample(
        uuid,
        start,
        end if end is not None else start,
        ArchiveSource(source, source, "1"),
        "Europe/Zurich",
        "{}",
        CategoryPayload("category", 1, value)
        if category
        else QuantityPayload("quantity", 1, value, unit, value, unit),
    )


def test_instantaneous_mean_min_max_and_half_open_hour(api, rules):
    points = [
        sample(60, unit="count/min"),
        sample(90, unit="count/min", uuid="second"),
        sample(
            999, start=HOUR + timedelta(hours=1), unit="count/min", uuid="next-hour"
        ),
    ]
    result = api.project_hour(points, rules["heart_rate"], HOUR)
    assert (result["mean"], result["min"], result["max"]) == (75, 60, 90)
    assert "sum" not in result


def test_source_aware_prorated_totals_do_not_double_count_devices(api, rules):
    points = [
        sample(120, end=HOUR + timedelta(hours=2)),
        sample(999, end=HOUR + timedelta(hours=1), source="b.phone", uuid="phone"),
        sample(
            30,
            start=HOUR + timedelta(minutes=30),
            end=HOUR + timedelta(minutes=45),
            uuid="additional",
        ),
    ]
    assert api.project_hour(points, rules["steps"], HOUR)["state"] == 90
    assert (
        api.project_hour(points, rules["steps"], HOUR + timedelta(hours=1))["state"]
        == 60
    )
    assert api.project_hour(list(reversed(points)), rules["steps"], HOUR)["state"] == 90


def test_instant_source_priority_includes_overlapping_intervals(api, rules):
    points = [
        sample(120, end=HOUR + timedelta(hours=1)),
        sample(999, start=HOUR + timedelta(minutes=30), source="b.phone", uuid="phone"),
        sample(5, start=HOUR + timedelta(minutes=30), uuid="same-source"),
    ]
    assert api.project_hour(points[:2], rules["steps"], HOUR)["state"] == 120
    assert api.project_hour(points, rules["steps"], HOUR)["state"] == 125
    # A preferred interval ending at the instant no longer covers that point.
    points[0] = sample(120, end=HOUR + timedelta(minutes=30))
    assert api.project_hour(points[:2], rules["steps"], HOUR)["state"] == 1119


@pytest.mark.parametrize(
    "start", ["2024-03-31T00:30:00+00:00", "2024-10-27T00:30:00+00:00"]
)
def test_sleep_is_elapsed_overlap_across_dst_not_wall_time(api, rules, start):
    begin = datetime.fromisoformat(start)
    hour = begin.replace(minute=0)
    end = begin + timedelta(hours=2)
    points = [
        sample(
            3,
            start=begin.astimezone(ZoneInfo("Europe/Zurich")),
            end=end.astimezone(ZoneInfo("Europe/Zurich")),
            category=True,
        )
    ]
    assert [
        api.project_hour(points, rules["sleep_core_hours"], hour + timedelta(hours=i))[
            "state"
        ]
        for i in range(3)
    ] == [0.5, 1, 0.5]
    assert api.project_hour(points, rules["sleep_rem_hours"], hour) is None


def test_sleep_union_stages_and_timeline_only_values(api, rules):
    points = [
        sample(3, end=HOUR + timedelta(hours=1), category=True),
        sample(
            3,
            end=HOUR + timedelta(hours=1),
            source="b.phone",
            category=True,
            uuid="phone",
        ),
        sample(0, end=HOUR + timedelta(hours=1), category=True, uuid="bed"),
    ]
    assert api.project_hour(points, rules["sleep_duration"], HOUR)["state"] == 1
    for metric in ("sleep_details", "asleep_time", "wake_time", "last_apple_workout"):
        assert api.project_hour(points, rules[metric], HOUR) is None


def test_invalid_canonical_unit_fails_instead_of_fabricating_statistics(api, rules):
    with pytest.raises(ValueError, match="invalid_unit"):
        api.project_hour([sample(60, unit="kg")], rules["heart_rate"], HOUR)


@pytest.mark.parametrize(
    "metric,unit,value,want",
    [
        ("walking_steadiness", "fraction", 0.8, 80),
        ("oxygen_saturation", "fraction", 0.98, 98),
        ("water_temperature", "degC", -2, -2),
        ("cycling_cadence", "rpm", 80, 80),
        ("blood_glucose", "mmol/L", 5, 5),
    ],
)
def test_new_means_convert_canonical_values_to_statistic_units(
    api, rules, metric, unit, value, want
):
    result = api.project_hour([sample(value, unit=unit)], rules[metric], HOUR)
    assert result["mean"] == pytest.approx(want)
    assert result["min"] == pytest.approx(want)
    assert result["max"] == pytest.approx(want)


@pytest.mark.parametrize(
    "metric,unit,value,want",
    [
        ("time_in_daylight", "min", 120, 3600),
        ("dietary_vitamin_d", "µg", 20, 10),
        ("distance_rowing", "m", 1000, 500),
        ("insulin_delivery", "IU", 10, 5),
        ("stand_time", "min", 60, 30),
    ],
)
def test_new_totals_prorate_originals_with_source_priority(
    api, rules, metric, unit, value, want
):
    points = [
        sample(value, unit=unit, end=HOUR + timedelta(hours=2)),
        sample(
            9999,
            unit=unit,
            source="b.phone",
            uuid="other",
            end=HOUR + timedelta(hours=2),
        ),
    ]
    result = api.project_hour(points, rules[metric], HOUR)
    assert result["state"] == pytest.approx(want)
    assert result["sum"] == pytest.approx(want)


def test_mindful_sessions_union_category_zero_intervals_in_seconds(api, rules):
    points = [
        sample(0, category=True, end=HOUR + timedelta(minutes=45)),
        sample(
            0,
            category=True,
            start=HOUR + timedelta(minutes=30),
            end=HOUR + timedelta(hours=1),
            uuid="overlap",
        ),
    ]
    assert api.project_hour(points, rules["mindful_minutes"], HOUR)["state"] == 3600
    with pytest.raises(ValueError, match="unsupported_category"):
        api.project_hour([sample(1, category=True)], rules["mindful_minutes"], HOUR)


def test_logarithmic_audio_exposure_is_timeline_only(api, rules):
    for metric in ("headphone_audio_exposure", "environmental_audio_exposure"):
        assert api.project_hour([sample(80, unit="dBA")], rules[metric], HOUR) is None


@pytest.fixture
async def store(hass, tmp_path):
    return await hass.async_add_executor_job(
        ArchiveStore.open, tmp_path / "archive.sqlite"
    )


@pytest.fixture
def projection_clock(monkeypatch):
    """Advance only durable job time; recorder/event-loop clocks remain real."""
    from custom_components.health_bridge import archive_store

    current = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz)

    # _instant also checks isinstance, so preserve real datetime identity there.
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class StoreClock(Clock, metaclass=ClockMeta):
        pass

    monkeypatch.setattr(archive_store, "datetime", StoreClock)

    def tick(seconds):
        current[0] += timedelta(seconds=seconds)

    return SimpleNamespace(tick=tick)


def payload(sample_type=STEP):
    result = json.loads(
        Path("docs/protocol/fixtures/archive-batch-v2.json").read_text()
    )
    result["sample_type"] = sample_type
    if sample_type == HEART:
        result["samples"][0]["payload"].update(
            raw_unit="count/min", canonical_unit="count/min"
        )
    return result


async def commit(hass, store, data, batch_id="batch-001"):
    data = deepcopy(data)
    data.update(batch_id=batch_id, request_id=batch_id)
    return await hass.async_add_executor_job(
        store.commit_batch, validate_archive_request(data, limits=ArchiveLimits())
    )


async def drain(hass, store, worker, maximum=20):
    for _ in range(maximum):
        jobs = await hass.async_add_executor_job(store.claim_projection_jobs, 1)
        if not jobs:
            return
        await worker.async_rebuild(jobs[0])
    pytest.fail("Projection queue did not drain")


async def rows(hass, recorder, statistic_id):
    await recorder.async_block_till_done()
    result = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        HOUR - timedelta(days=1),
        HOUR + timedelta(days=3),
        {statistic_id},
        "hour",
        None,
        {"mean", "min", "max", "state", "sum"},
    )
    return result.get(statistic_id, [])


async def test_real_statistics_metadata_idempotence_and_cumulative_correction(
    recorder_mock, api, hass, store
):
    worker = api.ArchiveProjectionWorker(hass, store)
    data = payload()
    extra = deepcopy(data["samples"][0])
    extra.update(
        uuid="bd085ccc-22f4-4e80-a865-149bb5b0d1d5",
        start="2024-01-01T11:00:00Z",
        end="2024-01-01T11:00:01Z",
    )
    extra["payload"].update(raw_value=8, canonical_value=8)
    data["samples"].append(extra)
    await commit(hass, store, data)
    await drain(hass, store, worker)
    sid = api.statistic_id("steps", "person-1")
    assert sid.startswith("health_bridge:steps_") and "person" not in sid
    assert sid != api.statistic_id("steps", "person-2")
    actual = await rows(hass, recorder_mock, sid)
    assert [(r["state"], r["sum"]) for r in actual] == [(12, 12), (8, 20)]
    metadata = await recorder_mock.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={sid})
    )
    assert metadata[sid][1]["mean_type"] == 0
    assert metadata[sid][1]["unit_class"] is None
    assert metadata[sid][1]["unit_of_measurement"] == "steps"
    await commit(hass, store, data, "retry")
    await drain(hass, store, worker)
    assert await rows(hass, recorder_mock, sid) == actual
    data["samples"] = data["samples"][:1]
    data["samples"][0]["payload"].update(raw_value=3, canonical_value=3)
    await commit(hass, store, data, "correct")
    await drain(hass, store, worker)
    assert [(r["state"], r["sum"]) for r in await rows(hass, recorder_mock, sid)] == [
        (3, 3),
        (8, 11),
    ]
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ] == ("current", None)


async def test_mean_replacement_and_last_sample_deletion_remove_old_hour(
    recorder_mock, api, hass, store
):
    worker = api.ArchiveProjectionWorker(hass, store)
    data = payload(HEART)
    await commit(hass, store, data)
    await drain(hass, store, worker)
    sid = api.statistic_id("heart_rate", "person-1")
    assert (await rows(hass, recorder_mock, sid))[0]["mean"] == 12
    data["samples"][0]["payload"].update(raw_value=70, canonical_value=70)
    await commit(hass, store, data, "correct")
    await drain(hass, store, worker)
    assert (await rows(hass, recorder_mock, sid))[0]["mean"] == 70
    data["deletions"] = [data["samples"][0]["uuid"]]
    data["samples"] = []
    await commit(hass, store, data, "delete")
    await drain(hass, store, worker)
    assert await rows(hass, recorder_mock, sid) == []


@pytest.mark.parametrize(
    "metric,unit,value,field,want,display,unit_class",
    [
        ("oxygen_saturation", "fraction", 0.98, "mean", 98, "%", None),
        ("water_temperature", "degC", -2, "mean", -2, "°C", "temperature"),
        ("time_in_daylight", "min", 2, "sum", 120, "s", "duration"),
        ("dietary_vitamin_d", "µg", 20, "sum", 20, "μg", "mass"),
        ("insulin_delivery", "IU", 5, "sum", 5, "IU", None),
        ("workout_effort_score", "appleEffortScore", 5, "mean", 5, None, None),
        ("mindful_minutes", None, 0, "sum", 60, "s", "duration"),
    ],
)
async def test_new_catalog_families_round_trip_real_recorder_metadata(
    recorder_mock,
    api,
    rules,
    hass,
    store,
    metric,
    unit,
    value,
    field,
    want,
    display,
    unit_class,
):
    data = payload(rules[metric].sample_type)
    data["samples"][0]["end"] = "2024-01-01T10:01:00Z"
    data["samples"][0]["payload"] = (
        {"kind": "category", "schema_version": 1, "value": value}
        if unit is None
        else {
            "kind": "quantity",
            "schema_version": 1,
            "raw_value": value,
            "raw_unit": unit,
            "canonical_value": value,
            "canonical_unit": unit,
        }
    )
    await commit(hass, store, data)
    worker = api.ArchiveProjectionWorker(hass, store)
    await drain(hass, store, worker)
    sid = api.statistic_id(metric, "person-1")
    actual = await rows(hass, recorder_mock, sid)
    assert actual[0][field] == pytest.approx(want)
    metadata = await recorder_mock.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={sid})
    )
    assert metadata[sid][1]["unit_of_measurement"] == display
    assert metadata[sid][1]["unit_class"] == unit_class
    assert (await worker.async_projection_status("person-1"))[
        rules[metric].sample_type
    ] == ("current", None)


async def test_missing_readback_never_reports_current_and_retry_recovers(
    recorder_mock, api, hass, store, monkeypatch, projection_clock
):
    worker = api.ArchiveProjectionWorker(hass, store)
    await commit(hass, store, payload())
    original = api.statistics_during_period
    monkeypatch.setattr(api, "statistics_during_period", lambda *args: {})
    job = (await hass.async_add_executor_job(store.claim_projection_jobs, 1))[0]
    await worker.async_rebuild(job)
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ][0] != "current"
    monkeypatch.setattr(api, "statistics_during_period", original)
    projection_clock.tick(301)
    await drain(hass, store, api.ArchiveProjectionWorker(hass, store))
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ] == ("current", None)


async def test_permanent_projection_error_surfaces_without_health_values(
    recorder_mock, api, hass, store
):
    data = payload()
    data["samples"][0]["payload"]["canonical_unit"] = "private-unit"
    await commit(hass, store, data)
    worker = api.ArchiveProjectionWorker(hass, store)
    job = (await hass.async_add_executor_job(store.claim_projection_jobs, 1))[0]
    await worker.async_rebuild(job)
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ] == ("failed", "invalid_unit")
    assert not await hass.async_add_executor_job(
        lambda: store.claim_projection_jobs(1, retry_failed=False)
    )


@pytest.mark.parametrize("recovery", ["correction", "deletion"])
async def test_projection_range_failure_retry_preserves_recorder_and_recovers(
    recorder_mock, api, hass, store, monkeypatch, recovery
):
    worker = api.ArchiveProjectionWorker(hass, store)
    data = payload()
    await commit(hass, store, data)
    await drain(hass, store, worker)
    sid = api.statistic_id("steps", "person-1")
    before = await rows(hass, recorder_mock, sid)
    unsafe = deepcopy(data)
    unsafe["samples"][0].update(uuid="bd085ccc-22f4-4e80-a865-149bb5b0d1d5",
                                 start="0001-01-01T00:00:00Z", end="9999-12-31T23:59:59Z")
    receipt = await commit(hass, store, unsafe, "oversized")
    assert receipt.projection_state == "failed"

    # Even a broken worker must never really sweep 87 million hours in this test.
    series = api._series
    def bounded_series(*args):
        for index, row in enumerate(series(*args)):
            assert index < 500, "unbounded projection sweep"
            yield row
    monkeypatch.setattr(api, "_series", bounded_series)
    await hass.async_add_executor_job(store.retry_failed_projections, "person-1")
    job = (await hass.async_add_executor_job(store.claim_projection_jobs, 1))[0]
    await worker.async_rebuild(job)
    assert (await worker.async_projection_status("person-1"))[STEP] == (
        "failed", "projection_range_exceeded"
    )
    assert await rows(hass, recorder_mock, sid) == before
    if recovery == "correction":
        unsafe["samples"][0].update(start=data["samples"][0]["start"], end=data["samples"][0]["end"])
    else:
        unsafe.update(deletions=[unsafe["samples"][0]["uuid"]], samples=[])
    await commit(hass, store, unsafe, "recover")
    await hass.async_add_executor_job(store.retry_failed_projections, "person-1")
    await drain(hass, store, worker)
    assert (await worker.async_projection_status("person-1"))[STEP] == ("current", None)
    assert (await rows(hass, recorder_mock, sid))[0]["sum"] == before[0]["sum"] * (2 if recovery == "correction" else 1)


async def test_expired_claim_is_recovered_after_restart(
    recorder_mock, api, hass, store, projection_clock
):
    await commit(hass, store, payload())
    await hass.async_add_executor_job(store.claim_projection_jobs, 1)
    assert not await hass.async_add_executor_job(store.claim_projection_jobs, 1)
    projection_clock.tick(301)
    await drain(hass, store, api.ArchiveProjectionWorker(hass, store))
    assert (await rows(hass, recorder_mock, api.statistic_id("steps", "person-1")))[0][
        "sum"
    ] == 12


async def test_archive_delete_invalidates_claim_before_it_can_clear_retained_statistics(
    recorder_mock, api, hass, store
):
    worker = api.ArchiveProjectionWorker(hass, store)
    data = payload()
    await commit(hass, store, data)
    await drain(hass, store, worker)
    sid = api.statistic_id("steps", "person-1")
    assert (await rows(hass, recorder_mock, sid))[0]["state"] == 12
    data["samples"][0]["payload"].update(raw_value=14, canonical_value=14)
    await commit(hass, store, data, "correction")
    claimed = (await hass.async_add_executor_job(store.claim_projection_jobs, 1))[0]
    await hass.async_add_executor_job(store.delete_user_archive, "person-1")
    await worker.async_rebuild(claimed)
    assert not await hass.async_add_executor_job(store.claim_projection_jobs, 1)
    assert (await rows(hass, recorder_mock, sid))[0]["state"] == 12


async def test_worker_health_entry_lifecycle_preserves_pal(
    recorder_mock, api, hass, bridge_entries
):
    worker = hass.data["health_bridge"]["archive_projection_worker"]
    assert worker.running
    await hass.config_entries.async_unload(bridge_entries[1].entry_id)
    assert worker.running
    await hass.config_entries.async_unload(bridge_entries[0].entry_id)
    assert not worker.running


async def test_deletion_rebuild_survives_crash_after_clear_and_keeps_other_user(
    recorder_mock, api, hass, store, monkeypatch, projection_clock
):
    worker = api.ArchiveProjectionWorker(hass, store)
    data = payload()
    extra = deepcopy(data["samples"][0])
    extra.update(
        uuid="bd085ccc-22f4-4e80-a865-149bb5b0d1d5",
        start="2024-01-01T11:00:00Z",
        end="2024-01-01T11:00:01Z",
    )
    data["samples"].append(extra)
    await commit(hass, store, data)
    other = deepcopy(data)
    other["user_id"] = "person-2"
    await commit(hass, store, other)
    await drain(hass, store, worker)
    other_id = api.statistic_id("steps", "person-2")
    other_rows = await rows(hass, recorder_mock, other_id)
    data["deletions"] = [extra["uuid"]]
    data["samples"] = []
    await commit(hass, store, data, "delete")
    normal = (await hass.async_add_executor_job(store.claim_projection_jobs, 1))[0]
    await worker.async_rebuild(normal)
    full = (await hass.async_add_executor_job(store.claim_projection_jobs, 1))[0]
    assert full.hour_start == api.FULL_REBUILD_HOUR
    original = api.async_add_external_statistics

    def crash(*args):
        raise asyncio.CancelledError

    import asyncio

    monkeypatch.setattr(api, "async_add_external_statistics", crash)
    with pytest.raises(asyncio.CancelledError):
        await worker.async_rebuild(full)
    sid = api.statistic_id("steps", "person-1")
    assert await rows(hass, recorder_mock, sid) == []
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ][0] == "pending"
    monkeypatch.setattr(api, "async_add_external_statistics", original)
    projection_clock.tick(301)
    await drain(hass, store, api.ArchiveProjectionWorker(hass, store))
    assert [(r["state"], r["sum"]) for r in await rows(hass, recorder_mock, sid)] == [
        (12, 12)
    ]
    assert await rows(hass, recorder_mock, other_id) == other_rows


async def test_ordinary_update_never_clears_statistics(
    recorder_mock, api, hass, store, monkeypatch
):
    worker = api.ArchiveProjectionWorker(hass, store)
    data = payload(HEART)
    await commit(hass, store, data)
    await drain(hass, store, worker)

    def forbidden(*args, **kwargs):
        raise AssertionError("Ordinary updates must not clear statistics")

    monkeypatch.setattr(recorder_mock, "async_clear_statistics", forbidden)
    data["samples"][0]["payload"].update(raw_value=85, canonical_value=85)
    await commit(hass, store, data, "correction")
    await drain(hass, store, worker)
    assert (
        await rows(hass, recorder_mock, api.statistic_id("heart_rate", "person-1"))
    )[0]["mean"] == 85


async def test_sleep_statistics_real_metadata_and_distinct_stage_sums(
    recorder_mock, api, hass, store
):
    data = payload()
    data["sample_type"] = "HKCategoryTypeIdentifierSleepAnalysis"
    data["samples"][0].update(
        end="2024-01-01T11:30:00Z",
        payload={"kind": "category", "schema_version": 1, "value": 5},
    )
    await commit(hass, store, data)
    await drain(hass, store, api.ArchiveProjectionWorker(hass, store))
    sid = api.statistic_id("sleep_rem_hours", "person-1")
    assert [(r["state"], r["sum"]) for r in await rows(hass, recorder_mock, sid)] == [
        (1, 1),
        (0.5, 1.5),
    ]
    meta = await recorder_mock.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={sid})
    )
    assert meta[sid][1]["unit_class"] == "duration"
    assert meta[sid][1]["unit_of_measurement"] == "h"
    assert (
        await rows(hass, recorder_mock, api.statistic_id("sleep_details", "person-1"))
        == []
    )


async def test_retry_exhaustion_requires_explicit_retry(
    recorder_mock, api, hass, store, monkeypatch, projection_clock
):
    worker = api.ArchiveProjectionWorker(hass, store)
    await commit(hass, store, payload())
    original = api.async_add_external_statistics

    def unavailable(*args):
        raise RuntimeError("Sensitive exception details must not reach status")

    monkeypatch.setattr(api, "async_add_external_statistics", unavailable)
    for _ in range(5):
        job = (
            await hass.async_add_executor_job(
                lambda: store.claim_projection_jobs(1, retry_failed=False)
            )
        )[0]
        await worker.async_rebuild(job)
        assert not await hass.async_add_executor_job(
            lambda: store.claim_projection_jobs(1, retry_failed=False)
        )
        projection_clock.tick(301)
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ] == ("failed", "statistics_unavailable")
    monkeypatch.setattr(api, "async_add_external_statistics", original)
    await worker.async_retry("person-1")
    await drain(hass, store, worker)
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ] == ("current", None)


async def test_new_archive_correction_during_readback_stays_pending(
    recorder_mock, api, hass, store, monkeypatch
):
    data = payload(HEART)
    await commit(hass, store, data)
    worker = api.ArchiveProjectionWorker(hass, store)
    original = worker._write_verified

    async def correction(*args):
        await original(*args)
        data["samples"][0]["payload"].update(raw_value=85, canonical_value=85)
        await commit(hass, store, data, "racing-correction")

    monkeypatch.setattr(worker, "_write_verified", correction)
    job = (await hass.async_add_executor_job(store.claim_projection_jobs, 1))[0]
    await worker.async_rebuild(job)
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        HEART
    ][0] == "pending"
    monkeypatch.setattr(worker, "_write_verified", original)
    await drain(hass, store, worker)
    assert (
        await rows(hass, recorder_mock, api.statistic_id("heart_rate", "person-1"))
    )[0]["mean"] == 85


async def test_additive_tail_coalesces_jobs_and_repairs_later_empty_hour(
    recorder_mock, api, hass, store
):
    data = payload()
    extra = deepcopy(data["samples"][0])
    extra.update(
        uuid="bd085ccc-22f4-4e80-a865-149bb5b0d1d5",
        start="2024-01-01T11:00:00Z",
        end="2024-01-01T11:00:01Z",
    )
    data["samples"].append(extra)
    await commit(hass, store, data)
    worker = api.ArchiveProjectionWorker(hass, store)
    job = (await hass.async_add_executor_job(store.claim_projection_jobs, 1))[0]
    await worker.async_rebuild(job)
    assert not await hass.async_add_executor_job(store.claim_projection_jobs, 1)
    data["samples"] = data["samples"][:1]
    data["samples"][0]["payload"].update(raw_value=3, canonical_value=3)
    data["deletions"] = [extra["uuid"]]
    await commit(hass, store, data, "correct-and-delete")
    await drain(hass, store, worker)
    assert [
        (r["state"], r["sum"])
        for r in await rows(hass, recorder_mock, api.statistic_id("steps", "person-1"))
    ] == [(3, 3)]


async def test_source_pagination_and_chunked_replay_cross_multiple_years(
    recorder_mock, api, hass, store
):
    data = payload()
    template = data["samples"][0]
    points = []
    for index in range(510):
        point = deepcopy(template)
        start = HOUR + timedelta(hours=index // 3)
        point.update(
            uuid=f"bd085ccc-22f4-4e80-a865-{index:012x}",
            start=start.isoformat().replace("+00:00", "Z"),
            end=start.isoformat().replace("+00:00", "Z"),
        )
        point["payload"].update(raw_value=1, canonical_value=1)
        points.append(point)
    for offset in range(0, 510, 200):
        data["samples"] = points[offset : offset + 200]
        await commit(hass, store, data, f"page-{offset}")
    worker = api.ArchiveProjectionWorker(hass, store)
    job = (await hass.async_add_executor_job(store.claim_projection_jobs, 1))[0]
    await worker.async_rebuild(job)
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ] == ("current", None)
    sid = api.statistic_id("steps", "person-1")
    actual = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        HOUR,
        HOUR + timedelta(hours=170),
        {sid},
        "hour",
        None,
        {"sum", "state"},
    )
    assert len(actual[sid]) == 170
    assert actual[sid][-1]["sum"] == 510
    earlier = deepcopy(template)
    earlier.update(start="2020-01-01T10:00:00Z", end="2020-01-01T10:00:01Z")
    data["samples"] = [earlier]
    await commit(hass, store, data, "older-history")
    await drain(hass, store, worker)
    actual = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        HOUR,
        HOUR + timedelta(hours=170),
        {sid},
        "hour",
        None,
        {"sum", "state"},
    )
    assert actual[sid][0]["sum"] == 15
    assert actual[sid][-1]["sum"] == 522


async def test_missing_statistics_api_keeps_health_entry_available(
    hass, enable_custom_integrations, monkeypatch
):
    import builtins
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    original = builtins.__import__

    def without_statistics(name, *args, **kwargs):
        if name == "archive_projection":
            raise ImportError("Statistics API unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_statistics)
    entry = MockConfigEntry(
        domain="health_bridge",
        data={
            "app_type": "health_assistant_link",
            "token": "compatibility-api-token-00000001",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert hass.data["health_bridge"]["archive_store"] is not None
    assert "archive_projection_worker" not in hass.data["health_bridge"]


async def test_all_statistics_reads_are_bounded_including_empty_hour_repair(
    recorder_mock, api, hass, store, monkeypatch
):
    original = api.statistics_during_period
    ranges = []

    def bounded(hass, start, end, *args):
        ranges.append((start, end))
        assert end - start <= timedelta(hours=168), "Unbounded statistics read"
        return original(hass, start, end, *args)

    monkeypatch.setattr(api, "statistics_during_period", bounded)
    data = payload()
    early = deepcopy(data["samples"][0])
    early.update(
        uuid="bd085ccc-22f4-4e80-a865-149bb5b0d1d5",
        start="2020-01-01T10:00:00Z",
        end="2020-01-01T10:00:01Z",
    )
    data["samples"].append(early)
    await commit(hass, store, data)
    worker = api.ArchiveProjectionWorker(hass, store)
    await drain(hass, store, worker)
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ] == ("current", None)
    data["deletions"] = [early["uuid"]]
    data["samples"] = []
    await commit(hass, store, data, "delete-oldest")
    await drain(hass, store, worker)
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ] == ("current", None)
    assert ranges and all(end - start <= timedelta(hours=168) for start, end in ranges)
    assert [
        (r["state"], r["sum"])
        for r in await rows(hass, recorder_mock, api.statistic_id("steps", "person-1"))
    ] == [(12, 12)]


async def test_status_reconciles_disappeared_statistics_before_current(
    recorder_mock, api, hass, store
):
    from custom_components.health_bridge.archive_webhook import (
        async_handle_archive_request,
    )

    worker = api.ArchiveProjectionWorker(hass, store)
    hass.data["health_bridge"] = {"archive_projection_worker": worker}
    data = payload(HEART)
    await commit(hass, store, data)
    await drain(hass, store, worker)
    request = {
        "request_type": "archive_status",
        "protocol_version": 2,
        "request_id": "status",
        "user_id": "person-1",
    }
    response = await async_handle_archive_request(hass, request, store)
    assert json.loads(response.body)["metrics"][0]["state"] == "current"
    sid = api.statistic_id("heart_rate", "person-1")
    recorder_mock.async_clear_statistics([sid])
    await recorder_mock.async_block_till_done()
    # A no-op archive receipt is not proof that recorder still has its rows.
    no_op = deepcopy(data)
    no_op.update(batch_id="no-op-after-clear", request_id="no-op-after-clear")
    receipt = await async_handle_archive_request(hass, no_op, store)
    assert json.loads(receipt.body)["projection_state"] == "pending"
    response = await async_handle_archive_request(hass, request, store)
    assert json.loads(response.body)["metrics"][0]["state"] == "pending"
    await drain(hass, store, api.ArchiveProjectionWorker(hass, store))
    response = await async_handle_archive_request(hass, request, store)
    assert json.loads(response.body)["metrics"][0]["state"] == "current"
    assert (await rows(hass, recorder_mock, sid))[0]["mean"] == 12


async def test_interior_deleted_hour_is_repaired_after_sparse_tail_upsert(
    recorder_mock, api, hass, store
):
    data = payload()
    template = data["samples"][0]
    points = []
    for index in range(3):
        point = deepcopy(template)
        point.update(
            uuid=f"bd085ccc-22f4-4e80-a865-{index:012x}",
            start=f"2024-01-01T{10 + index}:00:00Z",
            end=f"2024-01-01T{10 + index}:00:01Z",
        )
        points.append(point)
    data["samples"] = points
    await commit(hass, store, data)
    worker = api.ArchiveProjectionWorker(hass, store)
    await drain(hass, store, worker)
    data["samples"] = points[:1]
    data["samples"][0]["payload"].update(raw_value=3, canonical_value=3)
    data["deletions"] = [points[1]["uuid"]]
    await commit(hass, store, data, "delete-middle")
    await drain(hass, store, worker)
    assert (await hass.async_add_executor_job(store.projection_status, "person-1"))[
        STEP
    ] == ("current", None)
    assert [
        (r["state"], r["sum"])
        for r in await rows(hass, recorder_mock, api.statistic_id("steps", "person-1"))
    ] == [(3, 3), (12, 15)]


async def test_partial_recorder_loss_is_reconciled_without_clearing_survivors(
    recorder_mock, api, hass, store, monkeypatch
):
    from homeassistant.components.recorder.db_schema import Statistics

    data = payload(HEART)
    later = deepcopy(data["samples"][0])
    later.update(
        uuid="bd085ccc-22f4-4e80-a865-149bb5b0d1d5",
        start="2024-01-01T11:00:00Z",
        end="2024-01-01T11:00:01Z",
    )
    data["samples"].append(later)
    await commit(hass, store, data)
    worker = api.ArchiveProjectionWorker(hass, store)
    await drain(hass, store, worker)
    sid = api.statistic_id("heart_rate", "person-1")
    meta = await recorder_mock.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={sid})
    )

    def simulate_partial_restore():
        with recorder_mock.get_session() as session:
            session.query(Statistics).filter(
                Statistics.metadata_id == meta[sid][0],
                Statistics.start_ts == HOUR.timestamp(),
            ).delete()
            session.commit()

    await recorder_mock.async_add_executor_job(simulate_partial_restore)
    assert len(await rows(hass, recorder_mock, sid)) == 1
    assert (await worker.async_projection_status("person-1"))[HEART][0] == "pending"

    def forbidden(*args, **kwargs):
        raise AssertionError("Missing rows require replay, not a series clear")

    monkeypatch.setattr(recorder_mock, "async_clear_statistics", forbidden)
    await drain(hass, store, api.ArchiveProjectionWorker(hass, store))
    assert (await worker.async_projection_status("person-1"))[HEART] == (
        "current",
        None,
    )
    assert [r["mean"] for r in await rows(hass, recorder_mock, sid)] == [12, 12]


@pytest.mark.parametrize(
    "sample_type,metric,stale",
    [
        (HEART, "heart_rate", {"mean": 7, "min": 7, "max": 7}),
        (STEP, "steps", {"state": 0, "sum": 0}),
    ],
)
async def test_restored_stale_prefix_is_removed_before_current(
    recorder_mock, api, hass, store, sample_type, metric, stale
):
    data = payload(sample_type)
    await commit(hass, store, data)
    worker = api.ArchiveProjectionWorker(hass, store)
    await drain(hass, store, worker)
    sid = api.statistic_id(metric, "person-1")
    meta = await recorder_mock.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={sid})
    )
    old_hour = HOUR.replace(year=2020)
    api.async_add_external_statistics(
        hass, meta[sid][1], [{"start": old_hour, **stale}]
    )
    await recorder_mock.async_block_till_done()
    assert (await worker.async_projection_status("person-1"))[sample_type][
        0
    ] == "pending"
    await drain(hass, store, worker)
    assert (await worker.async_projection_status("person-1"))[sample_type] == (
        "current",
        None,
    )
    old_rows = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        old_hour,
        old_hour + timedelta(hours=1),
        {sid},
        "hour",
        None,
        {"mean"},
    )
    assert not old_rows.get(sid)
