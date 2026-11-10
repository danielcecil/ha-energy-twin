"""Sensors: live twin state, savings, plan, annual what-if, payback, sweep results."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import TwinEntity

GBP = "GBP"
PENCE = "p/kWh"


def _g(*path, default=None):
    def fn(d: dict):
        cur: Any = d
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur
    return fn


def _gbp(p: float | None) -> float | None:
    return None if p is None else round(p / 100.0, 2)


def _saving(bucket: str):
    def fn(d: dict):
        b = d.get("acc", {}).get(bucket)
        # electricity saved by the kit, plus gas you would no longer burn (if the heat pump is on)
        return _gbp(b["actual_p"] - b["twin_p"] + b.get("gas_p", 0.0)) if b else None
    return fn


def _cost(bucket: str, which: str):
    def fn(d: dict):
        b = d.get("acc", {}).get(bucket)
        return _gbp(b[which]) if b else None
    return fn


def _best(d: dict):
    rows = d.get("sweep") or []
    if not rows:
        return None
    r = rows[0]
    return f"{r['pv_kwp']:g} kWp + {r['battery_kwh']:g} kWh"


@dataclass(frozen=True, kw_only=True)
class TwinSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], Any]
    attr_fn: Callable[[dict], dict] | None = None


def _plan_series(d: dict) -> dict:
    p = d.get("plan") or {}
    return {"series": p.get("series"), "built": p.get("built"), "plan_cost_gbp": p.get("plan_cost_gbp"),
            "plan_baseline_gbp": p.get("plan_baseline_gbp")}


def _annual_attrs(d: dict) -> dict:
    a = d.get("annual") or {}
    fin = a.get("finance") or {}
    cum = fin.get("cumulative_by_year") or []
    sc = d.get("scenario") or {}
    led = {k: _saving(k)(d) for k in ("day", "month", "total")}
    hp = a.get("heat") or {}
    cmp = {"hp_saving": a.get("hp_saving"), "gas_cost_avoided": hp.get("gas_cost_avoided"),
           "gas_standing_avoided": hp.get("gas_standing_avoided"), "hp_grid_cost": hp.get("grid_cost_nokit"),
           "hp_electricity_kwh": hp.get("hp_electricity_kwh"), "gas_kwh_displaced": hp.get("gas_kwh_displaced"),
           "scop_space": hp.get("scop_space"), "scop_dhw": hp.get("scop_dhw"), "cheap_share_pct": hp.get("cheap_share_pct"),
           "dhw_electricity_kwh": hp.get("dhw_electricity_kwh"), "space_electricity_kwh": hp.get("space_electricity_kwh"),
           "backup_electricity_kwh": hp.get("backup_electricity_kwh"), "induction_kwh": hp.get("induction_kwh"),
           "gas_model": hp.get("gas_model"), "peak_hp_kw": hp.get("peak_hp_kw"), "peak_import_kw_year": a.get("peak_import_kw"),
           "bill_today": a.get("bill_today"), "bill_with": a.get("bill_with"), "elec_today": a.get("elec_today"),
           "elec_with": a.get("elec_with"), "gas_today": a.get("gas_today"), "gas_with": a.get("gas_with"),
           "gas_is_estimate": a.get("gas_is_estimate"), "cumulative_by_year": fin.get("cumulative_by_year"),
           "simple_payback_years": fin.get("simple_payback_years"), "finance_mode": fin.get("finance_mode"),
           "effective_apr_pct": fin.get("effective_apr_pct"), "cost_of_finance": fin.get("cost_of_finance"),
           "total_paid_finance": fin.get("total_paid_finance"), "upfront": fin.get("upfront"),
           "scenario": sc.get("name"), "battery_kwh": sc.get("battery_kwh"), "pv_kwp": sc.get("pv_kwp"),
           "inverter_kw": sc.get("inverter_kw"), "capex": fin.get("capex"), "payback_years": fin.get("payback_years"),
           "monthly_net_cashflow": fin.get("monthly_net_cashflow"), "npv": fin.get("npv"),
           "net_5y": cum[4] if len(cum) > 4 else None, "net_10y": cum[9] if len(cum) > 9 else None,
           "net_20y": cum[19] if len(cum) > 19 else None,
           "ledger_today": led["day"], "ledger_month": led["month"], "ledger_total": led["total"]}
    return {**cmp, **{k: a.get(k) for k in ("monthly", "pv_saving", "battery_saving", "baseline_year1", "scenario_year1",
                                  "pv_generation_kwh", "import_kwh", "export_kwh", "grid_charge_kwh",
                                  "battery_export_kwh", "battery_cycles", "strategy", "days_simulated",
                                  "history_days", "profile")}}


SENSORS: tuple[TwinSensorDescription, ...] = (
    # ---- live twin
    TwinSensorDescription(key="soc", translation_key="soc", native_unit_of_measurement=PERCENTAGE,
                          device_class=SensorDeviceClass.BATTERY, state_class=SensorStateClass.MEASUREMENT,
                          value_fn=_g("soc_pct")),
    TwinSensorDescription(key="stored_energy", translation_key="stored_energy",
                          native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                          device_class=SensorDeviceClass.ENERGY_STORAGE, state_class=SensorStateClass.MEASUREMENT,
                          value_fn=_g("soc_kwh")),
    TwinSensorDescription(key="battery_power", translation_key="battery_power",
                          native_unit_of_measurement=UnitOfPower.KILO_WATT, device_class=SensorDeviceClass.POWER,
                          state_class=SensorStateClass.MEASUREMENT,
                          value_fn=lambda d: _r(d.get("live", {}).get("battery"))),
    TwinSensorDescription(key="grid_power", translation_key="grid_power",
                          native_unit_of_measurement=UnitOfPower.KILO_WATT, device_class=SensorDeviceClass.POWER,
                          state_class=SensorStateClass.MEASUREMENT,
                          value_fn=lambda d: _r(d.get("live", {}).get("grid"))),
    TwinSensorDescription(key="solar_power", translation_key="solar_power",
                          native_unit_of_measurement=UnitOfPower.KILO_WATT, device_class=SensorDeviceClass.POWER,
                          state_class=SensorStateClass.MEASUREMENT,
                          value_fn=lambda d: _r(d.get("live", {}).get("pv"))),
    TwinSensorDescription(key="house_load", translation_key="house_load",
                          native_unit_of_measurement=UnitOfPower.KILO_WATT, device_class=SensorDeviceClass.POWER,
                          state_class=SensorStateClass.MEASUREMENT,
                          value_fn=lambda d: _r(d.get("live", {}).get("house"))),
    TwinSensorDescription(key="mode", translation_key="mode", value_fn=_g("live", "mode")),
    TwinSensorDescription(key="import_price", translation_key="import_price", native_unit_of_measurement=PENCE,
                          state_class=SensorStateClass.MEASUREMENT, value_fn=lambda d: _r(d.get("live", {}).get("import_p"))),
    TwinSensorDescription(key="export_price", translation_key="export_price", native_unit_of_measurement=PENCE,
                          state_class=SensorStateClass.MEASUREMENT, value_fn=lambda d: _r(d.get("live", {}).get("export_p"))),
    # ---- savings ledger
    TwinSensorDescription(key="saving_today", translation_key="saving_today", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_saving("day")),
    TwinSensorDescription(key="saving_month", translation_key="saving_month", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_saving("month")),
    TwinSensorDescription(key="saving_total", translation_key="saving_total", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_saving("total")),
    TwinSensorDescription(key="cost_today_actual", translation_key="cost_today_actual", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_cost("day", "actual_p")),
    TwinSensorDescription(key="cost_today_twin", translation_key="cost_today_twin", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_cost("day", "twin_p")),
    TwinSensorDescription(key="solar_today", translation_key="solar_today",
                          native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY,
                          value_fn=lambda d: _r(_g("acc", "day", "pv_kwh")(d))),
    TwinSensorDescription(key="export_today", translation_key="export_today",
                          native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY,
                          value_fn=lambda d: _r(_g("acc", "day", "export_kwh")(d))),
    TwinSensorDescription(key="ev_tonight", translation_key="ev_tonight",
                          native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY,
                          value_fn=_g("ev", "tonight_kwh"),
                          attr_fn=lambda d: {"vehicles": (d.get("ev") or {}).get("vehicles", [])}),
    # ---- heat pump / supply
    TwinSensorDescription(key="hp_power", translation_key="hp_power", native_unit_of_measurement=UnitOfPower.KILO_WATT,
                          device_class=SensorDeviceClass.POWER, state_class=SensorStateClass.MEASUREMENT,
                          value_fn=lambda d: _r(d.get("live", {}).get("hp_kw")),
                          attr_fn=lambda d: {"induction_kw": _r(d.get("live", {}).get("induction_kw")),
                                             "cop_space": _r(d.get("live", {}).get("cop_space")),
                                             "cop_dhw": _r(d.get("live", {}).get("cop_dhw")),
                                             "tank_charging": d.get("live", {}).get("tank_charging")}),
    TwinSensorDescription(key="hp_tank_temp", translation_key="hp_tank_temp", native_unit_of_measurement="°C",
                          device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT,
                          value_fn=lambda d: (d.get("hp") or {}).get("tank_temp_c"),
                          attr_fn=lambda d: {"tank_kwh": (d.get("hp") or {}).get("tank_kwh"),
                                             "tank_pct": (d.get("hp") or {}).get("tank_pct")}),
    TwinSensorDescription(key="peak_import", translation_key="peak_import", native_unit_of_measurement=UnitOfPower.KILO_WATT,
                          device_class=SensorDeviceClass.POWER, value_fn=_g("plan", "peak_import_kw"),
                          attr_fn=lambda d: {"fuse_limit_kw": (d.get("supply") or {}).get("limit_kw")}),
    TwinSensorDescription(key="fuse_usage", translation_key="fuse_usage", native_unit_of_measurement=PERCENTAGE,
                          value_fn=_g("plan", "fuse_pct")),
    TwinSensorDescription(key="annual_hp_saving", translation_key="annual_hp_saving", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_g("annual", "hp_saving"),
                          attr_fn=lambda d: dict((d.get("annual") or {}).get("heat") or {})),
    # ---- plan
    TwinSensorDescription(key="plan_target_soc", translation_key="plan_target_soc", native_unit_of_measurement=PERCENTAGE,
                          value_fn=_g("plan", "target_soc_pct"), attr_fn=_plan_series),
    TwinSensorDescription(key="plan_battery_export", translation_key="plan_battery_export",
                          native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY,
                          value_fn=_g("plan", "planned_export_kwh")),
    TwinSensorDescription(key="plan_saving", translation_key="plan_saving", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_g("plan", "plan_saving_gbp")),
    # ---- annual what-if
    TwinSensorDescription(key="annual_saving", translation_key="annual_saving", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_g("annual", "saving_year1"),
                          attr_fn=_annual_attrs),
    TwinSensorDescription(key="annual_pv_saving", translation_key="annual_pv_saving", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_g("annual", "pv_saving")),
    TwinSensorDescription(key="annual_battery_saving", translation_key="annual_battery_saving",
                          native_unit_of_measurement=GBP, device_class=SensorDeviceClass.MONETARY,
                          value_fn=_g("annual", "battery_saving")),
    TwinSensorDescription(key="payback_years", translation_key="payback_years", native_unit_of_measurement="years",
                          value_fn=_g("annual", "finance", "payback_years"),
                          attr_fn=lambda d: {"cumulative_by_year": _g("annual", "finance", "cumulative_by_year")(d),
                                             "simple_payback_years": _g("annual", "finance", "simple_payback_years")(d)}),
    TwinSensorDescription(key="capex", translation_key="capex", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_g("annual", "finance", "capex")),
    TwinSensorDescription(key="monthly_net_cashflow", translation_key="monthly_net_cashflow",
                          native_unit_of_measurement=GBP, device_class=SensorDeviceClass.MONETARY,
                          value_fn=_g("annual", "finance", "monthly_net_cashflow")),
    TwinSensorDescription(key="monthly_payment", translation_key="monthly_payment", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_g("annual", "finance", "monthly_payment")),
    TwinSensorDescription(key="net_gain", translation_key="net_gain", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_g("annual", "finance", "net_gain_horizon")),
    TwinSensorDescription(key="npv", translation_key="npv", native_unit_of_measurement=GBP,
                          device_class=SensorDeviceClass.MONETARY, value_fn=_g("annual", "finance", "npv")),
    # ---- sweep / events / diagnostics
    TwinSensorDescription(key="sweep_best", translation_key="sweep_best", value_fn=_best,
                          attr_fn=lambda d: {"rows": (d.get("sweep") or [])[:25], **(d.get("sweep_info") or {})}),
    TwinSensorDescription(key="next_event", translation_key="next_event",
                          value_fn=lambda d: (f"{d['events'][0]['kind']} {d['events'][0]['start']}" if d.get("events") else "none"),
                          attr_fn=lambda d: {"events": d.get("events", [])}),
    TwinSensorDescription(key="diagnostics", translation_key="diagnostics", entity_category=EntityCategory.DIAGNOSTIC,
                          value_fn=lambda d: "ok" if d.get("plan") else "no plan",
                          attr_fn=lambda d: {"diagnostics": d.get("diag", {})}),
)


def _r(v):
    return None if v is None else round(v, 3)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(TwinSensor(entry.runtime_data, d) for d in SENSORS)


class TwinSensor(TwinEntity, SensorEntity):
    entity_description: TwinSensorDescription

    def __init__(self, coordinator, description: TwinSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self):
        fn = self.entity_description.attr_fn
        return fn(self.coordinator.data or {}) if fn else None
