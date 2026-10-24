"""Units and conversions. XRP is integer drops everywhere. Rates are XRPL 1/10 basis points."""
from __future__ import annotations

from decimal import Decimal

RIPPLE_EPOCH = 946_684_800
DROPS = 1_000_000
RATE_UNITS = 100_000  # XRPL rate fields: 100_000 means 100%, so 1% = 1_000 and 1 bp = 10
YEAR_SECONDS = 365 * 24 * 3600


def unix_to_ripple(t: int | float) -> int:
    return int(t) - RIPPLE_EPOCH


def ripple_to_unix(t: int) -> int:
    return int(t) + RIPPLE_EPOCH


def xrp_to_drops(x) -> int:
    return int((Decimal(str(x)) * DROPS).to_integral_value())


def drops_to_xrp(d: int) -> Decimal:
    return Decimal(int(d)) / DROPS


def fmt_xrp(d: int, places: int = 4) -> str:
    return f"{drops_to_xrp(d):,.{places}f}"


def pct_to_units(pct: float) -> int:
    """5.0 (percent per year) -> 5000 (1/10 bp)."""
    return int(round(pct * RATE_UNITS / 100))


def units_to_pct(units: int) -> float:
    return units * 100 / RATE_UNITS


def period_rate(annual_units: int, interval_seconds: int) -> float:
    """Simple per-period rate from an annual rate in 1/10 bp."""
    return (annual_units / RATE_UNITS) * (interval_seconds / YEAR_SECONDS)
