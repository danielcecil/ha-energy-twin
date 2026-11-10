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
    _d("system_cost", CONF_SYSTEM_COST, 0, 100000, 100, "£"),
    _d("grant", CONF_GRANT, 0, 30000, 100, "£"),
    _d("finance_deposit", CONF_DEPOSIT, 0, 100000, 100, "£"),
    _d("finance_apr", CONF_APR, 0, 30, 0.1, "%"),
    _d("finance_term_months", CONF_TERM, 0, 360, 1, "months"),
    _d("finance_monthly", CONF_FIN_MONTHLY, 0, 2000, 1, "£/month"),
    _d("tank_litres", CONF_TANK_L, 100, 500, 10, "L"),
    _d("main_fuse_a", CONF_FUSE_A, 20, 200, 5, "A"),
    _d("gas_unit_p", CONF_GAS_P, 0, 30, 0.1, "p/kWh"),
    _d("hp_capacity_kw", CONF_HP_CAP_KW, 2, 20, 0.5, "kW"),
    _d("power_down_per_month", CONF_PD_PER_MONTH, 0, 30, 1),
    _d("power_up_per_month", CONF_PU_PER_MONTH, 0, 30, 1),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([*(TwinNumber(entry.runtime_data, d) for d in NUMBERS), EventRateNumber(entry.runtime_data)])


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


class EventRateNumber(TwinEntity, NumberEntity):
    """Optional event rate (p/kWh): import rate for Power Up, export rate for Power Down. 0 = use defaults."""

    _attr_translation_key = "event_rate_p"
    _attr_native_min_value, _attr_native_max_value, _attr_native_step = 0, 500, 0.5
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_mode = NumberMode.BOX

    def __init__(self, c) -> None:
        super().__init__(c, "event_rate_p")
        self._attr_entity_registry_enabled_default = c.is_primary

    @property
    def native_value(self):
        return self.coordinator.form["rate"]

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.form["rate"] = value
        self.async_write_ha_state()
