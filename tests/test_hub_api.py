"""The reconnect loop's backoff and the HTTP client's guards."""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.leyla.api import (
    LeylaApi,
    LeylaConnectionError,
    normalize_url,
    poster_url,
)
from custom_components.leyla.const import BACKOFF_MAX
from custom_components.leyla.hub import LeylaHub

from .conftest import URL, account


class _Stop(Exception):
    pass


def make_hub(hass: HomeAssistant, api: Any) -> LeylaHub:
    entry = MagicMock()
    entry.entry_id = "entry"
    return LeylaHub(hass, entry, api, MagicMock(), account())


async def test_backoff_grows_to_a_few_minutes_one_request_at_a_time(
    hass: HomeAssistant,
) -> None:
    api = MagicMock()
    api.live_ticket = AsyncMock(side_effect=LeylaConnectionError("refused"))
    hub = make_hub(hass, api)
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 12:
            raise _Stop

    with patch("custom_components.leyla.hub.asyncio.sleep", fake_sleep), pytest.raises(_Stop):
        await hub._run()
    # One ticket request per attempt, never more.
    assert api.live_ticket.await_count == 12
    assert delays[0] < 1.3
    assert all(b >= a * 1.3 for a, b in itertools.pairwise(delays[:7]))
    assert max(delays) <= BACKOFF_MAX * 1.2
    assert delays[-1] >= BACKOFF_MAX * 0.8


async def test_a_signed_out_ticket_starts_reauth(hass: HomeAssistant) -> None:
    from custom_components.leyla.api import LeylaAuthError

    api = MagicMock()
    api.live_ticket = AsyncMock(side_effect=LeylaAuthError)
    hub = make_hub(hass, api)
    await hub._run()
    hub.entry.async_start_reauth.assert_called_once_with(hass)
    assert api.live_ticket.await_count == 1


def test_normalize_url() -> None:
    assert normalize_url("leyla.local:10001/") == "http://leyla.local:10001"
    assert normalize_url(" https://media.example.org ") == "https://media.example.org"
    with pytest.raises(ValueError):
        normalize_url("ftp://x")


def test_live_url() -> None:
    assert LeylaApi(MagicMock(), "https://m.example").live_url("ab") == (
        "wss://m.example/api/v1/live?ticket=ab"
    )
    assert LeylaApi(MagicMock(), URL).live_url("ab").startswith("ws://leyla.local:10001/")


async def test_artwork_never_leaves_the_server() -> None:
    session = MagicMock()
    api = LeylaApi(session, URL, "tok")
    for url in (
        "https://elsewhere.example/api/v1/artwork/x.jpg",
        "/api/v1/auth/me",
        "//elsewhere.example/api/v1/artwork/x.jpg",
    ):
        with pytest.raises(LeylaConnectionError):
            await api.fetch_image(url)
    session.get.assert_not_called()


def test_poster_url() -> None:
    assert poster_url({"artwork": []}) is None
    assert (
        poster_url(
            {
                "artwork": [
                    {"kind": "backdrop", "url": "/b", "chosen": True},
                    {"kind": "poster", "url": "/p1"},
                    {"kind": "poster", "url": "/p2", "chosen": True},
                ]
            }
        )
        == "/p2"
    )
