"""Shared fixtures: a Leyla entry with the HTTP API and the socket faked."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_ACCESS_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.leyla.const import DOMAIN
from custom_components.leyla.hub import LeylaHub

URL = "http://leyla.local:10001"
TOKEN = "secret-token-value"
ACCOUNT_ID = "0199a0b1-0000-7000-8000-000000000001"
TV_ID = "0199a0b1-0000-7000-8000-0000000000aa"
TV2_ID = "0199a0b1-0000-7000-8000-0000000000bb"
LIBRARY_ID = "0199a0b1-0000-7000-8000-0000000000f1"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Load custom_components/leyla."""


def account(is_admin: bool = False) -> dict[str, Any]:
    """A /auth/me answer."""
    return {"id": ACCOUNT_ID, "username": "maya", "is_admin": is_admin, "needs_welcome": False}


class FakeSocket:
    """Stands in for the live WebSocket: records frames, answers commands."""

    def __init__(self, hub: LeylaHub) -> None:
        self.hub = hub
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        # command name -> (ok, result or error code)
        self.answers: dict[str, tuple[bool, Any]] = {}

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message.get("op") == "command":
            ok, value = self.answers.get(message["command"], (True, {}))
            payload: dict[str, Any] = {"id": message["id"], "ok": ok}
            if ok:
                payload["result"] = value
            else:
                payload["error"] = value
            self.hub.hass.loop.call_soon(
                self.hub.handle_envelope, {"event_type": "ack", "payload": payload}
            )

    async def close(self) -> None:
        self.closed = True

    def commands(self) -> list[tuple[str, dict[str, Any]]]:
        return [(m["command"], m["payload"]) for m in self.sent if m.get("op") == "command"]


@pytest.fixture
def mock_api() -> Generator[dict[str, AsyncMock]]:
    """Patch every HTTP call the integration makes."""
    with (
        patch("custom_components.leyla.api.LeylaApi.me", new_callable=AsyncMock) as me,
        patch(
            "custom_components.leyla.api.LeylaApi.libraries", new_callable=AsyncMock
        ) as libraries,
        patch(
            "custom_components.leyla.api.LeylaApi.now_playing", new_callable=AsyncMock
        ) as now_playing,
        patch(
            "custom_components.leyla.api.LeylaApi.catalog_item", new_callable=AsyncMock
        ) as catalog_item,
        patch(
            "custom_components.leyla.api.LeylaApi.fetch_image", new_callable=AsyncMock
        ) as fetch_image,
        patch(
            "custom_components.leyla.api.LeylaApi.live_ticket", new_callable=AsyncMock
        ) as live_ticket,
        # No real socket: tests drive the hub through FakeSocket.
        patch("custom_components.leyla.hub.LeylaHub._run", new_callable=AsyncMock),
    ):
        me.return_value = account()
        libraries.return_value = [{"id": LIBRARY_ID, "name": "Films"}]
        now_playing.return_value = []
        live_ticket.return_value = "t" * 64
        yield {
            "me": me,
            "libraries": libraries,
            "now_playing": now_playing,
            "catalog_item": catalog_item,
            "fetch_image": fetch_image,
            "live_ticket": live_ticket,
        }


@pytest.fixture
def entry() -> MockConfigEntry:
    """A configured Leyla entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"maya on {URL}",
        unique_id=ACCOUNT_ID,
        data={CONF_URL: URL, CONF_ACCESS_TOKEN: TOKEN},
    )


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[LeylaHub, FakeSocket]:
    """Set the entry up and connect a fake socket."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub: LeylaHub = entry.runtime_data
    socket = FakeSocket(hub)
    await hub._on_open(socket)  # type: ignore[arg-type]
    await hass.async_block_till_done()
    return hub, socket


def tv(
    device_id: str = TV_ID,
    name: str = "Leyla app on LG TV",
    state: dict[str, Any] | None = None,
    controller: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One entry of a remote.devices list."""
    return {"id": device_id, "name": name, "state": state, "controller": controller}


def playing(**overrides: Any) -> dict[str, Any]:
    """A TV state report while a film plays."""
    state = {
        "status": "playing",
        "item_id": "item-1",
        "media_source_id": "source-1",
        "title": "Arrival",
        "detail": None,
        "position_ms": 61_500,
        "duration_ms": 6_960_000,
        "audio_tracks": [],
        "audio_stream_index": None,
        "subtitle_tracks": [],
        "subtitle_stream_index": None,
        "has_next": False,
        "volume": {"level": 0.4, "muted": False},
        "error": None,
    }
    state.update(overrides)
    return state


def devices_event(revision: int, *devices: dict[str, Any]) -> dict[str, Any]:
    """A remote.devices frame."""
    return {
        "event_type": "remote.devices",
        "topic": "remote",
        "payload": {"revision": revision, "devices": list(devices)},
    }
