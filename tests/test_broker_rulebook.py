"""The referee only co-signs a LoanSet on a broker whose Data field holds the hash of the referee's own rulebook."""
import pytest
from xrpl.wallet import Wallet

from lockbox import covenant as C
from lockbox import protocol as P
from lockbox.covenant import AcceptedTerms, CovenantContext, evaluate
from lockbox.roles import Refused

LB, LENDER, BROKER = "rLockbox", "rLender", "B" * 64
BORROWER = Wallet.create()
RULES = C.rulebook_hash({"reserve_installments": 1})


def terms():
    return AcceptedTerms(broker_id=BROKER, principal=10_000_000, interest_rate_units=8000, payment_total=6, payment_interval=120,
                         grace_period=120, service_fee=50_000)


def setup(**kw):
    acc = C.sign_acceptance(BORROWER, terms(), {})
    base = dict(lockbox=LB, lender=LENDER, loan=None, balance_drops=20_000_000, account_reserve_drops=2_000_000, now=0, accepted=terms(),
                borrower=BORROWER.address, acceptance=acc, rulebook_hash=RULES, broker_data=RULES)
    base.update(kw)
    tx = {"TransactionType": "LoanSet", "Account": LENDER, "Counterparty": LB, "LoanBrokerID": BROKER, "PrincipalRequested": "10000000",
          "InterestRate": 8000, "PaymentTotal": 6, "PaymentInterval": 120, "GracePeriod": 120, "LoanServiceFee": "50000", "Fee": "60",
          "Data": acc.agreement_hash}
    return tx, CovenantContext(**base)


def failed(v):
    return {c.name for c in v.checks if not c.ok}


def test_matching_broker_rulebook_allowed():
    tx, c = setup()
    assert evaluate(tx, c).allowed


def test_broker_with_other_rules_refused():
    tx, c = setup(broker_data=C.rulebook_hash({"reserve_installments": 0}))
    assert "broker_rulebook_hash" in failed(evaluate(tx, c))


def test_broker_without_data_refused():
    for data in ("", None):
        tx, c = setup(broker_data=data)
        assert "broker_rulebook_hash" in failed(evaluate(tx, c))


def test_referee_without_a_rulebook_hash_refuses():
    tx, c = setup(rulebook_hash="", broker_data="")
    assert "broker_rulebook_hash" in failed(evaluate(tx, c))


def test_hash_compare_ignores_hex_case():
    tx, c = setup(broker_data=RULES.lower())
    assert evaluate(tx, c).allowed


# ---- on the simulated ledger ----

def test_sim_broker_view_returns_the_data_field(sc):
    assert sc.b.broker_view(sc.broker_id).data == sc.rulebook_hash == C.rulebook_hash(sc.params.rulebook())


def test_sim_loanset_refused_when_the_referee_runs_different_rules(sc):
    sc.lock()
    sc.quote()
    sc.params.reserve_installments = 2  # the referee now enforces something the broker never published
    sc.agent.accept_offer(sc.offer, sc.broker_id)
    with pytest.raises(Refused) as e:
        sc.servicer.originate(sc.offer, sc.agent)
    assert "broker_rulebook_hash" in str(e.value)


def test_sim_loanset_refused_on_a_broker_without_the_rulebook(sc):
    la = sc.servicer.address
    br = sc._send(P.broker_set(la, sc.vault_id, 500_000_000, 25_000, 100_000), sc.lender, "LoanBrokerSet (no Data)")
    bare = br.created["LoanBroker"]
    sc._send(P.cover_deposit(la, bare, 40_000_000), sc.lender, "cover")
    sc.attach(sc.vault_id, bare)
    sc.lock()
    sc.quote()
    sc.agent.accept_offer(sc.offer, bare)
    with pytest.raises(Refused) as e:
        sc.servicer.originate(sc.offer, sc.agent)
    assert "broker_rulebook_hash" in str(e.value)
