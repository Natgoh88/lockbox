"""Build a Scenario on the real ledger from saved wallets and ids."""
from __future__ import annotations

from .chain import XrplChain, load_state, load_wallets, save_state
from .scenario import Scenario, demo_policy


def build(interval: int = 120, url: str | None = None) -> Scenario:
    chain = XrplChain(url) if url else XrplChain()
    w = load_wallets()
    sc = Scenario(b=chain, lender=w["lender"], borrower=w["borrower"], policy_key=w["policy"], lockbox_key=w["lockbox"],
                  payout=w["payout"].address, customers=[w["cust1"], w["cust2"]], uw=demo_policy(interval))
    st = load_state()
    if st.get("vault_id") and st.get("broker_id"):
        sc.attach(st["vault_id"], st["broker_id"], st.get("loan_id"))
    return sc


def persist(sc: Scenario) -> None:
    save_state({k: v for k, v in sc.ids().items() if v})
