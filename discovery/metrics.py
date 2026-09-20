"""Exact, reusable Decimal metrics for weekly discovery."""

from __future__ import annotations

from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Iterable


DecimalValues = Iterable[Decimal | None]
FINANCIAL_PRECISION_CEILING = 4096
# ponytail: 4096 significant digits bound financial-data work; raise it only
# after profiling and validating a wider upstream financial-data contract.


def _values(values: DecimalValues) -> tuple[Decimal | None, ...]:
    try:
        items = tuple(values)
    except TypeError as error:
        raise TypeError("values must be an iterable of Decimal or None") from error
    if any(value is not None and not isinstance(value, Decimal) for value in items):
        raise TypeError("values must contain only Decimal or None")
    return items


def _minimum(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("minimum observations must be an integer")
    if value <= 0:
        raise ValueError("minimum observations must be positive")
    return value


def _integer_digits_upper(value: int) -> int:
    return max(1, (value.bit_length() * 30103) // 100000 + 1)


def _working_precision(
    values: Iterable[Decimal],
    extra: int = 32,
) -> int | None:
    items = tuple(values)
    if not items:
        return 34
    nonzero = tuple(value for value in items if not value.is_zero())
    if not nonzero:
        return 34
    highest = max(value.adjusted() for value in nonzero)
    lowest = min(value.as_tuple().exponent for value in nonzero)
    required = max(
        34,
        highest
        - lowest
        + 1
        + _integer_digits_upper(len(nonzero))
        + extra,
    )
    return required if required <= FINANCIAL_PRECISION_CEILING else None


def _context(precision: int) -> Context:
    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emax=MAX_EMAX,
        Emin=MIN_EMIN,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    precision = _working_precision(values)
    if precision is None:
        return None
    scale = max(value.copy_abs() for value in values)
    scale_exponent = scale.adjusted()
    try:
        with localcontext(_context(precision)) as context:
            zero = context.create_decimal(0)
            if scale.is_zero():
                return zero
            down = context.create_decimal(-scale_exponent)
            up = context.create_decimal(scale_exponent)
            total = zero
            for value in values:
                total = context.add(total, context.scaleb(value, down))
            result = context.scaleb(
                context.divide(total, context.create_decimal(len(values))),
                up,
            )
    except DecimalException:
        return None
    return result if result.is_finite() else None


def finite_values(values: DecimalValues) -> tuple[Decimal, ...]:
    """Return finite observations in input order as an immutable tuple."""

    return tuple(
        value
        for value in _values(values)
        if value is not None and value.is_finite()
    )


def mean(values: DecimalValues) -> Decimal | None:
    """Return the Decimal arithmetic mean of finite observations."""

    usable = finite_values(values)
    return _mean(usable) if usable else None


def median(values: DecimalValues) -> Decimal | None:
    """Return the exact middle value (or even-pair average) of finite values."""

    usable = tuple(sorted(finite_values(values)))
    if not usable:
        return None
    middle = len(usable) // 2
    if len(usable) % 2:
        return usable[middle]
    return _mean(usable[middle - 1 : middle + 1])


def cagr(values: DecimalValues, years: int) -> Decimal | None:
    """Return ``(last / first) ** (1 / years) - 1``.

    The first and last supplied observations are the endpoints. Missing or
    non-finite interior observations do not affect the result, but a missing,
    non-finite, or non-positive endpoint makes it unavailable.
    """

    if isinstance(years, bool) or not isinstance(years, int):
        raise TypeError("years must be an integer")
    if years <= 0:
        raise ValueError("years must be positive")
    supplied = _values(values)
    if len(supplied) < 2:
        return None
    first, last = supplied[0], supplied[-1]
    if (
        first is None
        or last is None
        or not first.is_finite()
        or not last.is_finite()
        or first <= 0
        or last <= 0
    ):
        return None
    year_digits = _integer_digits_upper(years)
    endpoint_precision = _working_precision((first, last), extra=year_digits + 40)
    if endpoint_precision is None:
        return None
    try:
        with localcontext(_context(endpoint_precision)) as context:
            ratio = context.divide(last, first)
            annual_log = context.divide(
                context.ln(ratio),
                context.create_decimal(years),
            )
            result = context.subtract(
                context.exp(annual_log),
                context.create_decimal(1),
            )
    except DecimalException:
        return None
    return result if result.is_finite() else None


def coefficient_of_variation(values: DecimalValues) -> Decimal | None:
    """Return population standard deviation divided by the absolute mean.

    At least two finite observations and a non-zero mean are required.
    """

    usable = finite_values(values)
    if len(usable) < 2:
        return None
    precision = _working_precision(usable, extra=40)
    if precision is None:
        return None
    scale = max(value.copy_abs() for value in usable)
    scale_exponent = scale.adjusted()
    try:
        with localcontext(_context(precision)) as context:
            zero = context.create_decimal(0)
            if scale.is_zero():
                return None
            count = context.create_decimal(len(usable))
            down = context.create_decimal(-scale_exponent)
            scaled = tuple(context.scaleb(value, down) for value in usable)
            total = zero
            for value in scaled:
                total = context.add(total, value)
            average = context.divide(total, count)
            if average.is_zero():
                return None
            squared_deviations = zero
            for value in scaled:
                deviation = context.subtract(value, average)
                squared_deviations = context.add(
                    squared_deviations,
                    context.multiply(deviation, deviation),
                )
            variance = context.divide(squared_deviations, count)
            if variance.is_zero():
                return zero
            result = context.divide(
                context.sqrt(variance),
                average.copy_abs(),
            )
    except DecimalException:
        return None
    return result if result.is_finite() else None


def cash_conversion(
    ocf: DecimalValues,
    net_income: DecimalValues,
    min_observations: int,
) -> Decimal | None:
    """Average aligned OCF/net-income ratios with enough valid periods.

    A pair is omitted when either side is missing or non-finite, or when net
    income is zero. Negative net income remains a valid denominator.
    """

    minimum = _minimum(min_observations)
    ocf_values = _values(ocf)
    income_values = _values(net_income)
    if len(ocf_values) != len(income_values):
        raise ValueError("ocf and net_income must have equal lengths")
    pairs = tuple(
        (cash, earnings)
        for cash, earnings in zip(ocf_values, income_values, strict=True)
        if cash is not None
        and earnings is not None
        and cash.is_finite()
        and earnings.is_finite()
        and not earnings.is_zero()
    )
    if len(pairs) < minimum:
        return None
    ratio_precision = max(
        40,
        max(
            len(cash.as_tuple().digits) + len(earnings.as_tuple().digits) + 32
            for cash, earnings in pairs
        ),
    )
    if ratio_precision > FINANCIAL_PRECISION_CEILING:
        return None
    try:
        with localcontext(_context(ratio_precision)) as context:
            ratios = tuple(
                context.divide(cash, earnings) for cash, earnings in pairs
            )
    except DecimalException:
        return None
    if any(not ratio.is_finite() for ratio in ratios):
        return None
    return _mean(ratios)


def normalized_earnings(
    deducted_earnings: DecimalValues,
    min_observations: int,
) -> Decimal | None:
    """Return median finite deducted earnings when the minimum is met."""

    minimum = _minimum(min_observations)
    usable = finite_values(deducted_earnings)
    return median(usable) if len(usable) >= minimum else None


def empirical_percentile_rank(
    current: Decimal,
    history: DecimalValues,
) -> Decimal | None:
    """Rank any finite signed Decimal from zero through one hundred.

    Ties use average rank with ``(rank - 1) / (n - 1)`` semantics. Against
    one observation, lower/equal/higher maps to 0/50/100.
    """

    if not isinstance(current, Decimal):
        raise TypeError("current must be a Decimal")
    supplied = _values(history)
    if not current.is_finite():
        return None
    usable = tuple(
        value
        for value in supplied
        if value is not None and value.is_finite()
    )
    if not usable:
        return None
    if len(usable) == 1:
        constant = 0 if current < usable[0] else 100 if current > usable[0] else 50
        with localcontext(_context(34)) as context:
            return context.create_decimal(constant)
    below = sum(value < current for value in usable)
    equal = sum(value == current for value in usable)
    numerator = (2 * below + equal - 1) * 100
    denominator = 2 * (len(usable) - 1)
    precision = max(
        34,
        _integer_digits_upper(abs(numerator)),
        _integer_digits_upper(denominator),
    )
    if precision > FINANCIAL_PRECISION_CEILING:
        return None
    with localcontext(_context(precision)) as context:
        zero = context.create_decimal(0)
        hundred = context.create_decimal(100)
        result = context.divide(
            context.create_decimal(numerator),
            context.create_decimal(denominator),
        )
        return min(hundred, max(zero, result))


def percentile_rank(
    current: Decimal,
    history: DecimalValues,
) -> Decimal | None:
    """Return a deterministic empirical rank from zero through one hundred.

    Non-positive and non-finite history is ignored. Ties use average rank with
    ``(rank - 1) / (n - 1)`` semantics and out-of-range results are clamped.
    Against one historical point, lower/equal/higher maps to 0/50/100.
    """

    if not isinstance(current, Decimal):
        raise TypeError("current must be a Decimal")
    supplied = _values(history)
    if not current.is_finite() or current <= 0:
        return None
    usable = tuple(
        value
        for value in supplied
        if value is not None and value.is_finite() and value > 0
    )
    if not usable:
        return None
    if len(usable) == 1:
        constant = 0 if current < usable[0] else 100 if current > usable[0] else 50
        with localcontext(_context(34)) as context:
            return context.create_decimal(constant)
    below = sum(value < current for value in usable)
    equal = sum(value == current for value in usable)
    numerator = (2 * below + equal - 1) * 100
    denominator = 2 * (len(usable) - 1)
    precision = max(
        34,
        _integer_digits_upper(abs(numerator)),
        _integer_digits_upper(denominator),
    )
    if precision > FINANCIAL_PRECISION_CEILING:
        return None
    with localcontext(_context(precision)) as context:
        zero = context.create_decimal(0)
        hundred = context.create_decimal(100)
        result = context.divide(
            context.create_decimal(numerator),
            context.create_decimal(denominator),
        )
        return min(hundred, max(zero, result))
