"""Bounded, revision-fenced UUID inventories against SQLite and the HAL route."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import Barrier
from uuid import UUID

import pytest

from custom_components.health_bridge import archive_store as api
from custom_components.health_bridge.archive_protocol import (
    ArchiveLimits,
    ArchiveProtocolError,
    validate_archive_request,
)
from tests.test_archive_webhook import (
    payload as http_payload,
    send,
    PAL_TOKEN,
    USER,
    SECRET,
)
from tests.test_archive_owner import OwnedStore

TYPE = "HKQuantityTypeIdentifierStepCount"
START = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
END = datetime.fromisoformat("2024-01-02T00:00:00+00:00")


def payload(**changes):
    value = json.loads(Path("docs/protocol/fixtures/archive-batch-v2.json").read_text())
    return {**value, **changes}


def commit(store, **changes):
    return store.commit_batch(
        validate_archive_request(payload(**changes), limits=ArchiveLimits())
    )


def query(**changes):
    return api.ArchiveInventoryQuery(
        **{
            "user_id": "person-1",
            "sample_type": TYPE,
            "start": START,
            "end": END,
            **changes,
        }
    )


def request(**changes):
    return http_payload(
        "archive_inventory",
        sample_type=TYPE,
        start="2024-01-01T00:00:00Z",
        end="2024-01-02T00:00:00Z",
        limit=200,
        cursor=None,
        **changes,
    )


@pytest.fixture
async def archive_client(bridge_client, bridge_entries, hass):
    entry = bridge_entries[0]
    hass.config_entries.async_update_entry(entry, data={**entry.data, "user_id": USER})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    store = hass.data["health_bridge"]["archive_store"]
    claim = await hass.async_add_executor_job(
        store.claim_owner, USER, SECRET, datetime.now(timezone.utc)
    )
    await hass.async_add_executor_job(
        store.approve_owner, USER, claim.claim_id, datetime.now(timezone.utc)
    )
    return bridge_client


@pytest.fixture
def store(tmp_path):
    return OwnedStore(api.ArchiveStore.open(tmp_path / "archive.sqlite"))


def test_inventory_pages_same_time_ids_and_scope(store):
    samples = [
        {**deepcopy(payload()["samples"][0]), "uuid": str(UUID(int=i))}
        for i in range(1, 202)
    ]
    commit(store, samples=samples[:200])
    commit(store, batch_id="second", samples=samples[200:])
    page = store.inventory_page(query())
    assert page.sample_ids == tuple(s["uuid"] for s in samples[:200])
    assert page.revision == 2 and page.next_cursor
    last = store.inventory_page(query(cursor=page.next_cursor))
    assert last.sample_ids == (samples[-1]["uuid"],)
    assert last.revision == 2 and last.next_cursor is None
    assert store.inventory_page(query(user_id="other")).sample_ids == ()
    assert store.inventory_page(query(sample_type="HKWorkoutType")).sample_ids == ()
    for changes in (
        {"user_id": "other"},
        {"sample_type": "HKWorkoutType"},
        {"start": START.replace(hour=1)},
        {"end": END.replace(hour=1)},
        {"limit": 100},
    ):
        with pytest.raises(api.ArchiveStoreError, match="invalid_cursor"):
            store.inventory_page(query(cursor=page.next_cursor, **changes))


def test_half_open_start_membership_excludes_boundary_spanning_sample(store):
    template = payload()["samples"][0]
    times = [
        ("2023-12-31T23:00:00Z", "2024-01-01T01:00:00Z"),
        ("2024-01-01T00:00:00Z", "2024-01-02T02:00:00Z"),
        ("2024-01-02T00:00:00Z", "2024-01-02T00:00:00Z"),
    ]
    commit(
        store,
        samples=[
            {**template, "uuid": str(UUID(int=i + 1)), "start": s, "end": e}
            for i, (s, e) in enumerate(times)
        ],
    )
    assert store.inventory_page(query()).sample_ids == (str(UUID(int=2)),)


def test_revision_changes_between_pages_and_even_on_noop_new_batch(store):
    commit(store)
    commit(
        store,
        batch_id="another",
        samples=[{**payload()["samples"][0], "uuid": str(UUID(int=2))}],
    )
    page = store.inventory_page(query(limit=1))
    commit(store, batch_id="noop")
    with pytest.raises(api.ArchiveStoreError, match="inventory_changed"):
        store.inventory_page(query(limit=1, cursor=page.next_cursor))
    assert store.inventory_page(query()).revision == 3


def test_stale_conditional_delete_rolls_back_all_state_and_retry_is_exact(store):
    commit(store)
    revision = store.inventory_page(query()).revision
    deletion = validate_archive_request(
        payload(
            batch_id="delete",
            samples=[],
            deletions=[payload()["samples"][0]["uuid"]],
            expected_inventory_revision=revision,
            expected_owner_generation=1,
        ),
        limits=ArchiveLimits(),
    )
    commit(store, batch_id="concurrent")
    with sqlite3.connect(store._path) as db:
        before = list(db.iterdump())
    with pytest.raises(api.ArchiveStoreError, match="inventory_changed"):
        store.commit_batch(deletion)
    with sqlite3.connect(store._path) as db:
        assert list(db.iterdump()) == before
    deletion = replace(deletion, expected_inventory_revision=revision + 1)
    receipt = store.commit_batch(deletion)
    assert receipt.committed_deletions == 1
    assert store.inventory_page(query()).sample_ids == ()
    assert store.inventory_page(query()).revision == 3
    reopened = OwnedStore(api.ArchiveStore.open(store._path))
    assert reopened.commit_batch(deletion) == receipt
    assert reopened.inventory_page(query()).revision == 3


def test_two_concurrent_conditional_deletes_cannot_share_revision(store):
    samples = [{**payload()["samples"][0], "uuid": str(UUID(int=i))} for i in (1, 2)]
    commit(store, samples=samples)
    barrier = Barrier(2)

    def delete(index):
        # Separate store instances exercise SQLite's transaction lock, not RLock.
        writer = OwnedStore(api.ArchiveStore.open(store._path))
        batch = validate_archive_request(
            payload(
                batch_id=f"delete-{index}",
                samples=[],
                deletions=[samples[index]["uuid"]],
                expected_inventory_revision=1,
                expected_owner_generation=1,
            ),
            limits=ArchiveLimits(),
        )
        barrier.wait()
        try:
            return writer.commit_batch(batch).committed_deletions
        except api.ArchiveStoreError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(delete, range(2)))
    assert set(results) == {1, "inventory_changed"}
    assert len(store.inventory_page(query()).sample_ids) == 1
    assert store.inventory_page(query()).revision == 2


def test_other_user_writes_do_not_invalidate_inventory_and_delete_does(store):
    samples = [{**payload()["samples"][0], "uuid": str(UUID(int=i))} for i in (1, 2)]
    commit(store, samples=samples)
    first = store.inventory_page(query(limit=1))
    commit(store, user_id="other")
    assert store.inventory_page(query(limit=1, cursor=first.next_cursor)).revision == 1
    store.delete_user_archive("person-1")
    with pytest.raises(api.ArchiveStoreError, match="inventory_changed"):
        store.inventory_page(query(limit=1, cursor=first.next_cursor))
    commit(store, samples=samples)
    assert store.inventory_page(query()).revision == 3


@pytest.mark.parametrize("cursor", ["not-base64", "W10=", "bnVsbA=="])
def test_malformed_cursor_is_redacted(store, cursor):
    with pytest.raises(api.ArchiveStoreError, match="^invalid_cursor$"):
        store.inventory_page(query(cursor=cursor))


def test_migrates_schema_one_preserving_originals_and_receipts(tmp_path):
    path = tmp_path / "old.sqlite"
    # Build an actual v1 schema and populate it using a committed legacy payload.
    store = OwnedStore(api.ArchiveStore.open(path))
    first = commit(store)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE inventory_revisions")
        db.execute("PRAGMA user_version=1")
    migrated = OwnedStore(api.ArchiveStore.open(path))
    page = migrated.inventory_page(query())
    assert page.sample_ids == (payload()["samples"][0]["uuid"],)
    assert page.revision == 1
    assert commit(migrated) == first
    commit(migrated, batch_id="new")
    assert migrated.inventory_page(query()).revision == 2
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (3,)


@pytest.mark.parametrize("limit", [0, 201, True, 1.5])
def test_inventory_store_enforces_page_bound(store, limit):
    with pytest.raises(api.ArchiveStoreError, match="invalid_query"):
        store.inventory_page(query(limit=limit))


@pytest.mark.parametrize("revision", [-1, True, "1", None])
def test_conditional_revision_must_be_nonnegative_integer(revision):
    with pytest.raises(ArchiveProtocolError):
        validate_archive_request(
            payload(
                samples=[],
                deletions=[str(UUID(int=1))],
                expected_inventory_revision=revision,
            ),
            limits=ArchiveLimits(),
        )


def test_conditional_batch_cannot_upload_samples():
    with pytest.raises(ArchiveProtocolError):
        validate_archive_request(
            payload(expected_inventory_revision=1), limits=ArchiveLimits()
        )


@pytest.mark.parametrize("token", [PAL_TOKEN, "wrong"])
async def test_inventory_requires_hal_before_validation(archive_client, token):
    response = await send(
        archive_client, request(token=token, protocol_version="invalid")
    )
    assert response.status == 401


async def test_inventory_enforces_bound_user_before_validation(archive_client):
    response = await send(
        archive_client, request(user_id="other", protocol_version="invalid")
    )
    assert response.status == 403


async def test_inventory_route_and_stale_delete_error(archive_client):
    assert (await send(archive_client, http_payload())).status == 200
    response = await send(archive_client, request())
    assert response.status == 200
    page = await response.json()
    assert page == {
        "ok": True,
        "request_type": "archive_inventory",
        "protocol_version": 2,
        "request_id": "control-1",
        "sample_ids": [payload()["samples"][0]["uuid"]],
        "revision": 1,
        "owner_generation": 1,
        "next_cursor": None,
    }
    assert (
        await send(archive_client, http_payload(batch_id="concurrent"))
    ).status == 200
    response = await send(
        archive_client,
        http_payload(
            batch_id="deletion",
            samples=[],
            deletions=page["sample_ids"],
            expected_inventory_revision=page["revision"],
            expected_owner_generation=page["owner_generation"],
        ),
    )
    assert response.status == 409
    assert await response.json() == {"ok": False, "error": "inventory_changed"}


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 201},
        {"limit": True},
        {"start": "2024-01-01T00:00:00+00:00"},
        {"end": "2024-01-01T00:00:00Z"},
        {"cursor": "x" * 2049},
        {"sample_type": "HKQuantityTypeIdentifierUnknown"},
    ],
)
async def test_inventory_route_rejects_invalid_queries(archive_client, changes):
    data = request()
    data.update(changes)
    assert (await send(archive_client, data)).status in (413, 422)


def test_canonical_inventory_fixture():
    fixture = json.loads(
        Path("docs/protocol/fixtures/archive-inventory-v2.json").read_text()
    )
    validated = validate_archive_request(fixture["request"], limits=ArchiveLimits())
    assert validated.request_type == "archive_inventory"
    assert fixture["response"]["sample_ids"] == [payload()["samples"][0]["uuid"]]
