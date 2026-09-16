from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ValuationHistory
from market.types import ValuationPoint


class ValuationMethodMismatchError(ValueError):
    pass


def persist_valuation_history(
    session: Session,
    rows: Iterable[ValuationPoint],
    *,
    fetched_at: datetime,
) -> None:
    points: dict[tuple[str, str], ValuationPoint] = {}
    methods: dict[str, str] = {}
    for row in rows:
        prior_method = methods.setdefault(row.code, row.ratio_method)
        if prior_method != row.ratio_method:
            raise ValuationMethodMismatchError(
                f"{row.code} ratio_method mismatch: "
                f"{prior_method} != {row.ratio_method}"
            )
        key = (row.code, row.month)
        points[key] = row

    with session.no_autoflush:
        existing_rows = session.scalars(
            select(ValuationHistory).where(ValuationHistory.code.in_(methods))
        ).all()
        existing_rows.extend(
            row
            for row in session.new
            if isinstance(row, ValuationHistory) and row.code in methods
        )
        for existing in existing_rows:
            ratio_method = methods[existing.code]
            if existing.ratio_method != ratio_method:
                raise ValuationMethodMismatchError(
                    f"{existing.code} ratio_method mismatch: "
                    f"{existing.ratio_method} != {ratio_method}"
                )

    for key in sorted(points):
        row = points[key]
        session.merge(
            ValuationHistory(
                code=row.code,
                month=row.month,
                trade_date=row.trade_date,
                pe=row.pe,
                pb=row.pb,
                price=row.price,
                source=row.source,
                ratio_method=row.ratio_method,
                fetched_at=fetched_at,
            )
        )
