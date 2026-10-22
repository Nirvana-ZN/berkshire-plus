"""Branch-agnostic cash safety calculations over already trusted CNY inputs.

None means missing, never zero. Evidence selection/trust and industry
applicability belong to adapters; these results certify arithmetic only.
"""

from collections.abc import Callable
from decimal import Decimal, DecimalException, localcontext

from discovery.fundamental_types import MetricResult
from discovery.metrics import _context, _working_precision


CASH_METRIC_KEYS = ('usable_cash', 'debt_burden', 'usable_net_cash_to_mcap', 'fcf_yield')
_ZERO = Decimal('0')


def _input(name: str, value: Decimal | None) -> MetricResult:
    if value is None:
        return MetricResult(None, 'missing', f'{name}_missing', ())
    if not isinstance(value, Decimal):
        raise TypeError(f'{name} must be Decimal or None')
    if (not value.is_finite() or (name in {'cash', 'restricted_cash', 'debt'} and value < 0)
            or (name == 'mcap' and value <= 0)):
        return MetricResult(None, 'untrusted', f'{name}_invalid', ())
    return MetricResult(value, 'verified', None, ())


def _calculate(inputs: tuple[MetricResult, ...], operation: Callable[..., Decimal]) -> MetricResult:
    # Invalid known evidence takes precedence over an independently missing input.
    for status in ('untrusted', 'missing'):
        for item in inputs:
            if item.status == status:
                return item
    values = tuple(item.value for item in inputs)
    precision = _working_precision(values)
    if precision is not None:
        try:
            with localcontext(_context(precision)):
                value = operation(*values)
            if value.is_finite():
                return MetricResult(value, 'verified', None, ())
        except DecimalException:
            pass
    return MetricResult(None, 'untrusted', 'cash_metrics_numeric_range', ())


def cash_metrics(*, cash: Decimal | None, restricted_cash: Decimal | None,
                 debt: Decimal | None, fcf: Decimal | None,
                 mcap: Decimal | None) -> dict[str, MetricResult]:
    """Separate usable cash, debt service and equity yields without negative rewards.

    Cash/restricted/debt must be nonnegative; mcap must be positive. Signed FCF
    remains usable for yield, but FCF <= 0 makes debt_burden unavailable with
    fcf_not_positive (untrusted). Missing dependencies return missing. Conflicting
    amounts/nonfinite/domain failures return untrusted. Independent metrics survive.
    No source trust, normalization window or industry identity is inferred here.
    """
    c, r, d, f, m = (_input(name, value) for name, value in (
        ('cash', cash), ('restricted_cash', restricted_cash), ('debt', debt),
        ('fcf', fcf), ('mcap', mcap)))
    usable = _calculate((c, r), lambda cash, restricted: cash - restricted)
    if usable.value is not None and usable.value < 0:
        usable = MetricResult(None, 'untrusted', 'restricted_cash_exceeds_cash', ())
    net_debt = _calculate((d, usable), lambda debt, cash: debt - cash)
    positive_fcf = f
    if f.value is not None and f.value <= 0:
        positive_fcf = MetricResult(None, 'untrusted', 'fcf_not_positive', ())
    return {
        'usable_cash': usable,
        'debt_burden': _calculate((net_debt, positive_fcf),
                                  lambda debt, fcf: max(debt, _ZERO) / fcf),
        'usable_net_cash_to_mcap': _calculate((net_debt, m),
                                             lambda debt, mcap: max(-debt, _ZERO) / mcap),
        'fcf_yield': _calculate((f, m), lambda fcf, mcap: fcf / mcap),
    }
