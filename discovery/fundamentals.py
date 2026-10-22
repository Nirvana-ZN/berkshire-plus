"""Point-in-time amounts, operating confirmation and schema4 evidence codec.

Financial records are cumulative CNY amounts on a verified comparable basis.
Legacy FinancialRow source dates are never converted into evidence here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from hashlib import sha256
import json
import re

from discovery.constants import CHINA_MARKET_TIMEZONE
from discovery.evidence_store import available_at, select_evidence_as_of
from discovery.fundamental_types import (CapitalEvent, CoverageWindow, EvidenceRecord,
                                         FundamentalSnapshot, MetricResult, OperatingResult,
                                         evidence_identity)
from discovery.metrics import _context, _working_precision

_AMOUNTS = frozenset({'deducted_net_income', 'net_income', 'revenue', 'ocf', 'capex'})
CLASSIFICATION_VERSION = 'industry-branch-v1'


def period_order(period: str) -> tuple[int, int]:
    if not isinstance(period, str) or not re.fullmatch(r'[0-9]{4}(Q1|H1|Q3)?', period):
        raise ValueError('invalid cumulative report period')
    return int(period[:4]), {'Q1': 1, 'H1': 2, 'Q3': 3, '': 4}[period[4:]]


def required_ttm_periods(end_period: str) -> tuple[str, ...]:
    year, _ = period_order(end_period)
    suffix = end_period[4:]
    return (end_period,) if not suffix else (str(year - 1), end_period, f'{year - 1}{suffix}')


def _prior_year(period: str) -> str:
    year, _ = period_order(period)
    return f'{year - 1}{period[4:]}'


def _prior_quarter(period: str) -> str:
    year, quarter = period_order(period)
    return str(year - 1) if quarter == 1 else f'{year}{("Q1", "H1", "Q3")[quarter - 2]}'


def _fact(rows: Sequence[EvidenceRecord], field: str, period: str) -> tuple[MetricResult, str | None]:
    candidates = [row for row in rows if row.kind == 'financial' and row.period == period and field in row.values]
    ids = tuple(sorted({row.evidence_id for row in candidates}))
    if not candidates:
        return MetricResult(None, 'missing', f'{field}:{period}:missing', ()), None
    # Untrusted extension records cannot override separately verified legacy facts.
    trusted = [row for row in candidates if row.status == 'verified' and row.values[field] is not None]
    if not trusted:
        status = ('untrusted' if any(row.status == 'untrusted' for row in candidates) else
                  'not_applicable' if all(row.status == 'not_applicable' for row in candidates) else 'missing')
        return MetricResult(None, status, f'{field}:{period}:{status}', ids), None
    if any(row.basis_id.casefold().startswith(('unknown', 'unverified'))
           or row.values.get('_basis_status') in {'unknown', 'unverified'} for row in trusted):
        return MetricResult(None, 'untrusted', 'basis_unverified', ids), None
    numbers = []
    try:
        numbers = [Decimal(row.values[field]) for row in trusted]
        if any(not value.is_finite() for value in numbers):
            raise ValueError('nonfinite')
    except (InvalidOperation, ValueError):
        return MetricResult(None, 'untrusted', f'{field}:{period}:invalid', ids), None
    if len({row.code for row in trusted}) != 1 or len({row.basis_id for row in trusted}) != 1 or len(set(numbers)) != 1:
        return MetricResult(None, 'untrusted', 'conflicting_facts', ids), None
    return MetricResult(numbers[0], 'verified', None, tuple(sorted(row.evidence_id for row in trusted))), trusted[0].basis_id


def _arithmetic(values: Sequence[MetricResult], fn) -> MetricResult:
    ids = tuple(sorted({key for value in values for key in value.evidence_ids}))
    for status in ('untrusted', 'missing', 'not_applicable'):
        failed = next((value for value in values if value.status == status), None)
        if failed is not None:
            return MetricResult(None, status, failed.reason, ids)
    amounts = tuple(value.value for value in values)
    precision = _working_precision(amounts)
    if precision is not None:
        try:
            with localcontext(_context(precision)):
                result = fn(*amounts)
            if result.is_finite():
                return MetricResult(result, 'verified', None, ids)
        except DecimalException:
            pass
    return MetricResult(None, 'untrusted', 'fundamentals_numeric_range', ids)


def _comparable(rows, field, periods):
    facts = [_fact(rows, field, period) for period in periods]
    results = tuple(item[0] for item in facts)
    bases = {basis for result, basis in facts if result.status == 'verified'}
    codes = {row.code for row in rows if row.kind == 'financial' and row.period in periods and row.values.get(field) is not None}
    if len(bases) > 1 or len(codes) > 1:
        return (MetricResult(None, 'untrusted', 'incomparable_basis', tuple(sorted({key for result in results for key in result.evidence_ids}))),)
    return results


def ttm_amount(rows: Sequence[EvidenceRecord], field: str, end_period: str) -> MetricResult:
    if field not in _AMOUNTS:
        raise ValueError('TTM requires an amount field, never EPS or ratios')
    values = _comparable(rows, field, required_ttm_periods(end_period))
    return _arithmetic(values, lambda *v: v[0] if len(v) == 1 else v[0] + v[1] - v[2])


def quarter_amount(rows: Sequence[EvidenceRecord], field: str, end_period: str) -> MetricResult:
    if field not in _AMOUNTS:
        raise ValueError('quarter requires an amount field, never EPS or ratios')
    _, quarter = period_order(end_period)
    periods = (end_period,) if quarter == 1 else (end_period, _prior_quarter(end_period))
    return _arithmetic(_comparable(rows, field, periods), lambda *v: v[0] if len(v) == 1 else v[0] - v[1])


def _yoy(current: MetricResult, prior: MetricResult) -> MetricResult:
    if prior.value is not None and prior.value <= 0:
        return MetricResult(None, 'missing', 'comparison_not_positive', tuple(sorted(set(current.evidence_ids + prior.evidence_ids))))
    return _arithmetic((current, prior), lambda now, old: now / old - 1)


def operating_state(snapshot: FundamentalSnapshot, policy: Mapping[str, object]) -> OperatingResult:
    """Use a supplied reviewed calendar; never infer disclosure from today's month.

    Calendar date deadlines expire after the complete Shanghai civil day.
    Nonpositive prior profit has no meaningful conventional growth rate.
    """
    rows = snapshot.annuals + snapshot.interims
    latest = max((row.period for row in rows), key=period_order, default=None)
    def unknown(reason, metrics=None):
        return OperatingResult('unknown', latest, (reason,), metrics or {})
    try:
        if (policy.get('version') != 'operating-v1' or policy.get('calendar_status') != 'verified'
                or not policy.get('calendar_version') or not str(policy.get('calendar_source_url', '')).startswith('https://')
                or not re.fullmatch('[0-9a-f]{64}', str(policy.get('calendar_document_sha256', '')))):
            return unknown('operating_policy_unavailable')
        drop = policy['profit_drop']
        if not isinstance(drop, (str, Decimal)):
            raise ValueError('profit_drop requires decimal text')
        drop = Decimal(drop)
        if not drop.is_finite() or not -1 <= drop < 0:
            raise ValueError('invalid profit_drop')
        calendar = policy['period_due_dates']
        if not isinstance(calendar, Mapping) or not calendar:
            return unknown('operating_calendar_unavailable')
        due = []
        for period, value in calendar.items():
            period_order(period)
            day = date.fromisoformat(value)
            deadline = datetime.combine(day + timedelta(days=1), time.min, CHINA_MARKET_TIMEZONE)
            if deadline <= snapshot.cutoff_at:
                due.append(period)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return unknown('operating_policy_invalid')
    if latest is None:
        return unknown('financial_reports_missing')
    if due and period_order(latest) < max(map(period_order, due)):
        return unknown('report_overdue')
    prior = _prior_year(latest)
    profit = _comparable(rows, 'deducted_net_income', (latest, prior))
    ocf = _comparable(rows, 'ocf', (latest, prior))
    yoy = _yoy(*profit) if len(profit) == 2 else profit[0]
    metrics = {'deducted_profit_yoy': yoy, 'current_ocf': ocf[0], 'prior_ocf': ocf[-1]}
    if any(value.status != 'verified' for value in metrics.values()):
        return unknown('operating_required_data_missing', metrics)
    reasons = []
    if yoy.value <= drop:
        reasons.append('deducted_profit_drop')
    if ocf[1].value > 0 and ocf[0].value < 0:
        reasons.append('ocf_turned_negative')
    if reasons:
        return OperatingResult('deteriorating', latest, tuple(reasons), metrics)
    previous = _prior_quarter(latest)
    # Compare each quarter against the same quarter last year, never adjacent levels.
    for key, period in (('latest_quarter_yoy', latest), ('previous_quarter_yoy', previous)):
        endpoints = (period, _prior_year(period))
        comparable = [row for row in rows if row.kind == 'financial' and row.period in endpoints and row.status == 'verified' and row.values.get('deducted_net_income') is not None]
        bases = {row.basis_id for row in comparable}
        metrics[key] = (MetricResult(None, 'untrusted', 'incomparable_basis', tuple(sorted(row.evidence_id for row in comparable))) if len(bases) > 1 else
                        _yoy(quarter_amount(rows, 'deducted_net_income', period), quarter_amount(rows, 'deducted_net_income', _prior_year(period))))
    revenue_periods = set(required_ttm_periods(latest) + required_ttm_periods(prior))
    comparable_revenue = [row for row in rows if row.kind == 'financial' and row.period in revenue_periods and row.status == 'verified' and row.values.get('revenue') is not None]
    revenue_bases = {row.basis_id for row in comparable_revenue}
    metrics['revenue_ttm_yoy'] = (MetricResult(None, 'untrusted', 'incomparable_basis', tuple(sorted(row.evidence_id for row in comparable_revenue))) if len(revenue_bases) > 1 else
                                  _yoy(ttm_amount(rows, 'revenue', latest), ttm_amount(rows, 'revenue', prior)))
    additional = [metrics[name] for name in ('latest_quarter_yoy', 'previous_quarter_yoy', 'revenue_ttm_yoy')]
    if any(value.status != 'verified' for value in additional):
        return OperatingResult('stable', latest, ('improvement_evidence_insufficient',), metrics)
    now, before, revenue = (value.value for value in additional)
    return OperatingResult('improving' if now > before > 0 and revenue > 0 else 'stable', latest, (), metrics)


def plain(value: object) -> object:
    """Canonical schema4 primitives; all new financial values are strings."""
    if isinstance(value, (EvidenceRecord, CapitalEvent, CoverageWindow, OperatingResult, MetricResult)):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError('datetime must be aware')
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('nonfinite financial value')
        return str(value)
    if value is None or type(value) in {str, bool, int}:
        return value
    raise ValueError('unsupported schema4 value')


def _digest(value: object) -> str:
    return sha256(json.dumps(plain(value), sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def evidence_from_payload(value: Mapping[str, object]) -> EvidenceRecord:
    raw = dict(value)
    for key in ('published_at', 'observed_at', 'source_updated_at'):
        raw[key] = datetime.fromisoformat(raw[key]) if raw[key] is not None else None
    record = EvidenceRecord(**raw)
    if evidence_identity(record) != record.evidence_id:
        raise ValueError('evidence identity mismatch')
    return record


def freeze_fundamentals(records: Sequence[EvidenceRecord], actions: Sequence[CapitalEvent],
                        coverage: Sequence[CoverageWindow], *, code: str,
                        cutoff_at: datetime, current_mcap: Decimal | None) -> FundamentalSnapshot:
    if current_mcap is not None and (not isinstance(current_mcap, Decimal) or not current_mcap.is_finite() or current_mcap <= 0):
        raise ValueError('current_mcap must be a finite positive Decimal')
    selected = select_evidence_as_of(records, code=code, cutoff=cutoff_at)
    if any(evidence_identity(row) != row.evidence_id for row in selected):
        raise ValueError('evidence identity mismatch')
    derived = {row.values.get('_source_evidence_id') for row in selected
               if row.kind == 'company_classification' and row.values.get('_classification_version') == CLASSIFICATION_VERSION}
    selected = tuple(sorted((*selected, *(classification_evidence(row, industry=row.values['_industry'], version=CLASSIFICATION_VERSION)
                            for row in selected if row.kind == 'company_profile' and row.evidence_id not in derived)),
                            key=lambda row: (row.kind, row.period, available_at(row), row.evidence_id)))
    annuals, interims, extensions = [], [], []
    for row in selected:
        if row.kind == 'financial':
            _, quarter = period_order(row.period)
            (annuals if quarter == 4 else interims).append(row)
        else:
            extensions.append(row)
    from market.corporate_actions import parse_capital_events
    valid_actions = parse_capital_events(selected)
    # Supplied actions can never bypass source-version verification.
    if any(action.code != code for action in actions):
        raise ValueError('action company mismatch')
    verified_actions = {action.evidence_id: action for action in valid_actions}
    for action in actions:
        if action.evidence_id in verified_actions and action != verified_actions[action.evidence_id]:
            raise ValueError('action differs from its selected source evidence')
    selected_actions = tuple(action for action in valid_actions if action.effective_at <= cutoff_at)
    unavailable_ids = {row.evidence_id for row in records if row.code != code or available_at(row) > cutoff_at}
    # CoverageWindow deliberately has no creation timestamp. Production callers
    # must use load_coverage(cutoff=...); referenced superseded evidence need not
    # be selected as current facts to retain the verified coverage assertion.
    windows = tuple(sorted((window for window in coverage if window.code == code and not set(window.evidence_ids) & unavailable_ids),
                           key=lambda window: (window.kind, window.start, window.end, window.complete, window.evidence_ids)))
    data = dict(code=code, cutoff_at=cutoff_at, annuals=tuple(annuals), interims=tuple(interims),
                actions=selected_actions, extensions=tuple(extensions), coverage=windows, current_mcap=current_mcap)
    return FundamentalSnapshot(**data, fingerprint=_digest(data))


def classification_evidence(source: EvidenceRecord, *, industry: str, version: str) -> EvidenceRecord:
    """Derive only the category; source trust and actual document identity survive."""
    from discovery.industry import classify_industry_branch
    if source.values.get('_industry') != industry or not version:
        raise ValueError('classification requires an industry label retained in source evidence')
    record = replace(source, evidence_id='pending', kind='company_classification', supersedes_id=None,
                     values={'_industry_branch': classify_industry_branch(industry), '_classification_version': version,
                             '_industry': industry, '_source_evidence_id': source.evidence_id})
    return replace(record, evidence_id=evidence_identity(record))


def deducted_pe(snapshot: FundamentalSnapshot) -> MetricResult:
    """Current dated cap over positive deducted TTM; no annual-share fallback."""
    rows = snapshot.annuals + snapshot.interims
    latest = max((row.period for row in rows), key=period_order, default=None)
    profit = ttm_amount(rows, 'deducted_net_income', latest) if latest else MetricResult(None, 'missing', 'financial_reports_missing', ())
    if profit.value is not None and profit.value <= 0:
        return MetricResult(None, 'missing', 'deducted_profit_not_positive', profit.evidence_ids)
    cap = MetricResult(snapshot.current_mcap, 'verified' if snapshot.current_mcap is not None else 'missing',
                       None if snapshot.current_mcap is not None else 'current_mcap_missing',
                       tuple(row.evidence_id for row in snapshot.extensions if row.kind == 'current_market_cap'))
    return _arithmetic((cap, profit), lambda market_cap, earnings: market_cap / earnings)


def market_cap_evidence(quote, *, code: str, market_date: date | None, cutoff_at: datetime) -> EvidenceRecord:
    """Single quote-to-frozen-price eligibility boundary; retain every rejection."""
    status, reason, amount = quote.status, quote.reason, quote.value
    if market_date is None:
        status, reason, amount = 'missing', 'frozen_price_market_day_missing', None
    elif quote.code != code or (quote.market_date is not None and quote.market_date != market_date):
        status, reason, amount = 'untrusted', 'current_mcap_code_or_day_mismatch', None
    if quote.observed_at > cutoff_at:
        status, reason, amount = 'missing', 'current_mcap_after_cutoff', None
    period = market_date or quote.market_date
    record = EvidenceRecord('pending', code, 'current_market_cap', period.isoformat() if period else 'unknown',
                            quote.observed_at, quote.observed_at, None, quote.source_url, quote.response_sha256,
                            {'current_mcap': str(amount) if amount is not None else None,
                             '_quote_code': quote.code, '_market_date': quote.market_date.isoformat() if quote.market_date else None,
                             '_quote_status': quote.status, '_quote_reason': quote.reason,
                             '_required_market_date': market_date.isoformat() if market_date else None,
                             '_source': quote.source, '_reason': reason}, status, 'current-market-cap-cny')
    return replace(record, evidence_id=evidence_identity(record))


def snapshot_payload(snapshot: FundamentalSnapshot) -> dict[str, object]:
    return {'annual_evidence': plain(snapshot.annuals), 'interim_history': plain(snapshot.interims),
            'capital_events': plain(snapshot.actions), 'fundamental_extensions': plain(snapshot.extensions),
            'coverage': plain(snapshot.coverage), 'evidence_ids': sorted({row.evidence_id for row in snapshot.annuals + snapshot.interims + snapshot.extensions}
                | {key for window in snapshot.coverage for key in window.evidence_ids}),
            'current_mcap': plain(snapshot.current_mcap), 'fundamental_fingerprint': snapshot.fingerprint,
            'decision_cutoff_at': plain(snapshot.cutoff_at)}


def fundamentals_from_payload(payload: Mapping[str, object], *, code: str) -> FundamentalSnapshot:
    """Validate and reconstruct the exact schema4 frozen input, including digest."""
    records = tuple(evidence_from_payload(row) for key in ('annual_evidence', 'interim_history', 'fundamental_extensions') for row in payload[key])
    if any(row.code != code for row in records):
        raise ValueError('fundamental company mismatch')
    windows = tuple(CoverageWindow(row['code'], row['kind'], date.fromisoformat(row['start']), date.fromisoformat(row['end']),
                                   row['complete'], tuple(row['evidence_ids'])) for row in payload['coverage'])
    cap = payload['current_mcap']
    if cap is not None and not isinstance(cap, str):
        raise ValueError('current_mcap requires decimal text')
    snapshot = freeze_fundamentals(records, (), windows, code=code,
                                   cutoff_at=datetime.fromisoformat(payload['decision_cutoff_at']),
                                   current_mcap=Decimal(cap) if cap is not None else None)
    if snapshot_payload(snapshot) != {key: plain(payload[key]) for key in snapshot_payload(snapshot)}:
        raise ValueError('frozen fundamentals integrity mismatch')
    return snapshot
