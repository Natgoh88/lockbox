"""Transaction builders and meta parsing. Pure: no network, no signing.

Builders return validated xrpl-py models. Use `as_dict(tx)` for the JSON form the sim and the
covenant look at. Amounts are XRP drops as strings, rates are XRPL 1/10 basis points.
"""
from __future__ import annotations

from xrpl.models.currencies import XRP
from xrpl.models.transactions import (
    AccountSet,
    LoanBrokerCoverDeposit,
    LoanBrokerSet,
    LoanManage,
    LoanPay,
    LoanSet,
    Payment,
    SignerListSet,
    VaultCreate,
    VaultDeposit,
)
from xrpl.models.transactions.account_set import AccountSetAsfFlag
from xrpl.models.transactions.loan_manage import LoanManageFlag
from xrpl.models.transactions.signer_list_set import SignerEntry

from .underwrite import Offer
from .units import unix_to_ripple


def as_dict(tx) -> dict:
    d = tx.to_xrpl()
    if "Flags" in d:
        d["Flags"] = int(d["Flags"])
    return d


# ---- vault and broker (the lender side) ----

def vault_create(owner: str, closed_ended: bool = False, subscription_unix: int | None = None,
                 redemption_unix: int | None = None, assets_maximum: str | None = None) -> VaultCreate:
    kw: dict = {"account": owner, "asset": XRP()}
    if assets_maximum:
        kw["assets_maximum"] = assets_maximum
    if closed_ended:
        if subscription_unix is None or redemption_unix is None:
            raise ValueError("closed-ended vaults need subscription and redemption times")
        kw.update(vault_kind=1, subscription_date=unix_to_ripple(subscription_unix), redemption_date=unix_to_ripple(redemption_unix))
    return VaultCreate(**kw)


def vault_deposit(account: str, vault_id: str, drops: int) -> VaultDeposit:
    return VaultDeposit(account=account, vault_id=vault_id, amount=str(drops))


def broker_set(owner: str, vault_id: str, debt_maximum: int, cover_rate_minimum: int, cover_rate_liquidation: int,
               management_fee_rate: int = 0) -> LoanBrokerSet:
    return LoanBrokerSet(
        account=owner,
        vault_id=vault_id,
        debt_maximum=str(debt_maximum),
        cover_rate_minimum=cover_rate_minimum,
        cover_rate_liquidation=cover_rate_liquidation,
        management_fee_rate=management_fee_rate,
    )


def cover_deposit(owner: str, broker_id: str, drops: int) -> LoanBrokerCoverDeposit:
    return LoanBrokerCoverDeposit(account=owner, loan_broker_id=broker_id, amount=str(drops))


# ---- the loan ----

def loan_set(lender: str, lockbox: str, broker_id: str, offer: Offer) -> LoanSet:
    """Build the LoanSet from an approved offer. Account = lender (single signature), Counterparty = lockbox
    (multi-signed by two of its three signers inside CounterpartySignature)."""
    if not offer.approved:
        raise ValueError("cannot build a loan from a declined offer")
    kw = dict(
        account=lender,
        counterparty=lockbox,
        loan_broker_id=broker_id,
        principal_requested=str(offer.principal_drops),
        interest_rate=offer.interest_rate_units,
        payment_total=offer.payment_total,
        payment_interval=offer.payment_interval,
        grace_period=offer.grace_period,
    )
    if offer.origination_fee_drops:
        kw["loan_origination_fee"] = str(offer.origination_fee_drops)
    if offer.service_fee_drops:
        kw["loan_service_fee"] = str(offer.service_fee_drops)
    return LoanSet(**kw)


def loan_pay(account: str, loan_id: str, drops: int, late: bool = False) -> LoanPay:
    if late:
        from xrpl.models.transactions.loan_pay import LoanPayFlag
        return LoanPay(account=account, loan_id=loan_id, amount=str(drops), flags=LoanPayFlag.TF_LOAN_LATE_PAYMENT)
    return LoanPay(account=account, loan_id=loan_id, amount=str(drops))


def loan_manage(account: str, loan_id: str, action: str) -> LoanManage:
    flag = {"default": LoanManageFlag.TF_LOAN_DEFAULT, "impair": LoanManageFlag.TF_LOAN_IMPAIR,
            "unimpair": LoanManageFlag.TF_LOAN_UNIMPAIR}[action]
    return LoanManage(account=account, loan_id=loan_id, flags=flag)


# ---- the lockbox ----

def lockdown_signer_list(lockbox: str, borrower_signer: str, lender_signer: str, policy_signer: str) -> SignerListSet:
    """2-of-3: borrower agent, lender servicer, policy referee. Any two move money."""
    return SignerListSet(
        account=lockbox,
        signer_quorum=2,
        signer_entries=[SignerEntry(account=a, signer_weight=1) for a in (borrower_signer, lender_signer, policy_signer)],
    )


def lockdown_disable_master(lockbox: str) -> AccountSet:
    return AccountSet(account=lockbox, set_flag=AccountSetAsfFlag.ASF_DISABLE_MASTER)


def payment(account: str, destination: str, drops: int) -> Payment:
    return Payment(account=account, destination=destination, amount=str(drops))


# ---- meta parsing ----

def created_ids(meta: dict) -> dict[str, str]:
    """{'Vault': index, 'LoanBroker': index, 'Loan': index} for ledger objects this tx created."""
    out: dict[str, str] = {}
    for n in meta.get("AffectedNodes", []):
        c = n.get("CreatedNode")
        if c and c.get("LedgerEntryType") in ("Vault", "LoanBroker", "Loan"):
            out[c["LedgerEntryType"]] = c["LedgerIndex"]
    return out
