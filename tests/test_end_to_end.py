"""The whole story on the in-memory ledger."""
import pytest

from lockbox import protocol as P
from lockbox.roles import Refused
from lockbox.units import xrp_to_drops


def test_quote_comes_from_the_ledger_history(sc):
    o = sc.quote()
    assert o.approved and o.principal_drops > 0
    assert o.metrics.history_buckets >= 5
    # the lender's own payments are not counted as revenue
    assert all(f for f in o.reasons)


def test_servicer_refuses_an_unlocked_lockbox(sc):
    sc.quote()
    sc.agent.accept_offer(sc.offer, sc.broker_id)
    with pytest.raises(RuntimeError, match="not locked down"):
        sc.servicer.originate(sc.offer, sc.agent)


def test_lockbox_master_key_is_dead_after_lock(sc):
    sc.lock()
    r = sc.b.submit(sc.b.prepare(P.as_dict(P.payment(sc.lockbox, sc.payout, 1_000))), [sc.lockbox_key])
    assert not r.ok


def test_borrower_alone_cannot_move_money(sc):
    sc.lock()
    r = sc.b.submit(sc.b.prepare(P.as_dict(P.payment(sc.lockbox, sc.payout, 1_000)), 2), [sc.agent.key])
    assert r.code == "tefBAD_QUORUM"


def test_lender_alone_cannot_move_money(sc):
    sc.lock()
    r = sc.b.submit(sc.b.prepare(P.as_dict(P.payment(sc.lockbox, sc.lender, 1_000)), 2), [sc.lender])
    assert r.code == "tefBAD_QUORUM"


def test_happy_path_repays_in_full_and_lender_earns(sc):
    lender_before = sc.b.balance(sc.lender)
    sc.lock()
    sc.quote()
    assert sc.borrow().ok
    acts = sc.run(6 * 120 + 60, step=30, revenue_per_step=xrp_to_drops(1))
    assert acts.count("pay") == 6
    s = sc.state()
    assert s["status"] == "paid" and s["loan"]["remaining"] == 0
    assert s["vault"]["total"] > xrp_to_drops(300)  # interest accrued to depositors
    assert sc.b.balance(sc.lender) > lender_before - xrp_to_drops(300) - xrp_to_drops(40)  # service fees reached the lender


def test_lender_can_collect_without_the_borrower_online(loaned):
    # the borrower agent is never asked to sign a LoanPay: lender + referee are enough
    loaned.run(6 * 120 + 60, step=30, revenue_per_step=xrp_to_drops(1))
    assert loaned.state()["status"] == "paid"


def test_withdraw_everything_refused_withdraw_free_cash_allowed(loaned):
    s = loaned.state()["lockbox"]
    v, r = loaned.agent.withdraw(s["balance"] - 2_000_000)
    assert not v.allowed and r is None and "within_free_cash" in v.reason
    # M2: right after funding the money is loan principal, not earned cash, so nothing is free yet
    assert s["free_cash"] == 0
    loaned.earn(xrp_to_drops(5))  # a buyer pays an invoice
    s = loaned.state()["lockbox"]
    assert s["free_cash"] > 0
    before = loaned.b.balance(loaned.payout)
    v, r = loaned.agent.withdraw(s["free_cash"])
    assert v.allowed and r.ok
    assert loaned.b.balance(loaned.payout) == before + s["free_cash"]
    # what is left still covers the next installment
    left = loaned.b.balance(loaned.lockbox)
    assert left >= loaned.state()["loan"]["installment"]


def test_a_withdrawal_to_a_stranger_is_refused(loaned):
    tx = loaned.b.prepare(P.as_dict(P.payment(loaned.lockbox, loaned.lender, 1_000)), 2)
    with pytest.raises(Refused):
        loaned.policy.cosign(tx)


def test_tampered_loanset_is_refused_by_the_referee(sc):
    sc.lock()
    sc.quote()
    sc.agent.accept_offer(sc.offer, sc.broker_id)
    bigger = sc.offer
    tx = sc.b.prepare(P.as_dict(P.loan_set(sc.servicer.address, sc.lockbox, sc.broker_id, bigger)), 2)
    tx["PrincipalRequested"] = str(int(tx["PrincipalRequested"]) * 3)
    with pytest.raises(Refused) as e:
        sc.policy.cosign(tx)
    assert "same_principal" in str(e.value)


def test_borrower_refuses_a_loanset_that_differs_from_the_offer(sc):
    sc.lock()
    sc.quote()
    sc.agent.accept_offer(sc.offer, sc.broker_id)
    tx = sc.b.prepare(P.as_dict(P.loan_set(sc.servicer.address, sc.lockbox, sc.broker_id, sc.offer)), 2)
    tx["InterestRate"] = tx["InterestRate"] + 5000
    with pytest.raises(ValueError):
        sc.agent.cosign_loan(tx)


def test_no_second_loan_while_one_is_active(loaned):
    loaned.agent.accept_offer(loaned.offer, loaned.broker_id)
    tx = loaned.b.prepare(P.as_dict(P.loan_set(loaned.servicer.address, loaned.lockbox, loaned.broker_id, loaned.offer)), 2)
    with pytest.raises(Refused):
        loaned.policy.cosign(tx)


def _spend_principal(loaned):
    """M2: the borrower spends everything it may on its approved supplier (what the loan was for).
    Before M2 the tests pulled this money to the borrower's own wallet as 'free cash', which the rulebook now refuses.
    Takes the `stocked` fixture: the supplier was approved before LoanSet."""
    bk = loaned.state()["buckets"]
    v, r = loaned.agent.pay_payee(loaned.supplier, bk["unspent_principal"] + bk["earned_free_cash"], "PO-1")
    assert v.allowed and r.ok, v.reason
    return r


def test_revenue_stops_loan_goes_late_impaired_defaulted_cover_pays_and_recovery_runs(stocked):
    loaned = stocked
    # the business spends the loan on stock and then its revenue dries up
    _spend_principal(loaned)
    vault_before = loaned.state()["vault"]["total"]
    cover_before = loaned.state()["broker"]["cover"]
    acts = loaned.run(6 * 120 + 600, step=30, revenue_per_step=0)
    assert "pay" in acts  # the first installment was protected by the reserve and was paid
    assert "short" in acts and "impair" in acts and "default" in acts
    final = loaned.state()
    assert final["status"] == "defaulted"
    assert final["broker"]["cover"] < cover_before  # first-loss cover was used
    assert final["vault"]["total"] > vault_before - xrp_to_drops(67.2)  # the vault lost less than the full principal
    assert final["vault"]["loss_unrealized"] == 0


def _stress(loaned, top_up_xrp):
    """Borrower spends all it may with a supplier, pays the first installment from what is left, then revenue nearly stops."""
    _spend_principal(loaned)
    loaned.run(100, step=10)  # first installment is collected at the lead time
    assert loaned.state()["loan"]["remaining"] == 5
    if top_up_xrp:
        loaned.earn(xrp_to_drops(top_up_xrp))


def test_default_recovers_what_is_left_and_only_to_the_lender(stocked):
    loaned = stocked
    _stress(loaned, top_up_xrp=8)  # balance is now above the reserve but below the next installment
    lender_before = loaned.b.balance(loaned.lender)
    cover_before = loaned.state()["broker"]["cover"]
    acts = loaned.run(500, step=30)
    assert "short" in acts and "impair" in acts and "default" in acts
    assert any(a.startswith("recovery") for a in acts)
    s = loaned.state()
    assert s["status"] == "defaulted"
    assert s["broker"]["cover"] < cover_before
    assert loaned.b.balance(loaned.lender) > lender_before  # recovery payment arrived
    reserve = loaned.b.reserve(loaned.lockbox)
    assert loaned.b.balance(loaned.lockbox) <= reserve + loaned.params.fee_buffer_drops + 100  # swept down to reserve plus fee buffer
    # the borrower is frozen out, and cannot pull anything after the default
    v, r = loaned.agent.withdraw(1_000)
    assert not v.allowed


def test_recovery_never_exceeds_what_was_owed(stocked):
    loaned = stocked
    _stress(loaned, top_up_xrp=8)
    loaned.run(500, step=30)
    loaned.earn(xrp_to_drops(500))  # a windfall after default
    loaned.servicer.tick()
    loaned.servicer.tick()
    owed = loaned.policy.owed_at_default
    assert loaned.policy.recovered <= owed
    assert loaned.policy.recovery_remaining == owed - loaned.policy.recovered


def test_late_payment_during_grace_clears_the_impairment(stocked):
    loaned = stocked
    _stress(loaned, top_up_xrp=0)
    acts = loaned.run(160, step=10)  # second installment is due and cannot be paid
    assert "impair" in acts and loaned.state()["loan"]["impaired"]
    assert loaned.state()["vault"]["loss_unrealized"] > 0
    loaned.earn(xrp_to_drops(30))  # revenue returns during the grace period
    acts = loaned.run(20, step=10)
    assert "late pay" in acts
    s = loaned.state()
    assert not s["loan"]["impaired"] and s["loan"]["remaining"] == 4 and s["status"] == "current"
    assert s["vault"]["loss_unrealized"] == 0


def test_withdrawals_are_frozen_while_the_loan_is_late(stocked):
    loaned = stocked
    _stress(loaned, top_up_xrp=0)
    loaned.run(160, step=10)
    loaned.earn(xrp_to_drops(30))
    v, r = loaned.agent.withdraw(1_000)
    assert not v.allowed and "loan_current" in v.reason


def test_events_record_the_decisions(loaned):
    from lockbox import events
    loaned.agent.withdraw(10**12)
    kinds = [e["kind"] for e in events.recent()]
    assert "quote" in kinds and "verify_lockbox" in kinds and "loan_set" in kinds and "verdict" in kinds
