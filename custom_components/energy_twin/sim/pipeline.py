"""History -> load profile -> annual report. Glue between raw statistics and the engine."""
from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import annual, load
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
              tz: ZoneInfo, lat: float, lon: float, today: date, pd: tuple, pu: tuple) -> Slots:
    """pd / pu = (per_month, start_hour, hours) for assumed Power Down / Power Up events."""
    if hist_house is not None and hist_days >= FULL_YEAR_DAYS and last_hist:
        start = last_hist - timedelta(days=364)
    else:
        start, hist_house, hist_ev = today, None, None
    events = (synthetic_events(start, 365, tz, "power_down", *pd)
              + synthetic_events(start, 365, tz, "power_up", *pu))
    return load.build_year(hw_yield=hw.pv_annual_yield, profile=profile, cfg=tariff, tz=tz, lat=lat, lon=lon,
                           start_day=start, days=365, history_house=hist_house, history_ev=hist_ev, events=events)


def annual_report(sl: Slots, hw: Hardware, st: Strategy, tariff: TariffConfig, costs: Costs,
                  terms: FinanceTerms, attribution: bool = True) -> dict:
    a = annual.evaluate(sl, hw, st, tariff.cheap_start_min, attribution=attribution)
    f = 365.0 / a.days
    pv_sav, batt_sav = annual.split_savings(a)
    capex = costs.total(hw.pv_kwp, hw.battery_kwh)
    fin = evaluate_finance(capex, pv_sav, batt_sav, terms)
    return {
        "saving_year1": round(a.saving_gbp * f, 2),
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


def run_sweep(sl: Slots, hw: Hardware, st: Strategy, tariff: TariffConfig, costs: Costs, terms: FinanceTerms,
              batteries: list[float], arrays: list[float], inverters: list[float] | None) -> list[dict]:
    return annual.sweep(lambda _y: sl, hw, st, tariff.cheap_start_min, costs, terms,
                        batteries=batteries, arrays=arrays, inverters=inverters)
