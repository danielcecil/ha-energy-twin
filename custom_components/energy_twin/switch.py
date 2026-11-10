"""Switches for the two strategy toggles."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ARBITRAGE, CONF_BATT_EXPORT, CONF_GAS_DISCONNECT, CONF_HP_ENABLED
from .entity import TwinEntity

SWITCHES = (("arbitrage", CONF_ARBITRAGE), ("allow_battery_export", CONF_BATT_EXPORT),
            ("hp_enabled", CONF_HP_ENABLED), ("gas_disconnect", CONF_GAS_DISCONNECT))


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(TwinSwitch(entry.runtime_data, k, o) for k, o in SWITCHES)


class TwinSwitch(TwinEntity, SwitchEntity):
    def __init__(self, c, key: str, option: str) -> None:
        super().__init__(c, key)
        self._attr_translation_key = key
        self._option = option

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.cfg[self._option])

    async def async_turn_on(self, **kw) -> None:
        await self.coordinator.async_set_options({self._option: True})

    async def async_turn_off(self, **kw) -> None:
        await self.coordinator.async_set_options({self._option: False})
