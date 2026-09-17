from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db import Base


class CanonicalDecimalText(TypeDecorator[Decimal]):
    """Store exact decimals as canonical, non-exponent TEXT."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, _dialect) -> str | None:
        if value is None:
            return None
        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid decimal: {value}") from exc
        if not amount.is_finite():
            raise ValueError(f"non-finite decimal: {value}")
        if amount == 0:
            return "0"
        text = format(amount, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    def process_result_value(self, value: object, _dialect) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


class UTCDateTime(TypeDecorator[datetime]):
    """Store naive UTC; treat legacy naive inputs as UTC and return aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, _dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    date: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)


class Cash(Base):
    __tablename__ = "cash"
    __table_args__ = (UniqueConstraint("user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="CNY")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    rule_type: Mapped[str] = mapped_column("type", String, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)


class Instrument(Base):
    __tablename__ = "instruments"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    listed_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    industry: Mapped[str | None] = mapped_column(String, nullable=True)


class Quote(Base):
    __tablename__ = "quotes"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, nullable=False, default="")
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PositionTarget(Base):
    __tablename__ = "position_targets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String, primary_key=True)
    target_weight: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class RulerVersion(Base):
    __tablename__ = "ruler_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    digest: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class JobLock(Base):
    __tablename__ = "job_lock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_type: Mapped[str | None] = mapped_column(String, nullable=True)
    held_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    owner_token: Mapped[str | None] = mapped_column(String, nullable=True)


class ScreenRun(Base):
    __tablename__ = "screen_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_text: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ScreenResult(Base):
    __tablename__ = "screen_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    screen_run_id: Mapped[int] = mapped_column(ForeignKey("screen_runs.id"), nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)


class Financial(Base):
    __tablename__ = "financials"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    period: Mapped[str] = mapped_column(String, primary_key=True)
    period_type: Mapped[str] = mapped_column(String, primary_key=True)
    roe: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    ocf: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    capex: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    ebit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    interest: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    gross_margin: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    net_margin: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    shares: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    share_change_ma: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    npl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    eps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    revenue: Mapped[Decimal | None] = mapped_column(CanonicalDecimalText(), nullable=True)
    deducted_net_income: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    total_assets: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    net_assets: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    cash: Mapped[Decimal | None] = mapped_column(CanonicalDecimalText(), nullable=True)
    short_loan: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    long_loan: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    bond_payable: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    noncurrent_liab_1year: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    interest_debt: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    pretax_income: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    income_tax: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    normalized_effective_tax_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 8), nullable=True
    )
    invested_capital: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    roic: Mapped[Decimal | None] = mapped_column(Numeric(30, 8), nullable=True)
    capital_adequacy: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 8), nullable=True
    )
    core_tier1_capital_adequacy: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 8), nullable=True
    )
    nonperforming_loan_balance: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    impairment_loss: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    government_grants: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    asset_disposal_gain: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    goodwill: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    acquisition_cash_paid: Mapped[Decimal | None] = mapped_column(
        CanonicalDecimalText(), nullable=True
    )
    source_as_of: Mapped[str | None] = mapped_column(String, nullable=True)


class FinancialFetchCompleteness(Base):
    __tablename__ = "financial_fetch_completeness"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String, primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class FinancialFieldProvenance(Base):
    __tablename__ = "financial_field_provenance"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    period: Mapped[str] = mapped_column(String, primary_key=True)
    period_type: Mapped[str] = mapped_column(String, primary_key=True)
    field: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, primary_key=True)


class ValuationSeries(Base):
    __tablename__ = "valuation_series"
    __table_args__ = (
        UniqueConstraint(
            "code",
            "ratio_method",
            name="uq_valuation_series_code_ratio_method",
        ),
    )

    code: Mapped[str] = mapped_column(String, primary_key=True)
    ratio_method: Mapped[str] = mapped_column(String, nullable=False)


class ValuationHistory(Base):
    __tablename__ = "valuation_history"
    __table_args__ = (
        ForeignKeyConstraint(
            ["code", "ratio_method"],
            ["valuation_series.code", "valuation_series.ratio_method"],
            name="fk_valuation_history_series_method",
        ),
    )

    code: Mapped[str] = mapped_column(String, primary_key=True)
    month: Mapped[str] = mapped_column(String, primary_key=True)
    trade_date: Mapped[str] = mapped_column(String, nullable=False)
    pe: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    pb: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    ratio_method: Mapped[str] = mapped_column(String, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryDataFill(Base):
    __tablename__ = "discovery_data_fills"
    __table_args__ = (Index("ix_discovery_data_fills_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_day: Mapped[str | None] = mapped_column(String, nullable=True)
    daily_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    run_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_request_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_status: Mapped[str | None] = mapped_column(String, nullable=True)


class DiscoveryDataCheckpoint(Base):
    __tablename__ = "discovery_data_checkpoints"
    __table_args__ = (
        Index(
            "ix_discovery_data_checkpoints_fill_status_retry",
            "fill_id",
            "status",
            "next_retry_at",
        ),
    )

    fill_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_data_fills.id", ondelete="CASCADE"),
        primary_key=True,
    )
    code: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    valuation_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryUniverseSecurity(Base):
    __tablename__ = "discovery_universe_securities"

    fill_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_data_fills.id", ondelete="CASCADE"),
        primary_key=True,
    )
    code: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    industry: Mapped[str] = mapped_column(String, nullable=False)
    exchange: Mapped[str | None] = mapped_column(String, nullable=True)
    listing_date: Mapped[str | None] = mapped_column(String, nullable=True)
    is_st: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_delisting: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    trading_status: Mapped[str | None] = mapped_column(String, nullable=True)
    is_suspended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_trade_date: Mapped[str | None] = mapped_column(String, nullable=True)
    industry_version: Mapped[str | None] = mapped_column(String, nullable=True)
    source_as_of: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[str | None] = mapped_column(String, nullable=True)
    market_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryFillTradingDate(Base):
    __tablename__ = "discovery_fill_trading_dates"

    fill_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_data_fills.id", ondelete="CASCADE"),
        primary_key=True,
    )
    trade_date: Mapped[str] = mapped_column(String, primary_key=True)


class ResearchJob(Base):
    __tablename__ = "research_jobs"
    __table_args__ = (
        CheckConstraint(
            "discovery_track IS NULL OR discovery_track IN "
            "('quality_discount', 'deep_value', 'garp')",
            name="ck_research_jobs_discovery_track",
        ),
        CheckConstraint(
            "discovery_run_id IS NULL OR discovery_track IS NOT NULL",
            name="ck_research_jobs_discovery_pair",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    screen_run_id: Mapped[int | None] = mapped_column(ForeignKey("screen_runs.id"), nullable=True)
    discovery_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="SET NULL"), nullable=True
    )
    discovery_track: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ResearchArtifact(Base):
    __tablename__ = "research_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    research_job_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id"), nullable=False)
    structured_json: Mapped[str] = mapped_column(Text, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    report_path: Mapped[str | None] = mapped_column(String, nullable=True)


class Watchlist(Base):
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    research_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Thesis(Base):
    __tablename__ = "theses"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    research_job_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id"), nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    structured_json: Mapped[str] = mapped_column(Text, nullable=False)
    last_event: Mapped[str] = mapped_column(String, nullable=False)


class PortfolioReview(Base):
    __tablename__ = "portfolio_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_path: Mapped[str | None] = mapped_column(String, nullable=True)
    has_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
