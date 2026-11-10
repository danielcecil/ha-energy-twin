from dataclasses import replace
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_twin.sim import heat, load, pipeline
from custom_components.energy_twin.sim.engine import simulate
from custom_components.energy_twin.sim.finance import Costs, FinanceTerms
from custom_components.energy_twin.sim.models import SLOT_H, Hardware, Strategy, TariffConfig

TZ = ZoneInfo("Europe/London")
CFG = TariffConfig()
HP = heat.HeatParams(enabled=True)


def year(with_hp: bool, ev_kwh=12.0):
    prof = load.default_profile(9.0, ev_kwh, CFG)
    hw = Hardware(pv_kwp=6, battery_kwh=10)
    h = (HP, heat.default_gas_model(HP)) if with_hp else None
    return hw, pipeline.make_year(prof, None, None, 0, None, hw=hw, tariff=CFG, tz=TZ, lat=52.5, lon=-1.5,
                                  today=date(2026, 1, 1), pd=(0, 17, 1), pu=(0, 13, 2), heat=h)


def test_heat_pump_economics_are_gas_avoided_minus_extra_grid_cost():
    hw, sl = year(True)
    assert sl.new_kw and sum(sl.new_kw) > 0 and sl.heat["gas_kwh_displaced"] > 9000
    rep = pipeline.annual_report(sl, hw, Strategy(name="rules"), CFG, Costs(hp=7000), FinanceTerms(), heat_p=HP)
    h = rep["heat"]
    assert h["net_hp_saving"] == pytest.approx(h["gas_cost_avoided"] + h["gas_standing_avoided"] - h["grid_cost_nokit"], abs=0.02)
    assert h["gas_cost_avoided"] == pytest.approx(h["gas_kwh_displaced"] * HP.gas_unit_p / 100, abs=0.02)
    # total = kit saving (given the heat pump's load) + heat pump package
    assert rep["saving_year1"] == pytest.approx(rep["pv_saving"] + rep["battery_saving"] + rep["hp_saving"], abs=0.05)
    assert rep["finance"]["capex"] >= 7000                             # heat pump cost is in the outlay
    assert h["cheap_share_pct"] > 8


def test_kit_is_worth_more_with_a_heat_pump_in_the_house():
    hw0, s0 = year(False)
    hw1, s1 = year(True)
    st = Strategy(name="rules")
    r0 = pipeline.annual_report(s0, hw0, st, CFG, Costs(), FinanceTerms())
    r1 = pipeline.annual_report(s1, hw1, st, CFG, Costs(), FinanceTerms(), heat_p=HP)
    kit0 = r0["pv_saving"] + r0["battery_saving"]
    kit1 = r1["pv_saving"] + r1["battery_saving"]
    assert kit1 > kit0 * 1.05                                          # more daytime load -> more self-consumed solar
    assert r0["heat"] is None and r0["hp_saving"] == 0


def test_cheaper_gas_shrinks_the_heat_pump_saving():
    hw, sl = year(True)
    st = Strategy(name="rules")
    lo = pipeline.annual_report(sl, hw, st, CFG, Costs(), FinanceTerms(), heat_p=replace(HP, gas_unit_p=4.0))
    hi = pipeline.annual_report(sl, hw, st, CFG, Costs(), FinanceTerms(), heat_p=replace(HP, gas_unit_p=9.0))
    assert hi["hp_saving"] > lo["hp_saving"] + 200


def test_main_fuse_limits_grid_import_and_battery_charging():
    hw, sl = year(True)
    chunk = CFG.cheap_start_min
    free = simulate(sl, replace(hw, import_limit_kw=0), Strategy(name="rules"), chunk_tod_min=chunk, baseline_with_new=True)
    limit = 7.0                                    # car + heat pump + a 5 kW battery charge would exceed this
    assert free.peak_import_kw > limit + 0.5
    for name in ("rules", "optimised"):
        res = simulate(sl, replace(hw, import_limit_kw=limit), Strategy(name=name), chunk_tod_min=chunk,
                       baseline_with_new=True)
        unavoidable = max(h + e + n - p for h, e, n, p in zip(sl.house_kw, sl.ev_kw, sl.new_kw, res.pv_kw))
        assert unavoidable < limit
        assert res.peak_import_kw <= limit + 0.3, (name, res.peak_import_kw)      # DP power steps ~0.25 kW
        assert res.saving_gbp > 0.8 * free.saving_gbp                              # throttling costs little


def test_pipeline_without_heat_is_unchanged():
    hw, sl = year(False)
    assert not sl.new_kw and sl.heat is None
    rep = pipeline.annual_report(sl, hw, Strategy(name="rules"), CFG, Costs(), FinanceTerms())
    assert rep["hp_saving"] == 0 and rep["saving_year1"] == pytest.approx(rep["pv_saving"] + rep["battery_saving"], abs=0.05)
