"""Lender side: create the vault, the loan broker, deposit liquidity and put in first-loss cover.

  python scripts/02_lend_setup.py --deposit 50 --cover 15

LendingProtocolV1_1 only lets a broker sit on a closed-ended vault. Deposits are open until the
subscription date, loans can only start after it, and every loan must end at least 60 s before the
redemption date, when depositors can withdraw again. So run 04_borrow.py after --subscribe-min.
"""
import argparse

import _boot  # noqa: F401

from lockbox import devnet, events
from lockbox.backend import addr_of
from lockbox.chain import explorer
from lockbox.units import fmt_xrp, xrp_to_drops

ap = argparse.ArgumentParser()
ap.add_argument("--deposit", type=float, default=50)  # faucet gives 100 XRP; leave room for cover and fees
ap.add_argument("--cover", type=float, default=15)
ap.add_argument("--subscribe-min", type=float, default=5, help="minutes deposits stay open; loans start after this")
ap.add_argument("--invest-min", type=float, default=75, help="minutes loans can run before depositors can redeem")
a = ap.parse_args()

sc = devnet.build()
la = addr_of(sc.lender)
bal = sc.b.balance(la)
print("lender balance", fmt_xrp(bal), "XRP")
if bal < xrp_to_drops(a.deposit + a.cover + 5):
    raise SystemExit("lender needs more XRP: lower --deposit/--cover or fund it from the faucet")

events.reset()
sc.setup_lender(xrp_to_drops(a.deposit), xrp_to_drops(a.cover), closed_ended=True,
                subscribe_s=int(a.subscribe_min * 60), invest_s=int(a.invest_min * 60))
for e in events.recent():
    if e.get("kind") == "tx":
        print(f"{e['label']:<28} {e['code']}  {explorer(e['hash'])}")
devnet.persist(sc)
print("vault ", sc.vault_id)
print("broker", sc.broker_id, " rulebook hash in Data", sc.rulebook_hash[:16], "...")
print(f"loans can start after {a.subscribe_min:g} min; depositors can redeem after {a.subscribe_min + a.invest_min:g} min")
print("saved to data/state.json")
