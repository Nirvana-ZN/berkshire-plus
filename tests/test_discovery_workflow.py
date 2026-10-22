from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db import create_session_factory, init_db
from discovery.builds import BUILD_VERSION
from tests.discovery_helpers import (
    RULER_IDS,
    SNAPSHOT_AT,
    add_unfrozen_run,
    company,
    discovery_ruler_values,
    freeze_one,
    versions,
)

def _passing_track_outcome(context):
    from jobs.discovery import TrackExecutionOutcome, TrackExecutionResult

    return TrackExecutionOutcome(
        status="waiting_confirmation",
        results=tuple(
            TrackExecutionResult(
                code=code,
                verdict="pass",
                reason=None,
                metrics_json="{}",
                rank=None,
            )
            for code in context.candidate_codes
        ),
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
        "ruler_payloads_json",
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
        "retry_of_stage_id",
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
        "common_stage_id",
        "predecessor_stage_id",
        "predecessor_stage",
        "retry_of_stage_id",
        "reopen_of_stage_id",
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
    from sqlalchemy import select

    from app.discovery_models import DiscoveryCommonStage, DiscoveryStage

    now = _now()
    if stage == "track_gate":
        parent = session.scalars(
            select(DiscoveryCommonStage)
            .where(
                DiscoveryCommonStage.run_id == run_id,
                DiscoveryCommonStage.superseded_at.is_(None),
            )
            .order_by(DiscoveryCommonStage.attempt.desc())
        ).first()
        if parent is None:
            parent = _add_common_stage(session, run_id)
        parent.status = "confirmed"
        common_stage_id = parent.id
        predecessor_stage_id = None
        predecessor_stage = None
    else:
        expected = "track_gate" if stage == "competition" else "competition"
        parent = session.scalars(
            select(DiscoveryStage)
            .where(
                DiscoveryStage.run_id == run_id,
                DiscoveryStage.track == track,
                DiscoveryStage.stage == expected,
                DiscoveryStage.superseded_at.is_(None),
            )
            .order_by(DiscoveryStage.attempt.desc())
        ).first()
        if parent is None:
            parent = _add_track_stage(
                session, run_id, track=track, stage=expected
            )
        parent.status = "confirmed"
        common_stage_id = None
        predecessor_stage_id = parent.id
        predecessor_stage = expected
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
        common_stage_id=common_stage_id,
        predecessor_stage_id=predecessor_stage_id,
        predecessor_stage=predecessor_stage,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def test_discovery_schema_has_all_tables_columns_foreign_keys_and_index(
    tmp_path: Path,
) -> None:
    from datetime import date

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
        fk["referred_table"] == "discovery_common_stages"
        and fk["constrained_columns"] == ["common_stage_id", "run_id"]
        and fk["referred_columns"] == ["id", "run_id"]
        for fk in schema.get_foreign_keys("discovery_stages")
    )
    assert any(
        fk["referred_table"] == "discovery_stages"
        and fk["constrained_columns"]
        == [
            "predecessor_stage_id",
            "run_id",
            "track",
            "predecessor_stage",
        ]
        and fk["referred_columns"]
        == ["id", "run_id", "track", "stage"]
        for fk in schema.get_foreign_keys("discovery_stages")
    )
    assert any(
        fk["referred_table"] == "discovery_common_stages"
        and fk["constrained_columns"] == ["retry_of_stage_id", "run_id"]
        and fk["referred_columns"] == ["id", "run_id"]
        for fk in schema.get_foreign_keys("discovery_common_stages")
    )
    for origin in ("retry_of_stage_id", "reopen_of_stage_id"):
        assert any(
            fk["referred_table"] == "discovery_stages"
            and fk["constrained_columns"]
            == [origin, "run_id", "track", "stage"]
            and fk["referred_columns"]
            == ["id", "run_id", "track", "stage"]
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


def test_job_lock_has_nullable_owner_token(tmp_path: Path) -> None:
    schema = inspect(init_db(tmp_path / "lock-token.db"))
    token = next(
        column
        for column in schema.get_columns("job_lock")
        if column["name"] == "owner_token"
    )
    assert token["nullable"] is True


def test_discovery_lease_requires_exact_owner_token(tmp_path: Path) -> None:
    from app.models import JobLock
    from jobs.lock import (
        JOB_TYPE_DISCOVERY,
        acquire_discovery,
        release,
    )

    engine = init_db(tmp_path / "lease-owner.db")
    factory = create_session_factory(engine)
    with factory() as session:
        token = acquire_discovery(session, 7)
        assert token
        session.commit()
    with factory() as session:
        assert not release(session, 7, JOB_TYPE_DISCOVERY)
        session.commit()
        lock = session.get(JobLock, 1)
        assert lock is not None and lock.owner_token == token
        assert acquire_discovery(session, 7) is None
        session.rollback()
    with factory() as session:
        assert release(
            session,
            7,
            JOB_TYPE_DISCOVERY,
            owner_token=token,
        )
        session.commit()


def test_shutdown_revokes_hung_discovery_worker_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event
    from time import monotonic

    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app import discovery_routes
    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
    )
    from app.main import create_app
    from jobs import discovery as discovery_job
    from jobs.lock import clear_stale_lock
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "bounded-shutdown.db"
    _seed_api_snapshot(db_path)
    entered = Event()
    resume = Event()
    finished = Event()
    original_classify = discovery_job.classify_common
    original_drain = discovery_routes._drain

    def blocked(item, dates):
        entered.set()
        assert resume.wait(5)
        return original_classify(item, dates)

    def observed_drain(*args):
        try:
            return original_drain(*args)
        finally:
            finished.set()

    monkeypatch.setattr(discovery_job, "classify_common", blocked)
    monkeypatch.setattr(discovery_routes, "_drain", observed_drain)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    client = TestClient(app)
    client.__enter__()
    response = client.post("/api/discoveries", json={})
    run_id = response.json()["id"]
    assert entered.wait(5)
    started = monotonic()
    client.__exit__(None, None, None)
    assert monotonic() - started < 1
    resume.set()
    assert finished.wait(5)

    engine = init_db(db_path)
    factory = create_session_factory(engine)
    with factory() as session:
        clear_stale_lock(session, now_provider=lambda: SNAPSHOT_AT)
        assert session.scalar(
            select(func.count()).select_from(DiscoveryCommonResult)
        ) == 0
        stage = session.scalar(
            select(DiscoveryCommonStage).where(
                DiscoveryCommonStage.run_id == run_id
            )
        )
        assert stage is not None
        assert (stage.status, stage.failure_kind) == (
            "failed",
            "transient",
        )
    engine.dispose()


def test_snapshot_build_runs_off_the_event_loop(tmp_path: Path) -> None:
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import create_app
    from market.quotes import FakeQuoteClient

    on_loop: list[bool] = []

    def snapshot(session, **kwargs):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        raise ValueError("snapshot abandoned by test")

    app = create_app(
        db_path=tmp_path / "snapshot-thread.db",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        discovery_snapshot_provider=snapshot,
    )
    with TestClient(app) as client:
        client.post("/api/discoveries", json={})
    assert on_loop == [False]


def test_shutdown_does_not_revoke_newer_discovery_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    from fastapi.testclient import TestClient

    from app import discovery_routes
    from app.main import create_app
    from app.models import JobLock
    from jobs import discovery as discovery_job
    from jobs.lock import (
        JOB_TYPE_DISCOVERY,
        acquire_discovery,
        release,
    )
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "shutdown-lease-aba.db"
    _seed_api_snapshot(db_path)
    entered = Event()
    resume = Event()
    finished = Event()
    original_classify = discovery_job.classify_common
    original_drain = discovery_routes._drain

    def blocked(item, dates):
        entered.set()
        assert resume.wait(5)
        return original_classify(item, dates)

    def observed_drain(*args):
        try:
            return original_drain(*args)
        finally:
            finished.set()

    monkeypatch.setattr(discovery_job, "classify_common", blocked)
    monkeypatch.setattr(discovery_routes, "_drain", observed_drain)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    client = TestClient(app)
    client.__enter__()
    response = client.post("/api/discoveries", json={})
    run_id = response.json()["id"]
    assert entered.wait(5)
    with app.state.discovery_workers_lock:
        worker = next(iter(app.state.discovery_workers))
        captured_run, old_token = app.state.discovery_worker_leases[worker]
    assert captured_run == run_id
    with app.state.session_factory() as session:
        assert release(
            session,
            run_id,
            JOB_TYPE_DISCOVERY,
            owner_token=old_token,
        )
        session.commit()
        new_token = acquire_discovery(session, run_id)
        assert new_token and new_token != old_token
        session.commit()

    client.__exit__(None, None, None)
    with app.state.session_factory() as session:
        lock = session.get(JobLock, 1)
        assert lock is not None and lock.owner_token == new_token
    resume.set()
    assert finished.wait(5)


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
        DiscoveryStage,
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
            attempt=2,
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
    from sqlalchemy import delete

    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
        DiscoveryDecision,
        DiscoveryEvidence,
        DiscoveryOverride,
        DiscoveryResult,
        DiscoveryStage,
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

        session.execute(
            delete(DiscoveryStage).where(DiscoveryStage.id == competition.id)
        )
        session.execute(
            delete(DiscoveryStage).where(DiscoveryStage.id == gate.id)
        )
        session.execute(
            delete(DiscoveryCommonStage).where(
                DiscoveryCommonStage.id == common.id
            )
        )
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
        TRACK_STAGES,
        TRACKS,
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
    algorithm_version: object = BUILD_VERSION,
    superseded: object = False,
    failure_kind: object = None,
    stage_id: object = _UNSET,
    common_stage_id: object = _UNSET,
    predecessor_stage_id: object = _UNSET,
    retry_of_stage_id: object = None,
    reopen_of_stage_id: object = None,
):
    from discovery.workflow import StageState

    resolved_track = (
        None if stage == "common" else "quality_discount"
    ) if track is _UNSET else track
    resolved_stage_id = 1 if stage_id is _UNSET else stage_id
    resolved_common_id = (
        101 if stage == "track_gate" else None
    ) if common_stage_id is _UNSET else common_stage_id
    resolved_predecessor_id = (
        102 if stage in {"competition", "adjudication"} else None
    ) if predecessor_stage_id is _UNSET else predecessor_stage_id
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
        stage_id=resolved_stage_id,  # type: ignore[arg-type]
        common_stage_id=resolved_common_id,  # type: ignore[arg-type]
        predecessor_stage_id=resolved_predecessor_id,  # type: ignore[arg-type]
        retry_of_stage_id=retry_of_stage_id,  # type: ignore[arg-type]
        reopen_of_stage_id=reopen_of_stage_id,  # type: ignore[arg-type]
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
        stage_id=(
            state.common_stage_id
            if previous_stage == "common"
            else state.predecessor_stage_id
        ),
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
        stage_id=None,
        retry_of_stage_id=1,
    )
    assert old.status == "failed"
    assert old.attempt == 2
    assert old.failure_kind == "transient"


def test_pure_reopen_rejects_adjudication_and_records_exact_origin() -> None:
    from discovery.workflow import WorkflowError, reopen_stage

    adjudication = _stage_state(
        stage="adjudication",
        status="confirmed",
        predecessor_stage_id=2,
    )
    with pytest.raises(WorkflowError) as error:
        reopen_stage(
            adjudication,
            run_status="running",
            predecessor=_predecessor_for(adjudication),
        )
    assert error.value.code == "invalid_reopen_stage"

    competition = _stage_state(
        stage="competition",
        status="confirmed",
        predecessor_stage_id=2,
    )
    reopened = reopen_stage(
        competition,
        run_status="running",
        predecessor=_predecessor_for(competition),
    )
    assert reopened.stage_id is None
    assert reopened.retry_of_stage_id is None
    assert reopened.reopen_of_stage_id == competition.stage_id


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

    assert BUILD_VERSION == "discovery-v7"
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
            "algorithm_version": "discovery-v1",
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": "discovery-v2",
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": "discovery-v3",
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": "discovery-v4",
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
        {
            "algorithm_version": "discovery-v5",
            "snapshot_complete": True,
            "ruler_payload_complete": True,
            "persisted_input_fingerprint": "input-1",
            "requested_input_fingerprint": "input-1",
            "persisted_ruler_version_id": 7,
            "requested_ruler_version_id": 7,
        },
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
    from discovery.snapshot import _sha256
    import hashlib

    nested = {"b": [value, True, 1.5], "a": False}
    assert _sha256(nested) == hashlib.sha256(
        canonical_json(nested).encode("utf-8")
    ).hexdigest()


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
            snapshot_status=None,
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
        session.flush()
        fill.snapshot_status = "ready"
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
        session.commit()
        securities[0].name = "被修改"
        securities[1].listing_date = None
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()

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


def test_upgraded_discovery_stage_lineage_triggers_are_idempotent(
    tmp_path: Path,
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError

    db_path = tmp_path / "legacy-lineage.db"
    legacy = create_engine(f"sqlite:///{db_path.as_posix()}")
    with legacy.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, "
                "name VARCHAR NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE ruler_versions (id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL, digest VARCHAR NOT NULL, "
                "reason VARCHAR NOT NULL, payload_json TEXT NOT NULL, "
                "activated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE discovery_runs (id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL, week_key VARCHAR NOT NULL, "
                "current BOOLEAN NOT NULL, status VARCHAR NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "snapshot_at DATETIME, data_version VARCHAR NOT NULL, "
                "financial_version VARCHAR NOT NULL, "
                "valuation_version VARCHAR NOT NULL, "
                "quality_discount_ruler_version_id INTEGER, "
                "deep_value_ruler_version_id INTEGER, "
                "garp_ruler_version_id INTEGER, "
                "algorithm_version VARCHAR NOT NULL, "
                "input_fingerprint VARCHAR, error TEXT, "
                "replay_of_run_id INTEGER)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE discovery_common_stages ("
                "id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, "
                "attempt INTEGER NOT NULL, status VARCHAR NOT NULL, "
                "total_count INTEGER NOT NULL, pass_count INTEGER NOT NULL, "
                "drop_count INTEGER NOT NULL, "
                "insufficient_count INTEGER NOT NULL, error TEXT, "
                "confirmed_at DATETIME, input_fingerprint VARCHAR NOT NULL, "
                "algorithm_version VARCHAR NOT NULL, failure_kind VARCHAR, "
                "superseded_at DATETIME, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, UNIQUE(run_id, attempt), "
                "UNIQUE(id, run_id))"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE discovery_stages ("
                "id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, "
                "track VARCHAR NOT NULL, stage VARCHAR NOT NULL, "
                "attempt INTEGER NOT NULL, status VARCHAR NOT NULL, "
                "total_count INTEGER NOT NULL, pass_count INTEGER NOT NULL, "
                "drop_count INTEGER NOT NULL, "
                "insufficient_count INTEGER NOT NULL, error TEXT, "
                "confirmed_at DATETIME, input_fingerprint VARCHAR NOT NULL, "
                "ruler_version_id INTEGER, algorithm_version VARCHAR NOT NULL, "
                "failure_kind VARCHAR, superseded_at DATETIME, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "UNIQUE(run_id, track, stage, attempt), "
                "UNIQUE(id, run_id, track, stage, attempt), "
                "UNIQUE(id, run_id, track, stage))"
            )
        )
        conn.execute(text("INSERT INTO users VALUES (1, '本机用户')"))
        conn.execute(
            text(
                "INSERT INTO discovery_runs VALUES ("
                "1, 1, '2026-W37', 1, 'running', "
                "'2026-09-17 03:00:00', '2026-09-17 03:00:00', NULL, "
                "'d', 'f', 'v', NULL, NULL, NULL, 'discovery-v1', "
                "'fingerprint', NULL, NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO discovery_common_stages VALUES ("
                "1, 1, 1, 'confirmed', 0, 0, 0, 0, NULL, "
                "'2026-09-17 03:00:00', 'fingerprint', 'discovery-v1', "
                "NULL, NULL, '2026-09-17 03:00:00', "
                "'2026-09-17 03:00:00')"
            )
        )
        for row in [
            (
                1,
                "track_gate",
                "confirmed",
                "2026-09-17 03:01:00",
            ),
            (
                2,
                "competition",
                "confirmed",
                "2026-09-17 03:02:00",
            ),
            (
                3,
                "adjudication",
                "queued",
                "2026-09-17 03:03:00",
            ),
        ]:
            stage_id, stage_name, status, created = row
            conn.execute(
                text(
                    "INSERT INTO discovery_stages VALUES ("
                    f"{stage_id}, 1, 'garp', '{stage_name}', 1, "
                    f"'{status}', 0, 0, 0, 0, NULL, "
                    + (
                        f"'{created}'"
                        if status == "confirmed"
                        else "NULL"
                    )
                    + ", 'fingerprint', NULL, 'discovery-v1', NULL, NULL, "
                    f"'{created}', '{created}')"
                )
            )
        conn.execute(
            text(
                "INSERT INTO discovery_runs VALUES ("
                "2, 1, '2026-W36', 0, 'running', "
                "'2026-09-17 02:00:00', '2026-09-17 02:00:00', NULL, "
                "'d', 'f', 'v', NULL, NULL, NULL, 'discovery-v1', "
                "'fingerprint-2', NULL, NULL)"
            )
        )
        for common_id, attempt in [(2, 1), (3, 2)]:
            conn.execute(
                text(
                    "INSERT INTO discovery_common_stages VALUES ("
                    f"{common_id}, 2, {attempt}, 'confirmed', "
                    "0, 0, 0, 0, NULL, '2026-09-17 02:00:00', "
                    "'fingerprint-2', 'discovery-v1', NULL, NULL, "
                    "'2026-09-17 02:00:00', '2026-09-17 02:00:00')"
                )
            )
        conn.execute(
            text(
                "INSERT INTO discovery_stages VALUES ("
                "4, 2, 'garp', 'track_gate', 1, 'queued', "
                "0, 0, 0, 0, NULL, NULL, 'fingerprint-2', NULL, "
                "'discovery-v1', NULL, NULL, "
                "'2026-09-17 02:01:00', '2026-09-17 02:01:00')"
            )
        )
    legacy.dispose()

    init_db(db_path).dispose()
    engine = init_db(db_path)
    with engine.connect() as conn:
        recovered = conn.execute(
            text(
                "SELECT id, common_stage_id, predecessor_stage_id, "
                "predecessor_stage FROM discovery_stages "
                "WHERE id IN (1, 2, 3, 4) ORDER BY id"
            )
        ).all()
    assert recovered == [
        (1, 1, None, None),
        (2, None, 1, "track_gate"),
        (3, None, 2, "competition"),
        (4, None, None, None),
    ]
    insert = (
        "INSERT INTO discovery_stages "
        "(run_id, track, stage, attempt, status, total_count, pass_count, "
        "drop_count, insufficient_count, input_fingerprint, "
        "algorithm_version, common_stage_id, predecessor_stage_id, "
        "predecessor_stage, created_at, updated_at) VALUES "
    )
    values = (
        "1, 'deep_value', 'track_gate', 1, 'queued', 0, 0, 0, 0, "
        "'fingerprint', 'discovery-v1', "
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                insert
                + f"({values}1, NULL, NULL, "
                "'2026-09-17 03:00:00', '2026-09-17 03:00:00')"
            )
        )
    with pytest.raises(IntegrityError, match="invalid discovery stage lineage"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    insert
                    + "(1, 'quality_discount', 'track_gate', 1, 'queued', "
                    "0, 0, 0, 0, 'fingerprint', 'discovery-v1', "
                    "NULL, NULL, NULL, "
                    "'2026-09-17 03:00:00', '2026-09-17 03:00:00')"
                )
            )
    with pytest.raises(IntegrityError, match="invalid discovery stage lineage"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE discovery_stages SET common_stage_id = NULL "
                    "WHERE id = 5"
                )
            )
    with pytest.raises(IntegrityError, match="invalid discovery stage origin"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO discovery_stages "
                    "(run_id, track, stage, attempt, status, total_count, "
                    "pass_count, drop_count, insufficient_count, "
                    "input_fingerprint, algorithm_version, common_stage_id, "
                    "retry_of_stage_id, reopen_of_stage_id, created_at, "
                    "updated_at) VALUES "
                    "(1, 'deep_value', 'track_gate', 2, 'queued', "
                    "0, 0, 0, 0, 'fingerprint', 'discovery-v1', 1, "
                    "5, 5, '2026-09-17 03:01:00', "
                    "'2026-09-17 03:01:00')"
                )
            )
    with pytest.raises(
        IntegrityError, match="invalid discovery common stage origin"
    ):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE discovery_common_stages "
                    "SET retry_of_stage_id = 1 WHERE id = 1"
                )
            )
    engine.dispose()


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
        ("quality_discount", "track_gate", 1),
        ("quality_discount", "competition", 1),
        ("quality_discount", "adjudication", 1),
        ("deep_value", "track_gate", 1),
        ("deep_value", "competition", 1),
        ("garp", "track_gate", 1),
        ("garp", "competition", 1),
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


def _api_payload(
    captured_at: datetime,
    *,
    financial_rows=None,
    valuation_points=None,
    code: str = '600000.SH',
) -> str:
    from discovery.company_payload import serialize_company_payload
    from market.types import (
        DiscoveryCompanyData,
        DiscoverySourceAsOf,
        FinancialRow,
        SecurityState,
        ValuationPoint,
    )

    rows = (
        (
            FinancialRow(
                code=code,
                period="2025",
                period_type="annual",
                net_income=Decimal("100"),
                source_as_of="2026-03-31",
            ),
        )
        if financial_rows is None
        else financial_rows
    )
    points = (
        (
            ValuationPoint(
                code=code,
                month="2026-09",
                trade_date="2026-09-17",
                pe=Decimal("8"),
                pb=Decimal("0.8"),
                price=Decimal("10"),
                source="eastmoney",
                ratio_method="PE_TTM/PB_MRQ",
            ),
        )
        if valuation_points is None
        else valuation_points
    )
    return serialize_company_payload(
        DiscoveryCompanyData(
            code=code,
            security_state=SecurityState(
                code=code,
                name="浦发银行",
                industry="银行",
                exchange="SH",
                listing_date="1999-11-10",
                is_st=False,
                is_delisting=False,
                trading_status="trading",
                last_trade_date="2026-09-17",
                industry_version="eastmoney-2026-09-17",
                industry_as_of="2026-09-17",
                source="eastmoney",
                market_status_code=1,
            ),
            financial_rows=tuple(rows),
            valuation_points=tuple(points),
            request_count=1,
            source_as_of=DiscoverySourceAsOf(
                security="2026-09-17",
                financial=(
                    max(
                        (
                            row.source_as_of
                            for row in rows
                            if row.source_as_of is not None
                        ),
                        default=None,
                    )
                ),
                valuation=(
                    max((row.trade_date for row in points), default=None)
                ),
            ),
        ),
        captured_at=captured_at,
    )


def _seed_api_snapshot(db_path: Path) -> None:
    from app.models import (
        DiscoveryDataCheckpoint,
        DiscoveryDataFill,
        DiscoveryFillTradingDate,
        DiscoveryUniverseSecurity,
        Financial,
        FinancialFetchCompleteness,
        FinancialFieldProvenance,
        RulerVersion,
        ValuationHistory,
        ValuationSeries,
    )
    from discovery.company_payload import company_payload_digest

    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        fill = DiscoveryDataFill(
            status="done",
            total=1,
            completed=1,
            failed=0,
            request_count=1,
            daily_budget=100,
            budget_day="2026-09-17",
            daily_requests=1,
            started_at=SNAPSHOT_AT,
            updated_at=SNAPSHOT_AT,
            snapshot_status=None,
        )
        session.add(fill)
        session.flush()
        payload_json = _api_payload(SNAPSHOT_AT)
        session.add(
            DiscoveryUniverseSecurity(
                fill_id=fill.id,
                code="600000.SH",
                name="浦发银行",
                industry="银行",
                exchange="SH",
                listing_date="1999-11-10",
                is_st=False,
                is_delisting=False,
                trading_status="trading",
                is_suspended=False,
                last_trade_date="2026-09-17",
                industry_version="eastmoney-2026-09-17",
                source_as_of="2026-09-17",
                provenance="eastmoney",
                market_status_code=1,
                snapshot_at=SNAPSHOT_AT,
            )
        )
        session.add(DiscoveryFillTradingDate(fill_id=fill.id, trade_date="2026-09-17"))
        session.add(
            DiscoveryDataCheckpoint(
                fill_id=fill.id,
                code="600000.SH",
                status="done",
                attempts=1,
                next_retry_at=None,
                valuation_points=1,
                error=None,
                payload_json=payload_json,
                payload_digest=company_payload_digest(payload_json),
                updated_at=SNAPSHOT_AT,
            )
        )
        financial = Financial(
            code="600000.SH",
            period="2025",
            period_type="annual",
            net_income=Decimal("100"),
            source_as_of="2026-03-31",
        )
        session.add(financial)
        session.add(
            FinancialFieldProvenance(
                code=financial.code,
                period=financial.period,
                period_type=financial.period_type,
                field="net_income",
                source="discovery",
            )
        )
        session.add(
            FinancialFetchCompleteness(
                code="600000.SH",
                mode="discovery",
                completed_at=SNAPSHOT_AT,
            )
        )
        session.add(ValuationSeries(code="600000.SH", ratio_method="PE_TTM/PB_MRQ"))
        session.flush()
        session.add(
            ValuationHistory(
                code="600000.SH",
                month="2026-09",
                trade_date="2026-09-17",
                pe=Decimal("8"),
                pb=Decimal("0.8"),
                price=Decimal("10"),
                source="eastmoney",
                ratio_method="PE_TTM/PB_MRQ",
                fetched_at=SNAPSHOT_AT,
            )
        )
        session.add(
            RulerVersion(
                user_id=1,
                digest="legacy-funnel",
                reason="旧漏斗尺子",
                payload_json='{"funnel":{"ROE_FLOOR":"0.1"}}',
                activated_at=SNAPSHOT_AT,
            )
        )
        session.flush()
        fill.snapshot_status = "ready"
        session.commit()
    engine.dispose()


def _add_second_api_fill(
    db_path: Path,
    *,
    valuation_points: int,
    payload_json: str | None = None,
) -> tuple[datetime, datetime]:
    from app.models import (
        DiscoveryDataCheckpoint,
        DiscoveryDataFill,
        DiscoveryFillTradingDate,
        DiscoveryUniverseSecurity,
    )
    from discovery.company_payload import company_payload_digest

    started = SNAPSHOT_AT + timedelta(hours=1)
    completed = SNAPSHOT_AT + timedelta(hours=2)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        fill = DiscoveryDataFill(
            status="done",
            total=1,
            completed=1,
            failed=0,
            request_count=1,
            daily_budget=100,
            budget_day="2026-09-17",
            daily_requests=1,
            started_at=started,
            run_started_at=started,
            updated_at=completed,
            snapshot_status=None,
        )
        session.add(fill)
        session.flush()
        checkpoint_payload = (
            _api_payload(
                completed,
                financial_rows=(),
                valuation_points=(),
            )
            if payload_json is None
            else payload_json
        )
        session.add_all(
            [
                DiscoveryUniverseSecurity(
                    fill_id=fill.id,
                    code="600000.SH",
                    name="浦发银行",
                    industry="银行",
                    exchange="SH",
                    listing_date="1999-11-10",
                    is_st=False,
                    is_delisting=False,
                    trading_status="trading",
                    is_suspended=False,
                    last_trade_date="2026-09-17",
                    industry_version="eastmoney-2026-09-17",
                    source_as_of="2026-09-17",
                    provenance="eastmoney",
                    market_status_code=1,
                    snapshot_at=started,
                ),
                DiscoveryFillTradingDate(fill_id=fill.id, trade_date="2026-09-17"),
                DiscoveryDataCheckpoint(
                    fill_id=fill.id,
                    code="600000.SH",
                    status="done",
                    attempts=1,
                    next_retry_at=None,
                    valuation_points=valuation_points,
                    error=None,
                    payload_json=checkpoint_payload,
                    payload_digest=company_payload_digest(
                        checkpoint_payload
                    ),
                    updated_at=completed,
                ),
            ]
        )
        session.flush()
        fill.snapshot_status = "ready"
        session.commit()
    engine.dispose()
    return started, completed


def test_discovery_api_create_read_and_missing_rulers(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "discovery-api.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        created = client.post("/api/discoveries", json={"sync": True})
        assert created.status_code == 200
        run = created.json()
        assert run["status"] == "failed"
        assert run["ruler_version_ids"] == {
            "quality_discount": None,
            "deep_value": None,
            "garp": None,
        }
        assert client.post("/api/discoveries", json={}).json()["id"] == run["id"]

        safety = client.get(
            f"/api/discoveries/{run['id']}/stages/safety"
        ).json()
        assert safety["current"]["status"] == "confirmed"
        assert [row["code"] for row in safety["pass"]] == ["600000.SH"]

        for track in ("quality_discount", "deep_value", "garp"):
            stage = client.get(
                f"/api/discoveries/{run['id']}/tracks/{track}"
                "/stages/track_gate"
            ).json()
            assert stage["current"]["status"] == "ruler_not_ready"

        from app.models import Financial, ValuationHistory
        from discovery.snapshot import load_frozen_inputs

        with app.state.session_factory() as session:
            session.get(
                Financial, ("600000.SH", "2025", "annual")
            ).net_income = Decimal("999")
            session.get(
                ValuationHistory, ("600000.SH", "2026-09")
            ).price = Decimal("99")
            session.commit()
            frozen = load_frozen_inputs(session, run["id"])
        company = frozen.companies[0]
        assert company.fields["price"] == "10"
        assert (
            company.fields["financial_history"][0]["values"]["net_income"]
            == "100"
        )
        assert company.fields["financial_history"][0]["provenance"] == {
            "net_income": ("discovery",)
        }
        assert company.fields["valuation_history"][0]["source"] == "eastmoney"
        assert frozen.data_version.startswith("discovery-fill:1:")
        assert frozen.financial_version.startswith("financial:fill:1:")
        assert frozen.valuation_version.startswith("valuation:fill:1:")


def test_discovery_api_rejects_bad_body_and_supports_abandon(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "discovery-abandon.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/discoveries",
            content="[1]",
            headers={"content-type": "application/json"},
        ).status_code == 400
        created = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()
        abandoned = client.post(
            f"/api/discoveries/{created['id']}/abandon", json={}
        )
        assert abandoned.status_code == 200
        body = abandoned.json()
        assert body["status"] == "abandoned"
        assert {
            track: (row["stage"], row["status"], row["created"])
            for track, row in body["tracks"].items()
        } == {
            track: ("track_gate", "ruler_not_ready", True)
            for track in ("quality_discount", "deep_value", "garp")
        }
        assert client.get("/api/discoveries/current").status_code == 404


def test_three_tracks_advance_independently_with_injected_executor(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryStage
    from app.main import create_app
    from app.models import RulerVersion
    from discovery.evidence import build_evidence_card
    from discovery.snapshot import canonical_json
    from jobs.discovery import (
        TrackExecutionEvidence,
        TrackExecutionOutcome,
        TrackExecutionResult,
    )
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "discovery-tracks.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        for track in ("quality_discount", "deep_value", "garp"):
            payload_json, digest = discovery_ruler_values(track)
            session.add(
                RulerVersion(
                    user_id=1,
                    scope=f"discovery:{track}",
                    digest=digest,
                    reason="测试尺子",
                    payload_json=payload_json,
                    activated_at=SNAPSHOT_AT,
                )
            )
        session.commit()
    engine.dispose()

    executed: list[tuple[str, str]] = []

    def canonical_source_dates(company):
        result = {}
        for source, value in company.as_of.items():
            if type(value) is not str:
                continue
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                continue
            if parsed.isoformat() == value:
                result[source] = value
        return result

    def execute(context):
        executed.append((context.track, context.stage))
        companies = {
            company.code: company for company in context.frozen_inputs.companies
        }
        source_dates = {
            code: canonical_source_dates(company)
            for code, company in companies.items()
        }
        metrics_by_code = {}
        evidence_by_code = {}
        for code in context.candidate_codes:
            metrics = {
                field: "1" for field in context.ruler_payload["formulas"]
            }
            metrics.update(
                {
                    "code": code,
                    "branch": "bank",
                    "quality_branch": "bank",
                }
            )
            for count in (
                "valuation_pe_observations",
                "valuation_pb_observations",
                "roe_observations",
                "cash_conversion_observations",
                "bank_roe_observations",
                "bank_capital_available_count",
                "bank_npl_available_count",
                "normalized_earnings_observations",
                "fcf_observations",
                "net_debt_observations",
                "interest_coverage_observations",
                "bank_npl_observations",
                "dilution_observations",
                "revenue_observations",
                "one_time_profit_observations",
                "impairment_observations",
                "non_core_income_proxy_observations",
                "revenue_growth_observations",
                "deducted_profit_growth_observations",
                "roic_observations",
                "roe_fallback_observations",
            ):
                metrics[count] = "999"
            if context.track == "deep_value":
                policy = context.ruler_payload["sector_branches"][
                    "branch_policies"
                ]["bank"]
                metrics["branch"] = (
                    "bank"
                    if policy in {"unsupported", "insufficient"}
                    else policy
                )
            metrics_by_code[code] = metrics
            if context.stage == "competition":
                card = build_evidence_card(
                    code,
                    metrics,
                    context.ruler_payload,
                    source_as_of=source_dates[code],
                )
                assert card.verdict == "pass"
                evidence_by_code[code] = card.items
        return TrackExecutionOutcome(
            status="waiting_confirmation",
            results=tuple(
                TrackExecutionResult(
                    code=code,
                    verdict="pass",
                    reason=None,
                    metrics_json=canonical_json(metrics_by_code[code]),
                    rank=index,
                )
                for index, code in enumerate(
                    context.candidate_codes, start=1
                )
            ),
            evidence=(
                tuple(
                    TrackExecutionEvidence(
                        code=code,
                        ruler_version_id=context.ruler_version_id,
                        rule_version=item.rule_version,
                        kind=item.kind,
                        rule_id=item.rule_id,
                        input_fields_json=canonical_json(item.input_fields),
                        display_text=item.display_text,
                        as_of=item.as_of,
                        data_completeness=item.data_completeness,
                    )
                    for code in context.candidate_codes
                    for item in evidence_by_code[code]
                )
                if context.stage == "competition"
                else ()
            ),
        )

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
        discovery_track_executor=execute,
    )
    with TestClient(app) as client:
        run = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()
        run_id = run["id"]
        assert run["status"] == "completed"
        assert executed[:3] == [
            ("quality_discount", "track_gate"),
            ("deep_value", "track_gate"),
            ("garp", "track_gate"),
        ]
        assert sorted(executed[3:]) == sorted(
            (track, "competition")
            for track in ("quality_discount", "deep_value", "garp")
        )
        for track in ("quality_discount", "deep_value", "garp"):
            gate = client.get(
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/track_gate"
            ).json()
            assert gate["current"]["status"] == "confirmed"
            assert gate["pass"][0]["metrics"]["code"] == "600000.SH"
            competition_body = client.get(
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/competition"
            ).json()
            assert (
                competition_body["current"]["status"] == "confirmed"
            ), competition_body["current"]["error"]

    with SessionLocal() as session:
        stages = session.scalars(
            select(DiscoveryStage).order_by(DiscoveryStage.id)
        ).all()
        assert len(stages) == 6
        assert all(stage.attempt == 1 for stage in stages)


def test_startup_resumes_confirmed_upstream_queued_stage_once(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryRun, DiscoveryStage
    from app.main import create_app
    from app.models import RulerVersion
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "discovery-resume.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        payload_json, digest = discovery_ruler_values("garp")
        session.add(
            RulerVersion(
                user_id=1,
                scope="discovery:garp",
                digest=digest,
                reason="测试尺子",
                payload_json=payload_json,
                activated_at=SNAPSHOT_AT,
            )
        )
        session.commit()
    engine.dispose()

    base = dict(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    def interrupted(context):
        raise RuntimeError("garp gate interrupted")

    with TestClient(
        create_app(**base, discovery_track_executor=interrupted)
    ) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]

    with SessionLocal() as session:
        run = session.get(DiscoveryRun, run_id)
        gate = session.scalar(
            select(DiscoveryStage).where(
                DiscoveryStage.run_id == run_id,
                DiscoveryStage.track == "garp",
            )
        )
        assert run is not None and gate is not None
        assert (gate.stage, gate.status) == ("track_gate", "failed")
        gate.status = "queued"
        gate.error = None
        gate.failure_kind = None
        run.status = "running"
        session.commit()

    executed: list[str] = []

    def execute(context):
        executed.append(context.stage)
        return _passing_track_outcome(context)

    with TestClient(
        create_app(**base, discovery_track_executor=execute)
    ) as client:
        assert client.get(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate"
        ).json()["current"]["status"] == "confirmed"
    assert executed == ["track_gate", "competition"]


def test_concurrent_create_is_idempotent(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.discovery_models import (
        DiscoveryCommonStage,
        DiscoveryRun,
        DiscoveryStage,
    )
    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "discovery-concurrent.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        creates = list(
            pool.map(
                lambda _: client.post(
                    "/api/discoveries", json={"sync": True}
                ),
                range(2),
            )
        )
        assert [response.status_code for response in creates] == [200, 200]
        run_ids = {response.json()["id"] for response in creates}
        assert len(run_ids) == 1

    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(DiscoveryRun)
        ) == 1
        common = session.scalars(select(DiscoveryCommonStage)).all()
        assert len(common) == 1 and common[0].status == "confirmed"
        stages = session.scalars(select(DiscoveryStage)).all()
        assert len(stages) == 3
        assert {stage.track for stage in stages} == {
            "quality_discount",
            "deep_value",
            "garp",
        }
    engine.dispose()


def test_current_discovery_uses_china_week_and_allows_rollover(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryRun
    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "discovery-rollover.db"
    _seed_api_snapshot(db_path)
    clock = [SNAPSHOT_AT]
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: clock[0],
    )
    with TestClient(app) as client:
        first = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()
        assert first["week_key"] == "2026-W38"
        assert client.get("/api/discoveries/current").json()["id"] == first["id"]

        clock[0] += timedelta(days=7)
        assert client.get("/api/discoveries/current").status_code == 404
        second = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()
        assert second["week_key"] == "2026-W39"
        assert second["id"] != first["id"]
        current = client.get("/api/discoveries/current").json()
        assert current["id"] == second["id"]
        assert current["status"] == "failed"

    with app.state.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(DiscoveryRun)
        ) == 2


def test_newer_nonready_fill_blocks_older_ready_snapshot(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryRun
    from app.main import create_app
    from app.models import DiscoveryDataFill
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "newer-paused-fill.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    newer_row_time = SNAPSHOT_AT - timedelta(hours=1)
    with SessionLocal() as session:
        session.add(
            DiscoveryDataFill(
                status="paused",
                total=1,
                completed=0,
                failed=0,
                request_count=0,
                daily_budget=100,
                budget_day="2026-09-17",
                daily_requests=0,
                started_at=newer_row_time,
                updated_at=newer_row_time,
                snapshot_status="ready",
            )
        )
        session.commit()
    engine.dispose()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        response = client.post("/api/discoveries", json={"sync": True})
        assert response.status_code == 409
        assert response.json()["code"] == "DATA_NOT_READY"
    with app.state.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(DiscoveryRun)
        ) == 0


def test_failed_checkpoint_cannot_leak_stale_global_data(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import DiscoveryDataCheckpoint
    from discovery.snapshot import load_frozen_inputs
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "failed-checkpoint.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_discovery_checkpoint_payload_immutable"
            )
        )
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_discovery_checkpoint_status_immutable"
            )
        )
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (1, "600000.SH")
        )
        assert checkpoint is not None
        checkpoint.status = "failed"
        checkpoint.error = "upstream failed"
        checkpoint.payload_json = None
        checkpoint.payload_digest = None
        session.commit()
    engine.dispose()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        run = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()
        assert client.get(
            f"/api/discoveries/{run['id']}/stages/safety"
        ).json()["insufficient"][0]["reason"] == "price_missing"
    with app.state.session_factory() as session:
        frozen = load_frozen_inputs(session, run["id"]).companies[0]
        assert frozen.fields["price"] is None
        assert frozen.fields["financial_history"] == ()
        assert frozen.fields["valuation_history"] == ()


def test_snapshot_ignores_future_and_unproven_global_data(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from app.main import create_app
    from app.models import (
        Financial,
        FinancialFieldProvenance,
        ValuationHistory,
    )
    from discovery.snapshot import load_frozen_inputs
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "snapshot-contamination.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        session.execute(
            delete(FinancialFieldProvenance).where(
                FinancialFieldProvenance.code == "600000.SH",
                FinancialFieldProvenance.period == "2025",
            )
        )
        session.add(
            Financial(
                code="600000.SH",
                period="2026",
                period_type="annual",
                net_income=Decimal("777"),
                source_as_of="2026-12-31",
            )
        )
        session.add(
            FinancialFieldProvenance(
                code="600000.SH",
                period="2026",
                period_type="annual",
                field="net_income",
                source="discovery",
            )
        )
        session.add(
            ValuationHistory(
                code="600000.SH",
                month="2026-10",
                trade_date="2026-10-01",
                pe=Decimal("1"),
                pb=Decimal("0.1"),
                price=Decimal("99"),
                source="future",
                ratio_method="PE_TTM/PB_MRQ",
                fetched_at=SNAPSHOT_AT + timedelta(seconds=1),
            )
        )
        session.commit()
    engine.dispose()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
    with app.state.session_factory() as session:
        frozen = load_frozen_inputs(session, run_id).companies[0]
    history = frozen.fields["financial_history"]
    assert len(history) == 1
    assert history[0]["period"] == "2025"
    assert history[0]["values"]["net_income"] == "100"
    assert history[0]["provenance"] == {"net_income": ("discovery",)}
    valuations = frozen.fields["valuation_history"]
    assert [row["month"] for row in valuations] == ["2026-09"]
    assert frozen.fields["price"] == "10"


def test_create_dispatch_failure_retries_existing_queued_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from jobs.lock import lock_status
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "create-dispatch-retry.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    starts: list[int] = []

    def flaky_start(_app, target, args):
        starts.append(1)
        if len(starts) == 1:
            return RuntimeError("thread unavailable")
        target(*args)
        return None

    with TestClient(app) as client:
        monkeypatch.setattr(
            "app.discovery_routes._start_discovery_worker", flaky_start
        )
        first = client.post("/api/discoveries", json={})
        assert first.status_code == 500
        with app.state.session_factory() as session:
            assert lock_status(session) is None

        retried = client.post("/api/discoveries", json={})
        assert retried.status_code == 200
        assert retried.json()["status"] == "failed"
        assert len(starts) == 2


def test_sync_flag_is_ignored_when_sync_mode_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "async-only.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    pending = []

    class DeferredThread:
        def __init__(self, *, target, args=(), daemon=None):
            self.target = target
            self.args = args

        def start(self):
            pending.append(self)

    with TestClient(app) as client:
        monkeypatch.setattr(
            "app.discovery_routes.threading.Thread", DeferredThread
        )
        response = client.post(
            "/api/discoveries", json={"sync": True}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        assert len(pending) == 1
        worker = pending.pop()
        worker.target(*worker.args)
        assert client.get("/api/discoveries/current").json()["status"] == (
            "failed"
        )


def test_stage_reads_preserve_zero_result_attempts_and_partitions(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
        DiscoveryInputSnapshot,
        DiscoveryResult,
        DiscoveryStage,
    )
    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "stage-attempt-reads.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        client.post(
            f"/api/discoveries/{run_id}/stages/safety/confirm",
            json={"sync": True},
        )
        with app.state.session_factory() as session:
            common = session.query(DiscoveryCommonStage).filter_by(
                run_id=run_id
            ).one()
            frozen = session.query(DiscoveryInputSnapshot).filter_by(
                run_id=run_id,
                code="600000.SH",
            ).one()
            session.add_all(
                DiscoveryInputSnapshot(
                    run_id=run_id,
                    code=code,
                    name=name,
                    industry=frozen.industry,
                    security_status=frozen.security_status,
                    fields_json=frozen.fields_json,
                    sources_json=frozen.sources_json,
                    as_of_json=frozen.as_of_json,
                    data_version=frozen.data_version,
                    financial_version=frozen.financial_version,
                    valuation_version=frozen.valuation_version,
                    ruler_versions_json=frozen.ruler_versions_json,
                    algorithm_version=frozen.algorithm_version,
                    input_fingerprint=frozen.input_fingerprint,
                    snapshot_at=frozen.snapshot_at,
                )
                for code, name in (
                    ("000001.SZ", "冻结公司一"),
                    ("000002.SZ", "冻结公司二"),
                    ("000003.SZ", "冻结公司三"),
                )
            )
            session.add_all(
                [
                    DiscoveryCommonResult(
                        run_id=run_id,
                        attempt=1,
                        code="000001.SZ",
                        verdict="drop",
                        reason="test-drop",
                        metrics_json='{"score":0}',
                        created_at=SNAPSHOT_AT,
                    ),
                    DiscoveryCommonResult(
                        run_id=run_id,
                        attempt=1,
                        code="000002.SZ",
                        verdict="insufficient",
                        reason="test-insufficient",
                        metrics_json='{"score":null}',
                        created_at=SNAPSHOT_AT,
                    ),
                    DiscoveryCommonStage(
                        run_id=run_id,
                        attempt=2,
                        status="failed",
                        total_count=0,
                        pass_count=0,
                        drop_count=0,
                        insufficient_count=0,
                        error="zero-result failure",
                        input_fingerprint=common.input_fingerprint,
                        algorithm_version=common.algorithm_version,
                        failure_kind="permanent",
                        created_at=SNAPSHOT_AT,
                        updated_at=SNAPSHOT_AT,
                    ),
                ]
            )
            gate = session.query(DiscoveryStage).filter_by(
                run_id=run_id,
                track="garp",
                stage="track_gate",
            ).one()
            from discovery.rulers import activate
            from tests.discovery_helpers import discovery_ruler_payload

            garp_ruler = activate(
                session,
                discovery_ruler_payload("garp"),
                "route partition fixture",
            )
            second_gate = DiscoveryStage(
                run_id=run_id,
                track="garp",
                stage="track_gate",
                attempt=2,
                status="waiting_confirmation",
                total_count=3,
                pass_count=1,
                drop_count=1,
                insufficient_count=1,
                input_fingerprint=gate.input_fingerprint,
                ruler_version_id=garp_ruler.id,
                algorithm_version=gate.algorithm_version,
                common_stage_id=common.id,
                predecessor_stage_id=None,
                predecessor_stage=None,
                created_at=SNAPSHOT_AT,
                updated_at=SNAPSHOT_AT,
            )
            session.add(second_gate)
            session.flush()
            common.total_count = 3
            common.pass_count = 1
            common.drop_count = 1
            common.insufficient_count = 1
            common.superseded_at = SNAPSHOT_AT
            gate.superseded_at = SNAPSHOT_AT
            session.add_all(
                DiscoveryResult(
                    run_id=run_id,
                    track="garp",
                    stage="track_gate",
                    attempt=2,
                    code=code,
                    verdict=verdict,
                    reason=f"test-{verdict}",
                    metrics_json=f'{{"bucket":"{verdict}"}}',
                    rank=rank,
                    created_at=SNAPSHOT_AT,
                )
                for rank, (code, verdict) in enumerate(
                    [
                        ("000001.SZ", "pass"),
                        ("000002.SZ", "drop"),
                        ("000003.SZ", "insufficient"),
                    ],
                    start=1,
                )
            )
            session.commit()

        safety = client.get(
            f"/api/discoveries/{run_id}/stages/safety"
        ).json()
        assert [row["attempt"] for row in safety["attempts"]] == [1, 2]
        assert safety["attempts"][1]["status"] == "failed"
        assert all(
            safety["attempts"][1][bucket] == []
            for bucket in ("pass", "drop", "insufficient")
        )
        assert [len(safety["attempts"][0][bucket]) for bucket in (
            "pass",
            "drop",
            "insufficient",
        )] == [1, 1, 1]

        track = client.get(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate"
        ).json()
        assert [row["attempt"] for row in track["attempts"]] == [1, 2]
        assert all(
            track["attempts"][0][bucket] == []
            for bucket in ("pass", "drop", "insufficient")
        )
        assert [len(track[bucket]) for bucket in (
            "pass",
            "drop",
            "insufficient",
        )] == [1, 1, 1]


@pytest.mark.parametrize(
    "metrics_json",
    ["{", '{"bad":NaN}', '{"bad":1e999}', "[]"],
)
def test_corrupt_persisted_metrics_return_stable_error(
    tmp_path: Path, metrics_json: str
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryCommonResult
    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / f"corrupt-metrics-{len(metrics_json)}.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        with app.state.session_factory() as session:
            result = session.scalar(select(DiscoveryCommonResult))
            assert result is not None
            result.metrics_json = metrics_json
            session.commit()
        response = client.get(
            f"/api/discoveries/{run_id}/stages/safety"
        )
        assert response.status_code == 500
        assert response.json()["code"] == "DISCOVERY_RESULT_CORRUPT"


@pytest.mark.parametrize("body", [b"{", b"null", b"[]", b'"text"'])
def test_all_discovery_mutations_reject_malformed_nonobjects(
    tmp_path: Path, body: bytes
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryStage
    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / f"bad-bodies-{len(body)}.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    headers = {"content-type": "application/json"}
    with TestClient(app) as client:
        assert client.post(
            "/api/discoveries", content=body, headers=headers
        ).status_code == 400
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        paths = (
            f"/api/discoveries/{run_id}/stages/safety/retry",
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/track_gate/retry",
            f"/api/discoveries/{run_id}/recompute",
            f"/api/discoveries/{run_id}/abandon",
        )
        for path in paths:
            assert client.post(
                path, content=body, headers=headers
            ).status_code == 400
        assert client.get("/api/discoveries/current").json()["id"] == run_id
        assert client.get(
            f"/api/discoveries/{run_id}/stages/safety"
        ).json()["current"]["attempt"] == 1
        with app.state.session_factory() as session:
            assert session.scalar(
                select(func.count()).select_from(DiscoveryStage)
            ) == 3


def test_foreign_discovery_run_is_not_visible_or_mutable(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import User
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "foreign-discovery.db"
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        session.add(User(id=2, name="其他用户"))
        session.flush()
        run_id = _insert_run(
            session,
            user_id=2,
            week_key="2026-W38",
            current=True,
        )
        _add_common_stage(session, run_id)
        session.commit()
    engine.dispose()
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        assert client.get("/api/discoveries/current").status_code == 404
        for method, path in (
            ("get", f"/api/discoveries/{run_id}/stages/safety"),
            (
                "get",
                f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate",
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/stages/safety/confirm",
            ),
            ("post", f"/api/discoveries/{run_id}/abandon"),
        ):
            response = (
                client.post(path, json={})
                if method == "post"
                else client.get(path)
            )
            assert response.status_code == 404


def test_abandon_prevents_later_retry_and_queue_mutation(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryCommonStage, DiscoveryStage
    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "abandon-immutable.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        assert client.post(
            f"/api/discoveries/{run_id}/abandon", json={}
        ).status_code == 200
        rejected = client.post(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate/retry",
            json={},
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "DISCOVERY_RUN_IMMUTABLE"
    with app.state.session_factory() as session:
        common = session.scalar(select(DiscoveryCommonStage))
        assert common is not None and common.status == "confirmed"
        assert session.scalar(
            select(func.count()).select_from(DiscoveryStage)
        ) == 3


def test_startup_marks_running_transient_failed_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryCommonStage
    from app.main import create_app
    from jobs.lock import JOB_TYPE_DISCOVERY, release
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "interrupted-running.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )

    class LostThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

    with TestClient(app) as client:
        monkeypatch.setattr(
            "app.discovery_routes.threading.Thread", LostThread
        )
        run_id = client.post("/api/discoveries", json={}).json()["id"]
        with app.state.session_factory() as session:
            stage = session.scalar(select(DiscoveryCommonStage))
            assert stage is not None
            stage.status = "running"
            release(session, run_id, JOB_TYPE_DISCOVERY)
            session.commit()

    restarted = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(restarted):
        pass
    with restarted.state.session_factory() as session:
        stages = session.scalars(select(DiscoveryCommonStage)).all()
        assert len(stages) == 1
        assert (stages[0].status, stages[0].failure_kind) == (
            "failed",
            "transient",
        )
        assert session.scalar(
            select(func.count()).select_from(DiscoveryCommonStage)
        ) == 1


def test_exact_discovery_endpoint_surface(tmp_path: Path) -> None:
    from app.main import create_app
    from market.quotes import FakeQuoteClient

    app = create_app(
        db_path=tmp_path / "surface.db",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
    )
    included = next(
        route for route in app.routes if hasattr(route, "original_router")
    )
    surface = {
        (method, route.path)
        for route in included.original_router.routes
        for method in route.methods
    }
    assert surface == {
        ("POST", "/api/discoveries"),
        ("POST", "/api/discoveries/{run_id}/research"),
        ("GET", "/api/discoveries/current"),
        ("GET", "/api/discoveries/{run_id}/stages/safety"),
        (
            "POST",
            "/api/discoveries/{run_id}/stages/safety/confirm",
        ),
        (
            "POST",
            "/api/discoveries/{run_id}/stages/safety/retry",
        ),
        (
            "GET",
            "/api/discoveries/{run_id}/tracks/{track}/stages/{stage}",
        ),
        (
            "POST",
            "/api/discoveries/{run_id}/tracks/{track}/stages/{stage}/confirm",
        ),
        (
            "POST",
            "/api/discoveries/{run_id}/tracks/{track}/stages/{stage}/retry",
        ),
        ("POST", "/api/discoveries/{run_id}/overrides"),
        ("POST", "/api/discoveries/{run_id}/decisions"),
        ("POST", "/api/discoveries/{run_id}/recompute"),
        ("POST", "/api/discoveries/{run_id}/abandon"),
    }


def test_checkpoint_security_set_mismatch_is_data_not_ready(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from app.main import create_app
    from app.models import DiscoveryDataCheckpoint
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "snapshot-set-mismatch.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        session.execute(delete(DiscoveryDataCheckpoint))
        session.commit()
    engine.dispose()
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        response = client.post("/api/discoveries", json={"sync": True})
        assert response.status_code == 409
        assert response.json()["code"] == "DATA_NOT_READY"


def test_drain_observes_forced_abandon_before_queued_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryCommonStage, DiscoveryRun
    from app.main import create_app
    from jobs.lock import lock_status
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "abandon-before-drain.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    pending = []

    class DeferredThread:
        def __init__(self, *, target, args=(), daemon=None):
            self.target = target
            self.args = args

        def start(self):
            pending.append(self)

    with TestClient(app) as client:
        monkeypatch.setattr(
            "app.discovery_routes.threading.Thread", DeferredThread
        )
        run_id = client.post("/api/discoveries", json={}).json()["id"]
        with app.state.session_factory() as session:
            run = session.get(DiscoveryRun, run_id)
            assert run is not None
            run.current = False
            run.status = "abandoned"
            session.commit()
        worker = pending.pop()
        worker.target(*worker.args)

    with app.state.session_factory() as session:
        run = session.get(DiscoveryRun, run_id)
        stage = session.scalar(select(DiscoveryCommonStage))
        assert run is not None and run.status == "abandoned"
        assert stage is not None and stage.status == "queued"
        assert lock_status(session) is None


def test_new_fill_zero_counts_never_inherits_prior_global_data(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from discovery.snapshot import load_frozen_inputs
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "fill-affiliation-zero.db"
    _seed_api_snapshot(db_path)
    _started, completed = _add_second_api_fill(db_path, valuation_points=0)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: completed,
    )
    with TestClient(app) as client:
        run_id = client.post("/api/discoveries", json={"sync": True}).json()["id"]
    with app.state.session_factory() as session:
        company = load_frozen_inputs(session, run_id).companies[0]
    assert company.fields["financial_history"] == ()
    assert company.fields["valuation_history"] == ()
    assert company.fields["price"] is None


def test_new_fill_exact_payload_includes_data(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from discovery.snapshot import load_frozen_inputs
    from market.quotes import FakeQuoteClient
    from market.types import FinancialRow, ValuationPoint

    db_path = tmp_path / "fill-affiliation-valid.db"
    _seed_api_snapshot(db_path)
    completed_at = SNAPSHOT_AT + timedelta(hours=2)
    payload = _api_payload(
        completed_at,
        financial_rows=(
            FinancialRow(
                code="600000.SH",
                period="2025",
                period_type="annual",
                net_income=Decimal("100"),
                source_as_of="2026-03-31",
            ),
        ),
        valuation_points=(
            ValuationPoint(
                code="600000.SH",
                month="2026-09",
                trade_date="2026-09-17",
                pe=Decimal("8"),
                pb=Decimal("0.8"),
                price=Decimal("10"),
                source="eastmoney",
                ratio_method="PE_TTM/PB_MRQ",
            ),
        ),
    )
    _started, completed = _add_second_api_fill(
        db_path,
        valuation_points=1,
        payload_json=payload,
    )
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: completed,
    )
    with TestClient(app) as client:
        run_id = client.post("/api/discoveries", json={"sync": True}).json()["id"]
    with app.state.session_factory() as session:
        company = load_frozen_inputs(session, run_id).companies[0]
    assert company.fields["financial_history"][0]["values"]["net_income"] == "100"
    assert [row["month"] for row in company.fields["valuation_history"]] == ["2026-09"]
    assert company.fields["price"] == "10"


def test_post_cutoff_done_checkpoint_is_not_affiliated(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import DiscoveryDataCheckpoint
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "checkpoint-after-cutoff.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (1, "600000.SH"))
        assert checkpoint is not None
        checkpoint.updated_at = SNAPSHOT_AT + timedelta(seconds=1)
        session.commit()
    engine.dispose()
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        response = client.post("/api/discoveries", json={"sync": True})
        assert response.status_code == 409
        assert response.json()["code"] == "DATA_NOT_READY"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_mutation_bodies_reject_nonfinite_json_constants(
    tmp_path: Path, constant: str
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / f"nonfinite-body-{constant}.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    headers = {"content-type": "application/json"}
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/discoveries",
                content=f'{{"sync":{constant}}}',
                headers=headers,
            ).status_code
            == 400
        )
        run_id = client.post("/api/discoveries", json={"sync": True}).json()["id"]
        for path in (
            f"/api/discoveries/{run_id}/stages/safety/retry",
            f"/api/discoveries/{run_id}/abandon",
        ):
            response = client.post(
                path,
                content=f'{{"sync":{constant}}}',
                headers=headers,
            )
            assert response.status_code == 400


def test_track_executor_failure_does_not_strand_sibling_tracks(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.discovery_models import DiscoveryStage
    from app.main import create_app
    from app.models import RulerVersion
    from discovery.snapshot import canonical_json
    from jobs.lock import lock_status
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "track-failure-independence.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        for track in ("quality_discount", "deep_value", "garp"):
            payload_json, digest = discovery_ruler_values(track)
            session.add(
                RulerVersion(
                    user_id=1,
                    scope=f"discovery:{track}",
                    digest=digest,
                    reason="失败隔离测试",
                    payload_json=payload_json,
                    activated_at=SNAPSHOT_AT,
                )
            )
        session.commit()
    engine.dispose()

    executed: list[str] = []

    def execute(context):
        executed.append(context.track)
        if context.track == "deep_value":
            raise RuntimeError("deep value failed")
        return _passing_track_outcome(context)

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
        discovery_track_executor=execute,
    )
    with TestClient(app) as client:
        response = client.post("/api/discoveries", json={"sync": True})
        assert response.status_code == 200
        run_id = response.json()["id"]
    assert set(executed) == {"quality_discount", "deep_value", "garp"}
    with app.state.session_factory() as session:
        stages = {
            row.track: row
            for row in session.query(DiscoveryStage).filter_by(
                run_id=run_id, stage="track_gate"
            )
        }
        assert stages["deep_value"].status == "failed"
        assert stages["deep_value"].failure_kind == "permanent"
        assert stages["quality_discount"].status == "confirmed"
        assert stages["garp"].status == "confirmed"
        assert lock_status(session) is None


def test_integrity_race_winner_redispatches_failed_original_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event, Lock

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from starlette.requests import Request

    from app import discovery_routes
    from app.discovery_routes import create_discovery_router
    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "integrity-winner-redispatch.db"
    _seed_api_snapshot(db_path)
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    starts: list[int] = []

    def flaky_start(_app, target, args):
        starts.append(1)
        if len(starts) == 1:
            return RuntimeError("thread unavailable")
        target(*args)
        return None

    with TestClient(app), ThreadPoolExecutor(max_workers=2) as pool:
        warm = Event()
        all_ready = Event()
        ready = 0
        ready_lock = Lock()

        def occupy() -> None:
            nonlocal ready
            with ready_lock:
                ready += 1
                if ready == 2:
                    all_ready.set()
            assert warm.wait(5)

        warmed = [pool.submit(occupy) for _ in range(2)]
        assert all_ready.wait(5)
        warm.set()
        for future in warmed:
            future.result()

        original_scalar = Session.scalar
        initial_reads = 0
        read_lock = Lock()
        both_read = Barrier(2)
        release_complete = Event()
        original_release = discovery_routes._release_committed_lock

        def observed_release(*args, **kwargs):
            original_release(*args, **kwargs)
            release_complete.set()

        def synchronized_scalar(self, statement, *args, **kwargs):
            nonlocal initial_reads
            result = original_scalar(self, statement, *args, **kwargs)
            sql = str(statement)
            if (
                "discovery_runs.week_key" not in sql
                or "discovery_runs.current" not in sql
            ):
                return result
            with read_lock:
                if initial_reads >= 2:
                    assert release_complete.wait(5)
                    return result
                initial_reads += 1
            both_read.wait(5)
            return result

        monkeypatch.setattr(Session, "scalar", synchronized_scalar)
        monkeypatch.setattr(
            discovery_routes, "_release_committed_lock", observed_release
        )
        monkeypatch.setattr(
            discovery_routes, "_start_discovery_worker", flaky_start
        )
        endpoint = next(
            route.endpoint
            for route in create_discovery_router().routes
            if route.path == "/api/discoveries"
        )

        def invoke():
            async def receive():
                return {
                    "type": "http.request",
                    "body": b"{}",
                    "more_body": False,
                }

            request = Request(
                {"type": "http", "method": "POST", "app": app},
                receive,
            )
            return asyncio.run(endpoint(request))

        first = pool.submit(invoke)
        second = pool.submit(invoke)
        responses = [first.result(), second.result()]

    assert sorted(getattr(response, "status_code", 200) for response in responses) == [
        200,
        500,
    ]
    winner = next(response for response in responses if isinstance(response, dict))
    assert winner["status"] == "failed"
    assert len(starts) == 2


def test_lifespan_bounds_discovery_worker_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event, Thread

    from fastapi.testclient import TestClient

    from app import discovery_routes
    from app.main import create_app
    from jobs import discovery as discovery_job
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "discovery-worker-shutdown.db"
    _seed_api_snapshot(db_path)
    entered = Event()
    release_worker = Event()
    worker_finished = Event()
    original = discovery_job.classify_common
    original_drain = discovery_routes._drain

    def blocked_classifier(*args, **kwargs):
        entered.set()
        assert release_worker.wait(5)
        return original(*args, **kwargs)

    def observed_drain(*args):
        try:
            return original_drain(*args)
        finally:
            worker_finished.set()

    monkeypatch.setattr(discovery_job, "classify_common", blocked_classifier)
    monkeypatch.setattr(discovery_routes, "_drain", observed_drain)
    first_app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    first_client = TestClient(first_app)
    first_client.__enter__()
    closed = Event()
    try:
        response = first_client.post("/api/discoveries", json={})
        assert response.status_code == 200
        assert entered.wait(5)
        with first_app.state.discovery_workers_lock:
            assert len(first_app.state.discovery_workers) == 1

        shutdown_finished = Event()

        def shutdown() -> None:
            first_client.__exit__(None, None, None)
            closed.set()
            shutdown_finished.set()

        closer = Thread(target=shutdown)
        closer.start()
        assert shutdown_finished.wait(2)
        release_worker.set()
        assert worker_finished.wait(5)
        closer.join()
        with first_app.state.discovery_workers_lock:
            assert not first_app.state.discovery_workers
    finally:
        release_worker.set()
        if not closed.is_set():
            first_client.__exit__(None, None, None)

    second_app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(second_app) as second_client:
        current = second_client.get("/api/discoveries/current")
        assert current.status_code == 200
        assert current.json()["status"] == "failed"
        with second_app.state.discovery_workers_lock:
            assert not second_app.state.discovery_workers


def test_worker_start_registration_is_atomic_with_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event
    from threading import Thread as RealThread

    from fastapi.testclient import TestClient

    from app.discovery_routes import _start_discovery_worker
    from app.main import create_app
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "atomic-worker-registration.db"
    _seed_api_snapshot(db_path)
    start_entered = Event()
    allow_start_return = Event()
    start_returned = Event()
    target_finished = Event()
    joined_unstarted: list[bool] = []

    class BlockingStartThread:
        def __init__(self, *, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.ran = False

        def start(self):
            start_entered.set()
            assert allow_start_return.wait(5)
            start_returned.set()

        def join(self):
            joined_unstarted.append(not start_returned.is_set())
            if not self.ran and start_returned.is_set():
                self.ran = True
                self.target(*self.args)
                target_finished.set()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=False,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    client = TestClient(app)
    client.__enter__()
    monkeypatch.setattr(
        "app.discovery_routes.threading.Thread", BlockingStartThread
    )
    starter_finished = Event()
    teardown_finished = Event()

    def target() -> None:
        target_finished.set()

    def start_worker() -> None:
        assert _start_discovery_worker(app, target, ()) is None
        starter_finished.set()

    def teardown() -> None:
        with app.state.discovery_workers_lock:
            app.state.discovery_shutting_down = True
            workers = tuple(app.state.discovery_workers)
        for worker in workers:
            worker.join()
        teardown_finished.set()

    starter = RealThread(target=start_worker)
    starter.start()
    assert start_entered.wait(5)
    teardown_thread = RealThread(target=teardown)
    teardown_thread.start()
    assert not teardown_finished.wait(0.1)
    assert joined_unstarted == []
    allow_start_return.set()
    starter.join(5)
    teardown_thread.join(5)
    assert starter_finished.is_set()
    assert teardown_finished.is_set()
    assert target_finished.is_set()
    assert joined_unstarted == [False]
    with app.state.discovery_workers_lock:
        assert not app.state.discovery_workers
    client.__exit__(None, None, None)


def test_latest_fill_payload_omits_prior_financial_periods_and_values(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import Financial, FinancialFieldProvenance
    from discovery.snapshot import load_frozen_inputs
    from market.quotes import FakeQuoteClient
    from market.types import FinancialRow

    db_path = tmp_path / "exact-fill-financial-payload.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        session.add(
            Financial(
                code="600000.SH",
                period="2024",
                period_type="annual",
                net_income=Decimal("999"),
                source_as_of="2025-03-31",
            )
        )
        session.add(
            FinancialFieldProvenance(
                code="600000.SH",
                period="2024",
                period_type="annual",
                field="net_income",
                source="discovery",
            )
        )
        session.commit()
    engine.dispose()
    completed_at = SNAPSHOT_AT + timedelta(hours=2)
    payload = _api_payload(
        completed_at,
        financial_rows=(
            FinancialRow(
                code="600000.SH",
                period="2025",
                period_type="annual",
                net_income=Decimal("222"),
                source_as_of="2026-03-31",
            ),
        ),
        valuation_points=(),
    )
    _started, completed = _add_second_api_fill(
        db_path, valuation_points=0, payload_json=payload
    )
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: completed,
    )
    with TestClient(app) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
    with app.state.session_factory() as session:
        history = load_frozen_inputs(session, run_id).companies[0].fields[
            "financial_history"
        ]
    assert [row["period"] for row in history] == ["2025"]
    assert history[0]["values"]["net_income"] == "222"


def test_production_snapshot_never_queries_shared_company_caches(
    tmp_path: Path,
) -> None:
    from sqlalchemy import event

    from discovery.snapshot import build_production_snapshot

    db_path = tmp_path / "no-shared-cache-reads.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    forbidden = {
        "financials",
        "financial_field_provenance",
        "financial_fetch_completeness",
        "valuation_history",
    }

    def reject_cache_read(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        sql = statement.lower()
        if sql.lstrip().startswith("select") and any(
            table in sql for table in forbidden
        ):
            raise AssertionError(f"shared cache queried: {statement}")

    event.listen(engine, "before_cursor_execute", reject_cache_read)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        company = build_production_snapshot(session).companies[0]
    assert company.fields["financial_history"][0]["values"]["net_income"] == (
        "100"
    )
    assert company.fields["price"] == "10"
    event.remove(engine, "before_cursor_execute", reject_cache_read)
    engine.dispose()


def test_snapshot_versions_hash_exact_checkpoint_payload_content(
    tmp_path: Path,
) -> None:
    from app.models import DiscoveryDataCheckpoint
    from discovery.company_payload import company_payload_digest
    from discovery.snapshot import build_production_snapshot
    from market.types import FinancialRow

    paths = [tmp_path / "payload-a.db", tmp_path / "payload-b.db"]
    for path in paths:
        _seed_api_snapshot(path)
    engine = init_db(paths[1])
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (1, "600000.SH")
        )
        assert checkpoint is not None
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_discovery_checkpoint_payload_immutable"
            )
        )
        payload_json = _api_payload(
            SNAPSHOT_AT,
            financial_rows=(
                FinancialRow(
                    code="600000.SH",
                    period="2025",
                    period_type="annual",
                    net_income=Decimal("101"),
                    source_as_of="2026-03-31",
                ),
            ),
        )
        checkpoint.payload_json = payload_json
        checkpoint.payload_digest = company_payload_digest(payload_json)
        session.commit()
    engine.dispose()

    snapshots = []
    for path in paths:
        engine = init_db(path)
        with create_session_factory(engine)() as session:
            snapshots.append(build_production_snapshot(session))
        engine.dispose()
    assert snapshots[0].versions.data_version != snapshots[1].versions.data_version
    assert (
        snapshots[0].versions.financial_version
        != snapshots[1].versions.financial_version
    )
    assert (
        snapshots[0].versions.valuation_version
        == snapshots[1].versions.valuation_version
    )


def test_snapshot_keeps_annual_rows_but_versions_cover_interim(
    tmp_path: Path,
) -> None:
    from app.models import DiscoveryDataCheckpoint
    from discovery.company_payload import company_payload_digest
    from discovery.snapshot import build_production_snapshot
    from market.types import FinancialRow

    snapshots = []
    for interim_income in ("40", "41"):
        path = tmp_path / f"interim-{interim_income}.db"
        _seed_api_snapshot(path)
        engine = init_db(path)
        with create_session_factory(engine)() as session:
            session.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_discovery_checkpoint_payload_immutable"
                )
            )
            checkpoint = session.get(
                DiscoveryDataCheckpoint, (1, "600000.SH")
            )
            payload_json = _api_payload(
                SNAPSHOT_AT,
                financial_rows=(
                    FinancialRow(
                        code="600000.SH",
                        period="2025",
                        period_type="annual",
                        net_income=Decimal("100"),
                        source_as_of="2026-03-31",
                    ),
                    FinancialRow(
                        code="600000.SH",
                        period="2025H1",
                        period_type="interim",
                        net_income=Decimal(interim_income),
                        source_as_of="2025-08-31",
                    ),
                ),
            )
            checkpoint.payload_json = payload_json
            checkpoint.payload_digest = company_payload_digest(payload_json)
            session.commit()
            snapshots.append(build_production_snapshot(session))
        engine.dispose()
    for snapshot in snapshots:
        history = snapshot.companies[0].fields["financial_history"]
        assert [row["period_type"] for row in history] == ["annual"]
    assert (
        snapshots[0].versions.financial_version
        != snapshots[1].versions.financial_version
    )


def test_streamed_snapshot_versions_match_whole_value_hashes(
    tmp_path: Path,
) -> None:
    from app.models import (
        DiscoveryDataCheckpoint,
        DiscoveryDataFill,
        DiscoveryFillTradingDate,
        DiscoveryUniverseSecurity,
    )
    from discovery.company_payload import (
        company_payload_digest,
        parse_company_payload,
    )
    from discovery.snapshot import _sha256, build_production_snapshot

    db_path = tmp_path / "streamed-versions.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    with create_session_factory(engine)() as session:
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_discovery_universe_security_insert_immutable"
            )
        )
        fill = session.scalars(select(DiscoveryDataFill)).one()
        template = session.scalars(select(DiscoveryUniverseSecurity)).one()
        columns = DiscoveryUniverseSecurity.__table__.columns
        # Generate the second issuer's own identity-bearing schema4 content.
        # Text replacement would leave its embedded fundamental digest invalid.
        done_text = _api_payload(SNAPSHOT_AT, code="600036.SH")
        for code, status, text_value in (
            ("000001.SZ", "failed", None),
            ("600036.SH", "done", done_text),
        ):
            session.add(
                DiscoveryUniverseSecurity(
                    **{
                        column.name: getattr(template, column.name)
                        for column in columns
                        if column.name != "code"
                    },
                    code=code,
                )
            )
            session.add(
                DiscoveryDataCheckpoint(
                    fill_id=fill.id,
                    code=code,
                    status=status,
                    attempts=1,
                    next_retry_at=None,
                    valuation_points=0 if text_value is None else 1,
                    error=None if text_value else "boom",
                    payload_json=text_value,
                    payload_digest=(
                        text_value and company_payload_digest(text_value)
                    ),
                    updated_at=SNAPSHOT_AT,
                )
            )
        session.commit()

        snapshot = build_production_snapshot(session)
        cutoff = fill.updated_at.astimezone(timezone.utc)
        checkpoints = session.scalars(
            select(DiscoveryDataCheckpoint).order_by(DiscoveryDataCheckpoint.code)
        ).all()
        securities = session.scalars(
            select(DiscoveryUniverseSecurity).order_by(
                DiscoveryUniverseSecurity.code
            )
        ).all()
        payloads = {
            row.code: parse_company_payload(row.payload_json)
            for row in checkpoints
            if row.status == "done"
        }
        boundary = {
            "fill_id": fill.id,
            "cutoff": cutoff,
            "checkpoints": [
                {
                    "code": row.code,
                    "status": row.status,
                    "valuation_points": row.valuation_points,
                    "payload_json": (
                        row.payload_json if row.status == "done" else None
                    ),
                }
                for row in checkpoints
            ],
            "securities": [
                {column.name: getattr(row, column.name) for column in columns}
                for row in securities
            ],
            "trading_dates": tuple(
                session.scalars(select(DiscoveryFillTradingDate.trade_date)).all()
            ),
        }
    engine.dispose()
    stamp = f"{fill.id}:{cutoff.isoformat()}"
    assert len(snapshot.companies) == 3
    assert snapshot.versions.data_version == (
        f"discovery-fill:{stamp}:{_sha256(boundary)[:20]}"
    )
    assert snapshot.versions.financial_version == (
        f"financial:fill:{stamp}:"
        + _sha256(
            {code: {key: value[key] for key in (
                'financial_rows', 'schema_version', 'annual_evidence', 'interim_history',
                'capital_events', 'fundamental_extensions', 'coverage', 'evidence_ids',
                'current_mcap', 'fundamental_fingerprint', 'decision_cutoff_at',
                'operating_policy', 'operating_result',
            )} for code, value in payloads.items()}
        )[:20]
    )
    assert snapshot.versions.valuation_version == (
        f"valuation:fill:{stamp}:"
        + _sha256(
            {code: value["valuation_points"] for code, value in payloads.items()}
        )[:20]
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "missing",
        "malformed",
        "count",
        "digest_missing",
        "digest_malformed",
        "digest_mismatch",
    ],
)
def test_latest_done_checkpoint_rejects_unreproducible_payload(
    tmp_path: Path, tamper: str
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import DiscoveryDataCheckpoint
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / f"unreproducible-{tamper}.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        if tamper != "count":
            session.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_discovery_checkpoint_payload_immutable"
                )
            )
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (1, "600000.SH")
        )
        assert checkpoint is not None
        if tamper == "missing":
            checkpoint.payload_json = None
        elif tamper == "malformed":
            checkpoint.payload_json = '{"code":"600000.SH","x":NaN}'
        elif tamper == "count":
            checkpoint.valuation_points = 2
        elif tamper == "digest_missing":
            checkpoint.payload_digest = None
        elif tamper == "digest_malformed":
            checkpoint.payload_digest = "ABC"
        else:
            checkpoint.payload_digest = "0" * 64
        session.commit()
    engine.dispose()
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        response = client.post("/api/discoveries", json={"sync": True})
    assert response.status_code == 409
    assert response.json()["code"] == "DATA_NOT_READY"
    detail = response.json()["detail"].lower()
    assert "restart" in detail or "start a new fill" in detail


@pytest.mark.parametrize(
    "future_part",
    ["calendar", "universe_snapshot", "universe_source", "checkpoint"],
)
def test_production_snapshot_rejects_future_fill_timestamps(
    tmp_path: Path, future_part: str
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import (
        DiscoveryDataCheckpoint,
        DiscoveryFillTradingDate,
        DiscoveryUniverseSecurity,
    )
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / f"future-fill-{future_part}.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        if future_part == "calendar":
            calendar = session.get(
                DiscoveryFillTradingDate, (1, "2026-09-17")
            )
            assert calendar is not None
            session.delete(calendar)
            session.add(
                DiscoveryFillTradingDate(fill_id=1, trade_date="2099-01-01")
            )
        elif future_part == "universe_snapshot":
            security = session.get(
                DiscoveryUniverseSecurity, (1, "600000.SH")
            )
            assert security is not None
            security.snapshot_at = datetime(
                2099, 1, 1, tzinfo=timezone.utc
            )
        elif future_part == "universe_source":
            security = session.get(
                DiscoveryUniverseSecurity, (1, "600000.SH")
            )
            assert security is not None
            security.source_as_of = "2099-01-01"
        else:
            session.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_discovery_checkpoint_payload_immutable"
                )
            )
            checkpoint = session.get(
                DiscoveryDataCheckpoint, (1, "600000.SH")
            )
            assert checkpoint is not None
            checkpoint.updated_at = datetime(
                2099, 1, 1, tzinfo=timezone.utc
            )
        if future_part in {"universe_snapshot", "universe_source"}:
            with pytest.raises(IntegrityError, match="immutable"):
                session.commit()
            session.rollback()
            engine.dispose()
            return
        session.commit()
    engine.dispose()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        response = client.post("/api/discoveries", json={"sync": True})
    assert response.status_code == 409
    assert response.json()["code"] == "DATA_NOT_READY"


@pytest.mark.parametrize(
    ("market_date", "expected_status"),
    [("2026-09-17", 200), ("2026-09-18", 409)],
)
def test_snapshot_date_cutoff_uses_china_market_day(
    tmp_path: Path,
    market_date: str,
    expected_status: int,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import (
        DiscoveryDataCheckpoint,
        DiscoveryDataFill,
        DiscoveryFillTradingDate,
        DiscoveryUniverseSecurity,
    )
    from discovery.company_payload import company_payload_digest
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / f"china-cutoff-{market_date}.db"
    _seed_api_snapshot(db_path)
    cutoff = datetime(2026, 9, 16, 17, tzinfo=timezone.utc)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_discovery_checkpoint_payload_immutable"
            )
        )
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_discovery_universe_security_immutable"
            )
        )
        fill = session.get(DiscoveryDataFill, 1)
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (1, "600000.SH")
        )
        security = session.get(
            DiscoveryUniverseSecurity, (1, "600000.SH")
        )
        calendar = session.get(
            DiscoveryFillTradingDate, (1, "2026-09-17")
        )
        assert all(
            row is not None
            for row in (fill, checkpoint, security, calendar)
        )
        fill.started_at = cutoff - timedelta(hours=1)
        fill.updated_at = cutoff
        checkpoint.updated_at = cutoff
        security.snapshot_at = cutoff
        payload_json = _api_payload(cutoff)
        checkpoint.payload_json = payload_json
        checkpoint.payload_digest = company_payload_digest(payload_json)
        if market_date != "2026-09-17":
            session.delete(calendar)
            session.add(
                DiscoveryFillTradingDate(
                    fill_id=1,
                    trade_date=market_date,
                )
            )
        session.commit()
    engine.dispose()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: cutoff,
    )
    with TestClient(app) as client:
        response = client.post("/api/discoveries", json={"sync": True})
    assert response.status_code == expected_status
    if expected_status == 409:
        assert response.json()["code"] == "DATA_NOT_READY"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_tagged_discovery_ruler_is_ignored(
    tmp_path: Path, constant: str
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import RulerVersion
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / f"nonfinite-ruler-{constant}.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        session.add(
            RulerVersion(
                user_id=1,
                scope="discovery:quality_discount",
                digest="nonfinite",
                reason="非法尺子",
                payload_json=(
                    '{"algorithm_version":"discovery-v1","scope":'
                    f'"weekly-discovery","threshold":{constant},'
                    '"track":"quality_discount"}'
                ),
                activated_at=SNAPSHOT_AT,
            )
        )
        session.commit()
    engine.dispose()
    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT,
    )
    with TestClient(app) as client:
        run = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()
    assert run["ruler_version_ids"]["quality_discount"] is None
