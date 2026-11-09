"""Synthetic PV model for the annual what-if (the live twin uses Forecast.Solar instead).

Shape: clear-sky sin(elevation) curve. Seasonality: UK monthly yield fractions. Weather: a
seeded per-day clearness factor. Scale: `annual_yield` kWh/kWp/yr, which you should set from a
PVGIS / installer estimate for your roof - orientation and tilt are folded into that one number.
"""
from __future__ import annotations

import calendar
import math
import random
from datetime import datetime

# Share of annual UK generation by month (south-facing, ~35 deg tilt). Normalised in code.
_MONTH_FRAC = [2.4, 4.4, 8.0, 11.6, 13.6, 14.2, 13.8, 11.8, 8.8, 5.6, 3.3, 2.1]
_FRAC = [f / sum(_MONTH_FRAC) for f in _MONTH_FRAC]


def _sin_elev(dt_utc: datetime, lat: float, lon: float) -> float:
    n = dt_utc.timetuple().tm_yday
    decl = math.radians(23.44) * math.sin(2 * math.pi * (284 + n) / 365)
    hours = dt_utc.hour + dt_utc.minute / 60 + 0.25                 # mid-slot
    ha = math.radians(15 * (hours + lon / 15 - 12))
    la = math.radians(lat)
    return math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)


def synth_pv_unit(starts: list[datetime], lat: float, lon: float, annual_yield: float,
                  seed: int = 7) -> tuple[list[float], list[float]]:
    """Return (actual, expected) kW-per-kWp lists for the given UTC slot starts.

    `expected` has weather factor 1.0 (what a planner knows without a forecast);
    `actual` applies a seeded random clearness per day.
    """
    rng = random.Random(seed)
    n = len(starts)
    shape = [max(0.0, _sin_elev(s, lat, lon)) ** 1.15 for s in starts]
    # group by UTC date
    days: dict[tuple[int, int, int], list[int]] = {}
    for i, s in enumerate(starts):
        days.setdefault((s.year, s.month, s.day), []).append(i)
    # per-month weather factors, rescaled to mean 1 so the monthly total is preserved
    wf: dict[tuple[int, int, int], float] = {}
    by_month: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for d in days:
        by_month.setdefault((d[0], d[1]), []).append(d)
    for _, ds in by_month.items():
        raw = [min(1.9, max(0.15, rng.gauss(1.0, 0.55))) for _ in ds]
        mean = sum(raw) / len(raw)
        for d, w in zip(ds, raw):
            wf[d] = w / mean
    actual, expected = [0.0] * n, [0.0] * n
    for d, idx in days.items():
        tot = sum(shape[i] for i in idx)
        if tot <= 0:
            continue
        dim = calendar.monthrange(d[0], d[1])[1]
        kwh_day = _FRAC[d[1] - 1] * annual_yield / dim
        for i in idx:
            base = kwh_day * shape[i] / tot / 0.5          # kW per kWp
            expected[i] = base
            actual[i] = base * wf[d]
    return actual, expected
