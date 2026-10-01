"""An approved device is the sole writer of a user's original archive."""

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from custom_components.health_bridge.archive_protocol import (
    ArchiveInventoryQuery,
    ArchiveLimits,
    validate_archive_request,
)
from custom_components.health_bridge.archive_store import (
    ArchiveQuery,
    ArchiveStore,
    ArchiveStoreError,
)


NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
SECRET_A = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
SECRET_B = base64.urlsafe_b64encode(bytes(range(32, 64))).decode().rstrip("=")
SAMPLE_TYPE = "HKQuantityTypeIdentifierStepCount"


@pytest.fixture
def store(tmp_path):
    return ArchiveStore.open(tmp_path / "owner.sqlite")


def batch(*, batch_id="first", samples=None, deletions=None, revision=None):
    payload = json.loads(Path("docs/protocol/fixtures/archive-batch-v2.json").read_text())
    payload["batch_id"] = batch_id
    payload["request_id"] = batch_id
    if samples is not None:
        payload["samples"] = samples
    if deletions is not None:
        payload["deletions"] = deletions
    if revision is not None:
        payload["expected_inventory_revision"] = revision
        payload["expected_owner_generation"] = 1
    return validate_archive_request(payload, limits=ArchiveLimits())


def inventory():
    return ArchiveInventoryQuery(
        "person-1", SAMPLE_TYPE,
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
    )


def approve(store, secret):
    claim = store.claim_owner("person-1", secret, NOW)
    return store.approve_owner("person-1", claim.claim_id, NOW)


def test_pending_owner_claim_inventory_only_lists_live_claims(store):
    first = store.claim_owner("person-1", SECRET_A, NOW)
    store.claim_owner("person-2", SECRET_B, NOW - timedelta(days=2))

    claims = store.list_pending_owner_claims(NOW)

    assert [(item.user_id, item.claim.claim_id, item.claim.fingerprint,
             item.owner_generation) for item in claims] == [
        ("person-1", first.claim_id, first.fingerprint, 0)
    ]


def test_pending_owner_claim_inventory_marks_transfer(store):
    approve(store, SECRET_A)
    replacement = store.claim_owner("person-1", SECRET_B, NOW)

    claims = store.list_pending_owner_claims(NOW)

    assert [(item.user_id, item.claim.claim_id, item.owner_generation)
            for item in claims] == [("person-1", replacement.claim_id, 1)]


class OwnedStore:
    """Give legacy store tests an approved synthetic phone without changing their calls."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def _ensure_owner(self, user_id):
        if self._store.owner_status(user_id, SECRET_A, NOW).state == "unbound":
            claim = self._store.claim_owner(user_id, SECRET_A, NOW)
            self._store.approve_owner(user_id, claim.claim_id, NOW)

    def commit_batch(self, value):
        self._ensure_owner(value.user_id)
        return self._store.commit_batch(value, SECRET_A)

    def inventory_page(self, value):
        self._ensure_owner(value.user_id)
        return self._store.inventory_page(value, SECRET_A)


def counts(store):
    with sqlite3.connect(store._path) as db:
        return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("samples", "tombstones", "receipts", "coverage_intervals", "projection_jobs"))


def downgrade_receipt_tables(db):
    """Model the deployed pre-generation receipt/coverage identity."""
    db.execute("ALTER TABLE coverage_intervals RENAME TO coverage_v3")
    db.execute("ALTER TABLE receipts RENAME TO receipts_v3")
    db.execute("""CREATE TABLE receipts (
        user_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
        PRIMARY KEY (user_id, batch_id))""")
    db.execute("""INSERT INTO receipts
        SELECT user_id, batch_id, payload_hash, receipt_json FROM receipts_v3""")
    db.execute("""CREATE TABLE coverage_intervals (
        user_id TEXT NOT NULL, sample_type TEXT NOT NULL, batch_id TEXT NOT NULL,
        kind TEXT NOT NULL, start TEXT, end TEXT, anchor TEXT,
        authorization_start TEXT, PRIMARY KEY (user_id, batch_id),
        FOREIGN KEY (user_id, batch_id) REFERENCES receipts(user_id, batch_id)
            ON DELETE CASCADE)""")
    db.execute("""INSERT INTO coverage_intervals
        SELECT user_id, sample_type, batch_id, kind, start, end, anchor,
        authorization_start FROM coverage_v3""")
    db.execute("DROP TABLE coverage_v3")
    db.execute("DROP TABLE receipts_v3")


def test_unapproved_owner_cannot_write_or_inventory(store):
    original = counts(store)
    with pytest.raises(ArchiveStoreError, match="owner_required"):
        store.commit_batch(batch(), SECRET_A)
    with pytest.raises(ArchiveStoreError, match="owner_required"):
        store.inventory_page(inventory(), SECRET_A)
    assert counts(store) == original
    approve(store, SECRET_A)
    with pytest.raises(ArchiveStoreError, match="owner_changed"):
        store.commit_batch(batch(), SECRET_B)
    with pytest.raises(ArchiveStoreError, match="owner_changed"):
        store.inventory_page(inventory(), SECRET_B)
    assert counts(store) == original


def test_claim_conflict_expiry_and_idempotence(store):
    first = store.claim_owner("person-1", SECRET_A, NOW)
    assert first.fingerprint and len(first.fingerprint) == 12
    assert first.expires_at == NOW + timedelta(hours=24)
    assert store.claim_owner("person-1", SECRET_A, NOW + timedelta(hours=1)) == first
    with pytest.raises(ArchiveStoreError, match="claim_conflict"):
        store.claim_owner("person-1", SECRET_B, NOW + timedelta(hours=1))
    assert store.pending_owner_claim("person-1", NOW + timedelta(hours=1)) == first
    assert store.owner_status("person-1", SECRET_A, NOW).state == "pending"
    assert store.owner_status("person-1", SECRET_B, NOW).state == "unbound"
    second = store.claim_owner("person-1", SECRET_B, NOW + timedelta(hours=24))
    assert second.claim_id != first.claim_id
    assert store.pending_owner_claim("person-1", NOW + timedelta(hours=24)) == second


def test_transfer_revokes_old_owner_and_preserves_old_rows(store):
    assert approve(store, SECRET_A).generation == 1
    first = batch()
    receipt = store.commit_batch(first, SECRET_A)
    sample_id = first.samples[0].uuid
    detail = store.sample_detail("person-1", SAMPLE_TYPE, sample_id)
    before = counts(store)
    assert approve(store, SECRET_B).generation == 2
    page = store.inventory_page(inventory(), SECRET_B)
    assert page.sample_ids == ()
    assert page.owner_generation == 2
    with pytest.raises(ArchiveStoreError, match="owner_changed"):
        store.commit_batch(first, SECRET_A)
    assert store.sample_detail("person-1", SAMPLE_TYPE, sample_id) == detail
    assert counts(store) == before
    with sqlite3.connect(store._path) as db:
        assert db.execute("SELECT owner_generation FROM samples").fetchone() == (1,)
    assert receipt.committed_samples == 1


def test_direct_deletion_before_reupload_preserves_old_original(store):
    approve(store, SECRET_A)
    first = batch()
    store.commit_batch(first, SECRET_A)
    sample_id = first.samples[0].uuid
    approve(store, SECRET_B)
    deleted = store.commit_batch(batch(batch_id="delete", samples=[], deletions=[sample_id]), SECRET_B)
    assert deleted.committed_deletions == 1
    assert store.sample_detail("person-1", SAMPLE_TYPE, sample_id) is not None
    assert store.inventory_page(inventory(), SECRET_B).sample_ids == ()
    with sqlite3.connect(store._path) as db:
        assert db.execute("SELECT owner_generation FROM tombstones").fetchone() == (2,)


def test_reupload_then_delete_changes_projection_once(store):
    approve(store, SECRET_A)
    first = batch()
    store.commit_batch(first, SECRET_A)
    approve(store, SECRET_B)
    with sqlite3.connect(store._path) as db:
        original_count = db.execute("SELECT count(*) FROM samples").fetchone()[0]
    uploaded = store.commit_batch(batch(batch_id="reupload"), SECRET_B)
    assert uploaded.committed_samples == 0
    assert store.inventory_page(inventory(), SECRET_B).sample_ids == (first.samples[0].uuid,)
    with sqlite3.connect(store._path) as db:
        assert db.execute("SELECT count(*) FROM samples").fetchone()[0] == original_count
        assert db.execute("SELECT owner_generation FROM samples").fetchone() == (2,)
    deleted = store.commit_batch(batch(batch_id="delete", samples=[], deletions=[first.samples[0].uuid]), SECRET_B)
    assert deleted.committed_deletions == 1
    assert store.sample_detail("person-1", SAMPLE_TYPE, first.samples[0].uuid) is None


def test_new_generation_can_reupload_after_prior_generation_tombstone(store):
    approve(store, SECRET_A)
    first = batch()
    sample_id = first.samples[0].uuid
    store.commit_batch(first, SECRET_A)
    store.commit_batch(batch(batch_id="old-delete", samples=[], deletions=[sample_id]), SECRET_A)
    approve(store, SECRET_B)
    receipt = store.commit_batch(batch(batch_id="new-upload"), SECRET_B)
    assert receipt.committed_samples == 1
    assert store.inventory_page(inventory(), SECRET_B).sample_ids == (sample_id,)
    with sqlite3.connect(store._path) as db:
        assert db.execute("SELECT owner_generation FROM tombstones").fetchone() == (1,)
        assert db.execute("SELECT owner_generation FROM samples").fetchone() == (2,)


def test_tombstone_browse_keeps_keyset_stable_across_generations(store):
    approve(store, SECRET_A)
    sample_id = batch().samples[0].uuid
    store.commit_batch(batch(batch_id="old-delete", samples=[], deletions=[sample_id]), SECRET_A)
    approve(store, SECRET_B)
    store.commit_batch(batch(batch_id="new-delete", samples=[], deletions=[sample_id]), SECRET_B)
    assert store.tombstone_page("person-1", SAMPLE_TYPE, limit=1) == (
        {"uuid": sample_id, "batch_id": "new-delete"},
    )
    assert store.tombstone_page("person-1", SAMPLE_TYPE, after=sample_id, limit=1) == ()
    with sqlite3.connect(store._path) as db:
        assert db.execute("SELECT count(*) FROM tombstones").fetchone() == (2,)


def test_reject_pending_claim_keeps_active_owner(store):
    approve(store, SECRET_A)
    pending = store.claim_owner("person-1", SECRET_B, NOW)
    assert store.owner_status("person-1", SECRET_B, NOW).state == "pending"
    assert store.owner_status("person-1", SECRET_A, NOW).state == "active"
    store.reject_owner("person-1", pending.claim_id)
    assert store.pending_owner_claim("person-1", NOW) is None
    assert store.owner_status("person-1", SECRET_B, NOW).state == "not_owner"
    assert store.assert_owner("person-1", SECRET_A) == 1


def test_reject_stale_claim_id_does_not_report_success_or_drop_current_claim(store):
    pending = store.claim_owner("person-1", SECRET_A, NOW)
    with pytest.raises(ArchiveStoreError, match="claim_not_found"):
        store.reject_owner("person-1", "stale-claim-id")
    assert store.pending_owner_claim("person-1", NOW) == pending


def test_backup_copy_preserves_owner_digest_and_generation(store, tmp_path):
    approve(store, SECRET_A)
    store.commit_batch(batch(), SECRET_A)
    approve(store, SECRET_B)
    store.begin_backup()
    try:
        copied = tmp_path / "copied.sqlite"
        copied.write_bytes(store._path.read_bytes())
    finally:
        store.end_backup()
    restored = ArchiveStore.open(copied)
    assert restored.assert_owner("person-1", SECRET_B) == 2
    with pytest.raises(ArchiveStoreError, match="owner_changed"):
        restored.assert_owner("person-1", SECRET_A)
    with sqlite3.connect(copied) as db:
        row = db.execute("SELECT credential_digest FROM archive_owners").fetchone()[0]
        assert row != SECRET_B and len(row) == 64


def test_transfer_rejects_stale_inventory_batch_atomically(store):
    approve(store, SECRET_A)
    first = batch()
    store.commit_batch(first, SECRET_A)
    page = store.inventory_page(inventory(), SECRET_A)
    approve(store, SECRET_B)
    before = counts(store)
    deletion = batch(batch_id="stale", samples=[], deletions=[first.samples[0].uuid], revision=page.revision)
    with pytest.raises(ArchiveStoreError, match="owner_changed"):
        store.commit_batch(deletion, SECRET_B, expected_owner_generation=page.owner_generation)
    assert counts(store) == before
    assert store.sample_detail("person-1", SAMPLE_TYPE, first.samples[0].uuid) is not None


def test_revoked_owner_cannot_replay_receipt(store):
    approve(store, SECRET_A)
    first = batch()
    receipt = store.commit_batch(first, SECRET_A)
    assert store.commit_batch(first, SECRET_A) == receipt
    approve(store, SECRET_B)
    with pytest.raises(ArchiveStoreError, match="owner_changed"):
        store.commit_batch(first, SECRET_A)


def test_new_owner_same_batch_id_gets_new_receipt_and_coverage(store):
    approve(store, SECRET_A)
    first = batch(batch_id="shared-id")
    original_receipt = store.commit_batch(first, SECRET_A)
    assert original_receipt.committed_samples == 1
    approve(store, SECRET_B)
    new_receipt = store.commit_batch(first, SECRET_B)
    assert new_receipt.committed_samples == 0
    assert store.commit_batch(first, SECRET_B) == new_receipt
    assert store.sample_detail("person-1", SAMPLE_TYPE, first.samples[0].uuid)["owner_generation"] == 2
    with sqlite3.connect(store._path) as db:
        assert db.execute(
            "SELECT owner_generation FROM receipts WHERE batch_id='shared-id' ORDER BY owner_generation"
        ).fetchall() == [(1,), (2,)]
        assert db.execute(
            "SELECT owner_generation FROM coverage_intervals WHERE batch_id='shared-id' ORDER BY owner_generation"
        ).fetchall() == [(1,), (2,)]


def test_active_secret_cannot_claim_or_approve_itself(store):
    approve(store, SECRET_A)
    first = batch()
    store.commit_batch(first, SECRET_A)
    with pytest.raises(ArchiveStoreError, match="owner_already_active"):
        store.claim_owner("person-1", SECRET_A, NOW)
    with sqlite3.connect(store._path) as db:
        digest = db.execute("SELECT credential_digest FROM archive_owners").fetchone()[0]
        db.execute(
            "INSERT INTO archive_owner_claims VALUES (?, ?, ?, ?)",
            ("person-1", "stale-self-claim", digest, "2026-09-27T00:00:00.000000Z"),
        )
    with pytest.raises(ArchiveStoreError, match="owner_already_active"):
        store.approve_owner("person-1", "stale-self-claim", NOW)
    assert store.assert_owner("person-1", SECRET_A) == 1
    assert store.inventory_page(inventory(), SECRET_A).sample_ids == (first.samples[0].uuid,)


def test_browse_and_detail_include_generation_without_secret(store):
    approve(store, SECRET_A)
    first = batch()
    store.commit_batch(first, SECRET_A)
    detail = store.sample_detail("person-1", SAMPLE_TYPE, first.samples[0].uuid)
    assert detail["owner_generation"] == 1
    assert SECRET_A not in json.dumps(detail)
    query = ArchiveQuery("person-1", SAMPLE_TYPE, inventory().start, inventory().end)
    page, generations = store.query_samples_with_provenance(query)
    assert tuple(sample.uuid for sample in page.samples) == (first.samples[0].uuid,)
    assert generations == (1,)
    approve(store, SECRET_B)
    page, generations = store.query_samples_with_provenance(query)
    assert tuple(sample.uuid for sample in page.samples) == (first.samples[0].uuid,)
    assert generations == (1,)


def test_existing_schema_three_receipts_upgrade_without_losing_coverage(store):
    approve(store, SECRET_A)
    first = batch()
    store.commit_batch(first, SECRET_A)
    with sqlite3.connect(store._path) as db:
        downgrade_receipt_tables(db)
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
    reopened = ArchiveStore.open(store._path)
    with sqlite3.connect(store._path) as db:
        assert db.execute("SELECT owner_generation FROM receipts").fetchall() == [(0,)]
        assert db.execute("SELECT owner_generation FROM coverage_intervals").fetchall() == [(0,)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert reopened.sample_detail("person-1", SAMPLE_TYPE, first.samples[0].uuid) is not None
    assert reopened.assert_owner("person-1", SECRET_A) == 1
    ArchiveStore.open(store._path)


def test_schema_two_migration_retains_legacy_rows_and_tombstones(tmp_path):
    path = tmp_path / "old.sqlite"
    store = ArchiveStore.open(path)
    approve(store, SECRET_A)
    first = batch()
    store.commit_batch(first, SECRET_A)
    second_id = "bd085ccc-22f4-4e80-a865-149bb5b0d1d5"
    sample = json.loads(Path("docs/protocol/fixtures/archive-batch-v2.json").read_text())["samples"][0]
    store.commit_batch(batch(batch_id="second", samples=[{**sample, "uuid": second_id}]), SECRET_A)
    store.commit_batch(batch(batch_id="delete", samples=[], deletions=[second_id]), SECRET_A)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE samples DROP COLUMN owner_generation")
        db.execute("ALTER TABLE tombstones RENAME TO tombstones_v3")
        db.execute("""CREATE TABLE tombstones (
            user_id TEXT NOT NULL, sample_type TEXT NOT NULL, sample_id TEXT NOT NULL,
            batch_id TEXT NOT NULL, PRIMARY KEY (user_id, sample_type, sample_id))""")
        db.execute("""INSERT INTO tombstones
            SELECT user_id, sample_type, sample_id, batch_id FROM tombstones_v3""")
        db.execute("DROP TABLE tombstones_v3")
        downgrade_receipt_tables(db)
        db.execute("DROP TABLE archive_owners")
        db.execute("DROP TABLE archive_owner_claims")
        db.execute("PRAGMA user_version=2")
    migrated = ArchiveStore.open(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
        assert db.execute("SELECT owner_generation FROM samples").fetchone() == (0,)
        assert db.execute("SELECT owner_generation FROM tombstones").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM receipts").fetchone() == (3,)
        assert db.execute("SELECT DISTINCT owner_generation FROM receipts").fetchall() == [(0,)]
        assert db.execute("SELECT DISTINCT owner_generation FROM coverage_intervals").fetchall() == [(0,)]
    approve(migrated, SECRET_B)
    assert migrated.inventory_page(inventory(), SECRET_B).sample_ids == ()
    migrated_receipt = migrated.commit_batch(first, SECRET_B)
    assert migrated_receipt.committed_samples == 0
    assert migrated.commit_batch(first, SECRET_B) == migrated_receipt
    assert migrated.commit_batch(batch(batch_id="readable", samples=[{**sample, "uuid": second_id}]), SECRET_B).committed_samples == 1
    assert migrated.sample_detail("person-1", SAMPLE_TYPE, first.samples[0].uuid) is not None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM samples").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM receipts WHERE batch_id='first'").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM coverage_intervals WHERE batch_id='first'").fetchone() == (2,)
