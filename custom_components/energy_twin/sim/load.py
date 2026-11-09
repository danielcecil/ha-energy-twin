"""Household load model: learn profiles from history, or fall back to a typical UK shape."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import SLOT_H, GridEvent, Slots, TariffConfig
from .solar import synth_pv_unit
from .tariff import build_prices, make_starts, tod_minutes

UTC = timezone.utc
# Relative winter/summer electricity use (mean ~1.0). Used to de-/re-seasonalise short histories.
SEASONAL = [1.18, 1.12, 1.04, 0.97, 0.90, 0.85, 0.84, 0.86, 0.93, 1.02, 1.10, 1.19]


def default_shape() -> list[float]:
    """Typical household shape, 48 values summing to 1 (night trough, morning + evening peaks)."""
    w = []
    for i in range(48):
        h = i / 2
        v = 0.35
        v += 0.9 * _bump(h, 7.75, 1.0)
        v += 0.6 * _bump(h, 12.5, 1.8)
        v += 1.6 * _bump(h, 18.5, 1.9)
        v += 0.4 * _bump(h, 21.5, 1.5)
        w.append(v)
    s = sum(w)
    return [x / s for x in w]


def _bump(h: float, mu: float, sd: float) -> float:
    import math
    return math.exp(-0.5 * ((h - mu) / sd) ** 2)


@dataclass
class LoadProfile:
    """Mean kW per half-hour slot-of-day, for weekdays / weekends, house and EV separately."""

    house_wd: list[float]
    house_we: list[float]
    ev: list[float]
    days: int = 0            # how many history days it was learned from
    source: str = "default"

    def house(self, d: date, slot: int) -> float:
        return (self.house_we if d.weekday() >= 5 else self.house_wd)[slot]

    @property
    def daily_house_kwh(self) -> float:
        return (5 * sum(self.house_wd) + 2 * sum(self.house_we)) / 7 * SLOT_H

    @property
    def daily_ev_kwh(self) -> float:
        return sum(self.ev) * SLOT_H


def default_profile(daily_house_kwh: float = 9.0, daily_ev_kwh: float = 0.0,
                    tariff: TariffConfig | None = None) -> LoadProfile:
    shape = default_shape()
    house = [x * daily_house_kwh / SLOT_H for x in shape]
    ev = [0.0] * 48
    if daily_ev_kwh > 0:
        tariff = tariff or TariffConfig()
        slots = [i for i in range(48) if tariff.in_cheap_window(i * 30)]
        for i in slots:
            ev[i] = daily_ev_kwh / (len(slots) * SLOT_H)
    return LoadProfile(house, [x * 1.08 for x in house], ev, 0, "default")


@dataclass
class HistorySlot:
    start: datetime      # UTC
    total_kw: float      # whole-home import
    ev_kw: float | None  # EV charging if known


def hourly_to_slots(rows: list[tuple[datetime, float]]) -> dict[datetime, float]:
    """(hour start UTC, kWh in that hour) -> {half-hour start: kW}."""
    out: dict[datetime, float] = {}
    for start, kwh in rows:
        if kwh is None or kwh < 0:
            continue
        start = start.astimezone(UTC)
        out[start] = kwh
        out[start + timedelta(minutes=30)] = kwh        # kWh/h == kW, same for both halves
    return out


def split_ev(total: dict[datetime, float], ev: dict[datetime, float] | None,
             tz: ZoneInfo, cfg: TariffConfig, night_base_kw: float) -> tuple[dict, dict]:
    """Separate EV from house load. With EV statistics, subtract them; otherwise assume any
    off-peak-window demand above `night_base_kw` is the car(s)."""
    house, evs = {}, {}
    for s, kw in total.items():
        if ev is not None:
            e = min(kw, max(0.0, ev.get(s, 0.0)))
        else:
            loc = s.astimezone(tz)
            in_win = cfg.in_cheap_window(loc.hour * 60 + loc.minute)
            e = max(0.0, kw - night_base_kw) if in_win else 0.0
        evs[s], house[s] = e, kw - e
    return house, evs


def learn_profile(house: dict[datetime, float], ev: dict[datetime, float], tz: ZoneInfo) -> LoadProfile:
    """Average into weekday/weekend slot-of-day profiles, de-seasonalised by month."""
    sums = {"wd": [0.0] * 48, "we": [0.0] * 48}
    cnt = {"wd": [0] * 48, "we": [0] * 48}
    esum, ecnt = [0.0] * 48, [0] * 48
    month_days: dict[int, set] = {}
    for s, kw in house.items():
        loc = s.astimezone(tz)
        i = (loc.hour * 60 + loc.minute) // 30
        k = "we" if loc.weekday() >= 5 else "wd"
        f = SEASONAL[loc.month - 1]
        sums[k][i] += kw / f
        cnt[k][i] += 1
        esum[i] += ev.get(s, 0.0)
        ecnt[i] += 1
        month_days.setdefault(loc.month, set()).add(loc.date())
    wd = [sums["wd"][i] / cnt["wd"][i] if cnt["wd"][i] else 0.0 for i in range(48)]
    we = [sums["we"][i] / cnt["we"][i] if cnt["we"][i] else wd[i] for i in range(48)]
    evp = [esum[i] / ecnt[i] if ecnt[i] else 0.0 for i in range(48)]
    days = len({d for ds in month_days.values() for d in ds})
    return LoadProfile(wd, we, evp, days, "history")


# --------------------------------------------------------------------------- year timeline
def build_year(
    *,
    hw_yield: float,
    profile: LoadProfile,
    cfg: TariffConfig,
    tz: ZoneInfo,
    lat: float,
    lon: float,
    start_day: date,
    days: int = 365,
    history_house: dict[datetime, float] | None = None,
    history_ev: dict[datetime, float] | None = None,
    events: list[GridEvent] | None = None,
) -> Slots:
    """365 days of slots. Uses real history where it covers the day, else the learned profile
    (re-seasonalised). Planner 'expectations' always come from the profile + weather-free PV."""
    first_local = datetime(start_day.year, start_day.month, start_day.day, tzinfo=tz)
    first = first_local.astimezone(UTC)
    end_local = datetime.combine(start_day + timedelta(days=days), datetime.min.time(), tzinfo=tz)
    n = int((end_local.astimezone(UTC) - first).total_seconds() // 1800)
    starts = make_starts(first, n)
    tod = tod_minutes(starts, tz)
    imp, exp, cheap, ev, bonus = build_prices(starts, tod, cfg, events=events)

    # seasonal normaliser: the profile is de-seasonalised, so multiply back per month
    house, evl, e_house, e_ev = [], [], [], []
    months = []
    for i, s in enumerate(starts):
        loc = s.astimezone(tz)
        slot = tod[i] // 30
        f = SEASONAL[loc.month - 1]
        exp_h = profile.house(loc.date(), slot) * f
        exp_e = profile.ev[slot]
        e_house.append(exp_h)
        e_ev.append(exp_e)
        if history_house is not None and s in history_house:
            house.append(history_house[s])
            evl.append(history_ev.get(s, 0.0) if history_ev else 0.0)
        else:
            house.append(exp_h)
            evl.append(exp_e)
        months.append(f"{loc.year}-{loc.month:02d}")
        # EV charging outside the off-peak window is smart-charged (IOG) -> cheap rate
        if evl[-1] >= 0.5 and not cheap[i] and ev[i] == "":
            imp[i], cheap[i] = min(imp[i], cfg.cheap_p), True

    act, expu = synth_pv_unit(starts, lat, lon, hw_yield)
    return Slots(starts, tod, imp, exp, cheap, ev, bonus, house, evl, act, e_house, e_ev, expu, months)


def covered_days(history: dict[datetime, float] | None, tz: ZoneInfo) -> int:
    if not history:
        return 0
    return len({s.astimezone(tz).date() for s in history})
