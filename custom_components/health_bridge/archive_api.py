"""HA-authenticated administrator access to partitioned originals.

There is no verified HA-account to Health Bridge user pairing. Until one exists,
all health archive operations are administrator-only. Shared webhook credentials
are deliberately not accepted here. Responses never contain integration tokens.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from uuid import UUID

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .archive_store import ArchiveQuery, ArchiveStoreError, _wire_sample
from .const import DOMAIN
from .statistic_rules import RULES, TYPE_METRICS

CARD_URL = "/health_bridge/health-bridge-archive.js"
_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")
MAX_RANGE = timedelta(days=36600)
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _response(data, status=200):
    return web.json_response(data, status=status, headers=HEADERS)


def _error(code, status):
    return _response({"ok": False, "error": code}, status)


def _sample_type(request):
    value = request.query.get("sample_type")
    if value not in TYPE_METRICS:
        raise ValueError
    return value


def _query(request, user):
    dates = []
    for key in ("start", "end"):
        value = request.query.get(key, "")
        if not _UTC.fullmatch(value):
            raise ValueError
        dates.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
    start, end = dates
    limit = int(request.query.get("limit", "200"))
    if not 1 <= limit <= 500 or not timedelta(0) < end - start <= MAX_RANGE:
        raise ValueError
    return ArchiveQuery(
        user, _sample_type(request), start, end, limit, request.query.get("cursor")
    )


class ArchiveCardView(HomeAssistantView):
    """Public static source contains no data; all data routes require HA auth."""

    url = CARD_URL
    name = "health_bridge:archive_card"
    requires_auth = False

    async def get(self, request):
        return web.FileResponse(
            Path(__file__).parent / "cards/health-bridge-archive.js",
            headers={**HEADERS, "Content-Type": "text/javascript"},
        )


class ArchiveView(HomeAssistantView):
    url = "/api/health_bridge/archive/{user_id}/{operation}"
    name = "api:health_bridge:archive"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    def _access(self, request, user):
        if not request["hass_user"].is_admin:
            return _error("administrator_required", 403)
        if not _USER.fullmatch(user):
            return _error("invalid_query", 400)
        if self.hass.data.get(DOMAIN, {}).get("archive_store") is None:
            return _error("archive_unavailable", 503)
        return None

    async def get(self, request, user_id, operation):
        denied = self._access(request, user_id)
        if denied is not None:
            return denied
        store = self.hass.data[DOMAIN]["archive_store"]
        try:
            if operation == "owner":
                now = datetime.now(timezone.utc)
                state = await self.hass.async_add_executor_job(
                    store.owner_status, user_id, None, now
                )
                claim = await self.hass.async_add_executor_job(
                    store.pending_owner_claim, user_id, now
                )
                return _response(
                    {
                        "ok": True,
                        "user_id": user_id,
                        "owner_state": "active" if state.generation > 0 else "unbound",
                        "owner_generation": state.generation,
                        "pending_claim": None
                        if claim is None
                        else {
                            "claim_id": claim.claim_id,
                            "fingerprint": claim.fingerprint,
                            "expires_at": claim.expires_at.isoformat().replace(
                                "+00:00", "Z"
                            ),
                        },
                    }
                )
            if operation == "status":
                return await self._status(user_id, store)
            if operation == "sample":
                sample_type = _sample_type(request)
                sample_id = str(UUID(request.query.get("uuid", "")))
                sample = await self.hass.async_add_executor_job(
                    store.sample_detail, user_id, sample_type, sample_id
                )
                return (
                    _response({"sample": sample})
                    if sample
                    else _error("sample_not_found", 404)
                )
            if operation not in {"samples", "export"}:
                return _error("not_found", 404)
            query = _query(request, user_id)
            if operation == "export" and query.cursor is not None:
                raise ValueError
            page, generations = await self.hass.async_add_executor_job(
                store.query_samples_with_provenance, query
            )
            if operation == "export":
                return await self._export(request, store, query, page, generations)
            return _response(
                {
                    "samples": [
                        {**_wire_sample(s), "owner_generation": generation}
                        for s, generation in zip(page.samples, generations, strict=True)
                    ],
                    "next_cursor": page.next_cursor,
                }
            )
        except ValueError, ArchiveStoreError:
            return _error("invalid_query", 400)
        except Exception:
            # Do not let HA's exception handler log database/sample content.
            return _error("archive_unavailable", 503)

    async def post(self, request, user_id, operation):
        denied = self._access(request, user_id)
        if denied is not None:
            return denied
        data = self.hass.data[DOMAIN]
        try:
            if operation in {"owner-approve", "owner-reject"}:
                body = bytearray()
                async for chunk in request.content.iter_chunked(1024):
                    body.extend(chunk)
                    if len(body) > 1024:
                        return _error("invalid_confirmation", 400)
                confirmation = json.loads(body)
                action = "APPROVE" if operation == "owner-approve" else "REJECT"
                claim_id = (
                    confirmation.get("claim_id")
                    if isinstance(confirmation, dict)
                    else None
                )
                if (
                    not isinstance(claim_id, str)
                    or not re.fullmatch(r"[0-9a-fA-F-]{36}", claim_id)
                    or confirmation
                    != {
                        "claim_id": claim_id,
                        "confirm_user_id": user_id,
                        "confirm": action,
                    }
                ):
                    return _error("invalid_confirmation", 400)
                store = data["archive_store"]
                if operation == "owner-approve":
                    state = await self.hass.async_add_executor_job(
                        store.approve_owner,
                        user_id,
                        claim_id,
                        datetime.now(timezone.utc),
                    )
                    return _response(
                        {
                            "ok": True,
                            "owner_state": state.state,
                            "owner_generation": state.generation,
                        }
                    )
                await self.hass.async_add_executor_job(
                    store.reject_owner, user_id, claim_id
                )
                return _response({"ok": True, "pending_claim": None})
            if operation == "retry":
                await self.hass.async_add_executor_job(
                    data["archive_store"].retry_failed_projections, user_id
                )
                return _response({"ok": True, "projection_state": "pending"})
            if operation != "delete":
                return _error("not_found", 404)
            # Read a bounded body even with chunked transfer encoding.
            body = bytearray()
            async for chunk in request.content.iter_chunked(1024):
                body.extend(chunk)
                if len(body) > 1024:
                    return _error("invalid_confirmation", 400)
            confirmation = json.loads(body)
            if confirmation != {"confirm_user_id": user_id, "confirm": "DELETE"}:
                return _error("invalid_confirmation", 400)
            worker = data.get("archive_projection_worker")
            # Prevent a projection already in flight from racing this operation.
            if worker is not None:
                await worker.async_delete_archive(user_id)
            else:
                await self.hass.async_add_executor_job(
                    data["archive_store"].delete_user_archive, user_id
                )
            return _response(
                {
                    "ok": True,
                    "deleted_user_id": user_id,
                    "recorder_copies_deleted": False,
                }
            )
        except ArchiveStoreError as exc:
            return _error(
                "claim_not_found"
                if exc.code == "claim_not_found"
                else "archive_unavailable",
                404 if exc.code == "claim_not_found" else 503,
            )
        except ValueError, UnicodeError:
            return _error("invalid_confirmation", 400)
        except Exception:
            return _error("archive_unavailable", 503)

    async def _status(self, user, store):
        worker = self.hass.data[DOMAIN].get("archive_projection_worker")
        available = worker is not None and worker.available
        states = (
            await worker.async_projection_status(user)
            if available
            else await self.hass.async_add_executor_job(store.projection_status, user)
        )
        try:
            from .archive_projection import statistic_id
        except ImportError:

            def statistic_id(metric, user):
                return None

        metrics = []
        from . import _resolve_backfill_entity_id

        for sample_type, (state, _) in sorted(states.items()):
            for metric in TYPE_METRICS.get(sample_type, ()):
                timeline = RULES[metric].mode == "timeline"
                metrics.append(
                    {
                        "metric": metric,
                        "sample_type": sample_type,
                        "state": state
                        if available or state != "current"
                        else "pending",
                        "statistic_id": None
                        if timeline
                        else statistic_id(metric, user),
                        "statistic_type": "change"
                        if RULES[metric].additive
                        else "mean",
                        "entity_id": _resolve_backfill_entity_id(
                            self.hass, user, metric
                        ),
                        "timeline_only": timeline,
                    }
                )
        return _response(
            {
                "user_id": user,
                "statistics_available": available,
                "metrics": metrics,
                "sample_types": list(TYPE_METRICS),
            }
        )

    async def _export(self, request, store, query, page, generations):
        response = web.StreamResponse(
            headers={
                **HEADERS,
                "Content-Type": "application/x-ndjson",
                "Content-Disposition": 'attachment; filename="health-bridge-archive.jsonl"',
            }
        )
        await response.prepare(request)

        async def emit(value):
            await response.write(
                (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()
            )

        samples = tombstones = 0
        try:
            await emit(
                {
                    "kind": "manifest",
                    "schema_version": 1,
                    "user_id": query.user_id,
                    "sample_type": query.sample_type,
                    "start": query.start.isoformat(),
                    "end": query.end.isoformat(),
                    "tombstone_scope": "all_for_user_and_type",
                    "snapshot": False,
                }
            )
            while True:
                for sample, generation in zip(page.samples, generations, strict=True):
                    await emit(
                        {
                            "kind": "sample",
                            "user_id": query.user_id,
                            "sample_type": query.sample_type,
                            "sample": {
                                **_wire_sample(sample),
                                "owner_generation": generation,
                            },
                        }
                    )
                    samples += 1
                if page.next_cursor is None:
                    break
                query = replace(query, cursor=page.next_cursor)
                page, generations = await self.hass.async_add_executor_job(
                    store.query_samples_with_provenance, query
                )
            after = ""
            while rows := await self.hass.async_add_executor_job(
                store.tombstone_page,
                query.user_id,
                query.sample_type,
                after,
                query.limit,
            ):
                for row in rows:
                    await emit(
                        {
                            "kind": "tombstone",
                            "user_id": query.user_id,
                            "sample_type": query.sample_type,
                            **row,
                        }
                    )
                    tombstones += 1
                after = rows[-1]["uuid"]
            await emit(
                {"kind": "complete", "samples": samples, "tombstones": tombstones}
            )
        except ConnectionError, RuntimeError:
            # Disconnected streams cannot be retried with a second HTTP response.
            return response
        except Exception:
            await emit(
                {"kind": "error", "error": "archive_unavailable", "complete": False}
            )
        await response.write_eof()
        return response


def register_archive_views(hass):
    """Register once; views resolve the current store after entry reloads."""
    data = hass.data[DOMAIN]
    if not data.get("archive_api_registered"):
        hass.http.register_view(ArchiveView(hass))
        hass.http.register_view(ArchiveCardView())
        data["archive_api_registered"] = True
