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
from datetime import datetime, timezone

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
from .statistic_rules import TYPE_METRICS


ARCHIVE_REQUEST_TYPES = frozenset(
    {
        "archive_capability",
        "archive_owner_claim",
        "archive_batch",
        "archive_status",
        "archive_inventory",
    }
)
ARCHIVE_LIMITS = ArchiveLimits()
ARCHIVE_BATCHES_PER_MINUTE = 60
ARCHIVE_CONTROLS_PER_MINUTE = 120

# Exactly one entry per audited original type, including timeline-only sources.
# This packaged catalog also supplies projection rules; live state classes do not.
ARCHIVE_TYPE_METRICS = TYPE_METRICS


def archive_error(code: str, status: int) -> web.Response:
    """Return stable errors without logging payloads or database exceptions."""
    return web.json_response({"ok": False, "error": code}, status=status)


def _reported_projection_state(state: str, statistics_available: bool) -> str:
    """An empty outbox alone cannot prove HA statistics were read back."""
    if state == "current" and not statistics_available:
        return "pending"
    return state


def _open_store(path: str) -> ArchiveStore:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return ArchiveStore.open(path)


async def async_setup_archive(hass: HomeAssistant) -> None:
    """Open durable storage without making live setup depend on its health."""
    # The store belongs to the HA process, not the HAL config entry. Replacing
    # it on reload would discard an active backup fence and let writes resume
    # while HA is still copying the checkpointed file. Failed opens may retry.
    if hass.data[DOMAIN].get("archive_store") is not None:
        return
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
    worker = hass.data.get(DOMAIN, {}).get("archive_projection_worker")
    statistics_available = store is not None and worker is not None and worker.available
    try:
        batch = validate_archive_request(payload, limits=ARCHIVE_LIMITS)
    except ArchiveProtocolError as exc:
        return archive_error(
            exc.code,
            413
            if exc.code == "limit_exceeded"
            else 403
            if exc.code == "owner_required"
            else 422,
        )

    if store is None:
        if batch.request_type != "archive_capability":
            return archive_error("archive_unavailable", 503)
        owner = None
    else:
        try:
            owner = await hass.async_add_executor_job(
                store.owner_status,
                batch.user_id,
                batch.uploader_credential,
                datetime.now(timezone.utc),
            )
        except ArchiveStoreError as exc:
            return archive_error(
                "invalid_request"
                if exc.code == "invalid_credential"
                else "archive_unavailable",
                422 if exc.code == "invalid_credential" else 503,
            )
        except OSError, sqlite3.Error:
            return archive_error("archive_unavailable", 503)

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
                statistics_available=statistics_available,
                owner_state=owner.state if owner else "unbound",
                owner_generation=owner.generation if owner else 0,
                claim_id=owner.claim_id if owner else None,
                fingerprint=owner.fingerprint if owner else None,
                expires_at=owner.expires_at.isoformat().replace("+00:00", "Z")
                if owner and owner.expires_at
                else None,
            ).as_dict()
        )
    try:
        if batch.request_type == "archive_owner_claim":
            claim = await hass.async_add_executor_job(
                store.claim_owner,
                batch.user_id,
                batch.uploader_credential,
                datetime.now(timezone.utc),
            )
            state = await hass.async_add_executor_job(
                store.owner_status,
                batch.user_id,
                batch.uploader_credential,
                datetime.now(timezone.utc),
            )
            return web.json_response(
                {
                    "ok": True,
                    "request_type": "archive_owner_claim",
                    "protocol_version": 2,
                    "request_id": batch.request_id,
                    "ownership_contract_version": 1,
                    "owner_state": state.state,
                    "owner_generation": state.generation,
                    "claim_id": claim.claim_id,
                    "fingerprint": claim.fingerprint,
                    "expires_at": claim.expires_at.isoformat().replace("+00:00", "Z"),
                }
            )
        if owner.state != "active":
            return archive_error(
                "owner_pending"
                if owner.state == "pending"
                else "owner_required"
                if owner.state == "unbound"
                else "owner_changed",
                403,
            )
        if batch.request_type == "archive_status":
            # Projection status may await recorder. Recheck after that await.
            states = (
                await worker.async_projection_status(batch.user_id)
                if statistics_available
                else await hass.async_add_executor_job(
                    store.projection_status, batch.user_id
                )
            )
            await hass.async_add_executor_job(
                store.assert_owner, batch.user_id, batch.uploader_credential
            )
            return web.json_response(
                ArchiveProjectionStatus(
                    batch.request_id,
                    tuple(
                        MetricProjectionStatus(
                            metric,
                            _reported_projection_state(state, statistics_available),
                            error,
                        )
                        for sample_type, (state, error) in sorted(states.items())
                        for metric in ARCHIVE_TYPE_METRICS.get(sample_type, ())
                    ),
                ).as_dict()
            )
        if batch.sample_type not in ARCHIVE_TYPE_METRICS:
            return archive_error("unsupported_sample_type", 422)
        if batch.request_type == "archive_inventory":
            page = await hass.async_add_executor_job(
                store.inventory_page, batch.inventory_query, batch.uploader_credential
            )
            return web.json_response(page.as_dict(batch.request_id))
        receipt = await hass.async_add_executor_job(
            store.commit_batch,
            batch,
            batch.uploader_credential,
            batch.expected_owner_generation,
        )
        acknowledgement = receipt.as_dict()
        # A receipt proves only archive COMMIT. The status request performs
        # fresh recorder reconciliation before it may say statistics current.
        acknowledgement["projection_state"] = (
            "failed" if receipt.projection_state == "failed" else "pending"
        )
        return web.json_response(acknowledgement)
    except ArchiveStoreError as exc:
        if exc.code in {"owner_required", "owner_changed"}:
            return archive_error(exc.code, 403)
        if exc.code == "claim_conflict":
            return archive_error("owner_pending", 409)
        if exc.code == "invalid_credential":
            return archive_error("invalid_request", 422)
        if exc.code in {"batch_conflict", "inventory_changed"}:
            return archive_error(exc.code, 409)
        if exc.code in {"invalid_query", "invalid_cursor"}:
            return archive_error(exc.code, 422)
        return archive_error("archive_unavailable", 503)
    except OSError, sqlite3.Error:
        return archive_error("archive_unavailable", 503)
