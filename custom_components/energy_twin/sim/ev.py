"""Per-vehicle EV charging plan (no Home Assistant imports).

No statistics, no weekday guesses. Each car is planned from what it is doing *now*:

* plugged in at home? -> it will charge the energy it still needs, from its actual battery level up to its charge
  limit (or, for a car that does not report a level, a session size you entered, or "keep going" while it is drawing
  power). A car left plugged in at its target needs nothing, so nothing is planned.
* not plugged in at home (or away) -> nothing is planned. If it gets plugged in later the plan is rebuilt from that
  moment, with whatever battery level it has then.
* "iog" cars use Octopus smart-charge dispatch slots (any time of day) first, then the off-peak window; "offpeak"
  cars only the off-peak window, from its start, at the charger's rate. A big top-up carries on into the next
  window(s) until it is done.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import EV_ON_KW, SLOT_H, TariffConfig

CHARGE_EFF = 0.9          # wall energy needed per kWh stored in the pack

_FALSE_PLUG = {"off", "false", "no", "unplugged", "disconnected", "not_connected", "not_plugged", "not connected",
               "not plugged", "unplug", "idle_disconnected"}
_TRUE_PLUG = {"on", "true", "yes", "plugged", "plugged_in", "plugged in", "connected", "charging", "complete",
              "completed", "waiting", "ready", "stopped", "starting", "pending", "preparing", "finishing",
              "suspended", "fully_charged", "awaiting_start", "paused", "no_power", "nopower"}
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
    return False                        # not_home / away / any other zone name


@dataclass(frozen=True)
class Vehicle:
    name: str
    mode: str = "offpeak"                 # "iog" | "offpeak"
    charger_kw: float = 7.0
    capacity_kwh: float = 60.0
    target_soc_pct: float = 80.0          # fixed target, used when the car has no charge-limit entity
    session_kwh: float = 0.0              # only for a car with no battery-level entity: energy per charge (0 = unknown)
    plug_states: tuple[str, ...] = ()     # optional override: states that mean "plugged in"
    home_states: tuple[str, ...] = ()
    power_entity: str | None = None
    plug_entity: str | None = None
    home_entity: str | None = None
    soc_entity: str | None = None
    energy_stat: str | None = None
    target_soc_entity: str | None = None  # e.g. a Tesla's charge-limit number: the real target beats a fixed one
    device_id: str | None = None

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
            soc_entity=d.get("soc_entity") or None, energy_stat=d.get("energy_stat") or None,
            target_soc_entity=d.get("target_soc_entity") or None, device_id=d.get("device_id") or None)


@dataclass
class VehicleInfo:
    """Live state of one car as read from Home Assistant (all optional)."""

    plugged: bool | None = None
    home: bool | None = None
    soc_pct: float | None = None
    target_soc_pct: float | None = None   # from the car's own charge-limit entity, when there is one
    live_kw: float = 0.0
    delivered_kwh: float = 0.0           # delivered so far in the current charge session
    raw_plug: str | None = None
    raw_home: str | None = None


def needed_kwh(v: Vehicle, info: VehicleInfo) -> float | None:
    """Wall energy this car still wants, or None when there is no way to know."""
    if info.soc_pct is not None:
        target = info.target_soc_pct if info.target_soc_pct else v.target_soc_pct
        return max(0.0, (target - info.soc_pct) / 100.0 * v.capacity_kwh) / CHARGE_EFF
    if v.session_kwh > 0:
        return max(0.0, v.session_kwh - info.delivered_kwh)
    return None


def is_charging(info: VehicleInfo) -> bool:
    return info.live_kw >= EV_ON_KW


def is_present(info: VehicleInfo) -> bool:
    """Connected at home (an unknown home status counts as home; a car that is drawing power is clearly connected)."""
    return (info.plugged is True or is_charging(info)) and info.home is not False


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


def _fill(kw: list[float], idxs, energy: float, power: float) -> float:
    """Charge ASAP at `power` over `idxs` until `energy` is delivered. Returns energy left over."""
    for i in idxs:
        if energy <= 1e-9:
            break
        e = min(power * SLOT_H, energy)
        kw[i] += e / SLOT_H
        energy -= e
    return energy


def build_ev_load(starts: list[datetime], tod: list[int], tz: ZoneInfo, tariff: TariffConfig,
                  vehicles: list[Vehicle], infos: dict[str, VehicleInfo],
                  dispatch_slots: set[datetime]) -> tuple[list[float], list[dict]]:
    """Planned EV kW per slot for the horizon, plus a plain-English reason per car."""
    n = len(starts)
    kw = [0.0] * n
    blocks = _blocks([tariff.in_cheap_window(t) for t in tod])
    notes = []
    for v in vehicles:
        info = infos.get(v.name) or VehicleInfo()
        mine = [0.0] * n
        charging, present = is_charging(info), is_present(info)
        need: float | None = 0.0
        unknown = False
        planned = via_dispatch = 0.0
        if not present:
            why = ("plugged in but away from home - nothing planned" if info.plugged and info.home is False
                   else "not plugged in at home - nothing planned")
        else:
            need = needed_kwh(v, info)
            unknown = need is None
            if need is None:
                # no battery level and no session size: while it is drawing power, assume it carries on through the
                # next off-peak window; if it isn't charging there is nothing to base a plan on
                need = (v.charger_kw * SLOT_H * (blocks[0][1] - blocks[0][0] + 1)) if (charging and blocks) else 0.0
                why = ("plugged in; no battery level, assuming it keeps charging through the off-peak window" if need
                       else "plugged in; no battery level or session size, so nothing planned")
            elif need <= 0.05:
                why = "plugged in but already at its charge limit - nothing to charge"
            else:
                why = "plugged in at home - charging what it still needs"
            left = need
            if v.mode == "iog" and left > 0:
                disp = [i for i in range(n) if starts[i] in dispatch_slots]
                if disp:                                        # planned smart-charge slots are facts, not guesses
                    left = _fill(mine, disp, left, v.charger_kw)
                    via_dispatch = need - left
            for a, b in blocks:                                  # then off-peak windows, carrying on into the next
                if left <= 1e-9:
                    break
                left = _fill(mine, [i for i in range(a, b + 1) if mine[i] == 0.0], left, v.charger_kw)
            planned = need - left
            if charging and n:                                   # what it is drawing right now is known
                mine[0] = max(mine[0], info.live_kw)
        for i in range(n):
            kw[i] += mine[i]
        notes.append({"vehicle": v.name, "mode": v.mode, "plugged": info.plugged, "home": info.home,
                      "raw_plug_state": info.raw_plug, "raw_home_state": info.raw_home, "soc_pct": info.soc_pct,
                      "target_soc_pct": info.target_soc_pct or v.target_soc_pct, "charging_now": charging,
                      "needed_kwh": None if unknown else round(need or 0.0, 1), "planned_kwh": round(planned, 1),
                      "expected_kwh_tonight": round(planned, 1), "via_dispatch_kwh": round(via_dispatch, 1),
                      "why": why})
    return kw, notes
