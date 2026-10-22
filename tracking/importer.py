"""Retained-source qualification, not autonomous source authentication.

Raw-file hashes establish integrity. A separately dated independent review
records authenticity, archive and completeness assessments supplied by a trusted
retention process; local names, URLs and hashes cannot establish those facts.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from tracking.contracts import canonical_json, digest
from tracking.market import availability_time, day_value, select_events, utc_time, local_day, SHANGHAI

REQUIRED_HISTORY_PARTS = frozenset({
    'universe_intervals', 'report_versions', 'trading_calendar',
    'market_events', 'benchmark_series', 'source_manifest',
})
TRUST_LIMITATION = ('Retained hashes establish integrity, not authentic identity or historical '
    'availability. Source authenticity, archive completeness and reviewer independence '
    'require the external trusted retention and independent review process.')


class QualificationError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise QualificationError(reason)


def text(value):
    return type(value) is str and bool(value.strip())


def reject_model_provenance(value):
    if isinstance(value, Mapping):
        require(not (set(value) & {'fail_count', 'gray_count', 'then_verdict', 'live_verdict', 'research_label'}
            or value.get('verdict') in ('fail', 'gray', 'pass')
            or any(value.get(key) in ('llm', 'model_labels', 'research_hypothesis')
                   for key in ('origin', 'source', 'proposal_kind'))), 'independent_validation_required')
        for child in value.values():
            reject_model_provenance(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            reject_model_provenance(child)


def strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    def invalid(_):
        raise QualificationError('non_exact_json_number')
    value = json.loads(data, object_pairs_hook=unique, parse_constant=invalid, parse_float=invalid)
    require(isinstance(value, Mapping), 'json_object_required')
    canonical_json(value)
    return value


def retained_bytes(reference, root=Path('.')):
    require(isinstance(reference, Mapping) and text(reference.get('path'))
        and type(reference.get('sha256')) is str and len(reference['sha256']) == 64,
        'retained_source_required')
    raw = (Path(root) / reference['path']).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == reference['sha256'], 'retained_source_hash_mismatch')
    return raw


def retained_document(reference, root=Path('.')):
    return strict_json(retained_bytes(reference, root).decode('utf-8'))


def absolute_reference(reference, root):
    return {**reference, 'path': str((Path(root) / reference['path']).resolve())}


def record_id(record):
    return record.get('event_id', record.get('record_id'))


def normalized_event(event):
    return {key: value for key, value in event.items() if key != 'retained_source'}


def calculation_events(events, as_of):
    """Complete selected heads plus required version/ex-event ancestry at cutoff."""
    by_id = {row['event_id']: row for row in events}
    needed = {row['event_id'] for row in select_events([normalized_event(row) for row in events], as_of)}
    pending = list(needed)
    while pending:
        row = by_id[pending.pop()]
        for reference in (row.get('supersedes_id'), row.get('ex_event_id')):
            if reference and reference not in needed:
                require(reference in by_id, 'market_reference_missing')
                needed.add(reference)
                pending.append(reference)
    return tuple(by_id[key] for key in sorted(needed))


class RetainedSources:
    """One immutable verification snapshot; no cache survives an ingestion call."""
    def __init__(self, manifest_ref, review_ref, root=Path('.')):
        self.root = Path(root)
        self.manifest_ref, self.review_ref = manifest_ref, review_ref
        self.manifest = retained_document(manifest_ref, self.root)
        self.review = retained_document(review_ref, self.root)
        m, r = self.manifest, self.review
        reject_model_provenance(m)
        reject_model_provenance(r)
        require(m.get('origin') == 'retained_source_manifest_v1' and text(m.get('author')),
                'source_manifest_required')
        generated, reviewed = utc_time(m['generated_at']), utc_time(r['reviewed_at'])
        require(r.get('origin') == 'human_review' and text(r.get('reviewer'))
            and r['reviewer'] != m['author'] and generated <= reviewed <= datetime.now(timezone.utc)
            and r.get('manifest_sha256') == manifest_ref['sha256']
            and r.get('decision') == 'source_evidence_reviewed'
            and all(text(r.get(key)) for key in
                    ('independence_basis', 'provenance_assessment', 'archive_assessment')),
            'independent_source_review_required')
        require(type(m.get('records')) is list and m['records'], 'source_records_required')
        self.entries = {}
        for entry in m['records']:
            key = (entry['part'], entry['record_id'])
            require(key not in self.entries, 'duplicate_source_record')
            self.entries[key] = entry

    def verify(self, part, record, *, archival=False):
        entry = self.entries.get((part, record_id(record)))
        require(entry is not None, 'source_record_link_missing')
        value = normalized_event(record)
        reject_model_provenance(value)
        require(entry.get('projection_sha256') == digest(value)
                and entry.get('source_id') == value.get('source_id'), 'source_projection_mismatch')
        raw = retained_bytes(entry['original'], self.root)
        extraction = entry['extraction']
        require(text(entry.get('source_url')) and entry['source_url'].startswith('https://')
            and all(text(extraction.get(key)) for key in ('method', 'locator', 'excerpt'))
            and extraction['excerpt'].encode('utf-8') in raw, 'original_source_extraction_required')
        acquired = utc_time(entry['acquired_at'])
        require(acquired <= utc_time(self.manifest['generated_at']), 'source_acquisition_chronology')
        if 'observed_at' in value:
            require(utc_time(value['observed_at']) == acquired, 'actual_observation_time_mismatch')
        archived = archival or 'availability_basis' in value or 'archived_available_at' in value
        if archived:
            archive = retained_document(entry['archive'], self.root)
            reject_model_provenance(archive)
            available = utc_time(value.get('archived_available_at', value.get('available_at')))
            require(archive.get('origin') == 'archive_capture'
                and archive.get('original_sha256') == entry['original']['sha256']
                and archive.get('record_id') == record_id(value)
                and archive.get('source_url') == entry['source_url']
                and utc_time(archive['original_available_at']) <= utc_time(archive['captured_at'])
                <= available <= acquired, 'historical_archive_link_invalid')
            if 'observed_at' in value:
                require(value.get('availability_basis') == 'retained_archive_v1',
                        'historical_availability_basis_required')
                availability_time(value)
            else:
                require(value.get('archive_verified') is True, 'historical_archive_link_invalid')
        return {'manifest_sha256': self.manifest_ref['sha256'],
                'review_sha256': self.review_ref['sha256'],
                'original_sha256': entry['original']['sha256'],
                'archive_sha256': entry.get('archive', {}).get('sha256'),
                'acquired_at': entry['acquired_at'], 'reviewed_at': self.review['reviewed_at']}


def qualify_events(events):
    """Verify every producer mapping before passing its normalized part to T15."""
    sources, result = {}, []
    for event in events:
        proof = event.get('retained_source')
        require(isinstance(proof, Mapping), 'retained_source_required')
        key = canonical_json({key: value for key, value in proof.items() if key != 'part'})
        if key not in sources:
            sources[key] = RetainedSources(proof['manifest'], proof['review'], proof.get('root', '.'))
        source = sources[key]
        source.verify(proof['part'], event)
        # Snapshot the mapping so a provider cannot mutate it during persistence.
        result.append(strict_json(canonical_json(event)))
    return tuple(result)


def missing_history_parts(bundle):
    return tuple(sorted(REQUIRED_HISTORY_PARTS - bundle.keys()))


def verify_input_projection(document):
    """Recompute the exact company and run semantic hashes used by T14."""
    run, companies = document['run'], document['companies']
    require(type(companies) is list and companies, 'historical_input_projection_missing')
    values = {}
    version_keys = ('data_version', 'financial_version', 'valuation_version',
                    'ruler_version_ids', 'ruler_payloads', 'algorithm_version')
    for item in companies:
        payload, fingerprint = item['payload'], item['input_fingerprint']
        code = payload['code']
        require(text(code) and code not in values and digest(payload) == fingerprint
                and all(payload[key] == run[key] for key in version_keys),
                'historical_input_projection_mismatch')
        values[code] = payload
    require(run['companies'] == [{'code': code, 'input_fingerprint': digest(values[code])}
                                 for code in sorted(values)]
        and digest(run) == document['input_fingerprint'], 'historical_input_projection_mismatch')
    return values


def _projection_value(payload, path):
    require(type(path) is str and path.startswith('/') and len(path) > 1,
            'historical_input_projection_mismatch')
    value = payload
    for component in path[1:].split('/'):
        component = component.replace('~1', '/').replace('~0', '~')
        value = value[int(component)] if type(value) is list else value[component]
    return value


def _metadata_date(value):
    # Listing/report-period labels are dates, not market-session clocks. A 1990
    # listing does not make a 2026 observation use the unsupported pre-1992 zone.
    require(type(value) is str, 'historical_metadata_date_invalid')
    parsed = date.fromisoformat(value)
    require(parsed.isoformat() == value, 'historical_metadata_date_invalid')
    return parsed


def _history(bundle):
    require(type(bundle.get('schema_version')) is int and bundle['schema_version'] == 1,
            'unsupported_history_schema')
    require(not missing_history_parts(bundle), 'historical_evidence_unavailable')
    root = Path(bundle.get('_root', '.'))
    start, end = day_value(bundle['coverage']['start']), day_value(bundle['coverage']['end'])
    decision = utc_time(bundle['decision_at'])
    decision_day = decision.astimezone(SHANGHAI).date()
    require(start <= decision_day <= end, 'history_window_invalid')
    parts = {}
    for name in sorted(REQUIRED_HISTORY_PARTS):
        ref = bundle[name]
        require(text(ref.get('source_id')) and ref.get('coverage') == bundle['coverage'],
                f'part_coverage_missing:{name}')
        parts[name] = retained_document(ref, root)
    sources = RetainedSources(bundle['source_manifest'], bundle['source_review'], root)
    for name in REQUIRED_HISTORY_PARTS - {'source_manifest'}:
        part = parts[name]
        require(part.get('origin') == 'normalized_history' and type(part.get('records')) is list,
                f'history_records_missing:{name}')
        require(sources.manifest.get('datasets', {}).get(name) == bundle[name]['sha256'],
                f'source_dataset_mismatch:{name}')
        require(part['records'], f'history_records_missing:{name}')
        seen = set()
        for row in part['records']:
            require(text(record_id(row)) and record_id(row) not in seen, 'duplicate_history_record')
            seen.add(record_id(row))
            sources.verify(name, row, archival=True)
    intervals = parts['universe_intervals']['records']
    inventory = retained_document(sources.manifest['universe_inventory'], root)
    projection = [{key: row.get(key) for key in ('code', 'listed_at', 'delisted_at')} for row in intervals]
    require(inventory.get('origin') == 'official_records'
        and sorted(projection, key=canonical_json) == sorted(inventory['records'], key=canonical_json),
        'historical_universe_incomplete')
    universe = {}
    for row in intervals:
        listed = _metadata_date(row['listed_at'])
        delisted = _metadata_date(row['delisted_at']) if row.get('delisted_at') else None
        require(text(row.get('code')) and row['code'] not in universe
                and (delisted is None or listed < delisted), 'security_interval_invalid')
        universe[row['code']] = (listed, delisted)
    reports = parts['report_versions']['records']
    versions, children = {}, set()
    for row in sorted(reports, key=lambda row: (utc_time(row['available_at']), record_id(row))):
        identity = record_id(row)
        require(row['code'] in universe and _metadata_date(row['period_end']) <= utc_time(row['available_at']).astimezone(SHANGHAI).date(),
                'report_availability_invalid')
        parent_id = row.get('supersedes_id')
        if parent_id:
            parent = versions.get(parent_id)
            require(parent is not None and parent_id not in children
                and (parent['code'], parent['period_end']) == (row['code'], row['period_end'])
                and utc_time(parent['available_at']) <= utc_time(row['available_at']), 'report_revision_chain_invalid')
            children.add(parent_id)
        else:
            require(not any((old['code'], old['period_end']) == (row['code'], row['period_end'])
                            for old in versions.values()), 'report_revision_chain_invalid')
        versions[identity] = row
    active = {code for code, (listed, delisted) in universe.items()
              if listed <= decision_day and (delisted is None or decision_day < delisted)}
    available = {row['code'] for row in reports if utc_time(row['available_at']) <= decision}
    require(active <= available, 'historical_report_versions_unavailable')
    source_input = retained_document(sources.manifest['input_projection'], root)
    company_inputs = verify_input_projection(source_input)
    require(sources.manifest.get('input_fingerprint') == source_input['input_fingerprint']
            and set(company_inputs) == active, 'historical_input_projection_mismatch')
    bindings = sources.manifest.get('company_report_versions')
    require(isinstance(bindings, Mapping) and set(bindings) == active,
            'historical_input_projection_missing')
    for code, version_ids in bindings.items():
        require(type(version_ids) is list and version_ids and len(set(version_ids)) == len(version_ids),
                'historical_input_projection_missing')
        for version_id in version_ids:
            row = versions.get(version_id)
            require(row is not None and row['code'] == code and utc_time(row['available_at']) <= decision,
                    'historical_report_versions_unavailable')
            require(not any(candidate.get('supersedes_id') == version_id
                            and utc_time(candidate['available_at']) <= decision for candidate in versions.values()),
                    'historical_report_version_superseded_at_cutoff')
            require(row.get('input_projection') == company_inputs[code]
                and isinstance(row.get('value_paths'), Mapping) and row['value_paths']
                and all(type(path) is str and path.startswith('/fields/') and len(path) > 8
                        for path in row['value_paths']),
                'historical_input_projection_mismatch')
            try:
                for key in ('value_paths', 'basis_paths', 'version_paths'):
                    for path, value in row.get(key, {}).items():
                        require(_projection_value(company_inputs[code], path) == value,
                                'historical_input_projection_mismatch')
                if 'period_path' in row:
                    require(_projection_value(company_inputs[code], row['period_path']) == row['period_end'],
                            'historical_input_projection_mismatch')
            except (KeyError, IndexError, TypeError, ValueError):
                raise QualificationError('historical_input_projection_mismatch') from None
    events = [row for name in ('trading_calendar', 'market_events', 'benchmark_series')
              for row in parts[name]['records']]
    require(all(row['type'] == 'calendar_day' for row in parts['trading_calendar']['records'])
        and all(row['type'] == 'benchmark' for row in parts['benchmark_series']['records'])
        and all(row['type'] not in ('calendar_day', 'benchmark') for row in parts['market_events']['records']),
        'history_part_type_mismatch')
    selected = select_events(events, end)
    current = [row for row in selected if not row.get('withdrawn')]
    calendar = {row['date']: row['is_session'] for row in current if row['type'] == 'calendar_day'}
    bars = {(row['code'], local_day(row)) for row in current if row['type'] == 'bar'}
    benchmarks = {(row['code'], local_day(row), row['valuation_point']) for row in current if row['type'] == 'benchmark'}
    coverage = [row for row in current if row['type'] == 'coverage']
    day = start
    while day <= end:
        require(str(day) in calendar, 'historical_calendar_incomplete')
        for code, (listed, delisted) in universe.items():
            if day < listed or (delisted is not None and day >= delisted):
                continue
            assertions = [row for row in coverage if row['code'] == code
                          and day_value(row['start_date']) <= day <= day_value(row['end_date'])]
            require(assertions and all(row['actions_complete'] for row in assertions),
                    f'historical_actions_coverage_incomplete:{code}')
            if calendar[str(day)]:
                require((code, day) in bars and all(row['bars_complete'] for row in assertions),
                        f'historical_raw_prices_incomplete:{code}')
        if calendar[str(day)]:
            require(all((code, day, point) in benchmarks for code in
                ('CSI300_TOTAL_RETURN', 'CSI500_TOTAL_RETURN') for point in ('session_open', 'session_close')),
                'historical_benchmarks_incomplete')
        day += timedelta(days=1)
    for row in events:
        if row['type'] in ('benchmark', 'calendar_day'):
            continue
        require(row['code'] in universe, 'event_security_interval_missing')
        listed, delisted = universe[row['code']]
        effective = local_day(row)
        require(effective >= listed and (row['type'] != 'bar' or delisted is None or effective < delisted),
                'event_outside_security_interval')
    require({row['code'] for row in selected if row['type'] == 'benchmark'} >=
            {'CSI300_TOTAL_RETURN', 'CSI500_TOTAL_RETURN'}, 'historical_benchmarks_unavailable')
    return parts, sources


def validate_history_bundle(bundle: Mapping[str, object]) -> tuple[str, ...]:
    """Return concrete gaps; no boolean can substitute for retained proof/review."""
    if not isinstance(bundle, Mapping):
        return ('historical_evidence_unavailable',)
    missing = missing_history_parts(bundle)
    if missing:
        return ('historical_evidence_unavailable', *(f'missing_history_part:{name}' for name in missing))
    try:
        _history(bundle)
        return ()
    except QualificationError as exc:
        return (str(exc),)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return ('unsupported_historical_timezone' if 'unsupported historical timezone' in str(exc)
                else 'historical_evidence_invalid',)


def verify_history_cohort_bindings(bundle, manifests, frozen_input):
    """Bind source qualification to every original cohort, for import and T13.

    The caller supplies the retained original input preimage: the CLI exports it
    from the existing run; T13 independently verifies its immutable audit binding.
    A self-consistent source bundle at a later cutoff is insufficient.
    """
    companies = verify_input_projection(frozen_input)
    require(manifests, 'original_cohort_bindings_required')
    decision = utc_time(bundle['decision_at'])
    for manifest in manifests:
        require(type(manifest['run_id']) is int and type(frozen_input['run_id']) is int
            and manifest['run_id'] == frozen_input['run_id']
            and manifest['input_hash'] == manifest['source_run_hash'] == frozen_input['input_fingerprint']
            and manifest['universe'] == sorted(companies), 'historical_cohort_binding_mismatch')
        require(manifest.get('decision_cutoff_at') is not None, 'historical_decision_cutoff_unavailable')
        require(decision == utc_time(manifest['decision_cutoff_at']), 'historical_decision_cutoff_mismatch')
        require(all(company['as_of'].get('decision_cutoff_at') is not None
            and utc_time(company['as_of']['decision_cutoff_at']) == decision for company in companies.values()),
            'historical_decision_cutoff_mismatch')
    # Includes report availability/revisions and universe activity at this exact
    # original decision cutoff, together with all retained source/archive proof.
    require(not validate_history_bundle(bundle), 'data_coverage_required')
    root = bundle.get('_root', '.')
    source = retained_document(bundle['source_manifest'], root)
    projection = retained_document(source['input_projection'], root)
    require(canonical_json(projection) == canonical_json(frozen_input), 'historical_input_projection_mismatch')


def history_events(bundle):
    errors = validate_history_bundle(bundle)
    require(not errors, ';'.join(errors))
    parts, _ = _history(bundle)
    root = Path(bundle.get('_root', '.'))
    proof = {'manifest': absolute_reference(bundle['source_manifest'], root),
             'review': absolute_reference(bundle['source_review'], root), 'root': str(root.resolve())}
    return tuple(dict(row, retained_source={**proof, 'part': name})
                 for name in ('trading_calendar', 'market_events', 'benchmark_series')
                 for row in parts[name]['records'])


class RetainedEventProvider:
    """Local retained feed. No live feed or source readiness is silently invented."""
    def __init__(self, events):
        self.events = qualify_events(events)

    def fetch_events(self, codes: Sequence[str], start: date, end: date) -> tuple[Mapping[str, object], ...]:
        # Keep ancestors and future observed corrections; the engine selects at cutoff.
        return tuple(row for row in self.events if row['code'] in {*codes, 'MARKET'})
