"""Past grace, the servicer tries one late LoanPay before it defaults the loan."""
from lockbox.units import xrp_to_drops


def _past_grace_unticked(sc):
    """Supplier spend, first installment paid, revenue stops, the second due date and its grace pass with no servicer pass."""
    sc.agent.pay_payee(sc.supplier, sc.state()["buckets"]["unspent_principal"], "PO-1")
    sc.run(100, step=10)
    assert sc.state()["loan"]["remaining"] == 5
    ln = sc.b.loan_view(sc.servicer.loan_id)
    sc.wait(ln.next_due + ln.grace_period - sc.b.now() + 10)


def test_cash_there_past_grace_pays_late_instead_of_default(stocked):
    _past_grace_unticked(stocked)
    stocked.earn(xrp_to_drops(30))
    acts = stocked.servicer.tick()
    assert acts == ["late pay"]
    s = stocked.state()
    assert s["status"] != "defaulted" and s["loan"]["remaining"] == 4


def test_no_cash_past_grace_defaults(stocked):
    _past_grace_unticked(stocked)
    acts = stocked.servicer.tick()
    assert "default" in acts and not any("late pay" in a for a in acts)
    assert stocked.state()["status"] == "defaulted"


def test_late_pay_that_fails_on_the_ledger_still_defaults(stocked, monkeypatch):
    _past_grace_unticked(stocked)
    stocked.earn(xrp_to_drops(30))
    monkeypatch.setattr(stocked.b, "_do_LoanPay", lambda tx: stocked.b._log(tx, "tecKILLED"))
    acts = stocked.servicer.tick()
    assert "late pay failed tecKILLED" in acts and "default" in acts
    assert stocked.state()["status"] == "defaulted"


def test_only_one_late_try_per_due_date_past_grace(stocked, monkeypatch):
    _past_grace_unticked(stocked)
    stocked.earn(xrp_to_drops(30))
    calls = []
    monkeypatch.setattr(stocked.servicer, "_late_pay", lambda ln, acts: calls.append(1) or False)
    stocked.servicer.tick()
    stocked.servicer.tick()
    assert len(calls) == 1
