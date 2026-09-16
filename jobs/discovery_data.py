from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.models import Financial, ValuationHistory, ValuationSeries
from market.types import FinancialRow, ValuationPoint

_DISCOVERY_SHARED_FINANCIAL_FIELDS = (
    "net_income",
    "ocf",
    "capex",
    "shares",
    "npl",
)
_DISCOVERY_ONLY_FINANCIAL_FIELDS = (
    "revenue",
    "deducted_net_income",
    "total_assets",
    "net_assets",
    "cash",
    "short_loan",
    "long_loan",
    "bond_payable",
    "noncurrent_liab_1year",
    "interest_debt",
    "pretax_income",
    "income_tax",
    "normalized_effective_tax_rate",
    "invested_capital",
    "roic",
    "capital_adequacy",
    "core_tier1_capital_adequacy",
    "nonperforming_loan_balance",
    "impairment_loss",
    "government_grants",
    "asset_disposal_gain",
    "goodwill",
    "acquisition_cash_paid",
    "source_as_of",
)
_DISCOVERY_FINANCIAL_FIELDS = (
    _DISCOVERY_SHARED_FINANCIAL_FIELDS + _DISCOVERY_ONLY_FINANCIAL_FIELDS
)


class ValuationMethodMismatchError(ValueError):
    pass


def persist_discovery_financials(
    session: Session,
    rows: Iterable[FinancialRow],
) -> None:
    for row in rows:
        stored = session.get(
            Financial,
            (row.code, row.period, row.period_type),
        )
        if stored is None:
            stored = Financial(
                code=row.code,
                period=row.period,
                period_type=row.period_type,
            )
            session.add(stored)
        for field in _DISCOVERY_FINANCIAL_FIELDS:
            setattr(stored, field, getattr(row, field))


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

    for code in sorted(methods):
        ratio_method = methods[code]
        session.execute(
            insert(ValuationSeries)
            .values(code=code, ratio_method=ratio_method)
            .on_conflict_do_nothing(index_elements=[ValuationSeries.code])
        )
        claimed = session.get(ValuationSeries, code)
        if claimed is None or claimed.ratio_method != ratio_method:
            existing_method = None if claimed is None else claimed.ratio_method
            raise ValuationMethodMismatchError(
                f"{code} ratio_method mismatch: "
                f"{existing_method} != {ratio_method}"
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
