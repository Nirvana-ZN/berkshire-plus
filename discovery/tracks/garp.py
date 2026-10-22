"""Pure growth-at-a-reasonable-price classification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import TypeAlias

from discovery.fundamental_types import FundamentalSnapshot
from discovery.fundamentals import capital_return, collection_metrics, collection_signals
from discovery.industry import classify_industry_branch
from discovery.metrics import cagr, cash_conversion, mean, percentile_rank
from discovery.rulers import GARP_FIELD_DOMAIN_POLICY, TrackRuler, garp_ruler
from discovery.snapshot import FrozenCompanyInput
from discovery.tracks.quality_discount import (
    MAX_FINANCIAL_ROWS,
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


def frozen_fundamental_diagnostics(snapshot: FundamentalSnapshot, policy: Mapping[str, object]) -> dict[str, object]:
    """Opt-in v8 handoff; shadow diagnostics never enter legacy gates or ranks."""
    return {'roic_gross_5y': capital_return(snapshot),
            'collection_metrics': collection_metrics(snapshot),
            'shadow_signals': collection_signals(snapshot, policy)}


_FIELDS = (
    "revenue",
    "deducted_net_income",
    "net_income",
    "ocf",
    "shares",
    "roic",
    "roe",
    "interest_debt",
    "goodwill",
    "acquisition_cash_paid",
    "gross_margin",
    "net_margin",
    "impairment_loss",
    "other_income",
    "asset_disposal_gain",
)
_DOMAINS = MappingProxyType(
    {
        "revenue": "nonnegative",
        "deducted_net_income": "signed",
        "net_income": "signed",
        "ocf": "signed",
        "shares": "positive",
        "roic": "signed",
        "roe": "signed",
        "interest_debt": "nonnegative",
        "goodwill": "nonnegative",
        "acquisition_cash_paid": "signed",
        "gross_margin": "signed",
        "net_margin": "signed",
        "impairment_loss": "signed",
        "other_income": "signed",
        "asset_disposal_gain": "signed",
    }
)
_NUMERIC_METRICS = (
    "revenue_cagr",
    "deducted_profit_cagr",
    "revenue_consistent_growth_periods",
    "deducted_profit_consistent_growth_periods",
    "latest_revenue_growth",
    "latest_deducted_profit_growth",
    "revenue_growth_slowdown",
    "deducted_profit_growth_slowdown",
    "return_on_capital",
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
)
_WINDOW_FIELDS = MappingProxyType(
    {
        "growth": ("revenue", "deducted_net_income"),
        "return_on_capital": (),
        "cash_conversion": ("ocf", "net_income"),
        "dilution": ("shares",),
        "growth_drivers": (
            "revenue",
            "interest_debt",
            "goodwill",
            "acquisition_cash_paid",
        ),
        "margin": ("gross_margin", "net_margin"),
        "quality_adjustments": (
            "deducted_net_income",
            "impairment_loss",
            "other_income",
            "asset_disposal_gain",
        ),
    }
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
            "code": code,
            "branch": None,
            "return_metric_label": None,
            "latest_fiscal_year": None,
            "latest_fiscal_year_staleness": None,
            "allowed_latest_annual_staleness_years": None,
            "revenue_cagr_periods": None,
            "deducted_profit_cagr_periods": None,
            "valuation_pe_observations": None,
            "triggered_evidence_rule_ids": (),
            "risk_flags": (),
        }
    )
    for name in _WINDOW_FIELDS:
        values.update(
            {
                f"{name}_window_start_year": None,
                f"{name}_window_end_year": None,
                f"{name}_available_years": (),
                f"{name}_available_count": None,
                f"{name}_observations": None,
            }
        )
    values["revenue_growth_available_years"] = ()
    values["deducted_profit_growth_available_years"] = ()
    values["revenue_growth_observations"] = None
    values["deducted_profit_growth_observations"] = None
    values["roic_observations"] = None
    values["roe_fallback_observations"] = None
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
    configured = common["annual_observations"]
    periods = common["cagr_periods"]
    assert isinstance(configured, Mapping)
    assert isinstance(periods, Mapping)
    allowed = int(common["latest_annual_staleness_years"])
    audit: dict[str, JsonMetric] = {
        "allowed_latest_annual_staleness_years": str(allowed),
        "revenue_cagr_periods": str(periods["revenue"]),
        "deducted_profit_cagr_periods": str(periods["deducted_profit"]),
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


def _exact(
    window: _Window | None,
    rows: Sequence[_Annual],
    required: int,
) -> bool:
    return (
        window is not None
        and len(rows) == required
        and tuple(row.year for row in rows)
        == tuple(range(window.start_year, window.end_year + 1))
    )


def _latest(window: _Window | None, rows: Sequence[_Annual]) -> bool:
    return (
        window is not None
        and bool(rows)
        and rows[-1].year == window.end_year
    )


def _exact_endpoints(
    window: _Window | None,
    rows: Sequence[_Annual],
) -> bool:
    return (
        window is not None
        and bool(rows)
        and rows[0].year == window.start_year
        and rows[-1].year == window.end_year
    )


def _growth(
    values: Sequence[Decimal],
    minimum: Decimal,
) -> tuple[int, Decimal | None]:
    consistent = 0
    latest = None
    for left, right in zip(values, values[1:], strict=False):
        change = _subtract(_divide(right, left), Decimal(1)) if left > 0 else None
        if change is not None and change >= minimum:
            consistent += 1
        latest = change
    return consistent, latest


def _increase_share(
    first: Decimal | None,
    last: Decimal | None,
    revenue: Decimal | None,
) -> Decimal | None:
    increase = _subtract(last, first)
    if increase is None:
        return None
    return _divide(max(Decimal(0), increase), revenue)


def _sum_abs(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    return _subtract(left.copy_abs(), right.copy_abs().copy_negate())


def _sum(values: Sequence[Decimal | None]) -> Decimal | None:
    total = Decimal(0)
    for value in values:
        if value is None:
            return None
        total = _subtract(total, value.copy_negate())
        if total is None:
            return None
    return total


def _metric_values(
    company: FrozenCompanyInput,
    ruler: TrackRuler,
    windows: Mapping[str, _Window],
    valuations: Sequence[object],
) -> tuple[dict[str, Decimal | None], dict[str, int], dict[str, JsonMetric]]:
    metrics = {key: None for key in _NUMERIC_METRICS}
    counts: dict[str, int] = {}
    audit: dict[str, JsonMetric] = {}
    common = ruler.sector_branches["common"]
    assert isinstance(common, Mapping)
    configured = common["annual_observations"]
    periods = common["cagr_periods"]
    assert isinstance(configured, Mapping)
    assert isinstance(periods, Mapping)

    growth_window = windows.get("growth")
    revenue_rows = _available(growth_window, "revenue")
    profit_rows = _available(growth_window, "deducted_net_income")
    counts["revenue_growth"] = len(revenue_rows)
    counts["deducted_profit_growth"] = len(profit_rows)
    audit["revenue_growth_available_years"] = tuple(
        str(row.year) for row in revenue_rows
    )
    audit["deducted_profit_growth_available_years"] = tuple(
        str(row.year) for row in profit_rows
    )
    if _exact(
        growth_window,
        revenue_rows,
        ruler.min_observations["revenue_growth"],
    ):
        revenue = tuple(row.values["revenue"] for row in revenue_rows)
        metrics["revenue_cagr"] = cagr(revenue, int(periods["revenue"]))
        consistent, latest = _growth(
            revenue,  # type: ignore[arg-type]
            Decimal(ruler.thresholds["revenue_period_growth_min"]),
        )
        metrics["revenue_consistent_growth_periods"] = Decimal(consistent)
        metrics["latest_revenue_growth"] = latest
    if _exact(
        growth_window,
        profit_rows,
        ruler.min_observations["deducted_profit_growth"],
    ):
        profits = tuple(
            row.values["deducted_net_income"] for row in profit_rows
        )
        metrics["deducted_profit_cagr"] = cagr(
            profits, int(periods["deducted_profit"])
        )
        consistent, latest = _growth(
            profits,  # type: ignore[arg-type]
            Decimal(ruler.thresholds["deducted_profit_period_growth_min"]),
        )
        metrics["deducted_profit_consistent_growth_periods"] = Decimal(
            consistent
        )
        metrics["latest_deducted_profit_growth"] = latest
    for prefix in ("revenue", "deducted_profit"):
        trend = metrics[f"{prefix}_cagr"]
        latest = metrics[f"latest_{prefix}_growth"]
        slowdown = _subtract(trend, latest)
        metrics[f"{prefix}_growth_slowdown"] = (
            None if slowdown is None else max(Decimal(0), slowdown)
        )

    return_window = windows.get("return_on_capital")
    roic_rows = _available(return_window, "roic")
    roe_rows = _available(return_window, "roe")
    counts["roic"] = len(roic_rows)
    counts["roe_fallback"] = len(roe_rows)
    audit["roic_observations"] = str(len(roic_rows))
    audit["roe_fallback_observations"] = str(len(roe_rows))
    if roic_rows:
        audit["return_metric_label"] = "roic"
        if (
            len(roic_rows) >= ruler.min_observations["roic"]
            and _latest(return_window, roic_rows)
        ):
            metrics["return_on_capital"] = mean(
                row.values["roic"] for row in roic_rows
            )
    else:
        audit["return_metric_label"] = "roe_fallback"
        if (
            len(roe_rows) >= ruler.min_observations["roe_fallback"]
            and _latest(return_window, roe_rows)
        ):
            metrics["return_on_capital"] = mean(
                row.values["roe"] for row in roe_rows
            )

    cash_window = windows.get("cash_conversion")
    cash_rows = tuple(
        row
        for row in _available(cash_window, "ocf", "net_income")
        if row.values["net_income"] is not None
        and row.values["net_income"] > 0
    )
    counts["cash_conversion"] = len(cash_rows)
    metrics["cash_conversion"] = cash_conversion(
        (row.values["ocf"] for row in cash_rows),
        (row.values["net_income"] for row in cash_rows),
        ruler.min_observations["cash_conversion"],
    )

    dilution_window = windows.get("dilution")
    dilution_rows = _available(dilution_window, "shares")
    counts["dilution"] = len(dilution_rows)
    if (
        len(dilution_rows) >= ruler.min_observations["dilution"]
        and _exact_endpoints(dilution_window, dilution_rows)
    ):
        metrics["dilution_rate"] = _subtract(
            _divide(
                dilution_rows[-1].values["shares"],
                dilution_rows[0].values["shares"],
            ),
            Decimal(1),
        )

    drivers_window = windows.get("growth_drivers")
    driver_rows = _available(
        drivers_window,
        "revenue",
        "interest_debt",
        "goodwill",
        "acquisition_cash_paid",
    )
    counts["growth_drivers"] = len(driver_rows)
    if (
        len(driver_rows) >= ruler.min_observations["growth_drivers"]
        and _exact_endpoints(drivers_window, driver_rows)
    ):
        first, last = driver_rows[0].values, driver_rows[-1].values
        for metric, field in (
            ("interest_debt_increase_to_revenue", "interest_debt"),
            ("goodwill_increase_to_revenue", "goodwill"),
        ):
            metrics[metric] = _increase_share(
                first[field], last[field], last["revenue"]
            )
        metrics["acquisition_cash_to_revenue"] = _divide(
            _sum(
                tuple(
                    max(Decimal(0), row.values["acquisition_cash_paid"])
                    for row in driver_rows
                )
            ),
            _sum(tuple(row.values["revenue"] for row in driver_rows)),
        )

    margin_window = windows.get("margin")
    margin_rows = _available(margin_window, "gross_margin", "net_margin")
    counts["margin"] = len(margin_rows)
    if (
        len(margin_rows) >= ruler.min_observations["margin"]
        and _exact_endpoints(margin_window, margin_rows)
    ):
        metrics["gross_margin_change"] = _subtract(
            margin_rows[-1].values["gross_margin"],
            margin_rows[0].values["gross_margin"],
        )
        metrics["net_margin_change"] = _subtract(
            margin_rows[-1].values["net_margin"],
            margin_rows[0].values["net_margin"],
        )

    quality_window = windows.get("quality_adjustments")
    quality_rows = _available(
        quality_window,
        "deducted_net_income",
        "impairment_loss",
        "other_income",
        "asset_disposal_gain",
    )
    counts["quality_adjustments"] = len(quality_rows)
    if (
        len(quality_rows) >= ruler.min_observations["quality_adjustments"]
        and _latest(quality_window, quality_rows)
    ):
        latest = quality_rows[-1].values
        denominator = latest["deducted_net_income"]
        denominator = (
            None
            if denominator is None or denominator.is_zero()
            else denominator.copy_abs()
        )
        metrics["impairment_share"] = _divide(
            latest["impairment_loss"].copy_abs(),  # type: ignore[union-attr]
            denominator,
        )
        metrics["non_core_income_proxy_share"] = _divide(
            _sum_abs(latest["other_income"], latest["asset_disposal_gain"]),
            denominator,
        )

    pe = tuple(
        row.pe
        for row in valuations
        if row.pe is not None and row.pe > 0  # type: ignore[attr-defined]
    )
    counts["valuation_pe"] = len(pe)
    current_pe = _decimal(company.fields["pe"], "current pe")
    if current_pe is not None and current_pe > 0:
        metrics["current_pe"] = current_pe
        metrics["pe_percentile"] = percentile_rank(current_pe, pe)
    growth = metrics["deducted_profit_cagr"]
    if growth is not None and growth > 0:
        metrics["capped_growth"] = min(
            growth, Decimal(ruler.thresholds["peg_growth_cap"])
        )
        metrics["peg"] = _divide(
            current_pe,
            _multiply(metrics["capped_growth"], Decimal(100)),
        )
    return metrics, counts, audit


def _missing(reason: str) -> tuple[str, str]:
    return "insufficient", reason


def _simple_gate(
    metric: str,
    minimum: str,
    threshold: str,
    direction: str,
    ruler: TrackRuler,
    metrics: Mapping[str, Decimal | None],
    counts: Mapping[str, int],
    reason: str | None = None,
) -> list[tuple[str, str]]:
    value = metrics[metric]
    if counts.get(minimum, 0) < ruler.min_observations[minimum] or value is None:
        return [_missing(f"{minimum}_insufficient")]
    limit = Decimal(ruler.thresholds[threshold])
    failed = value < limit if direction == "below" else value > limit
    return [
        (
            "drop",
            reason
            or f"{metric}_{'below_min' if direction == 'below' else 'above_max'}",
        )
    ] if failed else []


def _gate_failures(
    gate: str,
    ruler: TrackRuler,
    metrics: Mapping[str, Decimal | None],
    counts: Mapping[str, int],
    label: str | None,
) -> list[tuple[str, str]]:
    if gate == "revenue_growth":
        return _simple_gate(
            "revenue_cagr",
            "revenue_growth",
            "revenue_cagr_min",
            "below",
            ruler,
            metrics,
            counts,
        )
    if gate == "deducted_profit_growth":
        return _simple_gate(
            "deducted_profit_cagr",
            "deducted_profit_growth",
            "deducted_profit_cagr_min",
            "below",
            ruler,
            metrics,
            counts,
        )
    if gate == "sustained_growth":
        failures = []
        for metric, minimum, reason in (
            (
                "revenue_consistent_growth_periods",
                "sustained_revenue_growth",
                "revenue_growth_not_sustained",
            ),
            (
                "deducted_profit_consistent_growth_periods",
                "sustained_profit_growth",
                "deducted_profit_growth_not_sustained",
            ),
        ):
            value = metrics[metric]
            failures.extend(
                [_missing(f"{minimum}_insufficient")]
                if value is None
                else [("drop", reason)]
                if value < ruler.min_observations[minimum]
                else []
            )
        return failures
    if gate == "growth_slowdown":
        failures = []
        for metric, threshold in (
            ("revenue_growth_slowdown", "revenue_growth_slowdown_max"),
            (
                "deducted_profit_growth_slowdown",
                "deducted_profit_growth_slowdown_max",
            ),
        ):
            value = metrics[metric]
            if value is None:
                failures.append(_missing("latest_growth_insufficient"))
            elif value > Decimal(ruler.thresholds[threshold]):
                failures.append(("drop", f"{metric}_above_max"))
        return failures
    if gate == "return_on_capital":
        value = metrics["return_on_capital"]
        minimum = "roic" if label == "roic" else "roe_fallback"
        if value is None:
            return [_missing(f"{minimum}_insufficient")]
        return (
            [("drop", "return_on_capital_below_min")]
            if value < Decimal(ruler.thresholds["return_on_capital_min"])
            else []
        )
    simple = {
        "cash_conversion": (
            "cash_conversion",
            "cash_conversion",
            "cash_conversion_min",
            "below",
            None,
        ),
        "dilution": (
            "dilution_rate",
            "dilution",
            "dilution_rate_max",
            "above",
            None,
        ),
        "interest_debt_trend": (
            "interest_debt_increase_to_revenue",
            "growth_drivers",
            "interest_debt_increase_to_revenue_max",
            "above",
            None,
        ),
        "goodwill_trend": (
            "goodwill_increase_to_revenue",
            "growth_drivers",
            "goodwill_increase_to_revenue_max",
            "above",
            None,
        ),
        "acquisition_cash_trend": (
            "acquisition_cash_to_revenue",
            "growth_drivers",
            "acquisition_cash_to_revenue_max",
            "above",
            None,
        ),
        "gross_margin": (
            "gross_margin_change",
            "margin",
            "gross_margin_deterioration_max",
            "below_negative",
            "gross_margin_deterioration",
        ),
        "net_margin": (
            "net_margin_change",
            "margin",
            "net_margin_deterioration_max",
            "below_negative",
            "net_margin_deterioration",
        ),
        "impairment": (
            "impairment_share",
            "quality_adjustments",
            "impairment_share_max",
            "above",
            None,
        ),
        "non_core_income_proxy": (
            "non_core_income_proxy_share",
            "quality_adjustments",
            "non_core_income_proxy_share_max",
            "above",
            None,
        ),
    }
    if gate in simple:
        metric, minimum, threshold, direction, reason = simple[gate]
        if direction == "below_negative":
            value = metrics[metric]
            if counts.get(minimum, 0) < ruler.min_observations[minimum] or value is None:
                return [_missing(f"{minimum}_insufficient")]
            return (
                [("drop", str(reason))]
                if value
                < Decimal(ruler.thresholds[threshold]).copy_negate()
                else []
            )
        return _simple_gate(
            metric,
            minimum,
            threshold,
            direction,
            ruler,
            metrics,
            counts,
            reason,
        )
    if gate == "peg":
        value = metrics["peg"]
        if value is None:
            return [_missing("peg_insufficient")]
        return (
            [("drop", "peg_above_max")]
            if value > Decimal(ruler.thresholds["peg_max"])
            else []
        )
    if gate == "valuation":
        failures = []
        if (
            counts.get("valuation_pe", 0)
            < ruler.min_observations["valuation_pe"]
            or metrics["pe_percentile"] is None
        ):
            failures.append(_missing("valuation_pe_insufficient"))
        elif metrics["pe_percentile"] > Decimal(
            ruler.thresholds["pe_percentile_max"]
        ):
            failures.append(("drop", "valuation_pe_percentile_above_max"))
        if metrics["current_pe"] is None:
            failures.append(_missing("current_pe_insufficient"))
        elif metrics["current_pe"] > Decimal(ruler.thresholds["pe_max"]):
            failures.append(("drop", "current_pe_above_max"))
        return failures
    raise ValueError(f"unsupported GARP gate: {gate}")


def classify(company: FrozenCompanyInput, ruler: TrackRuler) -> TrackVerdict:
    """Classify one immutable company without database, network, or model I/O."""

    ruler = garp_ruler(ruler)
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
            or company.snapshot_at.utcoffset()
            != timezone.utc.utcoffset(None)
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
                ("garp.industry_version_mismatch",),
            )
        policies = ruler.sector_branches["branch_policies"]
        assert isinstance(policies, Mapping)
        policy = policies[shared]
        if policy != "nonfinancial":
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
                (f"garp.{reason}",),
            )
        nonfinancial = ruler.sector_branches["nonfinancial"]
        assert isinstance(nonfinancial, Mapping)
        if (company.industry or "").strip() in nonfinancial["excluded_industries"]:
            return _verdict(
                code,
                "insufficient",
                "industry_cyclical_unsupported",
                metrics,
                ("garp.industry_cyclical_unsupported",),
            )

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
                ("garp.financial_history_stale",),
            )
        common = ruler.sector_branches["common"]
        assert isinstance(common, Mapping)
        assert common["field_domain_policy"] == GARP_FIELD_DOMAIN_POLICY
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
                or _decimal(company.fields["pe"], "current pe") != latest.pe
            ):
                raise _SnapshotInvalid("current valuation mismatch")
        calculated, counts, calculation_audit = _metric_values(
            company, ruler, windows, valuations
        )
        metrics.update(calculation_audit)
    except _SnapshotInvalid as error:
        reason = str(error)
        evidence: tuple[str, ...] = ()
        if isinstance(error, (_DomainInvalid, _AnnualPeriodDuplicate)):
            evidence = (f"garp.{reason}",)
        elif "valuation" in reason:
            reason = "valuation_history_invalid"
            evidence = ("garp.valuation_history_invalid",)
        elif "financial" in reason:
            reason = "financial_history_invalid"
        else:
            reason = "snapshot_schema_invalid"
        return _verdict(code, "insufficient", reason, metrics, evidence)

    for key, value in calculated.items():
        metrics[key] = _decimal_text(value)
    for name, count in counts.items():
        metrics[f"{name}_observations"] = str(count)

    config = ruler.sector_branches["nonfinancial"]
    assert isinstance(config, Mapping)
    failures: list[tuple[str, str]] = []
    evidence: list[str] = []
    risks = []
    for metric in (
        "revenue_growth_slowdown",
        "deducted_profit_growth_slowdown",
        "interest_debt_increase_to_revenue",
        "goodwill_increase_to_revenue",
        "acquisition_cash_to_revenue",
    ):
        value = calculated[metric]
        if value is not None and value > 0:
            risks.append(f"{metric}_worsening")
    for metric in ("gross_margin_change", "net_margin_change"):
        value = calculated[metric]
        if value is not None and value < 0:
            risks.append(f"{metric}_deterioration")
    latest_row = max(
        (
            row
            for row in history
            if row.values["deducted_net_income"] is not None
            and row.values["shares"] is not None
        ),
        key=lambda row: row.year,
        default=None,
    )
    if latest_row is not None and calculated["current_pe"] is not None:
        deducted = latest_row.values["deducted_net_income"]
        ttm_earnings = _divide(
            _multiply(
                _decimal(company.fields["price"], "current price"),
                latest_row.values["shares"],
            ),
            calculated["current_pe"],
        )
        # ponytail: fixed 1.5x of the last annual deducted profit; ~60% real
        # growth only reaches ~1.3x by mid-year. Move to the ruler if tuned.
        ceiling = _multiply(deducted, Decimal("1.5"))
        if (
            ttm_earnings is not None
            and ceiling is not None
            and deducted > 0  # type: ignore[operator]
            and ttm_earnings > ceiling
        ):
            risks.append("ttm_earnings_above_deducted")
    label = metrics["return_metric_label"]
    assert label is None or isinstance(label, str)
    for gate in config["gate_order"]:
        for status, reason in _gate_failures(
            str(gate), ruler, calculated, counts, label
        ):
            failures.append((status, reason))
            rule_id = f"garp.{reason}"
            if rule_id not in evidence:
                evidence.append(rule_id)
    if not failures:
        return _verdict(code, "pass", None, metrics, evidence, risks)
    status, reason = failures[0]
    return _verdict(code, status, reason, metrics, evidence, risks)
