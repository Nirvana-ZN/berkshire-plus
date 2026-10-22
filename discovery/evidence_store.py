"""Immutable evidence repository and point-in-time availability rules."""

from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.selection_models import FundamentalCoverage, FundamentalEvidence
from discovery.constants import CHINA_MARKET_TIMEZONE
from discovery.field_dictionary import FIELD_DICTIONARY, SELECTION_FIELD_DICTIONARY
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


def _validate_numeric_values(record: EvidenceRecord) -> None:
    """Validate registered amounts without rewriting identity-bearing strings.

    Generic evidence text and reserved provenance metadata are not financial
    fields. The repository enforces the numeric contract independently of any
    market-layer parser, including for trusted internal callers.
    """
    for field, value in record.values.items():
        action_number = (record.kind in {'capital_event', 'capital_plan', 'buyback_trade'}
                         and field in {'shares_before', 'shares_after', 'factor', 'sequence'})
        if value is None or (field not in FIELD_DICTIONARY and field not in SELECTION_FIELD_DICTIONARY and not action_number):
            continue
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f'{field} must contain a finite exact decimal') from exc
        if not amount.is_finite():
            raise ValueError(f'{field} must contain a finite exact decimal')


def append_evidence(session: Session, record: EvidenceRecord) -> str:
    if evidence_identity(record) != record.evidence_id:
        raise ValueError('evidence identity conflicts with canonical content')
    _validate_numeric_values(record)
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
        if record.kind == 'capital_event':
            economic_id = record.values.get('economic_event_id')
            if not economic_id or economic_id != json.loads(parent.values_json).get('economic_event_id'):
                raise ValueError('capital supersession must retain the same economic_event_id')
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
    return select_evidence_as_of(visible, code=code, cutoff=cutoff, historical=historical)


def load_coverage_references(session: Session, code: str, cutoff: datetime,
                             windows: Sequence[CoverageWindow]) -> tuple[EvidenceRecord, ...]:
    """Load original coverage sources and their visible revision proof chains.

    This does not select current facts or infer original trust from successors.
    Capture freezes these records as proof extensions, never financial inputs.
    """
    _aware(cutoff)
    frontier = {key for window in windows if window.code == code for key in window.evidence_ids}
    records = {}
    while frontier:
        query = select(FundamentalEvidence).where(
            FundamentalEvidence.code == code, FundamentalEvidence.available_at <= cutoff,
            (FundamentalEvidence.evidence_id.in_(frontier)
             | FundamentalEvidence.supersedes_id.in_(frontier)))
        loaded = (_record(row) for row in session.scalars(query))
        found = {row.evidence_id: row for row in loaded}
        found = {key: row for key, row in found.items() if available_at(row) <= cutoff}
        frontier = set(found) - set(records)
        records.update(found)
    return tuple(records[key] for key in sorted(records))


def select_evidence_as_of(records: Sequence[EvidenceRecord], *, code: str,
                          cutoff: datetime, historical: bool = False) -> tuple[EvidenceRecord, ...]:
    """Shared pure cutoff-before-version boundary for repository and snapshots."""
    _aware(cutoff)
    visible = list({row.evidence_id: row for row in records
                    if row.code == code and available_at(row, historical=historical) <= cutoff}.values())
    # Unreviewed protected-source corrections remain visible as raw evidence,
    # but cannot hide verified facts that downstream validation still needs.
    def authoritative(row):
        if row.kind == 'company_profile':
            return row.status == 'verified'
        if row.kind == 'capital_event':
            return row.status == 'verified' and row.values.get('stage') in {'implemented', 'withdrawn'}
        if row.kind in {'dividend', 'buyback', 'equity_financing', 'buyback_trade'}:
            return row.status == 'verified' and row.values.get('stage') in {
                'proposed', 'approved', 'implemented', 'paid', 'executed',
                'cancelled', 'traded', 'withdrawn', 'revoked'}
        if row.kind == 'shareholder_return_coverage':
            return row.status == 'verified' and row.values.get('stage') in {None, 'withdrawn', 'revoked'}
        return True

    protected = {'company_profile', 'capital_event', 'dividend', 'buyback', 'equity_financing',
                 'buyback_trade', 'shareholder_return_coverage'}
    superseded = {row.supersedes_id for row in visible if row.kind not in protected}
    by_id = {row.evidence_id: row for row in visible}
    for row in visible:
        if row.kind in protected and authoritative(row):
            parent_id = row.supersedes_id
            seen = set()
            while parent_id in by_id and parent_id not in seen:
                parent = by_id[parent_id]
                if row.kind == 'company_profile':
                    if (parent.code, parent.kind, parent.period) != (row.code, row.kind, row.period):
                        raise ValueError('profile supersession must retain company, kind and period')
                elif row.kind != 'capital_event':
                    if (parent.code, parent.kind, parent.period) != (row.code, row.kind, row.period):
                        raise ValueError('shareholder supersession must retain company, kind and period')
                    if row.kind != 'shareholder_return_coverage' and parent.values.get('plan_id') != row.values.get('plan_id'):
                        raise ValueError('shareholder supersession must retain plan_id')
                    # A later proposal cannot withdraw an actual payment/trade.
                    # Nor may one actual stage erase another stage's ledger.
                    stage, prior = row.values.get('stage'), parent.values.get('stage')
                    plans = {'proposed', 'approved', 'implemented'}
                    if (stage not in {'withdrawn', 'revoked'} and stage != prior
                            and not {stage, prior} <= plans):
                        break
                seen.add(parent_id)
                superseded.add(parent_id)
                parent_id = by_id[parent_id].supersedes_id
    return tuple(sorted((row for row in visible if row.evidence_id not in superseded),
                        key=lambda row: (row.kind, row.period, available_at(row, historical=historical), row.evidence_id)))


def append_coverage(session: Session, window: CoverageWindow, *,
                    captured_at: datetime | None = None) -> str:
    """Append coverage using the trusted service capture clock, never archival time.

    An existing digest retains its original creation time even when a retry
    supplies a different capture timestamp. This keyword is an internal clock
    injection boundary; normalized evidence imports do not expose it.
    """
    created_at = datetime.now(timezone.utc) if captured_at is None else captured_at
    _aware(created_at)
    created_at = created_at.astimezone(timezone.utc)
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
        created_at=created_at, digest=digest,
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
