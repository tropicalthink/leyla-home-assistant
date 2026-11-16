"""Active streams and conversions on the server (admin sign-ins only)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LeylaConfigEntry
from .const import CONVERTING_VIDEO_ACTION, DOMAIN, MAX_SESSIONS_IN_ATTRIBUTES
from .hub import NowPlayingCoordinator

PARALLEL_UPDATES = 0


def is_converting(session: dict[str, Any]) -> bool:
    """The server re-encodes this stream's picture."""
    return session.get("video_action") == CONVERTING_VIDEO_ACTION


@dataclass(frozen=True, kw_only=True)
class LeylaSensorDescription(SensorEntityDescription):
    """Which sessions a sensor counts."""

    include: Callable[[dict[str, Any]], bool]


SENSORS: tuple[LeylaSensorDescription, ...] = (
    LeylaSensorDescription(
        key="active_streams",
        translation_key="active_streams",
        state_class=SensorStateClass.MEASUREMENT,
        include=lambda _session: True,
    ),
    LeylaSensorDescription(
        key="active_conversions",
        translation_key="active_conversions",
        state_class=SensorStateClass.MEASUREMENT,
        include=is_converting,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LeylaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Only an administrator's sign-in may read the server's sessions."""
    coordinator = entry.runtime_data.now_playing
    if coordinator is None:
        return
    async_add_entities(
        LeylaStreamsSensor(coordinator, entry, description) for description in SENSORS
    )


def session_summary(session: dict[str, Any]) -> dict[str, Any]:
    """The few fields of a session worth an attribute."""
    return {
        "account": session.get("account_name"),
        "device": session.get("device_name"),
        "title": session.get("title"),
        "delivery": session.get("delivery_mode"),
        "height": session.get("output_height"),
    }


class LeylaStreamsSensor(CoordinatorEntity[NowPlayingCoordinator], SensorEntity):
    """A count of live sessions, with a short list of them."""

    _attr_has_entity_name = True
    # The list changes with every stream; history keeps only the count.
    _unrecorded_attributes = frozenset({"sessions", "more"})
    entity_description: LeylaSensorDescription

    def __init__(
        self,
        coordinator: NowPlayingCoordinator,
        entry: LeylaConfigEntry,
        description: LeylaSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    def _sessions(self) -> list[dict[str, Any]]:
        return [
            s
            for s in self.coordinator.data or []
            if isinstance(s, dict) and self.entity_description.include(s)
        ]

    @property
    def native_value(self) -> int:
        """How many."""
        return len(self._sessions())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The newest few, small enough for the recorder."""
        sessions = self._sessions()
        return {
            "sessions": [session_summary(s) for s in sessions[:MAX_SESSIONS_IN_ATTRIBUTES]],
            "more": max(0, len(sessions) - MAX_SESSIONS_IN_ATTRIBUTES),
        }
