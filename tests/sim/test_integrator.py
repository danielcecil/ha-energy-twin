from datetime import datetime, timedelta, timezone

import pytest

from custom_components.energy_twin.ha_data import PowerIntegrator

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def s(n):
    return T0 + timedelta(seconds=n)


def test_a_spike_between_ticks_is_counted_not_missed():
    """Home Mini readings: 0.4 kW, then a 6 kW kettle for 10 s, then 0.4 kW. One-minute sampling sees 0.4 kW."""
    i = PowerIntegrator()
    i.feed(s(0), 0.4)
    assert i.take(s(0)) == pytest.approx(0.4)                      # first tick: nothing to average yet
    i.feed(s(20), 6.0)
    i.feed(s(30), 0.4)
    i.feed(s(50), 0.4)
    exact = (0.4 * 20 + 6.0 * 10 + 0.4 * 30) / 60                  # 1.3 kW average over the minute
    assert i.take(s(60)) == pytest.approx(exact)
    assert exact > 3 * 0.4                                           # what plain sampling would have reported


def test_each_tick_covers_only_its_own_window_and_unavailable_gives_none():
    i = PowerIntegrator()
    i.feed(s(0), 1.0)
    i.take(s(0))
    i.feed(s(30), 3.0)
    assert i.take(s(60)) == pytest.approx(2.0)
    assert i.take(s(120)) == pytest.approx(3.0)                     # the next minute is just the latest value
    i.feed(s(130), None)                                             # sensor drops out
    assert i.take(s(180)) is None
    assert PowerIntegrator().take(s(0)) is None                      # never seen anything


def test_resync_restarts_from_the_current_value():
    i = PowerIntegrator()
    i.feed(s(0), 5.0)
    i.take(s(0))
    i.resync(s(30), 0.5)
    assert i.take(s(60)) == pytest.approx(0.5)
