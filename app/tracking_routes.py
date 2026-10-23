"""Public paper-tracking reads; retained source material stays private."""

import json
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.discovery_models import DiscoveryRun
from app.selection_models import StrategyCohort, StrategyExperiment, TrackingRequest
from discovery.constants import TRACKS
from ledger.positions import LOCAL_USER_ID
from tracking.cohorts import cohort_manifest, register_cohort
from tracking.contracts import VARIANTS, canonical_json, default_tracking_config, digest
from tracking.evaluation import compare_cohorts, load_observations, qualified_benchmarks
from tracking.market import SHANGHAI
from jobs.lock import JOB_TYPE_TRACKING, acquire_tracking, release
from jobs.tracking import refresh_tracking


def _error(status, code):
    return JSONResponse(status_code=status, content={'code': code, 'error': code})


def _now():
    return datetime.now(timezone.utc)


def _positive_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 19 and value.isascii()
            and value.isdigit() and 0 < int(value) <= 2**63 - 1)


def _config_id(value):
    return (value == 'paper-v1' or isinstance(value, str) and value.startswith('experiment:')
            and _positive_id(value.removeprefix('experiment:')))


def _claim(factory, key, fingerprint, as_of):
    """Unique identity, CAS expiry recovery; freeze date and cohort scope once."""
    token, now = uuid4().hex, _now()
    try:
        with factory.begin() as session:
            row = session.get(TrackingRequest, key)
            if row is not None:
                if row.fingerprint != fingerprint:
                    return None, _error(409, 'idempotency_conflict')
                if row.status != 'pending':
                    return None, JSONResponse(status_code=row.http_status, content=json.loads(row.response_json))
                changed = session.execute(update(TrackingRequest).where(
                    TrackingRequest.key == key, TrackingRequest.status == 'pending',
                    TrackingRequest.token == row.token, TrackingRequest.expires_at <= now).values(
                        token=token, expires_at=now + timedelta(seconds=120)))
                if changed.rowcount != 1:
                    return None, _error(409, 'tracking_request_pending')
                scope, day = json.loads(row.scope_json), row.as_of
            else:
                scope, day = [row.id for row in _owned_cohorts(session)], as_of
                session.add(TrackingRequest(key=key, fingerprint=fingerprint,
                    scope_json=canonical_json(scope), as_of=day, token=token,
                    expires_at=now + timedelta(seconds=120), status='pending'))
            return (token, scope, day), None
    except (IntegrityError, OperationalError):
        return None, _error(409, 'tracking_request_pending')


def _renew_claim(factory, key, token):
    now = _now()
    with factory.begin() as session:
        changed = session.execute(update(TrackingRequest).where(
            TrackingRequest.key == key, TrackingRequest.token == token,
            TrackingRequest.status == 'pending', TrackingRequest.expires_at > now).values(
                expires_at=now + timedelta(seconds=120)))
        return changed.rowcount == 1


def _finish(factory, key, token, status, body):
    with factory.begin() as session:
        changed = session.execute(update(TrackingRequest).where(
            TrackingRequest.key == key, TrackingRequest.token == token,
            TrackingRequest.status == 'pending', TrackingRequest.expires_at > _now()).values(
                status='completed', http_status=status, response_json=canonical_json(body)))
        if changed.rowcount != 1:
            return _error(409, 'tracking_request_lease_lost')
    return JSONResponse(status_code=status, content=body)


def _config(session, config_id):
    if config_id == 'paper-v1':
        return default_tracking_config()
    if isinstance(config_id, str) and config_id.startswith('experiment:'):
        identity = config_id.removeprefix('experiment:')
        if identity.isdigit() and int(identity) > 0 and any(
                row.experiment_id == int(identity) for row in _owned_cohorts(session)):
            return json.loads(session.get(StrategyExperiment, int(identity)).config_json)
    return None


def _execute(request, payload, key, operation):
    factory = request.app.state.session_factory
    clock = request.app.state.tracking_clock()
    claim, rejection = _claim(factory, key, digest({'operation': operation, 'body': payload}),
                              clock.astimezone(SHANGHAI).date())
    if rejection is not None:
        return rejection
    token, scope, as_of = claim
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(30):
            try:
                if not _renew_claim(factory, key, token):
                    return
            except OperationalError:
                # A short competing write can be retried; expiry still fences completion.
                continue

    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        status, body = 200, {}
        if operation == 'register':
            with factory.begin() as session:
                run = session.get(DiscoveryRun, payload['run_id'])
                config = _config(session, payload['config_id'])
                if run is None or run.user_id != LOCAL_USER_ID:
                    status, body = 404, {'code': 'run_not_found'}
                elif config is None:
                    status, body = 404, {'code': 'config_not_found'}
                else:
                    lease = acquire_tracking(session)
                    if lease is None:
                        status, body = 409, {'code': 'tracking_busy'}
                    else:
                        identity = register_cohort(session, run_id=run.id, track=payload['track'],
                            variant_id=payload['variant'], config=config, registered_at=clock)
                        release(session, 0, JOB_TYPE_TRACKING, owner_token=lease)
                        body = {'cohort_id': identity}
        else:
            result = refresh_tracking(factory, request.app.state.tracking_provider,
                                      as_of=as_of, cohort_ids=scope)
            if result['status'] == 'busy':
                status, body = 409, {'code': 'tracking_busy'}
            elif result['status'] == 'provider_required':
                status, body = 503, {'code': 'evidence_unavailable'}
            elif result['status'] in ('failed', 'partial_failed'):
                # Provider diagnostics can contain private paths; only stable codes are public.
                status, body = 503, {'code': 'evidence_unavailable', 'status': result['status'],
                    'updated_cohorts': result['updated_cohorts'], 'errors': [
                        {'cohort_id': item['cohort_id'], 'code': _public_code(item['reason'])}
                        for item in result['errors']]}
            else:
                body = {'status': result['status'], 'updated_cohorts': result['updated_cohorts'],
                        'as_of': as_of.isoformat()}
    except (ValueError, KeyError, TypeError) as exc:
        status, body = 409, {'code': _public_code(str(exc), 'historical_evidence_unavailable')}
    except (IntegrityError, OperationalError):
        status, body = 409, {'code': 'tracking_busy'}
    except (OSError, RuntimeError):
        status, body = 503, {'code': 'evidence_unavailable'}
    finally:
        stopped.set()
        worker.join(timeout=1)
    return _finish(factory, key, token, status, body)


def _public_code(reason, fallback='evidence_unavailable'):
    codes = {'evidence_unavailable', 'capital_actions_unreconciled', 'period_basis_mismatch',
             'benchmark_unavailable', 'historical_evidence_unavailable',
             'independent_validation_required', 'snapshot_schema_restart_required'}
    return reason if reason in codes else fallback


def _owned_cohorts(session):
    return list(session.scalars(select(StrategyCohort).join(DiscoveryRun).where(
        DiscoveryRun.user_id == LOCAL_USER_ID).order_by(StrategyCohort.id)))


def _observations(session, identities):
    return [item for item in load_observations(session) if item['cohort_id'] in identities]


def _public_benchmarks(metrics):
    fields = ('id', 'source', 'status', 'entry_date', 'entry_valuation_point', 'end_date',
              'end_valuation_point', 'return', 'relative_return', 'event_ids', 'series_revisions')
    return {role: {field: row.get(field) for field in fields}
            for role, row in qualified_benchmarks(metrics).items()}


def _comparison(observations):
    result = compare_cohorts(observations)
    for group in result['groups']:
        for variant in group['variants'].values():
            for row in variant['observations']:
                row['benchmarks'] = _public_benchmarks(row)
                row['cost_sensitivity'] = _public_sensitivity(row.get('cost_sensitivity'))
    return result


def _public_sensitivity(value):
    return {bps: {field: metrics.get(field) for field in ('net_return', 'horizon_return', 'trading_cost', 'turnover')}
            for bps, metrics in (value or {}).items()}


def _horizons(observations, identity):
    result = []
    fields = ('horizon_status', 'entry_target_date', 'target_date', 'expected_exit_date',
              'actual_exit_date', 'terminal_date', 'observation_date', 'entry_coverage',
              'exit_completion_rate', 'net_return', 'horizon_return', 'gross_return',
              'max_drawdown', 'trading_cost', 'turnover', 'cost_sensitivity', 'data_gaps')
    for item in observations:
        if item['cohort_id'] != identity:
            continue
        for months, metrics in item['result']['horizons'].items():
            row = {field: metrics.get(field) for field in fields}
            row.update(horizon_months=int(months), as_of=item['result']['as_of'],
                       status=metrics['horizon_status'], benchmarks=_public_benchmarks(metrics))
            row['cost_sensitivity'] = _public_sensitivity(metrics.get('cost_sensitivity'))
            if row['status'] != 'completed':
                row['horizon_return'] = None
            result.append(row)
    return sorted(result, key=lambda row: row['horizon_months'])


def _cohort(session, row, observations):
    manifest = cohort_manifest(session, row.id)
    fields = ('run_id', 'track', 'variant_id', 'kind', 'algorithm_version', 'registered_at',
              'published_run_at', 'publication_time_status', 'manifest_hash', 'protocol_hash',
              'config_hash', 'registration_hash', 'input_hash', 'original_member_count')
    return {**{field: manifest.get(field) for field in fields}, 'id': row.id,
            'experiment_id': row.experiment_id, 'config_id': f'experiment:{row.experiment_id}',
            'members': [{key: member[key] for key in ('code', 'weight', 'selection_reason')}
                        for member in manifest['members']],
            'horizons': _horizons(observations, row.id),
            'status': 'observed' if any(item['cohort_id'] == row.id for item in observations) else 'pending',
            'horizons_months': manifest['protocol']['config']['horizons_months']}


def create_tracking_router() -> APIRouter:
    router = APIRouter(prefix='/api/tracking')

    async def mutate(request, operation):
        key = request.headers.get('Idempotency-Key', '')
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            return _error(400, 'invalid_tracking_request')
        if not key.strip() or len(key) > 200 or not isinstance(payload, dict):
            return _error(400, 'invalid_tracking_request')
        if operation == 'refresh':
            if payload:
                return _error(400, 'invalid_tracking_request')
        elif (set(payload) != {'run_id', 'track', 'variant', 'config_id'}
              or type(payload['run_id']) is not int or not 0 < payload['run_id'] <= 2**63 - 1
              or not isinstance(payload['track'], str) or payload['track'] not in TRACKS
              or not isinstance(payload['variant'], str) or payload['variant'] not in VARIANTS
              or not _config_id(payload['config_id'])):
            return _error(400, 'invalid_tracking_request')
        return await run_in_threadpool(_execute, request, payload, key, operation)

    @router.post('/cohorts')
    async def register(request: Request):
        return await mutate(request, 'register')

    @router.post('/refresh')
    async def refresh(request: Request):
        return await mutate(request, 'refresh')

    @router.get('/cohorts')
    def cohorts(request: Request):
        params = request.query_params
        track, kind, horizon = params.get('track'), params.get('kind'), params.get('horizon')
        if ((track is not None and track not in TRACKS)
                or (kind is not None and kind not in ('prospective', 'retrospective_observation'))
                or (horizon is not None and horizon not in ('3', '6', '12'))):
            return _error(400, 'invalid_tracking_filter')
        with request.app.state.session_factory() as session:
            rows = _owned_cohorts(session)
            observations = _observations(session, {row.id for row in rows})
            items = [_cohort(session, row, observations) for row in rows]
            items = [item for item in items if (track is None or item['track'] == track)
                     and (kind is None or item['kind'] == kind)
                     and (params.get('build') is None or item['algorithm_version'] == params['build'])]
            ids = {item['id'] for item in items}
            selected = [item for item in observations if item['cohort_id'] in ids]
            if horizon is not None:
                for item in items:
                    item['horizons'] = [row for row in item['horizons'] if row['horizon_months'] == int(horizon)]
                selected = [{**item, 'result': {**item['result'], 'horizons': {
                    key: value for key, value in item['result']['horizons'].items() if key == horizon}}}
                            for item in selected]
            return {'items': items, 'comparison': _comparison(selected),
                    'available_config_ids': ['paper-v1', *[f'experiment:{identity}'
                        for identity in sorted({row.experiment_id for row in rows})]]}

    @router.get('/cohorts/{cohort_id}')
    def cohort(request: Request, cohort_id: str):
        if not _positive_id(cohort_id):
            return _error(400, 'invalid_cohort_id')
        cohort_id = int(cohort_id)
        with request.app.state.session_factory() as session:
            rows = _owned_cohorts(session)
            row = next((row for row in rows if row.id == cohort_id), None)
            if row is None:
                return _error(404, 'cohort_not_found')
            return _cohort(session, row, _observations(session, {cohort_id}))

    @router.get('/experiments/{experiment_id}')
    def experiment(request: Request, experiment_id: str):
        if not _positive_id(experiment_id):
            return _error(400, 'invalid_experiment_id')
        experiment_id = int(experiment_id)
        with request.app.state.session_factory() as session:
            rows = [row for row in _owned_cohorts(session) if row.experiment_id == experiment_id]
            if not rows:
                return _error(404, 'experiment_not_found')
            experiment = session.get(StrategyExperiment, experiment_id)
            observations = _observations(session, {row.id for row in rows})
            return {'id': experiment.id, 'config_id': f'experiment:{experiment.id}', 'kind': experiment.kind,
                    'config_hash': experiment.config_hash, 'protocol_version': experiment.protocol_version,
                    'registered_at': experiment.registered_at.isoformat(),
                    'config': json.loads(experiment.config_json),
                    'cohort_ids': [row.id for row in rows], 'comparison': _comparison(observations)}

    return router
