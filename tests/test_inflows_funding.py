"""The payment that created the account is an opening deposit, not revenue (found on the first Devnet run:
the 100 XRP faucet payment made the lockbox look like one huge customer and the loan was declined)."""
from lockbox.inflows import created_account, parse_account_tx

LB, FAUCET, CUST = "rLockbox", "rFaucet", "rCustomer"


def pay(sender, drops, date, creates=False):
    nodes = [{"ModifiedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {"Account": sender}}}]
    if creates:
        nodes.append({"CreatedNode": {"LedgerEntryType": "AccountRoot", "NewFields": {"Account": LB, "Balance": str(drops), "Sequence": 1}}})
    else:
        nodes.append({"ModifiedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {"Account": LB}}})
    return {"validated": True, "hash": f"H{date}",
            "tx_json": {"TransactionType": "Payment", "Account": sender, "Destination": LB, "DeliverMax": str(drops), "date": date},
            "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": str(drops), "AffectedNodes": nodes}}


def test_account_creating_payment_is_not_revenue():
    got = parse_account_tx([pay(FAUCET, 100_000_000, 1000, creates=True), pay(CUST, 2_500_000, 1100)], LB)
    assert [(i.sender, i.drops) for i in got] == [(CUST, 2_500_000)]


def test_later_payments_from_the_funder_still_count():
    got = parse_account_tx([pay(FAUCET, 100_000_000, 1000, creates=True), pay(FAUCET, 3_000_000, 1200)], LB)
    assert [(i.sender, i.drops) for i in got] == [(FAUCET, 3_000_000)]


def test_creating_some_other_account_does_not_hide_a_payment():
    e = pay(CUST, 2_000_000, 1000)
    e["meta"]["AffectedNodes"].append({"CreatedNode": {"LedgerEntryType": "AccountRoot", "NewFields": {"Account": "rSomeoneElse"}}})
    assert not created_account(e["meta"], LB)
    assert len(parse_account_tx([e], LB)) == 1
