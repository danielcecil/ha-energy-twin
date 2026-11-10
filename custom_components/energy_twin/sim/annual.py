"""Annual what-if: run a year of slots, attribute savings, sweep hardware sizes."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .engine import simulate
from .finance import Costs, FinanceTerms, evaluate_finance  # noqa: F401 - re-exported for callers
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


def sweep(sl_for: "callable", hw: Hardware, st: Strategy, chunk_tod_min: int, batteries: list[float],
          arrays: list[float], inverters: list[float] | None = None, baseline_with_new: bool = False,
          hp_saving: float = 0.0) -> list[dict]:
    """Evaluate every size combination. Ranked by annual saving, with the marginal gain of each step up.

    There is no price here on purpose: one setup has one system cost. The marginal columns let you judge
    "is +5 kWh of battery worth £X more?" against your own quotes.
    """
    sl = sl_for(hw.pv_annual_yield)
    rows: list[dict] = []
    for kwp in arrays:
        for kwh in batteries:
            for inv in inverters or [hw.inverter_kw]:
                if kwp == 0 and kwh == 0:
                    continue
                h = replace(hw, pv_kwp=kwp, battery_kwh=kwh, inverter_kw=inv)
                a = evaluate(sl, h, st, chunk_tod_min, attribution=False, baseline_with_new=baseline_with_new)
                f = 365.0 / a.days
                rows.append({
                    "pv_kwp": kwp, "battery_kwh": kwh, "inverter_kw": inv,
                    "saving_year1": round(a.saving_gbp * f + hp_saving, 2),
                    "import_kwh": round(a.import_kwh * f), "export_kwh": round(a.export_kwh * f),
                    "solar_kwh": round(a.pv_kwh * f), "cycles": round(a.cycles * f),
                    "peak_import_kw": round(a.peak_import_kw, 1),
                })
    by = {(r["pv_kwp"], r["battery_kwh"], r["inverter_kw"]): r for r in rows}
    for r in rows:
        prev_b = max((b for (p, b, i) in by if p == r["pv_kwp"] and i == r["inverter_kw"] and b < r["battery_kwh"]), default=None)
        prev_a = max((p for (p, b, i) in by if b == r["battery_kwh"] and i == r["inverter_kw"] and p < r["pv_kwp"]), default=None)
        if prev_b is not None:
            d = r["saving_year1"] - by[(r["pv_kwp"], prev_b, r["inverter_kw"])]["saving_year1"]
            r["battery_step_gain"] = round(d, 2)
            r["battery_gain_per_kwh"] = round(d / (r["battery_kwh"] - prev_b), 1)
        if prev_a is not None:
            d = r["saving_year1"] - by[(prev_a, r["battery_kwh"], r["inverter_kw"])]["saving_year1"]
            r["array_step_gain"] = round(d, 2)
            r["array_gain_per_kwp"] = round(d / (r["pv_kwp"] - prev_a), 1)
    rows.sort(key=lambda r: r["saving_year1"], reverse=True)
    return rows
