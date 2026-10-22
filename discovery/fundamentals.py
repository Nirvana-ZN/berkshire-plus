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


def roic_gross(*, ebit: Decimal, tax_rate: Decimal, ic_begin: Decimal, ic_end: Decimal) -> MetricResult:
    """Gross IC return as a fraction; selected verified inputs, never vendor ROIC."""
    inputs = (ebit, tax_rate, ic_begin, ic_end)
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in inputs):
        return MetricResult(None, 'untrusted', 'capital_return_inputs_invalid', ())
    if not 0 <= tax_rate <= 1 or ic_begin <= 0 or ic_end <= 0:
        return MetricResult(None, 'untrusted', 'capital_return_inputs_out_of_range', ())
    return _arithmetic(tuple(MetricResult(value, 'verified', None, ()) for value in inputs),
                       lambda earnings, tax, begin, end: earnings * (1 - tax) / ((begin + end) / 2))


def _snapshot_rows(snapshot: FundamentalSnapshot) -> tuple[EvidenceRecord, ...]:
    # Freeze/repository own supersession selection. Consumers only enforce the
    # same-company cutoff boundary; they never select another source version.
    return tuple(row for row in snapshot.annuals + snapshot.interims
                 if row.kind == 'financial' and row.code == snapshot.code and available_at(row) <= snapshot.cutoff_at)


def _on_basis(rows, requirements, result):
    facts = [_fact(rows, field, period) for field, period in set(requirements)]
    bases = {basis for fact, basis in facts if fact.status == 'verified'}
    if len(bases) > 1:
        ids = tuple(sorted(set(result.evidence_ids).union(*(fact.evidence_ids for fact, _ in facts))))
        return MetricResult(None, 'untrusted', 'incomparable_basis', ids)
    return result


def _facts_calculation(rows, requirements, fn):
    return _on_basis(rows, requirements, _arithmetic(tuple(_fact(rows, field, period)[0]
                                                         for field, period in requirements), fn))


def classification_identity(snapshot) -> tuple[MetricResult, str | None]:
    """Trusted classification identity, independent of metric applicability."""
    rows = tuple(row for row in snapshot.extensions if row.kind == 'company_classification'
                 and row.code == snapshot.code and available_at(row) <= snapshot.cutoff_at)
    ids = tuple(sorted(row.evidence_id for row in rows))
    if not rows:
        return MetricResult(None, 'missing', 'industry_classification_missing', ids), None
    trusted = tuple(row for row in rows if row.status == 'verified')
    if not trusted:
        status = 'untrusted' if any(row.status == 'untrusted' for row in rows) else 'missing'
        return MetricResult(None, status, f'industry_classification_{status}', ids), None
    branches = {row.values.get('_industry_branch') for row in trusted}
    if (len(branches) != 1 or any(row.values.get('_classification_version') != CLASSIFICATION_VERSION for row in trusted)
            or not branches <= {'bank', 'insurance', 'other_financial', 'nonfinancial', 'unknown'}):
        return MetricResult(None, 'untrusted', 'industry_classification_invalid', ids), None
    branch = next(iter(branches))
    ids = tuple(sorted(row.evidence_id for row in trusted))
    if branch == 'unknown':
        return MetricResult(None, 'missing', 'industry_branch_unknown', ids), branch
    return MetricResult(Decimal(1), 'verified', None, ids), branch


def _classification(snapshot):
    identity, branch = classification_identity(snapshot)
    if identity.status != 'verified':
        return identity
    if branch != 'nonfinancial':
        return replace(identity, value=None, status='not_applicable', reason='industrial_capital_return')
    return identity


def _effective_tax_rate(rows, period):
    requirements = [('pretax_income', period), ('income_tax', period)]
    current = [_fact(rows, field, when)[0] for field, when in requirements]
    failure = _arithmetic(current, lambda *_: Decimal(0))
    if failure.status != 'verified':
        return failure, requirements
    pretax, tax = (fact.value for fact in current)
    rate = _facts_calculation(rows, requirements, lambda profit, expense: expense / profit) if pretax > 0 else None
    if rate is not None and rate.status != 'verified':
        return rate, requirements
    if rate is not None and 0 <= rate.value <= 1:
        return rate, requirements
    # Only a known invalid effective rate invokes fallback. Missing or untrusted
    # current tax facts never become a historical or statutory estimate.
    valid = []
    inspected = list(current)
    for year in range(int(period) - 3, int(period)):
        prior_requirements = [('pretax_income', str(year)), ('income_tax', str(year))]
        profit, _ = _fact(rows, 'pretax_income', str(year))
        expense, _ = _fact(rows, 'income_tax', str(year))
        inspected.extend((profit, expense))
        if profit.status != 'verified' or expense.status != 'verified' or profit.value <= 0:
            continue
        prior = _facts_calculation(rows, prior_requirements, lambda p, t: t / p)
        if prior.status == 'verified' and 0 <= prior.value <= 1:
            valid.append(prior)
            requirements.extend(prior_requirements)
    ids = tuple(sorted({key for fact in inspected for key in fact.evidence_ids}))
    if not valid:
        return MetricResult(None, 'missing', f'effective_tax_rate:{period}:missing', ids), requirements
    ordered = sorted(valid, key=lambda fact: fact.value)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else _arithmetic(ordered[middle - 1:middle + 1], lambda a, b: (a + b) / 2)
    return replace(median, evidence_ids=ids), requirements


def capital_return(snapshot: FundamentalSnapshot) -> MetricResult:
    """Five consecutive annual gross ROICs require six year-end IC balances.

    ROE and provider ROIC are separate legacy metrics. All endpoints and tax
    fallback inputs must share a verified consolidation/currency/accounting basis.
    """
    return _capital_return(snapshot, source_only=False)


def capital_return_sources(snapshot: FundamentalSnapshot) -> MetricResult:
    """Use the producer's exact source selection without its economic domain gates."""
    return _capital_return(snapshot, source_only=True)


def _capital_return(snapshot: FundamentalSnapshot, *, source_only: bool) -> MetricResult:
    classification = _classification(snapshot)
    if classification.status != 'verified':
        return classification
    rows = _snapshot_rows(snapshot)
    latest = max((int(row.period) for row in rows if len(row.period) == 4), default=None)
    if latest is None:
        return MetricResult(None, 'missing', 'financial_reports_missing', classification.evidence_ids)
    requirements = []
    balances = {}
    for year in range(latest - 5, latest + 1):
        period = str(year)
        needed = [('total_equity_including_minority', period), ('interest_debt', period)]
        requirements.extend(needed)
        debt, _ = _fact(rows, 'interest_debt', period)
        balance = _facts_calculation(rows, needed, lambda equity, debt: equity + debt)
        if debt.value is not None and debt.value < 0:
            balance = MetricResult(None, 'untrusted', f'interest_debt:{period}:negative', balance.evidence_ids)
        elif not source_only and balance.value is not None and balance.value <= 0:
            balance = MetricResult(None, 'untrusted', f'invested_capital:{period}:not_positive', balance.evidence_ids)
        balances[year] = balance
    returns = []
    for year in range(latest - 4, latest + 1):
        period = str(year)
        earnings, _ = _fact(rows, 'ebit', period)
        if earnings.status == 'verified' or earnings.reason in {'conflicting_facts', 'basis_unverified', f'ebit:{period}:invalid'}:
            requirements.append(('ebit', period))
        else:
            needed = [('pretax_income', period), ('interest_expense', period)]
            earnings = _facts_calculation(rows, needed, lambda pretax, interest: pretax + interest)
            requirements.extend(needed)
        tax, needed = _effective_tax_rate(rows, period)
        requirements.extend(needed)
        inputs = (earnings, tax, balances[year - 1], balances[year])
        # Delegate arithmetic to the public unit-decimal calculation once.
        ready = _arithmetic(inputs, lambda *_: Decimal(0))
        if ready.status == 'verified' and not source_only:
            ready = replace(roic_gross(ebit=earnings.value, tax_rate=tax.value,
                                      ic_begin=balances[year - 1].value, ic_end=balances[year].value),
                            evidence_ids=ready.evidence_ids)
        returns.append(ready)
    result = _on_basis(rows, requirements, _arithmetic(returns, lambda *values: sum(values) / 5))
    return replace(result, evidence_ids=tuple(sorted(set(result.evidence_ids + classification.evidence_ids))))


def _flow_ratio(rows, numerator, denominator, period):
    needed = [(field, when) for field in (numerator, denominator) for when in required_ttm_periods(period)]
    top, bottom = (ttm_amount(rows, field, period) for field in (numerator, denominator))
    result = _arithmetic((top, bottom), lambda *_: Decimal(0))
    if result.status == 'verified':
        result = (MetricResult(None, 'missing', f'{denominator}:{period}:not_positive', result.evidence_ids)
                  if bottom.value <= 0 else _arithmetic((top, bottom), lambda a, b: a / b))
    return _on_basis(rows, needed, result)


def _balance_ratio(rows, fields, denominator, period):
    needed = [(field, period) for field in fields] + [(denominator, when) for when in required_ttm_periods(period)]
    amounts = tuple(_fact(rows, field, period)[0] for field in fields)
    revenue = ttm_amount(rows, denominator, period)
    result = _arithmetic((*amounts, revenue), lambda *_: Decimal(0))
    if result.status == 'verified':
        if any(amount.value < 0 for amount in amounts):
            result = MetricResult(None, 'untrusted', f'balance:{period}:negative', result.evidence_ids)
        elif revenue.value <= 0:
            result = MetricResult(None, 'missing', f'{denominator}:{period}:not_positive', result.evidence_ids)
        else:
            result = _arithmetic((*amounts, revenue), lambda *v: sum(v[:-1]) / v[-1])
    return _on_basis(rows, needed, result)


def collection_metrics(snapshot: FundamentalSnapshot) -> dict[str, MetricResult]:
    """Auditable shadow explanations; balance endpoints over same-period TTM flows."""
    rows = _snapshot_rows(snapshot)
    latest = max((row.period for row in rows), key=period_order, default=None)
    names = ('receivables_ratio', 'cash_conversion', 'inventory_ratio')
    if latest is None:
        return {key: MetricResult(None, 'missing', 'financial_reports_missing', ())
                for key in (*names, *(f'prior_{name}' for name in names),
                            *(f'{name}_change' for name in names), 'restricted_cash_ratio')}
    metrics = {}
    for label, fields in (('receivables_ratio', ('accounts_receivable', 'contract_assets')),
                          ('cash_conversion', ('ocf', 'net_income')), ('inventory_ratio', ('inventory',))):
        periods = (latest, _prior_year(latest))
        for prefix, period in zip(('', 'prior_'), periods):
            metrics[prefix + label] = (_flow_ratio(rows, *fields, period) if label == 'cash_conversion'
                                       else _balance_ratio(rows, fields, 'revenue', period))
        needed = ([(field, when) for field in fields for period in periods for when in required_ttm_periods(period)]
                  if label == 'cash_conversion' else
                  [(field, period) for field in fields for period in periods]
                  + [('revenue', when) for period in periods for when in required_ttm_periods(period)])
        metrics[label + '_change'] = _on_basis(rows, needed, _arithmetic(
            (metrics[label], metrics['prior_' + label]), lambda now, before: now - before))
    needed = [('restricted_cash', latest), ('cash', latest)]
    restriction, cash = (_fact(rows, field, latest)[0] for field in ('restricted_cash', 'cash'))
    ratio = _facts_calculation(rows, needed, lambda *_: Decimal(0))
    if ratio.status == 'verified':
        if cash.value <= 0 or restriction.value < 0 or restriction.value > cash.value:
            ratio = MetricResult(None, 'untrusted', 'restricted_cash_inputs_out_of_range', ratio.evidence_ids)
        else:
            ratio = _arithmetic((restriction, cash), lambda a, b: a / b)
    metrics['restricted_cash_ratio'] = ratio
    return metrics


def collection_signals(snapshot: FundamentalSnapshot, policy: Mapping[str, object]) -> tuple[str, ...]:
    """Shadow flags/status markers only; callers must never turn these into gates."""
    required = ('version', 'receivables_ratio_increase_min', 'restricted_cash_ratio_min')
    if any(key not in policy for key in required):
        return ('collection_policy_missing',)
    try:
        thresholds = tuple(policy[key] for key in required[1:])
        if policy['version'] != 'collection-v1' or any(not isinstance(value, (str, Decimal)) for value in thresholds):
            raise ValueError('invalid collection policy')
        increase, restricted = map(Decimal, thresholds)
        if any(not value.is_finite() or not 0 <= value <= 1 for value in (increase, restricted)):
            raise ValueError('invalid collection threshold')
    except (InvalidOperation, ValueError):
        return ('collection_policy_untrusted',)
    metrics = collection_metrics(snapshot)
    pressure = _arithmetic((metrics['receivables_ratio_change'], metrics['cash_conversion_change']),
                           lambda a, b: Decimal(int(a >= increase and b < 0)))
    # Both individually comparable ratios must also describe the same entity.
    pressure_ids = set(pressure.evidence_ids)
    pressure_rows = tuple(row for row in _snapshot_rows(snapshot) if row.evidence_id in pressure_ids)
    needed = [(field, row.period) for row in pressure_rows
              for field in ('accounts_receivable', 'contract_assets', 'revenue', 'ocf', 'net_income')
              if field in row.values]
    pressure = _on_basis(pressure_rows, needed, pressure)
    material = metrics['restricted_cash_ratio']
    signals = []
    for name, result, triggered in (('collection_pressure', pressure, pressure.value == 1),
                                     ('restricted_cash_material', material, material.value is not None and material.value >= restricted)):
        if result.status != 'verified':
            signals.append(f'{name}_{result.status}')
        elif triggered:
            signals.append(name)
    return tuple(signals)


def _yoy(current: MetricResult, prior: MetricResult) -> MetricResult:
    if prior.value is not None and prior.value <= 0:
        return MetricResult(None, 'missing', 'comparison_not_positive', tuple(sorted(set(current.evidence_ids + prior.evidence_ids))))
    return _arithmetic((current, prior), lambda now, old: now / old - 1)


def operating_state(snapshot: FundamentalSnapshot, policy: Mapping[str, object]) -> OperatingResult:
    """Use a supplied reviewed calendar; never infer disclosure from today's month.

    Reviewed completeness must cover the cutoff's Shanghai business date.
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
        if policy.get('calendar_complete') is not True:
            return unknown('operating_calendar_incomplete')
        coverage_end = policy.get('calendar_coverage_end')
        try:
            covered_through = date.fromisoformat(coverage_end)
            if covered_through.isoformat() != coverage_end:
                raise ValueError('calendar coverage requires an ISO date')
        except (TypeError, ValueError):
            return unknown('operating_calendar_coverage_unavailable')
        if covered_through < snapshot.cutoff_at.astimezone(CHINA_MARKET_TIMEZONE).date():
            return unknown('operating_calendar_expired')
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


def coverage_reference_evidence(record: EvidenceRecord) -> EvidenceRecord:
    """Freeze an original source verbatim without making it a current fact."""
    if record.kind == 'coverage_reference' or evidence_identity(record) != record.evidence_id:
        raise ValueError('invalid coverage reference identity')
    encoded = json.dumps(plain(record), ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False)
    wrapper = replace(record, evidence_id='pending', kind='coverage_reference',
                      supersedes_id=None, values={'record': encoded})
    return replace(wrapper, evidence_id=evidence_identity(wrapper))


def _coverage_reference_original(wrapper: EvidenceRecord) -> EvidenceRecord:
    try:
        original = evidence_from_payload(json.loads(wrapper.values['record']))
        if coverage_reference_evidence(original) != wrapper:
            raise ValueError('coverage reference differs from original source')
        return original
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('invalid frozen coverage reference') from error


def _coverage_reference_closure(records, reference_ids):
    """Proof dependencies only; authoritative fact selection remains separate."""
    retained = set(reference_ids) & records.keys()
    while True:
        added = {key for key, row in records.items() if row.supersedes_id in retained
                 and (row.kind, row.period) == (records[row.supersedes_id].kind,
                                                records[row.supersedes_id].period)} - retained
        if not added:
            return retained
        retained.update(added)


def coverage_reference_records(snapshot: FundamentalSnapshot) -> dict[str, EvidenceRecord]:
    """Return frozen original proof records, retaining their actual status."""
    result = {}
    for row in snapshot.annuals + snapshot.interims + snapshot.extensions:
        original = _coverage_reference_original(row) if row.kind == 'coverage_reference' else row
        if original.code != snapshot.code or available_at(original) > snapshot.cutoff_at:
            raise ValueError('coverage reference company/cutoff mismatch')
        result[original.evidence_id] = original
    return result


def verified_coverage_sources(snapshot: FundamentalSnapshot, window: CoverageWindow) -> tuple[bool, tuple[str, ...]]:
    """Verify original source trust and any reviewed receipt revision/revocation."""
    records = coverage_reference_records(snapshot)
    return _verified_coverage_records(records, window, cutoff=snapshot.cutoff_at)


def verified_coverage_records(records: Sequence[EvidenceRecord], window: CoverageWindow) -> tuple[bool, tuple[str, ...]]:
    """The same proof boundary for pure callers with preselected cutoff evidence.

    Unwrap only for proof validation; originals must never become active facts.
    The caller, like the shareholder calculators, owns cutoff selection.
    """
    originals = tuple(_coverage_reference_original(row) if row.kind == 'coverage_reference' else row
                      for row in records)
    if any(row.code != window.code for row in originals):
        return False, window.evidence_ids
    return _verified_coverage_records({row.evidence_id: row for row in originals}, window,
        cutoff=max((available_at(row) for row in originals), default=datetime.min.replace(tzinfo=timezone.utc)))


def _verified_coverage_records(records, window, *, cutoff):
    from market.evidence import SHAREHOLDER_COVERAGE_SCOPES

    ids = _coverage_reference_closure(records, window.evidence_ids)
    audit_ids = tuple(sorted(ids | set(window.evidence_ids)))
    if not window.evidence_ids or any(key not in records or records[key].status != 'verified'
                                      for key in window.evidence_ids):
        return False, audit_ids
    # Untrusted successors cannot erase a verified withdrawal. The shared
    # selector determines the latest verified source versions at this cutoff.
    receipt_kind = 'shareholder_return_coverage' if window.kind in SHAREHOLDER_COVERAGE_SCOPES.values() else 'capital_event_coverage'
    receipts = select_evidence_as_of(tuple(records[key] for key in ids
        if records[key].kind == receipt_kind and records[key].status == 'verified'),
        code=window.code, cutoff=cutoff)
    if receipt_kind == 'shareholder_return_coverage' and not receipts:
        return False, audit_ids
    expected_scope = next((scope for scope, kind in SHAREHOLDER_COVERAGE_SCOPES.items() if kind == window.kind), 'all_capital_actions')
    required = {field for key in window.evidence_ids if records[key].kind == receipt_kind
                for field in ('scope', 'complete', 'start', 'end') if field in records[key].values}
    for receipt in receipts:
        values = receipt.values
        if (not required <= values.keys() or values.get('stage') in {'withdrawn', 'revoked'}
                or values.get('scope', 'all_capital_actions') != expected_scope
                or values.get('complete', 'true') != 'true'):
            return False, audit_ids
        try:
            if receipt_kind == 'shareholder_return_coverage' and (
                    values.get('start') != window.start.isoformat() or values.get('end') != window.end.isoformat()):
                return False, audit_ids
            if (('start' in values and date.fromisoformat(values['start']) > window.start)
                    or ('end' in values and date.fromisoformat(values['end']) < window.end)):
                return False, audit_ids
        except (TypeError, ValueError):
            return False, audit_ids
    return True, audit_ids


def freeze_fundamentals(records: Sequence[EvidenceRecord], actions: Sequence[CapitalEvent],
                        coverage: Sequence[CoverageWindow], *, code: str,
                        cutoff_at: datetime, current_mcap: Decimal | None) -> FundamentalSnapshot:
    if current_mcap is not None and (not isinstance(current_mcap, Decimal) or not current_mcap.is_finite() or current_mcap <= 0):
        raise ValueError('current_mcap must be a finite positive Decimal')
    proofs = tuple(_coverage_reference_original(row) for row in records if row.kind == 'coverage_reference')
    if any(row.code != code or available_at(row) > cutoff_at for row in proofs):
        raise ValueError('coverage reference company/cutoff mismatch')
    records = tuple(row for row in records if row.kind != 'coverage_reference')
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
    proof_records = {row.evidence_id: row for row in (*records, *proofs)
                     if row.code == code and available_at(row) <= cutoff_at}
    referenced = _coverage_reference_closure(proof_records, {key for window in windows for key in window.evidence_ids})
    selected_ids = {row.evidence_id for row in selected}
    extensions.extend(coverage_reference_evidence(proof_records[key]) for key in sorted(referenced - selected_ids))
    extensions.sort(key=lambda row: (row.kind, row.period, available_at(row), row.evidence_id))
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
    return deducted_valuation(snapshot)['pe_deducted_ttm']


def deducted_valuation(snapshot: FundamentalSnapshot) -> dict[str, MetricResult]:
    """Shared frozen TTM/PE calculation for valuation, PEG and earnings yield."""
    rows = snapshot.annuals + snapshot.interims
    latest = max((row.period for row in rows), key=period_order, default=None)
    profit = ttm_amount(rows, 'deducted_net_income', latest) if latest else MetricResult(None, 'missing', 'financial_reports_missing', ())
    if profit.value is not None and profit.value <= 0:
        profit = MetricResult(None, 'missing', 'deducted_profit_not_positive', profit.evidence_ids)
    cap = MetricResult(snapshot.current_mcap, 'verified' if snapshot.current_mcap is not None else 'missing',
                       None if snapshot.current_mcap is not None else 'current_mcap_missing',
                       tuple(row.evidence_id for row in snapshot.extensions if row.kind == 'current_market_cap'))
    if cap.value is not None and cap.value <= 0:
        cap = replace(cap, value=None, status='missing', reason='current_mcap_not_positive')
    return {'deducted_profit_ttm': profit,
            'pe_deducted_ttm': _arithmetic((cap, profit), lambda market_cap, earnings: market_cap / earnings)}


def deducted_peg(*, mcap: Decimal, deducted_ttm: Decimal, growth: Decimal) -> MetricResult:
    """Deducted TTM valuation over capped fractional growth; no reported-PE fallback."""
    values = (mcap, deducted_ttm, growth)
    for name, value in zip(('current_mcap', 'deducted_profit', 'growth'), values):
        if not isinstance(value, Decimal) or not value.is_finite():
            return MetricResult(None, 'untrusted', f'{name}_invalid', ())
        if value <= 0:
            return MetricResult(None, 'missing', f'{name}_not_positive', ())
    return _arithmetic(tuple(MetricResult(value, 'verified', None, ()) for value in values),
                       lambda cap, profit, rate: (cap / profit) / (min(rate, Decimal('.25')) * 100))


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
