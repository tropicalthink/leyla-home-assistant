"""A small client for the parts of the Leyla HTTP API this integration uses.

Everything here talks to the operator's own server and nothing else. The
bearer token is sent only to that server's origin: an artwork URL that
points anywhere else is refused rather than fetched with the token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import aiohttp

from .const import CLIENT_DEVICE

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class LeylaError(Exception):
    """Base error."""


class LeylaConnectionError(LeylaError):
    """The server could not be reached, or answered with something unexpected."""


class LeylaAuthError(LeylaError):
    """The token is missing, expired or signed out (401)."""


class LeylaCodeGone(LeylaError):
    """The sign-in code expired or was never approved in time (404 or 410)."""


class LeylaApiError(LeylaError):
    """Any other error answer, with the server's error code when it sent one."""

    def __init__(self, status: int, code: str | None) -> None:
        super().__init__(f"HTTP {status} {code or ''}".strip())
        self.status = status
        self.code = code


@dataclass(frozen=True)
class DeviceCode:
    """A sign-in code waiting to be approved at <server>/link."""

    user_code: str
    poll_token: str
    expires_at: str
    interval: int
    verification_path: str


def normalize_url(raw: str) -> str:
    """The server's base URL: a scheme and host, no trailing slash."""
    url = raw.strip().rstrip("/")
    if "://" not in url:
        url = f"http://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("not an http(s) URL")
    return url


class LeylaApi:
    """HTTP calls against one Leyla server, as one signed-in account."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str | None = None) -> None:
        self._session = session
        self.url = url
        self.token = token
        origin = urlsplit(url)
        self._origin = (origin.scheme, origin.netloc)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        auth: bool = True,
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if auth:
            if not self.token:
                raise LeylaAuthError
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with self._session.request(
                method,
                f"{self.url}{path}",
                json=json,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = None
        except (TimeoutError, aiohttp.ClientError) as err:
            raise LeylaConnectionError(str(err)) from err
        if status == 401:
            raise LeylaAuthError
        return status, body

    @staticmethod
    def _error(status: int, body: Any) -> LeylaApiError:
        code = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = body["error"].get("code")
        return LeylaApiError(status, code)

    async def reachable(self) -> None:
        """Something answers at this address (`/healthz`, no sign-in needed)."""
        status, body = await self._request("GET", "/healthz", auth=False)
        if status != 200:
            raise self._error(status, body)

    async def me(self) -> dict[str, Any]:
        """The signed-in account: id, username, is_admin."""
        status, body = await self._request("GET", "/api/v1/auth/me")
        if status != 200 or not isinstance(body, dict) or "id" not in body:
            raise LeylaConnectionError(f"unexpected answer to /auth/me: {status}")
        return body

    async def start_device_code(self) -> DeviceCode:
        """Ask for a sign-in code (public route)."""
        status, body = await self._request(
            "POST",
            "/api/v1/auth/device-codes",
            json={"device": CLIENT_DEVICE},
            auth=False,
        )
        if status not in (200, 201) or not isinstance(body, dict):
            if status in (404, 405):
                # No such route: this is not a Leyla server.
                raise LeylaConnectionError("no sign-in code route")
            raise self._error(status, body)
        try:
            return DeviceCode(
                user_code=str(body["user_code"]),
                poll_token=str(body["poll_token"]),
                expires_at=str(body["expires_at"]),
                interval=max(1, int(body.get("interval", 5))),
                verification_path=str(body.get("verification_path", "/link")),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise LeylaConnectionError("malformed sign-in code answer") from err

    async def poll_device_code(self, poll_token: str) -> dict[str, Any] | None:
        """None while the code waits; the session body once approved."""
        status, body = await self._request(
            "POST",
            "/api/v1/auth/device-codes/poll",
            json={"poll_token": poll_token},
            auth=False,
        )
        if status == 202:
            return None
        if status in (200, 201) and isinstance(body, dict) and body.get("access_token"):
            return body
        if status in (404, 410):
            raise LeylaCodeGone
        raise self._error(status, body)

    async def live_ticket(self) -> str:
        """A single-use ticket for the live channel, valid 30 s."""
        status, body = await self._request("POST", "/api/v1/live/ticket")
        if status == 403:
            raise LeylaAuthError
        if status != 200 or not isinstance(body, dict) or not body.get("ticket"):
            raise self._error(status, body)
        return str(body["ticket"])

    def live_url(self, ticket: str) -> str:
        """The live channel's WebSocket URL for one ticket."""
        base = self.url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}/api/v1/live?ticket={quote(ticket)}"

    async def now_playing(self) -> list[dict[str, Any]]:
        """The live playback sessions (admins only)."""
        status, body = await self._request("GET", "/api/v1/admin/now-playing")
        if status != 200 or not isinstance(body, dict):
            raise self._error(status, body)
        sessions = body.get("sessions")
        return sessions if isinstance(sessions, list) else []

    async def libraries(self) -> list[dict[str, Any]]:
        """The libraries this account may see."""
        status, body = await self._request("GET", "/api/v1/libraries")
        if status != 200 or not isinstance(body, list):
            raise self._error(status, body)
        return body

    async def catalog_item(self, item_id: str) -> dict[str, Any]:
        """One title's detail."""
        status, body = await self._request("GET", f"/api/v1/catalog/{quote(item_id, safe='')}")
        if status != 200 or not isinstance(body, dict):
            raise self._error(status, body)
        return body

    async def catalog_list(
        self,
        *,
        library_id: str | None = None,
        kind: str | None = None,
        query: str | None = None,
        sort: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One page of titles: `items`, `total`, `has_more`."""
        params: dict[str, str] = {"limit": str(limit), "offset": str(offset)}
        if library_id:
            params["library_id"] = library_id
        if kind:
            params["type"] = kind
        if query:
            params["q"] = query
        if sort:
            params["sort"] = sort
        status, body = await self._request("GET", f"/api/v1/catalog?{urlencode(params)}")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise self._error(status, body)
        return body

    async def series_continue(self, series_id: str) -> dict[str, Any] | None:
        """The episode a series goes on with for this account, if any."""
        status, body = await self._request(
            "GET", f"/api/v1/series/{quote(series_id, safe='')}/continue"
        )
        if status != 200:
            raise self._error(status, body)
        return body if isinstance(body, dict) and body.get("media_source_id") else None

    async def continue_watching(self) -> list[dict[str, Any]]:
        """What this account is partway through, newest first."""
        status, body = await self._request("GET", "/api/v1/home/continue-watching")
        if status != 200 or not isinstance(body, dict):
            raise self._error(status, body)
        items = body.get("items")
        return items if isinstance(items, list) else []

    async def catalog_counts(self) -> list[dict[str, Any]]:
        """Each library's number of titles (films and series)."""
        status, body = await self._request("GET", "/api/v1/catalog/counts")
        if status != 200 or not isinstance(body, list):
            raise self._error(status, body)
        return body

    async def start_scan(self, library_id: str) -> None:
        """Ask the server to scan one library (admins only)."""
        status, body = await self._request(
            "POST", f"/api/v1/libraries/{quote(library_id, safe='')}/scans"
        )
        if status not in (200, 201, 202):
            raise self._error(status, body)

    async def updates(self) -> dict[str, Any]:
        """The server's own update state (admins only)."""
        status, body = await self._request("GET", "/api/v1/admin/updates")
        if status != 200 or not isinstance(body, dict) or "running" not in body:
            raise self._error(status, body)
        return body

    async def install_update(self) -> None:
        """Fetch, rehearse and install the newer release now."""
        status, body = await self._request("POST", "/api/v1/admin/updates/install")
        if status not in (200, 202, 204):
            raise self._error(status, body)

    async def findings(self) -> list[dict[str, Any]]:
        """What the server has judged needs attention (admins only)."""
        status, body = await self._request("GET", "/api/v1/admin/findings")
        if status != 200 or not isinstance(body, dict):
            raise self._error(status, body)
        findings = body.get("findings")
        return findings if isinstance(findings, list) else []

    async def acknowledge_finding(self, finding_id: str, title: str | None = None) -> None:
        """Set a finding aside, as Administration's inbox does."""
        path = f"/api/v1/admin/findings/{quote(finding_id, safe='')}/acknowledge"
        if title:
            path += f"?{urlencode({'title': title})}"
        status, body = await self._request("POST", path)
        if status not in (200, 204):
            raise self._error(status, body)

    async def fetch_image(self, url: str) -> tuple[bytes, str]:
        """An artwork image, fetched with the bearer from this server only."""
        parts = urlsplit(url)
        if parts.scheme or parts.netloc:
            if (parts.scheme, parts.netloc) != self._origin:
                raise LeylaConnectionError("artwork on another origin")
            path = parts.path + (f"?{parts.query}" if parts.query else "")
        else:
            path = url
        if not path.startswith("/api/v1/artwork/"):
            raise LeylaConnectionError("not an artwork path")
        if not self.token:
            raise LeylaAuthError
        try:
            async with self._session.get(
                f"{self.url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 401:
                    raise LeylaAuthError
                if response.status != 200:
                    raise LeylaApiError(response.status, None)
                data = await response.content.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    raise LeylaConnectionError("artwork too large")
                return data, response.content_type or "image/jpeg"
        except (TimeoutError, aiohttp.ClientError) as err:
            raise LeylaConnectionError(str(err)) from err


def poster_url(detail: dict[str, Any]) -> str | None:
    """The best poster of a title: a list row's `poster_artwork`, else the
    detail's chosen poster, else its first."""
    listed = detail.get("poster_artwork")
    if isinstance(listed, dict) and isinstance(listed.get("url"), str):
        return str(listed["url"])
    artwork = detail.get("artwork")
    if not isinstance(artwork, list):
        return None
    posters = [a for a in artwork if isinstance(a, dict) and a.get("kind") == "poster"]
    posters.sort(key=lambda a: not a.get("chosen", False))
    for art in posters:
        if isinstance(art.get("url"), str):
            return str(art["url"])
    return None
