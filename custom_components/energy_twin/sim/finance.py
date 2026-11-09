"""Cash vs finance maths: payback, loan payments, long-run net gain."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Costs:
    pv_fixed: float = 1500.0
    pv_per_kwp: float = 900.0
    battery_fixed: float = 1500.0
    battery_per_kwh: float = 350.0
    other: float = 0.0

    def pv(self, kwp: float) -> float:
        return self.pv_fixed + self.pv_per_kwp * kwp if kwp > 0 else 0.0

    def battery(self, kwh: float) -> float:
        return self.battery_fixed + self.battery_per_kwh * kwh if kwh > 0 else 0.0

    def total(self, kwp: float, kwh: float) -> float:
        return self.pv(kwp) + self.battery(kwh) + (self.other if (kwp > 0 or kwh > 0) else 0.0)


@dataclass(frozen=True)
class FinanceTerms:
    deposit: float = 0.0
    apr_pct: float = 0.0              # 0 = cash purchase (or 0% finance)
    term_months: int = 0              # 0 = pay everything up front
    tariff_escalation_pct: float = 3.0
    pv_degradation_pct: float = 0.5
    battery_degradation_pct: float = 2.0
    discount_rate_pct: float = 3.0
    horizon_years: int = 20


def monthly_payment(principal: float, apr_pct: float, months: int) -> float:
    if months <= 0 or principal <= 0:
        return 0.0
    r = apr_pct / 100 / 12
    return principal / months if r == 0 else principal * r / (1 - (1 + r) ** -months)


def evaluate_finance(capex: float, pv_saving: float, battery_saving: float, t: FinanceTerms) -> dict:
    """pv_saving / battery_saving are year-1 savings in GBP. Returns headline numbers."""
    year1 = pv_saving + battery_saving
    financed = max(0.0, capex - t.deposit) if t.term_months > 0 else 0.0
    upfront = capex - financed
    pay = monthly_payment(financed, t.apr_pct, t.term_months)
    total_paid_finance = pay * t.term_months
    cum = -upfront
    payback = None
    disc = t.discount_rate_pct / 100
    npv = -upfront
    series = []
    for y in range(1, t.horizon_years + 1):
        sav = (pv_saving * (1 - t.pv_degradation_pct / 100) ** (y - 1)
               + battery_saving * (1 - t.battery_degradation_pct / 100) ** (y - 1)) \
            * (1 + t.tariff_escalation_pct / 100) ** (y - 1)
        paid = pay * 12 if (y - 1) * 12 < t.term_months else 0.0
        if y * 12 > t.term_months > (y - 1) * 12:
            paid = pay * (t.term_months - (y - 1) * 12)
        net = sav - paid
        prev = cum
        cum += net
        npv += net / (1 + disc) ** y
        if payback is None and cum >= 0:
            payback = (y - 1) + (-prev / net if net > 0 else 1.0)
        series.append(round(cum, 2))
    return {
        "capex": round(capex, 2),
        "year1_saving": round(year1, 2),
        "simple_payback_years": round(capex / year1, 1) if year1 > 0 else None,
        "payback_years": round(payback, 1) if payback is not None else None,
        "monthly_payment": round(pay, 2),
        "monthly_net_cashflow": round(year1 / 12 - pay, 2),
        "total_interest": round(total_paid_finance - financed, 2) if financed else 0.0,
        "net_gain_horizon": round(cum, 2),
        "npv": round(npv, 2),
        "cumulative_by_year": series,
    }
