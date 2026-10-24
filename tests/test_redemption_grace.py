"""The referee refuses a LoanSet whose last due date plus the grace period runs past the vault's redemption date.
The ledger's own closed-vault check ignores the grace period, so a loan could still be in grace when depositors redeem."""
from xrpl.wallet import Wallet

from lockbox import covenant as C
from lockbox.covenant import AcceptedTerms, CovenantContext, evaluate
from lockbox.roles import Refused
from lockbox.simsetup import build

LB, LENDER, BROKER = "rLockbox", "rLender", "B" * 64
BORROWER = Wallet.create()
RULES = C.rulebook_hash({"reserve_installments": 1})


def setup(**kw):
    terms = AcceptedTerms(broker_id=BROKER, principal=10_000_000, interest_rate_units=8000, payment_total=6, payment_interval=120,
                          grace_period=120, service_fee=50_000)
    acc = C.sign_acceptance(BORROWER, terms, {})
    base = dict(lockbox=LB, lender=LENDER, loan=None, balance_drops=20_000_000, account_reserve_drops=2_000_000, now=0, accepted=terms,
                borrower=BORROWER.address, acceptance=acc, rulebook_hash=RULES, broker_data=RULES)
    base.update(kw)
    tx = {"TransactionType": "LoanSet", "Account": LENDER, "Counterparty": LB, "LoanBrokerID": BROKER, "PrincipalRequested": "10000000",
          "InterestRate": 8000, "PaymentTotal": 6, "PaymentInterval": 120, "GracePeriod": 120, "LoanServiceFee": "50000", "Fee": "60",
          "Data": acc.agreement_hash}
    return tx, CovenantContext(**base)


def failed(v):
    return {c.name for c in v.checks if not c.ok}

# setup(): now 0, 6 payments every 120 s, grace 120 s -> last due 720, grace ends 840


def test_ends_with_grace_before_redemption_allowed():
    tx, c = setup(vault_redemption=840)
    assert evaluate(tx, c).allowed


def test_grace_running_past_redemption_refused():
    tx, c = setup(vault_redemption=839)
    v = evaluate(tx, c)
    assert not v.allowed and failed(v) == {"ends_before_redemption"}
    assert "840" in v.reason and "839" in v.reason


def test_open_ended_vault_has_no_redemption_date():
    tx, c = setup(vault_redemption=None)
    assert evaluate(tx, c).allowed


def test_unreadable_vault_fails_closed():
    tx, c = setup(vault_redemption=None, vault_readable=False)
    assert "ends_before_redemption" in failed(evaluate(tx, c))


def test_only_loanset_is_checked():
    tx, c = setup(vault_redemption=1)
    tx = {"TransactionType": "Payment", "Account": "rLockbox", "Destination": "rX", "Amount": "1", "Fee": "10"}
    assert "ends_before_redemption" not in {ch.name for ch in evaluate(tx, c).checks}


def test_sim_ledger_would_take_it_but_the_referee_refuses():
    # building takes 720 s, so the loan starts 720 s after setup and its last payment is due 720 s later (1440).
    # Redemption at 300 + 1220 = 1520: the ledger wants the end 60 s before (1460, ok), with grace it is 1560 (too late).
    sc = build(closed_ended=True, subscribe_s=300, invest_s=1220)
    red = sc.state()["vault"]["redemption"]
    assert sc.b.now() + 6 * 120 <= red - 60 < sc.b.now() + 6 * 120 + 120
    sc.lock()
    sc.quote()
    try:
        sc.borrow()
        raise AssertionError("the referee co-signed a loan whose grace runs past redemption")
    except Refused as e:
        assert any(ch.name == "ends_before_redemption" and not ch.ok for ch in e.verdict.checks)


def test_sim_loan_with_room_for_grace_goes_through():
    sc = build(closed_ended=True, subscribe_s=300, invest_s=1260)
    sc.lock()
    sc.quote()
    assert sc.borrow().ok
