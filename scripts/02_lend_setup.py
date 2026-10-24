"""Lender side: create the vault, the loan broker, deposit liquidity and put in first-loss cover.

  python scripts/02_lend_setup.py --deposit 50 --cover 15

LendingProtocolV1_1 only lets a broker sit on a closed-ended vault. Deposits are open until the
subscription date, loans can only start after it, and every loan must end at least 60 s before the
redemption date, when depositors can withdraw again. So run 04_borrow.py after --subscribe-min.
Scenario.setup_lender still makes an open-ended vault (fine on the sim), so this script does the
same steps itself until that grows a closed-ended option.
"""
import argparse

import _boot  # noqa: F401

from lockbox import covenant, devnet, protocol as P
from lockbox.backend import addr_of
from lockbox.chain import explorer
from lockbox.units import RATE_UNITS, fmt_xrp, xrp_to_drops

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

now = sc.b.now()
sub, red = int(now + a.subscribe_min * 60), int(now + (a.subscribe_min + a.invest_min) * 60)
v = sc._send(P.vault_create(la, closed_ended=True, subscription_unix=sub, redemption_unix=red), sc.lender, "VaultCreate")
vault_id = v.created["Vault"]
print("VaultCreate (closed-ended)", explorer(v.hash))
r = sc._send(P.vault_deposit(la, vault_id, xrp_to_drops(a.deposit)), sc.lender, "VaultDeposit")
print("VaultDeposit", explorer(r.hash))
sc.rulebook_hash = covenant.rulebook_hash(sc.params.rulebook())
br = sc._send(P.broker_set(la, vault_id, 500_000_000, int(25 * RATE_UNITS / 100), RATE_UNITS, data=sc.rulebook_hash),
              sc.lender, "LoanBrokerSet")
broker_id = br.created["LoanBroker"]
print("LoanBrokerSet", explorer(br.hash))
r = sc._send(P.cover_deposit(la, broker_id, xrp_to_drops(a.cover)), sc.lender, "LoanBrokerCoverDeposit")
print("LoanBrokerCoverDeposit", explorer(r.hash))

sc.attach(vault_id, broker_id)
devnet.persist(sc)
print("vault ", vault_id)
print("broker", broker_id)
print(f"loans can start after {a.subscribe_min:g} min; depositors can redeem after {a.subscribe_min + a.invest_min:g} min")
print("saved to data/state.json")
