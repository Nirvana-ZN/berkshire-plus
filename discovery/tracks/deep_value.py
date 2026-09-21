"""Pure deep-value classification over one frozen company snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import TypeAlias

from discovery.industry import classify_industry_branch
from discovery.metrics import mean, median, normalized_earnings, percentile_rank
from discovery.rulers import (
    DEEP_VALUE_FIELD_DOMAIN_POLICY,
    TrackRuler,
    deep_value_ruler,
)
from discovery.snapshot import FrozenCompanyInput
from discovery.tracks.quality_discount import (
    MAX_FINANCIAL_ROWS,
    MAX_VALUATION_ROWS,
    _AS_OF_KEYS,
    _FIELD_KEYS,
    _FIELD_SOURCE_KEYS,
    _FINANCIAL_ROW_KEYS,
    _FINANCIAL_VALUE_KEYS,
    _SECURITY_KEYS,
    _SOURCE_KEYS,
    _AnnualPeriodDuplicate,
    _DomainInvalid,
    _SnapshotInvalid,
    _complete_years,
    _date,
    _decimal,
    _decimal_text,
    _divide,
    _multiply,
    _period_end,
    _sequence,
    _subtract,
    _timestamp,
    _valuation_history,
)

JsonMetric: TypeAlias = None | str | tuple[str, ...]
_FIELDS = (
    "deducted_net_income",
    "ocf",
    "capex",
    "shares",
    "cash",
    "interest_debt",
    "ebit",
    "interest",
    "revenue",
    "net_income",
    "impairment_loss",
    "government_grants",
    "asset_disposal_gain",
    "roe",
    "npl",
    "capital_adequacy",
    "core_tier1_capital_adequacy",
)
_DOMAINS = MappingProxyType(
    {
        "deducted_net_income": "signed",
        "ocf": "signed",
        "capex": "nonnegative",
        "shares": "positive",
        "cash": "nonnegative",
        "interest_debt": "nonnegative",
        "ebit": "signed",
        "interest": "signed",
        "revenue": "nonnegative",
        "net_income": "signed",
        "impairment_loss": "signed",
        "government_grants": "signed",
        "asset_disposal_gain": "signed",
        "roe": "signed",
        "npl": "nonnegative",
        "capital_adequacy": "signed",
        "core_tier1_capital_adequacy": "signed",
    }
)
_WINDOW_FIELDS = MappingProxyType(
    {
        "normalized_earnings": ("deducted_net_income",),
        "fcf": ("ocf", "capex"),
        "net_debt": ("interest_debt", "cash"),
        "interest_coverage": ("ebit", "interest"),
        "dilution": ("shares",),
        "revenue": ("revenue",),
        "one_time_profit": ("net_income", "deducted_net_income"),
        "impairment": ("impairment_loss",),
        "subsidy_disposal": ("government_grants", "asset_disposal_gain"),
        "bank_roe": ("roe",),
        "bank_npl": ("npl",),
        "bank_capital": (
            "capital_adequacy",
            "core_tier1_capital_adequacy",
        ),
    }
)
_NUMERIC_METRICS = (
    "listing_years",
    "normalized_earnings",
    "normalized_pe",
    "cumulative_fcf",
    "normalized_fcf",
    "net_debt_to_normalized_fcf",
    "interest_coverage",
    "dilution_rate",
    "revenue_decline",
    "one_time_profit_share",
    "impairment_share",
    "subsidy_disposal_share",
    "pb_percentile",
    "pb_median_ratio",
    "roe_mean",
    "npl_change",
    "bank_capital_adequacy",
    "bank_core_tier1_capital_adequacy",
)


@dataclass(frozen=True)
class TrackVerdict:
    code: str
    verdict: str
    reason: str | None
    metrics: Mapping[str, JsonMetric]
    evidence_rule_ids: tuple[str, ...]
    risk_flags: tuple[str, ...]

    @property
    def primary_reason(self) -> str | None:
        return self.reason


@dataclass(frozen=True)
class _Annual:
    year: int
    period_end: date
    source_as_of: date
    values: Mapping[str, Decimal | None]


@dataclass(frozen=True)
class _Window:
    start_year: int
    end_year: int
    rows: tuple[_Annual, ...]


def _empty_metrics(code: str) -> dict[str, JsonMetric]:
    values: dict[str, JsonMetric] = {
        key: None for key in _NUMERIC_METRICS
    }
    values.update(
        {
            f"{name}_{suffix}": (() if suffix == "available_years" else None)
            for name in _WINDOW_FIELDS
            for suffix in (
                "window_start_year",
                "window_end_year",
                "available_years",
                "available_count",
            )
        }
    )
    values.update(
        {
            "code": code,
            "branch": None,
            "latest_fiscal_year": None,
            "latest_fiscal_year_staleness": None,
            "allowed_latest_annual_staleness_years": None,
            "normalized_earnings_observations": None,
            "fcf_observations": None,
            "net_debt_observations": None,
            "interest_coverage_observations": None,
            "dilution_observations": None,
            "revenue_observations": None,
            "one_time_profit_observations": None,
            "impairment_observations": None,
            "subsidy_disposal_observations": None,
            "valuation_pb_observations": None,
            "bank_roe_observations": None,
            "bank_npl_observations": None,
            "bank_capital_observations": None,
            "triggered_evidence_rule_ids": (),
            "risk_flags": (),
        }
    )
    return values


def _verdict(
    code: str,
    verdict: str,
    reason: str | None,
    metrics: Mapping[str, JsonMetric],
    evidence: Sequence[str] = (),
    risks: Sequence[str] = (),
) -> TrackVerdict:
    values = dict(metrics)
    values["triggered_evidence_rule_ids"] = tuple(evidence)
    values["risk_flags"] = tuple(risks)
    return TrackVerdict(
        code,
        verdict,
        reason,
        MappingProxyType(values),
        tuple(evidence),
        tuple(risks),
    )


def _trusted(row: Mapping[str, object], field: str) -> Decimal | None:
    values = row["values"]
    provenance = row["provenance"]
    sources = row["field_sources"]
    if not all(isinstance(item, Mapping) for item in (values, provenance, sources)):
        raise _SnapshotInvalid("financial row objects")
    assert isinstance(values, Mapping)
    assert isinstance(provenance, Mapping)
    assert isinstance(sources, Mapping)
    if (
        set(values) != _FINANCIAL_VALUE_KEYS
        or set(provenance) != set(sources)
        or set(sources)
        != {name for name, value in values.items() if value is not None}
    ):
        raise _SnapshotInvalid("financial values schema")
    value = _decimal(values[field], f"financial {field}")
    if value is None:
        return None
    metadata = sources.get(field)
    if (
        tuple(provenance.get(field, ())) != ("discovery",)
        or not isinstance(metadata, Mapping)
        or set(metadata) != _FIELD_SOURCE_KEYS
        or metadata.get("source") != "discovery"
        or metadata.get("trusted") is not True
    ):
        return None
    domain = _DOMAINS[field]
    if domain == "positive" and value <= 0:
        raise _DomainInvalid(f"{field}_not_positive")
    if domain == "nonnegative" and value < 0:
        raise _DomainInvalid(f"{field}_negative")
    return value


def _financial_history(
    company: FrozenCompanyInput,
    financial_as_of: date,
) -> tuple[_Annual, ...]:
    rows = _sequence(
        company.fields["financial_history"],
        "financial history",
        MAX_FINANCIAL_ROWS,
    )
    result: dict[int, _Annual] = {}
    for raw in rows:
        if not isinstance(raw, Mapping) or set(raw) != _FINANCIAL_ROW_KEYS:
            raise _SnapshotInvalid("financial row schema")
        if raw["period_type"] != "annual":
            continue
        year, period_end = _period_end(raw["period"])
        if year in result:
            raise _AnnualPeriodDuplicate("annual_fiscal_year_duplicate")
        source_as_of = _date(raw["source_as_of"], "financial source as-of")
        if period_end > company.snapshot_at.date() or source_as_of > financial_as_of:
            raise _SnapshotInvalid("financial look-ahead")
        result[year] = _Annual(
            year,
            period_end,
            source_as_of,
            MappingProxyType({field: _trusted(raw, field) for field in _FIELDS}),
        )
    return tuple(result[year] for year in sorted(result))


def _latest_completed_fiscal_year(snapshot_date: date) -> int:
    return (
        snapshot_date.year
        if (snapshot_date.month, snapshot_date.day) == (12, 31)
        else snapshot_date.year - 1
    )


def _windows(
    history: Sequence[_Annual],
    ruler: TrackRuler,
    snapshot_date: date,
) -> tuple[Mapping[str, _Window], dict[str, JsonMetric], bool]:
    common = ruler.sector_branches["common"]
    assert isinstance(common, Mapping)
    configured = common["lookback_windows"]
    assert isinstance(configured, Mapping)
    allowed = int(common["latest_annual_staleness_years"])
    audit: dict[str, JsonMetric] = {
        "allowed_latest_annual_staleness_years": str(allowed),
    }
    if not history:
        return MappingProxyType({}), audit, False
    latest = history[-1].year
    staleness = _latest_completed_fiscal_year(snapshot_date) - latest
    audit["latest_fiscal_year"] = str(latest)
    audit["latest_fiscal_year_staleness"] = str(max(0, staleness))
    windows: dict[str, _Window] = {}
    for name, required_fields in _WINDOW_FIELDS.items():
        years = int(configured[name])
        start = latest - years + 1
        rows = tuple(row for row in history if start <= row.year <= latest)
        available = tuple(
            row
            for row in rows
            if all(row.values[field] is not None for field in required_fields)
        )
        windows[name] = _Window(start, latest, rows)
        audit[f"{name}_window_start_year"] = str(start)
        audit[f"{name}_window_end_year"] = str(latest)
        audit[f"{name}_available_years"] = tuple(
            str(row.year) for row in available
        )
        audit[f"{name}_available_count"] = str(len(available))
    return MappingProxyType(windows), audit, staleness > allowed


def _available(window: _Window | None, *fields: str) -> tuple[_Annual, ...]:
    if window is None:
        return ()
    return tuple(
        row
        for row in window.rows
        if all(row.values[field] is not None for field in fields)
    )


def _sum(values: Sequence[Decimal]) -> Decimal | None:
    total = Decimal(0)
    for value in values:
        total = _subtract(total, value.copy_negate())
        if total is None:
            return None
    return total


def _nonfinancial_metrics(
    company: FrozenCompanyInput,
    ruler: TrackRuler,
    windows: Mapping[str, _Window],
) -> tuple[dict[str, Decimal | None], dict[str, int]]:
    metrics = {key: None for key in _NUMERIC_METRICS}
    counts: dict[str, int] = {}

    earnings_rows = _available(
        windows.get("normalized_earnings"), "deducted_net_income"
    )
    earnings = tuple(row.values["deducted_net_income"] for row in earnings_rows)
    counts["normalized_earnings"] = len(earnings)
    metrics["normalized_earnings"] = normalized_earnings(
        earnings,
        ruler.min_observations["normalized_earnings"],
    )

    fcf_rows = _available(windows.get("fcf"), "ocf", "capex")
    fcf = tuple(
        _subtract(row.values["ocf"], row.values["capex"])
        for row in fcf_rows
    )
    usable_fcf = tuple(value for value in fcf if value is not None)
    counts["fcf"] = len(usable_fcf)
    if len(usable_fcf) >= ruler.min_observations["fcf"]:
        metrics["cumulative_fcf"] = _sum(usable_fcf)
        metrics["normalized_fcf"] = median(usable_fcf)

    share_rows = _available(windows.get("dilution"), "shares")
    counts["dilution"] = len(share_rows)
    if len(share_rows) >= ruler.min_observations["dilution"]:
        ratio = _divide(share_rows[-1].values["shares"], share_rows[0].values["shares"])
        metrics["dilution_rate"] = _subtract(ratio, Decimal(1))

    net_debt_rows = _available(
        windows.get("net_debt"), "interest_debt", "cash"
    )
    counts["net_debt"] = len(net_debt_rows)
    if (
        len(net_debt_rows) >= ruler.min_observations["net_debt"]
        and metrics["normalized_fcf"] is not None
        and metrics["normalized_fcf"]
        > Decimal(ruler.thresholds["normalized_fcf_min"])
    ):
        net_debt = _subtract(
            net_debt_rows[-1].values["interest_debt"],
            net_debt_rows[-1].values["cash"],
        )
        metrics["net_debt_to_normalized_fcf"] = _divide(
            net_debt, metrics["normalized_fcf"]
        )

    interest_rows = _available(
        windows.get("interest_coverage"), "ebit", "interest"
    )
    counts["interest_coverage"] = len(interest_rows)
    if interest_rows:
        metrics["interest_coverage"] = _divide(
            interest_rows[-1].values["ebit"],
            interest_rows[-1].values["interest"].copy_abs(),  # type: ignore[union-attr]
        )

    revenue_rows = _available(windows.get("revenue"), "revenue")
    counts["revenue"] = len(revenue_rows)
    if (
        len(revenue_rows) >= ruler.min_observations["revenue"]
        and revenue_rows[0].values["revenue"] is not None
        and revenue_rows[0].values["revenue"] > 0
    ):
        decline = _divide(
            _subtract(
                revenue_rows[0].values["revenue"],
                revenue_rows[-1].values["revenue"],
            ),
            revenue_rows[0].values["revenue"],
        )
        metrics["revenue_decline"] = (
            None if decline is None else max(Decimal(0), decline)
        )

    denominator = metrics["normalized_earnings"]
    denominator = (
        denominator.copy_abs()
        if denominator is not None and not denominator.is_zero()
        else None
    )
    one_time_rows = _available(
        windows.get("one_time_profit"), "net_income", "deducted_net_income"
    )
    counts["one_time_profit"] = len(one_time_rows)
    if one_time_rows and denominator is not None:
        excess = _subtract(
            one_time_rows[-1].values["net_income"],
            one_time_rows[-1].values["deducted_net_income"],
        )
        metrics["one_time_profit_share"] = _divide(
            None if excess is None else max(Decimal(0), excess),
            denominator,
        )

    impairment_rows = _available(
        windows.get("impairment"), "impairment_loss"
    )
    counts["impairment"] = len(impairment_rows)
    if impairment_rows and denominator is not None:
        metrics["impairment_share"] = _divide(
            impairment_rows[-1].values["impairment_loss"].copy_abs(),  # type: ignore[union-attr]
            denominator,
        )

    subsidy_rows = _available(
        windows.get("subsidy_disposal"),
        "government_grants",
        "asset_disposal_gain",
    )
    counts["subsidy_disposal"] = len(subsidy_rows)
    if subsidy_rows and denominator is not None:
        latest = subsidy_rows[-1].values
        total = _sum(
            (
                latest["government_grants"].copy_abs(),  # type: ignore[union-attr]
                latest["asset_disposal_gain"].copy_abs(),  # type: ignore[union-attr]
            )
        )
        metrics["subsidy_disposal_share"] = _divide(total, denominator)

    latest_shares = share_rows[-1].values["shares"] if share_rows else None
    price = _decimal(company.fields["price"], "current price")
    market_value = _multiply(price, latest_shares)
    if (
        metrics["normalized_earnings"] is not None
        and metrics["normalized_earnings"]
        > Decimal(ruler.thresholds["normalized_earnings_min"])
    ):
        metrics["normalized_pe"] = _divide(
            market_value, metrics["normalized_earnings"]
        )
    return metrics, counts


def _financial_metrics(
    company: FrozenCompanyInput,
    ruler: TrackRuler,
    windows: Mapping[str, _Window],
    valuations: Sequence[object],
) -> tuple[dict[str, Decimal | None], dict[str, int]]:
    metrics = {key: None for key in _NUMERIC_METRICS}
    counts: dict[str, int] = {}
    pb = tuple(
        row.pb
        for row in valuations
        if row.pb is not None and row.pb > 0  # type: ignore[attr-defined]
    )
    counts["valuation_pb"] = len(pb)
    current_pb = _decimal(company.fields["pb"], "current pb")
    if current_pb is not None and current_pb > 0:
        metrics["pb_percentile"] = percentile_rank(current_pb, pb)
        metrics["pb_median_ratio"] = _divide(current_pb, median(pb))

    roe_rows = _available(windows.get("bank_roe"), "roe")
    roe = tuple(row.values["roe"] for row in roe_rows)
    counts["bank_roe"] = len(roe)
    metrics["roe_mean"] = mean(roe)

    npl_rows = _available(windows.get("bank_npl"), "npl")
    counts["bank_npl"] = len(npl_rows)
    if len(npl_rows) >= ruler.min_observations["bank_npl"]:
        metrics["npl_change"] = _subtract(
            npl_rows[-1].values["npl"], npl_rows[0].values["npl"]
        )

    capital_rows = _available(
        windows.get("bank_capital"),
        "capital_adequacy",
        "core_tier1_capital_adequacy",
    )
    counts["bank_capital"] = len(capital_rows)
    if capital_rows:
        metrics["bank_capital_adequacy"] = capital_rows[-1].values[
            "capital_adequacy"
        ]
        metrics["bank_core_tier1_capital_adequacy"] = capital_rows[-1].values[
            "core_tier1_capital_adequacy"
        ]
    return metrics, counts


def _missing(metric: str, reason: str, ruler: TrackRuler) -> tuple[str, str]:
    assert ruler.missing_policy[metric] == "insufficient"
    return "insufficient", reason


def _gate_failures(
    gate: str,
    ruler: TrackRuler,
    metrics: Mapping[str, Decimal | None],
    counts: Mapping[str, int],
) -> list[tuple[str, str]]:
    thresholds = ruler.thresholds
    if gate == "listing_age":
        value = metrics["listing_years"]
        if value is None:
            return [_missing("listing_years", "listing_date_invalid", ruler)]
        return (
            [("insufficient", "listing_history_insufficient")]
            if value < Decimal(thresholds["listing_years_min"])
            else []
        )
    specs = {
        "normalized_earnings": (
            "normalized_earnings",
            "normalized_earnings",
            "normalized_earnings_min",
            "below_or_equal",
        ),
        "normalized_valuation": (
            "normalized_pe",
            "normalized_earnings",
            "normalized_pe_max",
            "above",
        ),
        "positive_cumulative_fcf": (
            "cumulative_fcf",
            "fcf",
            "cumulative_fcf_min",
            "below_or_equal",
        ),
        "net_debt": (
            "net_debt_to_normalized_fcf",
            "net_debt",
            "net_debt_to_normalized_fcf_max",
            "above",
        ),
        "interest_coverage": (
            "interest_coverage",
            "interest_coverage",
            "interest_coverage_min",
            "below",
        ),
        "dilution": (
            "dilution_rate",
            "dilution",
            "dilution_rate_max",
            "above",
        ),
        "revenue_decline": (
            "revenue_decline",
            "revenue",
            "revenue_decline_max",
            "above",
        ),
        "one_time_profit": (
            "one_time_profit_share",
            "one_time_profit",
            "one_time_profit_share_max",
            "above",
        ),
        "impairment": (
            "impairment_share",
            "impairment",
            "impairment_share_max",
            "above",
        ),
        "subsidy_disposal": (
            "subsidy_disposal_share",
            "subsidy_disposal",
            "subsidy_disposal_share_max",
            "above",
        ),
        "bank_roe": (
            "roe_mean",
            "bank_roe",
            "bank_roe_min",
            "below",
        ),
        "bank_npl_trend": (
            "npl_change",
            "bank_npl",
            "bank_npl_increase_max",
            "above",
        ),
    }
    if gate in specs:
        metric, minimum, threshold, direction = specs[gate]
        value = metrics[metric]
        if gate == "net_debt" and (
            metrics["normalized_fcf"] is not None
            and metrics["normalized_fcf"]
            <= Decimal(thresholds["normalized_fcf_min"])
        ):
            return [("drop", "normalized_fcf_not_positive")]
        if counts.get(minimum, 0) < ruler.min_observations[minimum] or value is None:
            return [_missing(metric, f"{minimum}_insufficient", ruler)]
        limit = Decimal(thresholds[threshold])
        failed = (
            value > limit
            if direction == "above"
            else value < limit
            if direction == "below"
            else value <= limit
        )
        reason = {
            "positive_cumulative_fcf": "cumulative_fcf_not_positive",
            "normalized_earnings": "normalized_earnings_not_positive",
        }.get(
            gate,
            f"{metric}_{'above_max' if direction == 'above' else 'below_min'}",
        )
        return [("drop", reason)] if failed else []
    if gate == "bank_valuation":
        failures = []
        for metric, suffix in (
            ("pb_percentile", "percentile"),
            ("pb_median_ratio", "median_ratio"),
        ):
            value = metrics[metric]
            if (
                counts.get("valuation_pb", 0)
                < ruler.min_observations["valuation_pb"]
                or value is None
            ):
                failures.append(
                    _missing(metric, f"valuation_pb_{suffix}_insufficient", ruler)
                )
            elif value > Decimal(thresholds[f"{metric}_max"]):
                failures.append(
                    (
                        "drop",
                        "valuation_pb_percentile_above_max"
                        if suffix == "percentile"
                        else "valuation_pb_not_discounted",
                    )
                )
        return failures
    if gate == "bank_capital":
        failures = []
        for metric, threshold in (
            ("bank_capital_adequacy", "bank_capital_adequacy_min"),
            (
                "bank_core_tier1_capital_adequacy",
                "bank_core_tier1_min",
            ),
        ):
            value = metrics[metric]
            if (
                counts.get("bank_capital", 0)
                < ruler.min_observations["bank_capital"]
                or value is None
            ):
                failures.append(
                    _missing(metric, f"{metric}_insufficient", ruler)
                )
            elif value < Decimal(thresholds[threshold]):
                failures.append(("drop", f"{metric}_below_min"))
        return failures
    raise ValueError(f"unsupported deep value gate: {gate}")


def classify(company: FrozenCompanyInput, ruler: TrackRuler) -> TrackVerdict:
    """Classify one company without database, network, or model access."""

    ruler = deep_value_ruler(ruler)
    if not isinstance(company, FrozenCompanyInput):
        raise TypeError("company must be a FrozenCompanyInput")
    code = company.code if type(company.code) is str else ""
    metrics = _empty_metrics(code)
    try:
        if (
            type(company.code) is not str
            or not company.code
            or type(company.snapshot_at) is not datetime
            or company.snapshot_at.tzinfo is None
            or company.snapshot_at.utcoffset() != timezone.utc.utcoffset(None)
            or not all(
                isinstance(value, Mapping)
                for value in (
                    company.fields,
                    company.sources,
                    company.as_of,
                    company.security_status,
                    company.ruler_version_ids,
                )
            )
            or set(company.fields) != _FIELD_KEYS
            or set(company.sources) != _SOURCE_KEYS
            or set(company.as_of) != _AS_OF_KEYS
            or set(company.security_status) != _SECURITY_KEYS
            or company.fields["listing_date"]
            != company.security_status["listing_date"]
            or company.algorithm_version != ruler.algorithm_version
        ):
            raise _SnapshotInvalid("snapshot schema")

        shared = classify_industry_branch(company.industry)
        metrics["branch"] = shared
        if company.security_status["industry_version"] != ruler.industry_version:
            return _verdict(
                code,
                "insufficient",
                "industry_version_mismatch",
                metrics,
                ("deep_value.industry_version_mismatch",),
            )
        policies = ruler.sector_branches["branch_policies"]
        assert isinstance(policies, Mapping)
        policy = policies[shared]
        if policy in {"unsupported", "insufficient"}:
            reason = (
                "industry_branch_unknown"
                if policy == "insufficient"
                else f"industry_branch_{shared}_unsupported"
            )
            return _verdict(
                code,
                "insufficient",
                reason,
                metrics,
                (f"deep_value.{reason}",),
            )
        branch = str(policy)
        metrics["branch"] = branch

        financial_as_of = _date(company.as_of["financial"], "financial as-of")
        valuation_as_of = _date(company.as_of["valuation"], "valuation as-of")
        valuation_fetched_at = _timestamp(
            company.as_of["valuation_fetched_at"], "valuation fetched-at"
        )
        security_snapshot_at = _timestamp(
            company.security_status["snapshot_at"], "security snapshot-at"
        )
        fill_as_of = _timestamp(company.as_of["fill"], "fill as-of")
        listing_date = _date(company.fields["listing_date"], "listing date")
        if (
            listing_date > company.snapshot_at.date()
            or financial_as_of > company.snapshot_at.date()
            or valuation_as_of > company.snapshot_at.date()
            or security_snapshot_at > company.snapshot_at
            or fill_as_of != company.snapshot_at
            or company.sources["financial_history"]
            != "checkpoint_payload:field_sources"
            or company.security_status["industry_as_of"]
            != company.as_of["security"]
        ):
            raise _SnapshotInvalid("snapshot look-ahead")

        history = _financial_history(company, financial_as_of)
        windows, audit, stale = _windows(
            history, ruler, company.snapshot_at.date()
        )
        metrics.update(audit)
        if stale:
            return _verdict(
                code,
                "insufficient",
                "financial_history_stale",
                metrics,
                ("deep_value.financial_history_stale",),
            )
        common = ruler.sector_branches["common"]
        assert isinstance(common, Mapping)
        assert common["field_domain_policy"] == DEEP_VALUE_FIELD_DOMAIN_POLICY
        valuations = _valuation_history(
            company,
            valuation_as_of,
            valuation_fetched_at,
            str(common["valuation_ratio_method"]),
        )
        if valuations:
            latest = valuations[-1]
            if (
                latest.trade_date != valuation_as_of
                or company.as_of["price"] != latest.trade_date.isoformat()
                or _decimal(company.fields["price"], "current price")
                != latest.price
                or _decimal(company.fields["pb"], "current pb") != latest.pb
            ):
                raise _SnapshotInvalid("current valuation mismatch")

        calculated, counts = (
            _financial_metrics(company, ruler, windows, valuations)
            if branch == "financial"
            else _nonfinancial_metrics(company, ruler, windows)
        )
        calculated["listing_years"] = _complete_years(
            listing_date, company.snapshot_at.date()
        )
    except _SnapshotInvalid as error:
        reason = str(error)
        evidence: tuple[str, ...] = ()
        if isinstance(error, (_DomainInvalid, _AnnualPeriodDuplicate)):
            evidence = (f"deep_value.{reason}",)
        elif "valuation" in reason:
            reason = "valuation_history_invalid"
            evidence = ("deep_value.valuation_history_invalid",)
        elif "financial" in reason:
            reason = "financial_history_invalid"
        else:
            reason = "snapshot_schema_invalid"
        return _verdict(code, "insufficient", reason, metrics, evidence)

    for key, value in calculated.items():
        metrics[key] = _decimal_text(value)
    for name, count in counts.items():
        metrics[f"{name}_observations"] = str(count)

    config = ruler.sector_branches[branch]
    assert isinstance(config, Mapping)
    failures: list[tuple[str, str]] = []
    evidence = []
    risks = []
    for gate in config["gate_order"]:
        gate_failures = _gate_failures(
            str(gate), ruler, calculated, counts
        )
        for status, reason in gate_failures:
            failures.append((status, reason))
            evidence.append(f"deep_value.{reason}")
            if reason in {
                "revenue_decline_above_max",
                "npl_change_above_max",
            }:
                risks.append(reason)
    if not failures:
        return _verdict(code, "pass", None, metrics, evidence, risks)
    status, reason = failures[0]
    return _verdict(code, status, reason, metrics, evidence, risks)
