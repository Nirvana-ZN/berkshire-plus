from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Cash, Trade

LOCAL_USER_ID = 1


@dataclass(frozen=True)
class PositionRow:
    name: str
    shares: Decimal
    cost: Decimal


def load_positions(
    session: Session, *, user_id: int = LOCAL_USER_ID
) -> tuple[list[PositionRow], Decimal]:
    trades = session.scalars(
        select(Trade)
        .where(Trade.user_id == user_id)
        .order_by(Trade.date, Trade.id)
    ).all()

    books: dict[str, tuple[Decimal, Decimal]] = {}
    for trade in trades:
        shares, cost = books.get(trade.name, (Decimal("0"), Decimal("0")))
        qty = Decimal(trade.shares)
        price = Decimal(trade.price)
        if trade.direction == "buy":
            shares += qty
            cost += qty * price
        elif trade.direction == "sell":
            if shares == 0:
                continue
            avg = cost / shares
            shares -= qty
            cost -= avg * qty
            if shares == 0:
                cost = Decimal("0")
        if shares == 0:
            books.pop(trade.name, None)
        else:
            books[trade.name] = (shares, cost)

    cash_row = session.scalar(select(Cash).where(Cash.user_id == user_id))
    cash = Decimal(cash_row.balance) if cash_row is not None else Decimal("0")
    positions = [
        PositionRow(name=name, shares=shares, cost=cost)
        for name, (shares, cost) in books.items()
        if shares != 0
    ]
    return positions, cash
