"""A media player for each TV on the account's Play on TV list.

A TV is listed while it runs Leyla signed in to this account (the web TV
shell on LG and Samsung, the app on Android TV and Fire TV). One that drops
off the list keeps its entity, unavailable, until it comes back.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LeylaConfigEntry
from .api import LeylaError
from .const import DOMAIN
from .hub import LeylaCommandError, LeylaHub, RemoteDevice, signal_devices

STATE_MAP: dict[str, MediaPlayerState] = {
    "idle": MediaPlayerState.IDLE,
    "loading": MediaPlayerState.BUFFERING,
    "buffering": MediaPlayerState.BUFFERING,
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "ended": MediaPlayerState.IDLE,
    "error": MediaPlayerState.IDLE,
}

BASE_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.PLAY_MEDIA
)
VOLUME_FEATURES = MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_MUTE

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LeylaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a player for every TV seen, now and as they arrive."""
    hub = entry.runtime_data
    added: set[str] = set()

    # TVs seen before a restart come back at once, unavailable until listed.
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    restored: list[LeylaMediaPlayer] = []
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.domain != "media_player" or reg.unique_id in added:
            continue
        known = devices.async_get(reg.device_id) if reg.device_id else None
        hub.devices.setdefault(
            reg.unique_id,
            RemoteDevice(id=reg.unique_id, name=(known.name if known else None) or "TV"),
        )
        added.add(reg.unique_id)
        restored.append(LeylaMediaPlayer(hub, reg.unique_id))
    if restored:
        async_add_entities(restored)

    @callback
    def add_new() -> None:
        new = [LeylaMediaPlayer(hub, tv) for tv in hub.devices if tv not in added]
        added.update(entity.tv_id for entity in new)
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(async_dispatcher_connect(hass, signal_devices(entry.entry_id), add_new))


class LeylaMediaPlayer(MediaPlayerEntity):
    """One TV, controlled through the live channel."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_media_image_remotely_accessible = False

    def __init__(self, hub: LeylaHub, device_id: str) -> None:
        self.hub = hub
        self.tv_id = device_id
        self._attr_unique_id = device_id
        device = hub.devices[device_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device.name,
            manufacturer="Leyla",
            model="TV",
        )
        if hub.server_device_id is not None:
            self._attr_device_info["via_device_id"] = hub.server_device_id

    async def async_added_to_hass(self) -> None:
        """Follow the hub's TV list."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_devices(self.hub.entry.entry_id), self.async_write_ha_state
            )
        )

    # ---- state -------------------------------------------------------------

    @property
    def _device(self) -> RemoteDevice:
        return self.hub.devices[self.tv_id]

    @property
    def _state(self) -> dict[str, Any]:
        return self._device.state or {}

    @property
    def available(self) -> bool:
        """Listed right now, over a live connection."""
        return self.hub.connected and self.tv_id in self.hub.listed

    @property
    def state(self) -> MediaPlayerState | None:
        """The TV's status; a TV that has not reported yet is idle."""
        return STATE_MAP.get(str(self._state.get("status")), MediaPlayerState.IDLE)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Volume only where the TV owns its volume; next only with a next episode."""
        features = BASE_FEATURES
        if isinstance(self._state.get("volume"), dict):
            features |= VOLUME_FEATURES
        if self._state.get("has_next"):
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        return features

    @property
    def media_content_id(self) -> str | None:
        """The catalog item playing."""
        return self._state.get("item_id")

    @property
    def media_content_type(self) -> MediaType | None:
        """An episode when the state has an episode line, else a film."""
        if not self._state.get("item_id"):
            return None
        return MediaType.EPISODE if self._state.get("detail") else MediaType.MOVIE

    @property
    def media_title(self) -> str | None:
        """For an episode the episode line ("S1 E3 · Pilot"), else the title."""
        return self._state.get("detail") or self._state.get("title")

    @property
    def media_series_title(self) -> str | None:
        """For an episode, the series."""
        return self._state.get("title") if self._state.get("detail") else None

    @property
    def media_duration(self) -> int | None:
        """Length in seconds."""
        duration = self._state.get("duration_ms")
        return int(duration) // 1000 if isinstance(duration, (int, float)) else None

    @property
    def media_position(self) -> int | None:
        """Position in seconds at the last report."""
        position = self._state.get("position_ms")
        return int(position) // 1000 if isinstance(position, (int, float)) else None

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When the TV last reported: Home Assistant moves the clock on from there."""
        return self._device.reported_at if self._state.get("position_ms") is not None else None

    @property
    def volume_level(self) -> float | None:
        """0 to 1, when the TV owns its volume."""
        volume = self._state.get("volume")
        return float(volume["level"]) if isinstance(volume, dict) and "level" in volume else None

    @property
    def is_volume_muted(self) -> bool | None:
        """Muted, when the TV owns its volume."""
        volume = self._state.get("volume")
        return bool(volume.get("muted")) if isinstance(volume, dict) else None

    @property
    def media_image_hash(self) -> str | None:
        """The item id: a new title is a new picture."""
        return self._state.get("item_id")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Who controls the TV and the last error, kept small."""
        attributes: dict[str, Any] = {}
        controller = self._device.controller
        attributes["controlled_from"] = (
            controller.get("name") if isinstance(controller, dict) else None
        )
        if self._state.get("status") == "error":
            attributes["error"] = self._state.get("error")
        return attributes

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """The poster, fetched with the sign-in's header; never a token in a URL."""
        item_id = self._state.get("item_id")
        if not item_id:
            return None, None
        image = await self.hub.poster(item_id)
        if image is None:
            return None, None
        return image

    # ---- commands ----------------------------------------------------------

    async def _control(self, action: str, **fields: Any) -> None:
        try:
            await self.hub.control(self.tv_id, action, **fields)
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

    async def async_media_play(self) -> None:
        """Play."""
        await self._control("play")

    async def async_media_pause(self) -> None:
        """Pause."""
        await self._control("pause")

    async def async_media_stop(self) -> None:
        """Stop and go back to where the TV was."""
        await self._control("stop")

    async def async_media_next_track(self) -> None:
        """The next episode."""
        await self._control("next")

    async def async_media_seek(self, position: float) -> None:
        """Seek, in seconds."""
        await self._control("seek", position_ms=max(0, int(position * 1000)))

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the TV's own volume, 0 to 1."""
        await self._control("volume", level=min(1.0, max(0.0, float(volume))))

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute."""
        await self._control("mute", muted=bool(mute))

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a title by its catalog id, or `item_id:media_source_id` for one file."""
        item_id, _, source_id = media_id.strip().partition(":")
        if not item_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_media_source"
            )
        try:
            if not source_id:
                source_id = await self.hub.primary_source(item_id) or ""
            if not source_id:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="no_media_source"
                )
            await self.hub.play(self.tv_id, item_id, source_id)
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
