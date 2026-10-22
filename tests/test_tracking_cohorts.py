"""Registration only: synthetic persisted selections and temporary SQLite files."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.db import create_session_factory, init_db
from app.discovery_models import DiscoveryCommonStage, DiscoveryResult, DiscoveryStage
from discovery.constants import TRACKS
from discovery.snapshot import canonical_json, freeze_inputs
from tests.discovery_helpers import SNAPSHOT_AT, add_unfrozen_run, company, versions
from tests.test_selection_v8 import candidate


PUBLISHED = SNAPSHOT_AT + timedelta(hours=1)


@pytest.fixture
def store(tmp_path):
    engine = init_db(tmp_path / 'cohorts.db')
    with create_session_factory(engine)() as session:
        yield session
    engine.dispose()


def persisted_run(session, *, legacy=False, week='2026-W38'):
    """001 wins ablation; 002's annual risk keeps it behind clear 003/004."""
    from discovery.rulers import canonical_payload, payload_digest
    run = add_unfrozen_run(session, week_key=week)
    run.current = True
    run.status = 'running'
    if not legacy:
        run.algorithm_version = 'discovery-v8'
        run.ruler_payloads_json = canonical_json({track: {
            'id': 1, 'payload': json.loads(canonical_payload(candidate(track))),
            'sha256': payload_digest(candidate(track)),
        } for track in sorted(TRACKS)})
    snapshots = [replace(company(f'60000{i}.SH'),
                         as_of={'price': '2026-09-17', 'decision_cutoff_at': SNAPSHOT_AT.isoformat()})
                 for i in range(1, 6)]
    freeze_inputs(session, run, snapshots,
                  replace(versions(), algorithm_version=run.algorithm_version),
                  {'SH': ['2026-09-16', '2026-09-17']}, snapshot_at=SNAPSHOT_AT)
    common = DiscoveryCommonStage(run_id=run.id, attempt=1, status='confirmed',
        input_fingerprint=run.input_fingerprint, algorithm_version=run.algorithm_version,
        created_at=SNAPSHOT_AT, updated_at=SNAPSHOT_AT, confirmed_at=SNAPSHOT_AT)
    session.add(common)
    session.flush()
    for track in sorted(TRACKS):
        gate = DiscoveryStage(run_id=run.id, track=track, stage='track_gate', attempt=1,
            status='confirmed', common_stage_id=common.id, input_fingerprint=run.input_fingerprint,
            algorithm_version=run.algorithm_version, ruler_version_id=1,
            created_at=SNAPSHOT_AT, updated_at=SNAPSHOT_AT, confirmed_at=SNAPSHOT_AT)
        session.add(gate)
        session.flush()
        competition = DiscoveryStage(run_id=run.id, track=track, stage='competition', attempt=1,
            status='waiting_confirmation', predecessor_stage_id=gate.id, predecessor_stage='track_gate',
            input_fingerprint=run.input_fingerprint, algorithm_version=run.algorithm_version,
            ruler_version_id=1, created_at=SNAPSHOT_AT, updated_at=SNAPSHOT_AT,
            confirmed_at=SNAPSHOT_AT)
        session.add(competition)
        session.flush()
        for i in range(1, 6):
            code = f'60000{i}.SH'
            session.add(DiscoveryResult(run_id=run.id, track=track, stage='track_gate',
                attempt=1, code=code, verdict='pass' if i < 5 else 'drop',
                reason=None if i < 5 else 'gate_failed', metrics_json='{}', created_at=SNAPSHOT_AT))
            if i == 5:
                continue
            metric = {name: str(i if direction == 'asc' else 10-i)
                      for name, direction in candidate(track).ranking_contract['branch_orders']['nonfinancial']}
            metric.update(ranking_branch='nonfinancial',
                          operating_state='deteriorating' if i == 1 else 'stable',
                          verified_risk_types=['interim_deterioration'] if i == 1 else
                          ['annual_deducted_drop'] if i == 2 else [])
            session.add(DiscoveryResult(run_id=run.id, track=track, stage='competition',
                attempt=1, code=code, verdict='pass' if i in (3, 4) else 'drop',
                reason=None if i in (3, 4) else 'industry_cap', metrics_json=canonical_json(metric),
                rank={3: 1, 4: 2, 2: 3, 1: 4}[i], created_at=SNAPSHOT_AT))
    session.commit()
    return run


def manifest_for(session, run_id, variant='full_selected', track='garp', config=None):
    from tracking.contracts import default_tracking_config
    from tracking.cohorts import cohort_manifest, register_cohort
    cohort_id = register_cohort(session, run_id=run_id, track=track, variant_id=variant,
        config=default_tracking_config() if config is None else config, registered_at=PUBLISHED)
    return cohort_id, cohort_manifest(session, cohort_id)


def publish_waiting(session, run):
    from jobs.discovery import _commit_if_current, confirm_waiting_layers
    confirm_waiting_layers(session, run, PUBLISHED)
    assert _commit_if_current(session, run.id, lambda: PUBLISHED)


def test_default_experiment_declares_execution_and_maturity():
    from tracking.contracts import default_tracking_config
    config = default_tracking_config()
    assert config['horizons_months'] == [3, 6, 12]
    assert config['entry'] == 'next_full_session_open'
    assert config['unfilled'] == 'retain_cash'
    assert config['round_lots'] is False


def test_publication_freezes_all_variants_from_persisted_results(store):
    from app.selection_models import StrategyCohort
    run = persisted_run(store)
    publish_waiting(store, run)
    assert run.status == 'running'  # Per-track results are public before aggregate completion.
    assert len(store.scalars(select(StrategyCohort)).all()) == 9
    _, full = manifest_for(store, run.id)
    _, ablated = manifest_for(store, run.id, 'no_operating_selected')
    _, all_passed = manifest_for(store, run.id, 'all_passed')
    assert [m['code'] for m in full['members']] == ['600003.SH', '600004.SH']
    assert [m['code'] for m in ablated['members']] == ['600001.SH', '600003.SH']
    assert [m['code'] for m in all_passed['members']] == [f'60000{i}.SH' for i in range(1, 5)]
    assert {m['weight'] for m in full['members']} == {'0.5'}
    assert {m['weight'] for m in all_passed['members']} == {'0.25'}
    assert full['kind'] == 'prospective'
    assert full['registered_at'] == full['published_run_at'] == PUBLISHED.isoformat()
    assert full['decision_cutoff_at'] == SNAPSHOT_AT.isoformat()
    assert full['source_run_hash'] == run.input_fingerprint
    assert all(row.state == 'pending' for row in store.scalars(select(StrategyCohort)))


def test_import_is_retrospective_even_with_backdated_registration(store):
    run = persisted_run(store, legacy=True)
    publish_waiting(store, run)
    run.status = 'completed'
    store.commit()
    _, manifest = manifest_for(store, run.id)
    assert manifest['kind'] == 'retrospective_observation'
    assert manifest['published_run_at'] is None
    assert manifest['publication_time_status'] == 'unavailable'
    from tracking.cohorts import register_cohort
    from tracking.contracts import default_tracking_config
    with pytest.raises(ValueError, match='config'):
        register_cohort(store, run_id=run.id, track='garp', variant_id='full_selected',
            config={**default_tracking_config(), 'kind': 'prospective'}, registered_at=SNAPSHOT_AT)
    with pytest.raises(ValueError, match='v8|operating'):
        manifest_for(store, run.id, 'no_operating_selected')


def legacy_overridden_run(session):
    from app.discovery_models import DiscoveryOverride
    run = persisted_run(session, legacy=True)
    publish_waiting(session, run)
    overrides = [DiscoveryOverride(run_id=run.id, track='garp', stage='competition', attempt=1,
        code=code, action=action, reason=reason, created_at=PUBLISHED)
        for code, action, reason in (
            ('600001.SH', 'forced_pass', 'retained review admitted'),
            ('600003.SH', 'forced_drop', 'retained review excluded'),
        )]
    session.add_all(overrides)
    session.commit()
    return run, overrides


def test_legacy_full_selection_uses_retained_competition_overrides(store):
    run, overrides = legacy_overridden_run(store)
    _, full = manifest_for(store, run.id)
    assert [member['code'] for member in full['members']] == ['600001.SH', '600004.SH']
    assert {member['weight'] for member in full['members']} == {'0.5'}
    assert full['kind'] == 'retrospective_observation'
    assert full['competition_override_audit'] == [
        {'id': overrides[0].id, 'run_id': run.id, 'track': 'garp', 'stage': 'competition',
         'attempt': 1, 'code': '600001.SH', 'action': 'forced_pass',
         'reason': 'retained review admitted', 'created_at': PUBLISHED.isoformat()},
        {'id': overrides[1].id, 'run_id': run.id, 'track': 'garp', 'stage': 'competition',
         'attempt': 1, 'code': '600003.SH', 'action': 'forced_drop',
         'reason': 'retained review excluded', 'created_at': PUBLISHED.isoformat()},
    ]
    _, all_passed = manifest_for(store, run.id, 'all_passed')
    assert [member['code'] for member in all_passed['members']] == [f'60000{i}.SH' for i in range(1, 5)]
    assert all_passed['source_selection_hash'] == full['source_selection_hash']


@pytest.mark.parametrize(('field', 'value'), [
    ('action', 'forced_drop'), ('reason', 'subsequently changed review text'),
])
def test_competition_override_hash_is_bound_and_registration_retries_remain_frozen(store, field, value):
    from tracking.contracts import default_tracking_config
    run, overrides = legacy_overridden_run(store)
    first_id, original = manifest_for(store, run.id)
    store.commit()
    # Change only a synthetic retained source record. Previously registered
    # cohorts must remain byte-identical; a fresh import must bind the change.
    setattr(overrides[0], field, value)
    store.commit()
    _, fresh = manifest_for(store, run.id, 'all_passed')
    assert fresh['source_selection_hash'] != original['source_selection_hash']
    assert manifest_for(store, run.id) == (first_id, original)
    _, other_cost = manifest_for(store, run.id,
        config={**default_tracking_config(), 'cost_bps_each_side': '30'})
    assert other_cost['members'] == original['members']
    assert other_cost['source_selection_hash'] == original['source_selection_hash']
    assert other_cost['competition_override_audit'] == original['competition_override_audit']


def test_registration_retry_reuses_id_and_new_cost_has_distinct_experiment(store):
    from app.selection_models import StrategyCohort
    from tracking.contracts import default_tracking_config
    run = persisted_run(store)
    publish_waiting(store, run)
    first, original = manifest_for(store, run.id)
    assert manifest_for(store, run.id)[0] == first
    other, changed = manifest_for(store, run.id,
        config={**default_tracking_config(), 'cost_bps_each_side': '30'})
    assert other != first
    assert store.get(StrategyCohort, other).experiment_id != store.get(StrategyCohort, first).experiment_id
    assert changed['kind'] == 'retrospective_observation'
    assert original['config_hash'] != changed['config_hash']


@pytest.mark.parametrize('statement', [
    "UPDATE strategy_members SET initial_weight='1'",
    "DELETE FROM strategy_members",
    "INSERT OR REPLACE INTO strategy_members SELECT * FROM strategy_members",
    "UPDATE strategy_experiments SET config_json='{}'",
    "DELETE FROM strategy_experiments",
    "INSERT OR REPLACE INTO strategy_experiments SELECT * FROM strategy_experiments",
    "UPDATE strategy_cohorts SET track='deep_value' WHERE track='garp'",
    "DELETE FROM strategy_cohorts",
    "INSERT OR REPLACE INTO strategy_cohorts SELECT * FROM strategy_cohorts",
])
def test_registration_is_immutable_even_through_sql(store, statement):
    run = persisted_run(store)
    publish_waiting(store, run)
    with pytest.raises(IntegrityError, match='immutable|append-only'):
        store.execute(text(statement))
    store.rollback()


def test_cross_track_members_and_original_denominator_survive_later_exclusion(store):
    from app.selection_models import StrategyMember
    run = persisted_run(store)
    publish_waiting(store, run)
    first, original = manifest_for(store, run.id)
    second, same_codes = manifest_for(store, run.id, track='deep_value')
    assert first != second
    assert len(store.scalars(select(StrategyMember).where(StrategyMember.cohort_id.in_([first, second]))).all()) == 4
    assert len({m['code'] for m in original['members'] + same_codes['members']}) == 2
    # Mutable observations and later rankings cannot remove an original slot.
    store.execute(text("UPDATE strategy_cohorts SET state='pending_maturity' WHERE id=:id"), {'id': first})
    store.execute(text("UPDATE discovery_results SET verdict='drop' WHERE stage='competition'"))
    store.commit()
    assert manifest_for(store, run.id)[1] == original
    changed_config = {**original['protocol']['config'], 'cost_bps_each_side': '30'}
    _, later = manifest_for(store, run.id, config=changed_config)
    assert later['members'] == original['members']
    assert original['original_member_count'] == 2
    assert original['protocol']['competition_exclusion'] == 'not_a_sell_signal'


@pytest.mark.parametrize('flag', ['operating_unknown', 'annual_operating_deterioration'])
def test_ablation_removes_operating_origin_flags_and_preserves_annual_risk(store, flag):
    run = persisted_run(store)
    for row in store.scalars(select(DiscoveryResult).where(
            DiscoveryResult.stage == 'competition', DiscoveryResult.code == '600001.SH')):
        metrics = json.loads(row.metrics_json)
        metrics['verified_risk_types'] = [flag]
        row.metrics_json = canonical_json(metrics)
    store.commit()
    publish_waiting(store, run)
    _, manifest = manifest_for(store, run.id, 'no_operating_selected')
    audit = manifest['ablation_audit']
    assert audit['600001.SH']['risk_state'] == 'clear'
    assert audit['600002.SH']['risk_state'] == 'watch'
    assert audit['600002.SH']['verified_risk_types'] == ['annual_deducted_drop']
    assert [m['code'] for m in manifest['members']] == ['600001.SH', '600003.SH']


def test_partial_publication_retry_preserves_first_track_receipt(store):
    from app.selection_models import StrategyCohort
    run = persisted_run(store)
    deferred = store.scalars(select(DiscoveryStage).where(
        DiscoveryStage.stage == 'competition', DiscoveryStage.track != 'garp')).all()
    for row in deferred:
        row.status = 'failed'
    store.commit()
    publish_waiting(store, run)
    assert len(store.scalars(select(StrategyCohort)).all()) == 3
    first, original = manifest_for(store, run.id)
    for row in deferred:
        row.status = 'waiting_confirmation'
    store.commit()
    from jobs.discovery import confirm_waiting_layers, _commit_if_current
    later = PUBLISHED + timedelta(minutes=1)
    confirm_waiting_layers(store, run, later)
    assert _commit_if_current(store, run.id, lambda: later)
    assert len(store.scalars(select(StrategyCohort)).all()) == 9
    assert manifest_for(store, run.id) == (first, original)
    assert manifest_for(store, run.id, track='deep_value')[1]['published_run_at'] == later.isoformat()


def empty_queued_competition(session):
    """A real valid zero-entrant stage exercises worker publication without market I/O."""
    run = persisted_run(session)
    stage = session.scalar(select(DiscoveryStage).where(
        DiscoveryStage.run_id == run.id, DiscoveryStage.track == 'garp', DiscoveryStage.stage == 'competition'))
    session.execute(delete(DiscoveryResult).where(DiscoveryResult.run_id == run.id,
        DiscoveryResult.track == 'garp', DiscoveryResult.stage == 'competition'))
    for row in session.scalars(select(DiscoveryResult).where(DiscoveryResult.run_id == run.id,
            DiscoveryResult.track == 'garp', DiscoveryResult.stage == 'track_gate')):
        row.verdict = 'drop'
    stage.status = 'queued'
    stage.confirmed_at = None
    session.commit()
    return run, stage


@pytest.mark.parametrize('registration_fails', [False, True])
def test_executor_publication_and_three_cohorts_commit_or_rollback_together(store, monkeypatch, registration_fails):
    from app.selection_models import StrategyCohort, StrategyExperiment
    from jobs import discovery as worker
    run, stage = empty_queued_competition(store)
    # Candidate remains inactive globally; this controlled executor test supplies
    # its local availability boundary and never activates a persisted ruler.
    monkeypatch.setattr(worker, 'AVAILABLE_BUILDS', {'discovery-v8'})
    if registration_fails:
        store.execute(text("CREATE TRIGGER reject_second_variant BEFORE INSERT ON strategy_cohorts "
                           "WHEN NEW.variant_id='no_operating_selected' "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic registration failure'); END"))
        store.commit()
    def executor(context):
        assert context.candidate_codes == ()
        return worker.TrackExecutionOutcome(status='waiting_confirmation')
    if registration_fails:
        with pytest.raises(IntegrityError, match='synthetic registration failure'):
            worker._execute_track_stage(store, stage.id, executor, lambda: PUBLISHED)
        assert store.get(DiscoveryStage, stage.id).status == 'failed'
        assert store.scalars(select(StrategyCohort)).all() == []
        assert store.scalars(select(StrategyExperiment)).all() == []
    else:
        assert worker._execute_track_stage(store, stage.id, executor, lambda: PUBLISHED)
        assert store.get(DiscoveryStage, stage.id).status == 'confirmed'
        assert len(store.scalars(select(StrategyCohort)).all()) == 3
        _, manifest = manifest_for(store, run.id)
        assert manifest['members'] == []
        assert manifest['published_run_at'] == PUBLISHED.isoformat()


@pytest.mark.parametrize('loss', ['lease', 'current'])
def test_publication_rolls_back_when_run_or_lease_is_lost(store, loss):
    from app.selection_models import StrategyCohort
    from jobs.discovery import _commit_if_current, confirm_waiting_layers, DiscoveryLockLostError
    run = persisted_run(store)
    confirm_waiting_layers(store, run, PUBLISHED)
    if loss == 'lease':
        store.info['discovery_lock_run_id'] = run.id
        store.info['discovery_lock_owner_token'] = 'lost-owner'
        with pytest.raises(DiscoveryLockLostError):
            _commit_if_current(store, run.id, lambda: PUBLISHED)
        store.rollback()
    else:
        run.current = False
        assert _commit_if_current(store, run.id, lambda: PUBLISHED) is False
    assert store.scalars(select(StrategyCohort)).all() == []
    assert {row.status for row in store.scalars(select(DiscoveryStage).where(
        DiscoveryStage.stage == 'competition'))} == {'waiting_confirmation'}


def test_new_attempt_cannot_replace_frozen_track_registration(store):
    from app.selection_models import StrategyCohort
    from jobs.discovery import confirm_waiting_layers
    run = persisted_run(store)
    publish_waiting(store, run)
    first, original = manifest_for(store, run.id)
    previous = store.get(DiscoveryStage, original['competition_stage_id'])
    previous.superseded_at = PUBLISHED + timedelta(minutes=1)
    next_stage = DiscoveryStage(run_id=run.id, track='garp', stage='competition', attempt=2,
        status='waiting_confirmation', predecessor_stage_id=previous.predecessor_stage_id,
        predecessor_stage='track_gate', reopen_of_stage_id=previous.id,
        input_fingerprint=run.input_fingerprint, algorithm_version=run.algorithm_version,
        ruler_version_id=1, created_at=PUBLISHED, updated_at=PUBLISHED)
    store.add(next_stage)
    store.commit()
    with pytest.raises(ValueError, match='cannot replace'):
        confirm_waiting_layers(store, run, PUBLISHED + timedelta(minutes=2))
    store.rollback()
    assert manifest_for(store, run.id) == (first, original)
    assert len(store.scalars(select(StrategyCohort)).all()) == 9
    assert store.get(DiscoveryStage, next_stage.id).status == 'waiting_confirmation'


def test_protocol_retains_identity_and_rules_without_inventing_future_data(store):
    run = persisted_run(store)
    publish_waiting(store, run)
    _, manifest = manifest_for(store, run.id)
    rules = manifest['protocol']
    assert rules['benchmarks']['primary']['id'] == 'CSI300_TOTAL_RETURN'
    assert rules['benchmarks']['auxiliary']['id'] == 'CSI500_TOTAL_RETURN'
    assert rules['benchmarks']['primary']['source'] == 'China Securities Index Co., Ltd.'
    assert rules['benchmarks']['primary']['return_convention'] == 'total_return'
    assert rules['benchmarks']['primary']['series_status'] == 'not_observed'
    assert 'records' not in rules['benchmarks']['primary']
    assert rules['exit_policy']['horizons_months'] == [3, 6, 12]
    assert rules['exit_policy']['session'] == 'first_session_after_target'
    assert rules['exit_policy']['anchor'] == 'entry_target_date'
    assert 'maturity_at' not in manifest and 'observation_end' not in manifest
    for key in ('protocol_hash', 'registration_hash', 'source_selection_hash', 'manifest_hash'):
        assert len(manifest[key]) == 64
    from discovery.builds import AVAILABLE_BUILDS, BUILD_VERSION
    assert BUILD_VERSION == 'discovery-v7' and 'discovery-v8' not in AVAILABLE_BUILDS


def test_append_members_and_delete_source_are_rejected(store):
    run = persisted_run(store)
    publish_waiting(store, run)
    cohort_id, original = manifest_for(store, run.id)
    for statement, params in [
        ("INSERT INTO strategy_members (cohort_id,code,initial_weight,gate_result_id,selection_reason) "
         "VALUES (:cohort,'extra','0.5',:gate,'extra')", {'cohort': cohort_id, 'gate': original['members'][0]['gate_result_id']}),
        ("DELETE FROM discovery_runs WHERE id=:id", {'id': run.id}),
    ]:
        with pytest.raises(IntegrityError):
            store.execute(text(statement), params)
        store.rollback()


@pytest.mark.parametrize('cost', ['NaN', 'Infinity', '-1', '10000', 0.1, True])
def test_registration_rejects_nonfinite_or_inexact_cost(store, cost):
    from tracking.contracts import default_tracking_config
    with pytest.raises(ValueError):
        manifest_for(store, 1, config={**default_tracking_config(), 'cost_bps_each_side': cost})
