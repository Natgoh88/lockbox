"""Build a ready-to-run Scenario on the in-memory SimChain (tests, offline demo, dashboard rehearsal)."""
from __future__ import annotations

from xrpl.wallet import Wallet

from . import protocol as P
from .scenario import Scenario, demo_policy
from .sim import SimChain
from .units import xrp_to_drops


def build(interval: int = 120, seed_revenue: bool = True) -> Scenario:
    chain = SimChain()
    names = ["lender", "borrower", "policy", "lockbox", "payout", "cust1", "cust2", "cust3"]
    addr = {n: Wallet.create().address for n in names}
    for n, xrp in {"lender": 500, "borrower": 5, "policy": 5, "lockbox": 10, "payout": 2, "cust1": 1000, "cust2": 1000, "cust3": 1000}.items():
        chain.fund(addr[n], xrp_to_drops(xrp))
    sc = Scenario(b=chain, lender=addr["lender"], borrower=addr["borrower"], policy_key=addr["policy"], lockbox_key=addr["lockbox"],
                  payout=addr["payout"], customers=[addr["cust1"], addr["cust2"], addr["cust3"]], uw=demo_policy(interval))
    sc.setup_lender(deposit_drops=xrp_to_drops(300), cover_drops=xrp_to_drops(40))
    if seed_revenue:
        # six intervals of steady revenue from three customers, a little uneven like real life
        pattern = [32, 41, 36, 44, 39, 42]
        for amt in pattern:
            for i in range(4):
                sc.earn(xrp_to_drops(amt / 4), i)
                chain.advance(interval // 4)
        # the business sweeps revenue to its own payout wallet as it goes, keeping a working float
        sweep = chain.balance(sc.lockbox) - xrp_to_drops(12)
        sc._send(P.payment(sc.lockbox, sc.payout, sweep), sc.lockbox_key, "sweep revenue to payout")
    return sc
