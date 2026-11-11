"""Cars are planned from their state RIGHT NOW - no statistics, no weekday guesses."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_twin.sim import ev
from custom_components.energy_twin.sim.models import SLOT_H, TariffConfig
from custom_components.energy_twin.sim.tariff import make_starts, tod_minutes

TZ = ZoneInfo("Europe/London")
CFG = TariffConfig()


def horizon(start_local=datetime(2026, 11, 9, 18, 0, tzinfo=TZ), n=72):
    starts = make_starts(start_local.astimezone(timezone.utc), n)
    return starts, tod_minutes(starts, TZ)


def plan(vehicles, infos, disp=frozenset(), **kw):
    starts, tod = horizon(**kw)
    kw_, notes = ev.build_ev_load(starts, tod, TZ, CFG, vehicles, infos, set(disp))
    return kw_, notes, starts, tod


def kwh(series, a=0, b=None):
    return sum(series[a:b]) * SLOT_H


@pytest.mark.parametrize("raw,exp", [
    ("on", True), ("Connected", True), ("charging", True), ("stopped", True), ("complete", True), ("no_power", True),
    ("disconnected", False), ("Not connected", False), ("off", False), ("unplugged", False),
    ("unknown", None), (None, None), ("banana", None), ("Connected: waiting for car demand", True),
])
def test_interpret_plugged(raw, exp):
    assert ev.interpret_plugged(raw) is exp


def test_interpret_overrides_and_home():
    assert ev.interpret_plugged("ready_to_go", ("ready_to_go",)) is True
    assert ev.interpret_plugged("connected", ("ready_to_go",)) is False
    assert ev.interpret_home("home") is True and ev.interpret_home("not_home") is False
    assert ev.interpret_home("Work") is False and ev.interpret_home("unavailable") is None
    assert ev.interpret_home("Garage", ("garage", "home")) is True


def test_needed_energy_comes_from_the_cars_own_level_and_limit():
    v = ev.Vehicle("A", capacity_kwh=60, target_soc_pct=80)
    assert ev.needed_kwh(v, ev.VehicleInfo(soc_pct=30)) == pytest.approx(30 / ev.CHARGE_EFF)
    assert ev.needed_kwh(v, ev.VehicleInfo(soc_pct=30, target_soc_pct=100)) == pytest.approx(42 / ev.CHARGE_EFF)  # charge limit wins
    assert ev.needed_kwh(v, ev.VehicleInfo(soc_pct=90)) == 0.0
    assert ev.needed_kwh(v, ev.VehicleInfo()) is None                          # no level, no session size: unknown
    assert ev.needed_kwh(replace_session(v, 12), ev.VehicleInfo(delivered_kwh=5)) == pytest.approx(7.0)


def replace_session(v, kwh_):
    from dataclasses import replace
    return replace(v, session_kwh=kwh_)


def test_plugged_in_car_charges_exactly_what_it_needs_from_the_start_of_the_off_peak_window():
    v = ev.Vehicle("B", mode="offpeak", charger_kw=7.0, capacity_kwh=60, target_soc_pct=80)
    kw, notes, starts, tod = plan([v], {"B": ev.VehicleInfo(plugged=True, home=True, soc_pct=50)})
    on = [i for i, x in enumerate(kw) if x > 0]
    assert on and all(CFG.in_cheap_window(tod[i]) for i in on) and tod[on[0]] == CFG.cheap_start_min
    assert max(kw) == pytest.approx(7.0) and kwh(kw) == pytest.approx(18 / ev.CHARGE_EFF)
    assert notes[0]["planned_kwh"] == notes[0]["needed_kwh"] and "charging what it still needs" in notes[0]["why"]


def test_car_left_plugged_in_at_its_limit_adds_nothing():
    """'Sometimes I leave the car plugged in for days so there's nothing left to charge overnight.'"""
    v = ev.Vehicle("A", capacity_kwh=60, target_soc_pct=80)
    kw, notes, *_ = plan([v], {"A": ev.VehicleInfo(plugged=True, home=True, soc_pct=80, raw_plug="on")})
    assert sum(kw) == 0 and notes[0]["needed_kwh"] == 0 and "already at its charge limit" in notes[0]["why"]
    kw, notes, *_ = plan([v], {"A": ev.VehicleInfo(plugged=True, home=True, soc_pct=95)})
    assert sum(kw) == 0                                                         # above the limit: still nothing


def test_unplugged_or_away_cars_are_not_planned_at_all():
    v = ev.Vehicle("A")
    for info, text in ((ev.VehicleInfo(plugged=False, home=True, soc_pct=20), "not plugged in at home"),
                       (ev.VehicleInfo(plugged=True, home=False, soc_pct=20), "away from home"),
                       (ev.VehicleInfo(plugged=False, home=False, soc_pct=20), "not plugged in at home"),
                       (ev.VehicleInfo(), "not plugged in at home")):
        kw, notes, *_ = plan([v], {"A": info})
        assert sum(kw) == 0 and text in notes[0]["why"]                         # no weekday guess, no 'maybe tonight'
    # an unknown 'home' status with the car plugged in counts as home
    kw, _, *_ = plan([v], {"A": ev.VehicleInfo(plugged=True, home=None, soc_pct=20)})
    assert sum(kw) > 0


def test_two_cars_with_different_levels_each_get_their_own_plan():
    cars = [ev.Vehicle("Y", mode="iog", capacity_kwh=75, target_soc_pct=80, charger_kw=7.0),
            ev.Vehicle("3", mode="offpeak", capacity_kwh=60, target_soc_pct=100, charger_kw=7.0)]
    infos = {"Y": ev.VehicleInfo(plugged=True, home=True, soc_pct=64), "3": ev.VehicleInfo(plugged=True, home=True, soc_pct=78)}
    _, notes, *_ = plan(cars, infos)
    n = {x["vehicle"]: x for x in notes}
    assert n["Y"]["needed_kwh"] == round((80 - 64) / 100 * 75 / 0.9, 1) and n["3"]["needed_kwh"] == round((100 - 78) / 100 * 60 / 0.9, 1)
    assert n["Y"]["planned_kwh"] == n["Y"]["needed_kwh"] and n["3"]["planned_kwh"] == n["3"]["needed_kwh"]
    # tomorrow it is a different story: the Y was driven and the 3 is left alone - the plan follows the new levels
    infos2 = {"Y": ev.VehicleInfo(plugged=True, home=True, soc_pct=30), "3": ev.VehicleInfo(plugged=True, home=True, soc_pct=100)}
    _, notes2, *_ = plan(cars, infos2)
    n2 = {x["vehicle"]: x for x in notes2}
    assert n2["Y"]["needed_kwh"] > n["Y"]["needed_kwh"] and n2["3"]["planned_kwh"] == 0


def test_iog_car_uses_dispatch_slots_offpeak_car_ignores_them():
    disp = {horizon()[0][i] for i in (4, 5, 6)}                                 # 20:00-21:30: outside the window
    infos = {n: ev.VehicleInfo(plugged=True, home=True, soc_pct=60) for n in "AB"}
    iog = ev.Vehicle("A", mode="iog", capacity_kwh=60, target_soc_pct=80)
    off = ev.Vehicle("B", mode="offpeak", capacity_kwh=60, target_soc_pct=80)
    ka, na, *_ = plan([iog], infos, disp)
    kb, nb, *_ = plan([off], infos, disp)
    assert all(ka[i] > 0 for i in (4, 5, 6)) and all(kb[i] == 0 for i in (4, 5, 6))
    assert kwh(ka) == pytest.approx(kwh(kb)) and na[0]["via_dispatch_kwh"] > 0 and nb[0]["via_dispatch_kwh"] == 0


def test_a_big_top_up_carries_on_into_the_next_off_peak_window():
    v = ev.Vehicle("A", charger_kw=3.6, capacity_kwh=80, target_soc_pct=100)    # 0 -> 100 % is ~89 kWh at 3.6 kW
    kw, notes, starts, tod = plan([v], {"A": ev.VehicleInfo(plugged=True, home=True, soc_pct=10)}, n=96)
    per_window = 12 * SLOT_H * 3.6                                              # 21.6 kWh in one 6 h window
    assert notes[0]["needed_kwh"] > per_window * 2
    assert notes[0]["planned_kwh"] == pytest.approx(per_window * 2, abs=0.2)    # both windows in the horizon are used
    assert sum(1 for i in range(96) if kw[i] > 0 and tod[i] == CFG.cheap_start_min) == 2


def test_measured_power_wins_for_now_and_unknown_levels_are_handled_honestly():
    starts, tod = horizon(datetime(2026, 11, 9, 23, 45, tzinfo=TZ), 24)         # mid-window
    v = ev.Vehicle("A", mode="offpeak", charger_kw=7.0)
    kw, notes = ev.build_ev_load(starts, tod, TZ, CFG, [v], {"A": ev.VehicleInfo(plugged=True, home=True, soc_pct=40, live_kw=6.8)}, set())
    assert kw[0] >= 6.8
    # no battery level, drawing power -> assume it keeps going through this off-peak window
    kw, notes = ev.build_ev_load(starts, tod, TZ, CFG, [v], {"A": ev.VehicleInfo(plugged=True, home=True, live_kw=6.8)}, set())
    assert sum(kw) > 6.8 * SLOT_H * 4 and "assuming it keeps charging" in notes[0]["why"]
    # no battery level, plugged in but idle -> nothing to base a plan on, so nothing is planned
    kw, notes = ev.build_ev_load(starts, tod, TZ, CFG, [v], {"A": ev.VehicleInfo(plugged=True, home=True)}, set())
    assert sum(kw) == 0 and notes[0]["needed_kwh"] is None
    # ...unless you told it how much a charge is, in which case what has already gone in is taken off
    kw, notes = ev.build_ev_load(starts, tod, TZ, CFG, [replace_session(v, 12)], {"A": ev.VehicleInfo(plugged=True, home=True, delivered_kwh=5)}, set())
    assert notes[0]["planned_kwh"] == pytest.approx(7.0)
