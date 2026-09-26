"""Durable original samples and projection outbox, independent of recorder.

All methods are synchronous executor work (including ``open``). Connections are
private and scoped to one operation, so a store can be shared by HA executor
threads. No connection, cursor, or lazy database iterator leaves this module.
Projection claims expire after five minutes; consumers must finish a claim in
that window. A new claim or archive correction invalidates the previous job ID.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Any, Iterator
from uuid import UUID, uuid4

from .archive_protocol import (
    ARCHIVE_SCHEMA_VERSION,
    ArchiveBatch,
    ArchiveInventoryPage,
    ArchiveInventoryQuery,
    ArchiveLimits,
    ArchiveProtocolError,
    ArchiveReceipt,
    ArchiveSample,
    validate_archive_fields,
)
from .archive_owner import OwnerClaim, OwnerState, credential_digest


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HOUR = timedelta(hours=1)
_CLAIM_SECONDS = 300
MAX_SAMPLE_PROJECTION_HOURS = 8_784
MAX_BATCH_PROJECTION_HOURS = 16_384
# A durable repair intent, outside real HealthKit history. Unlike a flag on an
# ordinary hour, archive corrections cannot erase it while recorder is cleared.
FULL_REBUILD_HOUR = datetime.min.replace(tzinfo=timezone.utc)
RECONCILE_HOUR = FULL_REBUILD_HOUR + _HOUR


class ArchiveStoreError(ValueError):
    """Stable error code without sample content or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ArchiveQuery:
    """Overlap query for one user/type and half-open UTC interval.

    Pages sort by sample start then UUID. Cursors are scoped to the query;
    concurrent corrections can move rows between pages (no snapshot guarantee).
    """

    user_id: str
    sample_type: str
    start: datetime
    end: datetime
    limit: int = 200
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ArchivePage:
    samples: tuple[ArchiveSample, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ProjectionJob:
    job_id: str
    user_id: str
    sample_type: str
    hour_start: datetime
    attempts: int
    last_error: str | None


_SCHEMA = (
    """CREATE TABLE receipts (
        user_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        owner_generation INTEGER NOT NULL DEFAULT 0,
        payload_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
        PRIMARY KEY (user_id, batch_id, owner_generation))""",
    """CREATE TABLE samples (
        user_id TEXT NOT NULL, sample_type TEXT NOT NULL, sample_id TEXT NOT NULL,
        start TEXT NOT NULL, end TEXT NOT NULL,
        content_hash TEXT NOT NULL, sample_json TEXT NOT NULL,
        owner_generation INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, sample_type, sample_id), CHECK (start <= end))""",
    """CREATE INDEX samples_range ON samples
        (user_id, sample_type, start, sample_id)""",
    """CREATE TABLE tombstones (
        user_id TEXT NOT NULL, sample_type TEXT NOT NULL, sample_id TEXT NOT NULL,
        batch_id TEXT NOT NULL, owner_generation INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, sample_type, sample_id, owner_generation))""",
    """CREATE TABLE coverage_intervals (
        user_id TEXT NOT NULL, sample_type TEXT NOT NULL, batch_id TEXT NOT NULL,
        owner_generation INTEGER NOT NULL DEFAULT 0,
        kind TEXT NOT NULL CHECK (kind IN ('interval', 'anchor')),
        start TEXT, end TEXT, anchor TEXT, authorization_start TEXT,
        PRIMARY KEY (user_id, batch_id, owner_generation),
        FOREIGN KEY (user_id, batch_id, owner_generation)
            REFERENCES receipts(user_id, batch_id, owner_generation)
            ON DELETE CASCADE,
        CHECK ((kind='interval' AND start IS NOT NULL AND end IS NOT NULL
                AND start < end AND anchor IS NULL)
            OR (kind='anchor' AND start IS NULL AND end IS NULL
                AND anchor IS NOT NULL)))""",
    """CREATE INDEX coverage_type ON coverage_intervals
        (user_id, sample_type, start, end)""",
    """CREATE TABLE projection_jobs (
        job_id TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL,
        sample_type TEXT NOT NULL, hour_start TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'failed')),
        lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, PRIMARY KEY (user_id, sample_type, hour_start))""",
    """CREATE INDEX projection_ready ON projection_jobs (state, lease_until)""",
)


def _executor_only() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ArchiveStoreError("executor_required")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _instant(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("invalid_timestamp")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _wire_sample(sample: ArchiveSample) -> dict[str, Any]:
    value = asdict(sample)
    value["start"], value["end"] = _instant(sample.start), _instant(sample.end)
    value["metadata"] = json.loads(value.pop("metadata_json"))
    device = value.pop("device_json")
    if device is not None:
        value["device"] = json.loads(device)
    for key in ("total_energy", "total_distance", "detail"):
        encoded = value["payload"].pop(key + "_json", None)
        if encoded is not None:
            value["payload"][key] = json.loads(encoded)
    return value


def _wire_batch(batch: ArchiveBatch) -> dict[str, Any]:
    try:
        coverage = batch.coverage
        if coverage is None:
            raise ValueError("missing_coverage")
        covered = {
            "kind": coverage.kind,
            "authorization_start": _instant(coverage.authorization_start)
            if coverage.authorization_start
            else None,
        }
        if coverage.kind == "interval":
            covered.update(start=_instant(coverage.start), end=_instant(coverage.end))
        else:
            covered["anchor"] = coverage.anchor
        value = {
            "request_type": batch.request_type,
            "protocol_version": batch.protocol_version,
            "request_id": batch.request_id,
            "user_id": batch.user_id,
            "batch_id": batch.batch_id,
            "sample_type": batch.sample_type,
            "coverage": covered,
            "samples": [_wire_sample(sample) for sample in batch.samples],
            "deletions": list(batch.deletions),
        }
        if batch.request_type != "archive_batch":
            raise ValueError("not_batch")
        if batch.expected_inventory_revision is not None:
            value["expected_inventory_revision"] = batch.expected_inventory_revision
        return value
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ArchiveProtocolError("invalid_request", "batch") from exc


def _read_sample(value: str, sample_type: str) -> ArchiveSample:
    return validate_archive_fields(
        {
            "request_type": "archive_batch",
            "protocol_version": 2,
            "request_id": "read",
            "user_id": "read",
            "batch_id": "read",
            "sample_type": sample_type,
            "coverage": {
                "kind": "anchor",
                "anchor": "read",
                "authorization_start": None,
            },
            "samples": [json.loads(value)],
            "deletions": [],
        },
        limits=ArchiveLimits(),
    ).samples[0]


def _hours(start: datetime, end: datetime) -> Iterator[str]:
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end or hour <= start == end:
        yield _instant(hour)
        if end - hour <= _HOUR:
            break
        hour += _HOUR


def _projection_hour_count(start: datetime, end: datetime) -> int:
    """Count intersecting hours arithmetically, including an instantaneous point."""
    span = end - start.replace(minute=0, second=0, microsecond=0)
    hours, remainder = divmod(span, _HOUR)
    return max(1, hours + bool(remainder))


class ArchiveStore:
    """A path and synchronous operations; never a shared SQLite connection."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._access_lock = RLock()
        self._backup_in_progress = False

    @classmethod
    def open(cls, path: str | Path) -> ArchiveStore:
        _executor_only()
        store = cls(Path(path).absolute())
        # Read the version before changing journal mode, even for future files.
        with store._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > ARCHIVE_SCHEMA_VERSION:
                raise ArchiveStoreError("unsupported_schema")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > ARCHIVE_SCHEMA_VERSION:
                raise ArchiveStoreError("unsupported_schema")
            if version == 0:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute("PRAGMA user_version=1")
            if version <= 1:
                db.execute("""CREATE TABLE IF NOT EXISTS inventory_revisions (
                    user_id TEXT NOT NULL, sample_type TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    PRIMARY KEY (user_id, sample_type))""")
                db.execute("""INSERT OR IGNORE INTO inventory_revisions
                    SELECT user_id, sample_type, COUNT(*) FROM coverage_intervals
                    GROUP BY user_id, sample_type""")
                db.execute("PRAGMA user_version=2")
            if version <= 2:
                db.execute("""CREATE TABLE IF NOT EXISTS archive_owners (
                    user_id TEXT PRIMARY KEY, credential_digest TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0))""")
                db.execute("""CREATE TABLE IF NOT EXISTS archive_owner_claims (
                    user_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL,
                    credential_digest TEXT NOT NULL, expires_at TEXT NOT NULL)""")
                columns = {row[1] for row in db.execute("PRAGMA table_info(samples)")}
                if "owner_generation" not in columns:
                    db.execute("ALTER TABLE samples ADD COLUMN owner_generation INTEGER NOT NULL DEFAULT 0")
                tombstone_columns = {row[1] for row in db.execute("PRAGMA table_info(tombstones)")}
                if "owner_generation" not in tombstone_columns:
                    db.execute("""CREATE TABLE tombstones_v3 (
                        user_id TEXT NOT NULL, sample_type TEXT NOT NULL,
                        sample_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                        owner_generation INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (user_id, sample_type, sample_id, owner_generation))""")
                    db.execute("""INSERT INTO tombstones_v3
                        SELECT user_id, sample_type, sample_id, batch_id, 0 FROM tombstones""")
                    db.execute("DROP TABLE tombstones")
                    db.execute("ALTER TABLE tombstones_v3 RENAME TO tombstones")
                db.execute("PRAGMA user_version=3")
            receipt_columns = {row[1] for row in db.execute("PRAGMA table_info(receipts)")}
            if "owner_generation" not in receipt_columns:
                # Upgrade already-deployed schema-3 stores too. Receipt and
                # coverage keys must be scoped together, preserving legacy 0.
                db.execute("""CREATE TABLE receipts_scoped (
                    user_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                    owner_generation INTEGER NOT NULL DEFAULT 0,
                    payload_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
                    PRIMARY KEY (user_id, batch_id, owner_generation))""")
                db.execute("""INSERT INTO receipts_scoped
                    SELECT user_id, batch_id, 0, payload_hash, receipt_json FROM receipts""")
                db.execute("""CREATE TABLE coverage_scoped (
                    user_id TEXT NOT NULL, sample_type TEXT NOT NULL,
                    batch_id TEXT NOT NULL, owner_generation INTEGER NOT NULL DEFAULT 0,
                    kind TEXT NOT NULL CHECK (kind IN ('interval', 'anchor')),
                    start TEXT, end TEXT, anchor TEXT, authorization_start TEXT,
                    PRIMARY KEY (user_id, batch_id, owner_generation),
                    FOREIGN KEY (user_id, batch_id, owner_generation)
                        REFERENCES receipts_scoped(user_id, batch_id, owner_generation)
                        ON DELETE CASCADE,
                    CHECK ((kind='interval' AND start IS NOT NULL AND end IS NOT NULL
                            AND start < end AND anchor IS NULL)
                        OR (kind='anchor' AND start IS NULL AND end IS NULL
                            AND anchor IS NOT NULL)))""")
                db.execute("""INSERT INTO coverage_scoped
                    SELECT user_id, sample_type, batch_id, 0,
                    kind, start, end, anchor, authorization_start FROM coverage_intervals""")
                db.execute("DROP TABLE coverage_intervals")
                db.execute("DROP TABLE receipts")
                db.execute("ALTER TABLE receipts_scoped RENAME TO receipts")
                db.execute("ALTER TABLE coverage_scoped RENAME TO coverage_intervals")
                db.execute("""CREATE INDEX coverage_type ON coverage_intervals
                    (user_id, sample_type, start, end)""")
        return store

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        _executor_only()
        # Drain operations before checkpointing and reject new work until HA
        # finishes copying the configuration. No thread owns a lock across hooks.
        with self._access_lock:
            if self._backup_in_progress:
                raise ArchiveStoreError("backup_in_progress")
            with self._database() as db:
                yield db

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def begin_backup(self) -> None:
        """Checkpoint acknowledged writes and freeze this store during HA backup.

        Only this integration owns the file. External writers must be stopped;
        the process-local fence cannot control unrelated SQLite connections.
        A busy checkpoint aborts the backup instead of allowing a partial copy.
        """
        _executor_only()
        with self._access_lock:
            if self._backup_in_progress:
                return
            with self._connection() as db:
                db.execute("PRAGMA busy_timeout=1000")
                busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if busy:
                    raise ArchiveStoreError("backup_checkpoint_busy")
            self._backup_in_progress = True

    def end_backup(self) -> None:
        """Resume archive work, also when a backup failed after pre-backup."""
        _executor_only()
        with self._access_lock:
            self._backup_in_progress = False

    @staticmethod
    def _owner_digest(secret: str) -> str:
        try:
            return credential_digest(secret)
        except ValueError as exc:
            raise ArchiveStoreError("invalid_credential") from exc

    @staticmethod
    def _owner_row(db: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT credential_digest, generation FROM archive_owners WHERE user_id=?",
            (user_id,),
        ).fetchone()

    @classmethod
    def _assert_owner_in_transaction(
        cls, db: sqlite3.Connection, user_id: str, digest: str
    ) -> int:
        owner = cls._owner_row(db, user_id)
        if owner is None:
            raise ArchiveStoreError("owner_required")
        if not hmac.compare_digest(owner["credential_digest"], digest):
            raise ArchiveStoreError("owner_changed")
        return owner["generation"]

    def assert_owner(self, user_id: str, uploader_secret: str) -> int:
        _executor_only()
        digest = self._owner_digest(uploader_secret)
        with self._connection() as db:
            db.execute("BEGIN")
            return self._assert_owner_in_transaction(db, user_id, digest)

    def claim_owner(self, user_id: str, uploader_secret: str, now: datetime) -> OwnerClaim:
        _executor_only()
        if not isinstance(user_id, str) or not _ID.fullmatch(user_id):
            raise ArchiveStoreError("invalid_user")
        digest = self._owner_digest(uploader_secret)
        instant = _instant(now)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._owner_row(db, user_id)
            if owner and hmac.compare_digest(owner["credential_digest"], digest):
                raise ArchiveStoreError("owner_already_active")
            existing = db.execute(
                "SELECT * FROM archive_owner_claims WHERE user_id=?", (user_id,)
            ).fetchone()
            if existing and existing["expires_at"] <= instant:
                db.execute("DELETE FROM archive_owner_claims WHERE user_id=?", (user_id,))
                existing = None
            if existing:
                if not hmac.compare_digest(existing["credential_digest"], digest):
                    raise ArchiveStoreError("claim_conflict")
                return OwnerClaim(existing["claim_id"], digest[:12], _date(existing["expires_at"]))
            claim = OwnerClaim(str(uuid4()), digest[:12], now + timedelta(hours=24))
            db.execute(
                "INSERT INTO archive_owner_claims VALUES (?, ?, ?, ?)",
                (user_id, claim.claim_id, digest, _instant(claim.expires_at)),
            )
            return claim

    def pending_owner_claim(self, user_id: str, now: datetime) -> OwnerClaim | None:
        _executor_only()
        instant = _instant(now)
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM archive_owner_claims WHERE user_id=? AND expires_at>?",
                (user_id, instant),
            ).fetchone()
        return (
            OwnerClaim(row["claim_id"], row["credential_digest"][:12], _date(row["expires_at"]))
            if row else None
        )

    def owner_status(
        self, user_id: str, uploader_secret: str | None, now: datetime
    ) -> OwnerState:
        _executor_only()
        digest = self._owner_digest(uploader_secret) if uploader_secret is not None else None
        instant = _instant(now)
        with self._connection() as db:
            db.execute("BEGIN")
            owner = self._owner_row(db, user_id)
            generation = owner["generation"] if owner else 0
            if owner and digest and hmac.compare_digest(owner["credential_digest"], digest):
                return OwnerState("active", generation)
            pending = db.execute(
                "SELECT * FROM archive_owner_claims WHERE user_id=? AND expires_at>?",
                (user_id, instant),
            ).fetchone()
            if pending and digest and hmac.compare_digest(pending["credential_digest"], digest):
                return OwnerState(
                    "pending", generation, pending["claim_id"],
                    digest[:12], _date(pending["expires_at"]),
                )
            return OwnerState("not_owner" if owner else "unbound", generation)

    def approve_owner(self, user_id: str, claim_id: str, now: datetime) -> OwnerState:
        _executor_only()
        instant = _instant(now)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            claim = db.execute(
                "SELECT * FROM archive_owner_claims WHERE user_id=? AND claim_id=? AND expires_at>?",
                (user_id, claim_id, instant),
            ).fetchone()
            if claim is None:
                raise ArchiveStoreError("claim_not_found")
            owner = self._owner_row(db, user_id)
            if owner and hmac.compare_digest(owner["credential_digest"], claim["credential_digest"]):
                raise ArchiveStoreError("owner_already_active")
            generation = owner["generation"] + 1 if owner else 1
            db.execute(
                """INSERT INTO archive_owners VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                credential_digest=excluded.credential_digest,
                generation=excluded.generation""",
                (user_id, claim["credential_digest"], generation),
            )
            db.execute("DELETE FROM archive_owner_claims WHERE user_id=?", (user_id,))
            return OwnerState("active", generation)

    def reject_owner(self, user_id: str, claim_id: str) -> None:
        _executor_only()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "DELETE FROM archive_owner_claims WHERE user_id=? AND claim_id=?",
                (user_id, claim_id),
            )
            if result.rowcount != 1:
                raise ArchiveStoreError("claim_not_found")

    def commit_batch(
        self, batch: ArchiveBatch, uploader_secret: str,
        expected_owner_generation: int | None = None,
    ) -> ArchiveReceipt:
        """Atomically accept a validated batch, or return its exact prior receipt.

        Dataclasses are revalidated as defense against accidental direct callers.
        A tombstone is terminal for a HealthKit UUID: an older rescan cannot
        resurrect a deleted sample. Committed counts count changed rows.
        """
        _executor_only()
        batch = validate_archive_fields(_wire_batch(batch), limits=ArchiveLimits())
        wire = _wire_batch(batch)
        payload_hash = _hash(_json(wire))
        scope = (batch.user_id, batch.sample_type)
        digest = self._owner_digest(uploader_secret)
        if expected_owner_generation is not None and (
            type(expected_owner_generation) is not int or expected_owner_generation < 0
        ):
            raise ArchiveStoreError("invalid_owner_generation")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            generation = self._assert_owner_in_transaction(db, batch.user_id, digest)
            if expected_owner_generation is not None and expected_owner_generation != generation:
                raise ArchiveStoreError("owner_changed")
            prior = db.execute(
                """SELECT payload_hash, receipt_json FROM receipts
                WHERE user_id=? AND batch_id=? AND owner_generation=?""",
                (batch.user_id, batch.batch_id, generation),
            ).fetchone()
            if prior:
                if prior["payload_hash"] != payload_hash:
                    raise ArchiveStoreError("batch_conflict")
                return ArchiveReceipt.from_dict(json.loads(prior["receipt_json"]))
            revision = self._inventory_revision(db, scope)
            if (
                batch.expected_inventory_revision is not None
                and batch.expected_inventory_revision != revision
            ):
                raise ArchiveStoreError("inventory_changed")
            db.execute(
                """INSERT INTO inventory_revisions VALUES (?, ?, ?)
                ON CONFLICT(user_id, sample_type) DO UPDATE SET revision=excluded.revision""",
                (*scope, revision + 1),
            )
            affected: set[str] = set()
            range_exceeded = False
            expanded_hours = 0

            def affect(start: datetime, end: datetime) -> None:
                nonlocal range_exceeded, expanded_hours
                if range_exceeded:
                    return
                count = _projection_hour_count(start, end)
                expanded_hours += count
                if count > MAX_SAMPLE_PROJECTION_HOURS or expanded_hours > MAX_BATCH_PROJECTION_HOURS:
                    range_exceeded = True
                    affected.clear()
                    return
                for hour in _hours(start, end):
                    affected.add(hour)

            committed_samples = committed_deletions = 0
            for sample, value in zip(batch.samples, wire["samples"], strict=True):
                key = (*scope, sample.uuid)
                if db.execute(
                    """SELECT 1 FROM tombstones WHERE user_id=? AND sample_type=?
                    AND sample_id=? AND owner_generation=?""",
                    (*key, generation),
                ).fetchone():
                    continue
                encoded = _json(value)
                content_hash = _hash(encoded)
                old = db.execute(
                    """SELECT start, end, content_hash, owner_generation FROM samples
                    WHERE user_id=? AND sample_type=? AND sample_id=?""",
                    key,
                ).fetchone()
                if old and old["content_hash"] == content_hash:
                    if old["owner_generation"] != generation:
                        db.execute(
                            """UPDATE samples SET owner_generation=? WHERE user_id=?
                            AND sample_type=? AND sample_id=?""",
                            (generation, *key),
                        )
                    continue
                if old:
                    affect(_date(old["start"]), _date(old["end"]))
                affect(sample.start, sample.end)
                db.execute(
                    """INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, sample_type, sample_id) DO UPDATE SET
                    start=excluded.start, end=excluded.end,
                    content_hash=excluded.content_hash, sample_json=excluded.sample_json,
                    owner_generation=excluded.owner_generation""",
                    (
                        *key,
                        _instant(sample.start),
                        _instant(sample.end),
                        content_hash,
                        encoded,
                        generation,
                    ),
                )
                committed_samples += 1
            for sample_id in batch.deletions:
                key = (*scope, sample_id)
                old = db.execute(
                    """SELECT start, end FROM samples WHERE user_id=? AND sample_type=?
                    AND sample_id=? AND owner_generation=?""",
                    (*key, generation),
                ).fetchone()
                if old:
                    affect(_date(old["start"]), _date(old["end"]))
                    db.execute(
                        """DELETE FROM samples WHERE user_id=? AND sample_type=?
                        AND sample_id=? AND owner_generation=?""",
                        (*key, generation),
                    )
                result = db.execute(
                    "INSERT OR IGNORE INTO tombstones VALUES (?, ?, ?, ?, ?)",
                    (*key, batch.batch_id, generation),
                )
                committed_deletions += result.rowcount
            if range_exceeded:
                # Keep the original transaction and one durable repair intent.
                # Replacing IDs also fences snapshots from a prior projection.
                db.execute(
                    "DELETE FROM projection_jobs WHERE user_id=? AND sample_type=?", scope
                )
                db.execute(
                    """INSERT INTO projection_jobs
                    (job_id, user_id, sample_type, hour_start, state, last_error)
                    VALUES (?, ?, ?, ?, 'failed', 'projection_range_exceeded')""",
                    (str(uuid4()), *scope, _instant(FULL_REBUILD_HOUR)),
                )
            for hour in sorted(affected):
                db.execute(
                    """INSERT INTO projection_jobs
                    (job_id, user_id, sample_type, hour_start, state) VALUES (?, ?, ?, ?, 'pending')
                    ON CONFLICT(user_id, sample_type, hour_start) DO UPDATE SET
                    job_id=excluded.job_id, state='pending', lease_until=NULL,
                    attempts=0, last_error=NULL""",
                    (str(uuid4()), *scope, hour),
                )
            # The receipt reports the archive's outbox state, not HA visibility.
            pending = db.execute(
                """SELECT state FROM projection_jobs WHERE user_id=? AND sample_type=?
                ORDER BY state='failed' DESC LIMIT 1""",
                scope,
            ).fetchone()
            receipt = ArchiveReceipt(
                batch.request_id,
                batch.batch_id,
                len(batch.samples),
                committed_samples,
                len(batch.deletions),
                committed_deletions,
                ("failed" if pending["state"] == "failed" else "pending") if pending else "current",
            )
            db.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                (batch.user_id, batch.batch_id, generation, payload_hash, _json(receipt.as_dict())),
            )
            covered = wire["coverage"]
            db.execute(
                "INSERT INTO coverage_intervals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *scope,
                    batch.batch_id,
                    generation,
                    covered["kind"],
                    covered.get("start"),
                    covered.get("end"),
                    covered.get("anchor"),
                    covered["authorization_start"],
                ),
            )
            return receipt

    def validate_projection_ranges(self, user_id: str, sample_type: str) -> None:
        """Bound replay even after retry, correction, migration, or backup restore.

        Scan timestamps with constant memory before any recorder mutation. The
        raw archive has no age or duration ceiling; only projection is limited.
        """
        with self._connection() as db:
            for row in db.execute(
                "SELECT start, end FROM samples WHERE user_id=? AND sample_type=?",
                (user_id, sample_type),
            ):
                if _projection_hour_count(_date(row["start"]), _date(row["end"])) > MAX_SAMPLE_PROJECTION_HOURS:
                    raise ValueError("projection_range_exceeded")

    @staticmethod
    def _inventory_revision(db: sqlite3.Connection, scope: tuple) -> int:
        row = db.execute(
            "SELECT revision FROM inventory_revisions WHERE user_id=? AND sample_type=?",
            scope,
        ).fetchone()
        return row[0] if row else 0

    def inventory_page(
        self, query: ArchiveInventoryQuery, uploader_secret: str
    ) -> ArchiveInventoryPage:
        """Read revision and bounded UUID page in one SQLite snapshot.

        Membership uses start, not overlap: samples starting before a readable
        boundary must never be inferred deleted. Cursors bind all query fields.
        """
        _executor_only()
        try:
            # Reuse the strict wire validation for direct executor callers.
            start, end = _instant(query.start), _instant(query.end)
            validate_archive_fields(
                {
                    "request_type": "archive_inventory",
                    "protocol_version": 2,
                    "request_id": "inventory",
                    "user_id": query.user_id,
                    "sample_type": query.sample_type,
                    "start": start,
                    "end": end,
                    "limit": query.limit,
                    "cursor": query.cursor,
                },
                limits=ArchiveLimits(),
            )
        except (
            ArchiveProtocolError,
            ValueError,
            TypeError,
            AttributeError,
            OverflowError,
        ) as exc:
            raise ArchiveStoreError("invalid_query") from exc
        digest = self._owner_digest(uploader_secret)
        scope = [query.user_id, query.sample_type, start, end, query.limit]
        after = None
        expected_revision = None
        expected_generation = None
        if query.cursor is not None:
            try:
                decoded = json.loads(
                    base64.b64decode(query.cursor, altchars=b"-_", validate=True)
                )
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 9
                    or decoded[:5] != scope
                    or type(decoded[4]) is not int
                    or type(decoded[5]) is not int
                    or decoded[5] < 0
                    or type(decoded[6]) is not int
                    or decoded[6] < 1
                    or any(not isinstance(v, str) for v in decoded[7:])
                ):
                    raise ValueError
                after_start = _instant(_date(decoded[7]))
                if after_start != decoded[7] or not start <= after_start < end:
                    raise ValueError
                # Inventory keys must use the stored canonical lowercase UUID.
                if str(UUID(decoded[8])) != decoded[8]:
                    raise ValueError
                expected_revision, expected_generation, after = decoded[5], decoded[6], decoded[7:]
            except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
                raise ArchiveStoreError("invalid_cursor") from exc
        with self._connection() as db:
            db.execute("BEGIN")
            generation = self._assert_owner_in_transaction(db, query.user_id, digest)
            if expected_generation is not None and expected_generation != generation:
                raise ArchiveStoreError("owner_changed")
            revision = self._inventory_revision(db, (query.user_id, query.sample_type))
            if expected_revision is not None and revision != expected_revision:
                raise ArchiveStoreError("inventory_changed")
            sql = """SELECT start, sample_id FROM samples
                WHERE user_id=? AND sample_type=? AND start>=? AND start<?
                AND owner_generation=?"""
            args = [query.user_id, query.sample_type, start, end, generation]
            if after:
                sql += " AND (start, sample_id) > (?, ?)"
                args.extend(after)
            sql += " ORDER BY start, sample_id LIMIT ?"
            rows = db.execute(sql, [*args, query.limit + 1]).fetchall()
        page = rows[: query.limit]
        cursor = None
        if len(rows) > query.limit:
            cursor = base64.urlsafe_b64encode(
                _json(
                    [*scope, revision, generation, page[-1]["start"], page[-1]["sample_id"]]
                ).encode()
            ).decode()
        return ArchiveInventoryPage(
            tuple(row["sample_id"] for row in page), revision, cursor, generation
        )

    def query_samples(self, query: ArchiveQuery) -> ArchivePage:
        """Browse canonical originals without changing the established page model."""
        return self.query_samples_with_provenance(query)[0]

    def query_samples_with_provenance(
        self, query: ArchiveQuery
    ) -> tuple[ArchivePage, tuple[int, ...]]:
        """Read originals and matching owner generations in one page snapshot."""
        _executor_only()
        try:
            if (
                not _ID.fullmatch(query.user_id)
                or not isinstance(query.sample_type, str)
                or not query.sample_type
            ):
                raise ValueError
            if type(query.limit) is not int or not 1 <= query.limit <= 500:
                raise ValueError
            start, end = _instant(query.start), _instant(query.end)
            if start >= end:
                raise ValueError
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise ArchiveStoreError("invalid_query") from exc
        scope = [query.user_id, query.sample_type, start, end]
        after = None
        if query.cursor is not None:
            try:
                if not isinstance(query.cursor, str) or len(query.cursor) > 2048:
                    raise ValueError
                decoded = json.loads(
                    base64.b64decode(query.cursor, altchars=b"-_", validate=True)
                )
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 6
                    or decoded[:4] != scope
                    or any(not isinstance(v, str) for v in decoded)
                ):
                    raise ValueError
                after = decoded[4:]
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ArchiveStoreError("invalid_cursor") from exc
        sql = """SELECT start, sample_id, sample_json, owner_generation FROM samples
            WHERE user_id=? AND sample_type=? AND start < ?
            AND (end > ? OR (start=end AND start >= ?))"""
        args: list[Any] = [query.user_id, query.sample_type, end, start, start]
        if after:
            sql += " AND (start, sample_id) > (?, ?)"
            args.extend(after)
        sql += " ORDER BY start, sample_id LIMIT ?"
        args.append(query.limit + 1)
        with self._connection() as db:
            rows = db.execute(sql, args).fetchall()
        page = rows[: query.limit]
        cursor = None
        if len(rows) > query.limit:
            cursor = base64.urlsafe_b64encode(
                _json([*scope, page[-1]["start"], page[-1]["sample_id"]]).encode()
            ).decode()
        return (
            ArchivePage(
                tuple(_read_sample(row["sample_json"], query.sample_type) for row in page),
                cursor,
            ),
            tuple(row["owner_generation"] for row in page),
        )

    def claim_projection_jobs(
        self, limit: int, *, retry_failed: bool = True
    ) -> tuple[ProjectionJob, ...]:
        _executor_only()
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ArchiveStoreError("invalid_limit")
        now = datetime.now(timezone.utc)
        jobs = []
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM projection_jobs
                WHERE (state='pending' AND (lease_until IS NULL OR lease_until <= ?))
                   OR (state='failed' AND ?)
                   OR (state='claimed' AND lease_until <= ?)
                ORDER BY hour_start, user_id, sample_type LIMIT ?""",
                (_instant(now), retry_failed, _instant(now), limit),
            ).fetchall()
            for row in rows:
                job_id = str(uuid4())
                db.execute(
                    """UPDATE projection_jobs SET job_id=?, state='claimed',
                    lease_until=?, attempts=attempts+1 WHERE job_id=?""",
                    (
                        job_id,
                        _instant(now + timedelta(seconds=_CLAIM_SECONDS)),
                        row["job_id"],
                    ),
                )
                jobs.append(
                    ProjectionJob(
                        job_id,
                        row["user_id"],
                        row["sample_type"],
                        _date(row["hour_start"]),
                        row["attempts"] + 1,
                        row["last_error"],
                    )
                )
        return tuple(jobs)

    def sample_detail(
        self, user_id: str, sample_type: str, sample_id: str
    ) -> dict | None:
        """Return one original using its full partition key, never UUID alone."""
        with self._connection() as db:
            row = db.execute(
                """SELECT sample_json, owner_generation FROM samples
                WHERE user_id=? AND sample_type=? AND sample_id=?""",
                (user_id, sample_type, sample_id),
            ).fetchone()
        return {**json.loads(row["sample_json"]), "owner_generation": row["owner_generation"]} if row else None

    def tombstone_page(
        self, user_id: str, sample_type: str, after: str = "", limit: int = 200
    ) -> tuple[dict, ...]:
        """Bounded UUID keyset; expose latest generation while retaining audit rows."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ArchiveStoreError("invalid_limit")
        with self._connection() as db:
            rows = db.execute(
                """SELECT t.sample_id, t.batch_id FROM tombstones AS t
                WHERE t.user_id=? AND t.sample_type=? AND t.sample_id>?
                AND t.owner_generation=(
                    SELECT MAX(newer.owner_generation) FROM tombstones AS newer
                    WHERE newer.user_id=t.user_id AND newer.sample_type=t.sample_type
                    AND newer.sample_id=t.sample_id)
                ORDER BY t.sample_id LIMIT ?""",
                (user_id, sample_type, after, limit),
            ).fetchall()
        return tuple({"uuid": row[0], "batch_id": row[1]} for row in rows)

    def projection_status(self, user_id: str) -> dict[str, tuple[str, str | None]]:
        """Read per-type outbox state without claiming or modifying jobs.

        Only imported types appear. Failed work takes precedence over pending
        work; a type becomes current only when all of its jobs are completed.
        """
        _executor_only()
        if not isinstance(user_id, str) or not _ID.fullmatch(user_id):
            raise ArchiveStoreError("invalid_user")
        with self._connection() as db:
            rows = db.execute(
                """SELECT coverage.sample_type,
                    MAX(CASE WHEN jobs.state='failed' THEN 2
                             WHEN jobs.state IS NOT NULL THEN 1 ELSE 0 END) AS priority,
                    MIN(CASE WHEN jobs.state='failed' THEN jobs.last_error END) AS error
                FROM (SELECT DISTINCT sample_type FROM coverage_intervals
                      WHERE user_id=?) AS coverage
                LEFT JOIN projection_jobs AS jobs
                  ON jobs.sample_type=coverage.sample_type AND jobs.user_id=?
                GROUP BY coverage.sample_type""",
                (user_id, user_id),
            ).fetchall()
        return {
            row["sample_type"]: (
                ("current", "pending", "failed")[row["priority"]],
                row["error"],
            )
            for row in rows
        }

    def complete_projection_job(self, job_id: str) -> None:
        with self._connection() as db:
            db.execute(
                "DELETE FROM projection_jobs WHERE job_id=? AND state='claimed'",
                (job_id,),
            )

    def projection_claim_exists(self, job_id: str) -> bool:
        """Fence a queued worker against explicit archive deletion/correction."""
        with self._connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM projection_jobs WHERE job_id=? AND state='claimed'",
                    (job_id,),
                ).fetchone()
                is not None
            )

    def fail_projection_job(self, job_id: str, error_code: str) -> None:
        _executor_only()
        if not isinstance(error_code, str) or not _ID.fullmatch(error_code):
            raise ArchiveStoreError("invalid_error_code")
        with self._connection() as db:
            db.execute(
                """UPDATE projection_jobs SET state='failed', lease_until=NULL,
                last_error=? WHERE job_id=? AND state='claimed'""",
                (error_code, job_id),
            )

    def defer_projection_job(
        self, job_id: str, error_code: str, delay: int | None
    ) -> None:
        """Retry transient failures without a hot loop; exhaustion is visible."""
        if delay is None:
            self.fail_projection_job(job_id, error_code)
            return
        with self._connection() as db:
            db.execute(
                """UPDATE projection_jobs SET state='pending', lease_until=?, last_error=?
                WHERE job_id=? AND state='claimed'""",
                (
                    _instant(datetime.now(timezone.utc) + timedelta(seconds=delay)),
                    error_code,
                    job_id,
                ),
            )

    def retry_failed_projections(self, user_id: str) -> None:
        """Authenticated archive UI may explicitly retry one user's failures."""
        _executor_only()
        if not isinstance(user_id, str) or not _ID.fullmatch(user_id):
            raise ArchiveStoreError("invalid_user")
        with self._connection() as db:
            db.execute(
                """UPDATE projection_jobs SET state='pending', lease_until=NULL,
                attempts=0, last_error=NULL WHERE user_id=? AND state='failed'""",
                (user_id,),
            )

    def request_full_projection_rebuild(self, job: ProjectionJob) -> None:
        """Persist the intent before any whole-series recorder clear."""
        with self._connection() as db:
            db.execute(
                """INSERT OR IGNORE INTO projection_jobs
                (job_id, user_id, sample_type, hour_start, state)
                VALUES (?, ?, ?, ?, 'pending')""",
                (
                    str(uuid4()),
                    job.user_id,
                    job.sample_type,
                    _instant(FULL_REBUILD_HOUR),
                ),
            )

    def request_projection_reconciliation(self, user_id: str, sample_type: str) -> None:
        """Durably replay missing statistics without clearing existing hours."""
        with self._connection() as db:
            db.execute(
                """INSERT OR IGNORE INTO projection_jobs
                (job_id, user_id, sample_type, hour_start, state)
                VALUES (?, ?, ?, ?, 'pending')""",
                (str(uuid4()), user_id, sample_type, _instant(RECONCILE_HOUR)),
            )

    def projection_tail_snapshot(self, job: ProjectionJob) -> tuple[str, ...]:
        """IDs fence completion against new corrections during a replay."""
        with self._connection() as db:
            return tuple(
                row[0]
                for row in db.execute(
                    """SELECT job_id FROM projection_jobs
                WHERE user_id=? AND sample_type=? AND hour_start>=?""",
                    (job.user_id, job.sample_type, _instant(job.hour_start)),
                )
            )

    def complete_projection_snapshot(self, job_ids: tuple[str, ...]) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "DELETE FROM projection_jobs WHERE job_id=?",
                ((job_id,) for job_id in job_ids),
            )

    def delete_user_archive(self, user_id: str) -> None:
        """Caller must enforce administrator auth and user-scoped confirmation."""
        _executor_only()
        if not isinstance(user_id, str) or not _ID.fullmatch(user_id):
            raise ArchiveStoreError("invalid_user")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Preserve monotonic revisions across deletion and subsequent re-import.
            db.execute(
                "UPDATE inventory_revisions SET revision=revision+1 WHERE user_id=?",
                (user_id,),
            )
            for table in ("samples", "tombstones", "projection_jobs", "receipts"):
                db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
