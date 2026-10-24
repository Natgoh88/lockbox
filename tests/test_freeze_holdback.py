"""M2 chunk B: freeze on ledger facts only, holdback, XRP fee reserve."""
from lockbox import buckets as B
from lockbox.buckets import Payee
from lockbox.covenant import CovenantContext, LoanView, evaluate, free_cash
from lockbox.units import unix_to_ripple, xrp_to_drops

LB, LENDER, PAYOUT, BUYER = "rLockbox", "rLender", "rPayout", "rBuyer"
RESERVE, FEE_BUF, MAX_FEE = 2_000_000, 5_000, 1_000
INST = 1_050_000
T0, IV = 1_800_000_000, 120


def entry(tx, ts, code="tesSUCCESS", **meta):
    return {"tx": {**tx, "date": unix_to_ripple(ts)}, "meta": {"TransactionResult": code, **meta}, "validated": True}


def loanset(principal=50_000_000, ts=T0):
    return entry({"TransactionType": "LoanSet", "Account": LENDER, "Counterparty": LB, "PrincipalRequested": str(principal), "Fee": "60",
                  "PaymentInterval": IV}, ts)


def revenue(drops, ts):
    return entry({"TransactionType": "Payment", "Account": BUYER, "Destination": LB, "Amount": str(drops), "Fee": "10"}, ts,
                 delivered_amount=str(drops))


def loan(**kw):
    base = dict(loan_id="L" * 64, borrower=LB, periodic_payment=1_000_000, service_fee=50_000, next_due=T0 + 10 * IV, payment_remaining=4,
                grace_period=IV, total_owed=4_200_000)
    base.update(kw)
    return LoanView(**base)


def ctx(history, now, start=12_000_000, ln=None, reserve_installments=1, holdback_bps=0):
    ln = ln or loan()
    bal = start + sum(B.balance_delta(e, LB) for e in history)
    cfg = B.Config(lockbox=LB, lender=LENDER, account_reserve=RESERVE, fee_buffer=FEE_BUF, reserve_installments=reserve_installments,
                   holdback_bps=holdback_bps)
    facts = B.LoanFacts(installment=ln.installment, payment_remaining=ln.payment_remaining, active=ln.active, settled=False)
    bk = B.replay(history, bal, cfg, facts, now=now)
    return CovenantContext(lockbox=LB, lender=LENDER, loan=ln, balance_drops=bal, account_reserve_drops=RESERVE, now=now, allowlist={PAYOUT},
                           buckets=bk, fee_buffer_drops=FEE_BUF, max_fee_drops=MAX_FEE, reserve_installments=reserve_installments,
                           holdback_bps=holdback_bps)


def send(amount, to=PAYOUT):
    return {"TransactionType": "Payment", "Account": LB, "Destination": to, "Amount": str(amount), "Fee": "30"}


def loanpay(amount=INST, fee=30):
    return {"TransactionType": "LoanPay", "Account": LB, "LoanID": "L" * 64, "Amount": str(amount), "Fee": str(fee)}


def failed(v):
    return {c.name for c in v.checks if not c.ok}


# ---- freeze ----

def test_past_due_freezes_withdrawals():
    c = ctx([loanset(), revenue(5_000_000, T0 + 10)], now=T0 + 10 * IV + 1)
    assert "loan_current" in failed(evaluate(send(1_000), c))


def test_lender_impair_alone_does_not_freeze():
    # the lender flags a healthy, current loan as impaired: that is not a ledger fact about the borrower
    hist = [loanset(), revenue(2_000_000, T0 + 10)]
    c = ctx(hist, now=T0 + 30, ln=loan(impaired=True))
    v = evaluate(send(1_000), c)
    assert v.allowed, v.reason


def test_revenue_short_two_intervals_in_a_row_freezes():
    hist = [loanset(), revenue(2_000_000, T0 + 10), revenue(100_000, T0 + IV + 10), revenue(100_000, T0 + 2 * IV + 10),
            revenue(20_000_000, T0 + 3 * IV - 5)]
    one_short = ctx(hist[:3], now=T0 + 2 * IV + 1)
    assert one_short.buckets.short_streak == 1
    assert evaluate(send(1_000), one_short).allowed
    two_short = ctx(hist[:4], now=T0 + 3 * IV + 1)
    assert two_short.buckets.revenue_by_interval == (2_000_000, 100_000, 100_000)
    assert two_short.buckets.short_streak == 2
    v = evaluate(send(1_000), two_short)
    assert not v.allowed and "revenue_covers_installment" in failed(v)


def test_freeze_lifts_when_revenue_recovers():
    hist = [loanset(), revenue(100_000, T0 + 10), revenue(100_000, T0 + IV + 10), revenue(5_000_000, T0 + 2 * IV + 10)]
    c = ctx(hist, now=T0 + 3 * IV + 1)
    assert c.buckets.short_streak == 0
    assert evaluate(send(1_000), c).allowed


def test_interval_in_progress_does_not_count():
    hist = [loanset(), revenue(100_000, T0 + 10), revenue(100_000, T0 + IV + 10)]
    c = ctx(hist, now=T0 + 2 * IV - 1)  # second interval has not finished yet
    assert c.buckets.short_streak == 1


def test_revenue_freeze_needs_the_clock():
    hist = [loanset(), revenue(100_000, T0 + 10), revenue(100_000, T0 + IV + 10)]
    bal = 12_000_000 + sum(B.balance_delta(e, LB) for e in hist)
    bk = B.replay(hist, bal, B.Config(lockbox=LB), B.LoanFacts(installment=INST, payment_remaining=4, active=True, settled=False))
    assert bk.short_streak == 0 and bk.revenue_by_interval == ()


# ---- holdback ----

def test_holdback_of_two_installments_keeps_two():
    hist = [loanset(), revenue(10_000_000, T0 + 10)]
    one = ctx(hist, now=T0 + 20)
    two = ctx(hist, now=T0 + 20, reserve_installments=2)
    assert two.buckets.installment_reserve == 2 * INST
    assert free_cash(one) - free_cash(two) == INST
    assert "within_free_cash" in failed(evaluate(send(free_cash(one)), two))


def test_holdback_never_reserves_more_installments_than_remain():
    c = ctx([loanset()], now=T0 + 20, ln=loan(payment_remaining=1), reserve_installments=3)
    assert c.buckets.installment_reserve == INST


def test_percentage_holdback_on_each_incoming_payment():
    hist = [revenue(4_000_000, T0 - 50), loanset(), revenue(10_000_000, T0 + 10)]
    plain = ctx(hist, now=T0 + 20)
    held = ctx(hist, now=T0 + 20, holdback_bps=2_000)  # 20% of revenue after the loan, nothing before it
    total_held = held.buckets.installment_reserve + held.buckets.holdback
    assert total_held == max(INST, 2_000_000)
    assert free_cash(plain) - free_cash(held) == 2_000_000 - INST


def test_holdback_pays_the_installment_first():
    hist = [loanset(), revenue(10_000_000, T0 + 10)]
    bal = 12_000_000 + sum(B.balance_delta(e, LB) for e in hist)
    pay = entry({"TransactionType": "LoanPay", "Account": LB, "Amount": str(INST), "Fee": "30"}, T0 + 100, delivered_amount=str(INST))
    cfg = B.Config(lockbox=LB, account_reserve=RESERVE, fee_buffer=FEE_BUF, holdback_bps=5_000)
    facts = B.LoanFacts(installment=INST, payment_remaining=3, active=True, settled=False)
    after = B.replay(hist + [pay], bal - INST - 30, cfg, facts)
    assert after.holdback + after.installment_reserve == 5_000_000 - INST - 30  # the LoanPay fee comes off the holdback too


# ---- XRP fee reserve ----

def test_withdrawing_all_free_cash_leaves_the_fee_buffer():
    c = ctx([loanset(), revenue(10_000_000, T0 + 10)], now=T0 + 20)
    fc = free_cash(c)
    assert evaluate(send(fc), c).allowed
    left = c.balance_drops - fc
    assert left == RESERVE + FEE_BUF + INST + c.buckets.unspent_principal


def test_loan_pay_must_leave_reserve_and_a_fee():
    tight = RESERVE + INST + MAX_FEE + 30  # just enough
    c = CovenantContext(lockbox=LB, lender=LENDER, loan=loan(), balance_drops=tight, account_reserve_drops=RESERVE, now=T0,
                        fee_buffer_drops=FEE_BUF, max_fee_drops=MAX_FEE)
    assert evaluate(loanpay(), c).allowed
    c.balance_drops = tight - 1
    assert "covered_by_balance" in failed(evaluate(loanpay(), c))


def test_fee_buffer_is_never_free_cash():
    c = CovenantContext(lockbox=LB, lender=LENDER, loan=None, balance_drops=RESERVE + FEE_BUF, account_reserve_drops=RESERVE, now=T0,
                        allowlist={PAYOUT}, fee_buffer_drops=FEE_BUF)
    assert free_cash(c) == 0
    assert not evaluate(send(1), c).allowed


def test_sim_holdback_two_installments(sc):
    from lockbox import protocol as P  # noqa: F401
    sc.params.reserve_installments = 2
    sc.lock()
    sc.quote()
    assert sc.borrow().ok
    bk = sc.state()["buckets"]
    assert bk["installment_reserve"] == 2 * sc.state()["loan"]["installment"]


def test_sim_revenue_stops_freezes_before_the_due_date_is_missed(loaned):
    loaned.earn(xrp_to_drops(30))  # plenty of earned cash to withdraw
    loaned.run(3 * 120, step=30)  # interval 0 has the top-up, then two whole intervals with no revenue
    s = loaned.state()
    assert s["status"] == "current"
    assert s["buckets"]["short_streak"] >= 2
    v, r = loaned.agent.withdraw(1_000)
    assert not v.allowed and "revenue_covers_installment" in v.reason
