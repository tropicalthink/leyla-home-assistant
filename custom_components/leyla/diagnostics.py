"""Diagnostics: the link's state, with the token and names redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant

from . import LeylaConfigEntry

TO_REDACT = {CONF_ACCESS_TOKEN, CONF_URL, "access_token", "poll_token", "username", "account_name"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LeylaConfigEntry
) -> dict[str, Any]:
    """What a bug report needs: never the token."""
    hub = entry.runtime_data
    sessions = hub.now_playing.data if hub.now_playing is not None else None
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "account": async_redact_data(dict(hub.account), TO_REDACT),
        "live": {
            "connected": hub.connected,
            "connects": hub.connect_count,
            "last_close": hub.last_close,
        },
        "devices": [
            {
                "listed": device.id in hub.listed,
                "status": (device.state or {}).get("status"),
                "has_volume": isinstance((device.state or {}).get("volume"), dict),
                "has_next": bool((device.state or {}).get("has_next")),
                "reported_at": device.reported_at.isoformat(),
            }
            for device in hub.devices.values()
        ],
        "libraries": len(hub.libraries),
        "streams": None
        if sessions is None
        else [
            {
                "delivery_mode": s.get("delivery_mode"),
                "delivery_reason": s.get("delivery_reason"),
                "output_height": s.get("output_height"),
            }
            for s in sessions
            if isinstance(s, dict)
        ],
    }
