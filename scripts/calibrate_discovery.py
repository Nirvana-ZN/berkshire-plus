"""Read-only discovery-track calibration evidence.

Exit codes: 0 means a ready evidence proposal was emitted, 2 means calibration
was deterministically blocked by missing/incomplete production prerequisites,
and 1 is reserved for invalid CLI usage or an unexpected operational failure.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from discovery.builds import BUILD_VERSION
from discovery.company_payload import (
    PAYLOAD_SCHEMA_BLOCK_REASON,
    SCHEMA_VERSION as COMPANY_PAYLOAD_SCHEMA_VERSION,
)
from discovery.field_dictionary import FIELD_DICTIONARY
from discovery.industry import INDUSTRY_BRANCHES, classify_industry_branch
from discovery.quality_contract import quality_ruler_branch_policies
from discovery.rulers import (
    DEEP_VALUE_FIELDS,
    DEEP_VALUE_FORMULAS,
    DEEP_VALUE_ORDER,
    DEEP_VALUE_FIELD_DOMAIN_POLICY,
    GARP_FIELDS,
    GARP_FORMULAS,
    GARP_ORDER,
    GARP_FIELD_DOMAIN_POLICY,
    QUALITY_DISCOUNT_FIELDS,
    QUALITY_DISCOUNT_FORMULAS,
    QUALITY_FIELD_DOMAIN_POLICY,
    canonical_payload,
    deep_value_ruler,
    garp_ruler,
    quality_discount_ruler,
)
from discovery.snapshot import (
    FrozenCompanyInput,
    build_production_snapshot,
    canonical_json,
)
from discovery.summaries import _distribution
from discovery.tracks.deep_value import _DOMAINS as DEEP_VALUE_DOMAINS
from discovery.tracks.quality_discount import (
    QUALITY_DOMAIN_INVALID_REASONS,
    _FINANCIAL_FIELD_DOMAINS as QUALITY_DOMAINS,
    MAX_FINANCIAL_ROWS,
    MAX_VALUATION_ROWS,
    bounded_ratio,
    classify,
    decimal_string,
)
from discovery.tracks.deep_value import classify as classify_deep_value
from discovery.tracks.garp import _DOMAINS as GARP_DOMAINS
from discovery.tracks.garp import classify as classify_garp
from market.types import ValuationPoint

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "app.db"
MAX_COMPANIES = 10_000
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
MAX_TOTAL_PAYLOAD_BYTES = 512 * 1024 * 1024
MAX_SINGLE_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_VALUATION_POINTS = MAX_COMPANIES * MAX_VALUATION_ROWS
MAX_TRADING_DATES = 10_000
# ponytail: these caps leave headroom above the current all-A universe while
# bounding pre-parse memory; raise only with measured universe/payload growth.
REQUIRED_TABLES = frozenset(
    {
        "discovery_data_fills",
        "discovery_data_checkpoints",
        "discovery_universe_securities",
        "discovery_fill_trading_dates",
        "job_lock",
        "ruler_versions",
    }
)
MINIMUMS = {
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
}
LOOKBACK_WINDOWS = {
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
}
LATEST_ANNUAL_STALENESS_YEARS = 1
HYPOTHESIS_BANDS: Mapping[str, Mapping[str, str]] = {
    "conservative": {
        "listing_years_min": "5",
        "roe_mean_min": "15",
        "roe_cv_max": "0.2",
        "net_income_positive_min": "0",
        "net_assets_positive_min": "0",
        "cash_conversion_min": "1.1",
        "dilution_rate_max": "0",
        "debt_to_equity_max": "0.7",
        "interest_coverage_min": "5",
        "margin_deterioration_max": "1",
        "pe_percentile_max": "20",
        "pe_median_ratio_max": "0.7",
        "pb_percentile_max": "20",
        "pb_median_ratio_max": "0.7",
        "normalized_pe_max": "12",
        "normalized_earnings_min": "0",
        "bank_roe_min": "12",
        "bank_capital_adequacy_min": "14",
        "bank_core_tier1_min": "10",
        "bank_npl_max": "1.5",
        "readiness_min": "0.9",
    },
    "base": {
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
    },
    "relaxed": {
        "listing_years_min": "5",
        "roe_mean_min": "10",
        "roe_cv_max": "0.35",
        "net_income_positive_min": "0",
        "net_assets_positive_min": "0",
        "cash_conversion_min": "0.8",
        "dilution_rate_max": "0.1",
        "debt_to_equity_max": "1.5",
        "interest_coverage_min": "2",
        "margin_deterioration_max": "3",
        "pe_percentile_max": "40",
        "pe_median_ratio_max": "0.9",
        "pb_percentile_max": "40",
        "pb_median_ratio_max": "0.9",
        "normalized_pe_max": "18",
        "normalized_earnings_min": "0",
        "bank_roe_min": "8",
        "bank_capital_adequacy_min": "11",
        "bank_core_tier1_min": "8",
        "bank_npl_max": "2.5",
        "readiness_min": "0.7",
    },
}
DEEP_VALUE_MINIMUMS = {
    "normalized_earnings": 5,
    "fcf": 5,
    "net_debt": 1,
    "interest_coverage": 1,
    "dilution": 2,
    "revenue": 2,
    "one_time_profit": 1,
    "impairment": 1,
    "non_core_income_proxy": 1,
    "valuation_pb": 60,
    "bank_roe": 5,
    "bank_npl": 2,
    "bank_capital": 1,
}
DEEP_VALUE_LOOKBACKS = {
    "normalized_earnings": 5,
    "fcf": 5,
    "net_debt": 1,
    "interest_coverage": 1,
    "dilution": 5,
    "revenue": 5,
    "one_time_profit": 1,
    "impairment": 1,
    "non_core_income_proxy": 1,
    "bank_roe": 5,
    "bank_npl": 5,
    "bank_capital": 5,
}
DEEP_VALUE_BANDS: Mapping[str, Mapping[str, str]] = {
    "conservative": {
        "listing_years_min": "5",
        "normalized_earnings_min": "0",
        "normalized_pe_max": "10",
        "cumulative_fcf_min": "0",
        "normalized_fcf_min": "0",
        "net_debt_to_normalized_fcf_max": "2",
        "interest_coverage_min": "3",
        "dilution_rate_max": "0.05",
        "revenue_decline_max": "0.1",
        "one_time_profit_share_max": "0.2",
        "impairment_share_max": "0.2",
        "non_core_income_proxy_share_max": "0.1",
        "pb_percentile_max": "20",
        "pb_median_ratio_max": "0.7",
        "bank_roe_min": "10",
        "bank_npl_increase_max": "0.1",
        "bank_capital_adequacy_min": "14",
        "bank_core_tier1_min": "10",
        "readiness_min": "0.9",
    },
    "base": {
        "listing_years_min": "5",
        "normalized_earnings_min": "0",
        "normalized_pe_max": "12",
        "cumulative_fcf_min": "0",
        "normalized_fcf_min": "0",
        "net_debt_to_normalized_fcf_max": "3",
        "interest_coverage_min": "2",
        "dilution_rate_max": "0.1",
        "revenue_decline_max": "0.2",
        "one_time_profit_share_max": "0.3",
        "impairment_share_max": "0.3",
        "non_core_income_proxy_share_max": "0.2",
        "pb_percentile_max": "30",
        "pb_median_ratio_max": "0.8",
        "bank_roe_min": "8",
        "bank_npl_increase_max": "0.2",
        "bank_capital_adequacy_min": "12",
        "bank_core_tier1_min": "9",
        "readiness_min": "0.8",
    },
    "relaxed": {
        "listing_years_min": "5",
        "normalized_earnings_min": "0",
        "normalized_pe_max": "15",
        "cumulative_fcf_min": "0",
        "normalized_fcf_min": "0",
        "net_debt_to_normalized_fcf_max": "4",
        "interest_coverage_min": "1.5",
        "dilution_rate_max": "0.15",
        "revenue_decline_max": "0.3",
        "one_time_profit_share_max": "0.4",
        "impairment_share_max": "0.4",
        "non_core_income_proxy_share_max": "0.3",
        "pb_percentile_max": "40",
        "pb_median_ratio_max": "0.9",
        "bank_roe_min": "6",
        "bank_npl_increase_max": "0.3",
        "bank_capital_adequacy_min": "11",
        "bank_core_tier1_min": "8",
        "readiness_min": "0.7",
    },
}
GARP_MINIMUMS = {
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
}
GARP_WINDOWS = {
    "growth": 5,
    "return_on_capital": 5,
    "cash_conversion": 5,
    "dilution": 5,
    "growth_drivers": 5,
    "margin": 5,
    "quality_adjustments": 1,
}
GARP_BANDS: Mapping[str, Mapping[str, str]] = {
    "conservative": {
        "revenue_cagr_min": "0.1",
        "deducted_profit_cagr_min": "0.12",
        "revenue_period_growth_min": "0.06",
        "deducted_profit_period_growth_min": "0.06",
        "revenue_growth_slowdown_max": "0.08",
        "deducted_profit_growth_slowdown_max": "0.08",
        "return_on_capital_min": "12",
        "cash_conversion_min": "1.1",
        "dilution_rate_max": "0",
        "interest_debt_increase_to_revenue_max": "0.1",
        "goodwill_increase_to_revenue_max": "0.05",
        "acquisition_cash_to_revenue_max": "0.05",
        "gross_margin_deterioration_max": "1",
        "net_margin_deterioration_max": "1",
        "impairment_share_max": "0.1",
        "non_core_income_proxy_share_max": "0.1",
        "peg_growth_cap": "0.2",
        "peg_max": "1.2",
        "pe_percentile_max": "60",
        "pe_max": "30",
        "readiness_min": "0.9",
    },
    "base": {
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
    },
    "relaxed": {
        "revenue_cagr_min": "0.06",
        "deducted_profit_cagr_min": "0.08",
        "revenue_period_growth_min": "0.03",
        "deducted_profit_period_growth_min": "0.03",
        "revenue_growth_slowdown_max": "0.15",
        "deducted_profit_growth_slowdown_max": "0.15",
        "return_on_capital_min": "8",
        "cash_conversion_min": "0.8",
        "dilution_rate_max": "0.1",
        "interest_debt_increase_to_revenue_max": "0.3",
        "goodwill_increase_to_revenue_max": "0.15",
        "acquisition_cash_to_revenue_max": "0.15",
        "gross_margin_deterioration_max": "3",
        "net_margin_deterioration_max": "3",
        "impairment_share_max": "0.3",
        "non_core_income_proxy_share_max": "0.3",
        "peg_growth_cap": "0.3",
        "peg_max": "1.8",
        "pe_percentile_max": "80",
        "pe_max": "40",
        "readiness_min": "0.7",
    },
}


def _blocked(
    *reasons: str,
    track: str = "quality_discount",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "track": track,
        "status": "blocked",
        "approval_ready": False,
        "blocked_reasons": list(reasons),
        "universe": None,
        "field_coverage": None,
        "provenance_coverage": None,
        "branch_coverage": None,
        "source_coverage": None,
        "distributions": None,
        "candidate_counts": None,
        "source_as_of": None,
        "industry_versions": None,
        "algorithm_build": BUILD_VERSION,
        "field_domain_policy": (
            DEEP_VALUE_FIELD_DOMAIN_POLICY
            if track == "deep_value"
            else GARP_FIELD_DOMAIN_POLICY
            if track == "garp"
            else QUALITY_FIELD_DOMAIN_POLICY
        ),
        "hypotheses": None,
        "hypothesis_labels": None,
        "proposed_readiness_threshold": None,
        "return_metric_coverage": None,
        "proposal": None,
        "note": (
            "Calibration checkpoint only; no ruler was activated or persisted."
        ),
    }


def _uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_uri(path), uri=True)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    }


def _preflight_latest_fill(
    connection: sqlite3.Connection,
) -> dict[str, int] | None:
    row = connection.execute(
        """
        SELECT
            fill.id,
            fill.total,
            (SELECT COUNT(*) FROM discovery_universe_securities AS security
             WHERE security.fill_id = fill.id),
            (SELECT COUNT(*) FROM discovery_data_checkpoints AS checkpoint
             WHERE checkpoint.fill_id = fill.id),
            (SELECT COUNT(*) FROM discovery_data_checkpoints AS checkpoint
             WHERE checkpoint.fill_id = fill.id AND checkpoint.status = 'done'),
            (SELECT COALESCE(SUM(
                LENGTH(CAST(COALESCE(checkpoint.payload_json, '') AS BLOB))
             ), 0) FROM discovery_data_checkpoints AS checkpoint
             WHERE checkpoint.fill_id = fill.id),
            (SELECT COALESCE(MAX(
                LENGTH(CAST(COALESCE(checkpoint.payload_json, '') AS BLOB))
             ), 0) FROM discovery_data_checkpoints AS checkpoint
             WHERE checkpoint.fill_id = fill.id),
            (SELECT COALESCE(SUM(checkpoint.valuation_points), 0)
             FROM discovery_data_checkpoints AS checkpoint
             WHERE checkpoint.fill_id = fill.id),
            (SELECT COUNT(*) FROM discovery_fill_trading_dates AS trading
             WHERE trading.fill_id = fill.id)
        FROM discovery_data_fills AS fill
        WHERE fill.status = 'done' AND fill.snapshot_status = 'ready'
        ORDER BY fill.id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    keys = (
        "fill_id",
        "declared_companies",
        "security_count",
        "checkpoint_count",
        "done_checkpoint_count",
        "payload_bytes",
        "max_payload_bytes",
        "valuation_points",
        "trading_dates",
    )
    return {key: int(value or 0) for key, value in zip(keys, row, strict=True)}


def _latest_fill_header(
    connection: sqlite3.Connection,
) -> tuple[int, str, str | None, int | None] | None:
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(discovery_data_fills)"
        )
    }
    schema_column = (
        "payload_schema_version"
        if "payload_schema_version" in columns
        else "NULL"
    )
    row = connection.execute(
        f"""
        SELECT id, status, snapshot_status, {schema_column}
        FROM discovery_data_fills
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    fill_id, status, snapshot_status, schema_version = row
    return (
        int(fill_id),
        str(status),
        None if snapshot_status is None else str(snapshot_status),
        None if schema_version is None else int(schema_version),
    )


def _preflight_resource_limit(preflight: Mapping[str, int]) -> bool:
    return (
        preflight["security_count"] > MAX_COMPANIES
        or preflight["checkpoint_count"] > MAX_COMPANIES
        or preflight["payload_bytes"] > MAX_TOTAL_PAYLOAD_BYTES
        or preflight["max_payload_bytes"] > MAX_SINGLE_PAYLOAD_BYTES
        or preflight["valuation_points"] > MAX_VALUATION_POINTS
        or preflight["trading_dates"] > MAX_TRADING_DATES
    )


def _preflight_incomplete(preflight: Mapping[str, int]) -> bool:
    total = preflight["declared_companies"]
    return (
        total <= 0
        or preflight["security_count"] != total
        or preflight["checkpoint_count"] != total
        or preflight["done_checkpoint_count"] != total
        or preflight["trading_dates"] <= 0
    )


def _branch_map(industries: Iterable[str | None]) -> dict[str, list[str]]:
    result = {"nonfinancial": [], "cyclical": [], "bank": []}
    cyclical_tokens = ("煤", "钢", "有色", "化工", "航运", "建材")
    for industry in sorted(
        {item.strip() for item in industries if item and item.strip() != "-"}
    ):
        shared = classify_industry_branch(industry)
        if shared == "bank":
            result["bank"].append(industry)
        elif shared == "nonfinancial" and any(
            token in industry for token in cyclical_tokens
        ):
            result["cyclical"].append(industry)
        elif shared == "nonfinancial":
            result["nonfinancial"].append(industry)
    # TrackRuler requires each configured branch to be explicit and non-empty.
    # A proposal cannot invent an industry merely to satisfy that contract.
    if any(not values for values in result.values()):
        raise ValueError("calibration universe does not cover every quality branch")
    return result


def _branch_counts(
    industries: Iterable[str | None],
) -> dict[str, int]:
    counts = Counter(
        classify_industry_branch(industry) for industry in industries
    )
    return {branch: counts[branch] for branch in INDUSTRY_BRANCHES}


def _quality_payload(
    thresholds: Mapping[str, str],
    *,
    industry_version: str,
    industries: Iterable[str | None],
    valuation_ratio_method: str,
) -> dict[str, object]:
    payload = {
        "scope": "weekly-discovery",
        "track": "quality_discount",
        "fields": list(QUALITY_DISCOUNT_FIELDS),
        "formulas": dict(QUALITY_DISCOUNT_FORMULAS),
        "thresholds": dict(thresholds),
        "min_observations": dict(MINIMUMS),
        "missing_policy": {
            field: "insufficient" for field in QUALITY_DISCOUNT_FIELDS
        },
        "sector_branches": {
            "common": {
                "valuation_ratio_method": valuation_ratio_method,
                "field_domain_policy": QUALITY_FIELD_DOMAIN_POLICY,
                "lookback_windows": dict(LOOKBACK_WINDOWS),
                "latest_annual_staleness_years": (
                    LATEST_ANNUAL_STALENESS_YEARS
                ),
            },
            "branch_policies": dict(
                quality_ruler_branch_policies(BUILD_VERSION)
            ),
            "industry_map": _branch_map(industries),
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
        "industry_version": industry_version,
        "algorithm_version": BUILD_VERSION,
    }
    ruler = quality_discount_ruler(payload)
    return json.loads(canonical_payload(ruler))


def _deep_value_payload(
    thresholds: Mapping[str, str],
    *,
    industry_version: str,
    valuation_ratio_method: str,
) -> dict[str, object]:
    payload = {
        "scope": "weekly-discovery",
        "track": "deep_value",
        "fields": list(DEEP_VALUE_FIELDS),
        "formulas": dict(DEEP_VALUE_FORMULAS),
        "thresholds": dict(thresholds),
        "min_observations": dict(DEEP_VALUE_MINIMUMS),
        "missing_policy": {
            field: "insufficient" for field in DEEP_VALUE_FIELDS
        },
        "sector_branches": {
            "common": {
                "valuation_ratio_method": valuation_ratio_method,
                "field_domain_policy": DEEP_VALUE_FIELD_DOMAIN_POLICY,
                "lookback_windows": dict(DEEP_VALUE_LOOKBACKS),
                "latest_annual_staleness_years": (
                    LATEST_ANNUAL_STALENESS_YEARS
                ),
            },
            "branch_policies": {
                "bank": "financial",
                "insurance": "unsupported",
                "other_financial": "unsupported",
                "nonfinancial": "nonfinancial",
                "unknown": "insufficient",
            },
            "nonfinancial": {
                "gate_order": [
                    "listing_age",
                    "normalized_earnings",
                    "normalized_valuation",
                    "positive_cumulative_fcf",
                    "net_debt",
                    "interest_coverage",
                    "dilution",
                    "revenue_decline",
                    "one_time_profit",
                    "impairment",
                    "non_core_income_proxy",
                ],
                "evidence_fields": [
                    "normalized_pe",
                    "cumulative_fcf",
                    "net_debt_to_normalized_fcf",
                    "interest_coverage",
                ],
            },
            "financial": {
                "gate_order": [
                    "listing_age",
                    "bank_valuation",
                    "bank_roe",
                    "bank_npl_trend",
                    "bank_capital",
                ],
                "evidence_fields": [
                    "pb_percentile",
                    "roe_mean",
                    "npl_change",
                    "bank_capital_adequacy",
                ],
            },
        },
        "order": [list(item) for item in DEEP_VALUE_ORDER],
        "industry_version": industry_version,
        "algorithm_version": BUILD_VERSION,
    }
    return json.loads(canonical_payload(deep_value_ruler(payload)))


def _garp_payload(
    thresholds: Mapping[str, str],
    *,
    industry_version: str,
    valuation_ratio_method: str,
) -> dict[str, object]:
    payload = {
        "scope": "weekly-discovery",
        "track": "garp",
        "fields": list(GARP_FIELDS),
        "formulas": dict(GARP_FORMULAS),
        "thresholds": dict(thresholds),
        "min_observations": dict(GARP_MINIMUMS),
        "missing_policy": {
            field: "insufficient" for field in GARP_FIELDS
        },
        "sector_branches": {
            "common": {
                "valuation_ratio_method": valuation_ratio_method,
                "field_domain_policy": GARP_FIELD_DOMAIN_POLICY,
                "annual_observations": dict(GARP_WINDOWS),
                "cagr_periods": {
                    "revenue": 4,
                    "deducted_profit": 4,
                },
                "latest_annual_staleness_years": (
                    LATEST_ANNUAL_STALENESS_YEARS
                ),
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
            },
        },
        "order": [list(item) for item in GARP_ORDER],
        "industry_version": industry_version,
        "algorithm_version": BUILD_VERSION,
    }
    return json.loads(canonical_payload(garp_ruler(payload)))


def _quantiles(values: Sequence[Decimal]) -> dict[str, str] | None:
    if not values:
        return None
    distribution = _distribution(values)
    return {
        "min": str(distribution["min"]),
        "p25": str(distribution["p25"]),
        "p50": str(distribution["median"]),
        "p75": str(distribution["p75"]),
        "max": str(distribution["max"]),
    }


def _as_decimal(value: object) -> Decimal | None:
    if type(value) is not str:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _freeze_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _classifier_input(company: Any, snapshot: Any) -> FrozenCompanyInput:
    if isinstance(company, FrozenCompanyInput):
        return company
    normalized = json.loads(
        canonical_json(
            {
                "security_status": company.security_status,
                "fields": company.fields,
                "sources": company.sources,
                "as_of": company.as_of,
            }
        )
    )
    frozen = {
        key: _freeze_value(value) for key, value in normalized.items()
    }
    return FrozenCompanyInput(
        code=company.code,
        name=company.name,
        industry=company.industry,
        security_status=frozen["security_status"],
        fields=frozen["fields"],
        sources=frozen["sources"],
        as_of=frozen["as_of"],
        data_version=snapshot.versions.data_version,
        financial_version=snapshot.versions.financial_version,
        valuation_version=snapshot.versions.valuation_version,
        ruler_version_ids=MappingProxyType(
            dict(snapshot.versions.ruler_version_ids)
        ),
        algorithm_version=snapshot.versions.algorithm_version,
        input_fingerprint="calibration-evidence-only",
        snapshot_at=snapshot.snapshot_at,
    )


def _valuation_method_distribution(
    companies: Sequence[Any],
) -> dict[str, dict[str, int]]:
    points: Counter[str] = Counter()
    company_counts: Counter[str] = Counter()
    for company in companies:
        company_methods: set[str] = set()
        history = company.fields.get("valuation_history")
        if not isinstance(history, Sequence):
            continue
        for row in history:
            if not isinstance(row, Mapping):
                continue
            method = row.get("ratio_method")
            if type(method) is str and method:
                points[method] += 1
                company_methods.add(method)
        company_counts.update(company_methods)
    return {
        method: {
            "companies": company_counts[method],
            "points": points[method],
        }
        for method in sorted(points)
    }


def _coverage(present: int, total: int) -> dict[str, object]:
    return {
        "present": present,
        "total": total,
        "ratio": (
            decimal_string(bounded_ratio(present, total)) if total else "0"
        ),
    }


VALUATION_SOURCE = str(
    ValuationPoint.__dataclass_fields__["source"].default
)
SECURITY_SOURCE = "eastmoney:push2delay"


def _canonical_field_source(field: str) -> dict[str, object]:
    spec = FIELD_DICTIONARY[field]
    return {
        "source": "discovery",
        "report": spec.report,
        "field": spec.field,
        "unit": spec.unit,
        "consolidated_basis": spec.consolidated_basis,
        "period_type": spec.period_type,
        "financial_sector_substitute": spec.financial_sector_substitute,
        "source_as_of": spec.source_as_of,
        "trusted": True,
    }


def _required_supported_branches(track: str) -> set[str]:
    return {
        "quality_discount": {"nonfinancial", "cyclical", "bank"},
        "deep_value": {"nonfinancial", "financial"},
        "garp": {"nonfinancial"},
    }[track]


def _ruler_branch(track: str, industry: str | None) -> str:
    shared = classify_industry_branch(industry)
    if track == "quality_discount":
        if shared == "nonfinancial":
            value = (industry or "").strip()
            return (
                "cyclical"
                if any(
                    token in value
                    for token in ("煤", "钢", "有色", "化工", "航运", "建材")
                )
                else "nonfinancial"
            )
        return shared if shared in {"bank", "unknown"} else f"unsupported_{shared}"
    if track == "deep_value":
        if shared == "bank":
            return "financial"
        if shared == "nonfinancial":
            return "nonfinancial"
        return "unknown" if shared == "unknown" else f"unsupported_{shared}"
    if shared == "nonfinancial":
        return "nonfinancial"
    return "unknown" if shared == "unknown" else f"unsupported_{shared}"


def _domain(track: str, field: str) -> str:
    domains: Mapping[str, object] = (
        QUALITY_DOMAINS
        if track == "quality_discount"
        else DEEP_VALUE_DOMAINS
        if track == "deep_value"
        else GARP_DOMAINS
    )
    value = domains[field]
    return str(value[0] if isinstance(value, tuple) else value)


def _trusted_decimal(
    row: Mapping[str, object],
    field: str,
    track: str,
) -> tuple[bool, Decimal | None]:
    values = row.get("values")
    metadata = row.get("field_sources")
    provenance = row.get("provenance")
    if not isinstance(values, Mapping):
        return False, None
    raw = values.get(field)
    present = raw is not None
    item = metadata.get(field) if isinstance(metadata, Mapping) else None
    field_provenance = (
        provenance.get(field) if isinstance(provenance, Mapping) else None
    )
    parsed = _as_decimal(raw)
    if (
        parsed is None
        or not isinstance(item, Mapping)
        or dict(item) != _canonical_field_source(field)
        or isinstance(field_provenance, (str, bytes))
        or not isinstance(field_provenance, Sequence)
        or "discovery" not in field_provenance
    ):
        return present, None
    domain = _domain(track, field)
    if domain in {"positive", "positive_usable"} and parsed <= 0:
        return present, None
    if domain == "nonnegative" and parsed < 0:
        return present, None
    return present, parsed


def _annual_rows(company: Any) -> dict[int, Mapping[str, object]]:
    history = company.fields.get("financial_history")
    if not isinstance(history, Sequence):
        return {}
    result: dict[int, Mapping[str, object]] = {}
    for row in history:
        if (
            not isinstance(row, Mapping)
            or row.get("period_type") != "annual"
        ):
            continue
        period = row.get("period")
        if type(period) is not str:
            continue
        year_text = period[:4]
        if len(year_text) == 4 and year_text.isdigit():
            year = int(year_text)
            if year in result:
                raise ValueError(
                    f"duplicate canonical annual year: {year}"
                )
            result[year] = row
    return result


def _requirements(
    track: str,
    branch: str,
    proposal: Mapping[str, object],
) -> tuple[tuple[str, tuple[str, ...], int, int, str], ...]:
    minimums = proposal["min_observations"]
    branches = proposal["sector_branches"]
    assert isinstance(minimums, Mapping) and isinstance(branches, Mapping)
    common = branches["common"]
    assert isinstance(common, Mapping)
    windows = (
        common["annual_observations"]
        if track == "garp"
        else common["lookback_windows"]
    )
    assert isinstance(windows, Mapping)

    def item(
        name: str,
        fields: tuple[str, ...],
        window: str,
        mode: str = "count",
    ) -> tuple[str, tuple[str, ...], int, int, str]:
        return (
            name,
            fields,
            int(windows[window]),
            int(minimums[name]),
            mode,
        )

    if track == "quality_discount":
        if branch == "bank":
            return (
                item("bank_roe", ("roe",), "roe"),
                item("net_assets", ("net_assets",), "positive_equity", "latest"),
                item(
                    "bank_capital",
                    ("capital_adequacy", "core_tier1_capital_adequacy"),
                    "bank_capital",
                    "latest",
                ),
                item("bank_npl", ("npl",), "bank_npl", "latest"),
            )
        requirements = [
            item("roe", ("roe",), "roe"),
            item(
                "positive_net_income",
                ("net_income",),
                "profitability",
            ),
            item(
                "net_assets",
                ("net_assets",),
                "positive_equity",
                "latest",
            ),
            item(
                "cash_conversion",
                ("ocf", "net_income"),
                "cash_conversion",
            ),
            item("dilution", ("shares",), "dilution", "endpoints"),
            item(
                "leverage",
                ("interest_debt", "net_assets"),
                "leverage",
                "latest",
            ),
            item(
                "interest_coverage",
                ("interest_coverage",),
                "interest_coverage",
                "latest",
            ),
            item("margin", ("gross_margin",), "margin", "endpoints"),
        ]
        if branch == "cyclical":
            requirements.append(
                item(
                    "normalized_earnings",
                    ("deducted_net_income",),
                    "normalized_earnings",
                )
            )
        return tuple(requirements)
    if track == "deep_value":
        if branch == "financial":
            return (
                item("bank_roe", ("roe",), "bank_roe"),
                item("bank_npl", ("npl",), "bank_npl", "endpoints"),
                item(
                    "bank_capital",
                    ("capital_adequacy", "core_tier1_capital_adequacy"),
                    "bank_capital",
                    "latest",
                ),
            )
        return (
            item(
                "normalized_earnings",
                ("deducted_net_income",),
                "normalized_earnings",
            ),
            item("fcf", ("ocf", "capex"), "fcf"),
            item(
                "net_debt",
                ("interest_debt", "cash"),
                "net_debt",
                "latest",
            ),
            item(
                "interest_coverage",
                ("interest_coverage",),
                "interest_coverage",
                "latest",
            ),
            item("dilution", ("shares",), "dilution", "endpoints"),
            item("revenue", ("revenue",), "revenue", "endpoints"),
            item(
                "one_time_profit",
                ("net_income", "deducted_net_income"),
                "one_time_profit",
                "latest",
            ),
            item(
                "impairment",
                ("impairment_loss",),
                "impairment",
                "latest",
            ),
            item(
                "non_core_income_proxy",
                ("other_income", "asset_disposal_gain"),
                "non_core_income_proxy",
                "latest",
            ),
        )
    return (
        item(
            "revenue_growth",
            ("revenue",),
            "growth",
            "exact",
        ),
        item(
            "deducted_profit_growth",
            ("deducted_net_income",),
            "growth",
            "exact",
        ),
        item(
            "cash_conversion",
            ("ocf", "net_income"),
            "cash_conversion",
        ),
        item("dilution", ("shares",), "dilution", "endpoints"),
        item(
            "growth_drivers",
            ("revenue", "interest_debt", "goodwill", "acquisition_cash_paid"),
            "growth_drivers",
            "endpoints",
        ),
        item(
            "margin",
            ("gross_margin", "net_margin"),
            "margin",
            "endpoints",
        ),
        item(
            "quality_adjustments",
            (
                "deducted_net_income",
                "impairment_loss",
                "other_income",
                "asset_disposal_gain",
            ),
            "quality_adjustments",
            "latest",
        ),
    )


def _requirement_result(
    company: Any,
    track: str,
    fields: tuple[str, ...],
    window: int,
    minimum: int,
    mode: str,
    allowed_staleness: int,
) -> tuple[dict[str, bool], bool]:
    history = _annual_rows(company)
    if not history:
        return {field: False for field in fields}, False
    latest = max(history)
    completed = (
        company.snapshot_at.year
        if (
            company.snapshot_at.month,
            company.snapshot_at.day,
        )
        == (12, 31)
        else company.snapshot_at.year - 1
    )
    stale = completed - latest
    start = latest - window + 1
    rows = [
        (year, history[year])
        for year in sorted(history)
        if start <= year <= latest
    ]
    present = {field: False for field in fields}
    available: list[int] = []
    for year, row in rows:
        trusted: dict[str, Decimal | None] = {}
        for field in fields:
            is_present, value = _trusted_decimal(row, field, track)
            present[field] = present[field] or is_present
            trusted[field] = value
        if all(trusted[field] is not None for field in fields) and not (
            track == "quality_discount"
            and fields == ("ocf", "net_income")
            and trusted["net_income"] <= 0  # type: ignore[operator]
        ):
            available.append(year)
    passed = len(available) >= minimum
    passed = passed and 0 <= stale <= allowed_staleness
    if mode == "latest":
        passed = passed and latest in available
    elif mode == "endpoints":
        passed = passed and start in available and latest in available
    elif mode == "exact":
        passed = (
            len(available) == window
            and minimum == window
            and available == list(range(start, latest + 1))
        )
    return present, passed


def _valuation_result(
    company: Any,
    field: str,
    minimum: int,
    method: str,
) -> tuple[bool, bool]:
    history = company.fields.get("valuation_history")
    if not isinstance(history, Sequence):
        return False, False
    present = False
    trusted = 0
    for row in history:
        if not isinstance(row, Mapping) or row.get("ratio_method") != method:
            continue
        raw = row.get(field)
        present = present or raw is not None
        value = _as_decimal(raw)
        if (
            value is not None
            and value > 0
            and row.get("source") == VALUATION_SOURCE
            and row.get("fetched_at") is not None
            and row.get("trade_date") is not None
        ):
            trusted += 1
    return present, trusted >= minimum


def _source_coverage(
    companies: Sequence[Any],
) -> dict[str, dict[str, object]]:
    total = len(companies)
    checks = {
        "security": lambda company: (
            company.security_status.get("security_source")
            == SECURITY_SOURCE
            and company.sources.get("security") == SECURITY_SOURCE
            and company.as_of.get("security") is not None
        ),
        "financial": lambda company: (
            company.sources.get("financial_history")
            == "checkpoint_payload:field_sources"
            and company.as_of.get("financial") is not None
        ),
        "valuation": lambda company: (
            isinstance(
                company.sources.get("valuation_history"), Sequence
            )
            and not isinstance(
                company.sources.get("valuation_history"), (str, bytes)
            )
            and bool(company.sources.get("valuation_history"))
            and all(
                source == VALUATION_SOURCE
                for source in company.sources["valuation_history"]
            )
            and company.as_of.get("valuation") is not None
        ),
        "price": lambda company: (
            company.sources.get("price") == VALUATION_SOURCE
            and company.as_of.get("price") is not None
        ),
        "fill": lambda company: company.as_of.get("fill") is not None,
    }
    return {
        key: _coverage(
            sum(bool(check(company)) for company in companies),
            total,
        )
        for key, check in checks.items()
    }


def _coverage_audit(
    companies: Sequence[Any],
    track: str,
    proposal: Mapping[str, object],
    selected_labels: Sequence[str | None],
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, dict[str, dict[str, object]]],
    dict[str, dict[str, object]],
    dict[str, int],
    dict[str, object] | None,
    bool,
]:
    thresholds = proposal["thresholds"]
    branches = proposal["sector_branches"]
    minimums = proposal["min_observations"]
    assert all(
        isinstance(value, Mapping)
        for value in (thresholds, branches, minimums)
    )
    threshold = Decimal(thresholds["readiness_min"])  # type: ignore[index]
    common = branches["common"]  # type: ignore[index]
    assert isinstance(common, Mapping)
    method = str(common["valuation_ratio_method"])
    applicable: Counter[str] = Counter()
    present: Counter[str] = Counter()
    trusted: Counter[str] = Counter()
    branch_applicable: Counter[tuple[str, str]] = Counter()
    branch_present: Counter[tuple[str, str]] = Counter()
    branch_trusted: Counter[tuple[str, str]] = Counter()
    sources: dict[str, set[str]] = {}
    missing: Counter[str] = Counter()
    branch_counts: Counter[str] = Counter()
    return_observations = Counter[str]()
    return_companies = Counter[str]()
    label_counts = Counter[str]()
    supported_total = 0

    for company, selected_label in zip(
        companies, selected_labels, strict=True
    ):
        branch = _ruler_branch(track, company.industry)
        branch_counts[branch] += 1
        if branch.startswith("unsupported_") or branch == "unknown":
            continue
        supported_total += 1
        states: dict[str, list[bool]] = {}
        for _name, fields, window, minimum, mode in _requirements(
            track, branch, proposal
        ):
            field_present, passed = _requirement_result(
                company,
                track,
                fields,
                window,
                minimum,
                mode,
                int(common["latest_annual_staleness_years"]),
            )
            for field in fields:
                state = states.setdefault(field, [False, True])
                state[0] = state[0] or field_present[field]
                state[1] = state[1] and passed

        valuation_requirements: tuple[tuple[str, int], ...]
        if track == "quality_discount":
            valuation_requirements = (
                (("pb", int(minimums["valuation_pb"])),)  # type: ignore[index]
                if branch == "bank"
                else (
                    ("pe", int(minimums["valuation_pe"])),  # type: ignore[index]
                    ("pb", int(minimums["valuation_pb"])),  # type: ignore[index]
                )
            )
        elif track == "deep_value":
            valuation_requirements = (
                (("pb", int(minimums["valuation_pb"])),)  # type: ignore[index]
                if branch == "financial"
                else (("price", 1),)
            )
        else:
            valuation_requirements = (
                ("pe", int(minimums["valuation_pe"])),  # type: ignore[index]
            )
        for field, minimum in valuation_requirements:
            raw_present, passed = _valuation_result(
                company, field, minimum, method
            )
            states[f"valuation_{field}"] = [raw_present, passed]

        if track == "garp":
            history = _annual_rows(company)
            latest = max(history, default=0)
            completed = (
                company.snapshot_at.year
                if (
                    company.snapshot_at.month,
                    company.snapshot_at.day,
                )
                == (12, 31)
                else company.snapshot_at.year - 1
            )
            return_staleness_valid = (
                0
                <= completed - latest
                <= int(common["latest_annual_staleness_years"])
            )
            window = int(common["annual_observations"]["return_on_capital"])  # type: ignore[index]
            rows = [
                row
                for year, row in sorted(history.items())
                if latest - window + 1 <= year <= latest
            ]
            parsed: dict[str, list[Decimal]] = {"roic": [], "roe": []}
            for row in rows:
                for field in parsed:
                    _is_present, value = _trusted_decimal(
                        row, field, track
                    )
                    if value is not None:
                        parsed[field].append(value)
            label = selected_label or "insufficient"
            label_counts[label] += 1
            selected = (
                "roe"
                if label == "roe_fallback"
                else "roic"
                if label == "roic"
                else None
            )
            selected_passed = False
            for field in ("roic", "roe"):
                key = "roe_fallback" if field == "roe" else field
                applicable[field] += 1
                branch_applicable[(branch, field)] += 1
                raw_present = any(
                    isinstance(row.get("values"), Mapping)
                    and row["values"].get(field) is not None
                    for row in rows
                )
                present[field] += int(raw_present)
                branch_present[(branch, field)] += int(raw_present)
                observations = (
                    len(parsed[field]) if selected == field else 0
                )
                return_observations[key] += observations
                required = int(
                    minimums["roe_fallback" if field == "roe" else "roic"]  # type: ignore[index]
                )
                passed = (
                    selected == field
                    and observations >= required
                    and return_staleness_valid
                    and bool(rows)
                    and latest
                    == max(
                        (
                            year
                            for year, row in history.items()
                            if _trusted_decimal(row, field, track)[1]
                            is not None
                        ),
                        default=0,
                    )
                )
                trusted[field] += int(passed)
                branch_trusted[(branch, field)] += int(passed)
                return_companies[key] += int(passed)
                selected_passed = selected_passed or passed
                if passed:
                    sources.setdefault(field, set()).add("discovery")
            if not selected_passed:
                missing["missing_trusted_return_on_capital"] += 1

        for field, (is_present, passed) in states.items():
            applicable[field] += 1
            present[field] += int(is_present)
            trusted[field] += int(passed)
            branch_applicable[(branch, field)] += 1
            branch_present[(branch, field)] += int(is_present)
            branch_trusted[(branch, field)] += int(passed)
            if passed:
                sources.setdefault(field, set()).add(
                    "valuation" if field.startswith("valuation_") else "discovery"
                )
            else:
                missing[f"missing_trusted_{field}"] += 1

    missing_branches = _required_supported_branches(track) - {
        branch
        for branch, count in branch_counts.items()
        if count > 0
    }
    for branch in sorted(missing_branches):
        missing[f"missing_supported_branch_{branch}"] += 1
    provenance = {
        field: {
            "present": present[field],
            "trusted": trusted[field],
            "total": applicable[field],
            "ratio": (
                decimal_string(
                    bounded_ratio(trusted[field], applicable[field])
                )
                if applicable[field]
                else "0"
            ),
            "sources": sorted(sources.get(field, set())),
        }
        for field in sorted(applicable)
    }
    branch_coverage = {
        branch: {
            field: {
                "present": branch_present[(branch, field)],
                "trusted": branch_trusted[(branch, field)],
                "total": branch_applicable[(branch, field)],
                "ratio": (
                    decimal_string(
                        bounded_ratio(
                            branch_trusted[(branch, field)],
                            branch_applicable[(branch, field)],
                        )
                    )
                    if branch_applicable[(branch, field)]
                    else "0"
                ),
            }
            for candidate_branch, field in sorted(branch_applicable)
            if candidate_branch == branch
        }
        for branch in sorted(
            {candidate_branch for candidate_branch, _field in branch_applicable}
        )
    }
    source_coverage = _source_coverage(companies)
    for source, row in source_coverage.items():
        absent = int(row["total"]) - int(row["present"])
        if absent:
            missing[f"missing_trusted_source_{source}"] += absent
    ready = (
        supported_total > 0
        and not missing_branches
        and all(
            row["trusted"] > 0
            and Fraction(int(row["trusted"]), int(row["total"]))
            >= Fraction(threshold)
            for field, row in provenance.items()
            if not (track == "garp" and field in {"roic", "roe"})
        )
        and all(
            row["trusted"] > 0
            and Fraction(int(row["trusted"]), int(row["total"]))
            >= Fraction(threshold)
            for fields in branch_coverage.values()
            for field, row in fields.items()
            if not (track == "garp" and field in {"roic", "roe"})
        )
        and all(
            row["present"] > 0
            and Fraction(int(row["present"]), int(row["total"]))
            >= Fraction(threshold)
            for row in source_coverage.values()
        )
    )
    return_coverage = None
    if track == "garp":
        selected_ready = (
            return_companies["roic"] + return_companies["roe_fallback"]
        )
        ready = (
            ready
            and selected_ready > 0
            and supported_total > 0
            and Fraction(selected_ready, supported_total)
            >= Fraction(threshold)
        )
        return_coverage = {
            "policy": "roic_then_roe",
            "selected_labels": {
                key: {
                    "count": label_counts[key],
                    **{
                        coverage_key: value
                        for coverage_key, value in _coverage(
                            label_counts[key], supported_total
                        ).items()
                        if coverage_key != "present"
                    },
                }
                for key in ("roic", "roe_fallback", "insufficient")
            },
            "roic": {
                "companies": return_companies["roic"],
                "observations": return_observations["roic"],
                **{
                    key: value
                    for key, value in _coverage(
                        return_companies["roic"], supported_total
                    ).items()
                    if key != "present"
                },
            },
            "roe_fallback": {
                "companies": return_companies["roe_fallback"],
                "observations": return_observations["roe_fallback"],
                **{
                    key: value
                    for key, value in _coverage(
                        return_companies["roe_fallback"], supported_total
                    ).items()
                    if key != "present"
                },
            },
        }
    return (
        provenance,
        branch_coverage,
        source_coverage,
        dict(sorted(missing.items())),
        return_coverage,
        ready,
    )


def _domain_valid(track: str, reason: str | None) -> bool:
    if track == "quality_discount":
        return reason not in QUALITY_DOMAIN_INVALID_REASONS
    return not (
        reason == "annual_fiscal_year_duplicate"
        or (reason or "").endswith(("_negative", "_not_positive"))
    )


def _domain_readiness(
    valid: int,
    total: int,
    threshold: Decimal,
) -> tuple[bool, int]:
    invalid = total - valid
    return (
        valid > 0
        and total > 0
        and Fraction(valid, total) >= Fraction(threshold),
        invalid,
    )


def _hypotheses(
    track: str,
    *,
    industry_version: str,
    industries: Sequence[str | None],
    valuation_ratio_method: str,
) -> dict[str, dict[str, object]]:
    if track == "quality_discount":
        return {
            name: _quality_payload(
                thresholds,
                industry_version=industry_version,
                industries=industries,
                valuation_ratio_method=valuation_ratio_method,
            )
            for name, thresholds in HYPOTHESIS_BANDS.items()
        }
    if track == "deep_value":
        return {
            name: _deep_value_payload(
                thresholds,
                industry_version=industry_version,
                valuation_ratio_method=valuation_ratio_method,
            )
            for name, thresholds in DEEP_VALUE_BANDS.items()
        }
    return {
        name: _garp_payload(
            thresholds,
            industry_version=industry_version,
            valuation_ratio_method=valuation_ratio_method,
        )
        for name, thresholds in GARP_BANDS.items()
    }


def _classify(track: str, company: FrozenCompanyInput, payload: object) -> Any:
    assert isinstance(payload, Mapping)
    if track == "quality_discount":
        return classify(company, quality_discount_ruler(payload))
    if track == "deep_value":
        return classify_deep_value(company, deep_value_ruler(payload))
    return classify_garp(company, garp_ruler(payload))


def _ready_artifact(
    path: Path,
    preflight: Mapping[str, int],
    track: str,
) -> dict[str, object]:
    engine = create_engine(
        "sqlite://",
        creator=lambda: _read_only_connection(path),
        poolclass=StaticPool,
    )
    try:
        with Session(engine) as session:
            snapshot = build_production_snapshot(session)
    finally:
        engine.dispose()
    if (
        snapshot.fill_id != preflight["fill_id"]
        or len(snapshot.companies) != preflight["security_count"]
    ):
        return _blocked("snapshot_incomplete_or_corrupt", track=track)
    if snapshot.versions.algorithm_version != BUILD_VERSION:
        return _blocked("algorithm_build_mismatch", track=track)
    if len(snapshot.companies) > MAX_COMPANIES:
        return _blocked("universe_row_limit_exceeded", track=track)
    snapshot_bytes = 0
    for company in snapshot.companies:
        financial = company.fields.get("financial_history")
        valuation = company.fields.get("valuation_history")
        if (
            not isinstance(financial, Sequence)
            or not isinstance(valuation, Sequence)
            or len(financial) > MAX_FINANCIAL_ROWS
            or len(valuation) > MAX_VALUATION_ROWS
        ):
            return _blocked("snapshot_history_limit_exceeded", track=track)
        snapshot_bytes += len(
            canonical_json(
                {
                    "security_status": company.security_status,
                    "fields": company.fields,
                    "sources": company.sources,
                    "as_of": company.as_of,
                }
            ).encode("utf-8")
        )
        if snapshot_bytes > MAX_SNAPSHOT_BYTES:
            return _blocked("snapshot_byte_limit_exceeded", track=track)

    versions = sorted(
        {
            str(company.security_status.get("industry_version"))
            for company in snapshot.companies
            if company.security_status.get("industry_version")
        }
    )
    if len(versions) != 1:
        return _blocked("industry_version_not_unique", track=track)
    industries = [company.industry for company in snapshot.companies]
    method_distribution = _valuation_method_distribution(snapshot.companies)
    if (
        len(method_distribution) != 1
        or next(iter(method_distribution.values()))["companies"]
        != len(snapshot.companies)
    ):
        return _blocked(
            "valuation_ratio_method_not_homogeneous", track=track
        )
    valuation_ratio_method = next(iter(method_distribution))
    try:
        proposals = _hypotheses(
            track,
            industry_version=versions[0],
            industries=industries,
            valuation_ratio_method=valuation_ratio_method,
        )
    except ValueError:
        return _blocked("sector_branch_coverage_incomplete", track=track)
    if any(
        set(proposal["sector_branches"]["branch_policies"])
        != set(INDUSTRY_BRANCHES)
        for proposal in proposals.values()
    ):
        return _blocked("branch_policy_incomplete", track=track)

    frozen_companies = tuple(
        _classifier_input(company, snapshot)
        for company in snapshot.companies
    )
    verdicts = {
        name: tuple(
            _classify(track, company, payload)
            for company in frozen_companies
        )
        for name, payload in proposals.items()
    }
    base = verdicts["base"]
    corrupt_reasons = {
        "snapshot_schema_invalid",
        "financial_history_invalid",
        "valuation_history_invalid",
        "industry_version_mismatch",
    }
    if any(row.primary_reason in corrupt_reasons for row in base):
        return _blocked("snapshot_incomplete_or_corrupt", track=track)
    metric_values: dict[str, list[Decimal]] = {}
    coverage_counts: Counter[str] = Counter()
    for verdict in base:
        for key, value in verdict.metrics.items():
            parsed = _as_decimal(value)
            if parsed is not None:
                metric_values.setdefault(key, []).append(parsed)
                coverage_counts[key] += 1
    total = len(snapshot.companies)
    branch_counts = dict(
        sorted(
            Counter(
                _ruler_branch(track, company.industry)
                for company in snapshot.companies
            ).items()
        )
    )
    declared_fields = proposals["base"]["fields"]
    if not isinstance(declared_fields, Sequence):
        return _blocked("ruler_payload_incomplete", track=track)
    field_coverage = {
        key: _coverage(coverage_counts[key], total)
        for key in sorted(
            set(coverage_counts)
            | {
                str(field)
                for field in declared_fields
                if field not in {"code", "return_metric_label"}
            }
        )
    }
    domain_valid = sum(
        _domain_valid(track, verdict.primary_reason) for verdict in base
    )
    field_coverage["field_domain_valid"] = _coverage(domain_valid, total)
    source_keys = (
        "security",
        "financial",
        "valuation",
        "valuation_fetched_at",
        "fill",
    )
    if any(
        company.as_of.get(key) is None
        for company in snapshot.companies
        for key in source_keys
    ):
        return _blocked("source_as_of_incomplete", track=track)
    selected_labels = tuple(
        (
            str(row.metrics.get("return_metric_label"))
            if row.metrics.get("return_metric_label") is not None
            else None
        )
        for row in base
    )
    (
        provenance_coverage,
        branch_coverage,
        source_coverage,
        coverage_missing,
        return_metric_coverage,
        coverage_ready,
    ) = _coverage_audit(
        frozen_companies,
        track,
        proposals["base"],
        selected_labels,
    )
    readiness_threshold = Decimal(
        proposals["base"]["thresholds"]["readiness_min"]
    )
    domain_ready, domain_invalid = _domain_readiness(
        domain_valid,
        total,
        readiness_threshold,
    )
    if domain_invalid:
        coverage_missing = {
            **coverage_missing,
            "field_domain_invalid": domain_invalid,
        }
    coverage_ready = coverage_ready and domain_ready
    domain_policy = {
        "quality_discount": QUALITY_FIELD_DOMAIN_POLICY,
        "deep_value": DEEP_VALUE_FIELD_DOMAIN_POLICY,
        "garp": GARP_FIELD_DOMAIN_POLICY,
    }[track]
    bands = {
        "quality_discount": HYPOTHESIS_BANDS,
        "deep_value": DEEP_VALUE_BANDS,
        "garp": GARP_BANDS,
    }[track]
    artifact = {
        "schema_version": 1,
        "track": track,
        "status": "ready" if coverage_ready else "blocked",
        "approval_ready": coverage_ready,
        "blocked_reasons": (
            [] if coverage_ready else ["trusted_field_coverage_below_readiness"]
        ),
        "universe": {
            "fill_id": snapshot.fill_id,
            "total": total,
            "eligible": sum(
                verdict.primary_reason
                not in {"listing_history_insufficient", "listing_date_invalid"}
                for verdict in base
            ),
            "snapshot_bytes": snapshot_bytes,
            "scope": "latest_completed_full_market_fill",
            "branches": branch_counts,
        },
        "field_coverage": field_coverage,
        "provenance_coverage": provenance_coverage,
        "branch_coverage": branch_coverage,
        "source_coverage": source_coverage,
        "distributions": {
            "valuation_ratio_methods": method_distribution,
            "valid_valuation_months": {
                "pe": _quantiles(
                    metric_values.get("valuation_pe_observations", [])
                ),
                "pb": _quantiles(
                    metric_values.get("valuation_pb_observations", [])
                ),
            },
            "metrics": {
                key: _quantiles(values)
                for key, values in sorted(metric_values.items())
            },
            "missing_reasons": dict(
                sorted(
                    (
                        Counter(
                            verdict.primary_reason
                            for verdict in base
                            if verdict.verdict == "insufficient"
                            and verdict.primary_reason is not None
                        )
                        + Counter(coverage_missing)
                    ).items()
                )
            ),
        },
        "candidate_counts": {
            name: {
                **{
                    verdict: sum(row.verdict == verdict for row in rows)
                    for verdict in ("pass", "drop", "insufficient")
                },
                "branches": {
                    branch: {
                        verdict: sum(
                            row.verdict == verdict
                            and _ruler_branch(track, company.industry) == branch
                            for company, row in zip(
                                snapshot.companies, rows, strict=True
                            )
                        )
                        for verdict in ("pass", "drop", "insufficient")
                    }
                    for branch in branch_counts
                },
            }
            for name, rows in verdicts.items()
        },
        "source_as_of": {
            "request_snapshot_at": snapshot.snapshot_at.isoformat(),
            **{
                key: max(
                    (
                        str(company.as_of[key])
                        for company in snapshot.companies
                        if company.as_of.get(key) is not None
                    ),
                    default=None,
                )
                for key in source_keys
            },
        },
        "industry_versions": versions,
        "algorithm_build": snapshot.versions.algorithm_version,
        "field_domain_policy": domain_policy,
        "hypotheses": proposals,
        "hypothesis_labels": {
            name: "calibration hypothesis; never a runtime fallback"
            for name in bands
        },
        "proposed_readiness_threshold": bands["base"][
            "readiness_min"
        ],
        "return_metric_coverage": return_metric_coverage,
        "proposal": proposals["base"],
        "note": (
            (
                "Evidence-only proposal. other_income is used only as a "
                "trusted conservative non-core-income proxy; it is not a "
                "government-grant claim. "
            )
            if track == "deep_value"
            else "Evidence-only proposal. "
        )
        + (
            "No ruler was activated; activation requires explicit approval "
            "and a nonblank reason."
        ),
    }
    return artifact


def calibrate(
    db_path: Path | str,
    *,
    track: str,
) -> dict[str, object]:
    """Return deterministic evidence without initializing or writing the DB."""

    if track not in {"quality_discount", "deep_value", "garp"}:
        raise ValueError("unsupported discovery calibration track")
    path = Path(db_path)
    if not path.is_file():
        return _blocked("database_missing", track=track)
    try:
        with _read_only_connection(path) as connection:
            tables = _tables(connection)
            if not REQUIRED_TABLES <= tables:
                return _blocked("missing_schema", track=track)
            header = _latest_fill_header(connection)
            if header is None:
                return _blocked("no_completed_fill", track=track)
            _fill_id, status, snapshot_status, schema_version = header
            if schema_version != COMPANY_PAYLOAD_SCHEMA_VERSION:
                return _blocked(PAYLOAD_SCHEMA_BLOCK_REASON, track=track)
            if status != "done" or snapshot_status != "ready":
                return _blocked("no_completed_fill", track=track)
            preflight = _preflight_latest_fill(connection)
            if preflight is None:
                return _blocked("no_completed_fill", track=track)
            if _preflight_resource_limit(preflight):
                return _blocked("resource_limit", track=track)
            if _preflight_incomplete(preflight):
                return _blocked(
                    "snapshot_incomplete_or_corrupt", track=track
                )
    except sqlite3.DatabaseError:
        return _blocked("database_corrupt", track=track)
    try:
        return _ready_artifact(path, preflight, track)
    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
        return _blocked("snapshot_incomplete_or_corrupt", track=track)


def _paths_alias(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--track",
        required=True,
        choices=["quality_discount", "deep_value", "garp"],
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if _paths_alias(args.db, args.output):
        parser.error("--output must not alias --db")
    artifact = calibrate(args.db, track=args.track)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if artifact["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
