"""Config + options flow. One declarative table drives every settings page."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .const import *  # noqa: F403
from . import ha_data
from .menus import SECTIONS, TOP, VEHICLE_MENU, options as menu_options
from .sim.presets import HARDWARE_PRESETS, TARIFF_PRESETS, apply_preset, preset_choices


def _ent(multiple: bool = False):
    return sel.EntitySelector(sel.EntitySelectorConfig(multiple=multiple))


def _num(lo, hi, step, unit=None, mode="box"):
    cfg = {"min": lo, "max": hi, "step": step, "mode": sel.NumberSelectorMode(mode)}
    if unit:
        cfg["unit_of_measurement"] = unit
    return sel.NumberSelector(sel.NumberSelectorConfig(**cfg))


def _choice(options: dict[str, str]):
    return sel.SelectSelector(sel.SelectSelectorConfig(
        options=[sel.SelectOptionDict(value=k, label=v) for k, v in options.items()],
        mode=sel.SelectSelectorMode.DROPDOWN))


def _sensor(multiple: bool = False, device_class: str | None = None):
    cfg: dict = {"domain": "sensor", "multiple": multiple}
    if device_class:
        cfg["device_class"] = device_class
    return sel.EntitySelector(sel.EntitySelectorConfig(**cfg))


_TIME, _BOOL, _TEXT = sel.TimeSelector(), sel.BooleanSelector(), sel.TextSelector()

FINANCE_MODES = {"cash": "Pay in cash", "loan": "Loan - interest rate over a term",
                 "fixed_monthly": "Fixed monthly payment (e.g. a quoted £149/month)"}
# step -> ordered [(key, selector, required)]
STEPS: dict[str, list[tuple[str, Any, bool]]] = {
    "sources_live": [
        (CONF_POWER_ENTITY, _ent(), False),
        (CONF_EV_POWER_ENTITIES, _ent(True), False),
        (CONF_PV_NOW_ENTITY, _ent(), False),
        (CONF_IMPORT_NOW_ENTITY, _ent(), False),
        (CONF_EXPORT_NOW_ENTITY, _ent(), False),
    ],
    "sources_octopus": [
        (CONF_IMPORT_RATES_ENTITIES, _ent(True), False),
        (CONF_EXPORT_RATES_ENTITIES, _ent(True), False),
        (CONF_DISPATCH_ENTITY, _ent(), False),
        (CONF_POWER_UP_ENTITY, _ent(), False),
        (CONF_POWER_DOWN_ENTITY, _ent(), False),
    ],
    "sources_solar_history": [
        (CONF_FORECAST_DEVICE, sel.DeviceSelector(sel.DeviceSelectorConfig(integration="forecast_solar")), False),
        (CONF_FORECAST_REF_KWP, _num(0.1, 100, 0.1, "kWp"), False),
        (CONF_FC_SCALE, _num(40, 150, 1, "%"), True),
        (CONF_CONSUMPTION_STAT, _sensor(device_class="energy"), False),
        (CONF_EV_STATS, _sensor(True, "energy"), False),
        (CONF_HISTORY_DAYS, _num(14, 730, 1, "days"), False),
    ],
    "sources_heat": [
        (CONF_GAS_STAT, _sensor(device_class="energy"), False),
        (CONF_GAS_RATE_ENTITY, _sensor(), False),
        (CONF_GAS_STANDING_ENTITY, _sensor(), False),
        (CONF_OUTDOOR_ENTITY, _ent(), False),
        (CONF_WEATHER_ENTITY, _ent(), False),
        (CONF_TANK_TEMP_ENTITY, _ent(), False),
    ],
    "supply": [
        (CONF_FUSE_A, _num(10, 400, 1, "A"), True),
        (CONF_PHASES, _choice({"1": "Single phase", "3": "Three phase"}), True),
        (CONF_VOLTS, _num(100, 480, 1, "V"), True),
        (CONF_FUSE_PCT, _num(30, 100, 1, "%"), True),
    ],
    "heat": [
        (CONF_HP_ENABLED, _BOOL, True),
        (CONF_TANK_L, _num(50, 1000, 10, "L"), True), (CONF_TANK_C, _num(40, 70, 1, "°C"), True),
        (CONF_TANK_LOSS, _num(0, 10, 0.1, "kWh/day"), True), (CONF_LEGIONELLA_C, _num(40, 75, 1, "°C"), True),
        (CONF_DHW_START, _TIME, True),
        (CONF_HP_ELEC_KW, _num(0.5, 15, 0.1, "kW"), True), (CONF_HP_CAP_KW, _num(2, 30, 0.5, "kW"), True),
        (CONF_FLOW_C, _num(25, 70, 1, "°C"), True), (CONF_CARNOT, _num(0.25, 0.65, 0.01), True),
        (CONF_BASE_TEMP, _num(10, 20, 0.5, "°C"), True),
    ],
    "heat_costs": [
        (CONF_BOILER_EFF_H, _num(0.5, 1.0, 0.01), True), (CONF_BOILER_EFF_D, _num(0.4, 1.0, 0.01), True),
        (CONF_COOK_GAS, _num(0, 10, 0.1, "kWh/day"), True), (CONF_INDUCTION, _num(0.2, 1.0, 0.01), True),
        (CONF_FALLBACK_GAS, _num(1000, 40000, 100, "kWh/yr"), True),
        (CONF_GAS_P, _num(0, 30, 0.01, "p/kWh"), True), (CONF_GAS_STANDING, _num(0, 100, 0.1, "p/day"), True),
        (CONF_GAS_DISCONNECT, _BOOL, True),
    ],
    "tariff": [
        (CONF_CHEAP_START, _TIME, True), (CONF_CHEAP_END, _TIME, True),
        (CONF_CHEAP_P, _num(-50, 100, 0.01, "p/kWh"), True),
        (CONF_STANDARD_P, _num(0, 150, 0.01, "p/kWh"), True),
        (CONF_EXPORT_P, _num(0, 100, 0.01, "p/kWh"), True),
        (CONF_PU_IMPORT_P, _num(-50, 100, 0.01, "p/kWh"), True),
        (CONF_PD_EXPORT_P, _num(0, 500, 0.01, "p/kWh"), True),
        (CONF_PD_BONUS_P, _num(0, 500, 0.01, "p/kWh"), True),
    ],
    "hardware": [
        (CONF_PV_KWP, _num(0, 50, 0.1, "kWp"), True),
        (CONF_INVERTER_KW, _num(0.5, 50, 0.1, "kW"), True),
        (CONF_PV_YIELD, _num(400, 1400, 10, "kWh/kWp/yr"), True),
        (CONF_BATT_KWH, _num(0, 100, 0.1, "kWh"), True),
        (CONF_BATT_KW, _num(0, 50, 0.1, "kW"), True),
        (CONF_RTE, _num(0.5, 1.0, 0.01), True),
        (CONF_RESERVE, _num(0, 50, 1, "%"), True),
        (CONF_EXPORT_LIMIT, _num(0, 50, 0.01, "kW"), True),
        (CONF_CYCLE_COST, _num(0, 50, 0.1, "p/kWh"), True),
    ],
    "finance": [
        (CONF_SYSTEM_COST, _num(0, 200000, 100, "£"), True), (CONF_GRANT, _num(0, 50000, 100, "£"), True),
        (CONF_FIN_MODE, _choice(FINANCE_MODES), True),
        (CONF_DEPOSIT, _num(0, 200000, 100, "£"), True), (CONF_FIN_FEE, _num(0, 10000, 10, "£"), True),
        (CONF_APR, _num(0, 40, 0.1, "%"), True), (CONF_TERM, _num(0, 360, 1, "months"), True),
        (CONF_FIN_MONTHLY, _num(0, 5000, 1, "£/month"), True),
        (CONF_ESCALATION, _num(-5, 15, 0.1, "%/yr"), True), (CONF_PV_DEG, _num(0, 5, 0.1, "%/yr"), True),
        (CONF_BATT_DEG, _num(0, 10, 0.1, "%/yr"), True), (CONF_DISCOUNT, _num(0, 15, 0.1, "%"), True),
        (CONF_HORIZON, _num(5, 40, 1, "years"), True),
    ],
    "strategy": [
        (CONF_STRATEGY, _choice(STRATEGIES), True), (CONF_MARGIN, _num(0, 60, 1, "%"), True),
        (CONF_EXPORT_FROM, _TIME, True), (CONF_EXPORT_UNTIL, _TIME, True), (CONF_EVENT_EXPORT, _BOOL, True),
        (CONF_EXPORT_MARGIN, _num(0, 20, 0.1, "p/kWh"), True),
        (CONF_ARBITRAGE, _BOOL, True), (CONF_BATT_EXPORT, _BOOL, True),
    ],
    "assumptions": [
        (CONF_NIGHT_BASE, _num(0, 3, 0.05, "kW"), True),
        (CONF_DAILY_HOUSE, _num(1, 60, 0.5, "kWh"), True), (CONF_DAILY_EV, _num(0, 120, 0.5, "kWh"), True),
        (CONF_PD_PER_MONTH, _num(0, 30, 1), True), (CONF_PD_START_HOUR, _num(0, 23.5, 0.5, "h"), True),
        (CONF_PD_HOURS, _num(0.5, 12, 0.5, "h"), True),
        (CONF_PU_PER_MONTH, _num(0, 30, 1), True), (CONF_PU_START_HOUR, _num(0, 23.5, 0.5, "h"), True),
        (CONF_PU_HOURS, _num(0.5, 12, 0.5, "h"), True),
    ],
}
VEHICLE_MODES = {"iog": "Intelligent Octopus Go (smart-charged, dispatch slots)", "offpeak": "Off-peak schedule only"}
# key, selector, required, default
VEHICLE_FIELDS: list[tuple[str, Any, bool, Any]] = [
    ("device_id", sel.DeviceSelector(), False, None),
    ("name", _TEXT, False, None),
    ("mode", _choice(VEHICLE_MODES), True, "offpeak"),
    ("power_entity", _ent(), False, None),
    ("plug_entity", _ent(), False, None),
    ("home_entity", _ent(), False, None),
    ("soc_entity", _ent(), False, None),
    ("target_soc_entity", _ent(), False, None),
    ("capacity_kwh", _num(10, 200, 0.5, "kWh"), True, 60.0),
    ("target_soc_pct", _num(10, 100, 1, "%"), True, 80.0),
    ("charger_kw", _num(1.4, 22, 0.1, "kW"), True, 7.0),
    ("session_kwh", _num(0, 150, 0.5, "kWh"), True, 0.0),
    ("energy_stat", _sensor(device_class="energy"), False, None),
    ("plug_states", _TEXT, False, None),
    ("home_states", _TEXT, False, None),
]

def _schema(step: str, current: dict) -> vol.Schema:
    fields: dict = {}
    for key, selector, required in STEPS[step]:
        cur = current.get(key)
        desc = {"suggested_value": cur} if cur not in (None, "", []) else None
        marker = (vol.Required if required else vol.Optional)(key, **({"description": desc} if desc else {}),
                                                              **({"default": cur} if required and cur is not None else {}))
        fields[marker] = selector
    return vol.Schema(fields)


def _merge(step: str, opts: dict, user_input: dict) -> dict:
    """Apply a submitted form; optional fields left empty are removed."""
    out = dict(opts)
    for key, _s, required in STEPS[step]:
        if key in user_input:
            out[key] = user_input[key]
        elif not required:
            out.pop(key, None)
    return out


class _FlowBase:
    """Shared step handlers for both the initial setup and the options menu."""

    opts: dict

    def _form(self, step: str, errors: dict | None = None):
        return self.async_show_form(step_id=step, data_schema=_schema(step, {**DEFAULTS, **self.opts}),
                                    errors=errors or {})

    async def _step(self, step: str, user_input, nxt):
        if user_input is None:
            return self._form(step)
        self.opts = _merge(step, self.opts, user_input)
        return await nxt()

    async def _preset(self, step: str, table: dict, key: str, user_input, nxt):
        if user_input is None:
            schema = vol.Schema({vol.Required(key, default=self.opts.get(key, "custom")): _choice(preset_choices(table))})
            return self.async_show_form(step_id=step, data_schema=schema)
        name = user_input[key]
        self.opts[key] = name
        self.opts.update(apply_preset(table, name))
        return await nxt()


class EnergyTwinConfigFlow(_FlowBase, ConfigFlow, domain=DOMAIN):
    """Each config entry is one *scenario* (a set of kit + prices). Add several to compare setups."""

    VERSION = 1

    def __init__(self) -> None:
        self.opts: dict = {}
        self.title = "Home Energy Twin"
        self.copying = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnergyTwinOptionsFlow()

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        existing = self._async_current_entries()
        if user_input is None:
            default = f"Scenario {len(existing) + 1}" if existing else "Home Energy Twin"
            fields: dict = {vol.Required(CONF_NAME, default=default): _TEXT}
            if existing:
                fields[vol.Optional("copy_from")] = _choice({e.entry_id: e.title for e in existing})
            return self.async_show_form(step_id="user", data_schema=vol.Schema(fields))
        self.title = (user_input.get(CONF_NAME) or self.title).strip()
        if src := self.hass.config_entries.async_get_entry(user_input.get("copy_from") or ""):
            self.opts, self.copying = dict(src.options), True
        return await self.async_step_hardware_preset()

    async def async_step_hardware_preset(self, user_input=None) -> ConfigFlowResult:
        nxt = self.async_step_hardware if self.copying else self.async_step_tariff_preset
        return await self._preset("hardware_preset", HARDWARE_PRESETS, CONF_HW_PRESET, user_input, nxt)

    async def async_step_hardware(self, user_input=None) -> ConfigFlowResult:
        return await self._step("hardware", user_input, self._finish)

    async def async_step_tariff_preset(self, user_input=None) -> ConfigFlowResult:
        return await self._preset("tariff_preset", TARIFF_PRESETS, CONF_TARIFF_PRESET, user_input,
                                  self.async_step_sources_live)

    async def async_step_sources_live(self, user_input=None) -> ConfigFlowResult:
        return await self._step("sources_live", user_input, self.async_step_sources_octopus)

    async def async_step_sources_octopus(self, user_input=None) -> ConfigFlowResult:
        return await self._step("sources_octopus", user_input, self.async_step_sources_solar_history)

    async def async_step_sources_solar_history(self, user_input=None) -> ConfigFlowResult:
        return await self._step("sources_solar_history", user_input, self._finish)

    async def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title=self.title, data={}, options=self.opts)


class EnergyTwinOptionsFlow(_FlowBase, OptionsFlow):
    """Menu-driven: change any section, then choose 'done'."""

    def __init__(self) -> None:
        self.opts: dict = {}

    _section: str | None = None

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if not self.opts:
            self.opts = dict(self.config_entry.options)
        self._section = None
        return self.async_show_menu(step_id="init", menu_options=menu_options(TOP))

    async def _show_section(self, name: str) -> ConfigFlowResult:
        self._section = name
        return self.async_show_menu(step_id=name, menu_options=menu_options(SECTIONS[name]))

    async def _back(self) -> ConfigFlowResult:
        """After finishing a form, return to the section menu it came from."""
        return await self._show_section(self._section) if self._section else await self.async_step_init()

    async def async_step_hardware_preset(self, user_input=None) -> ConfigFlowResult:
        return await self._preset("hardware_preset", HARDWARE_PRESETS, CONF_HW_PRESET, user_input, self._back)

    async def async_step_tariff_preset(self, user_input=None) -> ConfigFlowResult:
        return await self._preset("tariff_preset", TARIFF_PRESETS, CONF_TARIFF_PRESET, user_input, self._back)

    # ---- vehicles -----------------------------------------------------------------------------
    def _vehicles(self) -> list[dict]:
        return list(self.opts.get(CONF_VEHICLES) or [])

    async def async_step_vehicles(self, user_input=None) -> ConfigFlowResult:
        vs = self._vehicles()
        listing = "\n".join(f"- {v['name']} ({VEHICLE_MODES.get(v.get('mode'), v.get('mode'))})" for v in vs) or "None yet."
        ids = [m for m in VEHICLE_MENU if vs or m not in ("vehicle_edit", "vehicle_remove")]
        return self.async_show_menu(step_id="vehicles", menu_options=menu_options(ids),
                                    description_placeholders={"vehicles": listing})

    async def async_step_vehicle_edit(self, user_input=None) -> ConfigFlowResult:
        names = [v["name"] for v in self._vehicles()]
        if user_input is None:
            return self.async_show_form(step_id="vehicle_edit", data_schema=vol.Schema(
                {vol.Required("name"): _choice({n: n for n in names})}))
        self._editing = user_input["name"]
        return await self.async_step_vehicle_add()

    def _vehicle_form(self, cur: dict, errors: dict | None = None) -> ConfigFlowResult:
        fields: dict = {}
        for key, selector, required, default in VEHICLE_FIELDS:
            val = cur.get(key, default)
            kw: dict = {}
            if required and val not in (None, ""):
                kw["default"] = val
            elif not required and val not in (None, ""):
                kw["description"] = {"suggested_value": val}
            fields[(vol.Required if required else vol.Optional)(key, **kw)] = selector
        return self.async_show_form(step_id="vehicle_add", data_schema=vol.Schema(fields), errors=errors or {})

    async def async_step_vehicle_add(self, user_input=None) -> ConfigFlowResult:
        editing = getattr(self, "_editing", None)
        cur = next((v for v in self._vehicles() if v["name"] == editing or v.get("device_id") == editing), {}) if editing else {}
        if user_input is None:
            return self._vehicle_form(cur)
        veh = {k: v for k, v in user_input.items() if v not in (None, "")}
        name = (veh.get("name") or "").strip() or (ha_data.device_name(self.hass, veh.get("device_id")) or "")
        if not name:
            return self._vehicle_form({**cur, **veh}, {"base": "name_required"})
        veh["name"] = name
        vs = [v for v in self._vehicles() if v.get("name") not in (editing, name)]
        self.opts[CONF_VEHICLES] = vs + [veh]
        self._editing = None
        return await self.async_step_vehicles()

    async def async_step_vehicle_remove(self, user_input=None) -> ConfigFlowResult:
        names = [v["name"] for v in self._vehicles()]
        if user_input is None:
            return self.async_show_form(step_id="vehicle_remove", data_schema=vol.Schema(
                {vol.Required("name"): _choice({n: n for n in names})}))
        self.opts[CONF_VEHICLES] = [v for v in self._vehicles() if v["name"] != user_input["name"]]
        return await self.async_step_vehicles()

    async def async_step_done(self, user_input=None) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=self.opts)


def _make_step(name: str):
    async def handler(self, user_input=None):
        nxt = self._back if isinstance(self, EnergyTwinOptionsFlow) else self._finish
        return await self._step(name, user_input, nxt)
    return handler


for _name in ("sources_live", "sources_octopus", "sources_solar_history", "sources_heat", "supply", "heat",
              "heat_costs", "hardware", "tariff", "finance", "strategy",
              "assumptions"):
    if not hasattr(EnergyTwinOptionsFlow, f"async_step_{_name}"):
        setattr(EnergyTwinOptionsFlow, f"async_step_{_name}", _make_step(_name))


def _make_section(name: str):
    async def handler(self, user_input=None):
        return await self._show_section(name)
    return handler


for _sec in SECTIONS:
    setattr(EnergyTwinOptionsFlow, f"async_step_{_sec}", _make_section(_sec))
