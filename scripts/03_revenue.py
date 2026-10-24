"""Customers pay the borrower's lockbox address. This is the revenue history the underwriter reads.

  python scripts/03_revenue.py --minutes 8
Needs at least three underwriting intervals (default 120 s each) with revenue before a quote is approved.
"""
import argparse
import random
import time

import _boot  # noqa: F401

from lockbox import devnet
from lockbox.units import fmt_xrp, xrp_to_drops

ap = argparse.ArgumentParser()
ap.add_argument("--minutes", type=float, default=8)
ap.add_argument("--xrp", type=float, default=2.5, help="average per payment")
ap.add_argument("--every", type=int, default=30, help="seconds between payments")
a = ap.parse_args()

sc = devnet.build()
end = time.time() + a.minutes * 60
n = 0
while time.time() < end:
    amt = xrp_to_drops(a.xrp * random.uniform(0.8, 1.2))
    r = sc.earn(amt, n)
    n += 1
    print(f"{time.strftime('%H:%M:%S')} customer {n % 2 + 1} paid {fmt_xrp(amt)} XRP  {r.code}")
    time.sleep(a.every)
