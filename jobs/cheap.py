from __future__ import annotations

import json
import logging
from collections import Counter
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Financial, Instrument, Setting
from jobs.funnel import persist_financials
from market.cheap import (
    METHOD,
    SCOPE,
    SCOPE_LABEL,
    SHOW_LIMIT,
    SORTS,
    CheapRow,
    as_constituent,
    eligible_cheap,
    load_cheap_universe,
    row_json,
)
from market.types import FinancialRow, MarketError
from screen.funnel import ScreenRow, screen_keep

logger = logging.getLogger(__name__)

FILL_KEY = "cheap_filling"
BOARD_KEY = "cheap_board"
FILL_CHUNK = 40
IN_CHUNK = 400
BOARD_ROE10_FLOOR = Decimal("12")
BOARD_OCF_NI_FLOOR = Decimal("1")
INDUSTRY_CAP = 2
_BOARD_CACHE: dict[tuple[object, ...], dict[str, object]] = {}


def _json_number(value: Decimal | None) -> int | float | None:
    if value is None or not value.is_finite():
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _rows_from_db(session: Session, codes: list[str]) -> list[FinancialRow]:
    if not codes:
        return []
    found: list[Financial] = []
    for start in range(0, len(codes), IN_CHUNK):
        batch = codes[start : start + IN_CHUNK]
        # 去劣只用年报；季报/中报占库里 2/3，读进来纯浪费
        found.extend(
            session.scalars(
                select(Financial).where(
                    Financial.code.in_(batch),
                    Financial.period_type == "annual",
                )
            ).all()
        )
    return [
        FinancialRow(
            code=row.code,
            period=row.period,
            period_type=row.period_type,
            roe=row.roe,
            net_income=row.net_income,
            ocf=row.ocf,
            capex=row.capex,
            ebit=row.ebit,
            interest=row.interest,
            gross_margin=row.gross_margin,
            net_margin=row.net_margin,
            shares=row.shares,
            share_change_ma=row.share_change_ma,
            bps=row.bps,
            npl=row.npl,
            eps=row.eps,
        )
        for row in found
    ]


def _codes_with_financials(session: Session, codes: list[str]) -> set[str]:
    have: set[str] = set()
    for start in range(0, len(codes), IN_CHUNK):
        batch = codes[start : start + IN_CHUNK]
        have.update(
            session.scalars(
                select(Financial.code)
                .where(
                    Financial.code.in_(batch),
                    Financial.period_type == "annual",
                )
                .distinct()
            ).all()
        )
    return have


def _listed_years(session: Session, codes: list[str]) -> dict[str, int]:
    if not codes:
        return {}
    years: dict[str, int] = {}
    for start in range(0, len(codes), IN_CHUNK):
        batch = codes[start : start + IN_CHUNK]
        for code, year in session.execute(
            select(Instrument.code, Instrument.listed_year).where(
                Instrument.code.in_(batch),
                Instrument.listed_year.is_not(None),
            )
        ):
            years[code] = int(year)
    return years


def _board_setting_key(sort: str) -> str:
    return f"{BOARD_KEY}:{sort}"


def _board_fingerprint(sort: str, fetched_at: float, ready: int) -> str:
    return f"{METHOD}:{sort}:{int(fetched_at)}:{ready}"


def _read_persisted_board(
    session: Session, *, sort: str, fingerprint: str
) -> dict[str, object] | None:
    row = session.get(Setting, _board_setting_key(sort))
    if row is None or not row.value:
        return None
    try:
        body = json.loads(row.value)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict) or body.get("fingerprint") != fingerprint:
        return None
    payload = body.get("payload")
    return dict(payload) if isinstance(payload, dict) else None


def _write_persisted_board(
    session: Session, *, sort: str, fingerprint: str, payload: dict[str, object]
) -> None:
    session.merge(
        Setting(
            key=_board_setting_key(sort),
            value=json.dumps(
                {"fingerprint": fingerprint, "payload": payload},
                ensure_ascii=False,
            ),
        )
    )


def _ensure_financials(session: Session, market: object | None, codes: list[str]) -> list[FinancialRow]:
    cached = _rows_from_db(session, codes)
    have = {row.code for row in cached}
    need = [code for code in codes if code not in have]
    fetch = getattr(market, "fetch_financials", None)
    if need and callable(fetch):
        fetched = fetch(need)
        persist_financials(session, fetched)
        session.flush()
        cached = _rows_from_db(session, codes)
    return cached


def _eligible_codes(universe: list[CheapRow]) -> list[str]:
    seen: dict[str, CheapRow] = {}
    for sort in ("pe", "pb"):
        for row in eligible_cheap(universe, sort=sort):
            seen.setdefault(row.code, row)
    return [
        row.code
        for row in sorted(
            seen.values(),
            key=lambda row: (row.mcap is None, -(row.mcap or Decimal(0))),
        )
    ]


def _board_quality(row: ScreenRow) -> bool:
    """榜上无人看数据缺口，所以这里缺指标就不留，比去劣严。"""
    return (
        row.roe10 is not None
        and row.roe10 >= BOARD_ROE10_FLOOR
        and row.ocf_ni is not None
        and row.ocf_ni >= BOARD_OCF_NI_FLOOR
    )


def _value_score(cheap: CheapRow, screen: ScreenRow, sort: str) -> Decimal | None:
    """质量性价比：10年ROE 摊到每一份估值上。"""
    metric = cheap.pb if sort == "pb" else cheap.pe
    if metric is None or metric <= 0 or screen.roe10 is None:
        return None
    return screen.roe10 / metric


def _scored(
    eligible: list[CheapRow], rows: list[ScreenRow], *, sort: str
) -> list[tuple[Decimal, CheapRow, ScreenRow]]:
    by_code = {row.code: row for row in rows}
    out: list[tuple[Decimal, CheapRow, ScreenRow]] = []
    for cheap in eligible:
        screen = by_code.get(cheap.code)
        if screen is None:
            continue
        score = _value_score(cheap, screen, sort)
        if score is not None:
            out.append((score, cheap, screen))
    out.sort(key=lambda item: (-item[0], item[1].name))
    return out


def _shortlist(
    scored: list[tuple[Decimal, CheapRow, ScreenRow]],
) -> list[tuple[Decimal, CheapRow, ScreenRow]]:
    """L3 每行业最多 2 家，L4 取前 SHOW_LIMIT 家。"""
    picked: list[tuple[Decimal, CheapRow, ScreenRow]] = []
    per_industry: Counter[str] = Counter()
    for item in scored:
        industry = item[1].industry or "未知"
        if per_industry[industry] >= INDUSTRY_CAP:
            continue
        per_industry[industry] += 1
        picked.append(item)
        if len(picked) >= SHOW_LIMIT:
            break
    return picked


def _layer_rows(
    scored: list[tuple[Decimal, CheapRow, ScreenRow]],
) -> list[dict[str, object]]:
    rows = []
    for score, cheap, screen in scored:
        body = row_json(cheap)
        body["roe_mean"] = _json_number(screen.roe_mean)
        body["roe10"] = _json_number(screen.roe10)
        body["ocf_ni"] = _json_number(
            screen.ocf_ni.quantize(Decimal("0.01")) if screen.ocf_ni is not None else None
        )
        body["score"] = _json_number(score.quantize(Decimal("0.01")))
        rows.append(body)
    return rows


def cheap_fill_running(session: Session) -> bool:
    row = session.get(Setting, FILL_KEY)
    return bool(row and row.value == "1")


def begin_cheap_fill(session: Session) -> bool:
    if cheap_fill_running(session):
        return False
    session.merge(Setting(key=FILL_KEY, value="1"))
    return True


def clear_cheap_fill(session: Session) -> None:
    row = session.get(Setting, FILL_KEY)
    if row is not None:
        session.delete(row)


def load_cheap_board(
    session: Session,
    market: object | None,
    *,
    sort: str = "pe",
    fill: bool = False,
) -> dict[str, object]:
    if sort not in SORTS:
        raise ValueError(sort)
    universe, fetched_at = load_cheap_universe(session, market)
    eligible = eligible_cheap(universe, sort=sort)
    codes = [row.code for row in eligible]
    have = _codes_with_financials(session, codes)
    if fill:
        missing = [code for code in codes if code not in have]
        if missing:
            _ensure_financials(session, market, missing)
            have = _codes_with_financials(session, codes)
    pending = sum(1 for code in codes if code not in have)
    ready = [row for row in eligible if row.code in have]
    cache_key = (str(session.get_bind().url), sort, fetched_at, len(ready))
    cached = _BOARD_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    fingerprint = _board_fingerprint(sort, fetched_at, len(ready))
    persisted = _read_persisted_board(session, sort=sort, fingerprint=fingerprint)
    if persisted is not None:
        _BOARD_CACHE.clear()
        _BOARD_CACHE[cache_key] = persisted
        return dict(persisted)
    snaps = [as_constituent(row) for row in ready]
    financials = _rows_from_db(session, [row.code for row in ready])
    years = _listed_years(session, [row.code for row in ready])
    screened = screen_keep(snaps, financials, years)
    kept = [row for row in screened if _board_quality(row)]
    screened_scored = _scored(eligible, screened, sort=sort)
    kept_scored = _scored(eligible, kept, sort=sort)
    shortlist = _shortlist(kept_scored)
    rows = _layer_rows(shortlist)
    body = {
        "sort": sort,
        "scope": SCOPE,
        "scope_label": SCOPE_LABEL,
        "method": METHOD,
        "rows": rows,
        "layers": [
            {
                "key": "screened",
                "label": "去劣通过",
                "note": "过了 ROE / 现金流 / 毛利率等去劣门槛",
                "count": len(screened_scored),
                "rows": _layer_rows(screened_scored),
            },
            {
                "key": "passed",
                "label": "过质量门槛",
                "note": f"10年ROE ≥ {BOARD_ROE10_FLOOR}% 且 净现比 ≥ {BOARD_OCF_NI_FLOOR}",
                "count": len(kept_scored),
                "rows": _layer_rows(kept_scored),
            },
            {
                "key": "shortlist",
                "label": "短名单",
                "note": f"每行业最多 {INDUSTRY_CAP} 家，按性价比取前 {SHOW_LIMIT}",
                "count": len(rows),
                "rows": rows,
            },
        ],
        "universe": len(eligible),
        "pool": len(ready),
        "ready": len(ready),
        "pending": pending,
        "screened": len(screened),
        "passed": len(kept),
        "shown": len(rows),
        "fetched_at": fetched_at,
    }
    # ponytail: 内存 + settings 双缓存；截面/财报指纹一变就重算
    _BOARD_CACHE.clear()
    _BOARD_CACHE[cache_key] = body
    _write_persisted_board(session, sort=sort, fingerprint=fingerprint, payload=body)
    return dict(body)


def execute_cheap_fill(session: Session, market: object | None) -> None:
    try:
        universe, _fetched_at = load_cheap_universe(session, market)
        codes = _eligible_codes(universe)
        have = _codes_with_financials(session, codes)
        need = [code for code in codes if code not in have]
        # ponytail: 东财财务一票一请求；按批落库，全市场批量接口再换
        for start in range(0, len(need), FILL_CHUNK):
            batch = need[start : start + FILL_CHUNK]
            try:
                _ensure_financials(session, market, batch)
                session.commit()
            except MarketError:
                session.rollback()
                logger.exception("低估值财报批次失败")
    except Exception:
        logger.exception("低估值财报回填失败")
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        clear_cheap_fill(session)
        session.commit()
