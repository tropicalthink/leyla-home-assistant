"""The config flow: address, then a sign-in code or a token; reauth."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.leyla.api import (
    DeviceCode,
    LeylaAuthError,
    LeylaCodeGone,
    LeylaConnectionError,
)
from custom_components.leyla.const import DOMAIN

from .conftest import ACCOUNT_ID, TOKEN, URL, account

CODE = DeviceCode(
    user_code="ABCD-EFGH",
    poll_token="poll-secret",
    expires_at="2099-01-01T00:00:00Z",
    interval=0,
    verification_path="/link",
)


def patch_api(**methods: AsyncMock):
    """Patch LeylaApi methods for the flow."""
    patches = [
        patch(f"custom_components.leyla.api.LeylaApi.{name}", new=mock)
        for name, mock in methods.items()
    ]

    class _Many:
        def __enter__(self):
            for p in patches:
                p.start()

        def __exit__(self, *exc):
            for p in patches:
                p.stop()

    return _Many()


def no_setup():
    return patch("custom_components.leyla.async_setup_entry", return_value=True)


async def start(hass: HomeAssistant, url: str = "leyla.local:10001/"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: url})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "method"
    return result


async def test_invalid_url(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "ftp://leyla.local"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_URL: "invalid_url"}


async def test_token_path(hass: HomeAssistant) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    assert result["step_id"] == "token"
    me = AsyncMock(
        side_effect=[LeylaAuthError(), LeylaConnectionError("down"), account(), account()]
    )
    with patch_api(me=me), no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "wrong"}
        )
        assert result["errors"] == {"base": "invalid_auth"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "x"}
        )
        assert result["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: f"Bearer {TOKEN} "}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "maya on leyla.local:10001"
    assert result["data"] == {CONF_URL: URL, CONF_ACCESS_TOKEN: TOKEN}
    assert result["result"].unique_id == ACCOUNT_ID


async def test_code_path(hass: HomeAssistant) -> None:
    result = await start(hass)
    poll = AsyncMock(side_effect=[None, None, {"access_token": TOKEN, "account": account()}])
    with (
        patch_api(
            start_device_code=AsyncMock(return_value=CODE),
            poll_device_code=poll,
            me=AsyncMock(return_value=account()),
        ),
        no_setup(),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "code"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "wait_for_approval"
        assert result["description_placeholders"] == {
            "code": "ABCD-EFGH",
            "link": f"{URL}/link",
        }
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: URL, CONF_ACCESS_TOKEN: TOKEN}
    assert poll.await_count == 3
    poll.assert_awaited_with("poll-secret")


async def test_code_expired_offers_a_new_one(hass: HomeAssistant) -> None:
    result = await start(hass)
    with patch_api(
        start_device_code=AsyncMock(return_value=CODE),
        poll_device_code=AsyncMock(side_effect=[LeylaConnectionError("blip"), LeylaCodeGone()]),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "code"}
        )
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "code_expired"
    assert result["menu_options"] == ["code", "token"]


async def test_code_cannot_connect(hass: HomeAssistant) -> None:
    result = await start(hass)
    with patch_api(start_device_code=AsyncMock(side_effect=LeylaConnectionError("down"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "code"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_already_configured(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    with patch_api(me=AsyncMock(return_value=account())), no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "new-token"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_ACCESS_TOKEN] == "new-token"


async def test_reauth_with_token(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    with patch_api(me=AsyncMock(return_value=account())), no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "fresh"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == "fresh"


async def test_reauth_wrong_account(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    other = account() | {"id": "someone-else"}
    with patch_api(me=AsyncMock(return_value=other)), no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "theirs"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ACCESS_TOKEN] == TOKEN
