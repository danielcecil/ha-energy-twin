"""Builds per-slot price / flag arrays from config + live Octopus data + grid events."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import SLOT_H, GridEvent, TariffConfig

UTC = timezone.utc


def floor_half_hour(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


def make_starts(first: datetime, n: int) -> list[datetime]:
    return [first + timedelta(minutes=30 * i) for i in range(n)]


def tod_minutes(starts: list[datetime], tz: ZoneInfo) -> list[int]:
    out = []
    for s in starts:
        loc = s.astimezone(tz)
        out.append(loc.hour * 60 + loc.minute)
    return out


def _covers(a: datetime, b: datetime, s: datetime) -> bool:
    return a <= s < b


def build_prices(
    starts: list[datetime],
    tod: list[int],
    cfg: TariffConfig,
    *,
    import_rates: dict[datetime, float] | None = None,   # UTC slot start -> p/kWh
    export_rates: dict[datetime, float] | None = None,
    dispatches: list[tuple[datetime, datetime]] | None = None,
    events: list[GridEvent] | None = None,
):
    """Return (import_p, export_p, cheap, event, bonus_p) lists.

    Priority: configured tariff -> live Octopus rates (if supplied) -> smart-charge dispatches
    (always at the cheap rate) -> Power Up/Down events.
    """
    n = len(starts)
    imp, exp, cheap, ev, bonus = [0.0] * n, [0.0] * n, [False] * n, [""] * n, [0.0] * n
    for i, s in enumerate(starts):
        in_win = cfg.in_cheap_window(tod[i])
        p = cfg.cheap_p if in_win else cfg.standard_p
        if import_rates and s in import_rates:
            p = import_rates[s]
        c = in_win or (import_rates is not None and s in import_rates and p <= cfg.cheap_p + 0.5)
        for a, b in dispatches or ():
            if _covers(a, b, s):
                p, c = min(p, cfg.cheap_p), True
                break
        x = export_rates[s] if export_rates and s in export_rates else cfg.export_p
        b_p, kind = 0.0, ""
        for e in events or ():
            if _covers(e.start, e.end, s):
                kind = e.kind
                if kind == "power_up":
                    p = e.import_p if e.import_p is not None else cfg.power_up_import_p
                    c, x = True, 0.0                      # assume no export credit while charging
                else:
                    x = e.export_p if e.export_p is not None else cfg.power_down_export_p
                    b_p = e.bonus_p if e.bonus_p is not None else cfg.power_down_bonus_p
                    c = False
                break
        imp[i], exp[i], cheap[i], ev[i], bonus[i] = p, x, c, kind, b_p
    return imp, exp, cheap, ev, bonus


def events_to_slots(events: list[GridEvent], now: datetime, horizon_h: float = 48) -> list[GridEvent]:
    end = now + timedelta(hours=horizon_h)
    return [e for e in events if e.end > now and e.start < end]


__all__ = ["build_prices", "floor_half_hour", "make_starts", "tod_minutes", "events_to_slots", "SLOT_H"]
