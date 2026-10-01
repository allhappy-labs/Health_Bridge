"""Config flow for Health Bridge integration."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr

from . import async_delete_device_for_entry
from .archive_store import ArchiveStoreError
from .const import DOMAIN

CONF_DEVICE_ID = "device_id"
CONF_APP_TYPE = "app_type"
CONF_NUTRIENT_MASS_UNIT = "nutrient_mass_unit"
CONF_WATER_VOLUME_UNIT = "water_volume_unit"

APP_TYPE_HEALTH_ASSISTANT_LINK = "health_assistant_link"
APP_TYPE_PHONE_ASSISTANT_LINK = "phone_assistant_link"

ENTRY_TITLE_HEALTH_BRIDGE = "Health Bridge"
ENTRY_TITLE_PHONE_BRIDGE = "Phone Bridge"

DEFAULT_NUTRIENT_MASS_UNIT = "g"
DEFAULT_WATER_VOLUME_UNIT = "mL"
MINIMUM_TOKEN_LENGTH = 32


def _entry_unique_id(app_type: str) -> str:
    """Return a stable identifier that never contains authentication material."""
    return f"{DOMAIN}:{app_type}"


def _new_token() -> str:
    """Create a copy-friendly token with 256 bits of randomness."""
    return secrets.token_urlsafe(32)


def _normalized_strong_token(value: object) -> str | None:
    """Validate a user-supplied shared token without logging it."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    return token if len(token) >= MINIMUM_TOKEN_LENGTH else None


def _build_options_schema(
    mass_unit: str = DEFAULT_NUTRIENT_MASS_UNIT,
    water_unit: str = DEFAULT_WATER_VOLUME_UNIT,
) -> vol.Schema:
    """Build the options form schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_NUTRIENT_MASS_UNIT,
                default=mass_unit,
            ): vol.In(["g", "oz"]),
            vol.Required(
                CONF_WATER_VOLUME_UNIT,
                default=water_unit,
            ): vol.In(["mL", "fl_oz"]),
        }
    )


class HealthBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Health Bridge."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Choose which Assistant Link app is initiating shared setup."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[
                APP_TYPE_HEALTH_ASSISTANT_LINK,
                APP_TYPE_PHONE_ASSISTANT_LINK,
            ],
        )

    async def async_step_health_assistant_link(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Set up Health Bridge from Health Assistant Link."""
        return await self._async_step_app(
            APP_TYPE_HEALTH_ASSISTANT_LINK, user_input
        )

    async def async_step_phone_assistant_link(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Set up Health Bridge from Phone Assistant Link."""
        return await self._async_step_app(
            APP_TYPE_PHONE_ASSISTANT_LINK, user_input
        )

    async def _async_step_app(
        self, app_type: str, user_input: dict | None
    ) -> FlowResult:
        """Create the single shared integration entry for either app."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = _normalized_strong_token(user_input.get(CONF_TOKEN))
            if token is None:
                errors[CONF_TOKEN] = "weak_token"
            else:
                await self.async_set_unique_id(_entry_unique_id(app_type))
                self._abort_if_unique_id_configured()

                # Create the config entry; options can be set/changed later
                return self.async_create_entry(
                    title=(
                        ENTRY_TITLE_PHONE_BRIDGE
                        if app_type == APP_TYPE_PHONE_ASSISTANT_LINK
                        else ENTRY_TITLE_HEALTH_BRIDGE
                    ),
                    data={
                        CONF_TOKEN: token,
                        CONF_APP_TYPE: app_type,
                    },
                    options={
                        CONF_NUTRIENT_MASS_UNIT: DEFAULT_NUTRIENT_MASS_UNIT,
                        CONF_WATER_VOLUME_UNIT: DEFAULT_WATER_VOLUME_UNIT,
                    },
                )

        return self.async_show_form(
            step_id=app_type,
            data_schema=vol.Schema(
                {vol.Required(CONF_TOKEN, default=_new_token()): str}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Handle updates to config entry data from the settings UI."""
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            token = _normalized_strong_token(user_input.get(CONF_TOKEN))
            if token is None:
                errors = {CONF_TOKEN: "weak_token"}
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_TOKEN: token},
                )
        else:
            errors = {}

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_TOKEN, default=entry.data.get(CONF_TOKEN, "")): str}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options."""

    def __init__(self) -> None:
        self._selected_device_id: str | None = None
        self._approval_user_id: str | None = None
        self._approval_claim_id: str | None = None

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        menu_options = ["units", "edit_delete"]
        if self.config_entry.data.get(CONF_APP_TYPE) == APP_TYPE_HEALTH_ASSISTANT_LINK:
            menu_options.append("archive_approvals")
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_archive_approvals(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Let an administrator select one pending Health Bridge phone claim."""
        if self.config_entry.data.get(CONF_APP_TYPE) != APP_TYPE_HEALTH_ASSISTANT_LINK:
            return self.async_abort(reason="archive_unavailable")
        store = self.hass.data.get(DOMAIN, {}).get("archive_store")
        if store is None:
            return self.async_abort(reason="archive_unavailable")
        claims = await self.hass.async_add_executor_job(
            store.list_pending_owner_claims, datetime.now(timezone.utc)
        )
        if not claims:
            return self.async_abort(reason="no_pending_archive_claims")
        if user_input is not None:
            selected = next(
                (item for item in claims if item.claim.claim_id == user_input["claim_id"]),
                None,
            )
            if selected is None:
                return self.async_abort(reason="claim_not_found")
            self._approval_user_id = selected.user_id
            self._approval_claim_id = selected.claim.claim_id
            return await self.async_step_archive_approval_confirm()
        return self.async_show_form(
            step_id="archive_approvals",
            data_schema=vol.Schema(
                {
                    vol.Required("claim_id"): vol.In(
                        {
                            item.claim.claim_id: (
                                f"{item.user_id} — {item.claim.fingerprint}"
                            )
                            for item in claims
                        }
                    )
                }
            ),
        )

    async def async_step_archive_approval_confirm(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Require the matching iPhone fingerprint before changing ownership."""
        if self.config_entry.data.get(CONF_APP_TYPE) != APP_TYPE_HEALTH_ASSISTANT_LINK:
            return self.async_abort(reason="archive_unavailable")
        store = self.hass.data.get(DOMAIN, {}).get("archive_store")
        if store is None or self._approval_user_id is None or self._approval_claim_id is None:
            return self.async_abort(reason="archive_unavailable")
        now = datetime.now(timezone.utc)
        claim = await self.hass.async_add_executor_job(
            store.pending_owner_claim, self._approval_user_id, now
        )
        if claim is None or claim.claim_id != self._approval_claim_id:
            return self.async_abort(reason="claim_not_found")
        state = await self.hass.async_add_executor_job(
            store.owner_status, self._approval_user_id, None, now
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input["fingerprint"].strip().lower() != claim.fingerprint:
                errors["fingerprint"] = "fingerprint_mismatch"
            else:
                try:
                    if user_input["action"] == "approve":
                        await self.hass.async_add_executor_job(
                            store.approve_owner, self._approval_user_id,
                            self._approval_claim_id, datetime.now(timezone.utc),
                        )
                    else:
                        await self.hass.async_add_executor_job(
                            store.reject_owner, self._approval_user_id,
                            self._approval_claim_id,
                        )
                except ArchiveStoreError as exc:
                    if exc.code != "claim_not_found":
                        raise
                    return self.async_abort(reason="claim_not_found")
                else:
                    return self.async_create_entry(
                        title="", data=dict(self.config_entry.options)
                    )
        return self.async_show_form(
            step_id="archive_approval_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("fingerprint"): str,
                    vol.Required("action"): vol.In(
                        {"approve": "Approve uploader", "reject": "Reject claim"}
                    ),
                }
            ),
            description_placeholders={
                "user_id": self._approval_user_id,
                "fingerprint": claim.fingerprint,
                "transfer_warning": (
                    "This replaces the approved phone immediately. Old-phone-only originals "
                    "remain archived; back up Home Assistant before transferring."
                    if state.generation > 0 else
                    "This is the first approved archive uploader for this user."
                ),
            },
            errors=errors,
        )

    async def async_step_units(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        cur = self.config_entry.options
        mass_unit = cur.get(CONF_NUTRIENT_MASS_UNIT, DEFAULT_NUTRIENT_MASS_UNIT)
        vol_unit = cur.get(CONF_WATER_VOLUME_UNIT, DEFAULT_WATER_VOLUME_UNIT)

        return self.async_show_form(
            step_id="units",
            data_schema=_build_options_schema(mass_unit, vol_unit),
        )

    async def async_step_edit_delete(
        self, user_input: dict | None = None
    ) -> FlowResult:
        devices = self._get_health_bridge_devices()
        if not devices:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            self._selected_device_id = user_input[CONF_DEVICE_ID]
            return await self.async_step_confirm_delete()

        return self.async_show_form(
            step_id="edit_delete",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): vol.In(
                        {
                            device.id: self._get_device_label(device)
                            for device in devices
                        }
                    )
                }
            ),
        )

    async def async_step_confirm_delete(
        self, user_input: dict | None = None
    ) -> FlowResult:
        device = self._get_selected_device()
        if device is None:
            return self.async_abort(reason="device_not_found")

        if user_input is not None:
            if user_input["confirm"]:
                await async_delete_device_for_entry(
                    self.hass, self.config_entry, device.id
                )
                return self.async_create_entry(
                    title="",
                    data=dict(self.config_entry.options),
                )

            return await self.async_step_init()

        return self.async_show_form(
            step_id="confirm_delete",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            description_placeholders={"device_name": self._get_device_label(device)},
        )

    def _get_health_bridge_devices(self) -> list[dr.DeviceEntry]:
        """Return Health Bridge devices attached to this config entry."""
        device_registry = dr.async_get(self.hass)
        return [
            device
            for device in dr.async_entries_for_config_entry(
                device_registry, self.config_entry.entry_id
            )
            if any(identifier[0] == DOMAIN for identifier in device.identifiers)
        ]

    def _get_selected_device(self) -> dr.DeviceEntry | None:
        """Return the device selected for deletion."""
        if self._selected_device_id is None:
            return None

        return dr.async_get(self.hass).async_get(self._selected_device_id)

    @staticmethod
    def _get_device_label(device: dr.DeviceEntry) -> str:
        """Build a readable device label for the options UI."""
        return device.name_by_user or device.name or device.id
