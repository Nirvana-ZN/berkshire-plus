from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping, Sequence
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument, PositionTarget, Quote
from ledger.positions import LOCAL_USER_ID, PositionRow
from ledger.rules import LIGHT_NONE, lights_for

A_SHARE_CODE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"
CASH_NAME = "__CASH__"
REBALANCE_THRESHOLD = Decimal("0.03")


class QuoteError(ValueError):
    def __init__(self, error: str, code: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class QuotedPosition:
    name: str
    shares: Decimal
    cost: Decimal
    price: Decimal | None
    market_value: Decimal | None
    pnl: Decimal | None
    weight: Decimal | None
    stale: bool
    source: str | None
    code: str | None
    target: Decimal | None
    no_target: bool
    deviation: Decimal | None
    rule_light: str


@dataclass(frozen=True)
class QuotedCash:
    weight: Decimal | None
    target: Decimal | None
    no_target: bool
    deviation: Decimal | None


@dataclass(frozen=True)
class BookSnapshot:
    positions: tuple[QuotedPosition, ...]
    cash_balance: Decimal
    cash: QuotedCash


@dataclass(frozen=True)
class RefreshError:
    name: str
    error: str
    code: str
    detail: str


def is_a_share_code(code: str) -> bool:
    return bool(A_SHARE_CODE.match(code.strip().upper()))


def project_positions(
    session: Session,
    rows: Sequence[PositionRow],
    cash: Decimal,
    *,
    user_id: int = LOCAL_USER_ID,
) -> list[QuotedPosition]:
    quotes = {
        row.name: row
        for row in session.scalars(select(Quote).where(Quote.user_id == user_id)).all()
    }
    instruments = {
        row.name: row
        for row in session.scalars(
            select(Instrument).where(Instrument.user_id == user_id)
        ).all()
    }
    targets = _target_map(session, user_id=user_id)
    priced: list[tuple[PositionRow, Quote | None, Decimal | None]] = []
    market_values: list[Decimal | None] = []
    for row in rows:
        quote = quotes.get(row.name)
        price = None
        if quote is not None:
            try:
                candidate = Decimal(quote.price)
            except (InvalidOperation, ValueError, TypeError):
                candidate = None
            if candidate is not None and candidate.is_finite():
                price = candidate
        market_value = row.shares * price if price is not None else None
        priced.append((row, quote, price))
        market_values.append(market_value)

    weight_values = [mv for mv in market_values if mv is not None]
    priced_total = sum(weight_values, Decimal("0"))
    denominator = priced_total + Decimal(cash)
    lights = lights_for(
        session,
        {row.name: price for row, _quote, price in priced},
        user_id=user_id,
    )

    result: list[QuotedPosition] = []
    for (row, quote, price), market_value in zip(priced, market_values):
        weight = None
        pnl = None
        if market_value is not None:
            pnl = market_value - row.cost
        if market_value is not None and denominator > 0:
            weight = market_value / denominator
        instrument = instruments.get(row.name)
        code = None
        if quote is not None and quote.code:
            code = quote.code
        elif instrument is not None:
            code = instrument.code
        target = targets.get(row.name)
        no_target = target is None
        deviation = None
        if target is not None and weight is not None:
            deviation = weight - target
        result.append(
            QuotedPosition(
                name=row.name,
                shares=row.shares,
                cost=row.cost,
                price=price,
                market_value=market_value,
                pnl=pnl,
                weight=weight,
                stale=bool(quote.stale) if quote is not None else False,
                source=quote.source if quote is not None else None,
                code=code,
                target=target,
                no_target=no_target,
                deviation=deviation,
                rule_light=lights.get(row.name, LIGHT_NONE),
            )
        )
    return result


def project_cash(
    session: Session,
    quoted: Sequence[QuotedPosition],
    cash: Decimal,
    *,
    user_id: int = LOCAL_USER_ID,
) -> QuotedCash:
    priced_total = sum(
        (row.market_value for row in quoted if row.market_value is not None),
        Decimal("0"),
    )
    denominator = priced_total + Decimal(cash)
    weight = None
    if denominator > 0:
        weight = Decimal(cash) / denominator
    target = _target_map(session, user_id=user_id).get(CASH_NAME)
    no_target = target is None
    deviation = None
    if target is not None and weight is not None:
        deviation = weight - target
    return QuotedCash(
        weight=weight,
        target=target,
        no_target=no_target,
        deviation=deviation,
    )


def project_book(
    session: Session,
    rows: Sequence[PositionRow],
    cash: Decimal,
    *,
    user_id: int = LOCAL_USER_ID,
) -> BookSnapshot:
    quoted = tuple(project_positions(session, rows, cash, user_id=user_id))
    return BookSnapshot(
        positions=quoted,
        cash_balance=Decimal(cash),
        cash=project_cash(session, quoted, cash, user_id=user_id),
    )


def rebalance_alerts(quoted: Sequence[QuotedPosition], cash: QuotedCash) -> list[str]:
    if any(row.market_value is None for row in quoted):
        return []
    names: list[str] = []
    for row in quoted:
        if row.no_target or row.weight is None or row.target is None:
            continue
        if abs(row.weight - row.target) > REBALANCE_THRESHOLD:
            names.append(row.name)
    if (
        not cash.no_target
        and cash.weight is not None
        and cash.target is not None
        and abs(cash.weight - cash.target) > REBALANCE_THRESHOLD
    ):
        names.append(CASH_NAME)
    return names


def _target_map(session: Session, *, user_id: int) -> dict[str, Decimal]:
    rows = session.scalars(
        select(PositionTarget).where(PositionTarget.user_id == user_id)
    ).all()
    mapped: dict[str, Decimal] = {}
    for row in rows:
        try:
            weight = Decimal(row.target_weight)
        except (InvalidOperation, ValueError, TypeError):
            continue
        if weight.is_finite():
            mapped[row.name] = weight
    return mapped


def save_instrument(
    session: Session,
    *,
    name: object | None,
    code: object | None,
    user_id: int = LOCAL_USER_ID,
) -> Instrument:
    trade_name = _parse_name(name)
    a_share = _parse_code(code)
    existing_name = session.scalar(
        select(Instrument).where(Instrument.name == trade_name)
    )
    existing_code = session.get(Instrument, a_share)
    if existing_code is not None and existing_code.name != trade_name:
        raise QuoteError(
            error="代码已被占用",
            code="CODE_TAKEN",
            detail=f"代码 {a_share} 已映射到 {existing_code.name}",
        )
    if existing_name is not None and existing_name.code != a_share:
        session.delete(existing_name)
        session.flush()
        session.add(Instrument(code=a_share, name=trade_name, user_id=user_id))
    elif existing_name is None:
        session.add(Instrument(code=a_share, name=trade_name, user_id=user_id))
    quote = session.get(Quote, (user_id, trade_name))
    if quote is not None:
        quote.code = a_share
    try:
        session.commit()
    except QuoteError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    saved = session.scalar(select(Instrument).where(Instrument.name == trade_name))
    assert saved is not None
    return saved


def set_manual_price(
    session: Session,
    *,
    name: object | None,
    price: object | None,
    user_id: int = LOCAL_USER_ID,
) -> Quote:
    trade_name = _parse_name(name)
    amount = _parse_price(price)
    instrument = session.scalar(
        select(Instrument).where(
            Instrument.user_id == user_id, Instrument.name == trade_name
        )
    )
    code = instrument.code if instrument is not None else ""
    _upsert_quote(
        session,
        user_id=user_id,
        name=trade_name,
        code=code,
        price=amount,
        source=SOURCE_MANUAL,
        stale=False,
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    saved = session.get(Quote, (user_id, trade_name))
    assert saved is not None
    return saved


def apply_refresh(
    session: Session,
    *,
    fetched: Mapping[str, Decimal],
    targets: Sequence[tuple[str, str | None]],
    single: bool,
    user_id: int = LOCAL_USER_ID,
) -> list[RefreshError]:
    errors: list[RefreshError] = []
    for name, code in targets:
        if code is None or not is_a_share_code(code):
            existing = session.get(Quote, (user_id, name))
            if existing is not None and existing.code and is_a_share_code(existing.code):
                code = existing.code
        if code is None or not is_a_share_code(code):
            if single:
                raise QuoteError(
                    error="无法解析代码",
                    code="NO_CODE",
                    detail=f"持仓 {name} 没有有效的 .SH/.SZ/.BJ 代码",
                )
            errors.append(
                RefreshError(
                    name=name,
                    error="无法解析代码",
                    code="NO_CODE",
                    detail=f"持仓 {name} 没有有效的 .SH/.SZ/.BJ 代码",
                )
            )
            continue
        raw_price = fetched.get(code)
        price = raw_price if isinstance(raw_price, Decimal) else None
        if price is not None and price.is_finite() and price > 0:
            _upsert_quote(
                session,
                user_id=user_id,
                name=name,
                code=code,
                price=price,
                source=SOURCE_AUTO,
                stale=False,
            )
            continue
        existing = session.get(Quote, (user_id, name))
        if existing is not None:
            existing.stale = True
        else:
            errors.append(
                RefreshError(
                    name=name,
                    error="行情失败",
                    code="QUOTE_FAILED",
                    detail=f"未能取得 {name}（{code}）的现价",
                )
            )
    try:
        session.commit()
    except QuoteError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    return errors


def instrument_code_by_name(
    session: Session, *, user_id: int = LOCAL_USER_ID
) -> dict[str, str]:
    rows = session.scalars(
        select(Instrument).where(Instrument.user_id == user_id)
    ).all()
    return {row.name: row.code for row in rows}


def lookup_saved_instrument(
    session: Session, query: str, *, user_id: int = LOCAL_USER_ID
) -> tuple[str, str] | None:
    text = (query or "").strip()
    if not text:
        return None
    by_name = session.scalars(
        select(Instrument).where(Instrument.user_id == user_id, Instrument.name == text)
    ).first()
    if by_name is not None:
        return by_name.name, by_name.code
    upper = text.upper()
    if is_a_share_code(upper):
        by_code = session.get(Instrument, upper)
        if by_code is not None and by_code.user_id == user_id:
            return by_code.name, by_code.code
    if text.isdigit() and len(text) == 6:
        rows = session.scalars(
            select(Instrument).where(
                Instrument.user_id == user_id, Instrument.code.like(f"{text}.%")
            )
        ).all()
        if len(rows) == 1:
            return rows[0].name, rows[0].code
    return None


def _upsert_quote(
    session: Session,
    *,
    user_id: int,
    name: str,
    code: str,
    price: Decimal,
    source: str,
    stale: bool,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = session.get(Quote, (user_id, name))
    if row is None:
        session.add(
            Quote(
                user_id=user_id,
                name=name,
                code=code,
                price=price,
                source=source,
                updated_at=now,
                stale=stale,
            )
        )
        return
    row.code = code
    row.price = price
    row.source = source
    row.updated_at = now
    row.stale = stale


def _parse_name(value: object | None) -> str:
    if value is None:
        raise QuoteError(error="请求无效", code="INVALID_QUOTE", detail="缺少字段: name")
    text = str(value).strip()
    if not text:
        raise QuoteError(error="请求无效", code="INVALID_QUOTE", detail="缺少字段: name")
    return text


def _parse_code(value: object | None) -> str:
    if value is None:
        raise QuoteError(error="请求无效", code="INVALID_CODE", detail="缺少字段: code")
    text = str(value).strip().upper()
    if not text:
        raise QuoteError(error="请求无效", code="INVALID_CODE", detail="缺少字段: code")
    if not is_a_share_code(text):
        raise QuoteError(
            error="代码无效",
            code="INVALID_CODE",
            detail=f"代码须为 .SH / .SZ / .BJ: {value}",
        )
    return text


def _parse_price(value: object | None) -> Decimal:
    if value is None:
        raise QuoteError(error="请求无效", code="INVALID_PRICE", detail="缺少字段: price")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise QuoteError(
            error="请求无效",
            code="INVALID_PRICE",
            detail="price不是有效数字",
        ) from exc
    if not number.is_finite() or number <= 0:
        raise QuoteError(
            error="价格无效",
            code="INVALID_PRICE",
            detail="价格必须大于 0",
        )
    return number
