"""Deterministic evidence cards derived only from frozen ruler inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from discovery.tracks.quality_discount import (
    MAX_ARITHMETIC_PRECISION,
    _divide as _bounded_divide,
    _subtract as _bounded_subtract,
)


EVIDENCE_RULE_VERSION = "deterministic-evidence-v2"
ALLOWED_RISK_CATEGORIES = frozenset(
    {
        "metric_near_threshold",
        "worsening_trend",
        "sector_specific_risk",
        "missing_non_gating_evidence",
    }
)
FALLBACK_RISK_TEXT = "尚无结构化反方证据"
_OBSERVATION_REQUIREMENTS = MappingProxyType(
    {
        "pe_percentile": ("valuation_pe", "valuation_pe_observations"),
        "pe_median_ratio": ("valuation_pe", "valuation_pe_observations"),
        "pb_percentile": ("valuation_pb", "valuation_pb_observations"),
        "pb_median_ratio": ("valuation_pb", "valuation_pb_observations"),
        "roe_mean": ("roe", "roe_observations"),
        "roe_cv": ("roe", "roe_observations"),
        "cash_conversion": (
            "cash_conversion",
            "cash_conversion_observations",
        ),
        "bank_capital_adequacy": (
            "bank_capital",
            "bank_capital_available_count",
        ),
        "bank_core_tier1_capital_adequacy": (
            "bank_capital",
            "bank_capital_available_count",
        ),
        "bank_npl": ("bank_npl", "bank_npl_available_count"),
        "normalized_pe": (
            "normalized_earnings",
            "normalized_earnings_observations",
        ),
        "cumulative_fcf": ("fcf", "fcf_observations"),
        "net_debt_to_normalized_fcf": (
            "net_debt",
            "net_debt_observations",
        ),
        "interest_coverage": (
            "interest_coverage",
            "interest_coverage_observations",
        ),
        "npl_change": ("bank_npl", "bank_npl_observations"),
        "dilution_rate": ("dilution", "dilution_observations"),
        "revenue_decline": ("revenue", "revenue_observations"),
        "one_time_profit_share": (
            "one_time_profit",
            "one_time_profit_observations",
        ),
        "impairment_share": (
            "impairment",
            "impairment_observations",
        ),
        "subsidy_disposal_share": (
            "subsidy_disposal",
            "subsidy_disposal_observations",
        ),
    }
)
_QUALITY_BANK_FACTS = (
    ("pb_percentile", ("pb_percentile",)),
    ("roe_mean", ("roe_mean",)),
    (
        "bank_capital",
        (
            "bank_capital_adequacy",
            "bank_core_tier1_capital_adequacy",
        ),
    ),
    ("bank_npl", ("bank_npl",)),
)
_BANK_GATE_ORDER = (
    "listing_age",
    "positive_equity",
    "bank_roe",
    "bank_capital",
    "bank_npl",
    "bank_valuation",
)


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    category: str | None
    rule_id: str
    input_fields: Mapping[str, object]
    display_text: str
    rule_version: str
    as_of: str
    data_completeness: str


@dataclass(frozen=True)
class EvidenceCard:
    code: str
    verdict: str
    reason: str | None
    items: tuple[EvidenceItem, ...]


def _decimal(value: object) -> Decimal | None:
    if type(value) not in {str, int}:
        return None
    if (
        type(value) is str
        and len(value) > MAX_ARITHMETIC_PRECISION
    ) or (
        type(value) is int
        and value.bit_length() > MAX_ARITHMETIC_PRECISION * 4
    ):
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    exponent = result.as_tuple().exponent
    return (
        result
        if result.is_finite()
        and isinstance(exponent, int)
        and len(result.as_tuple().digits) <= MAX_ARITHMETIC_PRECISION
        and abs(exponent) <= MAX_ARITHMETIC_PRECISION
        and abs(result.adjusted()) <= MAX_ARITHMETIC_PRECISION
        else None
    )


def _sources(field: str) -> tuple[str, ...]:
    if field == "normalized_pe":
        return ("financial", "valuation")
    if field.startswith(("pe_", "pb_")):
        return ("valuation",)
    if field == "listing_years":
        return ("security",)
    return ("financial",)


def _provenance(
    fields: Sequence[str],
    source_as_of: Mapping[str, str],
) -> tuple[dict[str, object], str]:
    sources = tuple(
        dict.fromkeys(
            source
            for field in fields
            for source in _sources(field)
        )
    )
    if len(sources) == 1:
        source = sources[0]
        return {"source": source}, source_as_of[source]
    dates = {source: source_as_of[source] for source in sources}
    return {
        "sources": sources,
        "source_as_of": dates,
    }, min(dates.values())


def _risk_specs(
    track: str,
    branch: object,
) -> tuple[tuple[str, str, str], ...]:
    if track != "deep_value":
        return ()
    return (
        (
            (
                "normalized_pe",
                "normalized_pe_max",
                "max",
            ),
            (
                "net_debt_to_normalized_fcf",
                "net_debt_to_normalized_fcf_max",
                "max",
            ),
            (
                "interest_coverage",
                "interest_coverage_min",
                "min",
            ),
            ("dilution_rate", "dilution_rate_max", "max"),
            ("revenue_decline", "revenue_decline_max", "max"),
            (
                "one_time_profit_share",
                "one_time_profit_share_max",
                "max",
            ),
            ("impairment_share", "impairment_share_max", "max"),
            (
                "subsidy_disposal_share",
                "subsidy_disposal_share_max",
                "max",
            ),
        )
        if branch == "nonfinancial"
        else (
            ("pb_percentile", "pb_percentile_max", "max"),
            ("pb_median_ratio", "pb_median_ratio_max", "max"),
            ("roe_mean", "bank_roe_min", "min"),
            ("npl_change", "bank_npl_increase_max", "max"),
            (
                "bank_capital_adequacy",
                "bank_capital_adequacy_min",
                "min",
            ),
            (
                "bank_core_tier1_capital_adequacy",
                "bank_core_tier1_min",
                "min",
            ),
        )
        if branch == "financial"
        else ()
    )


def _source_dates(value: Mapping[str, object]) -> Mapping[str, str]:
    required = ("security", "financial", "valuation")
    if any(
        type(value.get(key)) is not str or not value[key]
        for key in required
    ):
        raise ValueError("evidence source as-of dates are incomplete")
    return MappingProxyType({key: str(value[key]) for key in required})


def _item(
    *,
    kind: str,
    category: str | None,
    rule_id: str,
    inputs: Mapping[str, object],
    text: str,
    as_of: str,
    completeness: str,
) -> EvidenceItem:
    return EvidenceItem(
        kind=kind,
        category=category,
        rule_id=rule_id,
        input_fields=MappingProxyType(dict(inputs)),
        display_text=text,
        rule_version=EVIDENCE_RULE_VERSION,
        as_of=as_of,
        data_completeness=completeness,
    )


def _field_order(ruler: Mapping[str, object]) -> tuple[str, ...]:
    order = ruler.get("order")
    formulas = ruler.get("formulas")
    if (
        isinstance(order, (str, bytes))
        or not isinstance(order, Sequence)
        or not isinstance(formulas, Mapping)
    ):
        raise ValueError("evidence ruler shape is invalid")
    result = []
    for item in order:
        if (
            isinstance(item, (str, bytes))
            or not isinstance(item, Sequence)
            or len(item) != 3
        ):
            raise ValueError("evidence ruler order is invalid")
        field = item[0]
        if field != "code":
            if type(field) is not str or field not in formulas:
                raise ValueError("ordered evidence field has no formula")
            result.append(field)
    return tuple(result)


def _fact_specs(
    track: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    branch = metrics.get("quality_branch") or metrics.get("branch")
    branches = ruler.get("sector_branches")
    if isinstance(branches, Mapping) and type(branch) is str:
        config = branches.get(branch)
        if isinstance(config, Mapping) and "evidence_fields" in config:
            evidence_fields = config["evidence_fields"]
            formulas = ruler.get("formulas")
            if (
                isinstance(evidence_fields, (str, bytes))
                or not isinstance(evidence_fields, Sequence)
                or not isinstance(formulas, Mapping)
                or not 3 <= len(evidence_fields) <= 4
                or any(
                    type(field) is not str or field not in formulas
                    for field in evidence_fields
                )
            ):
                raise ValueError("branch evidence fields are invalid")
            return tuple((field, (field,)) for field in evidence_fields)
    if track != "quality_discount" or branch != "bank":
        return tuple((field, (field,)) for field in _field_order(ruler))

    if not isinstance(branches, Mapping):
        raise ValueError("bank evidence branch policy is invalid")
    bank = branches.get("bank")
    policies = branches.get("branch_policies")
    if (
        not isinstance(bank, Mapping)
        or not isinstance(policies, Mapping)
        or tuple(bank.get("gate_order", ())) != _BANK_GATE_ORDER
        or bank.get("valuation_policy") != "pb"
        or policies.get("bank") != "bank"
    ):
        raise ValueError("bank evidence branch policy is invalid")
    return _QUALITY_BANK_FACTS


def _reliable_inputs(
    field: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    branch: object,
) -> Mapping[str, object] | None:
    policies = ruler.get("missing_policy")
    minimums = ruler.get("min_observations")
    if not isinstance(policies, Mapping) or not isinstance(minimums, Mapping):
        raise ValueError("evidence ruler reliability policy is invalid")
    if policies.get(field) not in {"insufficient", "skip"}:
        raise ValueError("ordered evidence field has no missing policy")
    value = metrics.get(field)
    if type(value) not in {str, int}:
        return None
    requirement = _OBSERVATION_REQUIREMENTS.get(field)
    if requirement is None:
        return None
    minimum_name, count_name = requirement
    if field == "roe_mean" and branch == "financial":
        minimum_name = "bank_roe"
        count_name = "bank_roe_observations"
    elif field == "roe_mean" and branch == "bank":
        minimum_name = "bank_roe"
    minimum = minimums.get(minimum_name)
    count = _decimal(metrics.get(count_name))
    if (
        type(minimum) is not int
        or minimum < 1
        or count is None
        or count != count.to_integral_value()
        or count < minimum
    ):
        return None
    return MappingProxyType(
        {
            field: value,
            count_name: metrics[count_name],
            f"min_observations.{minimum_name}": minimum,
        }
    )


def _fact_items(
    track: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    source_as_of: Mapping[str, str],
) -> tuple[EvidenceItem, ...]:
    formulas = ruler["formulas"]
    assert isinstance(formulas, Mapping)
    result = []
    branch = metrics.get("quality_branch") or metrics.get("branch")
    for rule_name, fields in _fact_specs(track, metrics, ruler):
        reliable = [
            _reliable_inputs(field, metrics, ruler, branch)
            for field in fields
        ]
        if any(inputs is None for inputs in reliable):
            continue
        inputs = {
            key: value
            for reliable_inputs in reliable
            if reliable_inputs is not None
            for key, value in reliable_inputs.items()
        }
        inputs.update(
            {
                f"formula.{field}": formulas[field]
                for field in fields
                if field in formulas
            }
        )
        provenance, as_of = _provenance(fields, source_as_of)
        inputs.update(provenance)
        result.append(
            _item(
                kind="fact",
                category=None,
                rule_id=f"{track}.fact.{rule_name}",
                inputs=inputs,
                text=",".join(f"{field}={metrics[field]}" for field in fields),
                as_of=as_of,
                completeness="complete",
            )
        )
        if len(result) == 4:
            break
    return tuple(result)


def _threshold_risk(
    track: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    source_as_of: Mapping[str, str],
) -> EvidenceItem | None:
    thresholds = ruler.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise ValueError("evidence ruler thresholds are invalid")
    branch = metrics.get("quality_branch") or metrics.get("branch")
    threshold_names = {
        "bank_capital_adequacy": (
            "bank_capital_adequacy_min",
            "min",
        ),
        "bank_core_tier1_capital_adequacy": (
            "bank_core_tier1_min",
            "min",
        ),
        "bank_npl": ("bank_npl_max", "max"),
    }
    if branch in {"bank", "financial"}:
        threshold_names["roe_mean"] = ("bank_roe_min", "min")
    configured = _risk_specs(track, branch)
    fields = (
        configured
        if track == "deep_value"
        else tuple(
            (field, "", "")
            for _, fact_fields in _fact_specs(track, metrics, ruler)
            for field in fact_fields
        )
    )
    for field, configured_name, configured_direction in fields:
        if _reliable_inputs(field, metrics, ruler, branch) is None:
            continue
        value = _decimal(metrics.get(field))
        assert value is not None
        names = (
            ((configured_name, configured_direction),)
            if configured_name
            else
            (threshold_names[field],)
            if field in threshold_names
            else (
                ((f"{field}_max", "max")),
                ((f"{field}_min", "min")),
            )
        )
        for name, direction in names:
            threshold = _decimal(thresholds.get(name))
            if threshold is None:
                continue
            distance = _bounded_divide(threshold.copy_abs(), Decimal(10))
            lower = _bounded_subtract(threshold, distance)
            upper = _bounded_subtract(
                threshold,
                None if distance is None else distance.copy_negate(),
            )
            if lower is None or upper is None:
                continue
            near = (
                lower <= value <= threshold
                if direction == "max"
                else threshold <= value <= upper
            )
            if near:
                provenance, as_of = _provenance((field,), source_as_of)
                return _item(
                    kind="risk",
                    category="metric_near_threshold",
                    rule_id=f"{track}.risk.metric_near_threshold.{field}",
                    inputs={
                        field: metrics[field],
                        name: thresholds[name],
                        **provenance,
                    },
                    text=f"{field}接近阈值",
                    as_of=as_of,
                    completeness="complete",
                )
    return None


def _flag_risk(
    track: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    source_as_of: Mapping[str, str],
) -> EvidenceItem | None:
    flags = metrics.get("risk_flags", ())
    if isinstance(flags, (str, bytes)) or not isinstance(flags, Sequence):
        raise ValueError("risk_flags must be a sequence")
    for flag in flags:
        if type(flag) is not str or not flag:
            raise ValueError("risk flags must be nonblank strings")
        lowered = flag.casefold()
        category = (
            "worsening_trend"
            if any(word in lowered for word in ("deterioration", "worsening", "trend"))
            else (
                "sector_specific_risk"
                if any(word in lowered for word in ("bank", "sector", "industry"))
                else None
            )
        )
        if category is not None:
            field = (
                flag.removesuffix("_worsening")
                if flag.endswith("_worsening")
                else None
            )
            inputs: dict[str, object] = {"risk_flag": flag}
            as_of = source_as_of["financial"]
            if field is not None:
                spec = next(
                    (
                        item
                        for item in _risk_specs(
                            track,
                            metrics.get("quality_branch")
                            or metrics.get("branch"),
                        )
                        if item[0] == field
                    ),
                    None,
                )
                thresholds = ruler.get("thresholds")
                formulas = ruler.get("formulas")
                if (
                    spec is None
                    or not isinstance(thresholds, Mapping)
                    or not isinstance(formulas, Mapping)
                    or field not in formulas
                    or spec[1] not in thresholds
                    or metrics.get(field) is None
                ):
                    continue
                provenance, as_of = _provenance((field,), source_as_of)
                inputs.update(
                    {
                        field: metrics[field],
                        spec[1]: thresholds[spec[1]],
                        f"formula.{field}": formulas[field],
                        **provenance,
                    }
                )
            else:
                inputs["source"] = "financial"
            return _item(
                kind="risk",
                category=category,
                rule_id=f"{track}.risk.{category}.{flag}",
                inputs=inputs,
                text=flag,
                as_of=as_of,
                completeness="complete",
            )
    return None


def _missing_risk(
    track: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    source_as_of: Mapping[str, str],
) -> EvidenceItem | None:
    fields = ruler.get("fields")
    formulas = ruler.get("formulas")
    policies = ruler.get("missing_policy")
    if (
        isinstance(fields, (str, bytes))
        or not isinstance(fields, Sequence)
        or not isinstance(formulas, Mapping)
        or not isinstance(policies, Mapping)
    ):
        raise ValueError("evidence ruler missing policy is invalid")
    for field in fields:
        if (
            type(field) is str
            and field in formulas
            and policies.get(field) == "skip"
            and metrics.get(field) is None
        ):
            provenance, as_of = _provenance((field,), source_as_of)
            return _item(
                kind="risk",
                category="missing_non_gating_evidence",
                rule_id=f"{track}.risk.missing_non_gating_evidence.{field}",
                inputs={"missing_field": field, **provenance},
                text=f"{field}缺少非门槛证据",
                as_of=as_of,
                completeness="partial",
            )
    return None


def build_evidence_card(
    code: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    *,
    source_as_of: Mapping[str, object],
) -> EvidenceCard:
    """Build three reliable ordered facts plus deterministic counter-signals."""

    if type(code) is not str or not code:
        raise ValueError("evidence code must be a nonblank string")
    if not isinstance(metrics, Mapping) or not isinstance(ruler, Mapping):
        raise TypeError("evidence inputs must be mappings")
    track = ruler.get("track")
    if type(track) is not str or not track:
        raise ValueError("evidence ruler track is invalid")
    dates = _source_dates(source_as_of)

    facts = _fact_items(track, metrics, ruler, dates)
    risks = tuple(
        item
        for item in (
            _threshold_risk(track, metrics, ruler, dates),
            _flag_risk(track, metrics, ruler, dates),
            _missing_risk(track, metrics, ruler, dates),
        )
        if item is not None
    )[: max(1, 5 - len(facts))]
    if not risks:
        risks = (
            _item(
                kind="risk",
                category=None,
                rule_id=f"{track}.risk.none",
                inputs={"source_as_of": dates},
                text=FALLBACK_RISK_TEXT,
                as_of=min(dates.values()),
                completeness="complete",
            ),
        )
    sufficient = len(facts) >= 3
    return EvidenceCard(
        code=code,
        verdict="pass" if sufficient else "insufficient",
        reason=None if sufficient else "evidence_facts_insufficient",
        items=facts + risks,
    )
