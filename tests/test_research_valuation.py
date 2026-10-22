"""Deterministic scenario inputs; synthetic retained sources and temporary DBs."""

from dataclasses import replace
from decimal import Decimal as D, localcontext
from datetime import timedelta
from hashlib import sha256
import json

import pytest

from research.valuation import ValuationAssumptions, price_bands, valuation_report


def assumptions(**changes):
    return replace(ValuationAssumptions('pe', D('2'), D('0.1'),
        (D('10'), D('12'), D('15')), (D('0.2'), D('0.1'), D('0')), 1, ('e',)), **changes)


def test_price_bands_are_reproducible_from_assumptions():
    assert price_bands(assumptions()) == (D('17.6'), D('23.76'), D('33'))
    with localcontext() as context:
        context.prec = 4
        assert price_bands(assumptions()) == (D('17.6'), D('23.76'), D('33'))


@pytest.mark.parametrize('changes', [
    {'method': 'model'}, {'metric_per_share': D('0')}, {'growth': D('-1')},
    {'metric_per_share': D('NaN')}, {'growth': D('Infinity')},
    {'multiples': (D('10'), D('NaN'), D('15'))},
    {'multiples': (D('15'), D('12'), D('10'))},
    {'multiples': (D('10'), D('12'))}, {'haircuts': (D('0'), D('1'), D('0'))},
    {'horizon_years': True}, {'horizon_years': 0}, {'evidence_ids': ()},
    {'growth': 0.1}, {'metric_per_share': D('1e5000')},
    {'metric_per_share': D('1e4000'), 'growth': D('0'),
     'multiples': (D('1e4000'), D('2e4000'), D('3e4000')), 'haircuts': (D('0'), D('0'), D('0'))},
])
def test_invalid_pure_inputs_raise_value_error(changes):
    with pytest.raises(ValueError):
        price_bands(assumptions(**changes))


def test_report_distinguishes_assumptions_prices_and_sensitivity():
    report = valuation_report(assumptions(method='pb'), D('20'))
    assert report['computed_prices'] == {'low': '17.6', 'base': '23.76', 'high': '33'}
    assert report['labels'] == {'low': '积极买入价', 'base': '常规买入价', 'high': '停止加仓价'}
    assert report['declared_assumptions']['growth'] == '0.1'
    assert report['pessimistic']['price'] == '17.6'
    assert len(report['sensitivity']) == 9
    assert report['current_price'] == '20'


from tests.test_research_evidence import CUTOFF, payload, model_output


def reviewed(tmp_path, kind, period, values, *, industry=None, explanation=''):
    from market.evidence import parse_source_evidence
    text = json.dumps(values, sort_keys=True) + (industry or '') + explanation
    digest = sha256(text.encode()).hexdigest()
    path = tmp_path / f'{kind}-{period}-{digest}.txt'
    path.write_text(text, encoding='utf-8')
    return parse_source_evidence(dict(code='000001.SZ', kind=kind, period=period,
        published_at=(CUTOFF - timedelta(days=1)).isoformat(), observed_at=(CUTOFF - timedelta(days=1)).isoformat(),
        source_url='https://example.invalid/reviewed', document_sha256=digest, source_document_path=str(path),
        source_excerpt=text, report_location='synthetic retained document', values=values, industry=industry,
        basis_id='fixture-consolidated-cny', status='verified',
        verification={'method': 'human_review', 'reviewer': 'synthetic', 'reviewed_at': (CUTOFF-timedelta(days=1)).isoformat()},
        excerpt_verification={'method': 'literal_utf8', 'document_sha256': digest,
            'excerpt_sha256': digest, 'report_location': 'synthetic retained document',
            'reviewer': 'synthetic', 'reviewed_at': (CUTOFF-timedelta(days=1)).isoformat()}))


def scenario(rows, **changes):
    by_kind = {row.kind: row for row in rows}
    result = dict(method='pe', normalized_metric_per_share='2', earnings_growth='0.1',
        multiple_low='10', multiple_base='12', multiple_high='15', haircut_low='0.2',
        haircut_base='0.1', haircut_high='0', horizon_years=1, metric_basis='deducted_ttm',
        period='2025', profile_evidence_id=by_kind['company_profile'].evidence_id,
        shares_evidence_id=by_kind['share_snapshot'].evidence_id,
        evidence_ids=[row.evidence_id for row in rows],
        rationale='Declared scenarios reviewed against the selected retained sources; growth is not a fact.',
        review={'method': 'human_review', 'reviewer': 'synthetic', 'reviewed_at': CUTOFF.isoformat()})
    if 'market_snapshot' in by_kind:
        result['price_evidence_id'] = by_kind['market_snapshot'].evidence_id
    if 'treasury_yield' in by_kind:
        result['treasury_evidence_id'] = by_kind['treasury_yield'].evidence_id
    result.update(changes)
    return result


def sources(tmp_path):
    return (reviewed(tmp_path, 'financial', '2025', {'roe': '10', 'net_income': '21', 'deducted_net_income': '20'}),
        reviewed(tmp_path, 'share_snapshot', '2026-10-02', {'shares': '10'}),
        reviewed(tmp_path, 'company_profile', '2026-10-02', {}, industry='制造业'),
        reviewed(tmp_path, 'market_snapshot', '2026-10-02', {'price': '20'}))


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.db import init_db
    monkeypatch.setattr('research.valuation._now', lambda: CUTOFF)
    monkeypatch.setattr('jobs.research._now', lambda: CUTOFF)
    engine = init_db(tmp_path / 'valuation.db')
    yield engine
    engine.dispose()


def append(session, rows, data=None):
    from discovery.evidence_store import append_evidence
    from research.valuation import append_assumptions
    for row in rows:
        append_evidence(session, row)
    session.flush()
    return append_assumptions(session, user_id=1, code=rows[0].code, payload=data or scenario(rows))


def test_persisted_owned_version_drives_actual_creation_and_model_cannot_override(store, tmp_path):
    from sqlalchemy.orm import Session
    from jobs.research import create_research_job, _job_evidence_context, _model_capture
    from research.evaluate import normalize_structured, _build_prompt
    rows = sources(tmp_path)
    with Session(store) as session:
        assumption_id = append(session, rows)
        assert append(session, rows) == assumption_id
        job = create_research_job(session, name='Synthetic', code=rows[0].code, assumption_id=assumption_id)
        session.commit()
        snapshot, _, context, fingerprint = _job_evidence_context(job)
        result = normalize_structured(model_output(rows[0]), payload(tmp_path, snapshot, context))
        assert result['evidence_contract']['valuation_complete'] is True
        # T10's unverified realization still lowers the safety gate; validated
        # valuation makes B reachable without upgrading that separate claim.
        assert result['data_grade'] == 'B'
        assert result['price_bands']['steady'] == '23.76'
        assert result['valuation_scenarios']['computed_prices']['high'] == '33'
        assert result['valuation_scenarios']['current_price'] == '20'
        assert '未来' in _build_prompt(payload(tmp_path, snapshot, context))
        capture = _model_capture(payload(tmp_path, snapshot, context), 'exact prompt', model='synthetic', credibility_gate=False)
        assert capture['input']['research_context']['valuation']['assumption_id'] == assumption_id
        assert fingerprint
        fail = normalize_structured(model_output(rows[0], verdict='fail'), payload(tmp_path, snapshot, context))
        assert fail['verdict'] == 'fail'
        # Frozen execution reuses retained proofs without file/source reads.
        for file in tmp_path.glob('*.txt'):
            file.unlink()
        assert _job_evidence_context(job)[2] == context
        assert normalize_structured(model_output(rows[0]), payload(tmp_path, snapshot, context))['price_bands'] == result['price_bands']


@pytest.mark.parametrize('statement', [
    "UPDATE selection_research_assumptions SET method='pb'",
    'DELETE FROM selection_research_assumptions',
    'INSERT OR REPLACE INTO selection_research_assumptions SELECT * FROM selection_research_assumptions',
    'DELETE FROM users WHERE id=1',
])
def test_assumptions_are_append_only_and_owner_delete_restricted(store, tmp_path, statement):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session
    with Session(store) as session:
        append(session, sources(tmp_path))
        session.commit()
    with store.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(text(statement))


def test_missing_id_and_wrong_owner_company_never_select_latest(store, tmp_path):
    from sqlalchemy.orm import Session
    from app.models import User
    from jobs.research import create_research_job, _job_evidence_context
    from app.selection_models import ResearchAssumption
    with Session(store) as session:
        rows = sources(tmp_path)
        assumption_id = append(session, rows)
        session.add(User(id=2, name='Synthetic second owner'))
        session.commit()
        assert _job_evidence_context(create_research_job(session, name='Synthetic', code=rows[0].code))[2]['valuation_complete'] is False
        with pytest.raises(ValueError, match='owner|company'):
            create_research_job(session, name='Other', code='000002.SZ', assumption_id=assumption_id)
        # The persisted owner is authoritative, even when the company matches.
        foreign = ResearchAssumption(user_id=2, code=rows[0].code, created_at=CUTOFF,
            method='pe', payload_json='{}', evidence_ids_json='[]', digest='foreign')
        session.add(foreign)
        session.flush()
        with pytest.raises(ValueError, match='owner|company'):
            create_research_job(session, name='Other owner', code=rows[0].code, assumption_id=foreign.id)


def test_stale_share_day_and_superseded_evidence_stay_watch(store, tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    from jobs.research import create_research_job, _job_evidence_context
    from discovery.evidence_store import append_evidence
    from discovery.fundamental_types import evidence_identity
    with Session(store) as session:
        rows = sources(tmp_path)
        assumption_id = append(session, rows)
        session.commit()
        monkeypatch.setattr('jobs.research._now', lambda: CUTOFF + timedelta(days=1))
        later = create_research_job(session, name='Synthetic', code=rows[0].code, assumption_id=assumption_id)
        assert _job_evidence_context(later)[2]['valuation_complete'] is False
        monkeypatch.setattr('jobs.research._now', lambda: CUTOFF)
        revised = replace(rows[0], supersedes_id=rows[0].evidence_id)
        revised = replace(revised, evidence_id=evidence_identity(revised))
        append_evidence(session, revised)
        session.flush()
        changed = create_research_job(session, name='Synthetic', code=rows[0].code, assumption_id=assumption_id)
        assert _job_evidence_context(changed)[2]['valuation_complete'] is False


def test_post_bonus_shares_and_cyclic_five_year_normalization_reuse_shared_formula(store, tmp_path):
    from sqlalchemy.orm import Session
    from jobs.research import create_research_job, _job_evidence_context
    annuals = tuple(reviewed(tmp_path, 'financial', str(year), {'deducted_net_income': str(value),
        'net_income': str(value), 'roe': '10', 'shares': '5'}) for year, value in
        zip(range(2021, 2026), (10, 15, 20, 30, 200), strict=True))
    rows = (*annuals, *sources(tmp_path)[1:])
    with Session(store) as session:
        assumption_id = append(session, rows, scenario(rows, metric_basis='normalized_deducted'))
        job = create_research_job(session, name='Synthetic', code=rows[0].code, assumption_id=assumption_id)
        report = _job_evidence_context(job)[2]['valuation']['validation']['report']
        assert report['source_facts']['numerator'] == '20'
        assert report['source_facts']['current_shares'] == '10'
        assert report['computed_prices']['base'] == '23.76'
        assert len(report['source_facts']['numerator_evidence_ids']) == 5
        with pytest.raises(ValueError, match='metric|evidence'):
            append(session, rows[1:], scenario(rows[1:], metric_basis='normalized_deducted'))


def test_bank_requires_explicit_reviewed_adjusted_basis_and_pb(store, tmp_path):
    from sqlalchemy.orm import Session
    from jobs.research import create_research_job, _job_evidence_context
    explanation = 'Reviewed parent-attributable adjusted assets after asset-quality deductions; current-share basis.'
    equity = reviewed(tmp_path, 'financial', '2025', {'roe': '10', 'net_income': '20',
        'adjusted_net_assets': '20'}, explanation=explanation)
    rows = (equity, sources(tmp_path)[1],
        reviewed(tmp_path, 'company_profile', '2026-10-02', {}, industry='银行'))
    data = scenario(rows, method='pb', metric_basis='adjusted_bvps', equity_evidence_id=equity.evidence_id,
                    bank_adjustment=explanation)
    with Session(store) as session:
        for changed in ({'method': 'pe', 'metric_basis': 'deducted_ttm'}, {'bank_adjustment': ''},
                        {'bank_adjustment': 'Unsupported adjustment explanation'}):
            with pytest.raises(ValueError):
                append(session, rows, {**data, **changed})
        assumption_id = append(session, rows, data)
        context = _job_evidence_context(create_research_job(session, name='Bank', code=equity.code,
            assumption_id=assumption_id))[2]
        assert context['valuation']['validation']['report']['method'] == 'pb'


@pytest.mark.parametrize(('days', 'status'), [(30, 'available'), (31, 'pending')])
def test_treasury_date_source_and_stale_comparison_boundary(store, tmp_path, days, status):
    from sqlalchemy.orm import Session
    from jobs.research import create_research_job, _job_evidence_context
    rows = (*sources(tmp_path), reviewed(tmp_path, 'treasury_yield',
        (CUTOFF.date() - timedelta(days=days)).isoformat(), {'yield': '0.02'}))
    with Session(store) as session:
        assumption_id = append(session, rows)
        report = _job_evidence_context(create_research_job(session, name='Synthetic', code=rows[0].code,
            assumption_id=assumption_id))[2]['valuation']['validation']['report']
        spread = report['valuation_spread']
        assert spread['status'] == status
        assert spread['treasury_source'] == rows[-1].evidence_id
        assert spread['value'] == ('0.08' if days == 30 else None)


def test_thesis_uses_exact_computed_bands_even_far_above_or_below_current_price():
    from research.thesis import band_bounds, expected_price, is_overvalued
    report = {'verdict': 'pass', 'as_of': {'price': '1000'}, 'price_bands': {
        'mode': 'steady', 'aggressive': '17.6', 'steady': '23.76', 'conservative': '33',
        'overvalued': '33', 'computed_version': 'valuation-scenarios-v1'}}
    assert band_bounds(report, ref_price=D('1000')) == (D('17.6'), D('33'))
    assert expected_price(report) == D('23.76')
    assert is_overvalued(report, current_price=D('1000'), price_usable=True)


def test_local_cli_dry_run_and_append_reach_validated_version_without_migrations(store, tmp_path, monkeypatch, capsys):
    import sys
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    from scripts.import_research_assumptions import main
    rows = sources(tmp_path)
    from discovery.evidence_store import append_evidence
    with Session(store) as session:
        for row in rows:
            append_evidence(session, row)
        session.commit()
    path = tmp_path / 'assumptions.json'
    path.write_text(json.dumps([{'user_id': 1, 'code': rows[0].code, 'payload': scenario(rows)}]), encoding='utf-8')
    target = tmp_path / 'valuation.db'
    before = target.read_bytes()
    argv = ['import_research_assumptions', '--db', str(target), '--input', str(path)]
    monkeypatch.setattr(sys, 'argv', [*argv, '--dry-run'])
    assert main() == 0
    assert target.read_bytes() == before
    assert 'dry-run' in capsys.readouterr().out
    monkeypatch.setattr(sys, 'argv', argv)
    assert main() == 0
    with Session(store) as session:
        assert session.execute(text('SELECT count(*) FROM selection_research_assumptions')).scalar_one() == 1


def test_api_explicit_assumption_selection_and_invalid_id_rejection(tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    from tests.test_research import _client, _write_key
    from app.db import init_db
    from app.models import Instrument
    monkeypatch.setattr('research.valuation._now', lambda: CUTOFF)
    monkeypatch.setattr('jobs.research._now', lambda: CUTOFF)
    _write_key(tmp_path)
    engine = init_db(tmp_path / 'app.db')
    rows = sources(tmp_path)
    with Session(engine) as session:
        session.add(Instrument(code=rows[0].code, name='Synthetic', user_id=1))
        assumption_id = append(session, rows)
        session.commit()
    engine.dispose()
    with _client(tmp_path) as client:
        for bad in (True, '1', 1.5, 0, -1, 999):
            response = client.post('/api/research', json={'name': 'Synthetic', 'code': rows[0].code,
                'sync': True, 'assumption_id': bad})
            assert response.status_code == 400
        response = client.post('/api/research', json={'name': 'Synthetic', 'code': rows[0].code,
            'sync': True, 'assumption_id': assumption_id})
        assert response.status_code == 200
        result = client.get(f"/api/research/{response.json()['id']}").json()['structured']
        assert result['evidence_contract']['assumption_id'] == assumption_id
        assert result['evidence_contract']['valuation_complete'] is True
        assert result['price_bands']['steady'] == '23.76'


@pytest.mark.parametrize('changes', [
    {'normalized_metric_per_share': '999'}, {'earnings_growth': 'NaN'},
    {'earnings_growth': 0.1}, {'review': {}}, {'rationale': ''},
    {'metric_basis': 'vendor_eps'}, {'period': '2024'},
])
def test_invalid_reviewed_inputs_cannot_append(store, tmp_path, changes):
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    with Session(store) as session:
        rows = sources(tmp_path)
        with pytest.raises(ValueError):
            append(session, rows, scenario(rows, **changes))
        assert session.execute(text('SELECT count(*) FROM selection_research_assumptions')).scalar_one() == 0


def test_untrusted_conflicting_nonpositive_or_incomparable_sources_cannot_append(store, tmp_path):
    from sqlalchemy.orm import Session
    from discovery.fundamental_types import evidence_identity
    from discovery.evidence_store import append_evidence
    rows = sources(tmp_path)
    with Session(store) as session:
        for index, changes in ((0, {'status': 'untrusted'}), (1, {'basis_id': 'other-consolidation-basis'})):
            changed = replace(rows[index], **changes)
            changed = replace(changed, evidence_id=evidence_identity(changed))
            values = (*rows[:index], changed, *rows[index+1:])
            with pytest.raises(ValueError):
                append(session, values)
            session.rollback()
        negative = reviewed(tmp_path, 'financial', '2025', {'deducted_net_income': '-1', 'roe': '10', 'net_income': '20'})
        with pytest.raises(ValueError):
            append(session, (negative, *rows[1:]))
        session.rollback()
        conflict = reviewed(tmp_path, 'share_snapshot', '2026-10-02', {'shares': '20'})
        append_evidence(session, conflict)
        with pytest.raises(ValueError, match='conflict'):
            append(session, rows)


def test_new_assumption_version_requires_new_job_and_missing_source_becomes_watch(store, tmp_path):
    from sqlalchemy.orm import Session
    from jobs.research import create_research_job, _job_evidence_context
    from research.evaluate import normalize_structured, render_markdown
    with Session(store) as session:
        rows = sources(tmp_path)
        first_id = append(session, rows)
        old = create_research_job(session, name='Synthetic', code=rows[0].code, assumption_id=first_id)
        session.commit()
        original = old.evidence_context_json
        new_id = append(session, rows, scenario(rows, earnings_growth='0.2'))
        assert new_id != first_id
        new = create_research_job(session, name='Synthetic', code=rows[0].code, assumption_id=new_id)
        assert _job_evidence_context(new)[2]['valuation']['validation']['report']['computed_prices']['base'] == '25.92'
        assert old.evidence_context_json == original
        snapshot, _, context, _ = _job_evidence_context(old)
        result = normalize_structured(model_output(rows[0]), payload(tmp_path, snapshot, context))
        rendered = render_markdown('Synthetic', result, web_search=False)
        assert '积极买入价：17.6' in rendered
        assert '常规买入价：23.76' in rendered
        assert '停止加仓价：33' in rendered
        assert '情景假设' in rendered and '敏感性' in rendered
        for file in tmp_path.glob('*.txt'):
            file.unlink()
        unavailable = create_research_job(session, name='Synthetic', code=rows[0].code, assumption_id=first_id)
        assert _job_evidence_context(unavailable)[2]['valuation_complete'] is False


def test_computed_price_in_model_prose_does_not_become_a_verified_source_fact(store, tmp_path):
    from sqlalchemy.orm import Session
    from jobs.research import create_research_job, _job_evidence_context
    from research.evaluate import normalize_structured
    with Session(store) as session:
        rows = sources(tmp_path)
        version = append(session, rows)
        job = create_research_job(session, name='Synthetic', code=rows[0].code, assumption_id=version)
        snapshot, _, context, _ = _job_evidence_context(job)
        raw = model_output(rows[0])
        raw['gates'][0].update(reason='Scenario price 23.76 is a source fact.', claim='Scenario price 23.76 is a source fact.')
        result = normalize_structured(raw, payload(tmp_path, snapshot, context))
        assert result['data_grade'] == 'C'
        assert 'financial_text_unbound' in result['gates'][0]['errors']
        assert result['evidence_contract']['valuation_complete'] is True
        assert result['price_bands']['steady'] == '23.76'


def test_retry_keeps_frozen_version_prices_and_exact_prompt_after_append(store, tmp_path, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.models import ResearchArtifact
    from jobs.research import create_research_job, _run_one
    from research.llm import FakeLLM
    monkeypatch.setattr('jobs.research.ensure_financials', lambda *args: None)
    monkeypatch.setattr('jobs.research.load_financials', lambda *args: [{'period': '2025', 'roe': 10, 'net_income': 20}])
    monkeypatch.setattr('jobs.research._saved_price', lambda *args, **kwargs: D('10'))
    monkeypatch.setattr('jobs.research._price_for_eval', lambda *args, **kwargs: D('10'))
    monkeypatch.setattr('jobs.research._industry_of', lambda *args: '')
    monkeypatch.setattr('jobs.research._load_industry_precedents', lambda *args, **kwargs: ())
    monkeypatch.setattr('jobs.research._load_discovery_risk_flags', lambda *args, **kwargs: ())
    monkeypatch.setattr('jobs.research.gate_is_open', lambda *args: False)
    monkeypatch.setattr('jobs.research.is_extreme_greed', lambda *args: False)
    monkeypatch.setattr('jobs.research.fetch_valuation', lambda *args: {})
    monkeypatch.setattr('jobs.research._fetch_notices', lambda *args: [])
    with Session(store) as session:
        rows = sources(tmp_path)
        first_id = append(session, rows)
        job = create_research_job(session, name='Synthetic', code=rows[0].code, assumption_id=first_id)
        session.commit()
        failed = FakeLLM(error=RuntimeError('synthetic retry trigger'))
        _run_one(session, job, llm=failed, quote_client=None, reports_dir=tmp_path,
            book={'positions': []}, web_search=False, as_of_date='2026-10-02')
        session.refresh(job)
        assert job.status == 'failed'
        capture = job.model_input_json
        assert json.loads(capture)['input']['research_context']['valuation']['assumption_id'] == first_id
        assert append(session, rows, scenario(rows, earnings_growth='0.2')) != first_id
        session.commit()
        for file in tmp_path.glob('*.txt'):
            file.unlink()
        def forbidden(*args, **kwargs):
            raise AssertionError('Frozen retry attempted a source/assumption refetch')
        monkeypatch.setattr('jobs.research._job_evidence_context', forbidden)
        monkeypatch.setattr('research.valuation.freeze_assumptions', forbidden)
        monkeypatch.setattr('jobs.research.load_financials', forbidden)
        retry = FakeLLM()
        _run_one(session, job, llm=retry, quote_client=None, reports_dir=tmp_path,
            book={'positions': [{'name': 'changed'}]}, web_search=True, as_of_date='2027-01-01')
        session.refresh(job)
        assert job.status == 'done'
        assert job.model_input_json == capture
        assert retry.prompts == failed.prompts
        artifact = session.scalar(select(ResearchArtifact).where(ResearchArtifact.research_job_id == job.id))
        result = json.loads(artifact.structured_json)
        assert result['price_bands']['steady'] == '23.76'
        assert result['evidence_contract']['assumption_id'] == first_id


def test_pe_uses_exact_deducted_ttm_not_reported_eps_or_annual_shares(store, tmp_path):
    from sqlalchemy.orm import Session
    from jobs.research import create_research_job, _job_evidence_context
    annual = sources(tmp_path)[0]
    current = reviewed(tmp_path, 'financial', '2026H1', {'deducted_net_income': '12',
        'net_income': '15', 'roe': '10', 'eps': '999', 'shares': '5'})
    prior = reviewed(tmp_path, 'financial', '2025H1', {'deducted_net_income': '10', 'net_income': '13'})
    rows = (annual, current, prior, *sources(tmp_path)[1:])
    with Session(store) as session:
        version = append(session, rows, scenario(rows, period='2026H1', normalized_metric_per_share='2.2'))
        job = create_research_job(session, name='Synthetic', code=annual.code, assumption_id=version)
        report = _job_evidence_context(job)[2]['valuation']['validation']['report']
        assert report['source_facts']['numerator'] == '22'
        assert report['declared_assumptions']['metric_per_share'] == '2.2'
        assert report['computed_prices']['base'] == '26.136'
