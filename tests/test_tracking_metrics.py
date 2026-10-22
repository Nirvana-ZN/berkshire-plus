"""Exact daily paper-equity metrics; no market network or production database."""

from decimal import Decimal as D, ROUND_DOWN, localcontext

import pytest


def test_drawdown_uses_the_entire_daily_equity_path():
    from tracking.metrics import max_drawdown
    assert max_drawdown((D('100'), D('120'), D('90'), D('110'))) == D('0.25')


def test_returns_include_losses_and_exact_decimal_values():
    from tracking.metrics import equity_return, max_drawdown
    assert equity_return(D('100'), D('110')) == D('.1')
    assert equity_return(D('100'), D('0')) == D('-1')
    assert max_drawdown((D('100'), D('0'), D('80'))) == D('1')
    with localcontext() as ctx:
        ctx.prec = 4
        assert equity_return(D('3'), D('4')) == D('0.3333333333333333333333333333333333')
        ctx.rounding = ROUND_DOWN
        assert equity_return(D('3'), D('5')) == D('0.6666666666666666666666666666666667')


@pytest.mark.parametrize('values', [(), (D('0'),), (D('-1'),), (D('1'), D('-1')),
                                    (D('1'), D('NaN')), (D('Infinity'),), (1.0,)])
def test_drawdown_rejects_invalid_equity(values):
    from tracking.metrics import max_drawdown
    with pytest.raises(ValueError):
        max_drawdown(values)


@pytest.mark.parametrize('initial,ending', [(D('0'), D('1')), (D('-1'), D('1')),
    (D('1'), D('-1')), (D('NaN'), D('1')), (D('1'), D('Infinity')), (1.0, D('1'))])
def test_return_rejects_invalid_equity(initial, ending):
    from tracking.metrics import equity_return
    with pytest.raises(ValueError):
        equity_return(initial, ending)
