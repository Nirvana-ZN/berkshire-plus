from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
from decimal import Decimal as D, Inexact, ROUND_DOWN, localcontext
import json

import pytest

from discovery.cash_quality import cash_metrics


def metrics(**changes):
    return cash_metrics(**({'cash': D('100'), 'restricted_cash': D('20'),
                           'debt': D('30'), 'fcf': D('10'), 'mcap': D('200')}
                          | changes))


def test_less_fcf_never_improves_net_cash_debt_rank():
    a, b = metrics(fcf=D('10')), metrics(fcf=D('1'))
    assert a['debt_burden'].value == b['debt_burden'].value == 0
    assert a['fcf_yield'].value > b['fcf_yield'].value


def test_post_issuance_current_mcap_controls_yields_without_annual_shares_fallback():
    old, current = metrics(mcap=D('200')), metrics(mcap=D('220'))
    assert current['usable_net_cash_to_mcap'].value < old['usable_net_cash_to_mcap'].value
    assert current['fcf_yield'].value < old['fcf_yield'].value
    missing = metrics(mcap=None)
    assert missing['usable_net_cash_to_mcap'].status == 'missing'
    assert missing['fcf_yield'].status == 'missing'


def test_cash_metrics_separate_usable_cash_debt_burden_and_yields():
    assert {k: v.value for k, v in metrics().items()} == {
        'usable_cash': D('80'), 'debt_burden': D('0'),
        'usable_net_cash_to_mcap': D('.25'), 'fcf_yield': D('.05')}
    indebted = metrics(debt=D('110'))
    assert indebted['debt_burden'].value == D('3')
    assert indebted['usable_net_cash_to_mcap'].value == 0


def test_missing_restricted_cash_only_blocks_dependent_metrics():
    result = metrics(restricted_cash=None)
    for name in ('usable_cash', 'debt_burden', 'usable_net_cash_to_mcap'):
        assert result[name].status == 'missing'
        assert result[name].value is None
        assert result[name].reason == 'restricted_cash_missing'
    assert result['fcf_yield'].value == D('.05')


@pytest.mark.parametrize('field,dependent', [
    ('cash', ('usable_cash', 'debt_burden', 'usable_net_cash_to_mcap')),
    ('debt', ('debt_burden', 'usable_net_cash_to_mcap')),
    ('fcf', ('debt_burden', 'fcf_yield')),
    ('mcap', ('usable_net_cash_to_mcap', 'fcf_yield')),
])
def test_missing_inputs_do_not_erase_independent_results(field, dependent):
    result = metrics(**{field: None})
    for key, item in result.items():
        assert item.status == ('missing' if key in dependent else 'verified')


@pytest.mark.parametrize('field,value', [
    ('cash', '-1'), ('restricted_cash', '-1'), ('debt', '-1'),
    ('cash', 'NaN'), ('restricted_cash', 'Infinity'), ('debt', '-Infinity'),
    ('fcf', 'NaN'), ('mcap', 'Infinity'), ('mcap', '0'), ('mcap', '-1'),
])
def test_invalid_inputs_are_untrusted_and_never_zero(field, value):
    result = metrics(**{field: D(value)})
    assert any(item.status == 'untrusted' for item in result.values())
    assert all(item.value is None for item in result.values()
               if item.status == 'untrusted')


def test_restricted_cash_exceeding_cash_is_conflict_not_clamped_zero():
    result = metrics(restricted_cash=D('101'))
    assert result['usable_cash'].status == 'untrusted'
    assert result['usable_cash'].reason == 'restricted_cash_exceeds_cash'
    assert result['usable_net_cash_to_mcap'].value is None
    assert result['fcf_yield'].value == D('.05')


@pytest.mark.parametrize('fcf', ['0', '-10'])
def test_nonpositive_fcf_cannot_pass_debt_condition_but_has_signed_yield(fcf):
    result = metrics(fcf=D(fcf))
    assert result['debt_burden'].value is None
    assert result['debt_burden'].reason == 'fcf_not_positive'
    assert result['fcf_yield'].value == (D('0') if fcf == '0' else D('-.05'))
    assert result['usable_net_cash_to_mcap'].value == D('.25')


def test_finite_decimal_arithmetic_is_independent_of_callers_context():
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_DOWN
        ctx.traps[Inexact] = True
        result = metrics(cash=D('100.001'), fcf=D('3'))
    assert result['usable_cash'].value == D('80.001')
    assert result['fcf_yield'].value == D('.015')
    assert metrics(cash=D('1e10000'))['usable_cash'].status == 'untrusted'


def test_binary_float_inputs_are_rejected():
    with pytest.raises(TypeError):
        metrics(cash=100.0)


def test_bank_applicability_is_explicit_in_track_adapter():
    from discovery.tracks.deep_value import cash_metrics_for_branch
    kwargs = dict(cash=D('100'), restricted_cash=D('0'), debt=D('0'),
                  fcf=D('10'), mcap=D('200'))
    bank = cash_metrics_for_branch(branch='bank', **kwargs)
    assert all(item.status == 'not_applicable' and item.value is None
               for item in bank.values())
    assert cash_metrics_for_branch(branch='nonfinancial', **kwargs)[
        'debt_burden'].value == 0
    assert all(item.status == 'missing' for item in
               cash_metrics_for_branch(branch='unknown', **kwargs).values())
    with pytest.raises(ValueError):
        cash_metrics_for_branch(branch='typo', **kwargs)


@pytest.mark.parametrize('basis,status', [('fixture-consolidated-cny', 'verified'), ('other-basis', 'untrusted')])
def test_cash_source_readiness_keeps_basis_cutoff_and_all_input_identities(basis, status):
    from discovery.fundamentals import cash_debt_sources, freeze_fundamentals
    from discovery.fundamental_types import evidence_identity
    from tests.selection_helpers import evidence
    cash = evidence('2025', cash='60', interest_debt='20')
    restricted = replace(evidence('2025', restricted_cash='0'), basis_id=basis)
    restricted = replace(restricted, evidence_id=evidence_identity(restricted))
    future = replace(evidence('2026H1', cash='-1', restricted_cash='0', interest_debt='0'),
                     observed_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    future = replace(future, evidence_id=evidence_identity(future))
    snapshot = freeze_fundamentals((cash, restricted, future), (), (), code=cash.code,
        cutoff_at=cash.observed_at, current_mcap=None)
    result = cash_debt_sources(snapshot)
    assert result.status == status
    assert set(result.evidence_ids) == {cash.evidence_id, restricted.evidence_id}
    assert result.value == (D('-40') if status == 'verified' else None)
    assert result.reason == (None if status == 'verified' else 'incomparable_basis')


OBSERVED = datetime(2026, 10, 22, 8, tzinfo=timezone.utc)
DAY = date(2026, 10, 21)
SOURCE = 'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET'


def market_cap(row=None, **changes):
    from market.eastmoney import parse_current_market_cap_response
    row = {'SECUCODE': '000001.SZ', 'TRADE_DATE': '2026-10-21 00:00:00',
           'TOTAL_MARKET_CAP': '123456789012.123456789'} if row is None else row
    raw = json.dumps({'success': True, 'result': {'data': [row]}}).encode()
    return parse_current_market_cap_response(
        raw, code='000001.SZ', market_date=DAY, observed_at=OBSERVED,
        source_url=SOURCE, **changes)


def test_current_mcap_keeps_actual_market_day_and_provenance():
    result = market_cap()
    assert result.value == D('123456789012.123456789')
    assert result.status == 'verified'
    assert result.market_date == DAY
    assert result.observed_at == OBSERVED
    assert result.source == 'eastmoney:RPT_VALUEANALYSIS_DET'
    assert result.source_url == SOURCE
    assert result.response_sha256 == '109e2d2236199f2fec287129579bc9f222c05e3d259261f66ecc55dfb101f815'
    with pytest.raises(FrozenInstanceError):
        result.value = D('1')
    with pytest.raises(ValueError):
        replace(result, value=D('NaN'))


@pytest.mark.parametrize('row,reason', [
    ({'SECUCODE': '000001.SZ', 'TOTAL_MARKET_CAP': '200'}, 'market_date_missing'),
    ({'SECUCODE': '000001.SZ', 'TRADE_DATE': '2026-10-20',
      'TOTAL_MARKET_CAP': '200'}, 'market_date_mismatch'),
    ({'SECUCODE': '000002.SZ', 'TRADE_DATE': '2026-10-21',
      'TOTAL_MARKET_CAP': '200'}, 'market_cap_code_mismatch'),
    ({'SECUCODE': '000001.SZ', 'TRADE_DATE': '2026-10-21',
      'TOTAL_MARKET_CAP': None}, 'current_mcap_missing'),
    ({'SECUCODE': '000001.SZ', 'TRADE_DATE': '2026-10-21',
      'TOTAL_MARKET_CAP': '-200'}, 'current_mcap_invalid'),
])
def test_unverified_market_cap_never_uses_old_annual_shares(row, reason):
    result = market_cap(row)
    assert result.value is None
    assert result.status in {'missing', 'untrusted'}
    assert result.reason == reason


def test_source_date_is_never_the_observation_day():
    assert market_cap().market_date != OBSERVED.date()
    result = market_cap({'SECUCODE': '000001.SZ', 'TRADE_DATE': '2026-10-23',
                         'TOTAL_MARKET_CAP': '200'})
    assert result.status == 'untrusted'


def test_market_cap_parser_preserves_numeric_json_precision():
    from market.eastmoney import parse_current_market_cap_response
    result = parse_current_market_cap_response(
        b'{"success":true,"result":{"data":[{"SECUCODE":"000001.SZ",'
        b'"TRADE_DATE":"2026-10-21","TOTAL_MARKET_CAP":123456789012.123456789}]}}',
        code='000001.SZ', market_date=DAY, observed_at=OBSERVED, source_url=SOURCE)
    assert result.value == D('123456789012.123456789')


def test_market_cap_provider_failure_or_empty_response_is_unavailable():
    from market.eastmoney import parse_current_market_cap_response
    for raw in (b'{"success":false}', b'{"success":true,"result":{"data":[]}}'):
        result = parse_current_market_cap_response(
            raw, code='000001.SZ', market_date=DAY, observed_at=OBSERVED, source_url=SOURCE)
        assert result.value is None
        assert result.status in {'missing', 'untrusted'}


@pytest.mark.parametrize('returned_day,expected_status', [
    ('2026-10-21 00:00:00', 'verified'), ('2026-10-20 00:00:00', 'untrusted')])
def test_fetch_current_mcap_requests_exact_day_and_keeps_raw_precision(
        monkeypatch, returned_day, expected_status):
    import httpx2
    from urllib.parse import parse_qs, urlparse
    from market.eastmoney import EastMoneyClient
    raw = ('{"success":true,"result":{"data":[{"SECUCODE":"000001.SZ",'
           f'"TRADE_DATE":"{returned_day}",'
           '"TOTAL_MARKET_CAP":123456789012.123456789}]}}').encode()
    requests, hooks = [], []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            requests.append(url)
            if len(requests) == 1:
                raise httpx2.ConnectError('temporary source failure')
            return httpx2.Response(200, content=raw)

    monkeypatch.setattr(httpx2, 'Client', Client)
    client = EastMoneyClient()
    result = client.fetch_current_market_cap(
        '000001.SZ', market_date=DAY, before_request=lambda: hooks.append('request'))
    query = parse_qs(urlparse(requests[-1]).query)
    assert query['reportName'] == ['RPT_VALUEANALYSIS_DET']
    assert query['columns'] == ['SECUCODE,TRADE_DATE,TOTAL_MARKET_CAP']
    assert query['filter'] == ['(SECUCODE="000001.SZ")(TRADE_DATE=\'2026-10-21\')']
    assert result.status == expected_status
    assert result.market_date == date.fromisoformat(returned_day[:10])
    assert result.value == (D('123456789012.123456789')
                            if expected_status == 'verified' else None)
    assert result.observed_at.utcoffset().total_seconds() == 0
    assert len(requests) == len(hooks) == client.request_count == 2


def test_fetch_current_mcap_propagates_policy_stop_before_network(monkeypatch):
    from market.eastmoney import EastMoneyClient
    from market.types import DiscoveryRequestBudgetExhausted
    client = EastMoneyClient()

    def stop():
        raise DiscoveryRequestBudgetExhausted('stop')

    with pytest.raises(DiscoveryRequestBudgetExhausted):
        client.fetch_current_market_cap('000001.SZ', market_date=DAY, before_request=stop)
    assert client.request_count == 0


@pytest.mark.parametrize('raw_date', ['2026-10-21junk', '2026-10-21T09:30:00',
                                     '2026-10-21T00:00:00Z', '20261021'])
def test_market_date_cannot_be_truncated_from_arbitrary_timestamp(raw_date):
    result = market_cap({'SECUCODE': '000001.SZ', 'TRADE_DATE': raw_date,
                         'TOTAL_MARKET_CAP': '200'})
    assert result.status == 'untrusted'
    assert result.value is None
