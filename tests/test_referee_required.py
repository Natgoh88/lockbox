"""M1: the referee is required on every payment out of the lockbox.

Weights: referee 2, borrower 1, lender 1, quorum 3. Any working pair includes the referee, so the
borrower and the lender together (say a broker colluding with a fake borrower) cannot move money.
"""
import pytest

from lockbox import protocol as P
from lockbox.backend import addr_of


@pytest.fixture
def locked(sc):
    sc.lock()
    return sc


def _pay(sc, signers):
    tx = sc.b.prepare(P.as_dict(P.payment(sc.lockbox, sc.payout, 1_000_000)), signers_count=len(signers))
    return sc.b.submit(tx, signers)


def test_signer_list_weights_and_quorum():
    d = P.as_dict(P.lockdown_signer_list("rLockbox", "rBorrower", "rLender", "rReferee"))
    weights = {e["SignerEntry"]["Account"]: e["SignerEntry"]["SignerWeight"] for e in d["SignerEntries"]}
    assert d["SignerQuorum"] == 3
    assert weights == {"rBorrower": 1, "rLender": 1, "rReferee": 2}


def test_borrower_plus_lender_refused(locked):
    r = _pay(locked, [locked.borrower, locked.lender])
    assert not r.ok and r.code == "tefBAD_QUORUM"


def test_referee_plus_borrower_allowed(locked):
    assert _pay(locked, [locked.policy_key, locked.borrower]).ok


def test_referee_plus_lender_allowed(locked):
    assert _pay(locked, [locked.policy_key, locked.lender]).ok


def test_referee_alone_refused(locked):
    r = _pay(locked, [locked.policy_key])
    assert not r.ok and r.code == "tefBAD_QUORUM"


def test_each_single_party_refused(locked):
    for k in (locked.borrower, locked.lender, locked.lockbox_key):
        assert not _pay(locked, [k]).ok


def test_loanset_counterparty_needs_the_referee(locked):
    sc = locked
    sc.quote()
    s = sc.servicer
    tx = sc.b.prepare(P.as_dict(P.loan_set(s.address, sc.lockbox, s.broker_id, sc.offer)), signers_count=2)
    r = sc.b.submit(tx, [sc.lender], counterparty_signers=[sc.borrower, sc.lender])
    assert not r.ok and r.code == "tefBAD_QUORUM"
    assert sc.borrow().ok  # the normal path: borrower + referee sign as counterparty


def test_verify_lockbox_accepts_the_weighted_list(locked):
    assert all(ok for _, ok in locked.servicer.verify_lockbox())


def _lock_with(sc, entries: dict, quorum: int):
    from xrpl.models.transactions import SignerListSet
    from xrpl.models.transactions.signer_list_set import SignerEntry
    tx = SignerListSet(account=sc.lockbox, signer_quorum=quorum,
                       signer_entries=[SignerEntry(account=a, signer_weight=w) for a, w in entries.items()])
    assert sc.b.submit(sc.b.prepare(P.as_dict(tx)), [sc.lockbox_key]).ok
    assert sc.b.submit(sc.b.prepare(P.as_dict(P.lockdown_disable_master(sc.lockbox))), [sc.lockbox_key]).ok


def _failed(sc):
    return [n for n, ok in sc.servicer.verify_lockbox() if not ok]


def test_verify_lockbox_rejects_old_flat_two_of_three(sc):
    b, l, p = addr_of(sc.borrower), addr_of(sc.lender), addr_of(sc.policy_key)
    _lock_with(sc, {b: 1, l: 1, p: 1}, 2)
    failed = _failed(sc)
    assert failed and any("quorum" in n for n in failed) and any("weight" in n for n in failed)
    sc.quote()
    with pytest.raises(RuntimeError):
        sc.borrow()


def test_verify_lockbox_rejects_borrower_heavy_weights(sc):
    # right quorum and the right three signers, but the borrower holds the heavy key
    b, l, p = addr_of(sc.borrower), addr_of(sc.lender), addr_of(sc.policy_key)
    _lock_with(sc, {b: 2, l: 1, p: 1}, 3)
    failed = _failed(sc)
    assert any("weight" in n for n in failed)
    assert any("without the referee" in n for n in failed)
