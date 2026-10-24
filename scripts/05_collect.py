"""Run the servicer: collect installments, impair, default, recover.

  python scripts/05_collect.py --revenue          # business keeps earning, loan repays
  python scripts/05_collect.py --withdraw-free    # borrower takes free cash first, then pass --no-revenue to force a default
"""
import argparse
import random
import time

import _boot  # noqa: F401

from lockbox import devnet
from lockbox.units import fmt_xrp, xrp_to_drops

ap = argparse.ArgumentParser()
ap.add_argument("--revenue", action="store_true")
ap.add_argument("--xrp", type=float, default=2.5)
ap.add_argument("--withdraw-free", action="store_true")
ap.add_argument("--every", type=int, default=15)
a = ap.parse_args()

sc = devnet.build()
assert sc.servicer and sc.servicer.loan_id, "no loan yet, run 04_borrow.py"
if a.withdraw_free:
    v, r = sc.agent.withdraw(sc.state()["lockbox"]["free_cash"])
    print("withdraw free cash:", v.allowed, r.code if r else v.reason)
v, r = sc.agent.withdraw(sc.b.balance(sc.lockbox))
print("withdraw everything:", "ALLOWED (bug)" if v.allowed else "refused: " + v.reason)

last = None
while True:
    if a.revenue:
        try:
            sc.earn(xrp_to_drops(a.xrp * random.uniform(0.8, 1.2)), random.randint(0, 1))
        except RuntimeError as e:  # a customer out of test XRP must never stop the servicer
            print(f"{time.strftime('%H:%M:%S')} revenue skipped: {e}")
    acts = sc.servicer.tick()
    s = sc.state()
    line = f"{time.strftime('%H:%M:%S')} {s['status']:<10} lockbox {fmt_xrp(s['lockbox']['balance'])}  remaining {s['loan']['remaining']}  {acts or ''}"
    if line != last:
        print(line)
        last = line
    if s["status"] == "paid" or (s["status"] == "defaulted" and s["recovery_remaining"] == 0):
        break
    if s["status"] == "defaulted" and not acts and s["lockbox"]["balance"] <= sc.b.reserve(sc.lockbox) + sc.params.fee_buffer_drops + 1000:
        break
    time.sleep(a.every)
print("final:", s["status"])
