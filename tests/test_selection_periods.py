from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest

from discovery.fundamentals import freeze_fundamentals, operating_state, quarter_amount, ttm_amount
from tests.selection_helpers import evidence

CUTOFF = datetime(2026, 10, 22, tzinfo=timezone.utc)
POLICY = {
    'version': 'operating-v1', 'profit_drop': '-0.15',
    'calendar_version': 'synthetic-reviewed-2026',
    'calendar_source_url': 'https://example.invalid/official-calendar',
    'calendar_document_sha256': 'a' * 64, 'calendar_status': 'verified',
    'period_due_dates': {'2025': '2026-04-30', '2026H1': '2026-08-31'},
}


def snapshot(rows):
    return freeze_fundamentals(rows, (), (), code='000001.SZ', cutoff_at=CUTOFF, current_mcap=None)


def test_deducted_ttm_uses_comparable_cumulative_reports():
    rows = (evidence('2025', deducted_net_income='100'),
            evidence('2025H1', deducted_net_income='40'),
            evidence('2026H1', deducted_net_income='50'))
    assert ttm_amount(rows, 'deducted_net_income', '2026H1').value == Decimal('110')


@pytest.mark.parametrize('period,expected', [('2026Q1', '10'), ('2026H1', '30'), ('2026Q3', '50'), ('2026', '70')])
def test_quarters_subtract_adjacent_cumulative_amounts(period, expected):
    rows = tuple(evidence(p, deducted_net_income=v) for p, v in [('2026Q1', '10'), ('2026H1', '40'), ('2026Q3', '90'), ('2026', '160')])
    assert quarter_amount(rows, 'deducted_net_income', period).value == Decimal(expected)


def test_missing_endpoint_no_profit_substitution_and_changed_basis():
    rows = (evidence('2025', deducted_net_income='100'), evidence('2025H1', net_income='40'), evidence('2026H1', deducted_net_income='50'))
    assert ttm_amount(rows, 'deducted_net_income', '2026H1').status == 'missing'
    rows = (rows[0], evidence('2025H1', deducted_net_income='40'), replace(rows[2], basis_id='new-scope'))
    assert ttm_amount(rows, 'deducted_net_income', '2026H1').reason == 'incomparable_basis'
    with pytest.raises(ValueError):
        ttm_amount(rows, 'eps', '2026H1')


def test_exact_amounts_ignore_ambient_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 2
        result = ttm_amount((evidence('2025', revenue='123456789.125'), evidence('2025H1', revenue='1.005'), evidence('2026H1', revenue='2.006')), 'revenue', '2026H1')
    assert result.value == Decimal('123456790.126')


def test_verified_status_cannot_hide_explicitly_unknown_basis():
    row = replace(evidence('2025', deducted_net_income='100'), basis_id='unverified-consolidation-basis')
    result = ttm_amount((row,), 'deducted_net_income', '2025')
    assert result.status == 'untrusted'
    assert result.reason == 'basis_unverified'


def test_not_applicable_is_not_missing_or_confirmed_zero():
    row = replace(evidence('2025', ocf='0'), status='not_applicable', values={'ocf': None})
    assert ttm_amount((row,), 'ocf', '2025').status == 'not_applicable'
    assert ttm_amount((evidence('2025', ocf='0'),), 'ocf', '2025').value == Decimal('0')


def test_incomparable_operating_basis_keeps_source_ids_and_reason():
    from discovery.fundamental_types import evidence_identity
    rows = list(stable_rows())
    changed = replace(rows[-1], basis_id='new-consolidation')
    rows[-1] = replace(changed, evidence_id=evidence_identity(changed))
    result = operating_state(snapshot(rows), POLICY)
    assert result.state == 'unknown'
    metric = result.metrics['deducted_profit_yoy']
    assert metric.reason == 'incomparable_basis'
    assert set(metric.evidence_ids) == {row.evidence_id for row in rows}


def stable_rows(cur='50', prev='40', ocf='20', old_ocf='10'):
    return (evidence('2025H1', deducted_net_income=prev, ocf=old_ocf), evidence('2026H1', deducted_net_income=cur, ocf=ocf))


def test_operating_stable_missing_improvement_is_explicit():
    result = operating_state(snapshot(stable_rows()), POLICY)
    assert result.state == 'stable'
    assert 'improvement_evidence_insufficient' in result.reasons


@pytest.mark.parametrize('cur,prev,ocf,expected', [('34', '40', '20', 'deteriorating'), ('50', '40', '-1', 'deteriorating'), ('10', '0', '20', 'unknown'), ('10', '-10', '20', 'unknown')])
def test_operating_deterioration_and_nonpositive_comparison(cur, prev, ocf, expected):
    assert operating_state(snapshot(stable_rows(cur, prev, ocf)), POLICY).state == expected


def test_operating_missing_required_data_wins_over_trigger():
    rows = (evidence('2025H1', deducted_net_income='40', ocf='10'), evidence('2026H1', deducted_net_income='10'))
    assert operating_state(snapshot(rows), POLICY).state == 'unknown'


def test_improving_uses_same_quarter_yoy_despite_seasonality():
    rows = [evidence('2025Q1', deducted_net_income='100'), evidence('2025H1', deducted_net_income='110', ocf='5', revenue='200'),
            evidence('2026Q1', deducted_net_income='110'), evidence('2026H1', deducted_net_income='125', ocf='5', revenue='220'),
            evidence('2024', revenue='300'), evidence('2024H1', revenue='180'), evidence('2025', revenue='350')]
    result = operating_state(snapshot(rows), POLICY)
    assert result.state == 'improving'
    assert result.metrics['latest_quarter_yoy'].value == Decimal('.5')
    assert result.metrics['previous_quarter_yoy'].value == Decimal('.1')


def test_due_calendar_is_required_and_frozen_cutoff_driven():
    snap = snapshot(stable_rows())
    assert operating_state(snap, {}).state == 'unknown'
    assert operating_state(snap, {**POLICY, 'calendar_status': 'untrusted'}).state == 'unknown'
    policy = {**POLICY, 'period_due_dates': {'2026Q3': '2026-10-21'}}
    assert 'report_overdue' in operating_state(snap, policy).reasons
    assert operating_state(snap, {**policy, 'period_due_dates': {'2026Q3': '2026-10-31'}}).state == 'stable'
