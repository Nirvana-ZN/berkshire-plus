"""Deterministic evidence cards derived only from frozen ruler inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType


ALLOWED_RISK_CATEGORIES = frozenset(
    {
        "metric_near_threshold",
        "worsening_trend",
        "sector_specific_risk",
        "missing_non_gating_evidence",
    }
)
FALLBACK_RISK_TEXT = "尚无结构化反方证据"


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


def _item(
    *,
    kind: str,
    category: str | None,
    rule_id: str,
    inputs: Mapping[str, object],
    text: str,
    rule_version: str,
    as_of: str,
    completeness: str,
) -> EvidenceItem:
    return EvidenceItem(
        kind=kind,
        category=category,
        rule_id=rule_id,
        input_fields=MappingProxyType(dict(inputs)),
        display_text=text,
        rule_version=rule_version,
        as_of=as_of,
        data_completeness=completeness,
    )


def _field_order(ruler: Mapping[str, object]) -> tuple[str, ...]:
    order = ruler.get("order")
    fields = ruler.get("fields")
    formulas = ruler.get("formulas")
    if (
        isinstance(order, (str, bytes))
        or not isinstance(order, Sequence)
        or isinstance(fields, (str, bytes))
        or not isinstance(fields, Sequence)
        or not isinstance(formulas, Mapping)
    ):
        raise ValueError("evidence ruler shape is invalid")
    ordered = [
        item[0]
        for item in order
        if (
            not isinstance(item, (str, bytes))
            and isinstance(item, Sequence)
            and len(item) == 3
            and item[0] != "code"
            and item[0] in formulas
        )
    ]
    ordered.extend(
        field
        for field in fields
        if field != "code" and field in formulas and field not in ordered
    )
    return tuple(ordered)


def _fact_items(
    track: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    rule_version: str,
    as_of: str,
) -> tuple[EvidenceItem, ...]:
    formulas = ruler["formulas"]
    assert isinstance(formulas, Mapping)
    result = []
    for field in _field_order(ruler):
        value = metrics.get(field)
        if value is None or type(value) not in {str, int}:
            continue
        result.append(
            _item(
                kind="fact",
                category=None,
                rule_id=f"{track}.fact.{field}",
                inputs={
                    field: value,
                    "formula": formulas[field],
                },
                text=f"{field}={value}",
                rule_version=rule_version,
                as_of=as_of,
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
    rule_version: str,
    as_of: str,
) -> EvidenceItem | None:
    thresholds = ruler.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise ValueError("evidence ruler thresholds are invalid")
    for field in _field_order(ruler):
        value = _decimal(metrics.get(field))
        if value is None:
            continue
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
                    rule_version=rule_version,
                    as_of=as_of,
                    completeness="complete",
                )
    return None


def _flag_risk(
    track: str,
    metrics: Mapping[str, object],
    rule_version: str,
    as_of: str,
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
                rule_version=rule_version,
                as_of=as_of,
                completeness="complete",
            )
    return None


def _missing_risk(
    track: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    rule_version: str,
    as_of: str,
) -> EvidenceItem | None:
    policies = ruler.get("missing_policy")
    if not isinstance(policies, Mapping):
        raise ValueError("evidence ruler missing policy is invalid")
    for field in _field_order(ruler):
        if policies.get(field) == "skip" and metrics.get(field) is None:
            return _item(
                kind="risk",
                category="missing_non_gating_evidence",
                rule_id=f"{track}.risk.missing_non_gating_evidence.{field}",
                inputs={"missing_field": field},
                text=f"{field}缺少非门槛证据",
                rule_version=rule_version,
                as_of=as_of,
                completeness="partial",
            )
    return None


def build_evidence_card(
    code: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    *,
    rule_version: str,
    as_of: str,
) -> EvidenceCard:
    """Build three facts plus at most two deterministic counter-signals."""

    if any(type(value) is not str or not value for value in (code, rule_version, as_of)):
        raise ValueError("evidence identity fields must be nonblank strings")
    if not isinstance(metrics, Mapping) or not isinstance(ruler, Mapping):
        raise TypeError("evidence inputs must be mappings")
    track = ruler.get("track")
    if type(track) is not str or not track:
        raise ValueError("evidence ruler track is invalid")

    facts = _fact_items(track, metrics, ruler, rule_version, as_of)
    risks = tuple(
        item
        for item in (
            _threshold_risk(track, metrics, ruler, rule_version, as_of),
            _flag_risk(track, metrics, rule_version, as_of),
            _missing_risk(track, metrics, ruler, rule_version, as_of),
        )
        if item is not None
    )[:2]
    if not risks:
        risks = (
            _item(
                kind="risk",
                category=None,
                rule_id=f"{track}.risk.none",
                inputs={"risk_flags": ()},
                text=FALLBACK_RISK_TEXT,
                rule_version=rule_version,
                as_of=as_of,
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
