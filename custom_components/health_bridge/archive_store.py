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
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .archive_protocol import (
    ARCHIVE_SCHEMA_VERSION,
    ArchiveBatch,
    ArchiveLimits,
    ArchiveProtocolError,
    ArchiveReceipt,
    ArchiveSample,
    validate_archive_fields,
    validate_archive_request,
)


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HOUR = timedelta(hours=1)
_CLAIM_SECONDS = 300
# A durable repair intent, outside real HealthKit history. Unlike a flag on an
# ordinary hour, archive corrections cannot erase it while recorder is cleared.
FULL_REBUILD_HOUR = datetime.min.replace(tzinfo=timezone.utc)


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
        payload_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
        PRIMARY KEY (user_id, batch_id))""",
    """CREATE TABLE samples (
        user_id TEXT NOT NULL, sample_type TEXT NOT NULL, sample_id TEXT NOT NULL,
        start TEXT NOT NULL, end TEXT NOT NULL,
        content_hash TEXT NOT NULL, sample_json TEXT NOT NULL,
        PRIMARY KEY (user_id, sample_type, sample_id), CHECK (start <= end))""",
    """CREATE INDEX samples_range ON samples
        (user_id, sample_type, start, sample_id)""",
    """CREATE TABLE tombstones (
        user_id TEXT NOT NULL, sample_type TEXT NOT NULL, sample_id TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        PRIMARY KEY (user_id, sample_type, sample_id))""",
    """CREATE TABLE coverage_intervals (
        user_id TEXT NOT NULL, sample_type TEXT NOT NULL, batch_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('interval', 'anchor')),
        start TEXT, end TEXT, anchor TEXT, authorization_start TEXT,
        PRIMARY KEY (user_id, batch_id),
        FOREIGN KEY (user_id, batch_id) REFERENCES receipts(user_id, batch_id)
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
        return value
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ArchiveProtocolError("invalid_request", "batch") from exc


def _read_sample(value: str, sample_type: str) -> ArchiveSample:
    return validate_archive_request(
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


class ArchiveStore:
    """A path and synchronous operations; never a shared SQLite connection."""

    def __init__(self, path: Path) -> None:
        self._path = path

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
        return store

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        _executor_only()
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

    def commit_batch(self, batch: ArchiveBatch) -> ArchiveReceipt:
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
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT payload_hash, receipt_json FROM receipts WHERE user_id=? AND batch_id=?",
                (batch.user_id, batch.batch_id),
            ).fetchone()
            if prior:
                if prior["payload_hash"] != payload_hash:
                    raise ArchiveStoreError("batch_conflict")
                return ArchiveReceipt.from_dict(json.loads(prior["receipt_json"]))
            affected: set[str] = set()
            committed_samples = committed_deletions = 0
            for sample, value in zip(batch.samples, wire["samples"], strict=True):
                key = (*scope, sample.uuid)
                if db.execute(
                    "SELECT 1 FROM tombstones WHERE user_id=? AND sample_type=? AND sample_id=?",
                    key,
                ).fetchone():
                    continue
                encoded = _json(value)
                content_hash = _hash(encoded)
                old = db.execute(
                    "SELECT start, end, content_hash FROM samples WHERE user_id=? AND sample_type=? AND sample_id=?",
                    key,
                ).fetchone()
                if old and old["content_hash"] == content_hash:
                    continue
                if old:
                    affected.update(_hours(_date(old["start"]), _date(old["end"])))
                affected.update(_hours(sample.start, sample.end))
                db.execute(
                    """INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, sample_type, sample_id) DO UPDATE SET
                    start=excluded.start, end=excluded.end,
                    content_hash=excluded.content_hash, sample_json=excluded.sample_json""",
                    (
                        *key,
                        _instant(sample.start),
                        _instant(sample.end),
                        content_hash,
                        encoded,
                    ),
                )
                committed_samples += 1
            for sample_id in batch.deletions:
                key = (*scope, sample_id)
                old = db.execute(
                    "SELECT start, end FROM samples WHERE user_id=? AND sample_type=? AND sample_id=?",
                    key,
                ).fetchone()
                if old:
                    affected.update(_hours(_date(old["start"]), _date(old["end"])))
                    db.execute(
                        "DELETE FROM samples WHERE user_id=? AND sample_type=? AND sample_id=?",
                        key,
                    )
                result = db.execute(
                    "INSERT OR IGNORE INTO tombstones VALUES (?, ?, ?, ?)",
                    (*key, batch.batch_id),
                )
                committed_deletions += result.rowcount
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
                "SELECT 1 FROM projection_jobs WHERE user_id=? AND sample_type=? LIMIT 1",
                scope,
            ).fetchone()
            receipt = ArchiveReceipt(
                batch.request_id,
                batch.batch_id,
                len(batch.samples),
                committed_samples,
                len(batch.deletions),
                committed_deletions,
                "pending" if pending else "current",
            )
            db.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?)",
                (batch.user_id, batch.batch_id, payload_hash, _json(receipt.as_dict())),
            )
            covered = wire["coverage"]
            db.execute(
                "INSERT INTO coverage_intervals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *scope,
                    batch.batch_id,
                    covered["kind"],
                    covered.get("start"),
                    covered.get("end"),
                    covered.get("anchor"),
                    covered["authorization_start"],
                ),
            )
            return receipt

    def query_samples(self, query: ArchiveQuery) -> ArchivePage:
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
        sql = """SELECT start, sample_id, sample_json FROM samples
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
        return ArchivePage(
            tuple(_read_sample(row["sample_json"], query.sample_type) for row in page),
            cursor,
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
            for table in ("samples", "tombstones", "projection_jobs", "receipts"):
                db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
