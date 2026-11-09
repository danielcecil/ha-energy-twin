"""Date-time entities: start / end of the manually added Power Up / Power Down event."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import TwinEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([EventTime(c, "event_start", "start"), EventTime(c, "event_end", "end")])


class EventTime(TwinEntity, DateTimeEntity):
    def __init__(self, c, key: str, field: str) -> None:
        super().__init__(c, key)
        self._attr_translation_key = key
        self._field = field
        self._attr_entity_registry_enabled_default = c.is_primary

    @property
    def native_value(self) -> datetime:
        return self.coordinator.form[self._field]

    async def async_set_value(self, value: datetime) -> None:
        self.coordinator.form[self._field] = value
        self.async_write_ha_state()
