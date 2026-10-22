"""Real temporary SQLite release/backup behavior; no production data."""
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db import init_db


def retained_release_fill(tmp_path, monkeypatch, *, restricted='0', companies=1, omit_inventory=False,
                          industries=None, listing_date=None, missing_cash_codes=(), research_sources=False, db_path=None,
                          financial_overrides=None, profile_status='verified', profile_industry=None):
    """Real local importer -> fill worker -> sealed schema4 producer, all synthetic."""
    from dataclasses import fields, replace
    from datetime import date, datetime, timedelta, timezone
    from decimal import Decimal
    from hashlib import sha256
    import sys
    from app.models import DiscoveryDataCheckpoint, DiscoveryUniverseSecurity, DiscoveryFillTradingDate
    from discovery.field_dictionary import FIELD_DICTIONARY
    from discovery.fundamentals import fundamentals_from_payload
    from market.evidence import parse_source_evidence, parse_event_coverage
    from market.types import CurrentMarketCap, FinancialRow, ValuationPoint
    from market.eastmoney import FakeMarketClient
    from jobs.discovery_data import execute_discovery_data_fill, RequestPolicy
    from tests.test_discovery_data import _fill, _bundle, FakeClock, _security
    from tests.test_selection_v8 import company
    from scripts.import_selection_evidence import main as import_main
    now = datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
    at = now - timedelta(hours=1)
    raw_payloads = []
    def retained(code, kind, period, values, *, text=None, status='verified', **extra):
        content = text or json.dumps(values, sort_keys=True)
        digest = sha256(content.encode()).hexdigest()
        document = tmp_path / f'{digest}.txt'
        document.write_text(content, encoding='utf-8')
        data = dict(code=code, kind=kind, period=period, published_at=at.isoformat(), observed_at=at.isoformat(),
            source_url='https://example.invalid/synthetic-reviewed-source', document_sha256=digest,
            values=values, basis_id='fixture-consolidated-cny', status=status,
            source_document_path=str(document), source_excerpt=content, report_location='synthetic complete source',
            verification={'method': 'human_review', 'reviewer': 'synthetic reviewer', 'reviewed_at': at.isoformat()},
            excerpt_verification={'method': 'literal_utf8', 'document_sha256': digest, 'excerpt_sha256': digest,
                'report_location': 'synthetic complete source', 'reviewer': 'synthetic reviewer', 'reviewed_at': at.isoformat()}, **extra)
        if status != 'verified':
            for key in ('source_document_path', 'source_excerpt', 'excerpt_verification'):
                data.pop(key)
        raw_payloads.append(data)
        return parse_source_evidence(data)
    codes = [f'600{i:03d}.SH' for i in range(companies)]
    states = [replace(_security(code), industry=industries[index] if industries else '制造业',
                      **({'listing_date': listing_date} if listing_date is not None else {})) for index, code in enumerate(codes)]
    dates = ['2026-09-15', '2026-09-16']
    inventory = {'version': 'release-universe-v1', 'market_date': '2026-09-16', 'trading_dates': {'SH': dates},
        'securities': [{'code': state.code, 'name': state.name, 'industry': state.industry,
            'security_status': {'exchange': state.exchange, 'listing_date': state.listing_date,
                'is_st': state.is_st, 'is_delisting': state.is_delisting, 'trading_status': state.trading_status,
                'is_suspended': False, 'last_trade_date': state.last_trade_date,
                'industry_version': state.industry_version, 'industry_as_of': state.industry_as_of,
                'security_source': state.source, 'market_status_code': state.market_status_code}}
            for state in states]}
    inventory_record = None if omit_inventory else retained(codes[0], 'release_universe', '2026-09-16', {},
        text=json.dumps(inventory, sort_keys=True), release_universe=inventory)
    sample = company(cap='200', restricted=restricted)
    original = fundamentals_from_payload(sample.fields, code=sample.code)
    calendar_text = json.dumps({'2025': '2026-04-30', '2026H1': '2026-08-31'})
    calendar_hash = sha256(calendar_text.encode()).hexdigest()
    descriptor = {'calendar_version': 'synthetic-release-calendar-v1',
        'calendar_source_url': 'https://example.invalid/synthetic-reviewed-source',
        'calendar_document_sha256': calendar_hash, 'calendar_coverage_end': '2026-12-31',
        'period_due_dates': json.loads(calendar_text)}
    policy = {'version': 'operating-v1', 'profit_drop': '-0.15', 'calendar_status': 'verified',
              'calendar_complete': True, **descriptor}
    bundles = {}
    class SuppliedMarket(FakeMarketClient):
        def fetch_discovery_data(self, code, *, selection_fundamentals=False, selection_source_dir=None, **kwargs):
            assert selection_fundamentals is True
            return super().fetch_discovery_data(code, **kwargs)
    allowed = {item.name for item in fields(FinancialRow)}
    for state in states:
        records, financials = [], []
        for source in original.annuals + original.interims:
            values = {key: value for key, value in source.values.items() if not key.startswith('_')}
            values.setdefault('capex', '10')
            values.update(accounts_receivable='5', contract_assets='0', inventory='3', related_party_funds='0',
                          total_equity_including_minority='80', interest_expense='4',
                          restricted_cash=None if state.code in missing_cash_codes else restricted)
            values.update(financial_overrides or {})
            records.append(retained(state.code, 'financial', source.period, values))
            if len(source.period) == 4:
                financials.append(FinancialRow(code=state.code, period=source.period, period_type='annual',
                    source_as_of='2026-09-16', **{key: Decimal(value) for key, value in values.items()
                        if key in allowed and key in FIELD_DICTIONARY and FIELD_DICTIONARY[key].trusted and value is not None}))
        profile_label = profile_industry if profile_industry is not None else state.industry
        records.append(retained(state.code, 'company_profile', '2026-09-16', {}, text=profile_label,
                                industry=profile_label, status=profile_status))
        records.append(retained(state.code, 'disclosure_calendar', '2026', {}, text=calendar_text, disclosure_calendar=descriptor))
        quote = retained(state.code, 'market_snapshot', '2026-09-16', {'current_mcap': '200', 'price': '10'})
        records.append(quote)
        if research_sources:
            records.append(retained(state.code, 'share_snapshot', '2026-09-17', {'shares': '10'}))
            records.append(retained(state.code, 'market_snapshot', '2026-09-17', {'price': '10'}))
        receipt = retained(state.code, 'capital_event_coverage', '2026-09-16',
            {'scope': 'all_capital_actions', 'start': '2020-12-31', 'end': '2025-12-31', 'complete': 'true', 'evidence_ids': '[]'})
        records.append(receipt)
        if inventory_record is not None and state.code == codes[0]:
            records.append(inventory_record)
        points = tuple(ValuationPoint(state.code, row['month'], row['trade_date'], Decimal(row['pe']),
            Decimal(row['pb']), Decimal(row['price'])) for row in sample.fields['valuation_history'])
        points = (*points[:-1], replace(points[-1], trade_date='2026-09-16'))
        bundles[state.code] = replace(_bundle(state.code), security_state=state, financial_rows=tuple(financials),
            valuation_points=points, source_as_of=replace(_bundle(state.code).source_as_of,
                security='2026-09-16', valuation='2026-09-16', financial='2026-09-16'),
            evidence_records=tuple(records), coverage=(parse_event_coverage(receipt, ()),),
            current_market_cap=CurrentMarketCap(state.code, Decimal('200'), date(2026, 9, 16), at,
                'retained-reviewed-quote', quote.source_url, quote.document_sha256, 'verified', None),
            operating_policy=policy)
    path = db_path or tmp_path / 'release.db'
    engine = init_db(path)
    source_path = tmp_path / 'import.json'
    source_path.write_text(json.dumps(raw_payloads, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['import_selection_evidence', '--db', str(path), '--input', str(source_path)])
    assert import_main() == 0
    with Session(engine) as session:
        fill = _fill(session, codes, now=now, with_snapshot=False)
        for state in states:
            session.add(DiscoveryUniverseSecurity(fill_id=fill.id, code=state.code, name=state.name,
                industry=state.industry, exchange=state.exchange, listing_date=state.listing_date,
                is_st=state.is_st, is_delisting=state.is_delisting, trading_status=state.trading_status,
                is_suspended=False, last_trade_date=state.last_trade_date, industry_version=state.industry_version,
                source_as_of=state.industry_as_of, provenance=state.source, market_status_code=state.market_status_code,
                snapshot_at=now))
        session.add_all(DiscoveryFillTradingDate(fill_id=fill.id, trade_date=day) for day in dates)
        session.flush()
        fill.snapshot_status = 'ready'
        session.commit()
        execute_discovery_data_fill(session, SuppliedMarket(discovery_data=bundles), fill.id,
            policy=RequestPolicy(requests_per_second=100, max_attempts=1, daily_budget=1000, max_runtime_seconds=60),
            clock=FakeClock(now), selection_fundamentals=True, operating_policy=policy)
        fill_id = fill.id
        assert fill.status == 'done', fill.last_error
        checkpoints = session.query(DiscoveryDataCheckpoint).all()
        assert all(row.status == 'done' for row in checkpoints), [(row.status, row.error) for row in checkpoints]
    return engine, fill_id



def seed_published_release(db_path):
    """Current API fixtures use real imported/frozen/publication evidence."""
    from datetime import datetime, timezone
    import pytest
    from scripts.release_selection_v8 import publish_release
    directory = db_path.parent / (db_path.stem + '-release-fixture')
    directory.mkdir()
    class FixtureReleaseClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
    with pytest.MonkeyPatch.context() as patch:
        engine, fill_id = retained_release_fill(directory, patch, db_path=db_path)
        engine.dispose()
        patch.setattr('scripts.release_selection_v8.datetime', FixtureReleaseClock)
        return publish_release(db_path, fill_id=fill_id, reason='synthetic API fixture', backup_dir=directory / 'backups')

def test_real_import_fill_qualified_report_and_zero_is_trusted(tmp_path, monkeypatch):
    from scripts.release_selection_v8 import release_report
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch)
    try:
        with Session(engine) as session:
            report = release_report(session, fill_id=fill_id)
        assert report['eligible'], [(track, key, item) for track, values in report['coverage']['tracks'].items() for key,item in values.items() if item['status']=='insufficient']
        assert report['coverage']['common']['applicable'] == report['coverage']['common']['trusted'] == 1
        assert report['coverage']['sources']['restricted_cash']['trusted'] == 1
        assert report['coverage']['branches']['bank']['status'] == 'not_applicable'
        assert report['coverage']['branches']['bank']['coverage'] is None
    finally:
        engine.dispose()


def test_missing_retained_inventory_or_restricted_cash_blocks_release(tmp_path, monkeypatch):
    from scripts.release_selection_v8 import release_report
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch, restricted=None, omit_inventory=True)
    try:
        with Session(engine) as session:
            report = release_report(session, fill_id=fill_id)
        assert report['eligible'] is False
        assert 'retained_universe_missing_or_mismatched' in report['blockers']
        assert report['coverage']['sources']['restricted_cash']['trusted'] == 0
    finally:
        engine.dispose()


def test_retired_v7_cannot_recompute_and_unreleased_v8_cannot_activate(tmp_path):
    from discovery.builds import AlgorithmUnavailableError, validate_recomputation
    from discovery.rulers import activate
    from tests.test_selection_v8 import candidate
    with pytest.raises(AlgorithmUnavailableError):
        validate_recomputation('discovery-v7', snapshot_complete=True, ruler_payload_complete=True,
            persisted_input_fingerprint='same', requested_input_fingerprint='same',
            persisted_ruler_version_id=1, requested_ruler_version_id=1)
    engine = init_db(tmp_path / 'unreleased.db')
    try:
        with Session(engine) as session:
            with pytest.raises(ValueError, match='release|publication'):
                activate(session, candidate(), 'not a release')
    finally:
        engine.dispose()


def test_consistent_backup_preserves_committed_wal_and_actual_inventory(tmp_path):
    from scripts.release_selection_v8 import consistent_backup
    path = tmp_path / 'source.db'
    engine = init_db(path)
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA journal_mode=WAL')
        conn.commit()
        conn.exec_driver_sql("INSERT INTO settings(key,value) VALUES ('wal-receipt','retained')")
        conn.commit()
        result = consistent_backup(path, backup_dir=tmp_path / 'backups')
        backup = Path(result['path'])
        assert backup.parent == tmp_path / 'backups'
        with sqlite3.connect(backup) as copied:
            assert copied.execute("SELECT value FROM settings WHERE key='wal-receipt'").fetchone()[0] == 'retained'
            assert copied.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert copied.execute('PRAGMA foreign_key_check').fetchall() == []
            actual = {name: copied.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                      for (name,) in copied.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            assert result['table_counts'] == actual
            assert {'tracking_requests', 'portfolio_exposure_versions', 'portfolio_exposure_selections',
                    'portfolio_review_exposure_captures', 'selection_releases'} <= actual.keys()
        from hashlib import sha256
        assert result['sha256'] == sha256(backup.read_bytes()).hexdigest()
    engine.dispose()


def test_backup_rejects_source_alias_outside_and_existing_target(tmp_path):
    from scripts.release_selection_v8 import consistent_backup
    path = tmp_path / 'source.db'
    init_db(path).dispose()
    backup_dir = tmp_path / 'backups'
    before = path.read_bytes()
    for target in (path, tmp_path / 'outside.db', backup_dir / '..' / 'escape.db'):
        with pytest.raises(ValueError):
            consistent_backup(path, backup_dir=backup_dir, destination=target)
    assert path.read_bytes() == before


def test_default_cli_is_read_only_even_when_coverage_is_blocked(tmp_path, capsys):
    from scripts.release_selection_v8 import main
    path = tmp_path / 'empty.db'
    init_db(path).dispose()
    before = path.read_bytes()
    assert main(['--db', str(path), '--fill-id', '1']) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['eligible'] is False
    assert report['acceptance']['software_suite'] == 'external_gate_not_certified'
    assert path.read_bytes() == before
    assert not (tmp_path / 'backups').exists()


def test_exact_fill_missing_never_falls_back(tmp_path):
    from discovery.snapshot import build_production_snapshot
    engine = init_db(tmp_path / 'exact.db')
    try:
        with Session(engine) as session:
            for bad in (True, 0, -1, '1'):
                with pytest.raises(ValueError, match='fill_id'):
                    build_production_snapshot(session, fill_id=bad)
            with pytest.raises(ValueError, match='fill'):
                build_production_snapshot(session, fill_id=999)
    finally:
        engine.dispose()


def test_publish_three_rulers_atomically_and_append_read_only_rollback(tmp_path, monkeypatch):
    from scripts.release_selection_v8 import publish_release, rollback_release
    from app.models import RulerVersion
    from app.selection_models import SelectionRelease
    from discovery.rulers import latest, require_ready, RulerNotReadyError, activate
    from sqlalchemy import select
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch)
    path = tmp_path / 'release.db'
    first = publish_release(path, fill_id=fill_id, reason='synthetic qualification', backup_dir=tmp_path / 'backups')
    with Session(engine) as session:
        receipt = session.get(SelectionRelease, first['id'])
        retained = receipt.payload_json
        ids = {track: require_ready(session, track)[0].id for track in first['rulers']}
        assert set(ids.values()) == {item['id'] for item in first['rulers'].values()}
        for track in ids:
            assert latest(session, track, user_id=2) is None
        assert len(session.scalars(select(RulerVersion).where(RulerVersion.scope.like('discovery:%'))).all()) == 3
    repeated = publish_release(path, fill_id=fill_id, reason='same synthetic qualification', backup_dir=tmp_path / 'backups')
    assert repeated['rulers'] == first['rulers']
    assert repeated['previous_id'] == first['id']
    with pytest.raises(ValueError, match='local owner'):
        publish_release(path, fill_id=fill_id, reason='foreign', backup_dir=tmp_path / 'backups', user_id=2)
    rollback = rollback_release(path, reason='synthetic rollback', backup_dir=tmp_path / 'backups')
    assert rollback['previous_id'] == repeated['id']
    with Session(engine) as session:
        assert session.get(SelectionRelease, first['id']).payload_json == retained
        for track, ruler_id in ids.items():
            assert latest(session, track) is None
            with pytest.raises(RulerNotReadyError):
                require_ready(session, track)
            with pytest.raises(ValueError, match='release|publication'):
                activate(session, json.loads(session.get(RulerVersion, ruler_id).payload_json), 'no bypass')
        with pytest.raises(Exception, match='append-only|immutable'):
            session.execute(__import__('sqlalchemy').text('DELETE FROM selection_releases'))
        session.rollback()
    engine.dispose()


def test_second_track_failure_rolls_back_every_append_and_authorization(tmp_path, monkeypatch):
    from scripts.release_selection_v8 import publish_release
    from discovery.rulers import activate, _release_sessions
    from tests.test_selection_v8 import candidate
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TRIGGER reject_second_release BEFORE INSERT ON ruler_versions "
            "WHEN NEW.scope='discovery:deep_value' BEGIN SELECT RAISE(ABORT,'synthetic second track failure'); END")
        before = conn.exec_driver_sql('SELECT count(*) FROM ruler_versions').scalar_one()
    with pytest.raises(Exception, match='synthetic second track failure'):
        publish_release(tmp_path / 'release.db', fill_id=fill_id, reason='must roll back', backup_dir=tmp_path / 'backups')
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT count(*) FROM ruler_versions').scalar_one() == before
        assert conn.exec_driver_sql('SELECT count(*) FROM selection_releases').scalar_one() == 0
    assert not _release_sessions
    with Session(engine) as session:
        with pytest.raises(ValueError, match='release|publication'):
            activate(session, candidate(), 'authorization cannot escape')
    engine.dispose()


def test_source_proof_changed_after_backup_invalidates_publication(tmp_path, monkeypatch):
    import scripts.release_selection_v8 as release
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch)
    original_backup = release.consistent_backup
    def changed_after_backup(*args, **kwargs):
        result = original_backup(*args, **kwargs)
        for source in tmp_path.glob('*.txt'):
            source.write_text('changed retained bytes', encoding='utf-8')
        return result
    monkeypatch.setattr(release, 'consistent_backup', changed_after_backup)
    with pytest.raises(ValueError, match='changed|stale'):
        release.publish_release(tmp_path / 'release.db', fill_id=fill_id, reason='race', backup_dir=tmp_path / 'backups')
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT count(*) FROM selection_releases').scalar_one() == 0
    engine.dispose()


def test_industry_minimum_cannot_hide_inside_global_ninety_percent(tmp_path, monkeypatch):
    from scripts.release_selection_v8 import release_report
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch, companies=10,
        industries=['制造业'] * 9 + ['软件服务'], missing_cash_codes=('600009.SH',))
    with Session(engine) as session:
        report = release_report(session, fill_id=fill_id)
    metric = report['coverage']['sources']['restricted_cash']
    assert (metric['trusted'], metric['applicable'], metric['coverage'], metric['status']) == (9, 10, '0.9', 'ready')
    assert report['coverage']['industries']['软件服务']['metrics']['source:restricted_cash']['trusted'] == 0
    assert report['eligible'] is False
    assert 'coverage_insufficient:industry:软件服务' in report['blockers']
    engine.dispose()


def test_metric_specific_listing_age_and_zero_denominator(tmp_path, monkeypatch):
    from scripts.release_selection_v8 import release_report
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch, listing_date='2021-01-01')
    with Session(engine) as session:
        report = release_report(session, fill_id=fill_id)
    assert report['eligible'], report['blockers']
    metrics = report['coverage']['tracks']['garp']
    assert metrics['deducted_growth_window']['applicable'] == 1
    assert metrics['roic_gross_5y']['applicable'] == 0
    assert metrics['roic_gross_5y']['coverage'] is None
    assert metrics['roic_gross_5y']['status'] == 'not_applicable'
    engine.dispose()


def _queued_release_run(session, receipt, *, common_queued=False):
    from datetime import datetime
    from app.discovery_models import DiscoveryRun, DiscoveryCommonStage, DiscoveryCommonResult, DiscoveryStage
    from discovery.constants import china_week_key
    from discovery.snapshot import build_production_snapshot, freeze_inputs, freeze_discovery_ruler_payloads
    snapshot = build_production_snapshot(session, fill_id=receipt['fill_id'])
    at = datetime.fromisoformat(receipt['created_at'])
    ids = {track: value['id'] for track, value in receipt['rulers'].items()}
    run = DiscoveryRun(user_id=1, week_key=china_week_key(at), current=True, status='running',
        created_at=at, updated_at=at, algorithm_version='discovery-v8',
        data_version=snapshot.versions.data_version, financial_version=snapshot.versions.financial_version,
        valuation_version=snapshot.versions.valuation_version,
        quality_discount_ruler_version_id=ids['quality_discount'], deep_value_ruler_version_id=ids['deep_value'],
        garp_ruler_version_id=ids['garp'], ruler_payloads_json=freeze_discovery_ruler_payloads(session, ids, 'discovery-v8'))
    session.add(run)
    session.flush()
    freeze_inputs(session, run, snapshot.companies, snapshot.versions, snapshot.trading_dates, snapshot_at=snapshot.snapshot_at)
    common = DiscoveryCommonStage(run_id=run.id, attempt=1, status='confirmed', total_count=1, pass_count=1,
        input_fingerprint=run.input_fingerprint, algorithm_version=run.algorithm_version,
        created_at=at, updated_at=at, confirmed_at=at)
    session.add(common)
    session.flush()
    if common_queued:
        common.status = 'queued'
        common.total_count = common.pass_count = 0
        common.confirmed_at = None
        session.commit()
        return run.id, common.id, at
    session.add(DiscoveryCommonResult(run_id=run.id, attempt=1, code='600000.SH', verdict='pass',
        reason=None, metrics_json='{}', created_at=at))
    gate = DiscoveryStage(run_id=run.id, track='quality_discount', stage='track_gate', attempt=1, status='queued',
        common_stage_id=common.id, input_fingerprint=run.input_fingerprint, algorithm_version=run.algorithm_version,
        ruler_version_id=ids['quality_discount'], created_at=at, updated_at=at)
    session.add(gate)
    session.commit()
    return run.id, gate.id, at


@pytest.mark.parametrize('during_callback', [False, True])
def test_rollback_blocks_queued_and_inflight_publication(tmp_path, monkeypatch, during_callback):
    from sqlalchemy import select, func
    from app.discovery_models import DiscoveryRun, DiscoveryStage, DiscoveryResult, DiscoveryInputSnapshot
    from app.selection_models import StrategyCohort
    from scripts.release_selection_v8 import publish_release, rollback_release
    from jobs.discovery import drain_run_queue, builtin_track_executor
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch)
    path = tmp_path / 'release.db'
    receipt = publish_release(path, fill_id=fill_id, reason='execution test', backup_dir=tmp_path / 'backups')
    with Session(engine) as session:
        run_id, stage_id, at = _queued_release_run(session, receipt)
        frozen = tuple(session.execute(select(*DiscoveryInputSnapshot.__table__.columns)).all())
        calls = []
        def execute(context):
            calls.append(context.stage_id)
            outcome = builtin_track_executor(context)
            rollback_release(path, reason='during calculation', backup_dir=tmp_path / 'backups')
            return outcome
        if not during_callback:
            rollback_release(path, reason='before queue drain', backup_dir=tmp_path / 'backups')
        outcome = drain_run_queue(session, run_id, track_executor=execute, now_provider=lambda: at)
        assert calls == ([stage_id] if during_callback else [])
        assert outcome.executed == 0
        assert session.scalar(select(func.count()).select_from(DiscoveryResult)) == 0
        assert session.scalar(select(func.count()).select_from(StrategyCohort)) == 0
        assert tuple(session.execute(select(*DiscoveryInputSnapshot.__table__.columns)).all()) == frozen
        interrupted = session.get(DiscoveryStage, stage_id)
        assert interrupted.status == ('failed' if during_callback else 'queued')
        if during_callback:
            assert interrupted.failure_kind == 'transient'
            assert 'publication_authority_lost' in interrupted.error
    engine.dispose()


def test_execution_authority_requires_original_owned_exact_receipt(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from app.discovery_models import DiscoveryRun
    from app.selection_models import SelectionRelease
    from scripts.release_selection_v8 import publish_release
    from discovery.rulers import execution_authorized
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch)
    path = tmp_path / 'release.db'
    first = publish_release(path, fill_id=fill_id, reason='original', backup_dir=tmp_path / 'backups')
    with Session(engine) as session:
        run_id, _, at = _queued_release_run(session, first)
        run = session.get(DiscoveryRun, run_id)
        assert execution_authorized(session, run)
        values = {column.name: getattr(run, column.name) for column in run.__table__.columns}
        for changes in ({'user_id': 2}, {'created_at': at - timedelta(seconds=1)},
                        {'garp_ruler_version_id': 999}, {'ruler_payloads_json': '{}'}):
            assert not execution_authorized(session, SimpleNamespace(**(values | changes)))
    publish_release(path, fill_id=fill_id, reason='later publication', backup_dir=tmp_path / 'backups')
    with Session(engine) as session:
        assert execution_authorized(session, session.get(DiscoveryRun, run_id))
        session.add(SelectionRelease(user_id=1, action='publish', build='discovery-v8', fill_id=fill_id,
            previous_id=None, reason='corrupt', created_at=at, payload_json='{}', digest='corrupt'))
        session.commit()
        assert not execution_authorized(session, session.get(DiscoveryRun, run_id))
    blank = init_db(tmp_path / 'missing.db')
    with Session(blank) as session:
        assert not execution_authorized(session, SimpleNamespace(**values))
    blank.dispose()
    engine.dispose()


def test_bank_readiness_uses_each_existing_proxy_window():
    from discovery.fundamentals import freeze_fundamentals
    from tests.selection_helpers import evidence
    from datetime import datetime, timezone
    CUTOFF = datetime(2026, 10, 22, tzinfo=timezone.utc)
    from tests.test_selection_v8 import candidate
    from scripts.release_selection_v8 import _source_metrics
    records = tuple(evidence(str(year), roe='12', **({'net_assets': '100', 'npl': '1',
        'capital_adequacy': '15', 'core_tier1_capital_adequacy': '12'} if year == 2025 else {}))
        for year in range(2021, 2026))
    snapshot = freeze_fundamentals(records, (), (), code=records[0].code, cutoff_at=CUTOFF, current_mcap=None)
    metrics = _source_metrics(snapshot, candidate('quality_discount'), 'bank')
    assert set(metrics) == {'bank_roe', 'net_assets', 'bank_capital', 'bank_npl'}
    assert all(row.status == 'verified' for row in metrics.values())
    assert _source_metrics(snapshot, candidate('deep_value'), 'bank')['bank_npl'].status == 'missing'


@pytest.mark.parametrize('industry', ['银行', '保险', '证券'])
def test_verified_financial_classification_qualifies_actual_fill(tmp_path, monkeypatch, industry):
    from discovery.fundamentals import capital_return, fundamentals_from_payload
    from discovery.snapshot import build_production_snapshot
    from scripts.release_selection_v8 import release_report
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch, industries=[industry])
    with Session(engine) as session:
        company = build_production_snapshot(session, fill_id=fill_id).companies[0]
        assert capital_return(fundamentals_from_payload(company.fields, code=company.code)).status == 'not_applicable'
        report = release_report(session, fill_id=fill_id)
        assert report['eligible'], report['blockers']
        assert report['coverage']['sources']['restricted_cash']['status'] == 'not_applicable'
        if industry == '银行':
            for track in ('quality_discount', 'deep_value'):
                assert report['coverage']['tracks'][track]
                assert all(row['status'] == 'ready' for row in report['coverage']['tracks'][track].values())
    engine.dispose()


@pytest.mark.parametrize('changes', [
    {'profile_status': 'untrusted'}, {'profile_industry': '制造业'}, {'profile_industry': '-'},
])
def test_financial_classification_still_requires_exact_trusted_identity(tmp_path, monkeypatch, changes):
    from scripts.release_selection_v8 import release_report
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch, industries=['银行'], **changes)
    with Session(engine) as session:
        report = release_report(session, fill_id=fill_id)
        assert not report['eligible']
        assert '600000.SH:unverified_classification_denominator' in report['blockers']
    engine.dispose()


def test_financial_classification_requires_retained_source_proof(tmp_path, monkeypatch):
    from hashlib import sha256
    from scripts.release_selection_v8 import release_report
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch, industries=['银行'])
    document = tmp_path / (sha256('银行'.encode()).hexdigest() + '.txt')
    document.write_text('changed retained source', encoding='utf-8')
    with Session(engine) as session:
        report = release_report(session, fill_id=fill_id)
        assert not report['eligible']
        assert '600000.SH:unverified_classification_denominator' in report['blockers']
    engine.dispose()


@pytest.mark.parametrize('overrides,expected_metric,expected_coverage', [
    ({'ebit': None}, 'verified', 'ready'),
    ({'interest_expense': None}, 'verified', 'ready'),
    ({'ebit': None, 'interest_expense': None}, 'missing', 'insufficient'),
    ({'ebit': '-20'}, 'verified', 'ready'),
    ({'interest_debt': '-1'}, 'untrusted', 'ready'),
])
def test_roic_readiness_shares_actual_source_alternatives_not_economic_thresholds(
    tmp_path, monkeypatch, overrides, expected_metric, expected_coverage,
):
    from discovery.fundamentals import capital_return, fundamentals_from_payload
    from discovery.snapshot import build_production_snapshot
    from scripts.release_selection_v8 import release_report
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch, financial_overrides=overrides)
    with Session(engine) as session:
        company = build_production_snapshot(session, fill_id=fill_id).companies[0]
        metric = capital_return(fundamentals_from_payload(company.fields, code=company.code))
        assert metric.status == expected_metric
        report = release_report(session, fill_id=fill_id)
        assert report['coverage']['tracks']['garp']['roic_gross_5y']['status'] == expected_coverage
        if overrides == {'ebit': None}:
            assert str(metric.value) == '0.15'
            assert report['eligible'], report['blockers']
        if overrides == {'ebit': '-20'}:
            assert metric.value < 0
    engine.dispose()


@pytest.mark.parametrize('stage_kind', ['common', 'track'])
@pytest.mark.parametrize('replacement', ['lease', 'attempt', 'week', 'run'])
def test_authority_loss_recovery_cannot_overwrite_changed_owner_attempt_or_history(tmp_path, monkeypatch, replacement, stage_kind):
    from datetime import timedelta
    from sqlalchemy import select, func
    from app.discovery_models import (DiscoveryRun, DiscoveryStage, DiscoveryResult, DiscoveryInputSnapshot,
                                      DiscoveryCommonStage, DiscoveryCommonResult)
    from app.models import JobLock
    from app.selection_models import StrategyCohort
    from jobs import discovery as worker
    from scripts.release_selection_v8 import publish_release, rollback_release
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch)
    path = tmp_path / 'release.db'
    receipt = publish_release(path, fill_id=fill_id, reason='recovery race', backup_dir=tmp_path / 'backups')
    with Session(engine) as session:
        run_id, stage_id, at = _queued_release_run(session, receipt, common_queued=stage_kind == 'common')
        model = DiscoveryCommonStage if stage_kind == 'common' else DiscoveryStage
        frozen = tuple(session.execute(select(*DiscoveryInputSnapshot.__table__.columns)).all())
        now, captured = [at], {}
        original = worker._recover_authority_loss
        def race_before_recovery(target, claim, clock):
            # Real output transaction has already rolled back. Inject a competing
            # owner/attempt/history transition before the fresh writer lock.
            with Session(engine) as competing:
                assert competing.scalar(select(func.count()).select_from(DiscoveryResult)) == 0
                assert competing.scalar(select(func.count()).select_from(StrategyCohort)) == 0
                if stage_kind == 'common':
                    assert competing.scalar(select(func.count()).select_from(DiscoveryCommonResult)) == 0
                stage = competing.get(model, stage_id)
                if replacement == 'lease':
                    competing.get(JobLock, 1).owner_token = 'replacement-owner'
                elif replacement == 'attempt':
                    stage.status, stage.failure_kind = 'failed', 'transient'
                    stage.error, stage.superseded_at = 'replacement attempt owns recovery', at
                    values = dict(run_id=run_id, attempt=2, status='running', input_fingerprint=stage.input_fingerprint,
                        algorithm_version=stage.algorithm_version, retry_of_stage_id=stage_id, created_at=at, updated_at=at)
                    if stage_kind == 'track':
                        values.update(track=stage.track, stage=stage.stage, common_stage_id=stage.common_stage_id,
                                      ruler_version_id=stage.ruler_version_id)
                    competing.add(model(**values))
                elif replacement == 'week':
                    now[0] += timedelta(days=7)
                else:
                    competing.get(DiscoveryRun, run_id).current = False
                competing.commit()
                captured['stage'] = tuple(getattr(stage, col.name) for col in stage.__table__.columns)
            original(target, claim, clock)
        monkeypatch.setattr(worker, '_recover_authority_loss', race_before_recovery)
        def interrupted(context):
            outcome = worker.builtin_track_executor(context)
            rollback_release(path, reason='race interruption', backup_dir=tmp_path / 'backups')
            return outcome
        original_common = worker.classify_common
        def interrupted_common(company, dates):
            outcome = original_common(company, dates)
            rollback_release(path, reason='common race interruption', backup_dir=tmp_path / 'backups')
            return outcome
        if stage_kind == 'common':
            monkeypatch.setattr(worker, 'classify_common', interrupted_common)
        if replacement == 'lease':
            with pytest.raises(worker.DiscoveryLockLostError):
                worker.drain_run_queue(session, run_id, track_executor=interrupted, now_provider=lambda: now[0])
        else:
            assert worker.drain_run_queue(session, run_id, track_executor=interrupted,
                                          now_provider=lambda: now[0]).executed == 0
        session.expire_all()
        stage = session.get(model, stage_id)
        assert tuple(getattr(stage, col.name) for col in stage.__table__.columns) == captured['stage']
        assert session.scalar(select(func.count()).select_from(DiscoveryResult)) == 0
        assert session.scalar(select(func.count()).select_from(StrategyCohort)) == 0
        if stage_kind == 'common':
            assert session.scalar(select(func.count()).select_from(DiscoveryCommonResult)) == 0
        assert tuple(session.execute(select(*DiscoveryInputSnapshot.__table__.columns)).all()) == frozen
        if replacement == 'lease':
            assert session.get(JobLock, 1).owner_token == 'replacement-owner'
        if replacement == 'attempt':
            newer = session.scalar(select(model).where(model.attempt == 2))
            assert newer.status == 'running' and newer.error is None
    engine.dispose()


@pytest.mark.parametrize('stage_kind', ['common', 'track'])
def test_live_republication_retries_interrupted_attempt_without_restart(tmp_path, monkeypatch, stage_kind):
    from datetime import datetime
    from fastapi.testclient import TestClient
    from sqlalchemy import select, func
    from app.main import create_app
    from app.discovery_models import (DiscoveryStage, DiscoveryRun, DiscoveryResult, DiscoveryInputSnapshot,
                                      DiscoveryCommonStage, DiscoveryCommonResult)
    from app.selection_models import StrategyCohort
    from market.quotes import FakeQuoteClient
    from jobs.discovery import drain_run_queue, builtin_track_executor
    from jobs import discovery as worker
    from scripts.release_selection_v8 import publish_release, rollback_release
    engine, fill_id = retained_release_fill(tmp_path, monkeypatch)
    path = tmp_path / 'release.db'
    receipt = publish_release(path, fill_id=fill_id, reason='original', backup_dir=tmp_path / 'backups')
    at = datetime.fromisoformat(receipt['created_at'])
    app = create_app(db_path=path, quote_client=FakeQuoteClient(), env_path=tmp_path / '.env',
                     allow_sync=True, discovery_clock=lambda: at)
    with TestClient(app) as client:
        with Session(engine) as session:
            run_id, stage_id, _ = _queued_release_run(session, receipt, common_queued=stage_kind == 'common')
            frozen = tuple(session.execute(select(*DiscoveryInputSnapshot.__table__.columns)).all())
            ruler_payloads = session.get(DiscoveryRun, run_id).ruler_payloads_json
            def interrupted(context):
                result = builtin_track_executor(context)
                rollback_release(path, reason='live interruption', backup_dir=tmp_path / 'backups')
                return result
            original = worker.classify_common
            def interrupted_common(company, dates):
                result = original(company, dates)
                rollback_release(path, reason='common interruption', backup_dir=tmp_path / 'backups')
                return result
            with monkeypatch.context() as patch:
                if stage_kind == 'common':
                    patch.setattr(worker, 'classify_common', interrupted_common)
                assert drain_run_queue(session, run_id, track_executor=interrupted, now_provider=lambda: at).executed == 0
            assert session.scalar(select(func.count()).select_from(DiscoveryResult)) == 0
            assert session.scalar(select(func.count()).select_from(StrategyCohort)) == 0
            model = DiscoveryCommonStage if stage_kind == 'common' else DiscoveryStage
            old = session.get(model, stage_id)
            assert old.status == 'failed' and old.failure_kind == 'transient'
            assert 'publication_authority_lost' in old.error
            if stage_kind == 'common':
                assert session.scalar(select(func.count()).select_from(DiscoveryCommonResult)) == 0
        publish_release(path, fill_id=fill_id, reason='resume original authority', backup_dir=tmp_path / 'backups')
        suffix = 'stages/safety/retry' if stage_kind == 'common' else 'tracks/quality_discount/stages/track_gate/retry'
        response = client.post(f'/api/discoveries/{run_id}/{suffix}', json={'sync': True})
        assert response.status_code == 200, response.text
        with Session(engine) as session:
            statement = select(model).where(model.run_id == run_id)
            if stage_kind == 'track':
                statement = statement.where(model.track == 'quality_discount', model.stage == 'track_gate')
            attempts = session.scalars(statement.order_by(model.attempt)).all()
            assert len(attempts) == 2
            assert attempts[0].status == 'failed' and attempts[0].failure_kind == 'transient'
            assert attempts[1].retry_of_stage_id == stage_id and attempts[1].status == 'confirmed'
            assert session.scalar(select(func.count()).select_from(DiscoveryResult)) > 0
            if stage_kind == 'track':
                assert session.scalar(select(func.count()).select_from(DiscoveryResult).where(
                    DiscoveryResult.run_id == run_id, DiscoveryResult.track == 'quality_discount',
                    DiscoveryResult.stage == 'track_gate', DiscoveryResult.attempt == 1)) == 0
            else:
                assert session.scalar(select(func.count()).select_from(DiscoveryCommonResult).where(
                    DiscoveryCommonResult.attempt == 1)) == 0
            assert tuple(session.execute(select(*DiscoveryInputSnapshot.__table__.columns)).all()) == frozen
            assert session.get(DiscoveryRun, run_id).ruler_payloads_json == ruler_payloads
    engine.dispose()
