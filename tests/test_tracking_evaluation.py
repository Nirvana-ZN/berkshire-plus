from datetime import date
import json

import pytest

from tests.test_tracking_ledger import manifest, market


def test_pending_maturity_and_missing_benchmarks_stay_unavailable():
    from tracking.evaluation import compare_cohorts
    from tracking.ledger import simulate_cohort
    cohort = manifest()
    cohort.update(track='garp', algorithm_version='v8', registered_at='2026-01-29T00:00:00+00:00',
                  variant_id='full_selected', registration_hash='synthetic-group')
    from tracking.contracts import digest
    cohort['manifest_hash'] = digest({k: v for k, v in cohort.items() if k != 'manifest_hash'})
    result = simulate_cohort(cohort, market(), date(2026, 2, 4))
    report = compare_cohorts([{'cohort_id': 1, 'manifest': cohort, 'result': result}])
    row = report['groups'][0]['variants']['full_selected']['observations'][0]
    assert row['horizon_return'] is None
    assert row['benchmarks']['primary']['relative_return'] is None
    assert report['inference'] == 'descriptive_only_overlapping_samples'


def test_registration_month_uses_shanghai_business_calendar():
    from tracking.evaluation import compare_cohorts
    from tracking.ledger import simulate_cohort
    from tracking.contracts import digest
    cohort = manifest()
    cohort.update(track='garp', algorithm_version='discovery-v8', variant_id='full_selected',
                  registered_at='2026-01-31T16:30:00+00:00')
    cohort['manifest_hash'] = digest({k: v for k, v in cohort.items() if k != 'manifest_hash'})
    result = simulate_cohort(cohort, market(), date(2026, 2, 4))
    report = compare_cohorts([{'cohort_id': 1, 'manifest': cohort, 'result': result}])
    assert {row['registration_month'] for row in report['groups']} == {'2026-02'}


@pytest.fixture
def retained_real_audit(tmp_path, monkeypatch):
    from jobs.tracking import refresh_tracking
    from tracking.evaluation import export_frozen_input, export_tracking_audit
    from tracking.importer import history_events, RetainedEventProvider, validate_history_bundle
    from tracking.cohorts import cohort_manifest
    from tests.test_tracking_job import tracking_store
    from tests.test_tracking_import import history_fixture, write_json
    engine, factory, ids = tracking_store(tmp_path, monkeypatch)
    with factory() as session:
        original = cohort_manifest(session, ids[0])
        frozen = export_frozen_input(session, original['run_id'])
    def delay(events):
        from tests.test_tracking_ledger import event
        for row in events:
            if row['type'] == 'bar' and row['effective_at'].startswith('2025-12-19'):
                row['open_limit_down'] = True
        events.append(event('dividend_ex', '2025-10-01', '600003.SH', cash_per_share='1',
            pay_date='2026-01-02', availability_basis='retained_archive_v1',
            archived_available_at='2025-10-01T08:00:00+00:00', observed_at='2026-10-20T00:00:00+00:00'))
    end = date(2025, 12, 24)
    bundle, _, _ = history_fixture(tmp_path, start=date(2025, 9, 17), end=end,
        codes=tuple(f'60000{i}.SH' for i in range(1, 6)), frozen_input=frozen, change_events=delay)
    bundle['decision_at'] = original['decision_cutoff_at']
    assert validate_history_bundle(bundle) == ()
    assert refresh_tracking(factory, RetainedEventProvider(history_events(bundle)), as_of=end)['status'] == 'completed'
    with factory() as session:
        audit = export_tracking_audit(session, ids, bundle=bundle, as_of=end, horizon_months=3)
    engine.dispose()
    return audit


def seal_tracking_report(root, audit):
    from tests.test_tracking_import import write_json
    from tracking.contracts import digest
    artifacts = {'tracking_audit': write_json(root, 'tracking-audit.json', audit),
                 'proposal': write_json(root, 'proposal.json', {'origin': 'independent_outcomes',
                    'change': 'review complete retained loss and return comparison'})}
    first = audit['observations'][0]['manifest']
    bindings = {'proposal_sha256': artifacts['proposal']['sha256'],
        'input_fingerprint': first['input_hash'], 'ruler_payload_hash': first['ruler_hash'],
        'protocol_hash': first['protocol_hash'], 'registration_hash': first['registration_hash']}
    report = {'schema_version': 2, 'adapter': 'paper-tracking-audit-v1',
        'proposal_kind': 'validated_experiment', 'source': 'independent_outcomes',
        'author': 'synthetic audit producer', 'generated_at': '2026-10-21T12:00:00+00:00',
        'training_end': '2025-09-16T00:00:00+00:00', 'release_reason': 'Review actual retained experiment',
        'bindings': bindings, 'artifacts': artifacts}
    report_ref = write_json(root, 'validation-report.json', report)
    review = write_json(root, 'validation-review.json', {
        'origin': 'human_review', 'reviewer': 'independent experiment reviewer',
        'reviewed_at': '2026-10-21T13:00:00+00:00', 'report_sha256': report_ref['sha256'],
        'decision': 'eligible_for_release_review', 'independence_basis': 'Independent of production/extraction',
        'provenance_assessment': 'Assessed immutable original publication and retained later sources'})
    return {'report_path': report_ref['path'], 'report_sha256': report_ref['sha256'],
            'review_path': review['path'], 'review_sha256': review['sha256'], **bindings}


def test_actual_registration_ledger_export_reaches_t13_without_rewriting_protocol(tmp_path, retained_real_audit, capsys):
    from scripts.evolve_rulers import activation_eligibility
    audit = retained_real_audit
    observation = audit['observations'][0]
    manifest = observation['manifest']
    metrics = observation['result']['horizons']['3']
    assert metrics['expected_exit_date'] == '2025-12-19'
    assert metrics['actual_exit_date'] == metrics['terminal_date'] == '2025-12-22'
    assert manifest['protocol']['benchmarks']['primary']['series_status'] == 'not_observed'
    assert metrics['benchmarks']['primary']['series_revisions'] == ['fixture-v1']
    evidence = seal_tracking_report(tmp_path, audit)
    assert evidence['input_fingerprint'] != json.loads((tmp_path / 'validation-report.json').read_text(encoding='utf-8'))['artifacts']['tracking_audit']['sha256']
    assert activation_eligibility(proposal_kind='validated_experiment', evidence=evidence) == (True, None)
    from scripts.track_selection import main
    from tests.test_tracking_import import write_json
    bundle = audit['history_bundle']
    bundle['cohort_bindings'] = {str(row['cohort_id']): row['manifest']['manifest_hash'] for row in audit['observations']}
    reference = write_json(tmp_path, 'complete-history.json', bundle)
    db = tmp_path / 'tracking.db'
    before = db.read_bytes()
    assert main(['--db', str(db), '--import-history', reference['path'], '--dry-run']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'history_qualified_for_engine'
    assert db.read_bytes() == before
    assert main(['--db', str(db), '--import-history', reference['path'], '--apply']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'completed'


@pytest.mark.parametrize('mutation', ['timing', 'input', 'model_in_ablation', 'model_elsewhere', 'missing_source', 'relative', 'omitted_source_event'])
def test_actual_audit_rejects_rebound_timing_input_source_and_model_labels(tmp_path, retained_real_audit, mutation):
    from scripts.evolve_rulers import activation_eligibility
    from tracking.contracts import digest
    audit = retained_real_audit
    if mutation == 'timing':
        audit['observations'][0]['result']['horizons']['3']['terminal_date'] = '2025-12-19'
    elif mutation == 'input':
        audit['frozen_input']['companies'][0]['payload']['fields']['amount'] = '999'
    elif mutation == 'missing_source':
        audit['events'][0].pop('retained_source')
    elif mutation == 'relative':
        audit['observations'][0]['result']['horizons']['3']['benchmarks']['primary']['relative_return'] = '99'
    elif mutation == 'omitted_source_event':
        from tracking.importer import normalized_event
        from tracking.ledger import simulate_cohort
        removed = next(row['event_id'] for row in audit['events'] if row['type'] == 'dividend_ex')
        audit['events'] = [row for row in audit['events'] if row['event_id'] != removed]
        remaining = {row['event_id']: normalized_event(row) for row in audit['events']}
        for entry in audit['observations']:
            entry['input_event_ids'] = [key for key in entry['input_event_ids'] if key != removed]
            entry['result'] = simulate_cohort(entry['manifest'], [remaining[key] for key in entry['input_event_ids']],
                                              date.fromisoformat(audit['as_of']))
    else:
        obs = next(row for row in audit['observations'] if row['manifest']['variant_id'] == 'no_operating_selected')
        manifest = obs['manifest']
        if mutation == 'model_in_ablation':
            next(iter(manifest['ablation_audit'].values()))['origin'] = 'llm'
        else:
            manifest['retained_note'] = {'verdict': 'pass'}
        manifest['manifest_hash'] = digest({k: v for k, v in manifest.items() if k != 'manifest_hash'})
        obs['result']['manifest_hash'] = manifest['manifest_hash']
    evidence = seal_tracking_report(tmp_path, audit)
    eligible, reason = activation_eligibility(proposal_kind='validated_experiment', evidence=evidence)
    assert eligible is False
    if mutation.startswith('model'):
        assert reason == 'independent_validation_required'


def test_mixed_revision_projection_withholds_relative_return():
    from tracking.evaluation import qualified_benchmarks
    metrics = {'benchmarks': {'primary': {'status': 'available', 'return': '0.1',
        'relative_return': '0.2', 'series_revisions': ['v1', 'v2']}}}
    projected = qualified_benchmarks(metrics)['primary']
    assert projected['status'] == 'benchmark_revision_compatibility_unavailable'
    assert projected['relative_return'] is projected['return'] is None
    assert metrics['benchmarks']['primary']['relative_return'] == '0.2'


def test_t13_refuses_mixed_revisions_even_with_fully_rebound_sources(tmp_path, retained_real_audit):
    from scripts.evolve_rulers import activation_eligibility
    from tests.test_tracking_import import rebind_bundle
    from tracking.importer import history_events, normalized_event
    from tracking.ledger import simulate_cohort
    audit = retained_real_audit
    bundle = audit['history_bundle']
    grouped = {name: json.loads(open(bundle[name]['path'], encoding='utf-8').read())['records']
        for name in ('universe_intervals', 'report_versions', 'trading_calendar', 'market_events', 'benchmark_series')}
    for row in grouped['benchmark_series']:
        if row['code'] == 'CSI300_TOTAL_RETURN' and row['effective_at'].startswith('2025-12-22'):
            row['series_revision'] = 'fixture-v2'
    rebind_bundle(tmp_path, bundle, grouped)
    replacements = {row['event_id']: row for row in history_events(bundle)}
    audit['events'] = [replacements[row['event_id']] for row in audit['events']]
    for entry in audit['observations']:
        entry['result'] = simulate_cohort(entry['manifest'],
            [normalized_event(replacements[key]) for key in entry['input_event_ids']], date.fromisoformat(audit['as_of']))
    evidence = seal_tracking_report(tmp_path, audit)
    assert activation_eligibility(proposal_kind='validated_experiment', evidence=evidence) == (
        False, 'benchmark_revision_compatibility_unavailable')
