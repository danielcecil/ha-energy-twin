"""History -> load profile -> annual report. Glue between raw statistics and the engine."""
from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import annual, ev as evm, heat as heatm, load
from .finance import Costs, FinanceTerms, evaluate_finance
from .models import GridEvent, Hardware, Slots, Strategy, TariffConfig

UTC = timezone.utc
MIN_PROFILE_DAYS = 14
FULL_YEAR_DAYS = 330


def build_profile(
    total_rows: list[tuple[datetime, float]] | None,
    ev_rows: list[tuple[datetime, float]] | None,
    tz: ZoneInfo,
    tariff: TariffConfig,
    night_base_kw: float,
    default_house_kwh: float,
    default_ev_kwh: float,
):
    """Return (profile, house_hist, ev_hist, hist_days, last_hist_date)."""
    if not total_rows:
        return load.default_profile(default_house_kwh, default_ev_kwh, tariff), None, None, 0, None
    total = load.hourly_to_slots(total_rows)
    ev = load.hourly_to_slots(ev_rows) if ev_rows else None
    house, evs = load.split_ev(total, ev, tz, tariff, night_base_kw)
    days = load.covered_days(total, tz)
    last = max(total).astimezone(tz).date()
    if days < MIN_PROFILE_DAYS:
        return load.default_profile(default_house_kwh, default_ev_kwh, tariff), None, None, days, last
    return load.learn_profile(house, evs, tz), house, evs, days, last


def synthetic_events(start_day: date, days: int, tz: ZoneInfo, kind: str, per_month: float,
                     start_hour: float, hours: float) -> list[GridEvent]:
    """Spread `per_month` assumed events evenly through each month (annual what-if only)."""
    out: list[GridEvent] = []
    if per_month <= 0:
        return out
    d = start_day
    end = start_day + timedelta(days=days)
    while d < end:
        dim = calendar.monthrange(d.year, d.month)[1]
        n = int(round(per_month))
        for k in range(n):
            day = (k + 1) * dim // (n + 1) or 1
            ed = date(d.year, d.month, min(day, dim))
            if start_day <= ed < end:
                s = datetime(ed.year, ed.month, ed.day, tzinfo=tz) + timedelta(hours=start_hour)
                out.append(GridEvent(kind, s.astimezone(UTC), (s + timedelta(hours=hours)).astimezone(UTC)))
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def make_year(profile, hist_house, hist_ev, hist_days, last_hist, *, hw: Hardware, tariff: TariffConfig,
              tz: ZoneInfo, lat: float, lon: float, today: date, pd: tuple, pu: tuple,
              vehicles: list | None = None, learns: dict | None = None,
              heat: tuple | None = None) -> Slots:
    """pd / pu = (per_month, start_hour, hours) for assumed Power Down / Power Up events."""
    if hist_house is not None and hist_days >= FULL_YEAR_DAYS and last_hist:
        start = last_hist - timedelta(days=364)
    else:
        start, hist_house, hist_ev = today, None, None
    events = (synthetic_events(start, 365, tz, "power_down", *pd)
              + synthetic_events(start, 365, tz, "power_up", *pu))
    real_history = hist_house is not None
    sl = load.build_year(hw_yield=hw.pv_annual_yield, profile=profile, cfg=tariff, tz=tz, lat=lat, lon=lon,
                         start_day=start, days=365, history_house=hist_house, history_ev=hist_ev, events=events)
    if vehicles:
        # per-car model: the planner's expectation is probability-weighted; the "actual" year is your real
        # EV history if you have a full year of it, otherwise a lumpy-but-deterministic charging pattern
        act, exp = evm.year_load(sl.start, sl.tod_min, tz, tariff, vehicles, learns or {})
        sl.exp_ev_kw = exp
        if not real_history:
            sl.ev_kw = act
    if heat and heat[0].enabled:
        hp, gm = heat
        act_t, exp_t = heatm.synth_temps(sl.start, tz)
        win = [tariff.in_cheap_window(t) for t in sl.tod_min]
        opp = [sl.cheap[i] and (not win[i] or sl.event[i] == "power_up") for i in range(sl.n)]
        loc = [x.astimezone(tz) for x in sl.start]
        wd, dk = [d.weekday() for d in loc], [d.date() for d in loc]
        a = heatm.simulate_heat(sl.tod_min, sl.cheap, opp, wd, dk, act_t, hp, gm)
        e = heatm.simulate_heat(sl.tod_min, sl.cheap, opp, wd, dk, exp_t, hp, gm)
        sl.new_kw, sl.exp_new_kw = a.new_kw, e.new_kw
        sl.heat = heatm.summarise(a, sl.cheap, hp)
        sl.heat["gas_model"] = {"source": gm.source, "base_kwh_day": round(gm.base_kwh_day, 2),
                                "k_per_degday": round(gm.k_per_degday, 2), "days": gm.days, "r2": round(gm.r2, 2)}
    return sl


def _hp_economics(sl: Slots, hp: "heatm.HeatParams", f: float) -> dict:
    """Gas avoided vs the extra grid electricity (priced slot by slot, with no solar/battery)."""
    grid = sum(sl.new_kw[i] * 0.5 * sl.import_p[i] for i in range(sl.n)) / 100.0 * f
    gas = sl.heat["gas_kwh_displaced"] * hp.gas_unit_p / 100.0
    standing = hp.gas_standing_p_day * 365 / 100.0 if hp.gas_disconnect else 0.0
    return {"grid_cost_nokit": round(grid, 2), "gas_cost_avoided": round(gas, 2), "gas_standing_avoided": round(standing, 2),
            "net_hp_saving": round(gas + standing - grid, 2)}


def annual_report(sl: Slots, hw: Hardware, st: Strategy, tariff: TariffConfig, costs: Costs,
                  terms: FinanceTerms, attribution: bool = True, heat_p: "heatm.HeatParams | None" = None,
                  tz: ZoneInfo | None = None, gas_model: "heatm.GasModel | None" = None) -> dict:
    has_hp = bool(sl.heat) and heat_p is not None and heat_p.enabled
    a = annual.evaluate(sl, hw, st, tariff.cheap_start_min, attribution=attribution, baseline_with_new=has_hp)
    f = 365.0 / a.days
    pv_sav, batt_sav = annual.split_savings(a)
    econ = _hp_economics(sl, heat_p, f) if has_hp else None
    hp_sav = econ["net_hp_saving"] if econ else 0.0
    capex = costs.total(hw.pv_kwp, hw.battery_kwh)
    fin = evaluate_finance(capex, pv_sav, batt_sav, terms, hp_sav)
    # the whole annual bill (electricity + gas): today vs with this setup - the easiest thing to compare
    elec_today = sum((sl.house_kw[i] + sl.ev_kw[i]) * 0.5 * sl.import_p[i] for i in range(sl.n)) / 100.0 * f
    elec_with = a.scenario_gbp * f
    bills: dict = {"elec_today": round(elec_today, 2), "elec_with": round(elec_with, 2),
                   "bill_today": None, "bill_with": None, "gas_today": None, "gas_with": None}
    if heat_p is not None and gas_model is not None and tz is not None:
        gas_kwh = heatm.annual_gas_kwh(sl.start, sl.tod_min, tz, heat_p, gas_model)
        standing = heat_p.gas_standing_p_day * 365 / 100.0
        gas_today = gas_kwh * heat_p.gas_unit_p / 100.0 + standing
        gas_with = (0.0 if heat_p.gas_disconnect else standing) if has_hp else gas_today
        bills.update({"gas_today": round(gas_today, 2), "gas_with": round(gas_with, 2), "gas_kwh_today": round(gas_kwh),
                      "bill_today": round(elec_today + gas_today, 2), "bill_with": round(elec_with + gas_with, 2),
                      "gas_is_estimate": gas_model.source != "history"})
    return {
        **bills,
        "saving_year1": round(a.saving_gbp * f + hp_sav, 2),
        "hp_saving": round(hp_sav, 2),
        "heat": ({**sl.heat, **econ} if econ else None),
        "peak_import_kw": round(a.peak_import_kw, 2),
        "baseline_year1": round(a.baseline_gbp * f, 2),
        "scenario_year1": round(a.scenario_gbp * f, 2),
        "pv_saving": round(pv_sav, 2),
        "battery_saving": round(batt_sav, 2),
        "pv_generation_kwh": round(a.pv_kwh * f),
        "import_kwh": round(a.import_kwh * f),
        "export_kwh": round(a.export_kwh * f),
        "grid_charge_kwh": round(a.grid_charge_kwh * f),
        "battery_export_kwh": round(a.battery_export_kwh * f),
        "battery_cycles": round(a.cycles * f),
        "monthly": a.monthly,
        "finance": fin,
        "strategy": st.name,
        "days_simulated": a.days,
    }


def run_sweep(sl: Slots, hw: Hardware, st: Strategy, tariff: TariffConfig, batteries: list[float],
              arrays: list[float], inverters: list[float] | None,
              heat_p: "heatm.HeatParams | None" = None) -> list[dict]:
    has_hp = bool(sl.heat) and heat_p is not None and heat_p.enabled
    hp_sav = _hp_economics(sl, heat_p, 1.0)["net_hp_saving"] if has_hp else 0.0
    return annual.sweep(lambda _y: sl, hw, st, tariff.cheap_start_min, batteries, arrays, inverters,
                        baseline_with_new=has_hp, hp_saving=hp_sav)
