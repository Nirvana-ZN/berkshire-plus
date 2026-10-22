"""Single v8 metric registry and adapter over already frozen evidence.

This boundary never selects source versions, creates classification evidence,
queries mutable rows, or upgrades provider trust. Legacy gate aliases are
explicit: the canonical metric and its alias always contain the same value.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal, DecimalException
from types import MappingProxyType

from discovery.builds import CANDIDATE_BUILD_VERSION
from discovery.constants import CHINA_MARKET_TIMEZONE
from discovery.field_dictionary import FIELD_DICTIONARY, SELECTION_FIELD_DICTIONARY
from discovery.fundamental_types import MetricResult
from discovery.fundamentals import (
    _arithmetic, _fact, _facts_calculation, _on_basis, deducted_peg, deducted_valuation, fundamentals_from_payload,
    period_order, plain,
)
from discovery.metrics import median


@dataclass(frozen=True)
class SelectionMetric:
    sources: tuple[str, ...]
    window: str
    formula: str
    label: str
    tracks: tuple[str, ...]
    alias: str | None = None
    gate: str | None = None
    missing_policy: str = 'insufficient'


_ALL = ('quality_discount', 'deep_value', 'garp')
_VALUATION = ('quality_discount', 'garp')
_NORMALIZED = ('quality_discount', 'deep_value')
METRIC_REGISTRY = MappingProxyType({
    'economic_dilution': SelectionMetric(('shares',), 'configured_dilution_endpoints_and_covered_actions',
        'end_shares/(start_shares*structural_factors)-1', '经济稀释率', _ALL, 'dilution_rate', 'dilution'),
    'gross_issuance_rate': SelectionMetric(('shares',), 'configured_dilution_endpoints_and_covered_actions',
        'capital_change_audit.gross_issuance_rate', '毛发行率', _ALL, missing_policy='shadow'),
    'cancellation_rate': SelectionMetric(('shares',), 'configured_dilution_endpoints_and_covered_actions',
        'capital_change_audit.cancellation_rate', '注销率', _ALL, missing_policy='shadow'),
    'bridge_error': SelectionMetric(('shares',), 'configured_dilution_endpoints_and_covered_actions',
        'capital_change_audit.max_absolute_share_error', '股本衔接差额（股）', _ALL, missing_policy='shadow'),
    'pe_deducted_ttm': SelectionMetric(('deducted_net_income',), 'latest_comparable_ttm_and_same_day_cap',
        'current_mcap/deducted_profit_ttm', '当前扣非 PE', _VALUATION, gate='valuation'),
    'deducted_peg': SelectionMetric(('deducted_net_income',), 'latest_comparable_ttm_and_configured_growth',
        '(current_mcap/deducted_profit_ttm)/(min(deducted_profit_cagr,0.25)*100)',
        '扣非 PEG', _VALUATION, 'peg', 'peg'),
    'earnings_yield': SelectionMetric(('deducted_net_income',), 'latest_comparable_ttm_and_same_day_cap',
        '100*deducted_profit_ttm/current_mcap', '扣非盈利收益率（百分数）', ('quality_discount',), gate='absolute_valuation'),
    'normalized_earnings': SelectionMetric(('deducted_net_income',), 'five_consecutive_annuals',
        'median(exact_five_annual.deducted_net_income)', '五年正常化扣非盈利', _NORMALIZED, gate='normalized_earnings'),
    'normalized_pe': SelectionMetric(('deducted_net_income',), 'five_consecutive_annuals_and_same_day_cap',
        'current_mcap/normalized_earnings', '正常化 PE', _NORMALIZED, gate='normalized_valuation'),
    'normalized_fcf': SelectionMetric(('ocf', 'capex'), 'five_consecutive_annuals',
        'median(exact_five_annual.ocf-exact_five_annual.capex)', '五年正常化 FCF', ('deep_value',), gate='net_debt'),
    'cumulative_fcf': SelectionMetric(('ocf', 'capex'), 'five_consecutive_annuals',
        'sum(exact_five_annual.ocf-exact_five_annual.capex)', '五年累计 FCF', ('deep_value',), gate='positive_cumulative_fcf'),
    'usable_cash': SelectionMetric(('cash', 'restricted_cash'), 'latest_comparable_balance',
        'cash-restricted_cash', '可用现金', ('deep_value',)),
    'debt_burden': SelectionMetric(('cash', 'restricted_cash', 'interest_debt', 'ocf', 'capex'),
        'latest_balance_and_five_annual_fcf', 'max(interest_debt-usable_cash,0)/normalized_fcf',
        '正债务负担', ('deep_value',), 'net_debt_to_normalized_fcf', 'net_debt'),
    'usable_net_cash_to_mcap': SelectionMetric(('cash', 'restricted_cash', 'interest_debt'),
        'latest_balance_and_same_day_cap', 'max(usable_cash-interest_debt,0)/current_mcap',
        '可用净现金/市值', ('deep_value',)),
    'fcf_yield': SelectionMetric(('ocf', 'capex'), 'five_annual_fcf_and_same_day_cap',
        'normalized_fcf/current_mcap', '正常化 FCF 收益率', ('deep_value',)),
    'roic_gross_5y': SelectionMetric(('ebit', 'pretax_income', 'income_tax', 'interest_expense',
        'total_equity_including_minority', 'interest_debt'), 'five_annual_nopats_six_ic_endpoints',
        'mean(verified_EBIT*(1-effective_tax_rate)/mean(IC_begin,IC_end))',
        '五年总投入资本回报（小数）', ('garp',), 'return_on_capital', 'return_on_capital'),
})
COLLECTION_POLICY = MappingProxyType({'version': 'collection-v1',
    'receivables_ratio_increase_min': '0.05', 'restricted_cash_ratio_min': '0.20'})
SHADOW_SOURCES = MappingProxyType({
    'receivables_ratio': ('accounts_receivable', 'contract_assets', 'revenue'),
    'prior_receivables_ratio': ('accounts_receivable', 'contract_assets', 'revenue'),
    'receivables_ratio_change': ('accounts_receivable', 'contract_assets', 'revenue'),
    'cash_conversion': ('ocf', 'net_income'), 'prior_cash_conversion': ('ocf', 'net_income'),
    'cash_conversion_change': ('ocf', 'net_income'),
    'inventory_ratio': ('inventory', 'revenue'), 'prior_inventory_ratio': ('inventory', 'revenue'),
    'inventory_ratio_change': ('inventory', 'revenue'), 'restricted_cash_ratio': ('restricted_cash', 'cash'),
})
_SCHEMA4 = frozenset({'annual_evidence', 'interim_history', 'capital_events',
    'fundamental_extensions', 'coverage', 'evidence_ids', 'current_mcap',
    'fundamental_fingerprint', 'decision_cutoff_at', 'operating_policy', 'operating_result', 'schema_version'})


def is_candidate(ruler):
    return ruler.algorithm_version == CANDIDATE_BUILD_VERSION


def registered_metrics(track):
    return {name: spec for name, spec in METRIC_REGISTRY.items() if track in spec.tracks}


def metric_contract(track):
    specs = registered_metrics(track)
    source_fields = set(field for spec in specs.values() for field in spec.sources)
    source_fields.update(SELECTION_FIELD_DICTIONARY)
    source_fields.update(field for sources in SHADOW_SOURCES.values() for field in sources)
    return {'version': 'selection-metrics-v1',
        'metrics': {name: asdict(spec) for name, spec in specs.items()},
        'source_fields': {field: {'registry': 'selection_evidence' if field in SELECTION_FIELD_DICTIONARY else 'legacy_financial',
                                  **asdict((SELECTION_FIELD_DICTIONARY if field in SELECTION_FIELD_DICTIONARY else FIELD_DICTIONARY)[field])}
                          for field in sorted(source_fields)},
        'collection_policy': dict(COLLECTION_POLICY), 'collection_effect': 'shadow_only',
        'shadow_metrics': {name: {'sources': list(sources), 'window': 'latest_and_prior_comparable_ttm',
                                 'formula': f'collection_metrics.{name}', 'missing_policy': 'shadow', 'gate': None}
            for name, sources in SHADOW_SOURCES.items()},
        'source_mode': 'schema4_frozen_evidence', 'current_mcap': 'verified_same_company_market_day_quote',
        'classification': 'frozen_company_classification',
        'ordinary_pe_history_label': '普通 PE 历史位置',
        'normalized_sensitivity_years': [7, 10]}


def ranking_contract(track):
    orders = {
        'quality_discount': {
            'nonfinancial': [('pe_percentile', 'asc'), ('roe_downside_volatility', 'asc'), ('cash_conversion', 'desc')],
            'cyclical': [('pb_percentile', 'asc'), ('normalized_pe', 'asc'), ('cash_conversion', 'desc')],
            'bank': [('pb_percentile', 'asc'), ('roe_mean', 'desc'), ('bank_core_tier1_capital_adequacy', 'desc')]},
        'deep_value': {
            'nonfinancial': [('normalized_pe', 'asc'), ('debt_burden', 'asc'), ('fcf_yield', 'desc'), ('usable_net_cash_to_mcap', 'desc')],
            'bank': [('pb_percentile', 'asc'), ('roe_mean', 'desc'), ('npl_change', 'asc')]},
        'garp': {'nonfinancial': [('deducted_profit_consistent_growth_periods', 'desc'), ('deducted_peg', 'asc'),
                                ('roic_gross_5y', 'desc'), ('cash_conversion', 'desc')]},
    }[track]
    # Existing explicitly calculated annual signals remain distinct from interim state.
    watch = ('margin_deterioration', 'cash_debt_double_high', 'dilution_rate_worsening',
             'revenue_decline_worsening', 'earnings_decline_worsening', 'npl_change_worsening',
             'revenue_growth_slowdown_worsening', 'deducted_profit_growth_slowdown_worsening',
             'interest_debt_increase_to_revenue_worsening', 'goodwill_increase_to_revenue_worsening',
             'acquisition_cash_to_revenue_worsening', 'gross_margin_change_deterioration',
             'net_margin_change_deterioration', 'annual_deducted_drop', 'thin_equity_high_pb',
             'goodwill_bloat', 'recent_ocf_collapse', 'consecutive_negative_fcf',
             'negative_5y_fcf', 'roe_step_down_3y')
    return {'version': 'selection-ranking-v1', 'branch_orders': {**orders, 'unknown': []},
        'risk_policy': {'operating_states': {'improving': 'clear', 'stable': 'clear', 'unknown': 'unknown',
                                            'deteriorating': 'deteriorating'},
                        'verified_risk_types': dict.fromkeys(watch, 'watch')},
        'industry_cap': 2, 'total_cap': 15,
        'annual_risk_policy': {'version': 'annual-risk-v1', 'pb_min': '8', 'roe_mean_min': '50',
            'goodwill_to_equity_min': '0.25', 'recent_cash_conversion_max': '0.4',
            'deducted_drop_max': '-0.15', 'roe_decline_min': '4',
            'negative_fcf_years': 2, 'fcf_window_years': 5, 'fcf_min_observations': 3,
            'roe_window_years': 4}}



def input_schema_matches(company, ruler, fields, sources, as_of):
    if not is_candidate(ruler):
        return set(company.fields) == fields and set(company.sources) == sources and set(company.as_of) == as_of
    return (set(company.fields) == fields | _SCHEMA4
            and set(company.sources) == sources | {'fundamentals'}
            and set(company.as_of) == as_of | {'decision_cutoff_at'}
            and company.fields['schema_version'] == 4
            and company.sources['fundamentals'] == 'checkpoint_payload:point_in_time_evidence')


class SelectionInputError(ValueError):
    """The candidate lacks the exact frozen evidence/policy contract."""


def load_fundamentals(company, ruler):
    try:
        snapshot = fundamentals_from_payload(company.fields, code=company.code)
    except (KeyError, TypeError, ValueError) as error:
        raise SelectionInputError('frozen selection evidence invalid') from error
    if (snapshot.cutoff_at != company.snapshot_at
            or datetime.fromisoformat(company.as_of['decision_cutoff_at']) != snapshot.cutoff_at
            or plain(company.fields['operating_policy']) != plain(ruler.operating_policy)):
        raise SelectionInputError('frozen selection policy/cutoff mismatch')
    from discovery.fundamentals import _classification, operating_state
    if plain(operating_state(snapshot, ruler.operating_policy)) != plain(company.fields['operating_result']):
        raise SelectionInputError('frozen operating result mismatch')
    classification = _classification(snapshot)
    if classification.status in {'missing', 'untrusted'}:
        raise SelectionInputError(classification.reason)
    trusted_labels = {row.values.get('_industry') for row in snapshot.extensions
                      if row.kind == 'company_classification' and row.status == 'verified'}
    if trusted_labels != {company.industry}:
        raise SelectionInputError('frozen classification label mismatch')
    return snapshot


def eligible_legacy_company(company, snapshot):
    """Restrict retained legacy cells; never synthesize trusted source metadata.

    Both the legacy loader's existing provenance rules and a matching verified
    schema4 fact are required. A cross-year/balance basis conflict is unavailable.
    The projection is evaluation-only; the immutable company is never rewritten.
    """
    try:
        return _eligible_legacy_company(company, snapshot)
    except (KeyError, TypeError, ValueError, DecimalException) as error:
        raise SelectionInputError('legacy evidence eligibility invalid') from error


def _eligible_legacy_company(company, snapshot):
    history, audit, bases = [], {}, set()
    for row in company.fields['financial_history']:
        values, statuses = {}, {}
        for field, raw in row['values'].items():
            if raw is None:
                values[field] = None
                continue
            result, basis = _fact(snapshot.annuals, field, row['period'])
            if result.status == 'verified' and result.value != Decimal(raw):
                result = replace(result, value=None, status='untrusted', reason='legacy_evidence_value_mismatch')
            values[field] = raw if result.status == 'verified' else None
            if result.status == 'verified':
                bases.add(basis)
            statuses[field] = plain(result)
        audit[row['period']] = {
            'verified_evidence_ids': sorted({key for item in statuses.values() if item['status'] == 'verified' for key in item['evidence_ids']}),
            'unavailable': {key: item for key, item in statuses.items() if item['status'] != 'verified'},
            'retained_fields': sorted(key for key, item in statuses.items() if item['status'] == 'verified')}
        present = {key for key, value in values.items() if value is not None}
        history.append({**row, 'values': values,
                        'provenance': {key: value for key, value in row['provenance'].items() if key in present},
                        'field_sources': {key: value for key, value in row['field_sources'].items() if key in present}})
    if len(bases) > 1:
        for row in history:
            row.update(values=dict.fromkeys(row['values']), provenance={}, field_sources={})
        audit['basis_conflict'] = 'incomparable_basis'
    return replace(company, fields={**company.fields, 'financial_history': tuple(history)}), audit


def _cap(snapshot, company):
    quotes = [row for row in snapshot.extensions if row.kind == 'current_market_cap']
    ids = tuple(row.evidence_id for row in quotes)
    if len(quotes) != 1:
        return MetricResult(None, 'missing' if not quotes else 'untrusted', 'current_mcap_evidence_unavailable', ids)
    quote = quotes[0]
    if quote.status != 'verified':
        return MetricResult(None, quote.status, quote.values.get('_reason') or 'current_mcap_untrusted', ids)
    if (quote.values.get('_quote_code') != company.code
            or quote.values.get('_market_date') != company.as_of['price']
            or quote.values.get('_required_market_date') != company.as_of['price']):
        return MetricResult(None, 'untrusted', 'current_mcap_code_or_day_mismatch', ids)
    value = snapshot.current_mcap
    quoted_value = quote.values.get('current_mcap')
    if value is None or value <= 0 or quoted_value is None or value != Decimal(quoted_value):
        return MetricResult(None, 'untrusted', 'current_mcap_invalid', ids)
    return MetricResult(value, 'verified', None, ids)


def annual_values(snapshot, end_year, *, years=5):
    """One exact normalization, reused by traps, cash metrics and valuation."""
    periods = tuple(str(year) for year in range(end_year - years + 1, end_year + 1))
    rows = snapshot.annuals
    earnings = _facts_calculation(rows, [('deducted_net_income', period) for period in periods],
                                 lambda *values: median(values))
    fcfs = tuple(_facts_calculation(rows, [('ocf', period), ('capex', period)], lambda a, b: a - b)
                 for period in periods)
    normalized = _arithmetic(fcfs, lambda *values: median(values))
    cumulative = _arithmetic(fcfs, lambda *values: sum(values, Decimal(0)))
    # Cross-year basis compatibility is required as well as same-year subtraction.
    requirements = [(field, period) for period in periods for field in ('ocf', 'capex')]
    return {'normalized_earnings': earnings, 'normalized_fcf': _on_basis(rows, requirements, normalized),
            'cumulative_fcf': _on_basis(rows, requirements, cumulative)}


def _dilution(snapshot, window):
    from discovery.capital_actions import capital_change_audit
    if window is None:
        return {name: MetricResult(None, 'missing', 'capital_shares_missing', ())
                for name in ('economic_dilution', 'gross_issuance_rate', 'cancellation_rate', 'bridge_error')}
    start, end = date(window.start_year, 12, 31), date(window.end_year, 12, 31)
    endpoints = [_fact(snapshot.annuals, 'shares', str(year))[0] for year in (window.start_year, window.end_year)]
    records = {row.evidence_id: row for row in snapshot.extensions + snapshot.annuals + snapshot.interims}
    assertions = [row for row in snapshot.coverage if row.kind == 'capital_event'
                  and row.start <= end and row.end >= start]
    coverage = [row for row in assertions if row.complete
                and row.start <= start and row.end >= end and row.evidence_ids
                and all(key in records and records[key].status == 'verified' for key in row.evidence_ids)]
    actions = tuple(action for action in snapshot.actions
                    if start < action.effective_at.astimezone(CHINA_MARKET_TIMEZONE).date() <= end)
    results = capital_change_audit(*(item.value for item in endpoints), actions, coverage_complete=bool(coverage))
    ids = {key for item in endpoints for key in item.evidence_ids} | {key for row in assertions for key in row.evidence_ids}
    unavailable = next((item for item in endpoints if item.status == 'untrusted'), None)
    requirements = [('shares', str(year)) for year in (window.start_year, window.end_year)]
    return {name: replace(unavailable if unavailable else _on_basis(snapshot.annuals, requirements, result),
                          evidence_ids=tuple(sorted(ids | set(result.evidence_ids)))) for name, result in results.items()}


def apply_metrics(company, ruler, branch, windows, calculated, counts, audit, *, snapshot, annual=None):
    """Replace gate inputs before classification; return only frozen plain audit."""
    from discovery.tracks.garp import frozen_fundamental_diagnostics
    from discovery.tracks.deep_value import cash_metrics_for_branch
    applicable = branch not in {'bank', 'insurance', 'other_financial', 'financial', 'unknown'}
    results = _dilution(snapshot, windows.get('dilution')) if applicable else {
        key: MetricResult(None, 'not_applicable', 'financial_branch', ())
        for key in ('economic_dilution', 'gross_issuance_rate', 'cancellation_rate', 'bridge_error')}
    cap = _cap(snapshot, company)
    rows = snapshot.annuals + snapshot.interims
    latest = max((row.period for row in rows), key=period_order, default=None)
    if ruler.track in _VALUATION:
        valuation = deducted_valuation(snapshot)
        profit = valuation['deducted_profit_ttm']
        pe = _arithmetic((cap, valuation['pe_deducted_ttm']), lambda _, value: value)
        growth = calculated.get('deducted_profit_cagr')
        if ruler.track == 'garp':
            calculated['capped_growth'] = min(growth, Decimal('.25')) if growth is not None and growth > 0 else None
        growth_window = windows.get('growth')
        growth_ids = tuple(sorted({key for year in (range(growth_window.start_year, growth_window.end_year + 1)
                                                   if growth_window else ())
                                   for key in _fact(snapshot.annuals, 'deducted_net_income', str(year))[0].evidence_ids}))
        growth_result = MetricResult(growth, 'verified' if growth is not None else 'missing',
                                    None if growth is not None else 'deducted_growth_missing', growth_ids)
        peg = _arithmetic((cap, profit, growth_result), lambda *_: Decimal(0))
        if peg.status == 'verified':
            peg = replace(deducted_peg(mcap=cap.value, deducted_ttm=profit.value, growth=growth), evidence_ids=peg.evidence_ids)
        results.update(pe_deducted_ttm=pe, deducted_peg=peg)
        if ruler.track == 'quality_discount':
            results['earnings_yield'] = _arithmetic((cap, profit), lambda value, earnings: 100 * earnings / value)
    if annual is not None and applicable:
        results.update({key: value for key, value in annual.items() if key in registered_metrics(ruler.track)})
        earnings = annual['normalized_earnings']
        if earnings.value is not None and earnings.value <= 0:
            earnings = replace(earnings, value=None, status='missing', reason='normalized_earnings_not_positive')
        results['normalized_pe'] = _arithmetic((cap, earnings), lambda value, earnings: value / earnings)
    if ruler.track == 'deep_value':
        def fact(field):
            return _fact(rows, field, latest)[0] if latest else MetricResult(None, 'missing', 'financial_reports_missing', ())
        inputs = {'cash': fact('cash'), 'restricted_cash': fact('restricted_cash'),
                  'debt': fact('interest_debt'), 'mcap': cap,
                  'fcf': (annual or {}).get('normalized_fcf', MetricResult(None, 'missing', 'fcf_missing', ()))}
        cash = cash_metrics_for_branch(branch='bank' if branch == 'financial' else branch,
                                      **{key: value.value for key, value in inputs.items()})
        dependencies = {'usable_cash': ('cash', 'restricted_cash'),
                        'debt_burden': ('cash', 'restricted_cash', 'debt', 'fcf'),
                        'usable_net_cash_to_mcap': ('cash', 'restricted_cash', 'debt', 'mcap'),
                        'fcf_yield': ('fcf', 'mcap')}
        for key, result in cash.items():
            needed = tuple(inputs[name] for name in dependencies[key])
            check = _arithmetic(needed, lambda *_: Decimal(0))
            results[key] = (replace(check, value=None) if check.status != 'verified' and applicable else
                            replace(result, evidence_ids=check.evidence_ids))
            if applicable:
                needed_fields = {'cash': 'cash', 'restricted_cash': 'restricted_cash', 'debt': 'interest_debt'}
                requirements = [(needed_fields[name], latest) for name in dependencies[key] if name in needed_fields]
                if 'fcf' in dependencies[key] and annual is not None:
                    end = windows['fcf'].end_year
                    requirements += [(field, str(year)) for year in range(end - 4, end + 1) for field in ('ocf', 'capex')]
                results[key] = _on_basis(rows, requirements, results[key])
    diagnostics = frozen_fundamental_diagnostics(snapshot, ruler.metric_contract['collection_policy'])
    if ruler.track == 'garp':
        results['roic_gross_5y'] = diagnostics['roic_gross_5y']
        audit['return_metric_label'] = 'roic_gross_5y'
    for name, spec in registered_metrics(ruler.track).items():
        result = results.get(name, MetricResult(None, 'not_applicable', 'branch_not_applicable', ()))
        results[name] = result
        calculated[name] = result.value
        if spec.alias:
            calculated[spec.alias] = result.value
    if applicable:
        counts['dilution'] = ruler.min_observations['dilution'] if results['economic_dilution'].status == 'verified' else 0
        if ruler.track == 'deep_value':
            counts['net_debt'] = ruler.min_observations['net_debt'] if results['debt_burden'].status == 'verified' else 0
    state = company.fields['operating_result']
    audit.update(metric_results=plain(results), collection_metrics=plain(diagnostics['collection_metrics']),
        shadow_signals=diagnostics['shadow_signals'], collection_policy=plain(ruler.metric_contract['collection_policy']),
        operating_result=state, operating_policy=plain(ruler.operating_policy), operating_state=state['state'],
        freeze_new_positions=state['state'] in {'unknown', 'deteriorating'},
        fundamental_fingerprint=snapshot.fingerprint,
        ranking_branch='bank' if branch == 'financial' else branch,
        metric_labels={'pe_percentile': '普通 PE 历史位置', 'current_pe': '当前普通 PE',
                       **{name: spec.label for name, spec in registered_metrics(ruler.track).items()}})
    if annual is not None and applicable:
        end = windows['normalized_earnings'].end_year
        audit['normalization_sensitivity'] = {str(years): plain(annual_values(snapshot, end, years=years))
                                             for years in ruler.metric_contract['normalized_sensitivity_years']}


def rank_inputs(metrics, ruler):
    """Only this adapter converts persisted decimal text for the strict ranker."""
    result = dict(metrics)
    branch = result['ranking_branch']
    reasons = {}
    records = metrics.get('metric_results', {})
    for field, _ in ruler.ranking_contract['branch_orders'][branch]:
        item = records.get(field)
        value = metrics.get(field)
        if isinstance(item, Mapping) and item.get('status') != 'verified':
            value = None
            reasons[field] = item.get('reason') or item['status']
        if value is not None:
            if not isinstance(value, (str, Decimal)):
                raise ValueError('frozen rank metric must be decimal text')
            value = Decimal(value)
            if not value.is_finite():
                raise ValueError('frozen rank metric must be finite')
            if isinstance(item, Mapping) and value != Decimal(item['value']):
                raise ValueError('frozen rank metric differs from its evidence result')
        result[field] = value
    result['ranking_missing_reasons'] = reasons
    return result
