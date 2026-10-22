"""Synthetic candidate integration; never certifies production source readiness."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D
import json

import pytest

from discovery.fundamental_types import CoverageWindow, evidence_identity
from discovery.fundamentals import freeze_fundamentals, operating_state, plain, snapshot_payload
from tests.selection_helpers import evidence
from tests.test_discovery_tracks_garp import _company, garp_payload


def candidate(track='garp', payload=None):
    from discovery.rulers import candidate_ruler
    if payload is None:
        from tests.test_discovery_tracks_quality import quality_payload
        from tests.test_discovery_tracks_deep_value import deep_value_payload
        payload = {'garp': garp_payload, 'quality_discount': quality_payload,
                   'deep_value': deep_value_payload}[track]()
    return candidate_ruler(payload, operating_policy={}, band='base')


def company(base=None, *, cap='1000', ebit='20', restricted='0', coverage=True, tax='4'):
    base = base or _company()
    base = replace(base, fields={**base.fields, 'valuation_history': tuple({**row, 'code': base.code} for row in base.fields['valuation_history'])})
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    def row(period, **values):
        item = replace(evidence(period, **values), code=base.code,
                       published_at=at, observed_at=at)
        return replace(item, evidence_id=evidence_identity(item))
    from tests.test_discovery_tracks_quality import _metadata
    annual = []
    rows = []
    for year in range(2020, 2026):
        original = next((item for item in base.fields['financial_history'] if item['period'] == str(year)), None)
        values = {key: value for key, value in (original['values'].items() if original else ()) if value is not None}
        values.update(cash='60', restricted_cash=restricted, interest_debt='20',
                      total_equity_including_minority='80', ebit=ebit, pretax_income='16',
                      income_tax=tax, interest_expense='4', shares='100')
        if original is None:
            values.update(deducted_net_income='20', revenue='100', ocf='30', capex='10')
        rows.append(row(str(year), **values))
        if original:
            old_values = {key: values.get(key) for key in original['values']}
            present = {key: value for key, value in old_values.items() if value is not None}
            annual.append({**original, 'values': old_values,
                           'field_sources': {key: _metadata(key) for key in present},
                           'provenance': {key: ('discovery',) for key in present}})
    base = replace(base, fields={**base.fields, 'financial_history': tuple(annual)})
    # Latest deducted TTM is 20; annual growth remains the independently frozen
    # growth series. Ordinary provider PE remains untouched.
    last_profit = D(rows[-1].values['deducted_net_income'])
    rows.extend((row('2025H1', deducted_net_income=str(last_profit - 10), revenue='50', ocf='15'),
                 row('2026H1', deducted_net_income='10', revenue='50', ocf='15',
                     cash='60', restricted_cash=restricted, interest_debt='20')))
    profile = replace(row('2025'), kind='company_profile', values={'_industry': base.industry})
    profile = replace(profile, evidence_id=evidence_identity(profile))
    receipt = replace(row('2025'), kind='capital_event_coverage')
    receipt = replace(receipt, evidence_id=evidence_identity(receipt))
    quote = replace(row('2025'), kind='current_market_cap', values={
        'current_mcap': cap, '_quote_code': base.code,
        '_market_date': base.as_of['price'], '_required_market_date': base.as_of['price']})
    quote = replace(quote, evidence_id=evidence_identity(quote))
    windows = (CoverageWindow(base.code, 'capital_event', date(2020, 12, 31),
                             date(2025, 12, 31), True, (receipt.evidence_id,)),) if coverage else ()
    snap = freeze_fundamentals((*rows, profile, receipt, quote), (), windows,
                              code=base.code, cutoff_at=base.snapshot_at, current_mcap=D(cap))
    return replace(base, algorithm_version='discovery-v8', fields={**base.fields,
        **snapshot_payload(snap), 'schema_version': 4, 'operating_policy': {},
        'operating_result': plain(operating_state(snap, {}))},
        sources={**base.sources, 'fundamentals': 'checkpoint_payload:point_in_time_evidence'},
        as_of={**base.as_of, 'decision_cutoff_at': snap.cutoff_at.isoformat()})


def test_one_off_income_cannot_lower_deducted_peg():
    from discovery.fundamentals import deducted_peg
    assert deducted_peg(mcap=D('1000'), deducted_ttm=D('20'), growth=D('.20')).value == D('2.5')


@pytest.mark.parametrize('field,value', [('mcap', '0'), ('deducted_ttm', '-1'), ('growth', '0')])
def test_nonpositive_peg_is_explicitly_unavailable(field, value):
    from discovery.fundamentals import deducted_peg
    args = dict(mcap=D('1000'), deducted_ttm=D('20'), growth=D('.2'))
    args[field] = D(value)
    result = deducted_peg(**args)
    assert result.value is None and result.reason and result.status != 'verified'


def test_garp_uses_deducted_value_but_keeps_ordinary_history():
    from discovery.tracks.garp import classify
    from tests.test_discovery_tracks_quality import _valuations
    from tests.test_discovery_tracks_garp import _garp_annual
    from discovery.tracks.quality_discount import decimal_string
    original = _company(valuations=_valuations(latest_pe='10'), annual=tuple(
        _garp_annual(year, deducted_net_income=decimal_string(D('20') * D('1.2') ** (year - 2021)))
        for year in range(2021, 2026)))
    legacy = classify(original, garp_payload())
    result = classify(company(original), candidate())
    assert result.verdict == 'drop'
    assert 'garp.peg_above_max' in result.evidence_rule_ids
    assert 'garp.pe_deducted_ttm_above_max' in result.evidence_rule_ids
    assert result.metrics['pe_deducted_ttm'] == '50'
    assert result.metrics['deducted_peg'] == '2.5'
    assert result.metrics['current_pe'] == '10'
    assert result.metrics['pe_percentile'] == legacy.metrics['pe_percentile']
    assert result.metrics['current_pe'] == legacy.metrics['current_pe']
    assert result.metrics['metric_labels']['pe_percentile'] == '普通 PE 历史位置'


@pytest.mark.parametrize('ebit,passes', [('10', True), ('9.9', False)])

def test_capital_gate_uses_fractional_gross_return(ebit, passes):
    from discovery.tracks.garp import classify
    result = classify(company(cap='200', ebit=ebit, tax='0'), candidate())
    assert ('garp.roic_gross_below_min' not in result.evidence_rule_ids) == passes


def test_missing_event_coverage_never_passes_unchanged_share_gate():
    from discovery.tracks.garp import classify
    result = classify(company(cap='200', coverage=False), candidate())
    assert result.verdict == 'insufficient'
    metric = result.metrics['metric_results']['economic_dilution']
    assert metric['status'] == 'missing'
    assert metric['reason'] == 'capital_actions_incomplete_coverage'


def test_candidate_is_callable_but_cannot_publish(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from discovery.builds import AVAILABLE_BUILDS
    from discovery.rulers import activate
    ruler = candidate()
    assert ruler.algorithm_version == 'discovery-v8'
    assert 'discovery-v8' not in AVAILABLE_BUILDS
    with Session(create_engine('sqlite://')) as session:
        with pytest.raises(ValueError, match='candidate'):
            activate(session, ruler, 'synthetic verification')


def test_candidate_policy_and_registry_are_in_digest():
    from discovery.rulers import payload_digest
    first = candidate()
    second = candidate(payload=garp_payload(cash_conversion_min='1.1'))
    assert payload_digest(first) != payload_digest(second)
    assert first.metric_contract['version'] == 'selection-metrics-v1'
    assert first.ranking_contract['branch_orders']['nonfinancial'][1] == ('deducted_peg', 'asc')


def test_quality_absolute_yield_and_deep_cash_use_frozen_evidence():
    from discovery.tracks.quality_discount import classify as quality
    from discovery.tracks.deep_value import classify as deep
    from tests.test_discovery_tracks_quality import frozen_company
    from tests.test_discovery_tracks_deep_value import _company as deep_company
    q = quality(company(frozen_company(), cap='1000'), candidate('quality_discount'))
    assert q.metrics['earnings_yield'] == '2'
    assert q.metrics['pe_deducted_ttm'] == '50'
    d = deep(company(deep_company(), cap='200'), candidate('deep_value'))
    assert D(d.metrics['normalized_pe']) == D('200') / D('100')
    assert d.metrics['debt_burden'] == '0'
    assert d.metrics['usable_net_cash_to_mcap'] == '0.2'
    assert D(d.metrics['fcf_yield']) == D(d.metrics['normalized_fcf']) / D('200')
    assert d.metrics['metric_results']['economic_dilution']['evidence_ids']
    missing = deep(company(deep_company(), cap='200', restricted=None), candidate('deep_value'))
    assert missing.metrics['debt_burden'] is None
    assert missing.metrics['fcf_yield'] == d.metrics['fcf_yield']
    assert missing.verdict == 'insufficient'


def competition_context(track, companies, verdicts, ruler, extra=()):
    from discovery.snapshot import FrozenRunInputs, canonical_json
    from jobs.discovery import TrackExecutionContext, TrackExecutionCandidate
    frozen = FrozenRunInputs(1, 'synthetic-run', 'data', 'financial', 'valuation',
        {track: 1}, {track: ruler.payload()}, 'discovery-v8', {'SH': ('2026-09-17',)},
        companies[0].snapshot_at, tuple(companies))
    return TrackExecutionContext(2, 1, track, 'competition', 1, 'synthetic-run', 1,
        'discovery-v8', None, 1, 'track_gate', frozen, ruler.payload(),
        tuple(TrackExecutionCandidate(row.code, row.verdict, row.verdict, row.reason,
            canonical_json(row.metrics), None, extra_risk_flags=extra) for row in verdicts))


def test_actual_executor_persists_rank_audit_and_same_card_metrics():
    from discovery.tracks.garp import classify
    from jobs.discovery import builtin_track_executor
    ruler = candidate()
    first = company(cap='200')
    second = company(replace(_company(), code='600001.SH'), cap='220')
    rows = [classify(item, ruler) for item in (first, second)]
    context = competition_context('garp', (first, second), rows, ruler,
                                  ('annual_deducted_drop', 'annual_deducted_drop', 'collection_pressure'))
    outcome = builtin_track_executor(context)
    assert outcome.status == 'waiting_confirmation'
    metrics = {row.code: json.loads(row.metrics_json) for row in outcome.results}
    for code, item in metrics.items():
        assert item['ranking_audit']['risk_state'] == 'unknown'  # No reviewed calendar.
        assert item['ranking_audit']['verified_risk_types'].count('annual_deducted_drop') == 1
        assert 'collection_pressure' not in item['ranking_audit']['verified_risk_types']
        assert item['ranking_audit']['dimensions']['deducted_peg']['value'] == item['deducted_peg']
        cards = [row for row in outcome.evidence if row.code == code]
        assert any('roic_gross_5y' in row.input_fields_json for row in cards)
        assert any(row.rule_id == 'garp.fact.roic_gross_5y' for row in cards)
        assert any('ranking_audit' in row.input_fields_json for row in cards)


def test_registered_contract_rejects_drift_and_v7_json_is_unchanged():
    from discovery.rulers import canonical_payload, garp_ruler
    old = canonical_payload(garp_payload())
    assert json.loads(old).keys() == garp_payload().keys()
    value = json.loads(canonical_payload(candidate()))
    value['metric_contract']['collection_policy']['restricted_cash_ratio_min'] = '0.1'
    with pytest.raises(ValueError, match='contract'):
        garp_ruler(value)
    value = json.loads(canonical_payload(candidate()))
    value['ranking_contract']['branch_orders']['nonfinancial'].append(['current_pe', 'asc'])
    with pytest.raises(ValueError, match='contract'):
        garp_ruler(value)


def test_shadow_sources_and_incomplete_coverage_remain_auditable():
    from discovery.fundamentals import fundamentals_from_payload
    from discovery.tracks.garp import classify
    ruler = candidate()
    for spec in ruler.metric_contract['shadow_metrics'].values():
        assert set(spec['sources']) <= set(ruler.metric_contract['source_fields'])
    item = company(cap='200')
    snapshot = fundamentals_from_payload(item.fields, code=item.code)
    incomplete = replace(snapshot.coverage[0], complete=False)
    snapshot = freeze_fundamentals(snapshot.annuals + snapshot.interims + snapshot.extensions,
                                  snapshot.actions, (incomplete,), code=item.code,
                                  cutoff_at=snapshot.cutoff_at, current_mcap=snapshot.current_mcap)
    item = replace(item, fields={**item.fields, **snapshot_payload(snapshot)})
    result = classify(item, ruler)
    metric = result.metrics['metric_results']['economic_dilution']
    assert result.verdict == 'insufficient'
    assert metric['reason'] == 'capital_actions_incomplete_coverage'
    assert set(incomplete.evidence_ids) <= set(metric['evidence_ids'])


def refreeze(item, transform):
    from discovery.fundamentals import fundamentals_from_payload
    snap = fundamentals_from_payload(item.fields, code=item.code)
    records = []
    for source in snap.annuals + snap.interims + snap.extensions:
        changed = transform(source)
        records.append(replace(changed, evidence_id=evidence_identity(changed)))
    updated = freeze_fundamentals(records, (), snap.coverage, code=item.code,
                                  cutoff_at=snap.cutoff_at, current_mcap=snap.current_mcap)
    return replace(item, fields={**item.fields, **snapshot_payload(updated)})


@pytest.mark.parametrize('change', ['untrusted', 'mismatch', 'basis'])
def test_legacy_metadata_cannot_bypass_schema4_source_eligibility(change):
    from discovery.tracks.garp import classify
    original = company(cap='200')
    def alter(row):
        if row.kind != 'financial' or row.period != '2025':
            return row
        if change == 'untrusted':
            return replace(row, status='untrusted')
        if change == 'basis':
            return replace(row, basis_id='different-consolidation')
        return replace(row, values={**row.values, 'revenue': '1'})
    result = classify(refreeze(original, alter), candidate())
    assert result.verdict == 'insufficient'
    assert result.metrics['revenue_cagr'] is None
    assert result.metrics['legacy_field_eligibility']


def test_bank_cash_is_not_applicable_and_shadow_flags_do_not_rank():
    from discovery.tracks.deep_value import classify
    from tests.test_discovery_tracks_deep_value import _company as deep_company
    from discovery.selection_contract import rank_inputs
    bank = classify(company(deep_company(industry='银行')), candidate('deep_value'))
    assert bank.metrics['ranking_branch'] == 'bank'
    for key in ('debt_burden', 'usable_cash', 'fcf_yield'):
        assert bank.metrics['metric_results'][key]['status'] == 'not_applicable'
        assert bank.metrics[key] is None
    ruler = candidate()
    row = {'ranking_branch': 'nonfinancial', 'roic_gross_5y': '99',
           'metric_results': {'roic_gross_5y': {'status': 'untrusted', 'reason': 'source_untrusted'}}}
    adapted = rank_inputs(row, ruler)
    assert adapted['roic_gross_5y'] is None
    assert adapted['ranking_missing_reasons']['roic_gross_5y'] == 'source_untrusted'


def test_candidate_artifacts_never_claim_production_or_recycle_counts(tmp_path):
    from scripts.calibrate_discovery import calibrate_candidate, main
    for track in ('quality_discount', 'deep_value', 'garp'):
        artifact = calibrate_candidate(track=track)
        assert artifact['algorithm_version'] == 'discovery-v8'
        assert artifact['input_fingerprint'] is None and artifact['sample_count'] is None
        assert artifact['source_readiness'] == 'incomplete'
        assert artifact['production_activation'] == 'inactive_T19_required'
        assert all(value['counts'] is None for value in artifact['hypotheses'].values())
    sampled = calibrate_candidate(track='garp', companies=(company(cap='200'),))
    assert len(sampled['input_fingerprint']) == 64 and sampled['sample_count'] == 1
    assert sampled['source_mode'] == 'synthetic_frozen_schema4'
    output = tmp_path / 'candidate.json'
    assert main(['--candidate-v8', '--track', 'garp', '--output', str(output)]) == 2
    assert json.loads(output.read_text(encoding='utf-8'))['status'] == 'candidate_blocked'
    protected = tmp_path / 'must-not-overwrite.db'
    protected.write_bytes(b'untouched')
    with pytest.raises(SystemExit):
        main(['--candidate-v8', '--track', 'garp', '--db', str(protected), '--output', str(protected)])
    assert protected.read_bytes() == b'untouched'


def test_summary_and_diff_preserve_status_and_shadow_meaning():
    from discovery.summaries import selection_result_summary, _track_threshold
    from discovery.diff import PersistedCompanyResult, selection_metric_changes
    metric = {'value': None, 'status': 'missing', 'reason': 'coverage', 'evidence_ids': []}
    before = {'metric_results': {'economic_dilution': metric}, 'shadow_signals': ['collection_pressure']}
    after = {'metric_results': {'economic_dilution': {**metric, 'status': 'verified', 'value': '0', 'reason': None}}}
    rows = [PersistedCompanyResult('2026-W43', i, 'garp', 'competition', '600000.SH', 'synthetic',
                f'input-{i}', 1, 'pass', None, metrics_json=json.dumps(value, sort_keys=True, separators=(',', ':'))) for i, value in enumerate((before, after), 1)]
    delta = selection_metric_changes(*rows)
    assert delta['metrics']['economic_dilution']['previous']['status'] == 'missing'
    assert delta['metrics']['economic_dilution']['current']['value'] == '0'
    summary = selection_result_summary((before, after))
    assert summary['metric_statuses']['economic_dilution'] == {'missing': 1, 'verified': 1}
    assert summary['risk_states'] == {'unknown': 2}
    assert _track_threshold('roic_gross_min', frozenset({'roic_gross_5y'})) == ('roic_gross_5y', 'gte')

