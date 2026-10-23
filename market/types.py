from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable
from discovery.fundamental_types import EvidenceRecord, CoverageWindow

DiscoveryPreRequest = Callable[[], None]


@dataclass(frozen=True)
class CurrentMarketCap:
    """One dated provider observation; absent/invalid observations carry no value."""

    code: str
    value: Decimal | None
    market_date: date | None
    observed_at: datetime
    source: str
    source_url: str
    response_sha256: str
    status: str
    reason: str | None

    def __post_init__(self) -> None:
        if (type(self.code) is not str or len(self.code) != 9
                or not self.code[:6].isdigit() or self.code[6:] not in {'.SH', '.SZ'}):
            raise ValueError('current market cap requires a canonical A-share code')
        if (not isinstance(self.observed_at, datetime)
                or self.observed_at.utcoffset() is None):
            raise ValueError('observed_at must be an aware datetime')
        object.__setattr__(self, 'observed_at', self.observed_at.astimezone(timezone.utc))
        if self.market_date is not None and type(self.market_date) is not date:
            raise ValueError('market_date must be an actual date or None')
        if self.status not in {'verified', 'missing', 'untrusted'}:
            raise ValueError('invalid current market cap status')
        if self.status == 'verified':
            from discovery.constants import china_market_date
            if (not isinstance(self.value, Decimal) or not self.value.is_finite()
                    or self.value <= 0 or self.market_date is None
                    or self.market_date > china_market_date(self.observed_at)):
                raise ValueError('verified current market cap requires positive value and observed market day')
            if self.reason is not None:
                raise ValueError('verified current market cap cannot have a failure reason')
        elif self.value is not None or not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError('unavailable current market cap requires reason and no value')
        if (not isinstance(self.source, str) or not self.source.strip()
                or not isinstance(self.source_url, str) or not self.source_url.startswith('https://')):
            raise ValueError('current market cap requires source provenance')
        if (not isinstance(self.response_sha256, str) or len(self.response_sha256) != 64
                or any(char not in '0123456789abcdef' for char in self.response_sha256)):
            raise ValueError('current market cap requires response SHA256')


class MarketError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TransientMarketError(MarketError):
    pass


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


class DiscoveryPermanentCompanyError(DiscoveryCompanyError):
    pass


class DiscoveryRequestStopped(MarketError):
    pass


class DiscoveryRequestBudgetExhausted(DiscoveryRequestStopped):
    pass


class DiscoveryRequestRuntimeExhausted(DiscoveryRequestStopped):
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
    interest_coverage: Decimal | None = None
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
    other_income: Decimal | None = None
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


def validate_security_state(
    state: SecurityState,
    code: str,
    *,
    cutoff: date | None = None,
) -> None:
    """Validate a complete immutable A-share security snapshot."""

    if type(state) is not SecurityState:
        raise ValueError("security_state must be an immutable SecurityState")
    expected = (code or "").strip().upper()
    if state.code != expected:
        raise ValueError("security_state code mismatch")
    if (
        len(expected) != 9
        or expected[6] != "."
        or not expected[:6].isdigit()
        or expected[7:] not in {"SH", "SZ"}
    ):
        raise ValueError("security_state code is not a canonical A-share code")
    if state.exchange != expected[7:]:
        raise ValueError("security_state exchange does not match code")
    for field in ("name", "industry", "industry_version", "source"):
        value = getattr(state, field)
        if type(value) is not str or not value or value != value.strip():
            raise ValueError(f"security_state {field} is invalid")
    if type(state.is_st) is not bool or type(state.is_delisting) is not bool:
        raise ValueError("security_state flags must be booleans")
    if state.trading_status not in {"trading", "suspended"}:
        raise ValueError("security_state trading_status is invalid")
    if state.market_status_code is not None and (
        type(state.market_status_code) is not int
    ):
        raise ValueError("security_state market_status_code is invalid")

    parsed: dict[str, date] = {}
    for field in ("listing_date", "last_trade_date", "industry_as_of"):
        value = getattr(state, field)
        if type(value) is not str:
            raise ValueError(f"security_state {field} is invalid")
        try:
            parsed[field] = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"security_state {field} is invalid") from exc
        if parsed[field].isoformat() != value:
            raise ValueError(f"security_state {field} is invalid")
    if parsed["listing_date"] > parsed["last_trade_date"]:
        raise ValueError("security_state listing_date follows last_trade_date")
    if parsed["last_trade_date"] > parsed["industry_as_of"]:
        raise ValueError("security_state last_trade_date exceeds source as-of")
    if cutoff is not None and parsed["industry_as_of"] > cutoff:
        raise ValueError("security_state source as-of exceeds fill cutoff")


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
    evidence_records: tuple[EvidenceRecord, ...] = ()
    coverage: tuple[CoverageWindow, ...] = ()
    current_market_cap: CurrentMarketCap | None = None
    operating_policy: Mapping[str, object] = field(default_factory=dict)
