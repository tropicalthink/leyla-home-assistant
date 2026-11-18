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
from .repairs import async_sync_findings

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.EVENT,
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.UPDATE,
]

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
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"url": entry.data[CONF_URL]},
        ) from err

    hub = LeylaHub(hass, entry, api, session, account)
    for coordinator in (hub.now_playing, hub.updates, hub.findings, hub.counts):
        if coordinator is not None:
            await coordinator.async_config_entry_first_refresh()
    await hub.async_start()
    entry.runtime_data = hub
    entry.async_on_unload(hub.async_stop)

    server = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Leyla",
        model="Leyla server",
        name=entry.title,
        sw_version=(hub.updates.data or {}).get("running") if hub.updates else None,
        configuration_url=entry.data[CONF_URL],
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    hub.server_device_id = server.id

    if hub.updates is not None:
        updates = hub.updates

        def follow_version() -> None:
            running = (updates.data or {}).get("running")
            if running:
                dr.async_get(hass).async_update_device(server.id, sw_version=running)

        entry.async_on_unload(updates.async_add_listener(follow_version))

    if hub.findings is not None:
        findings = hub.findings

        def sync() -> None:
            async_sync_findings(hass, entry, findings.data or [])

        sync()
        entry.async_on_unload(findings.async_add_listener(sync))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LeylaConfigEntry) -> bool:
    """Close the live channel and unload the platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: LeylaConfigEntry) -> None:
    """Take the entry's repairs with it."""
    async_sync_findings(hass, entry, [])


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: LeylaConfigEntry, device: dr.DeviceEntry
) -> bool:
    """A TV that is gone for good may be removed; the server and a TV that is
    on the list right now may not (it would only come straight back)."""
    hub = entry.runtime_data
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        if identifier == entry.entry_id or identifier in hub.listed:
            return False
        hub.devices.pop(identifier, None)
    return True
