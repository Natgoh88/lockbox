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
