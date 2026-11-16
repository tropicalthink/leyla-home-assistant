"""A media player per TV: state mapping, the list's lifecycle, commands."""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_DURATION,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_POSITION_UPDATED_AT,
    ATTR_MEDIA_SEEK_POSITION,
    ATTR_MEDIA_SERIES_TITLE,
    ATTR_MEDIA_TITLE,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    DOMAIN as MP_DOMAIN,
    MediaPlayerEntityFeature,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.leyla.const import DOMAIN

from .conftest import TV2_ID, TV_ID, devices_event, playing, setup, tv

ENTITY = "media_player.leyla_app_on_lg_tv"


async def test_tv_appears_with_its_state(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, socket = await setup(hass, entry)
    subscribed = [m["topic"] for m in socket.sent if m["op"] == "subscribe"]
    assert subscribed == ["remote", "catalog"]

    hub.handle_envelope(devices_event(3, tv(state=playing())))
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_MEDIA_TITLE] == "Arrival"
    assert state.attributes[ATTR_MEDIA_CONTENT_ID] == "item-1"
    assert state.attributes[ATTR_MEDIA_CONTENT_TYPE] == "movie"
    assert state.attributes[ATTR_MEDIA_POSITION] == 61
    assert state.attributes[ATTR_MEDIA_DURATION] == 6960
    assert state.attributes[ATTR_MEDIA_POSITION_UPDATED_AT] is not None
    assert state.attributes[ATTR_MEDIA_VOLUME_LEVEL] == 0.4
    assert state.attributes[ATTR_MEDIA_VOLUME_MUTED] is False
    features = MediaPlayerEntityFeature(state.attributes[ATTR_SUPPORTED_FEATURES])
    assert MediaPlayerEntityFeature.VOLUME_SET in features
    assert MediaPlayerEntityFeature.SEEK in features
    assert MediaPlayerEntityFeature.NEXT_TRACK not in features
    assert state.attributes["entity_picture"].startswith("/api/media_player_proxy/")
    assert "secret" not in state.attributes["entity_picture"]

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, TV_ID), entry.entry_id)
    assert device is not None
    assert device.name == "Leyla app on LG TV"
    assert device.via_device_id is not None


async def test_episode_without_volume(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, _ = await setup(hass, entry)
    hub.handle_envelope(
        devices_event(
            1,
            tv(
                state=playing(
                    status="paused",
                    title="Severance",
                    detail="S1 E3 · In Perpetuity",
                    volume=None,
                    has_next=True,
                )
            ),
        )
    )
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == STATE_PAUSED
    assert state.attributes[ATTR_MEDIA_TITLE] == "S1 E3 · In Perpetuity"
    assert state.attributes[ATTR_MEDIA_SERIES_TITLE] == "Severance"
    assert state.attributes[ATTR_MEDIA_CONTENT_TYPE] == "episode"
    assert ATTR_MEDIA_VOLUME_LEVEL not in state.attributes
    features = MediaPlayerEntityFeature(state.attributes[ATTR_SUPPORTED_FEATURES])
    assert MediaPlayerEntityFeature.VOLUME_SET not in features
    assert MediaPlayerEntityFeature.NEXT_TRACK in features


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("idle", STATE_IDLE),
        ("loading", "buffering"),
        ("buffering", "buffering"),
        ("ended", STATE_IDLE),
        ("error", STATE_IDLE),
    ],
)
async def test_status_mapping(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: dict[str, Any],
    status: str,
    expected: str,
) -> None:
    hub, _ = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing(status=status, error="boom"))))
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == expected
    if status == "error":
        assert state.attributes["error"] == "boom"


async def test_not_yet_reported_is_idle(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, _ = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=None, controller={"id": "p", "name": "Phone"})))
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == STATE_IDLE
    assert state.attributes["controlled_from"] == "Phone"


async def test_older_revision_is_dropped(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(5, tv(state=playing())))
    hub.handle_envelope(devices_event(4, tv(state=playing(status="paused"))))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_PLAYING

    # A reconnect starts over: a restarted server counts from 1 again.
    hub._on_closed()
    await hub._on_open(socket)  # type: ignore[arg-type]
    hub.handle_envelope(devices_event(1, tv(state=playing(status="paused"))))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_PAUSED


async def test_tv_leaves_and_returns(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, _ = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    await hass.async_block_till_done()
    hub.handle_envelope(devices_event(2))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE
    assert er.async_get(hass).async_get(ENTITY) is not None

    hub.handle_envelope(devices_event(3, tv(state=playing())))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_PLAYING

    # The link drops: every TV is unavailable until it is back.
    hub._on_closed()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE


async def test_tvs_come_back_after_a_restart(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, _ = await setup(hass, entry)
    hub.handle_envelope(
        devices_event(1, tv(state=playing()), tv(TV2_ID, "Leyla app on Android TV"))
    )
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE
    assert hass.states.get("media_player.leyla_app_on_android_tv").state == STATE_UNAVAILABLE


async def _call(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call(
        MP_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY, **data}, blocking=True
    )


async def test_commands_then_release(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing(has_next=True))))
    await hass.async_block_till_done()

    await _call(hass, "media_pause")
    await _call(hass, "media_play")
    await _call(hass, "media_stop")
    await _call(hass, "media_next_track")
    await _call(hass, "media_seek", **{ATTR_MEDIA_SEEK_POSITION: 90.25})
    await _call(hass, "volume_set", **{ATTR_MEDIA_VOLUME_LEVEL: 0.7})
    await _call(hass, "volume_mute", **{ATTR_MEDIA_VOLUME_MUTED: True})

    release = ("remote.release", {"device_id": TV_ID})
    assert socket.commands() == [
        ("remote.control", {"device_id": TV_ID, "action": "pause"}),
        release,
        ("remote.control", {"device_id": TV_ID, "action": "play"}),
        release,
        ("remote.control", {"device_id": TV_ID, "action": "stop"}),
        release,
        ("remote.control", {"device_id": TV_ID, "action": "next"}),
        release,
        ("remote.control", {"device_id": TV_ID, "action": "seek", "position_ms": 90250}),
        release,
        ("remote.control", {"device_id": TV_ID, "action": "volume", "level": 0.7}),
        release,
        ("remote.control", {"device_id": TV_ID, "action": "mute", "muted": True}),
        release,
    ]


async def test_refused_command_raises(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    await hass.async_block_till_done()
    socket.answers["remote.control"] = (False, "DEVICE_NOT_FOUND")
    with pytest.raises(HomeAssistantError, match="DEVICE_NOT_FOUND"):
        await _call(hass, "media_pause")
    # Nothing to release after a refusal.
    assert [c for c, _ in socket.commands()] == ["remote.control"]


async def test_command_while_disconnected_raises(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, _ = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing())))
    await hass.async_block_till_done()
    entity = hass.data["entity_components"][MP_DOMAIN].get_entity(ENTITY)
    hub._ws = None
    with pytest.raises(HomeAssistantError):
        await entity.async_media_pause()


async def test_play_media(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, socket = await setup(hass, entry)
    hub.handle_envelope(devices_event(1, tv(state=playing(status="idle"))))
    await hass.async_block_till_done()
    mock_api["catalog_item"].return_value = {"id": "film", "primary_media_source_id": "src-9"}

    await _call(hass, "play_media", media_content_id="film", media_content_type="movie")
    await _call(hass, "play_media", media_content_id="film:src-2", media_content_type="movie")

    plays = [p for c, p in socket.commands() if c == "remote.play"]
    assert plays == [
        {"device_id": TV_ID, "item_id": "film", "media_source_id": "src-9"},
        {"device_id": TV_ID, "item_id": "film", "media_source_id": "src-2"},
    ]
    mock_api["catalog_item"].assert_awaited_once_with("film")


async def test_media_image_uses_the_series_poster(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: dict[str, Any]
) -> None:
    hub, _ = await setup(hass, entry)
    hub.handle_envelope(
        devices_event(1, tv(state=playing(item_id="ep-1", title="Severance", detail="S1 E1")))
    )
    await hass.async_block_till_done()
    mock_api["catalog_item"].side_effect = [
        {
            "id": "ep-1",
            "artwork": [{"kind": "still", "url": "/api/v1/artwork/s"}],
            "series": {"id": "show"},
        },
        {
            "id": "show",
            "artwork": [
                {"kind": "poster", "url": "/api/v1/artwork/p1/abc.jpg", "chosen": False},
                {"kind": "poster", "url": "/api/v1/artwork/p2/def.jpg", "chosen": True},
            ],
        },
    ]
    mock_api["fetch_image"].return_value = (b"jpeg-bytes", "image/jpeg")
    entity = hass.data["entity_components"][MP_DOMAIN].get_entity(ENTITY)

    assert await entity.async_get_media_image() == (b"jpeg-bytes", "image/jpeg")
    # Cached: a second ask does not refetch.
    assert await entity.async_get_media_image() == (b"jpeg-bytes", "image/jpeg")
    mock_api["fetch_image"].assert_awaited_once_with("/api/v1/artwork/p2/def.jpg")
