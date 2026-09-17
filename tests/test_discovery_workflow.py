from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import create_session_factory, init_db


EXPECTED_COLUMNS = {
    "discovery_runs": {
        "id",
        "user_id",
        "week_key",
        "current",
        "status",
        "created_at",
        "updated_at",
        "snapshot_at",
        "data_version",
        "financial_version",
        "valuation_version",
        "quality_discount_ruler_version_id",
        "deep_value_ruler_version_id",
        "garp_ruler_version_id",
        "algorithm_version",
        "input_fingerprint",
        "error",
        "replay_of_run_id",
    },
    "discovery_input_snapshots": {
        "id",
        "run_id",
        "code",
        "name",
        "industry",
        "security_status",
        "fields_json",
        "sources_json",
        "as_of_json",
        "data_version",
        "financial_version",
        "valuation_version",
        "algorithm_version",
        "input_fingerprint",
        "snapshot_at",
    },
    "discovery_common_stages": {
        "id",
        "run_id",
        "attempt",
        "status",
        "total_count",
        "pass_count",
        "drop_count",
        "insufficient_count",
        "error",
        "confirmed_at",
        "input_fingerprint",
        "algorithm_version",
        "failure_kind",
        "superseded_at",
        "created_at",
        "updated_at",
    },
    "discovery_common_results": {
        "id",
        "run_id",
        "attempt",
        "code",
        "verdict",
        "reason",
        "metrics_json",
        "created_at",
    },
    "discovery_stages": {
        "id",
        "run_id",
        "track",
        "stage",
        "attempt",
        "status",
        "total_count",
        "pass_count",
        "drop_count",
        "insufficient_count",
        "error",
        "confirmed_at",
        "input_fingerprint",
        "ruler_version_id",
        "algorithm_version",
        "failure_kind",
        "superseded_at",
        "created_at",
        "updated_at",
    },
    "discovery_results": {
        "id",
        "run_id",
        "track",
        "stage",
        "attempt",
        "code",
        "verdict",
        "reason",
        "metrics_json",
        "rank",
        "created_at",
    },
    "discovery_overrides": {
        "id",
        "run_id",
        "track",
        "stage",
        "attempt",
        "code",
        "action",
        "reason",
        "created_at",
    },
    "discovery_decisions": {
        "id",
        "run_id",
        "track",
        "competition_attempt",
        "competition_stage_id",
        "stage",
        "code",
        "decision",
        "exclusion_reason",
        "keep_note",
        "created_at",
    },
    "discovery_evidence": {
        "id",
        "run_id",
        "track",
        "competition_attempt",
        "competition_stage_id",
        "stage",
        "code",
        "ruler_version_id",
        "rule_version",
        "kind",
        "rule_id",
        "input_fields_json",
        "display_text",
        "as_of",
        "data_completeness",
        "created_at",
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_values(*, week_key: str = "2026-W38", current: bool = True) -> dict:
    now = _now()
    return {
        "user_id": 1,
        "week_key": week_key,
        "current": current,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "snapshot_at": now,
        "data_version": "data-v1",
        "financial_version": "financial-v1",
        "valuation_version": "valuation-v1",
        "algorithm_version": "algorithm-v1",
        "input_fingerprint": "run-fingerprint",
    }


def _insert_run(session, **overrides) -> int:
    from app.discovery_models import DiscoveryRun

    values = _run_values()
    values.update(overrides)
    row = DiscoveryRun(**values)
    session.add(row)
    session.flush()
    return row.id


def _add_common_stage(session, run_id: int, attempt: int = 1):
    from app.discovery_models import DiscoveryCommonStage

    now = _now()
    row = DiscoveryCommonStage(
        run_id=run_id,
        attempt=attempt,
        status="queued",
        total_count=0,
        pass_count=0,
        drop_count=0,
        insufficient_count=0,
        input_fingerprint="run-fingerprint",
        algorithm_version="algorithm-v1",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _add_track_stage(
    session,
    run_id: int,
    *,
    track: str = "garp",
    stage: str = "track_gate",
    attempt: int = 1,
):
    from app.discovery_models import DiscoveryStage

    now = _now()
    row = DiscoveryStage(
        run_id=run_id,
        track=track,
        stage=stage,
        attempt=attempt,
        status="queued",
        total_count=0,
        pass_count=0,
        drop_count=0,
        insufficient_count=0,
        input_fingerprint="run-fingerprint",
        algorithm_version="algorithm-v1",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def test_discovery_schema_has_all_tables_columns_foreign_keys_and_index(
    tmp_path: Path,
) -> None:
    engine = init_db(tmp_path / "schema.db")
    schema = inspect(engine)

    assert EXPECTED_COLUMNS.keys() <= set(schema.get_table_names())
    for table, expected in EXPECTED_COLUMNS.items():
        assert expected == {
            column["name"] for column in schema.get_columns(table)
        }, table

    for table in {
        "discovery_input_snapshots",
        "discovery_common_stages",
        "discovery_stages",
    }:
        foreign_keys = schema.get_foreign_keys(table)
        assert any(
            fk["referred_table"] == "discovery_runs"
            and fk["constrained_columns"] == ["run_id"]
            and fk["options"].get("ondelete") == "CASCADE"
            for fk in foreign_keys
        ), table

    expected_stage_fks = {
        "discovery_common_results": (
            "discovery_common_stages",
            ["run_id", "attempt"],
            ["run_id", "attempt"],
        ),
        "discovery_results": (
            "discovery_stages",
            ["run_id", "track", "stage", "attempt"],
            ["run_id", "track", "stage", "attempt"],
        ),
        "discovery_overrides": (
            "discovery_stages",
            ["run_id", "track", "stage", "attempt"],
            ["run_id", "track", "stage", "attempt"],
        ),
        "discovery_decisions": (
            "discovery_stages",
            [
                "competition_stage_id",
                "run_id",
                "track",
                "stage",
                "competition_attempt",
            ],
            ["id", "run_id", "track", "stage", "attempt"],
        ),
        "discovery_evidence": (
            "discovery_stages",
            [
                "competition_stage_id",
                "run_id",
                "track",
                "stage",
                "competition_attempt",
            ],
            ["id", "run_id", "track", "stage", "attempt"],
        ),
    }
    for table, (parent, local_columns, parent_columns) in expected_stage_fks.items():
        assert any(
            fk["referred_table"] == parent
            and fk["constrained_columns"] == local_columns
            and fk["referred_columns"] == parent_columns
            and fk["options"].get("ondelete") == "CASCADE"
            for fk in schema.get_foreign_keys(table)
        ), table

    assert ("id", "run_id", "track", "stage", "attempt") in {
        tuple(constraint["column_names"])
        for constraint in schema.get_unique_constraints("discovery_stages")
    }

    run_fks = schema.get_foreign_keys("discovery_runs")
    assert any(
        fk["referred_table"] == "users"
        and fk["constrained_columns"] == ["user_id"]
        for fk in run_fks
    )
    assert any(
        fk["referred_table"] == "discovery_runs"
        and fk["constrained_columns"] == ["replay_of_run_id"]
        and fk["options"].get("ondelete") == "SET NULL"
        for fk in run_fks
    )
    assert {
        "quality_discount_ruler_version_id",
        "deep_value_ruler_version_id",
        "garp_ruler_version_id",
    } <= {
        fk["constrained_columns"][0]
        for fk in run_fks
        if fk["referred_table"] == "ruler_versions"
    }
    assert any(
        fk["referred_table"] == "ruler_versions"
        and fk["constrained_columns"] == ["ruler_version_id"]
        for fk in schema.get_foreign_keys("discovery_stages")
    )
    assert any(
        fk["referred_table"] == "ruler_versions"
        and fk["constrained_columns"] == ["ruler_version_id"]
        for fk in schema.get_foreign_keys("discovery_evidence")
    )

    with engine.connect() as conn:
        foreign_keys_on = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
        index_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = 'ux_discovery_current_week'"
            )
        ).scalar_one()
    assert foreign_keys_on == 1
    assert "WHERE current = 1" in index_sql


def test_current_week_partial_index_allows_history_but_rejects_second_current(
    tmp_path: Path,
) -> None:
    engine = init_db(tmp_path / "current.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        _insert_run(session, current=False, input_fingerprint="old-1")
        _insert_run(session, current=False, input_fingerprint="old-2")
        _insert_run(session, current=True, input_fingerprint="current")
        session.commit()

        with pytest.raises(IntegrityError):
            _insert_run(session, current=True, input_fingerprint="duplicate")
            session.commit()


def test_discovery_evidence_requires_ruler_version(tmp_path: Path) -> None:
    from app.discovery_models import DiscoveryEvidence

    engine = init_db(tmp_path / "evidence-ruler.db")
    ruler_column = next(
        column
        for column in inspect(engine).get_columns("discovery_evidence")
        if column["name"] == "ruler_version_id"
    )
    assert ruler_column["nullable"] is False
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run_id = _insert_run(session)
        competition = _add_track_stage(
            session, run_id, stage="competition"
        )
        session.add(
            DiscoveryEvidence(
                run_id=run_id,
                track="garp",
                competition_attempt=1,
                competition_stage_id=competition.id,
                code="600000.SH",
                rule_version="rules-v1",
                kind="evidence",
                rule_id="growth",
                input_fields_json='["revenue"]',
                display_text="Revenue grew.",
                as_of="2025-12-31",
                data_completeness="complete",
                created_at=_now(),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_discovery_composite_keys_reject_duplicates(tmp_path: Path) -> None:
    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
        DiscoveryDecision,
        DiscoveryEvidence,
        DiscoveryInputSnapshot,
        DiscoveryOverride,
        DiscoveryResult,
        DiscoveryStage,
    )

    engine = init_db(tmp_path / "unique.db")
    SessionLocal = create_session_factory(engine)
    now = _now()
    cases = [
        (
            DiscoveryInputSnapshot,
            {
                "code": "600000.SH",
                "name": "浦发银行",
                "industry": "银行",
                "security_status": "active",
                "fields_json": "{}",
                "sources_json": "{}",
                "as_of_json": "{}",
                "data_version": "d1",
                "financial_version": "f1",
                "valuation_version": "v1",
                "algorithm_version": "a1",
                "input_fingerprint": "company-1",
                "snapshot_at": now,
            },
        ),
        (
            DiscoveryCommonStage,
            {
                "attempt": 1,
                "status": "queued",
                "total_count": 0,
                "pass_count": 0,
                "drop_count": 0,
                "insufficient_count": 0,
                "input_fingerprint": "run-1",
                "algorithm_version": "a1",
                "created_at": now,
                "updated_at": now,
            },
        ),
        (
            DiscoveryCommonResult,
            {
                "attempt": 1,
                "code": "600000.SH",
                "verdict": "pass",
                "metrics_json": "{}",
                "created_at": now,
            },
        ),
        (
            DiscoveryStage,
            {
                "track": "garp",
                "stage": "track_gate",
                "attempt": 1,
                "status": "queued",
                "total_count": 0,
                "pass_count": 0,
                "drop_count": 0,
                "insufficient_count": 0,
                "input_fingerprint": "run-1",
                "algorithm_version": "a1",
                "created_at": now,
                "updated_at": now,
            },
        ),
        (
            DiscoveryResult,
            {
                "track": "garp",
                "stage": "track_gate",
                "attempt": 1,
                "code": "600000.SH",
                "verdict": "pass",
                "metrics_json": "{}",
                "created_at": now,
            },
        ),
        (
            DiscoveryOverride,
            {
                "track": "garp",
                "stage": "track_gate",
                "attempt": 1,
                "code": "600000.SH",
                "action": "forced_pass",
                "reason": "verified filing",
                "created_at": now,
            },
        ),
        (
            DiscoveryDecision,
            {
                "track": "garp",
                "competition_attempt": 1,
                "code": "600000.SH",
                "decision": "keep",
                "created_at": now,
            },
        ),
        (
            DiscoveryEvidence,
            {
                "track": "garp",
                "competition_attempt": 1,
                "code": "600000.SH",
                "ruler_version_id": 1,
                "rule_version": "rules-v1",
                "kind": "evidence",
                "rule_id": "growth",
                "input_fields_json": '["revenue"]',
                "display_text": "Revenue grew.",
                "as_of": "2025-12-31",
                "data_completeness": "complete",
                "created_at": now,
            },
        ),
    ]

    for model, values in cases:
        with SessionLocal() as session:
            run_id = _insert_run(
                session,
                week_key=f"2026-{model.__tablename__}",
                input_fingerprint=model.__tablename__,
            )
            if model is DiscoveryCommonResult:
                _add_common_stage(session, run_id)
            elif model in {DiscoveryResult, DiscoveryOverride}:
                _add_track_stage(
                    session,
                    run_id,
                    track=values["track"],
                    stage=values["stage"],
                    attempt=values["attempt"],
                )
            elif model in {DiscoveryDecision, DiscoveryEvidence}:
                competition = _add_track_stage(
                    session,
                    run_id,
                    track=values["track"],
                    stage="competition",
                    attempt=values["competition_attempt"],
                )
                values = values | {"competition_stage_id": competition.id}
            session.add_all(
                [model(run_id=run_id, **values), model(run_id=run_id, **values)]
            )
            with pytest.raises(IntegrityError):
                session.commit()


def test_discovery_artifacts_reject_missing_or_mismatched_stages(
    tmp_path: Path,
) -> None:
    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryDecision,
        DiscoveryEvidence,
        DiscoveryOverride,
        DiscoveryResult,
    )

    engine = init_db(tmp_path / "artifact-parent.db")
    SessionLocal = create_session_factory(engine)
    now = _now()
    with SessionLocal() as session:
        run_id = _insert_run(session, week_key="2026-W34")
        other_run_id = _insert_run(
            session,
            week_key="2026-W35",
            current=False,
            input_fingerprint="other",
        )
        _add_common_stage(session, run_id)
        competition = _add_track_stage(
            session, run_id, stage="competition"
        )
        session.commit()
        competition_id = competition.id

    invalid_rows = [
        DiscoveryCommonResult(
            run_id=run_id,
            attempt=2,
            code="600000.SH",
            verdict="pass",
            metrics_json="{}",
            created_at=now,
        ),
        DiscoveryResult(
            run_id=run_id,
            track="garp",
            stage="track_gate",
            attempt=1,
            code="600000.SH",
            verdict="pass",
            metrics_json="{}",
            created_at=now,
        ),
        DiscoveryOverride(
            run_id=run_id,
            track="garp",
            stage="competition",
            attempt=2,
            code="600000.SH",
            action="forced_pass",
            reason="verified",
            created_at=now,
        ),
        DiscoveryDecision(
            run_id=other_run_id,
            track="garp",
            competition_attempt=1,
            competition_stage_id=competition_id,
            code="600000.SH",
            decision="keep",
            created_at=now,
        ),
        DiscoveryEvidence(
            run_id=run_id,
            track="garp",
            competition_attempt=1,
            competition_stage_id=999_999,
            code="600000.SH",
            ruler_version_id=1,
            rule_version="rules-v1",
            kind="evidence",
            rule_id="growth",
            input_fields_json='["revenue"]',
            display_text="Revenue grew.",
            as_of="2025-12-31",
            data_completeness="complete",
            created_at=now,
        ),
    ]
    for row in invalid_rows:
        with SessionLocal() as session:
            session.add(row)
            with pytest.raises(IntegrityError):
                session.commit()


@pytest.mark.parametrize("artifact_kind", ["decision", "evidence"])
def test_competition_artifacts_reject_wrong_stage_and_accept_competition(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    from app.discovery_models import DiscoveryDecision, DiscoveryEvidence

    engine = init_db(tmp_path / f"{artifact_kind}-stage.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run_id = _insert_run(session)
        gate = _add_track_stage(session, run_id)
        competition = _add_track_stage(
            session, run_id, stage="competition"
        )
        session.commit()
        gate_id = gate.id
        competition_id = competition.id

    model = (
        DiscoveryDecision
        if artifact_kind == "decision"
        else DiscoveryEvidence
    )

    def artifact(stage_id: int, *, stage: str | None = None):
        values = {
            "run_id": run_id,
            "track": "garp",
            "competition_attempt": 1,
            "competition_stage_id": stage_id,
            "code": "600000.SH",
            "created_at": _now(),
        }
        if stage is not None:
            values["stage"] = stage
        if artifact_kind == "decision":
            values["decision"] = "keep"
        else:
            values.update(
                ruler_version_id=1,
                rule_version="rules-v1",
                kind="evidence",
                rule_id="growth",
                input_fields_json='["revenue"]',
                display_text="Revenue grew.",
                as_of="2025-12-31",
                data_completeness="complete",
            )
        return model(**values)

    for invalid in (
        artifact(gate_id),
        artifact(gate_id, stage="track_gate"),
    ):
        with SessionLocal() as session:
            session.add(invalid)
            with pytest.raises(IntegrityError):
                session.commit()

    with SessionLocal() as session:
        valid = artifact(competition_id)
        session.add(valid)
        session.commit()
        session.refresh(valid)
        assert valid.stage == "competition"


def test_deleting_stages_cascades_their_artifacts(tmp_path: Path) -> None:
    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryDecision,
        DiscoveryEvidence,
        DiscoveryOverride,
        DiscoveryResult,
    )

    engine = init_db(tmp_path / "artifact-cascade.db")
    SessionLocal = create_session_factory(engine)
    now = _now()
    with SessionLocal() as session:
        run_id = _insert_run(session)
        common = _add_common_stage(session, run_id)
        gate = _add_track_stage(session, run_id)
        competition = _add_track_stage(
            session, run_id, stage="competition"
        )
        session.add_all(
            [
                DiscoveryCommonResult(
                    run_id=run_id,
                    attempt=common.attempt,
                    code="600000.SH",
                    verdict="pass",
                    metrics_json="{}",
                    created_at=now,
                ),
                DiscoveryResult(
                    run_id=run_id,
                    track=gate.track,
                    stage=gate.stage,
                    attempt=gate.attempt,
                    code="600000.SH",
                    verdict="pass",
                    metrics_json="{}",
                    created_at=now,
                ),
                DiscoveryOverride(
                    run_id=run_id,
                    track=gate.track,
                    stage=gate.stage,
                    attempt=gate.attempt,
                    code="600000.SH",
                    action="forced_pass",
                    reason="verified",
                    created_at=now,
                ),
                DiscoveryDecision(
                    run_id=run_id,
                    track=competition.track,
                    competition_attempt=competition.attempt,
                    competition_stage_id=competition.id,
                    code="600000.SH",
                    decision="keep",
                    created_at=now,
                ),
                DiscoveryEvidence(
                    run_id=run_id,
                    track=competition.track,
                    competition_attempt=competition.attempt,
                    competition_stage_id=competition.id,
                    code="600000.SH",
                    ruler_version_id=1,
                    rule_version="rules-v1",
                    kind="evidence",
                    rule_id="growth",
                    input_fields_json='["revenue"]',
                    display_text="Revenue grew.",
                    as_of="2025-12-31",
                    data_completeness="complete",
                    created_at=now,
                ),
            ]
        )
        session.commit()

        session.delete(common)
        session.delete(gate)
        session.delete(competition)
        session.commit()

        for table in (
            "discovery_common_results",
            "discovery_results",
            "discovery_overrides",
            "discovery_decisions",
            "discovery_evidence",
        ):
            assert session.scalar(text(f"SELECT COUNT(*) FROM {table}")) == 0


def test_deleting_run_cascades_workflow_rows_and_preserves_research(
    tmp_path: Path,
) -> None:
    from app.discovery_models import DiscoveryCommonStage, DiscoveryInputSnapshot
    from app.models import ResearchJob

    engine = init_db(tmp_path / "cascade.db")
    SessionLocal = create_session_factory(engine)
    now = _now()
    with SessionLocal() as session:
        replay_source_id = _insert_run(
            session, week_key="2026-W36", current=False, input_fingerprint="source"
        )
        replay_id = _insert_run(
            session,
            week_key="2026-W37",
            current=False,
            input_fingerprint="replay",
            replay_of_run_id=replay_source_id,
        )
        session.add(
            DiscoveryInputSnapshot(
                run_id=replay_source_id,
                code="600000.SH",
                name="浦发银行",
                industry="银行",
                security_status="active",
                fields_json="{}",
                sources_json="{}",
                as_of_json="{}",
                data_version="d1",
                financial_version="f1",
                valuation_version="v1",
                algorithm_version="a1",
                input_fingerprint="company",
                snapshot_at=now,
            )
        )
        session.add(
            DiscoveryCommonStage(
                run_id=replay_source_id,
                attempt=1,
                status="queued",
                total_count=0,
                pass_count=0,
                drop_count=0,
                insufficient_count=0,
                input_fingerprint="source",
                algorithm_version="a1",
                created_at=now,
                updated_at=now,
            )
        )
        job = ResearchJob(
            user_id=1,
            discovery_run_id=replay_source_id,
            discovery_track="garp",
            name="浦发银行",
            code="600000.SH",
            status="queued",
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.commit()
        job_id = job.id

        session.execute(
            text("DELETE FROM discovery_runs WHERE id = :id"),
            {"id": replay_source_id},
        )
        session.commit()

        assert session.scalar(
            text(
                "SELECT COUNT(*) FROM discovery_input_snapshots "
                "WHERE run_id = :id"
            ),
            {"id": replay_source_id},
        ) == 0
        assert session.scalar(
            text(
                "SELECT COUNT(*) FROM discovery_common_stages "
                "WHERE run_id = :id"
            ),
            {"id": replay_source_id},
        ) == 0
        assert session.scalar(
            text(
                "SELECT replay_of_run_id FROM discovery_runs WHERE id = :id"
            ),
            {"id": replay_id},
        ) is None
        migrated_job = session.get(ResearchJob, job_id)
        assert migrated_job is not None
        assert migrated_job.discovery_run_id is None
        assert migrated_job.discovery_track == "garp"


def test_utc_columns_round_trip_as_aware_utc(tmp_path: Path) -> None:
    from app.discovery_models import DiscoveryRun

    engine = init_db(tmp_path / "utc.db")
    SessionLocal = create_session_factory(engine)
    local_time = datetime(2026, 9, 17, 16, 0, tzinfo=timezone.utc)
    with SessionLocal() as session:
        run_id = _insert_run(
            session,
            week_key="2026-W39",
            created_at=local_time,
            updated_at=local_time,
            snapshot_at=local_time,
        )
        session.commit()
        session.expire_all()

        run = session.get(DiscoveryRun, run_id)
        assert run is not None
        assert run.created_at == local_time
        assert run.created_at.tzinfo == timezone.utc
        assert run.snapshot_at == local_time


def test_research_discovery_provenance_enforces_track_pairing_and_ownership(
    tmp_path: Path,
) -> None:
    from app.models import User

    engine = init_db(tmp_path / "research-provenance.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        session.add(User(id=2, name="另一用户"))
        run_id = _insert_run(session, week_key="2026-W32")
        other_run_id = _insert_run(
            session,
            user_id=2,
            week_key="2026-W32",
            current=False,
            input_fingerprint="other-user",
        )
        session.commit()

    check_names = {
        constraint["name"]
        for constraint in inspect(engine).get_check_constraints("research_jobs")
    }
    assert {
        "ck_research_jobs_discovery_track",
        "ck_research_jobs_discovery_pair",
    } <= check_names

    insert_sql = text(
        "INSERT INTO research_jobs ("
        "id, user_id, discovery_run_id, discovery_track, name, code, status, "
        "created_at, updated_at"
        ") VALUES ("
        ":id, :user_id, :run_id, :track, '公司', '600000.SH', 'queued', "
        "'2026-09-17 08:00:00', '2026-09-17 08:00:00'"
        ")"
    )
    invalid_inserts = [
        {"id": 1, "user_id": 1, "run_id": None, "track": "momentum"},
        {"id": 2, "user_id": 1, "run_id": run_id, "track": None},
        {"id": 3, "user_id": 2, "run_id": run_id, "track": "garp"},
    ]
    for values in invalid_inserts:
        with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(insert_sql, values)

    with engine.begin() as conn:
        conn.execute(
            insert_sql,
            {"id": 4, "user_id": 1, "run_id": run_id, "track": "garp"},
        )

    invalid_updates = [
        (
            "UPDATE research_jobs SET discovery_track = 'momentum' WHERE id = 4",
            {},
        ),
        ("UPDATE research_jobs SET user_id = 2 WHERE id = 4", {}),
        (
            "UPDATE research_jobs SET discovery_run_id = :run_id WHERE id = 4",
            {"run_id": other_run_id},
        ),
        (
            "UPDATE discovery_runs SET user_id = 2 WHERE id = :run_id",
            {"run_id": run_id},
        ),
    ]
    for statement, values in invalid_updates:
        with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(text(statement), values)

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM discovery_runs WHERE id = :run_id"),
            {"run_id": run_id},
        )
        row = conn.execute(
            text(
                "SELECT user_id, discovery_run_id, discovery_track "
                "FROM research_jobs WHERE id = 4"
            )
        ).one()
    assert row == (1, None, "garp")


def test_existing_research_jobs_gain_nullable_discovery_provenance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    from sqlalchemy import create_engine

    legacy = create_engine(f"sqlite:///{db_path.as_posix()}")
    with legacy.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)"
            )
        )
        conn.execute(text("INSERT INTO users (id, name) VALUES (1, '本机用户')"))
        conn.execute(
            text(
                "CREATE TABLE research_jobs ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL REFERENCES users(id), "
                "screen_run_id INTEGER, name VARCHAR NOT NULL, "
                "code VARCHAR NOT NULL, status VARCHAR NOT NULL, error TEXT, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO research_jobs "
                "(id, user_id, name, code, status, created_at, updated_at) "
                "VALUES (1, 1, '旧任务', '600000.SH', 'done', "
                "'2026-09-16 08:00:00', '2026-09-16 08:00:00')"
            )
        )
    legacy.dispose()

    engine = init_db(db_path)
    schema = inspect(engine)
    assert {"discovery_run_id", "discovery_track"} <= {
        column["name"] for column in schema.get_columns("research_jobs")
    }
    with engine.connect() as conn:
        research_fks = conn.execute(
            text("PRAGMA foreign_key_list(research_jobs)")
        ).all()
        old_row = conn.execute(
            text(
                "SELECT discovery_run_id, discovery_track "
                "FROM research_jobs WHERE id = 1"
            )
        ).one()
    assert any(
        fk[2] == "discovery_runs"
        and fk[3] == "discovery_run_id"
        and fk[4] == "id"
        and fk[6] == "SET NULL"
        for fk in research_fks
    )
    assert old_row == (None, None)

    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run_id = _insert_run(session)
        session.commit()

    with engine.connect() as conn:
        trigger_names = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND "
                    "(name LIKE 'trg_research_jobs_discovery_%' OR "
                    "name = 'trg_discovery_runs_research_ownership_update')"
                )
            )
        }
    assert trigger_names == {
        "trg_research_jobs_discovery_provenance_insert",
        "trg_research_jobs_discovery_provenance_update",
        "trg_discovery_runs_research_ownership_update",
    }

    invalid_updates = [
        (
            "UPDATE research_jobs SET discovery_track = 'momentum' WHERE id = 1",
            {},
        ),
        (
            "UPDATE research_jobs SET discovery_run_id = :run_id WHERE id = 1",
            {"run_id": run_id},
        ),
    ]
    for statement, values in invalid_updates:
        with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(text(statement), values)

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE research_jobs SET "
                "discovery_run_id = :run_id, discovery_track = 'garp' "
                "WHERE id = 1"
            ),
            {"run_id": run_id},
        )

    init_db(db_path).dispose()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM discovery_runs WHERE id = :run_id"),
            {"run_id": run_id},
        )
        migrated_row = conn.execute(
            text(
                "SELECT user_id, discovery_run_id, discovery_track "
                "FROM research_jobs WHERE id = 1"
            )
        ).one()
    assert migrated_row == (1, None, "garp")
