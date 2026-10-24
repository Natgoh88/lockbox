"""One story, runnable on the sim or on a real ledger: the same code for tests, the offline demo and Devnet.

  setup_lender   vault, broker, first-loss cover, deposit
  earn           customers pay the borrower's lockbox address (the revenue history)
  lock           the borrower locks the account (referee weight 2, borrower 1, lender 1, quorum 3, master key off)
  quote          cash-flow underwriting from the lockbox's own ledger history
  borrow         accept the terms, lender verifies the lock, LoanSet signed by the right people
  run            advance time, keep earning (or not), servicer collects / impairs / defaults / recovers
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import covenant, events, health, protocol as P
from .backend import Backend, Result, addr_of
from .covenant import free_cash
from .inflows import parse_account_tx
from .roles import BorrowerAgent, Params, PolicyService, Servicer
from .underwrite import Offer, Policy, underwrite
from .units import RATE_UNITS


def demo_policy(interval: int = 120, service_fee_drops: int = 50_000) -> Policy:
    return Policy(payment_interval=interval, payment_total=6, grace_period=interval, lookback_buckets=12, min_history_buckets=3,
                  min_active_buckets=3, min_total_drops=5_000_000, service_fee_drops=service_fee_drops)


@dataclass
class Scenario:
    b: Backend
    lender: object
    borrower: object
    policy_key: object
    lockbox_key: object
    payout: str
    customers: list = field(default_factory=list)
    uw: Policy = field(default_factory=demo_policy)
    params: Params = field(default_factory=Params)

    vault_id: str = ""
    broker_id: str = ""
    rulebook_hash: str = ""
    offer: Offer | None = None

    def __post_init__(self):
        self.lockbox = addr_of(self.lockbox_key)
        self.params.allowlist = set(self.params.allowlist) | {self.payout}
        self.params.release_key = self.params.release_key or addr_of(self.borrower)
        self.policy = PolicyService(self.b, self.policy_key, self.lockbox, addr_of(self.lender), self.params, borrower=addr_of(self.borrower))
        self.agent = BorrowerAgent(self.b, self.borrower, self.lockbox_key, self.payout, self.policy)
        self.servicer: Servicer | None = None

    # ---- plumbing ----
    def _send(self, tx_model, key, label: str = "") -> Result:
        r = self.b.submit(self.b.prepare(P.as_dict(tx_model)), [key])
        events.emit("tx", ts=self.b.now(), label=label or r.tx["TransactionType"], ok=r.ok, code=r.code, hash=r.hash)
        if not r.ok:
            raise RuntimeError(f"{label or r.tx['TransactionType']} failed: {r.code}")
        return r

    def wait(self, seconds: int) -> None:
        if hasattr(self.b, "advance"):
            self.b.advance(seconds)
        else:
            time.sleep(seconds)

    # ---- the lender side ----
    def setup_lender(self, deposit_drops: int, cover_drops: int, debt_max: int = 500_000_000, cover_min_pct: float = 25.0) -> None:
        la = addr_of(self.lender)
        v = self._send(P.vault_create(la), self.lender, "VaultCreate")
        self.vault_id = v.created["Vault"]
        self._send(P.vault_deposit(la, self.vault_id, deposit_drops), self.lender, "VaultDeposit")
        self.rulebook_hash = covenant.rulebook_hash(self.params.rulebook())
        br = self._send(P.broker_set(la, self.vault_id, debt_max, int(cover_min_pct * RATE_UNITS / 100), RATE_UNITS, data=self.rulebook_hash),
                        self.lender, "LoanBrokerSet")
        self.broker_id = br.created["LoanBroker"]
        self._send(P.cover_deposit(la, self.broker_id, cover_drops), self.lender, "LoanBrokerCoverDeposit")
        self.servicer = Servicer(self.b, self.lender, self.policy, self.lockbox, addr_of(self.borrower), addr_of(self.policy_key),
                                 self.broker_id, self.vault_id)

    def attach(self, vault_id: str, broker_id: str, loan_id: str | None = None) -> None:
        """Re-attach to objects that already exist (after a restart)."""
        self.vault_id, self.broker_id = vault_id, broker_id
        self.servicer = Servicer(self.b, self.lender, self.policy, self.lockbox, addr_of(self.borrower), addr_of(self.policy_key),
                                 broker_id, vault_id)
        if loan_id:
            self.servicer.loan_id = loan_id
            self.servicer.interval = self.uw.payment_interval
            self.policy.set_loan(loan_id)

    def ids(self) -> dict:
        return {"vault_id": self.vault_id, "broker_id": self.broker_id, "loan_id": self.servicer.loan_id if self.servicer else None}

    # ---- the borrower side ----
    def earn(self, drops: int, customer_index: int = 0) -> Result:
        c = self.customers[customer_index % len(self.customers)]
        return self._send(P.payment(addr_of(c), self.lockbox, drops), c, "customer payment")

    def lock(self) -> None:
        results = self.agent.lock(addr_of(self.lender), addr_of(self.policy_key))
        if not all(r.ok for r in results):
            raise RuntimeError("lockdown failed: " + ", ".join(r.code for r in results))

    def approve_payee(self, address: str, cap_drops: int, invoice_ref: str) -> None:
        assert self.servicer is not None
        self.servicer.approve_payee(address, cap_drops, invoice_ref)

    def post_bond(self, drops: int) -> Result:
        return self.agent.post_bond(drops)

    def release(self):
        return self.agent.release()

    # ---- underwriting ----
    def quote(self, requested: int | None = None) -> Offer:
        # the business's own wallets (bond, top-ups) are not revenue
        own = {addr_of(self.lender), addr_of(self.borrower), self.payout}
        flows = parse_account_tx(self.b.account_tx(self.lockbox), self.lockbox, ignore_senders=own)
        self.offer = underwrite(flows, self.b.now(), self.uw, requested)
        events.emit("quote", ts=self.b.now(), approved=self.offer.approved, tier=self.offer.tier, score=self.offer.score,
                    principal=self.offer.principal_drops, installment=self.offer.installment_drops, reasons=self.offer.reasons,
                    declines=self.offer.declines)
        return self.offer

    def borrow(self) -> Result:
        assert self.offer is not None and self.servicer is not None
        self.agent.accept_offer(self.offer, self.broker_id)
        return self.servicer.originate(self.offer, self.agent)

    # ---- running ----
    def run(self, seconds: int, step: int = 30, revenue_per_step: int = 0) -> list[str]:
        """Move time forward in steps. Each step: optional customer payment, then one servicer pass."""
        assert self.servicer is not None
        acts: list[str] = []
        for _ in range(max(1, seconds // step)):
            self.wait(step)
            if revenue_per_step:
                self.earn(revenue_per_step)
            acts += self.servicer.tick()
        return acts

    # ---- reading ----
    def state(self) -> dict:
        now = self.b.now()
        s = self.servicer
        ln = self.b.loan_view(s.loan_id) if s and s.loan_id else None
        ctx = self.policy.context()
        broker = self.b.broker_view(self.broker_id) if self.broker_id else None
        vault = self.b.vault_view(self.vault_id) if self.vault_id else None
        o = self.offer
        return {
            "now": now,
            "lockbox": {"address": self.lockbox, "balance": self.b.balance(self.lockbox), "free_cash": free_cash(ctx),
                        **self.b.lockbox_state(self.lockbox)},
            "lender_balance": self.b.balance(addr_of(self.lender)),
            "payout_balance": self.b.balance(self.payout),
            "status": health.loan_status(ln, now),
            "loan": None if ln is None else {"id": ln.loan_id, "installment": ln.installment, "next_due": ln.next_due,
                                             "remaining": ln.payment_remaining, "impaired": ln.impaired, "defaulted": ln.defaulted,
                                             "owed": ln.total_owed},
            "broker": None if broker is None else {"cover": broker.cover_available, "debt": broker.debt_total,
                                                   "cover_ratio": None if health.cover_ratio(broker) == float("inf") else round(health.cover_ratio(broker), 2)},
            "vault": None if vault is None else {"total": vault.assets_total, "available": vault.assets_available,
                                                 "utilization": round(health.utilization(vault), 3), "loss_unrealized": vault.loss_unrealized},
            "offer": None if o is None else {"approved": o.approved, "tier": o.tier, "score": o.score, "principal": o.principal_drops,
                                              "installment": o.installment_drops, "rate_units": o.interest_rate_units, "payment_total": o.payment_total, "reasons": o.reasons,
                                              "declines": o.declines, "buckets": o.metrics.buckets},
            "recovery_remaining": self.policy.recovery_remaining,
            "buckets": ctx.buckets.to_dict() if ctx.buckets else None,
            "frozen": covenant.frozen_reasons(ctx),
            "ledger_index": self.policy.ledger_clock(),
            "approval": self.policy.approval_state(self.lockbox).outstanding,
            "rulebook_hash": self.rulebook_hash,
            "agreement_hash": self.policy.acceptance.agreement_hash if self.policy.acceptance else None,
            "approved_payees": {a: {"cap_drops": p.cap_drops, "invoice_ref": p.invoice_ref} for a, p in self.params.approved_payees.items()},
        }
