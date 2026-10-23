"""Explicit-path tracking CLI. Registration/history default to read-only dry-run."""

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from app.db import create_db_engine, create_session_factory
from app.discovery_models import DiscoveryRun, DiscoveryStage
from app.selection_models import StrategyCohort
from discovery.constants import TRACKS
from jobs.tracking import refresh_tracking
from scripts.calibrate_discovery import _read_only_connection
from tracking.cohorts import cohort_manifest, register_cohort
from tracking.contracts import VARIANTS, canonical_json, default_tracking_config
from tracking.evaluation import compare_cohorts, export_frozen_input, load_observations
from tracking.importer import (RetainedEventProvider, TRUST_LIMITATION, absolute_reference,
    history_events, strict_json, validate_history_bundle, verify_history_cohort_bindings)


def _readonly(path):
    return create_engine('sqlite://', creator=lambda: _read_only_connection(path), poolclass=StaticPool)


def _registration_preview(session, run_id):
    run = session.get(DiscoveryRun, run_id)
    if run is None:
        raise ValueError('run_not_found')
    rows = []
    for track in sorted(TRACKS):
        confirmed = session.scalar(select(DiscoveryStage.id).where(DiscoveryStage.run_id == run_id,
            DiscoveryStage.track == track, DiscoveryStage.stage == 'competition',
            DiscoveryStage.status == 'confirmed', DiscoveryStage.superseded_at.is_(None)))
        for variant in VARIANTS:
            original = session.scalar(select(StrategyCohort).where(StrategyCohort.run_id == run_id,
                StrategyCohort.track == track, StrategyCohort.variant_id == variant).order_by(StrategyCohort.id))
            gap = None if original else 'frozen_v8_publication_required' if variant == 'no_operating_selected' else (
                None if confirmed else 'confirmed_competition_required')
            rows.append({'track': track, 'variant_id': variant, 'existing_cohort_id': original.id if original else None,
                'status': 'existing' if original else 'unavailable' if gap else 'retrospective_registration_available',
                'gap': gap, 'publication_time_status': cohort_manifest(session, original.id)['publication_time_status']
                if original else 'unavailable'})
    return rows


def _feed(path):
    content = strict_json(path.read_text(encoding='utf-8'))
    events = []
    for row in content['events']:
        proof = row['retained_source']
        events.append({**row, 'retained_source': {**proof, 'root': str(path.parent.resolve()),
            'manifest': absolute_reference(proof['manifest'], path.parent),
            'review': absolute_reference(proof['review'], path.parent)}})
    return RetainedEventProvider(tuple(events))


def _run(args):
    if args.refresh and not args.events:
        return {'status': 'provider_required', 'applied': False}, 2
    bundle, provider = None, None
    if args.import_history:
        path = Path(args.import_history).resolve()
        bundle = {**strict_json(path.read_text(encoding='utf-8')), '_root': str(path.parent)}
        errors = validate_history_bundle(bundle)
        if errors:
            return {'status': 'history_gaps', 'dry_run': not args.apply, 'applied': False,
                    'errors': list(errors), 'backtest_completed': False}, 2
    db = Path(args.db).resolve()
    if not db.is_file():
        return {'status': 'database_missing', 'dry_run': not args.apply, 'applied': False}, 2
    # No initialization/migration is performed, including on explicit apply.
    engine = _readonly(db)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            if args.report:
                return compare_cohorts(load_observations(session)), 0
            if args.register_run is not None:
                preview = _registration_preview(session, args.register_run)
                if not args.apply:
                    return {'status': 'registration_preview', 'dry_run': True, 'applied': False, 'cohorts': preview}, 0
            if bundle is not None:
                bindings = bundle.get('cohort_bindings')
                if not isinstance(bindings, dict) or not bindings:
                    return {'status': 'history_gaps', 'dry_run': not args.apply, 'applied': False,
                            'errors': ['original_cohort_bindings_required'], 'backtest_completed': False}, 2
                manifests = []
                for identity, manifest_hash in bindings.items():
                    manifest = cohort_manifest(session, int(identity))
                    if manifest['manifest_hash'] != manifest_hash:
                        raise ValueError('historical_cohort_binding_mismatch')
                    manifests.append(manifest)
                verify_history_cohort_bindings(bundle, manifests,
                    export_frozen_input(session, manifests[0]['run_id']))
                if not args.apply:
                    return {'status': 'history_qualified_for_engine', 'dry_run': True, 'applied': False,
                            'cohort_ids': sorted(map(int, bindings)), 'backtest_completed': False}, 0
    finally:
        engine.dispose()
    if args.refresh and args.dry_run:
        _feed(Path(args.events).resolve())
        return {'status': 'refresh_preview', 'dry_run': True, 'applied': False}, 0
    # All retained file verification precedes opening a write transaction.
    if bundle is not None:
        provider = RetainedEventProvider(history_events(bundle))
    elif args.refresh:
        provider = _feed(Path(args.events).resolve())
    engine = create_db_engine(db)
    factory = create_session_factory(engine)
    try:
        if args.register_run is not None:
            result = []
            with factory.begin() as session:
                for row in preview:
                    if row['gap']:
                        result.append(row)
                        continue
                    identity = register_cohort(session, run_id=args.register_run, track=row['track'],
                        variant_id=row['variant_id'], config=default_tracking_config(),
                        registered_at=datetime.now(timezone.utc))
                    result.append({**row, 'cohort_id': identity})
            return {'status': 'registered', 'applied': True, 'dry_run': False, 'cohorts': result}, 0
        as_of = date.fromisoformat(args.as_of or bundle['coverage']['end'])
        result = refresh_tracking(factory, provider, as_of=as_of,
            cohort_ids=sorted(map(int, bundle['cohort_bindings'])) if bundle is not None else None)
        return {**result, 'applied': bool(result['updated_cohorts']), 'dry_run': False,
                'backtest_completed': False}, 0 if result['status'] == 'completed' else 2
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--register-run', type=int)
    action.add_argument('--refresh', action='store_true')
    action.add_argument('--import-history')
    action.add_argument('--report', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--as-of')
    parser.add_argument('--events')
    parser.add_argument('--format', choices=['json'], default='json')
    args = parser.parse_args(argv)
    if args.apply and (args.refresh or args.report):
        parser.error('--apply applies only to retrospective registration or history ingestion')
    if args.refresh and not args.as_of:
        parser.error('--refresh requires --as-of')
    try:
        result, code = _run(args)
    except (OSError, ValueError, TypeError, KeyError, SQLAlchemyError) as exc:
        result, code = {'status': 'tracking_refused', 'applied': False, 'reason': str(exc)}, 2
    result['source_trust_limitation'] = TRUST_LIMITATION
    print(canonical_json(result))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
