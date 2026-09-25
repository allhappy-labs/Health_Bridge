"""Authenticated archive dispatcher; all durable work runs in HA's executor.

The shared webhook authenticates the HAL envelope and enforces any configured
user binding before calling this module. A shared, unbound HAL token is an
integration credential, not a per-user security boundary (as with protocol v1).
"""

from __future__ import annotations

import math
from pathlib import Path
import sqlite3
import time

from aiohttp import web
from homeassistant.core import HomeAssistant

from .archive_protocol import (
    ARCHIVE_SCHEMA_VERSION,
    ArchiveCapability,
    ArchiveLimits,
    ArchiveProjectionStatus,
    ArchiveProtocolError,
    MetricProjectionStatus,
    validate_archive_request,
)
from .archive_store import ArchiveStore, ArchiveStoreError
from .const import DOMAIN


ARCHIVE_REQUEST_TYPES = frozenset(
    {"archive_capability", "archive_batch", "archive_status"}
)
ARCHIVE_LIMITS = ArchiveLimits()
ARCHIVE_BATCHES_PER_MINUTE = 60
ARCHIVE_CONTROLS_PER_MINUTE = 120
STATISTICS_AVAILABLE = False

# Direct original types whose archive payload semantics are implemented. The
# projection task expands this registry as its metric rules are audited.
ARCHIVE_TYPE_METRICS = {
    "HKQuantityTypeIdentifierStepCount": ("steps",),
    "HKQuantityTypeIdentifierHeartRate": ("heart_rate",),
    "HKCategoryTypeIdentifierSleepAnalysis": ("sleep_details",),
    "HKWorkoutTypeIdentifier": ("last_apple_workout",),
}


def archive_error(code: str, status: int) -> web.Response:
    """Return stable errors without logging payloads or database exceptions."""
    return web.json_response({"ok": False, "error": code}, status=status)


def _reported_projection_state(state: str) -> str:
    """An empty outbox alone cannot prove HA statistics were read back."""
    if state == "current" and not STATISTICS_AVAILABLE:
        return "pending"
    return state


def _open_store(path: str) -> ArchiveStore:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return ArchiveStore.open(path)


async def async_setup_archive(hass: HomeAssistant) -> None:
    """Open durable storage without making live setup depend on its health."""
    try:
        store = await hass.async_add_executor_job(
            _open_store, hass.config.path(".storage", "health_bridge_archive.sqlite")
        )
    except OSError, sqlite3.Error, ArchiveStoreError:
        store = None
    hass.data[DOMAIN]["archive_store"] = store


def archive_rate_limit(
    hass: HomeAssistant, entry_id: str, kind: str
) -> web.Response | None:
    """Bound rates by trusted entry, with independent control/batch budgets."""
    bucket = "batch" if kind == "archive_batch" else "control"
    limit = (
        ARCHIVE_BATCHES_PER_MINUTE if bucket == "batch" else ARCHIVE_CONTROLS_PER_MINUTE
    )
    windows = hass.data[DOMAIN].setdefault("archive_rate_windows", {})
    now = time.monotonic()
    start, count = windows.get((entry_id, bucket), (now, 0))
    if now - start >= 60:
        start, count = now, 0
    if count >= limit:
        response = archive_error("rate_limited", 429)
        response.headers["Retry-After"] = str(max(1, math.ceil(60 - (now - start))))
        return response
    windows[(entry_id, bucket)] = (start, count + 1)
    return None


async def async_handle_archive_request(
    hass: HomeAssistant, payload: dict, store: ArchiveStore | None
) -> web.Response:
    """Handle only authenticated, scoped v2 input; acknowledge after COMMIT."""
    try:
        batch = validate_archive_request(payload, limits=ARCHIVE_LIMITS)
    except ArchiveProtocolError as exc:
        return archive_error(exc.code, 413 if exc.code == "limit_exceeded" else 422)

    if batch.request_type == "archive_capability":
        return web.json_response(
            ArchiveCapability(
                request_id=batch.request_id,
                archive_schema_version=ARCHIVE_SCHEMA_VERSION,
                max_batch_bytes=ARCHIVE_LIMITS.max_batch_bytes,
                max_samples_per_batch=ARCHIVE_LIMITS.max_samples_per_batch,
                max_deletions_per_batch=ARCHIVE_LIMITS.max_deletions_per_batch,
                supported_sample_types=tuple(ARCHIVE_TYPE_METRICS),
                supported_metrics=tuple(
                    metric
                    for metrics in ARCHIVE_TYPE_METRICS.values()
                    for metric in metrics
                ),
                archive_available=store is not None,
                statistics_available=STATISTICS_AVAILABLE,
            ).as_dict()
        )
    if store is None:
        return archive_error("archive_unavailable", 503)
    try:
        if batch.request_type == "archive_status":
            states = await hass.async_add_executor_job(
                store.projection_status, batch.user_id
            )
            return web.json_response(
                ArchiveProjectionStatus(
                    batch.request_id,
                    tuple(
                        MetricProjectionStatus(
                            metric, _reported_projection_state(state), error
                        )
                        for sample_type, (state, error) in sorted(states.items())
                        for metric in ARCHIVE_TYPE_METRICS.get(sample_type, ())
                    ),
                ).as_dict()
            )
        if batch.sample_type not in ARCHIVE_TYPE_METRICS:
            return archive_error("unsupported_sample_type", 422)
        receipt = await hass.async_add_executor_job(store.commit_batch, batch)
        acknowledgement = receipt.as_dict()
        acknowledgement["projection_state"] = _reported_projection_state(
            receipt.projection_state
        )
        return web.json_response(acknowledgement)
    except ArchiveStoreError as exc:
        if exc.code == "batch_conflict":
            return archive_error(exc.code, 409)
        return archive_error("archive_unavailable", 503)
    except OSError, sqlite3.Error:
        return archive_error("archive_unavailable", 503)
