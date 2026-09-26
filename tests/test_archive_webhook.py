"""HTTP archive contracts with real Home Assistant setup and durable SQLite."""

import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from custom_components.health_bridge.archive_protocol import ArchiveCapability
from custom_components.health_bridge.archive_store import ArchiveQuery, ArchiveStore


WEBHOOK = "/api/webhook/health_bridge"
HAL_TOKEN = "health-assistant-compatibility-token-00001"
PAL_TOKEN = "phone-assistant-compatibility-token-000001"
USER = "archive-person"
TYPE = "HKQuantityTypeIdentifierStepCount"


@pytest.fixture
async def archive_client(bridge_client, bridge_entries, hass):
    entry = bridge_entries[0]
    hass.config_entries.async_update_entry(entry, data={**entry.data, "user_id": USER})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return bridge_client


def payload(kind="archive_batch", **changes):
    if kind == "archive_batch":
        result = json.loads(
            (
                Path(__file__).parents[1]
                / "docs/protocol/fixtures/archive-batch-v2.json"
            ).read_text()
        )
    else:
        result = {
            "request_type": kind,
            "protocol_version": 2,
            "request_id": "control-1",
        }
    return {**result, "user_id": USER, "token": HAL_TOKEN, **changes}


async def send(client, data):
    return await client.post(WEBHOOK, json=data)


async def stored_samples(hass):
    store = hass.data["health_bridge"]["archive_store"]
    return await hass.async_add_executor_job(
        store.query_samples,
        ArchiveQuery(
            USER,
            TYPE,
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 1, 1, tzinfo=timezone.utc),
        ),
    )


async def test_capability_advertises_only_available_archive_contract(archive_client):
    response = await send(archive_client, payload("archive_capability"))
    assert response.status == 200
    capability = ArchiveCapability.from_dict(await response.json())
    assert capability.request_id == "control-1"
    assert capability.archive_available is True
    assert capability.statistics_available is False
    assert TYPE in capability.supported_sample_types
    assert "steps" in capability.supported_metrics
    assert capability.max_batch_bytes <= 262144
    assert capability.max_samples_per_batch <= 200


async def test_capability_exposes_all_readable_metrics_and_unique_original_types(
    archive_client,
):
    response = await send(archive_client, payload("archive_capability"))
    capability = ArchiveCapability.from_dict(await response.json())
    assert len(capability.supported_metrics) == 107
    assert len(set(capability.supported_sample_types)) == 99
    assert "HKWorkoutType" in capability.supported_sample_types
    assert "HKWorkoutTypeIdentifier" not in capability.supported_sample_types
    assert not {
        "uv_exposure_sed",
        "net_calories",
        "last_sync_time",
        "test_connection",
    }.intersection(capability.supported_metrics)


async def test_unbound_hal_entry_preserves_declared_v1_user_namespace(
    bridge_client, hass
):
    response = await send(bridge_client, payload())
    assert response.status == 200
    assert len((await stored_samples(hass)).samples) == 1


async def test_duplicate_batch_with_changed_content_is_conflict(archive_client):
    assert (await send(archive_client, payload())).status == 200
    changed = payload()
    changed["samples"][0]["payload"]["raw_value"] = 99
    response = await send(archive_client, changed)
    assert response.status == 409
    assert "archive_commit" not in await response.json()


@pytest.mark.parametrize(
    "kind", ["archive_capability", "archive_batch", "archive_status"]
)
@pytest.mark.parametrize("token", [PAL_TOKEN, "wrong", "invalid-\N{LOCK}"])
async def test_archive_rejects_non_health_token_before_schema_validation(
    archive_client, kind, token
):
    response = await send(
        archive_client, payload(kind, token=token, protocol_version="invalid")
    )
    assert response.status == 401


async def test_large_legacy_live_envelope_keeps_existing_http_limit(archive_client):
    data = {
        "token": HAL_TOKEN,
        "user_id": USER,
        "data": {"test_connection": [{"value": True}]},
    }
    response = await archive_client.post(
        WEBHOOK,
        data=json.dumps(data).encode() + b" " * 262144,
        headers={"Content-Type": "application/json"},
    )
    assert response.status == 200
    assert (await response.json())["backfill_protocol"] == 1


async def test_archive_store_outage_keeps_live_and_capability_available(
    archive_client, bridge_entries, hass, monkeypatch
):
    def fail_open(cls, path):
        raise sqlite3.OperationalError("private database details")

    monkeypatch.setattr(ArchiveStore, "open", classmethod(fail_open))
    assert await hass.config_entries.async_reload(bridge_entries[0].entry_id)
    response = await send(archive_client, payload("archive_capability"))
    assert response.status == 200
    assert (await response.json())["archive_available"] is False
    assert (await send(archive_client, payload())).status == 503
    assert (await send(archive_client, payload("archive_status"))).status == 503
    response = await send(
        archive_client,
        {
            "token": HAL_TOKEN,
            "user_id": USER,
            "data": {"test_connection": [{"value": True}]},
        },
    )
    assert response.status == 200


async def test_unbound_entry_status_never_leaks_another_users_projection(bridge_client):
    assert (await send(bridge_client, payload())).status == 200
    response = await send(
        bridge_client, payload("archive_status", user_id="another-person")
    )
    assert response.status == 200
    assert (await response.json())["metrics"] == []


@pytest.mark.parametrize(
    "kind", ["archive_capability", "archive_batch", "archive_status"]
)
async def test_archive_rejects_cross_user_access_before_schema_validation(
    archive_client, kind, hass
):
    response = await send(
        archive_client, payload(kind, user_id="victim", protocol_version="invalid")
    )
    assert response.status == 403
    assert (await stored_samples(hass)).samples == ()


async def test_durable_ack_and_lost_response_retry_survive_entry_reload(
    archive_client, bridge_entries, hass
):
    first = await send(archive_client, payload())
    assert first.status == 200
    receipt = await first.json()
    assert receipt["archive_commit"] == "committed"
    assert receipt["committed_samples"] == 1
    assert receipt["projection_state"] == "pending"
    assert len((await stored_samples(hass)).samples) == 1
    previous_store = hass.data["health_bridge"]["archive_store"]
    await hass.config_entries.async_reload(bridge_entries[0].entry_id)
    await hass.async_block_till_done()
    assert hass.data["health_bridge"]["archive_store"] is not previous_store
    retried = await send(archive_client, payload())
    assert retried.status == 200
    assert await retried.json() == receipt
    assert len((await stored_samples(hass)).samples) == 1


async def test_status_is_read_only_and_projection_failure_does_not_undo_receipt(
    archive_client, hass
):
    response = await send(archive_client, payload())
    assert response.status == 200
    receipt = await response.json()
    response = await send(archive_client, payload("archive_status"))
    assert response.status == 200
    assert {
        item["metric"]: item["state"] for item in (await response.json())["metrics"]
    }["steps"] == "pending"
    store = hass.data["health_bridge"]["archive_store"]
    jobs = await hass.async_add_executor_job(store.claim_projection_jobs, 10)
    assert len(jobs) == 1
    assert jobs[0].attempts == 1
    await hass.async_add_executor_job(
        store.fail_projection_job, jobs[0].job_id, "statistics_unavailable"
    )
    response = await send(archive_client, payload("archive_status"))
    assert {item["metric"]: item for item in (await response.json())["metrics"]}[
        "steps"
    ] == {"metric": "steps", "state": "failed", "last_error": "statistics_unavailable"}
    assert await (await send(archive_client, payload())).json() == receipt
    jobs = await hass.async_add_executor_job(store.claim_projection_jobs, 10)
    await hass.async_add_executor_job(store.complete_projection_job, jobs[0].job_id)
    response = await send(archive_client, payload("archive_status"))
    assert {
        item["metric"]: item["state"] for item in (await response.json())["metrics"]
    }["steps"] == "pending"


async def test_empty_outbox_cannot_claim_statistics_current_without_worker(
    archive_client,
):
    data = payload()
    data["deletions"] = [data["samples"][0]["uuid"]]
    data["samples"] = []
    response = await send(archive_client, data)
    assert response.status == 200
    assert (await response.json())["projection_state"] == "pending"
    response = await send(archive_client, payload("archive_status"))
    assert response.status == 200
    assert (await response.json())["metrics"] == [
        {"metric": "steps", "state": "pending", "last_error": None}
    ]


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"request_id": "../invalid"}, "invalid_request"),
        ({"batch_id": ""}, "invalid_request"),
        (
            {"sample_type": "HKQuantityTypeIdentifierInventedType"},
            "unsupported_sample_type",
        ),
        ({"protocol_version": 1}, "unsupported_protocol"),
    ],
)
async def test_invalid_batches_never_commit(archive_client, hass, changes, code):
    response = await send(archive_client, payload(**changes))
    assert response.status == 422
    assert (await response.json())["error"] == code
    assert (await stored_samples(hass)).samples == ()


async def test_raw_body_limit_counts_whitespace_and_chunked_upload(
    archive_client, hass
):
    raw = json.dumps(payload()).encode() + b" " * 262144

    async def chunks():
        yield raw[:1000]
        yield raw[1000:]

    response = await archive_client.post(
        WEBHOOK, data=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status == 413
    assert (await stored_samples(hass)).samples == ()


async def test_sample_count_limit_never_commits(archive_client, hass):
    data = payload()
    data["samples"] *= 201
    response = await send(archive_client, data)
    assert response.status == 413
    assert (await stored_samples(hass)).samples == ()


async def test_batch_rate_limit_leaves_status_available(archive_client, monkeypatch):
    from custom_components.health_bridge import archive_webhook

    monkeypatch.setattr(archive_webhook, "ARCHIVE_BATCHES_PER_MINUTE", 2)
    assert (await send(archive_client, payload())).status == 200
    assert (await send(archive_client, payload())).status == 200
    limited = await send(archive_client, payload())
    assert limited.status == 429
    assert limited.headers["Retry-After"]
    assert (await send(archive_client, payload("archive_status"))).status == 200


async def test_storage_failure_cannot_acknowledge(archive_client, monkeypatch, hass):
    def fail_commit(self, batch):
        raise sqlite3.OperationalError("private health value")

    monkeypatch.setattr(ArchiveStore, "commit_batch", fail_commit)
    response = await send(archive_client, payload())
    assert response.status == 503
    assert await response.json() == {"ok": False, "error": "archive_unavailable"}
    assert (await stored_samples(hass)).samples == ()
