"""The three parties around a lockbox, as small classes over a Backend.

  BorrowerAgent  owns the revenue account, locks it, accepts terms, asks to withdraw
  PolicyService  the referee. Reads the ledger, applies covenant.py, adds its signature or refuses
  Servicer       the lender side. Verifies the lock, originates the loan, collects, impairs, defaults, recovers

In the demo all three run in one process with their own keys. In a real deployment the policy
service is a separate server that receives the unsigned transaction and returns a signature.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import buckets, covenant, events, health, protocol as P, verify
from .backend import Backend, Result, addr_of
from .buckets import Payee
from .covenant import AcceptedTerms, Acceptance, ApprovalState, CovenantContext, LoanView, Verdict
from .underwrite import Offer


class Refused(Exception):
    def __init__(self, verdict: Verdict):
        super().__init__(verdict.reason)
        self.verdict = verdict


@dataclass
class Params:
    allowlist: set[str] = field(default_factory=set)  # the borrower's own wallets: earned free cash only
    approved_payees: dict[str, Payee] = field(default_factory=dict)  # lender-approved suppliers: principal, capped, invoice memo
    max_fee_drops: int = 1_000
    fee_buffer_drops: int = 5_000
    pay_allowance_drops: int = 10_000
    reserve_installments: int = 1  # holdback: whole installments kept back
    holdback_bps: int = 0  # holdback: share of each revenue payment kept back while a loan is open (basis points)
    freeze_after_short_intervals: int = 2
    pay_lead_fraction: float = 0.5  # collect when this close to the due date (fraction of the interval). 0.25 (30 s) was too
    # late on Devnet: the LoanPay validated after the due date and failed with tecEXPIRED
    release_key: str = ""  # the borrower's registered key; the account goes back to it after full repayment

    def rulebook(self) -> dict:
        """The numbers the referee enforces. The fingerprint of name, version and these goes in the LoanBroker Data field."""
        return {"max_fee_drops": self.max_fee_drops, "fee_buffer_drops": self.fee_buffer_drops, "pay_allowance_drops": self.pay_allowance_drops,
                "reserve_installments": self.reserve_installments, "holdback_bps": self.holdback_bps,
                "freeze_after_short_intervals": self.freeze_after_short_intervals, "approval_window": covenant.APPROVAL_WINDOW,
                "signer_weights": {"referee": P.REFEREE_WEIGHT, "borrower": P.PARTY_WEIGHT, "lender": P.PARTY_WEIGHT},
                "quorum": P.LOCKBOX_QUORUM}


class PolicyService:
    def __init__(self, backend: Backend, key, lockbox: str, lender: str, params: Params, borrower: str = ""):
        self.b = backend
        self.key = key
        self.lockbox = lockbox
        self.lender = lender
        self.borrower = borrower
        self.params = params
        self.accepted: AcceptedTerms | None = None
        self.acceptance: Acceptance | None = None
        self.loan_id: str | None = None
        self._last_owed = 0
        self.owed_at_default: int | None = None
        self.recovered = 0
        self.last_verdict: Verdict | None = None
        self.outstanding: dict | None = None  # the one approval the referee has handed out and not yet seen settle
        self.vault_id: str | None = None  # the lender's vault: a LoanSet must finish, grace included, before it redeems
        self._seen: str | None = None  # hash of the newest lockbox history entry the last context was built from

    @property
    def address(self) -> str:
        return addr_of(self.key)

    def accept(self, terms: AcceptedTerms, acceptance: Acceptance | None = None) -> None:
        """The borrower accepted these exact terms. The signature is checked when the LoanSet comes in."""
        self.accepted = terms
        self.acceptance = acceptance
        events.emit("policy", ts=self.b.now(), msg="borrower accepted terms", principal=terms.principal,
                    agreement_hash=acceptance.agreement_hash if acceptance else None)

    def set_loan(self, loan_id: str) -> None:
        self.loan_id = loan_id

    @property
    def recovery_remaining(self) -> int:
        return 0 if self.owed_at_default is None else max(0, self.owed_at_default - self.recovered)

    def ledger_clock(self) -> int | None:
        try:
            return self.b.ledger_index()
        except (AttributeError, NotImplementedError):
            return None

    def approval_state(self, account: str) -> ApprovalState:
        """Clears the outstanding approval once the ledger shows it validated (sequence moved on) or expired."""
        ledger = self.ledger_clock()
        o = self.outstanding
        if o is not None and ledger is not None:
            try:
                done = self.b.next_sequence(o["account"]) > o["sequence"]
            except (AttributeError, NotImplementedError):
                done = False
            if done or ledger > o["last_ledger"]:
                self.outstanding = o = None
        try:
            seq = self.b.next_sequence(account) if ledger is not None else None
        except (AttributeError, NotImplementedError):
            seq = None
        return ApprovalState(ledger_index=ledger, next_sequence=seq, outstanding=o)

    def context(self, tx: dict | None = None) -> CovenantContext:
        loan: LoanView | None = self.b.loan_view(self.loan_id) if self.loan_id else None
        if loan is not None:
            if loan.active:
                self._last_owed = loan.total_owed or self._last_owed
            elif loan.defaulted and self.owed_at_default is None:
                self.owed_at_default = self._last_owed or loan.total_owed
        p = self.params
        balance, reserve = self.b.balance(self.lockbox), self.b.reserve(self.lockbox)
        cfg = buckets.Config(lockbox=self.lockbox, lender=self.lender, approved_payees=dict(p.approved_payees), account_reserve=reserve,
                             fee_buffer=p.fee_buffer_drops, reserve_installments=p.reserve_installments, holdback_bps=p.holdback_bps)
        history = self.b.account_tx(self.lockbox)
        bk = buckets.replay(history, balance, cfg, covenant.loan_facts(loan, self.recovery_remaining), now=self.b.now())
        seen = buckets._ordered([e for e in history if e.get("validated", True)])
        self._seen = (seen[-1].get("hash") or buckets._tx(seen[-1]).get("hash")) if seen else None
        return CovenantContext(
            lockbox=self.lockbox, lender=self.lender, loan=loan, balance_drops=balance,
            account_reserve_drops=reserve, now=self.b.now(), allowlist=set(p.allowlist),
            accepted=self.accepted, recovery_remaining=self.recovery_remaining, max_fee_drops=p.max_fee_drops,
            fee_buffer_drops=p.fee_buffer_drops, pay_allowance_drops=p.pay_allowance_drops, reserve_installments=p.reserve_installments,
            approved_payees=dict(p.approved_payees), buckets=bk, holdback_bps=p.holdback_bps,
            freeze_after_short_intervals=p.freeze_after_short_intervals,
            approval=self.approval_state(tx.get("Account", self.lockbox)) if tx is not None else None,
            borrower=self.borrower, acceptance=self.acceptance, release_key=p.release_key,
            rulebook_hash=covenant.rulebook_fingerprint(p.rulebook()), broker_data=self._broker_data(tx),
            **self._vault_redemption(tx),
        )

    def _broker_data(self, tx: dict | None) -> str | None:
        if not tx or tx.get("TransactionType") != "LoanSet":
            return None
        try:
            return getattr(self.b.broker_view(tx.get("LoanBrokerID", "")), "data", None)
        except Exception:  # unknown broker or unreadable entry: the check fails closed
            return None

    def _vault_redemption(self, tx: dict | None) -> dict:
        if not tx or tx.get("TransactionType") != "LoanSet" or not self.vault_id:
            return {}
        try:
            return {"vault_redemption": self.b.vault_view(self.vault_id).redemption}
        except Exception:  # unreadable vault: the check fails closed
            return {"vault_readable": False}

    def review(self, tx: dict, dry_run: bool = False) -> Verdict:
        """dry_run: only asks what the referee would say (a preview). Logged as a "dry_run" event without a decision
        record, never as a "verdict", so verify never replays it as a decision. Nothing is signed either way."""
        ctx = self.context(tx)
        v = covenant.evaluate(tx, ctx)
        rb = covenant.rulebook_identity(self.params.rulebook())
        if dry_run:
            events.emit("dry_run", ts=self.b.now(), tx=tx.get("TransactionType"), allowed=v.allowed, reason=v.reason,
                        rule=verify.deciding_rule(v), rulebook=rb["name"], rulebook_version=rb["version"],
                        rulebook_fingerprint=rb["fingerprint"], amount=tx.get("Amount") or tx.get("PrincipalRequested"),
                        to=tx.get("Destination"))
            return v
        # the decision record lets anyone replay this verdict later (scripts/verify.py)
        decision = {"tx": dict(tx), "inputs": verify.record_inputs(ctx), "seen": self._seen, "balance": ctx.balance_drops}
        events.emit("verdict", ts=self.b.now(), tx=tx.get("TransactionType"), allowed=v.allowed, reason=v.reason,
                    rule=verify.deciding_rule(v), decision=decision,
                    rulebook=rb["name"], rulebook_version=rb["version"], rulebook_fingerprint=rb["fingerprint"],
                    amount=tx.get("Amount") or tx.get("PrincipalRequested"), free_cash=v.free_cash,
                    to=tx.get("Destination"), checks=[{"name": c.name, "ok": c.ok, "detail": c.detail} for c in v.checks])
        return v

    def cosign(self, tx: dict):
        """Returns this service's key if the covenant allows the transaction, otherwise raises Refused."""
        v = self.last_verdict = self.review(tx)
        if not v.allowed:
            raise Refused(v)
        self.outstanding = {"account": tx["Account"], "sequence": tx["Sequence"], "last_ledger": tx["LastLedgerSequence"]}
        events.emit("approval", ts=self.b.now(), tx=tx.get("TransactionType"), **self.outstanding)
        return self.key

    def observe(self, tx: dict, result: Result) -> None:
        """Called after a submit so recovery accounting follows what actually happened. Also logs which ledger
        transaction the referee's approval became, so a decision can link to it."""
        events.emit("submitted", ts=self.b.now(), account=tx.get("Account"), sequence=tx.get("Sequence"), hash=result.hash,
                    ok=result.ok, code=result.code)
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
        """Install the weighted signer list (referee required), then switch off the master key. After this the seed
        in lockbox_key cannot move funds."""
        out = []
        for tx in (P.lockdown_signer_list(self.lockbox, self.address, lender_addr, policy_addr), P.lockdown_disable_master(self.lockbox)):
            r = self.b.submit(self.b.prepare(P.as_dict(tx)), [self.lockbox_key])
            events.emit("lock", ts=self.b.now(), tx=r.tx["TransactionType"], ok=r.ok, code=r.code)
            out.append(r)
        return out

    def accept_offer(self, offer: Offer, broker_id: str) -> AcceptedTerms:
        """Sign the hash of the exact terms plus the approved payees, and hand both to the referee."""
        self.offer = offer
        terms = AcceptedTerms(broker_id=broker_id, principal=offer.principal_drops, interest_rate_units=offer.interest_rate_units,
                              payment_total=offer.payment_total, payment_interval=offer.payment_interval, grace_period=offer.grace_period,
                              origination_fee=offer.origination_fee_drops, service_fee=offer.service_fee_drops)
        acc = covenant.sign_acceptance(self.key, terms, self.policy.params.approved_payees) if hasattr(self.key, "private_key") else None
        self.policy.accept(terms, acc)
        return terms

    def pay_off(self) -> tuple[Verdict, Result | None]:
        """Early full payoff: one LoanPay for TotalValueOutstanding plus LoanServiceFee, borrower + referee."""
        ln = self.b.loan_view(self.policy.loan_id) if self.policy.loan_id else None
        if ln is None:
            raise RuntimeError("no loan to pay off")
        amount = ln.total_owed + ln.service_fee
        return self._send_with_referee(P.loan_pay(self.lockbox, ln.loan_id, amount), "pay_off", drops=amount)

    def post_bond(self, drops: int) -> Result:
        """Send the business's own XRP into the lockbox as a bond. Locked until the loan is settled, swept first on default."""
        tx = self.b.prepare(P.as_dict(P.payment(self.address, self.lockbox, drops, memos=[P.memo(buckets.BOND_MEMO)])))
        r = self.b.submit(tx, [self.key])
        events.emit("bond", ts=self.b.now(), ok=r.ok, code=r.code, drops=drops)
        return r

    def release(self) -> tuple[Verdict, Result | None]:
        """After full repayment: referee + borrower set the borrower's registered key as the regular key, then the
        borrower removes the signer list with it. The order matters: XRPL will not remove the last way to sign.
        Checked on Devnet."""
        key = self.policy.params.release_key or self.address
        v, r = self._send_with_referee(P.set_regular_key(self.lockbox, key), "release_regular_key", key=key)
        if r is None or not r.ok:
            return v, r
        r2 = self.b.submit(self.b.prepare(P.as_dict(P.remove_signer_list(self.lockbox))), [self.key])
        events.emit("release_signer_list", ts=self.b.now(), ok=r2.ok, code=r2.code)
        return v, r2

    def cosign_loan(self, tx: dict):
        """The borrower checks the LoanSet against what it accepted before it signs."""
        o = self.offer
        ok = (o is not None and tx.get("PrincipalRequested") == str(o.principal_drops) and int(tx.get("InterestRate", 0)) == o.interest_rate_units
              and tx.get("PaymentTotal") == o.payment_total and tx.get("PaymentInterval") == o.payment_interval)
        if not ok:
            raise ValueError("LoanSet does not match the accepted offer")
        return self.key

    def _send_with_referee(self, tx_model, kind: str, **extra) -> tuple[Verdict, Result | None]:
        tx = self.b.prepare(P.as_dict(tx_model), signers_count=2)
        try:
            pkey = self.policy.cosign(tx)
        except Refused as e:
            return e.verdict, None
        r = self.b.submit(tx, [self.key, pkey])
        self.policy.observe(tx, r)
        events.emit(kind, ts=self.b.now(), ok=r.ok, code=r.code, **extra)
        return self.policy.last_verdict, r

    def withdraw(self, drops: int) -> tuple[Verdict, Result | None]:
        """Send earned free cash to the business's own payout wallet."""
        return self._send_with_referee(P.payment(self.lockbox, self.payout, drops), "withdraw", drops=drops)

    def pay_payee(self, dest: str, drops: int, invoice_ref: str) -> tuple[Verdict, Result | None]:
        """Pay an approved supplier out of the loan principal. The invoice reference rides in a memo."""
        tx = P.payment(self.lockbox, dest, drops, memos=[P.memo(invoice_ref, "invoice")])
        return self._send_with_referee(tx, "pay_payee", drops=drops, to=dest, invoice=invoice_ref)


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

    def approve_payee(self, address: str, cap_drops: int, invoice_ref: str) -> None:
        """The lender approves a supplier the loan principal may be spent with. Only before LoanSet: the list is part of
        the agreement hash the borrower signs, so it is frozen while a loan is open. (Mid-loan changes: roadmap.)"""
        ln = self.b.loan_view(self.loan_id) if self.loan_id else None
        if ln is not None and ln.active:
            raise RuntimeError("the approved-supplier list is frozen while a loan is open")
        self.policy.params.approved_payees[address] = Payee(cap_drops=cap_drops, invoice_ref=invoice_ref)
        events.emit("approve_payee", ts=self.b.now(), to=address, cap=cap_drops, invoice=invoice_ref)

    def verify_lockbox(self) -> list[tuple[str, bool]]:
        """Read the lockbox's on-ledger state. The lender does not trust the borrower's word, only the ledger."""
        s = self.b.lockbox_state(self.lockbox)
        signers, quorum = s["signers"], s["quorum"]
        want = P.lockbox_signer_weights(self.borrower_addr, self.address, self.policy_addr)
        without_referee = sum(w for a, w in signers.items() if a != self.policy_addr)
        checks = [
            ("master key disabled", bool(s["master_disabled"])),
            ("no regular key", not s.get("regular_key")),
            (f"signer quorum is {P.LOCKBOX_QUORUM}", quorum == P.LOCKBOX_QUORUM),
            ("signers are exactly borrower, lender, policy", set(signers) == set(want)),
            (f"weights: referee {P.REFEREE_WEIGHT}, borrower {P.PARTY_WEIGHT}, lender {P.PARTY_WEIGHT}", signers == want),
            ("no payment possible without the referee", 0 < quorum and without_referee < quorum),
        ]
        events.emit("verify_lockbox", ts=self.b.now(), checks=[{"name": n, "ok": ok} for n, ok in checks])
        return checks

    def originate(self, offer: Offer, borrower: BorrowerAgent) -> Result:
        failed = [n for n, ok in self.verify_lockbox() if not ok]
        if failed:
            raise RuntimeError("lockbox is not locked down: " + ", ".join(failed))
        a = self.policy.accepted
        data = covenant.agreement_hash(a, self.policy.params.approved_payees) if a else None
        tx = self.b.prepare(P.as_dict(P.loan_set(self.address, self.lockbox, self.broker_id, offer, data=data)), signers_count=2)
        policy_key = self.policy.cosign(tx)
        borrower_key = borrower.cosign_loan(tx)
        r = self.b.submit(tx, [self.key], counterparty_signers=[borrower_key, policy_key])
        self.policy.observe(tx, r)
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
            # past grace: one last late LoanPay if the cash is there, default only if that fails or the cash is not there
            if self.late_attempts.get(("past_grace", ln.next_due)) is None:
                self.late_attempts[("past_grace", ln.next_due)] = 1
                if self._late_pay(ln, acts):
                    return acts
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
            if self.late_attempts.get(ln.next_due, 0) < 3 and self._can_cover(ln):
                self.late_attempts[ln.next_due] = self.late_attempts.get(ln.next_due, 0) + 1
                self._late_pay(ln, acts)
            return acts

        lead = int(self.interval * self.policy.params.pay_lead_fraction)
        if now >= ln.next_due - lead and self.last_pay_attempt != ln.next_due:
            self.last_pay_attempt = ln.next_due
            spendable = self.b.balance(self.lockbox) - self.b.reserve(self.lockbox)
            if spendable < ln.installment + 2 * self.policy.params.max_fee_drops:
                events.emit("short", ts=now, balance=self.b.balance(self.lockbox), need=ln.installment)
                acts.append("short")
                return acts
            tx = self.b.prepare(P.as_dict(P.loan_pay(self.lockbox, self.loan_id, ln.installment)), signers_count=2)
            r = self._submit_with_referee(tx)
            if r is not None:
                events.emit("loan_pay", ts=now, ok=r.ok, code=r.code, drops=ln.installment, remaining=ln.payment_remaining - (1 if r.ok else 0))
                acts.append("pay" if r.ok else f"pay failed {r.code}")
        return acts

    def _can_cover(self, ln: LoanView) -> bool:
        spendable = self.b.balance(self.lockbox) - self.b.reserve(self.lockbox)
        return spendable >= ln.installment + 2 * self.policy.params.max_fee_drops

    def _late_pay(self, ln: LoanView, acts: list[str]) -> bool:
        """One LoanPay with the late-payment flag, if the lockbox can cover it. True if the ledger took it."""
        if not self._can_cover(ln):
            return False
        tx = self.b.prepare(P.as_dict(P.loan_pay(self.lockbox, self.loan_id, ln.installment, late=True)), signers_count=2)
        r = self._submit_with_referee(tx)
        if r is None:
            acts.append("late pay refused")
            return False
        events.emit("loan_pay", ts=self.b.now(), ok=r.ok, code=r.code, drops=ln.installment, late=True)
        acts.append("late pay" if r.ok else f"late pay failed {r.code}")
        return r.ok

    def snapshot(self) -> dict:
        now = self.b.now()
        ln = self.b.loan_view(self.loan_id) if self.loan_id else None
        return {"status": health.loan_status(ln, now), "loan": ln}
