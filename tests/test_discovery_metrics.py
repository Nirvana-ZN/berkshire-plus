from __future__ import annotations

import inspect
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    Clamped,
    Context,
    Decimal,
    DefaultContext,
    ROUND_DOWN,
    Rounded,
    getcontext,
    localcontext,
)

import pytest

from discovery.metrics import (
    FINANCIAL_PRECISION_CEILING,
    cagr,
    cash_conversion,
    coefficient_of_variation,
    finite_values,
    mean,
    median,
    normalized_earnings,
    percentile_rank,
)
from tests.discovery_helpers import decimal_series


D = Decimal


def test_finite_values_returns_immutable_tuple_without_mutating_input() -> None:
    values = [D("1.00"), None, D("NaN"), D("sNaN"), D("Infinity"), D("-2")]
    original = values.copy()

    result = finite_values(values)

    assert result == (D("1.00"), D("-2"))
    assert isinstance(result, tuple)
    assert values == original


def test_finite_values_accepts_a_generator() -> None:
    assert finite_values(value for value in [D("1"), None, D("2")]) == (
        D("1"),
        D("2"),
    )


@pytest.mark.parametrize("bad", [True, False, 1, 1.5, "1", object()])
def test_decimal_inputs_reject_type_confusion(bad: object) -> None:
    with pytest.raises(TypeError):
        finite_values([D("1"), bad])  # type: ignore[list-item]


@pytest.mark.parametrize(
    ("values", "expected_mean", "expected_median"),
    [
        ([D("1"), D("2"), D("9")], D("4"), D("2")),
        ([D("1"), D("2"), D("9"), D("12")], D("6"), D("5.5")),
        ([None, D("NaN"), D("Infinity")], None, None),
        ([D("1E+100"), D("3E+100")], D("2E+100"), D("2E+100")),
        ([D("1E-100"), D("3E-100")], D("2E-100"), D("2E-100")),
    ],
)
def test_mean_and_median_use_only_finite_decimals(
    values: list[Decimal | None],
    expected_mean: Decimal | None,
    expected_median: Decimal | None,
) -> None:
    assert mean(values) == expected_mean
    assert median(values) == expected_median


def test_cagr_uses_supplied_boundaries_and_ignores_invalid_interior_values() -> None:
    assert cagr([D("100"), None, D("sNaN"), D("121")], 2) == D("0.1")
    assert cagr((value for value in [D("100"), D("121")]), 2) == D("0.1")
    assert cagr([None, D("100"), D("121")], 2) is None
    assert cagr([D("100"), D("121"), D("Infinity")], 2) is None
    assert cagr([D("0"), D("121")], 2) is None
    assert cagr([D("100"), D("-121")], 2) is None
    assert cagr([D("100")], 1) is None


@pytest.mark.parametrize("years", [0, -1])
def test_cagr_rejects_non_positive_years(years: int) -> None:
    with pytest.raises(ValueError):
        cagr([D("100"), D("121")], years)


@pytest.mark.parametrize("years", [True, False, D("2"), 2.0, "2", None])
def test_cagr_rejects_non_integer_years(years: object) -> None:
    with pytest.raises(TypeError):
        cagr([D("100"), D("121")], years)  # type: ignore[arg-type]


def test_cagr_still_validates_interior_types() -> None:
    with pytest.raises(TypeError):
        cagr([D("100"), "missing", D("121")], 2)  # type: ignore[list-item]


def test_coefficient_of_variation_is_population_based() -> None:
    assert coefficient_of_variation([D("1"), D("3")]) == D("0.5")
    assert coefficient_of_variation([D("7"), D("7"), D("7")]) == D("0")
    assert coefficient_of_variation([D("-1"), D("1")]) is None
    assert coefficient_of_variation([D("7")]) is None
    assert coefficient_of_variation([None, D("NaN")]) is None


def test_cash_conversion_averages_only_valid_aligned_ratios() -> None:
    ocf = [D("10"), None, D("9"), D("Infinity"), D("4")]
    net_income = [D("5"), D("2"), D("-3"), D("1"), D("0")]

    assert cash_conversion(ocf, net_income, 2) == D("-0.5")
    assert cash_conversion(ocf, net_income, 3) is None
    assert ocf == [D("10"), None, D("9"), D("Infinity"), D("4")]
    assert net_income == [D("5"), D("2"), D("-3"), D("1"), D("0")]


def test_cash_conversion_accepts_generators_but_requires_equal_lengths() -> None:
    assert cash_conversion(
        (value for value in [D("4"), D("9")]),
        (value for value in [D("2"), D("3")]),
        2,
    ) == D("2.5")
    with pytest.raises(ValueError):
        cash_conversion([D("1")], [D("1"), D("2")], 1)


@pytest.mark.parametrize("minimum", [0, -1])
def test_minimum_observations_must_be_positive(minimum: int) -> None:
    with pytest.raises(ValueError):
        cash_conversion([D("1")], [D("1")], minimum)
    with pytest.raises(ValueError):
        normalized_earnings([D("1")], minimum)


@pytest.mark.parametrize("minimum", [True, D("1"), 1.0, "1", None])
def test_minimum_observations_must_be_exact_integer(minimum: object) -> None:
    with pytest.raises(TypeError):
        cash_conversion([D("1")], [D("1")], minimum)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        normalized_earnings([D("1")], minimum)  # type: ignore[arg-type]


def test_cash_conversion_rejects_bad_values_even_in_skipped_pairs() -> None:
    with pytest.raises(TypeError):
        cash_conversion([None], ["bad"], 1)  # type: ignore[list-item]


def test_normalized_earnings_is_median_not_peak_or_latest() -> None:
    earnings = [D("2"), None, D("100"), D("4"), D("NaN"), D("6")]
    assert normalized_earnings(earnings, 4) == D("5")
    assert normalized_earnings(earnings, 5) is None


@pytest.mark.parametrize(
    ("current", "history", "expected"),
    [
        (D("1"), [D("1"), D("2"), D("2"), D("4")], D("0")),
        (D("2"), [D("1"), D("2"), D("2"), D("4")], D("50")),
        (D("4"), [D("1"), D("2"), D("2"), D("4")], D("100")),
        (D("0.5"), [D("1"), D("2"), D("4")], D("0")),
        (D("5"), [D("1"), D("2"), D("4")], D("100")),
        (D("2"), [D("2")], D("50")),
        (D("1"), [D("2")], D("0")),
        (D("3"), [D("2")], D("100")),
        (
            D("2"),
            [None, D("-1"), D("0"), D("NaN"), D("1"), D("3")],
            D("50"),
        ),
    ],
)
def test_percentile_rank_has_deterministic_endpoints_and_ties(
    current: Decimal,
    history: list[Decimal | None],
    expected: Decimal,
) -> None:
    assert percentile_rank(current, history) == expected


def test_percentile_rank_accepts_generator_and_requires_positive_finite_current() -> None:
    assert percentile_rank(D("2"), (value for value in [D("1"), D("3")])) == D(
        "50"
    )
    assert percentile_rank(D("1"), []) is None
    assert percentile_rank(D("0"), [D("1")]) is None
    assert percentile_rank(D("NaN"), [D("1")]) is None


@pytest.mark.parametrize("current", [True, 1, 1.0, "1", None])
def test_percentile_rank_rejects_wrong_current_type(current: object) -> None:
    with pytest.raises(TypeError):
        percentile_rank(current, [D("1")])  # type: ignore[arg-type]


def test_percentile_history_rejects_wrong_types_instead_of_ignoring_them() -> None:
    with pytest.raises(TypeError):
        percentile_rank(D("1"), [D("1"), "2"])  # type: ignore[list-item]


def test_metrics_leave_global_decimal_context_unchanged_and_return_finite_values() -> None:
    before = getcontext().copy()
    results = [
        mean([D("1"), D("2")]),
        median([D("1"), D("2")]),
        cagr([D("100"), D("121")], 2),
        coefficient_of_variation([D("1"), D("3")]),
        cash_conversion([D("1")], [D("3")], 1),
        normalized_earnings([D("1"), D("2")], 2),
        percentile_rank(D("2"), [D("1"), D("3")]),
    ]

    assert repr(getcontext()) == repr(before)
    assert all(result is not None and result.is_finite() for result in results)


def test_metrics_source_never_converts_through_float() -> None:
    import discovery.metrics as metrics

    assert "float(" not in inspect.getsource(metrics)


def _context_signature(context: Context) -> tuple[object, ...]:
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.capitals,
        context.clamp,
        tuple(context.flags.items()),
        tuple(context.traps.items()),
    )


def _copy_context(target: Context, source: Context) -> None:
    for name in ("prec", "rounding", "Emin", "Emax", "capitals", "clamp"):
        setattr(target, name, getattr(source, name))
    for signal, value in source.flags.items():
        target.flags[signal] = value
    for signal, value in source.traps.items():
        target.traps[signal] = value


def _metric_probe() -> tuple[Decimal | None, ...]:
    return (
        mean(decimal_series("1", "3", "sNaN")),
        median(decimal_series("1", "3", "sNaN")),
        cagr(decimal_series("100", "sNaN", "121"), 2),
        coefficient_of_variation(decimal_series("1", "3", "sNaN")),
        cash_conversion(decimal_series("4", "9"), decimal_series("2", "3"), 2),
        normalized_earnings(decimal_series("1", "2", "3"), 3),
        percentile_rank(D("2"), decimal_series("1", "sNaN", "3")),
    )


def test_results_ignore_hostile_current_context_and_preserve_every_field() -> None:
    expected = (D("2"), D("2"), D("0.1"), D("0.5"), D("2.5"), D("2"), D("50"))
    with localcontext() as hostile:
        hostile.prec = 1
        hostile.rounding = ROUND_DOWN
        hostile.Emin = -9
        hostile.Emax = 9
        hostile.capitals = 0
        hostile.clamp = 1
        for signal in hostile.traps:
            hostile.traps[signal] = True
        hostile.flags[Rounded] = True
        hostile.flags[Clamped] = True
        before = _context_signature(hostile)

        assert _metric_probe() == expected
        assert _context_signature(getcontext()) == before


def test_results_ignore_hostile_default_context_without_mutating_it() -> None:
    saved = DefaultContext.copy()
    try:
        DefaultContext.prec = 1
        DefaultContext.rounding = ROUND_DOWN
        DefaultContext.Emin = -9
        DefaultContext.Emax = 9
        DefaultContext.capitals = 0
        DefaultContext.clamp = 1
        for signal in DefaultContext.traps:
            DefaultContext.traps[signal] = True
        DefaultContext.flags[Rounded] = True
        DefaultContext.flags[Clamped] = True
        before = _context_signature(DefaultContext)

        assert _metric_probe() == (
            D("2"),
            D("2"),
            D("0.1"),
            D("0.5"),
            D("2.5"),
            D("2"),
            D("50"),
        )
        assert _context_signature(DefaultContext) == before
    finally:
        _copy_context(DefaultContext, saved)


@pytest.mark.parametrize(
    ("first", "third", "expected"),
    [
        (
            D(f"1E+{MAX_EMAX}"),
            D(f"3E+{MAX_EMAX}"),
            D(f"2E+{MAX_EMAX}"),
        ),
        (
            D(f"1E{MIN_EMIN}"),
            D(f"3E{MIN_EMIN}"),
            D(f"2E{MIN_EMIN}"),
        ),
    ],
)
def test_scale_stable_mean_median_and_cv_at_decimal_limits(
    first: Decimal,
    third: Decimal,
    expected: Decimal,
) -> None:
    assert mean([first, third]) == expected
    assert median([first, third]) == expected
    assert coefficient_of_variation([first, third]) == D("0.5")


@pytest.mark.parametrize(
    ("cash", "income"),
    [
        (
            decimal_series(f"1E+{MAX_EMAX}", f"3E+{MAX_EMAX}"),
            decimal_series(f"1E+{MAX_EMAX}", f"1E+{MAX_EMAX}"),
        ),
        (
            decimal_series(f"1E{MIN_EMIN}", f"3E{MIN_EMIN}"),
            decimal_series(f"1E{MIN_EMIN}", f"1E{MIN_EMIN}"),
        ),
    ],
)
def test_cash_conversion_is_scale_stable_at_decimal_limits(
    cash: tuple[Decimal | None, ...],
    income: tuple[Decimal | None, ...],
) -> None:
    assert cash_conversion(cash, income, 2) == D("2")


def test_precision_ceiling_returns_none_instead_of_allocating_unbounded_work() -> None:
    distant = D(f"1E+{FINANCIAL_PRECISION_CEILING + 10}")
    assert mean([D("1"), distant]) is None
    assert median([D("1"), distant]) is None
    assert coefficient_of_variation([D("1"), distant]) is None
    assert cash_conversion([distant, D("1")], [D("1"), D("1")], 2) is None
    assert cagr(
        [D("1"), D("1" + "0" * (FINANCIAL_PRECISION_CEILING + 1))],
        1,
    ) is None
    assert cagr([D("1"), D("2")], 10 ** (FINANCIAL_PRECISION_CEILING + 1)) is None


def test_cagr_keeps_tiny_positive_growth_for_large_bounded_year_count() -> None:
    result = cagr([D("1"), D("2")], 10**41)

    assert result is not None
    assert result.is_finite()
    assert result > 0
