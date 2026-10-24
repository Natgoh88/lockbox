"""PolicyService.review(dry_run=True): the same verdict, but never logged as a decision and never signed."""
from lockbox import covenant as C
from lockbox import events, verify
from lockbox import protocol as P


def _withdraw_tx(sc, drops):
    return sc.b.prepare(P.as_dict(P.payment(sc.lockbox, sc.payout, drops)), signers_count=2)


def _kinds():
    return [e["kind"] for e in events.recent(10_000)]


def test_dry_run_gives_the_same_verdict_as_a_real_review(loaned):
    for drops in (10**12, 1_000):
        tx = _withdraw_tx(loaned, drops)
        dry = loaned.policy.review(tx, dry_run=True)
        real = loaned.policy.review(tx)
        assert dry.allowed == real.allowed and dry.reason == real.reason


def test_dry_run_is_not_a_verdict_event(loaned):
    before = _kinds().count("verdict")
    loaned.policy.review(_withdraw_tx(loaned, 10**12), dry_run=True)
    assert _kinds().count("verdict") == before
    ev = [e for e in events.recent(10_000) if e["kind"] == "dry_run"][-1]
    assert not ev["allowed"] and ev["rule"] == "within_free_cash" and "decision" not in ev
    assert ev["rulebook"] == "lending-covenant"


def test_dry_run_signs_nothing_and_leaves_no_outstanding_approval(loaned):
    loaned.policy.outstanding = None
    loaned.policy.last_verdict = None
    loaned.policy.review(_withdraw_tx(loaned, 1_000), dry_run=True)
    assert loaned.policy.outstanding is None and loaned.policy.last_verdict is None


def test_verify_never_replays_dry_runs(loaned):
    for _ in range(3):
        loaned.policy.review(_withdraw_tx(loaned, 10**12), dry_run=True)
    decisions = [e for e in events.recent(10_000) if e["kind"] == "verdict" and "decision" in e]
    doc = C.rulebook_document(
        loaned.params.rulebook(), loaned.params.approved_payees, loaned.policy.accepted)
    rows = verify.replay(decisions, loaned.b.account_tx(loaned.lockbox), loaned.b.balance(loaned.lockbox), loaned.lockbox, doc,
                         lambda b: loaned.b.broker_view(b).data)
    assert len(rows) == 1 and rows[0].tx.startswith("LoanSet") and rows[0].matches
