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
    assert cash["simple_payback_years"] == 8.0 and cash["payback_years"] == pytest.approx(8.0, abs=0.1)
    assert cash["monthly_payment"] == 0 and cash["finance_mode"] == "cash" and cash["effective_apr_pct"] is None
    loan = evaluate_finance(8000, 700, 300, FinanceTerms(mode="loan", apr_pct=6.9, term_months=60))
    assert loan["monthly_payment"] > 0 and loan["monthly_net_cashflow"] < 0
    assert loan["effective_apr_pct"] == 6.9 and loan["total_interest"] > 0


def test_fixed_monthly_finance_reveals_the_real_interest_rate():
    from custom_components.energy_twin.sim.finance import implied_apr
    # £10,000 repaid as £149/month for 10 years = £17,880 -> a hefty rate hiding in a friendly-looking payment
    r = evaluate_finance(10000, 800, 400, FinanceTerms(mode="fixed_monthly", fixed_monthly=149, term_months=120))
    assert r["monthly_payment"] == 149 and r["total_paid_finance"] == pytest.approx(17880)
    assert r["effective_apr_pct"] == pytest.approx(12.9, abs=0.15) and r["cost_of_finance"] == pytest.approx(7880)
    assert implied_apr(6000, 100, 60) == 0.0                                   # £100 x 60 = the principal: interest free
    assert implied_apr(6000, 118.5, 60) == pytest.approx(6.9, abs=0.2)
    # deposit and fee: only the remainder is financed; the fee is paid up front
    d = evaluate_finance(10000, 800, 400, FinanceTerms(mode="loan", apr_pct=0, term_months=100, deposit=2000, fee=150))
    assert d["monthly_payment"] == pytest.approx(80) and d["upfront"] == pytest.approx(2150)
    assert d["cost_of_finance"] == pytest.approx(150)                           # 0% loan: only the fee
    # a term of 0 means cash whatever the mode says
    assert evaluate_finance(5000, 500, 0, FinanceTerms(mode="loan", term_months=0))["monthly_payment"] == 0


def test_costs_are_one_system_price_less_grant():
    assert Costs(system=18000, grant=7500).total() == 10500 and Costs(system=5000, grant=9000).net == 0
    assert Costs(system=12000).total(6.0, 10.0) == 12000                      # size arguments no longer change the price


def test_sweep_ranks_by_saving_and_reports_what_each_step_adds():
    sl = year(365)
    rows = annual.sweep(lambda y: sl, HW, Strategy(name="rules"), CHUNK, batteries=[0, 5, 10], arrays=[0, 4, 6])
    assert len(rows) == 8
    assert [r["saving_year1"] for r in rows] == sorted((r["saving_year1"] for r in rows), reverse=True)
    assert not any(k in rows[0] for k in ("capex", "npv", "payback_years"))      # no prices in a sweep
    by = {(r["pv_kwp"], r["battery_kwh"]): r for r in rows}
    r = by[(6.0, 10.0)]
    assert r["battery_step_gain"] == pytest.approx(r["saving_year1"] - by[(6.0, 5.0)]["saving_year1"], abs=0.01)
    assert r["battery_gain_per_kwh"] == pytest.approx(r["battery_step_gain"] / 5, abs=0.1)
    assert r["array_step_gain"] == pytest.approx(r["saving_year1"] - by[(4.0, 10.0)]["saving_year1"], abs=0.01)
    assert "battery_step_gain" not in by[(6.0, 0.0)] and "array_step_gain" not in by[(0.0, 5.0)]
