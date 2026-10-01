"""Administrator approval from Health Bridge's Configure flow."""

from datetime import datetime, timezone

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry


NOW = datetime.now(timezone.utc)
PHONE_A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
PHONE_B = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"


async def _start_approval_flow(hass, entry):
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["type"] == FlowResultType.MENU
    assert "archive_approvals" in menu["menu_options"]
    return await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "archive_approvals"}
    )


async def test_configure_approves_matching_phone_fingerprint(bridge_entries, hass):
    hal_entry, pal_entry = bridge_entries
    store = hass.data["health_bridge"]["archive_store"]
    claim = await hass.async_add_executor_job(
        store.claim_owner, "person-1", PHONE_A, NOW
    )

    pal_menu = await hass.config_entries.options.async_init(pal_entry.entry_id)
    assert "archive_approvals" not in pal_menu["menu_options"]

    picker = await _start_approval_flow(hass, hal_entry)
    assert picker["type"] == FlowResultType.FORM
    assert picker["step_id"] == "archive_approvals"
    assert claim.claim_id in picker["data_schema"].schema["claim_id"].container

    confirm = await hass.config_entries.options.async_configure(
        picker["flow_id"], {"claim_id": claim.claim_id}
    )
    assert confirm["step_id"] == "archive_approval_confirm"
    assert confirm["description_placeholders"]["fingerprint"] == claim.fingerprint
    assert confirm["description_placeholders"]["user_id"] == "person-1"

    mismatch = await hass.config_entries.options.async_configure(
        confirm["flow_id"], {"fingerprint": "000000000000", "action": "approve"}
    )
    assert mismatch["errors"]["fingerprint"] == "fingerprint_mismatch"
    assert (
        await hass.async_add_executor_job(store.owner_status, "person-1", PHONE_A, NOW)
    ).state == "pending"

    result = await hass.config_entries.options.async_configure(
        mismatch["flow_id"], {"fingerprint": claim.fingerprint, "action": "approve"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert (
        await hass.async_add_executor_job(store.owner_status, "person-1", PHONE_A, NOW)
    ).state == "active"


async def test_configure_transfer_requires_matching_fingerprint(bridge_entries, hass):
    hal_entry, _ = bridge_entries
    store = hass.data["health_bridge"]["archive_store"]
    first = await hass.async_add_executor_job(
        store.claim_owner, "person-1", PHONE_A, NOW
    )
    await hass.async_add_executor_job(
        store.approve_owner, "person-1", first.claim_id, NOW
    )
    replacement = await hass.async_add_executor_job(
        store.claim_owner, "person-1", PHONE_B, NOW
    )
    picker = await _start_approval_flow(hass, hal_entry)
    confirm = await hass.config_entries.options.async_configure(
        picker["flow_id"], {"claim_id": replacement.claim_id}
    )
    assert confirm["description_placeholders"]["transfer_warning"]
    await hass.config_entries.options.async_configure(
        confirm["flow_id"],
        {"fingerprint": replacement.fingerprint, "action": "approve"},
    )
    assert (
        await hass.async_add_executor_job(store.owner_status, "person-1", PHONE_A, NOW)
    ).state == "not_owner"
    assert (
        await hass.async_add_executor_job(store.owner_status, "person-1", PHONE_B, NOW)
    ).generation == 2


async def test_configure_rejects_stale_claim_without_approving(bridge_entries, hass):
    hal_entry, _ = bridge_entries
    store = hass.data["health_bridge"]["archive_store"]
    claim = await hass.async_add_executor_job(
        store.claim_owner, "person-1", PHONE_A, NOW
    )
    picker = await _start_approval_flow(hass, hal_entry)
    confirm = await hass.config_entries.options.async_configure(
        picker["flow_id"], {"claim_id": claim.claim_id}
    )
    await hass.async_add_executor_job(store.reject_owner, "person-1", claim.claim_id)
    stale = await hass.config_entries.options.async_configure(
        confirm["flow_id"], {"fingerprint": claim.fingerprint, "action": "approve"}
    )
    assert stale["type"] == FlowResultType.ABORT
    assert stale["reason"] == "claim_not_found"
    assert (
        await hass.async_add_executor_job(store.owner_status, "person-1", PHONE_A, NOW)
    ).state == "unbound"


async def test_configure_rejects_pending_claim_without_changing_owner(
    bridge_entries, hass
):
    hal_entry, _ = bridge_entries
    store = hass.data["health_bridge"]["archive_store"]
    claim = await hass.async_add_executor_job(
        store.claim_owner, "person-1", PHONE_A, NOW
    )
    picker = await _start_approval_flow(hass, hal_entry)
    confirm = await hass.config_entries.options.async_configure(
        picker["flow_id"], {"claim_id": claim.claim_id}
    )

    result = await hass.config_entries.options.async_configure(
        confirm["flow_id"], {"fingerprint": claim.fingerprint, "action": "reject"}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert (
        await hass.async_add_executor_job(store.owner_status, "person-1", PHONE_A, NOW)
    ).state == "unbound"
    menu = await hass.config_entries.options.async_init(hal_entry.entry_id)
    empty = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "archive_approvals"}
    )
    assert empty["type"] == FlowResultType.ABORT
    assert empty["reason"] == "no_pending_archive_claims"


async def test_legacy_hal_entry_without_app_type_can_approve(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(
        domain="health_bridge",
        data={"token": "legacy-health-assistant-token-00000001"},
        unique_id="legacy-health-assistant-token-00000001",
        title="Health Bridge",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    store = hass.data["health_bridge"]["archive_store"]
    claim = await hass.async_add_executor_job(
        store.claim_owner, "olhapi", PHONE_A, NOW
    )

    picker = await _start_approval_flow(hass, entry)
    confirm = await hass.config_entries.options.async_configure(
        picker["flow_id"], {"claim_id": claim.claim_id}
    )
    result = await hass.config_entries.options.async_configure(
        confirm["flow_id"], {"fingerprint": claim.fingerprint, "action": "approve"}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert (
        await hass.async_add_executor_job(store.owner_status, "olhapi", PHONE_A, NOW)
    ).state == "active"
