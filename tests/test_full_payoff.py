"""Early full payoff, as in the XRPL "Pay off a loan" tutorial: one LoanPay for TotalValueOutstanding + LoanServiceFee, no flags."""
import pytest
from xrpl.wallet import Wallet

from lockbox import buckets as B
from lockbox.covenant import CovenantContext, LoanView, evaluate
from lockbox.units import xrp_to_drops

LB, LENDER, LID = "rLockbox", "rLender", "L" * 64
RESERVE, FEE_BUF = 2_000_000, 5_000
TVO, FEE = 4_200_000, 50_000
PAYOFF = TVO + FEE


def loan(**kw):
    base = dict(loan_id=LID, borrower=LB, periodic_payment=1_000_000, service_fee=FEE, next_due=1000, payment_remaining=4, grace_period=100,
                total_owed=TVO)
    base.update(kw)
    return LoanView(**base)


def ctx(balance=20_000_000, bond=0, **kw):
    bk = None
    if bond:
        hist = [{"tx": {"TransactionType": "Payment", "Account": "rOwn", "Destination": LB, "Amount": str(bond), "Fee": "10",
                        "Memos": [{"Memo": {"MemoData": b"bond".hex().upper()}}]},
                 "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": str(bond)}, "validated": True}]
        cfg = B.Config(lockbox=LB, lender=LENDER, account_reserve=RESERVE, fee_buffer=FEE_BUF)
        bk = B.replay(hist, balance, cfg, B.LoanFacts(installment=1_050_000, payment_remaining=4, active=True, settled=False))
    base = dict(lockbox=LB, lender=LENDER, loan=loan(), balance_drops=balance, account_reserve_drops=RESERVE, now=500, fee_buffer_drops=FEE_BUF,
                buckets=bk)
    base.update(kw)
    return CovenantContext(**base)


def payoff(amount=PAYOFF, **kw):
    tx = {"TransactionType": "LoanPay", "Account": LB, "LoanID": LID, "Amount": str(amount), "Fee": "30"}
    tx.update(kw)
    return tx


def failed(v):
    return {c.name for c in v.checks if not c.ok}


def test_full_payoff_allowed_while_current():
    v = evaluate(payoff(), ctx())
    assert v.allowed, v.reason


def test_full_payoff_refused_when_past_due():
    v = evaluate(payoff(), ctx(now=1001))
    assert not v.allowed and "payoff_while_current" in failed(v)


def test_full_payoff_refused_for_any_other_amount():
    for amt in (PAYOFF - 1, PAYOFF + 1, TVO, 2 * PAYOFF):
        assert "amount_matches_installment" in failed(evaluate(payoff(amt), ctx())), amt


def test_full_payoff_refused_with_flags():
    assert "no_special_flags" in failed(evaluate(payoff(Flags=131072), ctx()))  # tfLoanFullPayment
    assert "no_special_flags" in failed(evaluate(payoff(Flags=65536), ctx()))  # overpayment
    assert "no_special_flags" in failed(evaluate(payoff(Flags=262144), ctx()))  # late flag before the due date


def test_full_payoff_refused_if_it_would_touch_the_bond():
    bond = 3_000_000
    tight = RESERVE + FEE_BUF + PAYOFF + bond - 1  # one drop short without dipping into the bond
    v = evaluate(payoff(), ctx(balance=tight, bond=bond))
    assert not v.allowed and "payoff_within_funds" in failed(v)
    assert evaluate(payoff(), ctx(balance=tight + 1, bond=bond)).allowed


def test_full_payoff_refused_if_it_would_touch_the_fee_reserve():
    v = evaluate(payoff(), ctx(balance=RESERVE + FEE_BUF + PAYOFF - 1))
    assert not v.allowed and "payoff_within_funds" in failed(v)
    assert evaluate(payoff(), ctx(balance=RESERVE + FEE_BUF + PAYOFF)).allowed


def test_full_payoff_refused_once_the_loan_is_closed():
    assert "loan_open" in failed(evaluate(payoff(), ctx(loan=loan(payment_remaining=0))))


# ---- on the simulated ledger ----

def test_sim_full_payoff_from_unspent_principal_then_release(sc):
    sc.b.fund(sc.agent.address, xrp_to_drops(10))
    assert sc.post_bond(xrp_to_drops(5)).ok
    sc.lock()
    sc.quote()
    assert sc.borrow().ok
    s0 = sc.state()
    vault0, owed, fee = s0["vault"]["total"], s0["loan"]["owed"], sc.offer.service_fee_drops
    lender0 = sc.b.balance(sc.lender)
    v, r = sc.pay_off()
    assert v.allowed and r.ok, v.reason
    assert int(r.tx["Amount"]) == owed + fee
    s = sc.state()
    assert s["status"] == "paid" and s["loan"]["remaining"] == 0 and s["loan"]["owed"] == 0
    assert s["broker"]["debt"] == 0
    assert s["vault"]["total"] == vault0 + owed - sc.offer.principal_drops  # depositors got principal back plus the interest
    assert sc.b.balance(sc.lender) == lender0 + fee
    # the bond was never touched and is now the borrower's again
    assert s["buckets"]["bond"] == 0 and s["buckets"]["earned_free_cash"] >= xrp_to_drops(5)
    v, r = sc.release()
    assert v.allowed and r.ok, v.reason
    assert sc.b.lockbox_state(sc.lockbox)["signers"] == {}


def test_sim_full_payoff_refused_when_past_due(loaned):
    loaned.b.advance(loaned.state()["loan"]["next_due"] - loaned.b.now() + 1)
    v, r = loaned.pay_off()
    assert not v.allowed and r is None and "payoff_while_current" in v.reason
    assert loaned.state()["loan"]["remaining"] == 6


def test_sim_lender_and_referee_can_also_pay_off(loaned):
    s = loaned.state()
    from lockbox import protocol as P
    tx = loaned.b.prepare(P.as_dict(P.loan_pay(loaned.lockbox, loaned.servicer.loan_id, s["loan"]["owed"] + loaned.offer.service_fee_drops)), 2)
    r = loaned.b.submit(tx, [loaned.lender, loaned.policy.cosign(tx)])
    assert r.ok and loaned.state()["status"] == "paid"
