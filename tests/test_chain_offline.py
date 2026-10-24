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


class FakeClient:
    """Answers the two requests the referee's clock needs with the shapes Devnet returns."""
    def __init__(self, ledger=6385720, seq=6385567):
        self.ledger, self.seq, self.asked = ledger, seq, []

    def request(self, req):
        self.asked.append(req)
        name = type(req).__name__
        res = {"LedgerCurrent": {"ledger_current_index": self.ledger, "status": "success"},
               "AccountInfo": {"account_data": {"Sequence": self.seq, "Balance": "1"}, "ledger_current_index": self.ledger}}[name]
        return type("Response", (), {"result": res})()


def test_ledger_clock_and_next_sequence_for_the_referee():
    c = XrplChain("http://127.0.0.1:1")
    c.c = FakeClient()
    assert c.ledger_index() == 6385720
    assert c.next_sequence("rAnyone") == 6385567
    assert c.c.asked[-1].ledger_index == "current"  # queued transactions count


def test_prepare_keeps_last_ledger_inside_the_referee_window(monkeypatch):
    import lockbox.chain as chain
    from lockbox.covenant import APPROVAL_WINDOW
    monkeypatch.setattr(chain, "autofill", lambda model, client, n: model)  # no network: keep what prepare set
    c = XrplChain("http://127.0.0.1:1")
    c.c = FakeClient()
    w, dest = Wallet.create(), Wallet.create()
    tx = {**P.as_dict(P.payment(w.address, dest.address, 1000)), "Fee": "12", "Sequence": 5}
    assert c.prepare(tx)["LastLedgerSequence"] == 6385720 + APPROVAL_WINDOW
    assert c.prepare({**tx, "LastLedgerSequence": 6385722})["LastLedgerSequence"] == 6385722  # a caller's choice is kept


def test_periodic_payment_rounds_up_like_rippled():
    # the first Devnet loan: truncating gave 2146360 and LoanPay failed with tecINSUFFICIENT_PAYMENT
    from lockbox.chain import _num_up
    assert _num_up("2096360.733564440452") == 2096361 and _num_up("2096360") == 2096360 and _num_up(None) == 0
    c = XrplChain("http://127.0.0.1:1")
    c._entry = lambda index: {"PeriodicPayment": "2096360.733564440452", "LoanServiceFee": "50000", "NextPaymentDueDate": 846184981,
                              "PaymentRemaining": 6, "TotalValueOutstanding": "12578165"}
    assert c.loan_view("L").installment == 2146361


def test_broker_view_returns_the_data_field():
    # the referee refuses a LoanSet unless the broker's Data holds its rulebook hash
    c = XrplChain("http://127.0.0.1:1")
    c._entry = lambda index: {"CoverAvailable": "15000000", "DebtTotal": "0", "CoverRateMinimum": 25000, "CoverRateLiquidation": 100000,
                              "Data": "66FC381D1B04C6813F437B392C708EFE8EFABA9EB33963958B0FC75E546055EB"}
    assert c.broker_view("B").data == "66FC381D1B04C6813F437B392C708EFE8EFABA9EB33963958B0FC75E546055EB"
    c._entry = lambda index: {}
    assert c.broker_view("B").data == ""


def test_regular_key_signs_alone_for_the_account():
    # release step 2: the borrower's registered key, now the lockbox's regular key, removes the signer list
    from xrpl.core.binarycodec import decode, encode_for_signing
    from xrpl.core.keypairs import is_valid_message
    lockbox, regular = Wallet.create(), Wallet.create()
    c = Capture()
    tx = base(P.as_dict(P.remove_signer_list(lockbox.address)))
    assert c.submit(tx, [regular]).ok
    signed = c.sent[0]
    assert signed.account == lockbox.address
    assert signed.signing_pub_key == regular.public_key and signed.txn_signature and not signed.signers
    blob = signed.to_xrpl()
    assert decode(encode(blob))["SigningPubKey"] == regular.public_key
    assert is_valid_message(bytes.fromhex(encode_for_signing(blob)), bytes.fromhex(signed.txn_signature), regular.public_key)


def test_vault_view_reads_the_closed_ended_dates():
    from lockbox.units import ripple_to_unix

    class VaultClient:
        def request(self, req):
            return type("R", (), {"result": {"vault": {"AssetsTotal": "50000017", "AssetsAvailable": "39255331", "VaultKind": 1,
                                                       "SubscriptionDate": 846191051, "RedemptionDate": 846195551}}})()
    c = XrplChain("http://127.0.0.1:1")
    c.c = VaultClient()
    v = c.vault_view("V")
    assert v.closed_ended and v.subscription == ripple_to_unix(846191051) and v.redemption == ripple_to_unix(846195551)
    assert v.redemption - v.subscription == 4500
