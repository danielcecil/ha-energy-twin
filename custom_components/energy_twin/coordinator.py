"""The live digital twin: virtual battery + solar stepped against your real load, plus planning."""
from __future__ import annotations

import asyncio
import logging
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
from .sim import ev as evm, load, pipeline
from .sim.engine import _slot_cost, follow, plan_horizon, pv_series, settle
from .sim.finance import Costs, FinanceTerms
from .sim.models import EV_ON_KW, SLOT_H, GridEvent, Hardware, Slots, Strategy, TariffConfig
from .sim.solar import synth_pv_unit
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
            "discharge_kwh": 0.0, "load_kwh": 0.0}


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
        self.learn_live: dict[str, evm.VehicleLearn] = {}      # sessions/plug-ins observed by this twin
        self.learn_hist: dict[str, evm.VehicleLearn] = {}      # mined from recorder statistics
        self.veh_rt: dict[str, dict] = {}                      # per-car session tracking
        self.ev_info: dict = {}
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
        out = []
        for d in self.cfg.get(CONF_VEHICLES) or []:
            try:
                out.append(evm.Vehicle.from_dict(d))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def learn_for(self, name: str) -> evm.VehicleLearn:
        return evm.VehicleLearn.combine(self.learn_hist.get(name, evm.VehicleLearn()),
                                        self.learn_live.get(name, evm.VehicleLearn()))

    def vehicle_info(self, v: evm.Vehicle) -> evm.VehicleInfo:
        plug, home = self._state(v.plug_entity), self._state(v.home_entity)
        soc = ha_data.to_float(self._state(v.soc_entity))
        live = ha_data.power_kw(self._state(v.power_entity)) or 0.0
        rt = self.veh_rt.get(v.name, {})
        return evm.VehicleInfo(
            plugged=evm.interpret_plugged(plug.state if plug else None, v.plug_states),
            home=evm.interpret_home(home.state if home else None, v.home_states),
            soc_pct=soc, live_kw=live, delivered_kwh=rt.get("delivered", 0.0),
            raw_plug=plug.state if plug else None, raw_home=home.state if home else None)

    def _track_vehicles(self, now: datetime, h: float) -> None:
        """Learn only around real sessions: energy delivered while a car draws power, plug-in days."""
        today = now.astimezone(self.tz).date()
        for v in self.vehicles:
            rt = self.veh_rt.setdefault(v.name, {"delivered": 0.0, "last": None, "day": today.isoformat(),
                                                 "plugged_today": False})
            lrn = self.learn_live.setdefault(v.name, evm.VehicleLearn())
            info = self.vehicle_info(v)
            if rt["day"] != today.isoformat():                       # day rolled: record yesterday's plug-in
                y = date.fromisoformat(rt["day"])
                lrn.note_day(y.weekday(), rt["plugged_today"])
                rt["day"], rt["plugged_today"] = today.isoformat(), False
            if info.plugged and info.home is not False:
                rt["plugged_today"] = True
            if info.live_kw >= 0.3 and h > 0:
                rt["delivered"] += info.live_kw * h
                rt["last"] = now.isoformat()
                rt["plugged_today"] = True
            last = dt_util.parse_datetime(rt["last"]) if rt.get("last") else None
            idle = last is not None and (now - last) > timedelta(hours=3)
            if rt["delivered"] > 0 and (idle or info.plugged is False):    # session over
                lrn.end_session(rt["delivered"])
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
                        cycle_cost_p=c[CONF_CYCLE_COST])

    def tariff(self, cfg: dict | None = None) -> TariffConfig:
        c = cfg or self.cfg
        return TariffConfig(cheap_start_min=_mins(c[CONF_CHEAP_START]), cheap_end_min=_mins(c[CONF_CHEAP_END]),
                            cheap_p=c[CONF_CHEAP_P], standard_p=c[CONF_STANDARD_P], export_p=c[CONF_EXPORT_P],
                            power_up_import_p=c[CONF_PU_IMPORT_P], power_down_export_p=c[CONF_PD_EXPORT_P],
                            power_down_bonus_p=c[CONF_PD_BONUS_P])

    def strategy(self, cfg: dict | None = None) -> Strategy:
        c = cfg or self.cfg
        return Strategy(name=c[CONF_STRATEGY], margin_pct=c[CONF_MARGIN], export_from_min=_mins(c[CONF_EXPORT_FROM]),
                        export_margin_p=c[CONF_EXPORT_MARGIN], arbitrage=c[CONF_ARBITRAGE],
                        allow_battery_export=c[CONF_BATT_EXPORT])

    def costs(self, c: dict | None = None) -> Costs:
        c = c or self.cfg
        return Costs(c[CONF_PV_COST_FIXED], c[CONF_PV_COST_KWP], c[CONF_BATT_COST_FIXED], c[CONF_BATT_COST_KWH],
                     c[CONF_OTHER_COST])

    def terms(self, c: dict | None = None) -> FinanceTerms:
        c = c or self.cfg
        return FinanceTerms(c[CONF_DEPOSIT], c[CONF_APR], int(c[CONF_TERM]), c[CONF_ESCALATION], c[CONF_PV_DEG],
                            c[CONF_BATT_DEG], c[CONF_DISCOUNT], int(c[CONF_HORIZON]))

    # ------------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        saved = await self._store.async_load() or {}
        hw = self.hardware()
        self.soc = saved.get("soc", hw.emin)
        self.acc = {**self.acc, **saved.get("acc", {})}
        self.learn_live = {k: evm.VehicleLearn.from_dict(v) for k, v in (saved.get("learn") or {}).items()}
        self.veh_rt = saved.get("veh_rt") or {}
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

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {
            "soc": self.soc, "last": self.last_tick.isoformat() if self.last_tick else None, "acc": self.acc,
            "learn": {k: v.to_dict() for k, v in self.learn_live.items()}, "veh_rt": self.veh_rt,
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

    async def async_reset_twin(self) -> None:
        self.soc = self.hardware().emin
        self.acc = {"day_key": "", "month_key": "", "day": _blank_bucket(), "month": _blank_bucket(),
                    "total": _blank_bucket()}
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

    def _scale_pv(self, kw_ref: float) -> float:
        """kW from a forecast built for the reference array -> kW per kWp."""
        return kw_ref / max(self.cfg[CONF_FORECAST_REF_KWP], 0.01)

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
        imp_rates = ha_data.parse_rates(self._states(c.get(CONF_IMPORT_RATES_ENTITIES))) or None
        exp_rates = ha_data.parse_rates(self._states(c.get(CONF_EXPORT_RATES_ENTITIES))) or None
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

        fe = c.get(CONF_FORECAST_ENTRY)
        fc = await ha_data.cached(self.hass, ("fc", fe), 240, lambda: ha_data.forecast_pv_kw(self.hass, fe))
        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        _, synth = synth_pv_unit(starts, lat, lon, hw.pv_annual_yield)
        pv_unit, from_fc = [], 0
        for i, s in enumerate(starts):
            if s in fc:
                pv_unit.append(self._scale_pv(fc[s]))
                from_fc += 1
            else:
                pv_unit.append(synth[i])
        live_pv = ha_data.power_kw(self._state(c.get(CONF_PV_NOW_ENTITY)))
        if live_pv is not None:
            pv_unit[0] = self._scale_pv(live_pv)

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
            evk, self.ev_info = evm.build_ev_load(starts, tod, self.tz, tariff, vehicles, infos,
                                                  {v.name: self.learn_for(v.name) for v in vehicles}, disp_slots)
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
        hw_soc = self.soc if self.soc is not None else hw.emin
        res = await self.hass.async_add_executor_job(plan_horizon, sl, hw, st, hw_soc)
        self.diag = {
            "import_rate_slots": len(imp_rates or {}), "export_rate_slots": len(exp_rates or {}),
            "dispatches": len(dispatches), "events_active_or_upcoming": len(events),
            "forecast_slots_used": from_fc, "forecast_slots_total": len(starts),
            "load_profile": f"{self.profile.source} ({self.profile.days} days)",
            "horizon_slots": len(starts), "live_price_entity": live_imp is not None,
            "live_pv_entity": live_pv is not None,
            "vehicles": self.ev_info,
        }
        return Plan(sl, res, now, hw_soc)

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
            self.soc = hw.emin
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
        total = ha_data.power_kw(self._state(c.get(CONF_POWER_ENTITY)))
        veh = self.vehicles
        ev_states = ([self.vehicle_info(v).live_kw for v in veh if v.power_entity] if veh else []) or \
                    [ha_data.power_kw(s) for s in self._states(c.get(CONF_EV_POWER_ENTITIES))]
        ev = sum(v for v in ev_states if v) if ev_states else (sl.ev_kw[idx] if total is None else 0.0)
        house = max(0.0, total - ev) if total is not None else sl.house_kw[idx]
        pv = pv_plan[idx]
        r_plan = plan.battery_kw[idx]
        r_req = follow(r_plan, sl, idx, house, ev, pv, sl.exp_house_kw[idx], pv_plan[idx])
        dt_s = (now - self.last_tick).total_seconds() if self.last_tick else 0.0
        self.last_tick = now
        self._track_vehicles(now, dt_s / 3600.0 if 0 < dt_s <= MAX_GAP_S else 0.0)
        soc_before = self.soc
        self.soc, r = settle(hw, self.soc, r_req if hw.battery_kwh > 0 else 0.0, ev >= EV_ON_KW,
                             hw.discharge_cap(pv))
        grid = house + ev - pv + r
        imp_p, exp_p, bonus = sl.import_p[idx], sl.export_p[idx], sl.bonus_p[idx]
        cost_p, exp_kw = _slot_cost(hw, grid, r, imp_p, exp_p, bonus)
        actual_per_h = (house + ev) * imp_p                    # what you really pay right now (p/h)
        if 0 < dt_s <= MAX_GAP_S:
            h = dt_s / 3600.0
            self._roll_buckets(now)
            for b in (self.acc["day"], self.acc["month"], self.acc["total"]):
                b["actual_p"] += actual_per_h * h
                b["twin_p"] += cost_p / SLOT_H * h
                b["pv_kwh"] += pv * h
                b["export_kwh"] += exp_kw * h
                b["load_kwh"] += (house + ev) * h
                b["grid_charge_kwh"] += max(0.0, r - max(0.0, -(house + ev - pv))) * h if r > 0 else 0.0
                b["discharge_kwh"] += -r * h if r < 0 else 0.0
        mode = self._mode(sl.event[idx], r, grid, exp_kw, house + ev - pv, sl.cheap[idx])
        self._live = {"house": house, "ev": ev, "pv": pv, "battery": r, "grid": grid, "export": exp_kw,
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
                bexp += max(0.0, -r.battery_kw[t] - max(0.0, sl.exp_house_kw[t] + sl.exp_ev_kw[t] - pv[t])) * SLOT_H
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
            "series": {
                "t": [int(s.timestamp() * 1000) for s in sl.start],
                "battery_kw": [round(x, 2) for x in r.battery_kw],
                "grid_kw": [round(x, 2) for x in r.grid_kw],
                "soc_pct": [round(100 * x / cap, 1) for x in r.soc],
                "pv_kw": [round(x, 2) for x in pv],
                "load_kw": [round(sl.house_kw[i] + sl.ev_kw[i], 2) for i in range(n)],
                "import_p": [round(x, 1) for x in sl.import_p],
                "export_p": [round(x, 1) for x in sl.export_p],
                "event": sl.event,
            },
        }

    # ------------------------------------------------------------------ annual + sweeps
    async def _async_load_history(self) -> None:
        c = self.cfg
        stat = (c.get(CONF_CONSUMPTION_STAT) or "").strip()
        ev_ids = [s.strip() for s in (c.get(CONF_EV_STATS) or "").split(",") if s.strip()]
        veh_stats = {v.name: v.energy_stat for v in self.vehicles if v.energy_stat}
        ev_ids = list(dict.fromkeys(ev_ids + list(veh_stats.values())))
        ids, days = ([stat] if stat else []) + ev_ids, int(c[CONF_HISTORY_DAYS])
        rows = await ha_data.cached(self.hass, ("hist", tuple(ids), days), 3600,
                                    lambda: ha_data.history_hourly(self.hass, ids, days))
        total = rows.get(stat) if stat else None
        self.learn_hist = {n: evm.learn_from_rows(rows.get(sid, []), self.tz) for n, sid in veh_stats.items()
                           if rows.get(sid)}
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
        self.profile, self.hist = prof, (hh, he, days, last)
        self.plan_dirty = True

    def _year(self, c: dict) -> Slots:
        hw, tariff = self.hardware(c), self.tariff(c)
        prof = self.profile
        hh, he, days, last = self.hist
        return pipeline.make_year(
            prof, hh, he, days, last, hw=hw, tariff=tariff, tz=self.tz, lat=self.hass.config.latitude,
            lon=self.hass.config.longitude, today=dt_util.now().date(),
            vehicles=self.vehicles or None, learns={v.name: self.learn_for(v.name) for v in self.vehicles},
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
                    return pipeline.annual_report(self._year(c), hw, st, tariff, costs, terms)

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
            return pipeline.run_sweep(self._year(c), hw, st, tariff, costs, terms, batteries, arrays, inverters)

        try:
            self.sweep_rows = await self.hass.async_add_executor_job(job)
            self.sweep_info = {"running": False, "finished": dt_util.utcnow().isoformat(), "strategy": st.name,
                               "combinations": len(self.sweep_rows)}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Sweep failed")
            self.sweep_info = {"running": False, "error": True}
        self.async_set_updated_data(self._snapshot(dt_util.utcnow(), hw, self._live))
