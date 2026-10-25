"""M3: stress test. Coverage of the installment if revenue drops 30%."""
from lockbox.inflows import Inflow
from lockbox.underwrite import Policy, underwrite

NOW, IV = 1_800_000_000, 120


def flows(per=10_000_000):
    return [Inflow(NOW - (6 - k) * IV + 5 + i, per, s) for k in range(6) for i, s in enumerate("abc")]


def test_stress_coverage_is_conservative_inflow_down_30_percent_over_the_installment():
    o = underwrite(flows(), NOW, Policy(payment_interval=IV, lookback_buckets=12))
    st = o.stress
    cons = o.metrics.conservative
    assert st["revenue_drop_pct"] == 30
    assert st["stressed_inflow"] == int(cons * 0.7)
    assert abs(st["coverage"] - cons / o.installment_drops) < 1e-9
    assert abs(st["stressed_coverage"] - cons * 0.7 / o.installment_drops) < 1e-9
    # sized at coverage 2.0, the loan is still covered (1.4x) after a 30% drop
    assert 1.39 < st["stressed_coverage"] < 1.41 and st["still_covered"]


def test_stress_flags_a_loan_that_would_not_be_covered():
    p = Policy(payment_interval=IV, lookback_buckets=12, coverage_target=1.2)
    st = underwrite(flows(), NOW, p).stress
    assert st["stressed_coverage"] < 1.0 and not st["still_covered"]


def test_stress_drop_is_a_policy_setting_and_shows_in_the_reasons():
    o = underwrite(flows(), NOW, Policy(payment_interval=IV, lookback_buckets=12, stress_drop_pct=50))
    assert o.stress["revenue_drop_pct"] == 50
    assert any("50%" in r and "covers" in r for r in o.reasons)


def test_no_installment_no_coverage():
    st = underwrite([], NOW, Policy(payment_interval=IV)).stress
    assert st["coverage"] is None and st["stressed_coverage"] is None and not st["still_covered"]


def test_state_shows_the_stress_line(sc):
    sc.quote()
    st = sc.state()["offer"]["stress"]
    assert st["revenue_drop_pct"] == 30 and st["still_covered"]


def test_state_offer_has_the_service_fee_and_interval(sc):
    o = sc.quote()
    st = sc.state()["offer"]
    assert st["service_fee"] == o.service_fee_drops == 50_000 and st["payment_interval"] == o.payment_interval == 120
