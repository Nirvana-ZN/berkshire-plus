"""Bounded fetches outside transactions, short append transactions, resumable replay."""

from collections.abc import Mapping
from datetime import date
import json
import threading
import time

from sqlalchemy import select

from app.selection_models import StrategyCohort, StrategyMarketEvent, StrategyLedgerEntry, StrategyObservation
from jobs.lock import acquire_tracking, renew_tracking, release, JOB_TYPE_TRACKING
from tracking.cohorts import cohort_manifest
from tracking.contracts import canonical_json, digest
from tracking.importer import qualify_events, normalized_event, calculation_events, TRUST_LIMITATION
from tracking.ledger import simulate_cohort
from tracking.market import utc_time, SHANGHAI

PROVIDER_DEADLINE_SECONDS = 60
LEASE_RENEW_SECONDS = 30
APPEND_BATCH_SIZE = 200
_monotonic = time.monotonic
_wait_event = lambda event, seconds: event.wait(seconds)


def _fetch(provider, codes, start, end, renew):
    """A timed-out daemon owns no session and can only fill this discarded box."""
    ready, box = threading.Event(), {}
    def fetch():
        try:
            box['events'] = provider.fetch_events(codes, start, end)
        except Exception as exc:
            box['error'] = exc
        finally:
            ready.set()
    thread = threading.Thread(target=fetch, name='tracking-read-only-fetch', daemon=True)
    deadline = _monotonic() + PROVIDER_DEADLINE_SECONDS
    thread.start()
    while True:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise TimeoutError('provider_deadline_exceeded')
        if _wait_event(ready, min(LEASE_RENEW_SECONDS, remaining)):
            if _monotonic() >= deadline:
                raise TimeoutError('provider_deadline_exceeded')
            renew()
            if 'error' in box:
                raise box['error']
            if type(box.get('events')) is not tuple:
                raise ValueError('provider_must_return_event_tuple')
            return box['events']
        if _monotonic() >= deadline:
            raise TimeoutError('provider_deadline_exceeded')
        renew()


def _renew(factory, token):
    with factory.begin() as session:
        _require_lease(session, token)


def _require_lease(session, token):
    if not renew_tracking(session, token):
        raise RuntimeError('tracking_lease_lost')


def _event_order(events):
    remaining, ordered, done = {row['event_id']: row for row in events}, [], set()
    while remaining:
        ready = sorted(key for key, row in remaining.items()
                       if not row.get('supersedes_id') or row['supersedes_id'] in done
                       or row['supersedes_id'] not in remaining)
        if not ready:
            raise ValueError('market_revision_cycle')
        for key in ready:
            ordered.append(remaining.pop(key))
            done.add(key)
    return ordered


def _event_binding(record):
    proof = record['retained_source']
    return (normalized_event(record), proof['part'], proof['manifest']['sha256'], proof['review']['sha256'])


def _append_events(factory, token, events):
    ordered = _event_order(events)
    for start in range(0, len(ordered), APPEND_BATCH_SIZE):
        with factory.begin() as session:
            _require_lease(session, token)
            for record in ordered[start:start + APPEND_BATCH_SIZE]:
                content, fingerprint = canonical_json(record), digest(record)
                old = session.get(StrategyMarketEvent, record['event_id'])
                if old is not None:
                    if old.digest != fingerprint or old.payload_json != content:
                        raise ValueError('immutable_market_event_conflict')
                    continue
                session.add(StrategyMarketEvent(event_id=record['event_id'],
                    economic_event_id=record['economic_event_id'], code=record['code'], type=record['type'],
                    effective_at=utc_time(record['effective_at']), observed_at=utc_time(record['observed_at']),
                    source_id=record['source_id'], payload_json=content, digest=fingerprint,
                    supersedes_id=record.get('supersedes_id')))
                session.flush()  # Parent versions must exist before their FK children.


def _append_result(factory, token, cohort_id, result, events):
    metadata = {key: value for key, value in result.items() if key != 'horizons'}
    input_ids = sorted({row['event_id'] for row in events})
    for horizon, metrics in result['horizons'].items():
        # Bounded ledger appends precede the observation receipt. A crash leaves
        # immutable partial rows that the next replay verifies and resumes.
        for start in range(0, len(metrics['ledger']), APPEND_BATCH_SIZE):
            with factory.begin() as session:
                _require_lease(session, token)
                for row in metrics['ledger'][start:start + APPEND_BATCH_SIZE]:
                    values = {key: row[key] for key in ('event_id', 'entry_kind', 'sequence',
                        'shares_after', 'cash_after', 'receivable_after', 'cost')}
                    old = session.scalar(select(StrategyLedgerEntry).where(
                        StrategyLedgerEntry.cohort_id == cohort_id, StrategyLedgerEntry.horizon_months == int(horizon),
                        StrategyLedgerEntry.event_id == row['event_id'], StrategyLedgerEntry.entry_kind == row['entry_kind'],
                        StrategyLedgerEntry.calculation_version == result['calculation_version']))
                    if old is None:
                        session.add(StrategyLedgerEntry(cohort_id=cohort_id, horizon_months=int(horizon),
                            calculation_version=result['calculation_version'], **values))
                    elif any(getattr(old, key) != value for key, value in values.items()):
                        raise ValueError('immutable_ledger_conflict')
        with factory.begin() as session:
            _require_lease(session, token)
            content = canonical_json({'result_metadata': metadata, 'horizon': metrics,
                'input_event_ids': input_ids, 'source_trust_limitation': TRUST_LIMITATION})
            old = session.scalar(select(StrategyObservation).where(
                StrategyObservation.cohort_id == cohort_id, StrategyObservation.horizon_months == int(horizon),
                StrategyObservation.as_of == date.fromisoformat(result['as_of']),
                StrategyObservation.calculation_version == result['calculation_version'],
                StrategyObservation.event_digest == result['event_digest']))
            if old is None:
                session.add(StrategyObservation(cohort_id=cohort_id, horizon_months=int(horizon),
                    as_of=date.fromisoformat(result['as_of']), state=metrics['horizon_status'],
                    metrics_json=content, event_digest=result['event_digest'],
                    calculation_version=result['calculation_version']))
            elif old.metrics_json != content or old.state != metrics['horizon_status']:
                raise ValueError('immutable_observation_conflict')


def refresh_tracking(session_factory, provider, *, as_of: date, cohort_ids=None) -> Mapping[str, object]:
    """Append qualified observations without changing original registration.

    The optional cohort_ids is a scope restriction for an explicitly bound
    historical import. No source provider is supplied implicitly.
    """
    if provider is None:
        return {'status': 'provider_required', 'updated_cohorts': [], 'errors': []}
    if type(as_of) is not date:
        raise ValueError('as_of must be a date')
    with session_factory.begin() as session:
        token = acquire_tracking(session)
    if token is None:
        return {'status': 'busy', 'updated_cohorts': [], 'errors': []}
    updated, errors = [], []
    try:
        with session_factory() as session:
            query = select(StrategyCohort.id).order_by(StrategyCohort.id)
            if cohort_ids is not None:
                query = query.where(StrategyCohort.id.in_(cohort_ids))
            ids = list(session.scalars(query))
            if cohort_ids is not None and set(ids) != set(cohort_ids):
                raise ValueError('historical_cohort_binding_missing')
            manifests = [(identity, cohort_manifest(session, identity)) for identity in ids]
        for identity, manifest in manifests:
            try:
                codes = sorted({row['code'] for row in manifest['members']} |
                    {row['id'] for row in manifest['protocol']['benchmarks'].values()})
                publication = manifest.get('published_run_at')
                start = utc_time(publication).astimezone(SHANGHAI).date() if publication else as_of
                fetched = _fetch(provider, codes, min(start, as_of), as_of,
                                 lambda: _renew(session_factory, token))
                # Qualification and all file/network I/O occur outside DB transactions.
                fetched = qualify_events(fetched)
                if any(row['code'] not in {*codes, 'MARKET'} for row in fetched):
                    raise ValueError('provider_returned_unrequested_code')
                with session_factory() as session:
                    stored = session.scalars(select(StrategyMarketEvent).where(
                        StrategyMarketEvent.code.in_([*codes, 'MARKET']))).all()
                    prior = [json.loads(row.payload_json) for row in stored]
                    if any(digest(record) != row.digest for row, record in zip(stored, prior)):
                        raise ValueError('stored_market_integrity_mismatch')
                combined = {}
                for record in (*prior, *fetched):
                    old = combined.get(record['event_id'])
                    if old is not None and _event_binding(old) != _event_binding(record):
                        raise ValueError('immutable_market_event_conflict')
                    # Relative/absolute locators do not change verified content.
                    # Preserve the original retained reference instead of rewriting it.
                    combined[record['event_id']] = old if old is not None else record
                events = qualify_events(tuple(combined.values()))
                calculation_inputs = calculation_events(events, as_of)
                plain = [normalized_event(row) for row in calculation_inputs]
                result = simulate_cohort(manifest, plain, as_of)
                _append_events(session_factory, token, events)
                _append_result(session_factory, token, identity, result, calculation_inputs)
                updated.append(identity)
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                errors.append({'cohort_id': identity, 'reason': str(exc)})
                break
    finally:
        with session_factory.begin() as session:
            release(session, 0, JOB_TYPE_TRACKING, owner_token=token)
    return {'status': 'partial_failed' if errors and updated else 'failed' if errors else 'completed',
            'updated_cohorts': updated, 'errors': errors,
            'source_trust_limitation': TRUST_LIMITATION}
