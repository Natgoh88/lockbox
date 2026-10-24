"""M2 chunk A: buckets rebuilt from ledger history, approved payees, withdrawals only from earned free cash."""
import pytest

from lockbox import buckets as B
from lockbox import protocol as P
from lockbox.buckets import Payee
from lockbox.covenant import CovenantContext, LoanView, evaluate
from lockbox.units import xrp_to_drops

LB, LENDER, PAYOUT, SUPPLIER, BUYER = "rLockbox", "rLender", "rPayout", "rSupplier", "rBuyer"
RESERVE, FEE_BUF = 2_000_000, 5_000
INST = 1_050_000


def entry(tx, code="tesSUCCESS", **meta):
    return {"tx": tx, "meta": {"TransactionResult": code, **meta}, "validated": True}


def incoming(sender, drops, memo=None):
    tx = {"TransactionType": "Payment", "Account": sender, "Destination": LB, "Amount": str(drops), "Fee": "10"}
    if memo:
        tx["Memos"] = [{"Memo": {"MemoData": memo.encode().hex().upper()}}]
    return entry(tx, delivered_amount=str(drops))


def outgoing(dest, drops, fee=30, memo=None):
    tx = {"TransactionType": "Payment", "Account": LB, "Destination": dest, "Amount": str(drops), "Fee": str(fee)}
    if memo:
        tx["Memos"] = [{"Memo": {"MemoData": memo.encode().hex().upper()}}]
    return entry(tx, delivered_amount=str(drops))


def loanset(principal, fee=0):
    tx = {"TransactionType": "LoanSet", "Account": LENDER, "Counterparty": LB, "PrincipalRequested": str(principal), "Fee": "60",
          "PaymentInterval": 120}
    if fee:
        tx["LoanOriginationFee"] = str(fee)
    return entry(tx)


def loanpay(drops, fee=30):
    return entry({"TransactionType": "LoanPay", "Account": LB, "LoanID": "L" * 64, "Amount": str(drops), "Fee": str(fee)},
                 delivered_amount=str(drops))


def cfg(**kw):
    base = dict(lockbox=LB, lender=LENDER, approved_payees={SUPPLIER: Payee(cap_drops=40_000_000, invoice_ref="INV-7")},
                account_reserve=RESERVE, fee_buffer=FEE_BUF)
    base.update(kw)
    return B.Config(**base)


ACTIVE = B.LoanFacts(installment=INST, payment_remaining=4, active=True)


def run(history, start_balance=12_000_000, facts=ACTIVE, config=None):
    """Replay with the final balance computed from the history, like a real ledger would report it."""
    bal = start_balance + sum(B.balance_delta(e, LB) for e in history)
    return B.replay(history, bal, config or cfg(), facts)


def test_principal_lands_in_unspent_principal_not_free_cash():
    bk = run([loanset(50_000_000)])
    assert bk.unspent_principal == 50_000_000
    # the pre-loan float (12 XRP) pays the reserve, fee buffer and next installment first
    assert bk.earned_free_cash == 12_000_000 - RESERVE - FEE_BUF - INST


def test_buckets_add_up_to_the_balance():
    bk = run([incoming(BUYER, 5_000_000), loanset(50_000_000), outgoing(SUPPLIER, 10_000_000, memo="INV-7"),
              incoming(BUYER, 3_000_000), loanpay(INST)])
    parts = bk.reserve + bk.fee_buffer + bk.bond + bk.installment_reserve + bk.holdback + bk.unspent_principal + bk.earned_free_cash
    assert bk.shortfall == 0 and parts == bk.balance


def test_loan_pay_draws_earned_before_principal():
    bk = run([loanset(50_000_000), incoming(BUYER, 2_000_000), loanpay(INST)])
    assert bk.unspent_principal == 50_000_000  # the 12 XRP float plus revenue paid the installment


def test_supplier_payment_spends_principal_and_counts_against_the_cap():
    bk = run([loanset(50_000_000), outgoing(SUPPLIER, 10_000_000, memo="INV-7")])
    assert bk.unspent_principal == 40_000_000
    assert bk.paid_to_payees == {SUPPLIER: 10_000_000}


def test_refund_from_an_approved_payee_goes_back_to_principal():
    before = run([loanset(50_000_000), outgoing(SUPPLIER, 10_000_000, memo="INV-7")])
    after = run([loanset(50_000_000), outgoing(SUPPLIER, 10_000_000, memo="INV-7"), incoming(SUPPLIER, 10_000_000)])
    assert after.unspent_principal == before.unspent_principal + 10_000_000
    assert after.earned_free_cash == before.earned_free_cash


def test_principal_and_holdback_released_once_the_loan_is_settled():
    paid = B.LoanFacts(installment=INST, payment_remaining=0, active=False, settled=True)
    bk = run([loanset(50_000_000)], facts=paid)
    assert bk.unspent_principal == 0 and bk.installment_reserve == 0
    assert bk.earned_free_cash == bk.balance - RESERVE - FEE_BUF


def test_newest_first_history_gives_the_same_buckets():
    hist = [loanset(50_000_000), outgoing(SUPPLIER, 10_000_000, memo="INV-7"), incoming(BUYER, 3_000_000)]
    for i, e in enumerate(hist):
        e["ledger_index"] = 100 + i
    bal = 12_000_000 + sum(B.balance_delta(e, LB) for e in hist)
    assert B.replay(hist, bal, cfg(), ACTIVE) == B.replay(list(reversed(hist)), bal, cfg(), ACTIVE)


def test_replay_twice_gives_identical_buckets():
    hist = [incoming(BUYER, 5_000_000), loanset(50_000_000), outgoing(SUPPLIER, 10_000_000, memo="INV-7"), incoming(SUPPLIER, 1_000)]
    assert run(hist) == run(hist)
    assert run(hist) == run([dict(e) for e in hist])


def test_meta_balance_change_is_preferred_when_present():
    e = incoming(BUYER, 5_000_000)
    e["meta"]["AffectedNodes"] = [{"ModifiedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {"Account": LB, "Balance": "17000000"},
                                                    "PreviousFields": {"Balance": "12000000"}}}]
    assert B.balance_delta(e, LB) == 5_000_000
    assert B.balance_delta(outgoing(SUPPLIER, 1_000, fee=30), LB) == -1_030


# ---- the rulebook on top of the buckets ----

def loan():
    return LoanView(loan_id="L" * 64, borrower=LB, periodic_payment=1_000_000, service_fee=50_000, next_due=1000, payment_remaining=4,
                    grace_period=100, total_owed=4_200_000)


def ctx(history, **kw):
    bal = 12_000_000 + sum(B.balance_delta(e, LB) for e in history)
    c = cfg()
    bk = B.replay(history, bal, c, ACTIVE)
    base = dict(lockbox=LB, lender=LENDER, loan=loan(), balance_drops=bal, account_reserve_drops=RESERVE, now=500, allowlist={PAYOUT},
                approved_payees=dict(c.approved_payees), buckets=bk)
    base.update(kw)
    return CovenantContext(**base)


def send(to, amount, memo=None):
    tx = {"TransactionType": "Payment", "Account": LB, "Destination": to, "Amount": str(amount), "Fee": "30"}
    if memo:
        tx["Memos"] = [{"Memo": {"MemoType": "696E766F696365", "MemoData": memo.encode().hex().upper()}}]
    return tx


def failed(v):
    return {c.name for c in v.checks if not c.ok}


def test_principal_to_own_wallet_refused():
    c = ctx([loanset(50_000_000)])
    v = evaluate(send(PAYOUT, 20_000_000), c)
    assert not v.allowed and "within_free_cash" in failed(v)
    assert v.free_cash == c.buckets.earned_free_cash < 20_000_000


def test_earned_free_cash_to_own_wallet_allowed():
    c = ctx([loanset(50_000_000), incoming(BUYER, 6_000_000)])
    assert evaluate(send(PAYOUT, c.buckets.earned_free_cash), c).allowed


def test_approved_supplier_with_invoice_memo_allowed():
    c = ctx([loanset(50_000_000)])
    assert evaluate(send(SUPPLIER, 30_000_000, memo="INV-7"), c).allowed


def test_supplier_over_cap_refused():
    c = ctx([loanset(50_000_000), outgoing(SUPPLIER, 30_000_000, memo="INV-7")])
    v = evaluate(send(SUPPLIER, 10_000_001, memo="INV-7"), c)
    assert not v.allowed and "within_payee_cap" in failed(v)
    assert evaluate(send(SUPPLIER, 10_000_000, memo="INV-7"), c).allowed


def test_supplier_payment_without_invoice_memo_refused():
    c = ctx([loanset(50_000_000)])
    assert "invoice_memo" in failed(evaluate(send(SUPPLIER, 1_000_000), c))
    assert "invoice_memo" in failed(evaluate(send(SUPPLIER, 1_000_000, memo="INV-8"), c))


def test_supplier_payment_cannot_spend_the_next_installment():
    c = ctx([loanset(5_000_000)])
    spendable = c.buckets.unspent_principal + c.buckets.earned_free_cash
    assert evaluate(send(SUPPLIER, spendable, memo="INV-7"), c).allowed
    assert "within_principal" in failed(evaluate(send(SUPPLIER, spendable + 1, memo="INV-7"), c))


def test_refund_then_withdraw_refused():
    # launder principal through a friendly supplier: pay out, get it refunded, try to pull it to the own wallet
    hist = [loanset(50_000_000), outgoing(SUPPLIER, 30_000_000, memo="INV-7"), incoming(SUPPLIER, 30_000_000)]
    c = ctx(hist)
    assert c.buckets.unspent_principal == 50_000_000
    v = evaluate(send(PAYOUT, 30_000_000), c)
    assert not v.allowed and "within_free_cash" in failed(v)


def test_unknown_destination_still_refused():
    assert "destination_allowlisted" in failed(evaluate(send("rThief", 1_000), ctx([loanset(50_000_000)])))


# ---- end to end on the simulated ledger ----

@pytest.fixture
def funded(loaned):
    loaned.approve_payee(loaned.customers[2], cap_drops=xrp_to_drops(40), invoice_ref="PO-1001")
    return loaned


def test_sim_principal_is_not_free_cash_after_funding(funded):
    bk = funded.state()["buckets"]
    # the 12 XRP float does not quite cover reserve + fee buffer + installment, so principal tops up the installment reserve
    assert bk["earned_free_cash"] == 0
    assert funded.offer.principal_drops - bk["installment_reserve"] < bk["unspent_principal"] < funded.offer.principal_drops
    v, r = funded.agent.withdraw(xrp_to_drops(20))
    assert not v.allowed and r is None


def test_sim_pay_supplier_then_refund_then_withdraw(funded):
    sup = funded.customers[2]
    p0 = funded.state()["buckets"]["unspent_principal"]
    v, r = funded.agent.pay_payee(sup, xrp_to_drops(30), "PO-1001")
    assert v.allowed and r.ok
    assert funded.state()["buckets"]["unspent_principal"] == p0 - xrp_to_drops(30) - int(r.tx["Fee"])  # fee comes off too: no earned cash left
    funded._send(P.payment(sup, funded.lockbox, xrp_to_drops(30)), sup, "supplier refund")
    assert funded.state()["buckets"]["unspent_principal"] == p0 - int(r.tx["Fee"])
    assert funded.state()["buckets"]["earned_free_cash"] == 0
    v, r = funded.agent.withdraw(xrp_to_drops(30))
    assert not v.allowed and "within_free_cash" in v.reason


def test_sim_revenue_becomes_withdrawable(funded):
    before = funded.state()["buckets"]["earned_free_cash"]
    funded.earn(xrp_to_drops(5))
    after = funded.state()["buckets"]["earned_free_cash"]
    assert after > before
    v, r = funded.agent.withdraw(after)
    assert v.allowed and r.ok
