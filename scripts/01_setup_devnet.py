"""Fund the wallets from the Devnet faucet and report which lending amendments are live.

  python scripts/01_setup_devnet.py
"""
import _boot  # noqa: F401

from xrpl.wallet import Wallet

from lockbox.chain import WALLETS, XrplChain, fund, save_wallets

NAMES = ["lender", "borrower", "policy", "lockbox", "payout", "cust1", "cust2"]

c = XrplChain()
print("network:", c.url)
for feat in ("SingleAssetVault", "LendingProtocol", "LendingProtocolV1_1", "fixCleanup3_4_0"):
    try:
        print(f"  amendment {feat}: {'ENABLED' if c.feature_enabled(feat) else 'not enabled'}")
    except Exception as e:
        print(f"  amendment {feat}: could not check ({e})")
if not c.feature_enabled("LendingProtocol"):
    raise SystemExit("LendingProtocol is not enabled on this network. Check the xrpl.org lending docs for the current Devnet URL.")

wallets: dict[str, Wallet] = {}
if WALLETS.exists():
    print(f"{WALLETS.name} exists, delete it to start over")
    raise SystemExit(0)
for n in NAMES:
    print(f"funding {n} ...", end=" ", flush=True)
    wallets[n] = fund(c.c)
    print(wallets[n].address)
save_wallets(wallets)
print("saved. Do not reuse these wallets anywhere real.")
