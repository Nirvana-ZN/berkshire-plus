"""Pure quality-at-a-discount classification over one frozen snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from types import MappingProxyType
from typing import TypeAlias

from discovery.metrics import (
    cash_conversion,
    coefficient_of_variation,
    mean,
    median,
    normalized_earnings,
    percentile_rank,
)
from discovery.industry import IndustryBranch, classify_industry_branch
from discovery.rulers import (
    QUALITY_FIELD_DOMAIN_POLICY,
    TrackRuler,
    quality_discount_ruler,
)
from discovery.snapshot import FrozenCompanyInput
from market.types import FinancialRow

JsonMetric: TypeAlias = None | str | tuple[str, ...]
MAX_FINANCIAL_ROWS = 128
MAX_VALUATION_ROWS = 240
MAX_DECIMAL_CHARS = 256
MAX_DECIMAL_DIGITS = 256
MAX_DECIMAL_EXPONENT = 1024
MAX_ARITHMETIC_PRECISION = 2048

_FIELD_KEYS = frozenset(
    {
        "price",
        "pe",
        "pb",
        "listing_date",
        "financial_history",
        "valuation_history",
    }
)
_SOURCE_KEYS = frozenset(
    {"price", "security", "financial_history", "valuation_history"}
)
_AS_OF_KEYS = frozenset(
    {
        "price",
        "security",
        "financial",
        "valuation",
        "valuation_fetched_at",
        "fill",
    }
)
_SECURITY_KEYS = frozenset(
    {
        "exchange",
        "listing_date",
        "is_st",
        "is_delisting",
        "trading_status",
        "is_suspended",
        "last_trade_date",
        "industry_version",
        "industry_as_of",
        "security_source",
        "market_status_code",
        "snapshot_at",
    }
)
_FINANCIAL_ROW_KEYS = frozenset(
    {
        "period",
        "period_type",
        "source_as_of",
        "values",
        "provenance",
        "field_sources",
    }
)
_FINANCIAL_VALUE_KEYS = frozenset(
    field.name
    for field in fields(FinancialRow)
    if field.name not in {"code", "period", "period_type", "source_as_of"}
)
_FIELD_SOURCE_KEYS = frozenset(
    {
        "source",
        "report",
        "field",
        "unit",
        "consolidated_basis",
        "period_type",
        "financial_sector_substitute",
        "source_as_of",
        "trusted",
    }
)
_VALUATION_ROW_KEYS = frozenset(
    {
        "code",
        "month",
        "trade_date",
        "pe",
        "pb",
        "price",
        "source",
        "ratio_method",
        "fetched_at",
    }
)
_FINANCIAL_FIELD_DOMAINS = MappingProxyType(
    {
        "roe": ("signed_economic", None),
        "net_income": ("signed_economic", None),
        "ocf": ("signed_economic", None),
        "shares": ("positive", "shares_not_positive"),
        "cash": ("nonnegative", "cash_negative"),
        "short_loan": ("nonnegative", "short_loan_negative"),
        "long_loan": ("nonnegative", "long_loan_negative"),
        "bond_payable": ("nonnegative", "bond_payable_negative"),
        "noncurrent_liab_1year": (
            "nonnegative",
            "noncurrent_liab_1year_negative",
        ),
        "interest_debt": ("nonnegative", "interest_debt_negative"),
        "net_assets": ("signed_economic", None),
        "interest_coverage": ("signed_economic", None),
        "gross_margin": ("signed_economic", None),
        "deducted_net_income": ("signed_economic", None),
        # Regulatory ratios remain signed measurements. Negative reported
        # values are valid inputs and deterministically fail ruler minima.
        "capital_adequacy": ("signed_regulatory_ratio", None),
        "core_tier1_capital_adequacy": (
            "signed_regulatory_ratio",
            None,
        ),
        "npl": ("nonnegative", "bank_npl_negative"),
        "nonperforming_loan_balance": (
            "nonnegative",
            "nonperforming_loan_balance_negative",
        ),
    }
)
_VALUATION_FIELD_DOMAINS = MappingProxyType(
    {
        # Loss-making or negative-equity ratios are economically possible but
        # are not usable in positive-ratio valuation histories.
        "pe": ("positive_usable", None),
        "pb": ("positive_usable", None),
        "price": ("positive", "price_not_positive"),
    }
)
QUALITY_DOMAIN_INVALID_REASONS = frozenset(
    reason
    for domains in (_FINANCIAL_FIELD_DOMAINS, _VALUATION_FIELD_DOMAINS)
    for _domain, reason in domains.values()
    if reason is not None
)
_NUMERIC_METRICS = (
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
    "roe_observations",
    "cash_conversion_observations",
    "valuation_pe_observations",
    "valuation_pb_observations",
)
_WINDOW_NAMES = (
    "roe",
    "profitability",
    "cash_conversion",
    "dilution",
    "positive_equity",
    "leverage",
    "interest_coverage",
    "margin",
    "normalized_earnings",
    "bank_capital",
    "bank_npl",
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
class _FinancialPeriod:
    fiscal_year: int
    period_end: date
    source_as_of: date
    values: Mapping[str, Decimal | None]


@dataclass(frozen=True)
class _ValuationPoint:
    month: str
    trade_date: date
    pe: Decimal | None
    pb: Decimal | None
    price: Decimal | None


class _SnapshotInvalid(ValueError):
    pass


class _DomainInvalid(_SnapshotInvalid):
    pass


class _AnnualPeriodDuplicate(_SnapshotInvalid):
    pass


@dataclass(frozen=True)
class _FinancialWindow:
    start_year: int
    end_year: int
    rows: tuple[_FinancialPeriod, ...]


def _date(value: object, label: str) -> date:
    if type(value) is not str:
        raise _SnapshotInvalid(label)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise _SnapshotInvalid(label) from error
    if parsed.isoformat() != value:
        raise _SnapshotInvalid(label)
    return parsed


def _timestamp(value: object, label: str) -> datetime:
    if type(value) is not str:
        raise _SnapshotInvalid(label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _SnapshotInvalid(label) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _SnapshotInvalid(label)
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, label: str) -> Decimal | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > MAX_DECIMAL_CHARS:
        raise _SnapshotInvalid(label)
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise _SnapshotInvalid(label) from error
    exponent = parsed.as_tuple().exponent
    if (
        not parsed.is_finite()
        or not isinstance(exponent, int)
        or len(parsed.as_tuple().digits) > MAX_DECIMAL_DIGITS
        or abs(exponent) > MAX_DECIMAL_EXPONENT
        or abs(parsed.adjusted()) > MAX_DECIMAL_EXPONENT
    ):
        raise _SnapshotInvalid(label)
    canonical = _decimal_text(parsed)
    if canonical != value:
        raise _SnapshotInvalid(label)
    return parsed


def _domain_decimal(
    value: object,
    field: str,
    domains: Mapping[str, tuple[str, str | None]],
    label: str,
) -> Decimal | None:
    parsed = _decimal(value, label)
    return _enforce_domain(parsed, field, domains)


def _enforce_domain(
    parsed: Decimal | None,
    field: str,
    domains: Mapping[str, tuple[str, str | None]],
) -> Decimal | None:
    if parsed is None:
        return None
    domain, reason = domains[field]
    if domain == "positive" and parsed <= 0:
        assert reason is not None
        raise _DomainInvalid(reason)
    if domain == "nonnegative" and parsed < 0:
        assert reason is not None
        raise _DomainInvalid(reason)
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    exponent = value.as_tuple().exponent
    if (
        not value.is_finite()
        or not isinstance(exponent, int)
        or len(value.as_tuple().digits) > MAX_ARITHMETIC_PRECISION
        or abs(exponent) > MAX_ARITHMETIC_PRECISION
        or abs(value.adjusted()) > MAX_ARITHMETIC_PRECISION
    ):
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _context(precision: int) -> Context:
    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emax=MAX_ARITHMETIC_PRECISION,
        Emin=-MAX_ARITHMETIC_PRECISION,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def _precision(
    values: Sequence[Decimal],
    *,
    operation: str,
) -> int | None:
    if not values:
        return 34
    if any(not value.is_finite() for value in values):
        return None
    if operation == "multiply":
        required = sum(len(value.as_tuple().digits) for value in values) + 8
    elif operation == "subtract":
        highest = max(value.adjusted() for value in values)
        lowest = min(value.as_tuple().exponent for value in values)
        required = highest - lowest + 8
    else:
        required = (
            sum(len(value.as_tuple().digits) for value in values)
            + abs(values[0].adjusted() - values[-1].adjusted())
            + 64
        )
    required = max(34, required)
    return required if required <= MAX_ARITHMETIC_PRECISION else None


def _divide(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator.is_zero():
        return None
    precision = _precision((numerator, denominator), operation="divide")
    if precision is None:
        return None
    try:
        with localcontext(_context(precision)) as context:
            value = context.divide(numerator, denominator)
    except DecimalException:
        return None
    return value if value.is_finite() else None


def _multiply(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    precision = _precision((left, right), operation="multiply")
    if precision is None:
        return None
    try:
        with localcontext(_context(precision)) as context:
            value = context.multiply(left, right)
    except DecimalException:
        return None
    return value if value.is_finite() else None


def _subtract(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    precision = _precision((left, right), operation="subtract")
    if precision is None:
        return None
    try:
        with localcontext(_context(precision)) as context:
            value = context.subtract(left, right)
    except DecimalException:
        return None
    return value if value.is_finite() else None


def bounded_ratio(numerator: int, denominator: int) -> Decimal | None:
    """Return an isolated Decimal ratio for bounded nonnegative counts."""

    if (
        type(numerator) is not int
        or type(denominator) is not int
        or numerator < 0
        or denominator <= 0
    ):
        return None
    return _divide(Decimal(numerator), Decimal(denominator))


def decimal_string(value: Decimal | None) -> str | None:
    """Serialize a bounded Decimal without consulting ambient context."""

    return _decimal_text(value)


def _period_end(value: object) -> tuple[int, date]:
    if type(value) is not str:
        raise _SnapshotInvalid("financial period")
    if len(value) == 4 and value.isdigit():
        year = int(value)
        try:
            return year, date(year, 12, 31)
        except ValueError as error:
            raise _SnapshotInvalid("financial period") from error
    parsed = _date(value, "financial period")
    if (parsed.month, parsed.day) != (12, 31):
        raise _SnapshotInvalid("financial period")
    return parsed.year, parsed


def _sequence(value: object, label: str, maximum: int) -> Sequence[object]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or len(value) > maximum
    ):
        raise _SnapshotInvalid(label)
    return value


def _trusted_value(
    row: Mapping[str, object], field: str
) -> Decimal | None:
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
    if field not in values:
        raise _SnapshotInvalid("financial values schema")
    raw = values[field]
    parsed = _decimal(raw, f"financial {field}")
    if parsed is None:
        return None
    field_provenance = provenance.get(field)
    metadata = sources.get(field)
    if (
        isinstance(field_provenance, (str, bytes))
        or not isinstance(field_provenance, Sequence)
        or tuple(field_provenance) != ("discovery",)
        or not isinstance(metadata, Mapping)
        or set(metadata) != _FIELD_SOURCE_KEYS
        or metadata.get("source") != "discovery"
        or metadata.get("trusted") is not True
    ):
        return None
    return _enforce_domain(
        parsed,
        field,
        _FINANCIAL_FIELD_DOMAINS,
    )


def _financial_history(
    company: FrozenCompanyInput,
    financial_as_of: date,
) -> tuple[_FinancialPeriod, ...]:
    rows = _sequence(
        company.fields["financial_history"],
        "financial history",
        MAX_FINANCIAL_ROWS,
    )
    annual: dict[int, _FinancialPeriod] = {}
    for raw in rows:
        if not isinstance(raw, Mapping) or set(raw) != _FINANCIAL_ROW_KEYS:
            raise _SnapshotInvalid("financial row schema")
        if raw["period_type"] != "annual":
            continue
        fiscal_year, period_end = _period_end(raw["period"])
        if fiscal_year in annual:
            raise _AnnualPeriodDuplicate("annual_fiscal_year_duplicate")
        source_as_of = _date(raw["source_as_of"], "financial source as-of")
        if period_end > company.snapshot_at.date() or source_as_of > financial_as_of:
            raise _SnapshotInvalid("financial look-ahead")
        values = {
            field: _trusted_value(raw, field)
            for field in _FINANCIAL_FIELD_DOMAINS
        }
        candidate = _FinancialPeriod(
            fiscal_year,
            period_end,
            source_as_of,
            MappingProxyType(values),
        )
        annual[fiscal_year] = candidate
    return tuple(annual[key] for key in sorted(annual))


def _valuation_history(
    company: FrozenCompanyInput,
    valuation_as_of: date,
    valuation_fetched_at: datetime,
    ratio_method: str,
) -> tuple[_ValuationPoint, ...]:
    rows = _sequence(
        company.fields["valuation_history"],
        "valuation history",
        MAX_VALUATION_ROWS,
    )
    deduped: dict[str, _ValuationPoint] = {}
    trade_dates: set[date] = set()
    source: str | None = None
    for raw in rows:
        if not isinstance(raw, Mapping) or set(raw) != _VALUATION_ROW_KEYS:
            raise _SnapshotInvalid("valuation row schema")
        if raw["code"] != company.code:
            raise _SnapshotInvalid("valuation code")
        month = raw["month"]
        if type(month) is not str or len(month) != 7:
            raise _SnapshotInvalid("valuation month")
        try:
            if date.fromisoformat(f"{month}-01").strftime("%Y-%m") != month:
                raise ValueError
        except ValueError as error:
            raise _SnapshotInvalid("valuation month") from error
        trade_date = _date(raw["trade_date"], "valuation trade date")
        if trade_date.strftime("%Y-%m") != month:
            raise _SnapshotInvalid("valuation month/trade-date mismatch")
        if month in deduped or trade_date in trade_dates:
            raise _SnapshotInvalid("valuation duplicate month/trade-date")
        trade_dates.add(trade_date)
        fetched_at = _timestamp(raw["fetched_at"], "valuation fetched-at")
        if (
            trade_date > valuation_as_of
            or trade_date > company.snapshot_at.date()
            or fetched_at != valuation_fetched_at
            or fetched_at > company.snapshot_at
        ):
            raise _SnapshotInvalid("valuation look-ahead")
        if type(raw["source"]) is not str or not raw["source"]:
            raise _SnapshotInvalid("valuation source")
        if type(raw["ratio_method"]) is not str or not raw["ratio_method"]:
            raise _SnapshotInvalid("valuation method")
        source = raw["source"] if source is None else source
        if raw["source"] != source:
            raise _SnapshotInvalid("valuation source mismatch")
        if raw["ratio_method"] != ratio_method:
            raise _SnapshotInvalid("valuation ratio method mismatch")
        candidate = _ValuationPoint(
            month,
            trade_date,
            _domain_decimal(
                raw["pe"], "pe", _VALUATION_FIELD_DOMAINS, "valuation pe"
            ),
            _domain_decimal(
                raw["pb"], "pb", _VALUATION_FIELD_DOMAINS, "valuation pb"
            ),
            _domain_decimal(
                raw["price"],
                "price",
                _VALUATION_FIELD_DOMAINS,
                "valuation price",
            ),
        )
        deduped[month] = candidate
    frozen_sources = company.sources["valuation_history"]
    if (
        source is not None
        and (
            isinstance(frozen_sources, (str, bytes))
            or not isinstance(frozen_sources, Sequence)
            or tuple(frozen_sources) != (source,)
            or company.sources["price"] != source
        )
    ):
        raise _SnapshotInvalid("valuation provenance mismatch")
    return tuple(
        sorted(deduped.values(), key=lambda row: (row.trade_date, row.month))
    )


def _empty_metrics(code: str, branch: str | None = None) -> dict[str, JsonMetric]:
    result: dict[str, JsonMetric] = {
        key: None for key in _NUMERIC_METRICS
    }
    result.update(
        {
            key: () if key.endswith("_available_years") else None
            for name in _WINDOW_NAMES
            for key in (
                f"{name}_window_start_year",
                f"{name}_window_end_year",
                f"{name}_available_years",
                f"{name}_available_count",
            )
        }
    )
    result.update(
        {
            "code": code,
            "branch": branch,
            "quality_branch": None,
            "latest_fiscal_year": None,
            "latest_fiscal_year_staleness": None,
            "allowed_latest_annual_staleness_years": None,
            "dilution_endpoint_start_year": None,
            "dilution_endpoint_end_year": None,
            "triggered_evidence_rule_ids": (),
            "risk_flags": (),
        }
    )
    return result


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
        code=code,
        verdict=verdict,
        reason=reason,
        metrics=MappingProxyType(values),
        evidence_rule_ids=tuple(evidence),
        risk_flags=tuple(risks),
    )


def _industry_branches(
    company: FrozenCompanyInput,
    ruler: TrackRuler,
) -> tuple[IndustryBranch, str | None, str | None]:
    shared = classify_industry_branch(company.industry)
    if company.security_status["industry_version"] != ruler.industry_version:
        return shared, None, "industry_version_mismatch"
    policies = ruler.sector_branches["branch_policies"]
    assert isinstance(policies, Mapping)
    policy = policies[shared]
    if policy == "unsupported":
        return shared, None, f"industry_branch_{shared}_unsupported"
    if policy == "insufficient":
        return shared, None, "industry_branch_unknown"
    if policy == "bank":
        return shared, "bank", None
    industry_map = ruler.sector_branches["industry_map"]
    assert isinstance(industry_map, Mapping)
    matches = [
        branch
        for branch in ("nonfinancial", "cyclical")
        if company.industry in industry_map[branch]
    ]
    if len(matches) == 1:
        return shared, matches[0], None
    return shared, None, "industry_label_unmapped"


def _complete_years(listed: date, as_of: date) -> Decimal:
    years = as_of.year - listed.year
    if (as_of.month, as_of.day) < (listed.month, listed.day):
        years -= 1
    return Decimal(years)


def _latest_pair(
    history: Sequence[_FinancialPeriod],
    numerator: str,
    denominator: str,
) -> tuple[Decimal | None, Decimal | None]:
    for row in reversed(history):
        left, right = row.values[numerator], row.values[denominator]
        if left is not None and right is not None:
            return left, right
    return None, None


def _count_pairs(
    history: Sequence[_FinancialPeriod], left: str, right: str
) -> int:
    return sum(
        row.values[left] is not None and row.values[right] is not None
        for row in history
    )


def _latest_completed_fiscal_year(snapshot_date: date) -> int:
    return (
        snapshot_date.year
        if (snapshot_date.month, snapshot_date.day) == (12, 31)
        else snapshot_date.year - 1
    )


def _available_window_rows(
    name: str,
    rows: Sequence[_FinancialPeriod],
) -> tuple[_FinancialPeriod, ...]:
    required_fields = {
        "roe": ("roe",),
        "profitability": ("net_income",),
        "cash_conversion": ("ocf", "net_income"),
        "dilution": ("shares",),
        "positive_equity": ("net_assets",),
        "leverage": ("interest_debt", "net_assets"),
        "interest_coverage": ("interest_coverage",),
        "margin": ("gross_margin",),
        "normalized_earnings": ("deducted_net_income",),
        "bank_capital": (
            "capital_adequacy",
            "core_tier1_capital_adequacy",
        ),
        "bank_npl": ("npl",),
    }
    fields = required_fields[name]
    return tuple(
        row
        for row in rows
        if all(row.values[field] is not None for field in fields)
        and (
            name != "cash_conversion"
            or (
                row.values["net_income"] is not None
                and row.values["net_income"] > 0
            )
        )
    )


def _financial_windows(
    history: Sequence[_FinancialPeriod],
    ruler: TrackRuler,
    snapshot_date: date,
) -> tuple[
    Mapping[str, _FinancialWindow],
    dict[str, JsonMetric],
    bool,
]:
    common = ruler.sector_branches["common"]
    assert isinstance(common, Mapping)
    configured = common["lookback_windows"]
    assert isinstance(configured, Mapping)
    allowed_staleness = int(common["latest_annual_staleness_years"])
    audit: dict[str, JsonMetric] = {
        "allowed_latest_annual_staleness_years": str(allowed_staleness),
    }
    if not history:
        return MappingProxyType({}), audit, False
    latest_year = history[-1].fiscal_year
    staleness = _latest_completed_fiscal_year(snapshot_date) - latest_year
    audit["latest_fiscal_year"] = str(latest_year)
    audit["latest_fiscal_year_staleness"] = str(max(0, staleness))
    windows: dict[str, _FinancialWindow] = {}
    for name in _WINDOW_NAMES:
        years = int(configured[name])
        start_year = latest_year - years + 1
        rows = tuple(
            row
            for row in history
            if start_year <= row.fiscal_year <= latest_year
        )
        windows[name] = _FinancialWindow(start_year, latest_year, rows)
        available = _available_window_rows(name, rows)
        audit[f"{name}_window_start_year"] = str(start_year)
        audit[f"{name}_window_end_year"] = str(latest_year)
        audit[f"{name}_available_years"] = tuple(
            str(row.fiscal_year) for row in available
        )
        audit[f"{name}_available_count"] = str(len(available))
    return (
        MappingProxyType(windows),
        audit,
        staleness > allowed_staleness,
    )


def _metric_values(
    company: FrozenCompanyInput,
    ruler: TrackRuler,
    branch: str,
    windows: Mapping[str, _FinancialWindow],
    valuations: Sequence[_ValuationPoint],
    listing_date: date,
) -> tuple[
    dict[str, Decimal | None],
    dict[str, int],
    dict[str, JsonMetric],
]:
    values: dict[str, Decimal | None] = {
        key: None for key in _NUMERIC_METRICS
    }
    counts: dict[str, int] = {}
    values["listing_years"] = _complete_years(
        listing_date, company.snapshot_at.date()
    )
    roe_rows = windows.get("roe")
    roe_history = () if roe_rows is None else roe_rows.rows
    roe = tuple(row.values["roe"] for row in roe_history)
    values["roe_mean"] = mean(roe)
    values["roe_cv"] = coefficient_of_variation(roe)
    counts["roe"] = sum(value is not None for value in roe)

    profitability_rows = windows.get("profitability")
    profitability_history = (
        () if profitability_rows is None else profitability_rows.rows
    )
    income = tuple(
        row.values["net_income"] for row in profitability_history
    )
    valid_income = tuple(value for value in income if value is not None)
    counts["net_income"] = len(valid_income)
    positive_income_count = sum(value > 0 for value in valid_income)
    values["positive_net_income_observations"] = (
        Decimal(positive_income_count) if valid_income else None
    )
    values["latest_net_income"] = (
        profitability_history[-1].values["net_income"]
        if profitability_history
        else None
    )
    equity_rows = windows.get("positive_equity")
    equity_history = () if equity_rows is None else equity_rows.rows
    net_assets = tuple(
        row.values["net_assets"]
        for row in equity_history
        if row.values["net_assets"] is not None
    )
    counts["net_assets"] = len(net_assets)
    values["latest_net_assets"] = (
        equity_history[-1].values["net_assets"] if equity_history else None
    )

    audit: dict[str, JsonMetric] = {}
    if branch != "bank":
        cash_rows = windows.get("cash_conversion")
        cash_history = () if cash_rows is None else cash_rows.rows
        positive_pairs = tuple(
            (row.values["ocf"], row.values["net_income"])
            for row in cash_history
            if row.values["ocf"] is not None
            and row.values["net_income"] is not None
            and row.values["net_income"] > 0
        )
        counts["cash_conversion"] = len(positive_pairs)
        values["cash_conversion"] = cash_conversion(
            tuple(pair[0] for pair in positive_pairs),
            tuple(pair[1] for pair in positive_pairs),
            ruler.min_observations["cash_conversion"],
        )
        dilution_rows = windows.get("dilution")
        dilution_history = () if dilution_rows is None else dilution_rows.rows
        share_rows = tuple(
            row
            for row in dilution_history
            if row.values["shares"] is not None
        )
        shares = tuple(row.values["shares"] for row in share_rows)
        counts["dilution"] = len(shares)
        if len(shares) >= ruler.min_observations["dilution"]:
            ratio = _divide(shares[-1], shares[0])
            values["dilution_rate"] = _subtract(ratio, Decimal(1))
            audit["dilution_endpoint_start_year"] = str(
                share_rows[0].fiscal_year
            )
            audit["dilution_endpoint_end_year"] = str(
                share_rows[-1].fiscal_year
            )
        leverage_rows = windows.get("leverage")
        leverage_history = () if leverage_rows is None else leverage_rows.rows
        debt, equity = _latest_pair(
            leverage_history, "interest_debt", "net_assets"
        )
        counts["leverage"] = _count_pairs(
            leverage_history, "interest_debt", "net_assets"
        )
        if (
            equity is not None
            and equity > Decimal(ruler.thresholds["net_assets_positive_min"])
        ):
            values["debt_to_equity"] = _divide(debt, equity)
        interest_rows = windows.get("interest_coverage")
        interest_history = (
            () if interest_rows is None else interest_rows.rows
        )
        direct_coverage = tuple(
            row.values["interest_coverage"]
            for row in interest_history
            if row.values["interest_coverage"] is not None
        )
        counts["interest_coverage"] = len(direct_coverage)
        values["interest_coverage"] = (
            direct_coverage[-1] if direct_coverage else None
        )
        margin_rows = windows.get("margin")
        margin_history = () if margin_rows is None else margin_rows.rows
        margins = tuple(
            row.values["gross_margin"]
            for row in margin_history
            if row.values["gross_margin"] is not None
        )
        counts["margin"] = len(margins)
        if len(margins) >= ruler.min_observations["margin"]:
            values["margin_change"] = _subtract(margins[-1], margins[0])

    pe = tuple(
        row.pe for row in valuations if row.pe is not None and row.pe > 0
    )
    pb = tuple(
        row.pb for row in valuations if row.pb is not None and row.pb > 0
    )
    counts["valuation_pe"] = len(pe)
    counts["valuation_pb"] = len(pb)
    current_pe = _domain_decimal(
        company.fields["pe"], "pe", _VALUATION_FIELD_DOMAINS, "current pe"
    )
    current_pb = _domain_decimal(
        company.fields["pb"], "pb", _VALUATION_FIELD_DOMAINS, "current pb"
    )
    if current_pe is not None and current_pe > 0:
        values["pe_percentile"] = percentile_rank(current_pe, pe)
        values["pe_median_ratio"] = _divide(current_pe, median(pe))
    if current_pb is not None and current_pb > 0:
        values["pb_percentile"] = percentile_rank(current_pb, pb)
        values["pb_median_ratio"] = _divide(current_pb, median(pb))

    if branch == "cyclical":
        normalized_rows = windows.get("normalized_earnings")
        normalized_history = (
            () if normalized_rows is None else normalized_rows.rows
        )
        earnings = tuple(
            row.values["deducted_net_income"]
            for row in normalized_history
        )
        values["normalized_earnings"] = normalized_earnings(
            earnings,
            ruler.min_observations["normalized_earnings"],
        )
        latest_shares = next(
            (
                row.values["shares"]
                for row in reversed(normalized_history)
                if row.values["shares"] is not None
            ),
            None,
        )
        price = _domain_decimal(
            company.fields["price"],
            "price",
            _VALUATION_FIELD_DOMAINS,
            "current price",
        )
        earnings_floor = Decimal(ruler.thresholds["normalized_earnings_min"])
        if (
            price is not None
            and latest_shares is not None
            and values["normalized_earnings"] is not None
            and values["normalized_earnings"] > earnings_floor
        ):
            market_value = _multiply(price, latest_shares)
            values["normalized_pe"] = _divide(
                market_value,
                values["normalized_earnings"],
            )
    if branch == "bank":
        bank_capital_rows = windows.get("bank_capital")
        bank_capital_history = (
            () if bank_capital_rows is None else bank_capital_rows.rows
        )
        bank_npl_rows = windows.get("bank_npl")
        bank_npl_history = (
            () if bank_npl_rows is None else bank_npl_rows.rows
        )
        for metric, field in (
            ("bank_capital_adequacy", "capital_adequacy"),
            (
                "bank_core_tier1_capital_adequacy",
                "core_tier1_capital_adequacy",
            ),
            ("bank_npl", "npl"),
        ):
            history = (
                bank_npl_history
                if field == "npl"
                else bank_capital_history
            )
            values[metric] = (
                history[-1].values[field] if history else None
            )
        counts["bank_capital"] = sum(
            row.values["capital_adequacy"] is not None
            and row.values["core_tier1_capital_adequacy"] is not None
            for row in bank_capital_history
        )
        counts["bank_npl"] = sum(
            row.values["npl"] is not None for row in bank_npl_history
        )
    return values, counts, audit


def _missing(
    ruler: TrackRuler, metric: str, reason: str
) -> tuple[str, str] | None:
    return (
        None
        if ruler.missing_policy[metric] == "skip"
        else ("insufficient", reason)
    )


def _valuation_failures(
    ruler: TrackRuler,
    branch: str,
    metrics: Mapping[str, Decimal | None],
    counts: Mapping[str, int],
) -> list[tuple[str, str]]:
    config = ruler.sector_branches[branch]
    assert isinstance(config, Mapping)
    policy = config["valuation_policy"]
    selected = (
        ("pe", "pb")
        if policy in {"both", "either"}
        else (str(policy),)
    )
    ratio_failures: dict[str, list[tuple[str, str]]] = {}
    ratio_pass: dict[str, bool] = {}
    for ratio in selected:
        failures: list[tuple[str, str]] = []
        minimum = ruler.min_observations[f"valuation_{ratio}"]
        for suffix in ("percentile", "median_ratio"):
            metric = f"{ratio}_{suffix}"
            value = metrics[metric]
            if counts[f"valuation_{ratio}"] < minimum or value is None:
                missing = _missing(
                    ruler,
                    metric,
                    f"valuation_{ratio}_{suffix}_insufficient",
                )
                if missing is not None:
                    failures.append(missing)
                continue
            if value > Decimal(ruler.thresholds[f"{ratio}_{suffix}_max"]):
                reason = (
                    f"valuation_{ratio}_percentile_above_max"
                    if suffix == "percentile"
                    else f"valuation_{ratio}_not_discounted"
                )
                failures.append(("drop", reason))
        ratio_failures[ratio] = failures
        ratio_pass[ratio] = not failures
    if policy == "either" and any(ratio_pass.values()):
        return []
    return [
        failure
        for ratio in selected
        for failure in ratio_failures[ratio]
    ]


def _gate_failures(
    gate: str,
    company: FrozenCompanyInput,
    ruler: TrackRuler,
    branch: str,
    metrics: Mapping[str, Decimal | None],
    counts: Mapping[str, int],
) -> list[tuple[str, str]]:
    threshold = ruler.thresholds
    if gate == "listing_age":
        value = metrics["listing_years"]
        if value is None:
            missing = _missing(ruler, "listing_years", "listing_date_invalid")
            return [] if missing is None else [missing]
        return (
            [("insufficient", "listing_history_insufficient")]
            if value < Decimal(threshold["listing_years_min"])
            else []
        )
    if gate in {"roe_mean", "bank_roe"}:
        minimum_key = "bank_roe" if gate == "bank_roe" else "roe"
        metric_key = "roe_mean"
        if counts["roe"] < ruler.min_observations[minimum_key]:
            missing = _missing(ruler, metric_key, "roe_insufficient")
            return [] if missing is None else [missing]
        value = metrics[metric_key]
        if value is None:
            missing = _missing(ruler, metric_key, "roe_insufficient")
            return [] if missing is None else [missing]
        floor = threshold[
            "bank_roe_min" if gate == "bank_roe" else "roe_mean_min"
        ]
        return [("drop", "roe_mean_below_min")] if value < Decimal(floor) else []
    if gate == "roe_stability":
        value = metrics["roe_cv"]
        if counts["roe"] < ruler.min_observations["roe"] or value is None:
            missing = _missing(ruler, "roe_cv", "roe_stability_insufficient")
            return [] if missing is None else [missing]
        return (
            [("drop", "roe_stability_above_max")]
            if value > Decimal(threshold["roe_cv_max"])
            else []
        )
    if gate == "profitability":
        failures: list[tuple[str, str]] = []
        positive = metrics["positive_net_income_observations"]
        if (
            counts["net_income"]
            < ruler.min_observations["positive_net_income"]
            or positive is None
        ):
            missing = _missing(
                ruler,
                "positive_net_income_observations",
                "positive_net_income_observations_insufficient",
            )
            if missing is not None:
                failures.append(missing)
        elif positive < Decimal(
            ruler.min_observations["positive_net_income"]
        ):
            failures.append(
                ("drop", "positive_net_income_observations_below_min")
            )
        config = ruler.sector_branches[branch]
        assert isinstance(config, Mapping)
        if config["latest_net_income_policy"] == "require_positive":
            latest = metrics["latest_net_income"]
            if latest is None:
                missing = _missing(
                    ruler,
                    "latest_net_income",
                    "latest_net_income_insufficient",
                )
                if missing is not None:
                    failures.append(missing)
            elif latest <= Decimal(threshold["net_income_positive_min"]):
                failures.append(("drop", "latest_net_income_not_positive"))
        return failures
    if gate == "positive_equity":
        equity = metrics["latest_net_assets"]
        if (
            counts["net_assets"] < ruler.min_observations["net_assets"]
            or equity is None
        ):
            missing = _missing(
                ruler,
                "latest_net_assets",
                "latest_net_assets_insufficient",
            )
            return [] if missing is None else [missing]
        return (
            [("drop", "latest_net_assets_not_positive")]
            if equity <= Decimal(threshold["net_assets_positive_min"])
            else []
        )
    simple = {
        "cash_conversion": (
            "cash_conversion",
            "cash_conversion",
            "cash_conversion_min",
            "below",
        ),
        "dilution": (
            "dilution_rate",
            "dilution",
            "dilution_rate_max",
            "above",
        ),
        "leverage": (
            "debt_to_equity",
            "leverage",
            "debt_to_equity_max",
            "above",
        ),
        "interest_coverage": (
            "interest_coverage",
            "interest_coverage",
            "interest_coverage_min",
            "below",
        ),
    }
    if gate in simple:
        metric, minimum, threshold_name, direction = simple[gate]
        value = metrics[metric]
        if counts[minimum] < ruler.min_observations[minimum] or value is None:
            missing = _missing(ruler, metric, f"{minimum}_insufficient")
            return [] if missing is None else [missing]
        failed = (
            value < Decimal(threshold[threshold_name])
            if direction == "below"
            else value > Decimal(threshold[threshold_name])
        )
        return [("drop", f"{metric}_{direction}_{'min' if direction == 'below' else 'max'}")] if failed else []
    if gate == "margin":
        value = metrics["margin_change"]
        if counts["margin"] < ruler.min_observations["margin"] or value is None:
            missing = _missing(ruler, "margin_change", "margin_insufficient")
            return [] if missing is None else [missing]
        deteriorated = value < Decimal(
            threshold["margin_deterioration_max"]
        ).copy_negate()
        config = ruler.sector_branches[branch]
        assert isinstance(config, Mapping)
        return (
            [("drop", "margin_deterioration")]
            if deteriorated
            and config["margin_deterioration_policy"] == "drop"
            else []
        )
    if gate in {"valuation", "bank_valuation"}:
        return _valuation_failures(ruler, branch, metrics, counts)
    if gate == "normalized_earnings_positive":
        value = metrics["normalized_earnings"]
        if value is None:
            missing = _missing(
                ruler,
                "normalized_earnings",
                "normalized_earnings_insufficient",
            )
            return [] if missing is None else [missing]
        return (
            [("drop", "normalized_earnings_not_positive")]
            if value <= Decimal(threshold["normalized_earnings_min"])
            else []
        )
    if gate == "cyclical_valuation":
        failures = _valuation_failures(ruler, branch, metrics, counts)
        normalized_earnings_value = metrics["normalized_earnings"]
        if (
            normalized_earnings_value is not None
            and normalized_earnings_value
            <= Decimal(threshold["normalized_earnings_min"])
        ):
            return failures
        value = metrics["normalized_pe"]
        if value is None:
            missing = _missing(
                ruler, "normalized_pe", "normalized_pe_insufficient"
            )
            if missing is not None:
                failures.append(missing)
        elif value > Decimal(threshold["normalized_pe_max"]):
            failures.append(("drop", "normalized_pe_above_max"))
        return failures
    if gate == "bank_capital":
        failures = []
        for metric, threshold_name, reason in (
            (
                "bank_capital_adequacy",
                "bank_capital_adequacy_min",
                "bank_capital_adequacy_below_min",
            ),
            (
                "bank_core_tier1_capital_adequacy",
                "bank_core_tier1_min",
                "bank_core_tier1_below_min",
            ),
        ):
            value = metrics[metric]
            if (
                counts["bank_capital"]
                < ruler.min_observations["bank_capital"]
                or value is None
            ):
                missing = _missing(
                    ruler, metric, f"{metric}_insufficient"
                )
                if missing is not None:
                    failures.append(missing)
            elif value < Decimal(threshold[threshold_name]):
                failures.append(("drop", reason))
        return failures
    if gate == "bank_npl":
        value = metrics["bank_npl"]
        if counts["bank_npl"] < ruler.min_observations["bank_npl"] or value is None:
            missing = _missing(ruler, "bank_npl", "bank_npl_insufficient")
            return [] if missing is None else [missing]
        return (
            [("drop", "bank_npl_above_max")]
            if value > Decimal(threshold["bank_npl_max"])
            else []
        )
    raise ValueError(f"unsupported quality gate: {gate}")


def classify(company: FrozenCompanyInput, ruler: TrackRuler) -> TrackVerdict:
    """Classify one immutable company without I/O or mutable global state."""

    ruler = quality_discount_ruler(ruler)
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
        ):
            raise _SnapshotInvalid("snapshot schema")
        if (
            set(company.fields) != _FIELD_KEYS
            or set(company.sources) != _SOURCE_KEYS
            or set(company.as_of) != _AS_OF_KEYS
            or set(company.security_status) != _SECURITY_KEYS
            or company.fields["listing_date"]
            != company.security_status["listing_date"]
            or company.algorithm_version != ruler.algorithm_version
        ):
            raise _SnapshotInvalid("snapshot schema")
        shared_branch, quality_branch, branch_reason = _industry_branches(
            company, ruler
        )
        metrics["branch"] = shared_branch
        metrics["quality_branch"] = quality_branch
        if branch_reason is not None:
            return _verdict(
                company.code,
                "insufficient",
                branch_reason,
                metrics,
                (f"quality.{branch_reason}",),
            )
        assert quality_branch is not None
        financial_as_of = _date(company.as_of["financial"], "financial as-of")
        valuation_as_of = _date(company.as_of["valuation"], "valuation as-of")
        valuation_fetched_at = _timestamp(
            company.as_of["valuation_fetched_at"],
            "valuation fetched-at",
        )
        security_snapshot_at = _timestamp(
            company.security_status["snapshot_at"],
            "security snapshot-at",
        )
        fill_as_of = _timestamp(company.as_of["fill"], "fill as-of")
        listing_date = _date(company.fields["listing_date"], "listing date")
        _domain_decimal(
            company.fields["price"],
            "price",
            _VALUATION_FIELD_DOMAINS,
            "current price",
        )
        _domain_decimal(
            company.fields["pe"],
            "pe",
            _VALUATION_FIELD_DOMAINS,
            "current pe",
        )
        _domain_decimal(
            company.fields["pb"],
            "pb",
            _VALUATION_FIELD_DOMAINS,
            "current pb",
        )
        if (
            listing_date > company.snapshot_at.date()
            or financial_as_of > company.snapshot_at.date()
            or security_snapshot_at > company.snapshot_at
            or fill_as_of != company.snapshot_at
            or company.sources["financial_history"]
            != "checkpoint_payload:field_sources"
            or company.security_status["industry_as_of"]
            != company.as_of["security"]
        ):
            raise _SnapshotInvalid("snapshot look-ahead")
        if valuation_as_of > company.snapshot_at.date():
            raise _SnapshotInvalid("valuation look-ahead")
        financial = _financial_history(company, financial_as_of)
        windows, window_metrics, stale = _financial_windows(
            financial,
            ruler,
            company.snapshot_at.date(),
        )
        metrics.update(window_metrics)
        if stale:
            return _verdict(
                code,
                "insufficient",
                "financial_history_stale",
                metrics,
                ("quality.financial_history_stale",),
            )
        common_config = ruler.sector_branches["common"]
        assert isinstance(common_config, Mapping)
        assert common_config["field_domain_policy"] == QUALITY_FIELD_DOMAIN_POLICY
        valuations = _valuation_history(
            company,
            valuation_as_of,
            valuation_fetched_at,
            str(common_config["valuation_ratio_method"]),
        )
        if valuations:
            latest = valuations[-1]
            if (
                latest.trade_date != valuation_as_of
                or company.as_of["price"] != latest.trade_date.isoformat()
                or _domain_decimal(
                    company.fields["pe"],
                    "pe",
                    _VALUATION_FIELD_DOMAINS,
                    "current pe",
                )
                != latest.pe
                or _domain_decimal(
                    company.fields["pb"],
                    "pb",
                    _VALUATION_FIELD_DOMAINS,
                    "current pb",
                )
                != latest.pb
                or _domain_decimal(
                    company.fields["price"],
                    "price",
                    _VALUATION_FIELD_DOMAINS,
                    "current price",
                )
                != latest.price
            ):
                raise _SnapshotInvalid("current valuation mismatch")
        decimal_metrics, counts, calculation_audit = _metric_values(
            company,
            ruler,
            quality_branch,
            windows,
            valuations,
            listing_date,
        )
        metrics.update(calculation_audit)
    except _SnapshotInvalid as error:
        reason = str(error)
        evidence: tuple[str, ...] = ()
        if isinstance(error, _DomainInvalid):
            evidence = (f"quality.{reason}",)
        elif isinstance(error, _AnnualPeriodDuplicate):
            evidence = (f"quality.{reason}",)
        elif reason == "valuation ratio method mismatch":
            reason = "valuation_ratio_method_mismatch"
            evidence = ("quality.valuation_ratio_method_mismatch",)
        elif "valuation" in reason:
            reason = "valuation_history_invalid"
            evidence = ("quality.valuation_history_invalid",)
        elif "financial" in reason:
            reason = "financial_history_invalid"
        else:
            reason = "snapshot_schema_invalid"
        return _verdict(code, "insufficient", reason, metrics, evidence)

    for key, value in decimal_metrics.items():
        metrics[key] = _decimal_text(value)
    metrics["roe_observations"] = str(counts["roe"])
    metrics["cash_conversion_observations"] = str(
        counts.get("cash_conversion", 0)
    )
    metrics["valuation_pe_observations"] = str(counts["valuation_pe"])
    metrics["valuation_pb_observations"] = str(counts["valuation_pb"])

    config = ruler.sector_branches[quality_branch]
    assert isinstance(config, Mapping)
    evidence: list[str] = []
    risks: list[str] = []
    failures: list[tuple[str, str]] = []
    margin = decimal_metrics["margin_change"]
    for gate in config["gate_order"]:
        if (
            gate == "margin"
            and margin is not None
            and margin
            < Decimal(
                ruler.thresholds["margin_deterioration_max"]
            ).copy_negate()
        ):
            evidence.append("quality.margin_deterioration")
            risks.append("margin_deterioration")
        if (
            gate == "cyclical_valuation"
            and decimal_metrics["normalized_earnings"] is not None
            and decimal_metrics["normalized_earnings"]
            > Decimal(ruler.thresholds["normalized_earnings_min"])
        ):
            evidence.append("quality.cyclical_normalized_earnings")
        gate_failures = _gate_failures(
            gate,
            company,
            ruler,
            quality_branch,
            decimal_metrics,
            counts,
        )
        for status, reason in gate_failures:
            failures.append((status, reason))
            rule_id = f"quality.{reason}"
            if rule_id not in evidence:
                evidence.append(rule_id)
    if not failures:
        return _verdict(code, "pass", None, metrics, evidence, risks)
    status, reason = failures[0]
    return _verdict(code, status, reason, metrics, evidence, risks)
