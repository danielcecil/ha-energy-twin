"""Per-vehicle EV charging model (no Home Assistant imports).

Instead of smearing an average EV profile over every night, each car is modelled on its own:

* is it plugged in / at home right now?  ->  charge it (energy from its SoC, or a learned session size)
* if not, only a learned per-weekday probability of being plugged in tonight
* "iog" cars also charge in Octopus dispatch slots (any time of day); "offpeak" cars only in the
  off-peak window, from its start, at the charger's rate.

Learning only ever happens around real sessions (energy delivered while the car is plugged in).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from .models import SLOT_H, TariffConfig

CHARGE_EFF = 0.9          # wall energy needed per kWh stored in the pack
MIN_SESSION_KWH = 1.0
FALLBACK_SESSION_KWH = 10.0
KEEP_SESSIONS = 20

_FALSE_PLUG = {"off", "false", "no", "unplugged", "disconnected", "not_connected", "not_plugged", "not connected",
               "not plugged", "unplug", "idle_disconnected"}
_TRUE_PLUG = {"on", "true", "yes", "plugged", "plugged_in", "plugged in", "connected", "charging", "complete",
              "completed", "waiting", "ready", "stopped", "starting", "pending", "preparing", "finishing",
              "suspended", "fully_charged", "awaiting_start", "paused"}
_HOME = {"home", "on", "true", "yes", "arrived", "at_home"}


def _norm(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    return None if s in ("", "unknown", "unavailable", "none") else s


def interpret_plugged(raw, override: tuple[str, ...] = ()) -> bool | None:
    """Tolerant 'is the car connected?' across car/charger integrations. None = can't tell."""
    s = _norm(raw)
    if s is None:
        return None
    if override:
        return s in override
    if s in _FALSE_PLUG or "disconnect" in s or "unplug" in s or s.startswith("not "):
        return False
    if s in _TRUE_PLUG or any(t in s for t in ("connect", "plug", "charg")):
        return True
    return None


def interpret_home(raw, override: tuple[str, ...] = ()) -> bool | None:
    """device_tracker / person / binary_sensor 'is the car at home?'. A zone name other than home = away."""
    s = _norm(raw)
    if s is None:
        return None
    if override:
        return s in override
    if s in _HOME:
        return True
    return False                        # _AWAY tokens, or any other zone name


@dataclass(frozen=True)
class Vehicle:
    name: str
    mode: str = "offpeak"                 # "iog" | "offpeak"
    charger_kw: float = 7.0
    capacity_kwh: float = 60.0
    target_soc_pct: float = 80.0
    session_kwh: float = 0.0              # 0 = learn from sessions
    plug_states: tuple[str, ...] = ()     # optional override: states that mean "plugged in"
    home_states: tuple[str, ...] = ()
    power_entity: str | None = None
    plug_entity: str | None = None
    home_entity: str | None = None
    soc_entity: str | None = None
    energy_stat: str | None = None

    @staticmethod
    def from_dict(d: dict) -> "Vehicle":
        def toks(v) -> tuple[str, ...]:
            return tuple(t.strip().lower() for t in str(v or "").split(",") if t.strip())
        return Vehicle(
            name=d["name"], mode=d.get("mode", "offpeak"), charger_kw=float(d.get("charger_kw", 7.0)),
            capacity_kwh=float(d.get("capacity_kwh", 60.0)), target_soc_pct=float(d.get("target_soc_pct", 80.0)),
            session_kwh=float(d.get("session_kwh") or 0.0), plug_states=toks(d.get("plug_states")),
            home_states=toks(d.get("home_states")), power_entity=d.get("power_entity") or None,
            plug_entity=d.get("plug_entity") or None, home_entity=d.get("home_entity") or None,
            soc_entity=d.get("soc_entity") or None, energy_stat=d.get("energy_stat") or None)


@dataclass
class VehicleLearn:
    """What we've learned about one car: session sizes and which weekdays it gets plugged in."""

    sessions: list[float] = field(default_factory=list)
    wd_plugged: list[int] = field(default_factory=lambda: [0] * 7)
    wd_seen: list[int] = field(default_factory=lambda: [0] * 7)

    def prob(self, weekday: int) -> float:
        """P(plugged in on an evening of this weekday) - Laplace smoothed, 0.5 with no data."""
        return (self.wd_plugged[weekday] + 1) / (self.wd_seen[weekday] + 2)

    def typical_kwh(self, default: float = 0.0) -> float:
        if len(self.sessions) >= 3:
            return float(median(self.sessions[-KEEP_SESSIONS:]))
        return default or FALLBACK_SESSION_KWH

    def to_dict(self) -> dict:
        return {"sessions": self.sessions[-KEEP_SESSIONS:], "wd_plugged": self.wd_plugged, "wd_seen": self.wd_seen}

    @staticmethod
    def from_dict(d: dict | None) -> "VehicleLearn":
        d = d or {}
        ok = lambda k: isinstance(d.get(k), list) and len(d[k]) == 7  # noqa: E731
        return VehicleLearn(list(d.get("sessions", []))[-KEEP_SESSIONS:],
                            list(d["wd_plugged"]) if ok("wd_plugged") else [0] * 7,
                            list(d["wd_seen"]) if ok("wd_seen") else [0] * 7)

    @staticmethod
    def combine(a: "VehicleLearn", b: "VehicleLearn") -> "VehicleLearn":
        return VehicleLearn((a.sessions + b.sessions)[-KEEP_SESSIONS:],
                            [x + y for x, y in zip(a.wd_plugged, b.wd_plugged)],
                            [x + y for x, y in zip(a.wd_seen, b.wd_seen)])

    def end_session(self, kwh: float) -> bool:
        if kwh >= MIN_SESSION_KWH:
            self.sessions = (self.sessions + [round(kwh, 2)])[-KEEP_SESSIONS:]
            return True
        return False

    def note_day(self, weekday: int, plugged: bool) -> None:
        self.wd_seen[weekday] += 1
        if plugged:
            self.wd_plugged[weekday] += 1


@dataclass
class VehicleInfo:
    """Live state of one car as read from Home Assistant (all optional)."""

    plugged: bool | None = None
    home: bool | None = None
    soc_pct: float | None = None
    live_kw: float = 0.0
    delivered_kwh: float = 0.0           # delivered so far in the current session
    raw_plug: str | None = None
    raw_home: str | None = None


def needed_kwh(v: Vehicle, learn: VehicleLearn, info: VehicleInfo) -> float:
    """Wall energy this car still wants in its current/next charge."""
    if info.soc_pct is not None:
        return max(0.0, (v.target_soc_pct - info.soc_pct) / 100.0 * v.capacity_kwh) / CHARGE_EFF
    base = v.session_kwh or learn.typical_kwh()
    return max(0.0, base - info.delivered_kwh)


def tonight_probability(info: VehicleInfo, learn: VehicleLearn, weekday: int) -> float:
    if info.plugged and info.home is not False:
        return 1.0                      # it's connected at home: it will charge
    if info.delivered_kwh >= MIN_SESSION_KWH and info.live_kw >= 0.3:
        return 1.0
    return learn.prob(weekday)          # not connected: maybe tonight, per its history


def _blocks(flags: list[bool]) -> list[tuple[int, int]]:
    out, i, n = [], 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j + 1 < n and flags[j + 1]:
                j += 1
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def _evening_weekday(start: datetime, tz: ZoneInfo, tariff: TariffConfig) -> int:
    """Weekday of the evening the block belongs to (a block starting after midnight belongs to the day before)."""
    loc = start.astimezone(tz)
    mins = loc.hour * 60 + loc.minute
    if mins < tariff.cheap_end_min and tariff.cheap_start_min > tariff.cheap_end_min:
        loc -= timedelta(days=1)
    return loc.weekday()


def _fill(kw: list[float], idxs, energy: float, power: float, weight: float) -> float:
    """Charge ASAP at `power` over `idxs` until `energy` is delivered. Returns energy left over."""
    for i in idxs:
        if energy <= 1e-9:
            break
        e = min(power * SLOT_H, energy)
        kw[i] += e / SLOT_H * weight
        energy -= e
    return energy


def build_ev_load(starts: list[datetime], tod: list[int], tz: ZoneInfo, tariff: TariffConfig,
                  vehicles: list[Vehicle], infos: dict[str, VehicleInfo], learns: dict[str, VehicleLearn],
                  dispatch_slots: set[datetime]) -> tuple[list[float], list[dict]]:
    """Expected EV kW per slot for the planning horizon, plus a per-vehicle explanation."""
    n = len(starts)
    kw = [0.0] * n
    window = [tariff.in_cheap_window(t) for t in tod]
    blocks = _blocks(window)
    notes = []
    for v in vehicles:
        info = infos.get(v.name) or VehicleInfo()
        lrn = learns.get(v.name) or VehicleLearn()
        mine = [0.0] * n
        e0 = needed_kwh(v, lrn, info)
        wd0 = _evening_weekday(starts[blocks[0][0]], tz, tariff) if blocks else starts[0].astimezone(tz).weekday()
        p0 = tonight_probability(info, lrn, wd0)
        left, via_dispatch = e0, 0.0
        if v.mode == "iog" and e0 > 0:
            disp = [i for i in range(n) if starts[i] in dispatch_slots]
            if disp:                                    # planned smart-charge slots are facts, not guesses
                left = _fill(mine, disp, e0, v.charger_kw, 1.0)
                via_dispatch = e0 - left
                p0 = 1.0
        if blocks and left > 0:
            a, b = blocks[0]
            free = [i for i in range(a, b + 1) if mine[i] == 0.0]
            _fill(mine, free, left, v.charger_kw, p0)
        for k, (a, b) in enumerate(blocks[1:], start=1):          # later nights: expected, by weekday
            wd = _evening_weekday(starts[a], tz, tariff)
            _fill(mine, range(a, b + 1), lrn.typical_kwh(v.session_kwh), v.charger_kw, lrn.prob(wd))
        if info.live_kw >= 0.5 and n:                            # what it is drawing right now is known
            mine[0] = max(mine[0], info.live_kw)
        for i in range(n):
            kw[i] += mine[i]
        notes.append({"vehicle": v.name, "mode": v.mode, "plugged": info.plugged, "home": info.home,
                      "raw_plug_state": info.raw_plug, "raw_home_state": info.raw_home, "soc_pct": info.soc_pct,
                      "needed_kwh": round(e0, 1), "probability": round(p0, 2),
                      "expected_kwh_tonight": round(e0 * p0, 1), "via_dispatch_kwh": round(via_dispatch, 1),
                      "session_kwh_learned": round(lrn.typical_kwh(v.session_kwh), 1),
                      "learned_sessions": len(lrn.sessions)})
    return kw, notes


def year_load(starts: list[datetime], tod: list[int], tz: ZoneInfo, tariff: TariffConfig, vehicles: list[Vehicle],
              learns: dict[str, VehicleLearn]) -> tuple[list[float], list[float]]:
    """(actual, expected) EV kW for a year. Expected is probability-weighted; actual is lumpy but
    deterministic: a car charges on a night whenever its running probability total crosses 1."""
    n = len(starts)
    act, exp = [0.0] * n, [0.0] * n
    blocks = _blocks([tariff.in_cheap_window(t) for t in tod])
    for v in vehicles:
        lrn = learns.get(v.name) or VehicleLearn()
        kwh, acc = lrn.typical_kwh(v.session_kwh), 0.0
        for a, b in blocks:
            p = lrn.prob(_evening_weekday(starts[a], tz, tariff))
            _fill(exp, range(a, b + 1), kwh, v.charger_kw, p)
            acc += p
            if acc >= 1.0:
                acc -= 1.0
                _fill(act, range(a, b + 1), kwh, v.charger_kw, 1.0)
    return act, exp


def learn_from_rows(rows: list[tuple[datetime, float]], tz: ZoneInfo) -> VehicleLearn:
    """Mine hourly kWh history for sessions: one 'night' = noon to noon. Needs >=20 h of data per night."""
    nights: dict = {}
    hours: dict = {}
    for start, kwh in rows:
        loc = start.astimezone(tz)
        key = loc.date() if loc.hour >= 12 else loc.date() - timedelta(days=1)
        nights[key] = nights.get(key, 0.0) + max(0.0, kwh)
        hours[key] = hours.get(key, 0) + 1
    out = VehicleLearn()
    for key in sorted(nights):
        if hours[key] < 20:
            continue
        plugged = nights[key] >= MIN_SESSION_KWH
        out.note_day(key.weekday(), plugged)
        if plugged:
            out.sessions.append(round(nights[key], 2))
    out.sessions = out.sessions[-KEEP_SESSIONS:]
    return out
