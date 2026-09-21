from types import SimpleNamespace

import pytest


def test_effective_verdict_is_pure_and_strict() -> None:
    from discovery.overrides import StageOverride, StageResult, effective_verdict

    result = StageResult("garp", "competition", "600000.SH", "drop")
    override = StageOverride(
        "garp",
        "competition",
        "600000.SH",
        "forced_pass",
        "人工复核通过",
    )

    assert effective_verdict(result, None) == "drop"
    assert effective_verdict(result, override) == "pass"
    assert result.verdict == "drop"
    assert not hasattr(override, "decision")

    with pytest.raises(ValueError, match="same result"):
        effective_verdict(
            result,
            StageOverride(
                "garp",
                "competition",
                "000001.SZ",
                "forced_drop",
                "不同公司",
            ),
        )


@pytest.mark.parametrize(
    ("factory", "values"),
    [
        ("result", ("unknown", "competition", "600000.SH", "pass")),
        ("result", ("garp", "unknown", "600000.SH", "pass")),
        ("result", ("garp", "competition", "", "pass")),
        ("result", ("garp", "competition", "600000.SH", "maybe")),
        (
            "override",
            ("garp", "competition", "600000.SH", "pass", "reason"),
        ),
        (
            "override",
            ("garp", "competition", "600000.SH", "forced_pass", " "),
        ),
    ],
)
def test_override_values_reject_unknown_or_blank_fields(
    factory: str, values: tuple[object, ...]
) -> None:
    from discovery.overrides import StageOverride, StageResult

    cls = StageResult if factory == "result" else StageOverride
    with pytest.raises(ValueError):
        cls(*values)


def test_effective_verdict_rejects_non_value_objects() -> None:
    from discovery.overrides import effective_verdict

    with pytest.raises(TypeError):
        effective_verdict(
            SimpleNamespace(
                track="garp",
                stage="competition",
                code="600000.SH",
                verdict="pass",
            ),
            None,
        )


def _workflow_app(tmp_path, *, clock=None):
    from dataclasses import replace

    from app.db import create_session_factory, init_db
    from app.main import create_app
    from app.models import RulerVersion
    from discovery.snapshot import build_production_snapshot
    from jobs.discovery import TrackExecutionOutcome, TrackExecutionResult
    from market.quotes import FakeQuoteClient
    from tests.discovery_helpers import (
        SNAPSHOT_AT,
        company,
        discovery_ruler_values,
    )
    from tests.test_discovery_workflow import _seed_api_snapshot

    db_path = tmp_path / "task6.db"
    _seed_api_snapshot(db_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    with factory() as session:
        for track in ("quality_discount", "deep_value", "garp"):
            payload_json, digest = discovery_ruler_values(track)
            session.add(
                RulerVersion(
                    user_id=1,
                    scope=f"discovery:{track}",
                    digest=digest,
                    reason="Task 6",
                    payload_json=payload_json,
                    activated_at=SNAPSHOT_AT,
                )
            )
        session.commit()
    engine.dispose()

    def execute(context):
        return TrackExecutionOutcome(
            status="waiting_confirmation",
            results=tuple(
                TrackExecutionResult(
                code=code,
                verdict="pass",
                reason=None,
                metrics_json="{}",
                rank=index,
                )
                for index, code in enumerate(
                    context.candidate_codes, start=1
                )
            )
        )

    def snapshot(session, **kwargs):
        frozen = build_production_snapshot(session, **kwargs)
        return replace(
            frozen,
            companies=tuple(
                company(f"60000{index}.SH") for index in range(6)
            ),
        )

    return create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=clock or (lambda: SNAPSHOT_AT),
        discovery_track_executor=execute,
        discovery_snapshot_provider=snapshot,
    )


def _advance_to_competition(client) -> int:
    run_id = client.post(
        "/api/discoveries", json={"sync": True}
    ).json()["id"]
    assert client.post(
        f"/api/discoveries/{run_id}/stages/safety/confirm",
        json={"sync": True},
    ).status_code == 200
    assert client.post(
        f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate/confirm",
        json={"sync": True},
    ).status_code == 200
    return run_id


def test_current_partially_completed_run_can_be_abandoned(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.discovery_models import DiscoveryRun

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        with app.state.session_factory() as session:
            run = session.get(DiscoveryRun, run_id)
            assert run is not None
            run.status = "partially_completed"
            session.commit()

        response = client.post(
            f"/api/discoveries/{run_id}/abandon", json={}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "abandoned"
        assert response.json()["current"] is False


def test_prior_week_mutations_are_immutable_but_recompute_remains_allowed(
    tmp_path,
) -> None:
    import sqlite3
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from tests.discovery_helpers import SNAPSHOT_AT

    now = [SNAPSHOT_AT]
    app = _workflow_app(tmp_path, clock=lambda: now[0])
    db_path = tmp_path / "task6.db"

    def database_dump() -> tuple[str, ...]:
        with sqlite3.connect(db_path) as connection:
            return tuple(connection.iterdump())

    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        override = client.post(
            f"/api/discoveries/{run_id}/overrides",
            json={
                "track": "garp",
                "stage": "competition",
                "code": "600005.SH",
                "action": "forced_drop",
                "reason": "W38 覆盖",
            },
        )
        assert override.status_code == 200
        override_id = override.json()["id"]
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        assert client.post(
            f"/api/discoveries/{run_id}/decisions",
            json={
                "track": "garp",
                "code": "600000.SH",
                "decision": "keep",
            },
        ).status_code == 200

        now[0] = SNAPSHOT_AT + timedelta(days=7)
        before = database_dump()
        requests = [
            (
                "post",
                f"/api/discoveries/{run_id}/stages/safety/confirm",
                {"sync": True},
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/stages/safety/retry",
                {"sync": True},
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/tracks/garp/stages/competition/confirm",
                {"sync": True},
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/tracks/garp/stages/competition/retry",
                {"sync": True},
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/tracks/garp/stages/competition/reopen",
                {"sync": True},
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/overrides",
                {
                    "track": "garp",
                    "stage": "competition",
                    "code": "600004.SH",
                    "action": "forced_drop",
                    "reason": "W39 禁止",
                },
            ),
            (
                "delete",
                f"/api/discoveries/{run_id}/overrides/{override_id}",
                None,
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/decisions",
                {
                    "track": "garp",
                    "code": "600000.SH",
                    "decision": "exclude",
                    "reason": "W39 禁止",
                },
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/abandon",
                {},
            ),
        ]
        for method, path, payload in requests:
            response = client.request(method.upper(), path, json=payload)
            assert response.status_code == 409
            assert response.json()["code"] == "historical_run_immutable"
        assert database_dump() == before

        assert client.get(
            f"/api/discoveries/{run_id}/stages/safety"
        ).status_code == 200
        history = client.get(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition"
        )
        assert history.status_code == 200
        assert history.json()["attempts"]

        replay = client.post(
            f"/api/discoveries/{run_id}/recompute",
            json={"sync": True},
        )
        assert replay.status_code == 200
        assert replay.json()["week_key"] == "2026-W39"
        replay_id = replay.json()["id"]
        with app.state.session_factory() as session:
            from app.discovery_models import DiscoveryRun

            assert session.get(
                DiscoveryRun, replay_id
            ).replay_of_run_id == run_id
        assert client.post(
            f"/api/discoveries/{replay_id}/stages/safety/confirm",
            json={"sync": True},
        ).status_code == 200
        assert client.post(
            f"/api/discoveries/{replay_id}/overrides",
            json={
                "track": "garp",
                "stage": "track_gate",
                "code": "600000.SH",
                "action": "forced_drop",
                "reason": "W39 合法",
            },
        ).status_code == 200


def test_startup_and_duplicate_create_ignore_prior_week_queue(
    tmp_path,
) -> None:
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from app.db import create_session_factory, init_db
    from app.discovery_models import DiscoveryCommonStage
    from app.main import create_app
    from market.quotes import FakeQuoteClient
    from tests.discovery_helpers import SNAPSHOT_AT, company
    from tests.test_discovery_common import _add_common_stage, _freeze_run

    db_path = tmp_path / "rollover-startup.db"
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    with factory() as session:
        old = _freeze_run(
            session, [company("600000.SH")], week_key="2026-W38"
        )
        current = _freeze_run(
            session, [company("600000.SH")], week_key="2026-W39"
        )
        old.current = True
        current.current = True
        old_stage = _add_common_stage(session, old)
        current_stage = _add_common_stage(session, current)
        old_id, current_id = old.id, current.id
        old_stage_id, current_stage_id = old_stage.id, current_stage.id
        session.commit()
    engine.dispose()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: SNAPSHOT_AT + timedelta(days=7),
    )
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            assert session.get(
                DiscoveryCommonStage, old_stage_id
            ).status == "queued"
            assert session.get(
                DiscoveryCommonStage, current_stage_id
            ).status == "waiting_confirmation"
        duplicate = client.post(
            "/api/discoveries", json={"sync": True}
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["id"] == current_id
        assert duplicate.json()["id"] != old_id
        with app.state.session_factory() as session:
            assert session.get(
                DiscoveryCommonStage, old_stage_id
            ).status == "queued"


@pytest.mark.parametrize(
    "case",
    [
        "common_confirm",
        "track_confirm",
        "safety_retry",
        "track_retry",
        "reopen",
        "abandon",
    ],
)
def test_locking_route_rollover_after_acquire_rolls_back_everything(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    import sqlite3
    from datetime import timedelta

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app import discovery_routes
    from app.discovery_models import DiscoveryCommonStage, DiscoveryStage
    from app.models import JobLock
    from tests.discovery_helpers import SNAPSHOT_AT

    now = [SNAPSHOT_AT]
    app = _workflow_app(tmp_path, clock=lambda: now[0])
    db_path = tmp_path / "task6.db"
    with TestClient(app) as client:
        if case in {"common_confirm", "safety_retry", "abandon"}:
            run_id = client.post(
                "/api/discoveries", json={"sync": True}
            ).json()["id"]
        else:
            run_id = _advance_to_competition(client)

        if case == "common_confirm":
            path = f"/api/discoveries/{run_id}/stages/safety/confirm"
        elif case == "track_confirm":
            path = (
                f"/api/discoveries/{run_id}/tracks/garp"
                "/stages/competition/confirm"
            )
        elif case == "safety_retry":
            with app.state.session_factory() as session:
                stage = session.scalar(
                    select(DiscoveryCommonStage).where(
                        DiscoveryCommonStage.run_id == run_id,
                        DiscoveryCommonStage.superseded_at.is_(None),
                    )
                )
                stage.status = "failed"
                stage.failure_kind = "transient"
                session.commit()
            path = f"/api/discoveries/{run_id}/stages/safety/retry"
        elif case == "track_retry":
            with app.state.session_factory() as session:
                stage = session.scalar(
                    select(DiscoveryStage).where(
                        DiscoveryStage.run_id == run_id,
                        DiscoveryStage.track == "garp",
                        DiscoveryStage.stage == "competition",
                        DiscoveryStage.superseded_at.is_(None),
                    )
                )
                stage.status = "failed"
                stage.failure_kind = "transient"
                session.commit()
            path = (
                f"/api/discoveries/{run_id}/tracks/garp"
                "/stages/competition/retry"
            )
        elif case == "reopen":
            assert client.post(
                f"/api/discoveries/{run_id}/tracks/garp"
                "/stages/competition/confirm",
                json={"sync": True},
            ).status_code == 200
            path = (
                f"/api/discoveries/{run_id}/tracks/garp"
                "/stages/competition/reopen"
            )
        else:
            path = f"/api/discoveries/{run_id}/abandon"

        def dump_without_lock() -> tuple[str, ...]:
            with sqlite3.connect(db_path) as connection:
                return tuple(
                    line
                    for line in connection.iterdump()
                    if "job_lock" not in line
                )

        before = dump_without_lock()
        original_acquire = discovery_routes.acquire_discovery

        def acquire_then_rollover(session, selected_run_id):
            token = original_acquire(session, selected_run_id)
            if token is not None:
                now[0] += timedelta(days=7)
            return token

        monkeypatch.setattr(
            discovery_routes,
            "acquire_discovery",
            acquire_then_rollover,
        )
        response = client.post(path, json={"sync": True})
        assert response.status_code == 409
        assert response.json()["code"] == "historical_run_immutable"
        assert dump_without_lock() == before
        with app.state.session_factory() as session:
            lock = session.get(JobLock, 1)
            assert lock is not None and lock.job_id is None


@pytest.mark.parametrize(
    "case",
    ["override_create", "override_delete", "decision"],
)
def test_unlocked_route_rollover_at_commit_checkpoint_is_immutable(
    tmp_path,
    case: str,
) -> None:
    import sqlite3
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from tests.discovery_helpers import SNAPSHOT_AT

    now = [SNAPSHOT_AT]
    armed = [False]
    calls = [0]

    def clock():
        if armed[0]:
            calls[0] += 1
            if calls[0] > 1:
                return now[0] + timedelta(days=7)
        return now[0]

    app = _workflow_app(tmp_path, clock=clock)
    db_path = tmp_path / "task6.db"
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        path = f"/api/discoveries/{run_id}/overrides"
        payload = {
            "track": "garp",
            "stage": "competition",
            "code": "600005.SH",
            "action": "forced_drop",
            "reason": "rollover",
        }
        method = "post"
        if case == "override_delete":
            override_id = client.post(path, json=payload).json()["id"]
            path = (
                f"/api/discoveries/{run_id}/overrides/{override_id}"
            )
            payload = None
            method = "delete"
        elif case == "decision":
            assert client.post(
                f"/api/discoveries/{run_id}/tracks/garp"
                "/stages/competition/confirm",
                json={"sync": True},
            ).status_code == 200
            path = f"/api/discoveries/{run_id}/decisions"
            payload = {
                "track": "garp",
                "code": "600000.SH",
                "decision": "keep",
            }

        with sqlite3.connect(db_path) as connection:
            before = tuple(connection.iterdump())
        armed[0] = True
        response = client.request(
            method.upper(),
            path,
            json=payload,
        )
        assert response.status_code == 409
        assert response.json()["code"] == "historical_run_immutable"
        with sqlite3.connect(db_path) as connection:
            assert tuple(connection.iterdump()) == before


def test_recompute_uses_fresh_target_week_at_commit(tmp_path) -> None:
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from tests.discovery_helpers import SNAPSHOT_AT

    now = [SNAPSHOT_AT]
    armed = [False]
    calls = [0]

    def clock():
        if armed[0]:
            calls[0] += 1
            if calls[0] > 1:
                return now[0] + timedelta(days=7)
        return now[0]

    app = _workflow_app(tmp_path, clock=clock)
    with TestClient(app) as client:
        source_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        assert client.post(
            f"/api/discoveries/{source_id}/abandon", json={}
        ).status_code == 200
        armed[0] = True
        response = client.post(
            f"/api/discoveries/{source_id}/recompute",
            json={"sync": True},
        )
        assert response.status_code == 200
        assert response.json()["week_key"] == "2026-W39"


def test_override_decision_and_adjudication_contract(tmp_path) -> None:
    from fastapi.testclient import TestClient

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        path = f"/api/discoveries/{run_id}/overrides"
        saved = client.post(
            path,
            json={
                "track": "garp",
                "stage": "competition",
                "code": "600005.SH",
                "action": "forced_drop",
                "reason": "复核排除",
            },
        )
        assert saved.status_code == 200
        override_id = saved.json()["id"]
        replaced = client.post(
            path,
            json={
                "track": "garp",
                "stage": "competition",
                "code": "600005.SH",
                "action": "forced_pass",
                "reason": "复核恢复",
            },
        )
        assert replaced.json()["id"] == override_id
        assert client.delete(
            f"/api/discoveries/{run_id}/overrides/{override_id}"
        ).status_code == 200
        frozen = client.post(
            path,
            json={
                "track": "garp",
                "stage": "competition",
                "code": "600005.SH",
                "action": "forced_drop",
                "reason": "冻结排除",
            },
        )
        assert frozen.status_code == 200
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        assert client.delete(
            f"/api/discoveries/{run_id}/overrides/{frozen.json()['id']}"
        ).status_code == 409

        for index in range(5):
            response = client.post(
                f"/api/discoveries/{run_id}/decisions",
                json={
                    "track": "garp",
                    "code": f"60000{index}.SH",
                    "decision": "keep" if index < 3 else "exclude",
                    "reason": "不保留" if index >= 3 else None,
                    "note": "保留" if index < 3 else None,
                },
            )
            assert response.status_code == 200
        confirmed = client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/adjudication/confirm",
            json={},
        )
        assert confirmed.status_code == 200
        adjudication = client.get(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/adjudication"
        ).json()
        assert adjudication["current"]["status"] == "confirmed"
        assert len(adjudication["pass"]) == 3
        assert len(adjudication["drop"]) == 2
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/adjudication/confirm",
            json={},
        ).status_code == 200


@pytest.mark.parametrize(
    ("keep_count", "expected_status", "confirm_status"),
    [
        (0, "insufficient_candidates", 200),
        (1, "insufficient_candidates", 200),
        (2, "insufficient_candidates", 200),
        (3, "confirmed", 200),
        (4, "confirmed", 200),
        (5, "confirmed", 200),
        (6, "waiting_confirmation", 409),
    ],
)
def test_adjudication_keeper_bounds(
    tmp_path, keep_count: int, expected_status: str, confirm_status: int
) -> None:
    from fastapi.testclient import TestClient

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        incomplete = client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/adjudication/confirm",
            json={},
        )
        assert incomplete.status_code == 409
        assert incomplete.json()["code"] == "DISCOVERY_DECISIONS_INCOMPLETE"
        for index in range(6):
            assert client.post(
                f"/api/discoveries/{run_id}/decisions",
                json={
                    "track": "garp",
                    "code": f"60000{index}.SH",
                    "decision": "keep" if index < keep_count else "exclude",
                    "reason": "排除" if index >= keep_count else None,
                },
            ).status_code == 200
        confirmed = client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/adjudication/confirm",
            json={},
        )
        assert confirmed.status_code == confirm_status
        stage = client.get(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/adjudication"
        ).json()
        assert stage["current"]["status"] == expected_status
        if keep_count == 6:
            assert stage["pass"] == []


@pytest.mark.parametrize(
    ("keep_count", "expected_status"),
    [
        (0, "insufficient_candidates"),
        (2, "insufficient_candidates"),
        (3, "confirmed"),
        (5, "confirmed"),
    ],
)
def test_two_sessions_adjudication_confirm_is_idempotent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    keep_count: int,
    expected_status: str,
) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from starlette.requests import Request

    from app import discovery_routes
    from app.discovery_models import DiscoveryResult, DiscoveryStage

    app = _workflow_app(tmp_path)
    first_waiting = Event()
    second_waiting = Event()
    release_first = Event()
    first_finished = Event()
    guard = Lock()
    calls = 0
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        for index in range(6):
            assert client.post(
                f"/api/discoveries/{run_id}/decisions",
                json={
                    "track": "garp",
                    "code": f"60000{index}.SH",
                    "decision": "keep" if index < keep_count else "exclude",
                    "reason": "排除" if index >= keep_count else None,
                },
            ).status_code == 200

        original = discovery_routes.acquire_discovery

        def ordered_acquire(session, selected_run_id):
            nonlocal calls
            with guard:
                calls += 1
                call = calls
            if call == 1:
                first_waiting.set()
                assert release_first.wait(5)
            elif call == 2:
                second_waiting.set()
                assert first_finished.wait(5)
            return original(session, selected_run_id)

        monkeypatch.setattr(
            discovery_routes, "acquire_discovery", ordered_acquire
        )
        endpoint = next(
            route.endpoint
            for route in discovery_routes.create_discovery_router().routes
            if getattr(route, "path", None)
            == "/api/discoveries/{run_id}/tracks/{track}"
            "/stages/{stage}/confirm"
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
            return asyncio.run(
                endpoint(
                    request,
                    run_id,
                    "garp",
                    "adjudication",
                )
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invoke)
            assert first_waiting.wait(5)
            second = pool.submit(invoke)
            assert second_waiting.wait(5)
            release_first.set()
            first_response = first.result(timeout=5)
            first_finished.set()
            second_response = second.result(timeout=5)
        assert isinstance(first_response, dict)
        assert isinstance(second_response, dict)

    with app.state.session_factory() as session:
        stage = session.scalar(
            select(DiscoveryStage).where(
                DiscoveryStage.run_id == run_id,
                DiscoveryStage.track == "garp",
                DiscoveryStage.stage == "adjudication",
                DiscoveryStage.superseded_at.is_(None),
            )
        )
        assert stage is not None and stage.status == expected_status
        assert session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(
                DiscoveryResult.run_id == run_id,
                DiscoveryResult.track == "garp",
                DiscoveryResult.stage == "adjudication",
                DiscoveryResult.attempt == stage.attempt,
            )
        ) == 6


def test_retry_and_reopen_preserve_exact_attempt_chains(tmp_path) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from tests.discovery_helpers import SNAPSHOT_AT

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        with app.state.session_factory() as session:
            competition = session.scalar(
                select(DiscoveryStage).where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                )
            )
            assert competition is not None
            competition.status = "failed"
            competition.failure_kind = "transient"
            competition.error = "network"
            competition.total_count = 0
            competition.pass_count = 0
            competition.drop_count = 0
            competition.insufficient_count = 0
            session.query(DiscoveryResult).filter_by(
                run_id=run_id,
                track="garp",
                stage="competition",
                attempt=competition.attempt,
            ).delete(synchronize_session=False)
            session.commit()
        retried = client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/retry",
            json={"sync": True},
        )
        assert retried.status_code == 200
        competition_history = client.get(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition"
        ).json()
        assert [row["attempt"] for row in competition_history["attempts"]] == [
            1,
            2,
        ]
        assert competition_history["attempts"][0]["superseded"] is True
        assert (
            competition_history["attempts"][1]["retry_of_stage_id"]
            == competition_history["attempts"][0]["id"]
        )
        assert (
            competition_history["attempts"][1]["reopen_of_stage_id"] is None
        )

        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        reopened = client.post(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate/reopen",
            json={"sync": True},
        )
        assert reopened.status_code == 200
        gate_history = client.get(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate"
        ).json()
        assert [row["attempt"] for row in gate_history["attempts"]] == [1, 2]
        assert gate_history["attempts"][0]["superseded"] is True
        assert (
            gate_history["attempts"][1]["reopen_of_stage_id"]
            == gate_history["attempts"][0]["id"]
        )
        assert gate_history["attempts"][1]["retry_of_stage_id"] is None
        assert gate_history["current"]["status"] == "waiting_confirmation"
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate/reopen",
            json={},
        ).status_code == 409
        retry_reopen = client.post(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate/retry",
            json={},
        )
        assert retry_reopen.status_code == 409
        assert retry_reopen.json()["code"] == "DISCOVERY_RETRY_NOT_ALLOWED"
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/track_gate/confirm",
            json={"sync": True},
        ).status_code == 200
        retry_confirmed_reopen = client.post(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate/retry",
            json={},
        )
        assert retry_confirmed_reopen.status_code == 409
        assert (
            retry_confirmed_reopen.json()["code"]
            == "DISCOVERY_RETRY_NOT_ALLOWED"
        )
        with app.state.session_factory() as session:
            chains = session.scalars(
                select(DiscoveryStage)
                .where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                )
                .order_by(DiscoveryStage.attempt)
            ).all()
            assert [row.attempt for row in chains] == [1, 2, 3]
            assert chains[-1].predecessor_stage_id == gate_history["current"]["id"]
            assert session.scalar(
                select(DiscoveryResult.id).where(
                    DiscoveryResult.run_id == run_id,
                    DiscoveryResult.track == "garp",
                    DiscoveryResult.stage == "competition",
                    DiscoveryResult.attempt == 1,
                )
                ) is None
            assert chains[-1].created_at == SNAPSHOT_AT


def test_recompute_copies_frozen_inputs_without_mutating_source(tmp_path) -> None:
    from datetime import timedelta

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryInputSnapshot, DiscoveryRun
    from tests.discovery_helpers import SNAPSHOT_AT

    clock = [SNAPSHOT_AT]
    app = _workflow_app(tmp_path, clock=lambda: clock[0])
    with TestClient(app) as client:
        source_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        assert client.post(
            f"/api/discoveries/{source_id}/abandon", json={}
        ).status_code == 200
        with app.state.session_factory() as session:
            source = session.get(DiscoveryRun, source_id)
            assert source is not None
            before = tuple(
                getattr(source, column.name)
                for column in DiscoveryRun.__table__.columns
            )
            source_rows = session.scalars(
                select(DiscoveryInputSnapshot)
                .where(DiscoveryInputSnapshot.run_id == source_id)
                .order_by(DiscoveryInputSnapshot.code)
            ).all()
            source_payloads = [
                (
                    row.code,
                    row.fields_json,
                    row.sources_json,
                    row.as_of_json,
                    row.input_fingerprint,
                )
                for row in source_rows
            ]
        clock[0] += timedelta(days=7)
        first = client.post(
            f"/api/discoveries/{source_id}/recompute",
            json={"sync": True},
        )
        assert first.status_code == 200
        replay_id = first.json()["id"]
        assert replay_id != source_id
        assert client.post(
            f"/api/discoveries/{source_id}/recompute",
            json={"sync": True},
        ).json()["id"] == replay_id

    with app.state.session_factory() as session:
        source = session.get(DiscoveryRun, source_id)
        replay = session.get(DiscoveryRun, replay_id)
        assert source is not None and replay is not None
        assert tuple(
            getattr(source, column.name)
            for column in DiscoveryRun.__table__.columns
        ) == before
        assert replay.replay_of_run_id == source_id
        replay_rows = session.scalars(
            select(DiscoveryInputSnapshot)
            .where(DiscoveryInputSnapshot.run_id == replay_id)
            .order_by(DiscoveryInputSnapshot.code)
        ).all()
        assert [
            (
                row.code,
                row.fields_json,
                row.sources_json,
                row.as_of_json,
                row.input_fingerprint,
            )
            for row in replay_rows
        ] == source_payloads


def test_direct_adjudication_reopen_is_rejected(tmp_path) -> None:
    from fastapi.testclient import TestClient

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        response = client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/adjudication/reopen",
            json={"sync": True},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "invalid_reopen_stage"


def test_competition_reopen_preserves_old_decisions_and_starts_new_set(
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.discovery_models import (
        DiscoveryDecision,
        DiscoveryEvidence,
        DiscoveryResult,
        DiscoveryStage,
    )

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        assert client.post(
            f"/api/discoveries/{run_id}/decisions",
            json={
                "track": "garp",
                "code": "600000.SH",
                "decision": "keep",
                "note": "old",
            },
        ).status_code == 200
        with app.state.session_factory() as session:
            old_competition = session.scalar(
                select(DiscoveryStage).where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                    DiscoveryStage.attempt == 1,
                )
            )
            assert old_competition is not None
            session.add(
                DiscoveryEvidence(
                    run_id=run_id,
                    track="garp",
                    competition_attempt=1,
                    competition_stage_id=old_competition.id,
                    stage="competition",
                    code="600000.SH",
                    ruler_version_id=old_competition.ruler_version_id,
                    rule_version="old",
                    kind="evidence",
                    rule_id="old-rule",
                    input_fields_json="{}",
                    display_text="old evidence",
                    as_of="2026-09-17",
                    data_completeness="complete",
                    created_at=old_competition.created_at,
                )
            )
            session.commit()
            old_counts = (
                session.scalar(
                    select(func.count()).select_from(DiscoveryDecision)
                ),
                session.scalar(
                    select(func.count()).select_from(DiscoveryEvidence)
                ),
                session.scalar(
                    select(func.count()).select_from(DiscoveryResult)
                ),
            )
        reopened = client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/reopen",
            json={"sync": True},
        )
        assert reopened.status_code == 200
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        new_decision = client.post(
            f"/api/discoveries/{run_id}/decisions",
            json={
                "track": "garp",
                "code": "600000.SH",
                "decision": "exclude",
                "reason": "new",
            },
        )
        assert new_decision.status_code == 200
        assert new_decision.json()["competition_attempt"] == 2
    with app.state.session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(DiscoveryDecision)
            )
            == old_counts[0] + 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(DiscoveryEvidence)
            )
            == old_counts[1]
        )
        assert (
            session.scalar(
                select(func.count()).select_from(DiscoveryResult)
            )
            > old_counts[2]
        )
        old = session.scalar(
            select(DiscoveryDecision).where(
                DiscoveryDecision.competition_attempt == 1
            )
        )
        assert old is not None and old.keep_note == "old"


def test_abandoned_historical_http_reads_artifacts_without_executor(
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryEvidence, DiscoveryStage

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        override = client.post(
            f"/api/discoveries/{run_id}/overrides",
            json={
                "track": "garp",
                "stage": "competition",
                "code": "600000.SH",
                "action": "forced_pass",
                "reason": "history",
            },
        ).json()
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        assert client.post(
            f"/api/discoveries/{run_id}/decisions",
            json={
                "track": "garp",
                "code": "600000.SH",
                "decision": "keep",
                "note": "history",
            },
        ).status_code == 200
        with app.state.session_factory() as session:
            competition = session.scalar(
                select(DiscoveryStage).where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                    DiscoveryStage.attempt == 1,
                )
            )
            assert competition is not None
            session.add(
                DiscoveryEvidence(
                    run_id=run_id,
                    track="garp",
                    competition_attempt=1,
                    competition_stage_id=competition.id,
                    stage="competition",
                    code="600000.SH",
                    ruler_version_id=competition.ruler_version_id,
                    rule_version="history",
                    kind="rule",
                    rule_id="history",
                    input_fields_json="{}",
                    display_text="history",
                    as_of="2026-09-17",
                    data_completeness="complete",
                    created_at=competition.created_at,
                )
            )
            session.commit()
        assert client.post(
            f"/api/discoveries/{run_id}/abandon", json={}
        ).status_code == 200
        assert client.post(
            f"/api/discoveries/{run_id}/abandon", json={}
        ).status_code == 200
        app.state.discovery_track_executor = None
        body = client.get(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition"
        ).json()
        assert body["overrides"][0]["id"] == override["id"]
        assert body["decisions"][0]["keep_note"] == "history"
        assert body["evidence"][0]["rule_version"] == "history"
        paths = [
            (
                "post",
                f"/api/discoveries/{run_id}/overrides",
                {
                    "track": "garp",
                    "stage": "competition",
                    "code": "600000.SH",
                    "action": "forced_drop",
                    "reason": "no",
                },
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/decisions",
                {
                    "track": "garp",
                    "code": "600000.SH",
                    "decision": "keep",
                },
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/tracks/garp/stages/competition/retry",
                {},
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/tracks/garp/stages/competition/reopen",
                {},
            ),
            (
                "post",
                f"/api/discoveries/{run_id}/tracks/garp/stages/competition/confirm",
                {},
            ),
        ]
        for method, path, payload in paths:
            response = getattr(client, method)(path, json=payload)
            assert response.status_code == 409
            assert response.json()["code"] == "DISCOVERY_RUN_IMMUTABLE"
        deleted = client.delete(
            f"/api/discoveries/{run_id}/overrides/{override['id']}"
        )
        assert deleted.status_code == 409
        assert deleted.json()["code"] == "DISCOVERY_RUN_IMMUTABLE"


def test_run_freezes_ruler_payloads_and_ledger_row_is_immutable(tmp_path) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.discovery_models import DiscoveryRun
    from app.models import RulerVersion
    from discovery.snapshot import load_frozen_inputs

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
    with app.state.session_factory() as session:
        run = session.get(DiscoveryRun, run_id)
        assert run is not None and run.ruler_payloads_json
        before = run.ruler_payloads_json
        ruler = session.scalar(
            select(RulerVersion).where(
                RulerVersion.id == run.garp_ruler_version_id
            )
        )
        assert ruler is not None
        ruler.payload_json = '{"mutated":true}'
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()
        frozen = load_frozen_inputs(session, run_id)
        assert frozen.ruler_payloads["garp"]["payload"]["track"] == "garp"
        assert session.get(DiscoveryRun, run_id).ruler_payloads_json == before


@pytest.mark.parametrize("corruption", ["snapshot", "build", "ruler"])
def test_recompute_rejects_incomplete_frozen_provenance_but_history_reads(
    tmp_path, corruption: str
) -> None:
    from datetime import timedelta

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryInputSnapshot, DiscoveryRun
    from tests.discovery_helpers import SNAPSHOT_AT

    clock = [SNAPSHOT_AT]
    app = _workflow_app(tmp_path, clock=lambda: clock[0])
    with TestClient(app) as client:
        source_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        with app.state.session_factory() as session:
            source = session.get(DiscoveryRun, source_id)
            assert source is not None
            source.current = False
            source.status = "completed"
            if corruption == "snapshot":
                row = session.scalar(
                    select(DiscoveryInputSnapshot).where(
                        DiscoveryInputSnapshot.run_id == source_id
                    )
                )
                assert row is not None
                row.fields_json = '{"broken":NaN}'
            elif corruption == "build":
                source.algorithm_version = "retired-build"
            else:
                source.ruler_payloads_json = None
            session.commit()
        assert client.get(
            f"/api/discoveries/{source_id}/stages/safety"
        ).status_code == 200
        clock[0] += timedelta(days=7)
        response = client.post(
            f"/api/discoveries/{source_id}/recompute", json={}
        )
        assert response.status_code == 409
        assert response.json()["code"] == "algorithm_unavailable"


def test_recompute_current_conflict_and_two_request_idempotency(
    tmp_path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Barrier

    from fastapi.testclient import TestClient

    from app.discovery_models import DiscoveryRun
    from tests.discovery_helpers import SNAPSHOT_AT

    clock = [SNAPSHOT_AT]
    app = _workflow_app(tmp_path, clock=lambda: clock[0])
    with TestClient(app) as client:
        source_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        with app.state.session_factory() as session:
            source = session.get(DiscoveryRun, source_id)
            assert source is not None
            source.current = False
            source.status = "completed"
            session.commit()
        clock[0] += timedelta(days=7)
        current_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        conflict = client.post(
            f"/api/discoveries/{source_id}/recompute", json={}
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "DISCOVERY_CURRENT_CONFLICT"
        with app.state.session_factory() as session:
            current = session.get(DiscoveryRun, current_id)
            assert current is not None
            current.current = False
            current.status = "completed"
            session.commit()
        barrier = Barrier(2)

        def replay():
            barrier.wait(5)
            return client.post(
                f"/api/discoveries/{source_id}/recompute", json={}
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [
                future.result()
                for future in [pool.submit(replay), pool.submit(replay)]
            ]
        assert [response.status_code for response in responses] == [200, 200]
        assert len({response.json()["id"] for response in responses}) == 1


def test_track_retry_contention_returns_new_attempt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryStage

    app = _workflow_app(tmp_path)
    entered = Event()
    release = Event()
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        with app.state.session_factory() as session:
            stage = session.scalar(
                select(DiscoveryStage).where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                    DiscoveryStage.superseded_at.is_(None),
                )
            )
            assert stage is not None
            failed_stage_id = stage.id
            stage.status = "failed"
            stage.failure_kind = "transient"
            session.commit()

        original = app.state.discovery_track_executor

        def blocked(context):
            entered.set()
            assert release.wait(5)
            return original(context)

        app.state.discovery_track_executor = blocked
        first = client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/retry",
            json={},
        )
        assert first.status_code == 200
        assert entered.wait(5)
        with app.state.session_factory() as session:
            latest = session.scalar(
                select(DiscoveryStage)
                .where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                    DiscoveryStage.superseded_at.is_(None),
                )
                .order_by(DiscoveryStage.attempt.desc())
            )
            assert latest is not None
            assert latest.retry_of_stage_id == failed_stage_id
            assert latest.reopen_of_stage_id is None
        second = client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/retry",
            json={},
        )
        assert second.status_code == 200
        release.set()


def test_two_sessions_retry_one_exact_failed_origin(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from starlette.requests import Request

    from app import discovery_routes
    from app.discovery_models import DiscoveryStage

    app = _workflow_app(tmp_path)
    barrier = Barrier(2)
    guard = Lock()
    calls = 0
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        with app.state.session_factory() as session:
            failed = session.scalar(
                select(DiscoveryStage).where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                    DiscoveryStage.superseded_at.is_(None),
                )
            )
            assert failed is not None
            failed.status = "failed"
            failed.failure_kind = "transient"
            failed_id = failed.id
            session.commit()

        original = discovery_routes.acquire_discovery

        def synchronized_acquire(session, selected_run_id):
            nonlocal calls
            with guard:
                calls += 1
                call = calls
            if call <= 2:
                barrier.wait(5)
            return original(session, selected_run_id)

        monkeypatch.setattr(
            discovery_routes,
            "acquire_discovery",
            synchronized_acquire,
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
            endpoint = next(
                route.endpoint
                for route in discovery_routes.create_discovery_router().routes
                if getattr(route, "path", None)
                == "/api/discoveries/{run_id}/tracks/{track}"
                "/stages/{stage}/retry"
            )
            return asyncio.run(
                endpoint(request, run_id, "garp", "competition")
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [
                future.result()
                for future in [pool.submit(invoke), pool.submit(invoke)]
            ]
        assert all(isinstance(response, dict) for response in responses)
    with app.state.session_factory() as session:
        rows = session.scalars(
            select(DiscoveryStage)
            .where(
                DiscoveryStage.run_id == run_id,
                DiscoveryStage.track == "garp",
                DiscoveryStage.stage == "competition",
            )
            .order_by(DiscoveryStage.attempt)
        ).all()
        assert len(rows) == 2
        assert rows[1].retry_of_stage_id == failed_id
        assert rows[1].reopen_of_stage_id is None


def test_competition_reopen_contention_returns_new_attempt(tmp_path) -> None:
    from threading import Event

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryStage

    app = _workflow_app(tmp_path)
    entered = Event()
    release = Event()
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        original = app.state.discovery_track_executor
        with app.state.session_factory() as session:
            selected = session.scalar(
                select(DiscoveryStage).where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                    DiscoveryStage.superseded_at.is_(None),
                )
            )
            assert selected is not None
            selected_id = selected.id

        def blocked(context):
            entered.set()
            assert release.wait(5)
            return original(context)

        app.state.discovery_track_executor = blocked
        path = (
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/reopen"
        )
        assert client.post(path, json={}).status_code == 200
        assert entered.wait(5)
        with app.state.session_factory() as session:
            latest = session.scalar(
                select(DiscoveryStage)
                .where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                    DiscoveryStage.superseded_at.is_(None),
                )
                .order_by(DiscoveryStage.attempt.desc())
            )
            assert latest is not None
            assert latest.reopen_of_stage_id == selected_id
            assert latest.retry_of_stage_id is None
        assert client.post(path, json={}).status_code == 200
        release.set()


def test_two_sessions_reopen_one_exact_selected_origin(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from starlette.requests import Request

    from app import discovery_routes
    from app.discovery_models import DiscoveryStage

    app = _workflow_app(tmp_path)
    barrier = Barrier(2)
    guard = Lock()
    calls = 0
    with TestClient(app) as client:
        run_id = _advance_to_competition(client)
        assert client.post(
            f"/api/discoveries/{run_id}/tracks/garp"
            "/stages/competition/confirm",
            json={"sync": True},
        ).status_code == 200
        with app.state.session_factory() as session:
            selected = session.scalar(
                select(DiscoveryStage).where(
                    DiscoveryStage.run_id == run_id,
                    DiscoveryStage.track == "garp",
                    DiscoveryStage.stage == "competition",
                    DiscoveryStage.superseded_at.is_(None),
                )
            )
            assert selected is not None
            selected_id = selected.id

        original = discovery_routes.acquire_discovery

        def synchronized_acquire(session, selected_run_id):
            nonlocal calls
            with guard:
                calls += 1
                call = calls
            if call <= 2:
                barrier.wait(5)
            return original(session, selected_run_id)

        monkeypatch.setattr(
            discovery_routes,
            "acquire_discovery",
            synchronized_acquire,
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
            endpoint = next(
                route.endpoint
                for route in discovery_routes.create_discovery_router().routes
                if getattr(route, "path", None)
                == "/api/discoveries/{run_id}/tracks/{track}"
                "/stages/{stage}/reopen"
            )
            return asyncio.run(
                endpoint(request, run_id, "garp", "competition")
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [
                future.result()
                for future in [pool.submit(invoke), pool.submit(invoke)]
            ]
        assert all(isinstance(response, dict) for response in responses)
    with app.state.session_factory() as session:
        rows = session.scalars(
            select(DiscoveryStage)
            .where(
                DiscoveryStage.run_id == run_id,
                DiscoveryStage.track == "garp",
                DiscoveryStage.stage == "competition",
            )
            .order_by(DiscoveryStage.attempt)
        ).all()
        assert len(rows) == 2
        assert rows[1].reopen_of_stage_id == selected_id
        assert rows[1].retry_of_stage_id is None


def test_safety_retry_contention_returns_new_attempt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryCommonStage
    from jobs import discovery as discovery_job

    app = _workflow_app(tmp_path)
    entered = Event()
    release = Event()
    with TestClient(app) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        with app.state.session_factory() as session:
            stage = session.scalar(
                select(DiscoveryCommonStage).where(
                    DiscoveryCommonStage.run_id == run_id,
                    DiscoveryCommonStage.superseded_at.is_(None),
                )
            )
            assert stage is not None
            failed_stage_id = stage.id
            stage.status = "failed"
            stage.failure_kind = "transient"
            session.commit()

        original = discovery_job.classify_common

        def blocked(item, dates):
            entered.set()
            assert release.wait(5)
            return original(item, dates)

        monkeypatch.setattr(discovery_job, "classify_common", blocked)
        path = f"/api/discoveries/{run_id}/stages/safety/retry"
        assert client.post(path, json={}).status_code == 200
        assert entered.wait(5)
        with app.state.session_factory() as session:
            latest = session.scalar(
                select(DiscoveryCommonStage)
                .where(
                    DiscoveryCommonStage.run_id == run_id,
                    DiscoveryCommonStage.superseded_at.is_(None),
                )
                .order_by(DiscoveryCommonStage.attempt.desc())
            )
            assert latest is not None
            assert latest.retry_of_stage_id == failed_stage_id
        assert client.post(path, json={}).status_code == 200
        release.set()


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("terminal", "DISCOVERY_RETRY_TERMINAL"),
        ("permanent", "DISCOVERY_RETRY_PERMANENT"),
        ("input", "algorithm_unavailable"),
        ("build", "algorithm_unavailable"),
        ("ruler", "algorithm_unavailable"),
    ],
)
def test_safety_retry_rejects_non_retryable_provenance(
    tmp_path, case: str, expected_code: str
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.discovery_models import DiscoveryCommonStage, DiscoveryRun

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post(
            "/api/discoveries", json={"sync": True}
        ).json()["id"]
        with app.state.session_factory() as session:
            run = session.get(DiscoveryRun, run_id)
            stage = session.scalar(
                select(DiscoveryCommonStage).where(
                    DiscoveryCommonStage.run_id == run_id,
                    DiscoveryCommonStage.superseded_at.is_(None),
                )
            )
            assert run is not None and stage is not None
            stage.status = (
                "data_not_ready" if case == "terminal" else "failed"
            )
            stage.failure_kind = (
                None if case == "terminal" else "transient"
            )
            if case == "permanent":
                stage.failure_kind = "permanent"
            elif case == "input":
                stage.input_fingerprint = "different"
            elif case == "build":
                stage.algorithm_version = "retired-build"
            elif case == "ruler":
                run.ruler_payloads_json = None
            session.commit()
        response = client.post(
            f"/api/discoveries/{run_id}/stages/safety/retry",
            json={},
        )
        assert response.status_code == 409
        assert response.json()["code"] == expected_code


@pytest.mark.parametrize(
    "path",
    [
        "/api/discoveries/1/overrides",
        "/api/discoveries/1/decisions",
        "/api/discoveries/1/stages/safety/retry",
        "/api/discoveries/1/tracks/garp/stages/competition/retry",
        "/api/discoveries/1/tracks/garp/stages/competition/reopen",
        "/api/discoveries/1/recompute",
    ],
)
@pytest.mark.parametrize("body", [b"[]", b'{"value":NaN}'])
def test_task6_mutations_reject_non_object_or_nonfinite_body(
    tmp_path, path: str, body: bytes
) -> None:
    from fastapi.testclient import TestClient

    app = _workflow_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            path,
            content=body,
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400
