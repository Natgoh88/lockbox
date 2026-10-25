"""Plain-English answers built only from the tools' results. Used when no model is available (no AWS
credentials), and as the source of truth the model's answers are checked against in tests."""
from __future__ import annotations

import re

import auditor

ADDRESS = re.compile(r"\br[1-9A-HJ-NP-Za-km-z]{24,34}\b")
TX_HASH = re.compile(r"\b[0-9A-Fa-f]{64}\b")


def clean(text: str) -> str:
    """House style: no em or en dashes."""
    return text.replace(" — ", ", ").replace("—", ", ").replace("–", "-")


def _short(a: str) -> str:
    return f"{a[:6]}...{a[-4:]}" if a and len(a) > 12 else a


def _decision_line(r: dict) -> str:
    verb = "signed" if r["referee"] == "signed" else "REFUSED"
    line = f"{r['n']}. {r['transaction']}: the referee {verb} it. Deciding rule: {r['deciding_rule']}"
    if r["rule_says"]:
        line += f" ({r['rule_says']})"
    line += "."
    if r.get("same_transaction_as_decision"):
        line += f" This is the same transaction as decision {r['same_transaction_as_decision']}: the referee reviewed it twice before it was sent."
    led = r.get("on_ledger")
    if r["referee"] == "refused":
        if r.get("owner_could_take") and r["deciding_rule"] == "within_free_cash":
            line += f" At that moment the owner could take at most {r['owner_could_take']}; the rest was loan money or held back for repayment."
        line += " Because the referee refused, it was never signed, so it is not on the ledger."
    elif led and led["result"] == "tesSUCCESS":
        line += f" On the ledger: {led['explorer']}"
    elif led:
        line += f" The ledger then rejected it with {led['result']} ({led['explorer']})."
        if led["result"] == "tecEXPIRED":
            line += " It was sent just before the due date and validated just after, so it had to be paid again as a late payment."
    line += " Replay: " + ("matches." if r["matches"] else "DOES NOT MATCH: " + "; ".join(r["problems"]))
    return line


def _verify_summary(v: dict, only_refused: bool = False) -> str:
    rb = v["rulebook"]
    out = [f"Lockbox {v['address']} on {v['network']}."]
    if v["rulebook_matches_ledger"]:
        out.append(f"Rulebook {rb['name']} v{rb['version']}, fingerprint {rb['fingerprint'][:16]}..., is the same one written in the "
                   "loan broker's Data field on the ledger, and the loan's agreement hash matches too.")
    else:
        out.append("The rulebook does NOT match the ledger: " + "; ".join(v["rulebook_problems"]))
    if not v["has_published_decision_log"]:
        out.append("There is no published decision log for this lockbox, so I can only show what the ledger holds, not replay the referee.")
        return "\n".join(out)
    rows = v["decisions"]
    if only_refused:
        refused = [r for r in rows if r["referee"] == "refused"]
        if not refused:
            out.append(f"The referee refused nothing here: all {len(rows)} decisions were signed.")
        else:
            out.append(f"The referee refused {len(refused)} payment(s):")
            out += [_decision_line(r) for r in refused]
    else:
        out.append(f"The referee made {len(rows)} decisions: it signed {v['signed']} and refused {v['refused']}.")
        out += [_decision_line(r) for r in rows]
    out.append("Verify: " + (f"every decision replays to the same verdict and the same deciding rule, using only the "
                             f"published rulebook and the ledger history." if v["all_match"] else
                             "at least one decision does NOT replay the same way; see the lines marked DOES NOT MATCH."))
    out.append(v["note"])
    return "\n".join(out)


def _about_tx(h: str, address: str) -> str:
    v = auditor.verify_lockbox(address)
    for r in v["decisions"]:
        if r.get("on_ledger") and r["on_ledger"]["explorer"].upper().endswith(h.upper()):
            return clean(f"Transaction {h[:12]}... on lockbox {address}:\n" + _decision_line(r))
    hist = auditor.get_account_history(address, limit=1000)
    if any(t["explorer"] and t["explorer"].upper().endswith(h.upper()) for t in hist["transactions"]):
        return clean(f"{h[:12]}... is on the ledger for {address}, but it is not a payment the referee co-signed "
                     "(for example a lender's own LoanManage, or the lock itself).")
    return clean(f"I could not find {h[:12]}... among the transactions of {address}. Payments into the lockbox from buyers "
                 "do not need the referee, and a refused payment never gets a transaction hash.")


def answer(prompt: str) -> str:
    text = prompt or ""
    addresses = ADDRESS.findall(text)
    hashes = TX_HASH.findall(text)
    lower = text.lower()
    if not addresses and ("rulebook" in lower or "fingerprint" in lower):
        r = auditor.get_rulebook()
        rb = r["rulebook"]
        return clean(f"The published rulebook is {rb.get('name')} v{rb.get('version')}. Its fingerprint is {r['fingerprint']} "
                     f"(recomputed from the rules: {'the same' if r['fingerprint_consistent'] else 'DIFFERENT'}). "
                     f"The file's sha256 is {r['file_sha256']}. A lockbox's loan broker carries this fingerprint in its Data "
                     "field on the ledger, so anyone can check which rules the referee follows.")
    if not addresses:
        known = ", ".join(auditor.known_lockboxes())
        return clean("Give me a lockbox address on XRPL Devnet and I will check what the referee signed, which rule decided "
                     f"each payment, and whether verify matches. Lockboxes with a published decision log: {known}.")
    address = addresses[0]
    if hashes:
        return _about_tx(hashes[0], address)
    v = auditor.verify_lockbox(address)
    if v.get("exists") is False:
        return clean(f"{address} does not exist on XRPL Devnet.")
    return clean(_verify_summary(v, only_refused=("refus" in lower or "denied" in lower or "rejected" in lower)))
