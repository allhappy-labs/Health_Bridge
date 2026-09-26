"""A real backup restores originals and every durable recovery boundary."""

from copy import deepcopy
from datetime import datetime, timezone
import importlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile

import pytest

from custom_components.health_bridge.archive_protocol import (
    ArchiveLimits,
    validate_archive_request,
)
from custom_components.health_bridge.archive_store import (
    ArchiveQuery,
    ArchiveStore,
    ArchiveStoreError,
)


@pytest.fixture
def seeded(tmp_path):
    (tmp_path / ".storage").mkdir(exist_ok=True)
    path = tmp_path / ".storage/health_bridge_archive.sqlite"
    store = ArchiveStore.open(path)
    payload = json.loads(Path("docs/protocol/fixtures/archive-batch-v2.json").read_text())
    original = deepcopy(payload["samples"][0])
    deleted = deepcopy(original)
    deleted["uuid"] = "bd085ccc-22f4-4e80-a865-149bb5b0d1d5"
    payload["samples"].append(deleted)
    first = validate_archive_request(payload, limits=ArchiveLimits())
    receipt = store.commit_batch(first)
    payload.update(batch_id="backup-delete", request_id="backup-delete", samples=[],
                   deletions=[deleted["uuid"]], coverage={
                       "kind": "anchor", "anchor": "backup-anchor", "authorization_start": None,
                   })
    store.commit_batch(validate_archive_request(payload, limits=ArchiveLimits()))
    return store, path, first, receipt, original, deleted


def test_checkpoint_copy_restores_all_durable_state(seeded, tmp_path):
    store, path, first, receipt, original, deleted = seeded
    # Keep WAL mode active, with real committed frames not yet in the main file.
    keeper = sqlite3.connect(path)
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("UPDATE projection_jobs SET attempts=2, last_error='retry'")
    keeper.commit()
    wal = Path(f"{path}-wal")
    assert wal.stat().st_size > 0
    store.begin_backup()
    try:
        assert wal.stat().st_size == 0
        with pytest.raises(ArchiveStoreError, match="backup_in_progress"):
            store.commit_batch(first)
        restored_path = tmp_path / "restored.sqlite"
        shutil.copy2(path, restored_path)  # Deliberately copy no WAL or SHM.
    finally:
        store.end_backup()
        keeper.close()

    restored = ArchiveStore.open(restored_path)
    detail = restored.sample_detail("person-1", first.sample_type, original["uuid"])
    assert {key: detail[key] for key in original if key not in {"start", "end"}} == {
        key: value for key, value in original.items() if key not in {"start", "end"}
    }
    for key in ("start", "end"):
        assert datetime.fromisoformat(detail[key]) == datetime.fromisoformat(original[key])
    assert restored.sample_detail("person-1", first.sample_type, deleted["uuid"]) is None
    assert restored.tombstone_page("person-1", first.sample_type) == (
        {"uuid": deleted["uuid"], "batch_id": "backup-delete"},
    )
    assert restored.commit_batch(first) == receipt
    query = ArchiveQuery("person-1", first.sample_type,
                         datetime(2024, 1, 1, tzinfo=timezone.utc),
                         datetime(2024, 1, 2, tzinfo=timezone.utc))
    assert [sample.uuid for sample in restored.query_samples(query).samples] == [original["uuid"]]
    with sqlite3.connect(restored_path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM receipts").fetchone() == (2,)
        assert db.execute("SELECT kind, anchor FROM coverage_intervals ORDER BY rowid").fetchall() == [
            ("interval", None), ("anchor", "backup-anchor"),
        ]
        assert db.execute("SELECT state, attempts, last_error FROM projection_jobs").fetchall() == [
            ("pending", 2, "retry"),
        ]
    jobs = restored.claim_projection_jobs(10)
    assert len(jobs) == 1
    assert jobs[0].attempts == 3
    restored.complete_projection_job(jobs[0].job_id)
    assert restored.projection_status("person-1")[first.sample_type] == ("current", None)
    assert store.commit_batch(first) == receipt  # Writes resume after backup.

    stale = json.loads(Path("docs/protocol/fixtures/archive-batch-v2.json").read_text())
    stale.update(batch_id="stale-rescan", request_id="stale-rescan", samples=[deleted])
    assert restored.commit_batch(validate_archive_request(stale, limits=ArchiveLimits())).committed_samples == 0
    assert restored.sample_detail("person-1", first.sample_type, deleted["uuid"]) is None


def test_busy_checkpoint_aborts_backup_without_disabling_store(seeded):
    store, path, first, receipt, *_ = seeded
    reader = sqlite3.connect(path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM receipts").fetchall()
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE projection_jobs SET attempts=1")
        with pytest.raises(ArchiveStoreError, match="backup_checkpoint_busy"):
            store.begin_backup()
        assert store.commit_batch(first) == receipt
    finally:
        reader.close()


async def test_ha_backup_hooks_checkpoint_and_release(hass, tmp_path):
    backup = importlib.import_module("custom_components.health_bridge.backup")
    store = await hass.async_add_executor_job(ArchiveStore.open, tmp_path / "archive.sqlite")
    hass.data["health_bridge"] = {"archive_store": store}
    await backup.async_pre_backup(hass)
    with pytest.raises(ArchiveStoreError, match="backup_in_progress"):
        await hass.async_add_executor_job(store.projection_status, "person-1")
    await backup.async_post_backup(hass)
    assert await hass.async_add_executor_job(store.projection_status, "person-1") == {}


async def test_ha_discovers_backup_hooks_for_loaded_integration(bridge_entries, hass):
    assert "health_bridge" in hass.data["backup"].platforms


@pytest.mark.parametrize("include_recorder", [False, True])
async def test_real_ha_backup_engine_includes_archive(hass, seeded, tmp_path, include_recorder):
    from homeassistant.components.backup.manager import CoreBackupReaderWriter

    backup = importlib.import_module("custom_components.health_bridge.backup")
    store, _, *_ = seeded
    hass.data["health_bridge"] = {"archive_store": store}
    writer = CoreBackupReaderWriter(hass)
    await backup.async_pre_backup(hass)
    try:
        artifact, size = await hass.async_add_executor_job(
            writer._mkdir_and_generate_backup_contents,
            {"slug": "archive-test", "version": 2}, include_recorder, None, None,
        )
    finally:
        await backup.async_post_backup(hass)
    assert size > 0

    def restore():
        with tarfile.open(artifact) as outer:
            member = next(item for item in outer.getmembers() if item.name.endswith("homeassistant.tar.gz"))
            with tarfile.open(fileobj=io.BytesIO(outer.extractfile(member).read())) as inner:
                member = next(item for item in inner.getmembers() if item.name.endswith(".storage/health_bridge_archive.sqlite"))
                path = tmp_path / "from-ha-backup.sqlite"
                path.write_bytes(inner.extractfile(member).read())
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA user_version").fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM samples").fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM tombstones").fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM receipts").fetchone() == (2,)
            assert db.execute("SELECT count(*) FROM coverage_intervals").fetchone() == (2,)
            assert db.execute("SELECT state FROM projection_jobs").fetchall() == [("pending",)]

    await hass.async_add_executor_job(restore)
