"""Rulebook identity: name lending-covenant, version 1, fingerprint = sha256 of name + version + config.
The fingerprint goes in the broker Data field; rulebooks/lending-covenant-v1.json holds the config and approved suppliers."""
import hashlib
import json
import pathlib

from xrpl.wallet import Wallet

from lockbox import covenant as C
from lockbox import events
from lockbox.roles import Params
from lockbox.simsetup import build
from lockbox.units import xrp_to_drops

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_fingerprint_is_sha256_of_name_version_and_config():
    cfg = Params().rulebook()
    want = hashlib.sha256(json.dumps({"name": "lending-covenant", "version": 1, "config": cfg}, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest().upper()
    assert C.rulebook_fingerprint(cfg) == want
    assert C.rulebook_identity(cfg) == {"name": "lending-covenant", "version": 1, "fingerprint": want}


def test_fingerprint_changes_with_any_rule():
    a = Params().rulebook()
    assert C.rulebook_fingerprint(a) != C.rulebook_fingerprint({**a, "holdback_bps": 1})


def test_broker_data_holds_the_fingerprint(sc):
    fp = C.rulebook_fingerprint(sc.params.rulebook())
    assert sc.b.broker_view(sc.broker_id).data == fp
    st = sc.state()
    assert st["rulebook"] == {"name": "lending-covenant", "version": 1, "fingerprint": fp, "file": None}
    assert st["rulebook_hash"] == fp


def test_every_verdict_event_names_the_rulebook(loaned):
    loaned.agent.withdraw(10**12)
    verdicts = [e for e in events.recent() if e["kind"] == "verdict"]
    assert verdicts
    fp = C.rulebook_fingerprint(loaned.params.rulebook())
    for e in verdicts:
        assert e["rulebook"] == "lending-covenant" and e["rulebook_version"] == 1 and e["rulebook_fingerprint"] == fp


def test_rulebook_file_matches_the_broker_and_the_loan(tmp_path):
    sc = build(rulebook_path=str(tmp_path / "lending-covenant-v1.json"))
    supplier = Wallet.create().address
    sc.lock()
    sc.quote()
    sc.approve_payee(supplier, cap_drops=xrp_to_drops(100), invoice_ref="PO-7")
    assert sc.borrow().ok
    doc = json.loads((tmp_path / "lending-covenant-v1.json").read_text())
    assert doc["name"] == "lending-covenant" and doc["version"] == 1
    assert doc["approved_suppliers"] == {supplier: {"cap_drops": xrp_to_drops(100), "invoice_ref": "PO-7"}}
    assert C.rulebook_document_problems(doc) == []
    assert doc["fingerprint"] == sc.b.broker_view(sc.broker_id).data
    loanset = next(e["tx"] for e in sc.b.account_tx(sc.lockbox) if e["tx"]["TransactionType"] == "LoanSet")
    assert doc["agreement_hash"] == loanset["Data"]
    assert sc.state()["rulebook"]["file"].endswith("lending-covenant-v1.json")


def test_tampered_rulebook_document_is_caught():
    doc = C.rulebook_document(Params().rulebook(), {})
    doc["config"]["holdback_bps"] = 5000
    assert C.rulebook_document_problems(doc)


def test_committed_rulebook_file_matches_the_default_rules():
    doc = json.loads((ROOT / "rulebooks" / "lending-covenant-v1.json").read_text())
    assert C.rulebook_document_problems(doc) == []
    assert doc["fingerprint"] == C.rulebook_fingerprint(Params().rulebook())
