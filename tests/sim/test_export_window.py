"""Battery power goes to the grid only in the late-evening window - never overnight while the grid is cheap."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_twin.sim import load
from custom_components.energy_twin.sim.engine import export_allowed, plan_horizon, simulate
from custom_components.energy_twin.sim.models import GridEvent, Hardware, Slots, Strategy, TariffConfig, in_export_window
from custom_components.energy_twin.sim.tariff import build_prices, make_starts, tod_minutes

TZ = ZoneInfo("Europe/London")
CFG = TariffConfig()
HW = Hardware(pv_kwp=0, battery_kwh=13.5, battery_max_kw=10, inverter_kw=10, round_trip_eff=0.89)
USABLE = HW.emax - HW.emin
NAMES = ("optimised", "rules")


def live_style_horizon(events=None, car=False):
    """33 hours from 14:00, like the live plan. `car=False` = no car on charge tonight (the case that exposed it)."""
    n = 66
    starts = make_starts(datetime(2026, 11, 10, 14, 0, tzinfo=TZ).astimezone(timezone.utc), n)
    tod = tod_minutes(starts, TZ)
    imp, exp, cheap, ev, bonus = build_prices(starts, tod, CFG, events=events)
    prof = load.default_profile(9.0, 12.0, CFG)
    house = [prof.house(s.astimezone(TZ).date(), tod[i] // 30) * load.SEASONAL[10] for i, s in enumerate(starts)]
    evk = [prof.ev[tod[i] // 30] if car else 0.0 for i in range(n)]
    return Slots(starts, tod, imp, exp, cheap, ev, bonus, house, evk, [0.0] * n, list(house), list(evk), [0.0] * n,
                 [""] * n)


def night_exports(res, sl):
    return [i for i in range(sl.n) if CFG.in_cheap_window(sl.tod_min[i]) and res.export_kw[i] > 0.05 and res.battery_kw[i] < 0]


@pytest.mark.parametrize("name", NAMES)
def test_no_overnight_export_and_about_one_cycle(name):
    sl = live_style_horizon()
    res = plan_horizon(sl, HW, Strategy(name=name), HW.emin)
    assert night_exports(res, sl) == []
    # the old behaviour discharged ~22.6 kWh here (two full cycles); a normal day is about one
    assert res.battery_discharge_kwh <= USABLE * 1.15, res.battery_discharge_kwh


@pytest.mark.parametrize("name", NAMES)
def test_battery_export_only_in_window_over_a_year(name):
    prof = load.default_profile(9.0, 0.0, CFG)                              # no car, so nothing blocks night discharge
    sl = load.build_year(hw_yield=950, profile=prof, cfg=CFG, tz=TZ, lat=52.5, lon=-1.5, start_day=date(2026, 1, 1), days=120)
    st = Strategy(name=name)
    res = simulate(sl, HW, st, chunk_tod_min=CFG.cheap_start_min)
    bad = [i for i in range(sl.n) if res.battery_kw[i] < 0 and res.export_kw[i] > 0.05
           and not (in_export_window(sl.tod_min[i], st) and not sl.cheap[i])]
    assert bad == []
    assert res.battery_discharge_kwh / USABLE / (sl.n / 48) <= 1.15       # cycles per day
    assert res.battery_export_kwh > 0                                      # ...but the evening export still happens


@pytest.mark.parametrize("name", NAMES)
def test_power_down_event_overrides_the_window_unless_told_not_to(name):
    ev = [GridEvent("power_down", datetime(2026, 11, 11, 1, 0, tzinfo=TZ).astimezone(timezone.utc),
                    datetime(2026, 11, 11, 3, 0, tzinfo=TZ).astimezone(timezone.utc))]
    sl = live_style_horizon(events=ev)
    idx = [i for i, e in enumerate(sl.event) if e == "power_down"]
    honoured = plan_horizon(sl, HW, Strategy(name=name), HW.emin)
    assert sum(honoured.export_kw[i] for i in idx) * 0.5 > 3.0            # paid event: exports at night, by design
    strict = plan_horizon(sl, HW, Strategy(name=name, event_export_anywhere=False), HW.emin)
    assert sum(strict.export_kw[i] for i in idx) * 0.5 < 0.5              # strict mode keeps to the window


def test_window_maths_and_master_switch():
    st = Strategy()
    assert in_export_window(18 * 60, st) and in_export_window(23 * 60, st) and not in_export_window(23 * 60 + 30, st)
    assert not in_export_window(2 * 60, st) and not in_export_window(12 * 60, st)
    wrap = Strategy(export_from_min=22 * 60, export_until_min=1 * 60)       # a window that crosses midnight
    assert in_export_window(23 * 60, wrap) and in_export_window(30, wrap) and not in_export_window(2 * 60, wrap)
    sl = live_style_horizon()
    i = next(k for k in range(sl.n) if sl.tod_min[k] == 20 * 60)            # 20:00, not cheap
    assert export_allowed(sl, st, i) and not export_allowed(sl, replace(st, allow_battery_export=False), i)
    j = next(k for k in range(sl.n) if sl.tod_min[k] == 2 * 60)             # 02:00, cheap
    assert not export_allowed(sl, st, j)
