"""Pure JSON-ready summaries over persisted discovery stage artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from discovery.common import (
    CommonSafetyContract,
    common_safety_contract,
)
from discovery.constants import STAGE_STATUSES, TRACK_STAGES, TRACKS, VERDICTS
from discovery.metrics import empirical_percentile_rank
from discovery.rulers import (
    DISCOVERY_DECIMAL_COMPONENT_LIMIT,
    validate_ruler_version,
)
from discovery.tracks import scope_for_track

SUMMARY_SCHEMA_VERSION = "discovery.stage-summary/v1"
SUMMARY_RESULT_LIMIT = 20_000
SUMMARY_METRICS_JSON_BYTE_LIMIT = 64 * 1024
SUMMARY_ATTEMPT_METRICS_JSON_BYTE_LIMIT = 32 * 1024 * 1024
SUMMARY_METRIC_KEYS_PER_RESULT_LIMIT = 256
SUMMARY_UNIQUE_METRICS_LIMIT = 256
SUMMARY_TOTAL_METRIC_CELLS_LIMIT = 1_000_000
SUMMARY_DECIMAL_OUTPUT_LIMIT = DISCOVERY_DECIMAL_COMPONENT_LIMIT + 4
# ponytail: 20k rows and 32 MiB cover the all-A universe with substantial
# per-company headroom; raise only after profiling a larger persisted contract.

_COMPLETE_STATUSES = frozenset(
    {
        "waiting_confirmation",
        "confirmed",
        "ruler_not_ready",
        "data_not_ready",
        "insufficient_candidates",
    }
)
_OVERRIDE_STATUSES = frozenset({"waiting_confirmation", "confirmed"})
_DISTRIBUTION_KEYS = ("min", "p25", "median", "p75", "max")

_TRACK_RULES = {
    "track_gate": (
        {
            "id": "track_gate.frozen_ruler",
            "text": "仅按本次 attempt 冻结的本轨尺子判定，不读取当前尺子。",
        },
        {
            "id": "track_gate.missing_is_insufficient",
            "text": "关键指标不足时标记为数据不足，不跨轨道淘汰。",
        },
    ),
    "competition": (
        {
            "id": "competition.deterministic_order",
            "text": "仅在本轨通过者中按冻结尺子确定性排序，完全同序时证券代码升序。",
        },
        {
            "id": "competition.track_local",
            "text": "候选竞争保持轨道独立，不生成跨轨道总分。",
        },
    ),
    "adjudication": (
        {
            "id": "adjudication.explicit_decision",
            "text": "人工保留或排除与阶段覆盖分开保存，排除必须写明理由。",
        },
    ),
}


class DiscoverySummaryError(ValueError):
    """Persisted artifacts cannot form a trustworthy summary."""


def _attribute(row: object, name: str) -> object:
    try:
        return getattr(row, name)
    except (AttributeError, TypeError) as error:
        raise DiscoverySummaryError(
            f"persisted discovery artifact lacks {name}"
        ) from error


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise DiscoverySummaryError("persisted discovery metric is non-finite")
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    if (
        not isinstance(exponent, int)
        or len(digits) > DISCOVERY_DECIMAL_COMPONENT_LIMIT
        or abs(exponent) > DISCOVERY_DECIMAL_COMPONENT_LIMIT
        or abs(value.adjusted()) > DISCOVERY_DECIMAL_COMPONENT_LIMIT
    ):
        raise DiscoverySummaryError("persisted discovery metric is too large")
    if value.is_zero():
        return "0"
    sign_length = int(value.is_signed())
    if exponent >= 0:
        fixed_length = sign_length + len(digits) + exponent
    elif value.adjusted() >= 0:
        fixed_length = sign_length + len(digits) + 1
    else:
        fixed_length = (
            sign_length + 2 + (-value.adjusted() - 1) + len(digits)
        )
    if fixed_length > SUMMARY_DECIMAL_OUTPUT_LIMIT:
        raise DiscoverySummaryError("persisted discovery metric is too large")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _decimal(value: object) -> Decimal | None:
    if type(value) is Decimal:
        parsed = value
    elif type(value) is str:
        if len(value) > DISCOVERY_DECIMAL_COMPONENT_LIMIT:
            raise DiscoverySummaryError(
                "persisted discovery metric exceeds its decimal bound"
            )
        if not value or value != value.strip():
            return None
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
    else:
        return None
    _canonical_decimal(parsed)
    return parsed


def _metrics_json_byte_length(text: object) -> int:
    if type(text) is not str:
        raise DiscoverySummaryError("persisted discovery metrics are not text")
    if len(text) > SUMMARY_METRICS_JSON_BYTE_LIMIT:
        raise DiscoverySummaryError("persisted discovery metrics are too large")
    byte_length = len(text.encode("utf-8"))
    if byte_length > SUMMARY_METRICS_JSON_BYTE_LIMIT:
        raise DiscoverySummaryError("persisted discovery metrics are too large")
    return byte_length


def _metrics_json(text: object) -> dict[str, object]:
    _metrics_json_byte_length(text)
    assert type(text) is str

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            parse_int=Decimal,
            parse_float=Decimal,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (InvalidOperation, TypeError, ValueError) as error:
        raise DiscoverySummaryError(
            "persisted discovery metrics are corrupt"
        ) from error
    if type(value) is not dict:
        raise DiscoverySummaryError(
            "persisted discovery metrics must be an object"
        )
    return value


def _fraction_decimal(value: Fraction) -> str:
    numerator, denominator = value.numerator, value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise DiscoverySummaryError(
            "persisted discovery metric has no exact decimal summary"
        )
    scale = max(twos, fives)
    scaled = numerator * (2 ** (scale - twos)) * (5 ** (scale - fives))
    sign = "-" if scaled < 0 else ""
    try:
        digits = str(abs(scaled))
    except ValueError as error:
        raise DiscoverySummaryError(
            "persisted discovery metric summary exceeds its exact bound"
        ) from error
    if scale:
        digits = digits.rjust(scale + 1, "0")
        text = f"{sign}{digits[:-scale]}.{digits[-scale:]}"
        text = text.rstrip("0").rstrip(".")
    else:
        text = f"{sign}{digits}"
    if text in {"-0", ""}:
        text = "0"
    if len(text) > SUMMARY_DECIMAL_OUTPUT_LIMIT:
        raise DiscoverySummaryError(
            "persisted discovery metric summary exceeds its exact bound"
        )
    return text


def _quartile(values: Sequence[Decimal], quarter: int) -> Fraction:
    numerator = (len(values) - 1) * quarter
    lower, remainder = divmod(numerator, 4)
    if remainder == 0:
        return Fraction(values[lower])
    low = Fraction(values[lower])
    high = Fraction(values[lower + 1])
    return low + ((high - low) * remainder / 4)


def _distribution(values: Sequence[Decimal]) -> dict[str, object]:
    ordered = tuple(sorted(values))
    if not ordered:
        return {
            "status": "unavailable",
            "reason": "no_numeric_values",
            "count": 0,
            **{key: None for key in _DISTRIBUTION_KEYS},
        }
    quantiles = (
        Fraction(ordered[0]),
        _quartile(ordered, 1),
        _quartile(ordered, 2),
        _quartile(ordered, 3),
        Fraction(ordered[-1]),
    )
    return {
        "status": "available",
        "reason": None,
        "count": len(ordered),
        **{
            key: _fraction_decimal(value)
            for key, value in zip(_DISTRIBUTION_KEYS, quantiles, strict=True)
        },
    }


def _stage_identity(stage: object) -> tuple[dict[str, object], str | None, str]:
    stage_id = _attribute(stage, "id")
    run_id = _attribute(stage, "run_id")
    attempt = _attribute(stage, "attempt")
    status = _attribute(stage, "status")
    algorithm_version = _attribute(stage, "algorithm_version")
    if (
        type(stage_id) is not int
        or stage_id < 1
        or type(run_id) is not int
        or run_id < 1
        or type(attempt) is not int
        or attempt < 1
        or type(status) is not str
        or status not in STAGE_STATUSES
        or type(algorithm_version) is not str
        or not algorithm_version.strip()
    ):
        raise DiscoverySummaryError("persisted discovery stage is corrupt")
    track = getattr(stage, "track", None)
    name = getattr(stage, "stage", "safety")
    if track is None:
        if name != "safety":
            raise DiscoverySummaryError("persisted common stage is corrupt")
    elif (
        type(track) is not str
        or track not in TRACKS
        or type(name) is not str
        or name not in TRACK_STAGES
    ):
        raise DiscoverySummaryError("persisted track stage is corrupt")
    else:
        common_stage_id = _attribute(stage, "common_stage_id")
        predecessor_stage_id = _attribute(stage, "predecessor_stage_id")
        predecessor_stage = _attribute(stage, "predecessor_stage")
        expected_predecessor = {
            "track_gate": None,
            "competition": "track_gate",
            "adjudication": "competition",
        }[name]
        if (
            (
                name == "track_gate"
                and (
                    type(common_stage_id) is not int
                    or common_stage_id < 1
                    or predecessor_stage_id is not None
                    or predecessor_stage is not None
                )
            )
            or (
                name != "track_gate"
                and (
                    common_stage_id is not None
                    or type(predecessor_stage_id) is not int
                    or predecessor_stage_id < 1
                    or predecessor_stage != expected_predecessor
                )
            )
            or (
                status == "insufficient_candidates"
                and name != "adjudication"
            )
        ):
            raise DiscoverySummaryError(
                "persisted discovery stage lineage is corrupt"
            )
    return (
        {
            "id": stage_id,
            "run_id": run_id,
            "track": track,
            "stage": name,
            "attempt": attempt,
            "status": status,
            "algorithm_version": algorithm_version,
        },
        track,
        name,
    )


def _counter(stage: object, name: str) -> int:
    value = _attribute(stage, name)
    if type(value) is not int or value < 0:
        raise DiscoverySummaryError("persisted discovery counters are corrupt")
    return value


def _result_values(
    stage_identity: Mapping[str, object],
    track: str | None,
    name: str,
    rows: Iterable[object],
    known_metrics: frozenset[str],
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, tuple[Decimal, ...]],
]:
    try:
        results = tuple(rows)
    except TypeError as error:
        raise DiscoverySummaryError("persisted results are not iterable") from error
    if len(results) > SUMMARY_RESULT_LIMIT:
        raise DiscoverySummaryError("persisted result set is too large")
    if len(known_metrics) > SUMMARY_UNIQUE_METRICS_LIMIT:
        raise DiscoverySummaryError("persisted ruler declares too many metrics")
    by_code: dict[str, dict[str, object]] = {}
    metric_names = set(known_metrics)
    numeric: dict[str, list[Decimal]] = {
        metric: [] for metric in known_metrics
    }
    metric_cells = 0
    metrics_json_bytes = 0
    for row in results:
        code = _attribute(row, "code")
        verdict = _attribute(row, "verdict")
        if (
            type(code) is not str
            or not code.strip()
            or code in by_code
            or type(verdict) is not str
            or verdict not in VERDICTS
            or _attribute(row, "run_id") != stage_identity["run_id"]
            or _attribute(row, "attempt") != stage_identity["attempt"]
            or (
                track is not None
                and (
                    _attribute(row, "track") != track
                    or _attribute(row, "stage") != name
                )
            )
        ):
            raise DiscoverySummaryError("persisted discovery results are corrupt")
        metrics_text = _attribute(row, "metrics_json")
        metrics_json_bytes += _metrics_json_byte_length(metrics_text)
        if (
            metrics_json_bytes
            > SUMMARY_ATTEMPT_METRICS_JSON_BYTE_LIMIT
        ):
            raise DiscoverySummaryError(
                "persisted attempt metrics exceed the byte limit"
            )
        metrics = _metrics_json(metrics_text)
        if len(metrics) > SUMMARY_METRIC_KEYS_PER_RESULT_LIMIT:
            raise DiscoverySummaryError(
                "persisted result declares too many metrics"
            )
        metric_cells += len(metrics)
        if metric_cells > SUMMARY_TOTAL_METRIC_CELLS_LIMIT:
            raise DiscoverySummaryError(
                "persisted result metrics exceed the attempt cell limit"
            )
        for metric, value in metrics.items():
            if metric not in metric_names:
                metric_names.add(metric)
                if len(metric_names) > SUMMARY_UNIQUE_METRICS_LIMIT:
                    raise DiscoverySummaryError(
                        "persisted attempt declares too many metrics"
                    )
                numeric[metric] = []
            parsed = _decimal(value)
            if parsed is not None:
                numeric[metric].append(parsed)
        by_code[code] = {
            "row": row,
            "verdict": verdict,
        }
    return by_code, {
        metric: tuple(values) for metric, values in numeric.items()
    }


def _override_values(
    stage_identity: Mapping[str, object],
    track: str | None,
    name: str,
    rows: Iterable[object],
    results: Mapping[str, dict[str, object]],
) -> dict[str, object]:
    try:
        overrides = tuple(rows)
    except TypeError as error:
        raise DiscoverySummaryError("persisted overrides are not iterable") from error
    if track is None and overrides:
        raise DiscoverySummaryError("common stage cannot have overrides")
    if overrides and stage_identity["status"] not in _OVERRIDE_STATUSES:
        raise DiscoverySummaryError("persisted override is impossible")
    by_code: dict[str, object] = {}
    for row in overrides:
        code = _attribute(row, "code")
        action = _attribute(row, "action")
        reason = _attribute(row, "reason")
        if (
            type(code) is not str
            or code not in results
            or code in by_code
            or action not in {"forced_pass", "forced_drop"}
            or type(reason) is not str
            or not reason.strip()
            or _attribute(row, "run_id") != stage_identity["run_id"]
            or _attribute(row, "attempt") != stage_identity["attempt"]
            or _attribute(row, "track") != track
            or _attribute(row, "stage") != name
        ):
            raise DiscoverySummaryError("persisted override is impossible")
        by_code[code] = row
    return by_code


def _validate_counters(
    stage: object,
    status: object,
    results: Mapping[str, dict[str, object]],
) -> None:
    persisted = {
        "input": _counter(stage, "total_count"),
        "pass": _counter(stage, "pass_count"),
        "drop": _counter(stage, "drop_count"),
        "insufficient": _counter(stage, "insufficient_count"),
    }
    actual = {
        "input": len(results),
        **{
            verdict: sum(
                item["verdict"] == verdict for item in results.values()
            )
            for verdict in ("pass", "drop", "insufficient")
        },
    }
    if status in _COMPLETE_STATUSES and persisted != actual:
        raise DiscoverySummaryError("persisted discovery counters drifted")
    if status in {"queued", "running", "failed"} and (
        persisted != {"input": 0, "pass": 0, "drop": 0, "insufficient": 0}
        or results
    ):
        raise DiscoverySummaryError("incomplete discovery attempt is not empty")


def _track_threshold(
    key: str, known_metrics: frozenset[str]
) -> tuple[str | None, str | None]:
    if key in known_metrics:
        return key, None
    suffixes = (
        ("_minimum", "gte"),
        ("_maximum", "lte"),
        ("_floor", "gte"),
        ("_ceiling", "lte"),
        ("_min", "gte"),
        ("_max", "lte"),
        ("_cap", "lte"),
    )
    for suffix, direction in suffixes:
        if key.endswith(suffix):
            metric = key[: -len(suffix)]
            return (
                (metric, direction)
                if metric in known_metrics
                else (None, direction)
            )
    return None, None


def _ruler_values(
    stage: object,
    stage_identity: Mapping[str, object],
    track: str | None,
    ruler_version: object | None,
) -> tuple[
    dict[str, object],
    frozenset[str],
    tuple[dict[str, object], ...],
    tuple[dict[str, str], ...],
]:
    if track is None:
        if ruler_version is not None:
            raise DiscoverySummaryError(
                "common stage cannot use an external ruler"
            )
        try:
            contract: CommonSafetyContract = common_safety_contract(
                str(stage_identity["algorithm_version"])
            )
        except ValueError as error:
            raise DiscoverySummaryError(
                "common safety contract is unavailable"
            ) from error
        thresholds = (
            {
                "id": "price_floor_exclusive",
                "threshold": _canonical_decimal(contract.price_floor),
                "metric": "price",
                "direction": "gt",
            },
            {
                "id": "suspension_trading_days_max",
                "threshold": str(contract.suspension_max_trading_days),
                "metric": "suspension_trading_days",
                "direction": "lte",
            },
        )
        return (
            {
                "version_id": None,
                "reason": "built_in_common_safety",
                "scope": "discovery:common:safety",
                "algorithm_version": stage_identity["algorithm_version"],
                "status": "built_in",
            },
            contract.metric_names,
            thresholds,
            tuple(
                {"id": rule_id, "text": text}
                for rule_id, text in contract.rules
            ),
        )

    version_id = _attribute(stage, "ruler_version_id")
    if version_id is None:
        if ruler_version is not None:
            raise DiscoverySummaryError("unexpected persisted ruler")
        if stage_identity["status"] not in {
            "queued",
            "running",
            "failed",
            "ruler_not_ready",
        }:
            raise DiscoverySummaryError(
                "persisted track stage requires a ruler"
            )
        return (
            {
                "version_id": None,
                "reason": "ruler_not_ready",
                "scope": scope_for_track(track),
                "algorithm_version": None,
                "status": "unavailable",
            },
            frozenset(),
            (),
            _TRACK_RULES[str(stage_identity["stage"])],
        )
    if stage_identity["status"] == "ruler_not_ready":
        raise DiscoverySummaryError(
            "ruler-not-ready stage cannot bind a ruler"
        )
    if type(version_id) is not int or version_id < 1 or ruler_version is None:
        raise DiscoverySummaryError("persisted discovery ruler is missing")
    if _attribute(ruler_version, "id") != version_id:
        raise DiscoverySummaryError("persisted discovery ruler identity mismatch")
    reason = _attribute(ruler_version, "reason")
    if type(reason) is not str or not reason.strip():
        raise DiscoverySummaryError("persisted discovery ruler reason is corrupt")
    try:
        ruler = validate_ruler_version(
            ruler_version,  # type: ignore[arg-type]
            expected_track=track,
            expected_algorithm_version=str(stage_identity["algorithm_version"]),
            allow_retired_algorithm=True,
        )
    except (TypeError, ValueError) as error:
        raise DiscoverySummaryError(
            "persisted discovery ruler is corrupt"
        ) from error
    known_metrics = frozenset(
        metric
        for metric in (*ruler.fields, *ruler.formulas)
        if metric != "code"
    )
    if len(known_metrics) > SUMMARY_UNIQUE_METRICS_LIMIT:
        raise DiscoverySummaryError("persisted ruler declares too many metrics")
    thresholds = tuple(
        {
            "id": key,
            "threshold": value,
            "metric": (resolved := _track_threshold(key, known_metrics))[0],
            "direction": resolved[1],
        }
        for key, value in sorted(ruler.thresholds.items())
    )
    return (
        {
            "version_id": version_id,
            "reason": reason,
            "scope": ruler.scope,
            "algorithm_version": ruler.algorithm_version,
            "status": "ready",
        },
        known_metrics,
        thresholds,
        _TRACK_RULES[str(stage_identity["stage"])],
    )


def _distributions(
    numeric: Mapping[str, tuple[Decimal, ...]],
) -> dict[str, dict[str, object]]:
    return {
        metric: _distribution(numeric[metric])
        for metric in sorted(numeric)
    }


def _markers(
    thresholds: Sequence[Mapping[str, object]],
    numeric: Mapping[str, tuple[Decimal, ...]],
) -> list[dict[str, object]]:
    markers: list[dict[str, object]] = []
    for threshold in thresholds:
        metric = threshold["metric"]
        marker = {
            "id": threshold["id"],
            "threshold": threshold["threshold"],
            "metric": metric,
            "direction": threshold["direction"],
            "position": None,
        }
        if metric is None:
            markers.append(marker | {"status": "unresolved"})
            continue
        threshold_value = _decimal(threshold["threshold"])
        rank = (
            None
            if threshold_value is None
            else empirical_percentile_rank(
                threshold_value, numeric.get(str(metric), ())
            )
        )
        markers.append(
            marker
            | (
                {"status": "unavailable"}
                if rank is None
                else {
                    "status": "available",
                    "position": _canonical_decimal(rank),
                }
            )
        )
    return markers


def build_stage_summary(
    stage: object,
    results: Iterable[object],
    overrides: Iterable[object] = (),
    ruler_version: object | None = None,
) -> dict[str, object]:
    """Build one deterministic summary without database or market access."""

    identity, track, name = _stage_identity(stage)
    ruler, known_metrics, thresholds, rules = _ruler_values(
        stage, identity, track, ruler_version
    )
    result_values, numeric = _result_values(
        identity, track, name, results, known_metrics
    )
    override_values = _override_values(
        identity, track, name, overrides, result_values
    )
    _validate_counters(stage, identity["status"], result_values)
    effective = {
        code: (
            item["verdict"]
            if code not in override_values
            else (
                "pass"
                if _attribute(override_values[code], "action") == "forced_pass"
                else "drop"
            )
        )
        for code, item in result_values.items()
    }
    distributions = _distributions(numeric)
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "stage": identity,
        "rules": [dict(rule) for rule in rules],
        "ruler": ruler,
        "counts": {
            "input": len(result_values),
            "pass": sum(value == "pass" for value in effective.values()),
            "drop": sum(value == "drop" for value in effective.values()),
            "insufficient": sum(
                value == "insufficient" for value in effective.values()
            ),
            "overrides": len(override_values),
        },
        "thresholds": [dict(threshold) for threshold in thresholds],
        "distributions": distributions,
        "threshold_markers": _markers(thresholds, numeric),
    }
