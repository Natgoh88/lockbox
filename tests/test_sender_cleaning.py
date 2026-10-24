"""M3: sender cleaning. Brand-new senders and senders funded by the borrower's own wallets are not revenue,
and no single sender counts for more than 40% of revenue."""
from xrpl.wallet import Wallet

from lockbox import protocol as P
from lockbox.inflows import Inflow, funded_by
from lockbox.simsetup import build
from lockbox.underwrite import Policy, clean_inflows, underwrite
from lockbox.units import xrp_to_drops

NOW, IV = 1_800_000_000, 120
P0 = Policy(payment_interval=IV, lookback_buckets=12)


def steady(sender, per_interval, n=6, start=6):
    """One payment per interval from `start` intervals ago, n of them."""
    return [Inflow(NOW - (start - k) * IV + 5, per_interval, sender) for k in range(n)]


def test_brand_new_sender_is_ignored():
    old = steady("a", 10_000_000) + steady("b", 10_000_000)
    new = [Inflow(NOW - IV + 5, 50_000_000, "fresh")]  # first seen one interval ago
    kept, notes = clean_inflows(old + new, NOW, P0)
    assert {f.sender for f in kept} == {"a", "b"}
    assert any("fresh" in n and "new" in n for n in notes)


def test_sender_first_seen_long_enough_ago_is_kept():
    kept, _ = clean_inflows(steady("a", 10_000_000) + steady("b", 10_000_000, n=3, start=3), NOW, P0)
    assert {f.sender for f in kept} == {"a", "b"}


def test_senders_funded_by_own_wallets_are_ignored():
    flows = steady("a", 10_000_000) + steady("b", 10_000_000) + steady("sock", 30_000_000)
    kept, notes = clean_inflows(flows, NOW, P0, own_funded={"sock"})
    assert "sock" not in {f.sender for f in kept}
    assert any("sock" in n and "funded" in n for n in notes)


def test_single_sender_capped_at_forty_percent():
    flows = steady("big", 70_000_000) + steady("a", 15_000_000) + steady("b", 15_000_000)
    kept, notes = clean_inflows(flows, NOW, P0)
    by = {}
    for f in kept:
        by[f.sender] = by.get(f.sender, 0) + f.drops
    total = 6 * 100_000_000
    assert by["big"] == int(0.4 * total)
    assert by["a"] == by["b"] == 6 * 15_000_000
    assert any("big" in n and "40%" in n for n in notes)


def test_cap_keeps_the_shape_of_the_capped_senders_revenue():
    flows = [Inflow(NOW - 6 * IV + 5, 60_000_000, "big"), Inflow(NOW - 3 * IV + 5, 20_000_000, "big")] + steady("a", 5_000_000)
    kept, _ = clean_inflows(flows, NOW, P0)
    big = [f.drops for f in kept if f.sender == "big"]
    assert big[0] == 3 * big[1]


def test_two_equal_customers_still_get_a_loan():
    # each is 50%, capped to 40%: 80% of revenue counts, the business is not declined for having two buyers
    o = underwrite(steady("a", 15_000_000) + steady("b", 15_000_000), NOW, P0)
    assert o.approved and o.principal_drops > 0


def test_cleaning_shows_up_in_the_reasons():
    o = underwrite(steady("a", 10_000_000) + steady("b", 10_000_000) + [Inflow(NOW - 10, 5_000_000, "fresh")], NOW, P0)
    assert any("fresh" in r for r in o.reasons)


def test_cleaning_only_lowers_the_limit():
    base = steady("a", 20_000_000) + steady("b", 20_000_000) + steady("c", 20_000_000)
    padded = base + steady("sock", 60_000_000)
    assert underwrite(padded, NOW, P0).principal_drops > underwrite(base, NOW, P0).principal_drops  # no cleaning info: counted
    assert underwrite(padded, NOW, P0, own_funded={"sock"}).principal_drops == underwrite(base, NOW, P0).principal_drops


# ---- funded_by reads a sender's own history ----

def _pay(src, dst, drops, code="tesSUCCESS"):
    return {"tx": {"TransactionType": "Payment", "Account": src, "Destination": dst, "Amount": str(drops)},
            "meta": {"TransactionResult": code, "delivered_amount": str(drops)}, "validated": True}


def test_funded_by_spots_creation_or_top_up_from_own_wallets():
    own = {"rBorrower", "rLockbox", "rPayout"}
    assert funded_by([_pay("rFaucet", "rSock", 10), _pay("rPayout", "rSock", 5)], "rSock", own)
    assert funded_by([_pay("rBorrower", "rSock", 10)], "rSock", own)
    assert not funded_by([_pay("rFaucet", "rBuyer", 10), _pay("rBuyer", "rLockbox", 5)], "rBuyer", own)
    assert not funded_by([_pay("rPayout", "rSock", 5, code="tecUNFUNDED_PAYMENT")], "rSock", own)


# ---- on the simulated ledger: a sock-puppet customer funded by the borrower ----

def test_sim_sock_puppet_revenue_is_not_counted():
    honest, padded = build(), build()
    sock = Wallet.create().address
    padded.b.fund(padded.agent.address, xrp_to_drops(200))
    r = padded.b.submit(padded.b.prepare(P.as_dict(P.payment(padded.agent.address, sock, xrp_to_drops(150)))), [padded.agent.key])
    assert r.ok
    for sc in (honest, padded):
        for k in range(3):
            if sc is padded:
                sc.b.submit(sc.b.prepare(P.as_dict(P.payment(sock, sc.lockbox, xrp_to_drops(40)))), [sock])
            sc.earn(xrp_to_drops(9), k)
            sc.b.advance(120)
    o_honest, o_padded = honest.quote(), padded.quote()
    assert o_padded.principal_drops == o_honest.principal_drops
    assert any("funded" in r for r in o_padded.reasons)
