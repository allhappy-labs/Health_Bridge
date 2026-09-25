"""Home Assistant fixtures for the upstream compatibility baseline."""

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry


HAL_TOKEN = "health-assistant-compatibility-token-00001"
PAL_TOKEN = "phone-assistant-compatibility-token-000001"


@pytest.fixture
def hass_config_dir(tmp_path):
    """Keep the real archive database isolated for each HA lifecycle test."""
    return str(tmp_path)


@pytest.fixture
async def bridge_entries(hass, enable_custom_integrations):
    """Load both real config entries through Home Assistant's setup path."""
    entries = []
    for app_type, token, title in (
        ("health_assistant_link", HAL_TOKEN, "Health Bridge"),
        ("phone_assistant_link", PAL_TOKEN, "Phone Bridge"),
    ):
        entry = MockConfigEntry(
            domain="health_bridge",
            data={"app_type": app_type, "token": token},
            unique_id=token,
            title=title,
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        entries.append(entry)
    await hass.async_block_till_done()
    return tuple(entries)


@pytest.fixture
async def bridge_client(bridge_entries, hass_client_no_auth):
    """Exercise the registered shared webhook over HTTP."""
    return await hass_client_no_auth()
