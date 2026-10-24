"""The real ledger: a Backend over xrpl-py's JSON-RPC client. Written against the xrpl.org lending
tutorials and xrpl-py 5.x. NOT run against Devnet yet (the sandbox it was built in cannot reach XRPL),
so expect a small fix or two on the first run. Everything above this file is tested on the sim.
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountInfo, AccountTx, Feature, Ledger, LedgerEntry, ServerInfo
from xrpl.models.requests.vault_info import VaultInfo
from xrpl.models.transactions import LoanSet, Transaction
from xrpl.transaction import (
    autofill,
    combine_loanset_counterparty_signers,
    multisign,
    sign,
    sign_loan_set_by_counterparty,
    submit_and_wait,
)
from xrpl.wallet import Wallet, generate_faucet_wallet

from .backend import Result, addr_of
from .covenant import LoanView
from .health import BrokerView, VaultView
from .units import RATE_UNITS, ripple_to_unix

DEVNET_RPC = os.environ.get("XRPL_RPC", "https://s.devnet.rippletest.net:51234")
DEVNET_EXPLORER = "https://devnet.xrpl.org/transactions/"
LSF_DISABLE_MASTER = 0x00100000
LSF_LOAN_DEFAULT = 0x00010000
LSF_LOAN_IMPAIRED = 0x00020000

ROOT = Path(__file__).resolve().parents[1]
WALLETS = ROOT / ".wallets.json"
STATE = ROOT / "data" / "state.json"


def _num(v, default: int = 0) -> int:
    """Ledger Number / amount fields come back as strings, sometimes decimal or scientific."""
    if v is None:
        return default
    try:
        return int(Decimal(str(v)))
    except Exception:
        return default


class XrplChain:
    def __init__(self, url: str = DEVNET_RPC):
        self.url = url
        self.c = JsonRpcClient(url)
        self._close: tuple[float, int] | None = None

    # ---- reads ----
    def now(self) -> int:
        """Validated ledger close time, cached for two seconds. Falls back to the local clock."""
        t = time.time()
        if self._close and t - self._close[0] < 2:
            return self._close[1] + int(t - self._close[0])
        try:
            r = self.c.request(Ledger(ledger_index="validated")).result
            ct = ripple_to_unix(int(r["ledger"]["close_time"]))
            self._close = (t, ct)
            return ct
        except Exception:
            return int(t)

    def _info(self, addr: str, **kw) -> dict:
        return self.c.request(AccountInfo(account=addr, ledger_index="validated", **kw)).result

    def balance(self, addr: str) -> int:
        info = self._info(addr)
        if info.get("error") == "actNotFound":  # not funded yet; anything else is a real error
            return 0
        return int(info["account_data"]["Balance"])

    def reserve(self, addr: str) -> int:
        led = self.c.request(ServerInfo()).result["info"]["validated_ledger"]
        base, inc = Decimal(str(led["reserve_base_xrp"])), Decimal(str(led["reserve_inc_xrp"]))
        owners = int(self._info(addr)["account_data"].get("OwnerCount", 0))
        return int((base + inc * owners) * 1_000_000)

    def account_tx(self, addr: str) -> list[dict]:
        out: list[dict] = []
        marker = None
        for _ in range(5):
            req = AccountTx(account=addr, limit=200, marker=marker) if marker else AccountTx(account=addr, limit=200)
            res = self.c.request(req).result
            out += res.get("transactions", [])
            marker = res.get("marker")
            if not marker:
                break
        return out

    def lockbox_state(self, addr: str) -> dict:
        res = self._info(addr, signer_lists=True)
        data = res["account_data"]
        lists = res.get("signer_lists") or data.get("signer_lists") or []
        signers, quorum = {}, 0
        if lists:
            quorum = int(lists[0].get("SignerQuorum", 0))
            signers = {e["SignerEntry"]["Account"]: int(e["SignerEntry"]["SignerWeight"]) for e in lists[0].get("SignerEntries", [])}
        return {"master_disabled": bool(int(data.get("Flags", 0)) & LSF_DISABLE_MASTER), "signers": signers, "quorum": quorum,
                "regular_key": "RegularKey" in data}

    def _entry(self, index: str) -> dict | None:
        res = self.c.request(LedgerEntry(index=index, ledger_index="validated")).result
        return res.get("node")

    def loan_view(self, loan_id: str) -> LoanView | None:
        n = self._entry(loan_id)
        if not n:
            return None
        flags = int(n.get("Flags", 0))
        return LoanView(loan_id=loan_id, borrower=n.get("Borrower", ""), periodic_payment=_num(n.get("PeriodicPayment")),
                        service_fee=_num(n.get("LoanServiceFee")), next_due=ripple_to_unix(int(n.get("NextPaymentDueDate", 0))),
                        payment_remaining=int(n.get("PaymentRemaining", 0)), grace_period=int(n.get("GracePeriod", 0)),
                        defaulted=bool(flags & LSF_LOAN_DEFAULT), impaired=bool(flags & LSF_LOAN_IMPAIRED),
                        total_owed=_num(n.get("TotalValueOutstanding")))

    def broker_view(self, broker_id: str) -> BrokerView:
        n = self._entry(broker_id) or {}
        return BrokerView(cover_available=_num(n.get("CoverAvailable")), debt_total=_num(n.get("DebtTotal")),
                          cover_rate_minimum=int(n.get("CoverRateMinimum", 0)), cover_rate_liquidation=int(n.get("CoverRateLiquidation", RATE_UNITS)))

    def vault_view(self, vault_id: str) -> VaultView:
        v = self.c.request(VaultInfo(vault_id=vault_id)).result.get("vault", {})
        return VaultView(assets_total=_num(v.get("AssetsTotal")), assets_available=_num(v.get("AssetsAvailable")),
                         loss_unrealized=_num(v.get("LossUnrealized")))

    def feature_enabled(self, name: str) -> bool:
        feats = self.c.request(Feature()).result.get("features", {})
        return any(f.get("name") == name and f.get("enabled") for f in feats.values())

    # ---- writes ----
    def prepare(self, tx: dict, signers_count: int = 0) -> dict:
        model = Transaction.from_xrpl(tx)
        filled = autofill(model, self.c, signers_count or None)
        return filled.to_xrpl()

    def _finish(self, signed, tx: dict) -> Result:
        try:
            res = submit_and_wait(signed, self.c).result
        except Exception as e:  # reliable submission raises on tec results and timeouts
            msg = str(e)
            code = next((w for w in msg.replace(":", " ").replace(",", " ").split() if w[:3] in ("tec", "tef", "tem", "ter", "tel")), "error")
            return Result(ok=False, code=code, tx=tx, meta={"error": msg[:300]})
        meta = res.get("meta", {})
        code = meta.get("TransactionResult", "unknown")
        return Result(ok=code == "tesSUCCESS", code=code, tx=tx, meta=meta, hash=res.get("hash", ""))

    def submit(self, tx: dict, signers: list, counterparty_signers: list | None = None) -> Result:
        model = Transaction.from_xrpl(tx)
        account = tx["Account"]
        if tx["TransactionType"] == "LoanSet":
            first = sign(model, signers[0])
            parts = [sign_loan_set_by_counterparty(w, first, multisign=True).tx for w in (counterparty_signers or [])]
            final = combine_loanset_counterparty_signers(parts).tx
            return self._finish(final, tx)
        if len(signers) == 1 and addr_of(signers[0]) == account:
            return self._finish(sign(model, signers[0]), tx)
        signed = [sign(model, w, multisign=True) for w in signers]
        return self._finish(multisign(model, signed), tx)


# ---- wallets and saved ids for the scripts ----

def load_wallets() -> dict[str, Wallet]:
    data = json.loads(WALLETS.read_text())
    return {k: Wallet.from_seed(v) for k, v in data.items()}


def save_wallets(w: dict[str, Wallet]) -> None:
    WALLETS.write_text(json.dumps({k: v.seed for k, v in w.items()}, indent=2))


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2))


def fund(client: JsonRpcClient) -> Wallet:
    return generate_faucet_wallet(client)


def explorer(h: str) -> str:
    return DEVNET_EXPLORER + h
