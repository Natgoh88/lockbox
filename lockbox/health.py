"""Read-only health signals a lender or vault depositor would care about."""
from __future__ import annotations

from dataclasses import dataclass

from .covenant import LoanView
from .units import RATE_UNITS


@dataclass
class BrokerView:
    cover_available: int
    debt_total: int
    cover_rate_minimum: int  # 1/10 bp
    cover_rate_liquidation: int = RATE_UNITS
    data: str = ""  # the LoanBroker Data field (hex): Lockbox puts the rulebook hash here


@dataclass
class VaultView:
    assets_total: int
    assets_available: int
    loss_unrealized: int = 0


def required_cover(b: BrokerView) -> int:
    return b.debt_total * b.cover_rate_minimum // RATE_UNITS


def cover_ratio(b: BrokerView) -> float:
    """1.0 means exactly the minimum. Below 1.0 the broker stops lending and fee payouts stop."""
    need = required_cover(b)
    return float("inf") if need == 0 else b.cover_available / need


def first_loss_if_default(b: BrokerView, default_amount: int) -> int:
    """XLS-66: DefaultCovered = min(DebtTotal x CoverRateMinimum x CoverRateLiquidation, DefaultAmount)."""
    cap = b.debt_total * b.cover_rate_minimum // RATE_UNITS * b.cover_rate_liquidation // RATE_UNITS
    return min(cap, default_amount, b.cover_available)


def utilization(v: VaultView) -> float:
    return 0.0 if v.assets_total == 0 else 1 - v.assets_available / v.assets_total


def loan_status(loan: LoanView | None, now: int) -> str:
    if loan is None:
        return "none"
    if loan.defaulted:
        return "defaulted"
    if loan.payment_remaining <= 0:
        return "paid"
    if loan.impaired:
        return "impaired"
    if now > loan.next_due + loan.grace_period:
        return "defaultable"
    if now > loan.next_due:
        return "late"
    return "current"


def warnings(loan: LoanView | None, b: BrokerView | None, v: VaultView | None, now: int, balance: int, installment: int) -> list[str]:
    out: list[str] = []
    st = loan_status(loan, now)
    if st in ("late", "impaired", "defaultable"):
        out.append(f"loan is {st}")
    if loan is not None and st == "current" and balance < installment:
        out.append("lockbox balance is below the next installment")
    if b is not None and cover_ratio(b) < 1.0:
        out.append("first-loss cover is below the required minimum")
    if v is not None and utilization(v) > 0.9:
        out.append("vault utilization above 90%")
    return out
