"""Cash-flow underwriting. Deterministic, no model, every number explained.

Idea: lend against what actually arrived, sized so the installment is a fraction of the
conservative expected inflow per payment interval (a debt service coverage ratio).

  conservative inflow per interval = mean - 1 standard deviation (floored at 0)
  max installment                  = conservative inflow / target coverage
  max principal                    = annuity present value of that installment

The score only decides the tier (rate premium) and whether to decline. It never raises the limit.

Before measuring, senders are cleaned (clean_inflows):
  a sender first seen fewer than min_sender_age_intervals ago is ignored (too new to trust)
  a sender the borrower's own wallets ever paid (own_funded, found by inflows.funded_by) is ignored
  no single sender counts for more than max_sender_share of the cleaned revenue

Graduation: each loan this lockbox repaid on the ledger raises the limit by graduation_step (up to
graduation_max), but the installment never goes above conservative inflow / graduation_min_coverage.

Every offer also carries the same borrower priced WITHOUT the lockbox (Offer.unlocked). Those terms
are our assumptions, not market data: rate plus unlocked_rate_add_pct points, limit times
unlocked_limit_factor, because without the lockbox a lender cannot stop the cash leaving.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace

from .inflows import Inflow
from .units import fmt_xrp, period_rate, pct_to_units


@dataclass
class Policy:
    payment_interval: int = 3600  # seconds, also the bucket size for measuring inflow
    payment_total: int = 6
    grace_period: int = 3600
    lookback_buckets: int = 12
    min_history_buckets: int = 3  # buckets since first inflow
    min_active_buckets: int = 3  # buckets that had any revenue
    min_total_drops: int = 5_000_000
    coverage_target: float = 2.0  # conservative inflow per interval / installment
    base_rate_pct: float = 8.0
    tier_premium_pct: dict = field(default_factory=lambda: {"A": 0.0, "B": 3.0, "C": 6.0})
    decline_below_score: int = 45
    max_principal_drops: int = 500_000_000
    origination_fee_drops: int = 0
    service_fee_drops: int = 0
    min_sender_age_intervals: int = 2  # senders first seen more recently than this are not counted
    max_sender_share: float = 0.4  # one sender counts for at most this share of the cleaned revenue
    unlocked_rate_add_pct: float = 6.0  # OUR ASSUMPTION: extra rate for the same borrower without a lockbox
    unlocked_limit_factor: float = 0.5  # OUR ASSUMPTION: share of the limit the same borrower gets without a lockbox
    stress_drop_pct: float = 30  # stress test: revenue falls by this much
    graduation_step: float = 0.25  # limit multiplier added per loan repaid on the ledger
    graduation_max: float = 2.0
    graduation_min_coverage: float = 1.25  # even graduated, the installment stays at or below conservative inflow / this


@dataclass
class Metrics:
    buckets: list[int]
    history_buckets: int
    active_buckets: int
    total_drops: int
    mean: float
    std: float
    cv: float
    conservative: float
    top_sender_share: float
    senders: int
    recent_ratio: float


@dataclass
class Offer:
    approved: bool
    tier: str
    score: int
    principal_drops: int
    interest_rate_units: int
    payment_total: int
    payment_interval: int
    grace_period: int
    installment_drops: int
    origination_fee_drops: int
    service_fee_drops: int
    reasons: list[str]
    declines: list[str]
    metrics: Metrics
    rate_pct: float = 0.0
    unlocked: dict = field(default_factory=dict)  # the same borrower without the lockbox (our assumptions)
    stress: dict = field(default_factory=dict)  # coverage of the installment if revenue drops stress_drop_pct
    graduation: dict = field(default_factory=dict)  # repaid loans and the limit multiplier they earned


def annuity_payment(principal: float, r: float, n: int) -> float:
    if n <= 0:
        raise ValueError("n must be positive")
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def annuity_principal(payment: float, r: float, n: int) -> float:
    if r == 0:
        return payment * n
    return payment * (1 - (1 + r) ** -n) / r


def _in_window(f: Inflow, now: int, p: Policy) -> bool:
    return 0 <= now - f.ts < p.lookback_buckets * p.payment_interval


def _clean(inflows: list[Inflow], now: int, p: Policy, own_funded: frozenset | set = frozenset()):
    """(kept and capped, notes, kept before the cap)."""
    notes: list[str] = []
    first: dict[str, int] = {}
    for f in inflows:
        first[f.sender] = min(first.get(f.sender, f.ts), f.ts)
    min_age = p.min_sender_age_intervals * p.payment_interval
    new = {s for s, t in first.items() if now - t < min_age}
    dropped = (new | set(own_funded)) & set(first)
    for s in sorted(dropped):
        why = "funded by the borrower's own wallets" if s in own_funded else f"new (first seen under {p.min_sender_age_intervals} intervals ago)"
        lost = sum(f.drops for f in inflows if f.sender == s and _in_window(f, now, p))
        notes.append(f"ignored sender {s}: {why}, {fmt_xrp(lost)} XRP not counted")
    kept = [f for f in inflows if f.sender not in dropped]

    totals: dict[str, int] = {}
    for f in kept:
        if _in_window(f, now, p):
            totals[f.sender] = totals.get(f.sender, 0) + f.drops
    whole = sum(totals.values())
    cap = int(p.max_sender_share * whole)
    capped = {s: t for s, t in totals.items() if t > cap}
    for s in sorted(capped):
        notes.append(f"sender {s} is {capped[s] / whole:.0%} of revenue, counted at {p.max_sender_share:.0%}")
    out = [replace(f, drops=f.drops * cap // capped[f.sender]) if f.sender in capped else f for f in kept]
    return out, notes, kept


def clean_inflows(inflows: list[Inflow], now: int, p: Policy, own_funded: frozenset | set = frozenset()) -> tuple[list[Inflow], list[str]]:
    """Drop senders that are too new or funded by the borrower's own wallets, then cap any single sender."""
    out, notes, _ = _clean(inflows, now, p, own_funded)
    return out, notes


def measure(inflows: list[Inflow], now: int, p: Policy) -> Metrics:
    """Bucket inflows into complete intervals ending at `now`, newest last."""
    b = p.payment_interval
    n = p.lookback_buckets
    buckets = [0] * n
    senders: dict[str, int] = {}
    for f in inflows:
        age = now - f.ts
        if age < 0 or age >= n * b:
            continue
        idx = n - 1 - (age // b)
        buckets[int(idx)] += f.drops
        senders[f.sender] = senders.get(f.sender, 0) + f.drops
    first = min((f.ts for f in inflows if 0 <= now - f.ts < n * b), default=None)
    history = 0 if first is None else min(n, int((now - first) // b) + 1)
    window = buckets[n - history:] if history else []
    total = sum(window)
    mean = statistics.fmean(window) if window else 0.0
    std = statistics.pstdev(window) if len(window) > 1 else 0.0
    cv = (std / mean) if mean else 0.0
    cons = max(0.0, mean - std)
    top = (max(senders.values()) / sum(senders.values())) if senders else 1.0
    recent = (window[-1] / mean) if mean and window else 0.0
    return Metrics(
        buckets=buckets,
        history_buckets=history,
        active_buckets=sum(1 for x in window if x > 0),
        total_drops=total,
        mean=mean,
        std=std,
        cv=cv,
        conservative=cons,
        top_sender_share=top,
        senders=len(senders),
        recent_ratio=recent,
    )


def score(m: Metrics, p: Policy) -> tuple[int, list[str]]:
    """0 to 100. Four parts, each explained."""
    depth = 25 * min(1.0, m.history_buckets / p.lookback_buckets)
    stability = 35 * (1 - min(1.0, m.cv))
    spread = 20 * (1 - m.top_sender_share) / (1 - 1 / max(m.senders, 2)) if m.senders > 1 else 0.0
    spread = min(20.0, spread)
    recency = 20 * min(1.0, m.recent_ratio)
    total = int(round(depth + stability + spread + recency))
    d, s, sp, r = shown_points([depth, stability, spread, recency], total)
    notes = [
        f"history {m.history_buckets} of {p.lookback_buckets} intervals: {d}/25",
        f"inflow volatility (std/mean) {m.cv:.2f}: {s}/35",
        f"{m.senders} paying sender(s), largest share {m.top_sender_share:.0%}: {sp}/20",
        f"last interval at {m.recent_ratio:.0%} of the average: {r}/20",
    ]
    return total, notes


def shown_points(parts: list[float], total: int) -> list[int]:
    """Whole points for display that add up to the score (largest remainder). Rounding each part on its own
    could show 15+23+15+20 = 73 next to a score of 72. The score itself is unchanged."""
    out = [int(x) for x in parts]
    order = sorted(range(len(parts)), key=lambda i: parts[i] - out[i], reverse=True)
    for i in order[:max(0, total - sum(out))]:
        out[i] += 1
    return out


def tier_for(s: int) -> str:
    return "A" if s >= 75 else "B" if s >= 60 else "C" if s >= 45 else "D"


def unlocked_quote(approved: bool, principal: int, rate_pct: float, p: Policy) -> dict:
    """The same borrower priced without the lockbox. Labelled as our assumptions everywhere it is shown."""
    rate = rate_pct + p.unlocked_rate_add_pct
    units = pct_to_units(rate)
    pr = int(principal * p.unlocked_limit_factor) if approved else 0
    inst = int(annuity_payment(pr, period_rate(units, p.payment_interval), p.payment_total)) + p.service_fee_drops if pr > 0 else 0
    return {
        "label": "without a lockbox (our assumptions, not market data)",
        "approved": approved and pr > 0,
        "rate_pct": rate,
        "interest_rate_units": units,
        "principal_drops": pr,
        "installment_drops": inst,
        "assumptions": [
            "our assumption, not market data: without the lockbox the lender cannot stop the revenue leaving the account",
            f"so we price the same borrower at +{p.unlocked_rate_add_pct:g} points",
            f"and lend {'half' if p.unlocked_limit_factor == 0.5 else f'{p.unlocked_limit_factor:.0%}'} the limit",
        ],
    }


def stress_test(conservative: float, installment: int, p: Policy) -> dict:
    """Coverage = conservative inflow per interval / installment, today and after a revenue drop."""
    stressed = conservative * (1 - p.stress_drop_pct / 100)
    cov = conservative / installment if installment else None
    scov = stressed / installment if installment else None
    return {"revenue_drop_pct": p.stress_drop_pct, "conservative_inflow": int(conservative), "stressed_inflow": int(stressed),
            "installment": installment, "coverage": cov, "stressed_coverage": scov, "still_covered": bool(scov is not None and scov >= 1.0)}


def underwrite(inflows: list[Inflow], now: int, p: Policy | None = None, requested_drops: int | None = None,
               own_funded: frozenset | set = frozenset(), repaid_loans: int = 0) -> Offer:
    p = p or Policy()
    counted, cleaning, uncapped = _clean(inflows, now, p, own_funded)
    m = measure(counted, now, p)
    # customer spread is scored on real shares; the cap only limits how much one sender can lift the loan size
    real = measure(uncapped, now, p)
    m.top_sender_share, m.senders = real.top_sender_share, real.senders
    s, notes = score(m, p)
    tier = tier_for(s)
    declines: list[str] = []
    if m.history_buckets < p.min_history_buckets:
        declines.append(f"only {m.history_buckets} intervals of history, need {p.min_history_buckets}")
    if m.active_buckets < p.min_active_buckets:
        declines.append(f"revenue in only {m.active_buckets} intervals, need {p.min_active_buckets}")
    if m.total_drops < p.min_total_drops:
        declines.append(f"total inflow {fmt_xrp(m.total_drops)} XRP is below the {fmt_xrp(p.min_total_drops)} XRP minimum")
    if s < p.decline_below_score:
        declines.append(f"score {s} is below the {p.decline_below_score} cutoff")

    rate_pct = p.base_rate_pct + p.tier_premium_pct.get(tier, 0.0)
    units = pct_to_units(rate_pct)
    r = period_rate(units, p.payment_interval)
    inst_cap = m.conservative / p.coverage_target  # the whole installment, service fee included
    principal = annuity_principal(max(0.0, inst_cap - p.service_fee_drops), r, p.payment_total)
    mult = min(p.graduation_max, 1 + p.graduation_step * max(0, repaid_loans))
    if mult > 1:
        floor_cap = annuity_principal(max(0.0, m.conservative / p.graduation_min_coverage - p.service_fee_drops), r, p.payment_total)
        principal = min(principal * mult, floor_cap)
    principal = min(principal, p.max_principal_drops)
    if requested_drops is not None:
        principal = min(principal, requested_drops)
    principal_i = int(principal)
    installment = int(annuity_payment(principal_i, r, p.payment_total)) + p.service_fee_drops if principal_i > 0 else 0
    if principal_i <= 0 and not declines:
        declines.append("conservative inflow is zero")

    stress = stress_test(m.conservative, installment, p)
    reasons = notes + cleaning + [
        f"average inflow {fmt_xrp(int(m.mean))} XRP per interval, conservative {fmt_xrp(int(m.conservative))} XRP",
        f"installment kept at or below conservative inflow / {p.coverage_target:g}",
        f"tier {tier}: rate {rate_pct:.1f}% a year",
    ]
    if repaid_loans:
        reasons.append(f"graduation: {repaid_loans} loan(s) repaid on the ledger, limit x{mult:g} "
                       f"(installment still at most conservative inflow / {p.graduation_min_coverage:g})")
    if stress["stressed_coverage"] is not None:
        reasons.append(f"stress: if revenue drops {p.stress_drop_pct:g}%, conservative inflow covers the installment "
                       f"{stress['stressed_coverage']:.2f} times" + ("" if stress["still_covered"] else ", NOT enough"))
    approved = not declines
    return Offer(
        approved=approved,
        tier=tier,
        score=s,
        principal_drops=principal_i if approved else 0,
        interest_rate_units=units,
        payment_total=p.payment_total,
        payment_interval=p.payment_interval,
        grace_period=p.grace_period,
        installment_drops=installment if approved else 0,
        origination_fee_drops=p.origination_fee_drops,
        service_fee_drops=p.service_fee_drops,
        reasons=reasons,
        declines=declines,
        metrics=m,
        rate_pct=rate_pct,
        unlocked=unlocked_quote(approved, principal_i if approved else 0, rate_pct, p),
        stress=stress,
        graduation={"repaid_loans": repaid_loans, "multiplier": mult, "max_multiplier": p.graduation_max,
                    "min_coverage": p.graduation_min_coverage},
    )
