"""Turn raw account_tx history into a clean list of incoming XRP payments.

Only evidence we can defend is kept: validated, successful Payments to the account, XRP only,
measured by what was DELIVERED (not what the Amount field says), excluding self payments and
any sender we are told to ignore (for example the vault paying out loan principal).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .units import ripple_to_unix


@dataclass(frozen=True)
class Inflow:
    ts: int  # unix seconds
    drops: int
    sender: str
    tx_hash: str = ""


def _close_time(entry: dict, tx: dict) -> int | None:
    date = tx.get("date", entry.get("date"))
    if date is not None:
        return ripple_to_unix(int(date))
    iso = entry.get("close_time_iso")
    if iso:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    return None


def parse_account_tx(entries: list[dict], account: str, ignore_senders: set[str] | None = None) -> list[Inflow]:
    """entries = result["transactions"] from an account_tx call (API v1 or v2 shape)."""
    ignore = ignore_senders or set()
    out: list[Inflow] = []
    for e in entries:
        tx = e.get("tx_json") or e.get("tx") or {}
        meta = e.get("meta") or e.get("metaData") or {}
        if not e.get("validated", True):
            continue
        if tx.get("TransactionType") != "Payment" or tx.get("Destination") != account:
            continue
        if tx.get("Account") == account or tx.get("Account") in ignore:
            continue
        if meta.get("TransactionResult") != "tesSUCCESS":
            continue
        delivered = meta.get("delivered_amount", meta.get("DeliveredAmount"))
        if not isinstance(delivered, str) or not delivered.isdigit():
            continue  # IOU or unavailable: not counted in this XRP-only version
        ts = _close_time(e, tx)
        if ts is None:
            continue
        out.append(Inflow(ts=ts, drops=int(delivered), sender=tx["Account"], tx_hash=e.get("hash") or tx.get("hash", "")))
    return sorted(out, key=lambda i: i.ts)
