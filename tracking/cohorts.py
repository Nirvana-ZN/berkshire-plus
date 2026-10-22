"""Freeze paper cohorts from persisted selection results, within caller transactions.

Only the worker's first competition confirmation may create prospective rows.
The public registration API imports history or returns an existing registration;
its service timestamp cannot turn an old selection into a prospective experiment.
No function here commits, requests market data, or changes a frozen selection.
"""

from collections.abc import Mapping
from datetime import datetime
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.discovery_models import DiscoveryOverride, DiscoveryResult, DiscoveryRun, DiscoveryStage
from app.selection_models import StrategyCohort, StrategyExperiment, StrategyMember
from discovery.builds import CANDIDATE_BUILD_VERSION
from discovery.competition import CompetitionCandidate
from discovery.constants import TRACKS
from discovery.ranking import rank_candidates, ranking_audit
from discovery.rulers import parse_payload
from discovery.selection_contract import rank_inputs
from discovery.snapshot import load_frozen_inputs
from tracking.contracts import (
    OPERATING_FLAGS, VARIANTS, canonical_json, default_tracking_config, digest,
    protocol, utc_timestamp, validated_config,
)


def _existing(session, run_id, track, variant_id, config_hash):
    return session.scalar(select(StrategyCohort).join(StrategyExperiment).where(
        StrategyCohort.run_id == run_id, StrategyCohort.track == track,
        StrategyCohort.variant_id == variant_id, StrategyExperiment.config_hash == config_hash,
    ).order_by(StrategyCohort.id))


def _rows(session, stage):
    return session.scalars(select(DiscoveryResult).where(
        DiscoveryResult.run_id == stage.run_id, DiscoveryResult.track == stage.track,
        DiscoveryResult.stage == stage.stage, DiscoveryResult.attempt == stage.attempt,
    ).order_by(DiscoveryResult.code)).all()


def _source(session, stage):
    """Use exact persisted lineage, overrides and competition entrants, never live facts."""
    run = session.get(DiscoveryRun, stage.run_id)
    gate = session.get(DiscoveryStage, stage.predecessor_stage_id)
    if (gate is None or gate.stage != 'track_gate' or gate.status != 'confirmed'
            or gate.superseded_at is not None or gate.run_id != stage.run_id
            or gate.track != stage.track or stage.predecessor_stage != 'track_gate'
            or stage.input_fingerprint != run.input_fingerprint
            or gate.input_fingerprint != run.input_fingerprint
            or stage.algorithm_version != run.algorithm_version):
        raise ValueError('cohort source has invalid frozen stage lineage')
    frozen = load_frozen_inputs(session, run.id)
    companies = {company.code: company for company in frozen.companies}
    overrides = {row.code: row for row in session.scalars(select(DiscoveryOverride).where(
        DiscoveryOverride.run_id == run.id, DiscoveryOverride.track == stage.track,
        DiscoveryOverride.stage == gate.stage, DiscoveryOverride.attempt == gate.attempt))}
    gates = {row.code: row for row in _rows(session, gate) if
             (row.verdict == 'pass' if row.code not in overrides else overrides[row.code].action == 'forced_pass')}
    rows = _rows(session, stage)
    if {row.code for row in rows} != set(gates) or not set(gates) <= set(companies):
        raise ValueError('competition results differ from frozen gate-pass set')
    ruler_entry = frozen.ruler_payloads.get(stage.track)
    if not isinstance(ruler_entry, Mapping):
        raise ValueError('cohort source ruler unavailable')
    cutoffs = {company.as_of.get('decision_cutoff_at') for company in frozen.companies}
    cutoff = None
    if run.algorithm_version == CANDIDATE_BUILD_VERSION:
        if len(cutoffs) != 1 or None in cutoffs:
            raise ValueError('cohort source decision cutoff unavailable')
        cutoff = utc_timestamp(datetime.fromisoformat(next(iter(cutoffs)))).isoformat()
    source = {
        'run_id': run.id, 'track': stage.track, 'source_run_hash': run.input_fingerprint,
        'input_hash': run.input_fingerprint, 'algorithm_version': run.algorithm_version,
        'ruler_hash': ruler_entry['sha256'], 'decision_cutoff_at': cutoff,
        'gate_stage_id': gate.id, 'competition_stage_id': stage.id,
        'universe': sorted(companies), 'eligible_codes': sorted(gates),
        'market_as_of': {code: companies[code].as_of.get('price') for code in sorted(companies)},
        'source_selection_hash': digest({
            'gate': [{'id': row.id, 'code': row.code, 'verdict': row.verdict,
                      'reason': row.reason, 'metrics_json': row.metrics_json} for row in gates.values()],
            'competition': [{'id': row.id, 'code': row.code, 'verdict': row.verdict,
                             'rank': row.rank, 'reason': row.reason, 'metrics_json': row.metrics_json} for row in rows],
            'gate_overrides': [{'id': row.id, 'code': row.code, 'action': row.action, 'reason': row.reason}
                               for _, row in sorted(overrides.items())],
        }),
    }
    return source, gates, rows, companies, ruler_entry


def _members(gates, selected, reason):
    codes = sorted(selected)
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        weight = format(Decimal(1) / len(codes), 'f') if codes else None
    return [{'code': code, 'weight': weight, 'gate_result_id': gates[code].id,
             'selection_reason': reason} for code in codes]


def _write(session, source, members, *, variant_id, config, kind, registered_at,
           published_at, ablation_audit=None, source_registration_hash=None):
    config_hash = digest(config)
    rules = protocol(config)
    registration_hash = digest({
        'run_id': source['run_id'], 'source_run_hash': source['source_run_hash'],
        'kind': kind, 'config_hash': config_hash, 'protocol_hash': digest(rules),
    })
    experiment = session.scalar(select(StrategyExperiment).where(
        StrategyExperiment.registration_hash == registration_hash))
    if experiment is None:
        experiment = StrategyExperiment(registered_at=registered_at, kind=kind,
            protocol_version=rules['version'], config_json=canonical_json(config),
            config_hash=config_hash, registration_hash=registration_hash)
        session.add(experiment)
        session.flush()
    manifest = {
        **source, 'schema_version': 1, 'kind': kind, 'variant_id': variant_id,
        'registered_at': registered_at.isoformat(),
        'published_run_at': published_at.isoformat() if published_at else None,
        'publication_time_status': 'recorded' if published_at else 'unavailable',
        'config_hash': config_hash, 'registration_hash': registration_hash,
        'protocol_hash': digest(rules), 'protocol': rules, 'members': members,
        'original_member_count': len(members), 'ablation_audit': ablation_audit,
        'source_registration_hash': source_registration_hash,
    }
    manifest['manifest_hash'] = digest(manifest)
    cohort = StrategyCohort(experiment_id=experiment.id, run_id=source['run_id'],
        track=source['track'], variant_id=variant_id, published_run_at=published_at,
        registered_at=registered_at, manifest_json=canonical_json(manifest),
        input_hash=source['input_hash'], state='pending')
    session.add(cohort)
    session.flush()
    session.add_all(StrategyMember(cohort_id=cohort.id, code=row['code'],
        initial_weight=row['weight'], gate_result_id=row['gate_result_id'],
        selection_reason=row['selection_reason']) for row in members)
    session.flush()
    return cohort.id


def publish_track_cohorts(session: Session, *, stage_id: int, published_at: datetime) -> tuple[int, ...]:
    """Worker-only first-publication hook; caller confirms and commits atomically.

    Existing confirmed stages are never upgraded by this entry point. A retry
    before confirmation can reuse only an exact complete three-variant receipt.
    """
    at = utc_timestamp(published_at)
    stage = session.get(DiscoveryStage, stage_id)
    if (stage is None or stage.stage != 'competition' or stage.superseded_at is not None
            or stage.status not in ('running', 'waiting_confirmation')):
        raise ValueError('prospective registration requires first competition publication')
    run = session.get(DiscoveryRun, stage.run_id)
    if run.algorithm_version != CANDIDATE_BUILD_VERSION or run.current is not True or run.status == 'abandoned':
        raise ValueError('prospective registration requires a current v8 run')
    config = validated_config(default_tracking_config())
    existing = [_existing(session, run.id, stage.track, variant, digest(config)) for variant in VARIANTS]
    if any(existing):
        if all(existing) and all(cohort_manifest(session, row.id)['competition_stage_id'] == stage_id
                                 and cohort_manifest(session, row.id)['kind'] == 'prospective' for row in existing):
            return tuple(row.id for row in existing)
        raise ValueError('cohort publication cannot replace an existing registration')
    source, gates, rows, companies, ruler_entry = _source(session, stage)
    if at < utc_timestamp(datetime.fromisoformat(source['decision_cutoff_at'])):
        raise ValueError('publication precedes decision cutoff')
    ruler = parse_payload(ruler_entry['payload'])
    entrants = []
    for row in rows:
        metrics = rank_inputs(json.loads(row.metrics_json), ruler)
        metrics['operating_state'] = 'stable'
        risks = metrics.get('verified_risk_types')
        # None retains the ranker's unknown-evidence semantics.
        if risks is not None:
            metrics['verified_risk_types'] = [risk for risk in risks if risk not in OPERATING_FLAGS]
        entrants.append(CompetitionCandidate(row.code, companies[row.code].industry, metrics))
    ranked = rank_candidates(entrants, ruler.ranking_contract)
    audit = ranking_audit(entrants, ruler.ranking_contract)
    lists = {
        'full_selected': _members(gates, [row.code for row in rows if row.verdict == 'pass'], 'persisted_competition_pass'),
        'no_operating_selected': _members(gates, [row.code for row in ranked if row.verdict == 'pass'], 'operating_layer_removed'),
        'all_passed': _members(gates, gates, 'persisted_gate_pass'),
    }
    return tuple(_write(session, source, lists[variant], variant_id=variant, config=config,
                        kind='prospective', registered_at=at, published_at=at,
                        ablation_audit=audit if variant == 'no_operating_selected' else None)
                 for variant in VARIANTS)


def register_cohort(session: Session, *, run_id: int, track: str, variant_id: str,
                    config: Mapping[str, object], registered_at: datetime) -> int:
    """Idempotent retrieval or explicitly retrospective registration.

    HTTP callers must obtain registered_at from their injected service clock.
    This library has no HTTP registration surface or timestamp override schema.
    A new cost/benchmark experiment never reranks an already frozen variant.
    """
    at = utc_timestamp(registered_at)
    config = validated_config(config)
    if track not in TRACKS or variant_id not in VARIANTS or type(run_id) is not int:
        raise ValueError('invalid cohort run, track or variant')
    existing = _existing(session, run_id, track, variant_id, digest(config))
    if existing is not None:
        return existing.id
    original = session.scalar(select(StrategyCohort).where(
        StrategyCohort.run_id == run_id, StrategyCohort.track == track,
        StrategyCohort.variant_id == variant_id).order_by(StrategyCohort.id))
    if original is not None:
        old = cohort_manifest(session, original.id)
        source_keys = ('run_id', 'track', 'source_run_hash', 'input_hash', 'algorithm_version',
                       'ruler_hash', 'decision_cutoff_at', 'gate_stage_id', 'competition_stage_id',
                       'universe', 'eligible_codes', 'market_as_of', 'source_selection_hash')
        return _write(session, {key: old[key] for key in source_keys}, old['members'],
            variant_id=variant_id, config=config, kind='retrospective_observation',
            registered_at=at, published_at=original.published_run_at,
            ablation_audit=old['ablation_audit'], source_registration_hash=old['manifest_hash'])
    if variant_id == 'no_operating_selected':
        raise ValueError('no operating variant requires a frozen v8 publication receipt')
    stage = session.scalar(select(DiscoveryStage).where(
        DiscoveryStage.run_id == run_id, DiscoveryStage.track == track,
        DiscoveryStage.stage == 'competition', DiscoveryStage.status == 'confirmed',
        DiscoveryStage.superseded_at.is_(None)).order_by(DiscoveryStage.attempt.desc(), DiscoveryStage.id.desc()))
    if stage is None:
        raise ValueError('cohort requires persisted confirmed competition results')
    source, gates, rows, _, _ = _source(session, stage)
    selected = gates if variant_id == 'all_passed' else [row.code for row in rows if row.verdict == 'pass']
    members = _members(gates, selected, 'persisted_gate_pass' if variant_id == 'all_passed' else 'persisted_competition_pass')
    return _write(session, source, members, variant_id=variant_id, config=config,
                  kind='retrospective_observation', registered_at=at, published_at=None)


def cohort_manifest(session: Session, cohort_id: int) -> Mapping[str, object]:
    cohort = session.get(StrategyCohort, cohort_id)
    if cohort is None:
        raise ValueError('cohort not found')
    manifest = json.loads(cohort.manifest_json)
    if digest({key: value for key, value in manifest.items() if key != 'manifest_hash'}) != manifest.get('manifest_hash'):
        raise ValueError('cohort manifest integrity mismatch')
    members = [{'code': row.code, 'weight': row.initial_weight, 'gate_result_id': row.gate_result_id,
                'selection_reason': row.selection_reason} for row in session.scalars(
                    select(StrategyMember).where(StrategyMember.cohort_id == cohort_id).order_by(StrategyMember.code))]
    if members != manifest['members']:
        raise ValueError('cohort member integrity mismatch')
    return manifest
