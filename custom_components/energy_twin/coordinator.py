"""The live digital twin: virtual battery + solar stepped against your real load, plus planning."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import ha_data
from .const import *  # noqa: F403 - constants module is the single source of config keys
from .sim import ev as evm, heat as heatm, load, pipeline
from .sim.engine import _slot_cost, charge_headroom, follow, plan_horizon, pv_series, settle
from .sim.finance import Costs, FinanceTerms
from .sim.models import EV_ON_KW, SLOT_H, GridEvent, Hardware, Slots, Strategy, TariffConfig
from .sim.solar import shape_to_totals, synth_pv_unit
from .sim.tariff import build_prices, floor_half_hour, make_starts, tod_minutes

_LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
EV_DEFAULT_KW = 7.0          # assumed charger draw in a smart-charge dispatch slot we can't see
SHARED = "energy_twin_shared"
MAX_GAP_S = 600              # don't integrate across longer gaps (restart, outage)


def _mins(v: str | int) -> int:
    if isinstance(v, int):
        return v
    parts = str(v).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _blank_bucket() -> dict:
    return {"actual_p": 0.0, "twin_p": 0.0, "pv_kwh": 0.0, "export_kwh": 0.0, "grid_charge_kwh": 0.0,
            "discharge_kwh": 0.0, "load_kwh": 0.0, "gas_p": 0.0, "hp_kwh": 0.0, "import_kwh": 0.0,
            "meter_kwh": 0.0, "trueup_kwh": 0.0}


@dataclass
class Plan:
    slots: Slots
    result: Any
    built: datetime
    soc0: float
    notes: dict = field(default_factory=dict)


def all_coordinators(hass: HomeAssistant) -> list:
    return [e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)]


class TwinCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=TICK_SECONDS))
        self.entry = entry
        self.tz = ZoneInfo(hass.config.time_zone or TZ_DEFAULT)
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.soc: float | None = None
        self.last_tick: datetime | None = None
        self.acc: dict = {"day_key": "", "month_key": "", "day": _blank_bucket(), "month": _blank_bucket(),
                          "total": _blank_bucket()}
        self.plan: Plan | None = None
        self.plan_dirty = True
        self.annual: dict | None = None
        self.annual_dirty = True
        self.annual_built: datetime | None = None
        self.sweep_rows: list[dict] = []
        self.sweep_info: dict = {}
        self.profile = load.default_profile()
        self.hist: tuple = (None, None, 0, None)
        self.diag: dict = {}
        self._internal_update = False
        self._annual_lock = asyncio.Lock()
        self._live: dict = {}
        self.veh_rt: dict[str, dict] = {}                      # per-car session tracking
        self.ev_info: dict = {}
        self.gas_model = heatm.default_gas_model(heatm.HeatParams())
        self._rate_cache: dict[str, dict] = {"imp": {}, "exp": {}}   # last good Octopus rates, so a blank update can't reset prices
        self._rates_parsed = {"imp": 0, "exp": 0}
        self._integ: dict[str, ha_data.PowerIntegrator] = {}        # exact integration of the power sensor's readings
        self._meter_prev: float | None = None                       # last reading of the Octopus consumption total
        self._meter_raw = 0.0                                       # kWh we integrated from raw power since that reading
        self._meter_led = 0.0                                       # ...and what we booked to the ledger meanwhile
        self.slot_raw: dict[str, dict] = {}                         # raw ledger increments per half-hour (last ~4 days)
        self.slot_applied: dict[str, dict] = {}                     # correction already applied per half-hour
        self.official: dict[datetime, float] = {}                   # the meter's own half-hourly kWh (Octopus `charges`)
        self.hh_history: dict[str, float] = {}                      # ...kept, to learn your usage from actual data
        self.ledger_start: datetime | None = None
        self._oct_cache: tuple = (0.0, "", {})
        self._oct_today_kwh: float | None = None
        self._k_ema, self._k_n = 1.0, 0                             # meter / integrated-power ratio (calibration)
        self.learned_rates: dict[str, float] = {}                    # real cheap / standard / export p/kWh seen in those rates
        self.tank_kwh: float | None = None
        self.hp_plan: heatm.HeatResult | None = None
        self._last_idx: int | None = None
        nxt = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        # state of the 'add an event' form on the dashboard
        self.form: dict = {"kind": "power_down", "start": nxt, "end": nxt + timedelta(hours=1), "rate": 0.0}

    @property
    def manual_events(self) -> list[GridEvent]:
        """Power Up/Down events are grid-wide, so every scenario shares one list."""
        return self.hass.data.setdefault(SHARED, {"events": []})["events"]

    @property
    def is_primary(self) -> bool:
        entries = self.hass.config_entries.async_entries(DOMAIN)
        return not entries or entries[0].entry_id == self.entry.entry_id

    @property
    def vehicles(self) -> list[evm.Vehicle]:
        """Configured cars. A chosen device supplies its entities; anything set explicitly overrides it."""
        out = []
        for d in self.cfg.get(CONF_VEHICLES) or []:
            merged = {k: v for k, v in d.items() if v not in (None, "")}
            did = d.get("device_id")
            if did:
                for k, v in self._discover_vehicle(did).items():
                    merged.setdefault(k, v)
                merged.setdefault("name", ha_data.device_name(self.hass, did) or "")
            try:
                if merged.get("name"):
                    out.append(evm.Vehicle.from_dict(merged))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _discover_vehicle(self, device_id: str) -> dict[str, str]:
        cache = self.__dict__.setdefault("_veh_disc", {})
        hit = cache.get(device_id)
        if hit is None or time.monotonic() - hit[0] > 300:
            hit = cache[device_id] = (time.monotonic(), ha_data.discover_vehicle_entities(self.hass, device_id))
        return hit[1]

    def vehicle_info(self, v: evm.Vehicle) -> evm.VehicleInfo:
        plug, home = self._state(v.plug_entity), self._state(v.home_entity)
        soc = ha_data.to_float(self._state(v.soc_entity))
        live = ha_data.power_kw(self._state(v.power_entity)) or 0.0
        rt = self.veh_rt.get(v.name, {})
        return evm.VehicleInfo(
            plugged=evm.interpret_plugged(plug.state if plug else None, v.plug_states),
            home=evm.interpret_home(home.state if home else None, v.home_states),
            soc_pct=soc, live_kw=live, delivered_kwh=rt.get("delivered", 0.0),
            target_soc_pct=ha_data.to_float(self._state(v.target_soc_entity)),
            raw_plug=plug.state if plug else None, raw_home=home.state if home else None)

    def _track_vehicles(self, now: datetime, h: float) -> None:
        """Energy delivered in the current charge session (for a car with no battery-level entity). Nothing is
        learned: a session ends when the car stops drawing power for 3 hours or is unplugged."""
        for v in self.vehicles:
            rt = self.veh_rt.setdefault(v.name, {"delivered": 0.0, "last": None})
            info = self.vehicle_info(v)
            if info.live_kw >= 0.3 and h > 0:
                rt["delivered"] += info.live_kw * h
                rt["last"] = now.isoformat()
            last = dt_util.parse_datetime(rt["last"]) if rt.get("last") else None
            idle = last is not None and (now - last) > timedelta(hours=3)
            if rt["delivered"] > 0 and (idle or info.plugged is False):
                rt["delivered"], rt["last"] = 0.0, None

    # ------------------------------------------------------------------ config accessors
    @property
    def cfg(self) -> dict:
        return {**DEFAULTS, **self.entry.data, **self.entry.options}

    def hardware(self, cfg: dict | None = None) -> Hardware:
        c = cfg or self.cfg
        return Hardware(pv_kwp=c[CONF_PV_KWP], inverter_kw=c[CONF_INVERTER_KW], pv_annual_yield=c[CONF_PV_YIELD],
                        battery_kwh=c[CONF_BATT_KWH], battery_max_kw=c[CONF_BATT_KW], round_trip_eff=c[CONF_RTE],
                        reserve_pct=c[CONF_RESERVE], export_limit_kw=c[CONF_EXPORT_LIMIT],
                        cycle_cost_p=c[CONF_CYCLE_COST], import_limit_kw=self.import_limit_kw(c))

    def start_soc(self, hw: Hardware | None = None) -> float:
        """Where the virtual battery starts (first run, reset): the configured % of capacity, within its limits."""
        hw = hw or self.hardware()
        pct = float(self.cfg.get(CONF_START_SOC, 50.0))
        return min(max(hw.emax * pct / 100.0, hw.emin), hw.emax)

    @staticmethod
    def import_limit_kw(c: dict) -> float:
        """Main cut-out fuse -> the most we plan to draw from the grid (0 = no limit set)."""
        amps = float(c.get(CONF_FUSE_A) or 0)
        phases = 3 if str(c.get(CONF_PHASES, "1")) == "3" else 1
        return amps * float(c.get(CONF_VOLTS) or 230) * phases / 1000.0 * float(c.get(CONF_FUSE_PCT, 90)) / 100.0

    def _live_gas_p(self, c: dict) -> float:
        """Gas unit rate: your rate sensor if set and readable, otherwise the fixed number."""
        v = ha_data.price_p(self._state(c.get(CONF_GAS_RATE_ENTITY)))
        return v if v is not None and v > 0 else c[CONF_GAS_P]

    def _live_standing_p(self, c: dict) -> float:
        v = ha_data.pence_per_day(self._state(c.get(CONF_GAS_STANDING_ENTITY)))
        return v if v is not None and v >= 0 else c[CONF_GAS_STANDING]

    def heat_params(self, cfg: dict | None = None) -> heatm.HeatParams:
        c = cfg or self.cfg
        return heatm.HeatParams(
            enabled=bool(c[CONF_HP_ENABLED]), tank_litres=c[CONF_TANK_L], tank_setpoint_c=c[CONF_TANK_C],
            tank_loss_kwh_day=c[CONF_TANK_LOSS], legionella_c=c[CONF_LEGIONELLA_C], dhw_start_min=_mins(c[CONF_DHW_START]),
            hp_elec_max_kw=c[CONF_HP_ELEC_KW], hp_capacity_kw=c[CONF_HP_CAP_KW], flow_temp_c=c[CONF_FLOW_C],
            carnot_frac=c[CONF_CARNOT], base_temp_c=c[CONF_BASE_TEMP], boiler_eff_heating=c[CONF_BOILER_EFF_H],
            boiler_eff_dhw=c[CONF_BOILER_EFF_D], cooking_gas_kwh_day=c[CONF_COOK_GAS], induction_ratio=c[CONF_INDUCTION],
            fallback_annual_gas_kwh=c[CONF_FALLBACK_GAS], gas_unit_p=self._live_gas_p(c), gas_standing_p_day=self._live_standing_p(c),
            gas_disconnect=bool(c[CONF_GAS_DISCONNECT]))

    def tariff(self, cfg: dict | None = None) -> TariffConfig:
        c = cfg or self.cfg
        return TariffConfig(cheap_start_min=_mins(c[CONF_CHEAP_START]), cheap_end_min=_mins(c[CONF_CHEAP_END]),
                            cheap_p=c[CONF_CHEAP_P], standard_p=c[CONF_STANDARD_P], export_p=c[CONF_EXPORT_P],
                            power_up_import_p=c[CONF_PU_IMPORT_P], power_down_export_p=c[CONF_PD_EXPORT_P],
                            power_down_bonus_p=c[CONF_PD_BONUS_P])

    def strategy(self, cfg: dict | None = None) -> Strategy:
        c = cfg or self.cfg
        return Strategy(name=c[CONF_STRATEGY], margin_pct=c[CONF_MARGIN], export_from_min=_mins(c[CONF_EXPORT_FROM]),
                        export_until_min=_mins(c[CONF_EXPORT_UNTIL]), event_export_anywhere=bool(c[CONF_EVENT_EXPORT]),
                        export_margin_p=c[CONF_EXPORT_MARGIN], arbitrage=c[CONF_ARBITRAGE],
                        allow_battery_export=c[CONF_BATT_EXPORT])

    def costs(self, c: dict | None = None) -> Costs:
        c = c or self.cfg
        return Costs(system=float(c[CONF_SYSTEM_COST]), grant=float(c[CONF_GRANT]))

    def terms(self, c: dict | None = None) -> FinanceTerms:
        c = c or self.cfg
        term = int(c[CONF_TERM])
        mode = c.get(CONF_FIN_MODE) or ("loan" if term > 0 else "cash")
        return FinanceTerms(mode, c[CONF_DEPOSIT], c[CONF_FIN_FEE], c[CONF_APR], term, c[CONF_FIN_MONTHLY],
                            c[CONF_ESCALATION], c[CONF_PV_DEG], c[CONF_BATT_DEG], c[CONF_DISCOUNT], int(c[CONF_HORIZON]))

    # ------------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        saved = await self._store.async_load() or {}
        hw = self.hardware()
        # restore the virtual battery level; clamp in case the battery was resized while we were off
        saved_soc = saved.get("soc")
        self.soc = self.start_soc(hw) if saved_soc is None else min(max(float(saved_soc), hw.emin), hw.emax)
        self.acc = {**self.acc, **saved.get("acc", {})}
        self.veh_rt = saved.get("veh_rt") or {}
        self.tank_kwh = saved.get("tank_kwh")
        self.slot_raw = dict((saved.get("slots") or {}).get("raw") or {})
        self.slot_applied = dict((saved.get("slots") or {}).get("applied") or {})
        self.hh_history = {k: float(v) for k, v in (saved.get("hh") or {}).items()}
        self.ledger_start = dt_util.parse_datetime(saved["ledger_start"]) if saved.get("ledger_start") else None
        if isinstance(saved.get("meter_k"), list) and len(saved["meter_k"]) == 2:
            self._k_ema, self._k_n = float(saved["meter_k"][0]), int(saved["meter_k"][1])
        self.learned_rates = {k: float(v) for k, v in (saved.get("learned_rates") or {}).items()}
        for e in saved.get("events", []):
            try:
                ev = GridEvent(e["kind"], dt_util.parse_datetime(e["start"]), dt_util.parse_datetime(e["end"]),
                               e.get("import_p"), e.get("export_p"), e.get("bonus_p"))
            except (KeyError, TypeError):
                continue
            if not any((m.kind, m.start, m.end) == (ev.kind, ev.start, ev.end) for m in self.manual_events):
                self.manual_events.append(ev)
        if saved.get("last"):
            self.last_tick = dt_util.parse_datetime(saved["last"])
        pw = self.cfg.get(CONF_POWER_ENTITY)
        if pw and (st := self.hass.states.get(pw)) is not None:      # start integrating readings from the very first plan
            self.feed_power(pw, st, dt_util.utcnow())

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {
            "soc": self.soc, "last": self.last_tick.isoformat() if self.last_tick else None, "acc": self.acc,
            "veh_rt": self.veh_rt,
            "tank_kwh": self.tank_kwh, "learned_rates": self.learned_rates, "meter_k": [self._k_ema, self._k_n],
            "slots": {"raw": self.slot_raw, "applied": self.slot_applied}, "hh": self.hh_history,
            "ledger_start": self.ledger_start.isoformat() if self.ledger_start else None,
            "events": [{"kind": e.kind, "start": e.start.isoformat(), "end": e.end.isoformat(), "import_p": e.import_p,
                        "export_p": e.export_p, "bonus_p": e.bonus_p} for e in self.manual_events]}, 30)

    def consume_internal_update(self) -> bool:
        was, self._internal_update = self._internal_update, False
        return was

    # ------------------------------------------------------------------ user actions
    async def async_set_options(self, updates: dict, *, annual: bool = True) -> None:
        self._internal_update = True
        self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, **updates})
        self.plan_dirty = True
        self.annual_dirty = self.annual_dirty or annual
        hw = self.hardware()
        if self.soc is not None:
            self.soc = min(max(self.soc, hw.emin), hw.emax)
        await self.async_refresh()

    async def async_add_event(self, ev: GridEvent) -> None:
        if not any((m.kind, m.start, m.end) == (ev.kind, ev.start, ev.end) for m in self.manual_events):
            self.manual_events.append(ev)
        await self._refresh_all()

    async def _refresh_all(self) -> None:
        """An event changes every scenario's plan - refresh them all."""
        for c in all_coordinators(self.hass):
            c.plan_dirty = True
            c._save()
            await c.async_refresh()

    async def async_add_event_from_form(self) -> None:
        f = self.form
        if f["end"] <= f["start"]:
            raise HomeAssistantError("Event end must be after its start")
        rate = f["rate"] if f["rate"] and f["rate"] > 0 else None
        up = f["kind"] == "power_up"
        await self.async_add_event(GridEvent(f["kind"], f["start"], f["end"], rate if up else None,
                                             None if up else rate))

    async def async_clear_events(self) -> None:
        self.manual_events.clear()
        await self._refresh_all()

    async def async_set_soc_pct(self, pct: float) -> None:
        """Set the virtual battery level by hand (% of capacity; kept between your reserve and full)."""
        hw = self.hardware()
        self.soc = min(max(hw.emax * float(pct) / 100.0, hw.emin), hw.emax)
        self.plan_dirty = True
        self._save()
        await self.async_refresh()

    async def async_reset_twin(self) -> None:
        self.soc = self.start_soc()
        self.acc = {"day_key": "", "month_key": "", "day": _blank_bucket(), "month": _blank_bucket(),
                    "total": _blank_bucket()}
        self.slot_raw, self.slot_applied, self.ledger_start = {}, {}, None
        self.plan_dirty = True
        self._save()
        await self.async_refresh()

    # ------------------------------------------------------------------ inputs
    def _state(self, entity_id: str | None):
        return self.hass.states.get(entity_id) if entity_id else None

    def _states(self, ids) -> list:
        return [self.hass.states.get(i) for i in (ids or [])]

    def _all_events(self, now: datetime) -> list[GridEvent]:
        c = self.cfg
        evs = [e for e in self.manual_events if e.end > now]
        evs += ha_data.parse_events(self._state(c.get(CONF_POWER_UP_ENTITY)), "power_up", now)
        evs += ha_data.parse_events(self._state(c.get(CONF_POWER_DOWN_ENTITY)), "power_down", now)
        return evs

    def forecast_entry_id(self) -> str | None:
        return ha_data.resolve_forecast_entry(self.hass, self.cfg)

    def forecast_ref_kwp(self) -> float:
        """Array size the forecast describes: your override, else read from the forecast device, else 1 kWp."""
        c = self.cfg
        return float(c.get(CONF_FORECAST_REF_KWP) or ha_data.forecast_array_kwp(self.hass, self.forecast_entry_id()) or 1.0)

    def _scale_pv(self, kw_ref: float) -> float:
        """kW from a forecast built for the reference array -> kW per kWp."""
        return kw_ref / max(self.forecast_ref_kwp(), 0.01)

    # ------------------------------------------------------------------ planning
    async def _build_plan(self, now: datetime) -> Plan:
        c = self.cfg
        hw, st, tariff = self.hardware(c), self.strategy(c), self.tariff(c)
        t0 = floor_half_hour(now)
        starts, t, crossings = [], t0, 0
        while len(starts) < 100:
            starts.append(t)
            t += timedelta(minutes=30)
            loc = t.astimezone(self.tz)
            if loc.hour * 60 + loc.minute == tariff.cheap_start_min:
                crossings += 1
                if crossings == 2:
                    break
        tod = tod_minutes(starts, self.tz)
        imp_rates = self._rates("imp", c.get(CONF_IMPORT_RATES_ENTITIES), now)
        exp_rates = self._rates("exp", c.get(CONF_EXPORT_RATES_ENTITIES), now)
        self._learn_rates(tariff)
        # for any slot the rate lists don't cover, prefer what Octopus actually charges you over the placeholders
        tariff = replace(tariff, **{k: v for k, v in self.learned_rates.items() if k in ("cheap_p", "standard_p", "export_p")})
        dispatches = ha_data.parse_dispatches(self._state(c.get(CONF_DISPATCH_ENTITY)), now)
        events = self._all_events(now)
        imp, exp, cheap, ev_flag, bonus = build_prices(starts, tod, tariff, import_rates=imp_rates,
                                                       export_rates=exp_rates, dispatches=dispatches, events=events)
        # live price entities beat everything for the current slot
        live_imp = ha_data.price_p(self._state(c.get(CONF_IMPORT_NOW_ENTITY)))
        live_exp = ha_data.price_p(self._state(c.get(CONF_EXPORT_NOW_ENTITY)))
        if live_imp is not None and not ev_flag[0]:
            imp[0] = live_imp
            cheap[0] = cheap[0] or live_imp <= tariff.cheap_p + 0.5
        if live_exp is not None and not ev_flag[0]:
            exp[0] = live_exp

        fe = self.forecast_entry_id()
        fc = await ha_data.cached(self.hass, ("fc", fe), 240, lambda: ha_data.forecast_pv_kw(self.hass, fe))
        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        _, synth = synth_pv_unit(starts, lat, lon, hw.pv_annual_yield)
        ents = self.forecast_entities()
        ref = max(self.forecast_ref_kwp(), 0.01)
        scale = c[CONF_FC_SCALE] / 100.0
        st_ = self._state
        kwh_rem = ha_data.energy_kwh(st_(ents.get("energy_production_today_remaining")))
        kwh_tom = ha_data.energy_kwh(st_(ents.get("energy_production_tomorrow")))
        pk_today = ha_data.timestamp_state(st_(ents.get("power_highest_peak_time_today")))
        pk_tom = ha_data.timestamp_state(st_(ents.get("power_highest_peak_time_tomorrow")))
        from_fc, source = 0, "seasonal curve (no forecast configured)"
        if fc:
            # the integration's own hourly forecast, period-end keyed (see ha_data.wh_period_to_kw)
            pv_unit = [fc[s] / ref * scale if s in fc else synth[i] for i, s in enumerate(starts)]
            from_fc, source = sum(1 for s in starts if s in fc), "forecast integration (hourly)"
        elif kwh_rem is not None or kwh_tom is not None:
            # no hourly feed: size and align the seasonal curve to the forecast's own totals and peak time
            shaped = shape_to_totals(starts, self.tz, [u * ref for u in synth], now, kwh_rem, kwh_tom, pk_today, pk_tom)
            pv_unit = [shaped[i] / ref * scale for i in range(len(starts))]
            from_fc, source = len(starts), "forecast sensors (daily totals + peak time)"
        else:
            pv_unit = list(synth)
        live_pv = ha_data.power_kw(self._state(c.get(CONF_PV_NOW_ENTITY) or ents.get("power_production_now")))
        if live_pv is not None:
            pv_unit[0] = self._scale_pv(live_pv) * scale
        self._fc_diag = self._forecast_check(starts, pv_unit, ref, scale, source, ents, kwh_rem, kwh_tom, now)

        disp_slots = {floor_half_hour(a) + timedelta(minutes=30 * k)
                      for a, b in dispatches for k in range(max(1, int((b - a).total_seconds() // 1800)))}
        house = []
        for i, s in enumerate(starts):
            loc = s.astimezone(self.tz)
            house.append(self.profile.house(loc.date(), tod[i] // 30) * load.SEASONAL[loc.month - 1])
        vehicles = self.vehicles
        if vehicles:
            # per-car model: charge what is plugged in / dispatched; only probabilities for cars that aren't
            infos = {v.name: self.vehicle_info(v) for v in vehicles}
            evk, self.ev_info = evm.build_ev_load(starts, tod, self.tz, tariff, vehicles, infos, disp_slots)
            for note, v in zip(self.ev_info, vehicles):
                note["entities"] = {role: ent for role, ent in (
                    ("power", v.power_entity), ("plug", v.plug_entity), ("home", v.home_entity), ("battery", v.soc_entity),
                    ("charge limit", v.target_soc_entity), ("energy", v.energy_stat)) if ent}
        else:                                  # legacy: smoothed average profile
            evk = []
            for i, s in enumerate(starts):
                e = self.profile.ev[tod[i] // 30]
                if s in disp_slots and e < EV_ON_KW:
                    e = EV_DEFAULT_KW
                evk.append(e)
            self.ev_info = []
        sl = Slots(starts, tod, imp, exp, cheap, ev_flag, bonus, list(house), list(evk), pv_unit,
                   list(house), list(evk), list(pv_unit), [""] * len(starts))
        hp = self.heat_params(c)
        self.hp_plan = None
        if hp.enabled:
            sl.new_kw, sl.exp_new_kw = await self._heat_load(starts, tod, cheap, ev_flag, tariff, hp, now)
        hw_soc = self.soc if self.soc is not None else self.start_soc(hw)
        res = await self.hass.async_add_executor_job(plan_horizon, sl, hw, st, hw_soc)
        self.diag = {
            "import_rate_slots": self._rates_parsed["imp"], "export_rate_slots": self._rates_parsed["exp"],
            "rates_learned_p_per_kwh": dict(self.learned_rates) or "not yet",
            **({"import_rates_hint": "No Octopus rate list is being read, so prices for later half-hours come from the rates "
                                     "learned from your live price sensor (or the placeholders). Under Configure > Data sources "
                                     "> Octopus, pick the rate entities that have a 'rates' attribute (current + next day)."}
               if self._rates_parsed["imp"] == 0 else {}),
            "dispatches": len(dispatches), "events_active_or_upcoming": len(events),
            "forecast_slots_used": from_fc, "forecast_slots_total": len(starts),
            "load_profile": f"{self.profile.source} ({self.profile.days} days)",
            "horizon_slots": len(starts), "live_price_entity": live_imp is not None,
            "live_pv_entity": live_pv is not None,
            "vehicles": self.ev_info,
            "octopus_consumption": ({**self._octopus_ids(self.cfg.get(CONF_CONSUMPTION_STAT) or ""),
                                     "official_half_hours": len(self.official),
                                     "history_days": round(len(self.hh_history) / 48, 1)}
                                    if "." in (self.cfg.get(CONF_CONSUMPTION_STAT) or "") else "no consumption sensor set"),
            "power_integration": ("every reading as it arrives" if self._integ else "sampled once a minute"),
            "meter": ({"entity": self.cfg.get(CONF_CONSUMPTION_STAT), "calibration_factor": round(self.meter_k, 3),
                       "calibration_samples": self._k_n} if self.cfg.get(CONF_CONSUMPTION_STAT) else "no consumption sensor set"),
            "battery_export_window": f"{c[CONF_EXPORT_FROM][:5]}-{c[CONF_EXPORT_UNTIL][:5]}"
                                     + (" (Power Down events may go outside it)" if c[CONF_EVENT_EXPORT] else ""),
            **self._fc_diag,
            "heat_pump": ({"gas_rate_p": round(hp.gas_unit_p, 2), "gas_standing_p_day": round(hp.gas_standing_p_day, 1),
                           "gas_rate_from": "sensor" if ha_data.price_p(self._state(c.get(CONF_GAS_RATE_ENTITY))) else "fixed number",
                           "temps_from": self._temps_source, "gas_model": self.gas_model.source,
                           "tank_kwh": round(self.tank_kwh or 0.0, 1)} if hp.enabled else "off"),
            "fuse_limit_kw": round(hw.import_limit_kw, 1),
        }
        return Plan(sl, res, now, hw_soc)

    def _rates(self, kind: str, entity_ids, now: datetime) -> dict[datetime, float] | None:
        """Octopus rates, remembering the last good lists: an update that briefly shows no 'rates' must not make
        the plan fall back to placeholder prices (that made the import price flick between real and placeholder)."""
        cache = self._rate_cache[kind]
        parsed = ha_data.parse_rates(self._states(entity_ids))
        self._rates_parsed[kind] = len(parsed)
        cache.update(parsed)
        for k in [k for k in cache if k < now - timedelta(days=1)]:
            del cache[k]
        return cache or None

    def _learn_rates(self, tariff: TariffConfig) -> None:
        """Your real standard / cheap import rates and export rate, taken from the rate lists (medians)."""
        imp = self._rate_cache["imp"]
        cheap, std = [], []
        for t_, p_ in imp.items():
            loc = t_.astimezone(self.tz)
            (cheap if tariff.in_cheap_window(loc.hour * 60 + loc.minute) else std).append(p_)
        med = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
        if len(cheap) >= 2:
            self.learned_rates["cheap_p"] = round(med(cheap), 3)
        if len(std) >= 2:
            self.learned_rates["standard_p"] = round(med(std), 3)
        if len(self._rate_cache["exp"]) >= 2:
            self.learned_rates["export_p"] = round(med(list(self._rate_cache["exp"].values())), 3)

    async def async_use_live_rates(self) -> None:
        """Copy the rates learned from Octopus into the tariff settings (the annual what-if uses those numbers)."""
        upd = {k: v for k, v in self.learned_rates.items() if k in (CONF_CHEAP_P, CONF_STANDARD_P, CONF_EXPORT_P)}
        if upd:
            await self.async_set_options(upd)

    _temps_source = "synthetic"
    _fc_diag: dict = {}

    def forecast_entities(self) -> dict[str, str]:
        """The forecast device's own sensors, found automatically from its config entry."""
        return ha_data.forecast_entities(self.hass, self.forecast_entry_id())

    def _forecast_check(self, starts, pv_unit, ref, scale, source, ents, kwh_rem, kwh_tom, now) -> dict:
        """What the planner will believe, next to the forecast sensors' own numbers (they should agree)."""
        loc = [s.astimezone(self.tz).date() for s in starts]
        today = now.astimezone(self.tz).date()
        mine = lambda day: round(sum(pv_unit[i] * ref / scale * 0.5 for i in range(len(starts)) if loc[i] == day), 1)  # noqa: E731
        fe = self.forecast_entry_id()
        entry = self.hass.config_entries.async_get_entry(fe) if fe else None
        out: dict = {"forecast_device": entry.title if entry else "none found",
                     "forecast_array_kwp": round(self.forecast_ref_kwp(), 2), "forecast_source": source, "forecast_confidence_pct": round(scale * 100),
                     "forecast_entities_found": len(ents),
                     "planner_solar_remaining_today_kwh": mine(today),
                     "planner_solar_tomorrow_kwh": mine(today + timedelta(days=1))}
        if kwh_rem is not None:
            out["sensor_remaining_today_kwh"] = round(kwh_rem, 1)
        if kwh_tom is not None:
            out["sensor_tomorrow_kwh"] = round(kwh_tom, 1)
            if kwh_tom > 0.5:
                d = 100 * (out["planner_solar_tomorrow_kwh"] - kwh_tom) / kwh_tom
                out["forecast_check_tomorrow"] = "OK" if abs(d) <= 10 else f"MISMATCH {d:+.0f}% vs sensor"
        hour = ha_data.energy_kwh(self._state(ents.get("energy_current_hour")))
        if hour is not None and hour > 0.05 and source.startswith("forecast integration"):
            h0 = now.replace(minute=0, second=0, microsecond=0)
            ours = sum(pv_unit[i] * ref / scale * 0.5 for i, s in enumerate(starts) if h0 <= s < h0 + timedelta(hours=1))
            out["sensor_this_hour_kwh"] = round(hour, 2)
            # only comparable when the whole hour is inside the horizon (it starts at the current half-hour)
            if starts[0] <= h0:
                out["forecast_check_hour"] = "OK" if abs(ours - hour) <= 0.15 * hour + 0.03 else f"MISMATCH ({ours:.2f} vs {hour:.2f} kWh)"
        return out

    async def _heat_load(self, starts, tod, cheap, ev_flag, tariff, hp, now):
        c = self.cfg
        wx = c.get(CONF_WEATHER_ENTITY)
        fc = await ha_data.cached(self.hass, ("wx", wx), 1800, lambda: ha_data.forecast_temps(self.hass, wx))
        _, synth = heatm.synth_temps(starts, self.tz)
        now_c = ha_data.to_float(self._state(c.get(CONF_OUTDOOR_ENTITY)))
        # forecast where we have it; otherwise a seasonal curve nudged by what the sensor says right now
        bias = (now_c - synth[0]) if now_c is not None else 0.0
        temps = [fc[s] if s in fc else synth[i] + bias * math.exp(-i / 24.0) for i, s in enumerate(starts)]
        self._temps_source = "forecast" if fc else ("sensor-adjusted seasonal" if now_c is not None else "seasonal")
        tank0 = self.tank_kwh
        tt = ha_data.to_float(self._state(c.get(CONF_TANK_TEMP_ENTITY)))
        if tt is not None:                                    # a real tank sensor beats the model
            tank0 = min(hp.e_leg, max(0.0, hp.tank_litres * heatm.KWH_PER_L_K * (tt - hp.cold_in_c)))
        win = [tariff.in_cheap_window(t) for t in tod]
        res = heatm.horizon(starts, tod, self.tz, cheap, ev_flag, win, temps, hp, self.gas_model, tank0)
        self.hp_plan = res
        return res.new_kw, list(res.new_kw)

    # ------------------------------------------------------------------ exact load measurement
    def feed_power(self, entity_id: str, state, when: datetime) -> None:
        """Called on every state change of the power sensor (about every 20 s for a Home Mini)."""
        self._integ.setdefault(entity_id, ha_data.PowerIntegrator()).feed(when, ha_data.power_kw(state))

    @property
    def meter_k(self) -> float:
        """Correction for a power sensor that reads consistently high/low against the real (Octopus) meter."""
        k = min(1.4, max(0.7, self._k_ema))
        return k if (self._k_n >= 5 and abs(k - 1.0) > 0.02) else 1.0

    def _reconcile_meter(self, now: datetime, raw_kwh: float, led_kwh: float, imp_p: float, gated: bool) -> None:
        """Learn whether the live power sensor reads consistently high/low against the Octopus running total.
        (The ledger itself is corrected from the meter's half-hourly data - see _apply_corrections.)"""
        ent = (self.cfg.get(CONF_CONSUMPTION_STAT) or "").strip()
        if "." not in ent:
            return
        ids = self._octopus_ids(ent)
        m = ha_data.energy_kwh(self._state(ids.get("total") or ent))
        if m is None:
            return
        if gated:
            self._meter_raw += raw_kwh
        prev, self._meter_prev = self._meter_prev, m
        if prev is None or m == prev:
            return
        delta = m - prev if m >= prev else 0.0
        raw = self._meter_raw
        self._meter_raw = 0.0
        if gated and 0.2 <= delta < 30.0 and raw >= 0.2 and 0.5 < delta / raw < 2.0:
            r = delta / raw
            self._k_ema = r if self._k_n == 0 else 0.8 * self._k_ema + 0.2 * r
            self._k_n += 1

    # ------------------------------------------------------------------ the meter's own half-hourly data
    def _octopus_ids(self, ent: str) -> dict[str, str]:
        ts, known, ids = self._oct_cache
        now_m = time.monotonic()
        if ent != known or now_m - ts > 600:
            ids = ha_data.octopus_consumption_entities(self.hass, ent)
            self._oct_cache = (now_m, ent, ids)
        return ids

    def _price_for(self, slot: datetime) -> float:
        p = self._rate_cache["imp"].get(slot)
        if p is not None:
            return p
        tar, loc = self.tariff(), slot.astimezone(self.tz)
        if tar.in_cheap_window(loc.hour * 60 + loc.minute):
            return self.learned_rates.get("cheap_p", tar.cheap_p)
        return self.learned_rates.get("standard_p", tar.standard_p)

    def _refresh_official(self) -> None:
        """Read the meter's half-hourly consumption (`charges`). Yesterday's (the billing data) wins over today's
        Home Mini data for the same period; both are kept as the authoritative history."""
        ent = (self.cfg.get(CONF_CONSUMPTION_STAT) or "").strip()
        if "." not in ent:
            self.official = {}
            return
        ids = self._octopus_ids(ent)
        today_state = self._state(ids.get("today"))
        self._oct_today_kwh = ha_data.energy_kwh(today_state)
        merged = {**ha_data.parse_charges(today_state), **ha_data.parse_charges(self._state(ids.get("previous")))}
        if merged:
            self.official = merged
            for s_, v in merged.items():
                self.hh_history[s_.isoformat()] = round(v, 4)
            if len(self.hh_history) > 370 * 48:
                for k in sorted(self.hh_history)[: len(self.hh_history) - 370 * 48]:
                    del self.hh_history[k]

    def _book_slot(self, now: datetime, load: float, actual_p: float, twin_p: float, price: float) -> None:
        r = self.slot_raw.setdefault(floor_half_hour(now).isoformat(), {"load": 0.0, "actual_p": 0.0, "twin_p": 0.0,
                                                                        "price": price})
        r["load"] += load
        r["actual_p"] += actual_p
        r["twin_p"] += twin_p
        r["price"] = price

    def _apply_corrections(self, now: datetime) -> None:
        """Make the ledger match the meter, half-hour by half-hour. Idempotent and self-correcting: if Octopus later
        revises a period, the earlier correction is replaced, not stacked. Periods before the ledger began, and the
        half-hour still in progress, are left alone."""
        if not self.official:
            return
        cur = floor_half_hour(now)
        day_key, month_key = self.acc.get("day_key"), self.acc.get("month_key")
        for s_, e in self.official.items():
            if s_ >= cur or (self.ledger_start and s_ < self.ledger_start):
                continue
            iso = s_.isoformat()
            raw = self.slot_raw.get(iso) or {"load": 0.0, "actual_p": 0.0, "twin_p": 0.0, "price": self._price_for(s_)}
            load = raw["load"]
            if load > 0.01:                                           # keep the price-weighted shape, fix the size
                f = min(5.0, max(0.2, e / load))
                corr = {"load": e - load, "actual_p": raw["actual_p"] * (f - 1), "twin_p": raw["twin_p"] * (f - 1)}
            else:                                                     # a period we didn't see (e.g. HA was down)
                p = raw.get("price") or self._price_for(s_)
                corr = {"load": e - load, "actual_p": e * p - raw["actual_p"], "twin_p": e * p - raw["twin_p"]}
            applied = self.slot_applied.get(iso, {})
            delta = {k: corr[k] - applied.get(k, 0.0) for k in corr}
            if max(abs(v) for v in delta.values()) < 1e-9:
                continue
            loc = s_.astimezone(self.tz)
            targets = [self.acc["total"]]
            if loc.strftime("%Y-%m") == month_key:
                targets.append(self.acc["month"])
            if loc.strftime("%Y-%m-%d") == day_key:
                targets.append(self.acc["day"])
            for b in targets:
                b["load_kwh"] += delta["load"]
                b["actual_p"] += delta["actual_p"]
                b["twin_p"] += delta["twin_p"]
                b["trueup_kwh"] = b.get("trueup_kwh", 0.0) + delta["load"]
            self.slot_applied[iso] = corr
            self.slot_raw.setdefault(iso, raw)
        cutoff = (now - timedelta(days=4)).isoformat()
        for d in (self.slot_raw, self.slot_applied):
            for k in [k for k in d if k < cutoff]:
                del d[k]

    def _merge_official_history(self, rows):
        """Replace hours of the recorder's statistics with the meter's own half-hourly data where we have it."""
        if not self.hh_history:
            return rows
        hours: dict[datetime, dict[int, float]] = {}
        for iso, v in self.hh_history.items():
            t_ = dt_util.parse_datetime(iso)
            if t_ is not None:
                hours.setdefault(t_.replace(minute=0, second=0, microsecond=0), {})[t_.minute] = v
        base = dict(rows or [])
        base.update({h: d[0] + d[30] for h, d in hours.items() if 0 in d and 30 in d})
        return sorted(base.items())

    def _plan_index(self, now: datetime) -> int | None:
        if not self.plan:
            return None
        i = int((floor_half_hour(now) - self.plan.slots.start[0]).total_seconds() // 1800)
        return i if 0 <= i < self.plan.slots.n else None

    # ------------------------------------------------------------------ tick
    async def _async_update_data(self) -> dict:
        now = dt_util.utcnow()
        c = self.cfg
        hw = self.hardware(c)
        if self.soc is None:
            self.soc = self.start_soc(hw)
        stale = self.plan is None or (now - self.plan.built).total_seconds() > REPLAN_SECONDS \
            or self._plan_index(now) is None
        if self.plan_dirty or stale:
            self.plan = await self._build_plan(now)
            self.plan_dirty = False
        if self.annual_dirty or self.annual_built is None or (now - self.annual_built) > timedelta(hours=24):
            self.annual_dirty = False
            self.hass.async_create_task(self._async_annual())
        idx = self._plan_index(now)
        data = self._step(now, hw, idx) if idx is not None else {}
        self._save()
        return self._snapshot(now, hw, data)

    def _step(self, now: datetime, hw: Hardware, idx: int) -> dict:
        c, sl, plan = self.cfg, self.plan.slots, self.plan.result
        pv_plan = pv_series(sl.pv_unit, hw)
        pw_ent = c.get(CONF_POWER_ENTITY)
        inst = ha_data.power_kw(self._state(pw_ent))
        integ = self._integ.get(pw_ent)
        raw_total = None
        if inst is not None and integ is not None:
            if integ.kw != inst:
                integ.resync(now, inst)
            avg = integ.take(now)
            raw_total = avg if avg is not None else inst
        elif inst is not None:
            raw_total = inst
        total = None if raw_total is None else raw_total * self.meter_k
        veh = self.vehicles
        ev_states = ([self.vehicle_info(v).live_kw for v in veh if v.power_entity] if veh else []) or \
                    [ha_data.power_kw(s) for s in self._states(c.get(CONF_EV_POWER_ENTITIES))]
        ev = sum(v for v in ev_states if v) if ev_states else (sl.ev_kw[idx] if total is None else 0.0)
        house = max(0.0, total - ev) if total is not None else sl.house_kw[idx]
        pv = pv_plan[idx]
        new = sl.new_kw[idx] if sl.new_kw else 0.0            # modelled heat pump + induction hob (not on your meter yet)
        exp_new = sl.exp_new_kw[idx] if sl.exp_new_kw else 0.0
        real_house = house
        house = house + new
        r_plan = plan.battery_kw[idx]
        # Power can't be sent to a car from the battery (CT clamps / inverter firmware can't tell them apart), so
        # discharge is blocked whenever a car is drawing power. If no car power is measured, trust the plan's
        # expectation (plugged-in car / smart-charge slot) rather than assuming it is off.
        measured_ev = bool(ev_states)
        ev_block = ev >= EV_ON_KW or (not measured_ev and sl.ev_kw[idx] >= EV_ON_KW)
        r_req = follow(r_plan, sl, idx, house, ev, pv, sl.exp_house_kw[idx] + exp_new, pv_plan[idx], ev_block=ev_block)
        dt_s = (now - self.last_tick).total_seconds() if self.last_tick else 0.0
        self.last_tick = now
        h = dt_s / 3600.0 if 0 < dt_s <= MAX_GAP_S else 0.0       # real elapsed hours since the last tick
        self._track_vehicles(now, h)
        soc_before = self.soc
        # advance the virtual battery by the time that really passed (NOT a whole half-hour slot)
        self.soc, r = settle(hw, self.soc, r_req if hw.battery_kwh > 0 else 0.0, ev_block,
                             hw.discharge_cap(pv), dt=h or SLOT_H, step=h,
                             max_charge=charge_headroom(hw, house + ev - pv))
        grid = house + ev - pv + r
        imp_p, exp_p, bonus = sl.import_p[idx], sl.export_p[idx], sl.bonus_p[idx]
        # the price "right now" is what your live price sensors say, not whatever the plan guessed for this slot
        if not sl.event[idx]:
            live_i = ha_data.price_p(self._state(c.get(CONF_IMPORT_NOW_ENTITY)))
            live_x = ha_data.price_p(self._state(c.get(CONF_EXPORT_NOW_ENTITY)))
            imp_p = live_i if live_i is not None else imp_p
            exp_p = live_x if live_x is not None else exp_p
            slot_in_window = self.tariff(c).in_cheap_window(sl.tod_min[idx])
            if sl.cheap[idx] == slot_in_window:                    # skip smart-charge slots: they aren't the normal rate
                slot = floor_half_hour(now)
                if live_i is not None:
                    self._rate_cache["imp"][slot] = live_i
                if live_x is not None:
                    self._rate_cache["exp"][slot] = live_x
        cost_p, exp_kw = _slot_cost(hw, grid, r, imp_p, exp_p, bonus)
        hp_p = self.heat_params(c)
        gas_kw = self.hp_plan.gas_kw[idx] if (hp_p.enabled and self.hp_plan) else 0.0
        if not (hp_p.enabled and self.hp_plan):
            hp_p = replace(hp_p, gas_standing_p_day=0.0)
        actual_per_h = (real_house + ev) * imp_p               # what you really pay right now (p/h)
        if 0 < dt_s <= MAX_GAP_S:
            h = dt_s / 3600.0
            self._roll_buckets(now)
            for b in (self.acc["day"], self.acc["month"], self.acc["total"]):
                b["actual_p"] += actual_per_h * h
                b["twin_p"] += cost_p / SLOT_H * h
                b["pv_kwh"] += pv * h
                b["export_kwh"] += exp_kw * h
                b["import_kwh"] = b.get("import_kwh", 0.0) + max(grid, 0.0) * h
                b["load_kwh"] += (real_house + ev) * h
                b["hp_kwh"] = b.get("hp_kwh", 0.0) + new * h
                b["gas_p"] = b.get("gas_p", 0.0) + (gas_kw * hp_p.gas_unit_p
                                                    + (hp_p.gas_standing_p_day / 24.0 if hp_p.gas_disconnect else 0.0)) * h
                b["grid_charge_kwh"] += max(0.0, r - max(0.0, -(house + ev - pv))) * h if r > 0 else 0.0
                b["discharge_kwh"] += -r * h if r < 0 else 0.0
        if self.ledger_start is None:
            self.ledger_start = floor_half_hour(now)
        if 0 < dt_s <= MAX_GAP_S:
            self._book_slot(now, (real_house + ev) * (dt_s / 3600.0), actual_per_h * (dt_s / 3600.0),
                            cost_p / SLOT_H * (dt_s / 3600.0), imp_p)
        self._refresh_official()
        self._apply_corrections(now)
        gated_ok = 0 < dt_s <= MAX_GAP_S
        raw_kwh = (raw_total * h) if (raw_total is not None and gated_ok) else 0.0
        self._reconcile_meter(now, raw_kwh, (real_house + ev) * h if gated_ok else 0.0, imp_p, gated_ok)
        mode = self._mode(sl.event[idx], r, grid, exp_kw, house + ev - pv, sl.cheap[idx])
        hpr = self.hp_plan
        if hp_p.enabled and hpr and idx != self._last_idx:      # a new half-hour: carry the tank state forward
            self.tank_kwh = hpr.tank_start[idx]
        self._last_idx = idx
        self._live = {"hp_kw": hpr.hp_kw[idx] if hpr else 0.0, "induction_kw": hpr.induction_kw[idx] if hpr else 0.0,
                      "tank_kwh": hpr.tank_end[idx] if hpr else None, "cop_space": hpr.cop_space[idx] if hpr else None,
                      "cop_dhw": hpr.cop_dhw[idx] if hpr else None, "tank_charging": bool(hpr and hpr.charging[idx]),
                      "house": real_house, "ev": ev, "total": real_house + ev, "modelled": new, "pv": pv, "battery": r, "grid": grid,
                      "import": max(grid, 0.0), "export": exp_kw,
                      "import_p": imp_p, "export_p": exp_p, "mode": mode, "idx": idx, "soc_before": soc_before}
        return self._live

    @staticmethod
    def _mode(event: str, r: float, grid: float, exp_kw: float, net: float, cheap: bool) -> str:
        if event == "power_up":
            return "Power Up: charging"
        if event == "power_down":
            return "Power Down: exporting"
        if r > 0.05:
            return "Charging (cheap grid)" if cheap and grid > 0.05 else "Charging (solar)"
        if r < -0.05:
            return "Exporting from battery" if exp_kw > 0.05 and -r > max(net, 0) + 0.05 else "Powering house"
        return "Idle"

    def _roll_buckets(self, now: datetime) -> None:
        loc = now.astimezone(self.tz)
        dk, mk = loc.strftime("%Y-%m-%d"), loc.strftime("%Y-%m")
        if self.acc.get("day_key") != dk:
            self.acc["day"], self.acc["day_key"] = _blank_bucket(), dk
        if self.acc.get("month_key") != mk:
            self.acc["month"], self.acc["month_key"] = _blank_bucket(), mk

    # ------------------------------------------------------------------ snapshot for entities
    def _snapshot(self, now: datetime, hw: Hardware, live: dict) -> dict:
        usable = max(hw.emax - hw.emin, 1e-9)
        soc = self.soc or 0.0
        out: dict[str, Any] = {
            "live": live,
            "scenario": {"name": self.entry.title, "battery_kwh": hw.battery_kwh, "pv_kwp": hw.pv_kwp,
                         "inverter_kw": hw.inverter_kw, "strategy": self.cfg[CONF_STRATEGY]},
            "octopus": {"today_kwh": self._oct_today_kwh, "official_half_hours": len(self.official)},
            "meter": {"calibration_factor": round(self.meter_k, 3), "calibration_samples": self._k_n,
                      "entity": self.cfg.get(CONF_CONSUMPTION_STAT) or None},
            "ev": {"tonight_kwh": round(sum(n.get("expected_kwh_tonight", 0) for n in (self.ev_info or [])), 1),
                   "vehicles": self.ev_info or []},
            "soc_kwh": round(soc, 2),
            "soc_pct": round(100 * soc / hw.emax, 1) if hw.emax > 0 else 0.0,
            "usable_pct": round(100 * max(0.0, soc - hw.emin) / usable, 1) if hw.emax > 0 else 0.0,
            "acc": {k: {kk: round(vv, 4) for kk, vv in v.items()} if isinstance(v, dict) else v
                    for k, v in self.acc.items()},
            "annual": self.annual, "sweep": self.sweep_rows, "sweep_info": self.sweep_info, "diag": self.diag,
            "events": [{"kind": e.kind, "start": e.start.isoformat(), "end": e.end.isoformat()}
                       for e in sorted(self._all_events(now), key=lambda e: e.start)],
        }
        hpp = self.heat_params()
        if hpp.enabled and live:
            e = live.get("tank_kwh")
            out["hp"] = {"tank_kwh": round(e, 1) if e is not None else None,
                         "tank_pct": round(100 * e / hpp.e_max, 0) if e is not None else None,
                         "tank_temp_c": (round(hpp.cold_in_c + e / (hpp.tank_litres * heatm.KWH_PER_L_K), 1)
                                         if e is not None else None)}
        out["supply"] = {"limit_kw": round(hw.import_limit_kw, 1)}
        if self.plan:
            out["plan"] = self._plan_summary(hw)
        return out

    def _plan_summary(self, hw: Hardware) -> dict:
        sl, r = self.plan.slots, self.plan.result
        n = sl.n
        pv = r.pv_kw
        cap = hw.emax if hw.emax > 0 else 1.0
        # energy the plan deliberately exports from the battery (above house demand) before next cheap start
        bexp = 0.0
        for t in range(n):
            if r.battery_kw[t] < 0:
                newk = sl.exp_new_kw[t] if sl.exp_new_kw else 0.0
                bexp += max(0.0, -r.battery_kw[t] - max(0.0, sl.exp_house_kw[t] + newk + sl.exp_ev_kw[t] - pv[t])) * SLOT_H
        # overnight target: SoC reached by the end of the next cheap block
        target, in_block = None, False
        for t in range(n):
            if sl.cheap[t] and not sl.event[t]:
                in_block = True
                target = r.soc[t]
            elif in_block:
                break
        plan_cost = r.cost_total_p / 100
        base = r.baseline_total_p / 100
        return {
            "built": self.plan.built.isoformat(),
            "planned_export_kwh": round(bexp, 2),
            "target_soc_pct": round(100 * target / cap, 1) if target is not None and hw.emax > 0 else None,
            "plan_cost_gbp": round(plan_cost, 2),
            "plan_baseline_gbp": round(base, 2),
            "plan_saving_gbp": round(base - plan_cost, 2),
            "peak_import_kw": round(r.peak_import_kw, 2),
            "fuse_pct": round(100 * r.peak_import_kw / hw.import_limit_kw, 0) if hw.import_limit_kw > 0 else None,
            "series": {
                "t": [int(s.timestamp() * 1000) for s in sl.start],
                "battery_kw": [round(x, 2) for x in r.battery_kw],
                "grid_kw": [round(x, 2) for x in r.grid_kw],
                "soc_pct": [round(100 * x / cap, 1) for x in r.soc],
                "pv_kw": [round(x, 2) for x in pv],
                "load_kw": [round(sl.house_kw[i] + sl.ev_kw[i] + (sl.new_kw[i] if sl.new_kw else 0.0), 2) for i in range(n)],
                "hp_kw": [round(x, 2) for x in (self.hp_plan.hp_kw if self.hp_plan else [0.0] * n)],
                "tank_pct": [round(100 * x / max(self.heat_params().e_max, 1e-9), 1)
                             for x in (self.hp_plan.tank_end if self.hp_plan else [0.0] * n)],
                "import_p": [round(x, 1) for x in sl.import_p],
                "export_p": [round(x, 1) for x in sl.export_p],
                "event": sl.event,
            },
        }

    # ------------------------------------------------------------------ annual + sweeps
    async def _async_load_history(self) -> None:
        c = self.cfg
        stat = (c.get(CONF_CONSUMPTION_STAT) or "").strip()
        raw_ev = c.get(CONF_EV_STATS) or []
        ev_ids = [x.strip() for x in (raw_ev.split(",") if isinstance(raw_ev, str) else raw_ev) if x and x.strip()]
        veh_stats = {v.name: v.energy_stat for v in self.vehicles if v.energy_stat}
        ev_ids = list(dict.fromkeys(ev_ids + list(veh_stats.values())))
        ids, days = ([stat] if stat else []) + ev_ids, int(c[CONF_HISTORY_DAYS])
        rows = await ha_data.cached(self.hass, ("hist", tuple(ids), days), 3600,
                                    lambda: ha_data.history_hourly(self.hass, ids, days))
        total = self._merge_official_history(rows.get(stat) if stat else None)
        ev_rows = None
        if ev_ids:
            merged: dict[datetime, float] = {}
            for i in ev_ids:
                for s, v in rows.get(i, []):
                    merged[s] = merged.get(s, 0.0) + v
            ev_rows = sorted(merged.items())
        prof, hh, he, days, last = await self.hass.async_add_executor_job(
            pipeline.build_profile, total, ev_rows, self.tz, self.tariff(c), c[CONF_NIGHT_BASE],
            c[CONF_DAILY_HOUSE], c[CONF_DAILY_EV])
        if self.vehicles and prof.source == "history":
            # cars are planned from their live state, so the yearly what-if uses your stated EV assumption, not
            # charging patterns mined from history (history is only used to take the cars OUT of the house load)
            prof.ev = load.default_profile(c[CONF_DAILY_HOUSE], c[CONF_DAILY_EV], self.tariff(c)).ev
        self.profile, self.hist = prof, (hh, he, days, last)
        await self._async_calibrate_gas()
        self.plan_dirty = True

    async def _async_calibrate_gas(self) -> None:
        c = self.cfg
        hp = self.heat_params(c)
        gas = (c.get(CONF_GAS_STAT) or "").strip()
        tstat = (c.get(CONF_OUTDOOR_STAT) or c.get(CONF_OUTDOOR_ENTITY) or "").strip()
        if not (hp.enabled and gas and tstat):
            self.gas_model = heatm.default_gas_model(hp)
            return
        days = int(c[CONF_HISTORY_DAYS])
        g = await ha_data.cached(self.hass, ("gas", gas, days), 3600, lambda: ha_data.history_hourly(self.hass, [gas], days))
        t = await ha_data.cached(self.hass, ("temp", tstat, days), 3600,
                                 lambda: ha_data.history_hourly(self.hass, [tstat], days, "mean"))

        def daily(rows, mean: bool):
            acc: dict = {}
            for s_, v in rows:
                d = s_.astimezone(self.tz).date()
                acc.setdefault(d, []).append(v)
            return {d: (sum(v) / len(v) if mean else sum(v)) for d, v in acc.items() if (mean or len(v) >= 20)}

        self.gas_model = await self.hass.async_add_executor_job(
            heatm.calibrate, daily(g.get(gas, []), False), daily(t.get(tstat, []), True), hp)

    def _year(self, c: dict) -> Slots:
        hw, tariff = self.hardware(c), self.tariff(c)
        prof = self.profile
        hh, he, days, last = self.hist
        return pipeline.make_year(
            prof, hh, he, days, last, hw=hw, tariff=tariff, tz=self.tz, lat=self.hass.config.latitude,
            lon=self.hass.config.longitude, today=dt_util.now().date(),
            heat=(self.heat_params(c), self.gas_model) if c[CONF_HP_ENABLED] else None,
            pd=(c[CONF_PD_PER_MONTH], c[CONF_PD_START_HOUR], c[CONF_PD_HOURS]),
            pu=(c[CONF_PU_PER_MONTH], c[CONF_PU_START_HOUR], c[CONF_PU_HOURS]))

    async def _async_annual(self) -> None:
        if self._annual_lock.locked():
            self.annual_dirty = True
            return
        async with self._annual_lock:
            try:
                await self._async_load_history()
                c = self.cfg
                hw, st, tariff = self.hardware(c), self.strategy(c), self.tariff(c)
                costs, terms = self.costs(c), self.terms(c)

                def job() -> dict:
                    return pipeline.annual_report(self._year(c), hw, st, tariff, costs, terms, heat_p=self.heat_params(c),
                                              tz=self.tz, gas_model=self.gas_model)

                self.annual = await self.hass.async_add_executor_job(job)
                self.annual["history_days"] = self.hist[2]
                self.annual["profile"] = f"{self.profile.source} ({self.profile.days} days)"
                self.annual_built = dt_util.utcnow()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Annual what-if failed")
            self.async_set_updated_data(self._snapshot(dt_util.utcnow(), self.hardware(), self._live))

    async def async_recalculate(self) -> None:
        self.annual_dirty = True
        self.plan_dirty = True
        await self.async_refresh()

    async def async_run_sweep(self, batteries: list[float], arrays: list[float], inverters: list[float] | None,
                              strategy: str = "rules") -> None:
        c = self.cfg
        hw, tariff = self.hardware(c), self.tariff(c)
        st = replace(self.strategy(c), name=strategy)
        costs, terms = self.costs(c), self.terms(c)
        self.sweep_info = {"running": True, "started": dt_util.utcnow().isoformat(), "strategy": st.name}
        self.async_set_updated_data(self._snapshot(dt_util.utcnow(), hw, self._live))

        def job() -> list[dict]:
            return pipeline.run_sweep(self._year(c), hw, st, tariff, batteries, arrays, inverters,
                                      heat_p=self.heat_params(c))

        try:
            self.sweep_rows = await self.hass.async_add_executor_job(job)
            self.sweep_info = {"running": False, "finished": dt_util.utcnow().isoformat(), "strategy": st.name,
                               "combinations": len(self.sweep_rows)}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Sweep failed")
            self.sweep_info = {"running": False, "error": True}
        self.async_set_updated_data(self._snapshot(dt_util.utcnow(), hw, self._live))
