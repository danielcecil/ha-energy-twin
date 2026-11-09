"""Shared entity base."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TwinCoordinator


class TwinEntity(CoordinatorEntity[TwinCoordinator]):
    _attr_has_entity_name = True
    # big list attributes feed the dashboard charts; keep them out of the recorder database
    _unrecorded_attributes = frozenset({"series", "monthly", "rows", "cumulative_by_year", "events", "diagnostics"})

    def __init__(self, coordinator: TwinCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.entry.entry_id)},
                                            name=coordinator.entry.title, manufacturer="Community", model="Digital twin")
