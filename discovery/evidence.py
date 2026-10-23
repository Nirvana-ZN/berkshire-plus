"""Deterministic evidence cards derived only from frozen ruler inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from types import MappingProxyType

from discovery.industry import classify_industry_branch
from discovery.builds import CANDIDATE_BUILD_VERSION
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
        "balance_sheet_risk",
        "missing_non_gating_evidence",
    }
)
_EXPLICIT_RISK_CATEGORIES = MappingProxyType(
    {"cash_debt_double_high": "balance_sheet_risk"}
)
FALLBACK_RISK_TEXT = "尚无结构化反方证据"
_OBSERVATION_REQUIREMENTS = MappingProxyType(
    {
        "pe_percentile": ("valuation_pe", "valuation_pe_observations"),
        "pe_median_ratio": ("valuation_pe", "valuation_pe_observations"),
        "pb_percentile": ("valuation_pb", "valuation_pb_observations"),
        "pb_median_ratio": ("valuation_pb", "valuation_pb_observations"),
        "roe_mean": ("roe", "roe_observations"),
        "roe_downside_volatility": ("roe", "roe_observations"),
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
        "earnings_decline": (
            "normalized_earnings",
            "normalized_earnings_observations",
        ),
        "one_time_profit_share": (
            "one_time_profit",
            "one_time_profit_observations",
        ),
        "impairment_share": (
            "impairment",
            "impairment_observations",
        ),
        "non_core_income_proxy_share": (
            "non_core_income_proxy",
            "non_core_income_proxy_observations",
        ),
        "revenue_cagr": (
            "revenue_growth",
            "revenue_growth_observations",
        ),
        "deducted_profit_cagr": (
            "deducted_profit_growth",
            "deducted_profit_growth_observations",
        ),
        "return_on_capital": ("roic", "roic_observations"),
        "peg": (
            "deducted_profit_growth",
            "deducted_profit_growth_observations",
        ),
        "revenue_growth_slowdown": (
            "latest_growth",
            "revenue_growth_observations",
        ),
        "deducted_profit_growth_slowdown": (
            "latest_growth",
            "deducted_profit_growth_observations",
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


@dataclass(frozen=True)
class PersistedEvidenceItem:
    code: str
    ruler_version_id: int
    rule_version: str
    kind: str
    rule_id: str
    input_fields: Mapping[str, object]
    display_text: str
    as_of: str
    data_completeness: str


_MAX_CANONICAL_EVIDENCE_BYTES = 65_536


def _canonical_evidence_value(value: object) -> str | None:
    remaining = [512]

    def plain(item: object, depth: int = 0) -> object:
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 12:
            raise ValueError("evidence value is too complex")
        if item is None or type(item) in {bool, int, float, str}:
            return item
        if isinstance(item, Mapping):
            if any(type(key) is not str for key in item):
                raise TypeError("evidence object keys must be strings")
            return {
                key: plain(child, depth + 1)
                for key, child in item.items()
            }
        if isinstance(item, Sequence) and type(item) is not str:
            return [plain(child, depth + 1) for child in item]
        raise TypeError("evidence value is not JSON-compatible")

    try:
        encoded = json.dumps(
            plain(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return None
    return (
        encoded
        if len(encoded.encode("utf-8")) <= _MAX_CANONICAL_EVIDENCE_BYTES
        else None
    )


def _branch_matches_frozen_company(
    track: str,
    metrics: Mapping[str, object],
    ruler: Mapping[str, object],
    industry: object,
) -> bool:
    try:
        if industry is not None and type(industry) is not str:
            return False
        shared = classify_industry_branch(industry)
        if track == "garp":
            return metrics.get("branch") == shared
        branches = ruler.get("sector_branches")
        if not isinstance(branches, Mapping):
            return metrics.get("branch") == shared
        policies = branches.get("branch_policies")
        if (
            not isinstance(policies, Mapping)
            or type(policies.get(shared)) is not str
        ):
            return False
        policy = policies[shared]
        if track == "deep_value":
            expected = (
                shared
                if policy in {"unsupported", "insufficient"}
                else policy
            )
            return metrics.get("branch") == expected
        if track != "quality_discount" or metrics.get("branch") != shared:
            return False
        if policy in {"unsupported", "insufficient"}:
            expected_quality = None
        elif policy == "bank":
            expected_quality = "bank"
        else:
            industry_map = branches.get("industry_map")
            if not isinstance(industry_map, Mapping):
                return False
            matches = [
                branch
                for branch in ("nonfinancial", "cyclical")
                if isinstance(industry_map.get(branch), Sequence)
                and type(industry_map.get(branch)) is not str
                and industry in industry_map[branch]
            ]
            expected_quality = matches[0] if len(matches) == 1 else None
        return metrics.get("quality_branch") == expected_quality
    except (KeyError, TypeError, ValueError):
        return False


def verify_canonical_evidence_card(
    *,
    track: str,
    ruler: Mapping[str, object],
    ruler_version_id: int,
    code: str,
    metrics: Mapping[str, object],
    verdict: str,
    rank: int | None,
    reason: str | None,
    company_code: str,
    company_industry: object,
    source_as_of: Mapping[str, object],
    items: Sequence[PersistedEvidenceItem],
) -> bool:
    """Regenerate and exactly compare one bounded persisted evidence card."""

    if (
        type(track) is not str
        or not isinstance(ruler, Mapping)
        or ruler.get("track") != track
        or type(ruler_version_id) is not int
        or ruler_version_id < 1
        or type(code) is not str
        or not code
        or code != company_code
        or not isinstance(metrics, Mapping)
        or metrics.get("code") != code
        or not isinstance(source_as_of, Mapping)
        or type(items) not in {tuple, list}
        or not 1 <= len(items) <= 5
        or rank is not None
        and (type(rank) is not int or rank < 1)
        or not _branch_matches_frozen_company(
            track, metrics, ruler, company_industry
        )
        or _canonical_evidence_value(metrics) is None
        or _canonical_evidence_value(source_as_of) is None
    ):
        return False
    try:
        expected = build_evidence_card(
            code, metrics, ruler, source_as_of=source_as_of
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False
    if (
        expected.verdict == "insufficient"
        and rank is not None
        and (verdict, reason)
        != ("insufficient", "evidence_facts_insufficient")
        or expected.verdict == "pass"
        and reason == "evidence_facts_insufficient"
    ):
        return False

    expected_by_identity = {
        (item.kind, item.rule_id): item for item in expected.items
    }
    actual_by_identity: dict[
        tuple[str, str], PersistedEvidenceItem
    ] = {}
    for item in items:
        if (
            type(item) is not PersistedEvidenceItem
            or item.code != code
            or item.ruler_version_id != ruler_version_id
            or type(item.kind) is not str
            or type(item.rule_id) is not str
            or (item.kind, item.rule_id) in actual_by_identity
        ):
            return False
        actual_by_identity[(item.kind, item.rule_id)] = item
    if set(actual_by_identity) != set(expected_by_identity):
        return False

    for identity, expected_item in expected_by_identity.items():
        actual = actual_by_identity[identity]
        if (
            actual.rule_version != expected_item.rule_version
            or actual.display_text != expected_item.display_text
            or actual.as_of != expected_item.as_of
            or actual.data_completeness != expected_item.data_completeness
            or _canonical_evidence_value(actual.input_fields)
            != _canonical_evidence_value(dict(expected_item.input_fields))
        ):
            return False
    return True


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
    if field in {"normalized_pe", "peg"}:
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
    if track == "garp":
        return (
            (
                "revenue_growth_slowdown",
                "revenue_growth_slowdown_max",
                "max",
            ),
            (
                "deducted_profit_growth_slowdown",
                "deducted_profit_growth_slowdown_max",
                "max",
            ),
            ("peg", "peg_max", "max"),
            ("pe_percentile", "pe_percentile_max", "max"),
            (
                "interest_debt_increase_to_revenue",
                "interest_debt_increase_to_revenue_max",
                "max",
            ),
            (
                "goodwill_increase_to_revenue",
                "goodwill_increase_to_revenue_max",
                "max",
            ),
            (
                "acquisition_cash_to_revenue",
                "acquisition_cash_to_revenue_max",
                "max",
            ),
        )
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
            ("earnings_decline", "earnings_decline_max", "max"),
            (
                "one_time_profit_share",
                "one_time_profit_share_max",
                "max",
            ),
            ("impairment_share", "impairment_share_max", "max"),
            (
                "non_core_income_proxy_share",
                "non_core_income_proxy_share_max",
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
    if ruler.get('algorithm_version') == CANDIDATE_BUILD_VERSION:
        order = ruler['ranking_contract']['branch_orders'][metrics['ranking_branch']]
        return tuple((field, (field,)) for field, _ in order)
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
    if ruler.get('algorithm_version') == CANDIDATE_BUILD_VERSION:
        result = metrics.get('metric_results', {}).get(field)
        if result is not None:
            value = _decimal(result.get('value'))
            if result.get('status') != 'verified' or value is None or value != _decimal(metrics.get(field)):
                return None
            return {field: metrics[field], 'metric_result': result,
                    'metric_contract_version': ruler['metric_contract']['version']}
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
    if field == 'deducted_profit_consistent_growth_periods' and ruler.get('algorithm_version') == CANDIDATE_BUILD_VERSION:
        requirement = ('deducted_profit_growth', 'deducted_profit_growth_observations')
    if requirement is None:
        return None
    minimum_name, count_name = requirement
    return_lineage: dict[str, object] = {}
    if field == "roe_mean" and branch == "financial":
        minimum_name = "bank_roe"
        count_name = "bank_roe_observations"
    elif field == "roe_mean" and branch == "bank":
        minimum_name = "bank_roe"
    elif field == "return_on_capital" and metrics.get(
        "return_metric_label"
    ) == "roe_fallback":
        minimum_name = "roe_fallback"
        count_name = "roe_fallback_observations"
        branches = ruler.get("sector_branches")
        config = (
            branches.get(branch)
            if isinstance(branches, Mapping) and type(branch) is str
            else None
        )
        if (
            not isinstance(config, Mapping)
            or config.get("return_metric_policy") != "roic_then_roe"
        ):
            raise ValueError("ROE fallback evidence policy is invalid")
        return_lineage = {
            "return_metric_label": "roe_fallback",
            "return_metric_source_field": "roe",
            "return_metric_primary_field": "roic",
            "return_metric_policy": "roic_then_roe",
        }
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
            **return_lineage,
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
        text = ",".join(f"{field}={metrics[field]}" for field in fields)
        if ruler.get('algorithm_version') == CANDIDATE_BUILD_VERSION:
            inputs['ranking_audit'] = metrics.get('ranking_audit')
            labels = metrics.get('metric_labels', {})
            text = ','.join(f'{labels.get(field, field)}={metrics[field]}' for field in fields)
        if (
            fields == ("return_on_capital",)
            and metrics.get("return_metric_label") == "roe_fallback"
        ):
            text += " (ROE fallback)"
        result.append(
            _item(
                kind="fact",
                category=None,
                rule_id=f"{track}.fact.{rule_name}",
                inputs=inputs,
                text=text,
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
        category = _EXPLICIT_RISK_CATEGORIES.get(flag) or (
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


def _operating_risk(track, metrics, dates):
    result = metrics.get('operating_result')
    if not isinstance(result, Mapping) or result.get('state') not in {'deteriorating', 'unknown'}:
        return None
    unknown = result['state'] == 'unknown'
    return _item(
        kind='risk', category=None if unknown else 'worsening_trend',
        rule_id=f'{track}.risk.operating.{result["state"]}',
        inputs={'operating_result': result, 'operating_policy': metrics.get('operating_policy'),
                'fundamental_fingerprint': metrics.get('fundamental_fingerprint')},
        text='当期经营证据不足，待补充核验' if unknown else '当期经营恶化信号，进入研究待核并冻结新增仓位',
        as_of=dates['financial'], completeness='partial' if unknown else 'complete',
    )


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
    if ruler.get('algorithm_version') == CANDIDATE_BUILD_VERSION:
        # One auditable counter-evidence item retains every signal, including
        # competition additions and explicitly non-ranking shadow diagnostics.
        rank = metrics.get('ranking_audit', {})
        state = rank.get('risk_state', 'unknown')
        risk = _item(kind='risk', category='worsening_trend' if state in {'watch', 'deteriorating'} else None,
            rule_id=f'{track}.risk.selection_v8',
            inputs={key: metrics.get(key) for key in ('ranking_audit', 'risk_flags', 'verified_risk_types',
                'operating_result', 'operating_policy', 'freeze_new_positions', 'metric_results',
                'collection_metrics', 'collection_policy', 'shadow_signals', 'fundamental_fingerprint')},
            text=f'研究风险层级：{state}；回款与受限资金信号仅作影子证据。',
            as_of=dates['financial'], completeness='partial' if state == 'unknown' else 'complete')
        sufficient = len(facts) >= 3
        return EvidenceCard(code, 'pass' if sufficient else 'insufficient',
                            None if sufficient else 'evidence_facts_insufficient', facts + (risk,))
    risks = tuple(
        item
        for item in (
            _operating_risk(track, metrics, dates),
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
