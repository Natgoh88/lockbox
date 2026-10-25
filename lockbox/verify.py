"""Check the referee after the fact: did it follow its published rules?

1. The rulebook file must be consistent with itself, and its fingerprint must equal the broker's Data field on
   the ledger. With loan terms in it, its agreement hash must equal the LoanSet's Data field.
2. Every verdict the referee logged is replayed: the buckets are rebuilt from the lockbox's ledger history up to
   the point of the decision, the rules come from the rulebook file, and covenant.evaluate runs again. The
   replayed verdict and deciding rule must match what the referee logged.

What the ledger cannot tell us about the past (the clock, the loan as it stood, the referee's outstanding
approval) is taken from the decision record the referee logged with each verdict.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from . import buckets as bk
from . import covenant as C
from .covenant import AcceptedTerms, Acceptance, ApprovalState, CovenantContext, LoanView, Verdict

# CovenantContext fields that come from the rulebook config, not from the decision record
CONFIG_FIELDS = ("max_fee_drops", "fee_buffer_drops", "pay_allowance_drops", "reserve_installments", "holdback_bps",
                 "freeze_after_short_intervals")
# fields rebuilt from the ledger or the rulebook file at replay time
NOT_RECORDED = {"buckets", "balance_drops", "rulebook_hash", "broker_data", *CONFIG_FIELDS}


# a freeze outranks every other reason for a refusal: it is why nothing can go out, whatever the amount
FREEZE_CHECKS = ("not_in_default", "loan_current", "revenue_covers_installment")


def deciding_rule(v: Verdict) -> str:
    """For a refusal, the freeze if the account is frozen, otherwise the first failed check; for an approval, the
    last (most specific) check that passed. Explanation only: the verdict needs every check either way."""
    bad = [c.name for c in v.checks if not c.ok]
    frozen = [n for n in bad if n in FREEZE_CHECKS]
    if frozen:
        return frozen[0]
    if bad:
        return bad[0]
    return v.checks[-1].name if v.checks else ""


def record_inputs(ctx: CovenantContext) -> dict:
    """The parts of the referee's context that are not rebuilt from the ledger, as plain JSON."""
    out = {}
    for f in fields(ctx):
        if f.name in NOT_RECORDED:
            continue
        v = getattr(ctx, f.name)
        if f.name == "allowlist":
            v = sorted(v)
        elif f.name == "approved_payees":
            v = {a: {"cap_drops": p.cap_drops, "invoice_ref": p.invoice_ref} for a, p in v.items()}
        elif v is not None and hasattr(v, "__dataclass_fields__"):
            v = asdict(v)
        out[f.name] = v
    return out


def _inputs_to_context(inputs: dict, config: dict, buckets: bk.Buckets, balance: int, fingerprint: str,
                       broker_data: str | None) -> CovenantContext:
    d = dict(inputs)
    d["allowlist"] = set(d.get("allowlist") or [])
    d["approved_payees"] = C.payees_from_json(d.get("approved_payees") or {})
    for name, cls in (("loan", LoanView), ("accepted", AcceptedTerms), ("acceptance", Acceptance), ("approval", ApprovalState)):
        if d.get(name) is not None:
            d[name] = cls(**d[name])
    known = {f.name for f in fields(CovenantContext)}
    d = {k: v for k, v in d.items() if k in known}
    return CovenantContext(**d, **{k: config[k] for k in CONFIG_FIELDS}, buckets=buckets, balance_drops=balance,
                           rulebook_hash=fingerprint, broker_data=broker_data)


def _hash(e: dict) -> str | None:
    return e.get("hash") or bk._tx(e).get("hash")


@dataclass
class Row:
    n: int
    tx: str
    logged: str  # "allowed" or "refused"
    rule: str
    replayed: str
    replayed_rule: str
    problems: list[str]

    @property
    def matches(self) -> bool:
        return not self.problems


def rulebook_problems(doc: dict, broker_data: str | None, loanset_data: str | None = None) -> list[str]:
    out = C.rulebook_document_problems(doc)
    if str(broker_data or "").upper() != str(doc.get("fingerprint", "")).upper():
        out.append(f"fingerprint {doc.get('fingerprint')} is not the one on the ledger ({broker_data or 'empty'})")
    if loanset_data is not None and "agreement_hash" in doc and str(loanset_data).upper() != str(doc["agreement_hash"]).upper():
        out.append("agreement hash in the file is not the one in the LoanSet Data field")
    if doc.get("config", {}).get("approval_window") != C.APPROVAL_WINDOW:
        out.append("approval window in the file differs from the referee's code")
    return out


def replay(decisions: list[dict], history: list[dict], balance_now: int, lockbox: str, doc: dict,
           broker_data_of) -> list[Row]:
    """decisions: the referee's verdict events with a decision record. history: the lockbox's account_tx.
    broker_data_of(broker_id) reads a broker's Data field from the ledger."""
    entries = bk._ordered([e for e in history if e.get("validated", True)])
    deltas = [bk.balance_delta(e, lockbox) for e in entries]
    index = {_hash(e): i for i, e in enumerate(entries)}
    config, fingerprint = doc.get("config", {}), doc.get("fingerprint", "")
    file_payees = doc.get("approved_suppliers", {})
    rows = []
    for n, ev in enumerate(decisions, 1):
        rec = ev["decision"]
        tx, inputs = rec["tx"], rec["inputs"]
        problems = []
        seen = rec.get("seen")
        if seen is not None and seen not in index:
            problems.append("the history point it was decided at is not on the ledger")
            upto = -1
        else:
            upto = index[seen] if seen is not None else -1
        prefix = entries[:upto + 1]
        balance = balance_now - sum(deltas[upto + 1:])
        if balance != rec.get("balance"):
            problems.append(f"balance from the ledger {balance}, the referee logged {rec.get('balance')}")
        extra = set(inputs.get("approved_payees") or {}) - set(file_payees)
        if extra:
            problems.append("used a supplier that is not in the rulebook file")
        loan = LoanView(**inputs["loan"]) if inputs.get("loan") else None
        cfg = bk.Config(lockbox=lockbox, lender=inputs["lender"], approved_payees=C.payees_from_json(inputs.get("approved_payees") or {}),
                        account_reserve=inputs["account_reserve_drops"], fee_buffer=config.get("fee_buffer_drops", 0),
                        reserve_installments=config.get("reserve_installments", 1), holdback_bps=config.get("holdback_bps", 0))
        b = bk.replay(prefix, balance, cfg, C.loan_facts(loan, inputs.get("recovery_remaining", 0)), now=inputs["now"])
        broker_data = broker_data_of(tx.get("LoanBrokerID", "")) if tx.get("TransactionType") == "LoanSet" else None
        try:
            ctx = _inputs_to_context(inputs, config, b, balance, fingerprint, broker_data)
            v = C.evaluate(tx, ctx)
            replayed, rule = ("allowed" if v.allowed else "refused"), deciding_rule(v)
        except (KeyError, TypeError) as e:
            replayed, rule = "error", f"cannot replay: {e}"
        logged = "allowed" if ev.get("allowed") else "refused"
        if replayed != logged:
            problems.append(f"the rules say {replayed}, the referee logged {logged}")
        if rule != ev.get("rule"):
            problems.append(f"deciding rule replays as {rule}, the referee logged {ev.get('rule')}")
        rows.append(Row(n=n, tx=describe(tx, lockbox), logged=logged, rule=ev.get("rule") or "", replayed=replayed,
                        replayed_rule=rule, problems=problems))
    return rows


def role_labels(sc, doc: dict) -> dict[str, str]:
    """Address -> role for the table: the owner's payout wallet, the lockbox, the lender, and every approved
    supplier in the published rulebook file (so a restarted process with an empty list still labels them)."""
    labels = {a: "supplier" for a in doc.get("approved_suppliers", {})}
    labels.update({a: "supplier" for a in sc.params.approved_payees})
    labels[sc.payout] = "owner wallet"
    labels[sc.lockbox] = "lockbox"
    if sc.servicer:
        labels[sc.servicer.address] = "lender"
    return labels


def label_rows(rows: list[Row], decisions: list[dict], labels: dict[str, str]) -> None:
    for r, ev in zip(rows, decisions):
        dest = ev["decision"]["tx"].get("Destination")
        if dest in labels:
            r.tx = r.tx.split(" to ")[0] + f" to {labels[dest]}"


def describe(tx: dict, lockbox: str) -> str:
    t = tx.get("TransactionType", "?")
    amt = tx.get("Amount") or tx.get("PrincipalRequested")
    xrp = ""
    if isinstance(amt, str) and amt.isdigit():
        xrp = f" {int(amt) / 1e6:.2f} XRP" if int(amt) >= 10_000 else f" {int(amt)} drops"
    dest = tx.get("Destination")
    to = f" to {dest[:8]}..." if dest else ""
    return f"{t}{xrp}{to}"


RULE_TEXT = {
    "within_free_cash": "only earned free cash (and the owner's own deposit) may go to the owner's wallet",
    "within_principal": "loan money may go to an approved supplier, up to its cap, with the invoice memo",
    "loan_current": "no withdrawals while a payment is past due",
    "revenue_covers_installment": "no withdrawals after revenue fell short of the installment two intervals running",
    "covered_by_balance": "repayment comes first, as long as the fee reserve stays",
    "within_recovery_cap": "after a default the lender is paid what is still owed, no more",
    "not_in_default": "after a default nothing goes to the owner until the lender is repaid",
    "broker_rulebook_hash": "the loan is under this rulebook (fingerprint on the ledger) and matches the signed terms",
    "ends_before_redemption": "the loan, grace included, ends before depositors can redeem",
    "destination_allowlisted": "money only goes to the owner's own wallet, approved suppliers or the lender",
}


def legend(rows: list[Row]) -> str:
    used = dict.fromkeys(r.rule for r in rows if r.rule in RULE_TEXT)
    return "\n".join(f"  {k:<28} {RULE_TEXT[k]}" for k in used)


def table(rows: list[Row]) -> str:
    head = f"{'#':>3}  {'transaction':<34} {'verdict':<8} {'deciding rule':<28} matches"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(f"{r.n:>3}  {r.tx[:34]:<34} {r.logged:<8} {r.rule[:28]:<28} {'yes' if r.matches else 'NO'}")
        for p in r.problems:
            lines.append(f"{'':>5}  ! {p}")
    return "\n".join(lines)
