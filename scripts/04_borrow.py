"""Lock the account, underwrite from its ledger history, and take the loan.

  python scripts/04_borrow.py
"""
import _boot  # noqa: F401

from lockbox import devnet
from lockbox.chain import explorer
from lockbox.units import fmt_xrp

sc = devnet.build()
if sc.b.lockbox_state(sc.lockbox)["master_disabled"]:
    print("lockbox is already locked")
else:
    sc.lock()
    print("lockbox locked: referee required plus one other (referee 2, borrower 1, lender 1, quorum 3), master key off")
o = sc.quote()
print(f"approved={o.approved} tier={o.tier} score={o.score}")
for line in o.reasons + o.declines:
    print("  -", line)
if not o.approved:
    raise SystemExit("declined. Run 03_revenue.py for a few more minutes and try again.")
print(f"offer: {fmt_xrp(o.principal_drops)} XRP, installment {fmt_xrp(o.installment_drops)} XRP every {o.payment_interval}s x {o.payment_total}")
r = sc.borrow()
print("LoanSet:", r.code, explorer(r.hash) if r.hash else "")
if r.ok:
    devnet.persist(sc)
    print("loan", sc.servicer.loan_id)
    ln = sc.b.loan_view(sc.servicer.loan_id)
    print(f"start python scripts/05_collect.py --revenue NOW: the first installment is due in {ln.next_due - sc.b.now()} s")
else:
    print(r.meta)
