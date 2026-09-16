from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Rule
from ledger.positions import LOCAL_USER_ID

ADD_CEILING_TYPE = "加仓上限价"
LIGHT_HIT = "hit"
LIGHT_CLEAR = "clear"
LIGHT_NONE = "none"
LIGHT_UNKNOWN = "unknown"


def _finite_price(value: object) -> Decimal | None:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite():
        return None
    return amount


class RuleError(ValueError):
    def __init__(self, error: str, code: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RuleRow:
    id: int
    name: str
    rule_type: str
    price: Decimal
    note: str | None


def list_rules(session: Session, *, user_id: int = LOCAL_USER_ID) -> list[RuleRow]:
    rows = session.scalars(
        select(Rule).where(Rule.user_id == user_id).order_by(Rule.id)
    ).all()
    return [
        RuleRow(
            id=row.id,
            name=row.name,
            rule_type=row.rule_type,
            price=amount,
            note=row.note,
        )
        for row in rows
        if (amount := _finite_price(row.price)) is not None
    ]


def save_rule(
    session: Session,
    *,
    name: object | None,
    rule_type: object | None,
    price: object | None,
    note: object | None = None,
    user_id: int = LOCAL_USER_ID,
) -> RuleRow:
    parsed_name = _parse_name(name)
    parsed_type = _parse_type(rule_type)
    parsed_price = _parse_price(price)
    parsed_note = None if note is None else str(note).strip() or None
    row = Rule(
        user_id=user_id,
        name=parsed_name,
        rule_type=parsed_type,
        price=parsed_price,
        note=parsed_note,
    )
    session.add(row)
    try:
        session.commit()
    except RuleError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    session.refresh(row)
    return RuleRow(
        id=int(row.id),
        name=parsed_name,
        rule_type=parsed_type,
        price=parsed_price,
        note=parsed_note,
    )


def delete_rule(
    session: Session,
    rule_id: object,
    *,
    user_id: int = LOCAL_USER_ID,
) -> None:
    parsed_id = _parse_id(rule_id)
    row = session.get(Rule, parsed_id)
    if row is None or row.user_id != user_id:
        raise RuleError(
            error="未找到",
            code="NOT_FOUND",
            detail=f"没有规则 {parsed_id}",
        )
    session.delete(row)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def lights_for(
    session: Session,
    named_prices: Mapping[str, Decimal | None],
    *,
    user_id: int = LOCAL_USER_ID,
) -> dict[str, str]:
    ceilings = _ceiling_map(session, user_id=user_id)
    lights: dict[str, str] = {}
    for name, price in named_prices.items():
        caps = ceilings.get(name)
        if not caps:
            lights[name] = LIGHT_NONE
            continue
        if price is None or not price.is_finite() or price <= 0:
            lights[name] = LIGHT_UNKNOWN
            continue
        if any(price >= cap for cap in caps):
            lights[name] = LIGHT_HIT
        else:
            lights[name] = LIGHT_CLEAR
    return lights


def _ceiling_map(session: Session, *, user_id: int) -> dict[str, list[Decimal]]:
    rows = session.scalars(
        select(Rule).where(
            Rule.user_id == user_id, Rule.rule_type == ADD_CEILING_TYPE
        )
    ).all()
    mapped: dict[str, list[Decimal]] = {}
    for row in rows:
        try:
            amount = Decimal(row.price)
        except (InvalidOperation, ValueError, TypeError):
            continue
        if amount.is_finite() and amount > 0:
            mapped.setdefault(row.name, []).append(amount)
    return mapped


def _parse_id(value: object) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuleError(
            error="请求无效",
            code="INVALID_RULE",
            detail="规则编号无效",
        ) from exc
    if parsed <= 0:
        raise RuleError(
            error="请求无效",
            code="INVALID_RULE",
            detail="规则编号无效",
        )
    return parsed


def _parse_name(value: object | None) -> str:
    if value is None:
        raise RuleError(error="请求无效", code="INVALID_RULE", detail="缺少字段: name")
    text = str(value).strip()
    if not text:
        raise RuleError(error="请求无效", code="INVALID_RULE", detail="缺少字段: name")
    return text


def _parse_type(value: object | None) -> str:
    if value is None:
        raise RuleError(error="请求无效", code="INVALID_TYPE", detail="缺少字段: type")
    text = str(value).strip()
    if not text:
        raise RuleError(error="请求无效", code="INVALID_TYPE", detail="缺少字段: type")
    if text != ADD_CEILING_TYPE:
        raise RuleError(
            error="类型无效",
            code="INVALID_TYPE",
            detail=f"未知类型: {text}",
        )
    return text


def _parse_price(value: object | None) -> Decimal:
    if value is None:
        raise RuleError(error="请求无效", code="INVALID_PRICE", detail="缺少字段: price")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RuleError(
            error="请求无效",
            code="INVALID_PRICE",
            detail="price不是有效数字",
        ) from exc
    if not number.is_finite() or number <= 0:
        raise RuleError(
            error="价格无效",
            code="INVALID_PRICE",
            detail="价格必须大于 0",
        )
    return number
