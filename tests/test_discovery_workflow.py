from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import create_session_factory, init_db
from tests.discovery_helpers import (
    RULER_IDS,
    SNAPSHOT_AT,
    add_unfrozen_run,
    company,
    freeze_one,
    versions,
)


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
        "trading_dates_json",
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
        "ruler_versions_json",
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
    assert next(
        column
        for column in schema.get_columns("discovery_input_snapshots")
        if column["name"] == "ruler_versions_json"
    )["nullable"] is False

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
                "ruler_versions_json": (
                    '{"deep_value":1,"garp":1,"quality_discount":1}'
                ),
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
                ruler_versions_json=(
                    '{"deep_value":1,"garp":1,"quality_discount":1}'
                ),
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


def test_discovery_constants_are_exact() -> None:
    from discovery.constants import (
        COMMON_STAGE,
        RUN_TERMINAL_STATUSES,
        STAGE_STATUSES,
        TERMINAL_FAILURES,
        TRACKS,
        TRACK_STAGES,
        VERDICTS,
    )

    assert TRACKS == frozenset(
        {"quality_discount", "deep_value", "garp"}
    )
    assert COMMON_STAGE == "common"
    assert TRACK_STAGES == ("track_gate", "competition", "adjudication")
    assert STAGE_STATUSES == frozenset(
        {
            "queued",
            "running",
            "waiting_confirmation",
            "confirmed",
            "failed",
            "ruler_not_ready",
            "data_not_ready",
            "insufficient_candidates",
        }
    )
    assert RUN_TERMINAL_STATUSES == frozenset(
        {"failed", "completed", "partially_completed", "abandoned"}
    )
    assert VERDICTS == frozenset({"pass", "drop", "insufficient"})
    assert TERMINAL_FAILURES == frozenset(
        {
            "failed",
            "ruler_not_ready",
            "data_not_ready",
            "insufficient_candidates",
        }
    )


_UNSET = object()


def _stage_state(
    *,
    run_id: object = 1,
    track: object = _UNSET,
    stage: object = "common",
    status: object = "queued",
    attempt: object = 1,
    input_fingerprint: object = "input-1",
    ruler_version_id: object = 7,
    algorithm_version: object = "discovery-v1",
    superseded: object = False,
    failure_kind: object = None,
):
    from discovery.workflow import StageState

    resolved_track = (
        None if stage == "common" else "quality_discount"
    ) if track is _UNSET else track
    return StageState(
        run_id=run_id,  # type: ignore[arg-type]
        track=resolved_track,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        attempt=attempt,  # type: ignore[arg-type]
        input_fingerprint=input_fingerprint,  # type: ignore[arg-type]
        ruler_version_id=ruler_version_id,  # type: ignore[arg-type]
        algorithm_version=algorithm_version,  # type: ignore[arg-type]
        superseded=superseded,  # type: ignore[arg-type]
        failure_kind=failure_kind,  # type: ignore[arg-type]
    )


def _predecessor_for(state, **changes):
    from dataclasses import replace

    previous_stage = {
        "track_gate": "common",
        "competition": "track_gate",
        "adjudication": "competition",
    }.get(state.stage)
    if previous_stage is None:
        return None
    predecessor = _stage_state(
        run_id=state.run_id,
        track=None if previous_stage == "common" else state.track,
        stage=previous_stage,
        status="confirmed",
        ruler_version_id=state.ruler_version_id,
    )
    return replace(predecessor, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": 0},
        {"run_id": True},
        {"run_id": "1"},
        {"stage": None},
        {"stage": "unknown"},
        {"stage": "common", "track": "quality_discount"},
        {"stage": "track_gate", "track": None},
        {"stage": "track_gate", "track": "momentum"},
        {"attempt": 0},
        {"attempt": True},
        {"attempt": 1.0},
        {"input_fingerprint": ""},
        {"input_fingerprint": 1},
        {"algorithm_version": ""},
        {"algorithm_version": 1},
        {"ruler_version_id": True},
        {"ruler_version_id": 1.0},
        {"superseded": 0},
        {"status": "insufficient_candidates"},
        {"status": "ruler_not_ready"},
        {"status": "failed", "failure_kind": None},
        {"status": "failed", "failure_kind": []},
        {"status": "queued", "failure_kind": "transient"},
    ],
)
def test_stage_state_rejects_malformed_runtime_values(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _stage_state(**changes)


def test_stage_state_is_fully_immutable() -> None:
    from dataclasses import FrozenInstanceError

    state = _stage_state()

    with pytest.raises(FrozenInstanceError):
        state.status = "running"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("queued", "running"),
        ("running", "waiting_confirmation"),
        ("waiting_confirmation", "confirmed"),
    ],
)
def test_stage_state_accepts_only_the_exact_normal_flow(
    current: str, target: str
) -> None:
    from discovery.workflow import transition_stage

    old = _stage_state(status=current)
    new = transition_stage(
        old,
        target,
        run_status="running",
        predecessor=None,
    )

    assert new.status == target
    assert old.status == current


@pytest.mark.parametrize(
    ("stage", "current", "target", "failure_kind"),
    [
        ("common", "running", "failed", "transient"),
        ("common", "running", "failed", "permanent"),
        ("common", "running", "data_not_ready", None),
        ("track_gate", "running", "ruler_not_ready", None),
        (
            "adjudication",
            "waiting_confirmation",
            "insufficient_candidates",
            None,
        ),
    ],
)
def test_stage_state_accepts_designed_terminal_transitions(
    stage: str,
    current: str,
    target: str,
    failure_kind: str | None,
) -> None:
    from discovery.workflow import transition_stage

    state = _stage_state(stage=stage, status=current)
    result = transition_stage(
        state,
        target,
        run_status="running",
        predecessor=_predecessor_for(state),
        failure_kind=failure_kind,
    )

    assert result.status == target
    assert result.failure_kind == failure_kind


@pytest.mark.parametrize(
    ("stage", "current", "target", "failure_kind"),
    [
        ("common", "queued", "waiting_confirmation", None),
        ("common", "queued", "confirmed", None),
        ("common", "running", "confirmed", None),
        ("common", "running", "queued", None),
        ("common", "waiting_confirmation", "running", None),
        ("common", "confirmed", "waiting_confirmation", None),
        ("common", "failed", "running", None),
        ("common", "queued", "failed", "transient"),
        ("common", "running", "ruler_not_ready", None),
        ("common", "running", "insufficient_candidates", None),
        (
            "track_gate",
            "waiting_confirmation",
            "insufficient_candidates",
            None,
        ),
    ],
)
def test_stage_state_rejects_invalid_transitions(
    stage: str,
    current: str,
    target: str,
    failure_kind: str | None,
) -> None:
    from discovery.workflow import WorkflowError, transition_stage

    state = _stage_state(
        stage=stage,
        status=current,
        failure_kind="transient" if current == "failed" else None,
    )

    with pytest.raises(WorkflowError):
        transition_stage(
            state,
            target,
            run_status="running",
            predecessor=_predecessor_for(state),
            failure_kind=failure_kind,
        )


@pytest.mark.parametrize(
    ("stage", "expected_previous"),
    [
        ("track_gate", "common"),
        ("competition", "track_gate"),
        ("adjudication", "competition"),
    ],
)
def test_track_stage_accepts_exact_predecessor_chain(
    stage: str, expected_previous: str
) -> None:
    from discovery.workflow import transition_stage

    state = _stage_state(stage=stage)
    predecessor = _predecessor_for(state)

    assert predecessor.stage == expected_previous
    assert (
        transition_stage(
            state,
            "running",
            run_status="running",
            predecessor=predecessor,
        ).status
        == "running"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": 2},
        {"track": "deep_value"},
        {"stage": "adjudication"},
        {"status": "waiting_confirmation"},
        {"superseded": True},
    ],
)
def test_track_stage_rejects_wrong_or_stale_predecessor(
    changes: dict[str, object],
) -> None:
    from discovery.workflow import WorkflowError, transition_stage

    state = _stage_state(stage="competition")
    predecessor = _predecessor_for(state, **changes)

    with pytest.raises(WorkflowError) as error:
        transition_stage(
            state,
            "running",
            run_status="running",
            predecessor=predecessor,
        )
    assert error.value.code == "invalid_predecessor"


def test_common_rejects_predecessor_and_track_stage_rejects_omission() -> None:
    from discovery.workflow import WorkflowError, transition_stage

    common = _stage_state()
    gate = _stage_state(stage="track_gate")
    for state, predecessor in (
        (common, _stage_state(status="confirmed")),
        (gate, None),
    ):
        with pytest.raises(WorkflowError) as error:
            transition_stage(
                state,
                "running",
                run_status="running",
                predecessor=predecessor,
            )
        assert error.value.code == "invalid_predecessor"


def test_transition_requires_explicit_run_and_predecessor_context() -> None:
    from discovery.workflow import transition_stage

    state = _stage_state()

    with pytest.raises(TypeError):
        transition_stage(state, "running", predecessor=None)
    with pytest.raises(TypeError):
        transition_stage(state, "running", run_status="running")


@pytest.mark.parametrize(
    ("run_status", "code"),
    [
        ("abandoned", "run_abandoned"),
        ("failed", "run_terminal"),
        ("completed", "run_terminal"),
        ("partially_completed", "run_terminal"),
        ("unknown", "invalid_run_status"),
        ([], "invalid_run_status"),
    ],
)
def test_transition_rejects_terminal_or_invalid_run_status(
    run_status: object, code: str
) -> None:
    from discovery.workflow import WorkflowError, transition_stage

    with pytest.raises(WorkflowError) as error:
        transition_stage(
            _stage_state(),
            "running",
            run_status=run_status,  # type: ignore[arg-type]
            predecessor=None,
        )
    assert error.value.code == code


@pytest.mark.parametrize(
    "run_status",
    ["running", "completed", "partially_completed", "failed"],
)
def test_confirmed_duplicate_is_idempotent_after_terminal_aggregation(
    run_status: str,
) -> None:
    from discovery.workflow import WorkflowError, transition_stage

    state = _stage_state(stage="competition", status="confirmed")
    predecessor = _predecessor_for(state)
    assert (
        transition_stage(
            state,
            "confirmed",
            run_status=run_status,
            predecessor=predecessor,
        )
        is state
    )

    for invalid in (None, _predecessor_for(state, run_id=2)):
        with pytest.raises(WorkflowError) as error:
            transition_stage(
                state,
                "confirmed",
                run_status="running",
                predecessor=invalid,
            )
        assert error.value.code == "invalid_predecessor"


def test_confirmed_duplicate_still_validates_every_guard() -> None:
    from dataclasses import replace

    from discovery.workflow import WorkflowError, transition_stage

    state = _stage_state(stage="competition", status="confirmed")
    predecessor = _predecessor_for(state)
    cases = [
        (
            replace(state, superseded=True),
            "confirmed",
            "completed",
            predecessor,
            None,
            "stage_superseded",
        ),
        (
            state,
            "confirmed",
            "completed",
            _predecessor_for(state, superseded=True),
            None,
            "invalid_predecessor",
        ),
        (
            state,
            "confirmed",
            "completed",
            predecessor,
            "transient",
            "invalid_failure_kind",
        ),
        (
            state,
            [],
            "completed",
            predecessor,
            None,
            "invalid_status",
        ),
        (
            state,
            "confirmed",
            "unknown",
            predecessor,
            None,
            "invalid_run_status",
        ),
        (
            state,
            "confirmed",
            "abandoned",
            predecessor,
            None,
            "run_abandoned",
        ),
    ]
    for current, target, run_status, upstream, failure_kind, code in cases:
        with pytest.raises(WorkflowError) as error:
            transition_stage(
                current,
                target,  # type: ignore[arg-type]
                run_status=run_status,
                predecessor=upstream,
                failure_kind=failure_kind,
            )
        assert error.value.code == code


@pytest.mark.parametrize("operation", ["transition", "retry"])
def test_superseded_stage_rejects_every_mutation(operation: str) -> None:
    from discovery.workflow import WorkflowError, retry_stage, transition_stage

    state = _stage_state(
        status="failed" if operation == "retry" else "queued",
        superseded=True,
        failure_kind="transient" if operation == "retry" else None,
    )
    with pytest.raises(WorkflowError) as error:
        if operation == "transition":
            transition_stage(
                state,
                "running",
                run_status="running",
                predecessor=None,
            )
        else:
            retry_stage(
                state,
                run_status="running",
                predecessor=None,
                input_fingerprint="input-1",
                ruler_version_id=7,
            )
    assert error.value.code == "stage_superseded"


def test_retry_uses_natural_failure_and_preserves_old_attempt() -> None:
    from discovery.workflow import retry_stage, transition_stage

    running = _stage_state(
        stage="competition",
        status="running",
        attempt=2,
    )
    predecessor = _predecessor_for(running)
    old = transition_stage(
        running,
        "failed",
        run_status="running",
        predecessor=predecessor,
        failure_kind="transient",
    )
    new = retry_stage(
        old,
        run_status="running",
        predecessor=predecessor,
        input_fingerprint="input-1",
        ruler_version_id=7,
    )

    assert new == _stage_state(
        stage="competition",
        status="queued",
        attempt=3,
    )
    assert old.status == "failed"
    assert old.attempt == 2
    assert old.failure_kind == "transient"


@pytest.mark.parametrize(
    "available_builds",
    [
        "discovery-v1",
        b"discovery-v1",
        object(),
        1,
        frozenset({1}),
        ["discovery-v1", object()],
    ],
)
def test_retry_rejects_malformed_build_registry(
    available_builds: object,
) -> None:
    from discovery.workflow import WorkflowError, retry_stage

    state = _stage_state(status="failed", failure_kind="transient")
    with pytest.raises(WorkflowError) as error:
        retry_stage(
            state,
            run_status="running",
            predecessor=None,
            input_fingerprint="input-1",
            ruler_version_id=7,
            available_builds=available_builds,  # type: ignore[arg-type]
        )
    assert error.value.code == "algorithm_unavailable"


@pytest.mark.parametrize(
    "predecessor_changes",
    [
        None,
        {"run_id": 2},
        {"track": "deep_value"},
        {"stage": "adjudication"},
        {"status": "waiting_confirmation"},
        {"superseded": True},
    ],
)
def test_downstream_retry_rejects_missing_wrong_or_stale_predecessor(
    predecessor_changes: dict[str, object] | None,
) -> None:
    from discovery.workflow import WorkflowError, retry_stage

    state = _stage_state(
        stage="competition",
        status="failed",
        failure_kind="transient",
    )
    predecessor = (
        None
        if predecessor_changes is None
        else _predecessor_for(state, **predecessor_changes)
    )
    with pytest.raises(WorkflowError) as error:
        retry_stage(
            state,
            run_status="running",
            predecessor=predecessor,
            input_fingerprint="input-1",
            ruler_version_id=7,
        )
    assert error.value.code == "invalid_predecessor"


def test_common_retry_rejects_any_predecessor() -> None:
    from discovery.workflow import WorkflowError, retry_stage

    state = _stage_state(status="failed", failure_kind="transient")
    with pytest.raises(WorkflowError) as error:
        retry_stage(
            state,
            run_status="running",
            predecessor=_stage_state(status="confirmed"),
            input_fingerprint="input-1",
            ruler_version_id=7,
        )
    assert error.value.code == "invalid_predecessor"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"input_fingerprint": "input-2"}, "inputs_changed"),
        ({"ruler_version_id": 8}, "ruler_changed"),
        ({"algorithm_version": "retired-build"}, "algorithm_unavailable"),
        ({"failure_kind": "permanent"}, "retry_not_allowed"),
        ({"status": "data_not_ready", "failure_kind": None}, "retry_not_allowed"),
    ],
)
def test_retry_rejects_invalid_invariants(
    changes: dict[str, object], code: str
) -> None:
    from dataclasses import replace

    from discovery.workflow import WorkflowError, retry_stage

    old = _stage_state(status="failed", failure_kind="transient")
    candidate = replace(old, **changes)

    with pytest.raises(WorkflowError) as error:
        retry_stage(
            candidate,
            run_status="running",
            predecessor=None,
            input_fingerprint="input-1",
            ruler_version_id=7,
        )
    assert error.value.code == code
    assert candidate == replace(old, **changes)


@pytest.mark.parametrize(
    ("stage", "status"),
    [
        ("common", "data_not_ready"),
        ("track_gate", "ruler_not_ready"),
        ("adjudication", "insufficient_candidates"),
    ],
)
def test_dedicated_terminal_failures_reject_retry(
    stage: str, status: str
) -> None:
    from discovery.workflow import WorkflowError, retry_stage

    state = _stage_state(stage=stage, status=status)
    with pytest.raises(WorkflowError) as error:
        retry_stage(
            state,
            run_status="running",
            predecessor=_predecessor_for(state),
            input_fingerprint="input-1",
            ruler_version_id=7,
        )
    assert error.value.code == "retry_not_allowed"


@pytest.mark.parametrize(
    ("run_status", "code"),
    [
        ("abandoned", "run_abandoned"),
        ("failed", "run_terminal"),
        ("completed", "run_terminal"),
        ("partially_completed", "run_terminal"),
        ("unknown", "invalid_run_status"),
    ],
)
def test_retry_requires_explicit_mutable_run_status(
    run_status: str, code: str
) -> None:
    from discovery.workflow import WorkflowError, retry_stage

    state = _stage_state(status="failed", failure_kind="transient")
    with pytest.raises(WorkflowError) as error:
        retry_stage(
            state,
            run_status=run_status,
            predecessor=None,
            input_fingerprint="input-1",
            ruler_version_id=7,
        )
    assert error.value.code == code

    with pytest.raises(TypeError):
        retry_stage(
            state,
            predecessor=None,
            input_fingerprint="input-1",
            ruler_version_id=7,
        )


def _track_statuses(**overrides: str) -> dict[str, str]:
    statuses = {
        "quality_discount": "queued",
        "deep_value": "queued",
        "garp": "queued",
    }
    statuses.update(overrides)
    return statuses


@pytest.mark.parametrize(
    ("run_status", "common", "tracks", "expected"),
    [
        (
            "abandoned",
            "failed",
            _track_statuses(
                quality_discount="confirmed",
                deep_value="confirmed",
                garp="confirmed",
            ),
            "abandoned",
        ),
        (
            "running",
            "failed",
            _track_statuses(
                quality_discount="confirmed",
                deep_value="confirmed",
                garp="confirmed",
            ),
            "failed",
        ),
        (
            "running",
            "running",
            _track_statuses(
                quality_discount="confirmed",
                deep_value="confirmed",
                garp="confirmed",
            ),
            "completed",
        ),
        (
            "running",
            "running",
            _track_statuses(
                quality_discount="confirmed",
                deep_value="failed",
                garp="insufficient_candidates",
            ),
            "partially_completed",
        ),
        (
            "running",
            "data_not_ready",
            _track_statuses(
                quality_discount="failed",
                deep_value="data_not_ready",
                garp="ruler_not_ready",
            ),
            "failed",
        ),
        (
            "running",
            "confirmed",
            _track_statuses(
                quality_discount="confirmed",
                deep_value="running",
                garp="waiting_confirmation",
            ),
            "running",
        ),
        (
            "running",
            "confirmed",
            _track_statuses(garp="waiting_confirmation"),
            "waiting_confirmation",
        ),
        ("running", "queued", _track_statuses(), "queued"),
    ],
)
def test_aggregate_status_precedence(
    run_status: str,
    common: str,
    tracks: dict[str, str],
    expected: str,
) -> None:
    from discovery.aggregate import aggregate_run_status

    assert aggregate_run_status(run_status, common, tracks) == expected


@pytest.mark.parametrize(
    ("run_status", "common", "tracks"),
    [
        ("unknown", "queued", _track_statuses()),
        ([], "queued", _track_statuses()),
        ("running", {}, _track_statuses()),
        ("running", "queued", {}),
        (
            "running",
            "queued",
            {
                "quality_discount": "queued",
                "deep_value": "queued",
                "momentum": "queued",
            },
        ),
        ("running", "queued", _track_statuses(garp="unknown")),
        ("running", "queued", _track_statuses(garp=[])),
        ("running", "queued", []),
    ],
)
def test_aggregate_status_rejects_malformed_inputs(
    run_status: object, common: object, tracks: object
) -> None:
    from discovery.aggregate import aggregate_run_status

    with pytest.raises(ValueError):
        aggregate_run_status(
            run_status,  # type: ignore[arg-type]
            common,  # type: ignore[arg-type]
            tracks,  # type: ignore[arg-type]
        )


def test_recomputation_requires_complete_available_saved_build() -> None:
    from discovery.builds import (
        AVAILABLE_BUILDS,
        BUILD_VERSION,
        AlgorithmUnavailableError,
        validate_recomputation,
    )

    assert AVAILABLE_BUILDS == frozenset({BUILD_VERSION})
    validate_recomputation(
        BUILD_VERSION,
        snapshot_complete=True,
        ruler_payload_complete=True,
        persisted_input_fingerprint="input-1",
        requested_input_fingerprint="input-1",
        persisted_ruler_version_id=7,
        requested_ruler_version_id=7,
    )
    for values in (
        {
            "algorithm_version": "retired-build",
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": "yes",
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": True,
            "ruler_payload_complete": 1,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-2",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 8,
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "",
            "requested_input_fingerprint": "",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": None,
            "requested_ruler_version_id": None,
        },
        {
            "algorithm_version": [],
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": [],
            "requested_input_fingerprint": [],
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 1,
            "requested_ruler_version_id": True,
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": "",
            "requested_ruler_version_id": "",
        },
        {
            "algorithm_version": BUILD_VERSION,
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
            "available_builds": object(),
        },
    ):
        with pytest.raises(AlgorithmUnavailableError) as error:
            validate_recomputation(**values)
        assert error.value.code == "algorithm_unavailable"


def test_historical_read_does_not_consult_build_registry() -> None:
    from discovery.builds import read_historical

    persisted = {
        "algorithm_version": "retired-build",
        "results": [{"code": "600000.SH"}],
    }

    replay = read_historical(persisted)
    replay["results"][0]["code"] = "changed"

    assert replay is not persisted
    assert persisted["results"][0]["code"] == "600000.SH"


def test_historical_read_rejects_non_plain_objects() -> None:
    from discovery.builds import read_historical

    with pytest.raises(TypeError, match="plain JSON-like"):
        read_historical({"orm_row": object()})


def test_historical_read_rejects_cycles() -> None:
    from discovery.builds import read_historical

    persisted: dict[str, object] = {}
    persisted["self"] = persisted

    with pytest.raises(TypeError, match="cycles"):
        read_historical(persisted)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_historical_read_rejects_non_finite_numbers(value: float) -> None:
    from discovery.builds import read_historical

    with pytest.raises(TypeError, match="finite"):
        read_historical({"metric": value})


def test_snapshot_canonical_json_supports_financial_primitives() -> None:
    from discovery.snapshot import canonical_json

    value = {
        "amount": Decimal("10.2500"),
        "as_of": date(2025, 12, 31),
        "missing": None,
        "snapshot": datetime(
            2026, 9, 17, 11, 0, 0, 123000, tzinfo=timezone(timedelta(hours=8))
        ),
        "中文": "值",
    }

    assert canonical_json(value) == (
        '{"amount":"10.25","as_of":"2025-12-31","missing":null,'
        '"snapshot":"2026-09-17T03:00:00.123000Z","中文":"值"}'
    )


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 9, 17, 3, 0),
        float("nan"),
        float("inf"),
        Decimal("NaN"),
        object(),
        {1: "not-a-string-key"},
    ],
)
def test_snapshot_canonical_json_rejects_unsafe_values(value: object) -> None:
    from discovery.snapshot import canonical_json

    with pytest.raises((TypeError, ValueError)):
        canonical_json({"value": value})


def test_snapshot_canonical_json_rejects_cycles() -> None:
    from discovery.snapshot import canonical_json

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(TypeError, match="cycles"):
        canonical_json(cyclic)


def test_freeze_loads_only_copied_persisted_inputs(tmp_path: Path) -> None:
    from app.models import (
        DiscoveryDataFill,
        DiscoveryUniverseSecurity,
        Financial,
    )
    from discovery.snapshot import (
        CompanySnapshotInput,
        freeze_inputs,
        load_frozen_inputs,
    )

    engine = init_db(tmp_path / "snapshot-copy.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        fill = DiscoveryDataFill(
            status="completed",
            total=2,
            completed=2,
            failed=0,
            request_count=2,
            daily_budget=100,
            started_at=SNAPSHOT_AT,
            updated_at=SNAPSHOT_AT,
            snapshot_status="ready",
        )
        session.add(fill)
        session.flush()
        financials = [
            Financial(
                code="000001.SZ",
                period="2025",
                period_type="annual",
                net_income=Decimal("123.4500"),
                source_as_of="2025-12-31",
            ),
            Financial(
                code="600000.SH",
                period="2025",
                period_type="annual",
                net_income=None,
                source_as_of="2025-12-31",
            ),
        ]
        securities = [
            DiscoveryUniverseSecurity(
                fill_id=fill.id,
                code="000001.SZ",
                name="平安银行",
                industry="银行",
                exchange="SZ",
                listing_date="1991-04-03",
                is_st=False,
                is_delisting=False,
                trading_status="trading",
                is_suspended=False,
                last_trade_date="2026-09-17",
                provenance="eastmoney",
                source_as_of="2026-09-17",
                snapshot_at=SNAPSHOT_AT,
            ),
            DiscoveryUniverseSecurity(
                fill_id=fill.id,
                code="600000.SH",
                name="近期上市",
                industry="制造业",
                exchange="SH",
                listing_date="2026-08-01",
                is_st=False,
                is_delisting=False,
                trading_status="trading",
                is_suspended=False,
                last_trade_date="2026-09-17",
                provenance="eastmoney",
                source_as_of="2026-09-17",
                snapshot_at=SNAPSHOT_AT,
            ),
        ]
        session.add_all([*financials, *securities])
        run = add_unfrozen_run(session, week_key="2026-W40")
        inputs = [
            CompanySnapshotInput(
                code=security.code,
                name=security.name,
                industry=security.industry,
                security_status={
                    "exchange": security.exchange,
                    "trading_status": security.trading_status,
                    "is_suspended": security.is_suspended,
                },
                fields={
                    "net_income": financial.net_income,
                    "listing_date": security.listing_date,
                    "recent_listing": security.code == "600000.SH",
                },
                sources={
                    "net_income": "annual-report",
                    "listing_date": security.provenance,
                },
                as_of={
                    "net_income": financial.source_as_of,
                    "listing_date": security.source_as_of,
                },
            )
            for security, financial in zip(securities, financials, strict=True)
        ]
        freeze_inputs(
            session,
            run,
            inputs,
            versions(),
            {"SH": ["2026-09-16", "2026-09-17"], "SZ": ["2026-09-17"]},
            snapshot_at=SNAPSHOT_AT,
        )
        run_id = run.id
        session.commit()

        financials[0].net_income = Decimal("999")
        securities[0].name = "被修改"
        securities[1].listing_date = None
        session.commit()

        loaded = load_frozen_inputs(session, run_id)
        assert [item.code for item in loaded.companies] == [
            "000001.SZ",
            "600000.SH",
        ]
        first, second = loaded.companies
        assert first.name == "平安银行"
        assert first.fields["net_income"] == "123.45"
        assert second.fields["net_income"] is None
        assert second.fields["recent_listing"] is True
        assert second.fields["listing_date"] == "2026-08-01"
        assert loaded.trading_dates == {
            "SH": ("2026-09-16", "2026-09-17"),
            "SZ": ("2026-09-17",),
        }
        assert loaded.ruler_version_ids == RULER_IDS


def test_snapshot_fingerprint_is_order_independent_and_semantic(
    tmp_path: Path,
) -> None:
    from discovery.snapshot import freeze_inputs

    engine = init_db(tmp_path / "snapshot-fingerprint.db")
    SessionLocal = create_session_factory(engine)
    base = [
        company("600000.SH"),
        company("000001.SZ", amount=None, recent_listing=True),
    ]

    def frozen_fingerprint(
        week: int,
        companies,
        *,
        supplied_versions=None,
        trading_dates=None,
    ) -> str:
        actual_versions = supplied_versions or versions()
        with SessionLocal() as session:
            run = add_unfrozen_run(
                session,
                week_key=f"2026-W{week}",
                data_version=actual_versions.data_version,
                financial_version=actual_versions.financial_version,
                valuation_version=actual_versions.valuation_version,
                ruler_ids=dict(actual_versions.ruler_version_ids),
            )
            freeze_inputs(
                session,
                run,
                companies,
                actual_versions,
                trading_dates or {"SH": ["2026-09-17"], "SZ": ["2026-09-17"]},
                snapshot_at=SNAPSHOT_AT,
            )
            return run.input_fingerprint

    expected = frozen_fingerprint(41, base)
    assert frozen_fingerprint(42, reversed(base)) == expected

    changed = [
        replace(base[0], sources={**base[0].sources, "amount": "restated"}),
        replace(base[0], as_of={**base[0].as_of, "amount": "2026-01-01"}),
        replace(
            base[0],
            security_status={**base[0].security_status, "is_st": True},
        ),
        replace(base[0], fields={key: value for key, value in base[0].fields.items() if key != "amount"}),
    ]
    for week, first in enumerate(changed, start=43):
        assert frozen_fingerprint(week, [first, base[1]]) != expected
    assert (
        frozen_fingerprint(
            47,
            base,
            supplied_versions=versions(data_version="data-v2"),
        )
        != expected
    )
    assert (
        frozen_fingerprint(
            48,
            base,
            trading_dates={"SH": ["2026-09-16"], "SZ": ["2026-09-17"]},
        )
        != expected
    )


def test_freeze_validates_everything_before_session_mutation(
    tmp_path: Path,
) -> None:
    from sqlalchemy import select

    from app.discovery_models import DiscoveryInputSnapshot
    from discovery.snapshot import freeze_inputs

    engine = init_db(tmp_path / "snapshot-atomic.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = add_unfrozen_run(session, week_key="2026-W49")
        bad = replace(company("000001.SZ"), fields={"bad": object()})

        with pytest.raises(TypeError):
            freeze_inputs(
                session,
                run,
                [company("600000.SH"), bad],
                versions(),
                {"SH": ["2026-09-17"], "SZ": ["2026-09-17"]},
                snapshot_at=SNAPSHOT_AT,
            )

        assert session.scalars(select(DiscoveryInputSnapshot)).all() == []
        assert not session.new
        assert run.input_fingerprint is None
        assert run.snapshot_at is None
        assert run.trading_dates_json is None


@pytest.mark.parametrize("inputs", [[], [company("600000.SH"), company("600000.SH")]])
def test_freeze_rejects_empty_or_duplicate_universe_before_writes(
    tmp_path: Path, inputs
) -> None:
    from discovery.snapshot import freeze_inputs

    engine = init_db(tmp_path / f"snapshot-invalid-{len(inputs)}.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = add_unfrozen_run(session, week_key="2026-W50")
        with pytest.raises(ValueError):
            freeze_inputs(
                session,
                run,
                inputs,
                versions(),
                {"SH": ["2026-09-17"]},
                snapshot_at=SNAPSHOT_AT,
            )
        assert run.input_fingerprint is None
        assert not session.new


def test_freeze_rejects_metadata_mismatch_and_refreeze(tmp_path: Path) -> None:
    from discovery.snapshot import freeze_inputs

    engine = init_db(tmp_path / "snapshot-immutable.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = add_unfrozen_run(session, week_key="2026-W51")
        with pytest.raises(ValueError, match="metadata"):
            freeze_inputs(
                session,
                run,
                [company("600000.SH")],
                versions(data_version="data-v2"),
                {"SH": ["2026-09-17"]},
                snapshot_at=SNAPSHOT_AT,
            )
        freeze_inputs(
            session,
            run,
            [company("600000.SH")],
            versions(),
            {"SH": ["2026-09-17"]},
            snapshot_at=SNAPSHOT_AT,
        )
        with pytest.raises(ValueError, match="already frozen"):
            freeze_inputs(
                session,
                run,
                [company("600000.SH")],
                versions(),
                {"SH": ["2026-09-17"]},
                snapshot_at=SNAPSHOT_AT,
            )


def test_historical_replay_loads_all_attempts_without_current_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryDecision,
        DiscoveryEvidence,
        DiscoveryResult,
    )
    from discovery.snapshot import load_historical_replay

    engine = init_db(tmp_path / "historical-replay.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = add_unfrozen_run(session, week_key="2026-W52")
        common_one = _add_common_stage(session, run.id, attempt=1)
        common_one.status = "failed"
        common_one.failure_kind = "transient"
        common_one.superseded_at = SNAPSHOT_AT
        common_two = _add_common_stage(session, run.id, attempt=2)
        common_two.status = "confirmed"
        gate_one = _add_track_stage(session, run.id, attempt=1)
        gate_one.status = "failed"
        gate_one.failure_kind = "transient"
        gate_one.superseded_at = SNAPSHOT_AT
        gate_two = _add_track_stage(session, run.id, attempt=2)
        gate_two.status = "confirmed"
        competition = _add_track_stage(
            session, run.id, stage="competition", attempt=1
        )
        competition.status = "confirmed"
        session.add_all(
            [
                DiscoveryCommonResult(
                    run_id=run.id,
                    attempt=1,
                    code="600000.SH",
                    verdict="drop",
                    reason="old",
                    metrics_json='{"score":1}',
                    created_at=SNAPSHOT_AT,
                ),
                DiscoveryCommonResult(
                    run_id=run.id,
                    attempt=2,
                    code="600000.SH",
                    verdict="pass",
                    metrics_json='{"score":2}',
                    created_at=SNAPSHOT_AT,
                ),
                DiscoveryResult(
                    run_id=run.id,
                    track="garp",
                    stage="track_gate",
                    attempt=1,
                    code="600000.SH",
                    verdict="drop",
                    metrics_json='{"score":1}',
                    rank=2,
                    created_at=SNAPSHOT_AT,
                ),
                DiscoveryResult(
                    run_id=run.id,
                    track="garp",
                    stage="track_gate",
                    attempt=2,
                    code="600000.SH",
                    verdict="pass",
                    metrics_json='{"score":2}',
                    rank=1,
                    created_at=SNAPSHOT_AT,
                ),
                DiscoveryDecision(
                    run_id=run.id,
                    track="garp",
                    competition_attempt=1,
                    competition_stage_id=competition.id,
                    code="600000.SH",
                    decision="keep",
                    keep_note="persisted",
                    created_at=SNAPSHOT_AT,
                ),
                DiscoveryEvidence(
                    run_id=run.id,
                    track="garp",
                    competition_attempt=1,
                    competition_stage_id=competition.id,
                    code="600000.SH",
                    ruler_version_id=1,
                    rule_version="retired-rules",
                    kind="risk",
                    rule_id="leverage",
                    input_fields_json='["debt"]',
                    display_text="Persisted risk.",
                    as_of="2025-12-31",
                    data_completeness="complete",
                    created_at=SNAPSHOT_AT,
                ),
            ]
        )
        session.commit()

        monkeypatch.setattr(
            "discovery.builds.validate_recomputation",
            lambda *args, **kwargs: pytest.fail("current engine invoked"),
        )
        replay = load_historical_replay(session, run.id)

    assert [row["attempt"] for row in replay.common_results] == [1, 2]
    assert [row["attempt"] for row in replay.track_results] == [1, 2]
    assert replay.common_results[0]["superseded"] is True
    assert replay.track_results[0]["superseded"] is True
    assert replay.decisions[0]["keep_note"] == "persisted"
    assert replay.evidence[0]["rule_version"] == "retired-rules"
    with pytest.raises(TypeError):
        replay.track_results[0]["verdict"] = "changed"


def test_existing_discovery_snapshot_schema_upgrades_idempotently(
    tmp_path: Path,
) -> None:
    from sqlalchemy import create_engine

    from app.discovery_models import DiscoveryInputSnapshot, DiscoveryRun
    from discovery.snapshot import load_frozen_inputs

    db_path = tmp_path / "prior-discovery-schema.db"
    legacy = create_engine(f"sqlite:///{db_path.as_posix()}")
    with legacy.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE ruler_versions ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "digest VARCHAR NOT NULL, reason VARCHAR NOT NULL, "
                "payload_json TEXT NOT NULL, activated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE discovery_runs ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "week_key VARCHAR NOT NULL, current BOOLEAN NOT NULL, "
                "status VARCHAR NOT NULL, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, snapshot_at DATETIME, "
                "data_version VARCHAR NOT NULL, "
                "financial_version VARCHAR NOT NULL, "
                "valuation_version VARCHAR NOT NULL, "
                "quality_discount_ruler_version_id INTEGER, "
                "deep_value_ruler_version_id INTEGER, "
                "garp_ruler_version_id INTEGER, "
                "algorithm_version VARCHAR NOT NULL, "
                "input_fingerprint VARCHAR, error TEXT, replay_of_run_id INTEGER)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE discovery_input_snapshots ("
                "id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, "
                "code VARCHAR NOT NULL, name VARCHAR NOT NULL, industry VARCHAR, "
                "security_status VARCHAR NOT NULL, fields_json TEXT NOT NULL, "
                "sources_json TEXT NOT NULL, as_of_json TEXT NOT NULL, "
                "data_version VARCHAR NOT NULL, "
                "financial_version VARCHAR NOT NULL, "
                "valuation_version VARCHAR NOT NULL, "
                "algorithm_version VARCHAR NOT NULL, "
                "input_fingerprint VARCHAR NOT NULL, "
                "snapshot_at DATETIME NOT NULL, "
                "UNIQUE (run_id, code), "
                "FOREIGN KEY(run_id) REFERENCES discovery_runs(id) "
                "ON DELETE CASCADE)"
            )
        )
        conn.execute(text("INSERT INTO users VALUES (1, '本机用户')"))
        conn.execute(
            text(
                "INSERT INTO ruler_versions VALUES "
                "(1, 1, 'old', 'legacy', '{}', '2026-09-17 03:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO discovery_runs VALUES ("
                "1, 1, '2026-W37', 0, 'completed', "
                "'2026-09-17 03:00:00', '2026-09-17 03:00:00', "
                "'2026-09-17 03:00:00', 'd1', 'f1', 'v1', 1, 1, 1, "
                "'discovery-v1', 'legacy-run-fingerprint', NULL, NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO discovery_input_snapshots VALUES ("
                "1, 1, '600000.SH', '旧快照', '制造业', '{}', '{}', '{}', '{}', "
                "'d1', 'f1', 'v1', 'discovery-v1', "
                "'legacy-row-fingerprint', '2026-09-17 03:00:00')"
            )
        )
    legacy.dispose()

    init_db(db_path).dispose()
    engine = init_db(db_path)
    schema = inspect(engine)
    assert "trading_dates_json" in {
        column["name"] for column in schema.get_columns("discovery_runs")
    }
    assert "ruler_versions_json" in {
        column["name"]
        for column in schema.get_columns("discovery_input_snapshots")
    }

    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = session.get(DiscoveryRun, 1)
        snapshot = session.get(DiscoveryInputSnapshot, 1)
        assert run is not None and run.trading_dates_json is None
        assert snapshot is not None and snapshot.ruler_versions_json is None
        with pytest.raises(ValueError, match="incomplete frozen inputs"):
            load_frozen_inputs(session, 1)


def test_freeze_rejects_wrong_session_and_detached_run_without_writes(
    tmp_path: Path,
) -> None:
    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryInputSnapshot, DiscoveryRun
    from discovery.snapshot import freeze_inputs

    engine = init_db(tmp_path / "snapshot-session-ownership.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as owner:
        run = add_unfrozen_run(owner, week_key="2026-W53")
        owner.commit()
        original = (run.input_fingerprint, run.trading_dates_json, run.snapshot_at)

        with SessionLocal() as wrong:
            with pytest.raises(ValueError, match="supplied session"):
                freeze_inputs(
                    wrong,
                    run,
                    [company("600000.SH")],
                    versions(),
                    {"SH": ["2026-09-17"]},
                    snapshot_at=SNAPSHOT_AT,
                )
            assert not wrong.new
            wrong.commit()

        owner.expunge(run)
        with pytest.raises(ValueError, match="supplied session"):
            freeze_inputs(
                owner,
                run,
                [company("600000.SH")],
                versions(),
                {"SH": ["2026-09-17"]},
                snapshot_at=SNAPSHOT_AT,
            )
        owner.commit()

    with SessionLocal() as check:
        assert check.scalar(
            select(func.count()).select_from(DiscoveryInputSnapshot)
        ) == 0
        persisted = check.get(DiscoveryRun, run.id)
        assert persisted is not None
        assert (
            persisted.input_fingerprint,
            persisted.trading_dates_json,
            persisted.snapshot_at,
        ) == original


def test_bad_later_company_cannot_commit_partial_snapshot(tmp_path: Path) -> None:
    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryInputSnapshot, DiscoveryRun
    from discovery.snapshot import freeze_inputs

    engine = init_db(tmp_path / "snapshot-late-validation.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = add_unfrozen_run(session, week_key="2026-W54")
        run_id = run.id
        original = (run.input_fingerprint, run.trading_dates_json, run.snapshot_at)
        malformed = replace(company("000001.SZ"), sources={"amount": object()})
        with pytest.raises(TypeError):
            freeze_inputs(
                session,
                run,
                [company("600000.SH"), malformed],
                versions(),
                {"SH": ["2026-09-17"], "SZ": ["2026-09-17"]},
                snapshot_at=SNAPSHOT_AT,
            )
        assert not session.new
        session.commit()

    with SessionLocal() as check:
        assert check.scalar(
            select(func.count()).select_from(DiscoveryInputSnapshot)
        ) == 0
        persisted = check.get(DiscoveryRun, run_id)
        assert persisted is not None
        assert (
            persisted.input_fingerprint,
            persisted.trading_dates_json,
            persisted.snapshot_at,
        ) == original


@pytest.mark.parametrize(
    "tamper",
    [
        "payload",
        "row_metadata",
        "row_fingerprint",
        "run_fingerprint",
        "calendar",
        "row_timestamp",
        "run_timestamp",
    ],
)
def test_load_frozen_inputs_rejects_persisted_tampering(
    tmp_path: Path, tamper: str
) -> None:
    from sqlalchemy import select

    from app.discovery_models import DiscoveryInputSnapshot
    from discovery.snapshot import load_frozen_inputs

    engine = init_db(tmp_path / f"snapshot-tamper-{tamper}.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = freeze_one(session, week_key=f"2026-{tamper}")
        row = session.scalar(
            select(DiscoveryInputSnapshot).where(
                DiscoveryInputSnapshot.run_id == run.id
            )
        )
        assert row is not None
        if tamper == "payload":
            row.fields_json = '{"amount":null,"recent_listing":false}'
        elif tamper == "row_metadata":
            row.financial_version = "tampered"
        elif tamper == "row_fingerprint":
            row.input_fingerprint = "0" * 64
        elif tamper == "run_fingerprint":
            run.input_fingerprint = "0" * 64
        elif tamper == "calendar":
            run.trading_dates_json = '{"SH":["2026-09-15"]}'
        elif tamper == "row_timestamp":
            row.snapshot_at = SNAPSHOT_AT + timedelta(seconds=1)
        else:
            run.snapshot_at = SNAPSHOT_AT + timedelta(seconds=1)
        session.commit()
        session.expire_all()

        with pytest.raises(ValueError, match="integrity"):
            load_frozen_inputs(session, run.id)


def test_load_frozen_inputs_preserves_missing_and_absent_fields(
    tmp_path: Path,
) -> None:
    from discovery.snapshot import load_frozen_inputs

    engine = init_db(tmp_path / "snapshot-missing-absent.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = freeze_one(session)
        session.commit()
        loaded = load_frozen_inputs(session, run.id)

    fields = loaded.companies[0].fields
    assert fields["amount"] is None
    assert "not_supplied" not in fields


def test_historical_replay_includes_ordered_zero_result_stages(
    tmp_path: Path,
) -> None:
    from discovery.snapshot import load_historical_replay

    engine = init_db(tmp_path / "replay-stage-history.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = add_unfrozen_run(session, week_key="2026-W55")
        _add_common_stage(session, run.id, attempt=2)
        first = _add_common_stage(session, run.id, attempt=1)
        first.status = "failed"
        first.failure_kind = "transient"
        first.superseded_at = SNAPSHOT_AT
        stages = [
            _add_track_stage(
                session,
                run.id,
                track="garp",
                stage="adjudication",
                attempt=2,
            ),
            _add_track_stage(
                session,
                run.id,
                track="deep_value",
                stage="competition",
                attempt=1,
            ),
            _add_track_stage(
                session,
                run.id,
                track="quality_discount",
                stage="adjudication",
                attempt=1,
            ),
            _add_track_stage(
                session,
                run.id,
                track="garp",
                stage="track_gate",
                attempt=1,
            ),
        ]
        stages[0].ruler_version_id = 1
        session.commit()

        replay = load_historical_replay(session, run.id)

    assert [row["attempt"] for row in replay.common_stages] == [1, 2]
    assert replay.common_stages[0]["failure_kind"] == "transient"
    assert replay.common_stages[0]["superseded"] is True
    assert [
        (row["track"], row["stage"], row["attempt"])
        for row in replay.track_stages
    ] == [
        ("quality_discount", "adjudication", 1),
        ("deep_value", "competition", 1),
        ("garp", "track_gate", 1),
        ("garp", "adjudication", 2),
    ]
    assert replay.track_stages[-1]["ruler_version_id"] == 1
    assert replay.common_results == ()
    assert replay.track_results == ()


def test_historical_replay_rejects_unknown_run(tmp_path: Path) -> None:
    from discovery.snapshot import load_historical_replay

    engine = init_db(tmp_path / "replay-unknown.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        with pytest.raises(ValueError, match="run not found"):
            load_historical_replay(session, 999_999)
