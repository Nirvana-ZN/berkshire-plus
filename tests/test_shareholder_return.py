"""Synthetic shareholder execution and research realization contracts."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D
import json

import pytest

from discovery.fundamental_types import evidence_identity
from discovery.fundamentals import freeze_fundamentals, plain
from market.evidence import parse_source_evidence
from tests.selection_helpers import evidence
from research.shareholder_return import shareholder_metrics, latest_progress


def changed(row, **changes):
    row = replace(row, **changes)
    return replace(row, evidence_id=evidence_identity(row))


def execution(kind='buyback', stage='executed', amount='100', *, year=2025, **values):
    return changed(evidence(str(year), plan_id='p', stage=stage, amount=amount,
        window_start=f'{year - 1}-12-31', window_end=f'{year}-12-31',
        accumulation_scope='window', effective_date=f'{year}-12-30', **values), kind=kind)


def receipt(rows=(), *, kind='buyback', stage='executed', year=2025, **values):
    from market.evidence import parse_shareholder_coverage
    data = dict(start=f'{year - 1}-12-31', end=f'{year}-12-31', complete='true',
        scope=f'all_{kind}_{stage}', evidence_ids=json.dumps([r.evidence_id for r in rows]))
    data.update(values)
    row = parse_source_evidence(dict(code='000001.SZ', kind='shareholder_return_coverage',
        period=str(year), values=data, source_url='https://example.invalid/coverage',
        document_sha256='a' * 64, basis_id='fixture-consolidated-cny',
        report_location='reviewed register', status='verified',
        published_at='2026-10-01T00:00:00Z', observed_at='2026-10-02T00:00:00Z',
        verification=dict(method='human_review', reviewer='fixture', reviewed_at='2026-10-01T00:00:00Z')))
    return row, parse_shareholder_coverage(row, rows)


def metrics(rows=(), windows=(), **inputs):
    return shareholder_metrics(rows, coverage=windows,
        **dict(mcap=D('1000'), fcf=D('100'), deducted_profit=D('200')) | inputs)


def covered(rows, **options):
    proof, window = receipt(rows, **options)
    return (*rows, proof), (window,)


def test_announced_buyback_is_not_cash_return():
    row = replace(evidence('2026', plan_id='p', stage='proposed', amount='100'), kind='buyback')
    result = shareholder_metrics((row,), mcap=D('1000'), fcf=D('100'), deducted_profit=D('100'))
    assert result['buyback_executed'].value is None


def test_cumulative_progress_is_latest_not_sum_and_stages_do_not_overwrite():
    old = execution(amount='100')
    new = changed(execution(amount='150'), published_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
    cancellation = execution(stage='cancelled', shares='10', shares_before='100')
    rows, windows = covered((old, new))
    result = metrics((*rows, cancellation), windows)
    assert result['buyback_executed'].value == D('150')
    assert set(result['buyback_executed'].evidence_ids) >= {old.evidence_id, new.evidence_id}
    assert {row.values['stage'] for row in latest_progress((old, new, cancellation))} == {'executed', 'cancelled'}


def test_decreasing_cumulative_is_untrusted_unless_explicitly_revised():
    old = execution(amount='150')
    new = changed(execution(amount='100'), published_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
    rows, windows = covered((old, new))
    assert metrics(rows, windows)['buyback_executed'].status == 'untrusted'
    revised = changed(new, supersedes_id=old.evidence_id)
    rows, windows = covered((old, revised))
    assert metrics(rows, windows)['buyback_executed'].value == D('100')


def test_itemized_trades_dedupe_and_never_add_to_cumulative():
    trade = execution(amount='40', trade_id='t')
    trade = changed(trade, values={**trade.values, 'accumulation_scope': 'itemized'})
    duplicate = changed(trade, published_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
    rows, windows = covered((trade, duplicate))
    assert metrics(rows, windows)['buyback_executed'].value == D('40')
    rows, windows = covered((trade, execution(amount='150')))
    result = metrics(rows, windows)['buyback_executed']
    assert result.value is None and result.reason == 'mixed_execution_representations'


def test_missing_coverage_is_not_zero_and_relevant_complete_coverage_is_zero():
    assert metrics()['dividend_paid'].status == 'missing'
    proof, window = receipt(kind='dividend', stage='paid')
    result = metrics((proof,), (window,))
    assert result['dividend_paid'].value == 0
    assert result['buyback_executed'].value is None
    assert metrics((proof,), (replace(window, complete=False),))['dividend_paid'].value is None


def test_payment_year_comes_from_actual_cash_window_not_report_period():
    row = changed(execution('dividend', 'paid', '30'), period='2024')
    rows, windows = covered((row,), kind='dividend', stage='paid')
    assert metrics(rows, windows)['dividend_paid'].value == D('30')
    outside = changed(row, values={**row.values, 'effective_date': '2026-01-01'})
    with pytest.raises(ValueError, match='outside'):
        receipt((outside,), kind='dividend', stage='paid')


def test_cross_year_plan_to_date_cannot_be_an_annual_cash_amount():
    row = execution()
    row = changed(row, values={**row.values, 'window_start': '2023-12-31', 'accumulation_scope': 'plan_to_date'})
    rows, windows = covered((row,))
    assert metrics(rows, windows)['buyback_executed'].value is None


def test_withdrawn_plan_is_separate_from_actual_cash_and_incentive_is_not_cancellation():
    plan = execution(stage='proposed', amount='200')
    withdrawn = changed(plan, values={**plan.values, 'stage': 'withdrawn'}, supersedes_id=plan.evidence_id)
    actual = execution(purpose='incentive')
    rows, windows = covered((actual,))
    result = metrics((*rows, plan, withdrawn), windows)
    assert result['buyback_planned'].value is None
    assert result['buyback_executed'].value == 100
    assert result['buyback_incentive'].value == 100
    assert result['buyback_cancelled_shares'].value is None


def test_cancellation_requires_its_own_coverage_and_compatible_share_denominator():
    row = execution(stage='cancelled', shares='10', shares_before='100')
    rows, windows = covered((row,), stage='cancelled')
    result = metrics(rows, windows)
    assert result['buyback_cancelled_shares'].value == 10
    assert result['cancellation_ratio'].value == D('.1')
    assert result['buyback_executed'].value is None


def test_financing_and_net_cash_are_parallel_and_fcf_nonpositive_is_unavailable():
    buy = execution()
    issue = execution('equity_financing', amount='150')
    buyproof, buywindow = receipt((buy,))
    issueproof, issuewindow = receipt((issue,), kind='equity_financing')
    result = metrics((buy, issue, buyproof, issueproof), (buywindow, issuewindow))
    assert result['equity_financing'].value == 150
    assert result['buyback_net_of_equity_financing'].value == -50
    cash = execution('dividend', 'paid', '30')
    rows, windows = covered((cash,), kind='dividend', stage='paid')
    assert metrics(rows, windows, fcf=D('-1'))['dividend_fcf_ratio'].value is None


@pytest.mark.parametrize('amount', [None, 'NaN', '-1'])
def test_missing_or_invalid_execution_amount_is_never_zero(amount):
    row = execution(amount=amount)
    rows, windows = covered((row,))
    assert metrics(rows, windows)['buyback_executed'].value is None


def test_period_and_window_remain_distinct_and_ambiguous_scopes_fail_closed():
    old = execution(amount='100', year=2024)
    new = execution(amount='150')
    assert len(latest_progress((old, new))) == 2
    overlap = changed(new, period='2025H1', values={**new.values, 'window_start': '2025-06-30'})
    rows, windows = covered((new, overlap))
    assert metrics(rows, windows)['buyback_executed'].status == 'untrusted'


def test_reserved_metadata_guard_and_real_manual_source_import():
    row = execution(purpose='incentive')
    payload = dict(code=row.code, kind=row.kind, period=row.period, values=dict(row.values),
        source_url=row.source_url, document_sha256=row.document_sha256,
        report_location='synthetic register', status='verified', basis_id=row.basis_id,
        published_at=row.published_at.isoformat(), observed_at=row.observed_at.isoformat(),
        verification=dict(method='human_review', reviewer='fixture', reviewed_at=row.observed_at.isoformat()))
    assert parse_source_evidence(payload).values['plan_id'] == 'p'
    with pytest.raises(ValueError, match='reserved'):
        parse_source_evidence({**payload, 'values': {**payload['values'], '_window': 'fake'}})


def test_three_year_stability_requires_three_complete_calendar_cash_years():
    rows, windows = [], []
    for year in (2023, 2024, 2025):
        cash = execution('dividend', 'paid', '10', year=year)
        proof, window = receipt((cash,), kind='dividend', stage='paid', year=year)
        rows.extend((cash, proof))
        windows.append(window)
    assert metrics(tuple(rows), tuple(windows))['dividend_three_year_paid_frequency'].value == 1
    assert metrics(tuple(rows), tuple(windows[1:]))['dividend_three_year_paid_frequency'].value is None


def test_verified_cash_survives_untrusted_or_proposed_successor_and_revocation_survives_correction():
    from discovery.evidence_store import select_evidence_as_of
    cash = execution('dividend', 'paid', '30')
    for stage, status in [('paid', 'untrusted'), ('proposed', 'verified')]:
        successor = changed(cash, status=status, supersedes_id=cash.evidence_id,
                            values={**cash.values, 'stage': stage, 'amount': '300'})
        selected = select_evidence_as_of((cash, successor), code=cash.code,
            cutoff=datetime(2026, 10, 22, tzinfo=timezone.utc))
        assert cash in selected
    revoked = changed(cash, supersedes_id=cash.evidence_id, values={**cash.values, 'stage': 'withdrawn'})
    correction = changed(revoked, status='untrusted', supersedes_id=revoked.evidence_id)
    selected = select_evidence_as_of((cash, revoked, correction), code=cash.code,
        cutoff=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert cash not in selected and revoked in selected


def test_proposed_sibling_cannot_resurrect_a_verified_withdrawn_execution():
    from discovery.evidence_store import select_evidence_as_of
    cash = execution('dividend', 'paid', '30')
    revoked = changed(cash, supersedes_id=cash.evidence_id, values={**cash.values, 'stage': 'withdrawn'})
    proposal = changed(cash, supersedes_id=cash.evidence_id, values={**cash.values, 'stage': 'proposed'})
    for rows in ((cash, revoked, proposal), (cash, proposal, revoked)):
        selected = select_evidence_as_of(rows, code=cash.code, cutoff=datetime(2026, 10, 22, tzinfo=timezone.utc))
        assert cash not in selected and revoked in selected


def test_frozen_receipt_revocation_and_proof_only_history_do_not_reenter_cash():
    cash = execution('dividend', 'paid', '30')
    proof, window = receipt((cash,), kind='dividend', stage='paid')
    revocation = changed(proof, supersedes_id=proof.evidence_id,
        values={**proof.values, 'complete': 'false', 'stage': 'revoked'})
    snapshot = freeze_fundamentals((cash, proof, revocation), (), (window,), code=cash.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=D('1000'))
    assert metrics(snapshot.extensions, snapshot.coverage)['dividend_paid'].value is None
    # A corrected execution becomes current once; its historical proof is not a second payment.
    corrected = changed(cash, supersedes_id=cash.evidence_id, values={**cash.values, 'amount': '20'})
    snapshot = freeze_fundamentals((cash, corrected, proof), (), (window,), code=cash.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=D('1000'))
    assert metrics(snapshot.extensions, snapshot.coverage)['dividend_paid'].value == 20


def test_research_matches_annual_denominator_and_bank_applicability(tmp_path):
    from research.evaluate import _shareholder_audit
    from tests.test_evaluate import _input
    from tests.test_selection_capital_return import profile
    cash = execution('dividend', 'paid', '30')
    rows, windows = covered((cash,), kind='dividend', stage='paid')
    financial = evidence('2025', ocf='100', capex='40', deducted_net_income='100')
    for industry, expected in [('家用电器', 'verified'), ('银行', 'not_applicable')]:
        snapshot = freeze_fundamentals((*rows, financial, profile(industry)), (), windows, code=cash.code,
            cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=D('1000'))
        payload = replace(_input(tmp_path), code=cash.code, fundamentals=snapshot)
        audit = _shareholder_audit(payload)
        ratio = audit['metrics']['dividend_fcf_ratio']
        assert ratio['status'] == expected
        if expected == 'verified':
            assert ratio['value'] == '0.5'
            assert financial.evidence_id in ratio['evidence_ids']
        assert audit['window'] == {'start': '2024-12-31', 'end': '2025-12-31', 'annual': True}
        assert audit['metrics']['dividend_deducted_profit_ratio']['value'] == '0.3'


def test_unknown_realization_cannot_confirm_margin_and_does_not_force_rejection(tmp_path):
    from research.evaluate import GATE_NAMES, normalize_structured
    from tests.test_evaluate import _input, _rest
    raw = dict(verdict='pass', gates=[dict(name=name, status='pass', reason='source') for name in GATE_NAMES], **_rest())
    snapshot = freeze_fundamentals((), (), (), code='000001.SZ',
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
    payload = replace(_input(tmp_path), code=snapshot.code, fundamentals=snapshot)
    result = normalize_structured(raw, payload)
    assert result['realization_path']['path'] == 'unknown'
    assert result['verdict'] == 'gray'
    assert next(g for g in result['gates'] if g['name'] == '安全边际')['status'] == 'gray'
    failed = normalize_structured({**raw, 'verdict': 'fail'}, payload)
    assert failed['verdict'] == 'fail'


def test_realization_references_must_be_current_frozen_verified_sources(tmp_path):
    from research.evaluate import GATE_NAMES, normalize_structured
    from tests.test_evaluate import _input, _rest
    cash = execution('dividend', 'paid', '30')
    rows, windows = covered((cash,), kind='dividend', stage='paid')
    snapshot = freeze_fundamentals(rows, (), windows, code=cash.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
    payload = replace(_input(tmp_path), code=cash.code, fundamentals=snapshot)
    raw = dict(verdict='pass', gates=[dict(name=name, status='pass', reason='source') for name in GATE_NAMES], **_rest())
    path = dict(path='dividend', evidence_ids=[cash.evidence_id], falsification_condition='Paid dividends cease.')
    result = normalize_structured({**raw, 'realization_path': path}, payload)
    assert result['realization_path']['status'] == 'verified'
    assert result['verdict'] == 'gray'
    assert result['data_grade'] == 'C'
    assert result['price_bands']['mode'] == 'watch'
    assert normalize_structured({**raw, 'verdict': 'fail', 'realization_path': path}, payload)['verdict'] == 'fail'
    assert normalize_structured({**raw, 'realization_path': {**path, 'evidence_ids': ['invented']}}, payload)['verdict'] == 'gray'
    assert normalize_structured({**raw, 'realization_path': {**path, 'falsification_condition': ''}}, payload)['verdict'] == 'gray'


def test_nonannual_cash_has_no_annual_coverage_ratios_and_denominator_year_does_not_fallback(tmp_path):
    from research.evaluate import _shareholder_audit
    from tests.test_evaluate import _input
    from tests.test_selection_capital_return import profile
    cash = execution('dividend', 'paid', '30')
    rows, windows = covered((cash,), kind='dividend', stage='paid')
    prior = evidence('2024', ocf='100', capex='40', deducted_net_income='100')
    snapshot = freeze_fundamentals((*rows, prior, profile()), (), windows, code=cash.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
    result = _shareholder_audit(replace(_input(tmp_path), code=cash.code, fundamentals=snapshot))
    assert result['metrics']['dividend_deducted_profit_ratio']['value'] is None
    partial = changed(cash, values={**cash.values, 'window_start': '2025-06-30'})
    proof, window = receipt((partial,), kind='dividend', stage='paid', start='2025-06-30')
    assert metrics((partial, proof), (window,))['dividend_paid'].value == 30
    assert metrics((partial, proof), (window,))['dividend_deducted_profit_ratio'].reason == 'annual_denominator_window_mismatch'


def test_untrusted_missing_execution_preserves_status_and_stages_are_independent():
    unknown = changed(execution(amount=None), status='untrusted')
    result = metrics((unknown,))['buyback_executed']
    assert result.status == 'untrusted'
    cash = execution('dividend', 'paid', '30')
    rows, windows = covered((cash,), kind='dividend', stage='paid')
    malformed = changed(execution(), kind='buyback_trade', values={'stage': 'traded'})
    assert metrics((*rows, malformed), windows)['dividend_paid'].value == 30


def test_shared_selector_resolves_verified_plan_revisions_without_hiding_cash():
    from discovery.evidence_store import select_evidence_as_of
    proposed = execution(stage='proposed')
    approved = changed(proposed, supersedes_id=proposed.evidence_id,
        values={**proposed.values, 'stage': 'approved', 'amount': '150'})
    selected = select_evidence_as_of((proposed, approved), code=proposed.code,
        cutoff=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert selected == (approved,)


@pytest.mark.parametrize('change', [dict(complete='false'), dict(scope='all_buyback_executed'), dict(end='2025-11-30')])
def test_frozen_receipt_revision_invalidates_original_coverage(change):
    cash = execution('dividend', 'paid', '30')
    proof, window = receipt((cash,), kind='dividend', stage='paid')
    revision = changed(proof, supersedes_id=proof.evidence_id, values={**proof.values, **change})
    snapshot = freeze_fundamentals((cash, proof, revision), (), (window,), code=cash.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
    assert metrics(snapshot.extensions, snapshot.coverage)['dividend_paid'].value is None


def test_untrusted_original_receipt_cannot_gain_trust_from_its_verified_successor():
    from discovery.fundamental_types import CoverageWindow
    cash = execution('dividend', 'paid', '30')
    proof, window = receipt((cash,), kind='dividend', stage='paid')
    original = changed(proof, status='untrusted')
    successor = changed(proof, supersedes_id=original.evidence_id)
    window = replace(window, evidence_ids=(cash.evidence_id, original.evidence_id))
    snapshot = freeze_fundamentals((cash, original, successor), (), (window,), code=cash.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
    assert metrics(snapshot.extensions, snapshot.coverage)['dividend_paid'].value is None


def test_progress_trade_ids_and_actual_shanghai_business_dates():
    row = execution()
    row = changed(row, values={**row.values, 'effective_date': None, 'effective_at': '2025-12-31T16:00:00Z'})
    with pytest.raises(ValueError, match='outside'):
        receipt((row,))
    trade = changed(execution(), values={**execution().values, 'accumulation_scope': 'itemized'})
    rows, windows = covered((trade,))
    assert metrics(rows, windows)['buyback_executed'].status == 'untrusted'


def test_fcf_basis_conflict_and_numeric_context_are_not_silently_accepted(tmp_path):
    from decimal import localcontext
    from research.evaluate import _shareholder_audit
    from tests.test_evaluate import _input
    from tests.test_selection_capital_return import profile
    cash = execution('dividend', 'paid', '30')
    rows, windows = covered((cash,), kind='dividend', stage='paid')
    ocf = evidence('2025', ocf='100', deducted_net_income='100')
    capex = changed(evidence('2025', capex='40'), basis_id='different-scope')
    snapshot = freeze_fundamentals((*rows, ocf, capex, profile()), (), windows, code=cash.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
    audit = _shareholder_audit(replace(_input(tmp_path), code=cash.code, fundamentals=snapshot))
    assert audit['metrics']['dividend_fcf_ratio']['status'] == 'untrusted'
    with localcontext() as context:
        context.prec = 2
        result = metrics(rows, windows, mcap=D('1000'))['dividend_asof_yield']
        assert result.value == D('.03')


def test_actual_checklist_output_and_markdown_expose_separate_execution_and_path(tmp_path):
    from research.evaluate import GATE_NAMES, run_checklist
    from tests.test_evaluate import _input, _rest
    cash = execution('dividend', 'paid', '30')
    rows, windows = covered((cash,), kind='dividend', stage='paid')
    snapshot = freeze_fundamentals(rows, (), windows, code=cash.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
    payload = replace(_input(tmp_path), code=cash.code, fundamentals=snapshot)
    class Model:
        def complete(self, prompt, **kwargs):
            assert cash.evidence_id in prompt and 'realization_path' in prompt
            return json.dumps(dict(verdict='pass', gates=[dict(name=n, status='pass', reason='source') for n in GATE_NAMES],
                realization_path=dict(path='dividend', evidence_ids=[cash.evidence_id], falsification_condition='Cash payouts cease.'), **_rest()))
    output = run_checklist(Model(), payload)
    assert output.structured['shareholder_return']['metrics']['dividend_paid']['value'] == '30'
    assert '股东回报兑现与价值实现' in output.markdown
    assert 'Cash payouts cease.' in output.markdown and cash.evidence_id in output.markdown


def test_imported_asset_realization_is_a_dated_path_but_never_shareholder_cash(tmp_path):
    from research.evaluate import GATE_NAMES, normalize_structured
    from tests.test_evaluate import _input, _rest
    payload = dict(code='000001.SZ', kind='asset_realization', period='2026',
        values=dict(stage='implemented', amount='100', effective_date='2026-10-01'),
        source_url='https://example.invalid/disposal', document_sha256='b' * 64,
        report_location='actual completion notice', status='verified', basis_id='fixture-consolidated-cny',
        published_at='2026-10-02T00:00:00Z', observed_at='2026-10-03T00:00:00Z',
        verification=dict(method='human_review', reviewer='fixture', reviewed_at='2026-10-03T00:00:00Z'))
    source = parse_source_evidence(payload)
    raw = dict(verdict='pass', gates=[dict(name=n, status='pass', reason='source') for n in GATE_NAMES],
        realization_path=dict(path='asset_realization', evidence_ids=[source.evidence_id],
            falsification_condition='Settlement is reversed or the proceeds become unavailable.'), **_rest())
    snapshot = freeze_fundamentals((source,), (), (), code=source.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
    result = normalize_structured(raw, replace(_input(tmp_path), code=source.code, fundamentals=snapshot))
    assert result['realization_path']['status'] == 'verified'
    assert result['shareholder_return']['metrics']['dividend_paid']['value'] is None
    assert result['shareholder_return']['metrics']['buyback_cancelled_shares']['value'] is None
    for effective in (None, '2026-11-01'):
        bad = parse_source_evidence({**payload, 'values': {**payload['values'], 'effective_date': effective}})
        snapshot = freeze_fundamentals((bad,), (), (), code=bad.code,
            cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
        got = normalize_structured({**raw, 'realization_path': {**raw['realization_path'], 'evidence_ids': [bad.evidence_id]}},
            replace(_input(tmp_path), code=bad.code, fundamentals=snapshot))
        assert got['realization_path']['path'] == 'unknown'
    with pytest.raises(ValueError, match='reserved'):
        parse_source_evidence({**payload, 'values': {**payload['values'], '_implemented': 'true'}})


def test_invalid_older_cash_scope_does_not_poison_a_separate_complete_new_year():
    old = execution(amount='150', year=2024)
    decrease = changed(execution(amount='100', year=2024), published_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
    current = execution(amount='200')
    rows, windows = covered((current,))
    assert metrics((*rows, old, decrease), windows)['buyback_executed'].value == 200


def test_revoked_receipt_cannot_supply_missing_facts_to_a_different_valid_receipt():
    cash = execution('dividend', 'paid', '30')
    extra = changed(execution('dividend', 'paid', '50'), values={**cash.values, 'amount': '50', 'plan_id': 'extra'})
    proof, window = receipt((cash,), kind='dividend', stage='paid')
    other, otherwindow = receipt((extra,), kind='dividend', stage='paid')
    revoked = changed(other, supersedes_id=other.evidence_id, values={**other.values, 'complete': 'false'})
    result = metrics((cash, extra, proof, other, revoked), (window, otherwindow))['dividend_paid']
    assert result.status == 'untrusted' and result.value is None


def test_cancelled_share_ratio_preserves_untrusted_denominator_status():
    row = execution(stage='cancelled', shares='10', shares_before='NaN')
    rows, windows = covered((row,), stage='cancelled')
    assert metrics(rows, windows)['cancellation_ratio'].status == 'untrusted'


@pytest.mark.parametrize('free_cash', ['0', '-1'])
def test_nonpositive_applicable_fcf_is_not_a_coverage_multiple(free_cash):
    from tests.test_selection_capital_return import profile
    cash = execution('dividend', 'paid', '30')
    rows, windows = covered((cash,), kind='dividend', stage='paid')
    snapshot = freeze_fundamentals((*rows, profile()), (), windows, code=cash.code,
        cutoff_at=datetime(2026, 10, 22, tzinfo=timezone.utc), current_mcap=None)
    result = metrics(snapshot.extensions, windows, fcf=D(free_cash))['dividend_fcf_ratio']
    assert result.value is None and result.reason == 'fcf_not_positive'


def test_identical_trade_id_dedupes_across_reporting_period_and_source_metadata():
    row = execution(trade_id='t')
    row = changed(row, values={**row.values, 'accumulation_scope': 'itemized', '_report_location': 'page 1'})
    repeat = changed(row, period='2026', values={**row.values, '_report_location': 'page 2'},
        published_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert len(latest_progress((row, repeat))) == 1


def test_identical_cumulative_assertions_dedupe_with_independent_source_provenance():
    row = execution()
    repeat = changed(row, values={**row.values, '_report_location': 'other source page'})
    assert len(latest_progress((row, repeat))) == 1


def test_precise_future_asset_execution_is_not_supported_on_the_same_business_date(tmp_path):
    from research.evaluate import _normalize_realization, _shareholder_audit
    from tests.test_evaluate import _input
    row = changed(evidence('2026', stage='implemented', amount='100', effective_at='2026-10-22T10:00:00Z'), kind='asset_realization')
    snapshot = freeze_fundamentals((row,), (), (), code=row.code,
        cutoff_at=datetime(2026, 10, 22, 1, tzinfo=timezone.utc), current_mcap=None)
    payload = replace(_input(tmp_path), code=row.code, fundamentals=snapshot)
    got = _normalize_realization(dict(path='asset_realization', evidence_ids=[row.evidence_id],
        falsification_condition='Settlement is reversed.'), payload, _shareholder_audit(payload))
    assert got['path'] == 'unknown'


def test_untrusted_applicability_is_distinct_from_missing_classification():
    classification = changed(evidence('2025'), kind='company_classification', status='untrusted',
        values={'_industry_branch': 'bank', '_classification_version': 'industry-branch-v1'})
    result = metrics((classification,))['dividend_fcf_ratio']
    assert result.status == 'untrusted'


@pytest.mark.parametrize('kind,stage,key', [
    ('dividend', 'paid', 'dividend_paid'),
    ('buyback', 'executed', 'buyback_executed'),
    ('buyback', 'cancelled', 'buyback_cancelled_shares'),
    ('equity_financing', 'executed', 'equity_financing'),
])
@pytest.mark.parametrize('source_status,expected_status', [('untrusted', 'untrusted'), ('verified', 'missing')])
def test_execution_trust_survives_an_unrelated_complete_reporting_window(kind, stage, key, source_status, expected_status):
    row = changed(execution(kind, stage, shares='10'), status=source_status)
    proof, incomplete = receipt((row,), kind=kind, stage=stage, complete='false')
    before = metrics((row, proof), (incomplete,))[key]
    assert before.status == expected_status and before.value is None
    assert row.evidence_id in before.evidence_ids

    unrelated_kind, unrelated_stage, unrelated_key = (
        ('buyback', 'executed', 'buyback_executed') if kind != 'buyback'
        else ('dividend', 'paid', 'dividend_paid'))
    other_proof, other_window = receipt(kind=unrelated_kind, stage=unrelated_stage)
    result = metrics((row, proof, other_proof), (incomplete, other_window))
    after = result[key]
    assert after.status == expected_status and after.value is None
    assert after.reason == 'execution_coverage_missing'
    assert {row.evidence_id, proof.evidence_id} <= set(after.evidence_ids)
    assert other_proof.evidence_id not in after.evidence_ids
    assert result[unrelated_key].status == 'verified' and result[unrelated_key].value == 0
