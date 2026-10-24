"""Dashboard and control API.

  LOCKBOX_MODE=sim      (default) the whole story on the in-memory ledger, time moves when you press Advance
  LOCKBOX_MODE=devnet   the real ledger; an autopilot thread runs the servicer and (optionally) customer revenue

  uvicorn api.app:app --port 8403
"""
from __future__ import annotations

import os
import random
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from lockbox import events
from lockbox.units import xrp_to_drops

MODE = os.environ.get("LOCKBOX_MODE", "sim")
INTERVAL = int(os.environ.get("LOCKBOX_INTERVAL", "120"))
HERE = Path(__file__).resolve().parents[1]
events.set_path(os.environ.get("LOCKBOX_EVENTS", str(HERE / "data" / "events.jsonl")) if MODE != "sim" else "")

app = FastAPI(title="Lockbox")
LOCK = threading.RLock()
SC = None
AUTO = {"revenue": True, "autopilot": MODE != "sim", "revenue_xrp": float(os.environ.get("LOCKBOX_REVENUE_XRP", "2.5"))}


SUPPLIER_INVOICE = "PO-1001"


def make():
    if MODE == "sim":
        from xrpl.wallet import Wallet
        from lockbox.simsetup import build
        s = build(INTERVAL)
        # the exporter's supplier, approved by the lender before the loan (the list is frozen into the agreement hash)
        s.approve_payee(Wallet.create().address, cap_drops=xrp_to_drops(100), invoice_ref=SUPPLIER_INVOICE)
        return s
    from lockbox import devnet
    from lockbox.chain import apply_payees
    s = devnet.build(INTERVAL)
    apply_payees(s)  # suppliers approved before the loan (scripts/07_fresh_lockbox.py)
    return s


def sc():
    global SC
    if SC is None:
        SC = make()
    return SC


class Body(BaseModel):
    seconds: int = 120
    revenue_xrp: float = 0.0
    xrp: float | None = None
    mode: str = "free"  # free | all | xrp


def persist():
    if MODE != "sim":
        from lockbox import devnet
        devnet.persist(sc())


def autopilot():
    while True:
        time.sleep(15)
        if MODE == "sim" or not AUTO["autopilot"]:
            continue
        try:
            with LOCK:
                s = sc()
                if AUTO["revenue"] and s.servicer and s.servicer.loan_id:
                    try:
                        s.earn(xrp_to_drops(AUTO["revenue_xrp"] * random.uniform(0.8, 1.2)), random.randint(0, 1))
                    except RuntimeError as e:  # a buyer out of test XRP must never skip the servicer tick
                        events.emit("error", msg=f"revenue skipped: {e}"[:200])
                if s.servicer:
                    s.servicer.tick()
        except Exception as e:  # keep the loop alive, show the error on the dashboard
            events.emit("error", msg=str(e)[:200])


if MODE != "sim":
    threading.Thread(target=autopilot, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "dashboard" / "index.html").read_text()


@app.get("/api/state")
def state():
    with LOCK:
        s = sc()
        out = s.state()
        out["mode"] = MODE
        out["auto"] = AUTO
        out["interval"] = INTERVAL
        out["has_offer"] = s.offer is not None
        out["has_loan"] = bool(s.servicer and s.servicer.loan_id)
        out["locked"] = bool(out["lockbox"]["master_disabled"])
        return out


@app.get("/api/events")
def get_events(since: int = 0):
    return [e for e in events.recent(400) if e["i"] > since]


def guarded(fn):
    try:
        with LOCK:
            return fn()
    except HTTPException:
        raise
    except Exception as e:
        events.emit("error", msg=str(e)[:200])
        raise HTTPException(status_code=400, detail=str(e)[:300])


@app.post("/api/lock")
def lock():
    return guarded(lambda: (sc().lock(), {"ok": True})[1])


@app.post("/api/quote")
def quote():
    def go():
        o = sc().quote()
        return {"approved": o.approved, "principal": o.principal_drops}
    return guarded(go)


@app.post("/api/borrow")
def borrow():
    def go():
        r = sc().borrow()
        persist()
        return {"ok": r.ok, "code": r.code}
    return guarded(go)


@app.post("/api/earn")
def earn(b: Body):
    return guarded(lambda: {"ok": sc().earn(xrp_to_drops(b.revenue_xrp or 5)).ok})


@app.post("/api/advance")
def advance(b: Body):
    if MODE != "sim":
        raise HTTPException(400, "real ledger time moves on its own; use the autopilot")
    return guarded(lambda: {"actions": sc().run(b.seconds, 30, xrp_to_drops(b.revenue_xrp) if b.revenue_xrp else 0)})


@app.post("/api/tick")
def tick():
    return guarded(lambda: {"actions": sc().servicer.tick() if sc().servicer else []})


@app.post("/api/withdraw")
def withdraw(b: Body):
    def go():
        s = sc()
        st = s.state()["lockbox"]
        if b.mode == "all":
            amt = max(0, st["balance"] - s.b.reserve(s.lockbox))
        elif b.mode == "xrp" and b.xrp is not None:
            amt = xrp_to_drops(b.xrp)
        else:
            amt = st["free_cash"]
        v, r = s.agent.withdraw(amt)
        return {"amount": amt, "verdict": v.to_dict(), "ok": bool(r and r.ok)}
    return guarded(go)


@app.post("/api/own_wallet")
def own_wallet():
    """Send the loan money (unspent principal) to the business's own wallet. The referee should refuse."""
    def go():
        s = sc()
        bk = s.state()["buckets"] or {}
        amt = bk.get("unspent_principal") or xrp_to_drops(1)
        v, r = s.agent.withdraw(amt)
        return {"amount": amt, "verdict": v.to_dict(), "ok": bool(r and r.ok)}
    return guarded(go)


@app.post("/api/pay_supplier")
def pay_supplier(b: Body):
    """Pay the first approved supplier with its invoice memo: by default everything the loan and free cash allow."""
    def go():
        s = sc()
        st = s.state()
        if not st["approved_payees"]:
            raise HTTPException(400, "no approved supplier: the lender approves suppliers before the loan")
        addr, p = next(iter(st["approved_payees"].items()))
        bk = st["buckets"] or {}
        left = p["cap_drops"] - (bk.get("paid_to_payees") or {}).get(addr, 0)
        amt = xrp_to_drops(b.xrp) if b.xrp is not None else min(left, bk.get("unspent_principal", 0) + bk.get("earned_free_cash", 0))
        v, r = s.agent.pay_payee(addr, max(1, amt), p["invoice_ref"])
        return {"amount": amt, "to": addr, "verdict": v.to_dict(), "ok": bool(r and r.ok)}
    return guarded(go)


@app.post("/api/payoff")
def payoff():
    def go():
        v, r = sc().pay_off()
        return {"verdict": v.to_dict(), "ok": bool(r and r.ok), "code": r.code if r else None}
    return guarded(go)


@app.post("/api/release")
def release():
    def go():
        v, r = sc().release()
        return {"verdict": v.to_dict(), "ok": bool(r and r.ok), "code": r.code if r else None}
    return guarded(go)


@app.post("/api/auto")
def auto(b: dict):
    for k in ("revenue", "autopilot"):
        if k in b:
            AUTO[k] = bool(b[k])
    return AUTO


@app.post("/api/reset")
def reset():
    global SC
    if MODE != "sim":
        raise HTTPException(400, "reset is for the sim only")
    with LOCK:
        events.reset()
        SC = make()
    return {"ok": True}
