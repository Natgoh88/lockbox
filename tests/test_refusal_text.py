"""A refused withdrawal names what it would dip into. Text only: the decision is unchanged."""
from lockbox import buckets as B
from lockbox.covenant import not_free_explanation, xrp_text
from lockbox.units import xrp_to_drops


def _b(**kw):
    base = dict(balance=0, reserve=2_000_000, fee_buffer=5_000, bond=0, installment_reserve=0, holdback=0,
                unspent_principal=0, earned_free_cash=0, shortfall=0)
    base.update(kw)
    return B.Buckets(**base)


def test_installment_reserve_and_fee_buffer_named():
    b = _b(installment_reserve=9_989_963, earned_free_cash=30_000_000)
    t = not_free_explanation(30_000_000 + 9_994_963, 30_000_000, b)
    assert t == ("That would take 9.995 XRP that isn't free: 9.99 XRP set aside for the next installment and "
                 "0.005 XRP fee buffer. Repayment comes first.")


def test_loan_money_named_first():
    b = _b(unspent_principal=xrp_to_drops(50), installment_reserve=xrp_to_drops(10))
    t = not_free_explanation(xrp_to_drops(55), 0, b)
    assert t.startswith("That would take 55.00 XRP that isn't free: 50.00 XRP of loan money and 5.00 XRP set aside")
    assert t.endswith("Loan money may only pay approved suppliers.")


def test_more_than_the_account_holds():
    t = not_free_explanation(xrp_to_drops(1000), 0, _b())
    assert "more than the account holds" in t


def test_xrp_text():
    assert xrp_text(59_639_693) == "59.64 XRP" and xrp_text(5_000) == "0.005 XRP" and xrp_text(9_995_000) == "10.00 XRP"


def test_sim_withdraw_all_after_the_supplier_names_the_installment(stocked):
    sc = stocked
    sc.agent.pay_payee(sc.supplier, sc.state()["buckets"]["unspent_principal"], "PO-1")
    sc.run(120, step=30, revenue_per_step=xrp_to_drops(2.5))
    bk = sc.state()["buckets"]
    assert bk["unspent_principal"] == 0 and bk["installment_reserve"] > 0
    v, r = sc.agent.withdraw(sc.b.balance(sc.lockbox) - sc.b.reserve(sc.lockbox))
    assert not v.allowed and r is None
    detail = next(c.detail for c in v.checks if c.name == "within_free_cash")
    assert "set aside for the next installment" in detail and "Repayment comes first." in detail
    assert "loan money" not in detail
