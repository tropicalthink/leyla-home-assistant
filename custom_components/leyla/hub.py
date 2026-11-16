"""The live channel: one WebSocket per config entry, and what it carries.

The hub owns the socket, reconnects it with backoff, keeps the account's
TV list (topic `remote`), relays `catalog.changed` as a Home Assistant
event and nudges the admin streams coordinator on `admin.now_playing`.
Entities read the hub's state and listen for its dispatcher signal.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import random
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    LeylaApi,
    LeylaApiError,
    LeylaAuthError,
    LeylaConnectionError,
    LeylaError,
    poster_url,
)
from .const import (
    BACKOFF_FIRST,
    BACKOFF_MAX,
    CLOSE_ACCESS_CHANGED,
    CLOSE_SIGNED_OUT,
    COMMAND_TIMEOUT,
    DOMAIN,
    EVENT_LIBRARY_UPDATED,
    HEARTBEAT,
    KEEPALIVE,
    NOW_PLAYING_DEBOUNCE,
    NOW_PLAYING_POLL,
    STABLE_AFTER,
    TOPIC_ADMIN_NOW_PLAYING,
    TOPIC_CATALOG,
    TOPIC_REMOTE,
)

_LOGGER = logging.getLogger(__name__)

POSTER_CACHE_SIZE = 8
RESUBSCRIBE_AFTER = 5.0


class LeylaCommandError(LeylaError):
    """A live command answered with an error ack."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class RemoteDevice:
    """One TV on the account's list, as last reported."""

    id: str
    name: str
    state: dict[str, Any] | None = None
    controller: dict[str, Any] | None = None
    # When `state` last changed: the moment its position_ms was true.
    reported_at: datetime = field(default_factory=dt_util.utcnow)


def signal_devices(entry_id: str) -> str:
    """The dispatcher signal sent when the TV list or the link changes."""
    return f"{DOMAIN}_{entry_id}_devices"


class NowPlayingCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """The admin's live sessions: event-driven, with a slow poll behind it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: LeylaApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} now playing",
            update_interval=NOW_PLAYING_POLL,
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=NOW_PLAYING_DEBOUNCE, immediate=False
            ),
        )
        self.api = api

    async def _async_update_data(self) -> list[dict[str, Any]]:
        try:
            return await self.api.now_playing()
        except LeylaAuthError as err:
            raise ConfigEntryAuthFailed from err
        except LeylaApiError as err:
            if err.status == 403:
                # No longer an admin: report nothing rather than fail forever.
                return []
            raise UpdateFailed(str(err)) from err
        except LeylaError as err:
            raise UpdateFailed(str(err)) from err


class LeylaHub:
    """One live socket, reconnected with backoff, and the state it carries."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: LeylaApi,
        session: aiohttp.ClientSession,
        account: dict[str, Any],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        self._session = session
        self.account = account
        self.is_admin = bool(account.get("is_admin"))
        self.now_playing: NowPlayingCoordinator | None = (
            NowPlayingCoordinator(hass, entry, api) if self.is_admin else None
        )
        self.devices: dict[str, RemoteDevice] = {}
        # The server's own device, which every TV hangs under.
        self.server_device_id: str | None = None
        self.listed: set[str] = set()
        self.you: str | None = None
        self.connected = False
        self.libraries: dict[str, str] = {}
        self.connect_count = 0
        self.last_close: str | None = None
        self._revision: int | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._subscribing: dict[int, str] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._signed_out = False
        self._posters: OrderedDict[str, tuple[bytes, str] | None] = OrderedDict()
        self._unsub_keepalive: Callable[[], None] | None = None

    # ---- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        """Load what the socket does not carry, then open the socket."""
        try:
            await self._load_libraries()
        except LeylaAuthError as err:
            raise ConfigEntryAuthFailed from err
        except LeylaError:
            _LOGGER.debug("library names unavailable for now")
        self._task = self.entry.async_create_background_task(
            self.hass, self._run(), name=f"{DOMAIN} live channel"
        )
        self._unsub_keepalive = async_track_time_interval(self.hass, self._keepalive, KEEPALIVE)

    async def async_stop(self) -> None:
        """Close the socket and stop reconnecting."""
        self._stopping = True
        if self._unsub_keepalive:
            self._unsub_keepalive()
            self._unsub_keepalive = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._fail_pending()

    async def _keepalive(self, _now: datetime) -> None:
        """Keep the 30-day sliding sign-in alive while only the socket is used."""
        try:
            await self.api.me()
        except LeylaAuthError:
            self._sign_out()
        except LeylaError:
            pass

    @callback
    def _sign_out(self) -> None:
        """The token no longer works: stop and ask the person to sign in again."""
        if self._signed_out:
            return
        self._signed_out = True
        self._stopping = True
        self.entry.async_start_reauth(self.hass)

    async def _run(self) -> None:
        backoff = BACKOFF_FIRST
        while not self._stopping:
            opened_at: float | None = None
            try:
                ticket = await self.api.live_ticket()
                async with self._session.ws_connect(
                    self.api.live_url(ticket),
                    heartbeat=HEARTBEAT,
                    max_msg_size=1024 * 1024,
                ) as ws:
                    opened_at = self.hass.loop.time()
                    await self._on_open(ws)
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            self._on_text(message.data)
                        elif message.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSE,
                        ):
                            break
                    close_code = ws.close_code
                self.last_close = f"closed {close_code}"
                if close_code == CLOSE_SIGNED_OUT:
                    self._sign_out()
                elif close_code == CLOSE_ACCESS_CHANGED:
                    # Access changed (made or no longer an admin, say): set up
                    # again so the streams sensors match the account.
                    self._stopping = True
                    self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            except LeylaAuthError:
                self.last_close = "signed out"
                self._sign_out()
            except (TimeoutError, LeylaError, aiohttp.ClientError) as err:
                self.last_close = f"{type(err).__name__}: {err}"
                _LOGGER.debug("live channel: %s", self.last_close)
            finally:
                self._on_closed()
            if self._stopping:
                break
            if opened_at is not None and self.hass.loop.time() - opened_at >= STABLE_AFTER:
                backoff = BACKOFF_FIRST
            delay = backoff * random.uniform(0.8, 1.2)
            backoff = min(backoff * 2, BACKOFF_MAX)
            await asyncio.sleep(delay)

    async def _on_open(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self.connected = True
        self.connect_count += 1
        # A reconnect is a resync, and a restarted server counts revisions
        # from the start again.
        self._revision = None
        topics = [TOPIC_REMOTE, TOPIC_CATALOG]
        if self.is_admin:
            topics.append(TOPIC_ADMIN_NOW_PLAYING)
        for topic in topics:
            await self._subscribe(topic)
        if self.now_playing is not None:
            await self.now_playing.async_request_refresh()
        self._notify()

    @callback
    def _on_closed(self) -> None:
        self._ws = None
        self.connected = False
        self.listed = set()
        self._fail_pending()
        self._subscribing.clear()
        self._notify()

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(LeylaConnectionError("live channel closed"))
        self._pending.clear()

    async def _subscribe(self, topic: str) -> None:
        op_id = self._new_id()
        self._subscribing[op_id] = topic
        try:
            await self._send({"op": "subscribe", "topic": topic, "id": op_id})
        except LeylaConnectionError:
            self._subscribing.pop(op_id, None)

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise LeylaConnectionError("not connected")
        await self._ws.send_json(message)

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_devices(self.entry.entry_id))

    # ---- incoming ----------------------------------------------------------

    @callback
    def _on_text(self, data: str) -> None:
        try:
            envelope = json.loads(data)
        except ValueError:
            return
        if not isinstance(envelope, dict):
            return
        self.handle_envelope(envelope)

    @callback
    def handle_envelope(self, envelope: dict[str, Any]) -> None:
        """Apply one server frame."""
        kind = envelope.get("event_type")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if kind == "ack":
            future = self._pending.pop(payload.get("id"), None)  # type: ignore[arg-type]
            if future is not None and not future.done():
                future.set_result(payload)
            topic = self._subscribing.pop(payload.get("id"), None)  # type: ignore[arg-type]
            if payload.get("ok") is False:
                _LOGGER.debug("live op %s failed: %s", payload.get("id"), payload.get("error"))
                if topic is not None and payload.get("error") == "TRY_AGAIN":
                    # The server could not read what this account may see:
                    # nothing was subscribed, so ask again shortly.
                    async def again(_now: datetime, topic: str = topic) -> None:
                        await self._subscribe(topic)

                    async_call_later(self.hass, RESUBSCRIBE_AFTER, again)
        elif kind == "remote.devices":
            self._apply_devices(payload)
        elif kind == "catalog.changed":
            self.hass.async_create_task(self._library_changed(payload.get("library_id")))
        elif kind == "admin.now_playing.changed":
            if self.now_playing is not None:
                self.hass.async_create_task(self.now_playing.async_request_refresh())
        elif kind == "resync":
            self._resync(payload.get("topic") or envelope.get("topic"))

    @callback
    def _apply_devices(self, payload: dict[str, Any], *, force: bool = False) -> None:
        revision = payload.get("revision")
        if (
            isinstance(revision, int)
            and not force
            and self._revision is not None
            and revision <= self._revision
        ):
            return
        if isinstance(revision, int):
            self._revision = revision
        devices = payload.get("devices")
        if not isinstance(devices, list):
            return
        now = dt_util.utcnow()
        listed: set[str] = set()
        for raw in devices:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                continue
            device_id = raw["id"]
            listed.add(device_id)
            state = raw.get("state") if isinstance(raw.get("state"), dict) else None
            known = self.devices.get(device_id)
            if known is None:
                self.devices[device_id] = RemoteDevice(
                    id=device_id,
                    name=str(raw.get("name") or "TV"),
                    state=state,
                    controller=raw.get("controller"),
                    reported_at=now,
                )
                continue
            known.name = str(raw.get("name") or known.name)
            known.controller = raw.get("controller")
            if state != known.state:
                known.state = state
                known.reported_at = now
        self.listed = listed
        self._notify()

    @callback
    def _resync(self, topic: Any) -> None:
        if topic == TOPIC_REMOTE:
            self.hass.async_create_task(self._sync_devices())
        elif topic == TOPIC_ADMIN_NOW_PLAYING and self.now_playing is not None:
            self.hass.async_create_task(self.now_playing.async_request_refresh())
        elif topic == TOPIC_CATALOG:
            # Changes were missed and the server does not say where.
            self.hass.async_create_task(self._library_changed(None))

    async def _sync_devices(self) -> None:
        try:
            result = await self.command("remote.sync", {})
        except LeylaError:
            return
        if isinstance(result, dict):
            if isinstance(result.get("you"), str):
                self.you = result["you"]
            self._apply_devices(result, force=True)

    async def _load_libraries(self) -> None:
        rows = await self.api.libraries()
        self.libraries = {
            str(row["id"]): str(row.get("name") or "")
            for row in rows
            if isinstance(row, dict) and row.get("id")
        }

    async def _library_changed(self, library_id: Any) -> None:
        name = None
        if isinstance(library_id, str):
            name = self.libraries.get(library_id)
            if name is None:
                # A library added since the names were read: read them once.
                with contextlib.suppress(LeylaError):
                    await self._load_libraries()
                name = self.libraries.get(library_id)
        else:
            library_id = None
        self.hass.bus.async_fire(
            EVENT_LIBRARY_UPDATED,
            {
                "config_entry_id": self.entry.entry_id,
                "library_id": library_id,
                "library_name": name,
            },
        )

    # ---- commands ----------------------------------------------------------

    async def command(self, name: str, payload: dict[str, Any]) -> Any:
        """Send a live command and wait for its ack; the ack's result."""
        if self._ws is None or self._ws.closed:
            raise LeylaConnectionError("not connected")
        op_id = self._new_id()
        future: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()
        self._pending[op_id] = future
        try:
            await self._send({"op": "command", "command": name, "id": op_id, "payload": payload})
            async with asyncio.timeout(COMMAND_TIMEOUT):
                ack = await future
        except TimeoutError as err:
            raise LeylaCommandError("TIMEOUT") from err
        finally:
            self._pending.pop(op_id, None)
        if not ack.get("ok"):
            raise LeylaCommandError(str(ack.get("error") or "FAILED"))
        return ack.get("result")

    async def control(self, device_id: str, action: str, **fields: Any) -> None:
        """Send a TV one control action, then let go of it.

        Sending a command makes this sign-in the TV's controller, replacing
        whoever held it (a phone, say), and the TV shows "Controlled from"
        this sign-in while it is set. An automation acts once and does not
        stay to watch, so it releases right after: the TV does not keep a
        banner naming Home Assistant, and the next phone command takes the
        TV without competing with a controller that is not looking.
        """
        await self.command("remote.control", {"device_id": device_id, "action": action, **fields})
        await self._release(device_id)

    async def play(self, device_id: str, item_id: str, media_source_id: str) -> None:
        """Start a title on a TV, then let go of it (see `control`)."""
        await self.command(
            "remote.play",
            {"device_id": device_id, "item_id": item_id, "media_source_id": media_source_id},
        )
        await self._release(device_id)

    async def _release(self, device_id: str) -> None:
        try:
            await self.command("remote.release", {"device_id": device_id})
        except LeylaError as err:
            _LOGGER.debug("release of %s failed: %s", device_id, err)

    # ---- artwork -----------------------------------------------------------

    async def poster(self, item_id: str) -> tuple[bytes, str] | None:
        """A title's poster (the series' for an episode), cached for a few titles."""
        if item_id in self._posters:
            self._posters.move_to_end(item_id)
            return self._posters[item_id]
        image: tuple[bytes, str] | None = None
        try:
            detail = await self.api.catalog_item(item_id)
            url = poster_url(detail)
            series = detail.get("series")
            if url is None and isinstance(series, dict) and series.get("id"):
                url = poster_url(await self.api.catalog_item(str(series["id"])))
            if url is not None:
                image = await self.api.fetch_image(url)
        except LeylaAuthError:
            self._sign_out()
            return None
        except LeylaError as err:
            _LOGGER.debug("poster for %s unavailable: %s", item_id, err)
            return None
        self._posters[item_id] = image
        while len(self._posters) > POSTER_CACHE_SIZE:
            self._posters.popitem(last=False)
        return image

    async def primary_source(self, item_id: str) -> str | None:
        """The file a title plays by default."""
        detail = await self.api.catalog_item(item_id)
        source = detail.get("primary_media_source_id")
        return str(source) if source else None
