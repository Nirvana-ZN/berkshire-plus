"""Finite Decimal metrics on complete daily paper-equity paths."""

from collections.abc import Sequence
from decimal import Context, Decimal, localcontext


def _equity(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError('equity must be a finite nonnegative Decimal')
    return value


def equity_return(initial: Decimal, ending: Decimal) -> Decimal:
    if _equity(initial) == 0:
        raise ValueError('initial equity must be positive')
    with localcontext(Context(prec=34)):
        return (_equity(ending) - initial) / initial


def max_drawdown(equity: Sequence[Decimal]) -> Decimal:
    if not equity or _equity(equity[0]) == 0:
        raise ValueError('invalid equity curve')
    with localcontext(Context(prec=34)):
        peak, worst = equity[0], Decimal('0')
        for value in equity:
            peak = max(peak, _equity(value))
            worst = max(worst, (peak - value) / peak)
        return worst
