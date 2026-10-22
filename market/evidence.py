"""Conservative source boundary for normalized and Eastmoney evidence.

A human-review receipt records an explicit assertion; it does not certify an
archive. Standalone imports cannot grant archival trust. Reserved values keys
begin with '_' and carry provenance text, never numeric calculation inputs.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import urlparse

from discovery.constants import CHINA_MARKET_TIMEZONE
from discovery.field_dictionary import SELECTION_FIELD_DICTIONARY
from discovery.fundamental_types import EvidenceRecord, evidence_identity

SELECTION_EXTENSION_FIELDS = (
    'accounts_receivable', 'contract_assets', 'inventory',
    'total_equity_including_minority', 'interest_expense',
    'restricted_cash', 'related_party_funds',
)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be nonempty text')
    return value


def _time(value: object, name: str, *, provider: bool = False) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError(f'invalid {name}') from exc
    if not isinstance(value, datetime):
        raise ValueError(f'{name} must be a timezone-aware datetime')
    if value.utcoffset() is None:
        if not provider:
            raise ValueError(f'{name} must be a timezone-aware datetime')
        value = value.replace(tzinfo=CHINA_MARKET_TIMEZONE)
    return value.astimezone(timezone.utc)


def _decimal(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise ValueError('financial values require exact decimal text, Decimal or integer')
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('invalid decimal value') from exc
    if not amount.is_finite():
        raise ValueError('decimal value must be finite')
    if amount == 0:
        return '0'
    result = format(amount, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def _review(payload: Mapping[str, object]) -> str:
    review = payload.get('verification')
    if not isinstance(review, Mapping) or review.get('method') != 'human_review':
        raise ValueError('verified manual evidence requires an explicit human-review receipt')
    _text(review.get('reviewer'), 'reviewer')
    _time(review.get('reviewed_at'), 'reviewed_at')
    return json.dumps(dict(review), ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def parse_source_evidence(payload: Mapping[str, object]) -> EvidenceRecord:
    """Normalize without inferring trust, publication dates or absent zeros.

    `source=eastmoney` accepts a report/row envelope with explicit observation
    and source provenance. Present audited columns are still untrusted because
    zero/missing and full-market checks remain incomplete. `status=verified`
    is permitted only for normalized manual evidence with a human-review receipt.
    """
    if not isinstance(payload, Mapping):
        raise ValueError('evidence payload must be a mapping')
    if payload.get('archive_verified', False) is not False:
        raise ValueError('standalone import cannot certify archive_verified; validated archive bundle required')
    source_url = _text(payload.get('source_url'), 'source_url')
    if urlparse(source_url).scheme != 'https' or not urlparse(source_url).netloc:
        raise ValueError('source_url must be an HTTPS document URL')
    document_sha256 = _text(payload.get('document_sha256'), 'document_sha256')
    if not re.fullmatch(r'[0-9a-f]{64}', document_sha256):
        raise ValueError('document_sha256 must be a lowercase SHA256 digest')
    values: dict[str, str | None] = {}
    location = _text(payload.get('report_location'), 'report_location')
    if payload.get('source', 'normalized') == 'eastmoney':
        report = _text(payload.get('report'), 'report')
        raw = payload.get('row')
        if not isinstance(raw, Mapping):
            raise ValueError('provider row must be a mapping')
        published = _time(raw.get('NOTICE_DATE'), 'NOTICE_DATE publication', provider=True)
        updated = _time(raw['UPDATE_DATE'], 'UPDATE_DATE', provider=True) if raw.get('UPDATE_DATE') else None
        for name in SELECTION_EXTENSION_FIELDS:
            spec = SELECTION_FIELD_DICTIONARY[name]
            values[name] = _decimal(raw.get(spec.field)) if spec.report == report and isinstance(spec.field, str) else None
        status = 'untrusted' if any(value is not None for value in values.values()) else 'missing'
        date_only = True  # Vendor NOTICE_DATE does not certify intraday precision.
    elif payload.get('source', 'normalized') == 'normalized':
        raw = payload.get('values')
        if not isinstance(raw, Mapping):
            raise ValueError('values must be a mapping')
        for key, value in raw.items():
            _text(key, 'values key')
            if key.startswith('_'):
                raise ValueError('reserved provenance keys must be supplied through explicit metadata fields')
            values[key] = _decimal(value)
        published = _time(payload.get('published_at'), 'published_at')
        updated = _time(payload['source_updated_at'], 'source_updated_at') if payload.get('source_updated_at') is not None else None
        status = payload.get('status', 'untrusted')
        if status == 'verified':
            values['_verification'] = _review(payload)
        date_only = payload.get('date_only', False)
    else:
        raise ValueError('unsupported evidence source')
    values['_report_location'] = location
    # Archive provenance text may be retained, but never grants archive trust.
    if payload.get('archive_verification_source') is not None:
        values['_archive_verification_source'] = _text(payload['archive_verification_source'], 'archive_verification_source')
    row = EvidenceRecord(
        evidence_id='pending', code=payload.get('code'), kind=payload.get('kind'),
        period=payload.get('period'), published_at=published,
        observed_at=_time(payload.get('observed_at'), 'observed_at'),
        source_updated_at=updated, source_url=source_url, document_sha256=document_sha256,
        values=values, status=status, basis_id=payload.get('basis_id'),
        supersedes_id=payload.get('supersedes_id'), date_only=date_only, archive_verified=False,
    )
    identity = evidence_identity(row)
    if payload.get('evidence_id') is not None and payload['evidence_id'] != identity:
        raise ValueError('evidence identity conflicts with canonical content')
    return replace(row, evidence_id=identity)
