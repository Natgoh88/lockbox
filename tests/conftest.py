import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from lockbox import events
from lockbox.simsetup import build


@pytest.fixture(autouse=True)
def _clean_events():
    events.reset()
    yield


@pytest.fixture
def sc():
    return build()


@pytest.fixture
def loaned(sc):
    sc.lock()
    sc.quote()
    r = sc.borrow()
    assert r.ok, r.code
    return sc


@pytest.fixture
def stocked(sc):
    """A live loan with one supplier approved before LoanSet (the supplier list is frozen once the loan is open)."""
    from xrpl.wallet import Wallet
    from lockbox.units import xrp_to_drops
    sc.supplier = Wallet.create().address
    sc.lock()
    sc.quote()
    sc.approve_payee(sc.supplier, cap_drops=xrp_to_drops(1000), invoice_ref="PO-1")
    r = sc.borrow()
    assert r.ok, r.code
    return sc
