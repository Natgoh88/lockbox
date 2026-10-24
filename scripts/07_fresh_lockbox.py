"""A new lockbox wallet for the next Devnet loan, plus a supplier the lender approves before it.

  python scripts/07_fresh_lockbox.py
  python scripts/07_fresh_lockbox.py --supplier-cap 20 --invoice PO-1001

A lockbox released after repayment belongs to the borrower again and cannot take a new loan. This funds
a fresh lockbox from the faucet and makes it "lockbox" in .wallets.json. The old seed stays in the file
under lockbox_released_<address>, it is never printed. It also funds a supplier wallet and saves it as
the approved payee in data/payees.json, so 04_borrow puts it in the agreement.

Then: 02_lend_setup.py if the vault's loan window is closing, 03_revenue.py (the new lockbox needs about
3 intervals of buyer payments), 04_borrow.py, 05_collect.py --revenue.
"""
import argparse
import json

import _boot  # noqa: F401

from lockbox.chain import WALLETS, XrplChain, fund, load_state, load_wallets, save_payees, save_state
from lockbox.units import xrp_to_drops

ap = argparse.ArgumentParser()
ap.add_argument("--supplier-cap", type=float, default=20, help="XRP the supplier may be paid out of the loan")
ap.add_argument("--invoice", default="PO-1001")
a = ap.parse_args()

c = XrplChain()
old = load_wallets()["lockbox"]
st = c.lockbox_state(old.address)
if st["signers"]:
    raise SystemExit(f"lockbox {old.address} still has its signer list (not released). Keep using it.")

seeds = json.loads(WALLETS.read_text())
seeds[f"lockbox_released_{old.address}"] = seeds["lockbox"]
print("funding a new lockbox ...", end=" ", flush=True)
new = fund(c.c)
seeds["lockbox"] = new.seed
print(new.address)
print("funding a supplier ...", end=" ", flush=True)
sup = fund(c.c)
seeds["supplier"] = sup.seed
print(sup.address)
WALLETS.write_text(json.dumps(seeds, indent=2))

save_payees({sup.address: {"cap_drops": xrp_to_drops(a.supplier_cap), "invoice_ref": a.invoice}})
state = load_state()
state.pop("loan_id", None)  # the old loan belongs to the released lockbox
save_state(state)
print(f"approved supplier {sup.address}: cap {a.supplier_cap:g} XRP, invoice {a.invoice} (data/payees.json)")
print(f"old lockbox {old.address} kept as lockbox_released_{old.address}. Seeds saved, not printed.")
