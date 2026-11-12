"""Half-hourly dispatch simulation: battery physics, rule-based controller, DP optimiser.

Sign conventions: battery_kw is AC-side, + = charging. grid_kw + = import, - = export.
Hard rule baked into the physics: the battery NEVER discharges while a car is charging.
"""
from __future__ import annotations

from .models import EV_ON_KW, SLOT_H, Hardware, Result, Slots, Strategy, in_export_window

DT = SLOT_H
_EPS = 1e-9


# --------------------------------------------------------------------------- physics
def pv_series(unit: list[float], hw: Hardware) -> list[float]:
    """Scale kW/kWp to the array and clip at the inverter's AC rating."""
    k, cap = hw.pv_kwp, hw.inverter_kw
    return [min(u * k, cap) for u in unit]


def settle(hw: Hardware, soc: float, r_req: float, ev_on: bool, cap_d: float,
           dt: float = DT, step: float | None = None, max_charge: float | None = None) -> tuple[float, float]:
    """Apply a requested AC battery power within physical limits. Returns (new_soc, r_actual).

    `dt` is the window the power limits are checked over (the planner uses a whole half-hour slot);
    `step` is how much time actually elapses and is what the state of charge advances by. They are the
    same in the planner/annual runs, but the live twin ticks every minute inside a half-hour slot, so it
    must advance SoC by the real elapsed time (`step`), not by a whole slot.
    """
    step = dt if step is None else step
    eta = hw.eta
    if r_req > _EPS:
        cap_c = hw.charge_kw if max_charge is None else min(hw.charge_kw, max(0.0, max_charge))
        r = max(0.0, min(r_req, cap_c, (hw.emax - soc) / (eta * dt)))
        return soc + r * eta * step, r
    if r_req < -_EPS and not ev_on:
        r = -max(0.0, min(-r_req, cap_d, max(0.0, soc - hw.emin) * eta / dt))
        return soc + r * step / eta, r
    return soc, 0.0


def charge_headroom(hw: Hardware, load_net: float) -> float | None:
    """kW the main fuse still allows us to pull for battery charging (None = no limit configured)."""
    if hw.import_limit_kw <= 0:
        return None
    return max(0.0, hw.import_limit_kw - max(0.0, load_net))


def _slot_cost(hw: Hardware, grid: float, r: float, imp_p: float, exp_p: float, bonus_p: float) -> tuple[float, float]:
    exp = min(max(-grid, 0.0), hw.export_limit_kw)
    imp = max(grid, 0.0)
    dis = max(-r, 0.0)
    cost = imp * DT * imp_p - exp * DT * exp_p + dis * DT * (hw.cycle_cost_p - bonus_p)
    return cost, exp


def event_honoured(sl: Slots, st: Strategy, t: int) -> bool:
    """Is slot t a Power Down event we act on (export beyond the house's own use)?"""
    return sl.event[t] == "power_down" and st.event_export_anywhere


def export_allowed(sl: Slots, st: Strategy, t: int) -> bool:
    """May the battery send power to the GRID in slot t?

    Only in the late-evening window and never in a cheap slot - exporting overnight while the grid is cheap just
    cycles the battery again (charge at 7p, sell, recharge). A Power Down event can override.
    """
    if not st.allow_battery_export:
        return False
    if event_honoured(sl, st, t):
        return True
    return (not sl.cheap[t]) and in_export_window(sl.tod_min[t], st)


# --------------------------------------------------------------------------- rules controller
class _RulesCtx:
    """Pre-computed look-ahead for the rule-based controller (uses EXPECTED loads only)."""

    def __init__(self, sl: Slots, hw: Hardware, st: Strategy, exp_pv: list[float]):
        n, eta = sl.n, hw.eta
        rng = hw.emax - hw.emin
        m = 1.0 + st.margin_pct / 100.0
        self.need = [0.0] * n          # stored kWh (above reserve) needed at START of slot (non-cheap)
        self.target = [0.0] * n        # stored kWh (above reserve) to reach by END of cheap block
        self.best_exp = [0.0] * n      # best export price available in the rest of this non-cheap block
        self.left = [0] * n            # non-cheap slots left in block (incl. this)
        self.win_left = [0] * n        # export-window slots left in this stretch (incl. this)
        self.hold = [0.0] * n          # stored kWh to keep back for a later Power Down in this block
        hold_incl = [0.0] * (n + 1)
        self.next_cheap_p = [sl.import_p[-1] if n else 0.0] * n
        self.arb = [False] * n
        nxt_cheap_p = sl.import_p[-1] if n else 0.0
        for t in range(n - 1, -1, -1):
            nxt_noncheap = t + 1 < n and not sl.cheap[t + 1]
            if sl.cheap[t]:
                self.need[t] = 0.0
                if t + 1 < n:
                    self.target[t] = self.need[t + 1] if nxt_noncheap else self.target[t + 1]
                    best_after = self.best_exp[t + 1]
                else:
                    self.target[t], best_after = rng, 0.0
                self.best_exp[t] = best_after
                nxt_cheap_p = sl.import_p[t]
                if sl.event[t] == "power_up":
                    self.target[t] = rng
                elif st.arbitrage and st.allow_battery_export and best_after * hw.round_trip_eff - sl.import_p[t] > st.export_margin_p:
                    self.target[t] = rng
            else:
                nn = self.need[t + 1] if nxt_noncheap else 0.0
                if event_honoured(sl, st, t):
                    deficit = hw.discharge_cap(0.0) * DT / eta
                else:
                    net = (sl.exp_house_kw[t] - exp_pv[t]) * DT
                    deficit = net * m / eta if net > 0 else net * eta     # surplus is negative
                self.need[t] = min(rng, max(0.0, nn + deficit))
                self.best_exp[t] = max(sl.export_p[t], self.best_exp[t + 1] if nxt_noncheap else 0.0)
                self.left[t] = 1 + (self.left[t + 1] if nxt_noncheap else 0)
                if export_allowed(sl, st, t):
                    self.win_left[t] = 1 + (self.win_left[t + 1] if nxt_noncheap else 0)
                after = hold_incl[t + 1] if nxt_noncheap else 0.0
                self.hold[t] = after
                hold_incl[t] = after + (hw.discharge_cap(0.0) * DT / eta if event_honoured(sl, st, t) else 0.0)
            self.next_cheap_p[t] = nxt_cheap_p


def _rules_request(c: _RulesCtx, sl: Slots, hw: Hardware, st: Strategy, t: int, soc: float,
                   house: float, ev: float, pv: float) -> float:
    eta = hw.eta
    D = house + ev - pv
    ev_on = ev >= EV_ON_KW
    ev_ = sl.event[t]
    if ev_ == "power_up":
        return hw.charge_kw
    if event_honoured(sl, st, t):
        return -hw.discharge_cap(pv)
    if sl.cheap[t]:
        tgt = hw.emin + c.target[t]
        if soc < tgt - 1e-6:
            return min(hw.charge_kw, (tgt - soc) / (eta * DT))
        return -D if D < 0 else 0.0
    if ev_on:
        return -D if D < 0 else 0.0
    if D < 0:
        return -D
    r = -D
    if c.hold[t] > 0:                                  # keep charge back for a Power Down later today
        floor = hw.emin + min(hw.emax - hw.emin, c.hold[t])
        r = max(r, -max(0.0, soc - floor) * eta / DT)
    if export_allowed(sl, st, t):
        price_ok = sl.export_p[t] * hw.round_trip_eff >= c.next_cheap_p[t] + st.export_margin_p
        surplus = soc - (hw.emin + c.need[t])
        if price_ok and surplus > 1e-6:
            r -= surplus * eta / (DT * max(1, c.win_left[t]))
    return r


# --------------------------------------------------------------------------- DP optimiser
def terminal_price(sl: Slots) -> float:
    """What a stored kWh costs to replace: the cheap (non-event) import rate across the timeline."""
    base = [sl.import_p[t] for t in range(sl.n) if sl.cheap[t] and not sl.event[t]]
    return min(base) if base else (min(sl.import_p) if sl.n else 0.0)


def plan_dp(sl: Slots, hw: Hardware, st: Strategy, soc0: float, pv: list[float],
            terminal_p: float | None = None, load_margin_pct: float = 0.0) -> list[float]:
    """Backward-induction over a discretised state of charge. Returns AC battery kW per slot.

    Uses the slots' *actual* arrays - pass `sl.as_expected()` when planning ahead.
    The terminal value of stored energy is what it would cost to re-buy at the cheap rate,
    so the optimiser naturally exports surplus charge when export price > cheap_rate / round_trip.
    """
    import numpy as np

    n = sl.n
    if hw.battery_kwh <= 0 or n == 0:
        return [0.0] * n
    eta, big = hw.eta, 1e12
    N = int(min(121, max(21, (hw.emax - hw.emin) / 0.12 + 1)))
    S = np.linspace(hw.emin, hw.emax, N)
    dS = S[None, :] - S[:, None]
    chg = np.where(dS > 0, dS / (eta * DT), 0.0)
    dis = np.where(dS < 0, -dS * eta / DT, 0.0)
    r = chg - dis
    if terminal_p is None:
        terminal_p = terminal_price(sl)
    V = -(terminal_p / eta) * S
    pol = np.zeros((n, N), dtype=np.int32)
    mg = 1.0 + load_margin_pct / 100.0
    for t in range(n - 1, -1, -1):
        house = sl.house_kw[t] * mg
        D = house + sl.ev_kw[t] - pv[t]
        grid = D + r
        imp = np.maximum(grid, 0.0)
        exp = np.minimum(np.maximum(-grid, 0.0), hw.export_limit_kw)
        cost = imp * DT * sl.import_p[t] - exp * DT * sl.export_p[t] + dis * DT * (hw.cycle_cost_p - sl.bonus_p[t])
        bad = (chg > hw.charge_kw + 1e-6) | (dis > hw.discharge_cap(pv[t]) + 1e-6)
        if sl.ev_kw[t] >= EV_ON_KW:
            bad |= dis > 1e-9
        if hw.import_limit_kw > 0:
            bad |= chg > max(0.0, hw.import_limit_kw - max(0.0, D)) + 1e-6   # main fuse
        if not sl.cheap[t]:
            bad |= chg > max(0.0, -D) + 1e-6          # only solar may charge outside cheap slots
        surplus = -D
        if st.solar_battery_first and surplus > 0.05 and not (sl.event[t] and event_honoured(sl, st, t)):
            # an inverter in self-consumption mode sends solar to the battery before the grid: the battery must take
            # what it can (its charge rate, or the room it has left), within one power step of tolerance
            step_kw = float(S[1] - S[0]) / (eta * DT) if N > 1 else 0.0
            take = np.minimum(min(surplus, hw.charge_kw), (hw.emax - S) / (eta * DT))[:, None]
            bad |= chg < take - step_kw - 1e-6
        if not export_allowed(sl, st, t):          # late-evening window only (Power Down events excepted)
            bad |= dis > max(0.0, house - pv[t]) + 1e-6
        tot = np.where(bad, big, cost) + V[None, :]
        pol[t] = tot.argmin(axis=1)
        V = tot.min(axis=1)
    i = int(np.abs(S - min(max(soc0, hw.emin), hw.emax)).argmin())
    out = []
    for t in range(n):
        j = int(pol[t, i])
        out.append(float(r[i, j]))
        i = j
    return level_charging(out, sl)


def level_charging(r: list[float], sl: Slots) -> list[float]:
    """Spread each cheap block's charge evenly (a straight line, not an arbitrary lumpy split).

    Slots in a cheap block share one price, so every split costs the same; the DP just picks one
    arbitrarily (and its power steps are quantised). Averaging over a run of non-discharging cheap
    slots keeps total energy, cost and the final SoC identical, lowers peak grid draw and is
    gentler on the battery. Intermediate SoC stays between the run's start and end values.
    """
    out, n, i = list(r), len(r), 0
    while i < n:
        if sl.cheap[i] and not sl.event[i] and r[i] > -1e-9:
            j = i
            while (j + 1 < n and sl.cheap[j + 1] and not sl.event[j + 1] and r[j + 1] > -1e-9
                   and sl.import_p[j + 1] == sl.import_p[i]):
                j += 1
            seg = r[i:j + 1]
            if sum(1 for x in seg if x > 1e-6):                 # something is being charged
                avg = sum(seg) / len(seg)
                for k in range(i, j + 1):
                    out[k] = avg
            i = j + 1
        else:
            i += 1
    return out


def follow(r_plan: float, sl: Slots, t: int, house: float, ev: float, pv: float,
           exp_house: float, exp_pv: float, ev_block: bool = False) -> float:
    """Execute a planned set-point against what is actually happening (reactive wrapper)."""
    D_house = house - pv
    ev_on = ev >= EV_ON_KW or ev_block          # ev_block: a car we can't measure is expected to be charging
    if sl.event[t] == "power_up":
        return 1e9                                   # charge flat out; settle() clamps
    if r_plan > _EPS:
        if sl.cheap[t]:
            return r_plan                                        # grid charging is fine with a car on
        return min(r_plan, max(0.0, -(D_house + ev)))           # outside cheap slots: solar only
    if ev_on:
        return max(0.0, -(D_house + ev))                         # car charging: never discharge
    d_plan = -r_plan
    if sl.cheap[t] and d_plan <= _EPS:
        return max(0.0, -(D_house + ev))
    exp_deficit = max(0.0, exp_house - exp_pv)
    export_part = max(0.0, d_plan - exp_deficit)
    if export_part < 0.3:                                       # DP power-step quantisation noise
        export_part = 0.0
    return -(D_house + export_part)


# --------------------------------------------------------------------------- simulate
def simulate(sl: Slots, hw: Hardware, st: Strategy, soc0: float | None = None,
             chunk_tod_min: int | None = None, baseline_with_new: bool = False) -> Result:
    """Run the strategy over `sl`, scoring against actual loads/PV.

    `sl.new_kw` (heat pump, induction hob) is real demand the battery can serve and the grid must supply,
    but is left out of the baseline bill unless `baseline_with_new` (used to isolate what solar+battery save).
    """
    base_house = sl.house_kw
    base_new = sl.new_kw or [0.0] * sl.n
    if sl.new_kw or sl.exp_new_kw:
        enew = sl.exp_new_kw or [0.0] * sl.n
        sl = Slots(**{**sl.__dict__,
                      "house_kw": [a + b for a, b in zip(sl.house_kw, base_new)],
                      "exp_house_kw": [a + b for a, b in zip(sl.exp_house_kw, enew)],
                      "new_kw": [], "exp_new_kw": []})
    n = sl.n
    pv = pv_series(sl.pv_unit, hw)
    exp_pv = pv_series(sl.exp_pv_unit, hw)
    soc = hw.emin if soc0 is None else soc0
    res = Result([0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n, pv, [0.0] * n, [0.0] * n)

    plan: list[float] | None = None
    ctx: _RulesCtx | None = None
    chunk_end: list[int] = []
    if hw.battery_kwh > 0:
        if st.name == "optimised":
            plan = [0.0] * n
            exp_view = sl.as_expected()
            term_p = terminal_price(sl)
            # index of the end of the planning chunk each slot belongs to
            chunk_end = [n] * n
            nxt = n
            for t in range(n - 1, -1, -1):
                chunk_end[t] = nxt
                if chunk_tod_min is not None and sl.tod_min[t] == chunk_tod_min:
                    nxt = t
            # re-plan at the start, at each chunk start, and at the configured times of day
            replan = {0}
            for t in range(1, n):
                if sl.tod_min[t] == chunk_tod_min or sl.tod_min[t] in st.replan_min:
                    replan.add(t)
        else:
            ctx = _RulesCtx(sl, hw, st, exp_pv)

    for t in range(n):
        if plan is not None and t in replan:
            end = chunk_end[t] if chunk_end[t] > t else n
            plan[t:end] = plan_dp(exp_view.view(t, end), hw, st, soc, exp_pv[t:end],
                                  terminal_p=term_p, load_margin_pct=st.margin_pct * 0.5)
        house, ev, p = sl.house_kw[t], sl.ev_kw[t], pv[t]
        ev_on = ev >= EV_ON_KW
        if hw.battery_kwh <= 0:
            r_req = 0.0
        elif plan is not None:
            r_req = follow(plan[t], sl, t, house, ev, p,
                           sl.exp_house_kw[t] * (1 + st.margin_pct * 0.005), exp_pv[t])
        else:
            r_req = _rules_request(ctx, sl, hw, st, t, soc, house, ev, p)
        soc, r = settle(hw, soc, r_req, ev_on, hw.discharge_cap(p),
                        max_charge=charge_headroom(hw, house + ev - p))
        grid = house + ev - p + r
        cost, exp = _slot_cost(hw, grid, r, sl.import_p[t], sl.export_p[t], sl.bonus_p[t])
        res.battery_kw[t], res.grid_kw[t], res.export_kw[t], res.soc[t] = r, grid, exp, soc
        res.cost_p[t] = cost
        res.baseline_p[t] = ((base_house[t] + (base_new[t] if baseline_with_new else 0.0)) + ev) * DT * sl.import_p[t]
        res.peak_import_kw = max(res.peak_import_kw, grid)
        if r > 0:
            solar_part = max(0.0, -(house + ev - p))
            res.grid_charge_kwh += max(0.0, r - solar_part) * DT
        elif r < 0:
            res.battery_discharge_kwh += -r * DT
            res.battery_export_kwh += max(0.0, -r - max(0.0, house + ev - p)) * DT
        res.import_kwh += max(grid, 0.0) * DT
        res.export_kwh += exp * DT
        res.pv_kwh += p * DT
    res.cost_total_p = sum(res.cost_p)
    res.baseline_total_p = sum(res.baseline_p)
    return res


def plan_horizon(sl_expected: Slots, hw: Hardware, st: Strategy, soc0: float) -> Result:
    """Plan for the live twin: expected == actual, single DP chunk, rules or optimised."""
    return simulate(sl_expected, hw, st, soc0=soc0, chunk_tod_min=None)
