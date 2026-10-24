"""verify: the rulebook file must match the ledger, and every logged referee decision must replay the same."""
import copy
import json
import pathlib
import subprocess
import sys

import pytest
from xrpl.wallet import Wallet

from lockbox import covenant as C
from lockbox import events, verify
from lockbox.simsetup import build
from lockbox.units import xrp_to_drops

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def run(tmp_path):
    path = tmp_path / "lending-covenant-v1.json"
    sc = build(rulebook_path=str(path))
    sup = Wallet.create().address
    sc.lock()
    sc.quote()
    sc.approve_payee(sup, cap_drops=xrp_to_drops(1000), invoice_ref="PO-1")
    assert sc.borrow().ok
    sc.agent.withdraw(sc.state()["buckets"]["unspent_principal"])  # refused
    sc.agent.pay_payee(sup, xrp_to_drops(5), "PO-1")  # allowed
    sc.run(700, step=30)  # installment, late, default, recovery
    decisions = [e for e in events.recent(10_000) if e["kind"] == "verdict"]
    return sc, json.loads(path.read_text()), decisions


def _replay(sc, doc, decisions):
    return verify.replay(decisions, sc.b.account_tx(sc.lockbox), sc.b.balance(sc.lockbox), sc.lockbox, doc,
                         lambda b: sc.b.broker_view(b).data)


def _loanset_data(sc):
    return next(e["tx"]["Data"] for e in sc.b.account_tx(sc.lockbox) if e["tx"]["TransactionType"] == "LoanSet")


def test_honest_run_verifies(run):
    sc, doc, decisions = run
    assert verify.rulebook_problems(doc, sc.b.broker_view(sc.broker_id).data, _loanset_data(sc)) == []
    rows = _replay(sc, doc, decisions)
    assert len(rows) >= 5 and all(r.matches for r in rows), verify.table(rows)
    assert {r.logged for r in rows} == {"allowed", "refused"}
    assert "within_free_cash" in {r.rule for r in rows}


def test_every_verdict_event_carries_a_decision_record(run):
    _, _, decisions = run
    assert all("decision" in e and e["rule"] for e in decisions)
    json.dumps(decisions)  # plain JSON, so it can go to the events file on Devnet


def test_flipped_verdict_is_caught(run):
    sc, doc, decisions = run
    bad = copy.deepcopy(decisions)
    i = next(i for i, e in enumerate(bad) if not e["allowed"])
    bad[i]["allowed"] = True
    rows = _replay(sc, doc, bad)
    assert not rows[i].matches and "referee logged allowed" in rows[i].problems[0]
    assert all(r.matches for j, r in enumerate(rows) if j != i)


def test_changed_deciding_rule_is_caught(run):
    sc, doc, decisions = run
    bad = copy.deepcopy(decisions)
    bad[0]["rule"] = "something_else"
    assert not _replay(sc, doc, bad)[0].matches


def test_doctored_decision_inputs_are_caught(run):
    sc, doc, decisions = run
    bad = copy.deepcopy(decisions)
    i = next(i for i, e in enumerate(bad) if e["decision"]["tx"]["TransactionType"] == "LoanPay")
    bad[i]["decision"]["balance"] += 1_000_000  # claims the account held more than the ledger says
    assert not _replay(sc, doc, bad)[i].matches


def test_tampered_rulebook_file_is_caught(run):
    sc, doc, _ = run
    broker = sc.b.broker_view(sc.broker_id).data
    loose = copy.deepcopy(doc)
    loose["config"]["holdback_bps"] = 5000  # edited without fixing the fingerprint
    assert any("fingerprint does not match the config" in p for p in verify.rulebook_problems(loose, broker))
    loose["fingerprint"] = C.rulebook_fingerprint(loose["config"])  # a careful forger recomputes it
    assert any("not the one on the ledger" in p for p in verify.rulebook_problems(loose, broker))
    extra = copy.deepcopy(doc)
    extra["approved_suppliers"][Wallet.create().address] = {"cap_drops": 1, "invoice_ref": "X"}
    assert verify.rulebook_problems(extra, broker, _loanset_data(sc))  # agreement hash no longer recomputes


def test_supplier_missing_from_the_file_is_caught(run):
    sc, doc, decisions = run
    thin = copy.deepcopy(doc)
    thin["approved_suppliers"] = {}
    assert any("not in the rulebook file" in p for r in _replay(sc, thin, decisions) for p in r.problems)


@pytest.mark.parametrize("flag,code", [(None, 0), ("--tamper-verdict", 1), ("--tamper-rulebook", 1)])
def test_script_exit_codes(flag, code):
    args = [sys.executable, str(ROOT / "scripts" / "verify.py")] + ([flag] if flag else [])
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    assert r.returncode == code, r.stdout + r.stderr
    assert ("OK:" if code == 0 else "FAILED") in r.stdout


def test_roles_come_from_the_published_file_even_after_a_restart(run):
    sc, doc, decisions = run
    sc.params.approved_payees.clear()  # a restarted Devnet process has no suppliers in memory
    labels = verify.role_labels(sc, doc)
    sup = next(iter(doc["approved_suppliers"]))
    assert labels[sup] == "supplier" and labels[sc.payout] == "owner wallet" and labels[sc.servicer.address] == "lender"
    rows = _replay(sc, doc, decisions)
    verify.label_rows(rows, decisions, labels)
    texts = " | ".join(r.tx for r in rows)
    assert "to supplier" in texts and "to owner wallet" in texts
