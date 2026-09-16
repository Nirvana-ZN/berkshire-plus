from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


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
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    ocf: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    capex: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    ebit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    interest: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    gross_margin: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    net_margin: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    shares: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    share_change_ma: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    npl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    eps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)


class ResearchJob(Base):
    __tablename__ = "research_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    screen_run_id: Mapped[int | None] = mapped_column(ForeignKey("screen_runs.id"), nullable=True)
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
