"""Scan a library now (administrator sign-ins only)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LeylaConfigEntry
from .api import LeylaError
from .const import DOMAIN
from .hub import LeylaHub

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LeylaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One Scan button per library, for an administrator."""
    hub = entry.runtime_data
    if not hub.is_admin:
        return
    async_add_entities(
        LeylaScanButton(hub, entry, library_id, name) for library_id, name in hub.libraries.items()
    )


class LeylaScanButton(ButtonEntity):
    """Look for new, changed and removed files in one library now."""

    _attr_has_entity_name = True
    _attr_translation_key = "scan"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub: LeylaHub, entry: LeylaConfigEntry, library_id: str, name: str) -> None:
        self.hub = hub
        self.library_id = library_id
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_scan_{library_id}"
        self._attr_translation_placeholders = {"library": name or library_id}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_press(self) -> None:
        """Start the scan; the server runs it in the background."""
        try:
            await self.hub.api.start_scan(self.library_id)
        except LeylaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="scan_failed",
                translation_placeholders={"error": str(err)},
            ) from err
