"""Number entities: the what-if sliders the dashboard drives."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import *  # noqa: F403
from .entity import TwinEntity


@dataclass(frozen=True, kw_only=True)
class TwinNumberDescription(NumberEntityDescription):
    option: str
    annual: bool = True            # does changing it need the 365-day what-if re-run?


def _d(key, option, lo, hi, step, unit=None, mode=NumberMode.BOX, annual=True):
    return TwinNumberDescription(key=key, translation_key=key, option=option, native_min_value=lo,
                                 native_max_value=hi, native_step=step, native_unit_of_measurement=unit, mode=mode,
                                 annual=annual)


NUMBERS = (
    _d("pv_kwp", CONF_PV_KWP, 0, 30, 0.1, "kWp", NumberMode.SLIDER),
    _d("battery_kwh", CONF_BATT_KWH, 0, 60, 0.5, "kWh", NumberMode.SLIDER),
    _d("inverter_kw", CONF_INVERTER_KW, 1, 25, 0.1, "kW", NumberMode.SLIDER),
    _d("battery_max_kw", CONF_BATT_KW, 0, 25, 0.1, "kW"),
    _d("reserve_pct", CONF_RESERVE, 0, 50, 1, "%"),
    _d("export_limit_kw", CONF_EXPORT_LIMIT, 0, 25, 0.01, "kW"),
    _d("pv_annual_yield", CONF_PV_YIELD, 400, 1400, 10, "kWh/kWp"),
    _d("export_p", CONF_EXPORT_P, 0, 60, 0.1, "p/kWh"),
    _d("cheap_p", CONF_CHEAP_P, 0, 40, 0.1, "p/kWh"),
    _d("standard_p", CONF_STANDARD_P, 0, 80, 0.1, "p/kWh"),
    _d("pv_cost_per_kwp", CONF_PV_COST_KWP, 0, 3000, 10, "£/kWp"),
    _d("battery_cost_per_kwh", CONF_BATT_COST_KWH, 0, 2000, 10, "£/kWh"),
    _d("power_down_per_month", CONF_PD_PER_MONTH, 0, 30, 1),
    _d("power_up_per_month", CONF_PU_PER_MONTH, 0, 30, 1),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(TwinNumber(entry.runtime_data, d) for d in NUMBERS)


class TwinNumber(TwinEntity, NumberEntity):
    entity_description: TwinNumberDescription

    def __init__(self, coordinator, description) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.coordinator.cfg.get(self.entity_description.option)

    async def async_set_native_value(self, value: float) -> None:
        d = self.entity_description
        await self.coordinator.async_set_options({d.option: value}, annual=d.annual)
