"""Deterministic evidence cards derived only from frozen ruler inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType


EVIDENCE_RULE_VERSION = "deterministic-evidence-v1"
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
    }
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


def _reliable_inputs(
    field: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
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
    for field in _field_order(ruler):
        inputs = _reliable_inputs(field, metrics, ruler)
        if inputs is None:
            continue
        value = metrics[field]
        result.append(
            _item(
                kind="fact",
                category=None,
                rule_id=f"{track}.fact.{field}",
                inputs={**inputs, "formula": formulas[field]},
                text=f"{field}={value}",
                as_of=source_as_of[_source(field)],
                completeness="complete",
            )
        )
        if len(result) == 3:
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
    for field in _field_order(ruler):
        if _reliable_inputs(field, metrics, ruler) is None:
            continue
        value = _decimal(metrics.get(field))
        assert value is not None
        for suffix, direction in (("_max", "max"), ("_min", "min")):
            name = f"{field}{suffix}"
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
