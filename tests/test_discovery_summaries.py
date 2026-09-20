from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal, DivisionByZero, getcontext, setcontext
from fractions import Fraction
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from tests.discovery_helpers import (
    SNAPSHOT_AT,
    add_unfrozen_run,
    discovery_ruler_payload,
)


def _stage(
    *,
    track: str | None = None,
    stage: str = "safety",
    attempt: int = 1,
    status: str = "waiting_confirmation",
    counts: tuple[int, int, int, int] = (0, 0, 0, 0),
    ruler_version_id: int | None = None,
):
    total, passed, dropped, insufficient = counts
    values = {
        "id": attempt,
        "run_id": 1,
        "attempt": attempt,
        "status": status,
        "total_count": total,
        "pass_count": passed,
        "drop_count": dropped,
        "insufficient_count": insufficient,
        "algorithm_version": "discovery-v1",
    }
    if track is not None:
        common_stage_id = 1 if stage == "track_gate" else None
        predecessor_stage_id = None if stage == "track_gate" else 1
        predecessor_stage = (
            None
            if stage == "track_gate"
            else "track_gate"
            if stage == "competition"
            else "competition"
        )
        values |= {
            "track": track,
            "stage": stage,
            "ruler_version_id": ruler_version_id,
            "common_stage_id": common_stage_id,
            "predecessor_stage_id": predecessor_stage_id,
            "predecessor_stage": predecessor_stage,
        }
    return SimpleNamespace(**values)


def _result(
    code: str,
    metrics: dict[str, object] | str,
    *,
    verdict: str = "pass",
    track: str | None = None,
    stage: str = "safety",
    attempt: int = 1,
):
    values = {
        "run_id": 1,
        "attempt": attempt,
        "code": code,
        "verdict": verdict,
        "metrics_json": (
            metrics
            if type(metrics) is str
            else json.dumps(metrics, separators=(",", ":"))
        ),
    }
    if track is not None:
        values |= {"track": track, "stage": stage}
    return SimpleNamespace(**values)


def _override(
    code: str,
    *,
    action: str = "forced_drop",
    attempt: int = 1,
):
    return SimpleNamespace(
        run_id=1,
        track="deep_value",
        stage="track_gate",
        attempt=attempt,
        code=code,
        action=action,
        reason="人工复核",
    )


def _ruler(
    *,
    row_id: int = 7,
    reason: str = "首次校准",
    thresholds: dict[str, str] | None = None,
):
    from app.models import RulerVersion
    from discovery.rulers import canonical_payload, payload_digest

    payload = discovery_ruler_payload("deep_value")
    if thresholds is not None:
        payload["thresholds"] = thresholds
    return RulerVersion(
        id=row_id,
        user_id=1,
        scope="discovery:deep_value",
        digest=payload_digest(payload),
        reason=reason,
        payload_json=canonical_payload(payload),
        activated_at=SNAPSHOT_AT,
    )


def _summary(
    values: list[object],
    *,
    thresholds: dict[str, str] | None = None,
):
    from discovery.summaries import build_stage_summary

    ruler = _ruler(thresholds=thresholds)
    return build_stage_summary(
        _stage(
            track="deep_value",
            stage="track_gate",
            counts=(len(values), len(values), 0, 0),
            ruler_version_id=ruler.id,
        ),
        [
            _result(
                f"{index:06d}.SZ",
                {"metric": value},
                track="deep_value",
                stage="track_gate",
            )
            for index, value in enumerate(values, start=1)
        ],
        ruler_version=ruler,
    )


def test_common_and_track_share_schema_rules_ruler_and_effective_counts() -> None:
    from discovery.summaries import SUMMARY_SCHEMA_VERSION, build_stage_summary

    common = build_stage_summary(
        _stage(counts=(1, 1, 0, 0)),
        [_result("600000.SH", {"price": "12.5"})],
    )
    ruler = _ruler()
    track = build_stage_summary(
        _stage(
            track="deep_value",
            stage="track_gate",
            counts=(2, 1, 0, 1),
            ruler_version_id=ruler.id,
        ),
        [
            _result(
                "600000.SH",
                {"metric": "1"},
                track="deep_value",
                stage="track_gate",
            ),
            _result(
                "600001.SH",
                {"metric": None},
                verdict="insufficient",
                track="deep_value",
                stage="track_gate",
            ),
        ],
        [_override("600000.SH")],
        ruler,
    )

    assert common["schema_version"] == track["schema_version"] == (
        SUMMARY_SCHEMA_VERSION
    )
    assert common["ruler"] == {
        "version_id": None,
        "reason": "built_in_common_safety",
        "scope": "discovery:common:safety",
        "algorithm_version": "discovery-v1",
        "status": "built_in",
    }
    assert track["ruler"]["reason"] == "首次校准"
    assert track["counts"] == {
        "input": 2,
        "pass": 0,
        "drop": 1,
        "insufficient": 1,
        "overrides": 1,
    }
    assert all(
        list(rule) == ["id", "text"]
        and rule["id"]
        and any("\u4e00" <= char <= "\u9fff" for char in rule["text"])
        for summary in (common, track)
        for rule in summary["rules"]
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            ["1", "2", "3", "4", "5"],
            ("1", "2", "3", "4", "5"),
        ),
        (
            ["1", "2", "3", "4"],
            ("1", "1.75", "2.5", "3.25", "4"),
        ),
        (
            ["2", "2", "2", "2"],
            ("2", "2", "2", "2", "2"),
        ),
        (
            ["-5", "-1", "3"],
            ("-5", "-3", "-1", "1", "3"),
        ),
        (
            ["1e100", "2e100"],
            (
                "1" + ("0" * 100),
                "1" + ("25" + ("0" * 98)),
                "1" + ("5" + ("0" * 99)),
                "1" + ("75" + ("0" * 98)),
                "2" + ("0" * 100),
            ),
        ),
        (
            ["1e-100", "2e-100"],
            (
                "0." + ("0" * 99) + "1",
                "0." + ("0" * 99) + "125",
                "0." + ("0" * 99) + "15",
                "0." + ("0" * 99) + "175",
                "0." + ("0" * 99) + "2",
            ),
        ),
    ],
)
def test_distributions_use_exact_linear_quartiles(
    values: list[str], expected: tuple[str, ...]
) -> None:
    distribution = _summary(values)["distributions"]["metric"]
    assert tuple(
        distribution[key] for key in ("min", "p25", "median", "p75", "max")
    ) == expected


def test_empty_null_bool_date_and_text_metrics_stay_visible_unavailable() -> None:
    from discovery.summaries import build_stage_summary

    ruler = _ruler()
    empty = build_stage_summary(
        _stage(
            track="deep_value",
            stage="track_gate",
            status="failed",
            ruler_version_id=ruler.id,
        ),
        [],
        ruler_version=ruler,
    )
    mixed = _summary([None, True, "2026-09-17", "not-a-number"])

    assert empty["distributions"]["metric"]["status"] == "unavailable"
    assert mixed["distributions"]["metric"] == {
        "status": "unavailable",
        "reason": "no_numeric_values",
        "count": 0,
        "min": None,
        "p25": None,
        "median": None,
        "p75": None,
        "max": None,
    }


def test_threshold_markers_cover_endpoints_ties_and_unresolved_keys() -> None:
    summary = _summary(
        ["1", "2", "2", "3"],
        thresholds={
            "metric_floor": "1",
            "metric": "2",
            "metric_ceiling": "3",
            "mystery_floor": "2",
        },
    )
    markers = {marker["id"]: marker for marker in summary["threshold_markers"]}

    assert markers["metric_floor"]["position"] == "0"
    assert markers["metric"]["position"] == "50"
    assert markers["metric_ceiling"]["position"] == "100"
    assert markers["metric_floor"]["direction"] == "gte"
    assert markers["metric_ceiling"]["direction"] == "lte"
    assert markers["mystery_floor"] == {
        "id": "mystery_floor",
        "threshold": "2",
        "metric": None,
        "direction": "gte",
        "position": None,
        "status": "unresolved",
    }


def test_threshold_markers_rank_signed_values() -> None:
    summary = _summary(
        ["-5", "-1", "3"],
        thresholds={"metric_floor": "-1", "metric_ceiling": "1"},
    )
    markers = {marker["id"]: marker for marker in summary["threshold_markers"]}
    assert markers["metric_floor"]["position"] == "50"
    assert markers["metric_ceiling"]["position"] == "75"


def test_extreme_quartiles_are_exact_and_context_independent() -> None:
    original = getcontext().copy()
    hostile = original.copy()
    hostile.prec = 2
    try:
        setcontext(hostile)
        distribution = _summary(["1e-4095", "1"])["distributions"]["metric"]
        tiny = Fraction(1, 10**4095)
        assert Fraction(distribution["p25"]) == (tiny * 3 + 1) / 4
        assert Fraction(distribution["median"]) == (tiny + 1) / 2
        assert Fraction(distribution["p75"]) == (tiny + 3) / 4
        assert getcontext().prec == 2
    finally:
        setcontext(original)


def test_summary_is_deterministic_under_hostile_decimal_context() -> None:
    from discovery.summaries import build_stage_summary

    original = getcontext().copy()
    hostile = original.copy()
    hostile.prec = 2
    hostile.traps[DivisionByZero] = True
    try:
        setcontext(hostile)
        first = _summary(["1", "2", "3", "4"])
        assert getcontext().prec == 2
        second = _summary(["1", "2", "3", "4"])
        assert first == second
        assert list(first["distributions"]) == ["metric"]
        common = build_stage_summary(
            _stage(counts=(1, 1, 0, 0)),
            [_result("600000.SH", {"z": "2", "a": "1"})],
        )
        assert list(common["distributions"]) == sorted(
            common["distributions"]
        )
    finally:
        setcontext(original)


@pytest.mark.parametrize(
    "metrics_json",
    ["{", '{"metric":NaN}', "[]", '{"metric":1,"metric":2}'],
)
def test_malformed_metrics_are_corruption(metrics_json: str) -> None:
    from discovery.summaries import DiscoverySummaryError, build_stage_summary

    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            _stage(counts=(1, 1, 0, 0)),
            [_result("600000.SH", metrics_json)],
        )


def test_duplicate_results_or_impossible_overrides_are_corruption() -> None:
    from discovery.summaries import DiscoverySummaryError, build_stage_summary

    ruler = _ruler()
    stage = _stage(
        track="deep_value",
        stage="track_gate",
        counts=(1, 1, 0, 0),
        ruler_version_id=ruler.id,
    )
    row = _result(
        "600000.SH",
        {"metric": "1"},
        track="deep_value",
        stage="track_gate",
    )
    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(stage, [row, row], ruler_version=ruler)
    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            stage, [row], [_override("600001.SH")], ruler
        )
    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            stage,
            [row],
            [_override("600000.SH", action="maybe")],
            ruler,
        )


def test_malformed_persisted_ruler_is_corruption() -> None:
    from discovery.summaries import DiscoverySummaryError, build_stage_summary

    ruler = _ruler()
    ruler.payload_json = "{}"
    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            _stage(
                track="deep_value",
                stage="track_gate",
                status="failed",
                ruler_version_id=ruler.id,
            ),
            [],
            ruler_version=ruler,
        )


def test_ruler_decimal_exponent_bombs_fail_before_formatting() -> None:
    import hashlib

    from app.models import RulerVersion
    from discovery.rulers import TrackRuler, validate_ruler_version

    payload = discovery_ruler_payload("deep_value")
    payload["thresholds"] = {"metric_floor": "1e100000000"}
    with pytest.raises(ValueError):
        TrackRuler.from_payload(payload)

    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    row = RulerVersion(
        id=88,
        user_id=1,
        scope="discovery:deep_value",
        digest=hashlib.sha256(payload_json.encode()).hexdigest(),
        reason="corrupt exponent",
        payload_json=payload_json,
        activated_at=SNAPSHOT_AT,
    )
    with pytest.raises(ValueError):
        validate_ruler_version(row, allow_retired_algorithm=True)


def test_summary_metric_exponent_bomb_is_corruption() -> None:
    from discovery.summaries import DiscoverySummaryError, build_stage_summary

    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            _stage(counts=(1, 1, 0, 0)),
            [_result("600000.SH", {"price": "1e100000000"})],
        )


def test_inflight_and_failed_attempts_require_empty_artifacts_and_counters() -> None:
    from discovery.summaries import DiscoverySummaryError, build_stage_summary

    for status in ("queued", "running", "failed"):
        with pytest.raises(DiscoverySummaryError):
            build_stage_summary(
                _stage(status=status, counts=(1, 1, 0, 0)),
                [],
            )
        with pytest.raises(DiscoverySummaryError):
            build_stage_summary(
                _stage(status=status),
                [_result("600000.SH", {"price": "1"})],
            )


def test_complete_counter_drift_is_corruption() -> None:
    from discovery.summaries import DiscoverySummaryError, build_stage_summary

    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            _stage(counts=(2, 2, 0, 0)),
            [_result("600000.SH", {"price": "1"})],
        )


def test_track_state_ruler_and_lineage_matrix_is_strict() -> None:
    from discovery.summaries import DiscoverySummaryError, build_stage_summary

    ruler = _ruler()
    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            _stage(
                track="deep_value",
                stage="track_gate",
                status="confirmed",
                ruler_version_id=None,
            ),
            [],
        )
    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            _stage(
                track="deep_value",
                stage="track_gate",
                status="ruler_not_ready",
                ruler_version_id=ruler.id,
            ),
            [],
            ruler_version=ruler,
        )
    broken_lineage = _stage(
        track="deep_value",
        stage="competition",
        status="data_not_ready",
        ruler_version_id=ruler.id,
    )
    broken_lineage.predecessor_stage = "competition"
    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            broken_lineage, [], ruler_version=ruler
        )
    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(
            _stage(),
            [],
            ruler_version=ruler,
        )


def test_metric_aggregation_is_one_pass_over_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from discovery import summaries

    visits = 0

    class CountingMetrics(dict[str, object]):
        def items(self):
            parent = super().items()

            def counted():
                nonlocal visits
                for item in parent:
                    visits += 1
                    yield item

            return counted()

        def __contains__(self, key: object) -> bool:
            raise AssertionError("aggregation rescanned a result mapping")

    rows = [
        _result(f"{index:06d}.SZ", "{}")
        for index in range(40)
    ]
    metrics = [
        CountingMetrics({f"metric_{index}": str(index)})
        for index in range(40)
    ]
    monkeypatch.setattr(summaries, "_metrics_json", lambda _text: metrics.pop(0))
    summary = summaries.build_stage_summary(
        _stage(counts=(40, 40, 0, 0)),
        rows,
    )
    assert visits == 40
    assert len(summary["distributions"]) >= 40


@pytest.mark.parametrize(
    "limit_name",
    [
        "SUMMARY_METRICS_JSON_BYTE_LIMIT",
        "SUMMARY_METRIC_KEYS_PER_RESULT_LIMIT",
        "SUMMARY_UNIQUE_METRICS_LIMIT",
        "SUMMARY_TOTAL_METRIC_CELLS_LIMIT",
    ],
)
def test_metric_work_caps_fail_closed(
    monkeypatch: pytest.MonkeyPatch, limit_name: str
) -> None:
    from discovery import summaries

    monkeypatch.setattr(summaries, limit_name, 1)
    metrics = (
        '{"long":"值值"}'
        if limit_name == "SUMMARY_METRICS_JSON_BYTE_LIMIT"
        else {"a": "1", "b": "2"}
    )
    with pytest.raises(summaries.DiscoverySummaryError):
        summaries.build_stage_summary(
            _stage(counts=(1, 1, 0, 0)),
            [_result("600000.SH", metrics)],
        )


def test_metrics_json_limit_counts_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from discovery import summaries

    text = '{"a":"值"}'
    assert len(text) < len(text.encode("utf-8"))
    monkeypatch.setattr(
        summaries, "SUMMARY_METRICS_JSON_BYTE_LIMIT", len(text)
    )
    with pytest.raises(summaries.DiscoverySummaryError):
        summaries.build_stage_summary(
            _stage(counts=(1, 1, 0, 0)),
            [_result("600000.SH", text)],
        )


def test_attempt_metrics_byte_cap_is_incremental_and_allows_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from discovery import summaries

    row_bytes = summaries.SUMMARY_METRICS_JSON_BYTE_LIMIT
    prefix, suffix = '{"blob":{"padding":"', '"}}'
    text = prefix + ("a" * (row_bytes - len(prefix) - len(suffix))) + suffix
    assert len(text.encode("utf-8")) == row_bytes
    rows = [
        _result(f"{index:06d}.SZ", text)
        for index in range(8)
    ]
    parsed = 0
    original = summaries._metrics_json

    def counted(value):
        nonlocal parsed
        parsed += 1
        return original(value)

    monkeypatch.setattr(summaries, "_metrics_json", counted)
    monkeypatch.setattr(
        summaries,
        "SUMMARY_ATTEMPT_METRICS_JSON_BYTE_LIMIT",
        7 * row_bytes,
    )
    with pytest.raises(summaries.DiscoverySummaryError):
        summaries.build_stage_summary(
            _stage(counts=(8, 8, 0, 0)),
            rows,
        )
    assert parsed == 7

    parsed = 0
    monkeypatch.setattr(
        summaries,
        "SUMMARY_ATTEMPT_METRICS_JSON_BYTE_LIMIT",
        8 * row_bytes,
    )
    summary = summaries.build_stage_summary(
        _stage(counts=(8, 8, 0, 0)),
        rows,
    )
    assert parsed == 8
    assert summary["counts"]["input"] == 8


def test_attempt_metrics_byte_cap_counts_unicode_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from discovery import summaries

    text = '{"a":"值"}'
    byte_count = len(text.encode("utf-8"))
    assert len(text) < byte_count
    rows = [_result("600000.SH", text), _result("600001.SH", text)]
    parsed = 0
    original = summaries._metrics_json

    def counted(value):
        nonlocal parsed
        parsed += 1
        return original(value)

    monkeypatch.setattr(summaries, "_metrics_json", counted)
    monkeypatch.setattr(
        summaries,
        "SUMMARY_ATTEMPT_METRICS_JSON_BYTE_LIMIT",
        (2 * byte_count) - 1,
    )
    with pytest.raises(summaries.DiscoverySummaryError):
        summaries.build_stage_summary(
            _stage(counts=(2, 2, 0, 0)),
            rows,
        )
    assert parsed == 1


def test_known_ruler_metrics_count_toward_unique_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from discovery import summaries

    payload = discovery_ruler_payload("deep_value")
    payload["fields"] = ["a", "b", "code"]
    payload["formulas"] = {"a": "a", "b": "b"}
    payload["missing_policy"] = {
        "a": "insufficient",
        "b": "insufficient",
        "code": "insufficient",
    }
    payload["order"] = [
        ["a", "desc", "nulls_last"],
        ["code", "asc", "nulls_last"],
    ]
    from app.models import RulerVersion
    from discovery.rulers import canonical_payload, payload_digest

    ruler = RulerVersion(
        id=9,
        user_id=1,
        scope="discovery:deep_value",
        digest=payload_digest(payload),
        reason="many metrics",
        payload_json=canonical_payload(payload),
        activated_at=SNAPSHOT_AT,
    )
    monkeypatch.setattr(summaries, "SUMMARY_UNIQUE_METRICS_LIMIT", 1)
    with pytest.raises(summaries.DiscoverySummaryError):
        summaries.build_stage_summary(
            _stage(
                track="deep_value",
                stage="track_gate",
                status="failed",
                ruler_version_id=ruler.id,
            ),
            [],
            ruler_version=ruler,
        )


def test_retired_build_historical_summaries_remain_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from discovery import rulers
    from discovery.summaries import build_stage_summary

    ruler = _ruler()
    stage = _stage(
        track="deep_value",
        stage="track_gate",
        counts=(1, 1, 0, 0),
        ruler_version_id=ruler.id,
    )
    rows = [
        _result(
            "600000.SH",
            {"metric": "2"},
            track="deep_value",
            stage="track_gate",
        )
    ]
    before_track = build_stage_summary(stage, rows, ruler_version=ruler)
    before_common = build_stage_summary(
        _stage(counts=(1, 1, 0, 0)),
        [_result("600000.SH", {"price": "2"})],
    )
    monkeypatch.setattr(rulers, "AVAILABLE_BUILDS", frozenset())
    with pytest.raises(ValueError):
        rulers.validate_ruler_version(ruler)
    historical = rulers.validate_ruler_version(
        ruler, allow_retired_algorithm=True
    )
    with pytest.raises(ValueError):
        rulers.canonical_payload(historical)
    assert build_stage_summary(stage, rows, ruler_version=ruler) == before_track
    assert build_stage_summary(
        _stage(counts=(1, 1, 0, 0)),
        [_result("600000.SH", {"price": "2"})],
    ) == before_common


def test_unknown_historical_common_contract_is_corruption() -> None:
    from discovery.summaries import DiscoverySummaryError, build_stage_summary

    stage = _stage()
    stage.algorithm_version = "retired-unknown"
    with pytest.raises(DiscoverySummaryError):
        build_stage_summary(stage, [])


def test_uncalibrated_track_is_explicitly_unavailable() -> None:
    from discovery.summaries import build_stage_summary

    summary = build_stage_summary(
        _stage(
            track="garp",
            stage="track_gate",
            status="ruler_not_ready",
        ),
        [],
    )
    assert summary["ruler"] == {
        "version_id": None,
        "reason": "ruler_not_ready",
        "scope": "discovery:garp",
        "algorithm_version": None,
        "status": "unavailable",
    }
    assert summary["thresholds"] == []
    assert summary["threshold_markers"] == []


def _activate(session, track: str, threshold: str, reason: str):
    from discovery.rulers import activate

    payload = discovery_ruler_payload(track)
    if track == "quality_discount":
        payload["thresholds"]["roe_mean_min"] = threshold
    else:
        payload["thresholds"] = {"metric_floor": threshold}
    return activate(session, payload, reason)


def test_routes_attach_attempt_local_summaries_without_market_queries(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.db import create_session_factory, init_db
    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
        DiscoveryOverride,
        DiscoveryResult,
        DiscoveryStage,
    )
    from app.main import create_app
    from app.models import RulerVersion
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "summary-routes.db"
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    with factory() as session:
        old = _activate(session, "quality_discount", "1", "旧尺子")
        newer = _activate(session, "quality_discount", "50", "新尺子")
        deep = _activate(session, "deep_value", "5", "深度价值尺子")
        garp = _activate(session, "garp", "2", "成长尺子")
        import hashlib

        bomb_json = garp.payload_json.replace(
            '"metric_floor":"2"', '"metric_floor":"1e100000000"'
        )
        bomb_ruler = RulerVersion(
            user_id=1,
            scope=garp.scope,
            digest=hashlib.sha256(bomb_json.encode()).hexdigest(),
            reason="指数炸弹",
            payload_json=bomb_json,
            activated_at=SNAPSHOT_AT + timedelta(seconds=1),
        )
        session.add(bomb_ruler)
        session.flush()
        run = add_unfrozen_run(
            session,
            week_key="2026-W38",
            ruler_ids={
                "quality_discount": newer.id,
                "deep_value": deep.id,
                "garp": garp.id,
            },
        )
        run.current = True
        run.status = "running"
        run.input_fingerprint = "frozen-input"
        run.snapshot_at = SNAPSHOT_AT
        common = DiscoveryCommonStage(
            run_id=run.id,
            attempt=1,
            status="confirmed",
            total_count=1,
            pass_count=1,
            drop_count=0,
            insufficient_count=0,
            input_fingerprint="frozen-input",
            algorithm_version="discovery-v1",
            confirmed_at=SNAPSHOT_AT,
            created_at=SNAPSHOT_AT,
            updated_at=SNAPSHOT_AT,
        )
        session.add(common)
        session.flush()
        session.add(
            DiscoveryCommonResult(
                run_id=run.id,
                attempt=1,
                code="600000.SH",
                verdict="pass",
                reason=None,
                metrics_json='{"price":"12.5"}',
                created_at=SNAPSHOT_AT,
            )
        )

        stages = [
            DiscoveryStage(
                run_id=run.id,
                track="quality_discount",
                stage="track_gate",
                attempt=1,
                status="confirmed",
                total_count=1,
                pass_count=1,
                drop_count=0,
                insufficient_count=0,
                input_fingerprint="frozen-input",
                ruler_version_id=old.id,
                algorithm_version="discovery-v1",
                common_stage_id=common.id,
                predecessor_stage_id=None,
                predecessor_stage=None,
                confirmed_at=SNAPSHOT_AT,
                superseded_at=SNAPSHOT_AT + timedelta(seconds=1),
                created_at=SNAPSHOT_AT,
                updated_at=SNAPSHOT_AT,
            ),
            DiscoveryStage(
                run_id=run.id,
                track="quality_discount",
                stage="track_gate",
                attempt=2,
                status="waiting_confirmation",
                total_count=1,
                pass_count=1,
                drop_count=0,
                insufficient_count=0,
                input_fingerprint="frozen-input",
                ruler_version_id=newer.id,
                algorithm_version="discovery-v1",
                common_stage_id=common.id,
                predecessor_stage_id=None,
                predecessor_stage=None,
                created_at=SNAPSHOT_AT + timedelta(seconds=1),
                updated_at=SNAPSHOT_AT + timedelta(seconds=1),
            ),
            DiscoveryStage(
                run_id=run.id,
                track="deep_value",
                stage="track_gate",
                attempt=1,
                status="waiting_confirmation",
                total_count=1,
                pass_count=1,
                drop_count=0,
                insufficient_count=0,
                input_fingerprint="frozen-input",
                ruler_version_id=deep.id,
                algorithm_version="discovery-v1",
                common_stage_id=common.id,
                predecessor_stage_id=None,
                predecessor_stage=None,
                created_at=SNAPSHOT_AT,
                updated_at=SNAPSHOT_AT,
            ),
        ]
        session.add_all(stages)
        session.add(
            DiscoveryStage(
                run_id=run.id,
                track="garp",
                stage="track_gate",
                attempt=1,
                status="failed",
                total_count=0,
                pass_count=0,
                drop_count=0,
                insufficient_count=0,
                input_fingerprint="frozen-input",
                ruler_version_id=bomb_ruler.id,
                algorithm_version="discovery-v1",
                common_stage_id=common.id,
                predecessor_stage_id=None,
                predecessor_stage=None,
                failure_kind="permanent",
                created_at=SNAPSHOT_AT,
                updated_at=SNAPSHOT_AT,
            )
        )
        session.flush()
        session.add_all(
            [
                DiscoveryResult(
                    run_id=run.id,
                    track=stage.track,
                    stage="track_gate",
                    attempt=stage.attempt,
                    code="600000.SH",
                    verdict="pass",
                    reason=None,
                    metrics_json=f'{{"metric":"{value}"}}',
                    rank=1,
                    created_at=SNAPSHOT_AT,
                )
                for stage, value in zip(stages, ("1", "100", "999"), strict=True)
            ]
        )
        override = DiscoveryOverride(
            run_id=run.id,
            track="quality_discount",
            stage="track_gate",
            attempt=2,
            code="600000.SH",
            action="forced_drop",
            reason="人工复核",
            created_at=SNAPSHOT_AT,
        )
        session.add(override)
        session.commit()
        run_id = run.id
        old_id, newer_id = old.id, newer.id
        override_id = override.id

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    with TestClient(app) as client:
        event.listen(app.state.engine, "before_cursor_execute", capture)
        try:
            safety = client.get(
                f"/api/discoveries/{run_id}/stages/safety"
            )
            quality = client.get(
                f"/api/discoveries/{run_id}/tracks/quality_discount"
                "/stages/track_gate"
            )
        finally:
            event.remove(app.state.engine, "before_cursor_execute", capture)

        assert safety.status_code == quality.status_code == 200
        safety_body, quality_body = safety.json(), quality.json()
        assert safety_body["summary"] == safety_body["current"]["summary"]
        assert quality_body["summary"] == quality_body["current"]["summary"]
        assert list(safety_body["summary"]) == list(quality_body["summary"])
        assert all("summary" in row for row in quality_body["attempts"])
        assert [
            row["summary"]["ruler"]["version_id"]
            for row in quality_body["attempts"]
        ] == [old_id, newer_id]
        assert [
            row["summary"]["distributions"]["metric"]["median"]
            for row in quality_body["attempts"]
        ] == ["1", "100"]
        assert quality_body["summary"]["counts"]["drop"] == 1
        assert quality_body["summary"]["counts"]["overrides"] == 1
        assert quality_body["pass"] == []
        assert quality_body["drop"][0]["code"] == "600000.SH"
        assert safety_body["pass"][0]["metrics"]["price"] == "12.5"

        forbidden = (
            "financials",
            "valuation_history",
            "settings",
            "discovery_input_snapshots",
        )
        assert not any(
            table in statement
            for statement in statements
            for table in forbidden
        )
        assert not any(
            statement.lstrip().startswith(("insert ", "update ", "delete "))
            for statement in statements
        )

        bomb = client.get(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate"
        )
        assert bomb.status_code == 500
        assert bomb.json()["code"] == "DISCOVERY_RESULT_CORRUPT"

        with app.state.session_factory() as session:
            persisted = session.get(DiscoveryOverride, override_id)
            assert persisted is not None
            persisted.action = "corrupt"
            session.commit()
        corrupt = client.get(
            f"/api/discoveries/{run_id}/tracks/quality_discount"
            "/stages/track_gate"
        )
        assert corrupt.status_code == 500
        assert corrupt.json()["code"] == "DISCOVERY_RESULT_CORRUPT"

        from discovery import summaries

        monkeypatch.setattr(
            summaries, "SUMMARY_ATTEMPT_METRICS_JSON_BYTE_LIMIT", 1
        )
        aggregate_corrupt = client.get(
            f"/api/discoveries/{run_id}/stages/safety"
        )
        assert aggregate_corrupt.status_code == 500
        assert (
            aggregate_corrupt.json()["code"]
            == "DISCOVERY_RESULT_CORRUPT"
        )

    engine.dispose()
