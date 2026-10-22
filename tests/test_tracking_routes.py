"""Public contract, recovery, retained refresh and read-only projections on temp DBs."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.main import create_app
from app.selection_models import StrategyObservation, TrackingRequest
from market.quotes import FakeQuoteClient
from tracking.contracts import canonical_json, digest


def test_tracking_starts_with_empty_collection_and_gets_do_not_write(tmp_path):
    app = isolated_app(tmp_path)
    with TestClient(app) as client:
        response = client.get('/api/tracking/cohorts')
        assert response.status_code == 200
        assert response.json()['items'] == []
        assert client.get('/api/tracking/cohorts/999').status_code == 404
        assert client.get('/api/tracking/experiments/999').status_code == 404
        assert client.get('/api/tracking/cohorts?horizon=7').status_code == 400


def test_refresh_validates_and_durably_replays_unavailable_provider(tmp_path):
    app = isolated_app(tmp_path)
    with TestClient(app) as client:
        assert client.post('/api/tracking/refresh', json={}).status_code == 400
        headers = {'Idempotency-Key': 'refresh-once'}
        assert client.post('/api/tracking/refresh', json={'as_of': '2000-01-01'}, headers=headers).status_code == 400
        first = client.post('/api/tracking/refresh', json={}, headers=headers)
        assert first.status_code == 503
        assert first.json()['code'] == 'evidence_unavailable'
    with TestClient(app) as client:
        replay = client.post('/api/tracking/refresh', json={}, headers=headers)
        assert (replay.status_code, replay.json()) == (first.status_code, first.json())
        changed = client.post('/api/tracking/cohorts', json={'run_id': 1, 'track': 'garp',
            'variant': 'full_selected', 'config_id': 'paper-v1'}, headers=headers)
        assert changed.status_code == 409

def isolated_app(tmp_path, **kwargs):
    yaml = tmp_path/'yaml'
    yaml.mkdir(exist_ok=True)
    for name, body in {'trades': '成交: []\n', 'cash': '余额: 0\n',
                       'rules': '规则: []\n', 'symbols': '映射: {}\n'}.items():
        (yaml/f'{name}.yaml').write_text(body, encoding='utf-8')
    return create_app(db_path=tmp_path/'test.db', env_path=tmp_path/'.env', yaml_dir=yaml,
        reports_dir=tmp_path/'reports', quote_client=FakeQuoteClient(), **kwargs)


def test_registration_default_config_strict_body_and_semantic_replay(tmp_path):
    from tests.test_tracking_cohorts import persisted_run
    from app.discovery_models import DiscoveryStage
    app = isolated_app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = persisted_run(session)
            for row in session.scalars(select(DiscoveryStage).where(DiscoveryStage.stage == 'competition')):
                row.status = 'confirmed'
            session.commit()
            identity = run.id
        assert client.get('/api/tracking/cohorts').json()['available_config_ids'] == ['paper-v1']
        payload = {'run_id': identity, 'track': 'garp', 'variant': 'full_selected', 'config_id': 'paper-v1'}
        headers = {'Idempotency-Key': 'register1'}
        first = client.post('/api/tracking/cohorts', json=payload, headers=headers)
        assert first.status_code == 200, first.text
        assert client.post('/api/tracking/cohorts', json=dict(reversed(list(payload.items()))), headers=headers).json() == first.json()
        assert client.post('/api/tracking/cohorts', json={**payload, 'variant': 'all_passed'}, headers=headers).status_code == 409
        result = client.get(f"/api/tracking/cohorts/{first.json()['cohort_id']}").json()
        assert result['config_id'] == f"experiment:{result['experiment_id']}"
        roundtrip = client.post('/api/tracking/cohorts', json={**payload, 'config_id': result['config_id']},
            headers={'Idempotency-Key': 'config-roundtrip'})
        assert roundtrip.status_code == 200 and roundtrip.json() == first.json()
        assert result['kind'] == 'retrospective_observation'
        assert result['published_run_at'] is None
        assert result['horizons'] == []
        assert result['members']
        experiment = client.get(f"/api/tracking/experiments/{result['experiment_id']}").json()
        assert experiment['kind'] == result['kind']
        assert experiment['config_id'] == result['config_id']
        for invalid in ({**payload, 'track': []}, {**payload, 'run_id': True}, {**payload, 'run_id': 2**64},
                        {**payload, 'config_id': 'arbitrary'}, {**payload, 'published_at': '2020-01-01'}):
            assert client.post('/api/tracking/cohorts', json=invalid, headers={'Idempotency-Key': 'bad'}).status_code == 400
        assert client.get('/api/tracking/cohorts/wrong').status_code == 400
        assert client.get('/api/tracking/cohorts/' + '9'*5000).status_code == 400
        assert client.get('/api/tracking/experiments/0').status_code == 400
        unavailable = client.post('/api/tracking/cohorts', json={**payload, 'variant': 'no_operating_selected'},
            headers={'Idempotency-Key': 'missing-receipt'})
        assert unavailable.status_code == 409


def test_pending_recovery_freezes_server_date_scope_and_fences_late_owner(tmp_path, monkeypatch):
    import app.tracking_routes as routes
    instant = datetime(2026, 10, 22, 15, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(routes, '_now', lambda: instant)
    app = isolated_app(tmp_path, tracking_clock=lambda: datetime(2026, 10, 24, tzinfo=timezone.utc))
    with TestClient(app) as client:
        factory = app.state.session_factory
        fingerprint = digest({'operation': 'refresh', 'body': {}})
        claim, rejection = routes._claim(factory, 'recover', fingerprint, date(2026, 10, 22))
        assert rejection is None
        original_token, original_scope, original_date = claim
        assert original_scope == []
        from tests.test_tracking_cohorts import persisted_run, manifest_for
        from app.discovery_models import DiscoveryStage
        with factory() as session:
            run = persisted_run(session)
            for stage in session.scalars(select(DiscoveryStage).where(DiscoveryStage.stage == 'competition')):
                stage.status = 'confirmed'
            manifest_for(session, run.id)
            session.commit()
        assert client.post('/api/tracking/refresh', json={}, headers={'Idempotency-Key': 'recover'}).status_code == 409
        instant += timedelta(seconds=121)
        calls = []
        def refresh(factory, provider, *, as_of, cohort_ids):
            calls.append((as_of, cohort_ids))
            return {'status': 'completed', 'updated_cohorts': [], 'errors': []}
        monkeypatch.setattr(routes, 'refresh_tracking', refresh)
        response = client.post('/api/tracking/refresh', json={}, headers={'Idempotency-Key': 'recover'})
        assert response.status_code == 200
        assert calls == [(original_date, original_scope)]
        assert routes._finish(factory, 'recover', original_token, 200, {'late': True}).status_code == 409
        with factory() as session:
            row = session.get(TrackingRequest, 'recover')
            assert row.as_of == original_date and json.loads(row.scope_json) == []
            with pytest.raises(IntegrityError):
                session.execute(text("UPDATE tracking_requests SET response_json='{}' WHERE key='recover'"))
            session.rollback()
        assert client.post('/api/tracking/refresh', json={}, headers={'Idempotency-Key': 'recover'}).json() == response.json()
        assert len(calls) == 1


def test_public_projection_real_refresh_qualified_benchmarks_and_get_no_writes(tmp_path, monkeypatch):
    from tests.test_tracking_job import tracking_store, retained_market, counts
    from tracking.importer import RetainedEventProvider
    engine, factory, ids = tracking_store(tmp_path, monkeypatch)
    app = create_app(db_path=tmp_path/'tracking.db', env_path=tmp_path/'.env',
        quote_client=FakeQuoteClient(), tracking_provider=RetainedEventProvider(retained_market(tmp_path)),
        tracking_clock=lambda: datetime(2025, 9, 24, 8, tzinfo=timezone.utc))
    with TestClient(app) as client:
        response = client.post('/api/tracking/refresh', json={}, headers={'Idempotency-Key': 'real-refresh'})
        assert response.status_code == 200, response.text
        before = counts(factory)
        assert before[1] == 9
        result = client.get('/api/tracking/cohorts?track=garp&horizon=3').json()
        assert len(result['items']) == 3
        for row in result['items']:
            assert row['kind'] == 'prospective'
            assert row['horizons'][0]['status'] == 'pending_maturity'
            assert row['horizons'][0]['horizon_return'] is None
        assert counts(factory) == before
        rendered = json.dumps(result)
        for private in ('retained_source', 'payload_json', 'result_metadata', 'source_document_path', str(tmp_path)):
            assert private not in rendered
        with factory.begin() as session:
            old = session.scalar(select(StrategyObservation).where(StrategyObservation.cohort_id == ids[0],
                StrategyObservation.horizon_months == 3))
            metrics = json.loads(old.metrics_json)
            metrics['horizon']['benchmarks']['primary'].update(status='available',
                series_revisions=['r1', 'r2'], private_source_path=str(tmp_path/'private-proof'), relative_return='0.5')
            metrics['horizon']['benchmarks']['primary']['return'] = '0.1'
            session.add(StrategyObservation(cohort_id=ids[0], horizon_months=3, as_of=date(2025, 9, 25),
                state='pending_maturity', metrics_json=canonical_json(metrics), event_digest='fixture-revision',
                calculation_version='fixture-revision'))
        row = client.get(f'/api/tracking/cohorts/{ids[0]}').json()['horizons'][0]
        assert row['benchmarks']['primary']['status'] == 'benchmark_revision_compatibility_unavailable'
        assert row['benchmarks']['primary']['return'] is None
        assert row['benchmarks']['primary']['relative_return'] is None
        assert 'private_source_path' not in json.dumps(client.get('/api/tracking/cohorts').json())
    engine.dispose()


def test_discovery_projects_persisted_nested_audit_and_separates_weekly_causes(tmp_path):
    from app.discovery_routes import _selection_partition, _selection_change_labels
    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from tests.test_tracking_cohorts import persisted_run
    from types import SimpleNamespace
    app = isolated_app(tmp_path)
    with TestClient(app):
        with app.state.session_factory() as session:
            run = persisted_run(session)
            stage = session.scalar(select(DiscoveryStage).where(DiscoveryStage.run_id == run.id,
                DiscoveryStage.track == 'garp', DiscoveryStage.stage == 'competition'))
            row = session.scalar(select(DiscoveryResult).where(DiscoveryResult.run_id == run.id,
                DiscoveryResult.track == 'garp', DiscoveryResult.stage == 'competition', DiscoveryResult.code == '600003.SH'))
            stored = {'operating_state': 'unknown', 'ranking_audit': {'rank_score': '0', 'risk_state': 'unknown',
                'risk_evidence_missing': ['operating_state'], 'dimensions': {'peg': {'value': None, 'contribution': '0'}}},
                'metric_results': {'deducted_peg': {'status': 'missing', 'reason': 'source_missing'}}}
            row.metrics_json = canonical_json(stored)
            session.flush()
            result = _selection_partition(session, run, stage, [row], {row.code: 'Fixture'}, {})['pass'][0]
            assert result['selection_audit']['ranking_audit'] == stored['ranking_audit']
            assert result['selection_audit']['long_term_eligibility'] == '通过'
            assert result['selection_audit']['evidence_at']
            assert result['selection_audit']['missing'] == ['deducted_peg: source_missing']
            run.algorithm_version = 'legacy-fixture'
            legacy = _selection_partition(session, run, stage, [row], {row.code: 'Fixture'}, {})['pass'][0]
            assert legacy['selection_audit'] is None
    before = {'metric_results': {'normalized_earnings': {'value': '1'}, 'normalized_pe': {'value': '10'}}, 'operating_state': 'stable'}
    after = {'metric_results': {'normalized_earnings': {'value': '2'}, 'normalized_pe': {'value': '11'}}, 'operating_state': 'stable'}
    change = SimpleNamespace(cause='rule_changed', current_reason='industry_cap')
    assert _selection_change_labels(before, after, change) == ['fundamental_changed', 'valuation_changed', 'rule_changed', 'cap_excluded']


def test_registration_respects_global_lock_and_new_key_retries_failure(tmp_path):
    from tests.test_tracking_cohorts import persisted_run
    from app.discovery_models import DiscoveryStage
    from app.selection_models import StrategyCohort
    from jobs.lock import acquire_tracking, release, JOB_TYPE_TRACKING
    app = isolated_app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = persisted_run(session)
            for row in session.scalars(select(DiscoveryStage).where(DiscoveryStage.stage == 'competition')):
                row.status = 'confirmed'
            identity = run.id
            lease = acquire_tracking(session)
            session.commit()
        payload = {'run_id': identity, 'track': 'garp', 'variant': 'full_selected', 'config_id': 'paper-v1'}
        assert client.post('/api/tracking/cohorts', json=payload, headers={'Idempotency-Key': 'busy'}).status_code == 409
        with app.state.session_factory.begin() as session:
            assert not list(session.scalars(select(StrategyCohort)))
            assert release(session, 0, JOB_TYPE_TRACKING, owner_token=lease)
        # The failed receipt is immutable; a new key retries the operation.
        assert client.post('/api/tracking/cohorts', json=payload, headers={'Idempotency-Key': 'busy'}).status_code == 409
        assert client.post('/api/tracking/cohorts', json=payload, headers={'Idempotency-Key': 'retry'}).status_code == 200


def test_request_guards_repair_and_pending_renewal_preserve_identity(tmp_path, monkeypatch):
    import app.tracking_routes as routes
    from app.db import init_db
    instant = datetime(2026, 10, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(routes, '_now', lambda: instant)
    app = isolated_app(tmp_path)
    with TestClient(app):
        factory = app.state.session_factory
        claim, _ = routes._claim(factory, 'guards', digest({'body': {}}), instant.date())
        token = claim[0]
        instant += timedelta(seconds=30)
        assert routes._renew_claim(factory, 'guards', token)
        assert not routes._renew_claim(factory, 'guards', 'wrong-token')
        for statement in ("UPDATE tracking_requests SET scope_json='[99]' WHERE key='guards'",
                          "DELETE FROM tracking_requests WHERE key='guards'",
                          "INSERT OR REPLACE INTO tracking_requests SELECT * FROM tracking_requests WHERE key='guards'"):
            with factory() as session:
                with pytest.raises(IntegrityError):
                    session.execute(text(statement))
        with factory.begin() as session:
            session.execute(text('DROP TRIGGER tracking_request_identity'))
        repaired = init_db(tmp_path/'test.db')
        repaired.dispose()
        with factory() as session:
            with pytest.raises(IntegrityError):
                session.execute(text("UPDATE tracking_requests SET fingerprint='other' WHERE key='guards'"))
        instant += timedelta(seconds=121)
        assert not routes._renew_claim(factory, 'guards', token)
        assert routes._finish(factory, 'guards', token, 200, {}).status_code == 409


def test_weekly_bank_proxy_changes_remain_distinct_from_fundamentals():
    from app.discovery_routes import _selection_change_labels
    from types import SimpleNamespace
    change = SimpleNamespace(cause='data_changed', current_reason=None)
    assert _selection_change_labels({'pb_percentile': '10', 'roe_mean': '12'},
        {'pb_percentile': '20', 'roe_mean': '12'}, change) == ['valuation_changed']
    assert _selection_change_labels({'pb_percentile': '10', 'roe_mean': '12'},
        {'pb_percentile': '10', 'roe_mean': '13'}, change) == ['fundamental_changed']
