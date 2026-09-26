"""Home Assistant backup hooks for the separate original-sample database."""

import sqlite3

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .archive_store import ArchiveStoreError
from .const import DOMAIN


async def async_pre_backup(hass: HomeAssistant) -> None:
    """Fail the backup if the archive cannot be safely checkpointed."""
    domain_data = hass.data.get(DOMAIN, {})
    if "archive_store" not in domain_data:
        return
    if (store := domain_data["archive_store"]) is None:
        raise HomeAssistantError("Health Bridge archive is unavailable for backup")
    try:
        await hass.async_add_executor_job(store.begin_backup)
    except (OSError, sqlite3.Error, ArchiveStoreError) as exc:
        raise HomeAssistantError("Health Bridge archive checkpoint failed") from exc


async def async_post_backup(hass: HomeAssistant) -> None:
    """Home Assistant calls this on success and failure; always resume writes."""
    if (store := hass.data.get(DOMAIN, {}).get("archive_store")) is not None:
        await hass.async_add_executor_job(store.end_backup)
