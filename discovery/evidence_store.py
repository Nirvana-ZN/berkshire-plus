"""Immutable evidence repository and point-in-time availability rules."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.selection_models import FundamentalCoverage, FundamentalEvidence
from discovery.constants import CHINA_MARKET_TIMEZONE
from discovery.fundamental_types import CoverageWindow, EvidenceRecord, evidence_identity


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('cutoff must be a timezone-aware datetime')


def available_at(record: EvidenceRecord, *, historical: bool = False) -> datetime:
    published = record.published_at
    if record.date_only:
        local = published.astimezone(CHINA_MARKET_TIMEZONE)
        published = (local.replace(hour=0, minute=0, second=0, microsecond=0)
                     + timedelta(days=1)).astimezone(timezone.utc)
    if historical and record.archive_verified:
        return published.astimezone(timezone.utc)
    return max(published, record.observed_at).astimezone(timezone.utc)


def _record(row: FundamentalEvidence) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=row.evidence_id, code=row.code, kind=row.kind, period=row.period,
        published_at=row.published_at, observed_at=row.observed_at,
        source_updated_at=row.source_updated_at, source_url=row.source_url,
        document_sha256=row.document_sha256, values=json.loads(row.values_json),
        status=row.status, basis_id=row.basis_id, supersedes_id=row.supersedes_id,
        date_only=row.date_only, archive_verified=row.archive_verified,
    )


def append_evidence(session: Session, record: EvidenceRecord) -> str:
    if evidence_identity(record) != record.evidence_id:
        raise ValueError('evidence identity conflicts with canonical content')
    existing = session.get(FundamentalEvidence, record.evidence_id)
    if existing is not None:
        if evidence_identity(_record(existing)) != record.evidence_id:
            raise ValueError('stored evidence identity conflict')
        return record.evidence_id
    if record.supersedes_id is not None:
        parent = session.get(FundamentalEvidence, record.supersedes_id)
        if parent is None or (parent.code, parent.kind, parent.period) != (record.code, record.kind, record.period):
            raise ValueError('supersedes_id must reference the same company, kind and period')
        if record.observed_at < parent.observed_at:
            raise ValueError('superseding evidence cannot precede first observation')
    session.add(FundamentalEvidence(
        evidence_id=record.evidence_id, code=record.code, kind=record.kind, period=record.period,
        published_at=record.published_at, observed_at=record.observed_at,
        source_updated_at=record.source_updated_at, source_url=record.source_url,
        document_sha256=record.document_sha256, values_json=_json(dict(record.values)),
        status=record.status, basis_id=record.basis_id, supersedes_id=record.supersedes_id,
        date_only=record.date_only, archive_verified=record.archive_verified,
        available_at=available_at(record),
    ))
    session.flush()
    return record.evidence_id


def load_evidence_as_of(session: Session, code: str, cutoff: datetime, *, historical: bool = False) -> tuple[EvidenceRecord, ...]:
    _aware(cutoff)
    query = select(FundamentalEvidence).where(FundamentalEvidence.code == code)
    if not historical:
        query = query.where(FundamentalEvidence.available_at <= cutoff)
    visible = []
    for item in session.scalars(query):
        record = _record(item)
        if available_at(record, historical=historical) <= cutoff:
            visible.append(record)
    superseded = {row.supersedes_id for row in visible}
    return tuple(sorted((row for row in visible if row.evidence_id not in superseded),
                        key=lambda row: (row.kind, row.period, available_at(row, historical=historical), row.evidence_id)))


def append_coverage(session: Session, window: CoverageWindow) -> str:
    payload = dict(code=window.code, kind=window.kind, start=window.start.isoformat(),
                   end=window.end.isoformat(), complete=window.complete, evidence_ids=list(window.evidence_ids))
    digest = sha256(_json(payload).encode('utf-8')).hexdigest()
    if session.scalar(select(FundamentalCoverage).where(FundamentalCoverage.digest == digest)) is not None:
        return digest
    for evidence_id in window.evidence_ids:
        row = session.get(FundamentalEvidence, evidence_id)
        if row is None or row.code != window.code:
            raise ValueError('coverage references missing or different-company evidence')
    session.add(FundamentalCoverage(
        code=window.code, kind=window.kind, start_date=window.start,
        end_date=window.end, complete=window.complete,
        evidence_ids_json=_json(list(window.evidence_ids)),
        created_at=datetime.now(timezone.utc), digest=digest,
    ))
    session.flush()
    return digest


def load_coverage(
    session: Session,
    code: str,
    *,
    cutoff: datetime | None = None,
) -> tuple[CoverageWindow, ...]:
    """Read inventory, or coverage knowable at a production decision cutoff.

    Coverage has its own first-observation boundary: old report dates cannot
    backdate a newly recorded completeness assertion. Archive overrides belong
    to a separately validated historical path, not this production reader.
    """
    query = select(FundamentalCoverage).where(FundamentalCoverage.code == code)
    if cutoff is not None:
        _aware(cutoff)
        query = query.where(FundamentalCoverage.created_at <= cutoff)
    windows = [
        (row, tuple(json.loads(row.evidence_ids_json)))
        for row in session.scalars(query.order_by(FundamentalCoverage.id))
    ]
    if cutoff is not None:
        referenced_ids = {evidence_id for _, ids in windows for evidence_id in ids}
        available_ids = set(session.scalars(
            select(FundamentalEvidence.evidence_id).where(
                FundamentalEvidence.evidence_id.in_(referenced_ids),
                FundamentalEvidence.code == code,
                FundamentalEvidence.available_at <= cutoff,
            )
        )) if referenced_ids else set()
        windows = [(row, ids) for row, ids in windows if set(ids) <= available_ids]
    return tuple(
        CoverageWindow(row.code, row.kind, row.start_date, row.end_date, row.complete, ids)
        for row, ids in windows
    )
