"""Read-only readiness report; explicit release retains a consistent SQLite backup."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from types import SimpleNamespace

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import create_db_engine, database_read_only
from discovery.builds import BUILD_VERSION
from discovery.snapshot import FrozenCompanyInput, build_production_snapshot, canonical_json

TRACKS = ('quality_discount', 'deep_value', 'garp')
READINESS_POLICY = {'version': 'selection-readiness-v1', 'common_min': '0.95', 'required_min': '0.90',
    'industry_minima': 'same_threshold_for_every_nonempty_industry_no_exemption',
    'listing_years': {'default': 5, 'roic_gross_5y': 6},
    'zero_denominator': 'not_applicable', 'candidate_count': 'not_a_release_gate'}


def _hash(value):
    return sha256(canonical_json(value).encode()).hexdigest()


def _coverage(trusted, applicable, threshold):
    with localcontext() as context:
        context.prec = 34
        ratio = Decimal(trusted) / Decimal(applicable) if applicable else None
    return {'trusted': trusted, 'applicable': applicable, 'coverage': str(ratio) if ratio is not None else None,
            'threshold': threshold, 'status': ('not_applicable' if ratio is None else
                'ready' if ratio >= Decimal(threshold) else 'insufficient')}


def _contracts(companies, policy):
    from scripts.calibrate_discovery import (_quality_payload, _deep_value_payload, _garp_payload,
        HYPOTHESIS_BANDS, DEEP_VALUE_BANDS, GARP_BANDS, QUALITY_CYCLICAL_INDUSTRIES)
    from discovery.rulers import candidate_ruler
    versions = {row.security_status['industry_version'] for row in companies}
    if len(versions) != 1:
        raise ValueError('release industry versions must agree')
    kwargs = {'industry_version': next(iter(versions)), 'valuation_ratio_method': 'PE_TTM/PB_MRQ'}
    legacy = {'quality_discount': _quality_payload(HYPOTHESIS_BANDS['base'],
                  industries=[*[row.industry for row in companies], '\u5236\u9020\u4e1a', '\u94f6\u884c', *QUALITY_CYCLICAL_INDUSTRIES], **kwargs),
              'deep_value': _deep_value_payload(DEEP_VALUE_BANDS['base'], **kwargs),
              'garp': _garp_payload(GARP_BANDS['base'], **kwargs)}
    return {track: candidate_ruler(payload, operating_policy=policy) for track, payload in legacy.items()}


def _frozen_companies(snapshot):
    versions = snapshot.versions
    result = []
    for item in snapshot.companies:
        parts = json.loads(canonical_json({key: getattr(item, key)
            for key in ('security_status', 'fields', 'sources', 'as_of')}))
        result.append(FrozenCompanyInput(item.code, item.name, item.industry, **parts,
            data_version=versions.data_version, financial_version=versions.financial_version,
            valuation_version=versions.valuation_version, ruler_version_ids=dict.fromkeys(TRACKS),
            algorithm_version=BUILD_VERSION, input_fingerprint=_hash(parts), snapshot_at=snapshot.snapshot_at))
    return tuple(result)


def _proofs(snapshot):
    from discovery.fundamentals import coverage_reference_records
    from research.evidence import _source_proof
    records = {row.evidence_id: row for row in snapshot.annuals + snapshot.interims + snapshot.extensions}
    records.update(coverage_reference_records(snapshot))
    proof = {key: _source_proof(row, snapshot.cutoff_at)['status'] == 'verified'
             for key, row in records.items()}
    for key, row in records.items():
        if row.kind == 'company_classification':
            proof[key] = proof.get(row.values.get('_source_evidence_id'), False)
        elif row.kind == 'current_market_cap' and row.status == 'verified':
            # The quote producer retains exact response bytes/hash, not a local
            # proof locator. A reviewed retained same-response market snapshot
            # must bind its amount and date; a status label alone cannot pass.
            proof[key] = any(other.kind == 'market_snapshot' and proof[other.evidence_id]
                and other.document_sha256 == row.document_sha256 and other.source_url == row.source_url
                and other.period == row.values.get('_market_date')
                and other.values.get('current_mcap') == row.values.get('current_mcap')
                for other in records.values())
    return records, proof


def _source_metrics(snapshot, ruler, branch):
    """Source completeness independent of candidate counts or economic success."""
    from discovery.fundamentals import _facts_calculation, period_order, ttm_amount, cash_debt_sources
    from discovery.selection_contract import _dilution
    from discovery.fundamental_types import MetricResult
    rows = snapshot.annuals + snapshot.interims
    latest = max((row.period for row in rows), key=period_order, default=None)
    end = max((int(row.period) for row in snapshot.annuals), default=0)
    missing = MetricResult(None, 'missing', 'financial_reports_missing', ())
    def inputs(fields, years=1):
        required = [(field, str(year)) for year in range(end - years + 1, end + 1) for field in fields]
        return _facts_calculation(snapshot.annuals, required, lambda *values: sum(values)) if end else missing
    result = {}
    if branch == 'bank':
        if ruler.track != 'garp':
            from scripts.calibrate_discovery import _requirements
            from discovery.rulers import _legacy_candidate_projection
            requirements = _requirements(ruler.track, 'financial' if ruler.track == 'deep_value' else 'bank',
                                         _legacy_candidate_projection(ruler).payload())
            for name, fields, window, minimum, mode in requirements:
                years = list(range(end - window + 1, end + 1)) if end else []
                if mode == 'latest':
                    years = years[-1:]
                elif mode == 'endpoints':
                    years = [years[0], years[-1]] if len(years) > 1 else years
                facts = [_facts_calculation(snapshot.annuals, [(field, str(year)) for field in fields],
                                            lambda *values: sum(values)) for year in years]
                valid = [fact for fact in facts if fact.status == 'verified']
                enough = len(valid) >= minimum and (mode == 'count' or len(valid) == len(years))
                result[name] = MetricResult(Decimal(0) if enough else None, 'verified' if enough else 'missing',
                    None if enough else 'bank_proxy_window_incomplete',
                    tuple(sorted({key for fact in valid for key in fact.evidence_ids})))
        return result
    if branch != 'nonfinancial':
        return result
    common = ruler.sector_branches['common']
    windows = common.get('lookback_windows', common.get('annual_observations'))
    result['economic_dilution'] = _dilution(snapshot, SimpleNamespace(
        start_year=end - windows['dilution'] + 1, end_year=end) if end else None)['economic_dilution']
    cap_ids = tuple(row.evidence_id for row in snapshot.extensions if row.kind == 'current_market_cap')
    cap = MetricResult(snapshot.current_mcap, 'verified' if snapshot.current_mcap is not None else 'missing', None, cap_ids)
    profit = ttm_amount(rows, 'deducted_net_income', latest) if latest else missing
    def together(*values):
        invalid = next((item for item in values if item.status != 'verified'), None)
        return MetricResult(None if invalid else Decimal(0), invalid.status if invalid else 'verified',
            invalid.reason if invalid else None, tuple(sorted({key for item in values for key in item.evidence_ids})))
    if ruler.track in {'quality_discount', 'garp'}:
        result['deducted_ttm_and_current_cap'] = together(profit, cap)
    if ruler.track == 'garp':
        result['deducted_growth_window'] = inputs(('deducted_net_income', 'revenue'), 5)
        from discovery.fundamentals import capital_return_sources
        result['roic_gross_5y'] = capital_return_sources(snapshot)
    if ruler.track in {'quality_discount', 'deep_value'}:
        result['normalized_earnings'] = inputs(('deducted_net_income',), 5)
    if ruler.track == 'deep_value':
        result['five_year_fcf'] = inputs(('ocf', 'capex'), 5)
        result['usable_cash_debt'] = cash_debt_sources(snapshot)
        result['current_market_cap'] = cap
    # Operating confirmation compares the same cumulative period year-on-year;
    # an available TTM alone does not prove that comparison.
    prior = str(int(latest[:4]) - 1) + latest[4:] if latest else None
    result['operating_inputs'] = (_facts_calculation(rows,
        [(field, period) for field in ('deducted_net_income', 'ocf') for period in (latest, prior)],
        lambda *values: sum(values)) if latest else missing)
    return result


def _counts(connection):
    names = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for (name,) in names}


def _file_hash(path):
    hasher = sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def consistent_backup(db_path: Path, *, backup_dir: Path, destination: Path | None = None):
    """Capture one stable read transaction, including committed WAL contents."""
    source = Path(db_path).resolve(strict=True)
    if not Path(backup_dir).is_absolute() or (destination is not None and not Path(destination).is_absolute()):
        raise ValueError('backup directory and target must be absolute paths')
    requested_dir = Path(backup_dir).absolute()
    directory = requested_dir.resolve()
    if requested_dir != directory or any(part.is_symlink() for part in (requested_dir, *requested_dir.parents)):
        raise ValueError('backup directory must not traverse a symlink')
    target = Path(destination).absolute() if destination else directory / f'selection-v8-{uuid4().hex}.db'
    if target != target.resolve() or target.parent != directory or target == source or target.exists() or target.is_symlink():
        raise ValueError('backup target must be a new file inside the expected absolute backup directory')
    directory.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents replacing an existing file between validation
    # and SQLite open. The target is never a copied live database/WAL pair.
    with target.open('xb'):
        pass
    try:
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
            original.execute('BEGIN')
            counts = _counts(original)  # establishes the snapshot before backup
            with sqlite3.connect(target) as backup:
                original.backup(backup)
            original.rollback()
        with sqlite3.connect(target.as_uri() + '?mode=ro', uri=True) as check:
            if check.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('backup integrity check failed')
            if check.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('backup foreign key check failed')
            if _counts(check) != counts:
                raise ValueError('backup table counts differ from source snapshot')
        return {'path': str(target), 'sha256': _file_hash(target), 'table_counts': counts,
                'integrity': 'ok', 'foreign_keys': 'ok', 'method': 'sqlite_backup_api'}
    except BaseException:
        # Only the exact file exclusively created above may be removed.
        target.unlink(missing_ok=True)
        raise


def release_report(session: Session, *, fill_id: int):
    if type(fill_id) is not int or fill_id <= 0:
        raise ValueError('fill_id must be a positive integer')
    report = {'version': 'selection-release-v1', 'build': BUILD_VERSION, 'fill_id': fill_id,
        'coverage': {}, 'semantic_changes': ['branch_normalized_ranking', 'economic_dilution',
            'deducted_ttm_valuation', 'usable_cash_debt', 'verified_gross_roic', 'collection_shadow_only'],
        'acceptance': {'software_suite': 'external_gate_not_certified', 'frozen_fill': 'unavailable',
                       'source_authenticity': 'requires_independent_human_review'},
        'blockers': [], 'eligible': False}
    try:
        snapshot = build_production_snapshot(session, fill_id=fill_id)
    except (ValueError, TypeError) as error:
        report['blockers'].append(str(error))
        return report
    from app.models import DiscoveryDataFill, DiscoveryDataCheckpoint
    from discovery.common import classify_common
    from discovery.constants import china_market_date
    from discovery.fundamentals import (fundamentals_from_payload, _fact, classification_identity,
                                        period_order, cash_debt_sources)
    from discovery.industry import classify_industry_branch
    from discovery.field_dictionary import SELECTION_FIELD_DICTIONARY
    from discovery.rulers import canonical_payload, payload_digest
    from market.evidence import RELEASE_SECURITY_FIELDS, RELEASE_CALENDAR_FIELDS
    companies = _frozen_companies(snapshot)
    fill = session.get(DiscoveryDataFill, fill_id)
    checkpoints = session.scalars(select(DiscoveryDataCheckpoint).where(DiscoveryDataCheckpoint.fill_id == fill_id)).all()
    if (fill.payload_schema_version != 4 or fill.failed or fill.completed != len(companies) or fill.total != len(companies)
            or fill.request_count < len(companies) or any(row.status != 'done' or row.attempts < 1
                or row.updated_at < fill.started_at for row in checkpoints)):
        report['blockers'].append('independent_complete_schema4_capture_required')
    report['acceptance']['frozen_fill'] = 'validated'
    report['policy'] = READINESS_POLICY
    report['input_hash'] = _hash({'fill_id': fill_id, 'data_version': snapshot.versions.data_version,
        'financial_version': snapshot.versions.financial_version, 'valuation_version': snapshot.versions.valuation_version,
        'cutoff_at': snapshot.snapshot_at, 'companies': [{key: getattr(row, key) for key in
            ('code', 'name', 'industry', 'security_status', 'fields', 'sources', 'as_of')} for row in companies],
        'trading_dates': snapshot.trading_dates})
    policies = {canonical_json(row.fields['operating_policy']) for row in companies}
    if len(policies) != 1:
        report['blockers'].append('frozen_operating_policies_must_agree')
        return report
    policy = json.loads(next(iter(policies)))
    try:
        rulers = _contracts(companies, policy)
    except (TypeError, ValueError, KeyError) as error:
        report['blockers'].append(str(error))
        return report
    report['rulers'] = {track: json.loads(canonical_payload(ruler)) for track, ruler in rulers.items()}
    report['ruler_digests'] = {track: payload_digest(ruler) for track, ruler in rulers.items()}
    report['configuration_hash'] = _hash({'rulers': report['rulers'], 'readiness': READINESS_POLICY})
    market_date = max(day for dates in snapshot.trading_dates.values() for day in dates)
    inventory = {'version': 'release-universe-v1', 'market_date': market_date,
        'trading_dates': {key: list(value) for key, value in snapshot.trading_dates.items()},
        'securities': [{'code': row.code, 'name': row.name, 'industry': row.industry,
            'security_status': {key: row.security_status.get(key) for key in RELEASE_SECURITY_FIELDS}} for row in companies]}
    counts = {}
    source_audit = []
    universe_qualified = False
    def record(bucket, name, trusted):
        pair = counts.setdefault(bucket, {}).setdefault(name, [0, 0])
        pair[1] += 1
        pair[0] += int(trusted)
    from discovery.tracks.quality_discount import classify as quality
    from discovery.tracks.deep_value import classify as deep
    from discovery.tracks.garp import classify as garp
    classifications = []
    for company in companies:
        fundamental = fundamentals_from_payload(company.fields, code=company.code)
        records, proof = _proofs(fundamental)
        source_audit.extend({'evidence_id': key, 'document_sha256': row.document_sha256, 'retained_verified': proof[key]}
                            for key, row in sorted(records.items()))
        if company.code == companies[0].code:
            universe_qualified = any(row.kind == 'release_universe' and proof[row.evidence_id]
                and row.values.get('_release_universe') == canonical_json(inventory) for row in records.values())
        branch = classify_industry_branch(company.industry)
        classification, trusted_branch = classification_identity(fundamental)
        labels = {row.values.get('_industry') for row in fundamental.extensions
                  if row.kind == 'company_classification' and row.status == 'verified'}
        if (classification.status != 'verified' or trusted_branch != branch or labels != {company.industry}
                or not classification.evidence_ids or not all(proof.get(key, False) for key in classification.evidence_ids)):
            report['blockers'].append(f'{company.code}:unverified_classification_denominator')
        buckets = ('all', 'branch:' + branch, 'industry:' + (company.industry or 'unknown'))
        common = classify_common(company, snapshot.trading_dates)
        metadata_unknown = any(company.security_status.get(key) is None for key in
            ('exchange', 'listing_date', 'is_st', 'is_delisting', 'is_suspended', 'trading_status', 'last_trade_date',
             'industry_version', 'industry_as_of', 'security_source'))
        if branch == 'unknown' or metadata_unknown:
            report['blockers'].append(f'{company.code}:unknown_denominator_state')
        common_known = common.verdict != 'insufficient' and not metadata_unknown and branch != 'unknown'
        for bucket in buckets:
            record(bucket, 'common', common_known)
        if common.verdict == 'drop':
            continue
        cutoff = china_market_date(company.snapshot_at)
        try:
            listed = date.fromisoformat(company.security_status['listing_date'])
            age = cutoff.year - listed.year - ((cutoff.month, cutoff.day) < (listed.month, listed.day))
        except (ValueError, TypeError):
            age = 0
        calendar = {key: policy.get(key) for key in RELEASE_CALENDAR_FIELDS}
        calendar_ok = (policy.get('calendar_complete') is True and policy.get('calendar_status') == 'verified'
            and isinstance(calendar['calendar_coverage_end'], str) and calendar['calendar_coverage_end'] >= cutoff.isoformat()
            and any(row.kind == 'disclosure_calendar' and proof[row.evidence_id]
                and row.values.get('_disclosure_calendar') == canonical_json(calendar) for row in records.values()))
        if not calendar_ok:
            report['blockers'].append(f'{company.code}:retained_disclosure_calendar_missing_or_mismatched')
        def trusted(metric):
            return metric.status == 'verified' and bool(metric.evidence_ids) and all(proof.get(key, False) for key in metric.evidence_ids)
        if branch == 'nonfinancial' and age >= 5:
            all_rows = fundamental.annuals + fundamental.interims
            latest = max((row.period for row in all_rows), key=period_order, default=None)
            for field in SELECTION_FIELD_DICTIONARY:
                value = _fact(all_rows, field, latest)[0] if latest else None
                if field == 'restricted_cash':
                    value = cash_debt_sources(fundamental)
                for bucket in buckets:
                    record(bucket, 'source:' + field, value is not None and trusted(value))
        for track, classifier in (('quality_discount', quality), ('deep_value', deep), ('garp', garp)):
            verdict = classifier(company, rulers[track])
            classifications.append({'code': company.code, 'track': track, 'verdict': verdict.verdict,
                                    'reason': verdict.reason})
            for metric, value in _source_metrics(fundamental, rulers[track], branch).items():
                counts.setdefault('all', {}).setdefault(track + ':' + metric, [0, 0])
                if age < READINESS_POLICY['listing_years'].get(metric, 5):
                    continue
                latest_year = max((int(row.period) for row in fundamental.annuals), default=0)
                fresh = 0 <= cutoff.year - 1 - latest_year <= rulers[track].sector_branches['common']['latest_annual_staleness_years']
                for bucket in buckets:
                    record(bucket, track + ':' + metric, trusted(value) and fresh)
    if not universe_qualified:
        report['blockers'].append('retained_universe_missing_or_mismatched')
    def summarize(bucket):
        entries = {key: _coverage(*values, '0.95' if key == 'common' else '0.90')
                   for key, values in sorted(counts.get(bucket, {}).items())}
        bad = any(row['status'] == 'insufficient' for row in entries.values())
        applicable = entries.get('common', {}).get('applicable', 0)
        return {'status': 'not_applicable' if not applicable else 'insufficient' if bad else 'ready',
                'coverage': entries.get('common', {}).get('coverage'), 'metrics': entries}
    all_counts = summarize('all')['metrics']
    report['coverage'] = {'common': all_counts.get('common', _coverage(0, 0, '0.95')),
        'sources': {field: all_counts.get('source:' + field, _coverage(0, 0, '0.90')) for field in SELECTION_FIELD_DICTIONARY},
        'tracks': {track: {key.split(':', 1)[1]: value for key, value in all_counts.items() if key.startswith(track + ':')} for track in TRACKS},
        'branches': {branch: summarize('branch:' + branch) for branch in ('bank', 'nonfinancial', 'insurance', 'other_financial', 'unknown')},
        'industries': {key.split(':', 1)[1]: summarize(key) for key in counts if key.startswith('industry:')}}
    for bucket in counts:
        if summarize(bucket)['status'] == 'insufficient':
            report['blockers'].append('coverage_insufficient:' + bucket)
    if not companies:
        report['blockers'].append('empty_universe')
    report['source_audit_hash'] = _hash(source_audit)
    report['source_audit'] = source_audit
    report['candidate_diagnostics'] = classifications
    report['acceptance'].update(retained_universe='validated' if universe_qualified else 'unavailable',
        deterministic_contracts='validated', economic_thresholds='base_hypotheses_not_return_validation')
    report['blockers'] = sorted(set(report['blockers']))
    report['eligible'] = not report['blockers']
    return report


def _latest_receipt(session, user_id):
    from app.selection_models import SelectionRelease
    return session.scalar(select(SelectionRelease).where(SelectionRelease.user_id == user_id)
                          .order_by(SelectionRelease.id.desc()).limit(1))


def _receipt_identity(row):
    return None if row is None else {'id': row.id, 'digest': row.digest}


def _read_state(path, fill_id, user_id):
    engine = create_engine(f'sqlite+pysqlite:///{path.as_uri()}?mode=ro&uri=true')
    try:
        with Session(engine) as session:
            session.execute(text('BEGIN'))
            if session.execute(text('PRAGMA user_version')).scalar_one() != 8:
                raise ValueError('release requires supported migrated schema 8')
            return release_report(session, fill_id=fill_id), _receipt_identity(_latest_receipt(session, user_id))
    finally:
        engine.dispose()


def _validate_release_args(db_path, reason, user_id):
    from ledger.positions import LOCAL_USER_ID
    if type(user_id) is not int or user_id != LOCAL_USER_ID:
        raise ValueError('release requires the local owner')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('release requires a nonblank reason')
    return Path(db_path).resolve(strict=True), reason.strip()


def publish_release(db_path: Path, *, fill_id: int, reason: str, backup_dir: Path, user_id: int = 1):
    """Own the transaction; never expose an activation capability to a caller."""
    from app.selection_models import SelectionRelease
    from discovery.rulers import parse_payload, activate, _release_authorization
    path, reason = _validate_release_args(db_path, reason, user_id)
    report, prior = _read_state(path, fill_id, user_id)
    if report['eligible'] is not True:
        raise ValueError('release readiness blocked: ' + '; '.join(report['blockers']))
    backup = consistent_backup(path, backup_dir=backup_dir)
    backup_report, backup_prior = _read_state(Path(backup['path']), fill_id, user_id)
    if _hash(backup_report) != _hash(report) or backup_prior != prior:
        raise ValueError('release source/report/receipt changed during backup')
    engine = create_db_engine(path)
    try:
        if database_read_only(engine):
            raise ValueError('schema_read_only')
        with Session(engine) as session:
            session.execute(text('BEGIN IMMEDIATE'))
            current = release_report(session, fill_id=fill_id)
            if (_hash(current) != _hash(report) or _receipt_identity(_latest_receipt(session, user_id)) != prior
                    or session.execute(text('PRAGMA user_version')).scalar_one() != 8
                    or _file_hash(Path(backup['path'])) != backup['sha256']):
                raise ValueError('release source/report/receipt changed after backup')
            rulers = {track: parse_payload(payload) for track, payload in report['rulers'].items()}
            with _release_authorization(session, rulers, user_id=user_id):
                rows = {track: activate(session, rulers[track], reason, user_id=user_id) for track in TRACKS}
            created_at = datetime.now(timezone.utc)
            payload = {'version': 'selection-release-v1', 'user_id': user_id, 'action': 'publish',
                'build': BUILD_VERSION, 'fill_id': fill_id, 'previous_id': prior['id'] if prior else None,
                'previous_digest': prior['digest'] if prior else None, 'reason': reason,
                'created_at': created_at.isoformat(), 'input_hash': report['input_hash'],
                'configuration_hash': report['configuration_hash'], 'report': report, 'report_hash': _hash(report),
                'backup': backup, 'rulers': {track: {'id': row.id, 'digest': row.digest} for track, row in rows.items()}}
            row = SelectionRelease(user_id=user_id, created_at=created_at, action='publish', build=BUILD_VERSION,
                fill_id=fill_id, previous_id=payload['previous_id'], reason=reason,
                payload_json=canonical_json(payload), digest=_hash(payload))
            session.add(row)
            session.flush()
            result = {'id': row.id, 'digest': row.digest, **payload}
            session.commit()
            return result
    finally:
        engine.dispose()


def rollback_release(db_path: Path, *, reason: str, backup_dir: Path, user_id: int = 1):
    """Append a read-only decision. Historical receipts/rulers remain intact."""
    from app.selection_models import SelectionRelease
    path, reason = _validate_release_args(db_path, reason, user_id)
    engine = create_db_engine(path)
    try:
        if database_read_only(engine):
            raise ValueError('schema_read_only')
        with Session(engine) as session:
            prior = _latest_receipt(session, user_id)
            if prior is None:
                raise ValueError('no release to roll back')
            identity, fill_id = _receipt_identity(prior), prior.fill_id
        backup = consistent_backup(path, backup_dir=backup_dir)
        with Session(engine) as session:
            session.execute(text('BEGIN IMMEDIATE'))
            prior = _latest_receipt(session, user_id)
            if (_receipt_identity(prior) != identity or _file_hash(Path(backup['path'])) != backup['sha256']
                    or session.execute(text('PRAGMA user_version')).scalar_one() != 8):
                raise ValueError('release receipt changed after backup')
            created_at = datetime.now(timezone.utc)
            payload = {'version': 'selection-release-v1', 'user_id': user_id, 'action': 'read_only',
                'build': BUILD_VERSION, 'fill_id': fill_id, 'previous_id': prior.id, 'previous_digest': prior.digest,
                'created_at': created_at.isoformat(), 'reason': reason, 'backup': backup}
            row = SelectionRelease(user_id=user_id, action='read_only', build=BUILD_VERSION, fill_id=fill_id,
                previous_id=prior.id, reason=reason, created_at=created_at, payload_json=canonical_json(payload), digest=_hash(payload))
            session.add(row)
            session.flush()
            result = {'id': row.id, 'digest': row.digest, **payload}
            session.commit()
            return result
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--fill-id', type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--rollback-read-only', action='store_true')
    parser.add_argument('--reason')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args(argv)
    if (args.apply or args.rollback_read_only) and (not str(args.reason or '').strip() or args.backup_dir is None):
        parser.error('publication/rollback requires --reason and --backup-dir')
    if not args.rollback_read_only and args.fill_id is None:
        parser.error('--fill-id is required')
    try:
        path = args.db.resolve(strict=True)
        if args.apply or args.rollback_read_only:
            result = (publish_release(path, fill_id=args.fill_id, reason=args.reason, backup_dir=args.backup_dir)
                      if args.apply else rollback_release(path, reason=args.reason, backup_dir=args.backup_dir))
            print(canonical_json(result))
            return 0
        engine = create_engine(f'sqlite+pysqlite:///{path.as_uri()}?mode=ro&uri=true')
        try:
            with Session(engine) as session:
                report = release_report(session, fill_id=args.fill_id)
        finally:
            engine.dispose()
        print(canonical_json(report))
        return 0 if report['eligible'] else 2
    except (ValueError, OSError, sqlite3.DatabaseError, SQLAlchemyError) as error:
        print(canonical_json({'eligible': False, 'blockers': [str(error)]}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
