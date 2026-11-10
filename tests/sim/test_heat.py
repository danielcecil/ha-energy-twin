from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_twin.sim import heat
from custom_components.energy_twin.sim.models import Hardware, SLOT_H, Strategy, TariffConfig
from custom_components.energy_twin.sim.tariff import make_starts, tod_minutes

TZ = ZoneInfo("Europe/London")
CFG = TariffConfig()
P = heat.HeatParams(enabled=True)


def test_cop_behaves_physically():
    warm, cold = heat.cop(10, 45, 0.42), heat.cop(-3, 45, 0.42)
    assert 3.0 < warm < 4.5 and 2.2 < cold < warm                    # colder outside = worse
    assert heat.cop(7, 35, 0.42) > heat.cop(7, 55, 0.42)             # lower flow temperature = better
    assert heat.cop(7, 50, 0.42) < heat.cop(7, 45, 0.42)             # tank water costs more than radiators
    assert heat.capacity_kw(-5, 8) < heat.capacity_kw(7, 8) == pytest.approx(8)


def test_calibration_recovers_known_split():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
    temps = {d: 4 + 8 * ((i % 30) / 30) for i, d in enumerate(days)}            # 4..12C
    gas = {d: 6.0 + 9.0 * max(0, 15.5 - temps[d]) for d in days}                # base 6, k 9
    gm = heat.calibrate(gas, temps, P)
    assert gm.source == "history" and gm.base_kwh_day == pytest.approx(6.0, rel=0.01)
    assert gm.k_per_degday == pytest.approx(9.0, rel=0.01) and gm.r2 > 0.99
    assert heat.calibrate({}, {}, P).source == "default"
    flat = {d: 20.0 for d in days}                                              # no weather variation: can't split
    assert heat.calibrate(flat, {d: 20.0 for d in days}, P).source == "default"


def _run(days=2, start=datetime(2026, 1, 12, 0, tzinfo=TZ), p=P, temp=3.0, event=None, disp=None, tank0=None):
    n = 48 * days
    starts = make_starts(start.astimezone(timezone.utc), n)
    tod = tod_minutes(starts, TZ)
    cheap = [CFG.in_cheap_window(t) for t in tod]
    inwin = list(cheap)
    ev = [""] * n
    for i in event or ():
        ev[i], cheap[i] = "power_up", True
    for i in disp or ():
        cheap[i] = True
    res = heat.horizon(starts, tod, TZ, cheap, ev, inwin, [temp] * n, p, heat.default_gas_model(p), tank0)
    return res, tod, cheap


def test_tank_charges_at_4am_in_the_cheap_window_and_never_goes_flat():
    res, tod, cheap = _run(days=3)
    for d in (1, 2):
        on = [tod[i] for i in range(48 * d, 48 * d + 48) if res.charging[i]]
        assert on and min(on) == 4 * 60 and all(cheap[48 * d + t // 30] for t in on)   # starts at 04:00, all cheap
        assert not any(res.charging[48 * d + i] for i in range(12, 44))               # nothing 06:00-22:00
    assert min(res.tank_end) > 0.5 and sum(res.backup_kw) == 0                        # never runs dry, no resistive


def test_power_up_and_dispatch_slots_top_up_the_tank_opportunistically():
    base, tod, _ = _run(days=2, tank0=3.0)
    # pre-drained tank + a Power Up window at 13:00-14:00 on day 1
    pu = [26, 27]
    res, _, _ = _run(days=2, tank0=3.0, event=pu)
    assert any(res.charging[i] for i in pu) and not any(base.charging[i] for i in pu if base.tank_start[i] > 1.5)
    res2, _, _ = _run(days=2, tank0=6.0, disp=[36, 37])                 # smart-charge slot at 18:00
    assert any(res2.charging[i] for i in (36, 37))


def test_undersized_heat_pump_falls_back_to_resistive_heating():
    big, _, _ = _run(temp=-8.0)
    small, _, _ = _run(temp=-8.0, p=replace(P, hp_capacity_kw=3.0))
    assert sum(small.backup_kw) > sum(big.backup_kw) + 0.5          # too small: resistive backup kicks in
    assert sum(small.hp_kw) > sum(big.hp_kw)


def test_colder_weather_costs_more_and_cooking_is_small():
    mild, *_ = _run(temp=10.0)
    cold, *_ = _run(temp=-2.0)
    assert sum(cold.hp_kw) > 1.5 * sum(mild.hp_kw)
    cook = sum(mild.induction_kw) * SLOT_H / 2
    assert cook == pytest.approx(P.cooking_gas_kwh_day * P.induction_ratio, rel=1e-6)


def test_year_totals_are_plausible_and_mostly_cheap():
    days = 365
    starts = make_starts(datetime(2026, 1, 1, tzinfo=TZ).astimezone(timezone.utc), 48 * days)
    tod = tod_minutes(starts, TZ)
    cheap = [CFG.in_cheap_window(t) for t in tod]
    act, exp = heat.synth_temps(starts, TZ)
    loc = [s.astimezone(TZ) for s in starts]
    res = heat.simulate_heat(tod, cheap, [False] * len(starts), [d.weekday() for d in loc], [d.date() for d in loc],
                             act, P, heat.default_gas_model(P))
    s = heat.summarise(res, cheap, P)
    assert 9000 < s["gas_kwh_displaced"] < 14000                    # ~ the 11.5 MWh/yr default home
    assert 2.4 < s["scop_space"] < 3.8 and 2.2 < s["scop_dhw"] < 3.3
    assert 2500 < s["hp_electricity_kwh"] < 5500
    assert s["cheap_share_pct"] > 8                                  # tank is charged in the cheap window
    assert s["backup_electricity_kwh"] < 0.1 * s["hp_electricity_kwh"]
