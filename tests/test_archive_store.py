"""Durability and recovery contracts against real SQLite files."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import importlib
import json
from pathlib import Path
import sqlite3

import pytest

from custom_components.health_bridge.archive_protocol import (
    ArchiveLimits,
    ArchiveProtocolError,
    validate_archive_request,
)


TYPE = "HKQuantityTypeIdentifierStepCount"
UUID = "bd085ccc-22f4-4e80-a865-149bb5b0d1d4"
UUID2 = "bd085ccc-22f4-4e80-a865-149bb5b0d1d5"


@pytest.fixture
def api():
    name = "custom_components.health_bridge.archive_store"
    assert importlib.util.find_spec(name) is not None, (
        "Archive store is not implemented"
    )
    return importlib.import_module(name)


@pytest.fixture
def payload():
    return json.loads(Path("docs/protocol/fixtures/archive-batch-v2.json").read_text())


@pytest.fixture
def store(api, tmp_path):
    return api.ArchiveStore.open(tmp_path / "archive.sqlite3")


def batch(payload, batch_id=None):
    value = deepcopy(payload)
    if batch_id:
        value["batch_id"] = batch_id
        value["request_id"] = "request-" + batch_id
    return validate_archive_request(value, limits=ArchiveLimits())


def date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def query(api, **kwargs):
    return api.ArchiveQuery(
        user_id=kwargs.pop("user_id", "person-1"),
        sample_type=TYPE,
        start=date("2024-01-01T00:00:00Z"),
        end=date("2024-01-02T00:00:00Z"),
        **kwargs,
    )


def test_schema_is_versioned_indexed_and_wal_safe(api, tmp_path):
    path = tmp_path / "archive.sqlite3"
    api.ArchiveStore.open(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "samples",
            "tombstones",
            "receipts",
            "coverage_intervals",
            "projection_jobs",
        } <= tables
        assert db.execute("PRAGMA foreign_key_list(coverage_intervals)").fetchall()
        assert db.execute("PRAGMA index_list(samples)").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    api.ArchiveStore.open(path)


def test_newer_schema_is_rejected_without_modification(api, tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=2")
    before = path.read_bytes()
    with pytest.raises(api.ArchiveStoreError, match="unsupported_schema"):
        api.ArchiveStore.open(path)
    assert path.read_bytes() == before


def test_schema_upgrade_while_waiting_for_migration_lock_is_rejected(
    api, tmp_path, monkeypatch
):
    path = tmp_path / "archive.sqlite3"
    api.ArchiveStore.open(path)
    connect = sqlite3.connect

    class UpgradeBeforeLock(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE":
                # Another process can upgrade after the initial unlocked read.
                other = connect(path)
                try:
                    other.execute("PRAGMA user_version=2")
                finally:
                    other.close()
            return super().execute(sql, parameters)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: connect(*args, **kwargs, factory=UpgradeBeforeLock),
    )
    with pytest.raises(api.ArchiveStoreError, match="unsupported_schema"):
        api.ArchiveStore.open(path)


def test_valid_near_transport_limit_survives_normalized_storage(api, store, payload):
    template = payload["samples"][0]
    payload["samples"] = []
    for index in range(200):
        sample = deepcopy(template)
        sample["uuid"] = f"bd085ccc-22f4-4e80-a865-{index:012x}"
        sample["metadata"] = {"padding": ""}
        payload["samples"].append(sample)
    encoded_size = len(json.dumps(payload, separators=(",", ":")).encode())
    per_sample, remaining = divmod(262_140 - encoded_size, 200)
    assert per_sample > 0
    for index, sample in enumerate(payload["samples"]):
        sample["metadata"]["padding"] = "x" * (per_sample + (index < remaining))
    assert len(json.dumps(payload, separators=(",", ":")).encode()) == 262_140
    value = validate_archive_request(payload, limits=ArchiveLimits())
    receipt = store.commit_batch(value)
    assert receipt.committed_samples == 200
    assert len(store.query_samples(query(api)).samples) == 200
    assert store.commit_batch(value) == receipt


def test_identical_retry_returns_original_receipt_after_restart(
    api, store, payload, tmp_path
):
    receipt = store.commit_batch(batch(payload))
    assert receipt.committed_samples == 1
    assert receipt.projection_state == "pending"
    reopened = api.ArchiveStore.open(tmp_path / "archive.sqlite3")
    assert reopened.commit_batch(batch(payload)) == receipt
    assert len(reopened.query_samples(query(api)).samples) == 1
    assert len(reopened.claim_projection_jobs(10)) == 1
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM coverage_intervals").fetchone()[0] == 1


def test_batch_id_reuse_with_changed_content_is_rejected(api, store, payload):
    store.commit_batch(batch(payload))
    payload["samples"][0]["payload"]["raw_value"] = 99
    with pytest.raises(api.ArchiveStoreError, match="batch_conflict"):
        store.commit_batch(batch(payload))
    assert store.query_samples(query(api)).samples[0].payload.raw_value == 12


def test_distinct_ids_at_same_instant_and_users_are_preserved(api, store, payload):
    second = deepcopy(payload["samples"][0])
    second["uuid"] = UUID2
    payload["samples"].append(second)
    assert store.commit_batch(batch(payload)).committed_samples == 2
    payload["user_id"] = "person-2"
    assert store.commit_batch(batch(payload)).committed_samples == 2
    assert len(store.query_samples(query(api)).samples) == 2
    assert len(store.query_samples(query(api, user_id="person-2")).samples) == 2


def test_identical_sample_in_new_batch_does_not_queue_work(api, store, payload):
    store.commit_batch(batch(payload))
    job = store.claim_projection_jobs(1)[0]
    store.complete_projection_job(job.job_id)
    receipt = store.commit_batch(batch(payload, "again"))
    assert receipt.committed_samples == 0
    assert store.claim_projection_jobs(1) == ()


def test_correction_requeues_only_old_and_new_overlapping_hours(api, store, payload):
    store.commit_batch(batch(payload))
    for job in store.claim_projection_jobs(20):
        store.complete_projection_job(job.job_id)
    payload["samples"][0]["start"] = "2024-01-01T12:30:00Z"
    payload["samples"][0]["end"] = "2024-01-01T14:00:00Z"
    payload["samples"][0]["payload"]["canonical_value"] = 21
    assert store.commit_batch(batch(payload, "correction")).committed_samples == 1
    jobs = store.claim_projection_jobs(20)
    assert {j.hour_start for j in jobs} == {
        date("2024-01-01T10:00:00Z"),
        date("2024-01-01T12:00:00Z"),
        date("2024-01-01T13:00:00Z"),
    }
    assert len(store.query_samples(query(api)).samples) == 1
    assert store.query_samples(query(api)).samples[0].payload.canonical_value == 21


def test_tombstone_removes_sample_repairs_hour_and_blocks_stale_rescan(
    api, store, payload, tmp_path
):
    original = batch(payload)
    store.commit_batch(original)
    store.complete_projection_job(store.claim_projection_jobs(1)[0].job_id)
    payload["samples"] = []
    payload["deletions"] = [UUID, UUID2]
    receipt = store.commit_batch(batch(payload, "delete"))
    assert receipt.committed_deletions == 2
    assert store.query_samples(query(api)).samples == ()
    jobs = store.claim_projection_jobs(20)
    assert [j.hour_start for j in jobs] == [date("2024-01-01T10:00:00Z")]
    store.commit_batch(replace(original, batch_id="stale", request_id="stale"))
    assert store.query_samples(query(api)).samples == ()
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 2


def test_interval_and_anchor_coverage_keep_authorization_and_receipt(
    store, payload, tmp_path
):
    store.commit_batch(batch(payload))
    payload["coverage"] = {
        "kind": "anchor",
        "anchor": "position-2",
        "authorization_start": None,
    }
    store.commit_batch(batch(payload, "anchor"))
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        rows = db.execute(
            "SELECT kind, start, end, anchor, authorization_start, batch_id FROM coverage_intervals ORDER BY rowid"
        ).fetchall()
        assert rows == [
            (
                "interval",
                "2024-01-01T00:00:00.000000Z",
                "2024-01-02T00:00:00.000000Z",
                None,
                "2023-01-01T00:00:00.000000Z",
                "batch-001",
            ),
            ("anchor", None, None, "position-2", None, "anchor"),
        ]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_malformed_dataclass_never_partially_commits(api, store, payload, tmp_path):
    valid = batch(payload)
    invalid = replace(valid.samples[0], uuid=UUID2, metadata_json="not-json")
    with pytest.raises(ArchiveProtocolError):
        store.commit_batch(replace(valid, samples=(*valid.samples, invalid)))
    assert store.query_samples(query(api)).samples == ()
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        for table in ("samples", "receipts", "coverage_intervals", "projection_jobs"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_storage_failure_rolls_back_receipt_samples_coverage_and_jobs(
    api, store, payload, tmp_path
):
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_jobs BEFORE INSERT ON projection_jobs BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_batch(batch(payload))
    assert store.query_samples(query(api)).samples == ()
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        for table in ("receipts", "coverage_intervals", "projection_jobs"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_jobs")
    assert store.commit_batch(batch(payload)).committed_samples == 1


def test_query_overlap_half_open_boundaries_and_pagination(api, store, payload):
    samples = []
    for uuid, start, end in (
        (UUID, "2023-12-31T23:00:00Z", "2024-01-01T01:00:00Z"),
        (UUID2, "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"),
        (
            "bd085ccc-22f4-4e80-a865-149bb5b0d1d6",
            "2023-12-31T23:00:00Z",
            "2024-01-01T00:00:00Z",
        ),
        (
            "bd085ccc-22f4-4e80-a865-149bb5b0d1d7",
            "2024-01-02T00:00:00Z",
            "2024-01-02T00:00:00Z",
        ),
    ):
        sample = deepcopy(payload["samples"][0])
        sample.update(uuid=uuid, start=start, end=end)
        samples.append(sample)
    payload["samples"] = samples
    store.commit_batch(batch(payload))
    first = store.query_samples(query(api, limit=1))
    assert [s.uuid for s in first.samples] == [UUID]
    assert first.next_cursor is not None
    second = store.query_samples(query(api, limit=1, cursor=first.next_cursor))
    assert [s.uuid for s in second.samples] == [UUID2]
    assert second.next_cursor is None
    with pytest.raises(api.ArchiveStoreError, match="invalid_cursor"):
        store.query_samples(query(api, user_id="person-2", cursor=first.next_cursor))


@pytest.mark.parametrize("limit", [0, -1, 501, True])
def test_query_limit_is_bounded(api, store, limit):
    with pytest.raises(api.ArchiveStoreError, match="invalid_query"):
        store.query_samples(query(api, limit=limit))


def test_claim_completion_failure_and_expired_restart_recovery(
    api, store, payload, tmp_path, freezer
):
    store.commit_batch(batch(payload))
    claimed = store.claim_projection_jobs(10)
    assert len(claimed) == 1
    assert store.claim_projection_jobs(10) == ()
    reopened = api.ArchiveStore.open(tmp_path / "archive.sqlite3")
    assert reopened.claim_projection_jobs(10) == ()
    freezer.tick(301)
    retried = reopened.claim_projection_jobs(10)
    assert len(retried) == 1
    assert retried[0].job_id != claimed[0].job_id
    store.complete_projection_job(claimed[0].job_id)
    reopened.fail_projection_job(retried[0].job_id, "recorder_unavailable")
    again = reopened.claim_projection_jobs(10)
    assert again[0].last_error == "recorder_unavailable"
    reopened.complete_projection_job(again[0].job_id)
    assert reopened.claim_projection_jobs(10) == ()


def test_inflight_correction_cannot_be_lost_by_stale_completion(api, store, payload):
    store.commit_batch(batch(payload))
    old = store.claim_projection_jobs(1)[0]
    payload["samples"][0]["payload"]["canonical_value"] = 30
    store.commit_batch(batch(payload, "new"))
    store.complete_projection_job(old.job_id)
    store.fail_projection_job(old.job_id, "late_error")
    jobs = store.claim_projection_jobs(10)
    assert len(jobs) == 1
    assert jobs[0].job_id != old.job_id
    assert jobs[0].last_error is None


def test_delete_archive_is_strictly_user_scoped(api, store, payload, tmp_path):
    store.commit_batch(batch(payload))
    payload["user_id"] = "person-2"
    store.commit_batch(batch(payload))
    store.delete_user_archive("person-1")
    assert store.query_samples(query(api)).samples == ()
    assert len(store.query_samples(query(api, user_id="person-2")).samples) == 1
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        for table in (
            "samples",
            "tombstones",
            "receipts",
            "coverage_intervals",
            "projection_jobs",
        ):
            assert (
                db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE user_id='person-1'"
                ).fetchone()[0]
                == 0
            )
    assert {j.user_id for j in store.claim_projection_jobs(10)} == {"person-2"}


def test_parallel_executor_retries_commit_exactly_once(api, store, payload):
    value = batch(payload)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(store.commit_batch, [value] * 8))
    assert all(r == receipts[0] for r in receipts)
    assert len(store.query_samples(query(api)).samples) == 1
    assert len(store.claim_projection_jobs(10)) == 1


async def test_event_loop_access_is_rejected_before_sqlite(api, tmp_path):
    with pytest.raises(api.ArchiveStoreError, match="executor_required"):
        api.ArchiveStore.open(tmp_path / "archive.sqlite3")
    assert not (tmp_path / "archive.sqlite3").exists()


@pytest.mark.parametrize(
    "sample_type,typed_payload",
    [
        (
            "HKCategoryTypeIdentifierSleepAnalysis",
            {"kind": "category", "schema_version": 1, "value": 3},
        ),
        (
            "HKWorkoutTypeIdentifier",
            {
                "kind": "workout",
                "schema_version": 1,
                "activity_type": "walking",
                "duration_seconds": 60,
                "total_energy": {"value": 4.5, "unit": "kcal"},
                "total_distance": {"value": 50, "unit": "m"},
                "detail": {"indoor": True},
            },
        ),
    ],
)
def test_typed_payload_and_provenance_survive_reopen(
    api, store, payload, tmp_path, sample_type, typed_payload
):
    payload["sample_type"] = sample_type
    payload["samples"][0]["payload"] = typed_payload
    payload["samples"][0]["device"] = {"manufacturer": "Example", "model": "Watch"}
    value = batch(payload)
    store.commit_batch(value)
    reopened = api.ArchiveStore.open(tmp_path / "archive.sqlite3")
    result = reopened.query_samples(replace(query(api), sample_type=sample_type))
    assert result.samples == value.samples


def test_final_coverage_failure_rolls_back_deletion_and_receipt(
    api, store, payload, tmp_path
):
    store.commit_batch(batch(payload))
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_coverage BEFORE INSERT ON coverage_intervals BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    payload["samples"] = []
    payload["deletions"] = [UUID]
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_batch(batch(payload, "delete"))
    assert [s.uuid for s in store.query_samples(query(api)).samples] == [UUID]
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM coverage_intervals").fetchone()[0] == 1


async def test_all_store_operations_can_run_in_executor_only(api, tmp_path, payload):
    import asyncio

    store = await asyncio.to_thread(api.ArchiveStore.open, tmp_path / "archive.sqlite3")
    value = batch(payload)
    for operation, args in (
        (store.commit_batch, (value,)),
        (store.query_samples, (query(api),)),
        (store.claim_projection_jobs, (1,)),
        (store.complete_projection_job, ("job-id",)),
        (store.fail_projection_job, ("job-id", "error")),
        (store.delete_user_archive, ("person-1",)),
    ):
        with pytest.raises(api.ArchiveStoreError, match="executor_required"):
            operation(*args)
    await asyncio.to_thread(store.commit_batch, value)
    page = await asyncio.to_thread(store.query_samples, query(api))
    assert len(page.samples) == 1
