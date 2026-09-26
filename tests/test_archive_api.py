"""Authenticated archive HTTP contracts against real SQLite and HA routes."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import UUID

import pytest

from custom_components.health_bridge.archive_protocol import (
    ArchiveLimits,
    validate_archive_request,
)

BASE = "/api/health_bridge/archive/person-1"
TYPE = "HKQuantityTypeIdentifierStepCount"
PARAMS = {
    "sample_type": TYPE,
    "start": "2024-01-01T00:00:00Z",
    "end": "2025-01-01T00:00:00Z",
}
SECRET = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
OTHER_SECRET = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"


@pytest.fixture
async def api(bridge_entries, hass, hass_client):
    store = hass.data["health_bridge"]["archive_store"]
    original = json.loads(
        (
            Path(__file__).parents[1] / "docs/protocol/fixtures/archive-batch-v2.json"
        ).read_text()
    )
    for user in ("person-1", "person-2"):
        claim = await hass.async_add_executor_job(
            store.claim_owner, user, SECRET, datetime.now(timezone.utc)
        )
        await hass.async_add_executor_job(
            store.approve_owner, user, claim.claim_id, datetime.now(timezone.utc)
        )
        batch = deepcopy(original)
        batch["user_id"] = user
        batch["samples"] = [
            dict(deepcopy(original["samples"][0]), uuid=str(UUID(int=i)))
            for i in range(1, 5)
        ]
        batch["deletions"] = [str(UUID(int=99))]
        await hass.async_add_executor_job(
            store.commit_batch,
            validate_archive_request(batch, limits=ArchiveLimits()),
            SECRET,
        )
    return await hass_client()


@pytest.mark.parametrize("operation", ["samples", "sample", "export", "status"])
async def test_data_requires_ha_auth(bridge_entries, hass_client_no_auth, operation):
    client = await hass_client_no_auth()
    response = await client.get(f"{BASE}/{operation}", params=PARAMS)
    assert response.status == 401


async def test_admin_owner_claim_fingerprint_approval_and_transfer(api, hass):
    store = hass.data["health_bridge"]["archive_store"]
    claim = await hass.async_add_executor_job(
        store.claim_owner, "person-1", OTHER_SECRET, datetime.now(timezone.utc)
    )
    owner = await api.get(f"{BASE}/owner")
    assert owner.status == 200
    body = await owner.json()
    assert body["pending_claim"]["fingerprint"] == claim.fingerprint
    assert body["owner_generation"] == 1
    assert body["owner_state"] == "active"
    bad = await api.post(
        f"{BASE}/owner-approve",
        json={
            "claim_id": claim.claim_id,
            "confirm_user_id": "person-2",
            "confirm": "APPROVE",
        },
    )
    assert bad.status == 400
    approved = await api.post(
        f"{BASE}/owner-approve",
        json={
            "claim_id": claim.claim_id,
            "confirm_user_id": "person-1",
            "confirm": "APPROVE",
        },
    )
    assert approved.status == 200
    assert (await approved.json())["owner_generation"] == 2
    owner = await api.get(f"{BASE}/owner")
    assert (await owner.json())["pending_claim"] is None


async def test_non_admin_cannot_approve_owner(api, hass_admin_user):
    hass_admin_user.groups = []
    response = await api.post(
        f"{BASE}/owner-approve",
        json={
            "claim_id": "11111111-1111-1111-1111-111111111111",
            "confirm_user_id": "person-1",
            "confirm": "APPROVE",
        },
    )
    assert response.status == 403


async def test_rejecting_stale_claim_returns_not_found_and_keeps_current_claim(
    api, hass
):
    store = hass.data["health_bridge"]["archive_store"]
    claim = await hass.async_add_executor_job(
        store.claim_owner, "person-1", OTHER_SECRET, datetime.now(timezone.utc)
    )
    response = await api.post(
        f"{BASE}/owner-reject",
        json={
            "claim_id": "11111111-1111-1111-1111-111111111111",
            "confirm_user_id": "person-1",
            "confirm": "REJECT",
        },
    )
    assert response.status == 404
    assert await response.json() == {"ok": False, "error": "claim_not_found"}
    owner = await api.get(f"{BASE}/owner")
    assert (await owner.json())["pending_claim"]["claim_id"] == claim.claim_id


@pytest.mark.parametrize(
    "operation", ["samples", "sample", "export", "status", "retry", "delete"]
)
async def test_non_admin_cannot_guess_another_archive(api, hass_admin_user, operation):
    hass_admin_user.groups = []
    if operation in {"retry", "delete"}:
        response = await api.post(
            f"{BASE}/{operation}",
            json={"confirm_user_id": "person-1", "confirm": "DELETE"},
        )
    else:
        response = await api.get(f"{BASE}/{operation}", params=PARAMS)
    assert response.status == 403


async def test_range_keyset_ties_scope_and_detail(api):
    response = await api.get(f"{BASE}/samples", params={**PARAMS, "limit": "2"})
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    first = await response.json()
    assert [s["uuid"] for s in first["samples"]] == [str(UUID(int=1)), str(UUID(int=2))]
    assert {s["owner_generation"] for s in first["samples"]} == {1}
    response = await api.get(
        f"{BASE}/samples",
        params={**PARAMS, "limit": "2", "cursor": first["next_cursor"]},
    )
    second = await response.json()
    assert [s["uuid"] for s in second["samples"]] == [
        str(UUID(int=3)),
        str(UUID(int=4)),
    ]
    assert second["next_cursor"] is None
    for path, params in (
        (BASE.replace("person-1", "person-2"), PARAMS),
        (BASE, {**PARAMS, "end": "2025-02-01T00:00:00Z"}),
    ):
        response = await api.get(
            f"{path}/samples", params={**params, "cursor": first["next_cursor"]}
        )
        assert response.status == 400
    response = await api.get(
        f"{BASE}/sample", params={"sample_type": TYPE, "uuid": str(UUID(int=1))}
    )
    detail = (await response.json())["sample"]
    assert detail["payload"]["raw_value"] == 12
    assert detail["owner_generation"] == 1
    response = await api.get(
        f"{BASE.replace('person-1', 'absent')}/sample",
        params={"sample_type": TYPE, "uuid": str(UUID(int=1))},
    )
    assert response.status == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"start": "2024-01-01"},
        {"start": "2024-01-01T01:00:00+01:00"},
        {"end": "2024-01-01T00:00:00Z"},
        {"start": "1800-01-01T00:00:00Z"},
        {"limit": "501"},
        {"limit": "0"},
        {"limit": "x"},
        {"cursor": "bad"},
        {"sample_type": "unknown"},
    ],
)
async def test_invalid_bounds_pages_and_types_are_redacted(api, changes):
    response = await api.get(f"{BASE}/samples", params={**PARAMS, **changes})
    assert response.status == 400
    assert (await response.json()) == {"ok": False, "error": "invalid_query"}


async def test_export_is_versioned_jsonl_with_scoped_tombstones(api):
    response = await api.get(f"{BASE}/export", params={**PARAMS, "limit": "2"})
    assert response.status == 200
    assert response.content_type == "application/x-ndjson"
    rows = [json.loads(line) for line in (await response.text()).splitlines()]
    assert rows[0]["schema_version"] == 1
    assert rows[0]["tombstone_scope"] == "all_for_user_and_type"
    assert rows[-1] == {"kind": "complete", "samples": 4, "tombstones": 1}
    assert [r["sample"]["uuid"] for r in rows if r["kind"] == "sample"] == [
        str(UUID(int=i)) for i in range(1, 5)
    ]
    assert {r["sample"]["owner_generation"] for r in rows if r["kind"] == "sample"} == {
        1
    }
    assert [r["uuid"] for r in rows if r["kind"] == "tombstone"] == [str(UUID(int=99))]
    assert all(r.get("user_id", "person-1") == "person-1" for r in rows)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"confirm_user_id": "person-2", "confirm": "DELETE"},
        {"confirm_user_id": "person-1"},
    ],
)
async def test_delete_requires_exact_explicit_confirmation(api, body):
    assert (await api.post(f"{BASE}/delete", json=body)).status == 400
    response = await api.get(f"{BASE}/samples", params=PARAMS)
    assert len((await response.json())["samples"]) == 4


async def test_confirmed_delete_only_removes_chosen_archive(api):
    response = await api.post(
        f"{BASE}/delete", json={"confirm_user_id": "person-1", "confirm": "DELETE"}
    )
    assert response.status == 200
    assert (await response.json())["recorder_copies_deleted"] is False
    for user, count in (("person-1", 0), ("person-2", 4)):
        response = await api.get(
            f"/api/health_bridge/archive/{user}/samples", params=PARAMS
        )
        assert len((await response.json())["samples"]) == count


async def test_status_never_claims_current_without_recorder(api, hass):
    store = hass.data["health_bridge"]["archive_store"]
    for job in await hass.async_add_executor_job(store.claim_projection_jobs, 500):
        await hass.async_add_executor_job(store.complete_projection_job, job.job_id)
    response = await api.get(f"{BASE}/status")
    assert response.status == 200
    data = await response.json()
    assert data["statistics_available"] is False
    assert data["metrics"][0]["state"] == "pending"
    assert data["metrics"][0]["statistic_id"].startswith("health_bridge:steps_")
    response = await api.get(f"{BASE.replace('person-1', 'absent')}/status")
    assert (await response.json())["metrics"] == []


async def test_retry_only_requeues_the_selected_users_failed_jobs(api, hass):
    store = hass.data["health_bridge"]["archive_store"]
    jobs = await hass.async_add_executor_job(store.claim_projection_jobs, 500)
    for job in jobs:
        await hass.async_add_executor_job(
            store.fail_projection_job, job.job_id, "statistics_unavailable"
        )
    assert (await api.post(f"{BASE}/retry", json={})).status == 200
    for user, state in (("person-1", "pending"), ("person-2", "failed")):
        assert (await hass.async_add_executor_job(store.projection_status, user))[TYPE][
            0
        ] == state


async def test_export_reads_bounded_pages_and_midstream_errors_cannot_claim_complete(
    api, hass, monkeypatch, caplog
):
    store = hass.data["health_bridge"]["archive_store"]
    original = store.query_samples_with_provenance
    calls = []

    def query(request):
        calls.append(request.limit)
        if len(calls) == 2:
            raise sqlite3.OperationalError("private-health-detail")
        return original(request)

    monkeypatch.setattr(store, "query_samples_with_provenance", query)
    response = await api.get(f"{BASE}/export", params={**PARAMS, "limit": "2"})
    body = await response.text()
    rows = [json.loads(line) for line in body.splitlines()]
    assert [row["kind"] for row in rows] == ["manifest", "sample", "sample", "error"]
    assert rows[-1]["complete"] is False
    assert calls == [2, 2]
    assert "private-health-detail" not in body + caplog.text


async def test_storage_error_redacts_values_and_logs(api, hass, monkeypatch, caplog):
    def fail(query):
        raise sqlite3.OperationalError("sensitive-health-value token-secret")

    monkeypatch.setattr(
        hass.data["health_bridge"]["archive_store"],
        "query_samples_with_provenance",
        fail,
    )
    response = await api.get(f"{BASE}/samples", params=PARAMS)
    assert response.status == 503
    assert "sensitive-health-value" not in await response.text() + caplog.text
    assert "token-secret" not in caplog.text


async def test_registered_card_resource(bridge_client):
    response = await bridge_client.get("/health_bridge/health-bridge-archive.js")
    assert response.status == 200
    script = await response.text()
    for contract in (
        "health-bridge-archive",
        "api/health_bridge/archive/",
        "pending",
        "current",
        "failed",
        "showSaveFilePicker",
        "confirm_user_id",
        "hui-statistics-graph-card",
    ):
        assert contract in script


async def test_phone_only_frontend_does_not_register_a_missing_archive_resource(
    hass, enable_custom_integrations, hass_client_no_auth
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain="health_bridge",
        data={
            "app_type": "phone_assistant_link",
            "token": "phone-archive-card-test-token-0000001",
        },
        title="Phone Bridge",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = await hass_client_no_auth()
    response = await client.get("/health_bridge/health-bridge-archive.js")
    assert response.status == 200
