"""Annual what-if: run a year of slots, attribute savings, sweep hardware sizes."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .engine import simulate
from .finance import Costs, FinanceTerms, evaluate_finance
from .models import Hardware, Result, Slots, Strategy


@dataclass
class AnnualResult:
    days: int
    baseline_gbp: float
    scenario_gbp: float
    saving_gbp: float
    saving_pv_only_gbp: float | None
    saving_battery_only_gbp: float | None
    pv_kwh: float
    import_kwh: float
    export_kwh: float
    grid_charge_kwh: float
    battery_export_kwh: float
    cycles: float
    peak_import_kw: float = 0.0
    monthly: list[dict] = field(default_factory=list)

    def scaled(self, factor: float) -> "AnnualResult":
        f = factor
        return replace(self, saving_gbp=self.saving_gbp * f,
                       baseline_gbp=self.baseline_gbp * f, scenario_gbp=self.scenario_gbp * f,
                       saving_pv_only_gbp=None if self.saving_pv_only_gbp is None else self.saving_pv_only_gbp * f,
                       saving_battery_only_gbp=None if self.saving_battery_only_gbp is None else self.saving_battery_only_gbp * f)


def monthly_breakdown(sl: Slots, res: Result) -> list[dict]:
    agg: dict[str, list[float]] = {}
    for i, m in enumerate(sl.month):
        a = agg.setdefault(m, [0.0, 0.0])
        a[0] += res.baseline_p[i]
        a[1] += res.cost_p[i]
    return [{"month": m, "baseline": round(b / 100, 2), "scenario": round(c / 100, 2),
             "saving": round((b - c) / 100, 2)} for m, (b, c) in sorted(agg.items())]


def run(sl: Slots, hw: Hardware, st: Strategy, chunk_tod_min: int, baseline_with_new: bool = False) -> Result:
    return simulate(sl, hw, st, chunk_tod_min=chunk_tod_min, baseline_with_new=baseline_with_new)


def evaluate(sl: Slots, hw: Hardware, st: Strategy, chunk_tod_min: int, attribution: bool = True,
             baseline_with_new: bool = False) -> AnnualResult:
    bw = baseline_with_new
    full = run(sl, hw, st, chunk_tod_min, bw)
    pv_only = batt_only = None
    if attribution:
        if hw.pv_kwp > 0 and hw.battery_kwh > 0:
            pv_only = run(sl, replace(hw, battery_kwh=0.0), st, chunk_tod_min, bw).saving_gbp
            batt_only = run(sl, replace(hw, pv_kwp=0.0), st, chunk_tod_min, bw).saving_gbp
        elif hw.pv_kwp > 0:
            pv_only = full.saving_gbp
        elif hw.battery_kwh > 0:
            batt_only = full.saving_gbp
    days = max(1, round(sl.n / 48))
    cap = max(hw.emax - hw.emin, 1e-9)
    return AnnualResult(
        days=days,
        baseline_gbp=full.baseline_total_p / 100,
        scenario_gbp=full.cost_total_p / 100,
        saving_gbp=full.saving_gbp,
        saving_pv_only_gbp=pv_only,
        saving_battery_only_gbp=batt_only,
        pv_kwh=full.pv_kwh,
        import_kwh=full.import_kwh,
        export_kwh=full.export_kwh,
        grid_charge_kwh=full.grid_charge_kwh,
        battery_export_kwh=full.battery_export_kwh,
        cycles=full.battery_discharge_kwh * hw.eta ** -1 / cap if hw.battery_kwh > 0 else 0.0,
        peak_import_kw=full.peak_import_kw,
        monthly=monthly_breakdown(sl, full),
    )


def split_savings(a: AnnualResult) -> tuple[float, float]:
    """(pv_saving, battery_saving) year-1, giving the PV+battery synergy to the battery."""
    f = 365.0 / a.days
    total = a.saving_gbp * f
    pv = (a.saving_pv_only_gbp or 0.0) * f
    if a.saving_pv_only_gbp is None and a.saving_battery_only_gbp is not None:
        return 0.0, total
    return pv, total - pv


def sweep(sl_for: "callable", hw: Hardware, st: Strategy, chunk_tod_min: int, costs: Costs,
          terms: FinanceTerms, batteries: list[float], arrays: list[float], inverters: list[float] | None = None,
          baseline_with_new: bool = False, hp_saving: float = 0.0) -> list[dict]:
    """Evaluate every combination. `sl_for(yield)` returns the year's Slots (cached by caller)."""
    sl = sl_for(hw.pv_annual_yield)
    rows = []
    for kwp in arrays:
        for kwh in batteries:
            for inv in inverters or [hw.inverter_kw]:
                h = replace(hw, pv_kwp=kwp, battery_kwh=kwh, inverter_kw=inv)
                if kwp == 0 and kwh == 0:
                    continue
                a = evaluate(sl, h, st, chunk_tod_min, attribution=False, baseline_with_new=baseline_with_new)
                f = 365.0 / a.days
                capex = costs.total(kwp, kwh)
                fin = evaluate_finance(capex, a.saving_gbp * f, 0.0, terms, hp_saving)
                rows.append({
                    "pv_kwp": kwp, "battery_kwh": kwh, "inverter_kw": inv,
                    "capex": round(capex), "saving_year1": round(a.saving_gbp * f + hp_saving, 2),
                    "payback_years": fin["payback_years"], "net_gain": fin["net_gain_horizon"],
                    "npv": fin["npv"], "monthly_net": fin["monthly_net_cashflow"],
                    "import_kwh": round(a.import_kwh * f), "export_kwh": round(a.export_kwh * f),
                    "cycles": round(a.cycles * f),
                })
    rows.sort(key=lambda r: r["npv"], reverse=True)
    return rows
