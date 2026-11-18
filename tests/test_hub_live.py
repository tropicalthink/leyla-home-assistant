"""The live channel end to end against a fake socket: frames, closes,
resyncs, timeouts, keepalive and artwork."""

from __future__ import annotations

import asyncio
import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.leyla.api import LeylaAuthError, LeylaConnectionError
from custom_components.leyla.const import CLOSE_ACCESS_CHANGED, CLOSE_SIGNED_OUT
from custom_components.leyla.hub import LeylaCommandError, LeylaHub

from .conftest import TV_ID, URL, account, devices_event, playing, setup, tv


class Frame:
    def __init__(self, kind: aiohttp.WSMsgType, data: Any = None) -> None:
        self.type = kind
        self.data = data


class Socket:
    """An async-iterable socket that plays a script of frames, then closes."""

    def __init__(self, frames: list[Frame], close_code: int | None) -> None:
        self.frames = frames
        self.close_code = close_code
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> Socket:
        return self

    async def __anext__(self) -> Frame:
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def __aenter__(self) -> Socket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


def make_hub(hass: HomeAssistant, socket: Socket, **api: Any) -> LeylaHub:
    client = MagicMock()
    client.url = URL
    client.live_ticket = AsyncMock(return_value="t")
    client.live_url = MagicMock(return_value="ws://x")
    for name, value in api.items():
        setattr(client, name, value)
    session = MagicMock()
    session.ws_connect = MagicMock(return_value=socket)
    entry = MagicMock()
    entry.entry_id = "entry"
    return LeylaHub(hass, entry, client, session, account())


async def test_frames_then_signed_out(hass: HomeAssistant) -> None:
    socket = Socket(
        [
            Frame(
                aiohttp.WSMsgType.TEXT,
                '{"event_type": "remote.devices", "payload": {"revision": 1, "devices": []}}',
            ),
            Frame(aiohttp.WSMsgType.TEXT, "not json"),
            Frame(aiohttp.WSMsgType.TEXT, "[1, 2]"),
            Frame(aiohttp.WSMsgType.BINARY, b"x"),
            Frame(aiohttp.WSMsgType.CLOSE),
            Frame(aiohttp.WSMsgType.TEXT, "never read"),
        ],
        CLOSE_SIGNED_OUT,
    )
    hub = make_hub(hass, socket)
    await hub._run()
    assert hub.last_close == f"closed {CLOSE_SIGNED_OUT}"
    hub.entry.async_start_reauth.assert_called_once_with(hass)
    assert [m["topic"] for m in socket.sent] == ["remote", "catalog"]
    assert socket.frames[0].data == "never read"
    # Signing out twice asks once.
    hub._sign_out()
    hub.entry.async_start_reauth.assert_called_once()


async def test_access_changed_reloads(hass: HomeAssistant) -> None:
    socket = Socket([Frame(aiohttp.WSMsgType.ERROR)], CLOSE_ACCESS_CHANGED)
    hub = make_hub(hass, socket)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await hub._run()
    reload.assert_called_once_with("entry")


async def test_a_long_connection_resets_the_backoff(hass: HomeAssistant) -> None:
    hub = make_hub(hass, Socket([], 1000))
    delays: list[float] = []
    times = itertools.count(0.0, 1000.0)

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            hub._stopping = True
        hub._session.ws_connect.return_value = Socket([], 1000)

    with (
        patch("custom_components.leyla.hub.asyncio.sleep", fake_sleep),
        patch.object(hass.loop, "time", lambda: next(times)),
    ):
        await hub._run()
    assert all(d < 1.3 for d in delays)


async def test_keepalive(hass: HomeAssistant) -> None:
    hub = make_hub(hass, Socket([], None), me=AsyncMock(side_effect=LeylaConnectionError("x")))
    await hub._keepalive(MagicMock())
    hub.entry.async_start_reauth.assert_not_called()
    hub.api.me = AsyncMock(side_effect=LeylaAuthError)
    await hub._keepalive(MagicMock())
    hub.entry.async_start_reauth.assert_called_once()


async def test_start_and_stop(hass: HomeAssistant) -> None:
    hub = make_hub(
        hass, Socket([], None), libraries=AsyncMock(side_effect=LeylaConnectionError("x"))
    )
    hub.entry.async_create_background_task = MagicMock(
        side_effect=lambda _hass, coro, name: hass.async_create_task(coro)
    )
    hub._run = AsyncMock()  # type: ignore[method-assign]
    await hub.async_start()
    assert hub.libraries == {}
    socket = Socket([], None)
    hub._ws = socket  # type: ignore[assignment]
    loop_future: asyncio.Future[dict[str, Any]] = hass.loop.create_future()
    hub._pending[1] = loop_future
    await hub.async_stop()
    assert socket.closed
    with pytest.raises(LeylaConnectionError):
        loop_future.result()

    hub.api.libraries = AsyncMock(side_effect=LeylaAuthError)
    with pytest.raises(ConfigEntryAuthFailed):
        await hub.async_start()


async def test_signed_out_during_setup_asks_to_sign_in(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    mock_api["catalog_counts"].side_effect = LeylaAuthError
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress()
    )


async def test_resync_and_commands(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    mock_api["me"].return_value = account(is_admin=True)
    hub, socket = await setup(hass, entry)
    socket.answers["remote.sync"] = (
        True,
        {"you": "me", "revision": 9, **devices_event(9, tv(state=playing()))["payload"]},
    )
    hub.handle_envelope({"event_type": "resync", "topic": "remote", "payload": {}})
    hub.handle_envelope({"event_type": "resync", "payload": {"topic": "admin.now_playing"}})
    hub.handle_envelope({"event_type": "resync", "payload": {"topic": "catalog"}})
    hub.handle_envelope({"event_type": "admin.now_playing.changed", "payload": None})
    await hass.async_block_till_done()
    assert hub.you == "me" and TV_ID in hub.listed

    socket.answers["remote.sync"] = (False, "TRY_AGAIN")
    hub.handle_envelope({"event_type": "resync", "topic": "remote", "payload": {}})
    await hass.async_block_till_done()

    # No answer: the command times out.
    async def silent(message: dict[str, Any]) -> None:
        socket.sent.append(message)

    with (
        patch.object(socket, "send_json", silent),
        patch("custom_components.leyla.hub.COMMAND_TIMEOUT", 0.01),
    ):
        with pytest.raises(LeylaCommandError):
            await hub.command("remote.sync", {})
        # A release that fails does not fail the command it followed.
        socket.answers["remote.control"] = (True, {})
    socket.answers["remote.release"] = (False, "NOT_CONTROLLER")
    await hub.control(TV_ID, "play")

    # Not connected: subscribing quietly does nothing, commands refuse.
    hub._ws = None
    await hub._subscribe("remote")
    with pytest.raises(LeylaConnectionError):
        await hub.command("remote.sync", {})


async def test_posters(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    hub, _socket = await setup(hass, entry)
    details = {
        "e1": {"artwork": [], "series": {"id": "s1"}},
        "s1": {"artwork": [{"kind": "poster", "url": "/api/v1/artwork/s.jpg"}]},
    }
    mock_api["catalog_item"].side_effect = lambda item_id: details.get(item_id, {"artwork": []})
    mock_api["fetch_image"].return_value = (b"img", "image/jpeg")
    assert await hub.poster("e1") == (b"img", "image/jpeg")
    mock_api["fetch_image"].assert_awaited_with("/api/v1/artwork/s.jpg")
    for n in range(10):
        await hub.poster(f"x{n}")
    assert "e1" not in hub._posters and len(hub._posters) == 8

    mock_api["catalog_item"].side_effect = LeylaConnectionError("down")
    assert await hub.poster("y") is None
    mock_api["catalog_item"].side_effect = LeylaAuthError
    with patch.object(entry, "async_start_reauth") as reauth:
        assert await hub.poster("z") is None
    reauth.assert_called_once()
