from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

import pytest

from discovery.fundamental_types import evidence_identity
from discovery.fundamentals import freeze_fundamentals
from tests.selection_helpers import evidence

CUTOFF = datetime(2026, 10, 22, tzinfo=timezone.utc)


def freeze(rows, **kwargs):
    return freeze_fundamentals(rows, (), (), code='000001.SZ', cutoff_at=CUTOFF, current_mcap=None, **kwargs)


def identified(row):
    return replace(row, evidence_id=evidence_identity(row))


def test_freeze_partitions_financials_not_every_year_period():
    annual = evidence('2025', revenue='100')
    interim = evidence('2026H1', revenue='50')
    classification = identified(replace(annual, kind='company_classification', values={'_industry_branch': 'bank', '_classification_version': 'fixture-v1'}))
    snap = freeze((annual, interim, classification))
    assert snap.annuals == (annual,)
    assert snap.interims == (interim,)
    assert snap.extensions == (classification,)
    assert freeze((interim, classification, annual)).fingerprint == snap.fingerprint
    assert freeze((annual, interim)).fingerprint != snap.fingerprint


def test_late_revision_and_date_only_never_enter_old_snapshot():
    old = evidence('2025', revenue='100')
    new = identified(replace(old, values={'revenue': '999'}, supersedes_id=old.evidence_id, observed_at=CUTOFF + timedelta(seconds=1)))
    assert freeze((old, new)).fingerprint == freeze((old,)).fingerprint
    date_only = identified(replace(old, published_at=CUTOFF, observed_at=CUTOFF, date_only=True))
    assert freeze((date_only,)).annuals == ()


def test_eligible_restated_version_replaces_original():
    old = evidence('2025', revenue='100')
    new = identified(replace(old, values={'revenue': '110'}, supersedes_id=old.evidence_id, observed_at=CUTOFF))
    assert freeze((old, new)).annuals == (new,)


def bundle_with_evidence():
    from tests.test_discovery_data import _bundle
    from tests.test_selection_periods import POLICY, stable_rows
    return replace(_bundle('000001.SZ'), evidence_records=stable_rows('20'), operating_policy=POLICY)


def test_schema4_roundtrip_legacy_reader_and_integrity():
    from discovery.company_payload import serialize_company_payload, parse_company_payload
    from discovery.fundamentals import fundamentals_from_payload
    bundle = bundle_with_evidence()
    text = serialize_company_payload(bundle, captured_at=CUTOFF)
    payload = parse_company_payload(text)
    assert payload['schema_version'] == 4
    assert payload['operating_result']['state'] == 'deteriorating'
    assert len(payload['interim_history']) == 2
    assert payload['financial_rows'][0]['period_type'] == 'annual'
    assert fundamentals_from_payload(payload, code=bundle.code).interims == freeze(bundle.evidence_records).interims
    legacy_keys = {'schema_version', 'captured_at', 'code', 'security_state', 'source_as_of', 'financial_rows', 'valuation_points'}
    legacy = {key: value for key, value in payload.items() if key in legacy_keys}
    legacy['schema_version'] = 3
    old_text = json.dumps(legacy, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    assert parse_company_payload(old_text)['schema_version'] == 3
    payload['interim_history'][0]['values']['ocf'] = '999'
    with pytest.raises(ValueError, match='identity'):
        parse_company_payload(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False))


@pytest.mark.parametrize('wrong_code,wrong_day', [(False, False), (True, False), (False, True)])
def test_current_cap_uses_frozen_price_day_and_retains_source(wrong_code, wrong_day):
    from discovery.company_payload import serialize_company_payload, parse_company_payload
    from market.types import CurrentMarketCap, ValuationPoint
    from datetime import date
    quote = CurrentMarketCap('600000.SH' if wrong_code else '000001.SZ', Decimal('123456.789'),
                              date(2026, 10, 20 if wrong_day else 21), CUTOFF,
                              'eastmoney:RPT_VALUEANALYSIS_DET', 'https://example.invalid/cap', 'b' * 64, 'verified', None)
    bundle = replace(bundle_with_evidence(), current_market_cap=quote,
                     valuation_points=(ValuationPoint('000001.SZ', '2026-10', '2026-10-21', None, None, Decimal('10')),))
    payload = parse_company_payload(serialize_company_payload(bundle, captured_at=CUTOFF))
    cap = next(row for row in payload['fundamental_extensions'] if row['kind'] == 'current_market_cap')
    assert payload['current_mcap'] == (None if wrong_code or wrong_day else '123456.789')
    assert cap['document_sha256'] == quote.response_sha256
    assert cap['values']['_market_date'] == quote.market_date.isoformat()
    assert cap['observed_at'] == quote.observed_at.isoformat()
    assert cap['status'] == ('untrusted' if wrong_code or wrong_day else 'verified')


def test_unavailable_cap_retains_provider_reason_and_requires_frozen_price_day():
    from discovery.fundamentals import market_cap_evidence
    from market.types import CurrentMarketCap
    from datetime import date
    quote = CurrentMarketCap('000001.SZ', None, None, CUTOFF, 'eastmoney:RPT_VALUEANALYSIS_DET',
                              'https://example.invalid/empty', 'd' * 64, 'missing', 'current_mcap_market_date_missing')
    record = market_cap_evidence(quote, code=quote.code, market_date=date(2026, 10, 21), cutoff_at=CUTOFF)
    assert record.status == 'missing'
    assert record.values['_reason'] == quote.reason
    assert record.values['_quote_reason'] == quote.reason
    record = market_cap_evidence(quote, code=quote.code, market_date=None, cutoff_at=CUTOFF)
    assert record.values['_reason'] == 'frozen_price_market_day_missing'
    assert record.values['_quote_reason'] == quote.reason


def test_discovery_fetch_switch_reaches_evidence_and_quote_producers(tmp_path):
    from market.eastmoney import EastMoneyClient
    from tests.test_discovery_data import _security
    from market.types import FinancialRow, CurrentMarketCap
    from datetime import date
    calls = []
    class Client(EastMoneyClient):
        def _discovery_financials_for(self, code, **kwargs):
            kwargs['before_request']()
            return [FinancialRow(code, '2025', 'annual')]
        def fetch_valuation_history(self, code, **kwargs):
            kwargs['before_request']()
            return []
        def fetch_fundamental_evidence(self, code, *, source_dir, before_request):
            assert source_dir == tmp_path
            before_request()
            calls.append('evidence')
            return bundle_with_evidence().evidence_records
        def fetch_current_market_cap(self, code, *, market_date, before_request):
            assert market_date == date.fromisoformat(_security(code).last_trade_date)
            before_request()
            calls.append('quote')
            return CurrentMarketCap(code, Decimal('100'), market_date, CUTOFF, 'eastmoney:RPT_VALUEANALYSIS_DET',
                                    'https://example.invalid/cap', 'a' * 64, 'verified', None)
    attempts = []
    result = Client().fetch_discovery_data('000001.SZ', security_state=_security('000001.SZ'),
                                           before_request=lambda: attempts.append(1),
                                           selection_fundamentals=True, selection_source_dir=tmp_path)
    assert calls == ['evidence', 'quote']
    assert len(attempts) == result.request_count == 4
    assert result.evidence_records
    assert result.current_market_cap.value == Decimal('100')


def test_provider_producer_preserves_notice_observation_hash_and_separate_trust():
    from market.eastmoney import parse_fundamental_report_response
    raw = b'{"success":true,"result":{"data":[{"SECUCODE":"000001.SZ","REPORT_DATE":"2026-06-30","DATE_TYPE_CODE":"002","NOTICE_DATE":"2026-08-20","UPDATE_DATE":"2026-10-21","TOTAL_OPERATE_INCOME":123.4567890123456789,"DEDUCT_PARENT_NETPROFIT":10}]}}'
    rows = parse_fundamental_report_response(raw, code='000001.SZ', report='RPT_DMSK_FN_INCOME', observed_at=CUTOFF, source_url='https://example.invalid/raw')
    row = next(row for row in rows if 'revenue' in row.values)
    assert row.values['revenue'] == '123.4567890123456789'
    assert row.status == 'untrusted'
    assert row.date_only is True
    assert row.published_at.date().isoformat() == '2026-08-19'  # Shanghai midnight, UTC storage.
    assert row.observed_at == CUTOFF
    assert row.document_sha256 == sha256(raw).hexdigest()
    missing_notice = raw.replace(b'"NOTICE_DATE":"2026-08-20",', b'')
    assert parse_fundamental_report_response(missing_notice, code='000001.SZ', report='RPT_DMSK_FN_INCOME', observed_at=CUTOFF, source_url='https://example.invalid/raw') == ()


def test_classification_inherits_real_source_trust_and_provenance():
    from discovery.fundamentals import classification_evidence
    source = evidence('2025', revenue='100')
    source = identified(replace(source, values={**source.values, '_industry': '银行'}))
    classified = classification_evidence(source, industry='银行', version='test-v1')
    assert classified.values['_industry_branch'] == 'bank'
    assert classified.document_sha256 == source.document_sha256
    assert classified.source_url == source.source_url
    assert classification_evidence(replace(source, status='untrusted'), industry='银行', version='test-v1').status == 'untrusted'


def test_reviewed_profile_import_derives_classification_without_reserved_key_injection():
    from market.evidence import parse_source_evidence
    source = {'source': 'normalized', 'code': '000001.SZ', 'kind': 'company_profile', 'period': '2026',
              'industry': '银行', 'values': {}, 'published_at': '2026-08-01T00:00:00+00:00',
              'observed_at': '2026-08-02T00:00:00+00:00', 'source_url': 'https://example.invalid/official-profile',
              'document_sha256': 'c' * 64, 'report_location': 'profile page 1', 'basis_id': 'profile-v1',
              'status': 'verified', 'verification': {'method': 'human_review', 'reviewer': 'fixture-reviewer',
                                                    'reviewed_at': '2026-08-02T00:00:00+00:00'}}
    row = parse_source_evidence(source)
    snap = freeze((row,))
    classified = next(item for item in snap.extensions if item.kind == 'company_classification')
    assert classified.status == 'verified'
    assert classified.values['_industry_branch'] == 'bank'
    assert classified.values['_classification_version'] == 'industry-branch-v1'
    assert classified.document_sha256 == row.document_sha256
    assert classified.values['_source_evidence_id'] == row.evidence_id
    assert snap.annuals == ()
    with pytest.raises(ValueError, match='reserved'):
        parse_source_evidence({**source, 'values': {'_industry': '银行'}})
    with pytest.raises(ValueError, match='industry'):
        parse_source_evidence({key: value for key, value in source.items() if key != 'industry'})
    with pytest.raises(ValueError, match='review'):
        parse_source_evidence({key: value for key, value in source.items() if key != 'verification'})


def test_byte_fetch_is_opt_in_retained_and_policy_counted(tmp_path):
    from market.eastmoney import EastMoneyClient
    class Client(EastMoneyClient):
        def _get_json_direct_retry(self, url, *, before_request=None, raw_response=False):
            assert raw_response
            before_request()
            return b'{"success":true,"result":{"data":[]}}'
    attempts = []
    client = Client()
    assert client.fetch_fundamental_evidence('000001.SZ', source_dir=tmp_path, before_request=lambda: attempts.append(1)) == ()
    assert attempts
    retained = list(tmp_path.glob('*.json'))
    assert len(retained) == 1
    assert retained[0].stem == sha256(retained[0].read_bytes()).hexdigest()


def test_repository_selector_and_freeze_share_late_revision_boundary(tmp_path):
    from app.db import init_db
    from sqlalchemy.orm import Session
    from discovery.evidence_store import append_evidence, load_evidence_as_of
    engine = init_db(tmp_path / 'evidence.db')
    old = evidence('2025', revenue='100')
    new = identified(replace(old, values={'revenue': '999'}, supersedes_id=old.evidence_id, observed_at=CUTOFF + timedelta(days=1)))
    with Session(engine) as session:
        append_evidence(session, old)
        append_evidence(session, new)
        assert load_evidence_as_of(session, old.code, CUTOFF) == freeze((old, new)).annuals
    engine.dispose()


def test_frozen_competition_metrics_and_research_use_same_operating_input(tmp_path):
    from discovery.fundamentals import snapshot_payload
    from discovery.snapshot import FrozenCompanyInput
    from jobs.discovery import _competition_metrics
    from research.evaluate import EvalInput, _enrich_valuation
    from tests.test_selection_periods import POLICY
    from discovery.fundamentals import operating_state, plain
    snap = freeze(bundle_with_evidence().evidence_records)
    fields = {**snapshot_payload(snap), 'operating_policy': POLICY, 'operating_result': plain(operating_state(snap, POLICY))}
    company = FrozenCompanyInput('000001.SZ', 'fixture', '银行', {}, fields, {}, {}, 'd', 'f', 'v', {}, 'v7', 'fingerprint', CUTOFF)
    metrics = _competition_metrics(company, {}, ('interim_deterioration',))
    assert metrics['freeze_new_positions'] is True
    assert metrics['operating_result']['metrics']['deducted_profit_yoy']['evidence_ids']
    request = EvalInput('fixture', '000001.SZ', (), Decimal('100'), {}, False, '2026-10-22', 1, tmp_path,
                        fundamentals=snap, operating_policy=POLICY)
    valuation = _enrich_valuation(request)
    assert valuation['operating_result'] == metrics['operating_result']
    assert valuation['deducted_pe_result']['status'] == 'missing'


@pytest.mark.parametrize('state,completeness', [('deteriorating', 'complete'), ('unknown', 'partial')])
def test_actual_case_card_contains_frozen_policy_triggers_and_provenance(state, completeness):
    from discovery.evidence import build_evidence_card
    from tests.test_discovery_evidence import metrics, ruler_payload, SOURCE_AS_OF
    from tests.test_selection_periods import POLICY
    result = {'state': state, 'period': '2026H1', 'reasons': ['required_gap' if state == 'unknown' else 'deducted_profit_drop'],
              'metrics': {'deducted_profit_yoy': {'value': '-0.5', 'evidence_ids': ['fixture-source']}}}
    card = build_evidence_card('000001.SZ', metrics(operating_result=result, operating_policy=POLICY,
                                                    fundamental_fingerprint='frozen-hash'), ruler_payload(), source_as_of=SOURCE_AS_OF)
    item = next(item for item in card.items if '.risk.operating.' in item.rule_id)
    assert item.data_completeness == completeness
    assert item.input_fields['operating_result'] == result
    assert item.input_fields['operating_policy'] == POLICY
    assert item.input_fields['fundamental_fingerprint'] == 'frozen-hash'


def test_real_schema4_fill_snapshot_ignores_mutable_financial_and_late_evidence(tmp_path):
    from app.db import init_db
    from app.models import Financial, DiscoveryDataCheckpoint
    from sqlalchemy.orm import Session
    from tests.test_discovery_data import _fill, _bundle, FakeClock
    from market.eastmoney import FakeMarketClient
    from jobs.discovery_data import execute_discovery_data_fill, RequestPolicy
    from discovery.snapshot import build_production_snapshot
    from discovery.evidence_store import append_evidence
    from tests.test_selection_periods import POLICY
    engine = init_db(tmp_path / 'frozen-fill.db')
    code = '000001.SZ'
    bundle = replace(_bundle(code), evidence_records=bundle_with_evidence().evidence_records, operating_policy=POLICY)
    with Session(engine) as session:
        fill = _fill(session, [code], now=CUTOFF)
        execute_discovery_data_fill(session, FakeMarketClient(discovery_data={code: bundle}), fill.id,
                                     policy=RequestPolicy(requests_per_second=100, max_attempts=1, daily_budget=100, max_runtime_seconds=60),
                                     clock=FakeClock(CUTOFF))
        before = build_production_snapshot(session)
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill.id, code))
        checkpoint_bytes = checkpoint.payload_json
        row = session.get(Financial, (code, '2025', 'annual'))
        row.deducted_net_income = Decimal('999999')
        row.ocf = Decimal('-99999')
        late = identified(replace(bundle.evidence_records[-1], observed_at=CUTOFF + timedelta(days=1),
                                  values={'deducted_net_income': '999999', 'ocf': '999999'},
                                  supersedes_id=bundle.evidence_records[-1].evidence_id))
        append_evidence(session, late)
        session.commit()
        after = build_production_snapshot(session)
        assert before == after
        assert checkpoint.payload_json == checkpoint_bytes
        assert before.companies[0].fields['operating_result']['state'] == 'deteriorating'
        assert len(before.companies[0].fields['interim_history']) == 2
    engine.dispose()


def test_competition_candidate_and_audit_ignore_live_financial_mutation(tmp_path):
    from app.db import init_db
    from app.models import Financial
    from app.discovery_models import DiscoveryStage, DiscoveryResult, DiscoveryEvidence
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from tests.discovery_helpers import add_unfrozen_run, versions, company
    from tests.test_discovery_common import _add_track_stage, _add_track_result
    from tests.test_discovery_evidence import metrics
    from tests.test_discovery_tracks_quality import quality_payload
    from discovery.snapshot import freeze_inputs, load_frozen_inputs, canonical_json, _sha256
    from discovery.evidence_store import append_evidence
    from discovery.fundamentals import snapshot_payload, operating_state, plain
    from tests.test_selection_periods import POLICY
    from jobs.discovery import _track_candidates, builtin_track_executor, TrackExecutionContext, _validate_track_outcome, _execute_track_stage
    from discovery.builds import BUILD_VERSION
    engine = init_db(tmp_path / 'competition.db')
    records = tuple(identified(replace(row, code='600000.SH')) for row in bundle_with_evidence().evidence_records)
    snap = freeze_fundamentals(records, (), (), code='600000.SH', cutoff_at=CUTOFF, current_mcap=None)
    fields = {**snapshot_payload(snap), 'operating_policy': POLICY, 'operating_result': plain(operating_state(snap, POLICY))}
    item = replace(company('600000.SH'), fields=fields,
                   as_of={'financial': '2026-08-31', 'security': '2026-10-21', 'valuation': '2026-10-21'})
    with Session(engine) as session:
        run = add_unfrozen_run(session, week_key='2026-W43')
        rulers = json.loads(run.ruler_payloads_json)
        rulers['quality_discount']['payload'] = quality_payload()
        rulers['quality_discount']['sha256'] = _sha256(quality_payload())
        run.ruler_payloads_json = canonical_json(rulers)
        run.current = True
        for record in records:
            append_evidence(session, record)
        freeze_inputs(session, run, (item,), versions(), {'SH': ['2026-10-21']}, snapshot_at=CUTOFF)
        gate = _add_track_stage(session, run, track='quality_discount', created_at=CUTOFF, status='confirmed')
        result = _add_track_result(session, gate)
        result.metrics_json = canonical_json(metrics(code='600000.SH', branch='nonfinancial', quality_branch='nonfinancial'))
        stage = DiscoveryStage(run_id=run.id, track='quality_discount', stage='competition', attempt=1, status='queued',
                               input_fingerprint=run.input_fingerprint, algorithm_version=BUILD_VERSION,
                               ruler_version_id=1, common_stage_id=None, predecessor_stage_id=gate.id,
                               predecessor_stage='track_gate', created_at=CUTOFF, updated_at=CUTOFF)
        session.add(stage)
        session.flush()
        def outcome():
            frozen = load_frozen_inputs(session, run.id)
            context = TrackExecutionContext(stage.id, run.id, 'quality_discount', 'competition', 1,
                                             run.input_fingerprint, 1, BUILD_VERSION, None, gate.id, 'track_gate',
                                             frozen, quality_payload(), _track_candidates(session, stage))
            return frozen.input_fingerprint, _validate_track_outcome(context, builtin_track_executor(context))
        before = outcome()
        session.add(Financial(code='600000.SH', period='2026H1', period_type='interim',
                              deducted_net_income=Decimal('99999999'), ocf=Decimal('9999999')))
        session.commit()
        assert outcome() == before
        audit = json.loads(before[1].results[0].metrics_json)
        assert audit['operating_result']['state'] == 'deteriorating'
        assert audit['freeze_new_positions'] is True
        assert 'interim_deterioration' in audit['risk_flags']
        assert any('operating.deteriorating' in row.rule_id for row in before[1].evidence)
        session.commit()
        assert _execute_track_stage(session, stage.id, builtin_track_executor, lambda: CUTOFF)
        def persisted():
            return ([(row.rank, row.metrics_json) for row in session.scalars(select(DiscoveryResult).where(DiscoveryResult.stage == 'competition'))],
                    [(row.rule_id, row.input_fields_json) for row in session.scalars(select(DiscoveryEvidence).order_by(DiscoveryEvidence.id))],
                    load_frozen_inputs(session, run.id).input_fingerprint)
        persisted_before = persisted()
        assert json.loads(persisted_before[0][0][1])['operating_result'] == audit['operating_result']
        late = identified(replace(records[-1], values={'deducted_net_income': '1', 'ocf': '-100'},
                                  observed_at=CUTOFF + timedelta(days=1), supersedes_id=records[-1].evidence_id))
        append_evidence(session, late)
        live = session.get(Financial, ('600000.SH', '2026H1', 'interim'))
        live.deducted_net_income = Decimal('-99999999')
        session.commit()
        assert persisted() == persisted_before
        assert outcome() == before
    engine.dispose()


def test_freeze_reuses_trusted_action_supersession_and_coverage_references():
    from tests.test_corporate_actions import action, changed
    from discovery.fundamentals import fundamentals_from_payload, snapshot_payload
    from discovery.fundamental_types import CoverageWindow
    from datetime import date
    original = action()
    untrusted = changed(original, status='untrusted', supersedes_id=original.evidence_id,
                         values={**original.values, 'shares_after': '80'})
    snapshot = freeze((original, untrusted))
    assert len(snapshot.actions) == 1
    assert snapshot.actions[0].shares_after == Decimal('90')
    corrected = changed(untrusted, status='verified')
    coverage = CoverageWindow(original.code, 'capital_event', date(2025, 1, 1), date(2026, 10, 1), True, (original.evidence_id,))
    snapshot = freeze_fundamentals((original, corrected), (), (coverage,), code=original.code, cutoff_at=CUTOFF, current_mcap=None)
    restored = fundamentals_from_payload(snapshot_payload(snapshot), code=original.code)
    assert restored == snapshot
    assert restored.actions[0].shares_after == Decimal('80')
