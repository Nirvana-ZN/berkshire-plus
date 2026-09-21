from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from discovery.diff import (
    PersistedCompanyResult,
    classify_weekly_changes,
    load_weekly_changes,
)


def result(
    week_key: str,
    run_id: int,
    *,
    code: str = "600000.SH",
    fingerprint: str = "data-1",
    ruler_id: int = 11,
    verdict: str = "pass",
    reason: str | None = "cheap",
    decision: str | None = None,
) -> PersistedCompanyResult:
    return PersistedCompanyResult(
        week_key=week_key,
        run_id=run_id,
        track="quality_discount",
        stage="adjudication",
        code=code,
        name=f"公司-{code}",
        input_fingerprint=fingerprint,
        ruler_version_id=ruler_id,
        verdict=verdict,
        reason=reason,
        decision=decision,
    )


@pytest.mark.parametrize(
    ("current", "cause"),
    [
        ({"input_fingerprint": "data-2"}, "data_changed"),
        ({"ruler_version_id": 12}, "rule_changed"),
        (
            {"input_fingerprint": "data-2", "ruler_version_id": 12},
            "both_changed",
        ),
        ({"decision": "exclude", "verdict": "drop"}, "decision_changed"),
    ],
)
def test_classifies_exact_persisted_change_cause(
    current: dict[str, object], cause: str
) -> None:
    old = result("2026-W37", 1, decision="keep")
    new = replace(result("2026-W38", 2, decision="keep"), **current)

    change = classify_weekly_changes((old, new), current_run_id=2)[0]

    assert change.cause == cause
    assert "both_changed" not in change.classifications or cause == "both_changed"


def test_classifies_new_dropped_continuous_and_reentered() -> None:
    rows = (
        result("2026-W35", 1, code="NEW", verdict="drop"),
        result("2026-W35", 1, code="DROP"),
        result("2026-W35", 1, code="CONT"),
        result("2026-W35", 1, code="BACK"),
        result("2026-W36", 2, code="DROP"),
        result("2026-W36", 2, code="CONT"),
        result("2026-W36", 2, code="BACK", verdict="drop"),
        result("2026-W37", 3, code="NEW"),
        result("2026-W37", 3, code="DROP", verdict="drop"),
        result("2026-W37", 3, code="CONT"),
        result("2026-W37", 3, code="BACK"),
    )

    changes = {
        change.code: change
        for change in classify_weekly_changes(rows, current_run_id=3)
    }

    assert changes["NEW"].membership == "new"
    assert changes["DROP"].membership == "dropped"
    assert changes["CONT"].membership == "continuous"
    assert changes["BACK"].membership == "reentered"


def test_reason_change_keeps_independent_cause() -> None:
    rows = (
        result("2026-W37", 1, reason="old"),
        result("2026-W38", 2, fingerprint="data-2", reason="new"),
    )

    change = classify_weekly_changes(rows, current_run_id=2)[0]

    assert change.membership == "continuous"
    assert change.reason_changed is True
    assert change.cause == "data_changed"
    assert change.classifications == (
        "continuous",
        "reason_changed",
        "data_changed",
    )


def test_order_is_deterministic_and_history_is_run_scoped() -> None:
    rows = (
        result("2026-W38", 9, code="ZZZ"),
        result("2026-W37", 7, code="ZZZ"),
        result("2026-W38", 9, code="AAA"),
        result("2026-W37", 7, code="AAA"),
        result("2026-W38", 10, code="OTHER"),
    )

    changes = classify_weekly_changes(rows, current_run_id=9)

    assert [change.code for change in changes] == ["AAA", "ZZZ"]
    assert all(change.current_run_id == 9 for change in changes)


@pytest.mark.parametrize(
    "bad",
    [
        {"week_key": ""},
        {"run_id": True},
        {"ruler_version_id": True},
        {"verdict": "maybe"},
        {"decision": "buy"},
    ],
)
def test_rejects_malformed_persisted_rows(bad: dict[str, object]) -> None:
    values = result("2026-W38", 1).__dict__ | bad

    with pytest.raises((TypeError, ValueError)):
        PersistedCompanyResult(**values)


def test_rejects_duplicate_company_rows_in_one_run() -> None:
    row = result("2026-W38", 1)

    with pytest.raises(ValueError, match="duplicate"):
        classify_weekly_changes((row, row), current_run_id=1)


def test_iso_year_rollover_and_absent_week_are_distinct() -> None:
    continuous = classify_weekly_changes(
        (
            result("2026-W53", 1),
            result("2027-W01", 2),
        ),
        current_run_id=2,
    )
    reentered = classify_weekly_changes(
        (
            result("2026-W52", 1),
            result("2027-W01", 3),
        ),
        current_run_id=3,
        run_order=(
            ("2026-W52", 1),
            ("2026-W53", 2),
            ("2027-W01", 3),
        ),
    )

    assert continuous[0].membership == "continuous"
    assert reentered[0].membership == "reentered"


def test_dropped_is_reported_only_for_the_first_absent_week() -> None:
    old = result("2026-W35", 1)

    assert classify_weekly_changes(
        (old,),
        current_run_id=2,
        run_order=(("2026-W35", 1), ("2026-W36", 2)),
    )[0].membership == "dropped"
    assert classify_weekly_changes(
        (old,),
        current_run_id=3,
        run_order=(
            ("2026-W35", 1),
            ("2026-W36", 2),
            ("2026-W37", 3),
        ),
    ) == ()


def test_routes_expose_compact_and_typed_persisted_changes(tmp_path) -> None:
    from datetime import timedelta

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.db import create_session_factory, init_db
    from app.discovery_models import (
        DiscoveryCommonStage,
        DiscoveryResult,
        DiscoveryStage,
    )
    from app.main import create_app
    from discovery.rulers import activate
    from discovery.snapshot import freeze_inputs
    from market.quotes import FakeQuoteClient
    from tests.discovery_helpers import (
        SNAPSHOT_AT,
        add_unfrozen_run,
        company,
        discovery_ruler_payload,
        versions,
    )

    db_path = tmp_path / "diff-routes.db"
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    with factory() as session:
        rulers = {
            track: activate(
                session,
                discovery_ruler_payload(track),
                f"activate {track}",
            ).id
            for track in ("quality_discount", "deep_value", "garp")
        }
        run_ids = []
        for index, (week_key, amount) in enumerate(
            (("2026-W37", "10.25"), ("2026-W38", "11.25"))
        ):
            run = add_unfrozen_run(
                session,
                week_key=week_key,
                ruler_ids=rulers,
            )
            freeze_inputs(
                session,
                run,
                [company("600000.SH", amount=Decimal(amount))],
                versions(ruler_ids=rulers),
                {"SH": ["2026-09-16", "2026-09-17"]},
                snapshot_at=SNAPSHOT_AT + timedelta(days=index),
            )
            run.current = index == 1
            run.status = "completed"
            common = DiscoveryCommonStage(
                run_id=run.id,
                attempt=1,
                status="confirmed",
                total_count=1,
                pass_count=1,
                drop_count=0,
                insufficient_count=0,
                input_fingerprint=run.input_fingerprint,
                algorithm_version=run.algorithm_version,
                confirmed_at=SNAPSHOT_AT,
                created_at=SNAPSHOT_AT,
                updated_at=SNAPSHOT_AT,
            )
            session.add(common)
            session.flush()
            stage = DiscoveryStage(
                run_id=run.id,
                track="quality_discount",
                stage="track_gate",
                attempt=1,
                status="confirmed",
                total_count=1,
                pass_count=1,
                drop_count=0,
                insufficient_count=0,
                input_fingerprint=run.input_fingerprint,
                ruler_version_id=rulers["quality_discount"],
                algorithm_version=run.algorithm_version,
                common_stage_id=common.id,
                confirmed_at=SNAPSHOT_AT,
                created_at=SNAPSHOT_AT,
                updated_at=SNAPSHOT_AT,
            )
            session.add(stage)
            session.flush()
            session.add(
                DiscoveryResult(
                    run_id=run.id,
                    track="quality_discount",
                    stage="track_gate",
                    attempt=1,
                    code="600000.SH",
                    verdict="pass",
                    reason="cheap",
                    metrics_json='{"metric":"1"}',
                    rank=None,
                    created_at=SNAPSHOT_AT,
                )
            )
            run_ids.append(run.id)
        session.commit()
        loaded = load_weekly_changes(
            session,
            run_ids[-1],
            track="quality_discount",
            stage="track_gate",
        )
        assert loaded[0].cause == "data_changed"
        current_result = session.scalar(
            select(DiscoveryResult).where(
                DiscoveryResult.run_id == run_ids[-1]
            )
        )
        current_stage = session.scalar(
            select(DiscoveryStage).where(
                DiscoveryStage.run_id == run_ids[-1]
            )
        )
        assert current_result is not None and current_stage is not None
        session.delete(current_result)
        current_stage.total_count = 0
        current_stage.pass_count = 0
        session.flush()
        dropped = load_weekly_changes(
            session,
            run_ids[-1],
            track="quality_discount",
            stage="track_gate",
        )
        assert dropped[0].membership == "dropped"
        session.rollback()
        old_result = session.scalar(
            select(DiscoveryResult).where(
                DiscoveryResult.run_id == run_ids[0]
            )
        )
        assert old_result is not None
        old_result.metrics_json = '{"metric":"1"} '
        session.flush()
        assert load_weekly_changes(
            session,
            run_ids[-1],
            track="quality_discount",
            stage="track_gate",
        ) == ()
        session.rollback()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        current = client.get("/api/discoveries/current")
        details = client.get(
            f"/api/discoveries/{run_ids[-1]}/tracks/quality_discount"
            "/stages/track_gate"
        )

    assert current.status_code == details.status_code == 200
    assert current.json()["changes"] == [
        {
            "track": "quality_discount",
            "stage": "track_gate",
            "code": "600000.SH",
            "name": "公司-600000.SH",
            "classification": "data_changed",
            "membership": "continuous",
            "cause": "data_changed",
            "reason_changed": False,
            "previous_state": "pass",
            "current_state": "pass",
        }
    ]
    detail = details.json()["changes"][0]
    assert detail["classifications"] == ["continuous", "data_changed"]
    assert detail["previous_run_id"] == run_ids[0]
    assert detail["current_run_id"] == run_ids[1]
    engine.dispose()
