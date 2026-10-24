"""The covenant: the rulebook the policy service applies before it adds its signature.

The lockbox account is a 2-of-3 multisig with the master key disabled:

    borrower agent    the business that owns the revenue
    lender servicer   the vault / broker owner that funded the loan
    policy service    a neutral referee that only signs what this file allows

Any two signatures move money. So the borrower alone cannot spend the lender's next payment,
the lender alone cannot touch the borrower's cash, and the referee alone can do nothing.
The referee's rules are below. They are plain checks, no model, and they fail closed.

  LoanSet   (lockbox is the Counterparty) only if the borrower accepted these exact terms
  LoanPay   the right loan, the right amount, no special flags. Lender + referee can autopay.
  Payment   to the borrower's allowlist: only while the loan is current, only up to free cash
            to the lender: only after default, only up to what was owed (recovery)

Free cash = balance - account reserve - next installment(s) - fee buffer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TF_LOAN_LATE_PAYMENT = 262144  # the only LoanPay flag the lockbox ever uses (overpayment 65536 and full payment 131072 are never allowed)
TF_FULLY_CANONICAL = 0x80000000
TF_PARTIAL_PAYMENT = 0x00020000
FORBIDDEN_FIELDS = ("Paths", "SendMax", "DeliverMin", "DeliverMax", "Sponsor", "Delegate", "RegularKey")
LOANSET_BANNED_FEES = ("LatePaymentFee", "ClosePaymentFee", "OverpaymentFee")


@dataclass
class LoanView:
    loan_id: str
    borrower: str
    periodic_payment: int  # drops, read from the ledger
    service_fee: int  # drops per payment
    next_due: int  # unix seconds
    payment_remaining: int
    grace_period: int = 0
    defaulted: bool = False
    impaired: bool = False
    total_owed: int = 0  # TotalValueOutstanding, drops

    @property
    def installment(self) -> int:
        return self.periodic_payment + self.service_fee

    @property
    def active(self) -> bool:
        return not self.defaulted and self.payment_remaining > 0


@dataclass
class AcceptedTerms:
    """What the borrower agreed to. The referee refuses a LoanSet that differs."""
    broker_id: str
    principal: int
    interest_rate_units: int
    payment_total: int
    payment_interval: int
    grace_period: int
    origination_fee: int = 0
    service_fee: int = 0


@dataclass
class CovenantContext:
    lockbox: str
    lender: str
    loan: LoanView | None
    balance_drops: int
    account_reserve_drops: int
    now: int
    allowlist: set[str] = field(default_factory=set)
    accepted: AcceptedTerms | None = None
    recovery_remaining: int = 0  # drops still owed to the lender after a default
    max_fee_drops: int = 1_000
    fee_buffer_drops: int = 5_000
    pay_allowance_drops: int = 10_000  # LoanPay may exceed the installment by at most this much
    reserve_installments: int = 1


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Verdict:
    allowed: bool
    checks: list[Check]
    free_cash: int = 0

    @property
    def reason(self) -> str:
        bad = [c for c in self.checks if not c.ok]
        return "; ".join(f"{c.name}: {c.detail}" for c in bad) if bad else "all checks passed"

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "free_cash": self.free_cash,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def in_default(ctx: CovenantContext) -> bool:
    return ctx.loan is not None and ctx.loan.defaulted and ctx.recovery_remaining > 0


def installment_reserve(ctx: CovenantContext) -> int:
    loan = ctx.loan
    if loan is None or not loan.active:
        return 0
    return loan.installment * min(ctx.reserve_installments, loan.payment_remaining)


def raw_spendable(ctx: CovenantContext) -> int:
    return max(0, ctx.balance_drops - ctx.account_reserve_drops - ctx.fee_buffer_drops)


def free_cash(ctx: CovenantContext) -> int:
    """What the borrower may withdraw right now."""
    if in_default(ctx):
        return 0
    return max(0, raw_spendable(ctx) - installment_reserve(ctx))


def _is_drops(v) -> bool:
    return isinstance(v, str) and v.isdigit()


def _int(tx: dict, key: str) -> int:
    v = tx.get(key, 0)
    return int(v) if (isinstance(v, int) or _is_drops(v)) else -1


def evaluate(tx: dict, ctx: CovenantContext) -> Verdict:
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> bool:
        checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    ttype = tx.get("TransactionType")
    fc = free_cash(ctx)
    loan = ctx.loan

    add("type", ttype in ("LoanSet", "LoanPay", "Payment"), f"{ttype} is not allowed")
    if ttype == "LoanSet":
        add("parties", tx.get("Account") == ctx.lender and tx.get("Counterparty") == ctx.lockbox,
            "LoanSet must be Account=lender, Counterparty=lockbox")
    else:
        add("account", tx.get("Account") == ctx.lockbox, f"must be {ctx.lockbox}")
    fee = tx.get("Fee", "0")
    add("fee_cap", _is_drops(fee) and int(fee) <= ctx.max_fee_drops, f"fee {fee}, cap {ctx.max_fee_drops}")
    present = [f for f in FORBIDDEN_FIELDS if f in tx]
    add("no_forbidden_fields", not present, ", ".join(present))

    if ttype == "LoanSet":
        a = ctx.accepted
        add("borrower_accepted_terms", a is not None, "the borrower has not accepted any terms")
        add("no_open_loan", loan is None or not loan.active, "the lockbox already has an active loan")
        if a is not None:
            add("same_broker", tx.get("LoanBrokerID") == a.broker_id, "different loan broker")
            add("same_principal", _int(tx, "PrincipalRequested") == a.principal, f"{tx.get('PrincipalRequested')} vs {a.principal}")
            add("rate_not_higher", 0 <= _int(tx, "InterestRate") <= a.interest_rate_units, f"{tx.get('InterestRate')} vs {a.interest_rate_units}")
            add("same_schedule", tx.get("PaymentTotal") == a.payment_total and tx.get("PaymentInterval") == a.payment_interval
                and _int(tx, "GracePeriod") == a.grace_period, "payment schedule differs")
            add("fees_not_higher", 0 <= _int(tx, "LoanOriginationFee") <= a.origination_fee and 0 <= _int(tx, "LoanServiceFee") <= a.service_fee
                and not any(f in tx for f in LOANSET_BANNED_FEES), "extra or higher fees")

    elif ttype == "LoanPay":
        add("loan_known", loan is not None and tx.get("LoanID") == loan.loan_id, "LoanID does not match the lockbox loan")
        if loan is not None:
            flags = int(tx.get("Flags", 0)) & ~TF_FULLY_CANONICAL
            late = flags == TF_LOAN_LATE_PAYMENT and ctx.now > loan.next_due
            add("no_special_flags", flags == 0 or late, f"flags {flags} (only the late-payment flag is allowed, and only after the due date)")
            add("loan_open", loan.active, "loan is defaulted or fully paid")
            amt = tx.get("Amount")
            ok = _is_drops(amt) and loan.installment <= int(amt) <= loan.installment + ctx.pay_allowance_drops
            add("amount_matches_installment", ok, f"amount {amt}, installment {loan.installment} (+{ctx.pay_allowance_drops} allowed)")
            if _is_drops(amt):
                add("covered_by_balance", ctx.balance_drops - int(amt) >= ctx.account_reserve_drops, "would go below the account reserve")

    elif ttype == "Payment":
        dest = tx.get("Destination")
        flags = int(tx.get("Flags", 0)) & ~TF_FULLY_CANONICAL
        add("no_partial_payment", flags == 0, f"flags {flags}")
        amt = tx.get("Amount")
        add("xrp_only", _is_drops(amt) and int(amt) > 0, "Amount must be a positive XRP drops string")
        if dest == ctx.lender and dest not in ctx.allowlist:
            # recovery: only after default, only what is still owed
            add("recovery_after_default", loan is not None and loan.defaulted, "the lender can only be paid out of the lockbox after a default")
            if _is_drops(amt):
                cap = min(ctx.recovery_remaining, raw_spendable(ctx))
                add("within_recovery_cap", int(amt) <= cap, f"amount {amt}, recoverable {cap}")
        else:
            add("destination_allowlisted", dest in ctx.allowlist, f"{dest} is not on the allowlist")
            if loan is not None and loan.active:
                add("loan_current", not loan.impaired and ctx.now <= loan.next_due, "loan is impaired or past due, withdrawals are frozen")
            if in_default(ctx):
                add("not_in_default", False, "the loan defaulted and recovery is still pending")
            if _is_drops(amt):
                add("within_free_cash", int(amt) <= fc, f"amount {amt}, free cash {fc}")

    return Verdict(allowed=all(c.ok for c in checks), checks=checks, free_cash=fc)
