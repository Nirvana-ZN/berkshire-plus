from __future__ import annotations

import json
import time
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
METHOD = "v1"


@dataclass(frozen=True)
class CheapRow:
    code: str
    name: str
    price: Decimal | None
    pe: Decimal | None
    pb: Decimal | None
    mcap: Decimal | None
    industry: str


def _is_st(name: str) -> bool:
    return "ST" in (name or "").upper()


def _metric(row: CheapRow, sort: str) -> Decimal | None:
    return row.pb if sort == "pb" else row.pe


def eligible_cheap(rows: list[CheapRow], *, sort: str = "pe") -> list[CheapRow]:
    kept: list[CheapRow] = []
    for row in rows:
        if _is_st(row.name):
            continue
        metric = _metric(row, sort)
        if metric is None or metric <= 0:
            continue
        kept.append(row)
    kept.sort(key=lambda row: _metric(row, sort) or Decimal(0))
    return kept


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
    return [item for item in parsed if item is not None], float(fetched)


def _store(session: Session, rows: list[CheapRow], fetched_at: float) -> None:
    session.merge(
        Setting(
            key=CACHE_KEY,
            value=json.dumps(
                {
                    "method": METHOD,
                    "fetched_at": fetched_at,
                    "rows": [row_json(row) for row in rows],
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
