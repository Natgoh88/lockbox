"""The approved-supplier list is frozen at loan creation: it is part of the agreement hash in the LoanSet Data field."""
import pytest
from xrpl.wallet import Wallet

from lockbox import covenant as C
from lockbox.units import xrp_to_drops


def test_new_supplier_refused_while_a_loan_is_open(loaned):
    with pytest.raises(RuntimeError, match="frozen"):
        loaned.approve_payee(Wallet.create().address, cap_drops=xrp_to_drops(10), invoice_ref="PO-2")
    assert loaned.state()["approved_payees"] == {}


def test_changing_an_existing_supplier_refused_while_a_loan_is_open(stocked):
    with pytest.raises(RuntimeError, match="frozen"):
        stocked.approve_payee(stocked.supplier, cap_drops=xrp_to_drops(5000), invoice_ref="PO-1")
    assert stocked.state()["approved_payees"][stocked.supplier]["cap_drops"] == xrp_to_drops(1000)


def test_suppliers_approved_before_the_loan_are_in_the_agreement_hash(stocked):
    assert stocked.supplier in stocked.state()["approved_payees"]
    assert stocked.state()["agreement_hash"] == C.agreement_hash(stocked.policy.accepted, stocked.params.approved_payees)


def test_suppliers_can_be_approved_again_once_the_loan_is_repaid(loaned):
    loaned.run(6 * 120 + 60, step=30, revenue_per_step=xrp_to_drops(10))
    assert loaned.state()["status"] == "paid"
    loaned.approve_payee(Wallet.create().address, cap_drops=xrp_to_drops(10), invoice_ref="PO-3")
    assert len(loaned.state()["approved_payees"]) == 1
