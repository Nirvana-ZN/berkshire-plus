from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

DiscoveryPreRequest = Callable[[], None]


class MarketError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DiscoveryCompanyError(MarketError):
    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        request_count: int = 0,
    ) -> None:
        super().__init__(f"{code} {stage}: {message}")
        self.code = code
        self.stage = stage
        self.request_count = request_count


class DiscoveryRequestBudgetExhausted(MarketError):
    pass


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
    revenue: Decimal | None = None
    deducted_net_income: Decimal | None = None
    total_assets: Decimal | None = None
    net_assets: Decimal | None = None
    cash: Decimal | None = None
    short_loan: Decimal | None = None
    long_loan: Decimal | None = None
    bond_payable: Decimal | None = None
    noncurrent_liab_1year: Decimal | None = None
    interest_debt: Decimal | None = None
    pretax_income: Decimal | None = None
    income_tax: Decimal | None = None
    normalized_effective_tax_rate: Decimal | None = None
    invested_capital: Decimal | None = None
    roic: Decimal | None = None
    capital_adequacy: Decimal | None = None
    core_tier1_capital_adequacy: Decimal | None = None
    nonperforming_loan_balance: Decimal | None = None
    impairment_loss: Decimal | None = None
    government_grants: Decimal | None = None
    asset_disposal_gain: Decimal | None = None
    goodwill: Decimal | None = None
    acquisition_cash_paid: Decimal | None = None
    source_as_of: str | None = None


@dataclass(frozen=True)
class ValuationPoint:
    code: str
    month: str
    trade_date: str
    pe: Decimal | None
    pb: Decimal | None
    price: Decimal | None = None
    source: str = "eastmoney:RPT_VALUEANALYSIS_DET"
    ratio_method: str = "PE_TTM/PB_MRQ"


@dataclass(frozen=True)
class SecurityState:
    code: str
    name: str
    exchange: str
    listing_date: str
    is_st: bool
    is_delisting: bool
    trading_status: str
    last_trade_date: str
    industry: str
    industry_version: str
    industry_as_of: str
    source: str
    market_status_code: int | None = None


@dataclass(frozen=True)
class DiscoverySourceAsOf:
    security: str
    valuation: str | None
    financial: str | None


@dataclass(frozen=True)
class DiscoveryCompanyData:
    code: str
    security_state: SecurityState
    financial_rows: tuple[FinancialRow, ...]
    valuation_points: tuple[ValuationPoint, ...]
    request_count: int
    source_as_of: DiscoverySourceAsOf
