"""Descriptive comparisons and exact retained producer exports for release review."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timezone
from decimal import Context, Decimal, localcontext
import json

from sqlalchemy import select

from app.selection_models import StrategyCohort, StrategyMarketEvent, StrategyObservation
from discovery.snapshot import load_frozen_inputs
from tracking.cohorts import cohort_manifest
from tracking.contracts import VARIANTS, canonical_json, digest
from tracking.importer import (TRUST_LIMITATION, normalized_event, qualify_events, require,
    validate_history_bundle, verify_input_projection, retained_document, history_events, calculation_events)
from tracking.ledger import simulate_cohort
from tracking.market import SHANGHAI, utc_time

COMPARISON_POLICY = {'version': 'paper-comparison-v1',
    'benchmark_revisions': 'same_endpoint_series_revision_required',
    'samples': 'descriptive_only_overlapping_samples', 'annualization': 'not_extrapolated',
    'registration_month_timezone': 'Asia/Shanghai'}


def qualified_benchmarks(metrics):
    """Public-safe numeric projection; private T15 output remains unchanged."""
    output = json.loads(canonical_json(metrics.get('benchmarks', {})))
    for item in output.values():
        if item['status'] == 'available' and len(item['series_revisions']) != 1:
            item['status'] = 'benchmark_revision_compatibility_unavailable'
        if item['status'] != 'available':
            item['return'] = item['relative_return'] = None
    return output


def _mean(rows, key):
    numbers = []
    for row in rows:
        value = row.get(key)
        if value is not None:
            if type(value) is not str or not Decimal(value).is_finite():
                raise ValueError('comparison requires finite decimal text')
            numbers.append(Decimal(value))
    with localcontext(Context(prec=34)):
        return format(sum(numbers) / len(numbers), 'f') if numbers else None


def compare_cohorts(observations: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    groups, codes, memberships, latest = {}, set(), 0, {}
    for item in observations:
        manifest, result = item['manifest'], item['result']
        if manifest['variant_id'] not in VARIANTS or result['manifest_hash'] != manifest['manifest_hash']:
            raise ValueError('comparison manifest mismatch')
        identity = item.get('cohort_id', manifest['manifest_hash'])
        for horizon, metrics in result['horizons'].items():
            key = (identity, horizon)
            order = (result['as_of'], item.get('observation_id', 0))
            if key in latest and latest[key][0] == order and latest[key][1] != (item, metrics):
                raise ValueError('comparison revision requires explicit observation_id')
            if key not in latest or order >= latest[key][0]:
                latest[key] = (order, (item, metrics))
    counted = set()
    for (identity, horizon), (_, (item, metrics)) in latest.items():
        manifest, result = item['manifest'], item['result']
        if identity not in counted:
            counted.add(identity)
            codes.update(row['code'] for row in manifest['members'])
            memberships += len(manifest['members'])
        key = (manifest['track'], manifest['algorithm_version'],
               utc_time(manifest['registered_at']).astimezone(SHANGHAI).strftime('%Y-%m'),
               manifest.get('protocol_hash'), horizon)
        if key not in groups:
            groups[key] = {'track': key[0], 'algorithm_version': key[1], 'registration_month': key[2],
                'protocol_hash': key[3], 'horizon_months': int(horizon),
                'variants': {variant: {'observations': []} for variant in VARIANTS}}
        row = {field: metrics.get(field) for field in ('horizon_status', 'entry_target_date',
            'target_date', 'expected_exit_date', 'actual_exit_date', 'terminal_date', 'observation_date',
            'gross_return', 'net_return', 'horizon_return', 'gross_horizon_return', 'max_drawdown',
            'entry_coverage', 'exit_completion_rate', 'trading_cost', 'turnover', 'cost_sensitivity', 'data_gaps')}
        row.update(cohort_id=identity, as_of=result['as_of'], manifest_hash=manifest['manifest_hash'],
                   calculation_version=result['calculation_version'])
        if metrics.get('horizon_status') != 'completed':
            row['horizon_return'] = row['gross_horizon_return'] = None
        row['benchmarks'] = qualified_benchmarks(metrics)
        groups[key]['variants'][manifest['variant_id']]['observations'].append(row)
    for group in groups.values():
        group['missing_variants'] = []
        for variant, data in group['variants'].items():
            rows = sorted(data['observations'], key=lambda row: (str(row['cohort_id']), row['as_of']))
            data['observations'], data['count'] = rows, len(rows)
            if not rows:
                group['missing_variants'].append(variant)
            data['descriptive_means'] = {key: _mean(rows, key) for key in ('entry_coverage',
                'exit_completion_rate', 'max_drawdown', 'net_return', 'horizon_return', 'trading_cost', 'turnover')}
    return {'schema_version': 1, 'inference': 'descriptive_only_overlapping_samples',
        'comparison_policy': dict(COMPARISON_POLICY), 'comparison_policy_hash': digest(COMPARISON_POLICY),
        'groups': [groups[key] for key in sorted(groups)], 'unique_codes': sorted(codes),
        'membership_count': memberships, 'unique_company_count': len(codes),
        'source_trust_limitation': TRUST_LIMITATION}


def load_observations(session):
    """Latest as-of/revision per cohort+horizon; old rows remain independently retained."""
    rows = session.scalars(select(StrategyObservation).order_by(
        StrategyObservation.as_of.desc(), StrategyObservation.id.desc()))
    seen, output = set(), []
    for row in rows:
        key = (row.cohort_id, row.horizon_months)
        if key in seen:
            continue
        seen.add(key)
        payload = json.loads(row.metrics_json)
        output.append({'cohort_id': row.cohort_id, 'observation_id': row.id,
            'manifest': cohort_manifest(session, row.cohort_id),
            'result': {**payload['result_metadata'], 'horizons': {str(row.horizon_months): payload['horizon']}}})
    return output


def export_frozen_input(session, run_id):
    """Export the actual fingerprint preimages, not a byte-hash impersonation."""
    frozen = load_frozen_inputs(session, run_id)
    versions = {key: getattr(frozen, key) for key in ('data_version', 'financial_version',
        'valuation_version', 'ruler_version_ids', 'ruler_payloads', 'algorithm_version')}
    companies = []
    for company in frozen.companies:
        payload = {key: getattr(company, key) for key in ('code', 'name', 'industry',
            'security_status', 'fields', 'sources', 'as_of')}
        payload.update(versions)
        require(digest(payload) == company.input_fingerprint, 'frozen_company_fingerprint_mismatch')
        companies.append({'payload': payload, 'input_fingerprint': company.input_fingerprint})
    run = {**versions, 'trading_dates': frozen.trading_dates,
        'companies': [{'code': item['payload']['code'], 'input_fingerprint': item['input_fingerprint']}
                      for item in companies]}
    require(digest(run) == frozen.input_fingerprint, 'frozen_run_fingerprint_mismatch')
    return json.loads(canonical_json({'run_id': run_id, 'input_fingerprint': frozen.input_fingerprint,
        'run': run, 'companies': companies}))


def export_tracking_audit(session, cohort_ids, *, bundle, as_of: date, horizon_months: int):
    """Private retained export. This creates neither a reviewer nor an eligibility claim.

    Call after refresh; the immutable observations and their exact input event IDs
    are exported. Public HTTP consumers must use a projection without file paths.
    """
    require(horizon_months in (3, 6, 12), 'mature_observation_required')
    entries, events = [], {}
    for cohort_id in cohort_ids:
        manifest = cohort_manifest(session, cohort_id)
        results, metadata, input_ids = {}, None, None
        for months in (3, 6, 12):
            row = session.scalar(select(StrategyObservation).where(
                StrategyObservation.cohort_id == cohort_id, StrategyObservation.horizon_months == months,
                StrategyObservation.as_of == as_of).order_by(StrategyObservation.id.desc()))
            require(row is not None, 'retained_observation_required')
            payload = json.loads(row.metrics_json)
            require(metadata is None or metadata == payload['result_metadata'], 'mixed_observation_revisions')
            require(input_ids is None or input_ids == payload['input_event_ids'], 'mixed_observation_inputs')
            metadata, input_ids = payload['result_metadata'], payload['input_event_ids']
            results[str(months)] = payload['horizon']
        for event_id in input_ids:
            if event_id in events:
                continue
            row = session.get(StrategyMarketEvent, event_id)
            require(row is not None, 'retained_market_event_required')
            value = json.loads(row.payload_json)
            require(digest(value) == row.digest, 'stored_market_integrity_mismatch')
            events[event_id] = value
        entries.append({'cohort_id': cohort_id, 'manifest': manifest, 'input_event_ids': input_ids,
                        'result': {**metadata, 'horizons': results}})
    require(entries and len({row['manifest']['run_id'] for row in entries}) == 1,
            'single_registered_run_required')
    return {'origin': 'paper_tracking_audit_v1', 'as_of': str(as_of), 'horizon_months': horizon_months,
        'observations': entries, 'events': [events[key] for key in sorted(events)], 'history_bundle': bundle,
        'frozen_input': export_frozen_input(session, entries[0]['manifest']['run_id']),
        'source_trust_limitation': TRUST_LIMITATION}


def verify_tracking_audit(audit, bindings, *, training_end, generated_at):
    """Schema2 audit of real T14/T15 outputs; returns exact deterministic-label paths.

    This verifies content and recomputation, not who authored an original receipt.
    T13 still requires the separately bound independent human review.
    """
    require(audit.get('origin') == 'paper_tracking_audit_v1', 'invalid_validation_report')
    entries = audit['observations']
    require(type(entries) is list and len(entries) == 3
        and {row['manifest']['variant_id'] for row in entries} == set(VARIANTS), 'preregistration_required')
    months = audit['horizon_months']
    require(type(months) is int and months in (3, 6, 12), 'mature_observation_required')
    as_of = date.fromisoformat(audit['as_of'])
    require(datetime.combine(as_of, time.max, SHANGHAI) <= generated_at, 'mature_observation_required')
    frozen = audit['frozen_input']
    companies = verify_input_projection(frozen)
    first = entries[0]['manifest']
    require(bindings.get('input_fingerprint') == frozen['input_fingerprint'] == first['input_hash']
        and first['run_id'] == frozen['run_id'] and set(first['universe']) == set(companies),
        'validation_binding_mismatch')
    ruler = frozen['run']['ruler_payloads'][first['track']]
    require(digest(ruler['payload']) == ruler['sha256'] == bindings.get('ruler_payload_hash') == first['ruler_hash'],
            'validation_binding_mismatch')
    bundle = audit['history_bundle']
    require(not validate_history_bundle(bundle), 'data_coverage_required')
    source = retained_document(bundle['source_manifest'], bundle.get('_root', '.'))
    source_review = retained_document(bundle['source_review'], bundle.get('_root', '.'))
    require(utc_time(source_review['reviewed_at']) <= generated_at, 'independent_source_review_required')
    require(retained_document(source['input_projection'], bundle.get('_root', '.')) == frozen,
            'historical_input_projection_mismatch')
    require(utc_time(bundle['decision_at']) == utc_time(first['decision_cutoff_at'])
        and date.fromisoformat(bundle['coverage']['end']) >= as_of, 'data_coverage_required')
    events = qualify_events(audit['events'])
    by_id = {row['event_id']: row for row in events}
    require(len(by_id) == len(events), 'duplicate_history_record')
    historical_events = history_events(bundle)
    historical = {row['event_id']: normalized_event(row) for row in historical_events}
    require(all(historical.get(key) == normalized_event(row) and
                row['retained_source']['manifest']['sha256'] == bundle['source_manifest']['sha256']
                for key, row in by_id.items()), 'data_coverage_required')
    allowed_paths = set()
    audit_fields = {'ranking_branch', 'applicable_dimensions', 'effective_dimensions', 'dimensions',
        'rank_score', 'ranking_uninformative', 'operating_state', 'verified_risk_types', 'risk_state',
        'risk_evidence_missing', 'verdict', 'reason', 'rank', 'cap_exclusion_reason'}
    for index, entry in enumerate(entries):
        manifest, result = entry['manifest'], entry['result']
        require(all(manifest[key] == first[key] for key in ('run_id', 'track', 'input_hash', 'ruler_hash',
            'registration_hash', 'protocol_hash', 'published_run_at', 'registered_at', 'decision_cutoff_at', 'universe')),
            'preregistration_required')
        require(manifest['kind'] == 'prospective' and manifest['source_registration_hash'] is None
            and manifest['source_run_hash'] == frozen['input_fingerprint']
            and bindings.get('protocol_hash') == manifest['protocol_hash']
            and bindings.get('registration_hash') == manifest['registration_hash']
            and manifest['registration_hash'] == digest({'run_id': manifest['run_id'],
                'source_run_hash': manifest['source_run_hash'], 'kind': manifest['kind'],
                'config_hash': manifest['config_hash'], 'protocol_hash': manifest['protocol_hash']}),
            'preregistration_required')
        require(manifest['manifest_hash'] == digest({k: v for k, v in manifest.items() if k != 'manifest_hash'})
            and manifest['protocol_hash'] == digest(manifest['protocol']), 'preregistration_required')
        codes = {row['code'] for row in manifest['members']} | {'MARKET'} | {
            row['id'] for row in manifest['protocol']['benchmarks'].values()}
        expected = calculation_events([row for row in historical_events if row['code'] in codes], as_of)
        require(entry['input_event_ids'] == [row['event_id'] for row in expected], 'complete_source_event_set_required')
        selected_events = [normalized_event(by_id[key]) for key in entry['input_event_ids']]
        replay = simulate_cohort(manifest, selected_events, as_of)
        require(replay == result, 'loss_return_comparison_required')
        metrics = replay['horizons'][str(months)]
        require(metrics['horizon_status'] == 'completed' and metrics['horizon_return'] is not None
            and metrics['exit_completion_rate'] == '1' and not metrics['data_gaps'], 'mature_observation_required')
        entry_at = datetime.combine(date.fromisoformat(metrics['entry_target_date']), time(9, 30), SHANGHAI)
        require(utc_time(manifest['registered_at']) <= utc_time(manifest['published_run_at']) < entry_at,
                'preregistration_required')
        require(training_end < entry_at, 'time_out_of_sample_required')
        for role, expected_id in (('primary', 'CSI300_TOTAL_RETURN'), ('auxiliary', 'CSI500_TOTAL_RETURN')):
            benchmark = metrics['benchmarks'][role]
            require(benchmark['id'] == expected_id and benchmark['status'] == 'available'
                and benchmark['entry_date'] == metrics['entry_target_date']
                and benchmark['end_date'] == metrics['terminal_date']
                and benchmark['end_valuation_point'] == 'session_open', 'frozen_benchmark_required')
            require(len(benchmark['series_revisions']) == 1, 'benchmark_revision_compatibility_unavailable')
        if manifest['variant_id'] == 'no_operating_selected':
            ablation = manifest['ablation_audit']
            require(isinstance(ablation, Mapping) and set(ablation) == set(manifest['eligible_codes'])
                and {code for code, row in ablation.items() if row.get('verdict') == 'pass'} ==
                {row['code'] for row in manifest['members']}, 'preregistration_required')
            for code, row in ablation.items():
                require(isinstance(row, Mapping) and audit_fields <= set(row)
                    and row['verdict'] in ('pass', 'drop') and isinstance(row['dimensions'], Mapping),
                    'preregistration_required')
                allowed_paths.add(('observations', index, 'manifest', 'ablation_audit', code))
    return allowed_paths
