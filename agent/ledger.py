"""Read-only access to the public XRPL Devnet over JSON-RPC.

Only the read methods below can be called. There is no submit, no sign, no wallet: the auditor can look at
the ledger but never change it.
"""
from __future__ import annotations

import json
import os
import urllib.request

DEVNET_RPC = os.environ.get("LOCKBOX_XRPL_RPC", "https://s.devnet.rippletest.net:51234")
EXPLORER = "https://devnet.xrpl.org/transactions/"
READ_METHODS = frozenset({"account_info", "account_tx", "ledger_entry", "server_info"})


class LedgerError(RuntimeError):
    pass


def rpc(method: str, params: dict, url: str = DEVNET_RPC, timeout: int = 20) -> dict:
    if method not in READ_METHODS:
        raise PermissionError(f"{method} is not a read method; the auditor is read-only")
    body = json.dumps({"method": method, "params": [params]}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.load(r).get("result", {})
    if res.get("status") == "error" and res.get("error") not in ("actNotFound", "entryNotFound"):
        raise LedgerError(f"{method}: {res.get('error_message') or res.get('error')}")
    return res


def account_info(address: str) -> dict:
    """account_data plus signer_lists, or {} if the account does not exist."""
    res = rpc("account_info", {"account": address, "ledger_index": "validated", "signer_lists": True})
    if res.get("error") == "actNotFound":
        return {}
    data = dict(res.get("account_data", {}))
    data["signer_lists"] = res.get("signer_lists") or data.get("signer_lists") or []
    return data


def account_tx(address: str, pages: int = 5) -> list[dict]:
    """The account's validated transactions, newest first, as account_tx returns them."""
    out, marker = [], None
    for _ in range(pages):
        params = {"account": address, "limit": 200}
        if marker:
            params["marker"] = marker
        res = rpc("account_tx", params)
        out += res.get("transactions", [])
        marker = res.get("marker")
        if not marker:
            break
    return out


def ledger_entry(index: str) -> dict:
    if not index:
        return {}
    return rpc("ledger_entry", {"index": index, "ledger_index": "validated"}).get("node") or {}
