"""Synthetic normalized market assertions; tests never use data/app.db."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D, ROUND_DOWN, localcontext
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tracking.contracts import default_tracking_config, digest, protocol


START = date(2026, 1, 29)


def manifest(codes=('AAA',), cost='0', published='2026-01-29T00:00:00+00:00'):
    config = default_tracking_config()
    config['cost_bps_each_side'] = cost
    with localcontext() as ctx:
        ctx.prec = 34
        weight = str(D(1) / len(codes)) if codes else '0'
    result = {'schema_version': 1, 'published_run_at': published,
              'publication_time_status': 'recorded' if published else 'unavailable',
              'original_member_count': len(codes),
              'members': [{'code': code, 'weight': weight} for code in codes],
              'protocol': protocol(config)}
    result['protocol_hash'] = digest(result['protocol'])
    result['manifest_hash'] = digest(result)
    return result


def event(kind, day, code='AAA', event_id=None, **payload):
    identity = event_id or f'{kind}:{code}:{day}'
    return {'event_id': identity, 'economic_event_id': identity, 'code': code, 'type': kind,
            'effective_at': f'{day}T01:00:00+00:00',
            'observed_at': f'{day}T08:00:00+00:00', 'source_id': 'synthetic-reviewed',
            **payload}


def market(end=date(2026, 2, 4), codes=('AAA',), *, start=START, calendar_end=None):
    """Weekdays are a deliberately synthetic source calendar, not an exchange calendar."""
    records = []
    current = start
    while current <= (calendar_end or end):
        records.append(event('calendar_day', current, 'MARKET', date=str(current),
                             is_session=current.weekday() < 5,
                             observed_at=f'{start}T00:00:00+00:00'))
        if current <= end and current.weekday() < 5:
            for code in codes:
                records.append(event('bar', current, code, open='10', close='10', volume='1000',
                    suspended=False, open_limit_up=False, open_limit_down=False,
                    price_basis='unadjusted', adjustment_factor='1'))
        current += timedelta(days=1)
    for code in codes:
        records.append(event('coverage', start, code, start_date=str(start), end_date=str(end),
                             bars_complete=True, actions_complete=True))
    return records


def simulate(records=None, end=date(2026, 2, 4), cohort=None):
    from tracking.ledger import simulate_cohort
    return simulate_cohort(cohort or manifest(), records if records is not None else market(end), end)


def horizon(result, months=3):
    return result['horizons'][str(months)]


def change_bar(records, day, **changes):
    for row in records:
        if row['type'] == 'bar' and row['effective_at'].startswith(str(day)):
            row.update(changes)


def test_dividend_receivable_preserves_ex_date_equity():
    from tracking.ledger import dividend_receivable
    assert D('100') * D('9') + dividend_receivable(D('100'), D('1')) == D('1000')
    for bad in (D('-1'), D('NaN'), 1.0):
        with pytest.raises(ValueError):
            dividend_receivable(bad, D('1'))


def test_dividend_split_and_payment_preserve_total_equity_without_double_counting():
    records = market()
    records += json.loads((Path(__file__).parent / 'fixtures/selection/market-events.json').read_text())
    change_bar(records, '2026-02-02', open='9', close='9')
    change_bar(records, '2026-02-03', open='4.5', close='4.5')
    change_bar(records, '2026-02-04', open='4.5', close='4.5')
    result = horizon(simulate(records))
    assert {D(point['equity']) for point in result['equity_curve']} == {D('1')}
    assert result['positions']['AAA']['shares'] == '0.2'
    assert D(result['cash']) == D('.1')
    assert result['receivable'] == '0'
    assert result['horizon_return'] is None
    assert result['horizon_status'] == 'pending_maturity'
    assert [entry['entry_kind'] for entry in result['ledger']].count('dividend_paid') == 1


def test_same_day_actions_follow_explicit_sequence_before_open():
    records = market(date(2026, 2, 2))
    records.extend([
        event('split', '2026-02-02', factor='2', action_sequence='2'),
        event('dividend_ex', '2026-02-02', cash_per_share='1', pay_date='2026-02-04', action_sequence='1')])
    change_bar(records, '2026-02-02', open='4.5', close='4.5')
    result = horizon(simulate(list(reversed(records)), date(2026, 2, 2)))
    assert D(result['equity']) == D('1')
    assert D(result['receivable']) == D('.1')
    records[-1].pop('action_sequence')
    with pytest.raises(ValueError, match='sequence'):
        simulate(records, date(2026, 2, 2))


@pytest.mark.parametrize('same_time', [False, True])
@pytest.mark.parametrize('corrected_ex', [False, True])
def test_dividend_payment_rejects_order_before_selected_ex(same_time, corrected_ex):
    records = market()
    ex = event('dividend_ex', '2026-02-02', cash_per_share='1', pay_date='2026-02-02',
               action_sequence='2')
    payment = event('dividend_paid', '2026-02-02', ex_event_id=ex['event_id'],
                    effective_at='2026-02-02T01:00:00+00:00' if same_time else '2026-02-02T00:00:00+00:00',
                    action_sequence='1')
    if corrected_ex:
        # The referenced version originally preceded payment, but the selected
        # correction moves entitlement after it. Validate the selected version.
        original = dict(ex, effective_at=ex['effective_at'] if same_time else '2026-02-01T23:00:00+00:00',
                        action_sequence='0')
        records.append(original)
        ex = dict(ex, event_id='corrected-ex', supersedes_id=original['event_id'],
                  observed_at='2026-02-03T08:00:00+00:00')
    records.extend([ex, payment])
    with pytest.raises(ValueError, match='payment precedes selected ex'):
        simulate(records)


@pytest.mark.parametrize('same_time', [False, True])
@pytest.mark.parametrize('corrected_ex', [False, True])
def test_ordered_same_day_payment_transfers_selected_ex_entitlement(same_time, corrected_ex):
    records = market()
    ex = event('dividend_ex', '2026-02-02', cash_per_share='1', pay_date='2026-02-02',
               action_sequence='1')
    payment = event('dividend_paid', '2026-02-02', ex_event_id=ex['event_id'],
                    effective_at='2026-02-02T01:00:00+00:00' if same_time else '2026-02-02T01:15:00+00:00',
                    action_sequence='2')
    if corrected_ex:
        original = dict(ex, cash_per_share='2', action_sequence='3',
                        effective_at=ex['effective_at'] if same_time else '2026-02-02T01:30:00+00:00')
        records.append(original)
        ex = dict(ex, event_id='corrected-ex', supersedes_id=original['event_id'],
                  observed_at='2026-02-03T08:00:00+00:00')
    records.extend([payment, ex])
    for day in ('2026-02-02', '2026-02-03', '2026-02-04'):
        change_bar(records, day, open='9', close='9')
    result = horizon(simulate(records))
    assert result['equity'] == '1'
    assert result['cash'] == '0.1'
    assert result['receivable'] == '0'
    assert [row['event_id'] for row in result['ledger'] if row['entry_kind'].startswith('dividend_')] == [
        ex['event_id'], payment['event_id']]


def test_entry_waits_five_sessions_and_keeps_unfilled_cash():
    end = date(2026, 2, 9)
    records = market(end)
    for row in records:
        if row['type'] == 'bar' and row['effective_at'][:10] <= '2026-02-05':
            row['open_limit_up'] = True
    result = horizon(simulate(records, end))
    assert result['entry_coverage'] == '0'
    assert result['cash'] == '1'
    assert result['positions']['AAA']['entry_status'] == 'unfilled'
    assert not any(row['entry_kind'] == 'buy' for row in result['ledger'])


def test_delayed_fill_preserves_original_horizon_anchor_and_suspension():
    end = date(2026, 5, 4)
    records = market(end)
    change_bar(records, '2026-01-30', suspended=True)
    result = horizon(simulate(records, end))
    assert result['entry_target_date'] == '2026-01-30'
    assert result['positions']['AAA']['purchase_date'] == '2026-02-02'
    assert result['target_date'] == '2026-04-30'
    assert result['expected_exit_date'] == '2026-05-01'
    assert result['actual_exit_date'] == '2026-05-01'


@pytest.mark.parametrize('entry,target,exit_day', [
    ('2026-01-30', '2026-04-30', '2026-05-01'),
    ('2026-02-27', '2026-05-27', '2026-05-28'),
    ('2026-01-31', '2026-04-30', '2026-05-01'),
    ('2026-03-02', '2026-06-02', '2026-06-03'),
    ('2026-01-02', '2026-04-02', '2026-04-06')])
def test_month_clipping_and_strict_after_session(entry, target, exit_day):
    from tracking.market import add_months, first_session_after
    assert str(add_months(date.fromisoformat(entry), 3)) == target
    calendar = {date.fromisoformat(target): True}
    current = date.fromisoformat(target) + timedelta(days=1)
    while current <= date.fromisoformat(exit_day):
        calendar[current] = current == date.fromisoformat(exit_day)
        current += timedelta(days=1)
    assert str(first_session_after(date.fromisoformat(target), calendar)) == exit_day
    calendar.pop(date.fromisoformat(target) + timedelta(days=1))
    assert first_session_after(date.fromisoformat(target), calendar) is None


def test_t_plus_one_and_zero_volume_prevent_same_day_or_untradeable_fills():
    from tracking.market import executable
    bar = event('bar', '2026-05-01', open='10', close='10', volume='100',
                suspended=False, open_limit_up=False, open_limit_down=False)
    assert not executable(bar, 'sell', date(2026, 5, 1))
    assert executable(bar, 'sell', date(2026, 4, 30))
    assert not executable(dict(bar, open_limit_down=True), 'sell', date(2026, 4, 30))
    assert not executable(dict(bar, volume='0'), 'buy')


def test_pending_exit_keeps_revaluing_and_horizons_are_independent():
    end = date(2026, 5, 4)
    records = market(end)
    change_bar(records, '2026-05-01', open='8', close='7', open_limit_down=True)
    change_bar(records, '2026-05-04', open='6', close='5', open_limit_down=True)
    result = simulate(records, end)
    short = horizon(result)
    assert short['horizon_status'] == 'exit_pending'
    assert short['horizon_return'] is None
    assert D(short['equity']) == D('.5')
    assert D(short['max_drawdown']) == D('.5')
    change_bar(records, '2026-05-04', open_limit_down=False)
    completed = simulate(records, end)
    assert horizon(completed)['horizon_status'] == 'completed'
    assert D(horizon(completed)['horizon_return']) == D('-.4')
    for months in (6, 12):
        assert horizon(result, months)['equity_curve'] == horizon(completed, months)['equity_curve']
        assert horizon(completed, months)['positions']['AAA']['shares'] == '0.1'


def test_receivables_survive_sale_and_later_payment_changes_only_cash_class():
    end = date(2026, 5, 4)
    records = market(end)
    records.append(event('dividend_ex', '2026-04-30', cash_per_share='1', pay_date='2026-05-04'))
    for day in ('2026-04-30', '2026-05-01', '2026-05-04'):
        change_bar(records, day, open='9', close='9')
    before = horizon(simulate(records, date(2026, 5, 1)))
    assert before['horizon_return'] == '0'
    assert D(before['cash_realized_return']) == D('-.1')
    assert D(before['receivable']) == D('.1')
    records.append(event('dividend_paid', '2026-05-04', ex_event_id='dividend_ex:AAA:2026-04-30',
                         effective_at='2026-05-04T06:00:00+00:00'))
    after = horizon(simulate(records, end))
    assert after['horizon_return'] == '0'
    assert after['cash_realized_return'] == '0'
    assert after['actual_exit_date'] == '2026-05-01'


def test_unknown_delisting_retains_mark_and_zero_recovery_scenario_until_sourced_settlement():
    end = date(2026, 5, 4)
    records = market(end)
    records.append(event('delisting', '2026-02-02', settlement_status='unresolved', cash_per_share=None))
    result = horizon(simulate(records, end))
    assert result['horizon_return'] is None
    assert result['exit_status'] == 'unresolved_delisting'
    assert result['equity'] == '1'
    assert result['zero_recovery_equity'] == '0'
    records.append(event('delisting', '2026-05-04', settlement_status='settled', cash_per_share='4'))
    settled = horizon(simulate(records, end))
    assert D(settled['horizon_return']) == D('-.6')
    assert settled['exit_status'] == 'completed'


def test_early_known_settlement_waits_until_planned_horizon():
    records = market(date(2026, 5, 4), calendar_end=date(2026, 5, 4))
    records.append(event('delisting', '2026-02-02', settlement_status='settled', cash_per_share='4'))
    early = horizon(simulate(records, date(2026, 2, 4)))
    assert early['horizon_status'] == 'pending_maturity'
    assert early['horizon_return'] is None
    final = horizon(simulate(records, date(2026, 5, 4)))
    assert D(final['horizon_return']) == D('-.6')
    assert final['actual_exit_date'] == '2026-02-02'
    assert final['terminal_date'] == '2026-05-01'


def test_terminated_security_needs_no_later_bars_and_keeps_original_capital():
    end = date(2026, 5, 4)
    records = market(end)
    records.append(event('delisting', '2026-02-02', settlement_status='settled', cash_per_share='4'))
    records = [row for row in records if row['type'] != 'bar' or row['effective_at'][:10] < '2026-02-02']
    next(row for row in records if row['type'] == 'coverage')['end_date'] = '2026-02-01'
    records.append(event('coverage', '2026-02-02', start_date='2026-02-02', end_date=str(end),
                         bars_complete=False, actions_complete=True))
    result = horizon(simulate(records, end))
    assert result['horizon_status'] == 'completed'
    assert D(result['horizon_return']) == D('-.6')
    assert result['entry_coverage'] == '1'
    unresolved = deepcopy(records)
    action = next(row for row in unresolved if row['type'] == 'delisting')
    action.update(settlement_status='unresolved', cash_per_share=None)
    result = horizon(simulate(unresolved, end))
    assert result['horizon_status'] == 'unresolved_delisting'
    assert result['equity'] == '1'
    assert result['zero_recovery_equity'] == '0'


def test_exited_short_horizon_needs_no_later_bars_but_long_horizon_does():
    end = date(2026, 5, 4)
    records = [row for row in market(end) if row['event_id'] != 'bar:AAA:2026-05-04']
    result = simulate(records, end)
    assert horizon(result)['horizon_status'] == 'completed'
    assert horizon(result, 6)['horizon_status'] == 'data_incomplete'


def test_same_utc_timestamp_spellings_cannot_bypass_action_sequence_requirement():
    records = market()
    records.append(event('split', '2026-02-02', factor='2'))
    records.append(event('dividend_ex', '2026-02-02', cash_per_share='1', pay_date='2026-02-04',
                         effective_at='2026-02-02T01:00:00Z'))
    with pytest.raises(ValueError, match='sequence'):
        simulate(records)


def test_selected_withdrawal_identity_remains_bound_without_applying_action():
    records = market()
    old = event('split', '2026-02-02', factor='2')
    withdrawn = dict(old, event_id='withdrawn-split', withdrawn=True,
                     supersedes_id=old['event_id'], observed_at='2026-02-03T08:00:00+00:00')
    result = simulate(records + [old, withdrawn])
    assert horizon(result)['equity'] == '1'
    assert 'withdrawn-split' in result['selected_event_ids']
    assert result['event_digest'] != simulate(records)['event_digest']


def test_caller_decimal_rounding_cannot_change_calculation_identity_or_balances():
    cohort = manifest(cost='15')
    expected = simulate(cohort=cohort)
    with localcontext() as ctx:
        ctx.rounding = ROUND_DOWN
        ctx.prec = 8
        assert simulate(cohort=cohort) == expected


@pytest.mark.parametrize('boundary', ['publication', 'event', 'calendar', 'as_of'])
def test_unsupported_historical_timezone_dates_fail_closed(boundary):
    cohort, records, end = manifest(), market(), date(2026, 2, 4)
    if boundary == 'publication':
        cohort = manifest(published='1991-12-31T00:00:00+00:00')
    elif boundary == 'event':
        records.append(event('split', '1991-12-31', factor='2'))
    elif boundary == 'calendar':
        records.append(event('calendar_day', '1991-12-31', 'MARKET', date='1991-12-31', is_session=True))
    else:
        end = date(1991, 12, 31)
    with pytest.raises(ValueError, match='unsupported historical timezone'):
        simulate(records, end, cohort)


def test_unfilled_half_retains_original_weight_and_denominator():
    records = market(codes=('AAA', 'BBB'))
    for row in records:
        if row['type'] == 'bar' and row['code'] == 'BBB':
            row['suspended'] = True
    change_bar(records, '2026-02-04', close='20')
    result = horizon(simulate(records, cohort=manifest(codes=('AAA', 'BBB'))))
    assert result['entry_coverage'] == '0.5'
    assert result['cash'] == '0.5'
    assert result['equity'] == '1.5'


def test_date_only_action_requires_explicit_accounting_normalization():
    records = market()
    action = event('split', '2026-02-02', factor='2', effective_time_precision='date',
                   effective_date='2026-02-02')
    with pytest.raises(ValueError, match='normalization'):
        simulate(records + [action])
    action['normalization_policy'] = 'pre_open_accounting_v1'
    for day in ('2026-02-02', '2026-02-03', '2026-02-04'):
        change_bar(records, day, open='5', close='5')
    assert horizon(simulate(records + [action]))['equity'] == '1'


def test_rights_not_subscribed_and_entry_day_ex_has_no_new_holder_entitlement():
    records = market()
    records.append(event('rights', '2026-02-02'))
    records.append(event('dividend_ex', '2026-01-30', cash_per_share='1', pay_date='2026-02-04'))
    result = horizon(simulate(records))
    assert result['receivable'] == '0'
    assert result['equity'] == '1'
    assert any(row['entry_kind'] == 'rights_not_subscribed' for row in result['ledger'])


def test_costs_charge_only_executed_sides_and_report_all_sensitivities():
    result = horizon(simulate(cohort=manifest(cost='15')))
    with localcontext() as ctx:
        ctx.prec = 34
        shares = D('1') / (D('10') * D('1.0015'))
        assert D(result['positions']['AAA']['shares']) == shares
        assert abs(D(result['trading_cost']) - shares * D('10') * D('.0015')) < D('1e-32')
    assert result['gross_return'] == '0'
    assert D(result['net_return']) < 0
    assert set(result['cost_sensitivity']) == {'0', '15', '30', '50'}
    assert len([row for row in result['ledger'] if row['entry_kind'] in ('buy', 'sell')]) == 1
    completed = horizon(simulate(market(date(2026, 5, 4)), date(2026, 5, 4), manifest(cost='15')))
    assert D(completed['trading_cost']) > D(result['trading_cost'])


@pytest.mark.parametrize('gap', ['calendar', 'bar', 'actions', 'coverage'])
def test_missing_required_coverage_never_becomes_completed(gap):
    end = date(2026, 5, 4)
    records = market(end)
    if gap == 'calendar':
        records = [row for row in records if row['event_id'] != 'calendar_day:MARKET:2026-01-31']
    elif gap == 'bar':
        records = [row for row in records if row['event_id'] != 'bar:AAA:2026-02-02']
    elif gap == 'coverage':
        records = [row for row in records if row['type'] != 'coverage']
    else:
        next(row for row in records if row['type'] == 'coverage')['actions_complete'] = False
    result = horizon(simulate(records, end))
    assert result['horizon_status'] == 'data_incomplete'
    assert result['horizon_return'] is None
    assert result['data_gaps']


def test_missing_publication_and_empty_cohort_have_no_invented_holdings_or_completed_return():
    missing = horizon(simulate(cohort=manifest(published=None)))
    assert missing['horizon_status'] == 'entry_time_unavailable'
    assert missing['entry_target_date'] is None
    empty = horizon(simulate(cohort=manifest(codes=())))
    assert empty['horizon_status'] == 'empty_cohort'
    assert empty['positions'] == {}
    assert empty['horizon_return'] is None


def test_future_calendar_can_project_maturity_without_future_prices():
    records = market(calendar_end=date(2026, 5, 4))
    records.append(event('bar', '2026-05-01', open='99', close='99', volume='1000',
        suspended=False, open_limit_up=False, open_limit_down=False,
        price_basis='unadjusted', adjustment_factor='1', observed_at='2026-02-01T00:00:00+00:00'))
    result = horizon(simulate(records))
    assert result['expected_exit_date'] == '2026-05-01'
    assert result['equity'] == '1'
    records = [row for row in records if row['event_id'] != 'calendar_day:MARKET:2026-04-01']
    assert horizon(simulate(records))['expected_exit_date'] is None


def test_frozen_denominator_and_decimal_residual_never_create_negative_cash():
    codes = tuple(f'C{i}' for i in range(6))
    result = horizon(simulate(market(codes=codes), cohort=manifest(codes=codes)))
    assert D(result['cash']) >= 0
    assert abs(D(result['equity']) - D(1)) < D('1e-32')
    bad = manifest(codes=codes)
    bad['members'][0]['weight'] = '.5'
    bad['manifest_hash'] = digest({key: value for key, value in bad.items() if key != 'manifest_hash'})
    with pytest.raises(ValueError, match='weight'):
        simulate(market(codes=codes), cohort=bad)


def test_replay_is_idempotent_conflicts_rejected_and_versions_selected_at_cutoff():
    from tracking.market import select_events
    records = market()
    assert simulate(records) == simulate(records + [deepcopy(records[-1])])
    changed = dict(records[-1], bars_complete=False)
    with pytest.raises(ValueError, match='conflict'):
        simulate(records + [changed])
    old = next(row for row in records if row['event_id'] == 'bar:AAA:2026-02-02')
    corrected = dict(old, event_id='bar:corrected', close='8', supersedes_id=old['event_id'],
                     observed_at='2026-02-04T08:00:00+00:00')
    corrected_result = simulate(records + [corrected])
    assert corrected_result['calculation_version'] != simulate(records)['calculation_version']
    assert D(horizon(corrected_result)['max_drawdown']) == D('.2')
    chosen = select_events(records + [corrected], date(2026, 2, 3))
    assert old['event_id'] in {row['event_id'] for row in chosen}
    assert corrected['event_id'] not in {row['event_id'] for row in chosen}
    with pytest.raises(ValueError, match='supersed|version'):
        simulate(records + [dict(corrected, supersedes_id=None)])


def test_explicit_archived_availability_preserves_actual_observation_time():
    from tracking.market import select_events
    record = event('bar', '2026-02-02', open='10', close='10', volume='100',
        suspended=False, open_limit_up=False, open_limit_down=False,
        price_basis='unadjusted', adjustment_factor='1', observed_at='2026-10-01T08:00:00+00:00')
    assert select_events([record], date(2026, 2, 4)) == []
    archived = dict(record, availability_basis='retained_archive_v1',
                    archived_available_at='2026-02-02T08:00:00+00:00')
    selected = select_events([archived], date(2026, 2, 4))
    assert selected[0]['observed_at'] == record['observed_at']
    assert selected[0]['archived_available_at'] == archived['archived_available_at']
    for invalid in (dict(record, archived_available_at=archived['archived_available_at']),
                    dict(record, availability_basis='retained_archive_v1'),
                    dict(archived, archived_available_at='2026-11-01T08:00:00+00:00')):
        with pytest.raises(ValueError, match='availab'):
            select_events([invalid], date(2026, 2, 4))


@pytest.mark.parametrize('changes', [{'price_basis': 'adjusted'}, {'adjustment_factor': '2'},
    {'open': 10.0}, {'close': 'NaN'}, {'volume': '-1'}, {'suspended': 'false'},
    {'effective_at': '2026-02-02'}, {'source_id': ''}])
def test_invalid_price_basis_or_ambiguous_source_records_are_rejected(changes):
    records = market()
    change_bar(records, '2026-02-02', **changes)
    with pytest.raises(ValueError):
        simulate(records)


def test_benchmark_requires_matching_total_return_valuation_points_and_retains_revision():
    records = market()
    assert horizon(simulate(records))['benchmarks']['primary']['status'] == 'benchmark_unavailable'
    for day, point, level in [('2026-01-30', 'session_open', '100'),
                               ('2026-02-04', 'session_close', '105')]:
        records.append(event('benchmark', day, 'CSI300_TOTAL_RETURN',
            index_id='CSI300_TOTAL_RETURN', total_return_level=level, valuation_point=point,
            series_revision='synthetic-v1', return_convention='total_return',
            source_id='China Securities Index Co., Ltd.'))
    result = horizon(simulate(records))['benchmarks']['primary']
    assert result['return'] == '0.05'
    assert result['relative_return'] == '-0.05'
    assert result['series_revisions'] == ['synthetic-v1']
    records[-2]['valuation_point'] = 'session_close'
    assert horizon(simulate(records))['benchmarks']['primary']['status'] == 'benchmark_unavailable'


def test_completed_benchmark_ends_at_delayed_exit_open_not_later_payment_close():
    end = date(2026, 5, 4)
    records = market(end)
    change_bar(records, '2026-05-01', open_limit_down=True)
    for day, point, level in [('2026-01-30', 'session_open', '100'),
        ('2026-05-04', 'session_open', '110'), ('2026-05-04', 'session_close', '200')]:
        records.append(event('benchmark', day, 'CSI300_TOTAL_RETURN', event_id=f'{day}:{point}',
            index_id='CSI300_TOTAL_RETURN', total_return_level=level, valuation_point=point,
            series_revision='synthetic-v1', return_convention='total_return',
            source_id='China Securities Index Co., Ltd.'))
    result = horizon(simulate(records, end))
    assert result['actual_exit_date'] == '2026-05-04'
    assert result['benchmarks']['primary']['return'] == '0.1'


def test_tracking_tables_restrict_references_and_preserve_append_only_calculations(tmp_path):
    from app.db import init_db, create_session_factory
    from app.selection_models import StrategyMarketEvent, StrategyLedgerEntry, StrategyObservation
    from app.discovery_models import DiscoveryStage
    from sqlalchemy import select
    from tracking.cohorts import publish_track_cohorts
    from tests.test_tracking_cohorts import persisted_run, PUBLISHED
    engine = init_db(tmp_path / 'market-ledger.db')
    with create_session_factory(engine)() as session:
        run = persisted_run(session)
        stage = session.scalar(select(DiscoveryStage).where(DiscoveryStage.run_id == run.id,
                                                       DiscoveryStage.stage == 'competition'))
        cohort_id = publish_track_cohorts(session, stage_id=stage.id, published_at=PUBLISHED)[0]
        session.commit()
    instant = datetime(2026, 1, 30, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(StrategyMarketEvent.__table__.insert(), dict(event_id='e1',
            economic_event_id='economic1', code='AAA', type='bar', effective_at=instant,
            observed_at=instant, source_id='synthetic', payload_json='{}', digest='d1'))
        connection.execute(StrategyLedgerEntry.__table__.insert(), dict(id=1, cohort_id=cohort_id,
            horizon_months=3, event_id='e1', entry_kind='buy', sequence=1, shares_after='.1',
            cash_after='0', receivable_after='0', cost='0', calculation_version='engine:d1'))
        connection.execute(StrategyObservation.__table__.insert(), dict(id=1, cohort_id=cohort_id,
            horizon_months=3, as_of=date(2026, 1, 30), state='pending_maturity', metrics_json='{}',
            event_digest='d1', calculation_version='engine:d1'))
    for table in ('strategy_market_events', 'strategy_ledger_entries', 'strategy_observations'):
        for statement in (f'UPDATE {table} SET ' + ('code=code' if table == 'strategy_market_events' else 'id=id'),
                          f'DELETE FROM {table}', f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(text(statement))
        assert all(fk['options']['ondelete'] == 'RESTRICT' for fk in inspect(engine).get_foreign_keys(table))
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO strategy_ledger_entries SELECT 2, cohort_id, horizon_months, "
            "event_id, entry_kind, sequence, shares_after, cash_after, receivable_after, cost, 'engine:d2' "
            "FROM strategy_ledger_entries WHERE id=1"))
    engine.dispose()
