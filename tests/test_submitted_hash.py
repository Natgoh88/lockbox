"""Every referee-signed transaction logs its ledger hash, so a decision can link to the explorer."""
from lockbox import events


def test_each_signed_decision_has_a_submitted_hash(loaned):
    loaned.run(100, step=10)  # one installment
    evs = events.recent(10_000)
    signed = [e for e in evs if e["kind"] == "verdict" and e["allowed"]]
    sub = {(e["account"], e["sequence"]): e for e in evs if e["kind"] == "submitted"}
    assert len(signed) >= 2
    for v in signed:
        tx = v["decision"]["tx"]
        s = sub[(tx["Account"], tx["Sequence"])]
        assert s["hash"] and s["ok"]
        assert any(e["hash"] == s["hash"] for e in loaned.b.account_tx(loaned.lockbox))
