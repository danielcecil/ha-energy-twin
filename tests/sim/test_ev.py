from datetime import datetime, timedelta, timezone
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


@pytest.mark.parametrize("raw,exp", [
    ("on", True), ("Connected", True), ("charging", True), ("stopped", True), ("complete", True),
    ("disconnected", False), ("Not connected", False), ("off", False), ("unplugged", False),
    ("unknown", None), (None, None), ("banana", None), ("Connected: waiting for car demand", True),
])
def test_interpret_plugged(raw, exp):
    assert ev.interpret_plugged(raw) is exp


def test_interpret_overrides_and_home():
    assert ev.interpret_plugged("ready_to_go", ("ready_to_go",)) is True
    assert ev.interpret_plugged("connected", ("ready_to_go",)) is False      # override is authoritative
    assert ev.interpret_home("home") is True and ev.interpret_home("not_home") is False
    assert ev.interpret_home("Work") is False                                # any other zone = away
    assert ev.interpret_home("unavailable") is None
    assert ev.interpret_home("Garage", ("garage", "home")) is True


def test_needed_energy_from_soc_and_from_sessions():
    v = ev.Vehicle("A", capacity_kwh=60, target_soc_pct=80)
    lrn = ev.VehicleLearn()
    assert ev.needed_kwh(v, lrn, ev.VehicleInfo(soc_pct=30)) == pytest.approx(30 / ev.CHARGE_EFF)
    assert ev.needed_kwh(v, lrn, ev.VehicleInfo(soc_pct=90)) == 0.0
    # no SoC: learned session size minus what's already been delivered
    for k in (10, 12, 14):
        lrn.end_session(k)
    assert ev.needed_kwh(v, lrn, ev.VehicleInfo()) == pytest.approx(12.0)
    assert ev.needed_kwh(v, lrn, ev.VehicleInfo(delivered_kwh=5)) == pytest.approx(7.0)


def test_unplugged_car_is_probability_weighted_plugged_car_is_certain():
    lrn = ev.VehicleLearn(wd_plugged=[1] * 7, wd_seen=[4] * 7)               # plugged ~ 1 in 4 nights
    assert ev.tonight_probability(ev.VehicleInfo(plugged=True, home=True), lrn, 0) == 1.0
    assert ev.tonight_probability(ev.VehicleInfo(plugged=True, home=False), lrn, 0) == pytest.approx(2 / 6)
    assert ev.tonight_probability(ev.VehicleInfo(plugged=False, home=True), lrn, 0) == pytest.approx(2 / 6)


def test_offpeak_car_charges_only_in_window_from_its_start():
    starts, tod = horizon()
    v = ev.Vehicle("B", mode="offpeak", charger_kw=7.0, capacity_kwh=60, target_soc_pct=80)
    kw, notes = ev.build_ev_load(starts, tod, TZ, CFG, [v], {"B": ev.VehicleInfo(plugged=True, home=True, soc_pct=50)},
                                 {"B": ev.VehicleLearn()}, dispatch_slots=set())
    on = [i for i, x in enumerate(kw[:36]) if x > 0]                        # first 18 h = tonight
    assert on and all(CFG.in_cheap_window(tod[i]) for i in on)
    assert tod[on[0]] == CFG.cheap_start_min                                 # starts at 23:30, not before
    assert max(kw) == pytest.approx(7.0)
    assert sum(kw[:36]) * SLOT_H == pytest.approx(18 / ev.CHARGE_EFF, rel=1e-6)
    assert notes[0]["probability"] == 1.0


def test_iog_car_uses_dispatch_slots_outside_window_offpeak_car_ignores_them():
    starts, tod = horizon()
    disp = {starts[i] for i in (4, 5, 6)}                                    # 20:00-21:30: outside the window
    infos = {n: ev.VehicleInfo(plugged=True, home=True, soc_pct=60) for n in "AB"}
    iog = ev.Vehicle("A", mode="iog", charger_kw=7.0, capacity_kwh=60, target_soc_pct=80)
    off = ev.Vehicle("B", mode="offpeak", charger_kw=7.0, capacity_kwh=60, target_soc_pct=80)
    ka, _ = ev.build_ev_load(starts, tod, TZ, CFG, [iog], infos, {}, disp)
    kb, _ = ev.build_ev_load(starts, tod, TZ, CFG, [off], infos, {}, disp)
    assert all(ka[i] > 0 for i in (4, 5, 6)) and all(kb[i] == 0 for i in (4, 5, 6))
    # same energy overall; the IOG car just took part of it earlier
    assert sum(ka[:36]) == pytest.approx(sum(kb[:36]))


def test_two_cars_and_live_power_override():
    starts, tod = horizon(datetime(2026, 11, 9, 23, 45, tzinfo=TZ), 24)       # mid-window
    cars = [ev.Vehicle("A", mode="iog"), ev.Vehicle("B", mode="offpeak")]
    infos = {"A": ev.VehicleInfo(plugged=True, home=True, soc_pct=40, live_kw=6.8),
             "B": ev.VehicleInfo(plugged=False, home=False)}
    lrn = {"B": ev.VehicleLearn(wd_plugged=[0] * 7, wd_seen=[10] * 7), "A": ev.VehicleLearn()}
    kw, notes = ev.build_ev_load(starts, tod, TZ, CFG, cars, infos, lrn, set())
    assert kw[0] >= 6.8                                                      # measured draw wins for "now"
    b = next(n for n in notes if n["vehicle"] == "B")
    assert b["probability"] < 0.1 and b["expected_kwh_tonight"] < 1.0        # away, never plugs in -> ~nothing


def test_learning_from_history_only_counts_real_sessions():
    # 21 nights of hourly data: car charges 12 kWh on Mon/Wed/Fri nights only
    rows, day0 = [], datetime(2026, 10, 5, 12, tzinfo=TZ)                    # a Monday
    for d in range(21):
        for h in range(24):
            t = (day0 + timedelta(days=d, hours=h)).astimezone(timezone.utc)
            night = (day0 + timedelta(days=d)).weekday()
            kwh = 2.0 if (night in (0, 2, 4) and 11 <= h <= 16) else 0.0     # 23:00-04:00 local
            rows.append((t, kwh))
    lrn = ev.learn_from_rows(rows, TZ)
    assert lrn.prob(0) > 0.7 and lrn.prob(1) < 0.3 and lrn.prob(2) > 0.7     # Mon/Wed high, Tue low
    assert lrn.typical_kwh() == pytest.approx(12.0)
    assert len(lrn.sessions) == 9


def test_year_load_totals_match_expectation_and_are_lumpy():
    start = datetime(2026, 1, 1, tzinfo=TZ)
    starts = make_starts(start.astimezone(timezone.utc), 48 * 365)
    tod = tod_minutes(starts, TZ)
    v = ev.Vehicle("A", charger_kw=7.0)
    lrn = {"A": ev.VehicleLearn(sessions=[10, 10, 10], wd_plugged=[2] * 7, wd_seen=[6] * 7)}   # p = 3/8
    act, exp = ev.year_load(starts, tod, TZ, CFG, [v], lrn)
    assert sum(act) * SLOT_H == pytest.approx(sum(exp) * SLOT_H, rel=0.05)
    assert 100 < sum(exp) * SLOT_H / 10 < 180                                  # ~ 365 * 3/8 sessions
    nights_on = sum(1 for k in range(365) if any(act[48 * k + j] > 0 for j in range(48)))
    assert nights_on < 300                                                      # lumpy, not every night
