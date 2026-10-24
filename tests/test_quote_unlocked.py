"""M3: the same borrower priced twice, with and without the lockbox. The unlocked terms are our assumptions."""
from lockbox.inflows import Inflow
from lockbox.underwrite import Policy, annuity_payment, underwrite
from lockbox.units import pct_to_units, period_rate

NOW, IV = 1_800_000_000, 120
P0 = Policy(payment_interval=IV, lookback_buckets=12, service_fee_drops=50_000)


def flows():
    return [Inflow(NOW - (6 - k) * IV + 5 + i, 10_000_000, s) for k in range(6) for i, s in enumerate("abc")]


def test_unlocked_is_six_points_dearer_and_half_the_limit():
    o = underwrite(flows(), NOW, P0)
    u = o.unlocked
    assert o.approved and u["approved"]
    assert u["rate_pct"] == o.rate_pct + 6.0
    assert u["interest_rate_units"] == pct_to_units(o.rate_pct + 6.0)
    assert u["principal_drops"] == o.principal_drops // 2
    r = period_rate(u["interest_rate_units"], IV)
    assert u["installment_drops"] == int(annuity_payment(u["principal_drops"], r, o.payment_total)) + 50_000


def test_unlocked_terms_are_labelled_as_our_assumptions():
    u = underwrite(flows(), NOW, P0).unlocked
    assert "assumption" in u["label"].lower()
    assert any("not market data" in a for a in u["assumptions"])
    assert any("6" in a for a in u["assumptions"]) and any("half" in a for a in u["assumptions"])


def test_unlocked_settings_come_from_the_policy():
    p = Policy(payment_interval=IV, lookback_buckets=12, unlocked_rate_add_pct=4.0, unlocked_limit_factor=0.25)
    o = underwrite(flows(), NOW, p)
    assert o.unlocked["rate_pct"] == o.rate_pct + 4.0
    assert o.unlocked["principal_drops"] == int(o.principal_drops * 0.25)


def test_declined_borrower_is_declined_unlocked_too():
    o = underwrite([], NOW, P0)
    assert not o.approved and not o.unlocked["approved"] and o.unlocked["principal_drops"] == 0


def test_state_shows_both_quotes(sc):
    sc.quote()
    off = sc.state()["offer"]
    assert off["unlocked"]["principal_drops"] == off["principal"] // 2
    assert off["rate_pct"] + 6.0 == off["unlocked"]["rate_pct"]
