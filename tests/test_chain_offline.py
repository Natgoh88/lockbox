"""Signing paths of the real backend, checked without a network by capturing what would be submitted."""
from xrpl.core.binarycodec import encode
from xrpl.wallet import Wallet

from lockbox import protocol as P
from lockbox.chain import XrplChain, _num
from lockbox.backend import Result
from lockbox.underwrite import Metrics, Offer


class Capture(XrplChain):
    def __init__(self):
        super().__init__("http://127.0.0.1:1")
        self.sent = []

    def _finish(self, signed, tx):
        self.sent.append(signed)
        return Result(True, "tesSUCCESS", tx)


def offer():
    m = Metrics([], 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    return Offer(True, "A", 80, 10_000_000, 8000, 6, 120, 120, 1_700_000, 0, 50_000, [], [], m)


def base(d):
    return {**d, "Fee": "60", "Sequence": 5, "LastLedgerSequence": 1000}


def test_number_parsing():
    assert _num("1124125") == 1124125 and _num("1.124125e6") == 1124125 and _num(None) == 0 and _num("x", 7) == 7


def test_loanset_first_party_single_sig_counterparty_multisig():
    lender, borrower, policy, lockbox = (Wallet.create() for _ in range(4))
    c = Capture()
    tx = base(P.as_dict(P.loan_set(lender.address, lockbox.address, "A" * 64, offer())))
    r = c.submit(tx, [lender], counterparty_signers=[borrower, policy])
    assert r.ok
    signed = c.sent[0]
    assert signed.txn_signature and signed.signing_pub_key == lender.public_key
    signers = signed.counterparty_signature.signers
    assert {s.account for s in signers} == {borrower.address, policy.address}
    order = [s.account for s in signers]
    assert order == sorted(order, key=lambda a: __import__("xrpl").core.addresscodec.decode_classic_address(a))
    assert encode(signed.to_xrpl())  # serializes


def test_payment_from_a_multisig_account_is_multisigned_by_two():
    borrower, policy, lockbox, dest = (Wallet.create() for _ in range(4))
    c = Capture()
    tx = base(P.as_dict(P.payment(lockbox.address, dest.address, 1000)))
    tx["Fee"] = "30"
    assert c.submit(tx, [borrower, policy]).ok
    signed = c.sent[0]
    assert signed.signing_pub_key == "" and len(signed.signers) == 2
    assert encode(signed.to_xrpl())


def test_single_key_account_uses_a_plain_signature():
    w, dest = Wallet.create(), Wallet.create()
    c = Capture()
    tx = base(P.as_dict(P.payment(w.address, dest.address, 1000)))
    tx["Fee"] = "12"
    assert c.submit(tx, [w]).ok
    assert c.sent[0].txn_signature and not c.sent[0].signers
