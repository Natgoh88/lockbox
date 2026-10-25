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


def _xrp(drops) -> str:
    if isinstance(drops, str) and drops.isdigit():
        d = int(drops)
        return f"{d / 1e6:.2f} XRP" if d >= 10_000 else f"{d} drops"
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


def get_account_history(address: str, limit: int = 60) -> dict:
    """What the public XRPL Devnet ledger shows for this account: its keys and the payments it made."""
    info = ledger.account_info(address)
    if not info:
        return {"address": address, "exists": False, "network": "XRPL Devnet"}
    history = ledger.account_tx(address)
    decisions = _decisions(address)
    roles = _roles(address, history, json.loads(_rulebook_path(address).read_text()), decisions)
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
            "signed_by": [f"{roles.get(a, 'unknown')} {a}" for a in _signers(tx)],
            "explorer": ledger.EXPLORER + h if h else None,
        })
    return {
        "address": address,
        "exists": True,
        "network": "XRPL Devnet",
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


def verify_lockbox(address: str) -> dict:
    """Check the published rulebook against the ledger, then replay every referee decision for this lockbox."""
    decisions = _decisions(address)
    path = _rulebook_path(address)
    doc = json.loads(path.read_text())
    history = ledger.account_tx(address)
    info = ledger.account_info(address)
    if not info:
        return {"address": address, "exists": False, "network": "XRPL Devnet"}
    loanset = next((_tx(e) for e in history if _tx(e).get("TransactionType") == "LoanSet"
                    and _tx(e).get("Counterparty") == address and _meta(e).get("TransactionResult") == "tesSUCCESS"), None)
    broker_data = ledger.ledger_entry(loanset["LoanBrokerID"]).get("Data") if loanset else None
    problems = V.rulebook_problems(doc, broker_data, loanset.get("Data") if loanset else None)
    rows = V.replay(decisions, history, int(info["Balance"]), address, doc,
                    lambda broker: ledger.ledger_entry(broker).get("Data"))
    roles = _roles(address, history, doc, decisions)
    V.label_rows(rows, decisions, roles)
    on_ledger = {}  # the ledger transaction a signed decision became. A refused one shares its Sequence with the
    # next payment that was sent, so match on the whole payment, not just the sequence
    for e in history:
        tx = _tx(e)
        on_ledger.setdefault(_key(tx), (e.get("hash") or tx.get("hash"), _meta(e).get("TransactionResult")))
    out_rows, first_seen = [], {}
    for r, ev in zip(rows, decisions):
        tx = ev["decision"]["tx"]
        h, result = on_ledger.get(_key(tx), (None, None)) if ev.get("allowed") else (None, None)
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
            "on_ledger": {"result": result, "explorer": ledger.EXPLORER + h} if h else None,
        })
    return {
        "address": address,
        "network": "XRPL Devnet",
        "rulebook": {"name": doc.get("name"), "version": doc.get("version"), "fingerprint": doc.get("fingerprint"),
                     "file": path.name if path.name != "rulebook.json" else f"{address}/rulebook.json"},
        "on_ledger_fingerprint": broker_data,
        "rulebook_matches_ledger": not problems,
        "rulebook_problems": problems,
        "decisions": out_rows,
        "signed": sum(1 for r in out_rows if r["referee"] == "signed"),
        "refused": sum(1 for r in out_rows if r["referee"] == "refused"),
        "all_match": bool(out_rows) and all(r["matches"] for r in out_rows),
        "has_published_decision_log": bool(decisions),
        "note": "A refused payment never reaches the ledger: the referee does not sign it, so it has no transaction hash.",
    }


def get_rulebook() -> dict:
    """The published rulebook and its fingerprints: sha256 of the file, and the rulebook fingerprint recomputed
    from its name, version and rules (the value written in the loan broker's Data field)."""
    path = _rulebook_path()
    raw = path.read_bytes()
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
