"""Read-only: run the underwriter over a real XRPL address's payment history (mainnet by default).
Use an address you own or a public business address. Nothing is signed or sent.

  python scripts/06_score_address.py rXXXXXXXX --interval-days 1
"""
import argparse
import time

import _boot  # noqa: F401

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountTx

from lockbox.inflows import parse_account_tx
from lockbox.underwrite import Policy, underwrite
from lockbox.units import fmt_xrp

ap = argparse.ArgumentParser()
ap.add_argument("address")
ap.add_argument("--rpc", default="https://xrplcluster.com")
ap.add_argument("--interval-days", type=float, default=1.0)
a = ap.parse_args()

c = JsonRpcClient(a.rpc)
rows, marker = [], None
for _ in range(10):
    res = c.request(AccountTx(account=a.address, limit=400, marker=marker) if marker else AccountTx(account=a.address, limit=400)).result
    rows += res.get("transactions", [])
    marker = res.get("marker")
    if not marker:
        break
flows = parse_account_tx(rows, a.address)
iv = int(a.interval_days * 86400)
o = underwrite(flows, int(time.time()), Policy(payment_interval=iv, payment_total=6, grace_period=iv, lookback_buckets=12))
print(f"{len(flows)} incoming XRP payments found, {len(rows)} transactions read")
print(f"approved={o.approved} tier={o.tier} score={o.score}")
print("revenue per interval (oldest to newest, XRP):", [round(b / 1e6, 2) for b in o.metrics.buckets])
for line in o.reasons + o.declines:
    print("  -", line)
if o.approved:
    print(f"would lend {fmt_xrp(o.principal_drops)} XRP, installment {fmt_xrp(o.installment_drops)} XRP every {a.interval_days:g} day(s) x {o.payment_total}")
