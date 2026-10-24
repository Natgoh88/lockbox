"""Buckets: where every drop in the lockbox belongs, rebuilt from the ledger alone.

replay() walks the lockbox's account_tx history oldest first and tags money as it moves:

  bond               the borrower's own XRP sent in with a "bond" memo, locked until the loan is settled
  unspent principal  loan principal not yet paid to an approved payee, plus anything an approved payee sends back
  holdback           a share of each revenue payment, kept for repayment while a loan is open
  earned             everything else: buyer payments and the business's own float

Money going out comes off the pools in a fixed order. A LoanPay uses holdback, then earned money,
then principal, so the loan never repays itself while the business pockets the revenue. A payment to
an approved payee uses principal first. A recovery payment to the lender takes the bond first.

Then the rulebook's hold-backs are carved out of what is there: the account reserve and the fee
buffer, then the next installment(s). What is left of earned money is the earned free cash, the only
money the borrower may send to its own wallet.

Pure: same history, same balance, same config, same loan facts -> same buckets. Anyone can recompute it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

BOND_MEMO = "bond"


@dataclass(frozen=True)
class Payee:
    """A lender-approved destination for loan principal (a supplier), with a total cap and the invoice it pays."""
    cap_drops: int
    invoice_ref: str


@dataclass(frozen=True)
class LoanFacts:
    """The loan as the ledger reports it now."""
    installment: int = 0  # periodic payment plus service fee, drops
    payment_remaining: int = 0
    active: bool = False  # not defaulted, payments remain
    settled: bool = True  # nothing owed: no loan, repaid, or recovery finished

    @classmethod
    def none(cls) -> "LoanFacts":
        return cls()


@dataclass
class Config:
    lockbox: str
    lender: str = ""
    approved_payees: dict[str, Payee] = field(default_factory=dict)
    account_reserve: int = 0
    fee_buffer: int = 0
    reserve_installments: int = 1
    holdback_bps: int = 0  # share of each revenue payment held back while a loan is open, in basis points


@dataclass(frozen=True)
class Buckets:
    balance: int
    reserve: int
    fee_buffer: int
    bond: int
    installment_reserve: int
    holdback: int  # held back beyond the installment reserve
    unspent_principal: int
    earned_free_cash: int
    shortfall: int  # how far the balance falls short of everything held back, 0 when healthy
    paid_to_payees: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.__dict__.items()}


# ---- reading one history entry ----

def _tx(e: dict) -> dict:
    return e.get("tx_json") or e.get("tx") or {}


def _meta(e: dict) -> dict:
    return e.get("meta") or e.get("metaData") or {}


def _drops(v) -> int:
    return int(v) if isinstance(v, str) and v.isdigit() else 0


def memo_texts(tx: dict) -> list[str]:
    """Every MemoType and MemoData on the transaction, decoded from hex. Undecodable memos are skipped."""
    out = []
    for m in tx.get("Memos") or []:
        memo = m.get("Memo", {})
        for k in ("MemoType", "MemoData"):
            v = memo.get(k)
            if v:
                try:
                    out.append(bytes.fromhex(v).decode("utf-8"))
                except ValueError:
                    pass
    return out


def _result(e: dict) -> str:
    return _meta(e).get("TransactionResult", "")


def balance_delta(e: dict, lockbox: str) -> int:
    """How much this entry changed the lockbox's XRP balance. Uses the AccountRoot change in the metadata
    when the ledger gives it, otherwise works it out from the transaction fields."""
    meta = _meta(e)
    for n in meta.get("AffectedNodes", []):
        node = n.get("ModifiedNode") or n.get("CreatedNode") or n.get("DeletedNode") or {}
        if node.get("LedgerEntryType") != "AccountRoot":
            continue
        final = node.get("FinalFields") or node.get("NewFields") or {}
        if final.get("Account") != lockbox:
            continue
        prev = node.get("PreviousFields") or {}
        if "Balance" not in prev and "CreatedNode" not in n:
            return 0
        return _drops(final.get("Balance", "0")) - _drops(prev.get("Balance", "0"))
    tx = _tx(e)
    code = _result(e)
    if not (code.startswith("tes") or code.startswith("tec")):
        return 0
    ok = code.startswith("tes")
    d = 0
    ttype = tx.get("TransactionType")
    if tx.get("Account") == lockbox:
        d -= _drops(tx.get("Fee", "0"))
        if ok and ttype in ("Payment", "LoanPay"):
            d -= _drops(meta.get("delivered_amount") or tx.get("Amount"))
    if ok and ttype == "Payment" and tx.get("Destination") == lockbox and tx.get("Account") != lockbox:
        d += _drops(meta.get("delivered_amount") or tx.get("Amount"))
    if ok and ttype == "LoanSet" and tx.get("Counterparty") == lockbox:
        d += _drops(tx.get("PrincipalRequested")) - _drops(tx.get("LoanOriginationFee", "0"))
    return d


def _ordered(entries: list[dict]) -> list[dict]:
    """Oldest first. account_tx returns newest first on a real ledger; the sim appends in order."""
    def key(ie):
        i, e = ie
        li = e.get("ledger_index", _tx(e).get("ledger_index"))
        return (int(li) if li is not None else -1, int(_meta(e).get("TransactionIndex", 0)), i)
    has_ledger = any(e.get("ledger_index", _tx(e).get("ledger_index")) is not None for e in entries)
    return [e for _, e in sorted(enumerate(entries), key=key)] if has_ledger else list(entries)


# ---- the replay ----

class _Pools:
    def __init__(self, start: int):
        self.p = {"earned": start, "holdback": 0, "principal": 0, "bond": 0}

    def add(self, pool: str, drops: int) -> None:
        self.p[pool] += drops

    def draw(self, drops: int, order: tuple[str, ...]) -> None:
        for pool in order:
            take = min(drops, max(0, self.p[pool]))
            self.p[pool] -= take
            drops -= take
        self.p["earned"] -= drops  # more went out than was tagged: the history is incomplete, earned absorbs it


ANY_ORDER = ("earned", "holdback", "principal", "bond")
LOANPAY_ORDER = ("holdback", "earned", "principal", "bond")
PAYEE_ORDER = ("principal", "earned", "holdback", "bond")
RECOVERY_ORDER = ("bond", "holdback", "earned", "principal")


def replay(entries: list[dict], balance: int, cfg: Config, loan: LoanFacts) -> Buckets:
    lb = cfg.lockbox
    ordered = _ordered([e for e in entries if e.get("validated", True)])
    deltas = [balance_delta(e, lb) for e in ordered]
    pools = _Pools(balance - sum(deltas))
    paid: dict[str, int] = {}
    loan_seen = False

    for e, d in zip(ordered, deltas):
        if d == 0:
            continue
        tx = _tx(e)
        ttype = tx.get("TransactionType")
        ok = _result(e).startswith("tes")
        if d > 0:
            sender = tx.get("Account")
            if ok and ttype == "LoanSet":
                # the referee only signs a LoanSet once the last loan is settled, so its leftovers are the borrower's now
                pools.add("earned", pools.p["principal"] + pools.p["holdback"])
                pools.p["principal"] = pools.p["holdback"] = 0
                pools.add("principal", d)
                loan_seen = True
            elif ok and ttype == "Payment" and BOND_MEMO in memo_texts(tx):
                pools.add("bond", d)
            elif ok and ttype == "Payment" and sender in cfg.approved_payees:
                pools.add("principal", d)  # a refund from a supplier is still the lender's principal
            else:
                held = d * cfg.holdback_bps // 10_000 if loan_seen else 0
                pools.add("holdback", held)
                pools.add("earned", d - held)
            continue
        out = -d
        dest = tx.get("Destination")
        if ok and ttype == "LoanPay":
            order = LOANPAY_ORDER
        elif ok and ttype == "Payment" and dest in cfg.approved_payees:
            amount = out - _drops(tx.get("Fee", "0"))
            paid[dest] = paid.get(dest, 0) + amount
            pools.draw(_drops(tx.get("Fee", "0")), ANY_ORDER)
            out, order = amount, PAYEE_ORDER
        elif ok and ttype == "Payment" and dest == cfg.lender:
            order = RECOVERY_ORDER
        else:
            order = ANY_ORDER
        pools.draw(out, order)

    # true up to the real balance (rounding in a truncated history), then settle
    p = pools.p
    p["earned"] = balance - p["holdback"] - p["principal"] - p["bond"]
    if loan.settled and not loan.active:
        p["earned"] += p["holdback"] + p["principal"]
        p["holdback"] = p["principal"] = 0
        if loan_seen:  # a bond posted before any loan stays locked until that loan is settled
            p["earned"] += p["bond"]
            p["bond"] = 0

    def carve(need: int, order: tuple[str, ...]) -> int:
        for pool in order:
            take = min(need, max(0, p[pool]))
            p[pool] -= take
            need -= take
        return need

    inst = loan.installment * min(cfg.reserve_installments, loan.payment_remaining) if loan.active else 0
    short = carve(cfg.account_reserve + cfg.fee_buffer, ("earned", "holdback", "principal", "bond"))
    short += carve(inst, ("holdback", "earned", "principal"))
    return Buckets(balance=balance, reserve=cfg.account_reserve, fee_buffer=cfg.fee_buffer, bond=max(0, p["bond"]),
                   installment_reserve=inst, holdback=max(0, p["holdback"]), unspent_principal=max(0, p["principal"]),
                   earned_free_cash=max(0, p["earned"]), shortfall=short, paid_to_payees=paid)


def from_balance(balance: int, cfg: Config, loan: LoanFacts) -> Buckets:
    """No history: every drop counts as earned. Used when the caller has only a balance."""
    return replay([], balance, cfg, loan)
