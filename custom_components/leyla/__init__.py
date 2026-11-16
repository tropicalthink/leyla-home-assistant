"""The Leyla integration: TVs as media players, streams, library events.

Local only: it talks to the operator's own Leyla server and nothing else.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LeylaApi, LeylaAuthError, LeylaError
from .const import DOMAIN
from .hub import LeylaHub

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER, Platform.SENSOR]

type LeylaConfigEntry = ConfigEntry[LeylaHub]


async def async_setup_entry(hass: HomeAssistant, entry: LeylaConfigEntry) -> bool:
    """Sign in, open the live channel and set up the platforms."""
    session = async_get_clientsession(hass)
    api = LeylaApi(session, entry.data[CONF_URL], entry.data[CONF_ACCESS_TOKEN])
    try:
        account = await api.me()
    except LeylaAuthError as err:
        raise ConfigEntryAuthFailed from err
    except LeylaError as err:
        raise ConfigEntryNotReady(str(err)) from err

    hub = LeylaHub(hass, entry, api, session, account)
    if hub.now_playing is not None:
        await hub.now_playing.async_config_entry_first_refresh()
    await hub.async_start()
    entry.runtime_data = hub
    entry.async_on_unload(hub.async_stop)

    server = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Leyla",
        model="Leyla server",
        name=entry.title,
        configuration_url=entry.data[CONF_URL],
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    hub.server_device_id = server.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LeylaConfigEntry) -> bool:
    """Close the live channel and unload the platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
