"""The live channel over a real local WebSocket."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant

from custom_components.leyla.api import LeylaApi
from custom_components.leyla.hub import LeylaHub

from .conftest import account


async def test_ticket_subscribe_command_then_signed_out(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Ticket by bearer, socket by ticket only, a TV list, a command, then 4401."""
    frames: list[dict[str, Any]] = []
    released = asyncio.Event()

    async def ticket(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        return web.json_response({"ticket": "t1", "expires_at": "x"})

    async def live(request: web.Request) -> web.WebSocketResponse:
        assert request.query["ticket"] == "t1"
        assert "Authorization" not in request.headers
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            frame = message.json()
            frames.append(frame)
            await ws.send_json(
                {"event_type": "ack", "payload": {"id": frame["id"], "ok": True, "result": {}}}
            )
            if frame.get("topic") == "remote":
                tv = {"id": "tv", "name": "TV", "state": None, "controller": None}
                await ws.send_json(
                    {
                        "event_type": "remote.devices",
                        "topic": "remote",
                        "payload": {"revision": 1, "devices": [tv]},
                    }
                )
            if frame.get("command") == "remote.release":
                released.set()
                await ws.close(code=4401, message=b"signed out")
        return ws

    app = web.Application()
    app.router.add_post("/api/v1/live/ticket", ticket)
    app.router.add_get("/api/v1/live", live)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    session = aiohttp.ClientSession()
    try:
        api = LeylaApi(session, f"http://127.0.0.1:{server.port}", "tok")
        entry = MagicMock()
        entry.entry_id = "entry"
        hub = LeylaHub(hass, entry, api, session, account())
        run = asyncio.ensure_future(hub._run())
        async with asyncio.timeout(5):
            while not hub.listed:
                await asyncio.sleep(0.01)
        assert hub.connected
        await hub.control("tv", "pause")
        async with asyncio.timeout(5):
            await run
        assert released.is_set()
        assert [(f["op"], f.get("topic") or f.get("command")) for f in frames] == [
            ("subscribe", "remote"),
            ("subscribe", "catalog"),
            ("command", "remote.control"),
            ("command", "remote.release"),
        ]
        assert frames[2]["payload"] == {"device_id": "tv", "action": "pause"}
        # Signed out: no reconnect, and Home Assistant asks to sign in again.
        entry.async_start_reauth.assert_called_once_with(hass)
        assert not hub.connected
        assert hub.listed == set()
    finally:
        await session.close()
        await server.close()
