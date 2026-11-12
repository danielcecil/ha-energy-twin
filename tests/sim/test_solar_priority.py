"""Solar fills the battery (up to its charge rate / remaining room) before any is exported."""
from dataclasses import replace
from datetime import date

import pytest

from custom_components.energy_twin.sim import load
from custom_components.energy_twin.sim.engine import simulate
from custom_components.energy_twin.sim.models import Hardware, Strategy, TariffConfig
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/London")
CFG = TariffConfig()
HW = Hardware(pv_kwp=8, battery_kwh=13.5, battery_max_kw=5, inverter_kw=8, round_trip_eff=0.89, export_limit_kw=3.68)


def year(start=date(2026, 4, 1), days=90):
    prof = load.default_profile(9.0, 0.0, CFG)
    return load.build_year(hw_yield=950, profile=prof, cfg=CFG, tz=TZ, lat=52.5, lon=-1.5, start_day=start, days=days)


def violations(sl, res, hw=HW):
    """Slots where solar was exported although the battery could still have taken it (rate and room)."""
    bad, soc_prev = [], hw.emin
    for i in range(sl.n):
        surplus = res.pv_kw[i] - sl.house_kw[i] - sl.ev_kw[i]
        room_kw = (hw.emax - soc_prev) / (hw.eta * 0.5)
        take = min(surplus, hw.charge_kw, room_kw)
        if surplus > 0.3 and res.export_kw[i] > 0.05 and res.battery_kw[i] > -0.05 and res.battery_kw[i] < take - 0.6:
            bad.append((i, round(surplus, 2), round(res.battery_kw[i], 2), round(res.export_kw[i], 2)))
        soc_prev = res.soc[i]
    return bad


@pytest.mark.parametrize("name", ("optimised", "rules"))
def test_battery_takes_the_solar_before_the_grid_does(name):
    sl = year()
    res = simulate(sl, HW, Strategy(name=name, solar_battery_first=True), chunk_tod_min=CFG.cheap_start_min)
    assert violations(sl, res) == []
    assert res.export_kwh > 50                                    # it still exports once the battery is full / at its rate


def test_the_rule_costs_almost_nothing_and_can_be_switched_off():
    sl = year()
    on = simulate(sl, HW, Strategy(name="optimised", solar_battery_first=True), chunk_tod_min=CFG.cheap_start_min)
    off = simulate(sl, HW, Strategy(name="optimised", solar_battery_first=False), chunk_tod_min=CFG.cheap_start_min)
    assert on.saving_gbp >= off.saving_gbp * 0.97, (on.saving_gbp, off.saving_gbp)
