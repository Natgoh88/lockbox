"""Lender side: create the vault, the loan broker, deposit liquidity and put in first-loss cover.

  python scripts/02_lend_setup.py --deposit 50 --cover 15
"""
import argparse

import _boot  # noqa: F401

from lockbox import devnet
from lockbox.backend import addr_of
from lockbox.chain import explorer
from lockbox.units import fmt_xrp, xrp_to_drops

ap = argparse.ArgumentParser()
ap.add_argument("--deposit", type=float, default=50)  # faucet gives 100 XRP; leave room for cover and fees
ap.add_argument("--cover", type=float, default=15)
a = ap.parse_args()

sc = devnet.build()
bal = sc.b.balance(addr_of(sc.lender))
print("lender balance", fmt_xrp(bal), "XRP")
if bal < xrp_to_drops(a.deposit + a.cover + 5):
    raise SystemExit("lender needs more XRP: lower --deposit/--cover or fund it from the faucet")
sc.setup_lender(xrp_to_drops(a.deposit), xrp_to_drops(a.cover))
devnet.persist(sc)
print("vault ", sc.vault_id)
print("broker", sc.broker_id)
print("saved to data/state.json")
