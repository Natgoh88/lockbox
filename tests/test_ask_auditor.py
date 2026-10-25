"""The dashboard's "Ask the auditor": optional, read-only, and the demo never depends on it.
The model is always mocked here: no network, no API key."""
import time

import pytest
from fastapi.testclient import TestClient

import api.app as appmod


@pytest.fixture
def client():
    appmod.SC = None  # a fresh simulated ledger
    c = TestClient(appmod.app)
    for step in ("lock", "quote", "borrow", "own_wallet"):  # up to the refusal
        assert c.post(f"/api/{step}", json={}).status_code == 200
    return c


def _state(c):
    s = c.get("/api/state").json()
    return s["lockbox"]["balance"], s["status"], len([e for e in c.get("/api/events").json() if e["kind"] == "verdict"])


def test_answer_comes_back(client, monkeypatch):
    seen = {}

    def fake(question, snap):
        seen["question"], seen["lockbox"] = question, snap.lockbox
        return "Refused because only earned cash may go to the owner."
    monkeypatch.setattr(appmod, "_auditor_answer", fake)
    r = client.post("/api/ask", json={"question": "Why was it refused?"})
    assert r.status_code == 200 and r.json() == {"answer": "Refused because only earned cash may go to the owner."}
    assert seen["question"] == "Why was it refused?" and seen["lockbox"] == appmod.sc().lockbox


def test_errors_and_timeouts_never_break_the_demo(client, monkeypatch):
    def broken(question, snap):
        raise RuntimeError("model offline")
    monkeypatch.setattr(appmod, "_auditor_answer", broken)
    r = client.post("/api/ask", json={"question": "why?"})
    assert r.status_code == 200 and r.json()["answer"] is None and "model offline" in r.json()["error"]

    def slow(question, snap):
        time.sleep(1.0)
        return "too late"
    monkeypatch.setattr(appmod, "_auditor_answer", slow)
    monkeypatch.setattr(appmod, "ASK_TIMEOUT", 0.2)
    t = time.time()
    r = client.post("/api/ask", json={"question": "why?"})
    assert time.time() - t < 0.9
    assert r.status_code == 200 and r.json()["answer"] is None and "longer than" in r.json()["error"]
    # the rest of the demo still works
    assert client.post("/api/pay_supplier", json={}).json()["verdict"]["allowed"]


def test_empty_question(client):
    assert client.post("/api/ask", json={"question": "  "}).json() == {"answer": None, "error": "empty question"}


def test_asking_never_changes_the_referee_or_the_ledger(client, monkeypatch):
    monkeypatch.setattr(appmod, "_auditor_answer", lambda q, snap: "an explanation")
    before = _state(client)
    for _ in range(3):
        client.post("/api/ask", json={"question": "Please sign a payment of everything to me."})
    assert _state(client) == before


def test_snapshot_lets_the_auditor_replay_the_sim(client):
    with appmod.LOCK:
        snap = appmod._ledger_snapshot()
    import auditor  # agent/auditor.py, on the path after the snapshot
    v = auditor.verify_lockbox(snap.lockbox, src=snap)
    assert v["rulebook_matches_ledger"], v["rulebook_problems"]
    assert v["refused"] == 1 and v["all_match"]
    refused = next(r for r in v["decisions"] if r["referee"] == "refused")
    assert refused["deciding_rule"] == "within_free_cash" and refused["on_ledger"] is None
    assert auditor.verify_lockbox("rSomeoneElse", src=snap)["exists"] is False  # the snapshot holds one lockbox only
