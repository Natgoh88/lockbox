"""The dashboard's freeze demo, click for click: Stop sales, then Advance with no buyer payments.
Each click is one 2-minute interval with zero sales (what the dashboard now sends once sales are stopped)."""
from lockbox import verify
from lockbox.units import xrp_to_drops

IV = 120


def _click(sc):
    return sc.run(IV, step=30, revenue_per_step=0)  # /api/advance {seconds: 120, revenue_xrp: 0}


def _withdraw_free(sc):
    return sc.agent.withdraw(sc.state()["lockbox"]["free_cash"])  # /api/withdraw {mode: free}


def test_stop_sales_then_advance_freezes_before_default(stocked):
    sc = stocked
    sc.agent.pay_payee(sc.supplier, sc.state()["buckets"]["unspent_principal"], "PO-1")  # Pay supplier
    acts = _click(sc)  # Stop sales
    assert "pay" in acts and sc.state()["frozen"] == [] and sc.state()["status"] == "current"
    _click(sc)  # Advance #1: two whole intervals with no sales
    s = sc.state()
    assert "revenue_short" in s["frozen"] and s["status"] != "defaulted"
    assert s["buckets"]["short_streak"] >= 2

    # Withdraw free cash and Withdraw all: refused, the freeze is the deciding rule, in plain English
    for v, r in (_withdraw_free(sc), sc.agent.withdraw(sc.b.balance(sc.lockbox) - sc.b.reserve(sc.lockbox))):
        assert not v.allowed and r is None
        assert verify.deciding_rule(v) == "revenue_covers_installment"
        detail = next(c.detail for c in v.checks if c.name == "revenue_covers_installment")
        assert detail == "Sales were below the installment 2 periods in a row, so withdrawals are frozen."

    # collection still works while frozen: cash comes in, the servicer's LoanPay is signed
    sc.earn(xrp_to_drops(30))
    acts = sc.run(30, step=30)
    assert "late pay" in acts, acts
    assert sc.policy.last_verdict.allowed and sc.state()["loan"]["remaining"] == 4


def test_frozen_account_still_allows_full_payoff_while_current(stocked):
    sc = stocked
    sc.agent.pay_payee(sc.supplier, sc.state()["buckets"]["unspent_principal"], "PO-1")
    _click(sc)
    _click(sc)  # two empty intervals; the next payment is due right now, not yet past due
    assert "revenue_short" in sc.state()["frozen"] and "past_due" not in sc.state()["frozen"]
    sc.earn(xrp_to_drops(60))
    v, r = sc.pay_off()
    assert v.allowed and r.ok and sc.state()["status"] == "paid"


def test_without_the_freeze_the_old_stop_sales_jump_defaulted_in_one_click(stocked):
    # what the old button did: one 10-minute jump. The loan defaults inside that single click.
    sc = stocked
    sc.agent.pay_payee(sc.supplier, sc.state()["buckets"]["unspent_principal"], "PO-1")
    acts = sc.run(600, step=30, revenue_per_step=0)
    assert "default" in acts and sc.state()["status"] == "defaulted"
