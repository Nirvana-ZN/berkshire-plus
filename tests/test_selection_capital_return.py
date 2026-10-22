"""Synthetic, reviewed-input tests; these do not certify provider mappings."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D, localcontext

import pytest

from discovery.fundamental_types import evidence_identity
from discovery.fundamentals import capital_return, freeze_fundamentals, roic_gross
from tests.selection_helpers import evidence

CUTOFF = datetime(2026, 10, 22, tzinfo=timezone.utc)


def changed(row, **changes):
    row = replace(row, **changes)
    return replace(row, evidence_id=evidence_identity(row))


def profile(industry='家用电器', status='verified'):
    return changed(evidence('2025'), kind='company_profile', status=status,
                   values={'_industry': industry})


def snapshot(rows, classification=None):
    return freeze_fundamentals((*rows, classification or profile()), (), (),
                               code='000001.SZ', cutoff_at=CUTOFF, current_mcap=None)


def annuals(**overrides):
    return tuple(evidence(str(year), total_equity_including_minority='80',
                          interest_debt='20', ebit='20', pretax_income='16',
                          income_tax='4', interest_expense='4', **overrides)
                 for year in range(2020, 2026))


def alter(rows, year, **values):
    return tuple(changed(row, values={**row.values, **values}) if row.period == str(year) else row
                 for row in rows)


def test_gross_roic_uses_average_invested_capital():
    assert roic_gross(ebit=D('20'), tax_rate=D('.25'), ic_begin=D('90'), ic_end=D('110')).value == D('.15')


@pytest.mark.parametrize('field,value', [('tax_rate', D('-0.1')), ('tax_rate', D('1.1')),
    ('ic_begin', D('0')), ('ic_end', D('-1')), ('ebit', D('NaN')), ('ebit', 20.0)])
def test_invalid_inputs_are_untrusted(field, value):
    inputs = dict(ebit=D('20'), tax_rate=D('.25'), ic_begin=D('90'), ic_end=D('110'))
    inputs[field] = value
    result = roic_gross(**inputs)
    assert result.status == 'untrusted' and result.value is None


def test_zero_tax_and_signed_return_ignore_hostile_context():
    with localcontext() as context:
        context.prec = 2
        assert roic_gross(ebit=D('-20'), tax_rate=D('0'), ic_begin=D('90'), ic_end=D('110')).value == D('-.2')


def test_five_year_mean_uses_six_minority_inclusive_equity_balances_and_ids():
    rows = annuals()
    result = capital_return(snapshot(rows))
    assert result.value == D('.15') and result.status == 'verified'
    assert {row.evidence_id for row in rows} <= set(result.evidence_ids)
    # Parent-only net_assets and cash must not alter gross IC.
    assert capital_return(snapshot(alter(rows, 2025, net_assets='1', cash='10000'))).value == D('.15')


def test_roe_and_provider_roic_cannot_replace_missing_ebit_or_independent_interest():
    rows = alter(annuals(), 2025, ebit=None, interest_expense=None, roe='99', roic='99', finance_expense='4')
    result = capital_return(snapshot(rows))
    assert result.status == 'missing' and result.value is None
    assert 'interest_expense' in result.reason


def test_verified_pretax_plus_independent_interest_reconstructs_ebit():
    assert capital_return(snapshot(alter(annuals(), 2025, ebit=None))).value == D('.15')


@pytest.mark.parametrize('pretax,tax', [('-1', '1'), ('0', '0'), ('10', '20'), ('10', '-1')])
def test_invalid_current_tax_uses_only_previous_three_valid_years(pretax, tax):
    rows = alter(annuals(), 2025, pretax_income=pretax, income_tax=tax)
    rows = alter(alter(alter(rows, 2022, income_tax='0'), 2023, income_tax='4'), 2024, income_tax='8')
    # Historical rates 0, .25, .5 -> median .25. All annual NOPATs still count.
    assert capital_return(snapshot(rows)).value == D('.15')


def test_tax_fallback_never_uses_future_year_and_missing_tax_is_not_zero():
    rows = tuple(row for row in annuals() if row.period != '2020')
    assert capital_return(snapshot(rows)).status == 'missing'
    rows = tuple(evidence(str(y), total_equity_including_minority='80', interest_debt='20',
                          ebit='20', pretax_income='-1', income_tax='0') for y in range(2020, 2026))
    assert capital_return(snapshot(rows)).reason == 'effective_tax_rate:2021:missing'
    assert capital_return(snapshot(alter(annuals(), 2025, income_tax=None))).status == 'missing'


def test_merger_basis_jump_is_not_averaged_and_untrusted_equity_stays_untrusted():
    rows = annuals()
    modified = (*rows[:-1], changed(rows[-1], basis_id='new-consolidation'))
    assert capital_return(snapshot(modified)).reason == 'incomparable_basis'
    modified = (*rows[:-1], changed(rows[-1], status='untrusted'))
    assert capital_return(snapshot(modified)).status == 'untrusted'


@pytest.mark.parametrize('industry', ['银行', '保险', '证券'])
def test_financial_capital_return_is_not_applicable(industry):
    assert capital_return(snapshot(annuals(), profile(industry))).status == 'not_applicable'


@pytest.mark.parametrize('classification', [profile('', 'untrusted'), profile('-'), profile(status='untrusted')])
def test_unknown_or_untrusted_classification_cannot_imply_industrial(classification):
    assert capital_return(snapshot(annuals(), classification)).value is None


def test_missing_classification_cannot_imply_industrial():
    snap = freeze_fundamentals(annuals(), (), (), code='000001.SZ', cutoff_at=CUTOFF, current_mcap=None)
    assert capital_return(snap).reason == 'industry_classification_missing'


def test_nonpositive_ic_is_unavailable_but_verified_signed_inputs_are_preserved():
    assert capital_return(snapshot(alter(annuals(), 2020, total_equity_including_minority='0', interest_debt='0'))).value is None
    assert capital_return(snapshot(alter(annuals(), 2025, ebit=None, interest_expense='-1'))).value == D('.1425')
    assert capital_return(snapshot(alter(annuals(), 2025, total_equity_including_minority='-1'))).status == 'verified'


def test_confirmed_zero_tax_survives_pipeline_without_statutory_substitution():
    rows = tuple(changed(row, values={**row.values, 'income_tax': '0'}) for row in annuals())
    assert capital_return(snapshot(rows)).value == D('.2')


def test_even_tax_median_and_three_calendar_year_limit():
    # 2022 has invalid pretax; 2023 and 2024 yield .2 and .4 -> .3.
    rows = alter(alter(alter(alter(annuals(), 2025, pretax_income='-1'),
                       2022, pretax_income='-1'), 2023, income_tax='3.2'), 2024, income_tax='6.4')
    result = capital_return(snapshot(rows))
    # 2022 falls back to 2020/2021=.25. Rates: .25,.25,.2,.4,.3.
    assert result.value == D('.144')
    rows = tuple(row for row in annuals() if row.period != '2020')
    rows = (evidence('2017', pretax_income='10', income_tax='2'), *rows)
    rows = alter(rows, 2021, pretax_income='-1')
    assert 'effective_tax_rate:2021:missing' == capital_return(snapshot(rows)).reason


def test_future_classification_and_future_financial_fact_are_not_eligible():
    future = datetime(2026, 10, 23, tzinfo=timezone.utc)
    assert capital_return(snapshot(annuals(), changed(profile(), observed_at=future))).reason == 'industry_classification_missing'
    rows = (*annuals()[:-1], changed(annuals()[-1], observed_at=future))
    assert capital_return(snapshot(rows)).status == 'missing'


def test_untrusted_current_tax_never_uses_historical_fallback():
    rows = alter(annuals(), 2025, income_tax=None)
    tax = changed(evidence('2025', income_tax='4'), status='untrusted')
    result = capital_return(snapshot((*rows, tax)))
    assert result.status == 'untrusted'
    assert result.reason == 'income_tax:2025:untrusted'
    assert tax.evidence_id in result.evidence_ids


def test_conflicting_independent_facts_and_negative_debt_remain_untrusted():
    conflict = evidence('2025', ebit='30')
    result = capital_return(snapshot((*annuals(), conflict)))
    assert result.status == 'untrusted' and result.reason == 'conflicting_facts'
    assert {conflict.evidence_id, annuals()[-1].evidence_id} <= set(result.evidence_ids)
    assert capital_return(snapshot(alter(annuals(), 2025, interest_debt='-1'))).status == 'untrusted'


def test_bounded_numeric_failure_is_explicit():
    result = roic_gross(ebit=D('1e5000'), tax_rate=D('.25'), ic_begin=D('1'), ic_end=D('1'))
    assert result.status == 'untrusted' and result.reason == 'fundamentals_numeric_range'


def test_schema4_frozen_roundtrip_retains_metric_ids_and_values():
    from discovery.fundamentals import fundamentals_from_payload, snapshot_payload
    snap = snapshot(annuals())
    restored = fundamentals_from_payload(snapshot_payload(snap), code=snap.code)
    assert capital_return(restored) == capital_return(snap)


def test_untrusted_provider_ebit_can_use_verified_independent_reconstruction():
    rows = alter(annuals(), 2025, ebit=None)
    provider = changed(evidence('2025', ebit='999'), status='untrusted')
    result = capital_return(snapshot((*rows, provider)))
    assert result.value == D('.15')
    assert rows[-1].evidence_id in result.evidence_ids


def test_invalid_verified_ebit_never_hides_behind_independent_reconstruction():
    result = capital_return(snapshot(alter(annuals(), 2025, ebit='NaN')))
    assert result.status == 'untrusted' and result.reason == 'ebit:2025:invalid'
