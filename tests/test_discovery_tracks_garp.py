from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from decimal import DefaultContext, Decimal, getcontext, setcontext
from pathlib import Path
from types import MappingProxyType

import pytest

HISTORICAL_BUILD_VERSION = 'discovery-v7'
from discovery.snapshot import FrozenRunInputs
from tests.test_discovery_tracks_quality import (
    _annual,
    _calibration_preflight_db,
    _metadata,
    _valuations,
    frozen_company,
)


FIELDS = [
    "revenue_cagr",
    "deducted_profit_cagr",
    "revenue_consistent_growth_periods",
    "deducted_profit_consistent_growth_periods",
    "latest_revenue_growth",
    "latest_deducted_profit_growth",
    "revenue_growth_slowdown",
    "deducted_profit_growth_slowdown",
    "return_on_capital",
    "return_metric_label",
    "cash_conversion",
    "dilution_rate",
    "interest_debt_increase_to_revenue",
    "goodwill_increase_to_revenue",
    "acquisition_cash_to_revenue",
    "gross_margin_change",
    "net_margin_change",
    "impairment_share",
    "non_core_income_proxy_share",
    "capped_growth",
    "peg",
    "pe_percentile",
    "current_pe",
    "code",
]
FORMULAS = {
    "revenue_cagr": "cagr(exact_annual.revenue,revenue_cagr_periods)",
    "deducted_profit_cagr": (
        "cagr(exact_annual.deducted_net_income,deducted_profit_cagr_periods)"
    ),
    "revenue_consistent_growth_periods": (
        "count(exact_annual.revenue_yoy>=revenue_period_growth_min)"
    ),
    "deducted_profit_consistent_growth_periods": (
        "count(exact_annual.deducted_net_income_yoy"
        ">=deducted_profit_period_growth_min)"
    ),
    "latest_revenue_growth": "latest(annual.revenue_yoy)",
    "latest_deducted_profit_growth": (
        "latest(annual.deducted_net_income_yoy)"
    ),
    "revenue_growth_slowdown": (
        "max(0,revenue_cagr-latest_revenue_growth)"
    ),
    "deducted_profit_growth_slowdown": (
        "max(0,deducted_profit_cagr-latest_deducted_profit_growth)"
    ),
    "return_on_capital": "mean(primary(annual.roic,fallback=annual.roe))",
    "return_metric_label": "label(primary(annual.roic,fallback=annual.roe))",
    "cash_conversion": "sum(annual.ocf)/sum(annual.net_income)|annual.net_income>0",
    "dilution_rate": "latest(annual.shares)/earliest(annual.shares)-1",
    "interest_debt_increase_to_revenue": (
        "max(0,latest(annual.interest_debt)-earliest(annual.interest_debt))/"
        "latest(annual.revenue)"
    ),
    "goodwill_increase_to_revenue": (
        "max(0,latest(annual.goodwill)-earliest(annual.goodwill))/"
        "latest(annual.revenue)"
    ),
    "acquisition_cash_to_revenue": (
        "sum(max(0,annual.acquisition_cash_paid))/sum(annual.revenue)"
    ),
    "gross_margin_change": (
        "latest(annual.gross_margin)-earliest(annual.gross_margin)"
    ),
    "net_margin_change": (
        "latest(annual.net_margin)-earliest(annual.net_margin)"
    ),
    "impairment_share": (
        "copy_abs(latest(annual.impairment_loss))/"
        "copy_abs(latest(annual.deducted_net_income))"
    ),
    "non_core_income_proxy_share": (
        "(copy_abs(latest(annual.other_income))+"
        "copy_abs(latest(annual.asset_disposal_gain)))/"
        "copy_abs(latest(annual.deducted_net_income))"
    ),
    "capped_growth": "min(deducted_profit_cagr,peg_growth_cap)",
    "peg": "current.pe/(capped_growth*100)",
    "pe_percentile": "percentile_rank(current.pe,monthly.pe)",
    "current_pe": "current.pe",
}


def garp_payload(*, build_version=HISTORICAL_BUILD_VERSION, **threshold_overrides: str) -> dict[str, object]:
    thresholds = {
        "revenue_cagr_min": "0.08",
        "deducted_profit_cagr_min": "0.1",
        "revenue_period_growth_min": "0.05",
        "deducted_profit_period_growth_min": "0.05",
        "revenue_growth_slowdown_max": "0.1",
        "deducted_profit_growth_slowdown_max": "0.1",
        "return_on_capital_min": "10",
        "cash_conversion_min": "1",
        "dilution_rate_max": "0.05",
        "interest_debt_increase_to_revenue_max": "0.2",
        "goodwill_increase_to_revenue_max": "0.1",
        "acquisition_cash_to_revenue_max": "0.1",
        "gross_margin_deterioration_max": "2",
        "net_margin_deterioration_max": "2",
        "impairment_share_max": "0.2",
        "non_core_income_proxy_share_max": "0.2",
        "peg_growth_cap": "0.25",
        "peg_max": "1.5",
        "pe_percentile_max": "70",
        "pe_max": "35",
        "readiness_min": "0.8",
    }
    thresholds.update(threshold_overrides)
    return {
        "scope": "weekly-discovery",
        "track": "garp",
        "fields": FIELDS,
        "formulas": FORMULAS,
        "thresholds": thresholds,
        "min_observations": {
            "revenue_growth": 5,
            "deducted_profit_growth": 5,
            "sustained_revenue_growth": 3,
            "sustained_profit_growth": 3,
            "latest_growth": 2,
            "roic": 3,
            "roe_fallback": 3,
            "cash_conversion": 5,
            "dilution": 2,
            "growth_drivers": 5,
            "margin": 2,
            "quality_adjustments": 1,
            "valuation_pe": 60,
        },
        "missing_policy": {field: "insufficient" for field in FIELDS},
        "sector_branches": {
            "common": {
                "valuation_ratio_method": "PE_TTM/PB_MRQ",
                "field_domain_policy": "garp-domain-v2",
                "annual_observations": {
                    "growth": 5,
                    "return_on_capital": 5,
                    "cash_conversion": 5,
                    "dilution": 5,
                    "growth_drivers": 5,
                    "margin": 5,
                    "quality_adjustments": 1,
                },
                "cagr_periods": {
                    "revenue": 4,
                    "deducted_profit": 4,
                },
                "latest_annual_staleness_years": 1,
            },
            "branch_policies": {
                "bank": "unsupported",
                "insurance": "unsupported",
                "other_financial": "unsupported",
                "nonfinancial": "nonfinancial",
                "unknown": "insufficient",
            },
            "nonfinancial": {
                "gate_order": [
                    "revenue_growth",
                    "deducted_profit_growth",
                    "sustained_growth",
                    "growth_slowdown",
                    "return_on_capital",
                    "cash_conversion",
                    "dilution",
                    "interest_debt_trend",
                    "goodwill_trend",
                    "acquisition_cash_trend",
                    "gross_margin",
                    "net_margin",
                    "impairment",
                    "non_core_income_proxy",
                    "peg",
                    "valuation",
                ],
                "return_metric_policy": "roic_then_roe",
                "evidence_fields": [
                    "revenue_cagr",
                    "deducted_profit_cagr",
                    "return_on_capital",
                    "peg",
                ],
                "excluded_industries": ["贵金属"],
            },
        },
        "order": [
            [
                "deducted_profit_consistent_growth_periods",
                "desc",
                "nulls_last",
            ],
            ["peg", "asc", "nulls_last"],
            ["return_on_capital", "desc", "nulls_last"],
            ["cash_conversion", "desc", "nulls_last"],
            ["code", "asc", "nulls_last"],
        ],
        "industry_version": "test-industry-v1",
        "algorithm_version": build_version,
    }


def _garp_annual(year: int, **changes: str | None) -> dict[str, object]:
    offset = year - 2021
    values = {
        "revenue": str(100 + offset * 15),
        "deducted_net_income": str(20 + offset * 4),
        "net_income": str(20 + offset * 4),
        "ocf": str(25 + offset * 5),
        "shares": "100",
        "roic": "15",
        "roe": "18",
        "interest_debt": "20",
        "goodwill": "5",
        "acquisition_cash_paid": "2",
        "gross_margin": "30",
        "net_margin": "12",
        "impairment_loss": "1",
        "other_income": "1",
        "asset_disposal_gain": "1",
    }
    values.update(changes)
    row = _annual(year)
    row_values = dict(row["values"])
    row_values.update(values)
    row["values"] = row_values
    row["provenance"] = {
        field: ("discovery",)
        for field, value in row_values.items()
        if value is not None
    }
    row["field_sources"] = {
        field: _metadata(field)
        for field, value in row_values.items()
        if value is not None
    }
    return row


def _company(*, industry: str = "制造业", annual=None, valuations=None):
    return frozen_company(
        industry=industry,
        annual=annual
        or tuple(_garp_annual(year) for year in range(2021, 2026)),
        valuations=valuations,
    )


def _classify(company=None, payload=None):
    from discovery.rulers import garp_ruler
    from discovery.tracks.garp import classify

    return classify(
        company or _company(),
        garp_ruler(payload or garp_payload()),
    )


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_garp_ruler_is_semantically_complete_and_strict() -> None:
    from discovery.rulers import garp_ruler

    assert garp_ruler(garp_payload()).track == "garp"
    mutations = (
        lambda value: value["thresholds"].pop("peg_growth_cap"),
        lambda value: value["thresholds"].__setitem__(
            "revenue_period_growth_min", "0"
        ),
        lambda value: value["min_observations"].pop("roic"),
        lambda value: value["min_observations"].__setitem__(
            "growth_drivers", 4
        ),
        lambda value: value["missing_policy"].pop("goodwill_increase_to_revenue"),
        lambda value: value["formulas"].__setitem__("peg", "current.pe/growth"),
        lambda value: value["sector_branches"]["common"]["cagr_periods"].__setitem__(
            "revenue", 3
        ),
        lambda value: value["sector_branches"]["nonfinancial"][
            "gate_order"
        ].remove("valuation"),
        lambda value: value["sector_branches"]["nonfinancial"].__setitem__(
            "return_metric_policy", "hidden_fallback"
        ),
        lambda value: value["order"].pop(1),
        lambda value: value["order"].__setitem__(
            0, ["peg", "asc", "nulls_last"]
        ),
    )
    for mutation in mutations:
        payload = json.loads(json.dumps(garp_payload()))
        mutation(payload)
        with pytest.raises((TypeError, ValueError)):
            garp_ruler(payload)


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_exact_annual_observations_and_cagr_periods_are_audited() -> None:
    verdict = _classify()
    assert verdict.verdict == "pass"
    assert verdict.metrics["growth_window_start_year"] == "2021"
    assert verdict.metrics["growth_window_end_year"] == "2025"
    assert verdict.metrics["revenue_growth_available_years"] == (
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    )
    assert verdict.metrics["revenue_cagr_periods"] == "4"
    assert verdict.metrics["deducted_profit_cagr_periods"] == "4"

    too_short = tuple(_garp_annual(year) for year in range(2022, 2026))
    short = _classify(_company(annual=too_short))
    assert (short.verdict, short.primary_reason) == (
        "insufficient",
        "revenue_growth_insufficient",
    )
    assert short.metrics["revenue_growth_observations"] == "4"


@pytest.mark.parametrize(
    "minimum",
    ["sustained_revenue_growth", "sustained_profit_growth"],
)
@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_sustained_growth_minimum_requires_at_least_two_periods(
    minimum: str,
) -> None:
    from discovery.rulers import garp_ruler

    payload = garp_payload()
    payload["min_observations"][minimum] = 1
    with pytest.raises(
        ValueError,
        match="GARP exact observation policy is invalid",
    ):
        garp_ruler(payload)


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_one_year_low_base_spike_does_not_satisfy_sustained_growth() -> None:
    revenue = {
        2021: "100",
        2022: "100.1",
        2023: "100.2",
        2024: "100.3",
        2025: "200",
    }
    profit = {
        2021: "10",
        2022: "10.01",
        2023: "10.02",
        2024: "10.03",
        2025: "30",
    }
    annual = tuple(
        _garp_annual(
            year,
            revenue=revenue[year],
            deducted_net_income=profit[year],
            net_income=profit[year],
            ocf=profit[year],
        )
        for year in range(2021, 2026)
    )
    payload = garp_payload()
    payload["min_observations"]["sustained_revenue_growth"] = 2
    payload["min_observations"]["sustained_profit_growth"] = 2
    verdict = _classify(_company(annual=annual), payload)
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "revenue_growth_not_sustained",
    )
    assert verdict.metrics["revenue_consistent_growth_periods"] == "1"
    assert verdict.metrics["deducted_profit_consistent_growth_periods"] == "1"


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_latest_growth_slowdown_is_compared_with_multi_year_trend() -> None:
    annual = tuple(
        _garp_annual(
            year,
            revenue={
                2021: "100",
                2022: "130",
                2023: "169",
                2024: "220",
                2025: "225",
            }[year],
            deducted_net_income={
                2021: "20",
                2022: "26",
                2023: "34",
                2024: "44",
                2025: "45",
            }[year],
            net_income={
                2021: "20",
                2022: "26",
                2023: "34",
                2024: "44",
                2025: "45",
            }[year],
            ocf={
                2021: "24",
                2022: "32",
                2023: "41",
                2024: "53",
                2025: "54",
            }[year],
        )
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "revenue_growth_slowdown_above_max",
    )
    assert Decimal(str(verdict.metrics["revenue_growth_slowdown"])) > Decimal(
        "0.1"
    )


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_roic_is_primary_and_roe_fallback_is_explicitly_labeled() -> None:
    from discovery.evidence import build_evidence_card

    primary = _classify()
    assert primary.metrics["return_metric_label"] == "roic"
    assert primary.metrics["return_on_capital"] == "15"

    annual = tuple(
        _garp_annual(year, roic=None, roe="13")
        for year in range(2021, 2026)
    )
    fallback = _classify(_company(annual=annual))
    assert fallback.verdict == "pass"
    assert fallback.metrics["return_metric_label"] == "roe_fallback"
    assert fallback.metrics["return_on_capital"] == "13"
    card = build_evidence_card(
        fallback.code,
        fallback.metrics,
        garp_payload(),
        source_as_of={
            key: _company(annual=annual).as_of[key]
            for key in ("security", "financial", "valuation")
        },
    )
    return_fact = next(
        item
        for item in card.items
        if item.rule_id == "garp.fact.return_on_capital"
    )
    assert "ROE fallback" in return_fact.display_text
    assert return_fact.input_fields["return_metric_label"] == "roe_fallback"
    assert return_fact.input_fields["return_metric_source_field"] == "roe"
    assert return_fact.input_fields["return_metric_policy"] == "roic_then_roe"
    assert return_fact.input_fields["source"] == "financial"

    partial = tuple(
        _garp_annual(year, roic=None if year == 2025 else "15")
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=partial))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "roic_insufficient",
    )
    assert verdict.metrics["return_metric_label"] == "roic"


@pytest.mark.parametrize(
    ("latest", "reason"),
    [
        ({"ocf": "-1"}, "cash_conversion_below_min"),
        ({"shares": "110"}, "dilution_rate_above_max"),
        (
            {"interest_debt": "60"},
            "interest_debt_increase_to_revenue_above_max",
        ),
        ({"goodwill": "25"}, "goodwill_increase_to_revenue_above_max"),
        (
            {"acquisition_cash_paid": "100"},
            "acquisition_cash_to_revenue_above_max",
        ),
        ({"gross_margin": "27"}, "gross_margin_deterioration"),
        ({"net_margin": "9"}, "net_margin_deterioration"),
        ({"impairment_loss": "8"}, "impairment_share_above_max"),
        (
            {"other_income": "4", "asset_disposal_gain": "4"},
            "non_core_income_proxy_share_above_max",
        ),
    ],
)
@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_growth_quality_and_unit_economics_gates(
    latest: dict[str, str],
    reason: str,
) -> None:
    annual = tuple(
        _garp_annual(year, **(latest if year == 2025 else {}))
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == ("drop", reason)


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_acquisition_cash_uses_multi_year_flow_intensity_not_endpoint_delta() -> None:
    annual = tuple(
        _garp_annual(year, acquisition_cash_paid="20")
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "acquisition_cash_to_revenue_above_max",
    )
    intensity = Decimal(str(verdict.metrics["acquisition_cash_to_revenue"]))
    assert Decimal("0.15") < intensity < Decimal("0.16")


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_cash_conversion_minimum_is_inclusive_at_base_band() -> None:
    # net income is 20, 24, 28, 32, 36; OCF is exactly 80% of it.
    def verdict(latest_ocf: str):
        ocf = dict(zip(range(2021, 2025), ("16", "19.2", "22.4", "25.6")))
        ocf[2025] = latest_ocf
        annual = tuple(
            _garp_annual(year, ocf=ocf[year]) for year in range(2021, 2026)
        )
        return _classify(
            _company(annual=annual),
            garp_payload(cash_conversion_min="0.8"),
        )

    assert verdict("28.8").verdict == "pass"
    assert (verdict("28.79").verdict, verdict("28.79").primary_reason) == (
        "drop",
        "cash_conversion_below_min",
    )


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_ttm_earnings_far_above_latest_deducted_profit_is_flagged() -> None:
    # Market value 10 x 100 = 1000; latest deducted profit is 36.
    def flags(latest_pe: str):
        company = _company(valuations=_valuations(latest_pe=latest_pe))
        return _classify(company).risk_flags

    assert "ttm_earnings_above_deducted" not in flags("25")
    assert "ttm_earnings_above_deducted" in flags("15")


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_net_negative_acquisition_cash_counts_as_zero_not_invalid() -> None:
    annual = tuple(
        _garp_annual(year, acquisition_cash_paid="-50" if year == 2023 else "0")
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert verdict.verdict == "pass"
    assert Decimal(str(verdict.metrics["acquisition_cash_to_revenue"])) == 0


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_excluded_cyclical_industry_is_unsupported_and_labels_are_strict() -> None:
    from discovery.rulers import garp_ruler

    verdict = _classify(_company(industry="贵金属"))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "industry_cyclical_unsupported",
    )
    for labels in (["工业金属", "贵金属"][::-1], ["贵金属", "贵金属"], [" 贵金属"], "贵金属"):
        payload = json.loads(json.dumps(garp_payload()))
        payload["sector_branches"]["nonfinancial"]["excluded_industries"] = labels
        with pytest.raises((TypeError, ValueError)):
            garp_ruler(payload)
    v3 = json.loads(json.dumps(garp_payload()))
    v3["formulas"]["acquisition_cash_to_revenue"] = (
        "sum(annual.acquisition_cash_paid)/sum(annual.revenue)"
    )
    with pytest.raises(ValueError):
        garp_ruler(v3)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("roic", "roic_insufficient"),
        ("ocf", "cash_conversion_insufficient"),
        ("interest_debt", "growth_drivers_insufficient"),
        ("goodwill", "growth_drivers_insufficient"),
        ("acquisition_cash_paid", "growth_drivers_insufficient"),
        ("gross_margin", "margin_insufficient"),
        ("net_margin", "margin_insufficient"),
        ("impairment_loss", "quality_adjustments_insufficient"),
        ("other_income", "quality_adjustments_insufficient"),
        ("asset_disposal_gain", "quality_adjustments_insufficient"),
    ],
)
@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_every_gating_unavailable_input_is_insufficient(
    field: str,
    reason: str,
) -> None:
    annual = tuple(
        _garp_annual(year, **({field: None} if year == 2025 else {}))
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        reason,
    )


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_trend_windows_require_exact_configured_endpoints() -> None:
    annual = tuple(
        _garp_annual(year, goodwill=None if year == 2021 else "5")
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "growth_drivers_insufficient",
    )
    assert verdict.metrics["growth_drivers_window_start_year"] == "2021"
    assert verdict.metrics["goodwill_increase_to_revenue"] is None


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_growth_is_capped_for_peg_but_valuation_still_has_independent_gates() -> None:
    fast = tuple(
        _garp_annual(
            year,
            revenue=str(100 * (2 ** (year - 2021))),
            deducted_net_income=str(20 * (2 ** (year - 2021))),
            net_income=str(20 * (2 ** (year - 2021))),
            ocf=str(25 * (2 ** (year - 2021))),
        )
        for year in range(2021, 2026)
    )
    low_peg = _classify(
        _company(
            annual=fast,
            valuations=_valuations(latest_pe="30", latest_pb="0.8"),
        ),
        garp_payload(pe_percentile_max="100"),
    )
    assert low_peg.metrics["capped_growth"] == "0.25"
    assert low_peg.metrics["peg"] == "1.2"
    assert (low_peg.verdict, low_peg.primary_reason) == (
        "pass",
        None,
    )

    ceiling = _classify(
        _company(
            annual=fast,
            valuations=_valuations(latest_pe="40", latest_pb="0.8"),
        ),
        garp_payload(peg_max="2", pe_percentile_max="100"),
    )
    assert Decimal(str(ceiling.metrics["peg"])) < Decimal("2")
    assert ceiling.primary_reason == "current_pe_above_max"

    percentile = _classify(
        _company(
            annual=fast,
            valuations=_valuations(latest_pe="19", latest_pb="0.8"),
        ),
        garp_payload(pe_max="100"),
    )
    assert Decimal(str(percentile.metrics["peg"])) < Decimal("1.5")
    assert percentile.primary_reason == "valuation_pe_percentile_above_max"


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_exact_staleness_domain_and_ruler_order_are_enforced() -> None:
    stale = tuple(_garp_annual(year) for year in range(2019, 2024))
    assert _classify(_company(annual=stale)).primary_reason == (
        "financial_history_stale"
    )

    invalid = tuple(
        _garp_annual(year, goodwill="-1" if year == 2025 else "5")
        for year in range(2021, 2026)
    )
    assert _classify(_company(annual=invalid)).primary_reason == (
        "goodwill_negative"
    )

    failures = tuple(
        _garp_annual(
            year,
            revenue="100",
            deducted_net_income="10",
            net_income="10",
            ocf="1" if year == 2025 else "12",
        )
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=failures))
    assert verdict.primary_reason == "revenue_cagr_below_min"
    assert verdict.evidence_rule_ids[:3] == (
        "garp.revenue_cagr_below_min",
        "garp.deducted_profit_cagr_below_min",
        "garp.revenue_growth_not_sustained",
    )
    assert verdict.metrics["cash_conversion"] is not None


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_garp_decimal_arithmetic_is_bounded_and_context_isolated() -> None:
    saved = getcontext().copy()
    saved_default = DefaultContext.copy()
    hostile = saved.copy()
    hostile.prec = 2
    hostile.Emax = 9
    hostile.Emin = -9
    hostile.clear_flags()
    expected_flags = hostile.flags.copy()
    try:
        setcontext(hostile)
        DefaultContext.prec = 3
        verdict = _classify()
        assert verdict.verdict == "pass"
        assert getcontext().flags == expected_flags
    finally:
        setcontext(saved)
        DefaultContext.prec = saved_default.prec
        DefaultContext.rounding = saved_default.rounding
        DefaultContext.Emin = saved_default.Emin
        DefaultContext.Emax = saved_default.Emax
        DefaultContext.capitals = saved_default.capitals
        DefaultContext.clamp = saved_default.clamp
        DefaultContext.traps = saved_default.traps.copy()
        DefaultContext.flags = saved_default.flags.copy()


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_same_company_has_cross_track_divergent_verdicts() -> None:
    from tests.test_discovery_tracks_deep_value import (
        _classify as deep_value_classify,
        deep_value_payload,
    )
    from tests.test_discovery_tracks_quality import (
        _classify as quality_classify,
    )

    company = _company()
    assert quality_classify(company).verdict == "pass"
    assert deep_value_classify(
        company, deep_value_payload(normalized_pe_max="8")
    ).verdict == "drop"
    assert _classify(company).verdict == "pass"


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_builtin_dispatches_garp_and_generic_evidence_in_order() -> None:
    from jobs.discovery import (
        TrackExecutionCandidate,
        TrackExecutionContext,
        builtin_track_executor,
    )

    company = _company(
        annual=tuple(
            _garp_annual(year, roic=None, roe="13")
            for year in range(2021, 2026)
        )
    )
    frozen = FrozenRunInputs(
        run_id=1,
        input_fingerprint="run",
        data_version="data",
        financial_version="financial",
        valuation_version="valuation",
        ruler_version_ids=MappingProxyType(
            {"quality_discount": None, "deep_value": None, "garp": 3}
        ),
        ruler_payloads=MappingProxyType({}),
        algorithm_version=HISTORICAL_BUILD_VERSION,
        trading_dates=MappingProxyType({"SH": ("2026-09-17",)}),
        snapshot_at=company.snapshot_at,
        companies=(company,),
    )
    candidate = TrackExecutionCandidate(
        code=company.code,
        persisted_verdict="pass",
        effective_verdict="pass",
        reason=None,
        metrics_json="{}",
        rank=None,
    )
    gate = TrackExecutionContext(
        stage_id=1,
        run_id=1,
        track="garp",
        stage="track_gate",
        attempt=1,
        input_fingerprint="run",
        ruler_version_id=3,
        algorithm_version=HISTORICAL_BUILD_VERSION,
        common_stage_id=1,
        predecessor_stage_id=None,
        predecessor_stage=None,
        frozen_inputs=frozen,
        ruler_payload=MappingProxyType(garp_payload()),
        candidates=(candidate,),
    )
    outcome = builtin_track_executor(gate)
    assert outcome.status == "waiting_confirmation"
    assert outcome.results[0].verdict == "pass"

    competition = replace(
        gate,
        stage="competition",
        common_stage_id=None,
        predecessor_stage_id=1,
        predecessor_stage="track_gate",
        candidates=(
            replace(candidate, metrics_json=outcome.results[0].metrics_json),
        ),
    )
    ranked = builtin_track_executor(competition)
    assert ranked.results[0].rank == 1
    facts = [row for row in ranked.evidence if row.kind == "fact"]
    assert [row.rule_id for row in facts] == [
        "garp.fact.revenue_cagr",
        "garp.fact.deducted_profit_cagr",
        "garp.fact.return_on_capital",
        "garp.fact.peg",
    ]
    fallback = next(
        row
        for row in facts
        if row.rule_id == "garp.fact.return_on_capital"
    )
    assert "ROE fallback" in fallback.display_text
    assert json.loads(fallback.input_fields_json)["return_metric_label"] == (
        "roe_fallback"
    )
    assert all(row.rule_id.startswith("garp.") for row in ranked.evidence)


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_garp_competition_order_is_growth_consistency_then_peg() -> None:
    from discovery.competition import CompetitionCandidate, rank_competition
    from discovery.rulers import garp_ruler

    def candidate(code: str, periods: str, peg: str, roc: str, cash: str):
        return CompetitionCandidate(
            code,
            "制造业",
            {
                "deducted_profit_consistent_growth_periods": periods,
                "peg": peg,
                "return_on_capital": roc,
                "cash_conversion": cash,
            },
        )

    ruler = garp_ruler(garp_payload())
    ranked = rank_competition(
        (
            candidate("600001.SH", "3", "0.5", "30", "2"),
            candidate("600002.SH", "4", "1.4", "10", "0.8"),
            candidate("600003.SH", "4", "0.9", "10", "0.8"),
        ),
        ruler.order,
    )
    assert [row.code for row in ranked] == ["600003.SH", "600002.SH", "600001.SH"]
    assert ruler.order == (
        (
            "deducted_profit_consistent_growth_periods",
            "desc",
            "nulls_last",
        ),
        ("peg", "asc", "nulls_last"),
        ("return_on_capital", "desc", "nulls_last"),
        ("cash_conversion", "desc", "nulls_last"),
        ("code", "asc", "nulls_last"),
    )


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_growth_driver_trend_emits_generic_deterministic_risk() -> None:
    from discovery.evidence import build_evidence_card

    annual = tuple(
        _garp_annual(
            year,
            revenue=str(100 * (2 ** (year - 2021))),
            deducted_net_income=str(20 * (2 ** (year - 2021))),
            net_income=str(20 * (2 ** (year - 2021))),
            ocf=str(25 * (2 ** (year - 2021))),
            goodwill="10" if year == 2025 else "5",
        )
        for year in range(2021, 2026)
    )
    company = _company(annual=annual)
    verdict = _classify(company)
    assert verdict.verdict == "pass"
    card = build_evidence_card(
        company.code,
        verdict.metrics,
        garp_payload(),
        source_as_of={
            key: company.as_of[key]
            for key in ("security", "financial", "valuation")
        },
    )
    risk = next(item for item in card.items if item.kind == "risk")
    assert (
        risk.rule_id,
        risk.category,
    ) == (
        "garp.risk.worsening_trend.goodwill_increase_to_revenue_worsening",
        "worsening_trend",
    )


def _garp_calibration_snapshot(company):
    from discovery.snapshot import (
        CompanySnapshotInput,
        ProductionSnapshot,
        SnapshotVersions,
    )

    return ProductionSnapshot(
        fill_id=7,
        companies=(
            CompanySnapshotInput(
                code=company.code,
                name=company.name,
                industry=company.industry,
                security_status=company.security_status,
                fields=company.fields,
                sources=company.sources,
                as_of=company.as_of,
            ),
        ),
        versions=SnapshotVersions(
            data_version="data",
            financial_version="financial",
            valuation_version="valuation",
            ruler_version_ids=MappingProxyType(
                {"quality_discount": None, "deep_value": None, "garp": None}
            ),
            algorithm_version=HISTORICAL_BUILD_VERSION,
        ),
        trading_dates=MappingProxyType({"SH": ("2026-09-15",)}),
        snapshot_at=company.snapshot_at,
    )


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_calibrator_garp_approves_declared_roe_fallback_separately(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import calibrate_discovery

    db = tmp_path / "production-contract.db"
    _calibration_preflight_db(db)
    company = _company(
        annual=tuple(
            _garp_annual(year, roic=None, roe="13")
            for year in range(2021, 2026)
        )
    )
    snapshot = _garp_calibration_snapshot(company)
    monkeypatch.setattr(
        calibrate_discovery,
        "build_production_snapshot",
        lambda _session: snapshot,
    )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    first = calibrate_discovery.calibrate(db, track="garp")
    second = calibrate_discovery.calibrate(db, track="garp")
    assert first == second
    assert first["track"] == "garp"
    assert first["status"] == "ready"
    assert first["approval_ready"] is True
    assert first["proposal"] == first["hypotheses"]["base"]
    assert first["proposal"]["sector_branches"]["nonfinancial"][
        "return_metric_policy"
    ] == "roic_then_roe"
    assert first["return_metric_coverage"] == {
        "policy": "roic_then_roe",
        "selected_labels": {
            "roic": {"count": 0, "total": 1, "ratio": "0"},
            "roe_fallback": {"count": 1, "total": 1, "ratio": "1"},
            "insufficient": {"count": 0, "total": 1, "ratio": "0"},
        },
        "roic": {
            "companies": 0,
            "observations": 0,
            "total": 1,
            "ratio": "0",
        },
        "roe_fallback": {
            "companies": 1,
            "observations": 5,
            "total": 1,
            "ratio": "1",
        },
    }
    assert first["field_coverage"]["return_on_capital"]["ratio"] == "1"
    assert first["candidate_counts"]["base"]["pass"] == 1
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("roic", ["0", "-1"])
@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_calibrator_garp_counts_finite_signed_roic_as_provenance(
    tmp_path: Path,
    monkeypatch,
    roic: str,
) -> None:
    from scripts import calibrate_discovery

    db = tmp_path / "invalid-return.db"
    _calibration_preflight_db(db)
    company = _company(
        annual=tuple(
            _garp_annual(year, roic=roic, roe="13")
            for year in range(2021, 2026)
        )
    )
    monkeypatch.setattr(
        calibrate_discovery,
        "build_production_snapshot",
        lambda _session: _garp_calibration_snapshot(company),
    )
    artifact = calibrate_discovery.calibrate(db, track="garp")
    assert artifact["status"] == "ready"
    assert artifact["approval_ready"] is True
    assert artifact["candidate_counts"]["base"]["drop"] == 1
    assert artifact["return_metric_coverage"]["selected_labels"]["roic"] == {
        "count": 1,
        "total": 1,
        "ratio": "1",
    }
    assert artifact["return_metric_coverage"]["roic"]["companies"] == 1
    assert artifact["return_metric_coverage"]["roic"]["observations"] == 5
    assert artifact["return_metric_coverage"]["roe_fallback"][
        "observations"
    ] == 0
    assert "return_metric_label" not in artifact["field_coverage"]


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_calibrator_reports_exact_supported_and_unsupported_branches() -> None:
    from scripts.calibrate_discovery import _ruler_branch

    assert _ruler_branch("garp", "制造业") == "nonfinancial"
    assert _ruler_branch("garp", "银行") == "unsupported_bank"
    assert _ruler_branch("garp", "保险") == "unsupported_insurance"
    assert _ruler_branch("garp", "证券") == "unsupported_other_financial"
    assert _ruler_branch("garp", "-") == "unknown"
