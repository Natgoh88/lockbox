"""A small in-memory ledger that behaves like the parts of XRPL Lockbox uses.

It exists so the whole story (underwrite, lock down, lend, repay, freeze, default, recover) runs
and is tested with no network. It models signer quorums, a disabled master key, vault and broker
accounting and a fixed-term amortizing loan. It is NOT the real protocol: amortization is a plain
annuity with simple interest, so the real servicer must read PeriodicPayment from the ledger, and
the real ledger will differ in rounding and fees. Time moves only when you call advance().
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .backend import Result, addr_of
from .covenant import LoanView
from .health import BrokerView, VaultView, first_loss_if_default
from .underwrite import annuity_payment
from .units import RATE_UNITS, period_rate, ripple_to_unix, unix_to_ripple

ASF_DISABLE_MASTER = 4
LEDGER_SECONDS = 4  # a new ledger closes every few seconds on XRPL
FIRST_LEDGER = 1_000
SIM_LAST_LEDGER_OFFSET = 10  # prepare() fills LastLedgerSequence this far ahead, the referee's maximum


@dataclass
class Account:
    balance: int = 0
    signers: dict[str, int] = field(default_factory=dict)
    quorum: int = 0
    master_disabled: bool = False
    sequence: int = 1
    regular_key: str = ""


@dataclass
class SimLoan:
    id: str
    broker_id: str
    borrower: str
    principal: int
    principal_out: int
    rate_units: int
    interval: int
    grace: int
    total: int
    remaining: int
    periodic: int
    service_fee: int
    next_due: int
    start: int
    impaired: bool = False
    defaulted: bool = False
    total_owed: int = 0


class SimChain:
    def __init__(self, start: int = 1_800_000_000, reserve: int = 2_000_000):
        self.t = start
        self._start = start
        self._reserve = reserve
        self.accounts: dict[str, Account] = {}
        self.vaults: dict[str, dict] = {}
        self.brokers: dict[str, dict] = {}
        self.loans: dict[str, SimLoan] = {}
        self.history: dict[str, list[dict]] = {}
        self._n = 0

    # ---- Backend interface ----
    def now(self) -> int:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += int(seconds)

    def ledger_index(self) -> int:
        """The current open ledger. Moves with time: one ledger every LEDGER_SECONDS."""
        return FIRST_LEDGER + (self.t - self._start) // LEDGER_SECONDS

    def next_sequence(self, addr: str) -> int:
        return self.accounts.get(addr, Account()).sequence

    def fund(self, addr: str, drops: int) -> None:
        self.accounts.setdefault(addr, Account()).balance += drops

    def balance(self, addr: str) -> int:
        return self.accounts.get(addr, Account()).balance

    def reserve(self, addr: str) -> int:
        return self._reserve

    def account_tx(self, addr: str) -> list[dict]:
        return list(self.history.get(addr, []))

    def lockbox_state(self, addr: str) -> dict:
        a = self.accounts.get(addr, Account())
        return {"master_disabled": a.master_disabled, "signers": dict(a.signers), "quorum": a.quorum, "regular_key": a.regular_key or None}

    def prepare(self, tx: dict, signers_count: int = 0) -> dict:
        out = dict(tx)
        out.setdefault("Fee", str(10 * (1 + signers_count)))
        out.setdefault("Sequence", self.next_sequence(out["Account"]))
        out.setdefault("LastLedgerSequence", self.ledger_index() + SIM_LAST_LEDGER_OFFSET)
        return out

    def loan_view(self, loan_id: str) -> LoanView | None:
        ln = self.loans.get(loan_id)
        if ln is None:
            return None
        return LoanView(loan_id=ln.id, borrower=ln.borrower, periodic_payment=ln.periodic, service_fee=ln.service_fee,
                        next_due=ln.next_due, payment_remaining=ln.remaining, grace_period=ln.grace,
                        defaulted=ln.defaulted, impaired=ln.impaired, total_owed=ln.total_owed)

    def broker_view(self, broker_id: str) -> BrokerView:
        b = self.brokers[broker_id]
        return BrokerView(cover_available=b["cover"], debt_total=b["debt_total"], cover_rate_minimum=b["cover_min"],
                          cover_rate_liquidation=b["cover_liq"], data=b.get("data", ""))

    def vault_view(self, vault_id: str) -> VaultView:
        v = self.vaults[vault_id]
        return VaultView(assets_total=v["total"], assets_available=v["available"], loss_unrealized=v["loss_unrealized"])

    # ---- helpers ----
    def _id(self, kind: str) -> str:
        self._n += 1
        return hashlib.sha256(f"{kind}{self._n}".encode()).hexdigest().upper()

    def _authorized(self, account: str, signers: set[str]) -> str | None:
        """None if the signers may act for the account, otherwise the XRPL failure code."""
        a = self.accounts.get(account)
        if a is None:
            return "terNO_ACCOUNT"
        if account in signers and not a.master_disabled:
            return None
        if a.regular_key and signers == {a.regular_key}:
            return None
        if a.signers:
            weight = sum(w for s, w in a.signers.items() if s in signers)
            if weight >= a.quorum:
                return None
            return "tefBAD_QUORUM"
        if account in signers and a.master_disabled:
            return "tefMASTER_DISABLED"
        return "tefBAD_AUTH"

    def _log(self, tx: dict, code: str, meta_extra: dict | None = None, parties: list[str] | None = None) -> Result:
        h = hashlib.sha256((str(tx) + str(self.t) + str(self._n)).encode()).hexdigest().upper()
        self._n += 1
        meta = {"TransactionResult": code}
        if meta_extra:
            meta.update(meta_extra)
        meta.setdefault("TransactionIndex", self._n)
        entry = {"tx": {**tx, "date": unix_to_ripple(self.t), "hash": h}, "meta": meta, "validated": True, "hash": h,
                 "ledger_index": self.ledger_index()}
        for p in {tx.get("Account"), tx.get("Destination"), tx.get("Counterparty"), *(parties or [])}:
            if p:
                self.history.setdefault(p, []).append(entry)
        return Result(ok=code == "tesSUCCESS", code=code, tx=tx, meta=meta, hash=h)

    def _charge_fee(self, tx: dict) -> int:
        fee = int(tx.get("Fee", 10))
        a = self.accounts[tx["Account"]]
        a.balance -= fee
        return fee

    # ---- submit ----
    def submit(self, tx: dict, signers: list, counterparty_signers: list | None = None) -> Result:
        addrs = {addr_of(k) for k in signers}
        cps = {addr_of(k) for k in (counterparty_signers or [])}
        ttype = tx["TransactionType"]
        acct = tx["Account"]
        if acct not in self.accounts:
            return self._log(tx, "terNO_ACCOUNT")
        if "LastLedgerSequence" in tx and self.ledger_index() > int(tx["LastLedgerSequence"]):
            return self._log(tx, "tefMAX_LEDGER")
        if "Sequence" in tx:
            want = self.accounts[acct].sequence
            if int(tx["Sequence"]) < want:
                return self._log(tx, "tefPAST_SEQ")
            if int(tx["Sequence"]) > want:
                return self._log(tx, "terPRE_SEQ")
        if ttype == "LoanSet":
            if acct not in addrs or self._authorized(acct, {acct}) is not None:
                return self._log(tx, "tefBAD_AUTH")
            bad = self._authorized(tx["Counterparty"], cps)
            if bad:
                return self._log(tx, bad)
        else:
            bad = self._authorized(acct, addrs)
            if bad:
                return self._log(tx, bad)
        fn = getattr(self, f"_do_{ttype}", None)
        if fn is None:
            return self._log(tx, "temUNKNOWN")
        if self.accounts[acct].balance < int(tx.get("Fee", 10)):
            return self._log(tx, "terINSUF_FEE_B")
        before = {a: x.balance for a, x in self.accounts.items()}
        r = fn(tx)
        if r.code.startswith(("tes", "tec")):  # applied to the ledger: the sequence is used up
            self.accounts[acct].sequence += 1
        nodes = r.meta.setdefault("AffectedNodes", [])
        for a, x in self.accounts.items():
            if x.balance != before.get(a, 0):
                nodes.append({"ModifiedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {"Account": a, "Balance": str(x.balance)},
                                               "PreviousFields": {"Balance": str(before.get(a, 0))}}})
        return r

    def _do_Payment(self, tx: dict) -> Result:
        amt = int(tx["Amount"])
        a = self.accounts[tx["Account"]]
        if a.balance - amt - int(tx.get("Fee", 10)) < self._reserve:
            self._charge_fee(tx)
            return self._log(tx, "tecUNFUNDED_PAYMENT")
        self._charge_fee(tx)
        a.balance -= amt
        self.accounts.setdefault(tx["Destination"], Account()).balance += amt
        return self._log(tx, "tesSUCCESS", {"delivered_amount": str(amt)})

    def _do_SignerListSet(self, tx: dict) -> Result:
        self._charge_fee(tx)
        a = self.accounts[tx["Account"]]
        if int(tx["SignerQuorum"]) == 0:  # delete the list, but never the last way to sign
            if a.master_disabled and not a.regular_key:
                return self._log(tx, "tecNO_ALTERNATIVE_KEY")
            a.quorum, a.signers = 0, {}
            return self._log(tx, "tesSUCCESS")
        a.quorum = int(tx["SignerQuorum"])
        a.signers = {e["SignerEntry"]["Account"]: int(e["SignerEntry"]["SignerWeight"]) for e in tx["SignerEntries"]}
        return self._log(tx, "tesSUCCESS")

    def _do_AccountSet(self, tx: dict) -> Result:
        self._charge_fee(tx)
        if tx.get("SetFlag") == ASF_DISABLE_MASTER:
            a = self.accounts[tx["Account"]]
            if not a.signers and not a.regular_key:
                return self._log(tx, "tecNO_ALTERNATIVE_KEY")
            a.master_disabled = True
        return self._log(tx, "tesSUCCESS")

    def _do_SetRegularKey(self, tx: dict) -> Result:
        self._charge_fee(tx)
        a = self.accounts[tx["Account"]]
        key = tx.get("RegularKey", "")
        if not key and a.master_disabled and not a.signers:
            return self._log(tx, "tecNO_ALTERNATIVE_KEY")
        a.regular_key = key
        return self._log(tx, "tesSUCCESS")

    def _do_VaultCreate(self, tx: dict) -> Result:
        self._charge_fee(tx)
        vid = self._id("vault")
        self.vaults[vid] = {"owner": tx["Account"], "total": 0, "available": 0, "loss_unrealized": 0}
        return self._log(tx, "tesSUCCESS", {"AffectedNodes": [{"CreatedNode": {"LedgerEntryType": "Vault", "LedgerIndex": vid}}]})

    def _do_VaultDeposit(self, tx: dict) -> Result:
        v = self.vaults[tx["VaultID"]]
        amt = int(tx["Amount"])
        a = self.accounts[tx["Account"]]
        if a.balance - amt < self._reserve:
            return self._log(tx, "tecUNFUNDED")
        self._charge_fee(tx)
        a.balance -= amt
        v["total"] += amt
        v["available"] += amt
        return self._log(tx, "tesSUCCESS")

    def _do_LoanBrokerSet(self, tx: dict) -> Result:
        v = self.vaults.get(tx["VaultID"])
        if v is None or v["owner"] != tx["Account"]:
            return self._log(tx, "tecNO_PERMISSION")
        self._charge_fee(tx)
        bid = self._id("broker")
        self.brokers[bid] = {"owner": tx["Account"], "vault": tx["VaultID"], "cover": 0, "debt_total": 0,
                             "debt_max": int(tx.get("DebtMaximum", 0)) or 10**18,
                             "cover_min": int(tx.get("CoverRateMinimum", 0)), "cover_liq": int(tx.get("CoverRateLiquidation", RATE_UNITS)),
                             "data": tx.get("Data", "")}
        return self._log(tx, "tesSUCCESS", {"AffectedNodes": [{"CreatedNode": {"LedgerEntryType": "LoanBroker", "LedgerIndex": bid}}]})

    def _do_LoanBrokerCoverDeposit(self, tx: dict) -> Result:
        b = self.brokers[tx["LoanBrokerID"]]
        amt = int(tx["Amount"])
        a = self.accounts[tx["Account"]]
        if b["owner"] != tx["Account"]:
            return self._log(tx, "tecNO_PERMISSION")
        if a.balance - amt < self._reserve:
            return self._log(tx, "tecUNFUNDED")
        self._charge_fee(tx)
        a.balance -= amt
        b["cover"] += amt
        return self._log(tx, "tesSUCCESS")

    def _do_LoanSet(self, tx: dict) -> Result:
        b = self.brokers.get(tx["LoanBrokerID"])
        if b is None or b["owner"] != tx["Account"]:
            return self._log(tx, "tecNO_PERMISSION")
        v = self.vaults[b["vault"]]
        principal = int(tx["PrincipalRequested"])
        n = int(tx["PaymentTotal"])
        interval = int(tx["PaymentInterval"])
        if interval < 60:
            return self._log(tx, "tecKILLED")
        if principal > v["available"]:
            return self._log(tx, "tecINSUFFICIENT_FUNDS")
        if b["debt_total"] + principal > b["debt_max"]:
            return self._log(tx, "tecLIMIT_EXCEEDED")
        need = (b["debt_total"] + principal) * b["cover_min"] // RATE_UNITS
        if b["cover"] < need:
            return self._log(tx, "tecINSUFFICIENT_FUNDS")
        self._charge_fee(tx)
        rate = int(tx.get("InterestRate", 0))
        r = period_rate(rate, interval)
        periodic = int(round(annuity_payment(principal, r, n)))
        lid = self._id("loan")
        ln = SimLoan(id=lid, broker_id=tx["LoanBrokerID"], borrower=tx["Counterparty"], principal=principal, principal_out=principal,
                     rate_units=rate, interval=interval, grace=int(tx.get("GracePeriod", 0)), total=n, remaining=n, periodic=periodic,
                     service_fee=int(tx.get("LoanServiceFee", 0)), next_due=self.t + interval, start=self.t, total_owed=periodic * n)
        self.loans[lid] = ln
        v["available"] -= principal
        b["debt_total"] += principal
        orig = int(tx.get("LoanOriginationFee", 0))
        self.accounts[ln.borrower].balance += principal - orig
        self.accounts[b["owner"]].balance += orig
        return self._log(tx, "tesSUCCESS", {"AffectedNodes": [{"CreatedNode": {"LedgerEntryType": "Loan", "LedgerIndex": lid}}]})

    def _do_LoanPay(self, tx: dict) -> Result:
        ln = self.loans.get(tx["LoanID"])
        if ln is None:
            return self._log(tx, "tecNO_ENTRY")
        if tx["Account"] != ln.borrower:
            return self._log(tx, "tecNO_PERMISSION")
        if ln.defaulted or ln.remaining <= 0:
            return self._log(tx, "tecNO_PERMISSION")
        amt = int(tx["Amount"])
        due = ln.periodic + ln.service_fee
        if amt < due:
            return self._log(tx, "tecINSUFFICIENT_PAYMENT")
        if amt > due and amt == ln.total_owed + ln.service_fee:
            return self._pay_off(tx, ln, amt)
        a = self.accounts[ln.borrower]
        if a.balance - due - int(tx.get("Fee", 10)) < self._reserve:
            self._charge_fee(tx)
            return self._log(tx, "tecUNFUNDED_PAYMENT")
        self._charge_fee(tx)
        b = self.brokers[ln.broker_id]
        v = self.vaults[b["vault"]]
        r = period_rate(ln.rate_units, ln.interval)
        interest = int(ln.principal_out * r)
        principal_paid = ln.principal_out if ln.remaining == 1 else max(0, ln.periodic - interest)
        principal_paid = min(principal_paid, ln.principal_out)
        income = ln.periodic
        a.balance -= due
        v["available"] += income
        v["total"] += income - principal_paid
        self.accounts[b["owner"]].balance += ln.service_fee
        ln.principal_out -= principal_paid
        b["debt_total"] -= principal_paid
        ln.remaining -= 1
        ln.next_due += ln.interval
        ln.total_owed = ln.periodic * ln.remaining
        if ln.impaired:
            ln.impaired = False
            v["loss_unrealized"] = 0
        return self._log(tx, "tesSUCCESS", {"delivered_amount": str(due)})

    def _pay_off(self, tx: dict, ln: SimLoan, amt: int) -> Result:
        """Whole loan in one LoanPay (TotalValueOutstanding + LoanServiceFee, no flags): the vault gets
        TotalValueOutstanding, the broker owner the service fee, and the loan is closed."""
        a = self.accounts[ln.borrower]
        if a.balance - amt - int(tx.get("Fee", 10)) < self._reserve:
            self._charge_fee(tx)
            return self._log(tx, "tecUNFUNDED_PAYMENT")
        self._charge_fee(tx)
        b = self.brokers[ln.broker_id]
        v = self.vaults[b["vault"]]
        a.balance -= amt
        v["available"] += ln.total_owed
        v["total"] += ln.total_owed - ln.principal_out
        self.accounts[b["owner"]].balance += ln.service_fee
        b["debt_total"] -= ln.principal_out
        ln.principal_out = 0
        ln.remaining = 0
        ln.total_owed = 0
        if ln.impaired:
            ln.impaired = False
            v["loss_unrealized"] = 0
        return self._log(tx, "tesSUCCESS", {"delivered_amount": str(amt)})

    def _do_LoanManage(self, tx: dict) -> Result:
        ln = self.loans.get(tx["LoanID"])
        if ln is None:
            return self._log(tx, "tecNO_ENTRY")
        b = self.brokers[ln.broker_id]
        v = self.vaults[b["vault"]]
        if tx["Account"] != b["owner"]:
            return self._log(tx, "tecNO_PERMISSION")
        flags = int(tx.get("Flags", 0))
        if flags == 131072:  # impair: only once a payment is past due
            if ln.defaulted or ln.remaining <= 0:
                return self._log(tx, "tecNO_PERMISSION")
            if self.t <= ln.next_due:
                return self._log(tx, "tecTOO_SOON")
            self._charge_fee(tx)
            ln.impaired = True
            v["loss_unrealized"] = ln.principal_out
            return self._log(tx, "tesSUCCESS")
        if flags == 262144:  # unimpair
            self._charge_fee(tx)
            ln.impaired = False
            v["loss_unrealized"] = 0
            return self._log(tx, "tesSUCCESS")
        if flags == 65536:  # default
            if self.t <= ln.next_due + ln.grace:
                return self._log(tx, "tecTOO_SOON")
            if ln.defaulted or ln.remaining <= 0:
                return self._log(tx, "tecNO_PERMISSION")
            self._charge_fee(tx)
            loss = ln.principal_out
            covered = first_loss_if_default(self.broker_view(ln.broker_id), loss)
            b["cover"] -= covered
            v["available"] += covered
            v["total"] += covered - loss
            v["loss_unrealized"] = 0
            b["debt_total"] -= loss
            ln.defaulted = True
            ln.impaired = False
            return self._log(tx, "tesSUCCESS", {"default_covered": str(covered), "default_loss": str(loss)})
        return self._log(tx, "temINVALID_FLAG")
