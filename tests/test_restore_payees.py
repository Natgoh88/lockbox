"""restore_payees: after a restart the supplier list comes back only if it is exactly the published, on-ledger one."""
import json

import pytest
from xrpl.wallet import Wallet

from lockbox import covenant as C
from lockbox.simsetup import build
from lockbox.units import xrp_to_drops


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "lending-covenant-v1.json"
    sc = build(rulebook_path=str(path))
    sup = Wallet.create().address
    sc.lock()
    sc.quote()
    sc.approve_payee(sup, cap_drops=xrp_to_drops(100), invoice_ref="PO-7")
    assert sc.borrow().ok
    saved = {sup: {"cap_drops": xrp_to_drops(100), "invoice_ref": "PO-7"}}
    sc.params.approved_payees.clear()  # what a restarted process looks like: Params only lives in memory
    return sc, path, saved, sup


def test_exact_match_is_restored_while_the_loan_is_open(live):
    sc, path, saved, sup = live
    assert sc.restore_payees(saved, path) == 1
    assert sc.params.approved_payees[sup].invoice_ref == "PO-7"
    v, r = sc.agent.pay_payee(sup, xrp_to_drops(5), "PO-7")  # and the referee accepts the restored supplier
    assert v.allowed and r.ok


@pytest.mark.parametrize("change", ["cap", "invoice", "extra", "missing"])
def test_any_difference_from_the_file_is_refused(live, change):
    sc, path, saved, sup = live
    bad = {a: dict(p) for a, p in saved.items()}
    if change == "cap":
        bad[sup]["cap_drops"] += 1
    elif change == "invoice":
        bad[sup]["invoice_ref"] = "PO-8"
    elif change == "extra":
        bad[Wallet.create().address] = {"cap_drops": 1, "invoice_ref": "X"}
    else:
        bad = {}
    with pytest.raises(RuntimeError, match="not exactly"):
        sc.restore_payees(bad, path)
    assert sc.params.approved_payees == {}


def test_file_edited_to_match_a_new_list_is_refused(live):
    sc, path, saved, sup = live
    doc = json.loads(path.read_text())
    other = Wallet.create().address
    doc["approved_suppliers"][other] = {"cap_drops": 1, "invoice_ref": "X"}
    path.write_text(json.dumps(doc))
    with pytest.raises(RuntimeError, match="agreement hash"):
        sc.restore_payees(doc["approved_suppliers"], path)
    assert sc.params.approved_payees == {}


def test_file_with_other_rules_is_refused(live):
    sc, path, saved, _ = live
    doc = json.loads(path.read_text())
    doc["config"]["holdback_bps"] = 5000
    doc["fingerprint"] = C.rulebook_fingerprint(doc["config"])
    path.write_text(json.dumps(doc))
    with pytest.raises(RuntimeError, match="not the one on the ledger"):
        sc.restore_payees(saved, path)


def test_missing_or_broken_input_is_refused(live, tmp_path):
    sc, path, saved, sup = live
    with pytest.raises(RuntimeError, match="no published rulebook"):
        sc.restore_payees(saved, tmp_path / "nope.json")
    with pytest.raises(RuntimeError, match="malformed"):
        sc.restore_payees({sup: {"cap_drops": 1}}, path)
