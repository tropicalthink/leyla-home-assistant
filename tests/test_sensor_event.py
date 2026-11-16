"""Streams sensors (admins), the library event, setup and diagnostics."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.leyla.api import LeylaAuthError, LeylaConnectionError
from custom_components.leyla.const import EVENT_LIBRARY_UPDATED
from custom_components.leyla.diagnostics import async_get_config_entry_diagnostics

from .conftest import LIBRARY_ID, TOKEN, URL, account, devices_event, playing, setup, tv

STREAMS = "sensor.maya_on_http_leyla_local_10001_active_streams"
CONVERSIONS = "sensor.maya_on_http_leyla_local_10001_active_conversions"


def session(mode: str, title: str = "Arrival", video_action: str | None = None) -> dict[str, Any]:
    return {
        "session_id": f"s-{title}-{mode}",
        "account_name": "maya",
        "device_name": "Leyla app on LG TV",
        "title": title,
        "delivery_mode": mode,
        "video_action": video_action or ("direct" if mode == "direct_play" else "transcode"),
        "delivery_reason": "VIDEO_CODEC_UNSUPPORTED",
        "output_height": 1080 if mode != "direct_play" else None,
        "position_ms": 1000,
        "duration_ms": 2000,
    }


async def test_no_sensors_for_a_viewer(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    await setup(hass, entry)
    assert hass.states.get(STREAMS) is None
    mock_api["now_playing"].assert_not_awaited()


async def test_admin_sensors(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    mock_api["me"].return_value = account(is_admin=True)
    mock_api["now_playing"].return_value = [
        session("direct_play"),
        session("hls", "Dune"),
        # A remux over HLS is a stream, not a conversion.
        session("hls", "Heat", video_action="copy"),
    ]
    hub, socket = await setup(hass, entry)
    assert "admin.now_playing" in [m["topic"] for m in socket.sent if m["op"] == "subscribe"]

    streams = hass.states.get(STREAMS)
    assert streams.state == "3"
    assert streams.attributes["sessions"][1] == {
        "account": "maya",
        "device": "Leyla app on LG TV",
        "title": "Dune",
        "delivery": "hls",
        "height": 1080,
    }
    assert hass.states.get(CONVERSIONS).state == "1"

    # The server says something changed: one refetch after a short pause,
    # however many events arrive in it.
    calls = mock_api["now_playing"].await_count
    mock_api["now_playing"].return_value = [session("adaptive_hls")] * 12
    for _ in range(3):
        hub.handle_envelope(
            {"event_type": "admin.now_playing.changed", "payload": {"session_id": "x"}}
        )
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    assert mock_api["now_playing"].await_count == calls + 1
    assert hass.states.get(CONVERSIONS).state == "12"
    streams = hass.states.get(STREAMS)
    assert len(streams.attributes["sessions"]) == 10
    assert streams.attributes["more"] == 2


async def test_library_event(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, _ = await setup(hass, entry)
    events = async_capture_events(hass, EVENT_LIBRARY_UPDATED)

    hub.handle_envelope({"event_type": "catalog.changed", "payload": {"library_id": LIBRARY_ID}})
    await hass.async_block_till_done()
    assert events[-1].data == {
        "config_entry_id": entry.entry_id,
        "library_id": LIBRARY_ID,
        "library_name": "Films",
    }

    # A library added since setup: the names are read again, once.
    mock_api["libraries"].return_value = [
        {"id": LIBRARY_ID, "name": "Films"},
        {"id": "new-lib", "name": "Shows"},
    ]
    hub.handle_envelope({"event_type": "catalog.changed", "payload": {"library_id": "new-lib"}})
    await hass.async_block_till_done()
    assert events[-1].data["library_name"] == "Shows"
    assert mock_api["libraries"].await_count == 2

    # Missed events: the server cannot say where.
    hub.handle_envelope(
        {"event_type": "resync", "topic": "catalog", "payload": {"topic": "catalog", "skipped": 4}}
    )
    await hass.async_block_till_done()
    assert events[-1].data["library_id"] is None
    assert len(events) == 3


async def test_remote_resync_asks_for_the_list(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(9, tv(state=playing(status="paused"))))
    socket.answers["remote.sync"] = (
        True,
        {"you": "me", "revision": 2, "devices": [tv(state=playing())]},
    )
    hub.handle_envelope({"event_type": "resync", "payload": {"topic": "remote", "skipped": 1}})
    await hass.async_block_till_done()
    assert hub.you == "me"
    assert hass.states.get("media_player.leyla_app_on_lg_tv").state == "playing"


async def test_signed_out_at_setup_asks_to_sign_in(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    mock_api["me"].side_effect = LeylaAuthError
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_server_absent_at_setup_retries(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    mock_api["me"].side_effect = LeylaConnectionError("down")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_diagnostics_redact_the_token(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    mock_api["me"].return_value = account(is_admin=True)
    mock_api["now_playing"].return_value = [session("hls")]
    hub, _ = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    text = repr(diagnostics)
    assert TOKEN not in text
    assert URL not in text
    assert "maya" not in text
    assert diagnostics["live"]["connected"] is True
    assert diagnostics["devices"][0]["status"] == "playing"
    assert diagnostics["streams"][0]["delivery_mode"] == "hls"


async def test_try_again_subscribes_again(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, socket = await setup(hass, entry)
    catalog = next(m for m in socket.sent if m.get("topic") == "catalog")
    hub.handle_envelope(
        {"event_type": "ack", "payload": {"id": catalog["id"], "ok": False, "error": "TRY_AGAIN"}}
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert [m["topic"] for m in socket.sent if m["op"] == "subscribe"].count("catalog") == 2
