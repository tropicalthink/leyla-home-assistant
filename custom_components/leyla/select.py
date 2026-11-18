"""Audio and subtitle pickers for each TV, from what the TV says it has."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LeylaConfigEntry
from .api import LeylaError
from .const import DOMAIN
from .hub import LeylaCommandError, LeylaHub, signal_devices, tv_device_info

PARALLEL_UPDATES = 0

# The subtitle picker's first option: no subtitles.
OFF = "off"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LeylaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Two pickers per TV, now and as TVs arrive."""
    hub = entry.runtime_data

    @callback
    def add_new() -> None:
        new: list[SelectEntity] = []
        for tv in hub.devices:
            if tv in hub.selects_added:
                continue
            hub.selects_added.add(tv)
            new += [LeylaTrackSelect(hub, tv, "audio"), LeylaTrackSelect(hub, tv, "subtitle")]
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(async_dispatcher_connect(hass, signal_devices(entry.entry_id), add_new))


def track_label(track: dict[str, Any], taken: set[str]) -> str:
    """The TV's own label, made unique: two "English" tracks read "English (2)"."""
    label = str(track.get("label") or f"Track {track.get('stream_index')}")
    if label in taken:
        n = 2
        while f"{label} ({n})" in taken:
            n += 1
        label = f"{label} ({n})"
    taken.add(label)
    return label


class LeylaTrackSelect(SelectEntity):
    """The audio or subtitle track of what a TV is playing."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: LeylaHub, device_id: str, kind: str) -> None:
        self.hub = hub
        self.tv_id = device_id
        self.kind = kind
        self._attr_translation_key = f"{kind}_track"
        self._attr_unique_id = f"{device_id}_{kind}_track"
        self._attr_device_info = tv_device_info(hub, device_id)

    async def async_added_to_hass(self) -> None:
        """Follow the hub's TV list."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_devices(self.hub.entry.entry_id), self.async_write_ha_state
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """A removed TV gets its pickers back if it ever returns."""
        self.hub.selects_added.discard(self.tv_id)

    @property
    def _state(self) -> dict[str, Any]:
        device = self.hub.devices.get(self.tv_id)
        return (device.state if device else None) or {}

    def _tracks(self) -> dict[str, int | None]:
        """Label to stream index, in the TV's order; subtitles start with Off."""
        tracks: dict[str, int | None] = {OFF: None} if self.kind == "subtitle" else {}
        taken: set[str] = set(tracks)
        raw = self._state.get(f"{self.kind}_tracks")
        for track in raw if isinstance(raw, list) else []:
            if isinstance(track, dict) and isinstance(track.get("stream_index"), int):
                tracks[track_label(track, taken)] = track["stream_index"]
        return tracks

    @property
    def available(self) -> bool:
        """While the TV is listed and is playing something with tracks to pick."""
        return (
            self.hub.connected
            and self.tv_id in self.hub.listed
            and bool(self._state.get("item_id"))
            and len(self._tracks()) > (1 if self.kind == "subtitle" else 0)
        )

    @property
    def options(self) -> list[str]:
        """What the TV offers."""
        return list(self._tracks())

    @property
    def current_option(self) -> str | None:
        """The track the TV is using."""
        index = self._state.get(f"{self.kind}_stream_index")
        for label, stream in self._tracks().items():
            if stream == index:
                return label
        return OFF if self.kind == "subtitle" else None

    async def async_select_option(self, option: str) -> None:
        """Switch the TV to that track."""
        tracks = self._tracks()
        if option not in tracks:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_such_track",
                translation_placeholders={"track": option},
            )
        try:
            await self.hub.control(self.tv_id, self.kind, stream_index=tracks[option])
        except LeylaCommandError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": err.code},
            ) from err
        except LeylaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            ) from err
