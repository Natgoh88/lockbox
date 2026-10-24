"""Prove the referee followed its published rules.

  python scripts/verify.py                    run the demo story on the simulated ledger, then verify it
  python scripts/verify.py --tamper-verdict   same, but flip one logged verdict first (verify must catch it)
  python scripts/verify.py --tamper-rulebook  same, but loosen a rule in the rulebook file first (verify must catch it)
  python scripts/verify.py --devnet           verify the Devnet lockbox from data/events.jsonl and rulebooks/

Step 1 checks the rulebook file against the fingerprint the ledger holds in the broker's Data field.
Step 2 replays every referee decision: buckets rebuilt from the lockbox's ledger history at that point,
rules from the file, and the same covenant code. Each row says whether the replay matches the referee.
"""
import _boot  # noqa: F401

import argparse
import json
import pathlib
import sys
import tempfile

from lockbox import events, verify

ROOT = pathlib.Path(__file__).resolve().parents[1]


def sim_story(rulebook_path: str):
    """The DEMO.md flow on the simulated ledger: borrow, refused grab, supplier paid, installments, sales stop, default."""
    from xrpl.wallet import Wallet

    from lockbox.simsetup import build
    from lockbox.units import xrp_to_drops

    sc = build(rulebook_path=rulebook_path)
    supplier = Wallet.create().address
    sc.lock()
    sc.quote()
    sc.approve_payee(supplier, cap_drops=xrp_to_drops(1000), invoice_ref="PO-1001")
    assert sc.borrow().ok
    sc.agent.withdraw(sc.state()["buckets"]["unspent_principal"])  # loan money to the owner's wallet: refused
    bk = sc.state()["buckets"]
    sc.agent.pay_payee(supplier, bk["unspent_principal"] + bk["earned_free_cash"], "PO-1001")  # supplier: allowed
    sc.run(100, step=10)  # first installment collected
    sc.run(160, step=10)  # sales stop: the second installment cannot be paid, the loan is late
    sc.agent.withdraw(1_000)  # frozen while a payment is past due
    sc.run(400, step=30)  # still no sales: default, cover pays, recovery from the lockbox
    sc.agent.withdraw(1_000)
    labels = {sc.payout: "owner wallet", supplier: "supplier", sc.lockbox: "lockbox", sc.servicer.address: "lender"}
    return sc, labels


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--devnet", action="store_true", help="verify the Devnet lockbox instead of a fresh sim run")
    ap.add_argument("--events", default=str(ROOT / "data" / "events.jsonl"), help="the referee's event log (Devnet)")
    ap.add_argument("--rulebook", default=str(ROOT / "rulebooks" / "lending-covenant-v1.json"))
    ap.add_argument("--tamper-verdict", action="store_true", help="flip one logged verdict before verifying")
    ap.add_argument("--tamper-rulebook", action="store_true", help="loosen one rule in the rulebook file before verifying")
    a = ap.parse_args()

    if a.devnet:
        from lockbox import devnet
        sc = devnet.build()
        labels = {sc.payout: "owner wallet", sc.lockbox: "lockbox", sc.servicer.address if sc.servicer else "": "lender"}
        rulebook_path = a.rulebook
        decisions = [json.loads(line) for line in pathlib.Path(a.events).read_text().splitlines() if line.strip()]
        ledger = "Devnet"
    else:
        tmp = tempfile.mkdtemp(prefix="lockbox-verify-")
        rulebook_path = str(pathlib.Path(tmp) / "lending-covenant-v1.json")
        sc, labels = sim_story(rulebook_path)
        decisions = events.recent(10_000)
        ledger = "the simulated ledger (not real money)"
    decisions = [e for e in decisions if e.get("kind") == "verdict" and "decision" in e]

    doc = json.loads(pathlib.Path(rulebook_path).read_text())
    if a.tamper_rulebook:
        doc["config"]["holdback_bps"] = 5000
        doc["fingerprint"] = verify.C.rulebook_fingerprint(doc["config"])  # a careful forger recomputes the hash too
    if a.tamper_verdict and decisions:
        refused = next((e for e in decisions if not e["allowed"]), decisions[0])
        refused["allowed"] = not refused["allowed"]

    broker_data = sc.b.broker_view(sc.broker_id).data if sc.broker_id else None
    history = sc.b.account_tx(sc.lockbox)
    loanset = next((verify.bk._tx(e) for e in history if verify.bk._tx(e).get("TransactionType") == "LoanSet"
                    and verify.bk._meta(e).get("TransactionResult") == "tesSUCCESS"), None)

    print(f"Verifying the referee on {ledger}")
    print(f"lockbox {sc.lockbox}")
    print()
    print(f"1. Rulebook {doc.get('name')} v{doc.get('version')}, fingerprint {str(doc.get('fingerprint'))[:16]}...")
    rb_problems = verify.rulebook_problems(doc, broker_data, loanset.get("Data") if loanset else None)
    print(f"   on the ledger (broker Data): {str(broker_data or 'empty')[:16]}...")
    print("   file matches the ledger: " + ("yes" if not rb_problems else "NO"))
    for p in rb_problems:
        print(f"   ! {p}")
    print()

    rows = verify.replay(decisions, history, sc.b.balance(sc.lockbox), sc.lockbox, doc, lambda b: sc.b.broker_view(b).data)
    for r, ev in zip(rows, decisions):
        dest = ev["decision"]["tx"].get("Destination")
        if dest in labels:
            r.tx = r.tx.split(" to ")[0] + f" to {labels[dest]}"
    print(f"2. Replaying {len(rows)} referee decision(s) from the ledger history")
    print(verify.table(rows))
    bad = [r for r in rows if not r.matches]
    print()
    print("What the deciding rules say:")
    print(verify.legend(rows))
    print()
    if rb_problems or bad:
        print(f"FAILED: {len(bad)} decision(s) do not match" + (", and the rulebook file does not match the ledger" if rb_problems else ""))
        return 1
    print(f"OK: the rulebook matches the ledger and all {len(rows)} decisions follow from it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
