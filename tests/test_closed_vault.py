"""Closed-ended vault (LendingProtocolV1_1), as 02_lend_setup.py now creates on Devnet: deposits only before the
subscription date, loans only after it, and every loan must end at least 60 s before the redemption date."""
from lockbox import protocol as P
from lockbox.simsetup import build
from lockbox.units import xrp_to_drops


def test_closed_vault_story_runs_end_to_end():
    sc = build(closed_ended=True, subscribe_s=300, invest_s=4500)
    v = sc.state()["vault"]
    assert v["closed_ended"] and v["subscription"] < sc.b.now() < v["redemption"]
    sc.lock()
    sc.quote()
    assert sc.borrow().ok
    sc.run(6 * 120 + 60, step=30, revenue_per_step=xrp_to_drops(10))
    assert sc.state()["status"] == "paid"


def test_deposit_after_the_subscription_date_refused():
    sc = build(closed_ended=True, subscribe_s=300, invest_s=4500)  # building takes 720 s of revenue history
    r = sc.b.submit(sc.b.prepare(P.as_dict(P.vault_deposit(sc.servicer.address, sc.vault_id, xrp_to_drops(10)))), [sc.lender])
    assert r.code == "tecNO_PERMISSION"


def test_loan_before_the_subscription_date_refused():
    sc = build(closed_ended=True, subscribe_s=1000, invest_s=4500)  # building takes 720 s, so subscription is still open
    sc.lock()
    sc.quote()
    r = sc.borrow()
    assert not r.ok and r.code == "tecNO_PERMISSION"
    sc.b.advance(300)  # past the subscription date, revenue history still inside the underwriting window
    sc.quote()
    assert sc.borrow().ok


def test_loan_ending_too_close_to_redemption_refused():
    # redemption 1300 s after setup; the loan would start at 720 s and end at 1440 s
    sc = build(closed_ended=True, subscribe_s=300, invest_s=1000)
    sc.lock()
    sc.quote()
    r = sc.borrow()
    assert not r.ok and r.code == "tecNO_PERMISSION"


def test_build_makes_a_closed_ended_vault_like_devnet(sc):
    v = sc.state()["vault"]
    assert v["closed_ended"] is True and v["redemption"] - v["subscription"] == 4500
    assert sc.b.lockbox_state(sc.lockbox)["regular_key"] is None  # an address or None, never ""


def test_open_ended_vault_still_works_when_asked_for():
    sc = build(closed_ended=False)
    assert sc.state()["vault"]["closed_ended"] is False
    r = sc.b.submit(sc.b.prepare(P.as_dict(P.vault_deposit(sc.servicer.address, sc.vault_id, xrp_to_drops(10)))), [sc.lender])
    assert r.ok
