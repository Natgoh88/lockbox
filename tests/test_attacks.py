"""Red team: each test tries one loophole from CLAUDE.md against the rulebook, end to end on the simulated ledger.

Every attack goes through the same path the dashboard uses (BorrowerAgent, PolicyService, Servicer, SimChain),
so a pass means the referee or the ledger itself refused it. Rules not built yet are marked xfail with a reason.
"""
import pytest
from xrpl.models.transactions import SignerListSet
from xrpl.models.transactions.signer_list_set import SignerEntry

from lockbox import protocol as P
from lockbox.backend import addr_of
from lockbox.roles import Refused
from lockbox.units import xrp_to_drops

INVOICE = "PO-1001"


@pytest.fixture
def funded(sc):
    """A live loan, with customer 3 approved as a supplier the principal may be spent with (approved before borrow:
    the supplier list is frozen once the loan is open)."""
    sc.lock()
    sc.quote()
    sc.approve_payee(sc.customers[2], cap_drops=xrp_to_drops(40), invoice_ref=INVOICE)
    assert sc.borrow().ok
    return sc


def _withdraw_tx(sc, drops):
    return sc.b.prepare(P.as_dict(P.payment(sc.lockbox, sc.payout, drops)), signers_count=2)


def _failed(verdict):
    return {c.name for c in verdict.checks if not c.ok}


def test_payee_refund_laundering(funded):
    # pay a friendly supplier out of principal, have it sent straight back, then pull it to the own wallet
    sup = funded.customers[2]
    v, r = funded.agent.pay_payee(sup, xrp_to_drops(30), INVOICE)
    assert v.allowed and r.ok
    funded._send(P.payment(sup, funded.lockbox, xrp_to_drops(30)), sup, "supplier refund")
    assert funded.state()["buckets"]["earned_free_cash"] == 0  # the refund went back to unspent principal
    payout0 = funded.b.balance(funded.payout)
    v, r = funded.agent.withdraw(xrp_to_drops(30))
    assert not v.allowed and r is None
    assert "within_free_cash" in _failed(v)
    assert funded.b.balance(funded.payout) == payout0


def test_stacked_approvals(funded):
    # get the referee to sign one withdrawal, hold it back, and ask for a second one
    funded.earn(xrp_to_drops(10))
    first = _withdraw_tx(funded, xrp_to_drops(1))
    key = funded.policy.cosign(first)
    second = _withdraw_tx(funded, xrp_to_drops(1))
    with pytest.raises(Refused) as e:
        funded.policy.cosign(second)
    assert "one_approval_at_a_time" in _failed(e.value.verdict)
    # once the first settles on the ledger the referee will sign again
    assert funded.b.submit(first, [funded.borrower, key]).ok
    funded.policy.cosign(_withdraw_tx(funded, xrp_to_drops(1)))


def test_stale_approval_submitted_late(funded):
    funded.earn(xrp_to_drops(10))
    # a signature that would stay valid for a long time is never handed out
    long_lived = _withdraw_tx(funded, xrp_to_drops(1))
    long_lived["LastLedgerSequence"] = funded.b.ledger_index() + 20  # xrpl-py's autofill default
    with pytest.raises(Refused) as e:
        funded.policy.cosign(long_lived)
    assert "last_ledger_window" in _failed(e.value.verdict)
    # a fresh one, saved up and submitted after its window, is dead on the ledger
    tx = _withdraw_tx(funded, xrp_to_drops(1))
    key = funded.policy.cosign(tx)
    funded.b.advance(60)  # 15 ledgers later
    payout0 = funded.b.balance(funded.payout)
    r = funded.b.submit(tx, [funded.borrower, key])
    assert not r.ok and r.code == "tefMAX_LEDGER"
    assert funded.b.balance(funded.payout) == payout0


def test_loan_money_to_own_wallet(funded):
    principal = funded.offer.principal_drops
    payout0 = funded.b.balance(funded.payout)
    # straight to the payout wallet: principal is never free cash
    for amt in (principal, principal // 2, xrp_to_drops(1)):
        v, r = funded.agent.withdraw(amt)
        assert not v.allowed and r is None, amt
    # around the allowlist, to a wallet nobody approved
    v, r = funded.agent._send_with_referee(P.payment(funded.lockbox, funded.customers[0], xrp_to_drops(5)), "attack")
    assert not v.allowed and "destination_allowlisted" in _failed(v)
    # to the approved supplier without the invoice, or over its cap
    v, r = funded.agent._send_with_referee(P.payment(funded.lockbox, funded.customers[2], xrp_to_drops(5)), "attack")
    assert not v.allowed and "invoice_memo" in _failed(v)
    v, r = funded.agent.pay_payee(funded.customers[2], xrp_to_drops(41), INVOICE)
    assert not v.allowed and "within_payee_cap" in _failed(v)
    assert funded.b.balance(funded.payout) == payout0


def test_lender_impair_does_not_freeze_a_healthy_borrower(funded):
    # the lender flags a loan that is not late, hoping to lock up the borrower's own earnings
    from dataclasses import replace
    from lockbox import covenant
    funded.earn(xrp_to_drops(10))
    loan_id = funded.servicer.loan_id
    tx = funded.b.prepare(P.as_dict(P.loan_manage(addr_of(funded.lender), loan_id, "impair")))
    r = funded.b.submit(tx, [funded.lender])
    assert not r.ok and r.code == "tecTOO_SOON"  # the ledger only allows impair after the due date
    assert not funded.b.loan_view(loan_id).impaired
    free = funded.state()["buckets"]["earned_free_cash"]
    assert free > 0
    # even if the flag were set on a current loan, the rulebook would not freeze on it
    ctx = funded.policy.context()
    ctx.loan = replace(ctx.loan, impaired=True)
    assert covenant.frozen_reasons(ctx) == []
    v, r = funded.agent.withdraw(free)
    assert v.allowed and r.ok, v.reason


def test_release_while_a_loan_is_open(loaned):
    before = loaned.b.lockbox_state(loaned.lockbox)
    v, r = loaned.agent.release()
    assert not v.allowed and r is None
    assert {"loan_repaid", "nothing_owed"} <= _failed(v)
    after = loaned.b.lockbox_state(loaned.lockbox)
    assert after == before and after["regular_key"] is None


def test_borrower_plus_lender_bypass_the_referee(loaned):
    # a broker colluding with a fake borrower: both keys, no referee
    sc = loaned
    pair = [sc.borrower, sc.lender]
    b, l, p = addr_of(sc.borrower), addr_of(sc.lender), addr_of(sc.policy_key)
    balance0, state0 = sc.b.balance(sc.lockbox), sc.b.lockbox_state(sc.lockbox)
    attacks = {
        "drain": P.payment(sc.lockbox, l, xrp_to_drops(5)),
        "take the account": P.set_regular_key(sc.lockbox, b),
        "demote the referee": SignerListSet(account=sc.lockbox, signer_quorum=3,
                                            signer_entries=[SignerEntry(account=a, signer_weight=w) for a, w in {b: 2, l: 2, p: 1}.items()]),
    }
    for name, model in attacks.items():
        r = sc.b.submit(sc.b.prepare(P.as_dict(model), signers_count=2), pair)
        assert not r.ok and r.code == "tefBAD_QUORUM", name
    assert sc.b.balance(sc.lockbox) == balance0
    assert sc.b.lockbox_state(sc.lockbox) == state0
