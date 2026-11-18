"""Found on the home network, a new address, and the connection log lines."""

from __future__ import annotations

from ipaddress import ip_address
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_ZEROCONF
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.leyla.api import LeylaAuthError, LeylaConnectionError
from custom_components.leyla.const import CONF_SERVER_ID, DOMAIN
from custom_components.leyla.hub import LeylaHub

from .conftest import ACCOUNT_ID, TOKEN, URL, account
from .test_config_flow import no_setup, patch_api

SERVER_ID = "0199a0b1-0000-7000-8000-00000000c0de"


def found(host: str = "192.168.1.20", props: dict[str, str] | None = None) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        hostname="leyla.local.",
        name="Living room._leyla._tcp.local.",
        port=10001,
        type="_leyla._tcp.local.",
        properties={"id": SERVER_ID, "name": "Living room", "http": "10001"}
        if props is None
        else props,
    )


async def test_found_then_signed_in(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=found()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {
        "name": "Living room",
        "url": "http://192.168.1.20:10001",
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "method"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    with patch_api(me=AsyncMock(return_value=account())), no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: TOKEN}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_URL: "http://192.168.1.20:10001",
        CONF_ACCESS_TOKEN: TOKEN,
        CONF_SERVER_ID: SERVER_ID,
        "name": "Living room",
    }
    assert result["result"].unique_id == ACCOUNT_ID
    assert result["title"] == "Living room (maya)"


async def test_found_twice_is_one_flow(hass: HomeAssistant) -> None:
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=found()
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=found("fe80::1")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"


async def test_found_without_txt_uses_the_port(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=found("fe80::1", props={})
    )
    assert result["description_placeholders"] == {
        "name": "Living room",
        "url": "http://[fe80::1]:10001",
    }


async def test_known_server_is_not_offered_again(hass: HomeAssistant) -> None:
    by_id = MockConfigEntry(
        domain=DOMAIN,
        unique_id="a",
        data={CONF_URL: "https://media.example", CONF_SERVER_ID: SERVER_ID},
    )
    by_id.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=found()
    )
    assert result["reason"] == "already_configured"


async def test_known_address_learns_the_server_id(hass: HomeAssistant) -> None:
    by_host = MockConfigEntry(
        domain=DOMAIN, unique_id="b", data={CONF_URL: "http://192.168.1.20:10001"}
    )
    by_host.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=found()
    )
    assert result["reason"] == "already_configured"
    assert by_host.data[CONF_SERVER_ID] == SERVER_ID


async def test_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "ftp://x"}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    me = AsyncMock(
        side_effect=[LeylaAuthError(), LeylaConnectionError("down"), {**account(), "id": "other"}]
    )
    with patch_api(me=me), no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "10.0.0.5:10001"}
        )
        assert result["errors"] == {"base": "invalid_auth"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "10.0.0.5:10001"}
        )
        assert result["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "10.0.0.5:10001"}
        )
        assert result["reason"] == "wrong_account"

    result = await entry.start_reconfigure_flow(hass)
    with patch_api(me=AsyncMock(return_value=account())), no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "10.0.0.5:10001"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == "http://10.0.0.5:10001"
    assert entry.data[CONF_ACCESS_TOKEN] == TOKEN


async def test_logs_once_when_lost_and_once_when_back(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    api = MagicMock()
    api.url = URL
    api.live_ticket = AsyncMock(side_effect=LeylaConnectionError("refused"))
    entry = MagicMock()
    entry.entry_id = "entry"
    hub = LeylaHub(hass, entry, api, MagicMock(), account())
    sleeps = 0

    async def fake_sleep(_delay: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            hub._stopping = True

    caplog.set_level(logging.INFO, logger="custom_components.leyla")
    with patch("custom_components.leyla.hub.asyncio.sleep", fake_sleep):
        await hub._run()
    lost = [r for r in caplog.records if "Lost the connection" in r.message]
    assert len(lost) == 1
    socket = MagicMock()
    socket.send_json = AsyncMock()
    socket.closed = False
    await hub._on_open(socket)
    await hub._on_open(socket)
    assert len([r for r in caplog.records if "Connected to Leyla" in r.message]) == 1
