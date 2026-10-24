"""The three parties around a lockbox, as small classes over a Backend.

  BorrowerAgent  owns the revenue account, locks it, accepts terms, asks to withdraw
  PolicyService  the referee. Reads the ledger, applies covenant.py, adds its signature or refuses
  Servicer       the lender side. Verifies the lock, originates the loan, collects, impairs, defaults, recovers

In the demo all three run in one process with their own keys. In a real deployment the policy
service is a separate server that receives the unsigned transaction and returns a signature.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import covenant, events, health, protocol as P
from .backend import Backend, Result, addr_of
from .covenant import AcceptedTerms, CovenantContext, LoanView, Verdict
from .underwrite import Offer


class Refused(Exception):
    def __init__(self, verdict: Verdict):
        super().__init__(verdict.reason)
        self.verdict = verdict


@dataclass
class Params:
    allowlist: set[str] = field(default_factory=set)
    max_fee_drops: int = 1_000
    fee_buffer_drops: int = 5_000
    pay_allowance_drops: int = 10_000
    reserve_installments: int = 1
    pay_lead_fraction: float = 0.25  # collect when this close to the due date (fraction of the interval)


class PolicyService:
    def __init__(self, backend: Backend, key, lockbox: str, lender: str, params: Params):
        self.b = backend
        self.key = key
        self.lockbox = lockbox
        self.lender = lender
        self.params = params
        self.accepted: AcceptedTerms | None = None
        self.loan_id: str | None = None
        self._last_owed = 0
        self.owed_at_default: int | None = None
        self.recovered = 0

    @property
    def address(self) -> str:
        return addr_of(self.key)

    def accept(self, terms: AcceptedTerms) -> None:
        """The borrower accepted these exact terms. In production this call carries the borrower's signature."""
        self.accepted = terms
        events.emit("policy", ts=self.b.now(), msg="borrower accepted terms", principal=terms.principal)

    def set_loan(self, loan_id: str) -> None:
        self.loan_id = loan_id

    @property
    def recovery_remaining(self) -> int:
        return 0 if self.owed_at_default is None else max(0, self.owed_at_default - self.recovered)

    def context(self) -> CovenantContext:
        loan: LoanView | None = self.b.loan_view(self.loan_id) if self.loan_id else None
        if loan is not None:
            if loan.active:
                self._last_owed = loan.total_owed or self._last_owed
            elif loan.defaulted and self.owed_at_default is None:
                self.owed_at_default = self._last_owed or loan.total_owed
        p = self.params
        return CovenantContext(
            lockbox=self.lockbox, lender=self.lender, loan=loan, balance_drops=self.b.balance(self.lockbox),
            account_reserve_drops=self.b.reserve(self.lockbox), now=self.b.now(), allowlist=set(p.allowlist),
            accepted=self.accepted, recovery_remaining=self.recovery_remaining, max_fee_drops=p.max_fee_drops,
            fee_buffer_drops=p.fee_buffer_drops, pay_allowance_drops=p.pay_allowance_drops, reserve_installments=p.reserve_installments,
        )

    def review(self, tx: dict) -> Verdict:
        v = covenant.evaluate(tx, self.context())
        events.emit("verdict", ts=self.b.now(), tx=tx.get("TransactionType"), allowed=v.allowed, reason=v.reason,
                    amount=tx.get("Amount") or tx.get("PrincipalRequested"), free_cash=v.free_cash,
                    to=tx.get("Destination"), checks=[{"name": c.name, "ok": c.ok, "detail": c.detail} for c in v.checks])
        return v

    def cosign(self, tx: dict):
        """Returns this service's key if the covenant allows the transaction, otherwise raises Refused."""
        v = self.review(tx)
        if not v.allowed:
            raise Refused(v)
        return self.key

    def observe(self, tx: dict, result: Result) -> None:
        """Called after a submit so recovery accounting follows what actually happened."""
        if result.ok and tx.get("TransactionType") == "Payment" and tx.get("Destination") == self.lender:
            self.recovered += int(tx["Amount"])


class BorrowerAgent:
    def __init__(self, backend: Backend, key, lockbox_key, payout: str, policy: PolicyService):
        self.b = backend
        self.key = key
        self.lockbox_key = lockbox_key  # the lockbox account's master key, used once to lock it, then useless
        self.lockbox = addr_of(lockbox_key)
        self.payout = payout
        self.policy = policy
        self.offer: Offer | None = None

    @property
    def address(self) -> str:
        return addr_of(self.key)

    def lock(self, lender_addr: str, policy_addr: str) -> list[Result]:
        """Make the lockbox 2-of-3, then switch off its master key. After this the seed in lockbox_key cannot move funds."""
        out = []
        for tx in (P.lockdown_signer_list(self.lockbox, self.address, lender_addr, policy_addr), P.lockdown_disable_master(self.lockbox)):
            r = self.b.submit(self.b.prepare(P.as_dict(tx)), [self.lockbox_key])
            events.emit("lock", ts=self.b.now(), tx=r.tx["TransactionType"], ok=r.ok, code=r.code)
            out.append(r)
        return out

    def accept_offer(self, offer: Offer, broker_id: str) -> AcceptedTerms:
        self.offer = offer
        terms = AcceptedTerms(broker_id=broker_id, principal=offer.principal_drops, interest_rate_units=offer.interest_rate_units,
                              payment_total=offer.payment_total, payment_interval=offer.payment_interval, grace_period=offer.grace_period,
                              origination_fee=offer.origination_fee_drops, service_fee=offer.service_fee_drops)
        self.policy.accept(terms)
        return terms

    def cosign_loan(self, tx: dict):
        """The borrower checks the LoanSet against what it accepted before it signs."""
        o = self.offer
        ok = (o is not None and tx.get("PrincipalRequested") == str(o.principal_drops) and int(tx.get("InterestRate", 0)) == o.interest_rate_units
              and tx.get("PaymentTotal") == o.payment_total and tx.get("PaymentInterval") == o.payment_interval)
        if not ok:
            raise ValueError("LoanSet does not match the accepted offer")
        return self.key

    def withdraw(self, drops: int) -> tuple[Verdict, Result | None]:
        tx = self.b.prepare(P.as_dict(P.payment(self.lockbox, self.payout, drops)), signers_count=2)
        v = self.policy.review(tx)
        if not v.allowed:
            return v, None
        r = self.b.submit(tx, [self.key, self.policy.key])
        events.emit("withdraw", ts=self.b.now(), ok=r.ok, code=r.code, drops=drops)
        return v, r


class Servicer:
    def __init__(self, backend: Backend, lender_key, policy: PolicyService, lockbox: str, borrower_addr: str, policy_addr: str,
                 broker_id: str, vault_id: str):
        self.b = backend
        self.key = lender_key
        self.policy = policy
        self.lockbox = lockbox
        self.borrower_addr = borrower_addr
        self.policy_addr = policy_addr
        self.broker_id = broker_id
        self.vault_id = vault_id
        self.loan_id: str | None = None
        self.last_pay_attempt = -1
        self.interval = 0
        self.late_attempts: dict[int, int] = {}

    @property
    def address(self) -> str:
        return addr_of(self.key)

    def verify_lockbox(self) -> list[tuple[str, bool]]:
        """Read the lockbox's on-ledger state. The lender does not trust the borrower's word, only the ledger."""
        s = self.b.lockbox_state(self.lockbox)
        want = {self.borrower_addr, self.address, self.policy_addr}
        checks = [
            ("master key disabled", bool(s["master_disabled"])),
            ("no regular key", not s.get("regular_key")),
            ("signer quorum is 2", s["quorum"] == 2),
            ("signers are exactly borrower, lender, policy, weight 1 each", set(s["signers"]) == want and all(w == 1 for w in s["signers"].values())),
        ]
        events.emit("verify_lockbox", ts=self.b.now(), checks=[{"name": n, "ok": ok} for n, ok in checks])
        return checks

    def originate(self, offer: Offer, borrower: BorrowerAgent) -> Result:
        failed = [n for n, ok in self.verify_lockbox() if not ok]
        if failed:
            raise RuntimeError("lockbox is not locked down: " + ", ".join(failed))
        tx = self.b.prepare(P.as_dict(P.loan_set(self.address, self.lockbox, self.broker_id, offer)), signers_count=2)
        policy_key = self.policy.cosign(tx)
        borrower_key = borrower.cosign_loan(tx)
        r = self.b.submit(tx, [self.key], counterparty_signers=[borrower_key, policy_key])
        events.emit("loan_set", ts=self.b.now(), ok=r.ok, code=r.code, principal=offer.principal_drops)
        if r.ok:
            self.loan_id = r.created.get("Loan")
            self.interval = offer.payment_interval
            self.policy.set_loan(self.loan_id)
        return r

    # ---- the collection loop ----
    def _submit_with_referee(self, tx: dict) -> Result | None:
        try:
            pkey = self.policy.cosign(tx)
        except Refused as e:
            events.emit("blocked", ts=self.b.now(), tx=tx["TransactionType"], reason=e.verdict.reason)
            return None
        r = self.b.submit(tx, [self.key, pkey])
        self.policy.observe(tx, r)
        return r

    def tick(self) -> list[str]:
        """One pass. Safe to call as often as you like."""
        acts: list[str] = []
        if not self.loan_id:
            return acts
        now = self.b.now()
        self.policy.context()  # keeps the referee's record of what is owed current
        ln = self.b.loan_view(self.loan_id)
        if ln is None:
            return acts

        if ln.defaulted:
            rem = self.policy.recovery_remaining
            ctx = self.policy.context()
            amt = min(rem, covenant.raw_spendable(ctx))
            if amt > 0:
                tx = self.b.prepare(P.as_dict(P.payment(self.lockbox, self.address, amt)), signers_count=2)
                r = self._submit_with_referee(tx)
                if r is not None:
                    events.emit("recovery", ts=now, ok=r.ok, code=r.code, drops=amt)
                    acts.append(f"recovery {amt}")
            return acts
        if ln.payment_remaining <= 0:
            return acts

        if now > ln.next_due + ln.grace_period:
            r = self.b.submit(self.b.prepare(P.as_dict(P.loan_manage(self.address, self.loan_id, "default"))), [self.key])
            events.emit("default", ts=now, ok=r.ok, code=r.code, covered=r.meta.get("default_covered"), loss=r.meta.get("default_loss"))
            acts.append("default")
            self.policy.context()  # lets the referee record what was owed at the moment of default
            return acts
        if now > ln.next_due:
            if not ln.impaired:
                r = self.b.submit(self.b.prepare(P.as_dict(P.loan_manage(self.address, self.loan_id, "impair"))), [self.key])
                events.emit("impair", ts=now, ok=r.ok, code=r.code)
                acts.append("impair")
            # if cash has come back during the grace period, collect with the late-payment flag
            spendable = self.b.balance(self.lockbox) - self.b.reserve(self.lockbox)
            if spendable >= ln.installment + 100 and self.late_attempts.get(ln.next_due, 0) < 3:
                self.late_attempts[ln.next_due] = self.late_attempts.get(ln.next_due, 0) + 1
                tx = self.b.prepare(P.as_dict(P.loan_pay(self.lockbox, self.loan_id, ln.installment, late=True)), signers_count=2)
                r = self._submit_with_referee(tx)
                if r is not None:
                    events.emit("loan_pay", ts=now, ok=r.ok, code=r.code, drops=ln.installment, late=True)
                    acts.append("late pay" if r.ok else f"late pay failed {r.code}")
            return acts

        lead = int(self.interval * self.policy.params.pay_lead_fraction)
        if now >= ln.next_due - lead and self.last_pay_attempt != ln.next_due:
            self.last_pay_attempt = ln.next_due
            spendable = self.b.balance(self.lockbox) - self.b.reserve(self.lockbox)
            if spendable < ln.installment + 100:
                events.emit("short", ts=now, balance=self.b.balance(self.lockbox), need=ln.installment)
                acts.append("short")
                return acts
            tx = self.b.prepare(P.as_dict(P.loan_pay(self.lockbox, self.loan_id, ln.installment)), signers_count=2)
            r = self._submit_with_referee(tx)
            if r is not None:
                events.emit("loan_pay", ts=now, ok=r.ok, code=r.code, drops=ln.installment, remaining=ln.payment_remaining - (1 if r.ok else 0))
                acts.append("pay" if r.ok else f"pay failed {r.code}")
        return acts

    def snapshot(self) -> dict:
        now = self.b.now()
        ln = self.b.loan_view(self.loan_id) if self.loan_id else None
        return {"status": health.loan_status(ln, now), "loan": ln}
