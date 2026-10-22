from datetime import datetime, timedelta, timezone
from datetime import date
import json
import threading

import pytest
from sqlalchemy import event as sql_event, select, func

from tests.test_tracking_import import seal_sources
from tests.test_tracking_ledger import market


def tracking_store(tmp_path, monkeypatch):
    from app.db import init_db, create_session_factory
    from app.discovery_models import DiscoveryStage
    from tracking.cohorts import publish_track_cohorts
    from tests import test_tracking_cohorts as fixture
    from dataclasses import replace
    published = datetime(2025, 9, 17, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(fixture, 'SNAPSHOT_AT', published - timedelta(hours=1))
    original_freeze = fixture.freeze_inputs
    def historical_freeze(session, run, companies, versions, dates, **kwargs):
        def shifted(mapping):
            return {key: value.replace('2026-09', '2025-09') if isinstance(value, str) else value
                    for key, value in mapping.items()}
        companies = [replace(item, as_of=shifted(item.as_of), security_status=shifted(item.security_status))
                     for item in companies]
        return original_freeze(session, run, companies, versions,
            {key: [value.replace('2026-09', '2025-09') for value in days] for key, days in dates.items()}, **kwargs)
    monkeypatch.setattr(fixture, 'freeze_inputs', historical_freeze)
    engine = init_db(tmp_path / 'tracking.db')
    factory = create_session_factory(engine)
    with factory() as session:
        run = fixture.persisted_run(session)
        stage = session.scalar(select(DiscoveryStage).where(
            DiscoveryStage.run_id == run.id, DiscoveryStage.track == 'garp',
            DiscoveryStage.stage == 'competition'))
        ids = publish_track_cohorts(session, stage_id=stage.id, published_at=published)
        session.commit()
    return engine, factory, ids


def retained_market(tmp_path, end=date(2025, 9, 24)):
    records = market(end, codes=tuple(f'60000{i}.SH' for i in range(1, 6)), start=date(2025, 9, 17))
    return seal_sources(tmp_path, {'market_events': records})[2]


def counts(factory):
    from app.selection_models import StrategyMarketEvent, StrategyObservation, StrategyLedgerEntry
    with factory() as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
                     for model in (StrategyMarketEvent, StrategyObservation, StrategyLedgerEntry))


def test_tracking_lease_recovers_expiry_and_rejects_stale_owner(tmp_path):
    from app.db import init_db, create_session_factory
    from jobs.lock import acquire_tracking, renew_tracking, release, JOB_TYPE_TRACKING
    engine = init_db(tmp_path / 'lease.db')
    factory = create_session_factory(engine)
    now = datetime(2026, 2, 4, tzinfo=timezone.utc)
    with factory.begin() as session:
        first = acquire_tracking(session, now=now)
    with factory.begin() as session:
        assert acquire_tracking(session, now=now + timedelta(seconds=1)) is None
        second = acquire_tracking(session, now=now + timedelta(seconds=121))
        assert second and second != first
    with factory.begin() as session:
        assert not renew_tracking(session, first, now=now + timedelta(seconds=122))
        assert not release(session, 0, JOB_TYPE_TRACKING, owner_token=first)
        assert renew_tracking(session, second, now=now + timedelta(seconds=122))
    engine.dispose()


def test_refresh_fetches_outside_transactions_and_replays_idempotently(tmp_path, monkeypatch):
    from jobs.tracking import refresh_tracking
    engine, factory, ids = tracking_store(tmp_path, monkeypatch)
    records = retained_market(tmp_path)
    active = []
    sql_event.listen(engine, 'begin', lambda conn: active.append(conn))
    sql_event.listen(engine, 'commit', lambda conn: active.remove(conn))
    sql_event.listen(engine, 'rollback', lambda conn: active.remove(conn) if conn in active else None)
    class Provider:
        def fetch_events(self, codes, start, end):
            assert not active
            assert {'CSI300_TOTAL_RETURN', 'CSI500_TOTAL_RETURN'} <= set(codes)
            return tuple(row for row in records if row['code'] in {*codes, 'MARKET'})
    assert refresh_tracking(factory, Provider(), as_of=date(2025, 9, 24))['status'] == 'completed'
    before = counts(factory)
    assert before[0] and before[1] == 9 and before[2]
    assert refresh_tracking(factory, Provider(), as_of=date(2025, 9, 24))['status'] == 'completed'
    assert counts(factory) == before
    engine.dispose()


def test_network_failure_retains_prior_observations_and_resume(tmp_path, monkeypatch):
    from jobs.tracking import refresh_tracking
    from tracking.importer import RetainedEventProvider
    engine, factory, ids = tracking_store(tmp_path, monkeypatch)
    records = retained_market(tmp_path)
    good = RetainedEventProvider(records)
    refresh_tracking(factory, good, as_of=date(2025, 9, 24))
    before = counts(factory)
    class Broken:
        def fetch_events(self, codes, start, end):
            raise ConnectionError('synthetic network interruption')
    result = refresh_tracking(factory, Broken(), as_of=date(2025, 9, 25))
    assert result['status'] == 'failed'
    assert counts(factory) == before
    assert refresh_tracking(factory, good, as_of=date(2025, 9, 24))['status'] == 'completed'
    assert counts(factory) == before
    engine.dispose()


def test_provider_deadline_releases_lease_and_ignores_late_result(tmp_path, monkeypatch):
    from jobs import tracking
    from jobs.lock import lock_status
    engine, factory, ids = tracking_store(tmp_path, monkeypatch)
    gate = threading.Event()
    finished = threading.Event()
    clock = [0.0]
    monkeypatch.setattr(tracking, '_monotonic', lambda: clock[0])
    def wait(signal, seconds):
        clock[0] += seconds
        return False
    monkeypatch.setattr(tracking, '_wait_event', wait)
    class Stuck:
        def fetch_events(self, codes, start, end):
            gate.wait()
            finished.set()
            return ()
    result = tracking.refresh_tracking(factory, Stuck(), as_of=date(2025, 9, 24))
    assert result['status'] == 'failed' and result['errors'][0]['reason'] == 'provider_deadline_exceeded'
    assert clock[0] <= tracking.PROVIDER_DEADLINE_SECONDS
    with factory() as session:
        assert lock_status(session) is None
    gate.set()
    assert finished.wait(1)
    assert counts(factory) == (0, 0, 0)
    engine.dispose()


def test_startup_preserves_unexpired_tracking_lease(tmp_path):
    from app.db import init_db, create_session_factory
    from jobs.lock import acquire_tracking, clear_stale_lock, lock_status
    engine = init_db(tmp_path / 'startup.db')
    factory = create_session_factory(engine)
    now = datetime(2026, 2, 4, tzinfo=timezone.utc)
    with factory.begin() as session:
        assert acquire_tracking(session, now=now)
    with factory() as session:
        clear_stale_lock(session, now_provider=lambda: now + timedelta(seconds=30))
        assert lock_status(session) == {'type': 'tracking', 'job_id': 0}
        clear_stale_lock(session, now_provider=lambda: now + timedelta(seconds=121))
        assert lock_status(session) is None
    engine.dispose()


def test_cli_dry_run_is_read_only_and_refresh_requires_retained_provider(tmp_path, monkeypatch, capsys):
    from scripts.track_selection import main
    engine, factory, ids = tracking_store(tmp_path, monkeypatch)
    engine.dispose()
    db = tmp_path / 'tracking.db'
    before = db.read_bytes()
    assert main(['--db', str(db), '--register-run', '1', '--dry-run']) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['dry_run'] is True and preview['applied'] is False
    assert db.read_bytes() == before
    assert main(['--db', str(db), '--report', '--format', 'json']) == 0
    capsys.readouterr()
    assert db.read_bytes() == before
    missing = tmp_path / 'absent.db'
    assert main(['--db', str(missing), '--refresh', '--as-of', '2025-09-24']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'provider_required'
    assert not missing.exists()


def test_cli_local_retained_refresh_and_default_history_dry_run(tmp_path, monkeypatch, capsys):
    from scripts.track_selection import main
    from tests.test_tracking_import import write_json
    engine, factory, ids = tracking_store(tmp_path, monkeypatch)
    records = retained_market(tmp_path)
    feed = write_json(tmp_path, 'events.json', {'events': list(records)})
    engine.dispose()
    db = tmp_path / 'tracking.db'
    assert main(['--db', str(db), '--refresh', '--as-of', '2025-09-24', '--events', feed['path']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'completed'
    before = db.read_bytes()
    bundle = write_json(tmp_path, 'incomplete-history.json', {'schema_version': 1, 'point_in_time_verified': True})
    assert main(['--db', str(db), '--import-history', bundle['path']]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['dry_run'] is True and 'historical_evidence_unavailable' in report['errors']
    assert db.read_bytes() == before


@pytest.mark.parametrize('mismatch', ['late_report', 'run', 'input', 'universe', 'later_target_run'])
def test_history_import_binds_every_original_cohort_before_preview_or_apply(tmp_path, monkeypatch, capsys, mismatch):
    from scripts import track_selection
    from tests.test_tracking_import import history_fixture, rebind_bundle, write_json
    from tracking.cohorts import cohort_manifest, register_cohort
    from tracking.contracts import default_tracking_config, digest
    from tracking.evaluation import export_frozen_input
    from tracking.importer import validate_history_bundle
    engine, factory, ids = tracking_store(tmp_path, monkeypatch)
    with factory() as session:
        original = cohort_manifest(session, ids[0])
        frozen = export_frozen_input(session, original['run_id'])
        bindings = {str(identity): cohort_manifest(session, identity)['manifest_hash'] for identity in ids}
        if mismatch == 'later_target_run':
            from app.discovery_models import DiscoveryStage
            from tests.test_tracking_cohorts import persisted_run
            run = persisted_run(session, week='2025-W39')
            stage = session.scalar(select(DiscoveryStage).where(DiscoveryStage.run_id == run.id,
                DiscoveryStage.track == 'garp', DiscoveryStage.stage == 'competition'))
            stage.status = 'confirmed'
            extra = register_cohort(session, run_id=run.id, track='garp', variant_id='full_selected',
                config=default_tracking_config(), registered_at=datetime.fromisoformat(original['registered_at']))
            bindings[str(extra)] = cohort_manifest(session, extra)['manifest_hash']
            session.commit()
    if mismatch == 'run':
        frozen['run_id'] += 1
    elif mismatch in ('input', 'universe'):
        if mismatch == 'input':
            frozen['companies'][0]['payload']['fields']['amount'] = '999'
        else:
            frozen['companies'].pop()
        for company in frozen['companies']:
            company['input_fingerprint'] = digest(company['payload'])
        frozen['run']['companies'] = [{'code': company['payload']['code'],
            'input_fingerprint': company['input_fingerprint']} for company in frozen['companies']]
        frozen['input_fingerprint'] = digest(frozen['run'])
    bundle, grouped, _ = history_fixture(tmp_path, start=date(2025, 9, 17), end=date(2025, 9, 24),
        codes=tuple(company['payload']['code'] for company in frozen['companies']), frozen_input=frozen)
    bundle['decision_at'] = original['decision_cutoff_at']
    if mismatch == 'late_report':
        for report in grouped['report_versions']:
            report['available_at'] = '2025-09-18T00:00:00+00:00'
        rebind_bundle(tmp_path, bundle, grouped)
        bundle['decision_at'] = '2025-09-18T01:00:00+00:00'
    bundle['cohort_bindings'] = bindings
    # Each retained pack is self-consistent, but unrelated to an original bound input/cutoff.
    assert validate_history_bundle(bundle) == ()
    reference = write_json(tmp_path, 'mismatched-history.json', bundle)
    engine.dispose()
    def provider_must_not_be_constructed(*args, **kwargs):
        raise AssertionError('unbound history reached the provider')
    monkeypatch.setattr(track_selection, 'RetainedEventProvider', provider_must_not_be_constructed)
    db = tmp_path / 'tracking.db'
    before = db.read_bytes()
    for mode in ('--dry-run', '--apply'):
        assert track_selection.main(['--db', str(db), '--import-history', reference['path'], mode]) == 2
        report = json.loads(capsys.readouterr().out)
        assert report['applied'] is False
        assert report['reason'] in ('historical_input_projection_mismatch', 'historical_cohort_binding_mismatch',
                                    'historical_decision_cutoff_mismatch')
        assert db.read_bytes() == before


def test_future_arrival_keeps_existing_calculation_idempotent(tmp_path, monkeypatch):
    from jobs.tracking import refresh_tracking
    from tracking.importer import RetainedEventProvider, normalized_event
    engine, factory, _ = tracking_store(tmp_path, monkeypatch)
    records = retained_market(tmp_path)
    assert refresh_tracking(factory, RetainedEventProvider(records), as_of=date(2025, 9, 24))['status'] == 'completed'
    before = counts(factory)
    future = dict(normalized_event(next(r for r in records if r['type'] == 'bar' and r['code'] == '600003.SH')),
        event_id='future-bar', economic_event_id='future-bar', effective_at='2025-10-01T01:00:00+00:00',
        observed_at='2025-10-01T08:00:00+00:00')
    root = tmp_path / 'future'
    root.mkdir()
    new = seal_sources(root, {'market_events': [future]})[2]
    result = refresh_tracking(factory, RetainedEventProvider(new), as_of=date(2025, 9, 24))
    assert result['status'] == 'completed'
    after = counts(factory)
    assert after == (before[0] + 1, before[1], before[2])
    engine.dispose()


def test_partial_resume_and_correction_append_new_paths(tmp_path, monkeypatch):
    from jobs import tracking
    from tracking.importer import RetainedEventProvider, normalized_event
    from app.selection_models import StrategyObservation, StrategyLedgerEntry
    engine, factory, _ = tracking_store(tmp_path, monkeypatch)
    records = retained_market(tmp_path)
    good = RetainedEventProvider(records)
    original = tracking._append_result
    calls = [0]
    def interrupt(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError('synthetic interruption after first cohort')
        return original(*args, **kwargs)
    monkeypatch.setattr(tracking, '_append_result', interrupt)
    assert tracking.refresh_tracking(factory, good, as_of=date(2025, 9, 24))['status'] == 'partial_failed'
    assert counts(factory)[1] == 3
    monkeypatch.setattr(tracking, '_append_result', original)
    assert tracking.refresh_tracking(factory, good, as_of=date(2025, 9, 24))['status'] == 'completed'
    with factory() as session:
        original_rows = [(row.id, row.metrics_json) for row in session.scalars(select(StrategyObservation))]
        old_ledger = [(row.id, row.cash_after) for row in session.scalars(select(StrategyLedgerEntry))]
    record = normalized_event(next(r for r in records if r['event_id'] == 'bar:600003.SH:2025-09-18'))
    correction = {**record, 'event_id': 'corrected-first-mark', 'supersedes_id': record['event_id'],
                  'close': '8', 'observed_at': '2025-09-24T08:00:00+00:00'}
    root = tmp_path / 'correction'
    root.mkdir()
    amended = seal_sources(root, {'market_events': [correction]})[2]
    assert tracking.refresh_tracking(factory, RetainedEventProvider(amended), as_of=date(2025, 9, 24))['status'] == 'completed'
    with factory() as session:
        assert [(session.get(StrategyObservation, identity).id, session.get(StrategyObservation, identity).metrics_json)
                for identity, _ in original_rows] == original_rows
        assert [(session.get(StrategyLedgerEntry, identity).id, session.get(StrategyLedgerEntry, identity).cash_after)
                for identity, _ in old_ledger] == old_ledger
        assert session.scalar(select(func.count()).select_from(StrategyObservation)) == 18
    engine.dispose()


def test_startup_stale_read_cannot_clear_recovered_tracking_owner(tmp_path, monkeypatch):
    from app.db import init_db, create_session_factory
    from app.models import JobLock
    from jobs.lock import acquire_tracking, clear_stale_lock
    engine = init_db(tmp_path / 'startup-race.db')
    factory = create_session_factory(engine)
    then = datetime(2026, 2, 4, tzinfo=timezone.utc)
    now = then + timedelta(seconds=121)
    with factory.begin() as session:
        acquire_tracking(session, now=then)
    replacement = []
    with factory() as session:
        original_get = session.get
        calls = [0]
        def raced_get(model, identity, *args, **kwargs):
            row = original_get(model, identity, *args, **kwargs)
            if model is JobLock:
                calls[0] += 1
                if calls[0] == 2:
                    with factory.begin() as competitor:
                        replacement.append(acquire_tracking(competitor, now=now))
            return row
        monkeypatch.setattr(session, 'get', raced_get)
        clear_stale_lock(session, now_provider=lambda: now)
    with factory() as session:
        assert replacement[0] and session.get(JobLock, 1).owner_token == replacement[0]
    engine.dispose()
