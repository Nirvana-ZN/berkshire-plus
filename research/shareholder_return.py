"""Actual shareholder cash and cancellation ledgers from frozen evidence.

Normalized execution uses kind/stage/plan_id, source-defined (window_start,
window_end], accumulation_scope=window|itemized|plan_to_date and actual
effective_at (aware) or effective_date (explicit Shanghai business date).
Itemized records require trade_id. Plan-to-date amounts are retained but never
converted into annual cash by subtraction or by announcement year. All amounts
are CNY and shares are shares. Pure numeric denominators are caller-supplied;
the research adapter owns their period compatibility and frozen provenance.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation

from discovery.fundamental_types import CoverageWindow, EvidenceRecord, MetricResult
from discovery.evidence_store import available_at, select_evidence_as_of
from discovery.fundamentals import CLASSIFICATION_VERSION, _arithmetic, verified_coverage_records
from market.corporate_actions import latest_progress, shareholder_effective_date, shareholder_stage
from market.evidence import SHAREHOLDER_COVERAGE_SCOPES


CONTRACT_VERSION = 'shareholder-return-v1'
_LEDGERS = {'dividend_paid': ('dividend', 'paid', 'amount'),
            'buyback_executed': ('buyback', 'executed', 'amount'),
            'buyback_cancelled_shares': ('buyback', 'cancelled', 'shares'),
            'equity_financing': ('equity_financing', 'executed', 'amount')}


def _ids(rows):
    return tuple(sorted({row.evidence_id for row in rows}))


def _missing(reason, ids=(), *, status='missing'):
    return MetricResult(None, status, reason, tuple(sorted(set(ids))))


def reporting_window(coverage: Sequence[CoverageWindow]) -> tuple[date, date] | None:
    """Newest explicitly complete reporting interval; never backfill a revocation.

    Each ledger must independently prove this exact interval. An incomplete
    assertion alone supplies neither a window total nor a confirmed zero.
    """
    windows = [(row.start, row.end) for row in coverage
               if row.complete and row.kind in SHAREHOLDER_COVERAGE_SCOPES.values()]
    return max(windows, key=lambda item: (item[1], item[0])) if windows else None


def calendar_year(window: tuple[date, date] | None) -> int | None:
    if window is None:
        return None
    start, end = window
    return end.year if end == date(end.year, 12, 31) and start == date(end.year - 1, 12, 31) else None


def _amount(row, field):
    value = row.values.get(field)
    if row.status != 'verified':
        return _missing(f'execution_{row.status}', (row.evidence_id,), status=row.status)
    if value is None:
        return _missing(f'execution_{field}_missing', (row.evidence_id,))
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError('invalid amount')
    except (InvalidOperation, ValueError):
        return _missing(f'execution_{field}_invalid', (row.evidence_id,), status='untrusted')
    return MetricResult(number, 'verified', None, (row.evidence_id,))


def _sum(values, ids=()):
    total = _arithmetic(tuple(values), lambda *numbers: sum(numbers, Decimal(0)))
    return replace(total, evidence_ids=tuple(sorted(set(total.evidence_ids) | set(ids))))


def _ledger(records, coverage, window, kind, stage, field):
    raw = tuple(row for row in records if shareholder_stage(row) == (kind, stage))
    unavailable_status = 'untrusted' if any(row.status == 'untrusted' for row in raw) else 'missing'
    if window is None:
        return _missing('execution_window_missing', _ids(raw), status=unavailable_status), ()
    start, end = window
    matching = tuple(row for row in coverage if row.kind == f'shareholder_{kind}_{stage}'
                     and (row.start, row.end) == window)
    proof_ids, valid = set(), []
    for receipt in matching:
        verified, ids = verified_coverage_records(records, receipt)
        proof_ids.update(ids)
        if receipt.complete and verified:
            valid.append(set(ids))
    if not valid:
        return _missing('execution_coverage_missing', set(_ids(raw)) | proof_ids, status=unavailable_status), ()
    # Use authoritative versions before selecting dates: a withdrawn payment
    # must not survive in a separate stage bucket.
    try:
        candidates = tuple(row for row in records if row.kind == kind or kind == 'buyback' and row.kind == 'buyback_trade')
        current = (() if not candidates else select_evidence_as_of(candidates, code=candidates[0].code,
                   cutoff=max(map(available_at, candidates))))
        in_window = tuple(row for row in current if shareholder_stage(row) == (kind, stage)
                          and start < shareholder_effective_date(row) <= end)
        rows = latest_progress(in_window)
    except ValueError as exc:
        return _missing(str(exc), set(_ids(raw)) | proof_ids, status='untrusted'), ()
    if any(row.evidence_id not in ids for ids in valid for row in rows):
        return _missing('execution_outside_coverage_evidence', set(_ids(rows)) | proof_ids, status='untrusted'), ()
    plans = {}
    for row in rows:
        plans.setdefault(row.values.get('plan_id'), []).append(row)
    for plan in plans.values():
        scopes = {row.values.get('accumulation_scope') for row in plan}
        if len(scopes) > 1:
            return _missing('mixed_execution_representations', set(_ids(rows)) | proof_ids, status='untrusted'), ()
        if scopes == {'plan_to_date'}:
            return _missing('plan_to_date_not_reporting_window', set(_ids(rows)) | proof_ids), ()
        if scopes not in ({'window'}, {'itemized'}):
            return _missing('execution_accumulation_scope_missing', set(_ids(rows)) | proof_ids), ()
        if scopes == {'window'} and len(plan) != 1:
            return _missing('overlapping_cumulative_scopes', set(_ids(rows)) | proof_ids, status='untrusted'), ()
        if any((row.values.get('window_start'), row.values.get('window_end')) !=
               (start.isoformat(), end.isoformat()) for row in plan):
            return _missing('execution_scope_window_mismatch', set(_ids(rows)) | proof_ids, status='untrusted'), ()
    return _sum((_amount(row, field) for row in rows), proof_ids), rows


def _planned(records, kind):
    rows = tuple(row for row in records if row.kind == kind and row.values.get('stage')
                 in {'proposed', 'approved', 'implemented', 'withdrawn', 'revoked'})
    try:
        selected = latest_progress(rows)
    except ValueError as exc:
        return _missing(str(exc), _ids(rows), status='untrusted')
    # Proposal/approval/implementation are statuses of the same plan; amounts
    # are known announced limits and are never returned as executed cash.
    plans = {}
    for row in selected:
        key = (row.values.get('plan_id'), row.period, row.values.get('window_start'), row.values.get('window_end'))
        if key not in plans or (row.published_at, row.evidence_id) > (plans[key].published_at, plans[key].evidence_id):
            plans[key] = row
    if not plans:
        return _missing('announced_plan_missing', _ids(rows))
    result = _sum((_amount(row, 'amount') for row in plans.values()), _ids(rows))
    return replace(result, reason='known_announced_limits_only') if result.value is not None else result


def _denominator(value, name):
    if value is None:
        return _missing(f'{name}_missing')
    if not isinstance(value, Decimal) or not value.is_finite():
        return _missing(f'{name}_invalid', status='untrusted')
    if value <= 0:
        return _missing(f'{name}_not_positive')
    return MetricResult(value, 'verified', None, ())


def _fcf_applicability(records):
    rows = tuple(row for row in records if row.kind == 'company_classification')
    trusted = tuple(row for row in rows if row.status == 'verified')
    if not trusted:
        status = 'untrusted' if any(row.status == 'untrusted' for row in rows) else 'missing'
        return _missing(f'industry_classification_{status}', _ids(rows), status=status)
    branches = {row.values.get('_industry_branch') for row in trusted}
    if len(branches) != 1 or any(row.values.get('_classification_version') != CLASSIFICATION_VERSION for row in trusted):
        return _missing('industry_classification_invalid', _ids(rows), status='untrusted')
    if branches <= {'bank', 'insurance', 'other_financial'}:
        return _missing('industrial_fcf_not_applicable', _ids(trusted), status='not_applicable')
    if branches != {'nonfinancial'}:
        return _missing('industry_branch_unknown', _ids(trusted))
    return MetricResult(Decimal(1), 'verified', None, _ids(trusted))


def shareholder_metrics(records: Sequence[EvidenceRecord], *, mcap: Decimal | None,
                        fcf: Decimal | None, deducted_profit: Decimal | None,
                        coverage: Sequence[CoverageWindow] = ()) -> dict[str, MetricResult]:
    """Complete-window execution totals, ratios and separately known plans.

    Ratios use finite positive exact denominators. Annual payout denominators
    must describe the selected calendar cash year (integration enforces it).
    Current mcap is an as-of yield denominator, never an annualization rule.
    """
    codes = {row.code for row in records} | {row.code for row in coverage}
    if len(codes) > 1:
        raise ValueError('shareholder metrics require one company')
    # Wrappers may certify original references but are not action evidence.
    active = tuple(row for row in records if row.kind != 'coverage_reference')
    window = reporting_window(coverage)
    metrics, selected = {}, {}
    for key, (kind, stage, field) in _LEDGERS.items():
        metrics[key], selected[key] = _ledger(records, coverage, window, kind, stage, field)
    for kind in ('dividend', 'buyback', 'equity_financing'):
        metrics[kind + '_planned'] = _planned(active, kind)
    buyback = metrics['buyback_executed']
    if buyback.value is not None:
        rows = selected['buyback_executed']
        if any(row.values.get('purpose') not in {'incentive', 'cancellation', 'other'} for row in rows):
            metrics['buyback_incentive'] = _missing('buyback_purpose_missing', buyback.evidence_ids)
        else:
            metrics['buyback_incentive'] = _sum((_amount(row, 'amount') for row in rows
                if row.values.get('purpose') == 'incentive'), buyback.evidence_ids)
    else:
        metrics['buyback_incentive'] = buyback
    metrics['buyback_net_of_equity_financing'] = _arithmetic(
        (buyback, metrics['equity_financing']), lambda buy, issue: buy - issue)
    cancelled = metrics['buyback_cancelled_shares']
    rows = selected['buyback_cancelled_shares']
    # Ratios from different denominators cannot be added. A single reviewed
    # cancellation ledger supplies its explicitly compatible pre-event shares.
    if cancelled.value is not None and len(rows) == 1:
        base = _amount(rows[0], 'shares_before')
        if base.value is not None and base.value > 0 and cancelled.value <= base.value:
            metrics['cancellation_ratio'] = _arithmetic((cancelled, base), lambda shares, total: shares / total)
        else:
            metrics['cancellation_ratio'] = _missing('cancellation_share_denominator_invalid', cancelled.evidence_ids,
                status=base.status if base.status != 'verified' else 'untrusted')
    else:
        metrics['cancellation_ratio'] = _missing('cancellation_share_denominator_missing', cancelled.evidence_ids)
    cash = metrics['dividend_paid']
    cap = _denominator(mcap, 'current_mcap')
    metrics['dividend_asof_yield'] = _arithmetic((cash, cap), lambda paid, market_cap: paid / market_cap)
    metrics['buyback_asof_yield'] = _arithmetic((buyback, cap), lambda paid, market_cap: paid / market_cap)
    applicability = _fcf_applicability(active)
    if applicability.status != 'verified':
        metrics['dividend_fcf_ratio'] = replace(applicability,
            evidence_ids=tuple(sorted(set(applicability.evidence_ids) | set(cash.evidence_ids))))
    elif calendar_year(window) is None:
        metrics['dividend_fcf_ratio'] = _missing('annual_denominator_window_mismatch', cash.evidence_ids)
    else:
        metrics['dividend_fcf_ratio'] = _arithmetic((cash, _denominator(fcf, 'fcf'), applicability),
            lambda paid, free_cash, _: paid / free_cash)
    metrics['dividend_deducted_profit_ratio'] = (_missing('annual_denominator_window_mismatch', cash.evidence_ids)
        if calendar_year(window) is None else _arithmetic((cash, _denominator(deducted_profit, 'deducted_profit')),
            lambda paid, profit: paid / profit))
    year = calendar_year(window)
    if year is None:
        stability = _missing('three_complete_cash_years_missing')
    else:
        years = [_ledger(records, coverage, (date(y - 1, 12, 31), date(y, 12, 31)),
                          'dividend', 'paid', 'amount')[0] for y in range(year - 2, year + 1)]
        stability = _arithmetic(years, lambda *amounts: Decimal(sum(value > 0 for value in amounts)) / Decimal(3))
    metrics['dividend_three_year_paid_frequency'] = stability
    return metrics
