"""Retained synthetic records only; a caller flag cannot establish archive trust."""

from copy import deepcopy
from datetime import date
import hashlib
import json

import pytest

from tracking.contracts import canonical_json, digest
from tests.test_tracking_ledger import market, event


def write_json(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json(value).encode('utf-8')
    path.write_bytes(raw)
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def seal_sources(root, records_by_part, *, inventory=None, frozen_input=None):
    """Synthetic raw captures and an independently authored fixture receipt.

    These are test attestations, never claims about actual historical feeds.
    """
    entries = []
    for part, records in records_by_part.items():
        for row in records:
            identity = row.get('event_id', row.get('record_id'))
            index = len(entries)
            excerpt = canonical_json(row)
            raw = ('Synthetic source capture\n' + excerpt + '\nEnd source capture').encode('utf-8')
            original = root / f'original-{index}.txt'
            original.write_bytes(raw)
            original_ref = {'path': str(original), 'sha256': hashlib.sha256(raw).hexdigest()}
            available = row.get('archived_available_at', row.get('available_at', row.get('observed_at')))
            archive = write_json(root, f'archive-{index}.json', {
                'origin': 'archive_capture', 'record_id': identity, 'source_url': 'https://fixture.invalid/raw',
                'original_sha256': original_ref['sha256'],
                'original_available_at': available, 'captured_at': available})
            entries.append({'part': part, 'record_id': identity, 'projection_sha256': digest(row),
                'source_id': row['source_id'], 'original': original_ref, 'archive': archive,
                'source_url': 'https://fixture.invalid/raw',
                'acquired_at': row.get('observed_at', '2026-10-20T00:00:00+00:00'),
                'extraction': {'method': 'manual_reviewed_projection', 'locator': 'fixture record',
                               'excerpt': excerpt}})
    parts = {name: write_json(root, f'{name}.json', {'origin': 'normalized_history', 'records': records})
             for name, records in records_by_part.items()}
    source = {'origin': 'retained_source_manifest_v1', 'author': 'fixture extractor',
        'generated_at': '2026-10-20T01:00:00+00:00', 'records': entries,
        'datasets': {name: ref['sha256'] for name, ref in parts.items()}}
    if inventory is not None:
        source['universe_inventory'] = write_json(root, 'inventory.json',
            {'origin': 'official_records', 'records': inventory})
    if frozen_input is not None:
        source['input_projection'] = write_json(root, 'input-projection.json', frozen_input)
        source['input_fingerprint'] = frozen_input['input_fingerprint']
        source['company_report_versions'] = {item['payload']['code']: [row['record_id']
            for row in records_by_part['report_versions'] if row['code'] == item['payload']['code']]
            for item in frozen_input['companies']}
    manifest_ref = write_json(root, 'source-manifest.json', source)
    review_ref = write_json(root, 'source-review.json', {
        'origin': 'human_review', 'reviewer': 'independent fixture archivist',
        'reviewed_at': '2026-10-20T02:00:00+00:00', 'manifest_sha256': manifest_ref['sha256'],
        'decision': 'source_evidence_reviewed', 'independence_basis': 'Separate source reviewer',
        'provenance_assessment': 'Assessed original raw records and extraction linkage',
        'archive_assessment': 'Assessed original version archive and full universe, including exits'})
    proof = {'manifest': manifest_ref, 'review': review_ref}
    attached = tuple(dict(row, retained_source={**proof, 'part': name})
                     for name, rows in records_by_part.items() for row in rows if 'event_id' in row)
    return parts, proof, attached


def history_fixture(root, *, start=date(2026, 1, 29), end=date(2026, 2, 4), codes=('AAA',),
                    frozen_input=None, change_events=None):
    records = market(end, codes=codes, start=start)
    for calendar in [row for row in records if row['type'] == 'calendar_day' and row['is_session']]:
        day = calendar['date']
        for code in ('CSI300_TOTAL_RETURN', 'CSI500_TOTAL_RETURN'):
            for point in ('session_open', 'session_close'):
                records.append(event('benchmark', day, code, event_id=f'{code}:{day}:{point}', index_id=code,
                    source_id='China Securities Index Co., Ltd.', total_return_level='100',
                    return_convention='total_return', valuation_point=point, series_revision='fixture-v1'))
    for row in records:
        row.update(availability_basis='retained_archive_v1', archived_available_at=row['observed_at'],
                   observed_at='2026-10-20T00:00:00+00:00')
    if change_events:
        change_events(records)
    available = f'{start}T00:00:00+00:00'
    intervals = [{'record_id': f'listing:{code}', 'code': code, 'listed_at': '2020-01-01',
        'delisted_at': None, 'source_id': 'synthetic-reviewed', 'available_at': available,
        'archive_verified': True} for code in codes]
    reports = [{'record_id': f'report:{code}', 'code': code, 'period_end': '2024-12-31',
        'source_id': 'synthetic-reviewed', 'available_at': available, 'archive_verified': True,
        'supersedes_id': None} for code in codes]
    if frozen_input is None:
        versions = {'data_version': 'fixture-data', 'financial_version': 'fixture-financial',
            'valuation_version': 'fixture-valuation', 'ruler_version_ids': {}, 'ruler_payloads': {},
            'algorithm_version': 'discovery-v8'}
        companies = []
        for code in codes:
            payload = {**versions, 'code': code, 'name': code, 'industry': 'synthetic',
                'security_status': {}, 'fields': {'amount': '10'}, 'sources': {'amount': 'synthetic'},
                'as_of': {'amount': '2024-12-31'}}
            companies.append({'payload': payload, 'input_fingerprint': digest(payload)})
        run = {**versions, 'trading_dates': {'SH': [str(start)]}, 'companies': [
            {'code': item['payload']['code'], 'input_fingerprint': item['input_fingerprint']} for item in companies]}
        frozen_input = {'run_id': 1, 'input_fingerprint': digest(run), 'run': run, 'companies': companies}
    for report in reports:
        projection = next(item['payload'] for item in frozen_input['companies'] if item['payload']['code'] == report['code'])
        report.update(input_projection=projection, value_paths={'/fields/amount': projection['fields']['amount']})
    grouped = {'universe_intervals': intervals, 'report_versions': reports,
        'trading_calendar': [r for r in records if r['type'] == 'calendar_day'],
        'market_events': [r for r in records if r['type'] not in ('calendar_day', 'benchmark')],
        'benchmark_series': [r for r in records if r['type'] == 'benchmark']}
    inventory = [{k: r[k] for k in ('code', 'listed_at', 'delisted_at')} for r in intervals]
    parts, proof, attached = seal_sources(root, grouped, inventory=inventory, frozen_input=frozen_input)
    coverage = {'start': str(start), 'end': str(end)}
    return {'schema_version': 1, 'decision_at': f'{start}T01:00:00+00:00', 'coverage': coverage,
        **{name: {**ref, 'source_id': 'synthetic-reviewed', 'coverage': coverage} for name, ref in parts.items()},
        'source_manifest': {**proof['manifest'], 'source_id': 'synthetic-reviewed', 'coverage': coverage},
        'source_review': proof['review']}, grouped, attached


def test_current_financials_cannot_impersonate_historical_information():
    from tracking.importer import validate_history_bundle
    errors = validate_history_bundle({'schema_version': 1, 'point_in_time_verified': False})
    assert 'historical_evidence_unavailable' in errors


def test_true_flag_does_not_certify_historical_availability():
    from tracking.importer import validate_history_bundle
    assert 'historical_evidence_unavailable' in validate_history_bundle(
        {'schema_version': 1, 'point_in_time_verified': True, 'archive_verified': True})


def test_retained_history_qualifies_without_rewriting_observed_at(tmp_path):
    from tracking.importer import validate_history_bundle, history_events
    bundle, _, _ = history_fixture(tmp_path)
    assert validate_history_bundle(bundle) == ()
    assert {row['observed_at'] for row in history_events(bundle)} == {'2026-10-20T00:00:00+00:00'}


@pytest.mark.parametrize('mutation', ['raw', 'review', 'projection', 'survivors', 'restatement', 'future_review'])
def test_history_rejects_unbound_or_late_evidence(tmp_path, mutation):
    from tracking.importer import validate_history_bundle
    bundle, grouped, _ = history_fixture(tmp_path, codes=('AAA', 'DELISTED'))
    if mutation == 'raw':
        (tmp_path / 'original-0.txt').write_text('forged replacement')
    elif mutation in ('review', 'future_review'):
        review = json.loads((tmp_path / 'source-review.json').read_text(encoding='utf-8'))
        review['reviewer' if mutation == 'review' else 'reviewed_at'] = (
            'fixture extractor' if mutation == 'review' else '2099-01-01T00:00:00+00:00')
        bundle['source_review'] = write_json(tmp_path, 'source-review.json', review)
    else:
        if mutation == 'survivors':
            grouped['universe_intervals'].pop()
        elif mutation == 'restatement':
            grouped['report_versions'][0]['available_at'] = '2026-10-01T00:00:00+00:00'
        else:
            grouped['report_versions'][0]['code'] = 'FORGED'
        # Rehashing a part alone cannot alter the independently reviewed manifest.
        name = 'universe_intervals' if mutation == 'survivors' else 'report_versions'
        bundle[name].update(write_json(tmp_path, f'{name}.json',
            {'origin': 'normalized_history', 'records': grouped[name]}))
    assert validate_history_bundle(bundle)


def test_rebound_report_projection_cannot_substitute_unrelated_values(tmp_path):
    from tracking.importer import validate_history_bundle
    bundle, grouped, _ = history_fixture(tmp_path)
    frozen = json.loads((tmp_path / 'input-projection.json').read_text(encoding='utf-8'))
    inventory = json.loads((tmp_path / 'inventory.json').read_text(encoding='utf-8'))['records']
    grouped['report_versions'][0]['value_paths']['/fields/amount'] = '999'
    parts, proof, _ = seal_sources(tmp_path, grouped, inventory=inventory, frozen_input=frozen)
    for name, ref in parts.items():
        bundle[name].update(ref)
    bundle['source_manifest'].update(proof['manifest'])
    bundle['source_review'] = proof['review']
    assert 'historical_input_projection_mismatch' in validate_history_bundle(bundle)


def rebind_bundle(root, bundle, grouped):
    frozen = json.loads((root / 'input-projection.json').read_text(encoding='utf-8'))
    inventory = json.loads((root / 'inventory.json').read_text(encoding='utf-8'))['records']
    parts, proof, _ = seal_sources(root, grouped, inventory=inventory, frozen_input=frozen)
    for name, ref in parts.items():
        bundle[name].update(ref)
    bundle['source_manifest'].update(proof['manifest'])
    bundle['source_review'] = proof['review']


@pytest.mark.parametrize('gap', ['bar', 'actions', 'model_source', 'late_report', 'survivor_inventory'])
def test_rebound_source_pack_cannot_hide_specific_historical_gaps(tmp_path, gap):
    from tracking.importer import validate_history_bundle
    bundle, grouped, _ = history_fixture(tmp_path, codes=('AAA', 'EXITED'))
    if gap == 'bar':
        grouped['market_events'] = [r for r in grouped['market_events'] if r['event_id'] != 'bar:AAA:2026-02-02']
    elif gap == 'actions':
        next(r for r in grouped['market_events'] if r['type'] == 'coverage')['actions_complete'] = False
    elif gap == 'model_source':
        grouped['report_versions'][0]['origin'] = 'llm'
    elif gap == 'late_report':
        grouped['report_versions'][0]['available_at'] = '2026-10-01T00:00:00+00:00'
    else:
        grouped['universe_intervals'].pop()
    rebind_bundle(tmp_path, bundle, grouped)
    assert validate_history_bundle(bundle)


def test_old_listing_metadata_does_not_reject_supported_modern_market_window(tmp_path):
    from tracking.importer import validate_history_bundle
    bundle, grouped, _ = history_fixture(tmp_path)
    grouped['universe_intervals'][0]['listed_at'] = '1990-12-19'
    write_json(tmp_path, 'inventory.json', {'origin': 'official_records', 'records': [
        {key: grouped['universe_intervals'][0][key] for key in ('code', 'listed_at', 'delisted_at')}]})
    rebind_bundle(tmp_path, bundle, grouped)
    assert validate_history_bundle(bundle) == ()
    bundle['coverage']['start'] = '1991-12-31'
    assert validate_history_bundle(bundle) == ('unsupported_historical_timezone',)
