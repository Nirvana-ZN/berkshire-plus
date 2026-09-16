from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PositionTarget
from ledger.positions import LOCAL_USER_ID, PositionRow
from ledger.quotes import CASH_NAME, project_positions

SUM_TOLERANCE = Decimal("1e-6")


class TargetError(ValueError):
    def __init__(self, error: str, code: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.code = code
        self.detail = detail


def seed_targets_if_empty(
    session: Session,
    rows: list[PositionRow],
    cash: Decimal,
    *,
    user_id: int = LOCAL_USER_ID,
) -> bool:
    if _target_count(session, user_id=user_id) > 0:
        return False
    quoted = project_positions(session, rows, cash, user_id=user_id)
    priced_rows = [row for row in quoted if row.weight is not None]
    if not priced_rows:
        return False
    priced_total = sum(
        (row.market_value for row in priced_rows if row.market_value is not None),
        Decimal("0"),
    )
    cash_dec = max(Decimal(cash), Decimal("0"))
    denominator = priced_total + cash_dec
    if denominator <= 0:
        return False
    cash_weight = cash_dec / denominator
    if not cash_weight.is_finite() or cash_weight < 0 or cash_weight > 1:
        return False
    to_write: dict[str, Decimal] = {}
    for row in quoted:
        if row.weight is None:
            continue
        if not row.weight.is_finite() or row.weight < 0 or row.weight > 1:
            return False
        to_write[row.name] = row.weight
    to_write[CASH_NAME] = cash_weight
    total = sum(to_write.values(), Decimal("0"))
    if abs(total - 1) > SUM_TOLERANCE:
        return False
    for name, weight in to_write.items():
        session.add(
            PositionTarget(user_id=user_id, name=name, target_weight=weight)
        )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return True


def reclaim_orphan_targets(
    session: Session,
    held_names: set[str],
    *,
    user_id: int = LOCAL_USER_ID,
) -> bool:
    existing = session.scalars(
        select(PositionTarget).where(PositionTarget.user_id == user_id)
    ).all()
    if not existing:
        return False
    orphans = [
        row
        for row in existing
        if row.name != CASH_NAME and row.name not in held_names
    ]
    if not orphans:
        return False
    moved = sum((Decimal(row.target_weight) for row in orphans), Decimal("0"))
    cash_row = next((row for row in existing if row.name == CASH_NAME), None)
    if cash_row is None:
        session.add(
            PositionTarget(user_id=user_id, name=CASH_NAME, target_weight=moved)
        )
    else:
        cash_row.target_weight = Decimal(cash_row.target_weight) + moved
    for row in orphans:
        session.delete(row)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return True


def save_targets(
    session: Session,
    targets: Mapping[object, object],
    *,
    user_id: int = LOCAL_USER_ID,
) -> None:
    parsed = _parse_targets(targets)
    if CASH_NAME not in parsed:
        raise TargetError(
            error="目标合计无效",
            code="INVALID_TARGETS",
            detail="必须包含 __CASH__",
        )
    total = sum(parsed.values(), Decimal("0"))
    if abs(total - 1) > SUM_TOLERANCE:
        raise TargetError(
            error="目标合计无效",
            code="INVALID_TARGETS",
            detail=f"含现金合计必须为 1（容差 {SUM_TOLERANCE}），当前为 {total}",
        )
    existing = session.scalars(
        select(PositionTarget).where(PositionTarget.user_id == user_id)
    ).all()
    for row in existing:
        session.delete(row)
    session.flush()
    for name, weight in parsed.items():
        session.add(
            PositionTarget(user_id=user_id, name=name, target_weight=weight)
        )
    try:
        session.commit()
    except TargetError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise


def _target_count(session: Session, *, user_id: int) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(PositionTarget)
        .where(PositionTarget.user_id == user_id)
    )
    return int(count or 0)


def _parse_targets(targets: Mapping[object, object]) -> dict[str, Decimal]:
    parsed: dict[str, Decimal] = {}
    for raw_name, raw_weight in targets.items():
        name = str(raw_name).strip() if raw_name is not None else ""
        if not name:
            raise TargetError(
                error="请求无效",
                code="INVALID_TARGETS",
                detail="目标名称不能为空",
            )
        if name in parsed:
            raise TargetError(
                error="请求无效",
                code="INVALID_TARGETS",
                detail=f"重复的目标名称: {name}",
            )
        weight = _parse_weight(raw_weight, name)
        parsed[name] = weight
    return parsed


def _parse_weight(value: object, name: str) -> Decimal:
    if value is None:
        raise TargetError(
            error="请求无效",
            code="INVALID_TARGETS",
            detail=f"缺少目标权重: {name}",
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TargetError(
            error="请求无效",
            code="INVALID_TARGETS",
            detail=f"{name} 的目标权重不是有效数字",
        ) from exc
    if not number.is_finite() or number < 0 or number > 1:
        raise TargetError(
            error="目标权重无效",
            code="INVALID_TARGETS",
            detail=f"{name} 的目标权重必须落在 [0, 1]",
        )
    return number
