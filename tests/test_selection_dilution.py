from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D, Inexact, ROUND_DOWN, localcontext

import pytest

from discovery.capital_actions import capital_change_audit, economic_dilution
from discovery.fundamental_types import CapitalEvent


def action(event_id, kind, before, after, factor='1', *, sequence=1,
           at='2026-01-01T00:00:00+00:00'):
    return CapitalEvent(event_id, '000001.SZ', kind, datetime.fromisoformat(at),
                        sequence, D(before), D(after), D(factor), f'source-{event_id}')


def test_bonus_does_not_dilute_existing_owners():
    event = action('bonus', 'bonus', '100', '200', '2')
    result = economic_dilution(D('100'), D('200'), (event,), coverage_complete=True)
    assert result.value == D('0')
    assert result.status == 'verified'
    assert result.reason is None
    assert result.evidence_ids == ('source-bonus',)


def test_bonus_then_issuance_has_ten_percent_economic_dilution():
    events = (action('b', 'bonus', '100', '200', '2'),
              action('i', 'issuance', '200', '220', sequence=2))
    audit = capital_change_audit(D('100'), D('220'), events, coverage_complete=True)
    assert {key: value.value for key, value in audit.items()} == {
        'economic_dilution': D('.1'), 'gross_issuance_rate': D('.1'),
        'cancellation_rate': D('0'), 'bridge_error': D('0')}


def test_cancellation_can_produce_net_contraction():
    audit = capital_change_audit(D('100'), D('90'),
                                (action('c', 'buyback_cancel', '100', '90'),),
                                coverage_complete=True)
    assert audit['economic_dilution'].value == D('-.1')
    assert audit['cancellation_rate'].value == D('.1')


def test_issuance_and_cancellation_offset_without_erasing_gross_activity():
    events = (action('i', 'rights_issue', '100', '120'),
              action('c', 'buyback_cancel', '120', '100', sequence=2))
    audit = capital_change_audit(D('100'), D('100'), events, coverage_complete=True)
    assert audit['economic_dilution'].value == D('0')
    assert audit['gross_issuance_rate'].value == D('.2')
    assert audit['cancellation_rate'].value == D('.2')


def test_structural_changes_use_a_common_start_basis_for_gross_activity():
    events = (action('i1', 'issuance', '100', '120'),
              action('b', 'split', '120', '240', '2', sequence=2),
              action('i2', 'stock_compensation', '240', '260', sequence=3),
              action('c', 'buyback_cancel', '260', '240', sequence=4),
              action('r', 'consolidation', '240', '120', '.5', sequence=5))
    audit = capital_change_audit(D('100'), D('120'), events, coverage_complete=True)
    assert audit['economic_dilution'].value == D('.2')
    assert audit['gross_issuance_rate'].value == D('.3')
    assert audit['cancellation_rate'].value == D('.1')


def test_same_shanghai_day_uses_sequence_before_timestamp():
    # UTC dates differ but both actions are effective on Shanghai January 2.
    events = (action('i', 'issuance', '200', '220', sequence=2,
                     at='2026-01-01T17:00:00+00:00'),
              action('b', 'bonus', '100', '200', '2', sequence=1,
                     at='2026-01-02T05:00:00+00:00'))
    result = economic_dilution(D('100'), D('220'), events, coverage_complete=True)
    assert result.value == D('.1')
    assert result.evidence_ids == ('source-b', 'source-i')


def test_cross_day_uses_business_date_before_sequence():
    events = (action('i', 'issuance', '200', '220', sequence=0,
                     at='2026-01-02T00:00:00+00:00'),
              action('b', 'bonus', '100', '200', '2', sequence=9))
    assert economic_dilution(D('100'), D('220'), events,
                             coverage_complete=True).value == D('.1')


@pytest.mark.parametrize('events,start,end', [
    ((action('b', 'bonus', '201', '220', '2'),), '201', '220'),
    ((action('i', 'issuance', '102', '110'),), '100', '110'),
    ((action('i', 'issuance', '100', '110'),), '100', '112'),
    ((), '100', '110'),
    ((action('i', 'issuance', '100', '90'),), '100', '90'),
    ((action('c', 'buyback_cancel', '100', '110'),), '100', '110'),
    ((action('i', 'issuance', '100', '110', '2'),), '100', '110'),
    ((action('b', 'bonus', '100', '100', '0'),), '100', '100'),
    ((action('b', 'bonus', '100', '50', '.5'),), '100', '50'),
    ((action('c', 'consolidation', '100', '200', '2'),), '100', '200'),
    ((action('i', 'issuance', '-100', '100'),), '100', '100'),
    ((action('i', 'issuance', '100', '110', sequence=-1),), '100', '110'),
])
def test_invalid_semantics_or_unexplained_bridge_fails_closed(events, start, end):
    audit = capital_change_audit(D(start), D(end), events, coverage_complete=True)
    assert all(value.value is None and value.status == 'untrusted'
               and value.reason == 'capital_actions_unreconciled'
               for key, value in audit.items() if key != 'bridge_error')


def test_failed_bridge_keeps_measured_error_without_certifying_dilution():
    audit = capital_change_audit(D('100'), D('120'),
                                (action('i', 'issuance', '103', '110'),),
                                coverage_complete=True)
    assert audit['bridge_error'].value == D('10')
    assert audit['bridge_error'].status == 'verified'
    assert audit['bridge_error'].reason == 'capital_actions_unreconciled'
    assert audit['economic_dilution'].status == 'untrusted'


@pytest.mark.parametrize('second', [
    action('i', 'issuance', '110', '120', sequence=2),
    action('different', 'issuance', '110', '120', sequence=1),
    replace(action('other', 'issuance', '110', '120', sequence=2), code='000002.SZ'),
])
def test_duplicate_identity_ambiguous_order_and_mixed_issuers_fail_closed(second):
    result = economic_dilution(D('100'), D('120'),
                              (action('i', 'issuance', '100', '110'), second),
                              coverage_complete=True)
    assert (result.value, result.status, result.reason) == (
        None, 'untrusted', 'capital_actions_unreconciled')


def test_unknown_action_is_rejected_by_dto_before_calculation():
    with pytest.raises(ValueError, match='kind'):
        action('x', 'unknown', '100', '100')


@pytest.mark.parametrize('start,end', [(None, D('100')), (D('100'), None)])
def test_missing_share_endpoint_is_missing(start, end):
    audit = capital_change_audit(start, end, (), coverage_complete=True)
    assert all(value.value is None and value.status == 'missing'
               and value.reason == 'capital_shares_missing' for value in audit.values())


def test_incomplete_coverage_cannot_certify_zero_even_when_shares_match():
    audit = capital_change_audit(D('100'), D('100'), (), coverage_complete=False)
    assert all(value.value is None and value.status == 'missing'
               and value.reason == 'capital_actions_incomplete_coverage'
               for value in audit.values())


def test_explicit_complete_no_action_coverage_can_certify_zero():
    audit = capital_change_audit(D('100'), D('100'), (), coverage_complete=True)
    assert all(value.value == D('0') and value.status == 'verified'
               for value in audit.values())


@pytest.mark.parametrize('value', ['0', '-1', 'NaN', 'sNaN', 'Infinity'])
def test_invalid_endpoints_cannot_become_zero(value):
    result = economic_dilution(D(value), D('100'), (), coverage_complete=True)
    assert result.value is None
    assert result.status == 'untrusted'


@pytest.mark.parametrize('start,end,events,error', [
    ('100', '101', (), '1'),
    ('1000000', '1000100', (), '100'),
    ('100', '202', (action('b', 'bonus', '101', '202', '2'),), '1'),
])
def test_bridge_tolerance_accepts_absolute_or_relative_boundary(start, end, events, error):
    audit = capital_change_audit(D(start), D(end), events, coverage_complete=True)
    assert audit['bridge_error'].value == D(error)
    assert audit['economic_dilution'].status == 'verified'


@pytest.mark.parametrize('start,end', [('100', '101.00001'), ('1000000', '1000100.02')])
def test_bridge_tolerance_rejects_outside_boundary(start, end):
    assert economic_dilution(D(start), D(end), (), coverage_complete=True).status == 'untrusted'


def test_consolidation_factor_tolerance_uses_actual_before_and_after_shares():
    audit = capital_change_audit(
        D('1000000'), D('500090'),
        (action('c', 'consolidation', '1000000', '500090', '.5'),),
        coverage_complete=True)
    assert audit['economic_dilution'].value == D('.00018')
    assert audit['bridge_error'].value == D('90')


def test_consolidation_outside_actual_share_tolerance_is_untrusted():
    audit = capital_change_audit(
        D('1000000'), D('500100.01'),
        (action('c', 'consolidation', '1000000', '500100.01', '.5'),),
        coverage_complete=True)
    assert audit['economic_dilution'].status == 'untrusted'
    assert audit['bridge_error'].value == D('100.01')


def test_results_ignore_ambient_decimal_precision_rounding_and_traps():
    events = (action('i', 'issuance', '3', '4'),)
    expected = capital_change_audit(D('3'), D('4'), events, coverage_complete=True)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        actual = capital_change_audit(D('3'), D('4'), events, coverage_complete=True)
    assert actual == expected
    assert actual['economic_dilution'].value > D('.3333333333333333333333333333')


def test_unbounded_numeric_work_fails_closed():
    result = economic_dilution(D('1e5000'), D('1e-5000'), (), coverage_complete=True)
    assert (result.value, result.status, result.reason) == (
        None, 'untrusted', 'capital_actions_numeric_range')


def test_non_decimal_endpoints_and_non_boolean_coverage_are_programming_errors():
    with pytest.raises(TypeError, match='Decimal'):
        economic_dilution(100.0, D('100'), (), coverage_complete=True)
    with pytest.raises(TypeError, match='boolean'):
        economic_dilution(D('100'), D('100'), (), coverage_complete=1)
