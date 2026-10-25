"""The Lockbox auditor's three read-only tools.

get_account_history  what the public ledger shows for a lockbox: its keys, and every payment it made
verify_lockbox       the published rulebook checked against the ledger, then every referee decision replayed
get_rulebook         the rulebook file and its sha256 fingerprint

The replay is lockbox.verify (the same code as scripts/verify.py and the dashboard). What the ledger cannot say
about the past (the clock, the loan as it stood) comes from the referee's published decision log in data/.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if not (HERE / "lockbox").is_dir():  # local development: use the repo's package; the build vendors a copy
    sys.path.insert(0, str(HERE.parent))

from lockbox import covenant as C  # noqa: E402
from lockbox import verify as V  # noqa: E402
from lockbox.buckets import _meta, _tx  # noqa: E402

import ledger  # noqa: E402

DATA = HERE / "data"
RULEBOOK_FILE = "lending-covenant-v1.json"
# plain English for rules verify.RULE_TEXT does not cover (release and loan-term checks)
EXTRA_RULE_TEXT = {
    "nothing_owed": "the account is handed back to the borrower only when the loan is repaid and nothing is owed",
    "loan_repaid": "the account is handed back only after the loan is fully repaid",
    "release_to_registered_key": "the account can only be handed back to the borrower's registered key",
    "amount_matches_installment": "a repayment must be the installment the ledger asks for",
    "invoice_memo": "a supplier payment must carry the approved invoice number",
    "within_payee_cap": "a supplier can be paid only up to the amount the lender approved",
}
LOAN_MANAGE_ACTIONS = {0x00010000: "default the loan", 0x00020000: "mark the loan impaired (late)", 0x00040000: "clear the impaired mark"}
TX_TYPES = ("Payment", "LoanPay", "LoanSet", "LoanManage", "SetRegularKey", "SignerListSet", "AccountSet")
LATE_FLAG = 0x00040000


def _rulebook_path(address: str | None = None) -> Path:
    """A lockbox whose loan was signed under its own copy of the rulebook (different terms or suppliers) has it
    next to its decision log. Otherwise the published rulebook."""
    if address and (DATA / "lockboxes" / address / "rulebook.json").is_file():
        return DATA / "lockboxes" / address / "rulebook.json"
    for p in (DATA / "rulebooks" / RULEBOOK_FILE, HERE.parent / "rulebooks" / RULEBOOK_FILE):
        if p.is_file():
            return p
    raise FileNotFoundError(f"{RULEBOOK_FILE} is not bundled")


def _decisions(address: str) -> list[dict]:
    p = DATA / "lockboxes" / address / "decisions.jsonl"
    if not p.is_file():
        return []
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return [e for e in rows if e.get("kind") == "verdict" and "decision" in e]


def known_lockboxes() -> list[str]:
    d = DATA / "lockboxes"
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


class Devnet:
    """The public XRPL Devnet ledger plus the referee's published decision logs in data/."""
    network = "XRPL Devnet"

    def account_info(self, address: str) -> dict:
        return ledger.account_info(address)

    def account_tx(self, address: str) -> list[dict]:
        return ledger.account_tx(address)

    def ledger_entry(self, index: str) -> dict:
        return ledger.ledger_entry(index)

    def decisions(self, address: str) -> list[dict]:
        return _decisions(address)

    def rulebook(self, address: str | None = None) -> tuple[bytes, str]:
        path = _rulebook_path(address)
        return path.read_bytes(), (path.name if path.name != "rulebook.json" else f"{address}/rulebook.json")

    def explorer(self, tx_hash: str | None) -> str | None:
        return ledger.EXPLORER + tx_hash if tx_hash else None


class Snapshot:
    """A frozen, read-only copy of one lockbox: its account, history, the broker entries it points at, the
    referee's decision log and the rulebook. The dashboard takes one of its simulated ledger for each question."""

    def __init__(self, network: str, lockbox: str, info: dict, history: list[dict], entries: dict[str, dict],
                 decisions: list[dict], rulebook_raw: bytes, rulebook_name: str = RULEBOOK_FILE):
        self.network, self.lockbox = network, lockbox
        self._info, self._history, self._entries = info, history, entries
        self._decisions = [e for e in decisions if e.get("kind") == "verdict" and "decision" in e]
        self._rulebook = (rulebook_raw, rulebook_name)

    def account_info(self, address: str) -> dict:
        return dict(self._info) if address == self.lockbox else {}

    def account_tx(self, address: str) -> list[dict]:
        return list(self._history) if address == self.lockbox else []

    def ledger_entry(self, index: str) -> dict:
        return dict(self._entries.get(index, {}))

    def decisions(self, address: str) -> list[dict]:
        return list(self._decisions) if address == self.lockbox else []

    def rulebook(self, address: str | None = None) -> tuple[bytes, str]:
        return self._rulebook

    def explorer(self, tx_hash: str | None) -> str | None:
        return None  # a simulated ledger has no explorer


DEVNET = Devnet()


def _xrp(drops) -> str:
    """Amounts in XRP, with drops spelled out for small ones, so a model never has to convert units itself."""
    if isinstance(drops, str) and drops.isdigit():
        d = int(drops)
        return f"{d / 1e6:.2f} XRP" if d >= 10_000 else f"{d / 1e6:.6f} XRP ({d} drops)"
    return ""


def _memos(tx: dict) -> list[str]:
    out = []
    for m in tx.get("Memos", []):
        data = m.get("Memo", {}).get("MemoData", "")
        try:
            out.append(bytes.fromhex(data).decode())
        except ValueError:
            pass
    return out


def _loan_manage_action(tx: dict) -> str:
    flags = int(tx.get("Flags", 0))
    return next((v for k, v in LOAN_MANAGE_ACTIONS.items() if flags & k), "unknown")


def _signers(tx: dict) -> list[str]:
    if tx.get("TransactionType") == "LoanSet":
        cs = tx.get("CounterpartySignature", {}).get("Signers", [])
        return [s["Signer"]["Account"] for s in cs]
    return [s["Signer"]["Account"] for s in tx.get("Signers", [])]


def _roles(address: str, history: list[dict], doc: dict, decisions: list[dict]) -> dict[str, str]:
    roles = {address: "lockbox"}
    for e in history:  # the signer list the lockbox was locked with: weight 2 is the referee
        tx = _tx(e)
        if tx.get("TransactionType") == "SignerListSet" and tx.get("Account") == address:
            for s in tx.get("SignerEntries", []):
                a, w = s["SignerEntry"]["Account"], int(s["SignerEntry"]["SignerWeight"])
                roles.setdefault(a, "referee" if w == 2 else "signer")
        if tx.get("TransactionType") == "LoanSet" and tx.get("Counterparty") == address:
            roles[tx["Account"]] = "lender"
    for a in doc.get("approved_suppliers", {}):
        roles[a] = "approved supplier"
    for ev in decisions:
        for a in ev["decision"]["inputs"].get("allowlist") or []:
            roles[a] = "owner's wallet"
        b = ev["decision"]["inputs"].get("borrower")
        if b:
            roles[b] = "borrower"
    return roles


def get_account_history(address: str, limit: int = 60, src=DEVNET) -> dict:
    """What the ledger shows for this account: its keys and the payments it made."""
    info = src.account_info(address)
    if not info:
        return {"address": address, "exists": False, "network": src.network}
    history = src.account_tx(address)
    decisions = src.decisions(address)
    roles = _roles(address, history, json.loads(src.rulebook(address)[0]), decisions)
    lists = info.get("signer_lists") or []
    signers = {e["SignerEntry"]["Account"]: int(e["SignerEntry"]["SignerWeight"]) for e in (lists[0]["SignerEntries"] if lists else [])}
    txs, received = [], 0
    for e in history:
        tx, meta = _tx(e), _meta(e)
        if tx.get("TransactionType") == "Payment" and tx.get("Destination") == address:
            received += 1  # buyer payments in: counted, not listed
            continue
        if tx.get("TransactionType") not in TX_TYPES:
            continue
        h = e.get("hash") or tx.get("hash")
        dest = tx.get("Destination")
        txs.append({
            "type": tx.get("TransactionType"),
            "result": meta.get("TransactionResult"),
            "amount": _xrp(tx.get("Amount") or tx.get("PrincipalRequested")),
            "to": f"{roles.get(dest, 'unknown')} {dest}" if dest else None,
            "memos": _memos(tx),
            "late_payment_flag": bool(int(tx.get("Flags", 0)) & LATE_FLAG) if tx.get("TransactionType") == "LoanPay" else None,
            "loan_manage_action": _loan_manage_action(tx) if tx.get("TransactionType") == "LoanManage" else None,
            "signed_by": [f"{roles.get(a, 'unknown')} {a}" for a in _signers(tx)],
            "explorer": src.explorer(h),
        })
    return {
        "address": address,
        "exists": True,
        "network": src.network,
        "balance": _xrp(info.get("Balance")),
        "master_key_disabled": bool(int(info.get("Flags", 0)) & 0x00100000),
        "regular_key": info.get("RegularKey"),
        "signers_now": {f"{roles.get(a, 'unknown')} {a}": w for a, w in signers.items()},
        "quorum_now": lists[0].get("SignerQuorum") if lists else None,
        "payments_received": received,
        "transactions": txs[:limit],  # newest first
        "has_published_decision_log": bool(decisions),
    }


def _key(tx: dict) -> tuple:
    return (tx.get("Account"), tx.get("Sequence"), tx.get("TransactionType"), tx.get("Destination"),
            tx.get("Amount"), tx.get("LoanID"), tx.get("RegularKey"))


def verify_lockbox(address: str, src=DEVNET) -> dict:
    """Check the published rulebook against the ledger, then replay every referee decision for this lockbox."""
    decisions = src.decisions(address)
    raw, rulebook_name = src.rulebook(address)
    doc = json.loads(raw)
    history = src.account_tx(address)
    info = src.account_info(address)
    if not info:
        return {"address": address, "exists": False, "network": src.network}
    loanset = next((_tx(e) for e in history if _tx(e).get("TransactionType") == "LoanSet"
                    and _tx(e).get("Counterparty") == address and _meta(e).get("TransactionResult") == "tesSUCCESS"), None)
    broker_data = src.ledger_entry(loanset["LoanBrokerID"]).get("Data") if loanset else None
    problems = V.rulebook_problems(doc, broker_data, loanset.get("Data") if loanset else None)
    rows = V.replay(decisions, history, int(info["Balance"]), address, doc,
                    lambda broker: src.ledger_entry(broker).get("Data"))
    roles = _roles(address, history, doc, decisions)
    V.label_rows(rows, decisions, roles)
    on_ledger = {}  # the ledger transaction a signed decision became. A refused one shares its Sequence with the
    # next payment that was sent, so match on the whole payment, not just the sequence
    for e in history:
        tx = _tx(e)
        on_ledger.setdefault(_key(tx), (e.get("hash") or tx.get("hash"), _meta(e).get("TransactionResult"),
                                         bool(int(tx.get("Flags", 0)) & LATE_FLAG)))
    out_rows, first_seen = [], {}
    for r, ev in zip(rows, decisions):
        tx = ev["decision"]["tx"]
        h, result, late = on_ledger.get(_key(tx), (None, None, False)) if ev.get("allowed") else (None, None, False)
        same_as = first_seen.get(h) if h else None  # a preview of a decision and the decision itself share one transaction
        if h and same_as is None:
            first_seen[h] = r.n
        out_rows.append({
            "n": r.n,
            "transaction": r.tx,
            "referee": "signed" if r.logged == "allowed" else "refused",
            "deciding_rule": r.rule,
            "rule_says": V.RULE_TEXT.get(r.rule) or EXTRA_RULE_TEXT.get(r.rule, ""),
            "owner_could_take": _xrp(str(ev["free_cash"])) if ev.get("free_cash") is not None and tx.get("TransactionType") == "Payment" else None,
            "replay_says": "signed" if r.replayed == "allowed" else r.replayed,
            "matches": r.matches,
            "problems": r.problems,
            "dry_run": bool(ev.get("dry_run")),
            "same_transaction_as_decision": same_as,
            "on_ledger": {"result": result, "explorer": src.explorer(h), "paid_late": late} if h else None,
        })
    return {
        "address": address,
        "network": src.network,
        "rulebook": {"name": doc.get("name"), "version": doc.get("version"), "fingerprint": doc.get("fingerprint"),
                     "file": rulebook_name},
        "on_ledger_fingerprint": broker_data,
        "rulebook_matches_ledger": not problems,
        "rulebook_problems": problems,
        "decisions": out_rows,
        "summary": _summary(out_rows, decisions),
        "signed": sum(1 for r in out_rows if r["referee"] == "signed"),
        "refused": sum(1 for r in out_rows if r["referee"] == "refused"),
        "all_match": bool(out_rows) and all(r["matches"] for r in out_rows),
        "has_published_decision_log": bool(decisions),
        "note": "A refused payment never reaches the ledger: the referee does not sign it, so it has no transaction hash.",
    }


def _summary(rows: list[dict], decisions: list[dict]) -> dict:
    """Counts per transaction type, so nobody has to count rows. A decision reviewed twice counts once."""
    out: dict[str, dict] = {}
    for r, ev in zip(rows, decisions):
        if r.get("same_transaction_as_decision"):
            continue
        t = ev["decision"]["tx"].get("TransactionType", "?")
        c = out.setdefault(t, {"decisions": 0, "signed": 0, "refused": 0, "went_through_on_ledger": 0,
                               "rejected_by_ledger": 0, "paid_late": 0})
        c["decisions"] += 1
        c["signed" if r["referee"] == "signed" else "refused"] += 1
        led = r.get("on_ledger")
        if led:
            c["went_through_on_ledger" if led["result"] == "tesSUCCESS" else "rejected_by_ledger"] += 1
            c["paid_late"] += 1 if led.get("paid_late") and led["result"] == "tesSUCCESS" else 0
    return out


def get_rulebook(src=DEVNET) -> dict:
    """The published rulebook and its fingerprints: sha256 of the file, and the rulebook fingerprint recomputed
    from its name, version and rules (the value written in the loan broker's Data field)."""
    raw = src.rulebook()[0]
    doc = json.loads(raw)
    recomputed = C.rulebook_fingerprint(doc.get("config", {}))
    return {
        "rulebook": doc,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "fingerprint": doc.get("fingerprint"),
        "fingerprint_recomputed": recomputed,
        "fingerprint_consistent": recomputed.upper() == str(doc.get("fingerprint", "")).upper(),
        "problems": C.rulebook_document_problems(doc),
    }
