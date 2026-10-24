"""The payment that created the lockbox account is the owner's own money: spendable by the owner, shown as owner_funds."""
from lockbox import buckets as B
from lockbox.units import xrp_to_drops

LB, LENDER, BUYER = "rLockbox", "rLender", "rBuyer"


def _pay(src, dst, drops, created=False, ledger=1):
    nodes = [{"CreatedNode": {"LedgerEntryType": "AccountRoot", "NewFields": {"Account": dst, "Balance": str(drops)}}}] if created else \
        [{"ModifiedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {"Account": dst, "Balance": "0"}, "PreviousFields": {"Balance": "0"}}}]
    tx = {"TransactionType": "Payment", "Account": src, "Destination": dst, "Amount": str(drops), "Fee": "10"}
    meta = {"TransactionResult": "tesSUCCESS", "delivered_amount": str(drops)}
    if created:
        meta["AffectedNodes"] = nodes
    return {"tx": tx, "meta": meta, "validated": True, "ledger_index": ledger}


CFG = B.Config(lockbox=LB, lender=LENDER, account_reserve=1_000_000, fee_buffer=5_000)


def test_opening_deposit_is_owner_funds_not_earned():
    hist = [_pay("rFaucet", LB, 100_000_000, created=True, ledger=1), _pay(BUYER, LB, 20_000_000, ledger=2)]
    b = B.replay(hist, 120_000_000, CFG, B.LoanFacts.none())
    # the reserve and fee buffer come out of the owner's opening deposit first
    assert b.owner_funds == 100_000_000 - 1_005_000
    assert b.earned_free_cash == 20_000_000


def test_owner_funds_plus_earned_equals_the_old_free_cash():
    hist = [_pay("rFaucet", LB, 100_000_000, created=True, ledger=1), _pay(BUYER, LB, 20_000_000, ledger=2)]
    b = B.replay(hist, 120_000_000, CFG, B.LoanFacts.none())
    old = B.replay(hist[1:], 120_000_000, CFG, B.LoanFacts.none())  # without the creating entry, it all counted as earned
    assert b.owner_funds + b.earned_free_cash == old.earned_free_cash
    assert "owner_funds" in b.to_dict()


def test_no_creating_entry_no_owner_funds():
    b = B.replay([_pay(BUYER, LB, 20_000_000)], 20_000_000, CFG, B.LoanFacts.none())
    assert b.owner_funds == 0


def test_sim_lockbox_shows_owner_funds_and_the_owner_can_withdraw_them(sc):
    st = sc.state()
    assert "owner_funds" in st["buckets"]
    sc.lock()
    before = sc.state()["buckets"]
    assert before["owner_funds"] > 0
    v, r = sc.agent.withdraw(before["owner_funds"] + before["earned_free_cash"])
    assert v.allowed and r.ok, v.reason
    after = sc.state()["buckets"]
    assert after["owner_funds"] == 0 and after["earned_free_cash"] == 0
