from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_twin import ha_data
from custom_components.energy_twin.sim.solar import shape_to_totals, synth_pv_unit
from custom_components.energy_twin.sim.tariff import make_starts

TZ = ZoneInfo("Europe/London")
U = timezone.utc


def test_wh_period_is_keyed_by_the_end_of_its_period():
    """Library semantics: the value at key 09:00 is the 08:00-09:00 hour (not 09:00-10:00)."""
    k = lambda h, m=0: datetime(2026, 11, 10, h, m, tzinfo=U)  # noqa: E731
    out = ha_data.wh_period_to_kw([(k(9), 1000.0), (k(10), 2000.0)])
    assert out[k(8)] == out[k(8, 30)] == pytest.approx(1.0)               # 08:00-09:00
    assert out[k(9)] == out[k(9, 30)] == pytest.approx(2.0)               # 09:00-10:00
    assert k(10) not in out and k(7, 30) not in out
    # a key inside an hour (e.g. sunrise) belongs to the hour it ends in, and adds to that hour's total
    out2 = ha_data.wh_period_to_kw([(k(7, 58), 40.0), (k(8), 160.0)])
    assert out2[k(7)] == pytest.approx(0.2)


def _day(now_local, n=96):
    starts = make_starts(now_local.astimezone(U), n)
    _, exp = synth_pv_unit(starts, 52.5, -1.5, 950.0)
    return starts, [u * 5.0 for u in exp]                                  # a 5 kWp array


def test_shape_to_totals_matches_forecast_sensors_and_peak():
    now = datetime(2026, 6, 10, 9, 10, tzinfo=TZ)
    starts, shape = _day(datetime(2026, 6, 10, 9, 0, tzinfo=TZ), 96)       # 9:00 today .. 9:00 day after
    peak_tom = datetime(2026, 6, 11, 14, 0, tzinfo=TZ).astimezone(U)       # forecast says a late afternoon peak
    out = shape_to_totals(starts, TZ, shape, now, 21.5, 33.0, None, peak_tom)
    loc = [s.astimezone(TZ).date() for s in starts]
    today = sum(out[i] * 0.5 for i in range(96) if loc[i] == now.date() and starts[i] >= now.replace(minute=0))
    tom = sum(out[i] * 0.5 for i in range(96) if loc[i] == (now + timedelta(days=1)).date())
    assert today == pytest.approx(21.5, rel=0.01) and tom == pytest.approx(33.0, rel=0.01)
    t_idx = [i for i in range(96) if loc[i] == (now + timedelta(days=1)).date()]
    pk = max(t_idx, key=lambda i: out[i])
    assert abs((starts[pk] - peak_tom).total_seconds()) <= 1800            # peak moved to where the forecast puts it
    assert all(v >= 0 for v in out)


def test_shape_to_totals_leaves_unknown_days_and_missing_totals_alone():
    now = datetime(2026, 6, 10, 9, 10, tzinfo=TZ)
    starts, shape = _day(datetime(2026, 6, 10, 9, 0, tzinfo=TZ), 200)
    out = shape_to_totals(starts, TZ, shape, now, None, None)
    assert out == shape
    out2 = shape_to_totals(starts, TZ, shape, now, 10.0, None)
    day3 = [i for i, s in enumerate(starts) if s.astimezone(TZ).date() >= (now + timedelta(days=2)).date()]
    assert all(out2[i] == shape[i] for i in day3)
