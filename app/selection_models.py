"""Append-only selection evidence tables, sharing the application metadata."""

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DDL, ForeignKey, Index, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import UTCDateTime


class FundamentalEvidence(Base):
    __tablename__ = 'fundamental_evidence'
    __table_args__ = (
        Index('ix_fundamental_evidence_lookup', 'code', 'kind', 'period', 'available_at'),
        CheckConstraint("status IN ('verified','missing','untrusted','not_applicable')"),
    )

    evidence_id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    period: Mapped[str] = mapped_column(String, nullable=False)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    document_sha256: Mapped[str] = mapped_column(String, nullable=False)
    values_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    basis_id: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey('fundamental_evidence.evidence_id', ondelete='RESTRICT'))
    date_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    archive_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class FundamentalCoverage(Base):
    __tablename__ = 'fundamental_coverage'
    __table_args__ = (CheckConstraint('start_date <= end_date'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    digest: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class ResearchAssumption(Base):
    __tablename__ = 'selection_research_assumptions'
    __table_args__ = (Index('ix_research_assumption_owner_company', 'user_id', 'code', 'id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'), nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class StrategyExperiment(Base):
    __tablename__ = 'strategy_experiments'
    __table_args__ = (CheckConstraint("kind IN ('prospective','retrospective_observation')"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    registered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    protocol_version: Mapped[str] = mapped_column(String, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(String, nullable=False)
    registration_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class StrategyCohort(Base):
    __tablename__ = 'strategy_cohorts'
    __table_args__ = (
        UniqueConstraint('experiment_id', 'run_id', 'track', 'variant_id'),
        CheckConstraint("variant_id IN ('full_selected','no_operating_selected','all_passed')"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey('strategy_experiments.id', ondelete='RESTRICT'), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey('discovery_runs.id', ondelete='RESTRICT'), nullable=False)
    track: Mapped[str] = mapped_column(String, nullable=False)
    variant_id: Mapped[str] = mapped_column(String, nullable=False)
    # Legacy runs have no immutable publication receipt; do not invent one.
    published_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    registered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)


class StrategyMember(Base):
    __tablename__ = 'strategy_members'

    cohort_id: Mapped[int] = mapped_column(ForeignKey('strategy_cohorts.id', ondelete='RESTRICT'), primary_key=True)
    code: Mapped[str] = mapped_column(String, primary_key=True)
    initial_weight: Mapped[str] = mapped_column(Text, nullable=False)
    gate_result_id: Mapped[int] = mapped_column(ForeignKey('discovery_results.id', ondelete='RESTRICT'), nullable=False)
    selection_reason: Mapped[str] = mapped_column(Text, nullable=False)


class StrategyMarketEvent(Base):
    __tablename__ = 'strategy_market_events'
    __table_args__ = (Index('ix_strategy_market_economic_cutoff', 'economic_event_id', 'observed_at'),)

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    economic_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey('strategy_market_events.event_id', ondelete='RESTRICT'))


class StrategyLedgerEntry(Base):
    __tablename__ = 'strategy_ledger_entries'
    __table_args__ = (
        UniqueConstraint('cohort_id', 'horizon_months', 'event_id', 'entry_kind', 'calculation_version'),
        CheckConstraint('horizon_months IN (3,6,12)'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cohort_id: Mapped[int] = mapped_column(ForeignKey('strategy_cohorts.id', ondelete='RESTRICT'), nullable=False)
    horizon_months: Mapped[int] = mapped_column(nullable=False)
    event_id: Mapped[str] = mapped_column(ForeignKey('strategy_market_events.event_id', ondelete='RESTRICT'), nullable=False)
    entry_kind: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    shares_after: Mapped[str] = mapped_column(Text, nullable=False)
    cash_after: Mapped[str] = mapped_column(Text, nullable=False)
    receivable_after: Mapped[str] = mapped_column(Text, nullable=False)
    cost: Mapped[str] = mapped_column(Text, nullable=False)
    calculation_version: Mapped[str] = mapped_column(Text, nullable=False)


class StrategyObservation(Base):
    __tablename__ = 'strategy_observations'
    __table_args__ = (
        UniqueConstraint('cohort_id', 'horizon_months', 'as_of', 'calculation_version', 'event_digest'),
        CheckConstraint('horizon_months IN (3,6,12)'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cohort_id: Mapped[int] = mapped_column(ForeignKey('strategy_cohorts.id', ondelete='RESTRICT'), nullable=False)
    horizon_months: Mapped[int] = mapped_column(nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    event_digest: Mapped[str] = mapped_column(Text, nullable=False)
    calculation_version: Mapped[str] = mapped_column(Text, nullable=False)


def strategy_guard_statements():
    """Shared by metadata creation and idempotent initialization of existing DBs."""
    for model in (StrategyExperiment, StrategyCohort, StrategyMember):
        table = model.__table__
        changed = ' OR '.join(f'NEW.{column.name} IS NOT OLD.{column.name}'
                              for column in table.columns if column.name != 'state')
        yield (f'CREATE TRIGGER IF NOT EXISTS trg_{table.name}_immutable '
               f'BEFORE UPDATE ON {table.name} WHEN {changed} '
               "BEGIN SELECT RAISE(ABORT, 'strategy registration is immutable'); END")
        yield (f'CREATE TRIGGER IF NOT EXISTS trg_{table.name}_delete '
               f'BEFORE DELETE ON {table.name} '
               "BEGIN SELECT RAISE(ABORT, 'strategy registration is append-only'); END")
        if model is StrategyExperiment:
            duplicate = 'id = NEW.id OR registration_hash = NEW.registration_hash'
        elif model is StrategyCohort:
            duplicate = ('id = NEW.id OR (experiment_id = NEW.experiment_id AND run_id = NEW.run_id '
                         'AND track = NEW.track AND variant_id = NEW.variant_id)')
        else:
            duplicate = 'cohort_id = NEW.cohort_id AND code = NEW.code'
        yield (f'CREATE TRIGGER IF NOT EXISTS trg_{table.name}_replace '
               f'BEFORE INSERT ON {table.name} WHEN EXISTS '
               f'(SELECT 1 FROM {table.name} WHERE {duplicate}) '
               "BEGIN SELECT RAISE(ABORT, 'strategy registration is immutable'); END")
    yield ("CREATE TRIGGER IF NOT EXISTS trg_strategy_members_manifest "
           "BEFORE INSERT ON strategy_members WHEN NOT EXISTS ("
           "SELECT 1 FROM strategy_cohorts c, json_each(c.manifest_json, '$.members') m "
           "WHERE c.id = NEW.cohort_id AND json_extract(m.value, '$.code') = NEW.code "
           "AND json_extract(m.value, '$.weight') = NEW.initial_weight "
           "AND json_extract(m.value, '$.gate_result_id') = NEW.gate_result_id "
           "AND json_extract(m.value, '$.selection_reason') = NEW.selection_reason) "
           "BEGIN SELECT RAISE(ABORT, 'strategy membership is immutable'); END")
    for model, duplicate in (
        (StrategyMarketEvent, 'event_id = NEW.event_id'),
        (StrategyLedgerEntry, 'id = NEW.id OR (cohort_id = NEW.cohort_id AND '
         'horizon_months = NEW.horizon_months AND event_id = NEW.event_id AND '
         'entry_kind = NEW.entry_kind AND calculation_version = NEW.calculation_version)'),
        (StrategyObservation, 'id = NEW.id OR (cohort_id = NEW.cohort_id AND '
         'horizon_months = NEW.horizon_months AND as_of = NEW.as_of AND '
         'calculation_version = NEW.calculation_version AND event_digest = NEW.event_digest)'),
    ):
        table = model.__tablename__
        for action in ('UPDATE', 'DELETE'):
            yield (f'CREATE TRIGGER IF NOT EXISTS trg_{table}_{action.lower()} '
                   f'BEFORE {action} ON {table} BEGIN '
                   "SELECT RAISE(ABORT, 'strategy calculations are append-only'); END")
        yield (f'CREATE TRIGGER IF NOT EXISTS trg_{table}_replace BEFORE INSERT ON {table} '
               f'WHEN EXISTS (SELECT 1 FROM {table} WHERE {duplicate}) BEGIN '
               "SELECT RAISE(ABORT, 'strategy calculations are immutable'); END")


for _statement in strategy_guard_statements():
    event.listen(Base.metadata, 'after_create', DDL(_statement).execute_if(dialect='sqlite'))


# Triggers also protect direct SQL and INSERT OR REPLACE (which otherwise may
# bypass delete triggers under SQLite's default recursive_triggers setting).
for _table, _key in ((FundamentalEvidence.__table__, 'evidence_id'),
                     (FundamentalCoverage.__table__, 'digest'),
                     (ResearchAssumption.__table__, 'digest')):
    for _action in ('UPDATE', 'DELETE'):
        event.listen(_table, 'after_create', DDL(
            f'CREATE TRIGGER IF NOT EXISTS trg_{_table.name}_{_action.lower()} '
            f'BEFORE {_action} ON {_table.name} BEGIN '
            "SELECT RAISE(ABORT, 'selection evidence ledger is append-only and immutable'); END"
        ).execute_if(dialect='sqlite'))
    _duplicate = f'{_key} = NEW.{_key}'
    if _key == 'digest':
        _duplicate += ' OR id = NEW.id'
    event.listen(_table, 'after_create', DDL(
        f'CREATE TRIGGER IF NOT EXISTS trg_{_table.name}_replace '
        f'BEFORE INSERT ON {_table.name} '
        f'WHEN EXISTS (SELECT 1 FROM {_table.name} WHERE {_duplicate}) '
        "BEGIN SELECT RAISE(ABORT, 'selection evidence ledger is immutable'); END"
    ).execute_if(dialect='sqlite'))
