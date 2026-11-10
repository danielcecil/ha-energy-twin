from datetime import date
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_twin.sim import heat, load, pipeline
from custom_components.energy_twin.sim.finance import Costs, FinanceTerms
from custom_components.energy_twin.sim.models import Hardware, Strategy, TariffConfig

TZ = ZoneInfo("Europe/London")
CFG = TariffConfig()


def report(hw, hp_on):
    hp = heat.HeatParams(enabled=hp_on)
    gm = heat.default_gas_model(hp)
    prof = load.default_profile(9.0, 12.0, CFG)
    sl = pipeline.make_year(prof, None, None, 0, None, hw=hw, tariff=CFG, tz=TZ, lat=52.5, lon=-1.5,
                            today=date(2026, 1, 1), pd=(0, 17, 1), pu=(0, 13, 2), heat=(hp, gm) if hp_on else None)
    return pipeline.annual_report(sl, hw, Strategy(name="rules"), CFG, Costs(system=12000), FinanceTerms(),
                                  heat_p=hp, tz=TZ, gas_model=gm)


@pytest.mark.parametrize("kit,hp_on", [(Hardware(), False), (Hardware(pv_kwp=6, battery_kwh=10), False),
                                      (Hardware(pv_kwp=6, battery_kwh=10), True), (Hardware(), True)])
def test_bill_today_minus_bill_with_equals_the_headline_saving(kit, hp_on):
    r = report(kit, hp_on)
    assert r["bill_today"] - r["bill_with"] == pytest.approx(r["saving_year1"], abs=0.05)
    assert r["gas_today"] > 500 and r["elec_today"] > 800


def test_todays_bill_is_the_same_number_for_every_setup():
    bills = {round(report(k, h)["bill_today"], 2) for k, h in
             ((Hardware(), False), (Hardware(pv_kwp=6, battery_kwh=10), False), (Hardware(pv_kwp=6, battery_kwh=10), True))}
    assert len(bills) == 1                                    # the "do nothing" bill is a fixed reference
    r = report(Hardware(pv_kwp=6, battery_kwh=10), True)
    assert r["gas_with"] == 0 and r["bill_with"] < r["bill_today"] * 0.6 and r["gas_is_estimate"] is True
