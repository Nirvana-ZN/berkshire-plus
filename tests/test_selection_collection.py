"""Shadow collection diagnostics over synthetic frozen financial facts."""

from decimal import Decimal as D, localcontext

import pytest

from discovery.fundamentals import collection_metrics, collection_signals
from tests.selection_helpers import evidence
from tests.test_selection_capital_return import alter, changed, snapshot

POLICY = {'version': 'collection-v1', 'receivables_ratio_increase_min': '0.05',
          'restricted_cash_ratio_min': '0.20'}


def rows():
    return (evidence('2024', revenue='100', accounts_receivable='10', contract_assets='0',
                     ocf='20', net_income='10', inventory='20'),
            evidence('2025', revenue='100', accounts_receivable='15', contract_assets='0',
                     ocf='10', net_income='10', inventory='25', restricted_cash='20', cash='100'))


def test_exact_boundary_flags_are_shadow_only_and_auditable():
    snap = snapshot(rows())
    assert collection_signals(snap, POLICY) == ('collection_pressure', 'restricted_cash_material')
    metrics = collection_metrics(snap)
    assert metrics['receivables_ratio_change'].value == D('.05')
    assert metrics['cash_conversion_change'].value == D('-1')
    assert metrics['restricted_cash_ratio'].value == D('.2')
    assert metrics['inventory_ratio_change'].value == D('.05')
    assert set(metrics['receivables_ratio_change'].evidence_ids) == {row.evidence_id for row in rows()}


@pytest.mark.parametrize('field', ['accounts_receivable', 'contract_assets', 'net_income', 'ocf', 'revenue'])
def test_missing_required_field_is_independent_of_restricted_flag(field):
    snap = snapshot(alter(rows(), 2025, **{field: None}))
    assert collection_signals(snap, POLICY) == ('collection_pressure_missing', 'restricted_cash_material')


def test_missing_restriction_is_not_confirmed_zero_and_zero_is_not_missing():
    assert collection_signals(snapshot(alter(rows(), 2025, restricted_cash=None)), POLICY) == ('collection_pressure', 'restricted_cash_material_missing')
    assert collection_signals(snapshot(alter(rows(), 2025, restricted_cash='0')), POLICY) == ('collection_pressure',)


def test_no_declining_conversion_or_small_receivable_increase_does_not_trigger():
    assert collection_signals(snapshot(alter(rows(), 2025, ocf='20', restricted_cash='0')), POLICY) == ()
    assert collection_signals(snapshot(alter(rows(), 2025, accounts_receivable='14.99', restricted_cash='0')), POLICY) == ()


def test_untrusted_and_not_applicable_statuses_are_not_clean_signals():
    old, now = rows()
    assert collection_signals(snapshot((old, changed(now, status='untrusted'))), POLICY) == ('collection_pressure_untrusted', 'restricted_cash_material_untrusted')
    assert collection_signals(snapshot((old, changed(now, status='not_applicable', values={k: None for k in now.values}))), POLICY) == ('collection_pressure_not_applicable', 'restricted_cash_material_not_applicable')


def test_merger_basis_and_nonpositive_profit_cannot_create_pressure_or_clean_read():
    old, now = rows()
    assert collection_signals(snapshot((old, changed(now, basis_id='new-scope'))), POLICY)[0] == 'collection_pressure_untrusted'
    assert collection_signals(snapshot(alter(rows(), 2024, net_income='0')), POLICY)[0] == 'collection_pressure_missing'


@pytest.mark.parametrize('cash,restricted', [('0', '0'), ('100', '101'), ('100', '-1')])
def test_invalid_restricted_cash_ratio_is_unavailable(cash, restricted):
    assert collection_metrics(snapshot(alter(rows(), 2025, cash=cash, restricted_cash=restricted)))['restricted_cash_ratio'].value is None


def test_interim_uses_ttm_flows_and_same_period_balances():
    facts = (evidence('2024', revenue='100', ocf='20', net_income='10'),
             evidence('2024H1', revenue='40', ocf='10', net_income='5'),
             evidence('2025', revenue='110', ocf='20', net_income='10'),
             evidence('2025H1', revenue='50', ocf='10', net_income='5', accounts_receivable='10', contract_assets='0', inventory='20'),
             evidence('2026H1', revenue='60', ocf='5', net_income='5', accounts_receivable='20', contract_assets='0', inventory='25', restricted_cash='0', cash='100'))
    metrics = collection_metrics(snapshot(facts))
    with localcontext() as context:
        context.prec = 36
        assert metrics['receivables_ratio'].value == D('20') / D('120')
        assert metrics['prior_receivables_ratio'].value == D('10') / D('110')
    assert collection_signals(snapshot(facts), POLICY) == ('collection_pressure',)


@pytest.mark.parametrize('policy,expected', [({}, 'collection_policy_missing'),
    ({**POLICY, 'version': 'wrong'}, 'collection_policy_untrusted'),
    ({**POLICY, 'restricted_cash_ratio_min': .2}, 'collection_policy_untrusted'),
    ({**POLICY, 'receivables_ratio_increase_min': 'NaN'}, 'collection_policy_untrusted')])
def test_policy_is_explicit_and_decimal(policy, expected):
    assert collection_signals(snapshot(rows()), policy) == (expected,)


def test_collection_pressure_requires_common_basis_across_both_ratios():
    balances = tuple(changed(row, values={k: v for k, v in row.values.items() if k not in {'ocf', 'net_income'}})
                     for row in rows())
    flows = tuple(changed(row, basis_id='other-consolidated-scope',
                          values={k: v for k, v in row.values.items() if k in {'ocf', 'net_income'}})
                  for row in rows())
    assert collection_signals(snapshot((*balances, *flows)), POLICY)[0] == 'collection_pressure_untrusted'


def test_garp_adapter_is_opt_in_and_does_not_change_legacy_classification():
    from discovery.tracks.garp import frozen_fundamental_diagnostics
    result = frozen_fundamental_diagnostics(snapshot(rows()), POLICY)
    assert set(result) == {'roic_gross_5y', 'collection_metrics', 'shadow_signals'}
    assert result['roic_gross_5y'].status == 'missing'
    assert result['shadow_signals'] == ('collection_pressure', 'restricted_cash_material')
    assert result['collection_metrics'] == collection_metrics(snapshot(rows()))
