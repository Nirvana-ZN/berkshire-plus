"""Validated, immutable ruler payloads for independent discovery tracks."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from weakref import WeakKeyDictionary
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import RulerVersion
from discovery.builds import AVAILABLE_BUILDS, CANDIDATE_BUILD_VERSION, RETIRED_BUILDS
from discovery.industry import classify_industry_branch
from discovery.quality_contract import quality_ruler_branch_policies
from discovery.tracks import SCOPE_TRACKS, TRACKS, scope_for_track, track_for_scope
from ledger.positions import LOCAL_USER_ID

DISCOVERY_PAYLOAD_SCOPE = "weekly-discovery"
RULER_NOT_READY = "ruler_not_ready"
_release_sessions = WeakKeyDictionary()
DISCOVERY_DECIMAL_COMPONENT_LIMIT = 4096
# Bounds source text, coefficient digits, and exponent before any fixed-point
# formatting. Accepted values therefore format to at most roughly twice this.

JsonScalar: TypeAlias = None | bool | int | float | str
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[
    str, "FrozenJson"
]

_PAYLOAD_KEYS = frozenset(
    {
        "scope",
        "track",
        "fields",
        "formulas",
        "thresholds",
        "min_observations",
        "missing_policy",
        "sector_branches",
        "order",
        "industry_version",
        "algorithm_version",
    }
)
_DIRECTIONS = frozenset({"asc", "desc"})
_NULL_ORDERS = frozenset({"nulls_first", "nulls_last"})
QUALITY_DISCOUNT_FIELDS = (
    "listing_years",
    "roe_mean",
    "roe_downside_volatility",
    "roe_recent_ratio",
    "positive_net_income_observations",
    "latest_net_income",
    "latest_net_assets",
    "cash_conversion",
    "dilution_rate",
    "debt_to_equity",
    "cash_to_assets",
    "interest_debt_to_assets",
    "interest_coverage",
    "margin_change",
    "pe_percentile",
    "pe_median_ratio",
    "pb_percentile",
    "pb_median_ratio",
    "normalized_earnings",
    "normalized_pe",
    "earnings_yield",
    "deducted_profit_cagr",
    "peg",
    "bank_capital_adequacy",
    "bank_core_tier1_capital_adequacy",
    "bank_npl",
    "code",
)
QUALITY_DISCOUNT_ORDER = (
    ("pe_percentile", "asc", "nulls_last"),
    ("roe_downside_volatility", "asc", "nulls_last"),
    ("cash_conversion", "desc", "nulls_last"),
    ("code", "asc", "nulls_last"),
)
QUALITY_DISCOUNT_FORMULAS = MappingProxyType(
    {
        "roe_mean": "mean(annual.roe)",
        "roe_downside_volatility": "downside_volatility(annual.roe)",
        "roe_recent_ratio": "mean(recent.roe)/mean(annual.roe)",
        "positive_net_income_observations": (
            "count(annual.net_income>0)"
        ),
        "latest_net_income": "latest(annual.net_income)",
        "latest_net_assets": "latest(annual.net_assets)",
        "cash_conversion": (
            "sum(annual.ocf)/sum(annual.net_income)|annual.net_income>0"
        ),
        "dilution_rate": "latest(annual.shares)/earliest(annual.shares)-1",
        "debt_to_equity": (
            "latest(annual.interest_debt)/latest(annual.net_assets)"
        ),
        "cash_to_assets": "latest(annual.cash)/latest(annual.total_assets)",
        "interest_debt_to_assets": (
            "latest(annual.interest_debt)/latest(annual.total_assets)"
        ),
        "interest_coverage": "latest(annual.interest_coverage)",
        "margin_change": (
            "latest(annual.gross_margin)-earliest(annual.gross_margin)"
        ),
        "pe_percentile": "percentile_rank(current.pe,monthly.pe)",
        "pe_median_ratio": "current.pe/median(monthly.pe)",
        "pb_percentile": "percentile_rank(current.pb,monthly.pb)",
        "pb_median_ratio": "current.pb/median(monthly.pb)",
        "normalized_earnings": (
            "normalized_earnings(annual.deducted_net_income)"
        ),
        "normalized_pe": (
            "current.price*latest(annual.shares)/normalized_earnings"
        ),
        "earnings_yield": "100/current.pe",
        "deducted_profit_cagr": "cagr(annual.deducted_net_income,growth)",
        "peg": "current.pe/(min(deducted_profit_cagr,peg_growth_cap)*100)",
        "bank_capital_adequacy": "latest(annual.capital_adequacy)",
        "bank_core_tier1_capital_adequacy": (
            "latest(annual.core_tier1_capital_adequacy)"
        ),
        "bank_npl": "latest(annual.npl)",
    }
)
QUALITY_DISCOUNT_THRESHOLDS = frozenset(
    {
        "listing_years_min",
        "roe_mean_min",
        "roe_downside_volatility_max",
        "roe_recent_ratio_min",
        "cash_to_assets_min",
        "interest_debt_to_assets_min",
        "net_income_positive_min",
        "net_assets_positive_min",
        "cash_conversion_min",
        "dilution_rate_max",
        "debt_to_equity_max",
        "interest_coverage_min",
        "margin_deterioration_max",
        "pe_percentile_max",
        "pe_median_ratio_max",
        "pb_percentile_max",
        "pb_median_ratio_max",
        "normalized_pe_max",
        "normalized_earnings_min",
        "treasury_yield",
        "earnings_yield_spread_min",
        "peg_growth_cap",
        "peg_release_max",
        "bank_roe_min",
        "bank_capital_adequacy_min",
        "bank_core_tier1_min",
        "bank_npl_max",
        "readiness_min",
    }
)
QUALITY_DISCOUNT_MINIMUMS = frozenset(
    {
        "roe",
        "roe_recent",
        "positive_net_income",
        "net_assets",
        "cash_conversion",
        "dilution",
        "leverage",
        "interest_coverage",
        "margin",
        "valuation_pe",
        "valuation_pb",
        "normalized_earnings",
        "growth",
        "bank_roe",
        "bank_capital",
        "bank_npl",
    }
)
_QUALITY_BRANCH_KEYS = frozenset(
    {
        "common",
        "branch_policies",
        "industry_map",
        "nonfinancial",
        "cyclical",
        "bank",
    }
)
QUALITY_FIELD_DOMAIN_POLICY = "quality-domain-v1"
_QUALITY_COMMON_CONFIG_KEYS = frozenset(
    {
        "valuation_ratio_method",
        "field_domain_policy",
        "lookback_windows",
        "latest_annual_staleness_years",
    }
)
_QUALITY_LOOKBACK_KEYS = frozenset(
    {
        "roe",
        "roe_recent",
        "profitability",
        "cash_conversion",
        "dilution",
        "positive_equity",
        "leverage",
        "interest_coverage",
        "margin",
        "normalized_earnings",
        "growth",
        "bank_capital",
        "bank_npl",
    }
)
_QUALITY_BRANCH_CONFIG_KEYS = frozenset(
    {
        "gate_order",
        "valuation_policy",
        "margin_deterioration_policy",
        "latest_net_income_policy",
    }
)
_QUALITY_GATE_SETS = MappingProxyType({
    "nonfinancial": frozenset(
        {
            "listing_age",
            "roe_mean",
            "roe_stability",
            "roe_trend",
            "profitability",
            "positive_equity",
            "cash_conversion",
            "dilution",
            "leverage",
            "interest_coverage",
            "cash_debt",
            "margin",
            "valuation",
            "absolute_valuation",
        }
    ),
    "cyclical": frozenset(
        {
            "listing_age",
            "roe_mean",
            "roe_stability",
            "profitability",
            "positive_equity",
            "cash_conversion",
            "dilution",
            "leverage",
            "interest_coverage",
            "cash_debt",
            "margin",
            "normalized_earnings_positive",
            "cyclical_valuation",
        }
    ),
    "bank": frozenset(
        {
            "listing_age",
            "bank_roe",
            "positive_equity",
            "bank_capital",
            "bank_npl",
            "bank_valuation",
        }
    ),
})
DEEP_VALUE_FIELDS = (
    "listing_years",
    "normalized_earnings",
    "normalized_pe",
    "normalized_pe_bucket",
    "cumulative_fcf",
    "normalized_fcf",
    "net_debt_to_normalized_fcf",
    "cash_to_assets",
    "interest_debt_to_assets",
    "interest_coverage",
    "dilution_rate",
    "revenue_decline",
    "earnings_decline",
    "one_time_profit_share",
    "impairment_share",
    "non_core_income_proxy_share",
    "pb_percentile",
    "pb_median_ratio",
    "roe_mean",
    "npl_change",
    "bank_capital_adequacy",
    "bank_core_tier1_capital_adequacy",
    "code",
)
DEEP_VALUE_FORMULAS = MappingProxyType(
    {
        "normalized_earnings": "median(annual.deducted_net_income)",
        "normalized_pe": (
            "current.price*latest(annual.shares)/normalized_earnings"
        ),
        "normalized_pe_bucket": (
            "min(floor(normalized_pe*4/normalized_pe_max),3)"
        ),
        "cumulative_fcf": "sum(annual.ocf-annual.capex)",
        "normalized_fcf": "median(annual.ocf-annual.capex)",
        "net_debt_to_normalized_fcf": (
            "(latest(annual.interest_debt)-latest(annual.cash))/"
            "normalized_fcf"
        ),
        "cash_to_assets": "latest(annual.cash)/latest(annual.total_assets)",
        "interest_debt_to_assets": (
            "latest(annual.interest_debt)/latest(annual.total_assets)"
        ),
        "interest_coverage": "latest(annual.interest_coverage)",
        "dilution_rate": (
            "latest(annual.shares)/earliest(annual.shares)-1"
        ),
        "revenue_decline": (
            "max(0,(earliest(annual.revenue)-latest(annual.revenue))/"
            "earliest(annual.revenue))"
        ),
        "earnings_decline": (
            "max(0,(normalized_earnings-latest(annual.deducted_net_income))/"
            "normalized_earnings)"
        ),
        "one_time_profit_share": (
            "sum(max(0,annual.net_income-annual.deducted_net_income))/"
            "(copy_abs(normalized_earnings)*count(annual))"
        ),
        "impairment_share": (
            "sum(copy_abs(annual.impairment_loss))/"
            "(copy_abs(normalized_earnings)*count(annual))"
        ),
        "non_core_income_proxy_share": (
            "sum(copy_abs(annual.other_income)+"
            "copy_abs(annual.asset_disposal_gain))/"
            "(copy_abs(normalized_earnings)*count(annual))"
        ),
        "pb_percentile": "percentile_rank(current.pb,monthly.pb)",
        "pb_median_ratio": "current.pb/median(monthly.pb)",
        "roe_mean": "mean(annual.roe)",
        "npl_change": "latest(annual.npl)-earliest(annual.npl)",
        "bank_capital_adequacy": "latest(annual.capital_adequacy)",
        "bank_core_tier1_capital_adequacy": (
            "latest(annual.core_tier1_capital_adequacy)"
        ),
    }
)
DEEP_VALUE_ORDER = (
    ("normalized_pe_bucket", "asc", "nulls_last"),
    ("pb_percentile", "asc", "nulls_last"),
    ("net_debt_to_normalized_fcf", "asc", "nulls_last"),
    ("normalized_pe", "asc", "nulls_last"),
    ("code", "asc", "nulls_last"),
)
DEEP_VALUE_THRESHOLDS = frozenset(
    {
        "listing_years_min",
        "normalized_earnings_min",
        "normalized_pe_max",
        "cumulative_fcf_min",
        "normalized_fcf_min",
        "net_debt_to_normalized_fcf_max",
        "cash_to_assets_min",
        "interest_debt_to_assets_min",
        "interest_coverage_min",
        "dilution_rate_max",
        "revenue_decline_max",
        "earnings_decline_max",
        "one_time_profit_share_max",
        "impairment_share_max",
        "non_core_income_proxy_share_max",
        "pb_percentile_max",
        "pb_median_ratio_max",
        "bank_roe_min",
        "bank_npl_increase_max",
        "bank_capital_adequacy_min",
        "bank_core_tier1_min",
        "readiness_min",
    }
)
DEEP_VALUE_MINIMUMS = frozenset(
    {
        "normalized_earnings",
        "fcf",
        "net_debt",
        "interest_coverage",
        "dilution",
        "revenue",
        "one_time_profit",
        "impairment",
        "non_core_income_proxy",
        "valuation_pb",
        "bank_roe",
        "bank_npl",
        "bank_capital",
    }
)
DEEP_VALUE_FIELD_DOMAIN_POLICY = "deep-value-domain-v1"
_DEEP_COMMON_KEYS = frozenset(
    {
        "valuation_ratio_method",
        "field_domain_policy",
        "lookback_windows",
        "latest_annual_staleness_years",
    }
)
_DEEP_LOOKBACK_KEYS = frozenset(
    {
        "normalized_earnings",
        "fcf",
        "net_debt",
        "interest_coverage",
        "dilution",
        "revenue",
        "one_time_profit",
        "impairment",
        "non_core_income_proxy",
        "bank_roe",
        "bank_npl",
        "bank_capital",
    }
)
_DEEP_BRANCH_POLICIES = MappingProxyType(
    {
        "bank": "financial",
        "insurance": "unsupported",
        "other_financial": "unsupported",
        "nonfinancial": "nonfinancial",
        "unknown": "insufficient",
    }
)
_DEEP_BRANCH_KEYS = frozenset(
    {"common", "branch_policies", "nonfinancial", "financial"}
)
_DEEP_BRANCH_CONFIG_KEYS = frozenset({"gate_order", "evidence_fields"})
_DEEP_EVIDENCE_FIELDS = MappingProxyType(
    {
        "nonfinancial": (
            "normalized_pe",
            "cumulative_fcf",
            "net_debt_to_normalized_fcf",
            "interest_coverage",
        ),
        "financial": (
            "pb_percentile",
            "roe_mean",
            "npl_change",
            "bank_capital_adequacy",
        ),
    }
)
_DEEP_GATE_SETS = MappingProxyType(
    {
        "nonfinancial": frozenset(
            {
                "listing_age",
                "normalized_earnings",
                "normalized_valuation",
                "positive_cumulative_fcf",
                "net_debt",
                "interest_coverage",
                "cash_debt",
                "dilution",
                "revenue_decline",
                "earnings_decline",
                "one_time_profit",
                "impairment",
                "non_core_income_proxy",
            }
        ),
        "financial": frozenset(
            {
                "listing_age",
                "bank_valuation",
                "bank_roe",
                "bank_npl_trend",
                "bank_capital",
            }
        ),
    }
)
GARP_FIELDS = (
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
)
GARP_FORMULAS = MappingProxyType(
    {
        "revenue_cagr": "cagr(exact_annual.revenue,revenue_cagr_periods)",
        "deducted_profit_cagr": (
            "cagr(exact_annual.deducted_net_income,"
            "deducted_profit_cagr_periods)"
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
        "return_on_capital": (
            "mean(primary(annual.roic,fallback=annual.roe))"
        ),
        "return_metric_label": (
            "label(primary(annual.roic,fallback=annual.roe))"
        ),
        "cash_conversion": (
            "sum(annual.ocf)/sum(annual.net_income)|annual.net_income>0"
        ),
        "dilution_rate": (
            "latest(annual.shares)/earliest(annual.shares)-1"
        ),
        "interest_debt_increase_to_revenue": (
            "max(0,latest(annual.interest_debt)-"
            "earliest(annual.interest_debt))/latest(annual.revenue)"
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
)
GARP_ORDER = (
    ("deducted_profit_consistent_growth_periods", "desc", "nulls_last"),
    ("peg", "asc", "nulls_last"),
    ("return_on_capital", "desc", "nulls_last"),
    ("cash_conversion", "desc", "nulls_last"),
    ("code", "asc", "nulls_last"),
)
GARP_THRESHOLDS = frozenset(
    {
        "revenue_cagr_min",
        "deducted_profit_cagr_min",
        "revenue_period_growth_min",
        "deducted_profit_period_growth_min",
        "revenue_growth_slowdown_max",
        "deducted_profit_growth_slowdown_max",
        "return_on_capital_min",
        "cash_conversion_min",
        "dilution_rate_max",
        "interest_debt_increase_to_revenue_max",
        "goodwill_increase_to_revenue_max",
        "acquisition_cash_to_revenue_max",
        "gross_margin_deterioration_max",
        "net_margin_deterioration_max",
        "impairment_share_max",
        "non_core_income_proxy_share_max",
        "peg_growth_cap",
        "peg_max",
        "pe_percentile_max",
        "pe_max",
        "readiness_min",
    }
)
GARP_MINIMUMS = frozenset(
    {
        "revenue_growth",
        "deducted_profit_growth",
        "sustained_revenue_growth",
        "sustained_profit_growth",
        "latest_growth",
        "roic",
        "roe_fallback",
        "cash_conversion",
        "dilution",
        "growth_drivers",
        "margin",
        "quality_adjustments",
        "valuation_pe",
    }
)
GARP_FIELD_DOMAIN_POLICY = "garp-domain-v2"
_GARP_COMMON_KEYS = frozenset(
    {
        "valuation_ratio_method",
        "field_domain_policy",
        "annual_observations",
        "cagr_periods",
        "latest_annual_staleness_years",
    }
)
_GARP_WINDOW_KEYS = frozenset(
    {
        "growth",
        "return_on_capital",
        "cash_conversion",
        "dilution",
        "growth_drivers",
        "margin",
        "quality_adjustments",
    }
)
_GARP_BRANCH_POLICIES = MappingProxyType(
    {
        "bank": "unsupported",
        "insurance": "unsupported",
        "other_financial": "unsupported",
        "nonfinancial": "nonfinancial",
        "unknown": "insufficient",
    }
)
_GARP_GATE_SET = frozenset(
    {
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
    }
)


class RulerNotReadyError(ValueError):
    code = RULER_NOT_READY


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be a nonblank canonical string")
    return value


def _canonical_decimal(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a decimal string")
    if len(value) > DISCOVERY_DECIMAL_COMPONENT_LIMIT:
        raise ValueError(f"{label} exceeds the decimal component limit")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a decimal string") from error
    if not amount.is_finite():
        raise ValueError(f"{label} must be finite")
    digits = amount.as_tuple().digits
    exponent = amount.as_tuple().exponent
    if (
        not isinstance(exponent, int)
        or len(digits) > DISCOVERY_DECIMAL_COMPONENT_LIMIT
        or abs(exponent) > DISCOVERY_DECIMAL_COMPONENT_LIMIT
        or abs(amount.adjusted()) > DISCOVERY_DECIMAL_COMPONENT_LIMIT
    ):
        raise ValueError(f"{label} exceeds the decimal component limit")
    if amount == 0:
        canonical = "0"
    else:
        canonical = format(amount, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
    if value != canonical:
        raise ValueError(f"{label} must be canonical: {canonical}")
    return value


def _reject_aliases_and_cycles(
    value: object,
    *,
    active: set[int],
    seen: set[int],
) -> None:
    if type(value) in {type(None), bool, int, str}:
        return
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("ruler payload numbers must be finite")
        return
    if not isinstance(value, Mapping) and type(value) not in {list, tuple}:
        raise TypeError(
            f"ruler payload contains unsupported {type(value).__name__}"
        )
    identity = id(value)
    if identity in active:
        raise TypeError("ruler payload must not contain a cycle")
    if identity in seen:
        raise TypeError("ruler payload must not contain mutable aliases")
    seen.add(identity)
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("ruler payload object keys must be strings")
                _reject_aliases_and_cycles(item, active=active, seen=seen)
        else:
            for item in value:
                _reject_aliases_and_cycles(item, active=active, seen=seen)
    finally:
        active.remove(identity)


def _freeze_json(value: object) -> FrozenJson:
    if type(value) in {type(None), bool, int, float, str}:
        return value  # type: ignore[return-value]
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if type(value) in {list, tuple}:
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"unsupported ruler value: {type(value).__name__}")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _mapping(
    value: object,
    label: str,
    *,
    nonempty: bool = True,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or (nonempty and not value):
        raise ValueError(f"{label} must be a non-empty object")
    if any(type(key) is not str for key in value):
        raise TypeError(f"{label} keys must be strings")
    return value


def _string_mapping(value: object, label: str) -> Mapping[str, str]:
    source = _mapping(value, label)
    result: dict[str, str] = {}
    for key, item in source.items():
        result[_text(key, f"{label} key")] = _text(item, f"{label}.{key}")
    return MappingProxyType(result)


def _scope(value: str) -> str:
    if value in TRACKS:
        return scope_for_track(value)
    if value in SCOPE_TRACKS:
        return value
    raise ValueError(f"unknown discovery scope: {value}")


@dataclass(frozen=True)
class TrackRuler:
    """One complete track ruler; ``scope`` is the database ledger scope."""

    scope: str
    fields: tuple[str, ...]
    formulas: Mapping[str, str]
    thresholds: Mapping[str, str]
    min_observations: Mapping[str, int]
    missing_policy: Mapping[str, str]
    sector_branches: Mapping[str, FrozenJson]
    order: tuple[tuple[str, str, str], ...]
    industry_version: str
    algorithm_version: str
    ranking_contract: Mapping[str, object] = field(default_factory=dict)
    operating_policy: Mapping[str, object] = field(default_factory=dict)
    metric_contract: Mapping[str, object] = field(default_factory=dict)
    _allow_retired_algorithm: InitVar[bool] = False

    def __post_init__(self, _allow_retired_algorithm: bool) -> None:
        if type(_allow_retired_algorithm) is not bool:
            raise TypeError("allow_retired_algorithm must be a boolean")
        scope = _scope(self.scope)
        fields = tuple(self.fields)
        if not fields:
            raise ValueError("fields must not be empty")
        if any(
            type(field) is not str
            or not field
            or field != field.strip()
            for field in fields
        ):
            raise ValueError("fields must contain nonblank canonical strings")
        if len(fields) != len(set(fields)):
            raise ValueError("fields must not contain duplicates")

        formulas = _string_mapping(self.formulas, "formulas")
        threshold_source = _mapping(self.thresholds, "thresholds")
        thresholds = MappingProxyType(
            {
                _text(key, "threshold key"): _canonical_decimal(
                    value, f"thresholds.{key}"
                )
                for key, value in threshold_source.items()
            }
        )
        observation_source = _mapping(
            self.min_observations, "min_observations"
        )
        observations: dict[str, int] = {}
        for key, value in observation_source.items():
            name = _text(key, "min_observations key")
            if type(value) is not int or value < 1:
                raise ValueError(
                    f"min_observations.{name} must be a positive integer"
                )
            observations[name] = value

        policies = _string_mapping(self.missing_policy, "missing_policy")
        if set(policies) != set(fields):
            raise ValueError("missing_policy must cover every field exactly")

        _reject_aliases_and_cycles(
            self.sector_branches,
            active=set(),
            seen=set(),
        )
        branches = _mapping(self.sector_branches, "sector_branches")
        branch_values: dict[str, FrozenJson] = {}
        for key, value in branches.items():
            name = _text(key, "sector branch")
            if not isinstance(value, Mapping) or not value:
                raise ValueError(
                    f"sector_branches.{name} must be a non-empty object"
                )
            branch_values[name] = _freeze_json(value)

        if isinstance(self.order, (str, bytes)) or not isinstance(
            self.order, Sequence
        ):
            raise TypeError("order must be a sequence of triples")
        order: list[tuple[str, str, str]] = []
        for index, item in enumerate(self.order):
            if (
                isinstance(item, (str, bytes))
                or not isinstance(item, Sequence)
                or len(item) != 3
            ):
                raise ValueError(f"order[{index}] must be a triple")
            field = _text(item[0], f"order[{index}] field")
            direction = _text(item[1], f"order[{index}] direction")
            null_order = _text(item[2], f"order[{index}] null ordering")
            if field not in fields and field != "code":
                raise ValueError(f"order field is not declared: {field}")
            if direction not in _DIRECTIONS:
                raise ValueError(f"invalid order direction: {direction}")
            if null_order not in _NULL_ORDERS:
                raise ValueError(f"invalid null ordering: {null_order}")
            order.append((field, direction, null_order))
        if not order or len({item[0] for item in order}) != len(order):
            raise ValueError("order must contain unique fields")
        if order[-1] != ("code", "asc", "nulls_last"):
            raise ValueError(
                "order must end with code ascending and nulls_last"
            )

        industry_version = _text(
            self.industry_version, "industry_version"
        )
        algorithm_version = _text(
            self.algorithm_version, "algorithm_version"
        )
        if (
            not _allow_retired_algorithm
            and algorithm_version not in (AVAILABLE_BUILDS | {CANDIDATE_BUILD_VERSION})
        ):
            raise ValueError(
                f"algorithm build is unavailable: {algorithm_version}"
            )

        for name in ('ranking_contract', 'operating_policy', 'metric_contract'):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f'{name} must be an object')
            _reject_aliases_and_cycles(value, active=set(), seen=set())
            if algorithm_version != CANDIDATE_BUILD_VERSION and value:
                raise ValueError('selection contracts require the candidate build')
            object.__setattr__(self, name, _freeze_json(value))
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "formulas", formulas)
        object.__setattr__(self, "thresholds", thresholds)
        object.__setattr__(
            self, "min_observations", MappingProxyType(observations)
        )
        object.__setattr__(self, "missing_policy", policies)
        object.__setattr__(
            self, "sector_branches", MappingProxyType(branch_values)
        )
        object.__setattr__(self, "order", tuple(order))
        object.__setattr__(self, "industry_version", industry_version)
        object.__setattr__(self, "algorithm_version", algorithm_version)

    @property
    def track(self) -> str:
        return track_for_scope(self.scope)

    def payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "scope": DISCOVERY_PAYLOAD_SCOPE,
                "track": self.track,
                "fields": self.fields,
                "formulas": self.formulas,
                "thresholds": self.thresholds,
                "min_observations": self.min_observations,
                "missing_policy": self.missing_policy,
                "sector_branches": self.sector_branches,
                "order": self.order,
                "industry_version": self.industry_version,
                "algorithm_version": self.algorithm_version,
                **({'ranking_contract': self.ranking_contract,
                    'operating_policy': self.operating_policy,
                    'metric_contract': self.metric_contract}
                   if self.algorithm_version == CANDIDATE_BUILD_VERSION else {}),
            }
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        allow_retired_algorithm: bool = False,
    ) -> "TrackRuler":
        if not isinstance(payload, Mapping):
            raise TypeError("ruler payload must be an object")
        _reject_aliases_and_cycles(payload, active=set(), seen=set())
        keys = _PAYLOAD_KEYS | ({'ranking_contract', 'operating_policy', 'metric_contract'}
                                if payload.get('algorithm_version') == CANDIDATE_BUILD_VERSION else set())
        if set(payload) != keys:
            missing = sorted(keys - set(payload))
            extra = sorted(set(payload) - keys)
            raise ValueError(
                f"ruler payload keys mismatch; missing={missing}, extra={extra}"
            )
        if payload["scope"] != DISCOVERY_PAYLOAD_SCOPE:
            raise ValueError("discovery payload scope is incompatible")
        track = payload["track"]
        if type(track) is not str or track not in TRACKS:
            raise ValueError("discovery payload track is invalid")
        fields_value = payload["fields"]
        if isinstance(fields_value, (str, bytes)) or not isinstance(
            fields_value, Sequence
        ):
            raise TypeError("fields must be a sequence")
        return cls(
            scope=scope_for_track(track),
            fields=tuple(fields_value),
            formulas=payload["formulas"],  # type: ignore[arg-type]
            thresholds=payload["thresholds"],  # type: ignore[arg-type]
            min_observations=payload["min_observations"],  # type: ignore[arg-type]
            missing_policy=payload["missing_policy"],  # type: ignore[arg-type]
            sector_branches=payload["sector_branches"],  # type: ignore[arg-type]
            order=payload["order"],  # type: ignore[arg-type]
            industry_version=payload["industry_version"],  # type: ignore[arg-type]
            algorithm_version=payload["algorithm_version"],  # type: ignore[arg-type]
            ranking_contract=payload.get('ranking_contract', {}),
            operating_policy=payload.get('operating_policy', {}),
            metric_contract=payload.get('metric_contract', {}),
            _allow_retired_algorithm=allow_retired_algorithm,
        )

    @classmethod
    def from_json(
        cls,
        payload_json: str,
        *,
        allow_retired_algorithm: bool = False,
    ) -> "TrackRuler":
        if type(payload_json) is not str:
            raise TypeError("ruler payload JSON must be text")

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON number: {value}")

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        try:
            value = json.loads(
                payload_json,
                parse_constant=reject_constant,
                object_pairs_hook=unique_object,
            )
        except json.JSONDecodeError as error:
            raise ValueError("ruler payload is not valid JSON") from error
        if type(value) is not dict:
            raise TypeError("ruler payload must be a JSON object")
        return cls.from_payload(
            value,
            allow_retired_algorithm=allow_retired_algorithm,
        )


def parse_payload(value: TrackRuler | Mapping[str, object] | str) -> TrackRuler:
    if isinstance(value, TrackRuler):
        if value.algorithm_version not in (AVAILABLE_BUILDS | {CANDIDATE_BUILD_VERSION}):
            raise ValueError(
                f"algorithm build is unavailable: {value.algorithm_version}"
            )
        return value
    if isinstance(value, str):
        return TrackRuler.from_json(value)
    return TrackRuler.from_payload(value)


def validate_quality_discount_ruler(ruler: TrackRuler) -> TrackRuler:
    """Require the complete, explicitly supported quality ruler schema."""

    if isinstance(ruler, TrackRuler) and ruler.algorithm_version == CANDIDATE_BUILD_VERSION:
        return _validate_candidate(ruler, expected_track="quality_discount")
    if not isinstance(ruler, TrackRuler) or ruler.track != "quality_discount":
        raise ValueError("quality discount ruler scope is required")
    if ruler.fields != QUALITY_DISCOUNT_FIELDS:
        raise ValueError("quality discount fields do not match the supported schema")
    if dict(ruler.formulas) != dict(QUALITY_DISCOUNT_FORMULAS):
        raise ValueError("quality discount formulas do not match the supported schema")
    if ruler.order != QUALITY_DISCOUNT_ORDER:
        raise ValueError("quality discount order does not match the supported schema")
    if set(ruler.thresholds) != QUALITY_DISCOUNT_THRESHOLDS:
        raise ValueError("quality discount thresholds must be complete and exact")
    if set(ruler.min_observations) != QUALITY_DISCOUNT_MINIMUMS:
        raise ValueError(
            "quality discount observation minimums must be complete and exact"
        )
    if set(ruler.missing_policy) != set(QUALITY_DISCOUNT_FIELDS) or any(
        policy not in {"insufficient", "skip"}
        for policy in ruler.missing_policy.values()
    ):
        raise ValueError("quality discount missing policies are invalid")
    if ruler.missing_policy["code"] != "insufficient":
        raise ValueError("quality discount code policy must be insufficient")

    branches = ruler.sector_branches
    if set(branches) != _QUALITY_BRANCH_KEYS:
        raise ValueError("quality discount sector branches must be complete and exact")
    common = branches["common"]
    if (
        not isinstance(common, Mapping)
        or set(common) != _QUALITY_COMMON_CONFIG_KEYS
        or type(common["valuation_ratio_method"]) is not str
        or not common["valuation_ratio_method"]
        or common["valuation_ratio_method"]
        != common["valuation_ratio_method"].strip()
        or common["field_domain_policy"] != QUALITY_FIELD_DOMAIN_POLICY
    ):
        raise ValueError("quality discount common config is invalid")
    windows = common["lookback_windows"]
    if (
        not isinstance(windows, Mapping)
        or set(windows) != _QUALITY_LOOKBACK_KEYS
        or any(
            type(value) is not int or value <= 0 or value > 128
            for value in windows.values()
        )
        or type(common["latest_annual_staleness_years"]) is not int
        or common["latest_annual_staleness_years"] < 0
        or common["latest_annual_staleness_years"] > 128
    ):
        raise ValueError("quality discount calendar lookbacks are invalid")
    policies = branches["branch_policies"]
    if (
        not isinstance(policies, Mapping)
        or dict(policies)
        != dict(quality_ruler_branch_policies(ruler.algorithm_version))
    ):
        raise ValueError("quality discount branch policies are invalid")
    industry_map = branches["industry_map"]
    if not isinstance(industry_map, Mapping) or set(industry_map) != {
        "nonfinancial",
        "cyclical",
        "bank",
    }:
        raise ValueError("quality discount industry map is invalid")
    seen_industries: set[str] = set()
    for branch in ("nonfinancial", "cyclical", "bank"):
        industries = industry_map[branch]
        if (
            isinstance(industries, (str, bytes))
            or not isinstance(industries, Sequence)
            or not industries
            or any(
                type(industry) is not str
                or not industry
                or industry != industry.strip()
                for industry in industries
            )
            or len(set(industries)) != len(industries)
            or seen_industries.intersection(industries)
        ):
            raise ValueError(f"quality discount {branch} industries are invalid")
        expected_shared_branch = (
            "bank" if branch == "bank" else "nonfinancial"
        )
        if any(
            classify_industry_branch(industry) != expected_shared_branch
            for industry in industries
        ):
            raise ValueError(
                f"quality discount {branch} industries cross branch policy"
            )
        seen_industries.update(industries)

        config = branches[branch]
        if not isinstance(config, Mapping) or set(config) != _QUALITY_BRANCH_CONFIG_KEYS:
            raise ValueError(f"quality discount {branch} branch is invalid")
        gate_order = config["gate_order"]
        expected_gates = _QUALITY_GATE_SETS[branch]
        if (
            isinstance(gate_order, (str, bytes))
            or not isinstance(gate_order, Sequence)
            or len(gate_order) != len(expected_gates)
            or set(gate_order) != expected_gates
            or any(type(gate) is not str for gate in gate_order)
        ):
            raise ValueError(f"quality discount {branch} gate order is invalid")
        if config["valuation_policy"] not in {"both", "either", "pe", "pb"}:
            raise ValueError(f"quality discount {branch} valuation policy is invalid")
        if branch == "bank" and config["valuation_policy"] != "pb":
            raise ValueError("quality discount bank valuation policy must be pb")
        if config["margin_deterioration_policy"] not in {"evidence", "drop"}:
            raise ValueError(f"quality discount {branch} margin policy is invalid")
        if config["latest_net_income_policy"] not in {
            "require_positive",
            "ignore",
        }:
            raise ValueError(
                f"quality discount {branch} latest income policy is invalid"
            )
        if branch == "bank":
            if config["latest_net_income_policy"] != "ignore":
                raise ValueError(
                    "quality discount bank latest income policy must be ignore"
                )
            if any(
                gate_order.index("positive_equity")
                > gate_order.index(gate)
                for gate in (
                    "bank_roe",
                    "bank_capital",
                    "bank_npl",
                    "bank_valuation",
                )
            ):
                raise ValueError(
                    "quality discount bank equity gate order is invalid"
                )
        elif (
            gate_order.index("profitability")
            > gate_order.index("positive_equity")
            or gate_order.index("positive_equity")
            > gate_order.index("cash_conversion")
            or gate_order.index("positive_equity")
            > gate_order.index("leverage")
        ):
            raise ValueError(
                f"quality discount {branch} sign gate order is invalid"
            )

    parsed_thresholds = {
        key: Decimal(value) for key, value in ruler.thresholds.items()
    }
    if any(value < 0 for value in parsed_thresholds.values()):
        raise ValueError("quality discount thresholds cannot be negative")
    readiness = parsed_thresholds["readiness_min"]
    if readiness < 0 or readiness > 1:
        raise ValueError("quality discount readiness threshold must be within [0, 1]")
    if any(
        parsed_thresholds[key] > 100
        for key in ("pe_percentile_max", "pb_percentile_max")
    ):
        raise ValueError("quality discount percentile thresholds exceed 100")
    if any(
        minimum > (240 if key.startswith("valuation_") else 128)
        for key, minimum in ruler.min_observations.items()
    ):
        raise ValueError("quality discount observation minimum exceeds work bounds")
    observation_windows = {
        "roe": "roe",
        "roe_recent": "roe_recent",
        "bank_roe": "roe",
        "positive_net_income": "profitability",
        "net_assets": "positive_equity",
        "cash_conversion": "cash_conversion",
        "dilution": "dilution",
        "leverage": "leverage",
        "interest_coverage": "interest_coverage",
        "margin": "margin",
        "normalized_earnings": "normalized_earnings",
        "growth": "growth",
        "bank_capital": "bank_capital",
        "bank_npl": "bank_npl",
    }
    if any(
        ruler.min_observations[minimum] > windows[window]
        for minimum, window in observation_windows.items()
    ):
        raise ValueError(
            "quality discount observation minimum exceeds its calendar window"
        )
    if ruler.min_observations["growth"] < 2:
        raise ValueError("quality discount growth needs two endpoints")
    if parsed_thresholds["peg_growth_cap"] <= 0:
        raise ValueError("quality discount PEG growth cap must be positive")
    if parsed_thresholds["normalized_earnings_min"] != 0:
        raise ValueError(
            "quality discount normalized earnings threshold must be exactly zero"
        )
    if (
        parsed_thresholds["net_income_positive_min"] != 0
        or parsed_thresholds["net_assets_positive_min"] != 0
    ):
        raise ValueError(
            "quality discount profitability/equity thresholds must be zero"
        )
    return ruler


def quality_discount_ruler(
    value: TrackRuler | Mapping[str, object] | str,
) -> TrackRuler:
    """Build and semantically validate one supplied quality ruler."""

    return validate_quality_discount_ruler(parse_payload(value))


def validate_deep_value_ruler(ruler: TrackRuler) -> TrackRuler:
    """Require the complete deep-value schema implemented by this build."""

    if isinstance(ruler, TrackRuler) and ruler.algorithm_version == CANDIDATE_BUILD_VERSION:
        return _validate_candidate(ruler, expected_track="deep_value")
    if not isinstance(ruler, TrackRuler) or ruler.track != "deep_value":
        raise ValueError("deep value ruler scope is required")
    if ruler.fields != DEEP_VALUE_FIELDS:
        raise ValueError("deep value fields do not match the supported schema")
    if dict(ruler.formulas) != dict(DEEP_VALUE_FORMULAS):
        raise ValueError("deep value formulas do not match the supported schema")
    if ruler.order != DEEP_VALUE_ORDER:
        raise ValueError("deep value order does not match the supported schema")
    if set(ruler.thresholds) != DEEP_VALUE_THRESHOLDS:
        raise ValueError("deep value thresholds must be complete and exact")
    if set(ruler.min_observations) != DEEP_VALUE_MINIMUMS:
        raise ValueError(
            "deep value observation minimums must be complete and exact"
        )
    if set(ruler.missing_policy) != set(DEEP_VALUE_FIELDS) or any(
        policy != "insufficient"
        for policy in ruler.missing_policy.values()
    ):
        raise ValueError("deep value missing policies must all be insufficient")

    branches = ruler.sector_branches
    if set(branches) != _DEEP_BRANCH_KEYS:
        raise ValueError("deep value sector branches must be complete and exact")
    common = branches["common"]
    if (
        not isinstance(common, Mapping)
        or set(common) != _DEEP_COMMON_KEYS
        or common["field_domain_policy"] != DEEP_VALUE_FIELD_DOMAIN_POLICY
        or type(common["valuation_ratio_method"]) is not str
        or not common["valuation_ratio_method"]
        or common["valuation_ratio_method"]
        != common["valuation_ratio_method"].strip()
    ):
        raise ValueError("deep value common config is invalid")
    windows = common["lookback_windows"]
    if (
        not isinstance(windows, Mapping)
        or set(windows) != _DEEP_LOOKBACK_KEYS
        or any(
            type(value) is not int or value <= 0 or value > 128
            for value in windows.values()
        )
        or type(common["latest_annual_staleness_years"]) is not int
        or common["latest_annual_staleness_years"] < 0
        or common["latest_annual_staleness_years"] > 128
    ):
        raise ValueError("deep value calendar lookbacks are invalid")
    if (
        not isinstance(branches["branch_policies"], Mapping)
        or dict(branches["branch_policies"]) != dict(_DEEP_BRANCH_POLICIES)
    ):
        raise ValueError("deep value branch policies are invalid")
    for branch in ("nonfinancial", "financial"):
        config = branches[branch]
        expected_gates = _DEEP_GATE_SETS[branch]
        if (
            not isinstance(config, Mapping)
            or set(config) != _DEEP_BRANCH_CONFIG_KEYS
        ):
            raise ValueError(f"deep value {branch} branch is invalid")
        gates = config["gate_order"]
        evidence_fields = config["evidence_fields"]
        if (
            isinstance(gates, (str, bytes))
            or not isinstance(gates, Sequence)
            or len(gates) != len(expected_gates)
            or set(gates) != expected_gates
            or any(type(gate) is not str for gate in gates)
            or isinstance(evidence_fields, (str, bytes))
            or not isinstance(evidence_fields, Sequence)
            or tuple(evidence_fields) != _DEEP_EVIDENCE_FIELDS[branch]
        ):
            raise ValueError(f"deep value {branch} policy is invalid")

    thresholds = {
        key: Decimal(value) for key, value in ruler.thresholds.items()
    }
    if any(value < 0 for value in thresholds.values()):
        raise ValueError("deep value thresholds cannot be negative")
    if thresholds["readiness_min"] > 1:
        raise ValueError("deep value readiness threshold exceeds one")
    if thresholds["pb_percentile_max"] > 100:
        raise ValueError("deep value percentile threshold exceeds 100")
    if thresholds["normalized_pe_max"] <= 0:
        raise ValueError("deep value normalized PE maximum must be positive")
    for key in (
        "normalized_earnings_min",
        "cumulative_fcf_min",
        "normalized_fcf_min",
    ):
        if thresholds[key] != 0:
            raise ValueError(f"deep value {key} must be exactly zero")
    observation_windows = {
        "normalized_earnings": "normalized_earnings",
        "fcf": "fcf",
        "net_debt": "net_debt",
        "interest_coverage": "interest_coverage",
        "dilution": "dilution",
        "revenue": "revenue",
        "one_time_profit": "one_time_profit",
        "impairment": "impairment",
        "non_core_income_proxy": "non_core_income_proxy",
        "bank_roe": "bank_roe",
        "bank_npl": "bank_npl",
        "bank_capital": "bank_capital",
    }
    if any(
        ruler.min_observations[minimum] > windows[window]
        for minimum, window in observation_windows.items()
    ):
        raise ValueError(
            "deep value observation minimum exceeds its calendar window"
        )
    if ruler.min_observations["valuation_pb"] > 240:
        raise ValueError("deep value valuation minimum exceeds work bounds")
    return ruler


def deep_value_ruler(
    value: TrackRuler | Mapping[str, object] | str,
) -> TrackRuler:
    """Build and semantically validate one supplied deep-value ruler."""

    return validate_deep_value_ruler(parse_payload(value))


def validate_garp_ruler(ruler: TrackRuler) -> TrackRuler:
    """Require the exact GARP contract implemented by this build."""

    if isinstance(ruler, TrackRuler) and ruler.algorithm_version == CANDIDATE_BUILD_VERSION:
        return _validate_candidate(ruler, expected_track="garp")
    if not isinstance(ruler, TrackRuler) or ruler.track != "garp":
        raise ValueError("GARP ruler scope is required")
    if ruler.fields != GARP_FIELDS:
        raise ValueError("GARP fields do not match the supported schema")
    if dict(ruler.formulas) != dict(GARP_FORMULAS):
        raise ValueError("GARP formulas do not match the supported schema")
    if ruler.order != GARP_ORDER:
        raise ValueError("GARP order does not match the supported schema")
    if set(ruler.thresholds) != GARP_THRESHOLDS:
        raise ValueError("GARP thresholds must be complete and exact")
    if set(ruler.min_observations) != GARP_MINIMUMS:
        raise ValueError("GARP observation minimums must be complete and exact")
    if set(ruler.missing_policy) != set(GARP_FIELDS) or any(
        policy != "insufficient"
        for policy in ruler.missing_policy.values()
    ):
        raise ValueError("GARP missing policies must all be insufficient")

    branches = ruler.sector_branches
    if set(branches) != {"common", "branch_policies", "nonfinancial"}:
        raise ValueError("GARP sector branches must be complete and exact")
    common = branches["common"]
    if (
        not isinstance(common, Mapping)
        or set(common) != _GARP_COMMON_KEYS
        or common["field_domain_policy"] != GARP_FIELD_DOMAIN_POLICY
        or type(common["valuation_ratio_method"]) is not str
        or not common["valuation_ratio_method"]
        or common["valuation_ratio_method"]
        != common["valuation_ratio_method"].strip()
    ):
        raise ValueError("GARP common config is invalid")
    windows = common["annual_observations"]
    cagr_periods = common["cagr_periods"]
    if (
        not isinstance(windows, Mapping)
        or set(windows) != _GARP_WINDOW_KEYS
        or any(
            type(value) is not int or value <= 0 or value > 128
            for value in windows.values()
        )
        or not isinstance(cagr_periods, Mapping)
        or set(cagr_periods) != {"revenue", "deducted_profit"}
        or any(
            type(value) is not int or value <= 0 or value > 127
            for value in cagr_periods.values()
        )
        or cagr_periods["revenue"] != windows["growth"] - 1
        or cagr_periods["deducted_profit"] != windows["growth"] - 1
        or type(common["latest_annual_staleness_years"]) is not int
        or common["latest_annual_staleness_years"] < 0
        or common["latest_annual_staleness_years"] > 128
    ):
        raise ValueError("GARP exact annual windows are invalid")
    if (
        not isinstance(branches["branch_policies"], Mapping)
        or dict(branches["branch_policies"]) != dict(_GARP_BRANCH_POLICIES)
    ):
        raise ValueError("GARP branch policies are invalid")
    config = branches["nonfinancial"]
    if (
        not isinstance(config, Mapping)
        or set(config)
        != {
            "gate_order",
            "return_metric_policy",
            "evidence_fields",
            "excluded_industries",
        }
        or config["return_metric_policy"] != "roic_then_roe"
        or tuple(config["evidence_fields"])
        != (
            "revenue_cagr",
            "deducted_profit_cagr",
            "return_on_capital",
            "peg",
        )
    ):
        raise ValueError("GARP nonfinancial branch policy is invalid")
    excluded = config["excluded_industries"]
    if (
        isinstance(excluded, (str, bytes))
        or not isinstance(excluded, Sequence)
        or any(
            type(label) is not str or not label or label != label.strip()
            for label in excluded
        )
        or list(excluded) != sorted(set(excluded))
    ):
        raise ValueError("GARP excluded industries must be sorted unique labels")
    gates = config["gate_order"]
    if (
        isinstance(gates, (str, bytes))
        or not isinstance(gates, Sequence)
        or len(gates) != len(_GARP_GATE_SET)
        or set(gates) != _GARP_GATE_SET
        or any(type(gate) is not str for gate in gates)
    ):
        raise ValueError("GARP gate order is invalid")

    thresholds = {
        key: Decimal(value) for key, value in ruler.thresholds.items()
    }
    if any(value < 0 for value in thresholds.values()):
        raise ValueError("GARP thresholds cannot be negative")
    if thresholds["readiness_min"] > 1:
        raise ValueError("GARP readiness threshold exceeds one")
    if thresholds["pe_percentile_max"] > 100:
        raise ValueError("GARP percentile threshold exceeds 100")
    if thresholds["peg_growth_cap"] <= 0:
        raise ValueError("GARP PEG growth cap must be positive")
    if any(
        thresholds[key] <= 0 or thresholds[key] > 1
        for key in (
            "revenue_period_growth_min",
            "deducted_profit_period_growth_min",
        )
    ):
        raise ValueError(
            "GARP sustained-growth period minima must be within (0, 1]"
        )
    observation_windows = {
        "revenue_growth": "growth",
        "deducted_profit_growth": "growth",
        "latest_growth": "growth",
        "roic": "return_on_capital",
        "roe_fallback": "return_on_capital",
        "cash_conversion": "cash_conversion",
        "dilution": "dilution",
        "growth_drivers": "growth_drivers",
        "margin": "margin",
        "quality_adjustments": "quality_adjustments",
    }
    if any(
        ruler.min_observations[minimum] > windows[window]
        for minimum, window in observation_windows.items()
    ):
        raise ValueError("GARP observation minimum exceeds its annual window")
    if (
        ruler.min_observations["revenue_growth"] != windows["growth"]
        or ruler.min_observations["deducted_profit_growth"]
        != windows["growth"]
        or ruler.min_observations["latest_growth"] != 2
        or ruler.min_observations["growth_drivers"]
        != windows["growth_drivers"]
        or not (
            2
            <= ruler.min_observations["sustained_revenue_growth"]
            <= cagr_periods["revenue"]
        )
        or not (
            2
            <= ruler.min_observations["sustained_profit_growth"]
            <= cagr_periods["deducted_profit"]
        )
        or ruler.min_observations["valuation_pe"] > 240
    ):
        raise ValueError("GARP exact observation policy is invalid")
    return ruler


def garp_ruler(
    value: TrackRuler | Mapping[str, object] | str,
) -> TrackRuler:
    """Build and semantically validate one supplied GARP ruler."""

    return validate_garp_ruler(parse_payload(value))


def _validate_track_semantics(ruler: TrackRuler) -> TrackRuler:
    """Dispatch only semantics implemented by this build."""

    if ruler.track == "quality_discount":
        return validate_quality_discount_ruler(ruler)
    if ruler.track == "deep_value":
        return validate_deep_value_ruler(ruler)
    if ruler.track == "garp":
        return validate_garp_ruler(ruler)
    return ruler


def _canonical_ruler_payload(ruler: TrackRuler) -> str:
    return json.dumps(
        _plain(ruler.payload()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_payload(
    value: TrackRuler | Mapping[str, object] | str,
) -> str:
    return _canonical_ruler_payload(parse_payload(value))


def payload_digest(
    value: TrackRuler | Mapping[str, object] | str,
) -> str:
    return hashlib.sha256(canonical_payload(value).encode("utf-8")).hexdigest()


def validate_ruler_version(
    row: RulerVersion,
    *,
    expected_track: str | None = None,
    expected_algorithm_version: str | None = None,
    allow_retired_algorithm: bool = False,
) -> TrackRuler:
    """Parse and validate one persisted discovery ruler end-to-end."""

    if row.scope not in SCOPE_TRACKS:
        raise ValueError("persisted discovery ruler integrity mismatch")
    try:
        ruler = TrackRuler.from_json(
            row.payload_json,
            allow_retired_algorithm=allow_retired_algorithm,
        )
        if ruler.algorithm_version not in AVAILABLE_BUILDS and not allow_retired_algorithm:
            raise ValueError('candidate build is not a production ruler')
        # Retired payloads were semantically validated at activation; the
        # digest check below still proves they are the activated bytes.
        if ruler.algorithm_version not in RETIRED_BUILDS:
            ruler = _validate_track_semantics(ruler)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "persisted discovery ruler integrity mismatch"
        ) from error
    canonical = _canonical_ruler_payload(ruler)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if (
        ruler.scope != row.scope
        or canonical != row.payload_json
        or row.digest != digest
        or (
            expected_track is not None
            and ruler.track != expected_track
        )
        or (
            expected_algorithm_version is not None
            and ruler.algorithm_version != expected_algorithm_version
        )
    ):
        raise ValueError("persisted discovery ruler integrity mismatch")
    return ruler


parse_ruler_version = validate_ruler_version


def _rows(
    session: Session,
    scope: str,
    *,
    user_id: int,
    digest: str | None = None,
) -> list[RulerVersion]:
    query = select(RulerVersion).where(
        RulerVersion.user_id == user_id,
        RulerVersion.scope == scope,
    )
    if digest is not None:
        query = query.where(RulerVersion.digest == digest)
    return session.scalars(
        query.order_by(RulerVersion.id.desc())
    ).all()


def _newest_valid(
    rows: Sequence[RulerVersion],
    *,
    track: str,
    algorithm_version: str | None,
) -> RulerVersion | None:
    for row in rows:
        try:
            validate_ruler_version(
                row,
                expected_track=track,
                expected_algorithm_version=algorithm_version,
            )
        except (TypeError, ValueError):
            continue
        return row
    return None


def latest(
    session: Session,
    track_or_scope: str,
    *,
    user_id: int = LOCAL_USER_ID,
    algorithm_version: str | None = None,
) -> RulerVersion | None:
    scope = _scope(track_or_scope)
    if CANDIDATE_BUILD_VERSION in AVAILABLE_BUILDS:
        receipt = _published_release(session, user_id=user_id)
        if receipt is None or algorithm_version not in (None, CANDIDATE_BUILD_VERSION):
            return None
        expected = receipt['rulers'][track_for_scope(scope)]
        row = session.get(RulerVersion, expected['id'])
        return row
    return _newest_valid(
        _rows(session, scope, user_id=user_id),
        track=track_for_scope(scope),
        algorithm_version=algorithm_version,
    )


def activate(
    session: Session,
    value: TrackRuler | Mapping[str, object] | str,
    reason: object,
    *,
    user_id: int = LOCAL_USER_ID,
) -> RulerVersion:
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise ValueError("ruler activation reason must not be blank")
    ruler = _validate_track_semantics(parse_payload(value))
    if ruler.algorithm_version not in AVAILABLE_BUILDS:
        raise ValueError('candidate build cannot be published before production activation')
    payload_json = canonical_payload(ruler)
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if ruler.algorithm_version == CANDIDATE_BUILD_VERSION:
        permission = _release_sessions.get(session)
        allowed = (permission is not None and permission['transaction'] is session.get_transaction()
                   and permission['user_id'] == user_id
                   and permission['digests'].get(ruler.track) == digest)
        receipt = None if allowed else _published_release(session, user_id=user_id)
        if not allowed and (receipt is None or receipt['rulers'][ruler.track]['digest'] != digest):
            raise ValueError('v8 ruler requires a coverage-qualified atomic release publication')
        if not allowed:
            return session.get(RulerVersion, receipt['rulers'][ruler.track]['id'])
    existing = _newest_valid(
        _rows(
            session,
            ruler.scope,
            user_id=user_id,
            digest=digest,
        ),
        track=ruler.track,
        algorithm_version=ruler.algorithm_version,
    )
    if existing is not None:
        return existing
    session.execute(
        sqlite_insert(RulerVersion)
        .prefix_with("OR IGNORE")
        .values(
            user_id=user_id,
            scope=ruler.scope,
            digest=digest,
            reason=reason_text,
            payload_json=payload_json,
            activated_at=datetime.now(timezone.utc),
        )
    )
    row = _newest_valid(
        _rows(
            session,
            ruler.scope,
            user_id=user_id,
            digest=digest,
        ),
        track=ruler.track,
        algorithm_version=ruler.algorithm_version,
    )
    if row is None:
        raise ValueError("persisted discovery ruler integrity mismatch")
    return row


def load(row: RulerVersion) -> TrackRuler:
    return validate_ruler_version(row)


def readiness(
    session: Session,
    track_or_scope: str,
    *,
    user_id: int = LOCAL_USER_ID,
) -> Mapping[str, object]:
    scope = _scope(track_or_scope)
    track = track_for_scope(scope)
    row = latest(session, scope, user_id=user_id)
    if row is None:
        return MappingProxyType(
            {
                "track": track,
                "scope": scope,
                "status": RULER_NOT_READY,
                "active": None,
            }
        )
    try:
        ruler = validate_ruler_version(
            row,
            expected_track=track,
        )
    except (TypeError, ValueError):
        return MappingProxyType(
            {
                "track": track,
                "scope": scope,
                "status": RULER_NOT_READY,
                "active": None,
            }
        )
    return MappingProxyType(
        {
            "track": track,
            "scope": scope,
            "status": "ready",
            "active": MappingProxyType(
                {
                    "id": row.id,
                    "digest": row.digest,
                    "reason": row.reason,
                    "activated_at": row.activated_at.isoformat(),
                    "algorithm_version": ruler.algorithm_version,
                }
            ),
        }
    )


def require_ready(
    session: Session,
    track_or_scope: str,
    *,
    user_id: int = LOCAL_USER_ID,
) -> tuple[RulerVersion, TrackRuler]:
    scope = _scope(track_or_scope)
    track = track_for_scope(scope)
    row = latest(session, scope, user_id=user_id)
    if row is None:
        raise RulerNotReadyError(RULER_NOT_READY)
    try:
        return row, validate_ruler_version(
            row,
            expected_track=track,
        )
    except (TypeError, ValueError) as error:
        raise RulerNotReadyError(RULER_NOT_READY) from error


status = readiness


@contextmanager
def _release_authorization(session, rulers, *, user_id):
    """Internal exact-contract capability, bounded by one owner/transaction."""
    if user_id != LOCAL_USER_ID or session.get_transaction() is None or session in _release_sessions:
        raise ValueError('release requires the local owner and an exclusive caller transaction')
    if set(rulers) != set(TRACKS):
        raise ValueError('release requires all three tracks')
    _release_sessions[session] = {'user_id': user_id, 'transaction': session.get_transaction(),
                                 'digests': {track: payload_digest(ruler) for track, ruler in rulers.items()}}
    try:
        yield
    finally:
        _release_sessions.pop(session, None)


def _published_release(session, *, user_id):
    """Do not skip a corrupt/newer rollback receipt in favour of an old one."""
    from app.selection_models import SelectionRelease
    if user_id != LOCAL_USER_ID:
        return None
    row = session.scalar(select(SelectionRelease).where(SelectionRelease.user_id == user_id)
                         .order_by(SelectionRelease.id.desc()).limit(1).execution_options(populate_existing=True))
    return _validate_publication_receipt(session, row, user_id=user_id)


def _validate_publication_receipt(session, row, *, user_id):
    from app.selection_models import SelectionRelease
    if (row is None or row.user_id != user_id or user_id != LOCAL_USER_ID
            or row.action != 'publish' or row.build != CANDIDATE_BUILD_VERSION):
        return None
    try:
        value = json.loads(row.payload_json)
        canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        if (canonical != row.payload_json or hashlib.sha256(canonical.encode()).hexdigest() != row.digest
                or value['user_id'] != user_id or value['action'] != row.action or value['build'] != row.build
                or value['fill_id'] != row.fill_id or value['previous_id'] != row.previous_id
                or value['reason'] != row.reason or value['created_at'] != row.created_at.isoformat()
                or set(value['rulers']) != set(TRACKS) or value['report']['eligible'] is not True
                or value['report']['build'] != row.build or value['report']['fill_id'] != row.fill_id
                or value['input_hash'] != value['report']['input_hash']
                or value['configuration_hash'] != value['report']['configuration_hash']
                or value['report_hash'] != hashlib.sha256(json.dumps(value['report'], sort_keys=True,
                    separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()):
            return None
        if row.previous_id is not None:
            prior = session.get(SelectionRelease, row.previous_id)
            if (prior is None or prior.user_id != user_id or prior.id >= row.id
                    or prior.digest != value['previous_digest']):
                return None
        for track, identity in value['rulers'].items():
            ruler = session.get(RulerVersion, identity['id'])
            if (ruler is None or ruler.user_id != user_id or ruler.digest != identity['digest']
                    or ruler.digest != value['report']['ruler_digests'][track]
                    or json.loads(ruler.payload_json) != value['report']['rulers'][track]):
                return None
            validate_ruler_version(ruler, expected_track=track, expected_algorithm_version=CANDIDATE_BUILD_VERSION)
        return value
    except (KeyError, TypeError, ValueError):
        return None



def execution_authorized(session, run):
    """Authorize frozen execution, preserving its original exact publication.

    A newer valid publication does not replace frozen configuration. A newest
    rollback, corrupt receipt, missing authority, or retired build fails closed.
    Call again under the result transaction's SQLite write lock before commit.
    """
    from app.selection_models import SelectionRelease
    if run is None or run.algorithm_version not in AVAILABLE_BUILDS:
        return False
    if run.algorithm_version != CANDIDATE_BUILD_VERSION:
        return True  # Only explicitly available historical test builds reach here.
    if run.user_id != LOCAL_USER_ID or _published_release(session, user_id=run.user_id) is None:
        return False
    try:
        frozen = json.loads(run.ruler_payloads_json)
        ids = {track: getattr(run, f'{track}_ruler_version_id') for track in TRACKS}
        if set(frozen) != set(TRACKS) or any(type(value) is not int for value in ids.values()):
            return False
        for row in session.scalars(select(SelectionRelease).where(
                SelectionRelease.user_id == run.user_id, SelectionRelease.action == 'publish',
                SelectionRelease.created_at <= run.created_at).order_by(SelectionRelease.id.desc())
                .execution_options(populate_existing=True)):
            value = _validate_publication_receipt(session, row, user_id=run.user_id)
            if value is None:
                continue
            if all(value['rulers'][track]['id'] == ids[track]
                   and frozen[track]['id'] == ids[track]
                   and frozen[track]['sha256'] == value['rulers'][track]['digest']
                   and frozen[track]['payload'] == value['report']['rulers'][track]
                   for track in TRACKS):
                return True
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return False


def _legacy_candidate_projection(ruler):
    """Reuse structural gate validation; this object is never executed or published."""
    payload = _plain(ruler.payload())
    track = ruler.track
    schemas = {'quality_discount': (QUALITY_DISCOUNT_FIELDS, QUALITY_DISCOUNT_FORMULAS, QUALITY_DISCOUNT_ORDER),
               'deep_value': (DEEP_VALUE_FIELDS, DEEP_VALUE_FORMULAS, DEEP_VALUE_ORDER),
               'garp': (GARP_FIELDS, GARP_FORMULAS, GARP_ORDER)}
    fields, formulas, order = schemas[track]
    payload.update(fields=list(fields), formulas=dict(formulas), order=[list(row) for row in order],
                   missing_policy={key: ruler.missing_policy[key] for key in fields}, algorithm_version='discovery-v7')
    for key in ('ranking_contract', 'operating_policy', 'metric_contract'):
        payload.pop(key)
    if track == 'garp':
        payload['thresholds'].pop('roic_gross_min')
        payload['thresholds']['return_on_capital_min'] = '10'
        branch = payload['sector_branches']['nonfinancial']
        branch['return_metric_policy'] = 'roic_then_roe'
        branch['evidence_fields'] = ['revenue_cagr', 'deducted_profit_cagr', 'return_on_capital', 'peg']
    return _validate_track_semantics(TrackRuler.from_payload(payload, allow_retired_algorithm=True))


def candidate_ruler(value, *, operating_policy, band='base') -> TrackRuler:
    """Build a callable, unpublished v8 contract from a validated v7 hypothesis.

    The caller supplies the exact frozen operating policy ({} retains unknown).
    No source collection, database access or production activation occurs here.
    """
    from discovery.selection_contract import metric_contract, ranking_contract, registered_metrics
    if band not in {'conservative', 'base', 'relaxed'}:
        raise ValueError('unknown candidate hypothesis band')
    if isinstance(value, TrackRuler):
        historical = value
    elif isinstance(value, str):
        historical = TrackRuler.from_json(value, allow_retired_algorithm=True)
    else:
        historical = TrackRuler.from_payload(value, allow_retired_algorithm=True)
    base = _validate_track_semantics(historical)
    if base.algorithm_version != 'discovery-v7':
        raise ValueError('candidate factory requires a legacy hypothesis')
    payload = _plain(base.payload())
    specs = registered_metrics(base.track)
    payload['algorithm_version'] = CANDIDATE_BUILD_VERSION
    payload['ranking_contract'] = ranking_contract(base.track)
    payload['metric_contract'] = metric_contract(base.track)
    payload['operating_policy'] = _plain(_freeze_json(operating_policy))
    payload['fields'] = list(dict.fromkeys((*base.fields, *specs)))
    for name, spec in specs.items():
        payload['formulas'][name] = spec.formula
        payload['missing_policy'][name] = spec.missing_policy
        if spec.alias:
            payload['formulas'][spec.alias] = f'alias({name})'
    if 'peg_growth_cap' in payload['thresholds']:
        payload['thresholds']['peg_growth_cap'] = '0.25'
    if base.track == 'garp':
        payload['formulas']['return_metric_label'] = 'literal(roic_gross_5y)'
        payload['thresholds'].pop('return_on_capital_min')
        payload['thresholds']['roic_gross_min'] = {'conservative': '0.12', 'base': '0.1', 'relaxed': '0.08'}[band]
        payload['min_observations']['roic'] = 5
        branch = payload['sector_branches']['nonfinancial']
        branch['return_metric_policy'] = 'verified_roic_gross_only'
        branch['evidence_fields'] = ['revenue_cagr', 'deducted_profit_cagr', 'roic_gross_5y', 'deducted_peg']
    return _validate_candidate(TrackRuler.from_payload(json.loads(json.dumps(payload))), expected_track=base.track)


def _validate_candidate(ruler, *, expected_track):
    from discovery.selection_contract import metric_contract, ranking_contract, registered_metrics
    if ruler.track != expected_track:
        raise ValueError('candidate track mismatch')
    try:
        base = _legacy_candidate_projection(ruler)
    except (KeyError, TypeError) as error:
        raise ValueError('candidate contract is incomplete') from error
    specs = registered_metrics(ruler.track)
    expected_fields = tuple(dict.fromkeys((*base.fields, *specs)))
    formulas = dict(base.formulas)
    policies = dict(base.missing_policy)
    for name, spec in specs.items():
        formulas[name] = spec.formula
        policies[name] = spec.missing_policy
        if spec.alias:
            formulas[spec.alias] = f'alias({name})'
    if ruler.track == 'garp':
        formulas['return_metric_label'] = 'literal(roic_gross_5y)'
    if (ruler.fields != expected_fields or dict(ruler.formulas) != formulas
            or dict(ruler.missing_policy) != policies or ruler.order != base.order
            or _plain(ruler.metric_contract) != _plain(_freeze_json(metric_contract(ruler.track)))
            or _plain(ruler.ranking_contract) != _plain(_freeze_json(ranking_contract(ruler.track)))):
        raise ValueError('candidate metric/ranking contract mismatch')
    if 'peg_growth_cap' in ruler.thresholds and Decimal(ruler.thresholds['peg_growth_cap']) != Decimal('.25'):
        raise ValueError('candidate PEG growth cap must be 0.25')
    if ruler.track == 'garp':
        branch = ruler.sector_branches['nonfinancial']
        if (not 0 < Decimal(ruler.thresholds['roic_gross_min']) <= 1
                or ruler.min_observations['roic'] != 5
                or branch['return_metric_policy'] != 'verified_roic_gross_only'
                or tuple(branch['evidence_fields']) != ('revenue_cagr', 'deducted_profit_cagr', 'roic_gross_5y', 'deducted_peg')):
            raise ValueError('candidate gross ROIC policy mismatch')
    common = ruler.sector_branches['common']
    if ruler.track in {'quality_discount', 'deep_value'}:
        windows = common['lookback_windows']
        if windows['normalized_earnings'] != 5 or (ruler.track == 'deep_value' and windows['fcf'] != 5):
            raise ValueError('candidate normalization requires five annuals')
    return ruler
