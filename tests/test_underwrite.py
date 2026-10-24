from lockbox.inflows import Inflow
from lockbox.underwrite import Policy, annuity_payment, annuity_principal, measure, score, tier_for, underwrite
from lockbox.units import period_rate

NOW = 1_800_000_000


def flows(per_bucket, interval=120, n=6, senders=("a", "b", "c")):
    out = []
    for k in range(n):
        base = NOW - (n - k) * interval + 10
        for i, s in enumerate(senders):
            out.append(Inflow(base + i, per_bucket // len(senders), s))
    return out


def test_annuity_roundtrip():
    r = 0.01
    pay = annuity_payment(1000, r, 6)
    assert abs(annuity_principal(pay, r, 6) - 1000) < 1e-6
    assert annuity_payment(600, 0, 6) == 100


def test_steady_revenue_is_approved_and_installment_is_covered():
    p = Policy(payment_interval=120, lookback_buckets=12)
    o = underwrite(flows(30_000_000), NOW, p)
    assert o.approved and o.principal_drops > 0
    # the installment (service fee included) never exceeds conservative inflow / coverage target
    assert o.installment_drops <= o.metrics.conservative / p.coverage_target + 2


def test_conservative_inflow_is_mean_minus_std_and_never_negative():
    p = Policy(payment_interval=120, lookback_buckets=12)
    m = measure(flows(30_000_000), NOW, p)
    assert abs(m.conservative - max(0, m.mean - m.std)) < 1e-6
    wild = [Inflow(NOW - 10, 1_000_000, "a"), Inflow(NOW - 4 * 120, 90_000_000, "a"), Inflow(NOW - 2 * 120, 1_000, "a")]
    assert measure(wild, NOW, p).conservative >= 0


def test_declines_thin_history():
    p = Policy(payment_interval=120)
    o = underwrite([Inflow(NOW - 20, 50_000_000, "a")], NOW, p)
    assert not o.approved and o.principal_drops == 0
    assert any("history" in d or "intervals" in d for d in o.declines)


def test_declines_when_no_inflow():
    o = underwrite([], NOW, Policy(payment_interval=120))
    assert not o.approved and o.installment_drops == 0


def test_volatile_revenue_gets_smaller_loan_than_steady_with_same_total():
    p = Policy(payment_interval=120)
    steady = underwrite(flows(30_000_000), NOW, p)
    lumpy = []
    for k, amt in enumerate([90_000_000, 0, 0, 90_000_000, 0, 0]):
        if amt:
            lumpy.append(Inflow(NOW - (6 - k) * 120 + 10, amt, "a"))
    lo = underwrite(lumpy, NOW, p)
    assert lo.principal_drops < steady.principal_drops


def test_single_customer_scores_lower_than_spread():
    p = Policy(payment_interval=120)
    one = measure(flows(30_000_000, senders=("a",)), NOW, p)
    many = measure(flows(30_000_000, senders=("a", "b", "c")), NOW, p)
    assert score(one, p)[0] < score(many, p)[0]


def test_requested_amount_caps_principal_and_max_principal_applies():
    p = Policy(payment_interval=120, max_principal_drops=10_000_000)
    assert underwrite(flows(300_000_000), NOW, p).principal_drops == 10_000_000
    p2 = Policy(payment_interval=120)
    assert underwrite(flows(30_000_000), NOW, p2, requested_drops=1_000_000).principal_drops == 1_000_000


def test_tier_bands_and_rate_premium():
    assert [tier_for(x) for x in (90, 75, 74, 60, 59, 45, 44)] == ["A", "A", "B", "B", "C", "C", "D"]
    p = Policy(payment_interval=120)
    o = underwrite(flows(30_000_000), NOW, p)
    base = int(p.base_rate_pct * 1000)
    assert o.interest_rate_units == base + int(p.tier_premium_pct[o.tier] * 1000)


def test_service_fee_is_inside_the_installment_budget():
    p = Policy(payment_interval=120, service_fee_drops=100_000)
    o = underwrite(flows(30_000_000), NOW, p)
    assert o.installment_drops >= 100_000
    assert o.installment_drops <= o.metrics.conservative / p.coverage_target + 2


def test_period_rate_matches_annual():
    assert abs(period_rate(10_000, 31_536_000) - 0.10) < 1e-9
