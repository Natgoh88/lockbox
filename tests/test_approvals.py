"""M2 chunk C: approval hygiene. The referee signs only fresh, next-in-line transactions, one at a time."""
import pytest

from lockbox import protocol as P
from lockbox.covenant import ApprovalState, CovenantContext, evaluate
from lockbox.roles import Refused
from lockbox.units import xrp_to_drops

LB, LENDER, PAYOUT = "rLockbox", "rLender", "rPayout"


def ctx(approval):
    return CovenantContext(lockbox=LB, lender=LENDER, loan=None, balance_drops=20_000_000, account_reserve_drops=2_000_000, now=0,
                           allowlist={PAYOUT}, approval=approval)


def send(**kw):
    tx = {"TransactionType": "Payment", "Account": LB, "Destination": PAYOUT, "Amount": "1000", "Fee": "30", "Sequence": 7,
          "LastLedgerSequence": 105}
    tx.update(kw)
    return tx


def failed(v):
    return {c.name for c in v.checks if not c.ok}


FRESH = ApprovalState(ledger_index=100, next_sequence=7)


def test_fresh_next_in_line_transaction_allowed():
    assert evaluate(send(), ctx(FRESH)).allowed
    assert evaluate(send(LastLedgerSequence=110), ctx(FRESH)).allowed


def test_last_ledger_too_far_ahead_refused():
    # xrpl-py autofill uses current + 20: the referee wants at most 10
    assert "last_ledger_window" in failed(evaluate(send(LastLedgerSequence=111), ctx(FRESH)))
    assert "last_ledger_window" in failed(evaluate(send(LastLedgerSequence=120), ctx(FRESH)))


def test_stale_or_missing_last_ledger_refused():
    assert "last_ledger_window" in failed(evaluate(send(LastLedgerSequence=99), ctx(FRESH)))
    tx = send()
    del tx["LastLedgerSequence"]
    assert "last_ledger_window" in failed(evaluate(tx, ctx(FRESH)))


def test_sequence_must_be_the_next_one():
    assert "sequence_is_next" in failed(evaluate(send(Sequence=8), ctx(FRESH)))
    assert "sequence_is_next" in failed(evaluate(send(Sequence=6), ctx(FRESH)))
    assert "sequence_is_next" in failed(evaluate(send(Sequence=0, TicketSequence=12), ctx(FRESH)))


def test_outstanding_approval_blocks_a_second_one():
    busy = ApprovalState(ledger_index=100, next_sequence=7, outstanding={"account": LB, "sequence": 7, "last_ledger": 104})
    assert "one_approval_at_a_time" in failed(evaluate(send(), ctx(busy)))


def test_no_ledger_clock_fails_closed():
    blind = ApprovalState(ledger_index=None, next_sequence=None)
    v = evaluate(send(), ctx(blind))
    assert not v.allowed and "last_ledger_window" in failed(v) and "sequence_is_next" in failed(v)


# ---- on the simulated ledger ----

@pytest.fixture
def earning(loaned):
    loaned.earn(xrp_to_drops(20))
    return loaned


def _withdraw_tx(sc, drops=1_000_000):
    return sc.b.prepare(P.as_dict(P.payment(sc.lockbox, sc.payout, drops)), signers_count=2)


def test_sim_ledger_index_advances_with_time(sc):
    a = sc.b.ledger_index()
    sc.b.advance(40)
    assert sc.b.ledger_index() == a + 10


def test_sim_rejects_wrong_sequence_and_expired_last_ledger(earning):
    sc = earning
    tx = _withdraw_tx(sc)
    late = dict(tx, Sequence=tx["Sequence"] + 1)
    assert sc.b.submit(late, [sc.borrower, sc.policy_key]).code == "terPRE_SEQ"
    sc.b.advance(60)
    assert sc.b.submit(tx, [sc.borrower, sc.policy_key]).code == "tefMAX_LEDGER"


def test_stale_approval_submitted_late_fails_on_the_ledger(earning):
    sc = earning
    tx = _withdraw_tx(sc)
    key = sc.policy.cosign(tx)
    sc.b.advance(60)  # 15 ledgers: past its LastLedgerSequence
    r = sc.b.submit(tx, [sc.borrower, key])
    assert not r.ok and r.code == "tefMAX_LEDGER"
    with pytest.raises(Refused) as e:  # and the referee will not sign it again
        sc.policy.cosign(tx)
    assert "last_ledger_window" in str(e.value)


def test_second_stacked_approval_refused(earning):
    sc = earning
    first = _withdraw_tx(sc)
    sc.policy.cosign(first)
    second = _withdraw_tx(sc, 2_000_000)  # same Sequence: whichever lands first wins, the referee will not hand out both
    with pytest.raises(Refused) as e:
        sc.policy.cosign(second)
    assert "one_approval_at_a_time" in str(e.value)


def test_approval_allowed_again_after_the_first_validates(earning):
    sc = earning
    first = _withdraw_tx(sc)
    assert sc.b.submit(first, [sc.borrower, sc.policy.cosign(first)]).ok
    second = _withdraw_tx(sc)
    assert sc.policy.cosign(second)


def test_approval_allowed_again_after_the_first_expires(earning):
    sc = earning
    sc.policy.cosign(_withdraw_tx(sc))
    sc.b.advance(60)
    assert sc.policy.cosign(_withdraw_tx(sc))


def test_autofill_window_of_twenty_is_refused(earning):
    sc = earning
    tx = _withdraw_tx(sc)
    tx["LastLedgerSequence"] = sc.b.ledger_index() + 20
    with pytest.raises(Refused):
        sc.policy.cosign(tx)


def test_state_shows_the_outstanding_approval(earning):
    sc = earning
    tx = _withdraw_tx(sc)
    sc.policy.cosign(tx)
    st = sc.state()
    assert st["ledger_index"] == sc.b.ledger_index()
    assert st["approval"]["sequence"] == tx["Sequence"]
