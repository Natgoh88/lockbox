"""The whole story in the terminal, on the in-memory ledger: a good borrower and a bad one.

  python scripts/story.py
"""
import _boot  # noqa: F401

from lockbox import events
from lockbox.simsetup import build
from lockbox.units import fmt_xrp as x, xrp_to_drops


def line(s=""):
    print(s)


def head(s):
    print("\n== " + s)


head("A business earns on XRPL, and wants a working-capital loan")
sc = build()
line(f"lockbox holds {x(sc.b.balance(sc.lockbox))} XRP, revenue history is on the ledger")
sc.lock()
st = sc.b.lockbox_state(sc.lockbox)
line(f"locked: master key off = {st['master_disabled']}, quorum {st['quorum']} of {len(st['signers'])} signers")
o = sc.quote()
line(f"underwriting: score {o.score}, tier {o.tier}, offer {x(o.principal_drops)} XRP, installment {x(o.installment_drops)} XRP")
for r in o.reasons:
    line("   " + r)
sc.borrow()
s = sc.state()["lockbox"]
line(f"loan funded. lockbox {x(s['balance'])} XRP, free cash {x(s['free_cash'])} XRP")

head("The borrower tries to take everything")
v, _ = sc.agent.withdraw(s["balance"] - sc.b.reserve(sc.lockbox))
line("referee: " + ("signs" if v.allowed else "REFUSES"))
line("   " + v.reason)

head("Good path: revenue keeps coming")
sc.agent.withdraw(s["free_cash"])
lender0 = sc.b.balance(sc.lender)
sc.run(6 * 120 + 60, step=30, revenue_per_step=xrp_to_drops(10))
st = sc.state()
line(f"loan {st['status']}, all 6 installments collected without the borrower signing anything")
line(f"lender earned {x(sc.b.balance(sc.lender) - lender0)} XRP in service fees (interest on a 12 minute loan is a few drops, so the demo income is the fee)")

head("Bad path: same borrower profile, takes free cash, then revenue stops")
events.reset()
sc = build()
sc.lock()
sc.quote()
sc.borrow()
sc.agent.withdraw(sc.state()["lockbox"]["free_cash"])
cover0 = sc.state()["broker"]["cover"]
sc.run(6 * 120, step=30)
st = sc.state()
line(f"loan {st['status']}. first-loss cover {x(cover0)} -> {x(st['broker']['cover'])} XRP, vault {x(st['vault']['total'])} XRP (started 300)")
for e in events.recent():
    if e["kind"] in ("loan_pay", "short", "impair", "default", "recovery"):
        line(f"   {e['kind']}: " + ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("i", "ts", "kind")))
