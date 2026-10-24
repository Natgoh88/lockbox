from xrpl.wallet import Wallet

from lockbox.covenant import AcceptedTerms, CovenantContext, LoanView, evaluate, free_cash, sign_acceptance

BORROWER = Wallet.create()

LB, LENDER, PAYOUT, BROKER = "rLockbox", "rLender", "rPayout", "B" * 64
LID = "L" * 64


def loan(**kw):
    base = dict(loan_id=LID, borrower=LB, periodic_payment=1_000_000, service_fee=50_000, next_due=1000, payment_remaining=4,
                grace_period=100, total_owed=4_200_000)
    base.update(kw)
    return LoanView(**base)


def ctx(**kw):
    base = dict(lockbox=LB, lender=LENDER, loan=loan(), balance_drops=20_000_000, account_reserve_drops=2_000_000, now=500,
                allowlist={PAYOUT}, accepted=None, recovery_remaining=0)
    base.update(kw)
    return CovenantContext(**base)


def pay(amount=1_050_000, **kw):
    tx = {"TransactionType": "LoanPay", "Account": LB, "LoanID": LID, "Amount": str(amount), "Fee": "30"}
    tx.update(kw)
    return tx


def send(to=PAYOUT, amount=1_000_000, **kw):
    tx = {"TransactionType": "Payment", "Account": LB, "Destination": to, "Amount": str(amount), "Fee": "30"}
    tx.update(kw)
    return tx


def failed(v):
    return {c.name for c in v.checks if not c.ok}


def test_normal_loan_pay_allowed():
    assert evaluate(pay(), ctx()).allowed


def test_loan_pay_wrong_loan_amount_flags_fee():
    assert "loan_known" in failed(evaluate(pay(LoanID="X" * 64), ctx()))
    assert "amount_matches_installment" in failed(evaluate(pay(amount=1_000), ctx()))
    assert "amount_matches_installment" in failed(evaluate(pay(amount=9_000_000), ctx()))
    assert "no_special_flags" in failed(evaluate(pay(Flags=65536), ctx()))
    assert "no_special_flags" in failed(evaluate(pay(Flags=131072), ctx(now=1001)))
    assert "fee_cap" in failed(evaluate(pay(Fee="5000"), ctx()))


def test_loan_pay_after_default_or_paid_refused():
    assert "loan_open" in failed(evaluate(pay(), ctx(loan=loan(defaulted=True))))
    assert "loan_open" in failed(evaluate(pay(), ctx(loan=loan(payment_remaining=0))))


def test_free_cash_reserves_the_next_installment():
    c = ctx()
    assert free_cash(c) == 20_000_000 - 2_000_000 - 1_050_000 - 5_000


def test_withdrawal_within_free_cash_allowed_and_above_refused():
    c = ctx()
    assert evaluate(send(amount=free_cash(c)), c).allowed
    assert "within_free_cash" in failed(evaluate(send(amount=free_cash(c) + 1), c))


def test_withdrawal_to_unlisted_address_refused():
    assert "destination_allowlisted" in failed(evaluate(send(to="rThief"), ctx()))


def test_withdrawal_frozen_when_late_but_not_by_the_impaired_flag():
    # M2: freezes come from ledger facts only. The lender's impaired flag alone must not freeze a current borrower.
    assert evaluate(send(), ctx(loan=loan(impaired=True))).allowed
    assert "loan_current" in failed(evaluate(send(), ctx(now=1001)))
    assert "loan_current" in failed(evaluate(send(), ctx(now=1001, loan=loan(impaired=True))))


def test_no_loan_means_only_reserve_and_buffer_held_back():
    c = ctx(loan=None)
    assert free_cash(c) == 20_000_000 - 2_000_000 - 5_000
    assert evaluate(send(amount=free_cash(c)), c).allowed


def test_everything_else_refused():
    for t in ("SignerListSet", "AccountSet", "AccountDelete", "OfferCreate", "TrustSet", "DelegateSet"):
        v = evaluate({"TransactionType": t, "Account": LB, "Fee": "30"}, ctx())
        assert not v.allowed and "type" in failed(v), t
    # M2: SetRegularKey is the release path now, still refused while the loan is open
    v = evaluate({"TransactionType": "SetRegularKey", "Account": LB, "RegularKey": "rBorrower", "Fee": "30"}, ctx())
    assert not v.allowed and "loan_repaid" in failed(v)


def test_cross_currency_tricks_refused():
    assert "no_forbidden_fields" in failed(evaluate(send(SendMax="5000000"), ctx()))
    assert "no_forbidden_fields" in failed(evaluate(send(Paths=[[]]), ctx()))
    assert "no_partial_payment" in failed(evaluate(send(Flags=0x00020000), ctx()))
    assert "xrp_only" in failed(evaluate(send(amount=1) | {"Amount": {"currency": "USD", "issuer": "rI", "value": "1"}}, ctx()))


def test_wrong_account_refused():
    assert "account" in failed(evaluate(send(Account="rSomeoneElse"), ctx()))


def terms(**kw):
    base = dict(broker_id=BROKER, principal=10_000_000, interest_rate_units=8000, payment_total=6, payment_interval=120, grace_period=120,
                service_fee=50_000)
    base.update(kw)
    return AcceptedTerms(**base)


def loanset(**kw):
    tx = {"TransactionType": "LoanSet", "Account": LENDER, "Counterparty": LB, "LoanBrokerID": BROKER, "PrincipalRequested": "10000000",
          "InterestRate": 8000, "PaymentTotal": 6, "PaymentInterval": 120, "GracePeriod": 120, "LoanServiceFee": "50000", "Fee": "60"}
    tx.update(kw)
    return tx


def test_loanset_needs_accepted_matching_terms():
    # M2: the borrower signs the agreement hash and the LoanSet carries it in Data, otherwise the referee refuses
    acc = sign_acceptance(BORROWER, terms(), {})
    rules = "AB" * 32  # the broker's Data must match the referee's rulebook hash
    c = ctx(loan=None, accepted=terms(), borrower=BORROWER.address, acceptance=acc, rulebook_hash=rules, broker_data=rules)
    assert evaluate(loanset(Data=acc.agreement_hash), c).allowed
    assert "borrower_accepted_terms" in failed(evaluate(loanset(), ctx(loan=None)))
    assert "same_principal" in failed(evaluate(loanset(PrincipalRequested="20000000"), c))
    assert "rate_not_higher" in failed(evaluate(loanset(InterestRate=9000), c))
    assert "same_schedule" in failed(evaluate(loanset(PaymentTotal=3), c))
    assert "fees_not_higher" in failed(evaluate(loanset(LoanServiceFee="500000"), c))
    assert "fees_not_higher" in failed(evaluate(loanset(LatePaymentFee="1000"), c))
    assert "same_broker" in failed(evaluate(loanset(LoanBrokerID="C" * 64), c))
    assert "parties" in failed(evaluate(loanset(Counterparty="rOther"), c))


def test_loanset_refused_while_a_loan_is_active():
    assert "no_open_loan" in failed(evaluate(loanset(), ctx(accepted=terms())))


def test_recovery_only_after_default_and_only_up_to_owed():
    defaulted = loan(defaulted=True)
    c = ctx(loan=defaulted, recovery_remaining=3_000_000, balance_drops=10_000_000)
    assert evaluate(send(to=LENDER, amount=3_000_000), c).allowed
    assert "within_recovery_cap" in failed(evaluate(send(to=LENDER, amount=3_000_001), c))
    assert "recovery_after_default" in failed(evaluate(send(to=LENDER), ctx()))


def test_borrower_frozen_while_recovery_pending():
    c = ctx(loan=loan(defaulted=True), recovery_remaining=3_000_000, balance_drops=10_000_000)
    assert free_cash(c) == 0
    assert not evaluate(send(), c).allowed


def test_verdict_reason_text():
    v = evaluate(send(to="rThief"), ctx())
    assert "destination_allowlisted" in v.reason
    assert evaluate(pay(), ctx()).reason == "all checks passed"


def test_late_flag_only_after_the_due_date():
    assert "no_special_flags" in failed(evaluate(pay(Flags=262144), ctx(now=500)))
    assert evaluate(pay(Flags=262144), ctx(now=1001)).allowed
