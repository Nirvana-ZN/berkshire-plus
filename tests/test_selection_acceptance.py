"""Synthetic end-to-end evidence, never a production or mature-return claim.

R01–R16's boundary/counterexample coverage is mapped in the T19 report. This
fixture joins the actual producers; it does not manufacture readiness booleans.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

from sqlalchemy import select
from sqlalchemy.orm import Session


def _selection_run(session, snapshot):
    from app.discovery_models import DiscoveryRun, DiscoveryCommonStage, DiscoveryStage, DiscoveryResult
    from discovery.common import classify_common
    from discovery.snapshot import canonical_json, freeze_inputs, load_frozen_inputs, freeze_discovery_ruler_payloads
    from jobs.discovery import builtin_track_executor, TrackExecutionContext, TrackExecutionCandidate
    from tracking.cohorts import publish_track_cohorts
    at, versions = snapshot.snapshot_at, snapshot.versions
    ids = versions.ruler_version_ids
    run = DiscoveryRun(user_id=1, week_key='2026-W38', current=True, status='running', created_at=at, updated_at=at,
        data_version=versions.data_version, financial_version=versions.financial_version,
        valuation_version=versions.valuation_version, algorithm_version=versions.algorithm_version,
        quality_discount_ruler_version_id=ids['quality_discount'], deep_value_ruler_version_id=ids['deep_value'],
        garp_ruler_version_id=ids['garp'], ruler_payloads_json=freeze_discovery_ruler_payloads(session, ids, versions.algorithm_version))
    session.add(run)
    session.flush()
    freeze_inputs(session, run, snapshot.companies, versions, snapshot.trading_dates, snapshot_at=at)
    frozen = load_frozen_inputs(session, run.id)
    common = DiscoveryCommonStage(run_id=run.id, attempt=1, status='confirmed', input_fingerprint=run.input_fingerprint,
        algorithm_version=run.algorithm_version, created_at=at, updated_at=at, confirmed_at=at)
    session.add(common)
    session.flush()
    entrants = tuple(TrackExecutionCandidate(row.code, 'pass', 'pass', None, '{}', None)
        for row in frozen.companies if classify_common(row, frozen.trading_dates).verdict == 'pass')
    outcomes, cohort_ids = {}, []
    for track in ('quality_discount', 'deep_value', 'garp'):
        gate = DiscoveryStage(run_id=run.id, track=track, stage='track_gate', attempt=1, status='confirmed',
            common_stage_id=common.id, input_fingerprint=run.input_fingerprint, algorithm_version=run.algorithm_version,
            ruler_version_id=ids[track], created_at=at, updated_at=at, confirmed_at=at)
        session.add(gate)
        session.flush()
        context = TrackExecutionContext(gate.id, run.id, track, 'track_gate', 1, run.input_fingerprint,
            ids[track], run.algorithm_version, common.id, None, None, frozen,
            frozen.ruler_payloads[track]['payload'], entrants)
        outcome = builtin_track_executor(context)
        assert outcome.status == 'waiting_confirmation'
        for row in outcome.results:
            session.add(DiscoveryResult(run_id=run.id, track=track, stage='track_gate', attempt=1,
                code=row.code, verdict=row.verdict, reason=row.reason, metrics_json=row.metrics_json,
                rank=row.rank, created_at=at))
        competition = DiscoveryStage(run_id=run.id, track=track, stage='competition', attempt=1,
            status='waiting_confirmation', predecessor_stage_id=gate.id, predecessor_stage='track_gate',
            input_fingerprint=run.input_fingerprint, algorithm_version=run.algorithm_version,
            ruler_version_id=ids[track], created_at=at, updated_at=at)
        session.add(competition)
        session.flush()
        context = replace(context, stage_id=competition.id, stage='competition', common_stage_id=None,
            predecessor_stage_id=gate.id, predecessor_stage='track_gate', candidates=tuple(
                TrackExecutionCandidate(row.code, row.verdict, row.verdict, row.reason, row.metrics_json, row.rank)
                for row in outcome.results if row.verdict == 'pass'))
        ranked = builtin_track_executor(context)
        assert ranked.status == 'waiting_confirmation'
        for row in ranked.results:
            session.add(DiscoveryResult(run_id=run.id, track=track, stage='competition', attempt=1,
                code=row.code, verdict=row.verdict, reason=row.reason, metrics_json=row.metrics_json,
                rank=row.rank, created_at=at))
        session.flush()
        cohort_ids.extend(publish_track_cohorts(session, stage_id=competition.id, published_at=at + timedelta(hours=1)))
        competition.status = 'confirmed'
        competition.confirmed_at = at + timedelta(hours=1)
        outcomes[track] = ranked
    session.commit()
    return run.id, frozen, outcomes, cohort_ids


def test_frozen_source_selection_research_portfolio_and_paper_history(tmp_path, monkeypatch):
    from tests.test_selection_release import retained_release_fill
    from scripts.release_selection_v8 import publish_release
    from app.db import create_session_factory
    from app.models import Financial
    from discovery.snapshot import build_production_snapshot, load_frozen_inputs, canonical_json
    from discovery.evidence_store import append_evidence, load_evidence_as_of
    from discovery.fundamental_types import evidence_identity
    from discovery.fundamentals import fundamentals_from_payload, freeze_fundamentals, snapshot_payload, operating_state, plain
    from research.valuation import append_assumptions
    from jobs.research import create_research_job, _job_evidence_context
    from research.evaluate import normalize_structured, EvalInput
    from research.thesis import dynamic_sizing
    from tests.test_research_evidence import model_output
    from ledger.exposure import ExposurePolicy, check_proposed_exposure
    from tracking.cohorts import cohort_manifest
    from tracking.importer import RetainedEventProvider
    from tracking.evaluation import qualified_benchmarks, compare_cohorts
    from jobs.tracking import refresh_tracking
    from app.selection_models import StrategyObservation
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch, research_sources=True)
    at = datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
    code = '600000.SH'
    monkeypatch.setattr('research.valuation._now', lambda: at - timedelta(minutes=15))
    monkeypatch.setattr('jobs.research._now', lambda: at + timedelta(hours=2))
    with Session(engine) as session:
        records = load_evidence_as_of(session, code, at)
        profile = next(row for row in records if row.kind == 'company_profile')
        shares = next(row for row in records if row.kind == 'share_snapshot')
        quote = next(row for row in records if row.kind == 'market_snapshot' and row.period == '2026-09-17')
        scenario = {'method': 'pe', 'normalized_metric_per_share': '2', 'earnings_growth': '0.1',
            'multiple_low': '10', 'multiple_base': '12', 'multiple_high': '15', 'haircut_low': '0.2',
            'haircut_base': '0.1', 'haircut_high': '0', 'horizon_years': 1, 'metric_basis': 'deducted_ttm',
            'period': '2026H1', 'profile_evidence_id': profile.evidence_id, 'shares_evidence_id': shares.evidence_id,
            'price_evidence_id': quote.evidence_id, 'evidence_ids': [row.evidence_id for row in records],
            'rationale': 'Synthetic reviewed assumptions, not forecasts or source facts.',
            'review': {'method': 'human_review', 'reviewer': 'synthetic reviewer',
                       'reviewed_at': (at - timedelta(minutes=30)).isoformat()}}
        assumption_id = append_assumptions(session, user_id=1, code=code, payload=scenario)
        session.commit()
    class SyntheticPublicationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz)
    with monkeypatch.context() as publication_clock:
        publication_clock.setattr('scripts.release_selection_v8.datetime', SyntheticPublicationClock)
        receipt = publish_release(tmp_path / 'release.db', fill_id=fill_id, reason='synthetic acceptance only', backup_dir=tmp_path / 'backups')
    with Session(engine) as session:
        snapshot = build_production_snapshot(session, fill_id=fill_id)
        run_id, original, outcomes, cohort_ids = _selection_run(session, snapshot)
        from discovery.rulers import execution_authorized
        from app.discovery_models import DiscoveryRun
        assert execution_authorized(session, session.get(DiscoveryRun, run_id))
        assert len(outcomes) == 3
        for ranked in outcomes.values():
            assert len(ranked.results) == 1
            metrics = json.loads(ranked.results[0].metrics_json)
            assert 'ranking_audit' in metrics
            assert metrics['ranking_audit']['risk_state'] == 'deteriorating'
        job = create_research_job(session, name='Synthetic', code=code, discovery_run_id=run_id,
                                  discovery_track='garp', assumption_id=assumption_id)
        session.commit()
        fundamental, policy, research, job_hash = _job_evidence_context(job)
        assert fundamental.cutoff_at == at
        source = next(row for row in fundamental.annuals if row.period == '2025')
        model = model_output(source)
        for gate in model['gates']:
            gate['checked_at'] = at.isoformat()
        inputs = EvalInput(name='Synthetic', code=code, financials=({'period': '2025', 'roe': 20, 'net_income': 20},),
            price=Decimal('10'), book={'positions': [], 'cash_balance': 0}, web_search=False,
            as_of_date='2026-09-17', job_id=job.id, reports_dir=tmp_path, fundamentals=fundamental,
            operating_policy=policy, research_context=research)
        research_result = normalize_structured(model, inputs)
        assert research_result['data_grade'] == 'C'  # latest-period research inputs remain incomplete
        assert research_result['evidence_contract']['financial_inputs_complete'] is False
        assert research_result['price_bands']['steady'] == '23.76'
        assert research_result['valuation_scenarios']['source_facts']['numerator'] == '20'
        forged = json.loads(json.dumps(model))
        forged['gates'][0]['evidence_ids'] = ['invented']
        assert normalize_structured(forged, inputs)['data_grade'] != 'A'
        sizing = dynamic_sizing(research_result, current_price=Decimal('10'), price_usable=True, actual_weight=Decimal('.1'))
        assert sizing['action'] not in {'add_to_cap', 'normal_config'}
        exposure = check_proposed_exposure({code: Decimal('.2')}, {code: Decimal('.1')},
            {code: ('property',)}, ExposurePolicy({'property': Decimal('.25')}, Decimal('.1')))
        assert exposure['allowed'] is False and exposure['proposed_totals']['property'] == Decimal('.3')
        assert check_proposed_exposure({code: Decimal('.3')}, {code: Decimal('-.1')},
            {code: ('property',)}, ExposurePolicy({'property': Decimal('.25')}, Decimal('.1')))['allowed']
        # Actual later source selection changes the same-market fundamentals;
        # the original run/research never follows that source revision.
        live = session.get(Financial, (code, '2025', 'annual'))
        live.deducted_net_income = Decimal('999999')
        revised = replace(source, observed_at=at + timedelta(minutes=30), published_at=at + timedelta(minutes=30),
            supersedes_id=source.evidence_id, values={**source.values, 'deducted_net_income': '1'})
        revised = replace(revised, evidence_id=evidence_identity(revised))
        append_evidence(session, revised)
        session.commit()
        later = freeze_fundamentals(load_evidence_as_of(session, code, at + timedelta(hours=1)), (), (),
            code=code, cutoff_at=at + timedelta(hours=1), current_mcap=fundamental.current_mcap)
        assert later.fingerprint != fundamental.fingerprint
        assert next(row for row in later.annuals if row.period == '2025').values['deducted_net_income'] == '1'
        assert load_frozen_inputs(session, run_id) == original
        assert _job_evidence_context(job)[3] == job_hash
        assert build_production_snapshot(session, fill_id=fill_id) == snapshot
        manifests = {identity: cohort_manifest(session, identity) for identity in cohort_ids}
        assert all(row['input_hash'] == original.input_fingerprint for row in manifests.values())
        assert all(row['kind'] == 'prospective' for row in manifests.values())
    # Retain actual normalized market sources; no provider/network fallback.
    from tests.test_tracking_ledger import market, event
    from tests.test_tracking_import import seal_sources
    end = date(2026, 12, 23)
    events = market(end, codes=(code,), start=date(2026, 9, 17))
    for row in events:
        if row['type'] == 'bar' and row['effective_at'].startswith('2026-09-18'):
            row['suspended'] = True
        if row['type'] == 'bar' and row['effective_at'].startswith('2026-12-21'):
            row['open_limit_down'] = True
        if row['type'] == 'calendar_day' and row['is_session']:
            for index in ('CSI300_TOTAL_RETURN', 'CSI500_TOTAL_RETURN'):
                for point in ('session_open', 'session_close'):
                    events.append(event('benchmark', row['date'], index, event_id=f'{index}:{row["date"]}:{point}',
                        index_id=index, source_id='China Securities Index Co., Ltd.', total_return_level='100',
                        return_convention='total_return', valuation_point=point, series_revision='fixture-v1'))
    events.extend((event('dividend_ex', '2026-10-01', code, cash_per_share='1', pay_date='2026-10-05'),
                   event('dividend_paid', '2026-10-05', code, ex_event_id=f'dividend_ex:{code}:2026-10-01')))
    market_root = tmp_path / 'market'
    market_root.mkdir()
    retained = seal_sources(market_root, {'market_events': events},
        generated_at='2027-01-01T01:00:00+00:00', reviewed_at='2027-01-01T02:00:00+00:00')[2]
    class SimulatedReviewClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 1, 2, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr('tracking.importer.datetime', SimulatedReviewClock)
    factory = create_session_factory(engine)
    provider = RetainedEventProvider(retained)
    assert refresh_tracking(factory, provider, as_of=date(2026, 10, 6), cohort_ids=cohort_ids)['status'] == 'completed'
    with factory() as session:
        pending = session.scalars(select(StrategyObservation)).all()
        assert pending and all(json.loads(row.metrics_json)['horizon']['horizon_return'] is None for row in pending)
    assert refresh_tracking(factory, provider, as_of=end, cohort_ids=cohort_ids)['status'] == 'completed'
    with factory() as session:
        observations = session.scalars(select(StrategyObservation).where(StrategyObservation.horizon_months == 3,
            StrategyObservation.as_of == end)).all()
        assert observations
        for row in observations:
            metric = json.loads(row.metrics_json)['horizon']
            assert metric['horizon_status'] == 'completed'
            assert metric['actual_exit_date'] == '2026-12-22'
            assert qualified_benchmarks(metric)['primary']['status'] == 'available'
            assert metric['receivable'] == '0'
        from tracking.evaluation import load_observations
        comparison = compare_cohorts(load_observations(session))
        assert comparison['unique_company_count'] == 1
        assert comparison['membership_count'] == 9
        assert comparison['inference'] == 'descriptive_only_overlapping_samples'
        assert all(group['kind'] == 'prospective' for group in comparison['groups'])
        assert receipt['report']['acceptance']['software_suite'] == 'external_gate_not_certified'
    from scripts.evolve_rulers import activation_eligibility
    assert activation_eligibility(proposal_kind='model_hypothesis', evidence={'model_labels': ['pass']})[0] is False
    engine.dispose()
