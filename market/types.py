from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class MarketError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Constituent:
    code: str
    name: str
    mcap: Decimal | None
    pe: Decimal | None
    industry: str
    peg: Decimal | None = None
    business_model: str | None = None


@dataclass(frozen=True)
class FinancialRow:
    code: str
    period: str
    period_type: str
    roe: Decimal | None = None
    net_income: Decimal | None = None
    ocf: Decimal | None = None
    capex: Decimal | None = None
    ebit: Decimal | None = None
    interest: Decimal | None = None
    gross_margin: Decimal | None = None
    net_margin: Decimal | None = None
    shares: Decimal | None = None
    share_change_ma: bool | None = None
    bps: Decimal | None = None
    npl: Decimal | None = None
    eps: Decimal | None = None
