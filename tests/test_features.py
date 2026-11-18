"""Server updates, scans, library sizes and events, track pickers, repairs,
browsing and search, and removing a TV."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.media_player import (
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.update import (
    ATTR_IN_PROGRESS,
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_RELEASE_SUMMARY,
    ATTR_RELEASE_URL,
    ATTR_UPDATE_PERCENTAGE,
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

from custom_components.leyla.api import LeylaApiError, LeylaConnectionError
from custom_components.leyla.const import DOMAIN

from .conftest import LIBRARY_ID, TV_ID, UPDATES_CURRENT, account, devices_event, playing, setup, tv

PLAYER = "media_player.leyla_app_on_lg_tv"
AUDIO = "select.leyla_app_on_lg_tv_audio"
SUBTITLES = "select.leyla_app_on_lg_tv_subtitles"
UPDATE = "update.maya_on_http_leyla_local_10001_leyla_server"
SCAN = "button.maya_on_http_leyla_local_10001_scan_films"
TITLES = "sensor.maya_on_http_leyla_local_10001_films_titles"
EVENT = "event.maya_on_http_leyla_local_10001_library_updated"

FINDING = {
    "id": "disk_low:/media",
    "kind": "disk_low",
    "severity": "action",
    "title": "The media disk is nearly full",
    "explanation": "12 GB are left.",
    "impact": "New files stop being added.",
}


def admin(mock_api: dict[str, AsyncMock]) -> None:
    mock_api["me"].return_value = account(is_admin=True)


# ---- the server's updates ---------------------------------------------------


async def test_update_current_and_newer(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    admin(mock_api)
    await setup(hass, entry)
    state = hass.states.get(UPDATE)
    assert state is not None and state.state == STATE_OFF
    assert state.attributes[ATTR_INSTALLED_VERSION] == "0.15.1"
    (server,) = dr.async_get(hass).async_get_devices(
        identifiers={(DOMAIN, entry.entry_id)}, config_entry_id=entry.entry_id
    )
    assert server.sw_version == "0.15.1"

    mock_api["updates"].return_value = {
        **UPDATES_CURRENT,
        "state": "fetching",
        "newer": {"version": "0.15.2"},
        "progress": {"fetched_bytes": 25, "total_bytes": 100},
    }
    await entry.runtime_data.updates.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(UPDATE)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_LATEST_VERSION] == "0.15.2"
    assert state.attributes[ATTR_RELEASE_URL] == "https://leyla.media/changelog/0.15.2"
    assert state.attributes[ATTR_IN_PROGRESS] is True
    assert state.attributes[ATTR_UPDATE_PERCENTAGE] == 25

    mock_api["updates"].return_value = {**UPDATES_CURRENT, "running": "0.15.2"}
    await entry.runtime_data.updates.async_refresh()
    await hass.async_block_till_done()
    (server,) = dr.async_get(hass).async_get_devices(
        identifiers={(DOMAIN, entry.entry_id)}, config_entry_id=entry.entry_id
    )
    assert server.sw_version == "0.15.2"


@pytest.mark.parametrize(
    ("status", "summary"),
    [
        ({"needs_plan": True}, "This server needs a plan to take the newer release."),
        (
            {"state": "held", "held": {"code": "X", "detail": "Rehearsal found worse"}},
            "Rehearsal found worse",
        ),
        ({"failure": {"code": "FETCH_FAILED", "detail": ""}}, "FETCH_FAILED"),
        ({}, None),
    ],
)
async def test_update_summary(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    status: dict[str, Any],
    summary: str | None,
) -> None:
    admin(mock_api)
    mock_api["updates"].return_value = {**UPDATES_CURRENT, "newer": {"version": "0.16.0"}, **status}
    await setup(hass, entry)
    assert hass.states.get(UPDATE).attributes[ATTR_RELEASE_SUMMARY] == summary


async def test_update_install(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    admin(mock_api)
    mock_api["updates"].return_value = {**UPDATES_CURRENT, "newer": {"version": "0.16.0"}}
    await setup(hass, entry)
    await hass.services.async_call(
        UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: UPDATE}, blocking=True
    )
    mock_api["install_update"].assert_awaited_once()
    mock_api["install_update"].side_effect = LeylaApiError(409, "RELEASE_NEEDS_PLAN")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: UPDATE}, blocking=True
        )


async def test_no_admin_entities_for_a_viewer(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    await setup(hass, entry)
    assert hass.states.get(UPDATE) is None
    assert hass.states.get(SCAN) is None
    assert hass.states.get(TITLES) is not None
    mock_api["updates"].assert_not_awaited()


async def test_an_admin_no_longer(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    admin(mock_api)
    await setup(hass, entry)
    mock_api["findings"].side_effect = LeylaApiError(403, "FORBIDDEN")
    await entry.runtime_data.findings.async_refresh()
    assert entry.runtime_data.findings.data == []
    assert entry.runtime_data.findings.last_update_success
    mock_api["findings"].side_effect = LeylaApiError(500, None)
    await entry.runtime_data.findings.async_refresh()
    assert not entry.runtime_data.findings.last_update_success
    mock_api["findings"].side_effect = LeylaConnectionError("down")
    await entry.runtime_data.findings.async_refresh()
    assert not entry.runtime_data.findings.last_update_success


# ---- scans, sizes and the library event -------------------------------------


async def test_scan_button(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    admin(mock_api)
    await setup(hass, entry)
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: SCAN}, blocking=True
    )
    mock_api["start_scan"].assert_awaited_once_with(LIBRARY_ID)
    mock_api["start_scan"].side_effect = LeylaApiError(409, "SCAN_RUNNING")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: SCAN}, blocking=True
        )


async def test_library_size_and_event(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    hub, _socket = await setup(hass, entry)
    assert hass.states.get(TITLES).state == "42"
    assert hass.states.get(EVENT).state == "unknown"

    mock_api["catalog_counts"].return_value = [{"library_id": LIBRARY_ID, "item_count": 43}]
    hub.handle_envelope({"event_type": "catalog.changed", "payload": {"library_id": LIBRARY_ID}})
    await hass.async_block_till_done()
    event = hass.states.get(EVENT)
    assert event.state != "unknown"
    assert event.attributes["event_type"] == "updated"
    assert event.attributes["library_name"] == "Films"
    await hub.counts.async_refresh()
    assert hass.states.get(TITLES).state == "43"

    mock_api["catalog_counts"].return_value = []
    await hub.counts.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(TITLES).state == "0"


# ---- the TV's tracks --------------------------------------------------------

TRACKS = {
    "audio_tracks": [
        {"stream_index": 1, "label": "English"},
        {"stream_index": 2, "label": "English"},
        {"stream_index": 3, "label": "Deutsch"},
    ],
    "audio_stream_index": 2,
    "subtitle_tracks": [{"stream_index": 5, "label": "English (SDH)"}, {"stream_index": 6}],
    "subtitle_stream_index": None,
}


async def test_track_pickers(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    hub, socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing(**TRACKS))))
    await hass.async_block_till_done()

    audio = hass.states.get(AUDIO)
    assert audio.attributes[ATTR_OPTIONS] == ["English", "English (2)", "Deutsch"]
    assert audio.state == "English (2)"
    subtitles = hass.states.get(SUBTITLES)
    assert subtitles.attributes[ATTR_OPTIONS] == ["off", "English (SDH)", "Track 6"]
    assert subtitles.state == "off"

    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: AUDIO, ATTR_OPTION: "Deutsch"},
        blocking=True,
    )
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: SUBTITLES, ATTR_OPTION: "off"},
        blocking=True,
    )
    sent = [p for c, p in socket.commands() if c == "remote.control"]
    assert sent[0] == {"device_id": TV_ID, "action": "audio", "stream_index": 3}
    assert sent[1] == {"device_id": TV_ID, "action": "subtitle", "stream_index": None}

    socket.answers["remote.control"] = (False, "DEVICE_NOT_FOUND")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            SELECT_DOMAIN,
            SERVICE_SELECT_OPTION,
            {ATTR_ENTITY_ID: AUDIO, ATTR_OPTION: "English"},
            blocking=True,
        )
    hub._ws = None
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            SELECT_DOMAIN,
            SERVICE_SELECT_OPTION,
            {ATTR_ENTITY_ID: AUDIO, ATTR_OPTION: "English"},
            blocking=True,
        )


async def test_track_pickers_unavailable_without_tracks(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    hub, _socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    await hass.async_block_till_done()
    assert hass.states.get(AUDIO).state == STATE_UNAVAILABLE
    assert hass.states.get(SUBTITLES).state == STATE_UNAVAILABLE


async def test_select_refuses_an_unknown_track(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    hub, _socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing(**TRACKS))))
    await hass.async_block_till_done()
    entity = hass.data["entity_components"][SELECT_DOMAIN].get_entity(AUDIO)
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("Klingon")
    entity.hub.devices[TV_ID].state = {**playing(**TRACKS), "audio_stream_index": 99}
    assert entity.current_option is None


# ---- repairs ----------------------------------------------------------------


async def test_findings_become_repairs(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_client: ClientSessionGenerator,
) -> None:
    admin(mock_api)
    mock_api["findings"].return_value = [
        FINDING,
        {**FINDING, "id": "info:x", "severity": "info"},
        {**FINDING, "id": "done:x", "acknowledged_at": "2026-11-18T00:00:00Z"},
        "junk",
    ]
    assert await async_setup_component(hass, "repairs", {})
    await setup(hass, entry)
    registry = ir.async_get(hass)
    key = f"{entry.entry_id}:{FINDING['id']}"
    issue = registry.async_get_issue(DOMAIN, key)
    assert issue is not None and issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders["title"] == FINDING["title"]
    assert len([i for (d, _k), i in registry.issues.items() if d == DOMAIN]) == 1

    client = await hass_client()
    resp = await client.post("/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": key})
    flow = await resp.json()
    assert flow["step_id"] == "confirm"
    mock_api["findings"].return_value = []
    resp = await client.post(f"/api/repairs/issues/fix/{flow['flow_id']}", json={})
    assert (await resp.json())["type"] == "create_entry"
    mock_api["acknowledge_finding"].assert_awaited_once_with(FINDING["id"], FINDING["title"])
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, key) is None


async def test_repair_fix_when_leyla_is_away(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_client: ClientSessionGenerator,
) -> None:
    admin(mock_api)
    mock_api["findings"].return_value = [{**FINDING, "severity": "attention"}]
    assert await async_setup_component(hass, "repairs", {})
    await setup(hass, entry)
    key = f"{entry.entry_id}:{FINDING['id']}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, key).severity is ir.IssueSeverity.WARNING
    client = await hass_client()
    mock_api["acknowledge_finding"].side_effect = LeylaConnectionError("down")
    resp = await client.post("/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": key})
    flow = await resp.json()
    resp = await client.post(f"/api/repairs/issues/fix/{flow['flow_id']}", json={})
    assert (await resp.json())["reason"] == "cannot_connect"

    # Unloaded: nothing to tell.
    await hass.config_entries.async_unload(entry.entry_id)
    resp = await client.post("/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": key})
    flow = await resp.json()
    resp = await client.post(f"/api/repairs/issues/fix/{flow['flow_id']}", json={})
    assert (await resp.json())["reason"] == "not_loaded"

    # Removing the entry takes its repairs.
    await hass.config_entries.async_remove(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, key) is None


# ---- browsing, search and play ----------------------------------------------

FILM = {
    "id": "f1",
    "type": "film",
    "title": "Arrival",
    "year": 2016,
    "poster_artwork": {"url": "/api/v1/artwork/p1.jpg"},
}
SERIES = {"id": "s1", "type": "series", "title": "Severance", "year": 2022}


async def browse(
    hass: HomeAssistant, ws: WebSocketGenerator, content_id: str | None
) -> dict[str, Any]:
    client = await ws(hass)
    msg: dict[str, Any] = {"id": 1, "type": "media_player/browse_media", "entity_id": PLAYER}
    if content_id is not None:
        msg |= {"media_content_type": "video", "media_content_id": content_id}
    await client.send_json(msg)
    return await client.receive_json()


async def test_browse(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_ws_client: WebSocketGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    hub, _socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    await hass.async_block_till_done()

    root = (await browse(hass, hass_ws_client, None))["result"]
    assert [c["title"] for c in root["children"]] == ["Continue watching", "Films"]

    mock_api["continue_watching"].return_value = [
        {
            "catalog_item_id": "e1",
            "media_source_id": "m1",
            "type": "episode",
            "title": "Good News",
            "series_title": "Severance",
            "season_number": 1,
            "episode_number": 1,
        },
        {"catalog_item_id": "f2", "type": "film", "title": "Heat"},
        {"no": "id"},
    ]
    cont = (await browse(hass, hass_ws_client, "continue"))["result"]
    assert [c["title"] for c in cont["children"]] == ["Severance · S1 E1 · Good News", "Heat"]
    assert cont["children"][0]["media_content_id"] == "item:e1:m1"
    assert cont["children"][1]["media_content_id"] == "item:f2"

    mock_api["catalog_list"].return_value = {"items": [FILM, SERIES], "has_more": True}
    lib = (await browse(hass, hass_ws_client, f"library:{LIBRARY_ID}"))["result"]
    assert [c["title"] for c in lib["children"]] == ["Arrival (2016)", "Severance (2022)", "More"]
    assert lib["children"][0]["can_play"] and not lib["children"][0]["can_expand"]
    assert lib["children"][1]["can_expand"]
    assert lib["children"][2]["media_content_id"] == f"library:{LIBRARY_ID}:100"
    thumb = lib["children"][0]["thumbnail"]
    assert thumb.startswith("/api/media_player_proxy/") and "artwork" in thumb
    mock_api["catalog_list"].return_value = {"items": [], "has_more": False}
    page2 = (await browse(hass, hass_ws_client, f"library:{LIBRARY_ID}:100"))["result"]
    assert page2["media_content_id"] == f"library:{LIBRARY_ID}:100"

    mock_api["catalog_item"].return_value = {
        **SERIES,
        "children": [{"id": "se1", "type": "season", "title": "Season 1"}],
    }
    series = (await browse(hass, hass_ws_client, "title:s1"))["result"]
    assert series["children"][0]["media_content_id"] == "title:se1"
    mock_api["catalog_item"].return_value = {
        "id": "se1",
        "type": "season",
        "title": "Season 1",
        "children": [{"id": "e1", "type": "episode", "title": "Good News", "episode_number": 1}],
    }
    season = (await browse(hass, hass_ws_client, "title:se1"))["result"]
    assert season["children"][0]["title"] == "1. Good News"

    mock_api["fetch_image"].return_value = (b"jpeg", "image/jpeg")
    client = await hass_client()
    resp = await client.get(thumb)
    assert resp.status == 200 and await resp.read() == b"jpeg"
    mock_api["fetch_image"].side_effect = LeylaConnectionError("gone")
    resp = await client.get(thumb)
    assert resp.status != 200


async def test_browse_errors(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_ws_client: WebSocketGenerator,
) -> None:
    hub, _socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    await hass.async_block_till_done()
    assert not (await browse(hass, hass_ws_client, "nonsense:1"))["success"]
    mock_api["continue_watching"].side_effect = LeylaConnectionError("down")
    assert not (await browse(hass, hass_ws_client, "continue"))["success"]


async def test_search(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_ws_client: WebSocketGenerator,
) -> None:
    hub, _socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    await hass.async_block_till_done()
    mock_api["catalog_list"].return_value = {"items": [FILM], "has_more": False}
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "media_player/search_media",
            "entity_id": PLAYER,
            "search_query": "aliens arrive",
        }
    )
    result = await client.receive_json()
    assert result["success"], result
    assert result["result"]["result"][0]["title"] == "Arrival (2016)"
    assert mock_api["catalog_list"].await_args.kwargs["query"] == "aliens arrive"
    mock_api["catalog_list"].side_effect = LeylaConnectionError("down")
    await client.send_json(
        {"id": 2, "type": "media_player/search_media", "entity_id": PLAYER, "search_query": "x"}
    )
    assert not (await client.receive_json())["success"]


async def test_play_a_series_goes_on_where_it_was(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    hub, socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    await hass.async_block_till_done()

    mock_api["catalog_item"].return_value = {
        **SERIES,
        "primary_media_source_id": None,
        "children": [],
    }
    mock_api["series_continue"].return_value = {"catalog_item_id": "e3", "media_source_id": "m3"}
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {ATTR_ENTITY_ID: PLAYER, "media_content_type": "tvshow", "media_content_id": "title:s1"},
        blocking=True,
    )
    plays = [p for c, p in socket.commands() if c == "remote.play"]
    assert plays[-1] == {"device_id": TV_ID, "item_id": "e3", "media_source_id": "m3"}

    # Nothing started yet: the first episode of the first season.
    mock_api["series_continue"].return_value = None
    details = {
        "s1": {**SERIES, "primary_media_source_id": None, "children": [{"id": "se1"}]},
        "se1": {"id": "se1", "type": "season", "children": [{"id": "e1"}]},
        "e1": {"id": "e1", "type": "episode", "primary_media_source_id": "m1"},
    }
    mock_api["catalog_item"].side_effect = lambda item_id: details[item_id]
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {ATTR_ENTITY_ID: PLAYER, "media_content_type": "tvshow", "media_content_id": "title:s1"},
        blocking=True,
    )
    plays = [p for c, p in socket.commands() if c == "remote.play"]
    assert plays[-1] == {"device_id": TV_ID, "item_id": "e1", "media_source_id": "m1"}

    # An empty series has nothing to play.
    details["se1"]["children"] = []
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            MP_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {
                ATTR_ENTITY_ID: PLAYER,
                "media_content_type": "tvshow",
                "media_content_id": "title:s1",
            },
            blocking=True,
        )


# ---- removing a TV ----------------------------------------------------------


async def test_remove_a_tv_that_is_gone(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_ws_client: WebSocketGenerator,
) -> None:
    assert await async_setup_component(hass, "config", {})
    hub, _socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    await hass.async_block_till_done()
    devices = dr.async_get(hass)
    (tv_device,) = devices.async_get_devices(
        identifiers={(DOMAIN, TV_ID)}, config_entry_id=entry.entry_id
    )
    (server,) = devices.async_get_devices(
        identifiers={(DOMAIN, entry.entry_id)}, config_entry_id=entry.entry_id
    )
    client = await hass_ws_client(hass)

    async def remove(device_id: str) -> bool:
        await client.send_json_auto_id(
            {
                "type": "config/device_registry/remove_config_entry",
                "config_entry_id": entry.entry_id,
                "device_id": device_id,
            }
        )
        return bool((await client.receive_json())["success"])

    assert not await remove(server.id)
    assert not await remove(tv_device.id)  # listed right now
    hub.handle_envelope(devices_event(2))
    await hass.async_block_till_done()
    assert await remove(tv_device.id)
    await hass.async_block_till_done()
    assert TV_ID not in hub.devices
    assert hass.states.get(PLAYER) is None

    # It comes back: so do its player and pickers.
    hub.handle_envelope(devices_event(3, tv(state=playing(**TRACKS))))
    await hass.async_block_till_done()
    assert hass.states.get(PLAYER) is not None
    assert hass.states.get(AUDIO) is not None


async def test_setup_retries_when_leyla_is_away(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    mock_api["me"].side_effect = LeylaConnectionError("down")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
