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
from pathlib import Path
from types import MappingProxyType
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from discovery.builds import BUILD_VERSION
from discovery.industry import INDUSTRY_BRANCHES, classify_industry_branch
from discovery.quality_contract import quality_ruler_branch_policies
from discovery.rulers import (
    DEEP_VALUE_FIELD_DOMAIN_POLICY,
    QUALITY_DISCOUNT_FIELDS,
    QUALITY_DISCOUNT_FORMULAS,
    QUALITY_FIELD_DOMAIN_POLICY,
    canonical_payload,
    quality_discount_ruler,
)
from discovery.snapshot import (
    FrozenCompanyInput,
    build_production_snapshot,
    canonical_json,
)
from discovery.tracks.quality_discount import (
    QUALITY_DOMAIN_INVALID_REASONS,
    MAX_FINANCIAL_ROWS,
    MAX_VALUATION_ROWS,
    bounded_ratio,
    classify,
    decimal_string,
)

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
        "distributions": None,
        "candidate_counts": None,
        "source_as_of": None,
        "industry_versions": None,
        "algorithm_build": BUILD_VERSION,
        "field_domain_policy": (
            DEEP_VALUE_FIELD_DOMAIN_POLICY
            if track == "deep_value"
            else QUALITY_FIELD_DOMAIN_POLICY
        ),
        "proposed_readiness_threshold": None,
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


def _payload(
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


def _quantiles(values: Sequence[Decimal]) -> dict[str, str] | None:
    if not values:
        return None
    ordered = sorted(values)
    result = {}
    for label, numerator in (("min", 0), ("p25", 1), ("p50", 2), ("p75", 3), ("max", 4)):
        index = ((len(ordered) - 1) * numerator) // 4
        text = decimal_string(ordered[index])
        if text is None:
            return None
        result[label] = text
    return result


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


def _ready_artifact(
    path: Path,
    preflight: Mapping[str, int],
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
    if len(snapshot.companies) != preflight["security_count"]:
        return _blocked("snapshot_incomplete_or_corrupt")
    if len(snapshot.companies) > MAX_COMPANIES:
        return _blocked("universe_row_limit_exceeded")
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
            return _blocked("snapshot_history_limit_exceeded")
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
            return _blocked("snapshot_byte_limit_exceeded")

    versions = sorted(
        {
            str(company.security_status.get("industry_version"))
            for company in snapshot.companies
            if company.security_status.get("industry_version")
        }
    )
    if len(versions) != 1:
        return _blocked("industry_version_not_unique")
    industries = [company.industry for company in snapshot.companies]
    method_distribution = _valuation_method_distribution(snapshot.companies)
    if (
        len(method_distribution) != 1
        or next(iter(method_distribution.values()))["companies"]
        != len(snapshot.companies)
    ):
        return _blocked("valuation_ratio_method_not_homogeneous")
    valuation_ratio_method = next(iter(method_distribution))
    try:
        proposals = {
            name: _payload(
                thresholds,
                industry_version=versions[0],
                industries=industries,
                valuation_ratio_method=valuation_ratio_method,
            )
            for name, thresholds in HYPOTHESIS_BANDS.items()
        }
    except ValueError:
        return _blocked("sector_branch_coverage_incomplete")
    if any(
        set(proposal["sector_branches"]["branch_policies"])
        != set(INDUSTRY_BRANCHES)
        for proposal in proposals.values()
    ):
        return _blocked("branch_policy_incomplete")

    frozen_companies = tuple(
        _classifier_input(company, snapshot)
        for company in snapshot.companies
    )
    verdicts = {
        name: tuple(
            classify(company, quality_discount_ruler(payload))
            for company in frozen_companies
        )
        for name, payload in proposals.items()
    }
    base = verdicts["base"]
    metric_values: dict[str, list[Decimal]] = {}
    coverage_counts: Counter[str] = Counter()
    for verdict in base:
        for key, value in verdict.metrics.items():
            parsed = _as_decimal(value)
            if parsed is not None:
                metric_values.setdefault(key, []).append(parsed)
                coverage_counts[key] += 1
    total = len(snapshot.companies)
    branch_counts = _branch_counts(industries)
    field_coverage = {
        key: {
            "present": coverage_counts[key],
            "total": total,
            "ratio": (
                decimal_string(bounded_ratio(coverage_counts[key], total))
                if total
                else "0"
            ),
        }
        for key in sorted(metric_values)
    }
    domain_valid = sum(
        verdict.primary_reason not in QUALITY_DOMAIN_INVALID_REASONS
        for verdict in base
    )
    field_coverage["field_domain_valid"] = {
        "present": domain_valid,
        "total": total,
        "ratio": (
            decimal_string(bounded_ratio(domain_valid, total))
            if total
            else "0"
        ),
    }
    source_keys = (
        "security",
        "financial",
        "valuation",
        "valuation_fetched_at",
        "fill",
    )
    return {
        "schema_version": 1,
        "track": "quality_discount",
        "status": "ready",
        "approval_ready": True,
        "blocked_reasons": [],
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
                    Counter(
                        verdict.primary_reason
                        for verdict in base
                        if verdict.verdict == "insufficient"
                        and verdict.primary_reason is not None
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
                            and classify_industry_branch(company.industry)
                            == branch
                            for company, row in zip(
                                snapshot.companies, rows, strict=True
                            )
                        )
                        for verdict in ("pass", "drop", "insufficient")
                    }
                    for branch in INDUSTRY_BRANCHES
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
        "field_domain_policy": QUALITY_FIELD_DOMAIN_POLICY,
        "hypothesis_labels": {
            name: "calibration hypothesis; never a runtime fallback"
            for name in HYPOTHESIS_BANDS
        },
        "proposed_readiness_threshold": HYPOTHESIS_BANDS["base"][
            "readiness_min"
        ],
        "proposal": proposals["base"],
        "note": (
            "Evidence-only proposal; activation requires explicit approval "
            "and a nonblank reason."
        ),
    }


def calibrate(
    db_path: Path | str,
    *,
    track: str,
) -> dict[str, object]:
    """Return deterministic evidence without initializing or writing the DB."""

    if track not in {"quality_discount", "deep_value"}:
        raise ValueError("unsupported discovery calibration track")
    path = Path(db_path)
    if not path.is_file():
        return _blocked("database_missing", track=track)
    try:
        with _read_only_connection(path) as connection:
            tables = _tables(connection)
            if not REQUIRED_TABLES <= tables:
                return _blocked("missing_schema", track=track)
            if track == "deep_value":
                return _blocked(
                    "trusted_government_grants_unavailable",
                    track=track,
                )
            preflight = _preflight_latest_fill(connection)
            if preflight is None:
                return _blocked("no_completed_fill")
            if _preflight_resource_limit(preflight):
                return _blocked("resource_limit")
            if _preflight_incomplete(preflight):
                return _blocked("snapshot_incomplete_or_corrupt")
    except sqlite3.DatabaseError:
        return _blocked("database_corrupt", track=track)
    try:
        return _ready_artifact(path, preflight)
    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
        return _blocked("snapshot_incomplete_or_corrupt")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--track",
        required=True,
        choices=["quality_discount", "deep_value"],
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
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
