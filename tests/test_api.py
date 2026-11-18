"""The HTTP client against a mocked server: every call, and its errors."""

from __future__ import annotations

from http import HTTPStatus

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.leyla.api import (
    LeylaApi,
    LeylaApiError,
    LeylaAuthError,
    LeylaCodeGone,
    LeylaConnectionError,
)

from .conftest import URL

API = f"{URL}/api/v1"


@pytest.fixture
async def api(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> LeylaApi:
    return LeylaApi(async_get_clientsession(hass), URL, "tok")


async def test_reachable(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{URL}/healthz", text="ok")
    await api.reachable()
    assert "Authorization" not in (aioclient_mock.mock_calls[0][3] or {})
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/healthz", status=HTTPStatus.NOT_FOUND)
    with pytest.raises(LeylaApiError):
        await api.reachable()


async def test_me(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/auth/me", json={"id": "a", "username": "maya"})
    assert (await api.me())["id"] == "a"
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer tok"


async def test_me_errors(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/auth/me", status=HTTPStatus.UNAUTHORIZED)
    with pytest.raises(LeylaAuthError):
        await api.me()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/auth/me", text="<html>")
    with pytest.raises(LeylaConnectionError):
        await api.me()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/auth/me", exc=aiohttp.ClientError("refused"))
    with pytest.raises(LeylaConnectionError):
        await api.me()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/auth/me", exc=TimeoutError())
    with pytest.raises(LeylaConnectionError):
        await api.me()


async def test_no_token_is_refused_before_asking(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    with pytest.raises(LeylaAuthError):
        await LeylaApi(async_get_clientsession(hass), URL).me()


async def test_device_code(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        f"{API}/auth/device-codes",
        status=HTTPStatus.CREATED,
        json={"user_code": "AB", "poll_token": "p", "expires_at": "x", "interval": 0},
    )
    code = await api.start_device_code()
    assert (code.user_code, code.interval, code.verification_path) == ("AB", 1, "/link")
    assert "Authorization" not in (aioclient_mock.mock_calls[0][3] or {})


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, None, LeylaConnectionError),
        (500, {"error": {"code": "BOOM"}}, LeylaApiError),
        (200, {"user_code": "AB"}, LeylaConnectionError),
    ],
)
async def test_device_code_errors(
    api: LeylaApi, aioclient_mock: AiohttpClientMocker, status: int, body: object, error: type
) -> None:
    aioclient_mock.post(f"{API}/auth/device-codes", status=status, json=body)
    with pytest.raises(error):
        await api.start_device_code()


async def test_poll_device_code(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{API}/auth/device-codes/poll", status=HTTPStatus.ACCEPTED)
    assert await api.poll_device_code("p") is None
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API}/auth/device-codes/poll", json={"access_token": "t"})
    assert (await api.poll_device_code("p"))["access_token"] == "t"
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API}/auth/device-codes/poll", status=HTTPStatus.GONE)
    with pytest.raises(LeylaCodeGone):
        await api.poll_device_code("p")
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API}/auth/device-codes/poll", status=HTTPStatus.BAD_REQUEST)
    with pytest.raises(LeylaApiError):
        await api.poll_device_code("p")


async def test_live_ticket(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{API}/live/ticket", json={"ticket": "abc"})
    assert await api.live_ticket() == "abc"
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API}/live/ticket", status=HTTPStatus.FORBIDDEN)
    with pytest.raises(LeylaAuthError):
        await api.live_ticket()
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API}/live/ticket", status=HTTPStatus.SERVICE_UNAVAILABLE)
    with pytest.raises(LeylaApiError):
        await api.live_ticket()


async def test_lists(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/admin/now-playing", json={"sessions": [{"id": 1}]})
    aioclient_mock.get(f"{API}/libraries", json=[{"id": "l"}])
    aioclient_mock.get(f"{API}/catalog/x", json={"id": "x"})
    aioclient_mock.get(f"{API}/catalog/counts", json=[{"library_id": "l", "item_count": 3}])
    aioclient_mock.get(f"{API}/home/continue-watching", json={"items": [{"catalog_item_id": "c"}]})
    aioclient_mock.get(f"{API}/series/s/continue", json={"media_source_id": "m"})
    aioclient_mock.get(f"{API}/admin/updates", json={"running": "0.15.1"})
    aioclient_mock.get(f"{API}/admin/findings", json={"findings": [{"id": "k:s"}]})
    aioclient_mock.get(
        f"{API}/catalog?limit=5&offset=10&library_id=l&type=title&q=arrival&sort=title",
        json={"items": [], "has_more": False},
    )
    assert await api.now_playing() == [{"id": 1}]
    assert await api.libraries() == [{"id": "l"}]
    assert await api.catalog_item("x") == {"id": "x"}
    assert (await api.catalog_counts())[0]["item_count"] == 3
    assert (await api.continue_watching())[0]["catalog_item_id"] == "c"
    assert await api.series_continue("s") == {"media_source_id": "m"}
    assert (await api.updates())["running"] == "0.15.1"
    assert (await api.findings())[0]["id"] == "k:s"
    page = await api.catalog_list(
        library_id="l", kind="title", query="arrival", sort="title", limit=5, offset=10
    )
    assert page["has_more"] is False


async def test_lists_refuse_odd_answers(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/admin/now-playing", status=HTTPStatus.FORBIDDEN)
    aioclient_mock.get(f"{API}/libraries", json={"not": "a list"})
    aioclient_mock.get(f"{API}/catalog/x", status=HTTPStatus.NOT_FOUND)
    aioclient_mock.get(f"{API}/catalog/counts", json={})
    aioclient_mock.get(f"{API}/home/continue-watching", status=HTTPStatus.BAD_GATEWAY)
    aioclient_mock.get(f"{API}/series/s/continue", status=HTTPStatus.NOT_FOUND)
    aioclient_mock.get(f"{API}/admin/updates", json={})
    aioclient_mock.get(f"{API}/admin/findings", status=HTTPStatus.FORBIDDEN)
    aioclient_mock.get(f"{API}/catalog?limit=100&offset=0", json={"items": None})
    for call in (
        api.now_playing,
        api.libraries,
        lambda: api.catalog_item("x"),
        api.catalog_counts,
        api.continue_watching,
        lambda: api.series_continue("s"),
        api.updates,
        api.findings,
        api.catalog_list,
    ):
        with pytest.raises(LeylaApiError):
            await call()


async def test_empty_answers(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/admin/now-playing", json={})
    aioclient_mock.get(f"{API}/home/continue-watching", json={})
    aioclient_mock.get(f"{API}/series/s/continue", json=None)
    aioclient_mock.get(f"{API}/admin/findings", json={})
    assert await api.now_playing() == []
    assert await api.continue_watching() == []
    assert await api.series_continue("s") is None
    assert await api.findings() == []


async def test_actions(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{API}/libraries/l/scans", status=HTTPStatus.ACCEPTED)
    aioclient_mock.post(f"{API}/admin/updates/install", status=HTTPStatus.ACCEPTED)
    aioclient_mock.post(
        f"{API}/admin/findings/disk%3A%2Fmedia/acknowledge?title=Disk+full",
        status=HTTPStatus.NO_CONTENT,
    )
    await api.start_scan("l")
    await api.install_update()
    await api.acknowledge_finding("disk:/media", "Disk full")
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API}/libraries/l/scans", status=HTTPStatus.CONFLICT)
    aioclient_mock.post(f"{API}/admin/updates/install", status=HTTPStatus.CONFLICT)
    aioclient_mock.post(f"{API}/admin/findings/k/acknowledge", status=HTTPStatus.NOT_FOUND)
    for call in (
        lambda: api.start_scan("l"),
        api.install_update,
        lambda: api.acknowledge_finding("k"),
    ):
        with pytest.raises(LeylaApiError):
            await call()


async def test_fetch_image(api: LeylaApi, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(
        f"{API}/artwork/p.jpg?w=300", content=b"jpeg", headers={"Content-Type": "image/jpeg"}
    )
    assert await api.fetch_image(f"{URL}/api/v1/artwork/p.jpg?w=300") == (b"jpeg", "image/jpeg")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/artwork/a.jpg", status=HTTPStatus.UNAUTHORIZED)
    with pytest.raises(LeylaAuthError):
        await api.fetch_image("/api/v1/artwork/a.jpg")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/artwork/a.jpg", status=HTTPStatus.NOT_FOUND)
    with pytest.raises(LeylaApiError):
        await api.fetch_image("/api/v1/artwork/a.jpg")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/artwork/a.jpg", content=b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(LeylaConnectionError):
        await api.fetch_image("/api/v1/artwork/a.jpg")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/artwork/a.jpg", exc=aiohttp.ClientError())
    with pytest.raises(LeylaConnectionError):
        await api.fetch_image("/api/v1/artwork/a.jpg")


async def test_fetch_image_needs_a_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    with pytest.raises(LeylaAuthError):
        await LeylaApi(async_get_clientsession(hass), URL).fetch_image("/api/v1/artwork/a.jpg")
