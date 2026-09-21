"""Deterministic evidence cards derived only from frozen ruler inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType


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
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _source(field: str) -> str:
    if field.startswith(("pe_", "pb_")) or field == "normalized_pe":
        return "valuation"
    if field == "listing_years":
        return "security"
    return "financial"


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
    if track != "quality_discount" or metrics.get("quality_branch") != "bank":
        return tuple((field, (field,)) for field in _field_order(ruler))

    branches = ruler.get("sector_branches")
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
    if field == "roe_mean" and branch == "bank":
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
    branch = metrics.get("quality_branch")
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
        result.append(
            _item(
                kind="fact",
                category=None,
                rule_id=f"{track}.fact.{rule_name}",
                inputs=inputs,
                text=",".join(f"{field}={metrics[field]}" for field in fields),
                as_of=min(source_as_of[_source(field)] for field in fields),
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
    branch = metrics.get("quality_branch")
    fields = (
        field
        for _, fact_fields in _fact_specs(track, metrics, ruler)
        for field in fact_fields
    )
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
    if branch == "bank":
        threshold_names["roe_mean"] = ("bank_roe_min", "min")
    for field in fields:
        if _reliable_inputs(field, metrics, ruler, branch) is None:
            continue
        value = _decimal(metrics.get(field))
        assert value is not None
        names = (
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
            distance = abs(threshold) / Decimal(10)
            near = (
                threshold - distance <= value <= threshold
                if direction == "max"
                else threshold <= value <= threshold + distance
            )
            if near:
                return _item(
                    kind="risk",
                    category="metric_near_threshold",
                    rule_id=f"{track}.risk.metric_near_threshold.{field}",
                    inputs={field: metrics[field], name: thresholds[name]},
                    text=f"{field}接近阈值",
                    as_of=source_as_of[_source(field)],
                    completeness="complete",
                )
    return None


def _flag_risk(
    track: str,
    metrics: Mapping[str, object],
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
            return _item(
                kind="risk",
                category=category,
                rule_id=f"{track}.risk.{category}.{flag}",
                inputs={"risk_flag": flag},
                text=flag,
                as_of=source_as_of["financial"],
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
            return _item(
                kind="risk",
                category="missing_non_gating_evidence",
                rule_id=f"{track}.risk.missing_non_gating_evidence.{field}",
                inputs={"missing_field": field},
                text=f"{field}缺少非门槛证据",
                as_of=source_as_of[_source(field)],
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
            _flag_risk(track, metrics, dates),
            _missing_risk(track, metrics, ruler, dates),
        )
        if item is not None
    )[:2]
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
