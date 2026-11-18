"""The Leyla server's own updates (administrator sign-ins only).

Leyla updates itself: a new release is fetched, rehearsed on this server's
own library and installed when nobody is watching. This entity shows where
that stands and lets Home Assistant say "update now", which is the same
button as Settings › Updates.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LeylaConfigEntry
from .api import LeylaError
from .const import DOMAIN
from .hub import LeylaCoordinator

PARALLEL_UPDATES = 1

# The states in which the server is already moving to the newer release.
BUSY = frozenset({"fetching", "rehearsing", "installing"})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LeylaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Only an administrator may see or start the server's updates."""
    coordinator = entry.runtime_data.updates
    if coordinator is not None:
        async_add_entities([LeylaServerUpdate(coordinator, entry)])


class LeylaServerUpdate(CoordinatorEntity[LeylaCoordinator[dict[str, Any] | None]], UpdateEntity):
    """The server's version, and the newer one when there is one."""

    _attr_has_entity_name = True
    # The server's own device names it: "update.living_room".
    _attr_name = None
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    _attr_title = "Leyla"

    def __init__(
        self, coordinator: LeylaCoordinator[dict[str, Any] | None], entry: LeylaConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_server_update"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def _status(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        """Known once the server has answered."""
        return super().available and bool(self._status.get("running"))

    @property
    def installed_version(self) -> str | None:
        """The release running now."""
        return self._status.get("running")

    @property
    def latest_version(self) -> str | None:
        """The newer release, or the running one when there is none."""
        newer = self._status.get("newer")
        if isinstance(newer, dict) and newer.get("version"):
            return str(newer["version"])
        return self.installed_version

    @property
    def release_url(self) -> str | None:
        """What changed, on leyla.media."""
        version = self.latest_version
        return f"https://leyla.media/changelog/{version}" if version else None

    @property
    def release_summary(self) -> str | None:
        """Held, waiting for a plan or failed: the server's reason, briefly."""
        status = self._status
        if status.get("needs_plan"):
            return "This server needs a plan to take the newer release."
        held = status.get("held")
        if status.get("state") == "held" and isinstance(held, dict):
            return str(held.get("detail") or held.get("code") or "") or None
        failure = status.get("failure")
        if isinstance(failure, dict):
            return str(failure.get("detail") or failure.get("code") or "") or None
        return None

    @property
    def in_progress(self) -> bool:
        """Fetching, rehearsing on this library, or installing."""
        return self._status.get("state") in BUSY

    @property
    def update_percentage(self) -> int | None:
        """How much of the release is fetched, while it is."""
        progress = self._status.get("progress")
        if self._status.get("state") == "fetching" and isinstance(progress, dict):
            total = progress.get("total_bytes") or 0
            if total > 0:
                return min(100, int(100 * (progress.get("fetched_bytes") or 0) / total))
        return None

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Update now: Leyla fetches, rehearses and installs as it would on its own."""
        try:
            await self.entry.runtime_data.api.install_update()
        except LeylaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()
