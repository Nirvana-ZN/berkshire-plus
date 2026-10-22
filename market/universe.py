from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

SUSPENSION_MAX_TRADING_DAYS = 5
INDUSTRY_MAPPING_VERSION = "eastmoney:push2delay:clist:f100:v1"
DELISTING_NAME_METHOD = "exchange-rules:sh-prefix-tuishi:sz-suffix-tui:v1"


@dataclass(frozen=True)
class UniverseRow:
    code: str
    name: str
    price: Decimal | None
    pe: Decimal | None
    pb: Decimal | None
    mcap: Decimal | None
    industry: str
    exchange: str | None = None
    listing_date: str | None = None
    is_st: bool | None = None
    is_delisting: bool | None = None
    trading_status: str | None = None
    last_trade_date: str | None = None
    industry_version: str | None = None
    industry_as_of: str | None = None
    security_source: str | None = None
    market_status_code: int | None = None


def is_st_name(name: str) -> bool:
    return (name or "").strip().upper().startswith(("ST", "*ST", "S*ST", "SST"))


def delisting_name_state(code: str, name: str) -> bool | None:
    """Exchange-mandated abbreviation marks; independent of East Money f292."""
    code = (code or "").strip().upper()
    name = (name or "").strip()
    if (
        not name
        or len(code) != 9
        or code[6] != "."
        or not code[:6].isdigit()
        or not code.endswith((".SH", ".SZ"))
    ):
        return None
    # Shanghai prefixes 退市; Shenzhen suffixes 退 while the code stays fixed.
    return name.startswith("退市") if code.endswith(".SH") else name.endswith("退")


def security_state_eligible(
    row: UniverseRow,
    frozen_trading_dates: Sequence[str],
    *,
    suspension_max_trading_days: int = SUSPENSION_MAX_TRADING_DAYS,
) -> bool:
    if (
        row.exchange not in {"SH", "SZ"}
        or row.listing_date is None
        or row.is_st is not False
        or row.is_delisting is not False
        or not row.industry
        or row.industry == "-"
        or row.industry_version is None
        or row.industry_as_of is None
        or row.security_source is None
        or row.last_trade_date is None
        or not frozen_trading_dates
    ):
        return False
    if row.trading_status == "trading":
        return row.last_trade_date == frozen_trading_dates[-1]
    if row.trading_status == "suspended":
        return row.last_trade_date in frozen_trading_dates[
            -suspension_max_trading_days:
        ]
    return False
