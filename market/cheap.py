from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Setting
from market.types import Constituent

CACHE_KEY = "cheap_universe"
TTL_SEC = 3600
HARD_TTL_SEC = 6 * 3600
SHOW_LIMIT = 30
SORTS = frozenset({"pe", "pb"})
SCOPE = "hs_a"
SCOPE_LABEL = "沪深A股（不含北交所）"
METHOD = "v4"
INDUSTRY_MAPPING_VERSION = "eastmoney:push2delay:clist:f100:v1"
DELISTING_NAME_METHOD = "exchange-rules:sh-prefix-tuishi:sz-suffix-tui:v1"
_SECURITY_FIELDS = frozenset(
    {
        "exchange",
        "listing_date",
        "is_st",
        "is_delisting",
        "trading_status",
        "last_trade_date",
        "industry_version",
        "industry_as_of",
        "security_source",
        "market_status_code",
    }
)


@dataclass(frozen=True)
class CheapRow:
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


def _metric(row: CheapRow, sort: str) -> Decimal | None:
    return row.pb if sort == "pb" else row.pe


def eligible_cheap(rows: list[CheapRow], *, sort: str = "pe") -> list[CheapRow]:
    kept: list[CheapRow] = []
    for row in rows:
        if is_st_name(row.name):
            continue
        metric = _metric(row, sort)
        if metric is None or metric <= 0:
            continue
        kept.append(row)
    kept.sort(key=lambda row: _metric(row, sort) or Decimal(0))
    return kept


def security_state_eligible(
    row: CheapRow, frozen_trading_dates: Sequence[str]
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
        return row.last_trade_date in frozen_trading_dates[-5:]
    return False


def _json_number(value: Decimal | None) -> int | float | None:
    if value is None or not value.is_finite():
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def row_json(row: CheapRow) -> dict[str, object]:
    return {
        "code": row.code,
        "name": row.name,
        "price": _json_number(row.price),
        "pe": _json_number(row.pe),
        "pb": _json_number(row.pb),
        "mcap": _json_number(row.mcap),
        "industry": row.industry,
    }


def _cache_json(row: CheapRow) -> dict[str, object]:
    return {
        **row_json(row),
        "exchange": row.exchange,
        "listing_date": row.listing_date,
        "is_st": row.is_st,
        "is_delisting": row.is_delisting,
        "trading_status": row.trading_status,
        "last_trade_date": row.last_trade_date,
        "industry_version": row.industry_version,
        "industry_as_of": row.industry_as_of,
        "security_source": row.security_source,
        "market_status_code": row.market_status_code,
    }


def _as_dec(value: object) -> Decimal | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    return number if number.is_finite() else None


def _from_json(raw: object) -> CheapRow | None:
    if not isinstance(raw, dict):
        return None
    if not _SECURITY_FIELDS.issubset(raw):
        return None
    if any(
        raw.get(key) is not None and not isinstance(raw.get(key), bool)
        for key in ("is_st", "is_delisting")
    ):
        return None
    name = str(raw.get("name") or "").strip()
    code = str(raw.get("code") or "").strip()
    if not name or not code:
        return None
    return CheapRow(
        code=code,
        name=name,
        price=_as_dec(raw.get("price")),
        pe=_as_dec(raw.get("pe")),
        pb=_as_dec(raw.get("pb")),
        mcap=_as_dec(raw.get("mcap")),
        industry=str(raw.get("industry") or "").strip(),
        exchange=str(raw["exchange"]) if raw["exchange"] is not None else None,
        listing_date=(
            str(raw["listing_date"]) if raw["listing_date"] is not None else None
        ),
        is_st=raw["is_st"],
        is_delisting=raw["is_delisting"],
        trading_status=(
            str(raw["trading_status"]) if raw["trading_status"] is not None else None
        ),
        last_trade_date=(
            str(raw["last_trade_date"]) if raw["last_trade_date"] is not None else None
        ),
        industry_version=(
            str(raw["industry_version"]) if raw["industry_version"] is not None else None
        ),
        industry_as_of=(
            str(raw["industry_as_of"]) if raw["industry_as_of"] is not None else None
        ),
        security_source=(
            str(raw["security_source"]) if raw["security_source"] is not None else None
        ),
        market_status_code=(
            int(raw["market_status_code"])
            if isinstance(raw["market_status_code"], int)
            else None
        ),
    )


def _cached(
    session: Session, now: float, *, max_age: float = TTL_SEC
) -> tuple[list[CheapRow], float] | None:
    row = session.get(Setting, CACHE_KEY)
    if row is None or not row.value:
        return None
    try:
        body = json.loads(row.value)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict) or body.get("method") != METHOD:
        return None
    fetched = body.get("fetched_at")
    if not isinstance(fetched, (int, float)) or now - fetched > max_age:
        return None
    rows = body.get("rows")
    if not isinstance(rows, list):
        return None
    parsed = [_from_json(item) for item in rows]
    if any(item is None for item in parsed):
        return None
    return [item for item in parsed if item is not None], float(fetched)


def _store(session: Session, rows: list[CheapRow], fetched_at: float) -> None:
    session.merge(
        Setting(
            key=CACHE_KEY,
            value=json.dumps(
                {
                    "method": METHOD,
                    "fetched_at": fetched_at,
                    "rows": [_cache_json(row) for row in rows],
                },
                ensure_ascii=False,
            ),
        )
    )


def as_constituent(row: CheapRow) -> Constituent:
    return Constituent(
        code=row.code,
        name=row.name,
        mcap=row.mcap,
        pe=row.pe,
        industry=row.industry,
    )


def load_cheap_universe(session: Session, market: object | None) -> tuple[list[CheapRow], float]:
    now = time.time()
    fresh = _cached(session, now, max_age=TTL_SEC)
    if fresh is not None:
        return fresh
    # ponytail: 过期先吐旧截面，别卡在东财 80 页；硬过期才同步重拉
    stale = _cached(session, now, max_age=HARD_TTL_SEC)
    if stale is not None:
        return stale
    fetch = getattr(market, "fetch_cheap_universe", None)
    universe = list(fetch()) if callable(fetch) else []
    _store(session, universe, now)
    return universe, now
