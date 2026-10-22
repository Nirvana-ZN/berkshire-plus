"""Append-only selection evidence tables, sharing the application metadata."""

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DDL, ForeignKey, Index, String, Text, event
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
