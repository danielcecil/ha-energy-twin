from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_twin.sim import annual, load
from custom_components.energy_twin.sim.engine import simulate
from custom_components.energy_twin.sim.finance import Costs, FinanceTerms, evaluate_finance, monthly_payment
from custom_components.energy_twin.sim.models import EV_ON_KW, GridEvent, Hardware, Strategy, TariffConfig

TZ = ZoneInfo("Europe/London")
CFG = TariffConfig()
CHUNK = CFG.cheap_start_min


def year(days=365, ev_kwh=15.0, cfg=CFG, events=None, start=date(2026, 1, 1)):
    prof = load.default_profile(9.0, ev_kwh, cfg)
    return load.build_year(hw_yield=950, profile=prof, cfg=cfg, tz=TZ, lat=52.5, lon=-1.5,
                           start_day=start, days=days, events=events)


HW = Hardware(pv_kwp=6.0, battery_kwh=10.0, inverter_kw=5.0, battery_max_kw=5.0)


@pytest.mark.parametrize("strategy", ["rules", "optimised"])
def test_physics_invariants(strategy):
    sl = year(60)
    res = simulate(sl, HW, Strategy(name=strategy), chunk_tod_min=CHUNK)
    eta = HW.eta
    soc_prev = HW.emin
    for t in range(sl.n):
        assert HW.emin - 1e-6 <= res.soc[t] <= HW.emax + 1e-6
        # SoC change must match AC power through efficiency
        r = res.battery_kw[t]
        exp_delta = r * eta * 0.5 if r > 0 else r * 0.5 / eta
        assert res.soc[t] - soc_prev == pytest.approx(exp_delta, abs=1e-6)
        soc_prev = res.soc[t]
        assert abs(r) <= HW.battery_max_kw + 1e-6
        if sl.ev_kw[t] >= EV_ON_KW:
            assert r >= -1e-9, "battery must never feed the car"
        assert res.export_kw[t] <= HW.export_limit_kw + 1e-6
        # grid balance
        assert res.grid_kw[t] == pytest.approx(sl.house_kw[t] + sl.ev_kw[t] - res.pv_kw[t] + r, abs=1e-6)


def test_no_hardware_means_zero_saving():
    sl = year(30)
    res = simulate(sl, replace(HW, pv_kwp=0, battery_kwh=0), Strategy(name="rules"), chunk_tod_min=CHUNK)
    assert res.saving_gbp == pytest.approx(0.0, abs=1e-9)


def test_battery_only_arbitrage_saves_on_iog():
    sl = year(90)
    hw = replace(HW, pv_kwp=0)
    for name in ("rules", "optimised"):
        res = simulate(sl, hw, Strategy(name=name), chunk_tod_min=CHUNK)
        assert res.saving_gbp > 20, name
        assert res.grid_charge_kwh > 100


def test_optimiser_not_worse_than_rules():
    sl = year(120)
    r = simulate(sl, HW, Strategy(name="rules"), chunk_tod_min=CHUNK).saving_gbp
    o = simulate(sl, HW, Strategy(name="optimised"), chunk_tod_min=CHUNK).saving_gbp
    assert o >= r * 0.97, (r, o)


def test_export_arbitrage_follows_price():
    sl_hi = year(30, cfg=replace(CFG, export_p=15.0))
    sl_lo = year(30, cfg=replace(CFG, export_p=5.0))
    hw = replace(HW, pv_kwp=0, battery_kwh=25.0, battery_max_kw=5.0)   # spare capacity beyond house needs
    hi = simulate(sl_hi, hw, Strategy(name="optimised"), chunk_tod_min=CHUNK)
    lo = simulate(sl_lo, hw, Strategy(name="optimised"), chunk_tod_min=CHUNK)
    assert hi.battery_export_kwh > 50          # 15p > 7p/0.9 -> worth exporting
    assert lo.battery_export_kwh < 5           # 5p < 7p/0.9 -> never worth it
    # rules should agree on direction
    hi_r = simulate(sl_hi, hw, Strategy(name="rules"), chunk_tod_min=CHUNK)
    assert hi_r.battery_export_kwh > 50


def _event_day(kind, hour, hours, **kw):
    d = datetime(2026, 2, 10, hour, 0, tzinfo=timezone.utc)
    return GridEvent(kind, d, d + timedelta(hours=hours), **kw)


def test_power_down_discharges_and_exports():
    ev = _event_day("power_down", 17, 2, export_p=60.0)
    sl = year(5, events=[ev], start=date(2026, 2, 8))
    hw = replace(HW, pv_kwp=0)
    for name in ("rules", "optimised"):
        res = simulate(sl, hw, Strategy(name=name), chunk_tod_min=CHUNK)
        idx = [i for i, e in enumerate(sl.event) if e == "power_down"]
        assert len(idx) == 4
        exported = sum(res.export_kw[i] * 0.5 for i in idx)
        assert exported > 3.0, (name, exported)


def test_power_up_charges_fully_at_event_price():
    ev = _event_day("power_up", 13, 2, import_p=0.0)
    sl = year(5, events=[ev], start=date(2026, 2, 8))
    hw = replace(HW, pv_kwp=0)
    for name in ("rules", "optimised"):
        res = simulate(sl, hw, Strategy(name=name), chunk_tod_min=CHUNK)
        idx = [i for i, e in enumerate(sl.event) if e == "power_up"]
        assert res.soc[idx[-1]] >= hw.emax - 0.5, name


def test_annual_attribution_and_scaling():
    sl = year(365)
    a = annual.evaluate(sl, HW, Strategy(name="rules"), CHUNK)
    assert a.days == 365
    assert a.saving_gbp > a.saving_pv_only_gbp > 0
    assert a.saving_gbp > a.saving_battery_only_gbp > 0
    assert 3.0 < a.pv_kwh / 6.0 / 1000 * 1000 / 1000 * 1000 < 1500  # kWh/kWp plausibility (clip-adjusted)
    assert len(a.monthly) == 12
    pv_sav, b_sav = annual.split_savings(a)
    assert pv_sav + b_sav == pytest.approx(a.saving_gbp)


def test_pv_yield_matches_input():
    sl = year(365)
    per_kwp = sum(sl.exp_pv_unit) * 0.5
    assert per_kwp == pytest.approx(950, rel=0.02)


def test_history_profile_roundtrip():
    prof = load.default_profile(10.0, 12.0, CFG)
    assert prof.daily_house_kwh == pytest.approx(10.0 * (5 + 2 * 1.08) / 7, rel=0.01)
    assert prof.daily_ev_kwh == pytest.approx(12.0, rel=0.01)


def test_finance_maths():
    assert monthly_payment(6000, 0, 60) == pytest.approx(100)
    assert monthly_payment(6000, 6.9, 60) == pytest.approx(118.5, abs=0.5)
    cash = evaluate_finance(8000, 700, 300, FinanceTerms(tariff_escalation_pct=0, pv_degradation_pct=0, battery_degradation_pct=0))
    assert cash["simple_payback_years"] == 8.0
    assert cash["payback_years"] == pytest.approx(8.0, abs=0.1)
    loan = evaluate_finance(8000, 700, 300, FinanceTerms(apr_pct=6.9, term_months=60))
    assert loan["monthly_payment"] > 0 and loan["monthly_net_cashflow"] < 0


def test_sweep_ranks_and_includes_costs():
    sl = year(365)
    rows = annual.sweep(lambda y: sl, HW, Strategy(name="rules"), CHUNK, Costs(), FinanceTerms(),
                        batteries=[0, 5, 10], arrays=[0, 4, 6])
    assert len(rows) == 8
    assert rows == sorted(rows, key=lambda r: r["npv"], reverse=True)
