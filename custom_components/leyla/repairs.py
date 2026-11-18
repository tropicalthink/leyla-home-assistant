"""What the server says needs attention, as Home Assistant repairs.

Each finding of severity "action" or "attention" that nobody has set aside
becomes a repair, in the server's own words. Its fix is Leyla's "Set
aside": the server records who set it aside, and the repair goes. What to
actually do about it stays in Leyla, where the finding links to the fix.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .api import LeylaError
from .const import DOMAIN, REPAIR_SEVERITIES


def issue_id(entry: ConfigEntry, finding_id: str) -> str:
    """One repair per entry and finding."""
    return f"{entry.entry_id}:{finding_id}"


@callback
def async_sync_findings(
    hass: HomeAssistant, entry: ConfigEntry, findings: list[dict[str, Any]]
) -> None:
    """Open a repair for each finding that wants one and close the rest."""
    wanted: set[str] = set()
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or finding.get("severity") not in REPAIR_SEVERITIES
            or finding.get("acknowledged_at")
            or not isinstance(finding.get("id"), str)
        ):
            continue
        key = issue_id(entry, finding["id"])
        wanted.add(key)
        ir.async_create_issue(
            hass,
            DOMAIN,
            key,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR
            if finding.get("severity") == "action"
            else ir.IssueSeverity.WARNING,
            translation_key="finding",
            translation_placeholders={
                "title": str(finding.get("title") or finding.get("kind") or ""),
                "explanation": str(finding.get("explanation") or ""),
                "impact": str(finding.get("impact") or ""),
                "server": entry.title,
                "url": str(entry.data.get(CONF_URL, "")),
            },
            data={
                "entry_id": entry.entry_id,
                "finding_id": finding["id"],
                "title": str(finding.get("title") or ""),
            },
        )
    registry = ir.async_get(hass)
    prefix = f"{entry.entry_id}:"
    for (domain, key), _issue in list(registry.issues.items()):
        if domain == DOMAIN and key.startswith(prefix) and key not in wanted:
            ir.async_delete_issue(hass, DOMAIN, key)


class SetAsideFlow(ConfirmRepairFlow):
    """Set the finding aside in Leyla."""

    def __init__(self, data: dict[str, str | int | float | None]) -> None:
        super().__init__()
        self._data = data

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask, then tell the server."""
        if user_input is None:
            return await super().async_step_confirm(user_input)
        entry = self.hass.config_entries.async_get_entry(str(self._data.get("entry_id")))
        if entry is None or not hasattr(entry, "runtime_data"):
            return self.async_abort(reason="not_loaded")
        hub = entry.runtime_data
        try:
            await hub.api.acknowledge_finding(
                str(self._data["finding_id"]), str(self._data.get("title") or "") or None
            )
        except LeylaError:
            return self.async_abort(reason="cannot_connect")
        if hub.findings is not None:
            await hub.findings.async_request_refresh()
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """The one fix Home Assistant can offer: set it aside."""
    return SetAsideFlow(data or {})
