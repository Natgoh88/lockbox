from lockbox.inflows import parse_account_tx
from lockbox.units import (RIPPLE_EPOCH, drops_to_xrp, pct_to_units, period_rate, ripple_to_unix, unix_to_ripple, units_to_pct,
                           xrp_to_drops, YEAR_SECONDS)

ME = "rMe"


def entry(sender="rA", dest=ME, amt="5000000", delivered="5000000", result="tesSUCCESS", date=800000000, ttype="Payment", validated=True):
    return {"tx": {"TransactionType": ttype, "Account": sender, "Destination": dest, "Amount": amt, "date": date},
            "meta": {"TransactionResult": result, "delivered_amount": delivered}, "validated": validated, "hash": "H"}


def test_unit_roundtrips():
    assert unix_to_ripple(RIPPLE_EPOCH + 5) == 5
    assert ripple_to_unix(5) == RIPPLE_EPOCH + 5
    assert xrp_to_drops("1.5") == 1_500_000
    assert drops_to_xrp(1_500_000) == 1.5
    assert pct_to_units(5.0) == 5000
    assert units_to_pct(5000) == 5.0
    assert abs(period_rate(pct_to_units(10), YEAR_SECONDS) - 0.10) < 1e-9


def test_keeps_only_successful_validated_xrp_payments_to_me():
    rows = [
        entry(),
        entry(result="tecPATH_DRY"),
        entry(dest="rOther"),
        entry(sender=ME),
        entry(delivered={"currency": "USD", "issuer": "rI", "value": "5"}),
        entry(ttype="OfferCreate"),
        entry(validated=False),
    ]
    out = parse_account_tx(rows, ME)
    assert len(out) == 1 and out[0].drops == 5_000_000 and out[0].sender == "rA"


def test_uses_delivered_amount_not_amount_field():
    out = parse_account_tx([entry(amt="9000000", delivered="1000000")], ME)
    assert out[0].drops == 1_000_000


def test_ignores_listed_senders_and_sorts_by_time():
    rows = [entry(sender="rVault", date=5), entry(sender="rA", date=9), entry(sender="rB", date=7)]
    out = parse_account_tx(rows, ME, ignore_senders={"rVault"})
    assert [i.sender for i in out] == ["rB", "rA"]


def test_api_v2_shape():
    row = {"tx_json": {"TransactionType": "Payment", "Account": "rA", "Destination": ME}, "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": "7"},
           "validated": True, "hash": "X", "close_time_iso": "2026-10-24T12:00:00Z"}
    out = parse_account_tx([row], ME)
    assert len(out) == 1 and out[0].drops == 7 and out[0].tx_hash == "X"
