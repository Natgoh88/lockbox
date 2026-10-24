import pytest
from xrpl.wallet import Wallet

from lockbox import health, protocol as P
from lockbox.covenant import LoanView
from lockbox.sim import SimChain
from lockbox.underwrite import Offer, Metrics
from lockbox.units import RATE_UNITS


def addr():
    return Wallet.create().address


def offer(ok=True):
    m = Metrics([], 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    return Offer(ok, "A", 80, 10_000_000 if ok else 0, 8000, 6, 120, 120, 1_700_000, 0, 50_000, [], [], m)


def test_builders_produce_expected_json():
    a, b = addr(), addr()
    d = P.as_dict(P.loan_set(a, b, "A" * 64, offer()))
    assert d["Account"] == a and d["Counterparty"] == b and d["PrincipalRequested"] == "10000000"
    assert d["InterestRate"] == 8000 and d["PaymentTotal"] == 6 and d["LoanServiceFee"] == "50000"
    assert P.as_dict(P.loan_manage(a, "B" * 64, "impair"))["Flags"] == 131072
    assert P.as_dict(P.loan_manage(a, "B" * 64, "default"))["Flags"] == 65536
    sl = P.as_dict(P.lockdown_signer_list(a, b, addr(), addr()))
    assert sl["SignerQuorum"] == 2 and len(sl["SignerEntries"]) == 3
    assert P.as_dict(P.lockdown_disable_master(a))["SetFlag"] == 4


def test_declined_offer_cannot_become_a_loan():
    with pytest.raises(ValueError):
        P.loan_set(addr(), addr(), "A" * 64, offer(False))


def test_closed_ended_vault_needs_dates():
    with pytest.raises(ValueError):
        P.vault_create(addr(), closed_ended=True)
    d = P.as_dict(P.vault_create(addr(), True, 1_800_000_000, 1_800_000_500))
    assert d["VaultKind"] == 1 and d["RedemptionDate"] - d["SubscriptionDate"] == 500


def test_created_ids():
    meta = {"AffectedNodes": [{"CreatedNode": {"LedgerEntryType": "Loan", "LedgerIndex": "AB"}},
                              {"ModifiedNode": {"LedgerEntryType": "Vault", "LedgerIndex": "CD"}}]}
    assert P.created_ids(meta) == {"Loan": "AB"}


def test_cover_math():
    b = health.BrokerView(cover_available=5_000_000, debt_total=100_000_000, cover_rate_minimum=10_000, cover_rate_liquidation=RATE_UNITS)
    assert health.required_cover(b) == 10_000_000
    assert health.cover_ratio(b) == 0.5
    assert health.first_loss_if_default(b, 50_000_000) == 5_000_000  # capped by available cover
    b2 = health.BrokerView(50_000_000, 100_000_000, 10_000, RATE_UNITS)
    assert health.first_loss_if_default(b2, 50_000_000) == 10_000_000  # capped by min rate x debt


def test_loan_status_ladder():
    def mk(**kw):
        base = dict(loan_id="x", borrower="b", periodic_payment=1, service_fee=0, next_due=100, payment_remaining=3, grace_period=50)
        base.update(kw)
        return LoanView(**base)
    assert health.loan_status(None, 0) == "none"
    assert health.loan_status(mk(), 50) == "current"
    assert health.loan_status(mk(), 120) == "late"
    assert health.loan_status(mk(), 151) == "defaultable"
    assert health.loan_status(mk(impaired=True), 120) == "impaired"
    assert health.loan_status(mk(defaulted=True), 120) == "defaulted"
    assert health.loan_status(mk(payment_remaining=0), 120) == "paid"


def test_sim_signer_quorum_and_master_key():
    c = SimChain()
    a, b, p, other = addr(), addr(), addr(), addr()
    for x in (a, b, p, other):
        c.fund(x, 20_000_000)
    r = c.submit(c.prepare(P.as_dict(P.payment(a, other, 1_000))), [a])
    assert r.ok
    c.submit(c.prepare(P.as_dict(P.lockdown_signer_list(a, b, p, addr()))), [a])
    # master still on: the owner can still move funds
    assert c.submit(c.prepare(P.as_dict(P.payment(a, other, 1_000))), [a]).ok
    assert c.submit(c.prepare(P.as_dict(P.lockdown_disable_master(a))), [a]).ok
    r = c.submit(c.prepare(P.as_dict(P.payment(a, other, 1_000))), [a])
    assert not r.ok and r.code in ("tefMASTER_DISABLED", "tefBAD_QUORUM")
    assert c.submit(c.prepare(P.as_dict(P.payment(a, other, 1_000))), [b]).code == "tefBAD_QUORUM"
    assert c.submit(c.prepare(P.as_dict(P.payment(a, other, 1_000)), 2), [b, p]).ok
    assert c.submit(c.prepare(P.as_dict(P.payment(a, other, 1_000)), 2), [p, p]).code == "tefBAD_QUORUM"


def test_sim_cannot_disable_master_without_alternative_key():
    c = SimChain()
    a = addr()
    c.fund(a, 20_000_000)
    assert c.submit(c.prepare(P.as_dict(P.lockdown_disable_master(a))), [a]).code == "tecNO_ALTERNATIVE_KEY"
