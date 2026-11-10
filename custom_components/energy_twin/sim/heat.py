"""Heat pump (space heating + hot-water tank) and induction hob model. No Home Assistant imports.

Method, in plain terms
1. Calibrate your gas use against outdoor temperature (daily gas = base + k x heating-degree-days).
   `base` is hot water + cooking; `k` is space heating. Boiler efficiencies turn gas kWh into the
   *useful heat* you actually needed.
2. Per half-hour: space-heating demand follows (base_temp - outdoor temp); hot-water draw follows a daily shape.
3. The heat pump delivers that heat for electricity = heat / COP, where COP depends on outdoor and flow
   temperature (a Carnot-fraction model). The hot-water tank is a thermal store: it charges from
   04:00 in the cheap window, in Power Up / smart-charge slots, and as an emergency top-up.
4. Whatever the heat pump cannot deliver (undersized on cold days, tank run dry) falls back to resistive
   heating at COP 1 - so a too-small heat pump shows up as a bigger bill, honestly.

Accuracy: hot water and cooking are fairly well determined by your gas history; space heating is
approximate (COP, flow temperature, emitters and heat-pump sizing all matter) - treat results as ranges.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .models import SLOT_H

KWH_PER_L_K = 0.001163                      # specific heat of water, kWh per litre per kelvin
UK_MONTH_TEMP = [4.5, 4.8, 6.5, 8.5, 11.5, 14.5, 16.5, 16.3, 14.0, 10.5, 7.0, 5.0]   # monthly mean outdoor C
EMERGENCY_TANK_KWH = 1.5                    # top up from the grid at any price below this
FALLBACK_HDD_YEAR = 2066.0                  # heating-degree-days (base 15.5C) for the synthetic UK year


@dataclass(frozen=True)
class HeatParams:
    enabled: bool = False
    # hot water tank
    tank_litres: float = 270.0
    tank_setpoint_c: float = 50.0
    cold_in_c: float = 10.0
    tank_loss_kwh_day: float = 0.0           # 0 = estimate from volume
    legionella_c: float = 60.0               # weekly boost (Sunday); <= setpoint disables it
    dhw_start_min: int = 4 * 60              # main charge starts here, inside the cheap window
    # heat pump
    hp_elec_max_kw: float = 3.0
    hp_capacity_kw: float = 8.0              # heat output at 7C outdoor
    flow_temp_c: float = 45.0
    carnot_frac: float = 0.42
    base_temp_c: float = 15.5
    # what it replaces
    boiler_eff_heating: float = 0.85
    boiler_eff_dhw: float = 0.72
    cooking_gas_kwh_day: float = 1.0
    induction_ratio: float = 0.5             # electric kWh per gas kWh of cooking
    fallback_annual_gas_kwh: float = 11500.0
    # economics
    gas_unit_p: float = 6.0
    gas_standing_p_day: float = 30.0
    gas_disconnect: bool = True

    @property
    def loss_day(self) -> float:
        return self.tank_loss_kwh_day or 0.0067 * self.tank_litres

    @property
    def e_max(self) -> float:
        return self.tank_litres * KWH_PER_L_K * max(self.tank_setpoint_c - self.cold_in_c, 1.0)

    @property
    def e_leg(self) -> float:
        return self.tank_litres * KWH_PER_L_K * max(self.legionella_c - self.cold_in_c, 1.0)


def cop(t_out: float, t_flow: float, carnot: float) -> float:
    """Carnot-fraction COP: warmer outside or lower flow temperature = better."""
    hot = t_flow + 273.15 + 1.0
    cold = t_out - 5.0 + 273.15                        # evaporator runs cooler than the air
    return max(1.0, min(6.0, carnot * hot / max(hot - cold, 5.0)))


def capacity_kw(t_out: float, rated_kw: float) -> float:
    return rated_kw * min(1.3, max(0.4, 1.0 - 0.03 * (7.0 - t_out)))


def _norm(vals: list[float]) -> list[float]:
    s = sum(vals)
    return [v / s for v in vals]


def _bump(h: float, mu: float, sd: float) -> float:
    return math.exp(-0.5 * ((h - mu) / sd) ** 2)


DHW_SHAPE = _norm([0.05 + _bump(i / 2, 7.2, 0.9) + 0.35 * _bump(i / 2, 13.0, 1.5) + 0.9 * _bump(i / 2, 19.5, 1.6)
                   for i in range(48)])
COOK_SHAPE = _norm([0.01 + 0.3 * _bump(i / 2, 7.75, 0.6) + 0.25 * _bump(i / 2, 12.5, 0.8) + _bump(i / 2, 18.25, 1.0)
                    for i in range(48)])


# --------------------------------------------------------------------------- temperatures
def synth_temps(starts: list[datetime], tz: ZoneInfo, seed: int = 11) -> tuple[list[float], list[float]]:
    """(actual, expected) outdoor temperature per slot: monthly means + diurnal swing (+ seeded weather)."""
    rng = random.Random(seed)
    exp, act, noise, last_day = [], [], 0.0, None
    for s in starts:
        loc = s.astimezone(tz)
        doy = loc.timetuple().tm_yday
        pos = (doy - 15.0) / 30.4375                           # 0 = mid-January
        i = int(math.floor(pos)) % 12
        f = pos - math.floor(pos)
        mean = UK_MONTH_TEMP[i] * (1 - f) + UK_MONTH_TEMP[(i + 1) % 12] * f
        h = loc.hour + loc.minute / 60
        t = mean + 3.0 * math.cos(2 * math.pi * (h - 15.0) / 24.0)
        if loc.date() != last_day:
            noise, last_day = 0.7 * noise + rng.gauss(0, 2.5), loc.date()
        exp.append(t)
        act.append(t + noise)
    return act, exp


# --------------------------------------------------------------------------- calibration from gas history
@dataclass
class GasModel:
    base_kwh_day: float           # gas kWh/day that does not depend on weather (hot water + cooking)
    k_per_degday: float           # gas kWh per heating degree-day
    days: int = 0
    r2: float = 0.0
    source: str = "default"


def default_gas_model(p: HeatParams) -> GasModel:
    annual = p.fallback_annual_gas_kwh
    return GasModel(0.23 * annual / 365.0, 0.77 * annual / FALLBACK_HDD_YEAR, 0, 0.0, "default")


def calibrate(gas_daily: dict[date, float], temp_daily: dict[date, float], p: HeatParams) -> GasModel:
    """Least-squares fit: daily gas = base + k x HDD. Needs ~3 weeks with both gas and temperature."""
    pts = [(max(0.0, p.base_temp_c - temp_daily[d]), g) for d, g in gas_daily.items()
           if d in temp_daily and g and g > 0]
    n = len(pts)
    if n < 21:
        return default_gas_model(p)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx < 1e-6:                                    # no weather variation (e.g. all summer) -> can't split
        return default_gas_model(p)
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for _, y in pts) or 1.0
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in pts)
    if b <= 0 or a < 0.3 * p.cooking_gas_kwh_day:
        return default_gas_model(p)
    return GasModel(a, b, n, max(0.0, 1 - ss_res / ss_tot), "history")


# --------------------------------------------------------------------------- the simulation
@dataclass
class HeatResult:
    hp_kw: list[float]            # heat pump (+ resistive backup) electricity
    induction_kw: list[float]
    new_kw: list[float]           # hp + induction
    gas_kw: list[float]           # gas the old boiler + hob would have burned
    tank_start: list[float]       # tank heat (kWh) at the start of each slot
    tank_end: list[float]
    cop_space: list[float]
    cop_dhw: list[float]
    dhw_kw: list[float]
    space_kw: list[float]
    backup_kw: list[float]
    charging: list[bool]


def simulate_heat(tod: list[int], cheap: list[bool], opp: list[bool], weekday: list[int], day_key: list,
                  temps: list[float], p: HeatParams, gm: GasModel, tank0: float | None = None) -> HeatResult:
    n, dt = len(tod), SLOT_H
    e_max = p.e_max
    dhw_day = max(0.0, gm.base_kwh_day - p.cooking_gas_kwh_day) * p.boiler_eff_dhw     # useful kWh/day at the tap
    loss_slot = p.loss_day / 48.0
    H = gm.k_per_degday * p.boiler_eff_heating / 24.0                                   # kW of heat per kelvin
    tank = e_max * 0.85 if tank0 is None else min(max(tank0, 0.0), p.e_leg)
    defer, leg_day = 0.0, None
    z = lambda: [0.0] * n  # noqa: E731
    r = HeatResult(hp_kw=z(), induction_kw=z(), new_kw=z(), gas_kw=z(), tank_start=z(), tank_end=z(),
                   cop_space=z(), cop_dhw=z(), dhw_kw=z(), space_kw=z(), backup_kw=z(), charging=[False] * n)
    for t in range(n):
        T, slot = temps[t], tod[t] // 30
        c_s, c_d = cop(T, p.flow_temp_c, p.carnot_frac), cop(T, p.tank_setpoint_c, p.carnot_frac)
        r.tank_start[t] = tank
        draw = dhw_day * DHW_SHAPE[slot]
        tank -= draw + loss_slot * (tank / e_max if e_max > 0 else 0.0)
        short = 0.0
        if tank < 0:
            short, tank = -tank, 0.0                                  # tank ran dry: resistive top-up of the draw
        target = e_max
        if (p.legionella_c > p.tank_setpoint_c and weekday[t] == 6 and leg_day != day_key[t]
                and cheap[t] and ((tod[t] - p.dhw_start_min) % 1440) < 360):
            target = p.e_leg
        main = cheap[t] and (((tod[t] - p.dhw_start_min) % 1440) < 360 or tank < dhw_day)
        want = (main or opp[t] or tank < EMERGENCY_TANK_KWH) and tank < target - 1e-6
        dhw_elec = 0.0
        if want:
            add = min(p.hp_elec_max_kw * c_d * dt, target - tank)
            tank += add
            dhw_elec = add / c_d / dt
            r.charging[t] = True
            if target > e_max + 1e-6 and tank >= target - 1e-6:
                leg_day = day_key[t]
        demand_kw = H * max(0.0, p.base_temp_c - T)
        need = demand_kw * dt + defer
        deliver = 0.0 if dhw_elec > 0 else min(need, capacity_kw(T, p.hp_capacity_kw) * dt)
        defer = need - deliver
        backup_heat = 0.0
        if defer > 2 * capacity_kw(T, p.hp_capacity_kw) * dt:          # can't catch up: resistive heating
            backup_heat = defer - 2 * capacity_kw(T, p.hp_capacity_kw) * dt
            defer -= backup_heat
        space_elec = deliver / c_s / dt
        backup_elec = (backup_heat + short) / dt                       # COP 1
        cook = p.cooking_gas_kwh_day * COOK_SHAPE[slot]
        r.dhw_kw[t], r.space_kw[t], r.backup_kw[t] = dhw_elec, space_elec, backup_elec
        r.hp_kw[t] = dhw_elec + space_elec + backup_elec
        r.induction_kw[t] = cook * p.induction_ratio / dt
        r.new_kw[t] = r.hp_kw[t] + r.induction_kw[t]
        r.gas_kw[t] = demand_kw / p.boiler_eff_heating + draw / dt / p.boiler_eff_dhw + cook / dt
        r.tank_end[t], r.cop_space[t], r.cop_dhw[t] = tank, c_s, c_d
    return r


def horizon(starts: list[datetime], tod: list[int], tz: ZoneInfo, cheap: list[bool], event: list[str],
            in_window: list[bool], temps: list[float], p: HeatParams, gm: GasModel,
            tank0: float | None = None) -> HeatResult:
    """Convenience wrapper: derives weekday/date keys and the opportunistic (Power Up / dispatch) flags."""
    loc = [s.astimezone(tz) for s in starts]
    opp = [cheap[i] and (not in_window[i] or event[i] == "power_up") for i in range(len(starts))]
    # a smart-charge slot or Power Up outside the normal window is an opportunity to top up the tank
    return simulate_heat(tod, cheap, opp, [d.weekday() for d in loc], [d.date() for d in loc], temps, p, gm, tank0)


def annual_gas_kwh(starts: list[datetime], tod: list[int], tz: ZoneInfo, p: HeatParams, gm: GasModel) -> float:
    """Gas the current boiler + hob burn in a year (same weather + model as the heat pump run, so they agree)."""
    n = len(starts)
    act, _ = synth_temps(starts, tz)
    loc = [s.astimezone(tz) for s in starts]
    res = simulate_heat(tod, [False] * n, [False] * n, [d.weekday() for d in loc], [d.date() for d in loc], act, p, gm)
    return sum(res.gas_kw) * SLOT_H * 365.0 / max(n / 48.0, 1.0)


def summarise(res: HeatResult, cheap: list[bool], p: HeatParams) -> dict:
    dt = SLOT_H
    hp = sum(res.hp_kw) * dt
    cheap_hp = sum(res.hp_kw[i] for i in range(len(cheap)) if cheap[i]) * dt
    days = max(1.0, len(cheap) / 48)
    f = 365.0 / days
    dhw, sp, bk = sum(res.dhw_kw) * dt, sum(res.space_kw) * dt, sum(res.backup_kw) * dt
    heat_sp = sum(res.space_kw[i] * res.cop_space[i] for i in range(len(cheap))) * dt
    heat_dhw = sum(res.dhw_kw[i] * res.cop_dhw[i] for i in range(len(cheap))) * dt
    return {
        "hp_electricity_kwh": round(hp * f), "dhw_electricity_kwh": round(dhw * f),
        "space_electricity_kwh": round(sp * f), "backup_electricity_kwh": round(bk * f),
        "induction_kwh": round(sum(res.induction_kw) * dt * f), "gas_kwh_displaced": round(sum(res.gas_kw) * dt * f),
        "cheap_share_pct": round(100 * cheap_hp / hp, 1) if hp > 0 else 0.0,
        "scop_space": round(heat_sp / sp, 2) if sp > 0 else None,
        "scop_dhw": round(heat_dhw / dhw, 2) if dhw > 0 else None,
        "peak_hp_kw": round(max(res.hp_kw), 2) if res.hp_kw else 0.0,
        "tank_litres": p.tank_litres, "days_simulated": round(days),
    }
