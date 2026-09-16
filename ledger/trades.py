from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Cash, Trade
from ledger.positions import LOCAL_USER_ID, PositionRow, load_positions

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DIRECTIONS = {"buy": "buy", "sell": "sell", "买": "buy", "卖": "sell"}


class TradeError(ValueError):
    def __init__(self, error: str, code: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RecordedTrade:
    positions: list[PositionRow]
    cash: Decimal
    warning: str | None = None


def record_trade(
    session: Session,
    *,
    date: object | None,
    direction: object | None,
    name: object | None,
    shares: object | None,
    price: object | None,
    note: object | None = None,
    user_id: int = LOCAL_USER_ID,
) -> RecordedTrade:
    trade_date = _parse_date(date)
    trade_direction = _parse_direction(direction)
    trade_name = _parse_name(name)
    trade_shares = _parse_shares(shares)
    trade_price = _parse_price(price)
    trade_note = None if note is None or str(note) == "" else str(note)
    try:
        _book_trade(
            session,
            trade_date=trade_date,
            direction=trade_direction,
            name=trade_name,
            shares=trade_shares,
            price=trade_price,
            note=trade_note,
            user_id=user_id,
        )
        session.commit()
    except TradeError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    return _snapshot(session, user_id=user_id, warn_if_buy=trade_direction == "buy")


def set_position(
    session: Session,
    *,
    name: object | None,
    shares: object | None,
    unit_cost: object | None,
    user_id: int = LOCAL_USER_ID,
) -> RecordedTrade:
    trade_name = _parse_name(name)
    trade_shares = _parse_shares(shares)
    trade_price = _parse_unit_cost(unit_cost)
    held = next(
        (row for row in load_positions(session, user_id=user_id)[0] if row.name == trade_name),
        None,
    )
    if held is None:
        raise TradeError(
            error="没有该持仓",
            code="NO_POSITION",
            detail=f"没有持仓: {trade_name}",
        )
    try:
        _book_trade(
            session,
            trade_date=datetime.now(timezone.utc).date().isoformat(),
            direction="set",
            name=trade_name,
            shares=trade_shares,
            price=trade_price,
            note="账面调整",
            user_id=user_id,
        )
        session.commit()
    except TradeError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    return _snapshot(session, user_id=user_id)


def set_cash(
    session: Session,
    *,
    balance: object | None,
    user_id: int = LOCAL_USER_ID,
) -> RecordedTrade:
    amount = _parse_decimal(balance, "balance")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        session.execute(select(Cash).where(Cash.user_id == user_id).with_for_update())
        row = session.scalar(select(Cash).where(Cash.user_id == user_id))
        if row is None:
            session.add(
                Cash(user_id=user_id, balance=amount, currency="CNY", updated_at=now)
            )
        else:
            row.balance = amount
            row.updated_at = now
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _snapshot(session, user_id=user_id)


def _book_trade(
    session: Session,
    *,
    trade_date: str,
    direction: str,
    name: str,
    shares: Decimal,
    price: Decimal,
    note: str | None,
    user_id: int,
) -> None:
    session.execute(
        select(Cash).where(Cash.user_id == user_id).with_for_update()
    )
    positions, _cash = load_positions(session, user_id=user_id)
    held = next((row.shares for row in positions if row.name == name), Decimal("0"))
    if direction != "set":
        last_set = session.scalars(
            select(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.name == name,
                Trade.direction == "set",
            )
            .order_by(Trade.date.desc(), Trade.id.desc())
        ).first()
        if last_set is not None and trade_date < last_set.date:
            raise TradeError(
                error="成交日期过早",
                code="BEFORE_SET",
                detail=f"成交日期早于最近账面调整（{last_set.date}）",
            )
    if direction == "sell" and shares > held:
        raise TradeError(
            error="卖出超过持仓",
            code="SELL_EXCEEDS",
            detail=(
                f"卖出股数超过当前持仓: {name} "
                f"持有 {held} 卖出 {shares}"
            ),
        )
    session.add(
        Trade(
            user_id=user_id,
            date=trade_date,
            direction=direction,
            name=name,
            shares=shares,
            price=price,
            note=note,
        )
    )
    session.flush()
    if direction != "set":
        _apply_cash(
            session,
            user_id=user_id,
            direction=direction,
            amount=shares * price,
        )


def _snapshot(
    session: Session,
    *,
    user_id: int,
    warn_if_buy: bool = False,
) -> RecordedTrade:
    rows, cash = load_positions(session, user_id=user_id)
    warning = "现金不足" if warn_if_buy and cash < 0 else None
    return RecordedTrade(positions=rows, cash=cash, warning=warning)


def _missing(field: str) -> TradeError:
    return TradeError(
        error="请求无效",
        code="INVALID_TRADE",
        detail=f"缺少字段: {field}",
    )


def _invalid(detail: str) -> TradeError:
    return TradeError(error="请求无效", code="INVALID_TRADE", detail=detail)


def _parse_date(value: object | None) -> str:
    if value is None:
        raise _missing("date")
    text = str(value).strip()
    if not ISO_DATE.match(text):
        raise _invalid(f"成交日期必须是 YYYY-MM-DD: {text}")
    try:
        Date.fromisoformat(text)
    except ValueError as exc:
        raise _invalid(f"成交日期无效: {text}") from exc
    return text


def _parse_direction(value: object | None) -> str:
    if value is None:
        raise _missing("direction")
    text = str(value).strip()
    mapped = DIRECTIONS.get(text)
    if mapped is None:
        raise _invalid("方向必须是 buy 或 sell")
    return mapped


def _parse_name(value: object | None) -> str:
    if value is None:
        raise _missing("name")
    text = str(value).strip()
    if not text:
        raise _missing("name")
    return text


def _parse_decimal(value: object | None, field: str) -> Decimal:
    if value is None:
        raise _missing(field)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise _invalid(f"{field}不是有效数字") from exc
    if not number.is_finite():
        raise _invalid(f"{field}不是有效数字")
    return number


def _parse_shares(value: object | None) -> Decimal:
    shares = _parse_decimal(value, "shares")
    if shares <= 0:
        raise _invalid("股数必须大于 0")
    return shares


def _parse_price(value: object | None) -> Decimal:
    price = _parse_decimal(value, "price")
    if price <= 0:
        raise _invalid("价格必须大于 0")
    return price


def _parse_unit_cost(value: object | None) -> Decimal:
    price = _parse_decimal(value, "unit_cost")
    if price <= 0:
        raise _invalid("成本单价必须大于 0")
    return price


def _apply_cash(
    session: Session,
    *,
    user_id: int,
    direction: str,
    amount: Decimal,
) -> None:
    row = session.scalar(select(Cash).where(Cash.user_id == user_id))
    current = Decimal(row.balance) if row is not None else Decimal("0")
    balance = current - amount if direction == "buy" else current + amount
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row is None:
        session.add(
            Cash(
                user_id=user_id,
                balance=balance,
                currency="CNY",
                updated_at=now,
            )
        )
        return
    row.balance = balance
    row.updated_at = now
