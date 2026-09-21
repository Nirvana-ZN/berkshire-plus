from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from decimal import (
    DefaultContext,
    Decimal,
    Inexact,
    ROUND_DOWN,
    getcontext,
    setcontext,
)
from pathlib import Path
from types import MappingProxyType

import pytest

from discovery.builds import BUILD_VERSION
from discovery.snapshot import FrozenCompanyInput, canonical_json
from market.types import FinancialRow


SNAPSHOT_AT = datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
FINANCIAL_FIELDS = tuple(
    field.name
    for field in fields(FinancialRow)
    if field.name not in {"code", "period", "period_type", "source_as_of"}
)
METRIC_FIELDS = [
    "listing_years",
    "roe_mean",
    "roe_cv",
    "positive_net_income_observations",
    "latest_net_income",
    "latest_net_assets",
    "cash_conversion",
    "dilution_rate",
    "debt_to_equity",
    "interest_coverage",
    "margin_change",
    "pe_percentile",
    "pe_median_ratio",
    "pb_percentile",
    "pb_median_ratio",
    "normalized_earnings",
    "normalized_pe",
    "bank_capital_adequacy",
    "bank_core_tier1_capital_adequacy",
    "bank_npl",
    "code",
]
FORMULAS = {
    "roe_mean": "mean(annual.roe)",
    "roe_cv": "coefficient_of_variation(annual.roe)",
    "positive_net_income_observations": "count(annual.net_income>0)",
    "latest_net_income": "latest(annual.net_income)",
    "latest_net_assets": "latest(annual.net_assets)",
    "cash_conversion": "cash_conversion(annual.ocf,annual.net_income>0)",
    "dilution_rate": "latest(annual.shares)/earliest(annual.shares)-1",
    "debt_to_equity": "latest(annual.interest_debt)/latest(annual.net_assets)",
    "interest_coverage": "latest(annual.interest_coverage)",
    "margin_change": "latest(annual.gross_margin)-earliest(annual.gross_margin)",
    "pe_percentile": "percentile_rank(current.pe,monthly.pe)",
    "pe_median_ratio": "current.pe/median(monthly.pe)",
    "pb_percentile": "percentile_rank(current.pb,monthly.pb)",
    "pb_median_ratio": "current.pb/median(monthly.pb)",
    "normalized_earnings": "normalized_earnings(annual.deducted_net_income)",
    "normalized_pe": "current.price*latest(annual.shares)/normalized_earnings",
    "bank_capital_adequacy": "latest(annual.capital_adequacy)",
    "bank_core_tier1_capital_adequacy": (
        "latest(annual.core_tier1_capital_adequacy)"
    ),
    "bank_npl": "latest(annual.npl)",
}


def quality_payload(**threshold_overrides: str) -> dict[str, object]:
    thresholds = {
        "listing_years_min": "5",
        "roe_mean_min": "12",
        "roe_cv_max": "0.25",
        "net_income_positive_min": "0",
        "net_assets_positive_min": "0",
        "cash_conversion_min": "1",
        "dilution_rate_max": "0.05",
        "debt_to_equity_max": "1",
        "interest_coverage_min": "3",
        "margin_deterioration_max": "2",
        "pe_percentile_max": "30",
        "pe_median_ratio_max": "0.8",
        "pb_percentile_max": "30",
        "pb_median_ratio_max": "0.8",
        "normalized_pe_max": "15",
        "normalized_earnings_min": "0",
        "bank_roe_min": "10",
        "bank_capital_adequacy_min": "12",
        "bank_core_tier1_min": "9",
        "bank_npl_max": "2",
        "readiness_min": "0.8",
    }
    thresholds.update(threshold_overrides)
    return {
        "scope": "weekly-discovery",
        "track": "quality_discount",
        "fields": METRIC_FIELDS,
        "formulas": FORMULAS,
        "thresholds": thresholds,
        "min_observations": {
            "roe": 5,
            "positive_net_income": 5,
            "net_assets": 1,
            "cash_conversion": 5,
            "dilution": 2,
            "leverage": 1,
            "interest_coverage": 1,
            "margin": 2,
            "valuation_pe": 60,
            "valuation_pb": 60,
            "normalized_earnings": 5,
            "bank_roe": 5,
            "bank_capital": 1,
            "bank_npl": 1,
        },
        "missing_policy": {
            field: "insufficient" for field in METRIC_FIELDS
        },
        "sector_branches": {
            "common": {
                "valuation_ratio_method": "PE_TTM/PB_MRQ",
                "field_domain_policy": "quality-domain-v1",
                "lookback_windows": {
                    "roe": 10,
                    "profitability": 5,
                    "cash_conversion": 5,
                    "dilution": 5,
                    "positive_equity": 1,
                    "leverage": 1,
                    "interest_coverage": 1,
                    "margin": 5,
                    "normalized_earnings": 5,
                    "bank_capital": 5,
                    "bank_npl": 5,
                },
                "latest_annual_staleness_years": 1,
            },
            "branch_policies": {
                "bank": "bank",
                "insurance": "unsupported",
                "other_financial": "unsupported",
                "nonfinancial": "industry_map",
                "unknown": "insufficient",
            },
            "industry_map": {
                "nonfinancial": ["制造业"],
                "cyclical": ["煤炭"],
                "bank": ["银行"],
            },
            "nonfinancial": {
                "gate_order": [
                    "listing_age",
                    "roe_mean",
                    "roe_stability",
                    "profitability",
                    "positive_equity",
                    "cash_conversion",
                    "dilution",
                    "leverage",
                    "interest_coverage",
                    "margin",
                    "valuation",
                ],
                "valuation_policy": "both",
                "margin_deterioration_policy": "evidence",
                "latest_net_income_policy": "require_positive",
            },
            "cyclical": {
                "gate_order": [
                    "listing_age",
                    "roe_mean",
                    "roe_stability",
                    "profitability",
                    "positive_equity",
                    "cash_conversion",
                    "dilution",
                    "leverage",
                    "interest_coverage",
                    "margin",
                    "normalized_earnings_positive",
                    "cyclical_valuation",
                ],
                "valuation_policy": "both",
                "margin_deterioration_policy": "evidence",
                "latest_net_income_policy": "require_positive",
            },
            "bank": {
                "gate_order": [
                    "listing_age",
                    "positive_equity",
                    "bank_roe",
                    "bank_capital",
                    "bank_npl",
                    "bank_valuation",
                ],
                "valuation_policy": "pb",
                "margin_deterioration_policy": "evidence",
                "latest_net_income_policy": "ignore",
            },
        },
        "order": [
            ["pe_percentile", "asc", "nulls_last"],
            ["roe_cv", "asc", "nulls_last"],
            ["cash_conversion", "desc", "nulls_last"],
            ["code", "asc", "nulls_last"],
        ],
        "industry_version": "test-industry-v1",
        "algorithm_version": BUILD_VERSION,
    }


def _metadata(field: str) -> dict[str, object]:
    return {
        "source": "discovery",
        "report": "test",
        "field": field,
        "unit": "test",
        "consolidated_basis": "consolidated",
        "period_type": "annual",
        "financial_sector_substitute": None,
        "source_as_of": "NOTICE_DATE/UPDATE_DATE",
        "trusted": True,
    }


def _annual(
    year: int,
    *,
    roe: str = "15",
    net_income: str = "100",
    ocf: str = "120",
    shares: str = "100",
    cash: str | None = None,
    short_loan: str | None = None,
    long_loan: str | None = None,
    bond_payable: str | None = None,
    noncurrent_liab_1year: str | None = None,
    interest_debt: str = "40",
    net_assets: str = "100",
    interest_coverage: str = "8",
    gross_margin: str = "30",
    deducted_net_income: str = "100",
    capital_adequacy: str = "14",
    core_tier1_capital_adequacy: str = "10",
    npl: str = "1",
    nonperforming_loan_balance: str | None = None,
) -> dict[str, object]:
    supplied = {
        "roe": roe,
        "net_income": net_income,
        "ocf": ocf,
        "shares": shares,
        "cash": cash,
        "short_loan": short_loan,
        "long_loan": long_loan,
        "bond_payable": bond_payable,
        "noncurrent_liab_1year": noncurrent_liab_1year,
        "interest_debt": interest_debt,
        "net_assets": net_assets,
        "interest_coverage": interest_coverage,
        "gross_margin": gross_margin,
        "deducted_net_income": deducted_net_income,
        "capital_adequacy": capital_adequacy,
        "core_tier1_capital_adequacy": core_tier1_capital_adequacy,
        "npl": npl,
        "nonperforming_loan_balance": nonperforming_loan_balance,
    }
    values = {field: supplied.get(field) for field in FINANCIAL_FIELDS}
    return {
        "period": str(year),
        "period_type": "annual",
        "source_as_of": f"{year + 1}-04-01",
        "values": values,
        "provenance": {
            field: ("discovery",)
            for field, value in values.items()
            if value is not None
        },
        "field_sources": {
            field: _metadata(field)
            for field, value in values.items()
            if value is not None
        },
    }


def _valuations(count: int = 60, *, latest_pe: str = "8", latest_pb: str = "0.8"):
    rows = []
    year, month = 2021, 10
    for index in range(count):
        pe = str(10 + index % 10)
        pb = format(Decimal(10 + index % 10) / Decimal(10), "f").rstrip("0").rstrip(".")
        if index == count - 1:
            pe, pb = latest_pe, latest_pb
        rows.append(
            {
                "code": "600000.SH",
                "month": f"{year:04d}-{month:02d}",
                "trade_date": f"{year:04d}-{month:02d}-15",
                "pe": pe,
                "pb": pb,
                "price": "10",
                "source": "eastmoney:RPT_VALUEANALYSIS_DET",
                "ratio_method": "PE_TTM/PB_MRQ",
                "fetched_at": "2026-09-17T03:00:00Z",
            }
        )
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return tuple(rows)


def frozen_company(
    *,
    industry: str = "制造业",
    annual: tuple[dict[str, object], ...] | None = None,
    valuations: tuple[dict[str, object], ...] | None = None,
) -> FrozenCompanyInput:
    annual = annual or tuple(_annual(year) for year in range(2021, 2026))
    valuations = valuations or _valuations()
    latest = valuations[-1]
    return FrozenCompanyInput(
        code="600000.SH",
        name="测试公司",
        industry=industry,
        security_status=MappingProxyType(
            {
                "exchange": "SH",
                "listing_date": "2000-01-01",
                "is_st": False,
                "is_delisting": False,
                "trading_status": "trading",
                "is_suspended": False,
                "last_trade_date": "2026-09-17",
                "industry_version": "test-industry-v1",
                "industry_as_of": "2026-09-17",
                "security_source": "exchange",
                "market_status_code": 1,
                "snapshot_at": "2026-09-17T03:00:00Z",
            }
        ),
        fields=MappingProxyType(
            {
                "price": latest["price"],
                "pe": latest["pe"],
                "pb": latest["pb"],
                "listing_date": "2000-01-01",
                "financial_history": annual,
                "valuation_history": valuations,
            }
        ),
        sources=MappingProxyType(
            {
                "price": "eastmoney:RPT_VALUEANALYSIS_DET",
                "security": "exchange",
                "financial_history": "checkpoint_payload:field_sources",
                "valuation_history": ("eastmoney:RPT_VALUEANALYSIS_DET",),
            }
        ),
        as_of=MappingProxyType(
            {
                "price": latest["trade_date"],
                "security": "2026-09-17",
                "financial": "2026-04-01",
                "valuation": latest["trade_date"],
                "valuation_fetched_at": "2026-09-17T03:00:00Z",
                "fill": "2026-09-17T03:00:00Z",
            }
        ),
        data_version="data-v1",
        financial_version="financial-v1",
        valuation_version="valuation-v1",
        ruler_version_ids=MappingProxyType(
            {"quality_discount": 1, "deep_value": None, "garp": None}
        ),
        algorithm_version=BUILD_VERSION,
        input_fingerprint="fingerprint",
        snapshot_at=SNAPSHOT_AT,
    )


def _classify(company=None, payload=None):
    from discovery.rulers import quality_discount_ruler
    from discovery.tracks.quality_discount import classify

    return classify(
        company or frozen_company(),
        quality_discount_ruler(payload or quality_payload()),
    )


def test_quality_ruler_has_strict_semantic_schema() -> None:
    from discovery.rulers import quality_discount_ruler

    ruler = quality_discount_ruler(quality_payload())
    assert ruler.track == "quality_discount"
    assert ruler.order == (
        ("pe_percentile", "asc", "nulls_last"),
        ("roe_cv", "asc", "nulls_last"),
        ("cash_conversion", "desc", "nulls_last"),
        ("code", "asc", "nulls_last"),
    )
    for mutation in (
        lambda value: value["thresholds"].pop("roe_mean_min"),
        lambda value: value["thresholds"].pop("net_income_positive_min"),
        lambda value: value["thresholds"].__setitem__("hidden_fallback", "1"),
        lambda value: value["min_observations"].pop("positive_net_income"),
        lambda value: value["missing_policy"].pop("latest_net_assets"),
        lambda value: value["sector_branches"]["bank"].pop("gate_order"),
        lambda value: value["sector_branches"][
            "branch_policies"
        ].pop("insurance"),
        lambda value: value["sector_branches"]["common"].__setitem__(
            "field_domain_policy", "implicit"
        ),
        lambda value: value["sector_branches"]["common"][
            "lookback_windows"
        ].pop("roe"),
        lambda value: value["sector_branches"]["common"][
            "lookback_windows"
        ].__setitem__("cash_conversion", 0),
        lambda value: value["sector_branches"]["common"][
            "lookback_windows"
        ].__setitem__("roe", 129),
        lambda value: value["sector_branches"]["common"].pop(
            "latest_annual_staleness_years"
        ),
        lambda value: value["sector_branches"]["nonfinancial"].__setitem__(
            "latest_net_income_policy", "implicit"
        ),
        lambda value: value["sector_branches"]["nonfinancial"][
            "gate_order"
        ].remove("profitability"),
        lambda value: value["sector_branches"]["nonfinancial"][
            "gate_order"
        ].reverse(),
        lambda value: value["formulas"].__setitem__("roe_mean", "vendor_roe"),
        lambda value: value["order"].pop(2),
    ):
        value = json.loads(json.dumps(quality_payload()))
        mutation(value)
        with pytest.raises((TypeError, ValueError)):
            quality_discount_ruler(value)


def test_less_than_required_valuation_months_is_insufficient() -> None:
    verdict = _classify(frozen_company(valuations=_valuations(59)))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "valuation_pe_percentile_insufficient",
    )
    assert verdict.metrics["valuation_pe_observations"] == "59"


def test_gate_order_selects_one_primary_reason_and_keeps_all_evidence() -> None:
    annual = tuple(
        _annual(year, roe="8", ocf="50", net_income="100")
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert verdict.verdict == "drop"
    assert verdict.primary_reason == "roe_mean_below_min"
    assert verdict.evidence_rule_ids[:2] == (
        "quality.roe_mean_below_min",
        "quality.cash_conversion_below_min",
    )
    assert verdict.metrics["roe_mean"] == "8"
    assert verdict.metrics["cash_conversion"] == "0.5"


def test_persistent_losses_drop_before_cash_conversion() -> None:
    annual = tuple(
        _annual(
            year,
            net_income="-100",
            ocf="-120",
            net_assets="-100" if year == 2025 else "100",
        )
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "positive_net_income_observations_below_min",
    )
    assert verdict.metrics["positive_net_income_observations"] == "0"
    assert verdict.metrics["cash_conversion"] is None
    assert verdict.metrics["debt_to_equity"] is None
    assert verdict.evidence_rule_ids[:2] == (
        "quality.positive_net_income_observations_below_min",
        "quality.latest_net_income_not_positive",
    )


def test_cash_conversion_uses_only_strictly_positive_income_periods() -> None:
    annual = tuple(
        _annual(
            year,
            net_income=income,
            ocf=ocf,
        )
        for year, income, ocf in zip(
            range(2021, 2026),
            ("-100", "100", "-100", "100", "100"),
            ("-1000", "120", "-1000", "120", "120"),
            strict=True,
        )
    )
    payload = quality_payload()
    payload["min_observations"]["positive_net_income"] = 3
    payload["min_observations"]["cash_conversion"] = 3
    verdict = _classify(frozen_company(annual=annual), payload)
    assert verdict.verdict == "pass"
    assert verdict.metrics["positive_net_income_observations"] == "3"
    assert verdict.metrics["cash_conversion_observations"] == "3"
    assert verdict.metrics["cash_conversion"] == "1.2"


def test_latest_loss_policy_is_explicit_and_ordered_before_cash() -> None:
    annual = tuple(
        _annual(
            year,
            net_income="-100" if year == 2025 else "100",
            ocf="-120" if year == 2025 else "120",
        )
        for year in range(2021, 2026)
    )
    payload = quality_payload()
    payload["min_observations"]["positive_net_income"] = 4
    payload["min_observations"]["cash_conversion"] = 4
    required = _classify(frozen_company(annual=annual), payload)
    assert (required.verdict, required.primary_reason) == (
        "drop",
        "latest_net_income_not_positive",
    )
    assert required.metrics["cash_conversion"] == "1.2"

    payload["sector_branches"]["nonfinancial"][
        "latest_net_income_policy"
    ] = "ignore"
    assert _classify(frozen_company(annual=annual), payload).verdict == "pass"


@pytest.mark.parametrize("equity", ["0", "-100"])
def test_nonpositive_equity_drops_without_division(equity: str) -> None:
    annual = tuple(
        _annual(year, net_assets=equity if year == 2025 else "100")
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "latest_net_assets_not_positive",
    )
    assert verdict.metrics["latest_net_assets"] == equity
    assert verdict.metrics["debt_to_equity"] is None


def test_missing_equity_uses_exact_policy() -> None:
    annual = tuple(
        _annual(year, net_assets=None)  # type: ignore[arg-type]
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "latest_net_assets_insufficient",
    )


def test_bank_nonpositive_equity_drops_before_bank_quality_logic() -> None:
    annual = tuple(
        _annual(year, net_assets="-1")
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(industry="银行", annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "latest_net_assets_not_positive",
    )
    assert verdict.evidence_rule_ids[0] == (
        "quality.latest_net_assets_not_positive"
    )


@pytest.mark.parametrize("shares", ["0", "-1"])
def test_nonpositive_shares_are_invalid_not_arithmetic(shares: str) -> None:
    annual = tuple(
        _annual(year, shares=shares if year == 2023 else "100")
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "shares_not_positive",
    )
    assert verdict.evidence_rule_ids == ("quality.shares_not_positive",)
    assert verdict.metrics["dilution_rate"] is None


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("interest_debt", "interest_debt_negative"),
        ("cash", "cash_negative"),
        ("short_loan", "short_loan_negative"),
        ("long_loan", "long_loan_negative"),
        ("bond_payable", "bond_payable_negative"),
        ("noncurrent_liab_1year", "noncurrent_liab_1year_negative"),
    ],
)
def test_negative_accounting_balances_are_invalid(
    field: str,
    reason: str,
) -> None:
    annual = tuple(
        _annual(year, **{field: "-1"})  # type: ignore[arg-type]
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        reason,
    )
    assert verdict.evidence_rule_ids == (f"quality.{reason}",)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("npl", "bank_npl_negative"),
        (
            "nonperforming_loan_balance",
            "nonperforming_loan_balance_negative",
        ),
    ],
)
def test_negative_bank_npl_values_are_invalid(
    field: str,
    reason: str,
) -> None:
    annual = tuple(
        _annual(year, **{field: "-1"})  # type: ignore[arg-type]
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(industry="银行", annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        reason,
    )
    assert verdict.evidence_rule_ids == (f"quality.{reason}",)


@pytest.mark.parametrize("price", ["0", "-10"])
def test_nonpositive_price_is_invalid(price: str) -> None:
    valuations = list(_valuations())
    valuations[-1] = dict(valuations[-1], price=price)
    verdict = _classify(
        frozen_company(valuations=tuple(valuations))
    )
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "price_not_positive",
    )
    assert verdict.evidence_rule_ids == ("quality.price_not_positive",)


@pytest.mark.parametrize(("ratio", "value"), [("pe", "-10"), ("pb", "0")])
def test_nonpositive_valuation_ratios_are_unusable_not_false_passes(
    ratio: str,
    value: str,
) -> None:
    valuations = list(_valuations())
    valuations[0] = dict(valuations[0], **{ratio: value})
    verdict = _classify(frozen_company(valuations=tuple(valuations)))
    assert verdict.verdict == "insufficient"
    assert verdict.primary_reason == f"valuation_{ratio}_percentile_insufficient"
    assert verdict.metrics[f"valuation_{ratio}_observations"] == "59"


def test_signed_economic_fields_remain_valid_domains() -> None:
    annual = tuple(
        _annual(
            year,
            ocf="-120",
            interest_coverage="-2",
            gross_margin="-30",
        )
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "cash_conversion_below_min",
    )
    assert verdict.metrics["cash_conversion"] == "-1.2"
    assert verdict.metrics["interest_coverage"] == "-2"
    assert verdict.primary_reason != "financial_history_invalid"


def test_negative_capital_adequacy_is_valid_ratio_and_drops() -> None:
    annual = tuple(
        _annual(
            year,
            capital_adequacy="-1",
            core_tier1_capital_adequacy="-2",
        )
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(industry="银行", annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "bank_capital_adequacy_below_min",
    )
    assert verdict.metrics["bank_capital_adequacy"] == "-1"


def test_roe_level_and_stability_are_separate_ordered_gates() -> None:
    annual = tuple(
        _annual(year, roe=value)
        for year, value in zip(
            range(2021, 2026), ("5", "25", "5", "25", "5"), strict=True
        )
    )
    verdict = _classify(frozen_company(annual=annual))
    assert verdict.primary_reason == "roe_stability_above_max"
    assert verdict.metrics["roe_mean"] == "13"
    assert verdict.metrics["roe_cv"] is not None


def test_margin_risk_is_evidence_only_unless_branch_policy_drops() -> None:
    annual = tuple(
        _annual(year, gross_margin=str(30 - index))
        for index, year in enumerate(range(2021, 2026))
    )
    evidence_only = _classify(frozen_company(annual=annual))
    assert evidence_only.verdict == "pass"
    assert "quality.margin_deterioration" in evidence_only.evidence_rule_ids
    assert "margin_deterioration" in evidence_only.risk_flags

    payload = quality_payload()
    payload["sector_branches"]["nonfinancial"][
        "margin_deterioration_policy"
    ] = "drop"
    dropped = _classify(frozen_company(annual=annual), payload)
    assert dropped.primary_reason == "margin_deterioration"


@pytest.mark.parametrize("policy", ["both", "either", "pe", "pb"])
def test_configured_valuation_policy_checks_percentile_and_median(
    policy: str,
) -> None:
    payload = quality_payload(
        pe_percentile_max="100",
        pb_percentile_max="100",
        pe_median_ratio_max="0.5",
        pb_median_ratio_max="0.5",
    )
    payload["sector_branches"]["nonfinancial"]["valuation_policy"] = policy
    verdict = _classify(payload=payload)
    assert verdict.verdict == "drop"
    expected = (
        "valuation_pe_not_discounted"
        if policy in {"both", "either", "pe"}
        else "valuation_pb_not_discounted"
    )
    assert verdict.primary_reason == expected


def test_percentile_above_threshold_drops_for_pe_and_pb() -> None:
    verdict = _classify(
        frozen_company(valuations=_valuations(latest_pe="99", latest_pb="9"))
    )
    assert verdict.verdict == "drop"
    assert verdict.primary_reason == "valuation_pe_percentile_above_max"
    assert verdict.metrics["pe_percentile"] == "100"
    assert "quality.valuation_pb_percentile_above_max" in (
        verdict.evidence_rule_ids
    )


def test_median_missing_policy_is_independent_from_percentile_policy() -> None:
    earlier = dict(
        _valuations()[0],
        month="2021-09",
        trade_date="2021-09-15",
    )
    values = list(_valuations())
    values[-1] = dict(values[-1], pe=None)
    payload = quality_payload()
    payload["sector_branches"]["nonfinancial"]["valuation_policy"] = "pe"
    payload["missing_policy"]["pe_percentile"] = "skip"
    payload["missing_policy"]["pe_median_ratio"] = "insufficient"
    verdict = _classify(
        frozen_company(valuations=(earlier, *values)),
        payload,
    )
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "valuation_pe_median_ratio_insufficient",
    )


def test_cyclical_uses_normalized_earnings_and_price_times_shares() -> None:
    annual = tuple(
        _annual(
            year,
            net_income="1000" if year == 2025 else "100",
            ocf="1200" if year == 2025 else "120",
            deducted_net_income="100",
        )
        for year in range(2021, 2026)
    )
    verdict = _classify(
        frozen_company(industry="煤炭", annual=annual),
        quality_payload(normalized_pe_max="8"),
    )
    assert verdict.primary_reason == "normalized_pe_above_max"
    assert verdict.metrics["normalized_earnings"] == "100"
    assert verdict.metrics["normalized_pe"] == "10"
    assert "quality.cyclical_normalized_earnings" in verdict.evidence_rule_ids


@pytest.mark.parametrize("earnings", ["0", "-100"])
def test_cyclical_nonpositive_normalized_earnings_drops(
    earnings: str,
) -> None:
    annual = tuple(
        _annual(year, deducted_net_income=earnings)
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(industry="煤炭", annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "normalized_earnings_not_positive",
    )
    assert verdict.metrics["normalized_pe"] is None
    assert verdict.evidence_rule_ids[-1] == (
        "quality.normalized_earnings_not_positive"
    )


def test_bank_bypasses_industrial_gates_and_uses_declared_policy() -> None:
    annual = tuple(
        _annual(
            year,
            interest_debt="999999",
            interest_coverage="0.01",
            capital_adequacy="14",
            core_tier1_capital_adequacy="10",
            npl="1",
        )
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(industry="银行", annual=annual))
    assert verdict.verdict == "pass"
    assert verdict.metrics["branch"] == "bank"
    assert verdict.metrics["debt_to_equity"] is None
    assert verdict.metrics["interest_coverage"] is None


@pytest.mark.parametrize(
    ("latest", "reason"),
    [
        ({"shares": "120"}, "dilution_rate_above_max"),
        ({"interest_debt": "200"}, "debt_to_equity_above_max"),
        ({"interest_coverage": "2"}, "interest_coverage_below_min"),
    ],
)
def test_dilution_leverage_and_interest_follow_ruler_policies(
    latest: dict[str, str],
    reason: str,
) -> None:
    annual = tuple(
        _annual(year, **(latest if year == 2025 else {}))
        for year in range(2021, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == ("drop", reason)


def test_unknown_industry_and_malformed_snapshot_are_insufficient() -> None:
    unknown = _classify(frozen_company(industry="未知新行业"))
    assert (unknown.verdict, unknown.primary_reason) == (
        "insufficient",
        "industry_label_unmapped",
    )
    malformed = frozen_company()
    malformed = replace(
        malformed,
        fields=MappingProxyType(dict(malformed.fields) | {"surprise": "1"}),
    )
    verdict = _classify(malformed)
    assert verdict.verdict == "insufficient"
    assert verdict.primary_reason == "snapshot_schema_invalid"


@pytest.mark.parametrize(
    ("industry", "expected"),
    [
        ("银行", "bank"),
        ("商业银行", "bank"),
        ("保险", "insurance"),
        ("证券", "other_financial"),
        ("信托", "other_financial"),
        ("期货", "other_financial"),
        ("多元金融", "other_financial"),
        ("非银行金融", "other_financial"),
        ("非银行", "nonfinancial"),
        ("制造业", "nonfinancial"),
        ("-", "unknown"),
        (None, "unknown"),
    ],
)
def test_shared_industry_branch_classifier_is_used_everywhere(
    industry: str | None,
    expected: str,
) -> None:
    from discovery.data_readiness import classify_industry_branch as readiness
    from discovery.industry import classify_industry_branch
    from scripts.calibrate_discovery import _branch_counts

    assert classify_industry_branch(industry) == expected
    assert readiness(industry) == expected
    assert _branch_counts((industry,)) == {
        branch: int(branch == expected)
        for branch in (
            "bank",
            "insurance",
            "other_financial",
            "nonfinancial",
            "unknown",
        )
    }


def test_quality_branch_data_contract_is_build_versioned_and_immutable() -> None:
    from discovery.builds import BUILD_VERSION
    from discovery.quality_contract import (
        QUALITY_DATA_CONTRACTS,
        quality_branch_data_contract,
        quality_ruler_branch_policies,
    )

    contract = quality_branch_data_contract(BUILD_VERSION)
    assert set(contract) == {
        "bank",
        "insurance",
        "other_financial",
        "nonfinancial",
        "unknown",
    }
    assert contract["bank"].supported is True
    assert contract["bank"].valuation_policy == "pb"
    assert contract["nonfinancial"].valuation_policy == "configured"
    assert contract["insurance"].insufficient_reason == (
        "industry_branch_insurance_unsupported"
    )
    assert contract["unknown"].insufficient_reason == "industry_branch_unknown"
    assert dict(quality_ruler_branch_policies(BUILD_VERSION)) == {
        "bank": "bank",
        "insurance": "unsupported",
        "other_financial": "unsupported",
        "nonfinancial": "industry_map",
        "unknown": "insufficient",
    }
    with pytest.raises(TypeError):
        QUALITY_DATA_CONTRACTS["future"] = contract  # type: ignore[index]
    with pytest.raises(ValueError, match="data contract is unavailable"):
        quality_branch_data_contract("future")


@pytest.mark.parametrize(
    ("industry", "branch", "reason"),
    [
        ("保险", "insurance", "industry_branch_insurance_unsupported"),
        (
            "证券",
            "other_financial",
            "industry_branch_other_financial_unsupported",
        ),
        (
            "信托",
            "other_financial",
            "industry_branch_other_financial_unsupported",
        ),
        (
            "期货",
            "other_financial",
            "industry_branch_other_financial_unsupported",
        ),
        (
            "多元金融",
            "other_financial",
            "industry_branch_other_financial_unsupported",
        ),
        (
            "非银行金融",
            "other_financial",
            "industry_branch_other_financial_unsupported",
        ),
        ("-", "unknown", "industry_branch_unknown"),
    ],
)
def test_unsupported_financial_branches_never_run_industrial_gates(
    industry: str,
    branch: str,
    reason: str,
) -> None:
    verdict = _classify(frozen_company(industry=industry))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        reason,
    )
    assert verdict.metrics["branch"] == branch
    assert verdict.metrics["quality_branch"] is None
    assert verdict.evidence_rule_ids == (f"quality.{reason}",)


def test_bank_uses_dedicated_shared_branch() -> None:
    verdict = _classify(frozen_company(industry="商业银行"))
    assert verdict.verdict == "pass"
    assert verdict.metrics["branch"] == "bank"
    assert verdict.metrics["quality_branch"] == "bank"


def test_ambiguous_nonbank_label_never_uses_bank_or_industrial_gates() -> None:
    verdict = _classify(frozen_company(industry="非银行"))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "industry_label_unmapped",
    )
    assert verdict.metrics["branch"] == "nonfinancial"
    assert verdict.metrics["quality_branch"] is None


def test_histories_are_bounded_and_duplicate_periods_are_deduped() -> None:
    values = _valuations()
    duplicate = values[:-1] + (values[0], values[-1])
    deduped = _classify(frozen_company(valuations=duplicate))
    assert (deduped.verdict, deduped.primary_reason) == (
        "insufficient",
        "valuation_history_invalid",
    )

    oversized = _valuations() * 5
    bounded = _classify(frozen_company(valuations=oversized))
    assert (bounded.verdict, bounded.primary_reason) == (
        "insufficient",
        "valuation_history_invalid",
    )


def test_roe_uses_exact_ten_year_calendar_window() -> None:
    annual = tuple(
        _annual(
            year,
            roe="1000" if year == 2015 else "8",
        )
        for year in range(2015, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "roe_mean_below_min",
    )
    assert verdict.metrics["roe_mean"] == "8"
    assert verdict.metrics["roe_window_start_year"] == "2016"
    assert verdict.metrics["roe_window_end_year"] == "2025"
    assert verdict.metrics["roe_available_years"] == tuple(
        str(year) for year in range(2016, 2026)
    )
    assert verdict.metrics["roe_available_count"] == "10"


def test_old_profits_cannot_rescue_recent_profitability_window() -> None:
    annual = tuple(
        _annual(
            year,
            net_income="100" if year <= 2020 else "-100",
            ocf="120" if year <= 2020 else "-120",
        )
        for year in range(2016, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "positive_net_income_observations_below_min",
    )
    assert verdict.metrics["positive_net_income_observations"] == "0"
    assert verdict.metrics["profitability_available_years"] == (
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    )
    assert verdict.metrics["cash_conversion"] is None


def test_calendar_year_gaps_reduce_window_observations() -> None:
    annual = tuple(
        _annual(year)
        for year in (2021, 2022, 2024, 2025)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "roe_insufficient",
    )
    assert verdict.metrics["roe_available_years"] == (
        "2021",
        "2022",
        "2024",
        "2025",
    )
    assert verdict.metrics["roe_observations"] == "4"


def test_stale_latest_fiscal_year_is_insufficient() -> None:
    annual = tuple(
        _annual(year)
        for year in range(2019, 2024)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "financial_history_stale",
    )
    assert verdict.metrics["latest_fiscal_year"] == "2023"
    assert verdict.metrics["latest_fiscal_year_staleness"] == "2"
    assert verdict.metrics["allowed_latest_annual_staleness_years"] == "1"
    assert verdict.evidence_rule_ids == ("quality.financial_history_stale",)


def test_annual_periods_are_canonical_and_unique_by_fiscal_year() -> None:
    leap = list(
        _annual(year) for year in range(2021, 2026)
    )
    leap[3] = dict(leap[3], period="2024-02-29")
    assert _classify(
        frozen_company(annual=tuple(leap))
    ).primary_reason == "financial_history_invalid"

    duplicate = tuple(
        _annual(year) for year in range(2021, 2026)
    ) + (dict(_annual(2025), period="2025-12-31"),)
    duplicated = _classify(frozen_company(annual=duplicate))
    assert (duplicated.verdict, duplicated.primary_reason) == (
        "insufficient",
        "annual_fiscal_year_duplicate",
    )
    assert duplicated.evidence_rule_ids == (
        "quality.annual_fiscal_year_duplicate",
    )


def test_future_annual_period_is_never_included() -> None:
    future = tuple(
        _annual(year) for year in range(2022, 2027)
    )
    verdict = _classify(frozen_company(annual=future))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "financial_history_invalid",
    )


def test_dilution_reports_configured_window_endpoints() -> None:
    annual = tuple(
        _annual(
            year,
            shares=(
                "1"
                if year == 2020
                else "105"
                if year == 2025
                else "100"
            ),
        )
        for year in range(2020, 2026)
    )
    verdict = _classify(frozen_company(annual=annual))
    assert verdict.verdict == "pass"
    assert verdict.metrics["dilution_rate"] == "0.05"
    assert verdict.metrics["dilution_window_start_year"] == "2021"
    assert verdict.metrics["dilution_window_end_year"] == "2025"
    assert verdict.metrics["dilution_endpoint_start_year"] == "2021"
    assert verdict.metrics["dilution_endpoint_end_year"] == "2025"


def test_normalized_earnings_uses_configured_recent_window() -> None:
    annual = tuple(
        _annual(
            year,
            deducted_net_income="10000" if year == 2020 else "100",
        )
        for year in range(2020, 2026)
    )
    verdict = _classify(frozen_company(industry="煤炭", annual=annual))
    assert verdict.metrics["normalized_earnings"] == "100"
    assert verdict.metrics["normalized_earnings_available_years"] == (
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    )


def test_margin_and_bank_metrics_report_exact_branch_windows() -> None:
    industrial = tuple(
        _annual(year, gross_margin="100" if year == 2020 else "30")
        for year in range(2020, 2026)
    )
    margin = _classify(frozen_company(annual=industrial))
    assert margin.metrics["margin_change"] == "0"
    assert margin.metrics["margin_available_years"] == (
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    )

    bank_history = tuple(
        _annual(
            year,
            capital_adequacy="100" if year == 2020 else "14",
            npl="99" if year == 2020 else "1",
        )
        for year in range(2020, 2026)
    )
    bank = _classify(
        frozen_company(industry="银行", annual=bank_history)
    )
    assert bank.verdict == "pass"
    assert bank.metrics["bank_capital_available_years"] == (
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    )
    assert bank.metrics["bank_npl_available_years"] == (
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    )


def test_future_data_mixed_methods_and_untrusted_values_are_insufficient() -> None:
    future = list(_valuations())
    future[-1] = dict(future[-1], trade_date="2027-01-01")
    assert _classify(
        frozen_company(valuations=tuple(future))
    ).primary_reason == "valuation_history_invalid"

    mixed = list(_valuations())
    mixed[-1] = dict(mixed[-1], ratio_method="PE_STATIC/PB_LF")
    assert _classify(
        frozen_company(valuations=tuple(mixed))
    ).primary_reason == "valuation_ratio_method_mismatch"

    wrong = tuple(
        dict(row, ratio_method="PE_STATIC/PB_LF")
        for row in _valuations()
    )
    wrong_method = _classify(frozen_company(valuations=wrong))
    assert (wrong_method.verdict, wrong_method.primary_reason) == (
        "insufficient",
        "valuation_ratio_method_mismatch",
    )
    assert wrong_method.evidence_rule_ids == (
        "quality.valuation_ratio_method_mismatch",
    )

    fake_months = tuple(
        dict(row, trade_date="2026-09-15")
        for row in _valuations()
    )
    fake = _classify(frozen_company(valuations=fake_months))
    assert (fake.verdict, fake.primary_reason) == (
        "insufficient",
        "valuation_history_invalid",
    )
    assert fake.evidence_rule_ids == ("quality.valuation_history_invalid",)

    annual = list(frozen_company().fields["financial_history"])
    row = dict(annual[-1])
    row["field_sources"] = dict(row["field_sources"])
    row["field_sources"]["roe"] = dict(
        row["field_sources"]["roe"], trusted=False
    )
    annual[-1] = row
    assert _classify(
        frozen_company(annual=tuple(annual))
    ).primary_reason == "roe_insufficient"


def test_verdict_input_and_ruler_are_immutable_and_json_has_no_float() -> None:
    from discovery.rulers import quality_discount_ruler
    from discovery.tracks.quality_discount import classify

    company = frozen_company()
    ruler = quality_discount_ruler(quality_payload())
    before = canonical_json(
        {
            "security_status": company.security_status,
            "fields": company.fields,
            "sources": company.sources,
            "as_of": company.as_of,
        }
    )
    verdict = classify(company, ruler)
    assert canonical_json(
        {
            "security_status": company.security_status,
            "fields": company.fields,
            "sources": company.sources,
            "as_of": company.as_of,
        }
    ) == before

    def assert_no_float(value):
        if isinstance(value, MappingProxyType):
            value = dict(value)
        if isinstance(value, dict):
            for item in value.values():
                assert_no_float(item)
        elif isinstance(value, tuple):
            for item in value:
                assert_no_float(item)
        else:
            assert type(value) is not float

    assert_no_float(verdict.metrics)
    assert canonical_json(verdict.metrics) == canonical_json(
        classify(company, ruler).metrics
    )
    with pytest.raises(FrozenInstanceError):
        verdict.verdict = "drop"  # type: ignore[misc]
    with pytest.raises(TypeError):
        verdict.metrics["roe_mean"] = "0"  # type: ignore[index]


def test_quality_decimal_arithmetic_ignores_hostile_contexts() -> None:
    huge = "1" + "0" * 100
    annual = tuple(
        _annual(
            year,
            shares=huge,
            deducted_net_income="1",
        )
        for year in range(2021, 2026)
    )
    valuations = list(_valuations())
    valuations[-1] = dict(valuations[-1], price=huge)
    company = frozen_company(
        industry="煤炭",
        annual=annual,
        valuations=tuple(valuations),
    )
    saved_current = getcontext().copy()
    saved_default = DefaultContext.copy()
    hostile = saved_current.copy()
    hostile.prec = 2
    hostile.Emax = 9
    hostile.Emin = -9
    hostile.clear_flags()
    expected_flags = hostile.flags.copy()
    try:
        setcontext(hostile)
        DefaultContext.prec = 3
        DefaultContext.Emax = 9
        DefaultContext.Emin = -9
        verdict = _classify(company)
        assert verdict.metrics["normalized_pe"] == "1" + "0" * 200
        assert getcontext().flags == expected_flags
    finally:
        setcontext(saved_current)
        DefaultContext.prec = saved_default.prec
        DefaultContext.rounding = saved_default.rounding
        DefaultContext.Emin = saved_default.Emin
        DefaultContext.Emax = saved_default.Emax
        DefaultContext.capitals = saved_default.capitals
        DefaultContext.clamp = saved_default.clamp
        DefaultContext.traps = saved_default.traps.copy()
        DefaultContext.flags = saved_default.flags.copy()


def test_interest_coverage_sign_is_context_and_flag_invariant() -> None:
    annual = tuple(
        _annual(
            year,
            interest_coverage="2.994011976047904191616766467",
        )
        for year in range(2021, 2026)
    )
    saved_current = getcontext().copy()
    saved_default = DefaultContext.copy()
    hostile = saved_current.copy()
    hostile.prec = 2
    hostile.rounding = ROUND_DOWN
    hostile.clear_flags()
    hostile.flags[Inexact] = True
    expected_flags = hostile.flags.copy()
    try:
        setcontext(hostile)
        DefaultContext.prec = 2
        DefaultContext.rounding = ROUND_DOWN
        verdict = _classify(frozen_company(annual=annual))
        assert (verdict.verdict, verdict.primary_reason) == (
            "drop",
            "interest_coverage_below_min",
        )
        assert verdict.metrics["interest_coverage"].startswith("2.994011")
        assert getcontext().flags == expected_flags
    finally:
        setcontext(saved_current)
        DefaultContext.prec = saved_default.prec
        DefaultContext.rounding = saved_default.rounding
        DefaultContext.Emin = saved_default.Emin
        DefaultContext.Emax = saved_default.Emax
        DefaultContext.capitals = saved_default.capitals
        DefaultContext.clamp = saved_default.clamp
        DefaultContext.traps = saved_default.traps.copy()
        DefaultContext.flags = saved_default.flags.copy()


def test_extreme_decimal_exponent_is_bounded_before_formatting() -> None:
    annual = list(frozen_company().fields["financial_history"])
    annual[0] = _annual(2021, roe="1E+5000")
    verdict = _classify(frozen_company(annual=tuple(annual)))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "financial_history_invalid",
    )


def test_quality_and_other_track_concepts_remain_independent() -> None:
    company = frozen_company()
    quality = _classify(company)

    def other_track_fixture_gate(item: FrozenCompanyInput) -> str:
        return "drop" if Decimal(item.fields["pb"]) > Decimal("0.5") else "pass"

    assert quality.verdict == "pass"
    assert other_track_fixture_gate(company) == "drop"


def test_builtin_dispatcher_enforces_overall_coverage_without_artifacts() -> None:
    from discovery.rulers import quality_discount_ruler
    from jobs.discovery import (
        TrackExecutionCandidate,
        TrackExecutionContext,
        builtin_track_executor,
    )
    from discovery.snapshot import FrozenRunInputs

    payload = quality_payload(readiness_min="1")
    ruler = quality_discount_ruler(payload)
    ready = frozen_company()
    unknown = replace(
        ready,
        code="600001.SH",
        industry="未知",
        input_fingerprint="fingerprint-2",
    )
    frozen = FrozenRunInputs(
        run_id=1,
        input_fingerprint="run",
        data_version="data",
        financial_version="financial",
        valuation_version="valuation",
        ruler_version_ids=MappingProxyType(
            {"quality_discount": 1, "deep_value": None, "garp": None}
        ),
        ruler_payloads=MappingProxyType({}),
        algorithm_version=BUILD_VERSION,
        trading_dates=MappingProxyType({"SH": ("2026-09-17",)}),
        snapshot_at=SNAPSHOT_AT,
        companies=(ready, unknown),
    )
    candidates = tuple(
        TrackExecutionCandidate(code=company.code, persisted_verdict="pass",
                                effective_verdict="pass", reason=None,
                                metrics_json="{}", rank=None)
        for company in frozen.companies
    )
    context = TrackExecutionContext(
        stage_id=1,
        run_id=1,
        track="quality_discount",
        stage="track_gate",
        attempt=1,
        input_fingerprint="run",
        ruler_version_id=1,
        algorithm_version=BUILD_VERSION,
        common_stage_id=1,
        predecessor_stage_id=None,
        predecessor_stage=None,
        frozen_inputs=frozen,
        ruler_payload=MappingProxyType(payload),
        candidates=candidates,
    )
    outcome = builtin_track_executor(context)
    assert outcome.status == "data_not_ready"
    assert outcome.results == ()
    assert "0.5" in (outcome.error or "")
    assert ruler.thresholds["readiness_min"] == "1"


def test_calibrator_missing_schema_is_blocked_deterministic_and_read_only(
    tmp_path: Path,
) -> None:
    from scripts.calibrate_discovery import calibrate

    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    first = calibrate(db, track="quality_discount")
    second = calibrate(db, track="quality_discount")
    assert first == second
    assert first["status"] == "blocked"
    assert first["proposal"] is None
    assert first["distributions"] is None
    assert "missing_schema" in first["blocked_reasons"]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def _calibration_preflight_db(
    db: Path,
    *,
    payload: str = "{}",
) -> None:
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE discovery_data_fills (
                id INTEGER PRIMARY KEY,
                status TEXT,
                snapshot_status TEXT,
                total INTEGER,
                payload_schema_version INTEGER
            );
            CREATE TABLE discovery_data_checkpoints (
                fill_id INTEGER,
                code TEXT,
                status TEXT,
                valuation_points INTEGER,
                payload_json TEXT
            );
            CREATE TABLE discovery_universe_securities (
                fill_id INTEGER,
                code TEXT
            );
            CREATE TABLE discovery_fill_trading_dates (
                fill_id INTEGER,
                trade_date TEXT
            );
            CREATE TABLE job_lock (id INTEGER PRIMARY KEY);
            CREATE TABLE ruler_versions (id INTEGER PRIMARY KEY);
            """
        )
        connection.execute(
            "INSERT INTO discovery_data_fills "
            "VALUES (7, 'done', 'ready', 1, 2)"
        )
        connection.execute(
            "INSERT INTO discovery_data_checkpoints VALUES "
            "(7, '600000.SH', 'done', 60, ?)",
            (payload,),
        )
        connection.execute(
            "INSERT INTO discovery_universe_securities VALUES "
            "(7, '600000.SH')"
        )
        connection.execute(
            "INSERT INTO discovery_fill_trading_dates VALUES "
            "(7, '2026-09-15')"
        )


def test_calibrator_preflight_blocks_resources_before_snapshot_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import calibrate_discovery

    db = tmp_path / "preflight.db"
    _calibration_preflight_db(db, payload="汉")
    with calibrate_discovery._read_only_connection(db) as connection:
        preflight = calibrate_discovery._preflight_latest_fill(connection)
    assert preflight["payload_bytes"] == len("汉".encode("utf-8"))
    assert preflight["max_payload_bytes"] == len("汉".encode("utf-8"))
    called = False

    def forbidden_build(_session):
        nonlocal called
        called = True
        raise AssertionError("resource preflight must run before parsing")

    monkeypatch.setattr(calibrate_discovery, "MAX_TOTAL_PAYLOAD_BYTES", 2)
    monkeypatch.setattr(
        calibrate_discovery,
        "build_production_snapshot",
        forbidden_build,
    )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    artifact = calibrate_discovery.calibrate(db, track="quality_discount")
    assert artifact["status"] == "blocked"
    assert artifact["blocked_reasons"] == ["resource_limit"]
    assert called is False
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_calibrator_blocks_incomplete_and_corrupt_current_fills(
    tmp_path: Path,
) -> None:
    from scripts.calibrate_discovery import calibrate

    incomplete = tmp_path / "incomplete.db"
    _calibration_preflight_db(incomplete)
    with sqlite3.connect(incomplete) as connection:
        connection.execute(
            "UPDATE discovery_data_fills SET total = 2 WHERE id = 7"
        )
    assert calibrate(
        incomplete, track="quality_discount"
    )["blocked_reasons"] == ["snapshot_incomplete_or_corrupt"]

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    assert calibrate(
        corrupt, track="quality_discount"
    )["blocked_reasons"] == ["database_corrupt"]


def test_calibrator_blocks_noncurrent_algorithm_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from discovery.snapshot import (
        CompanySnapshotInput,
        ProductionSnapshot,
        SnapshotVersions,
    )
    from scripts import calibrate_discovery

    db = tmp_path / "wrong-build.db"
    _calibration_preflight_db(db)
    company = frozen_company()
    snapshot = ProductionSnapshot(
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
            algorithm_version="retired-build",
        ),
        trading_dates=MappingProxyType({"SH": ("2026-09-15",)}),
        snapshot_at=SNAPSHOT_AT,
    )
    monkeypatch.setattr(
        calibrate_discovery,
        "build_production_snapshot",
        lambda _session: snapshot,
    )
    artifact = calibrate_discovery.calibrate(
        db, track="quality_discount"
    )
    assert artifact["blocked_reasons"] == ["algorithm_build_mismatch"]
    assert artifact["approval_ready"] is False


def test_calibrator_ready_full_market_proposal_is_deterministic_and_read_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from discovery.snapshot import (
        CompanySnapshotInput,
        ProductionSnapshot,
        SnapshotVersions,
    )
    from scripts import calibrate_discovery

    db = tmp_path / "ready.db"
    _calibration_preflight_db(db)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE discovery_data_fills SET total = 3 WHERE id = 7"
        )
        for code in ("600001.SH", "600002.SH"):
            connection.execute(
                "INSERT INTO discovery_data_checkpoints VALUES "
                "(7, ?, 'done', 60, '{}')",
                (code,),
            )
            connection.execute(
                "INSERT INTO discovery_universe_securities VALUES (7, ?)",
                (code,),
            )

    def recode(industry: str, code: str) -> CompanySnapshotInput:
        company = frozen_company(industry=industry)
        values = dict(company.fields)
        values["valuation_history"] = tuple(
            dict(row, code=code) for row in values["valuation_history"]
        )
        return CompanySnapshotInput(
            code=code,
            name=company.name,
            industry=industry,
            security_status=company.security_status,
            fields=MappingProxyType(values),
            sources=company.sources,
            as_of=company.as_of,
        )

    snapshot = ProductionSnapshot(
        fill_id=7,
        companies=(
            recode("制造业", "600000.SH"),
            recode("煤炭", "600001.SH"),
            recode("银行", "600002.SH"),
        ),
        versions=SnapshotVersions(
            data_version="data",
            financial_version="financial",
            valuation_version="valuation",
            ruler_version_ids=MappingProxyType(
                {"quality_discount": None, "deep_value": None, "garp": None}
            ),
            algorithm_version=BUILD_VERSION,
        ),
        trading_dates=MappingProxyType({"SH": ("2026-09-15",)}),
        snapshot_at=SNAPSHOT_AT,
    )
    monkeypatch.setattr(
        calibrate_discovery,
        "build_production_snapshot",
        lambda _session: snapshot,
    )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    first = calibrate_discovery.calibrate(db, track="quality_discount")
    second = calibrate_discovery.calibrate(db, track="quality_discount")
    assert first == second
    assert first["status"] == "ready"
    assert first["universe"]["total"] == 3
    assert first["universe"]["scope"] == "latest_completed_full_market_fill"
    assert first["universe"]["branches"] == {
        "nonfinancial": 1,
        "cyclical": 1,
        "bank": 1,
    }
    assert first["proposal"]["track"] == "quality_discount"
    assert first["proposal"]["thresholds"]["readiness_min"] == "0.8"
    assert first["proposal"]["sector_branches"]["common"] == {
        "valuation_ratio_method": "PE_TTM/PB_MRQ",
        "field_domain_policy": "quality-domain-v1",
        "lookback_windows": {
            "roe": 10,
            "profitability": 5,
            "cash_conversion": 5,
            "dilution": 5,
            "positive_equity": 1,
            "leverage": 1,
            "interest_coverage": 1,
            "margin": 5,
            "normalized_earnings": 5,
            "bank_capital": 5,
            "bank_npl": 5,
        },
        "latest_annual_staleness_years": 1,
    }
    assert first["proposal"]["sector_branches"]["branch_policies"] == {
        "bank": "bank",
        "insurance": "unsupported",
        "other_financial": "unsupported",
        "nonfinancial": "industry_map",
        "unknown": "insufficient",
    }
    assert first["field_domain_policy"] == "quality-domain-v1"
    assert first["proposal"]["min_observations"][
        "positive_net_income"
    ] == 5
    assert first["proposal"]["thresholds"]["net_assets_positive_min"] == "0"
    assert first["field_coverage"][
        "positive_net_income_observations"
    ]["present"] == 3
    assert first["field_coverage"]["latest_net_assets"]["present"] == 3
    assert first["field_coverage"]["field_domain_valid"] == {
        "present": 3,
        "total": 3,
        "ratio": "1",
    }
    assert first["field_coverage"]["roe_window_start_year"]["present"] == 3
    assert first["distributions"]["valuation_ratio_methods"] == {
        "PE_TTM/PB_MRQ": {"companies": 3, "points": 180}
    }
    assert first["candidate_counts"]["base"]["branches"]["bank"] == {
        "pass": 1,
        "drop": 0,
        "insufficient": 0,
    }
    assert first["hypothesis_labels"]["base"].startswith(
        "calibration hypothesis"
    )
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_calibrator_uses_exact_linear_quartiles() -> None:
    from scripts.calibrate_discovery import _quantiles

    assert _quantiles((Decimal("1"), Decimal("2"))) == {
        "min": "1",
        "p25": "1.25",
        "p50": "1.5",
        "p75": "1.75",
        "max": "2",
    }


@pytest.mark.parametrize("alias_kind", ["same", "hardlink", "symlink"])
def test_calibrator_cli_rejects_database_output_alias_without_writing(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    from scripts.calibrate_discovery import main

    db = tmp_path / "calibration.db"
    _calibration_preflight_db(db)
    output = db
    if alias_kind != "same":
        output = tmp_path / f"{alias_kind}.json"
        try:
            if alias_kind == "hardlink":
                os.link(db, output)
            else:
                output.symlink_to(db)
        except OSError as error:
            pytest.skip(f"{alias_kind} unavailable: {error}")
    before = db.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "--track",
                "quality_discount",
                "--db",
                str(db),
                "--output",
                str(output),
            ]
        )
    assert raised.value.code == 2
    assert db.read_bytes() == before
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash


def test_api_uses_builtin_quality_gate_and_keeps_other_tracks_independent(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from discovery.rulers import activate, latest
    from discovery.snapshot import ProductionSnapshot, SnapshotVersions
    from market.quotes import FakeQuoteClient

    db = tmp_path / "api.db"
    base = frozen_company()
    security = dict(base.security_status)
    security["last_trade_date"] = "2026-09-15"
    company = replace(
        base,
        security_status=MappingProxyType(security),
    )

    def snapshot(session, **_kwargs):
        ruler = latest(session, "quality_discount")
        assert ruler is not None
        return ProductionSnapshot(
            fill_id=1,
            companies=(company,),  # type: ignore[arg-type]
            versions=SnapshotVersions(
                data_version="data-v1",
                financial_version="financial-v1",
                valuation_version="valuation-v1",
                ruler_version_ids=MappingProxyType(
                    {
                        "quality_discount": ruler.id,
                        "deep_value": None,
                        "garp": None,
                    }
                ),
                algorithm_version=BUILD_VERSION,
            ),
            trading_dates=MappingProxyType({"SH": ("2026-09-15",)}),
            snapshot_at=SNAPSHOT_AT,
        )

    app = create_app(
        db_path=db,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
        discovery_snapshot_provider=snapshot,
    )
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            activate(session, quality_payload(), "test calibration approval")
            session.commit()
        run = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()
        run_id = run["id"]
        assert run["ruler_readiness"] == {
            "quality_discount": "ready",
            "deep_value": "ruler_not_ready",
            "garp": "ruler_not_ready",
        }
        response = client.post(
            f"/api/discoveries/{run_id}/stages/safety/confirm",
            json={"sync": True},
        )
        assert response.status_code == 200
        quality = client.get(
            f"/api/discoveries/{run_id}/tracks/quality_discount"
            "/stages/track_gate"
        ).json()
        assert quality["current"]["status"] == "waiting_confirmation"
        assert [row["code"] for row in quality["pass"]] == ["600000.SH"]
        assert quality["pass"][0]["metrics"]["roe_mean"] == "15"
        assert quality["evidence"] == []
        for track in ("deep_value", "garp"):
            stage = client.get(
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/track_gate"
            ).json()
            assert stage["current"]["status"] == "ruler_not_ready"
