"""The dashboard's demo path to the end, click for click through the API: sales stop, the account freezes,
sales come back, the freeze lifts, the borrower pays off early and gets the account back.
Each click sends what dashboard/index.html sends."""
import api.app as appmod
from fastapi.testclient import TestClient

IV = appmod.INTERVAL


def _word(s):  # the dashboard's state word
    lb = s["lockbox"]
    if lb["master_disabled"] and not lb["signers"] and lb.get("regular_key"):
        return "RELEASED"
    return "FROZEN" if s["frozen"] else "OPEN"


def test_freeze_then_resume_sales_then_pay_off_and_release():
    appmod.SC = None  # a fresh simulated ledger
    c = TestClient(appmod.app)
    post = lambda path, body=None: c.post(f"/api/{path}", json=body or {}).json()  # noqa: E731
    state = lambda: c.get("/api/state").json()  # noqa: E731
    advance = lambda sales: post("advance", {"seconds": IV, "revenue_xrp": 10 if sales else 0})  # noqa: E731

    post("reset")
    for step in ("lock", "quote", "borrow"):
        post(step)
    assert not post("own_wallet")["ok"]  # Send loan to own wallet: refused
    assert post("pay_supplier")["ok"]

    advance(False)  # Stop sales
    assert _word(state()) == "OPEN"
    advance(False)  # Advance 2 min
    s = state()
    assert _word(s) == "FROZEN" and s["status"] == "current"
    r = post("withdraw", {"mode": "all"})  # Withdraw all
    assert not r["verdict"]["allowed"] and not r["ok"]

    # the old order: paying off now is refused, there is not enough money in the account
    assert not post("payoff")["verdict"]["allowed"]

    # Resume sales only flips the page's switch; then two Advance clicks with buyers paying
    advance(True)
    assert _word(state()) == "OPEN"  # the freeze lifts after one interval with sales
    advance(True)
    s = state()
    assert _word(s) == "OPEN" and s["status"] == "current"

    r = post("payoff")  # Repay in full
    assert r["verdict"]["allowed"] and r["ok"]
    assert state()["status"] == "paid"

    r = post("release")  # Release account
    assert r["verdict"]["allowed"] and r["ok"]
    assert _word(state()) == "RELEASED"
