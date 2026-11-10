"""Plain dataclasses shared by the simulation engine (no Home Assistant imports)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

SLOT_H = 0.5          # Octopus settles in half-hours
EV_ON_KW = 0.5        # a slot with >= this much EV load is "car is charging"


@dataclass(frozen=True)
class Hardware:
    """The kit being modelled. Set battery_kwh / pv_kwp to 0 to turn either off."""

    pv_kwp: float = 0.0
    inverter_kw: float = 5.0          # hybrid inverter AC rating (clips PV, caps battery AC power)
    pv_annual_yield: float = 950.0    # kWh per kWp per year (annual model only)
    battery_kwh: float = 0.0          # USABLE capacity
    battery_max_kw: float = 5.0       # battery's own charge/discharge limit
    round_trip_eff: float = 0.90
    reserve_pct: float = 10.0         # never discharge below this
    export_limit_kw: float = 3.68     # G98 single-phase default; G99 may allow more
    cycle_cost_p: float = 0.0         # optional wear cost per kWh discharged
    import_limit_kw: float = 0.0      # main-fuse limit on grid import (0 = unlimited)

    @property
    def eta(self) -> float:
        return math.sqrt(self.round_trip_eff)

    @property
    def emax(self) -> float:
        return self.battery_kwh

    @property
    def emin(self) -> float:
        return self.battery_kwh * self.reserve_pct / 100.0

    @property
    def charge_kw(self) -> float:
        return min(self.battery_max_kw, self.inverter_kw) if self.battery_kwh > 0 else 0.0

    def discharge_cap(self, pv_kw: float) -> float:
        """AC discharge headroom: hybrid inverter shares its rating with PV output."""
        if self.battery_kwh <= 0:
            return 0.0
        return max(0.0, min(self.battery_max_kw, self.inverter_kw - pv_kw))


@dataclass(frozen=True)
class Strategy:
    name: str = "optimised"           # "rules" | "optimised"
    margin_pct: float = 15.0          # safety margin on expected evening demand
    export_from_min: int = 16 * 60    # rules: earliest local time to export battery surplus
    export_margin_p: float = 1.0      # only export if it beats re-buying by this many pence
    arbitrage: bool = True            # allow charging from cheap grid purely to export
    allow_battery_export: bool = True
    # optimised: local times (minutes) at which the plan is re-solved from the actual state
    # (the live twin re-plans every few minutes; this mimics that in the annual run)
    replan_min: tuple = (6 * 60, 12 * 60, 16 * 60, 19 * 60)


@dataclass(frozen=True)
class TariffConfig:
    cheap_start_min: int = 23 * 60 + 30
    cheap_end_min: int = 5 * 60 + 30
    cheap_p: float = 7.0
    standard_p: float = 27.0
    export_p: float = 15.0
    power_up_import_p: float = 0.0      # placeholder - set to the real event terms
    power_down_export_p: float = 30.0   # placeholder - set to the real event terms
    power_down_bonus_p: float = 0.0     # extra p per kWh the battery discharges in an event

    def in_cheap_window(self, tod_min: int) -> bool:
        s, e = self.cheap_start_min, self.cheap_end_min
        return (tod_min >= s or tod_min < e) if s > e else (s <= tod_min < e)


@dataclass
class GridEvent:
    """A Power Up (charge everything) or Power Down (discharge + export) window."""

    kind: str                          # "power_up" | "power_down"
    start: datetime
    end: datetime
    import_p: float | None = None
    export_p: float | None = None
    bonus_p: float | None = None


@dataclass
class Slots:
    """A timeline of half-hour slots. All lists have equal length."""

    start: list[datetime]              # tz-aware UTC
    tod_min: list[int]                 # local minute-of-day
    import_p: list[float]
    export_p: list[float]
    cheap: list[bool]
    event: list[str]                   # "" | "power_up" | "power_down"
    bonus_p: list[float]
    house_kw: list[float]              # non-EV household load (actual)
    ev_kw: list[float]                 # EV charging load (actual)
    pv_unit: list[float]               # kW per kWp, actual
    exp_house_kw: list[float]          # what the planner *expects*
    exp_ev_kw: list[float]
    exp_pv_unit: list[float]
    month: list[str] = field(default_factory=list)   # "YYYY-MM" for reporting
    # extra electrified load (heat pump + induction hob). It is NOT in the baseline bill unless asked.
    new_kw: list[float] = field(default_factory=list)
    exp_new_kw: list[float] = field(default_factory=list)
    heat: dict | None = None                          # annual heat-pump summary (see sim/heat.py)

    @property
    def n(self) -> int:
        return len(self.start)

    def view(self, a: int, b: int) -> "Slots":
        return Slots(**{k: (v[a:b] if isinstance(v, list) else v) for k, v in self.__dict__.items()})

    def as_expected(self) -> "Slots":
        """Copy where 'actual' == 'expected' (used when planning ahead)."""
        c = Slots(**{k: (list(v) if isinstance(v, list) else v) for k, v in self.__dict__.items()})
        c.house_kw, c.ev_kw, c.pv_unit = list(self.exp_house_kw), list(self.exp_ev_kw), list(self.exp_pv_unit)
        return c


@dataclass
class Result:
    battery_kw: list[float]            # AC side, + = charging
    grid_kw: list[float]               # + = import, - = export
    export_kw: list[float]
    soc: list[float]                   # stored kWh at END of slot
    pv_kw: list[float]
    cost_p: list[float]
    baseline_p: list[float]
    cost_total_p: float = 0.0
    baseline_total_p: float = 0.0
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    pv_kwh: float = 0.0
    grid_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0
    battery_export_kwh: float = 0.0
    peak_import_kw: float = 0.0

    @property
    def saving_gbp(self) -> float:
        return (self.baseline_total_p - self.cost_total_p) / 100.0
