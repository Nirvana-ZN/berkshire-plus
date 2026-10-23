"""Shared economic risks: exact arithmetic and frozen recommendation inputs."""

from decimal import Decimal as D
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ledger.exposure import ExposurePolicy, check_proposed_exposure, exposure_totals, proposal_priority


def test_shared_demand_is_counted_across_industries():
    result = exposure_totals({'a': D('.10'), 'b': D('.15')},
                             {'a': ('property',), 'b': ('property', 'exports', 'property')})
    assert result == {'unknown': D('0'), 'property': D('.25'), 'exports': D('.15')}
    assert proposal_priority('fail', 'deteriorating', 'C') == 'exit_review'


def test_stock_etf_overlap_and_unknown_are_not_divided_or_hidden():
    result = exposure_totals({'stock': D('.4'), 'etf': D('.4'), 'unmapped': D('.2')},
                             {'stock': ('property', 'exports'), 'etf': ('property', 'exports')})
    assert result == {'property': D('.8'), 'exports': D('.8'), 'unknown': D('.2')}
    assert sum(result.values()) == D('1.8')


def test_limits_apply_after_addition_but_never_block_reductions():
    policy = ExposurePolicy({'property': D('.25')}, D('.1'))
    labels = {'a': ('property',), 'b': ('property',)}
    result = check_proposed_exposure({'a': D('.2')}, {'b': D('.1')}, labels, policy)
    assert result['allowed'] is False
    assert result['proposed_totals']['property'] == D('.3')
    assert result['breaches'] == {'property': D('.05')}
    assert check_proposed_exposure({'a': D('.4')}, {'a': D('-.05')}, labels, policy)['allowed']
    assert not check_proposed_exposure({'a': D('.2')}, {'unknown': D('.11')}, labels, policy)['allowed']


def test_unconfigured_limits_never_invent_breaches():
    result = check_proposed_exposure({'a': D('.6')}, {'b': D('.3')}, {}, ExposurePolicy({}, None))
    assert result['allowed'] is True
    assert result['breaches'] == {}
    assert result['proposed_totals'] == {'unknown': D('.9')}


@pytest.mark.parametrize('weight', [D('-.01'), D('NaN'), D('Infinity'), .1, True])
def test_invalid_weights_are_rejected(weight):
    with pytest.raises(ValueError):
        exposure_totals({'a': weight}, {})


def test_a_reduction_cannot_create_a_negative_position():
    with pytest.raises(ValueError):
        check_proposed_exposure({'a': D('.1')}, {'a': D('-.11')}, {}, ExposurePolicy({}, None))


def test_explicit_fail_precedes_financial_freeze_and_keeps_exact_scenario_prices():
    from research.thesis import dynamic_sizing
    structured = {'verdict': 'fail', 'data_grade': 'C', 'memo': {'role': 'core'},
                  'operating_result': {'state': 'deteriorating'},
                  'price_bands': {'computed_version': 'valuation-scenarios-v1',
                                 'aggressive': '17.6', 'steady': '23.76',
                                 'conservative': '33', 'overvalued': '33'}}
    result = dynamic_sizing(structured, current_price=D('10'), price_usable=True,
                           actual_weight=D('.1'), financials=[
                               {'period': '2025', 'net_income': D('100'), 'ocf': D('100')},
                               {'period': '2026', 'net_income': D('50'), 'ocf': D('-1')}])
    assert result['zone'] == 'exit'
    assert result['action'] == 'trim_or_exit'
    assert D(str(result['steady_bound'])) == D('23.76')


@pytest.mark.parametrize('grade,state', [('C', 'improving'), ('A', 'unknown'), ('B', 'deteriorating')])
def test_grade_and_operating_state_prevent_new_positions(grade, state):
    from research.thesis import dynamic_sizing
    result = dynamic_sizing({'verdict': 'pass', 'data_grade': grade,
        'operating_result': {'state': state}, 'memo': {'role': 'core'},
        'price_bands': {'aggressive': '10', 'steady': '20', 'conservative': '30'}},
        current_price=D('5'), price_usable=True, actual_weight=D('.05'))
    assert result['action'] not in {'add_to_cap', 'normal_config'}
    assert D(str(result['suggested_max'])) <= D('.05')
    assert D(str(result['role_cap'])) == D('.15')


# UTC previous evening is already the next Shanghai business date.
NOW = datetime(2026, 10, 22, 20, tzinfo=timezone.utc)


def source_payload(tmp_path, *, code='600036.SH', kind='segment_revenue', values=None, basis='property demand customer revenue'):
    values = values or {'revenue': '100'}
    excerpt = basis + ' ' + json.dumps(values, sort_keys=True)
    digest = sha256(excerpt.encode()).hexdigest()
    path = tmp_path / f'{code}-{kind}-{digest}.txt'
    path.write_text(excerpt, encoding='utf-8')
    at = (NOW - timedelta(days=1)).isoformat()
    return {'code': code, 'kind': kind, 'period': '2026-10-23', 'values': values,
            'source_url': 'https://example.invalid/synthetic', 'document_sha256': digest,
            'source_document_path': str(path), 'source_excerpt': excerpt,
            'report_location': 'synthetic retained source', 'basis_id': 'synthetic-consolidated',
            'published_at': at, 'observed_at': at, 'status': 'verified',
            'verification': {'method': 'human_review', 'reviewer': 'fixture', 'reviewed_at': at},
            'excerpt_verification': {'method': 'literal_utf8', 'document_sha256': digest,
                'excerpt_sha256': digest, 'report_location': 'synthetic retained source',
                'reviewer': 'fixture', 'reviewed_at': at}}


def mapping(record, labels=None, instrument_type='equity'):
    return {'labels': labels or ['property'], 'instrument_type': instrument_type,
            'evidence_ids': [record.evidence_id], 'basis_excerpt': 'property demand',
            'rationale': 'Reviewed economic demand interpretation, not industry inference.',
            'effective_at': (NOW - timedelta(days=2)).isoformat()}


def version_payload(records):
    return {'policy': {'limits': {'property': '0.25'}, 'unknown_limit': None},
            'mappings': {record.code: mapping(record) for record in records},
            'review': {'method': 'human_review', 'reviewer': 'fixture', 'reviewed_at': NOW.isoformat()}}


@pytest.fixture
def exposure_db(tmp_path, monkeypatch):
    from app.db import init_db
    monkeypatch.setattr('ledger.exposure._now', lambda: NOW)
    engine = init_db(tmp_path / 'exposure.db')
    yield engine
    engine.dispose()


def add_source(session, tmp_path, **kwargs):
    from discovery.evidence_store import append_evidence
    from market.evidence import parse_source_evidence
    row = parse_source_evidence(source_payload(tmp_path, **kwargs))
    append_evidence(session, row)
    return row


def test_explicit_selection_and_frozen_versions_do_not_follow_new_labels(exposure_db, tmp_path):
    from sqlalchemy.orm import Session
    from ledger.exposure import append_exposure_version, select_exposure_version, freeze_selected_exposure, exposure_report
    with Session(exposure_db) as session:
        record = add_source(session, tmp_path)
        data = version_payload([record])
        first = append_exposure_version(session, user_id=1, payload=data)
        assert append_exposure_version(session, user_id=1, payload=data) == first
        frozen = freeze_selected_exposure(session, user_id=1, cutoff_at=NOW, weights={record.code: D('.2')})
        assert frozen['version_id'] is None
        assert exposure_report(frozen)['current_totals'] == {'unknown': '0.2'}
        select_exposure_version(session, user_id=1, version_id=first)
        frozen = freeze_selected_exposure(session, user_id=1, cutoff_at=NOW, weights={record.code: D('.2')})
        data['mappings'][record.code]['labels'] = ['exports']
        second = append_exposure_version(session, user_id=1, payload=data, supersedes_id=first)
        assert second != first
        assert freeze_selected_exposure(session, user_id=1, cutoff_at=NOW, weights={})['version_id'] == first
        select_exposure_version(session, user_id=1, version_id=second)
        assert exposure_report(frozen)['current_totals'] == {'unknown': '0', 'property': '0.2'}
        assert not exposure_report(frozen, {record.code: D('.1')})['allowed']
        later = freeze_selected_exposure(session, user_id=1, cutoff_at=NOW, weights={record.code: D('.2')})
        assert exposure_report(later)['current_totals'] == {'unknown': '0', 'exports': '0.2'}
        assert later['fingerprint'] != frozen['fingerprint']


@pytest.mark.parametrize('change', ['wrong_kind', 'missing_file', 'wrong_excerpt', 'wrong_owner', 'future_review'])
def test_mapping_rejects_unqualified_sources_and_claims(exposure_db, tmp_path, change):
    from sqlalchemy.orm import Session
    from ledger.exposure import append_exposure_version
    with Session(exposure_db) as session:
        row = add_source(session, tmp_path, kind='financial' if change == 'wrong_kind' else 'segment_revenue')
        payload = version_payload([row])
        if change == 'missing_file':
            Path(row.values['_source_document_path']).unlink()
        if change == 'wrong_excerpt':
            payload['mappings'][row.code]['basis_excerpt'] = 'unrelated invented claim'
        if change == 'future_review':
            payload['review']['reviewed_at'] = (NOW + timedelta(days=1)).isoformat()
        with pytest.raises(ValueError):
            append_exposure_version(session, user_id=99 if change == 'wrong_owner' else 1, payload=payload)


def test_unavailable_source_and_future_effective_mapping_remain_unknown(exposure_db, tmp_path):
    from sqlalchemy.orm import Session
    from ledger.exposure import append_exposure_version, select_exposure_version, freeze_selected_exposure, exposure_report
    with Session(exposure_db) as session:
        row = add_source(session, tmp_path)
        payload = version_payload([row])
        payload['mappings'][row.code]['effective_at'] = (NOW + timedelta(days=1)).isoformat()
        version = append_exposure_version(session, user_id=1, payload=payload)
        select_exposure_version(session, user_id=1, version_id=version)
        context = freeze_selected_exposure(session, user_id=1, cutoff_at=NOW, weights={row.code: D('.2'), 'unpriced': None})
        report = exposure_report(context, {row.code: D('.01')})
        assert report['current_totals'] == {'unknown': '0.2'}
        assert report['complete'] is False and report['allowed'] is False
        assert report['unpriced_codes'] == ['unpriced']
        assert report['mapping_status'][row.code]['reason'] == 'mapping_not_effective'
        old = freeze_selected_exposure(session, user_id=1, cutoff_at=NOW-timedelta(seconds=1), weights={})
        assert old['version_id'] is None


def test_local_cli_defaults_read_only_and_apply_select_reaches_actual_positions(tmp_path):
    from tests.test_review import _client
    from market.evidence import parse_source_evidence
    with _client(tmp_path) as client:
        raw = source_payload(tmp_path)
        live = datetime.now(timezone.utc)
        source_at = (live - timedelta(days=1)).isoformat()
        raw.update(published_at=source_at, observed_at=source_at)
        raw['verification']['reviewed_at'] = source_at
        raw['excerpt_verification']['reviewed_at'] = source_at
        evidence_path = tmp_path / 'evidence.json'
        evidence_path.write_text(json.dumps([raw]), encoding='utf-8')
        root = Path(__file__).resolve().parents[1]
        source = subprocess.run([sys.executable, '-m', 'scripts.import_selection_evidence', '--db', str(tmp_path / 'app.db'),
                                 '--input', str(evidence_path)], cwd=root, capture_output=True, text=True)
        assert source.returncode == 0, source.stderr
        payload = version_payload([parse_source_evidence(raw)])
        payload['review']['reviewed_at'] = (live - timedelta(seconds=1)).isoformat()
        payload['mappings'][raw['code']]['effective_at'] = source_at
        path = tmp_path / 'exposure.json'
        path.write_text(json.dumps({'user_id': 1, 'payload': payload}), encoding='utf-8')
        command = [sys.executable, '-m', 'scripts.import_portfolio_exposure', '--db', str(tmp_path / 'app.db'),
                   '--input', str(path), '--select']
        dry = subprocess.run(command, cwd=root, capture_output=True, text=True)
        assert dry.returncode == 0, dry.stderr
        assert json.loads(dry.stdout)['applied'] is False
        assert client.get('/api/positions').json()['common_exposure']['version_id'] is None
        applied = subprocess.run([*command, '--apply'], cwd=root, capture_output=True, text=True)
        assert applied.returncode == 0, applied.stderr
        body = client.get('/api/positions').json()
        assert body['common_exposure']['version_id'] == json.loads(applied.stdout)['version']
        assert D(body['common_exposure']['current_totals']['property']) > 0
        assert body['common_exposure']['configured'] is True


def test_review_capture_survives_failure_label_revision_and_missing_source(tmp_path, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.models import PortfolioReview, Quote
    from app.selection_models import ReviewExposureCapture
    from jobs.review import _run_one
    from ledger.exposure import append_exposure_version, select_exposure_version
    from research.llm import FakeLLM
    from tests.test_review import _client, _write_key
    import review.run as runner
    _write_key(tmp_path)
    monkeypatch.setattr('ledger.exposure._now', lambda: NOW)
    monkeypatch.setattr('jobs.review._now', lambda: NOW)
    llm = FakeLLM(payload={'action': 'add', 'reason': 'model asks to add'},
                  payloads={'__PORTFOLIO__': {'portfolio_actions': [
                      {'action': 'buy more', 'names': ['招商银行'], 'cash_or_target_link': 'use cash'}]}})
    stable = {'verdict': 'pass', 'data_grade': 'B', 'operating_result': {'state': 'stable'},
              'memo': {'role': 'core'}, 'price_bands': {'computed_version': 'valuation-scenarios-v1',
                  'aggressive': '2000', 'steady': '2100', 'conservative': '2200', 'overvalued': '2200'}}
    monkeypatch.setattr('jobs.review.load_current_theses', lambda session, names, rows:
        {name: {'id': 7, 'verdict': 'pass', 'structured': stable, 'stale': False,
                'dynamic_sizing': {'action': 'add_to_cap', 'suggested_max': '.15'}} for name in names})
    original_writer = runner._write_report
    monkeypatch.setattr(runner, '_write_report', lambda *args: (_ for _ in ()).throw(OSError('synthetic report failure')))
    with _client(tmp_path, llm=llm) as client:
        factory = client.app.state.session_factory
        with factory() as session:
            record = add_source(session, tmp_path)
            data = version_payload([record])
            first = append_exposure_version(session, user_id=1, payload=data)
            select_exposure_version(session, user_id=1, version_id=first)
            session.commit()
        created = client.post('/api/reviews', json={'sync': True}).json()
        review_id = created['id']
        assert created['status'] == 'failed'
        original_prompts = llm.prompts.copy()
        with factory() as session:
            capture = session.scalar(select(ReviewExposureCapture).where(ReviewExposureCapture.review_id == review_id))
            frozen_json = capture.payload_json
            frozen = json.loads(frozen_json)
            assert frozen['exposure_context']['version_id'] == first
            assert frozen['as_of_date'] == '2026-10-23'
            for row in frozen['snapshot']['positions']:
                key = row['code'] or f"name:{row['name']}"
                assert D(frozen['exposure_context']['weights'][key]) == D(row['exposure_weight'])
                quote = session.get(Quote, (1, row['name']))
                assert row['exposure_price_observed_at'] == quote.updated_at.replace(tzinfo=timezone.utc).isoformat()
                assert row['exposure_price_source'] == quote.source
                # Later quote changes cannot relabel the retained price/date on retry.
                quote.price = D('99')
                quote.updated_at = datetime(2026, 10, 25, 6)
                quote.stale = True
            data['mappings'][record.code]['labels'] = ['exports']
            second = append_exposure_version(session, user_id=1, payload=data, supersedes_id=first)
            select_exposure_version(session, user_id=1, version_id=second)
            session.commit()
        Path(record.values['_source_document_path']).unlink()
        monkeypatch.setattr(runner, '_write_report', original_writer)
        def forbidden(*args, **kwargs):
            raise AssertionError('replay must not refresh quotes, sources or evaluations')
        monkeypatch.setattr('jobs.review.refresh_held_quotes', forbidden)
        monkeypatch.setattr('jobs.review.load_current_theses', forbidden)
        monkeypatch.setattr('ledger.exposure._sources', forbidden)
        with factory() as session:
            row = session.get(PortfolioReview, review_id)
            _run_one(session, row, llm=llm, quote_client=None, reports_dir=tmp_path / 'reports')
            assert row.status == 'done', row.error
            assert session.scalar(select(ReviewExposureCapture)).payload_json == frozen_json
            _run_one(session, row, llm=llm, quote_client=None, reports_dir=tmp_path / 'reports')
        assert llm.prompts[len(original_prompts):] == original_prompts
        body = client.get(f'/api/reviews/{review_id}').json()
        assert body['common_exposure']['version_id'] == first
        assert body['portfolio_actions'][0]['validation'] == 'unvalidated_note'
        assert '不构成新增许可' in body['markdown']


def test_immutable_versions_selections_and_capture_reject_sql_mutations(exposure_db, tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session
    from app.models import PortfolioReview
    from ledger.exposure import (append_exposure_version, select_exposure_version,
        freeze_selected_exposure, capture_review_input)
    with Session(exposure_db) as session:
        record = add_source(session, tmp_path)
        version = append_exposure_version(session, user_id=1, payload=version_payload([record]))
        select_exposure_version(session, user_id=1, version_id=version)
        row = PortfolioReview(user_id=1, status='queued', has_stale=False, created_at=NOW, updated_at=NOW)
        session.add(row)
        session.flush()
        capture_review_input(session, review_id=row.id, user_id=1, payload={'exposure_context':
            freeze_selected_exposure(session, user_id=1, cutoff_at=NOW, weights={})})
        with pytest.raises(ValueError, match='owner'):
            capture_review_input(session, review_id=row.id, user_id=2, payload={})
        session.commit()
    for table in ('portfolio_exposure_versions', 'portfolio_exposure_selections', 'portfolio_review_exposure_captures'):
        for sql in (f'DELETE FROM {table}', f'UPDATE {table} SET user_id=1', f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'):
            with exposure_db.begin() as connection, pytest.raises(IntegrityError, match='immutable'):
                connection.execute(text(sql))


def test_same_day_etf_comparison_keeps_facts_and_declarations_separate(exposure_db, tmp_path):
    from sqlalchemy.orm import Session
    from ledger.exposure import append_exposure_version, select_exposure_version, freeze_selected_exposure
    from review.run import ReviewInput, run_review
    from research.llm import FakeLLM
    with Session(exposure_db) as session:
        stock = add_source(session, tmp_path)
        fund = add_source(session, tmp_path, code='510000.SH', kind='fund_holdings')
        facts = add_source(session, tmp_path, code='510000.SH', kind='etf_snapshot',
            values={'fee_rate': '.005', 'valuation_multiple': '12'}, basis='broad market index PE trailing')
        data = version_payload([stock])
        data['mappings'][fund.code] = mapping(fund, instrument_type='etf')
        data['comparison'] = {'code': fund.code, 'as_of_date': '2026-10-23',
            'evidence_ids': [facts.evidence_id], 'basis_excerpt': 'broad market index',
            'broad_market_basis': 'broad market index', 'fee_rate': '.005', 'valuation_basis': 'PE trailing',
            'valuation_multiple': '12', 'earnings_assumption': 'reviewed growth assumption; not a forecast fact', 'confidence': 'B'}
        version = append_exposure_version(session, user_id=1, payload=data)
        select_exposure_version(session, user_id=1, version_id=version)
        frozen = freeze_selected_exposure(session, user_id=1, cutoff_at=NOW, weights={stock.code: D('.1'), fund.code: D('.2')})
        assert frozen['comparison']['status'] == 'available'
        output = run_review(FakeLLM(payload={'action': 'hold', 'reason': 'test'}), ReviewInput(
            snapshot={'positions': [{'name': 'Synthetic', 'code': stock.code, 'price': '10', 'exposure_price': '10'}]},
            evaluations={}, funnel_names=(), as_of_date='2026-10-23', review_id=1,
            reports_dir=tmp_path / 'reports', exposure_context=frozen))
        assert output.structured['common_exposure']['current_totals']['property'] == '0.3'
        assert output.structured['opportunity_cost']['data']['fee_rate'] == '0.005'
        assert '声明盈利假设' in output.markdown and '预期超额收益数字' in output.markdown
        assert 'expected_excess_return' not in json.dumps(output.structured)
        tomorrow = freeze_selected_exposure(session, user_id=1, cutoff_at=NOW+timedelta(days=1), weights={})
        assert tomorrow['comparison'] == {'status': 'missing', 'reason': 'comparison_date_mismatch', 'data': None}
        data['comparison']['fee_rate'] = '.006'
        with pytest.raises(ValueError, match='differs'):
            append_exposure_version(session, user_id=1, payload=data)


def test_review_and_live_optimization_check_combined_additions(exposure_db, tmp_path):
    from sqlalchemy.orm import Session
    from ledger.exposure import append_exposure_version, select_exposure_version, freeze_selected_exposure
    from review.run import ReviewInput, run_review
    from research.thesis import dynamic_sizing, portfolio_optimization
    from research.llm import FakeLLM
    with Session(exposure_db) as session:
        records = [add_source(session, tmp_path, code=code) for code in ('600036.SH', '600519.SH')]
        version = append_exposure_version(session, user_id=1, payload=version_payload(records))
        select_exposure_version(session, user_id=1, version_id=version)
        context = freeze_selected_exposure(session, user_id=1, cutoff_at=NOW,
            weights={record.code: D('.1') for record in records})
    paper = {'verdict': 'pass', 'data_grade': 'B', 'operating_result': {'state': 'stable'},
             'memo': {'role': 'core'}, 'price_bands': {'aggressive': '10', 'steady': '20', 'conservative': '30'}}
    sizing = dynamic_sizing(paper, current_price=D('5'), price_usable=True, actual_weight=D('.1'))
    positions = [{'name': record.code, 'code': record.code, 'weight': .1, 'price': 5,
                  'exposure_weight': '.1', 'exposure_target': '.15', 'dynamic_sizing': sizing} for record in records]
    result = portfolio_optimization(positions, exposure_context=context)
    assert len(result['add']) == 2
    assert all(item['ready'] is False for item in result['add'])
    assert result['common_exposure']['proposed_totals']['property'] == '0.3'
    output = run_review(FakeLLM(payload={'action': 'add', 'reason': 'model add'}), ReviewInput(
        snapshot={'positions': positions}, evaluations={record.code: {'verdict': 'pass', 'structured': paper,
            'dynamic_sizing': sizing} for record in records}, funnel_names=(), as_of_date='2026-10-23',
        review_id=2, reports_dir=tmp_path / 'reports', exposure_context=context))
    assert all(row['action'] == 'hold' and row['restriction'] == 'shared_exposure_limit' for row in output.structured['positions'])
    assert D(output.structured['common_exposure']['proposed_totals']['property']) == D('.3')


def test_review_priority_restricts_unknown_but_not_cuts_and_fail_wins(tmp_path):
    from review.run import ReviewInput, run_review
    from research.llm import FakeLLM
    names = ['fail', 'skip', 'gray', 'C', 'unknown', 'missing', 'cut']
    evaluations = {}
    for name in names:
        data = {'verdict': 'pass', 'data_grade': 'B', 'memo': {'role': 'core'},
                'operating_result': {'state': 'stable'}}
        if name in {'fail', 'gray'}:
            data['verdict'] = name
        if name == 'skip':
            data['memo']['role'] = 'skip'
        if name == 'C':
            data['data_grade'] = 'C'
        if name == 'unknown':
            data['operating_result']['state'] = 'unknown'
        if name == 'missing':
            data.pop('data_grade')
            data.pop('operating_result')
        evaluations[name] = {'verdict': data['verdict'], 'structured': data,
                             'financial_drift': {'period': '2026H1'} if name in {'fail', 'skip'} else None}
    llm = FakeLLM(payload={'action': 'add', 'reason': 'model add'},
                  payloads={'cut': {'action': 'cut', 'reason': 'reduce'}})
    output = run_review(llm, ReviewInput(snapshot={'positions': [{'name': name, 'price': '10'} for name in names]},
        evaluations=evaluations, funnel_names=(), as_of_date='2026-10-23', review_id=3, reports_dir=tmp_path))
    assert [row['action'] for row in output.structured['positions']] == ['sell', 'sell', 'hold', 'hold', 'hold', 'hold', 'cut']


def test_exact_arithmetic_ignores_ambient_precision_and_rejects_fake_caps():
    from decimal import localcontext
    with localcontext() as context:
        context.prec = 3
        result = check_proposed_exposure({'a': D('.12345678901234567890123456789')},
            {'a': D('.00000000000000000000000000001')}, {'a': ('property',)}, ExposurePolicy({}, None))
        assert result['proposed_totals']['property'] == D('.12345678901234567890123456790')
    for limits, unknown in [({'unknown': D('.1')}, None), ({'property': D('-1')}, None),
                             ({'property': D('Infinity')}, None), ({}, D('1.01'))]:
        with pytest.raises(ValueError):
            ExposurePolicy(limits, unknown)


def test_source_loss_retains_missing_status_and_confirmed_zero_weight(exposure_db, tmp_path):
    from sqlalchemy.orm import Session
    from ledger.exposure import append_exposure_version, select_exposure_version, freeze_selected_exposure, exposure_report
    with Session(exposure_db) as session:
        source = add_source(session, tmp_path)
        version = append_exposure_version(session, user_id=1, payload=version_payload([source]))
        select_exposure_version(session, user_id=1, version_id=version)
        Path(source.values['_source_document_path']).unlink()
        frozen = freeze_selected_exposure(session, user_id=1, cutoff_at=NOW, weights={source.code: D('0')})
        report = exposure_report(frozen)
        assert report['mapping_status'][source.code]['status'] == 'missing'
        assert report['mapping_status'][source.code]['source_states'][source.evidence_id] == 'missing'
        assert report['current_totals'] == {'unknown': '0'}
        assert report['complete'] is True


@pytest.mark.parametrize('price,stale,observed,reason,price_day', [
    ('7', True, '2026-10-22T20:00:00+00:00', 'stale_quote', '2026-10-23'),
    ('7', False, '2026-10-22T12:00:00+00:00', 'quote_date_mismatch', '2026-10-22'),
    (None, False, '2026-10-22T20:00:00+00:00', 'unusable_quote', '2026-10-23'),
    ('0', False, '2026-10-22T20:00:00+00:00', 'unusable_quote', '2026-10-23'),
    ('-1', False, '2026-10-22T20:00:00+00:00', 'unusable_quote', '2026-10-23'),
    ('NaN', False, '2026-10-22T20:00:00+00:00', 'unusable_quote', '2026-10-23'),
    ('Infinity', False, '2026-10-22T20:00:00+00:00', 'unusable_quote', '2026-10-23'),
    ('7', False, None, 'quote_date_missing', None),
    ('7', False, '2026-10-23T01:00:00', 'quote_date_missing', None),
    ('7', False, '2026-10-23T08:00:00+00:00', 'quote_time_after_cutoff', '2026-10-23'),
    ('7', False, '2026-10-22T20:00:00+00:00', None, '2026-10-23'),
])
def test_opportunity_cost_preserves_holding_quote_date_and_freshness(tmp_path, price, stale, observed, reason, price_day):
    from review.run import ReviewInput, _opportunity_cost, render_markdown
    row = {'name': 'Synthetic', 'code': '600036.SH', 'price': price, 'exposure_price': price,
           'stale': stale, 'exposure_price_observed_at': observed, 'exposure_price_source': 'manual'}
    benchmark = {'status': 'available', 'reason': None, 'data': {
        'code': '510000.SH', 'as_of_date': '2026-10-23', 'broad_market_basis': 'synthetic broad market',
        'fee_rate': '.005', 'valuation_basis': 'PE trailing', 'valuation_multiple': '12',
        'earnings_assumption': 'synthetic declaration', 'confidence': 'B',
        'common_risks': ['property'], 'evidence_ids': ['synthetic']}}
    payload = ReviewInput(snapshot={'positions': [row]}, evaluations={}, funnel_names=(),
        as_of_date='2026-10-23', review_id=1, reports_dir=tmp_path,
        exposure_context={'comparison': benchmark, 'labels': {}, 'cutoff_at': '2026-10-23T06:00:00+00:00'})
    result = _opportunity_cost(payload)
    holding = result['holdings'][0]
    assert holding['as_of_date'] == price_day
    assert holding['comparison_date'] == '2026-10-23'
    assert holding['price_stale'] is stale
    assert holding['price_source'] == 'manual'
    assert holding['price_observed_at'] == (observed if price_day else None)
    assert holding['price_reason'] == reason
    assert holding['current_price'] == ('7' if reason is None else None)
    assert holding['price_status'] == ('available' if reason is None else 'unavailable')
    markdown = render_markdown({'opportunity_cost': result}, as_of_date=payload.as_of_date, snapshot=payload.snapshot)
    if reason is None:
        assert '同日可用报价 7' in markdown
        assert '报价记录日期 2026-10-23' in markdown
    else:
        assert '持仓报价不可用' in markdown and reason in markdown
        assert '当日价格 7' not in markdown and '同日可用报价 7' not in markdown
