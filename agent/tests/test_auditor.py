"""Lockbox auditor tests. No network: the ledger answers come from tests/fixtures (public Devnet data)."""
import json
import sys
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))

import answer  # noqa: E402
import auditor  # noqa: E402
import build  # noqa: E402
import ledger  # noqa: E402

GOOD = "rnC9yaPArTeyTijnNbhgGajUTd2WHr4CzG"
DEFAULT = "r4UuCzXSK6UReG5enELvQQEwkudqnBWNXe"
REAL_RPC = ledger.rpc
FIXTURE = json.loads((AGENT / "tests" / "fixtures" / "devnet_rpc.json").read_text())


@pytest.fixture(autouse=True)
def recorded_ledger(monkeypatch):
    def fake(method, params, **kw):
        if method not in ledger.READ_METHODS:
            raise PermissionError(method)
        return FIXTURE[json.dumps([method, params], sort_keys=True)]
    monkeypatch.setattr(ledger, "rpc", fake)
    monkeypatch.setenv("LOCKBOX_AUDITOR_MODE", "offline")


def test_good_path_lockbox_all_signed_and_verify_matches():
    v = auditor.verify_lockbox(GOOD)
    assert v["rulebook_matches_ledger"] and v["rulebook_problems"] == []
    assert v["signed"] == 12 and v["refused"] == 0 and v["all_match"]
    rules = [r["deciding_rule"] for r in v["decisions"]]
    assert rules[0] == "broker_rulebook_hash" and rules.count("covered_by_balance") == 7
    supplier = next(r for r in v["decisions"] if "approved supplier" in r["transaction"])
    assert supplier["deciding_rule"] == "within_principal" and supplier["on_ledger"]["result"] == "tesSUCCESS"
    assert v["decisions"][11]["same_transaction_as_decision"] == 11  # the release was reviewed twice, sent once


def test_default_path_refusal_is_explained():
    v = auditor.verify_lockbox(DEFAULT)
    refused = [r for r in v["decisions"] if r["referee"] == "refused"]
    assert len(refused) == 1 and refused[0]["deciding_rule"] == "within_free_cash" and refused[0]["on_ledger"] is None
    assert v["all_match"]
    text = answer.answer(f"Why was the payment refused on {DEFAULT}?")
    assert "REFUSED" in text and "within_free_cash" in text and "128.21 XRP" in text and "not on the ledger" in text


def test_answer_says_which_payments_were_signed_and_that_verify_matched():
    text = answer.answer(f"What did the referee do on {GOOD}?")
    assert "signed 12 and refused 0" in text
    assert "within_principal" in text and "covered_by_balance" in text and "tecEXPIRED" in text
    assert "every decision replays to the same verdict" in text
    assert "—" not in text and "–" not in text


def test_history_names_the_referee_and_who_signed():
    h = auditor.get_account_history(GOOD)
    assert h["master_key_disabled"] and h["regular_key"]  # released after repayment
    pays = [t for t in h["transactions"] if t["type"] == "LoanPay" and t["result"] == "tesSUCCESS"]
    assert len(pays) == 6 and all(any(s.startswith("referee ") for s in t["signed_by"]) for t in pays)


def test_rulebook_fingerprint_recomputes():
    r = auditor.get_rulebook()
    assert r["fingerprint_consistent"] and r["problems"] == [] and len(r["file_sha256"]) == 64


def test_ledger_client_is_read_only():
    # the real client, not the recorded one: it refuses before any network call
    for method in ("submit", "sign", "submit_multisigned", "sign_for", "wallet_propose"):
        with pytest.raises(PermissionError):
            REAL_RPC(method, {})


def test_no_address_asks_for_one():
    text = answer.answer("hello")
    assert "Give me a lockbox address" in text and GOOD in text


def test_clean_removes_dashes():
    assert answer.clean("a — b–c") == "a, b-c"


def test_build_ships_no_secrets_or_signing_code(tmp_path):
    out = build.build()
    assert build.check(out) == []
    names = {p.name for p in out.rglob("*.py")}
    assert not names & build.FORBIDDEN_MODULES and "verify.py" in names


# fake secrets, assembled at runtime so no seed- or key-shaped string is written in the repo
FAKE_SEED = "sEd" + "TM1uX8pu2do5XvTnutH6HsouMaM2"
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS's documented example key


@pytest.mark.parametrize("name,content", [
    (".wallets.json", "{}"),
    ("notes.txt", f"seed {FAKE_SEED} here"),
    ("x.py", "from xrpl." + "wallet import Wallet\n"),
    ("chain.py", "# signing code\n"),
    ("k.txt", FAKE_AWS_KEY),
    ("d.json", '{"se' + 'ed": "x"}'),
])
def test_build_check_fails_on_secrets(tmp_path, name, content):
    (tmp_path / name).write_text(content)
    assert build.check(tmp_path)


def test_strands_agent_has_the_three_read_only_tools():
    import main
    assert main.mode() == "offline"
    out = main.invoke({"prompt": f"refused payments on {DEFAULT}"})
    assert out["mode"] == "offline" and "within_free_cash" in out["result"]
    names = {t.tool_name for t in (main.get_account_history, main.verify_lockbox, main.get_rulebook)}
    assert names == {"get_account_history", "verify_lockbox", "get_rulebook"}
