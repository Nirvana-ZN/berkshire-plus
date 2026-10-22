import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import text

from app.db import create_db_engine

FIXTURE = Path(__file__).parent / 'fixtures/selection/evidence.json'


def payload():
    return json.loads(FIXTURE.read_text(encoding='utf-8-sig'))[0]


def release_inventory():
    return {'version': 'release-universe-v1', 'market_date': '2026-09-16',
        'trading_dates': {'SZ': ['2026-09-15', '2026-09-16']},
        'securities': [{'code': '000001.SZ', 'name': 'synthetic', 'industry': '制造业',
            'security_status': {'exchange': 'SZ', 'listing_date': '2000-01-01', 'is_st': False,
                'is_delisting': False, 'trading_status': 'trading', 'is_suspended': False,
                'last_trade_date': '2026-09-16', 'industry_version': 'fixture-v1',
                'industry_as_of': '2026-09-16', 'security_source': 'retained-fixture', 'market_status_code': 1}}]}


def test_typed_release_inventory_and_calendar_cannot_inject_reserved_values():
    from market.evidence import parse_source_evidence
    data = {**payload(), 'code': '000001.SZ', 'kind': 'release_universe', 'period': '2026-09-16',
            'values': {}, 'release_universe': release_inventory()}
    row = parse_source_evidence(data)
    assert json.loads(row.values['_release_universe']) == release_inventory()
    calendar = {'calendar_version': 'fixture-calendar-v1', 'calendar_source_url': data['source_url'],
        'calendar_document_sha256': data['document_sha256'], 'calendar_coverage_end': '2026-12-31',
        'period_due_dates': {'2025': '2026-04-30', '2026H1': '2026-08-31'}}
    row = parse_source_evidence({**payload(), 'kind': 'disclosure_calendar', 'values': {},
                                 'disclosure_calendar': calendar})
    assert json.loads(row.values['_disclosure_calendar']) == calendar
    with pytest.raises(ValueError, match='reserved'):
        parse_source_evidence({**data, 'values': {'_release_universe': '{}'}})
    with pytest.raises(ValueError, match='anchor'):
        parse_source_evidence({**data, 'code': '600000.SH'})
    with pytest.raises(ValueError, match='calendar'):
        parse_source_evidence({**payload(), 'kind': 'disclosure_calendar', 'values': {},
            'disclosure_calendar': {**calendar, 'calendar_document_sha256': 'f' * 64}})


def test_normalized_json_preserves_missing_zero_and_report_location():
    from market.evidence import parse_source_evidence
    row = parse_source_evidence(payload())
    assert row.values['inventory'] == '0'
    assert row.values['restricted_cash'] is None
    assert row.values['_report_location'] == 'physical page 12; consolidated balance sheet'
    assert row.source_url == 'https://example.invalid/synthetic-report.pdf'
    assert row.source_updated_at == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert row.published_at == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert row.archive_verified is False
    assert row.status == 'untrusted'


def test_self_declared_archive_boolean_does_not_certify_archive():
    from market.evidence import parse_source_evidence
    data = {**payload(), 'archive_verified': True, 'trusted': True}
    with pytest.raises(ValueError, match='archive'):
        parse_source_evidence(data)


@pytest.mark.parametrize('value', [1.25, 'NaN', 'Infinity', '-Infinity', True])
def test_numeric_inputs_reject_floats_nonfinite_and_boolean(value):
    from market.evidence import parse_source_evidence
    data = {**payload(), 'values': {'inventory': value}}
    with pytest.raises(ValueError, match='decimal|Decimal'):
        parse_source_evidence(data)


def test_verified_manual_import_needs_a_review_receipt():
    from market.evidence import parse_source_evidence
    with pytest.raises(ValueError, match='verif|review'):
        parse_source_evidence({**payload(), 'status': 'verified'})


def test_provider_uses_only_audited_columns_and_never_claims_trust():
    from market.evidence import parse_source_evidence
    data = {**payload(), 'source': 'eastmoney', 'report': 'RPT_F10_FINANCE_GINCOME',
            'row': {'NOTICE_DATE': '2026-04-01 00:00:00', 'UPDATE_DATE': '2026-09-01 00:00:00',
                    'FE_INTEREST_EXPENSE': '125.50', 'INTEREST_EXPENSE': '999', 'FINANCE_EXPENSE': '777'}}
    row = parse_source_evidence(data)
    assert row.values['interest_expense'] == '125.5'
    assert row.values['related_party_funds'] is None
    assert row.values['restricted_cash'] is None
    assert row.status == 'untrusted'
    assert row.date_only is True
    assert row.source_updated_at != row.published_at


def test_provider_null_is_missing_and_update_date_cannot_supply_publication():
    from market.evidence import parse_source_evidence
    data = {**payload(), 'source': 'eastmoney', 'report': 'RPT_DMSK_FN_BALANCE',
            'row': {'NOTICE_DATE': '2026-04-01', 'ACCOUNTS_RECE': None, 'INVENTORY': None}}
    row = parse_source_evidence(data)
    assert row.values['inventory'] is None
    assert row.values['accounts_receivable'] is None
    assert row.status == 'missing'
    with pytest.raises(ValueError, match='NOTICE_DATE|publication'):
        parse_source_evidence({**data, 'row': {'UPDATE_DATE': '2026-09-01'}})


def test_cli_dry_run_has_no_database_side_effect_and_import_is_idempotent(tmp_path):
    target = tmp_path / 'import.db'
    args = [sys.executable, '-m', 'scripts.import_selection_evidence', '--db', str(target), '--input', str(FIXTURE)]
    result = subprocess.run([*args, '--dry-run'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not target.exists()
    for _ in range(2):
        result = subprocess.run(args, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    engine = create_db_engine(target)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM fundamental_evidence')).scalar_one() == 1
    engine.dispose()


def test_cli_invalid_batch_is_atomic(tmp_path):
    target = tmp_path / 'invalid.db'
    source = tmp_path / 'invalid.json'
    source.write_text(json.dumps([payload(), {**payload(), 'values': {'inventory': 'NaN'}}]), encoding='utf-8')
    result = subprocess.run([sys.executable, '-m', 'scripts.import_selection_evidence', '--db', str(target), '--input', str(source)], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'evidence import rejected: decimal value must be finite' in result.stderr
    assert not target.exists()


def test_raw_eastmoney_response_preserves_decimal_precision_and_hash():
    from hashlib import sha256
    from market.eastmoney import parse_selection_report_response
    raw = b'{"success":true,"result":{"data":[{"SECUCODE":"000001.SZ","REPORT_YEAR":"2025","REPORT_TYPE":"annual","NOTICE_DATE":"2026-04-01","INVENTORY":9007199254740993.01}]}}'
    rows = parse_selection_report_response(raw, code='000001.SZ', report='RPT_DMSK_FN_BALANCE',
        observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        source_url='https://example.invalid/provider', basis_id='synthetic-consolidated-cny')
    assert len(rows) == 1
    assert rows[0].values['inventory'] == '9007199254740993.01'
    assert rows[0].document_sha256 == sha256(raw).hexdigest()
    assert rows[0].period == '2025'
    assert rows[0].archive_verified is False
    assert rows[0].status == 'untrusted'


def test_raw_provider_failure_or_unidentified_row_is_not_empty_coverage():
    from market.eastmoney import parse_selection_report_response
    args = dict(code='000001.SZ', report='RPT_DMSK_FN_BALANCE',
                observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                source_url='https://example.invalid/provider', basis_id='synthetic-consolidated-cny')
    for raw in [b'{"success":false}', b'{"result":{}}', b'{"result":{"data":[{}]}}']:
        with pytest.raises(ValueError, match='provider|period|code'):
            parse_selection_report_response(raw, **args)


def test_explicit_human_review_is_preserved_without_archive_promotion():
    from market.evidence import parse_source_evidence
    review = {'method': 'human_review', 'reviewer': 'fixture-reviewer', 'reviewed_at': '2026-10-02T00:00:00+00:00'}
    row = parse_source_evidence({**payload(), 'status': 'verified', 'verification': review})
    assert row.status == 'verified'
    assert json.loads(row.values['_verification']) == review
    assert row.archive_verified is False


def test_cli_dry_run_existing_database_does_not_migrate_or_write(tmp_path):
    import sqlite3
    target = tmp_path / 'existing.db'
    with sqlite3.connect(target) as conn:
        conn.execute('CREATE TABLE sentinel (value TEXT)')
        conn.execute("INSERT INTO sentinel VALUES ('immutable history')")
    before = target.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'scripts.import_selection_evidence', '--db', str(target), '--input', str(FIXTURE), '--dry-run'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == before


def test_new_dictionary_mapping_is_not_sufficient_to_enable_trusted_calculations():
    from discovery.field_dictionary import SELECTION_FIELD_DICTIONARY
    from market.evidence import SELECTION_EXTENSION_FIELDS
    for name in SELECTION_EXTENSION_FIELDS:
        assert SELECTION_FIELD_DICTIONARY[name].trusted is False
    assert SELECTION_FIELD_DICTIONARY['restricted_cash'].field is None
    assert SELECTION_FIELD_DICTIONARY['related_party_funds'].field is None


def test_cli_append_creates_only_selection_tables_and_preserves_existing_rows(tmp_path):
    import sqlite3
    target = tmp_path / 'legacy.db'
    with sqlite3.connect(target) as conn:
        conn.execute('CREATE TABLE frozen_history (id INTEGER PRIMARY KEY, payload TEXT)')
        conn.execute("INSERT INTO frozen_history VALUES (1, 'original snapshot')")
        conn.execute("CREATE TRIGGER frozen_update BEFORE UPDATE ON frozen_history BEGIN SELECT RAISE(ABORT, 'immutable'); END")
    result = subprocess.run([sys.executable, '-m', 'scripts.import_selection_evidence', '--db', str(target), '--input', str(FIXTURE)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT * FROM frozen_history').fetchall() == [(1, 'original snapshot')]
        names = {item[0] for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names == {'frozen_history', 'fundamental_evidence', 'fundamental_coverage'}


def test_cli_rejects_invalid_supersession_without_partial_append(tmp_path):
    target = tmp_path / 'atomic.db'
    source = tmp_path / 'atomic.json'
    source.write_text(json.dumps([payload(), {**payload(), 'period': '2024', 'supersedes_id': 'no-such-evidence'}]), encoding='utf-8')
    result = subprocess.run([sys.executable, '-m', 'scripts.import_selection_evidence', '--db', str(target), '--input', str(source)], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'supersedes_id' in result.stderr
    engine = create_db_engine(target)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM fundamental_evidence')).scalar_one() == 0
    engine.dispose()


def test_selection_registry_does_not_expand_legacy_financial_persistence(tmp_path):
    from decimal import Decimal
    from sqlalchemy.orm import Session
    from app.db import init_db
    from app.models import Financial
    from discovery.field_dictionary import FIELD_DICTIONARY, SELECTION_FIELD_DICTIONARY
    from jobs.discovery_data import persist_discovery_financials
    from market.types import FinancialRow
    assert not (set(SELECTION_FIELD_DICTIONARY) & set(FIELD_DICTIONARY))
    engine = init_db(tmp_path / 'compatible.db')
    with Session(engine) as session:
        persist_discovery_financials(session, [FinancialRow(code='000001.SZ', period='2025', period_type='annual', revenue=Decimal('123.45'))])
        session.commit()
        assert session.get(Financial, ('000001.SZ', '2025', 'annual')).revenue == Decimal('123.45')
    engine.dispose()
