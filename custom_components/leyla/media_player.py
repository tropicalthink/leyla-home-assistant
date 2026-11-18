"""A media player for each TV on the account's Play on TV list.

A TV is listed while it runs Leyla signed in to this account (the web TV
shell on LG and Samsung, the app on Android TV and Fire TV). One that drops
off the list keeps its entity, unavailable, until it comes back.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.media_player import MediaPlayerEntity
from homeassistant.components.media_player.browse_media import (
    BrowseMedia,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.components.media_player.const import (
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.components.media_player.errors import BrowseError
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LeylaConfigEntry
from .api import LeylaError, poster_url
from .const import BROWSE_PAGE, DOMAIN, SEARCH_LIMIT
from .hub import LeylaCommandError, LeylaHub, RemoteDevice, signal_devices, tv_device_info

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
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.SEARCH_MEDIA
)

# Browse ids: the root, Continue watching, a library (with an offset for
# its next page), a title to open (a series or season) and an item to play.
ROOT = "leyla"
CONTINUE = "continue"
LIBRARY = "library"
TITLE = "title"
ITEM = "item"


def play_id(item_id: str, source_id: str | None = None) -> str:
    """The browse id that plays a title, with its file when one is known."""
    return f"{ITEM}:{item_id}:{source_id}" if source_id else f"{ITEM}:{item_id}"


VOLUME_FEATURES = MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_MUTE

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LeylaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a player for every TV seen, now and as they arrive."""
    hub = entry.runtime_data
    added = hub.players_added

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
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_should_poll = False
    _attr_media_image_remotely_accessible = False

    def __init__(self, hub: LeylaHub, device_id: str) -> None:
        self.hub = hub
        self.tv_id = device_id
        self._attr_unique_id = device_id
        self._attr_device_info = tv_device_info(hub, device_id)

    async def async_added_to_hass(self) -> None:
        """Follow the hub's TV list."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_devices(self.hub.entry.entry_id), self.async_write_ha_state
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """A TV removed by hand gets its player back if it ever returns."""
        self.hub.players_added.discard(self.tv_id)

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
        media_id = media_id.strip()
        for prefix in (f"{ITEM}:", f"{TITLE}:"):
            media_id = media_id.removeprefix(prefix)
        item_id, _, source_id = media_id.partition(":")
        if not item_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_media_source"
            )
        try:
            if not source_id:
                found = await self.hub.primary_source(item_id)
                if found is None:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="no_media_source"
                    )
                item_id, source_id = found
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

    # ---- browsing and search -----------------------------------------------

    async def async_browse_media(
        self, media_content_type: MediaType | str | None = None, media_content_id: str | None = None
    ) -> BrowseMedia:
        """Continue watching and each library, down to seasons and episodes."""
        try:
            if not media_content_id or media_content_id == ROOT:
                return self._root()
            kind, _, rest = media_content_id.partition(":")
            if kind == CONTINUE:
                return await self._continue()
            if kind == LIBRARY:
                library_id, _, offset = rest.partition(":")
                return await self._library(library_id, int(offset or 0))
            if kind == TITLE:
                return await self._title(rest)
        except LeylaError as err:
            raise BrowseError(f"Leyla did not answer: {err}") from err
        raise BrowseError(f"Unknown media: {media_content_id}")

    def _root(self) -> BrowseMedia:
        children = [
            BrowseMedia(
                title="Continue watching",
                media_class=MediaClass.DIRECTORY,
                media_content_id=CONTINUE,
                media_content_type=MediaType.VIDEO,
                can_play=False,
                can_expand=True,
            )
        ]
        children += [
            BrowseMedia(
                title=name or library_id,
                media_class=MediaClass.DIRECTORY,
                media_content_id=f"{LIBRARY}:{library_id}",
                media_content_type=MediaType.VIDEO,
                can_play=False,
                can_expand=True,
                thumbnail=self._library_cover(library_id),
            )
            for library_id, name in self.hub.libraries.items()
        ]
        return BrowseMedia(
            title="Leyla",
            media_class=MediaClass.DIRECTORY,
            media_content_id=ROOT,
            media_content_type=MediaType.VIDEO,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    async def _continue(self) -> BrowseMedia:
        items = await self.hub.api.continue_watching()
        children = []
        for row in items:
            if not isinstance(row, dict) or not row.get("catalog_item_id"):
                continue
            title = str(row.get("title") or "")
            if row.get("series_title"):
                where = (
                    f"S{row['season_number']} E{row['episode_number']} · "
                    if row.get("season_number") is not None
                    and row.get("episode_number") is not None
                    else ""
                )
                title = f"{row['series_title']} · {where}{title}"
            children.append(
                BrowseMedia(
                    title=title,
                    media_class=MediaClass.EPISODE
                    if row.get("type") == "episode"
                    else MediaClass.MOVIE,
                    media_content_id=play_id(row["catalog_item_id"], row.get("media_source_id")),
                    media_content_type=MediaType.VIDEO,
                    can_play=True,
                    can_expand=False,
                    thumbnail=self._thumb(row),
                )
            )
        return BrowseMedia(
            title="Continue watching",
            media_class=MediaClass.DIRECTORY,
            media_content_id=CONTINUE,
            media_content_type=MediaType.VIDEO,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.VIDEO,
        )

    async def _library(self, library_id: str, offset: int) -> BrowseMedia:
        page = await self.hub.api.catalog_list(
            library_id=library_id, sort="title", limit=BROWSE_PAGE, offset=offset
        )
        children = [self._entry(row) for row in page["items"] if isinstance(row, dict)]
        if page.get("has_more"):
            children.append(
                BrowseMedia(
                    title="More",
                    media_class=MediaClass.DIRECTORY,
                    media_content_id=f"{LIBRARY}:{library_id}:{offset + BROWSE_PAGE}",
                    media_content_type=MediaType.VIDEO,
                    can_play=False,
                    can_expand=True,
                )
            )
        return BrowseMedia(
            title=self.hub.libraries.get(library_id) or "Library",
            media_class=MediaClass.DIRECTORY,
            media_content_id=f"{LIBRARY}:{library_id}:{offset}"
            if offset
            else f"{LIBRARY}:{library_id}",
            media_content_type=MediaType.VIDEO,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.MOVIE,
        )

    async def _title(self, item_id: str) -> BrowseMedia:
        detail = await self.hub.api.catalog_item(item_id)
        node = self._entry(detail)
        node.children = [
            self._entry(child) for child in detail.get("children") or [] if isinstance(child, dict)
        ]
        return node

    def _entry(self, row: dict[str, Any]) -> BrowseMedia:
        """A film or an episode plays; a series or a season opens."""
        kind = row.get("type")
        title = str(row.get("title") or "")
        if kind == "episode" and row.get("episode_number") is not None:
            title = f"{row['episode_number']}. {title}"
        elif kind in ("film", "series") and row.get("year"):
            title = f"{title} ({row['year']})"
        opens = kind in ("series", "season")
        media_class = {
            "film": MediaClass.MOVIE,
            "series": MediaClass.TV_SHOW,
            "season": MediaClass.SEASON,
            "episode": MediaClass.EPISODE,
        }.get(str(kind), MediaClass.VIDEO)
        return BrowseMedia(
            title=title,
            media_class=media_class,
            media_content_id=f"{TITLE if opens else ITEM}:{row['id']}",
            media_content_type=MediaType.TVSHOW if opens else MediaType.VIDEO,
            can_play=True,
            can_expand=opens,
            thumbnail=self._thumb(row),
        )

    def _library_cover(self, library_id: str) -> str | None:
        """A library's newest poster, as Leyla's own library cards show it."""
        for row in self.hub.counts.data or []:
            if isinstance(row, dict) and str(row.get("library_id")) == library_id:
                covers = row.get("cover_artwork")
                if isinstance(covers, list) and covers and isinstance(covers[0], str):
                    return self._thumb({"poster_artwork": {"url": covers[0]}})
        return None

    def _thumb(self, row: dict[str, Any]) -> str | None:
        """The poster, through Home Assistant's own proxy: never a token in a URL."""
        url = poster_url(row)
        if url is None:
            return None
        return self.get_browse_image_url(MediaType.VIDEO, url.removeprefix(self.hub.api.url))

    async def async_get_browse_image(
        self,
        media_content_type: MediaType | str,
        media_content_id: str,
        media_image_id: str | None = None,
    ) -> tuple[bytes | None, str | None]:
        """A poster for the browser, fetched with the sign-in from this server only."""
        try:
            return await self.hub.api.fetch_image(media_content_id)
        except LeylaError:
            return None, None

    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        """Find films and series by title or description (and voice: "play
        Arrival on the living room TV")."""
        try:
            page = await self.hub.api.catalog_list(
                query=query.search_query, kind="title", limit=SEARCH_LIMIT
            )
        except LeylaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            ) from err
        return SearchMedia(
            result=[self._entry(row) for row in page["items"] if isinstance(row, dict)]
        )
