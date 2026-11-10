"""Cost and finance maths: one system cost per setup, payback, loan / fixed-payment finance, long-run net gain."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Costs:
    """What the whole setup costs: one installed price, less any grant. Kept deliberately simple."""

    system: float = 0.0
    grant: float = 0.0

    @property
    def net(self) -> float:
        return max(0.0, self.system - self.grant)

    def total(self, kwp: float = 0.0, kwh: float = 0.0) -> float:        # args kept for call-site compatibility
        return self.net


@dataclass(frozen=True)
class FinanceTerms:
    mode: str = "cash"                # cash | loan (APR over term) | fixed_monthly (quoted payment x term)
    deposit: float = 0.0
    fee: float = 0.0                  # arrangement / admin fee paid up front when financing
    apr_pct: float = 0.0              # loan: interest rate (0 = interest-free)
    term_months: int = 0
    fixed_monthly: float = 0.0        # fixed_monthly: the quoted payment
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


def implied_apr(principal: float, payment: float, months: int) -> float | None:
    """The interest rate (APR %, nominal monthly compounding) hidden in a fixed monthly payment."""
    if principal <= 0 or months <= 0 or payment <= 0:
        return None
    if payment * months <= principal:
        return 0.0
    lo, hi = 0.0, 100.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if monthly_payment(principal, mid, months) < payment:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 1)


def evaluate_finance(capex: float, pv_saving: float, battery_saving: float, t: FinanceTerms,
                     hp_saving: float = 0.0) -> dict:
    """Year-1 savings in GBP in, headline numbers out. `capex` is the net outlay (system cost less grant)."""
    year1 = pv_saving + battery_saving + hp_saving
    financed_on = t.mode in ("loan", "fixed_monthly") and t.term_months > 0
    financed = max(0.0, capex - t.deposit) if financed_on else 0.0
    upfront = capex - financed + (t.fee if financed_on else 0.0)
    if not financed_on:
        pay = 0.0
    elif t.mode == "fixed_monthly":
        pay = t.fixed_monthly
    else:
        pay = monthly_payment(financed, t.apr_pct, t.term_months)
    total_paid = pay * t.term_months if financed_on else 0.0
    apr = (implied_apr(financed, pay, t.term_months) if t.mode == "fixed_monthly" else t.apr_pct) if financed_on else None
    cum, payback = -upfront, None
    disc = t.discount_rate_pct / 100
    npv = -upfront
    series = []
    for y in range(1, t.horizon_years + 1):
        sav = ((pv_saving + hp_saving) * (1 - t.pv_degradation_pct / 100) ** (y - 1)
               + battery_saving * (1 - t.battery_degradation_pct / 100) ** (y - 1)) \
            * (1 + t.tariff_escalation_pct / 100) ** (y - 1)
        paid = 0.0
        if financed_on:
            paid = pay * max(0, min(12, t.term_months - (y - 1) * 12))
        net = sav - paid
        prev = cum
        cum += net
        npv += net / (1 + disc) ** y
        if payback is None and cum >= 0:
            payback = (y - 1) + (-prev / net if net > 0 else 1.0)
        series.append(round(cum, 2))
    return {
        "capex": round(capex, 2),
        "upfront": round(upfront, 2),
        "year1_saving": round(year1, 2),
        "simple_payback_years": round(capex / year1, 1) if (year1 > 0 and capex > 0) else None,
        "payback_years": round(payback, 1) if (payback is not None and capex > 0) else None,
        "finance_mode": t.mode if financed_on else "cash",
        "monthly_payment": round(pay, 2),
        "monthly_net_cashflow": round(year1 / 12 - pay, 2),
        "total_paid_finance": round(total_paid, 2),
        "cost_of_finance": round(total_paid - financed + (t.fee if financed_on else 0.0), 2) if financed_on else 0.0,
        "total_interest": round(max(0.0, total_paid - financed), 2) if financed_on else 0.0,
        "effective_apr_pct": apr,
        "net_gain_horizon": round(cum, 2),
        "npv": round(npv, 2),
        "cumulative_by_year": series,
    }
