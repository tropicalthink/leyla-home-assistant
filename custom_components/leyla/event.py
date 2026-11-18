"""An event entity that fires when a library's titles change.

The same moment as the `leyla_library_updated` bus event, as an entity, so
an automation can pick it in the UI and its history shows when each
library last changed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LeylaConfigEntry
from .const import DOMAIN
from .hub import signal_library

PARALLEL_UPDATES = 0

EVENT_UPDATED = "updated"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LeylaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One per server."""
    async_add_entities([LeylaLibraryEvent(entry)])


class LeylaLibraryEvent(EventEntity):
    """Titles in a library were added, changed or removed."""

    _attr_has_entity_name = True
    _attr_translation_key = "library_updated"

    def __init__(self, entry: LeylaConfigEntry) -> None:
        self.entry = entry
        self._attr_event_types = [EVENT_UPDATED]
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_library_updated"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        """Listen for the hub's library signal."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_library(self.entry.entry_id), self._on_library
            )
        )

    @callback
    def _on_library(self, data: dict[str, Any]) -> None:
        self._trigger_event(
            EVENT_UPDATED,
            {"library_id": data.get("library_id"), "library_name": data.get("library_name")},
        )
        self.async_write_ha_state()
