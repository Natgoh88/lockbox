"""M3: graduation. The limit grows with loans this lockbox repaid on the ledger."""
from lockbox.covenant import LoanView
from lockbox.inflows import Inflow, repaid_loans
from lockbox.underwrite import Policy, underwrite
from lockbox.units import xrp_to_drops

NOW, IV = 1_800_000_000, 120
P0 = Policy(payment_interval=IV, lookback_buckets=12)


def flows(per=10_000_000):
    return [Inflow(NOW - (6 - k) * IV + 5 + i, per, s) for k in range(6) for i, s in enumerate("abc")]


def test_no_history_no_graduation():
    o = underwrite(flows(), NOW, P0)
    assert o.graduation == {"repaid_loans": 0, "multiplier": 1.0, "max_multiplier": 2.0, "min_coverage": 1.25}


def test_each_repaid_loan_raises_the_limit():
    base = underwrite(flows(), NOW, P0).principal_drops
    two = underwrite(flows(), NOW, P0, repaid_loans=2)
    assert two.graduation["multiplier"] == 1.5
    assert abs(two.principal_drops - 1.5 * base) <= 2
    assert any("graduation" in r and "2 loan" in r for r in two.reasons)


def test_graduation_is_capped_and_never_drops_coverage_below_the_floor():
    many = underwrite(flows(), NOW, P0, repaid_loans=10)
    assert many.graduation["multiplier"] == 2.0
    assert many.installment_drops <= many.metrics.conservative / 1.25 + 2


def test_graduation_respects_max_principal_and_declines():
    p = Policy(payment_interval=IV, lookback_buckets=12, max_principal_drops=10_000_000)
    assert underwrite(flows(), NOW, p, repaid_loans=3).principal_drops == 10_000_000
    declined = underwrite([], NOW, P0, repaid_loans=5)
    assert not declined.approved and declined.principal_drops == 0


# ---- counting repaid loans from the ledger ----

LB = "rLockbox"


def _loanset(loan_id, cp=LB, code="tesSUCCESS"):
    return {"tx": {"TransactionType": "LoanSet", "Account": "rLender", "Counterparty": cp},
            "meta": {"TransactionResult": code, "AffectedNodes": [{"CreatedNode": {"LedgerEntryType": "Loan", "LedgerIndex": loan_id}}]},
            "validated": True}


def _view(remaining, defaulted=False):
    return LoanView(loan_id="x", borrower=LB, periodic_payment=1, service_fee=0, next_due=0, payment_remaining=remaining, defaulted=defaulted)


def test_repaid_loans_counts_only_fully_paid_loans_of_this_lockbox():
    hist = [_loanset("A"), _loanset("B"), _loanset("C"), _loanset("D"), _loanset("E", cp="rOther"), _loanset("F", code="tecKILLED")]
    views = {"A": _view(0), "B": _view(2), "C": _view(3, defaulted=True), "D": None, "E": _view(0), "F": _view(0)}
    assert repaid_loans(hist, LB, views.get) == 1  # only A: B open, C defaulted, D unreadable, E not ours, F failed


# ---- on the simulated ledger ----

def test_sim_second_loan_is_bigger_after_the_first_is_repaid(loaned):
    first = loaned.offer
    assert first.graduation["repaid_loans"] == 0
    loaned.run(6 * 120 + 60, step=30, revenue_per_step=xrp_to_drops(10))
    assert loaned.state()["status"] == "paid"
    o = loaned.quote()
    assert o.graduation["repaid_loans"] == 1 and o.graduation["multiplier"] == 1.25
    assert loaned.state()["offer"]["graduation"]["repaid_loans"] == 1
