from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_twin.sim import pipeline
from custom_components.energy_twin.sim.finance import Costs, FinanceTerms
from custom_components.energy_twin.sim.models import Hardware, Strategy, TariffConfig

TZ = ZoneInfo("Europe/London")
CFG = TariffConfig()


def fake_history(days, ev_kwh=12.0):
    """Hourly kWh: ~0.4 kW base + evening bump; car draws ev_kwh across 23:30-05:30."""
    end = datetime(2026, 11, 8, 0, tzinfo=timezone.utc)
    tot, ev = [], []
    for h in range(days * 24):
        t = end - timedelta(hours=days * 24 - h)
        loc = t.astimezone(TZ)
        base = 0.4 + (0.9 if 17 <= loc.hour < 21 else 0.0)
        e = ev_kwh / 6 if (loc.hour >= 23 or loc.hour < 5) else 0.0
        tot.append((t, base + e))
        ev.append((t, e))
    return tot, ev


def test_profile_learns_and_splits_ev():
    tot, ev = fake_history(30)
    prof, hh, he, days, last = pipeline.build_profile(tot, ev, TZ, CFG, 0.3, 9.0, 15.0)
    assert prof.source == "history" and days >= 29
    assert prof.daily_ev_kwh == pytest.approx(12.0, rel=0.15)
    # without EV stats the off-peak excess over the night base load is treated as the car
    prof2, *_ = pipeline.build_profile(tot, None, TZ, CFG, 0.4, 9.0, 15.0)
    assert prof2.daily_ev_kwh == pytest.approx(12.0, rel=0.25)


def test_short_history_falls_back_to_default():
    tot, ev = fake_history(5)
    prof, hh, *_ = pipeline.build_profile(tot, ev, TZ, CFG, 0.3, 9.0, 15.0)
    assert prof.source == "default" and hh is None


def test_full_year_history_used_directly_and_report_sane():
    tot, ev = fake_history(366)
    prof, hh, he, days, last = pipeline.build_profile(tot, ev, TZ, CFG, 0.3, 9.0, 15.0)
    hw = Hardware(pv_kwp=6, battery_kwh=10)
    sl = pipeline.make_year(prof, hh, he, days, last, hw=hw, tariff=CFG, tz=TZ, lat=52.5, lon=-1.5,
                            today=date(2026, 11, 9), pd=(2, 17, 1), pu=(1, 13, 2))
    assert sl.start[0].astimezone(TZ).date() == last - timedelta(days=364)
    assert any(e == "power_down" for e in sl.event) and any(e == "power_up" for e in sl.event)
    rep = pipeline.annual_report(sl, hw, Strategy(name="rules"), CFG, Costs(), FinanceTerms(apr_pct=6.9, term_months=60))
    assert rep["saving_year1"] > 500
    assert rep["finance"]["monthly_payment"] > 0
    assert rep["pv_saving"] + rep["battery_saving"] == pytest.approx(rep["saving_year1"], abs=0.05)
