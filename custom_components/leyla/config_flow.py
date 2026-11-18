"""Config flow: the server's address, then a sign-in code or a pasted token.

Leyla has no personal API tokens; a sign-in is a 30-day sliding session.
The preferred way in is a sign-in code: Home Assistant shows a code, the
person opens <server>/link on a device where they are signed in and
approves it, and the server hands this flow its own session (named "Leyla
app on Home Assistant" in Your devices, where it can be signed out). A
token copied from an existing sign-in also works.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_NAME, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.util import dt as dt_util
import probatio

from .api import (
    DeviceCode,
    LeylaApi,
    LeylaAuthError,
    LeylaCodeGone,
    LeylaError,
    normalize_url,
)
from .const import CONF_SERVER_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

# The server is the judge of a code's life; this only stops a flow whose
# server stopped answering from polling forever.
EXPIRY_SLACK = timedelta(seconds=30)


class LeylaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Leyla server."""

    VERSION = 1

    def __init__(self) -> None:
        self._url: str | None = None
        self._code: DeviceCode | None = None
        self._code_task: asyncio.Task[dict[str, Any] | None] | None = None
        self._token: str | None = None
        self._server_id: str | None = None
        self._name: str | None = None

    @callback
    def async_remove(self) -> None:
        """Stop polling when the flow is closed."""
        if self._code_task is not None:
            self._code_task.cancel()

    def _api(self, token: str | None = None) -> LeylaApi:
        assert self._url is not None
        return LeylaApi(async_get_clientsession(self.hass), self._url, token)

    # ---- the server --------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the server's address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                # Ask now, so a mistyped address is fixed here rather than
                # found after choosing how to sign in.
                try:
                    await self._api().reachable()
                except LeylaError:
                    errors["base"] = "cannot_connect"
                else:
                    return await self.async_step_method()
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_URL, default=self._url or ""): str}
            ),
            errors=errors,
        )

    # ---- found on the home network ------------------------------------------

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """A Leyla server announced itself (`_leyla._tcp`, with its id and port)."""
        props = discovery_info.properties
        port = props.get("http") or str(discovery_info.port or "")
        host = discovery_info.host
        if ":" in host:
            host = f"[{host}]"
        self._url = f"http://{host}:{port}" if port else f"http://{host}"
        self._server_id = props.get("id") or None
        self._name = props.get("name") or discovery_info.name.split(".", 1)[0]
        for entry in self._async_current_entries(include_ignore=True):
            if self._server_id and entry.data.get(CONF_SERVER_ID) == self._server_id:
                return self.async_abort(reason="already_configured")
            known = entry.data.get(CONF_URL, "")
            if (
                known
                and known.split("://", 1)[-1].rsplit(":", 1)[0].strip("[]") == discovery_info.host
            ):
                if self._server_id and not entry.data.get(CONF_SERVER_ID):
                    # Remember the server's id, so a new address is still it.
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_SERVER_ID: self._server_id}
                    )
                return self.async_abort(reason="already_configured")
        if self._server_id:
            # One discovery flow per server, however many addresses it has.
            await self.async_set_unique_id(f"server:{self._server_id}")
            self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up the server that was found?"""
        if user_input is not None:
            return await self.async_step_method()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={"name": self._name or "", "url": self._url or ""},
        )

    async def async_step_method(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign in with a code (preferred) or with a token."""
        return self.async_show_menu(step_id="method", menu_options=["code", "token"])

    # ---- sign-in code ------------------------------------------------------

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show a sign-in code and wait while the person approves it."""
        if self._code_task is None:
            try:
                self._code = await self._api().start_device_code()
            except LeylaError as err:
                _LOGGER.debug("sign-in code request failed: %s", err)
                return self.async_abort(reason="cannot_connect")
            self._code_task = self.hass.async_create_task(self._wait_for_approval(self._code))
        if self._code_task.done():
            task, self._code_task = self._code_task, None
            try:
                session = task.result()
            except LeylaCodeGone:
                return self.async_show_progress_done(next_step_id="code_expired")
            except LeylaError:
                return self.async_show_progress_done(next_step_id="code_failed")
            if session is None:
                return self.async_show_progress_done(next_step_id="code_expired")
            self._token = str(session["access_token"])
            return self.async_show_progress_done(next_step_id="finish")
        assert self._code is not None
        return self.async_show_progress(
            step_id="code",
            progress_action="wait_for_approval",
            description_placeholders={
                "code": self._code.user_code,
                "link": f"{self._url}{self._code.verification_path}",
            },
            progress_task=self._code_task,
        )

    async def _wait_for_approval(self, code: DeviceCode) -> dict[str, Any] | None:
        """Poll at the server's interval, one request at a time."""
        api = self._api()
        expires = dt_util.parse_datetime(code.expires_at)
        while True:
            await asyncio.sleep(code.interval)
            if expires is not None and dt_util.utcnow() > expires + EXPIRY_SLACK:
                return None
            try:
                session = await api.poll_device_code(code.poll_token)
            except LeylaCodeGone:
                raise
            except LeylaError as err:
                # A dropped request is not a refusal; keep waiting until the
                # server says the code is gone.
                _LOGGER.debug("sign-in code poll failed: %s", err)
                continue
            if session is not None:
                return session

    async def async_step_code_expired(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The code ran out: offer a new one or the token path."""
        return self.async_show_menu(step_id="code_expired", menu_options=["code", "token"])

    async def async_step_code_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The server answered the poll with an error."""
        return self.async_abort(reason="cannot_connect")

    # ---- token -------------------------------------------------------------

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Paste a token from an existing sign-in."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_ACCESS_TOKEN].strip()
            if token.lower().startswith("bearer "):
                token = token[len("bearer ") :].strip()
            try:
                await self._api(token).me()
            except LeylaAuthError:
                errors["base"] = "invalid_auth"
            except LeylaError:
                errors["base"] = "cannot_connect"
            else:
                self._token = token
                return await self.async_step_finish()
        return self.async_show_form(
            step_id="token",
            data_schema=probatio.Schema({probatio.Required(CONF_ACCESS_TOKEN): str}),
            errors=errors,
        )

    # ---- the entry ---------------------------------------------------------

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Read the account and create (or update) the entry."""
        assert self._token is not None
        try:
            account = await self._api(self._token).me()
        except LeylaAuthError:
            return self.async_abort(reason="invalid_auth")
        except LeylaError:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(str(account["id"]), raise_on_progress=False)
        data: dict[str, Any] = {CONF_URL: self._url, CONF_ACCESS_TOKEN: self._token}
        if self._server_id:
            data[CONF_SERVER_ID] = self._server_id
        if self._name:
            data[CONF_NAME] = self._name
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        self._abort_if_unique_id_configured(updates=data)
        host = self._url.split("://", 1)[-1] if self._url else ""
        who = account.get("username", "Leyla")
        title = f"{self._name} ({who})" if self._name else f"{who} on {host}"
        return self.async_create_entry(title=title, data=data)

    # ---- reauth ------------------------------------------------------------

    # ---- a new address -----------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The server moved: a new address, the same sign-in."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                try:
                    account = await self._api(entry.data[CONF_ACCESS_TOKEN]).me()
                except LeylaAuthError:
                    errors["base"] = "invalid_auth"
                except LeylaError:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(str(account["id"]))
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_URL: self._url}
                    )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_URL, default=self._url or entry.data[CONF_URL]): str}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The sign-in stopped working (signed out, expired or revoked)."""
        self._url = entry_data[CONF_URL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in again, by code or by token."""
        return self.async_show_menu(
            step_id="reauth_confirm",
            menu_options=["code", "token"],
            description_placeholders={"url": self._url or ""},
        )
