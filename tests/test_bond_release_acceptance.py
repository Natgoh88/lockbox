"""M2 chunk D: bond, release after repayment, signed acceptance, rulebook and agreement hashes."""
import pytest
from xrpl.wallet import Wallet

from lockbox import buckets as B
from lockbox import covenant as C
from lockbox import protocol as P
from lockbox.buckets import Payee
from lockbox.covenant import AcceptedTerms, CovenantContext, LoanView, evaluate
from lockbox.roles import Refused
from lockbox.units import xrp_to_drops

LB, LENDER, PAYOUT, SUPPLIER = "rLockbox", "rLender", "rPayout", "rSupplier"
BROKER = "B" * 64
BORROWER = Wallet.create()
STRANGER = Wallet.create()
PAYEES = {SUPPLIER: Payee(cap_drops=40_000_000, invoice_ref="INV-7")}


def terms(**kw):
    base = dict(broker_id=BROKER, principal=10_000_000, interest_rate_units=8000, payment_total=6, payment_interval=120, grace_period=120,
                service_fee=50_000)
    base.update(kw)
    return AcceptedTerms(**base)


def loanset(data, **kw):
    tx = {"TransactionType": "LoanSet", "Account": LENDER, "Counterparty": LB, "LoanBrokerID": BROKER, "PrincipalRequested": "10000000",
          "InterestRate": 8000, "PaymentTotal": 6, "PaymentInterval": 120, "GracePeriod": 120, "LoanServiceFee": "50000", "Fee": "60",
          "Data": data}
    tx.update(kw)
    return tx


def ctx(**kw):
    base = dict(lockbox=LB, lender=LENDER, loan=None, balance_drops=20_000_000, account_reserve_drops=2_000_000, now=500, allowlist={PAYOUT},
                accepted=terms(), approved_payees=dict(PAYEES), borrower=BORROWER.address, release_key=BORROWER.address)
    base.update(kw)
    return CovenantContext(**base)


def failed(v):
    return {c.name for c in v.checks if not c.ok}


# ---- hashes ----

def test_agreement_hash_is_stable_and_covers_terms_and_payees():
    h = C.agreement_hash(terms(), PAYEES)
    assert h == C.agreement_hash(terms(), dict(PAYEES)) and len(h) == 64
    assert h != C.agreement_hash(terms(principal=10_000_001), PAYEES)
    assert h != C.agreement_hash(terms(), {SUPPLIER: Payee(cap_drops=40_000_001, invoice_ref="INV-7")})
    assert h != C.agreement_hash(terms(), {})


def test_rulebook_hash_changes_with_any_rule():
    a = C.rulebook_hash({"reserve_installments": 1, "holdback_bps": 0})
    assert a == C.rulebook_hash({"holdback_bps": 0, "reserve_installments": 1})  # key order does not matter
    assert a != C.rulebook_hash({"reserve_installments": 2, "holdback_bps": 0})


# ---- signed acceptance ----

def test_loanset_with_signed_acceptance_and_hash_in_data_allowed():
    acc = C.sign_acceptance(BORROWER, terms(), PAYEES)
    rules = C.rulebook_hash({})
    v = evaluate(loanset(acc.agreement_hash), ctx(acceptance=acc, rulebook_hash=rules, broker_data=rules))
    assert v.allowed, v.reason


def test_loanset_refused_without_acceptance_signature():
    h = C.agreement_hash(terms(), PAYEES)
    assert "borrower_signed_terms" in failed(evaluate(loanset(h), ctx()))


def test_loanset_refused_with_a_bad_acceptance_signature():
    acc = C.sign_acceptance(STRANGER, terms(), PAYEES)  # signed, but not by the borrower
    assert "borrower_signed_terms" in failed(evaluate(loanset(acc.agreement_hash), ctx(acceptance=acc)))
    good = C.sign_acceptance(BORROWER, terms(), PAYEES)
    forged = C.Acceptance(good.agreement_hash, good.public_key, good.signature[:-2] + ("00" if good.signature[-2:] != "00" else "11"))
    assert "borrower_signed_terms" in failed(evaluate(loanset(good.agreement_hash), ctx(acceptance=forged)))


def test_loanset_refused_when_the_borrower_signed_different_terms():
    acc = C.sign_acceptance(BORROWER, terms(principal=5_000_000), PAYEES)
    v = evaluate(loanset(C.agreement_hash(terms(), PAYEES)), ctx(acceptance=acc))
    assert "borrower_signed_terms" in failed(v)


def test_loanset_refused_when_payees_changed_after_acceptance():
    acc = C.sign_acceptance(BORROWER, terms(), PAYEES)
    more = dict(PAYEES, rOther=Payee(cap_drops=1, invoice_ref="X"))
    assert "borrower_signed_terms" in failed(evaluate(loanset(acc.agreement_hash), ctx(acceptance=acc, approved_payees=more)))


def test_loanset_refused_without_the_agreement_hash_in_data():
    acc = C.sign_acceptance(BORROWER, terms(), PAYEES)
    assert "agreement_hash_in_data" in failed(evaluate(loanset("00" * 32), ctx(acceptance=acc)))
    tx = loanset(acc.agreement_hash)
    del tx["Data"]
    assert "agreement_hash_in_data" in failed(evaluate(tx, ctx(acceptance=acc)))


# ---- release ----

def loan(**kw):
    base = dict(loan_id="L" * 64, borrower=LB, periodic_payment=1_000_000, service_fee=50_000, next_due=1000, payment_remaining=4,
                grace_period=100, total_owed=4_200_000)
    base.update(kw)
    return LoanView(**base)


def regkey(key=None):
    return {"TransactionType": "SetRegularKey", "Account": LB, "RegularKey": key or BORROWER.address, "Fee": "30"}


def test_release_refused_while_a_loan_is_open():
    v = evaluate(regkey(), ctx(loan=loan()))
    assert not v.allowed and "loan_repaid" in failed(v)


def test_release_allowed_after_full_repayment():
    assert evaluate(regkey(), ctx(loan=loan(payment_remaining=0, total_owed=0))).allowed


def test_release_refused_after_default_or_while_recovery_is_owed():
    assert "loan_repaid" in failed(evaluate(regkey(), ctx(loan=loan(defaulted=True, total_owed=0))))
    assert "nothing_owed" in failed(evaluate(regkey(), ctx(loan=loan(payment_remaining=0, total_owed=0), recovery_remaining=5)))


def test_release_only_to_the_registered_key():
    v = evaluate(regkey(STRANGER.address), ctx(loan=loan(payment_remaining=0, total_owed=0)))
    assert "release_to_registered_key" in failed(v)
    assert "release_to_registered_key" in failed(evaluate(regkey(), ctx(release_key="")))


# ---- bond in the buckets ----

def _bond(drops):
    tx = {"TransactionType": "Payment", "Account": "rBorrowerWallet", "Destination": LB, "Amount": str(drops), "Fee": "10",
          "Memos": [{"Memo": {"MemoData": b"bond".hex().upper()}}]}
    return {"tx": tx, "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": str(drops)}, "validated": True}


def _ls(principal):
    return {"tx": {"TransactionType": "LoanSet", "Account": LENDER, "Counterparty": LB, "PrincipalRequested": str(principal), "Fee": "60"},
            "meta": {"TransactionResult": "tesSUCCESS"}, "validated": True}


def _pay_lender(drops):
    tx = {"TransactionType": "Payment", "Account": LB, "Destination": LENDER, "Amount": str(drops), "Fee": "30"}
    return {"tx": tx, "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": str(drops)}, "validated": True}


def _replay(hist, facts, start=12_000_000):
    bal = start + sum(B.balance_delta(e, LB) for e in hist)
    return B.replay(hist, bal, B.Config(lockbox=LB, lender=LENDER, account_reserve=2_000_000, fee_buffer=5_000), facts)


ACTIVE = B.LoanFacts(installment=1_050_000, payment_remaining=4, active=True, settled=False)


def test_bond_is_locked_and_not_free_cash():
    with_bond = _replay([_bond(5_000_000), _ls(50_000_000)], ACTIVE)
    without = _replay([_ls(50_000_000)], ACTIVE)
    assert with_bond.bond == 5_000_000
    assert with_bond.earned_free_cash == without.earned_free_cash


def test_bond_posted_before_any_loan_stays_locked():
    assert _replay([_bond(5_000_000)], B.LoanFacts.none()).bond == 5_000_000


def test_bond_released_after_the_loan_is_settled():
    settled = B.LoanFacts(installment=1_050_000, payment_remaining=0, active=False, settled=True)
    bk = _replay([_bond(5_000_000), _ls(50_000_000)], settled)
    assert bk.bond == 0 and bk.earned_free_cash == bk.balance - 2_000_000 - 5_000


def test_recovery_takes_the_bond_first():
    defaulted = B.LoanFacts(installment=1_050_000, payment_remaining=4, active=False, settled=False)
    bk = _replay([_bond(5_000_000), _ls(50_000_000), _pay_lender(4_000_000)], defaulted)
    assert bk.bond == 1_000_000 - 30  # the recovery fee comes off the bond too
    assert bk.unspent_principal == 50_000_000


# ---- on the simulated ledger ----

def test_sim_rulebook_hash_in_broker_data_and_agreement_hash_in_loanset_data(loaned):
    hist = loaned.b.account_tx(loaned.lender)
    broker_tx = next(e["tx"] for e in hist if e["tx"]["TransactionType"] == "LoanBrokerSet")
    loan_tx = next(e["tx"] for e in hist if e["tx"]["TransactionType"] == "LoanSet")
    assert broker_tx["Data"] == C.rulebook_hash(loaned.params.rulebook()) == loaned.state()["rulebook_hash"]
    assert loan_tx["Data"] == loaned.policy.acceptance.agreement_hash == loaned.state()["agreement_hash"]
    assert len(bytes.fromhex(loan_tx["Data"])) == 32  # well under the 256 byte Data limit; still check on Devnet


def test_sim_loanset_refused_with_a_bad_acceptance_signature(sc):
    sc.lock()
    sc.quote()
    sc.agent.accept_offer(sc.offer, sc.broker_id)
    good = sc.policy.acceptance
    sc.policy.acceptance = C.sign_acceptance(STRANGER, sc.policy.accepted, sc.params.approved_payees)
    assert sc.policy.acceptance.agreement_hash == good.agreement_hash
    with pytest.raises(Refused) as e:
        sc.servicer.originate(sc.offer, sc.agent)
    assert "borrower_signed_terms" in str(e.value)


def test_sim_bond_is_swept_to_the_lender_first_on_default(sc):
    sc.b.fund(sc.agent.address, xrp_to_drops(10))
    assert sc.post_bond(xrp_to_drops(8)).ok
    sc.lock()
    from lockbox.simsetup import build
    assert sc.quote().metrics.total_drops == build().quote().metrics.total_drops  # the bond is not counted as revenue
    sup = Wallet.create().address  # the loan buys stock, then the buyers stop paying
    sc.approve_payee(sup, cap_drops=xrp_to_drops(1000), invoice_ref="PO-9")  # before borrow: frozen once the loan is open
    assert sc.borrow().ok
    assert sc.state()["buckets"]["bond"] == xrp_to_drops(8)
    v, r = sc.agent.withdraw(xrp_to_drops(8))
    assert not v.allowed
    bk = sc.state()["buckets"]
    assert sc.agent.pay_payee(sup, bk["unspent_principal"] + bk["earned_free_cash"], "PO-9")[1].ok
    assert sc.state()["buckets"]["bond"] == xrp_to_drops(8)  # still untouched
    lender0 = sc.b.balance(sc.lender)
    sc.run(6 * 120 + 600, step=30)  # no revenue at all
    s = sc.state()
    assert s["status"] == "defaulted"
    assert sc.b.balance(sc.lender) - lender0 >= xrp_to_drops(8) - 1_000  # recovery reached the lender, bond included
    assert s["buckets"]["bond"] < 10_000  # swept, apart from a few drops of fees


def test_sim_release_refused_while_the_loan_is_open(loaned):
    v, r = loaned.release()
    assert not v.allowed and r is None and "loan_repaid" in v.reason
    assert loaned.b.lockbox_state(loaned.lockbox)["signers"]


def test_sim_release_after_full_repayment_hands_the_account_back(loaned):
    loaned.run(6 * 120 + 60, step=30, revenue_per_step=xrp_to_drops(10))
    assert loaned.state()["status"] == "paid"
    v, r = loaned.release()
    assert v.allowed and r.ok, v.reason
    st = loaned.b.lockbox_state(loaned.lockbox)
    assert st["signers"] == {} and st["quorum"] == 0 and st["regular_key"] == loaned.agent.address
    # the borrower now moves money alone with its registered key
    tx = loaned.b.prepare(P.as_dict(P.payment(loaned.lockbox, loaned.payout, 1_000_000)))
    assert loaned.b.submit(tx, [loaned.agent.key]).ok


def test_sim_cannot_remove_the_signer_list_before_a_regular_key(loaned):
    # XRPL refuses to remove an account's last way to sign: master is off, so the signer list must stay
    tx = loaned.b.prepare(P.as_dict(P.remove_signer_list(loaned.lockbox)), signers_count=2)
    r = loaned.b.submit(tx, [loaned.agent.key, loaned.policy_key])
    assert r.code == "tecNO_ALTERNATIVE_KEY"
