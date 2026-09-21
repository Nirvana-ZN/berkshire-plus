from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

import app.discovery_routes as discovery_routes
from app.discovery_models import (
    DiscoveryCommonStage,
    DiscoveryDecision,
    DiscoveryEvidence,
    DiscoveryInputSnapshot,
    DiscoveryResult,
    DiscoveryRun,
    DiscoveryStage,
)
from app.envfile import OPENAI_API_KEY, write_env_value
from app.main import create_app
from app.models import ResearchJob, RulerVersion, Watchlist
from discovery.constants import china_week_key
from jobs.lock import (
    JOB_TYPE_FUNNEL,
    JOB_TYPE_RESEARCH,
    lock_status,
    release,
    try_acquire,
)
from market.eastmoney import FakeMarketClient
from market.quotes import FakeQuoteClient
from research.llm import FakeLLM

NOW = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
TRACKS = ("quality_discount", "deep_value", "garp")


def _app(tmp_path: Path):
    write_env_value(tmp_path / ".env", OPENAI_API_KEY, "sk-test")
    return create_app(
        db_path=tmp_path / "app.db",
        env_path=tmp_path / ".env",
        quote_client=FakeQuoteClient(),
        market_client=FakeMarketClient(),
        llm_client=FakeLLM(),
        reports_dir=tmp_path / "reports",
        allow_sync=True,
        discovery_clock=lambda: NOW,
    )


def _run(session) -> DiscoveryRun:
    run = DiscoveryRun(
        user_id=1,
        week_key=china_week_key(NOW),
        current=True,
        status="done",
        created_at=NOW,
        updated_at=NOW,
        snapshot_at=NOW,
        data_version="data-v1",
        financial_version="financial-v1",
        valuation_version="valuation-v1",
        algorithm_version="discovery-v1",
        input_fingerprint="run-fingerprint",
    )
    session.add(run)
    session.flush()
    return run


def _seed_finalists(
    session,
    run: DiscoveryRun,
    by_track: dict[str, list[str]],
    *,
    status: str = "confirmed",
    keep: bool = True,
    evidence: bool = True,
) -> None:
    ruler_id = session.scalar(select(RulerVersion.id).order_by(RulerVersion.id))
    assert ruler_id is not None
    common = DiscoveryCommonStage(
        run_id=run.id,
        attempt=1,
        status="confirmed",
        total_count=len({code for codes in by_track.values() for code in codes}),
        pass_count=len({code for codes in by_track.values() for code in codes}),
        drop_count=0,
        insufficient_count=0,
        error=None,
        confirmed_at=NOW,
        input_fingerprint=run.input_fingerprint,
        algorithm_version=run.algorithm_version,
        failure_kind=None,
        superseded_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(common)
    session.flush()
    seen: set[str] = set()
    for track, codes in by_track.items():
        assert track in TRACKS
        for code in codes:
            if code not in seen:
                seen.add(code)
                session.add(
                    DiscoveryInputSnapshot(
                        run_id=run.id,
                        code=code,
                        name=f"公司-{code}",
                        industry="制造业",
                        security_status="trading",
                        fields_json="{}",
                        sources_json="{}",
                        as_of_json="{}",
                        data_version=run.data_version,
                        financial_version=run.financial_version,
                        valuation_version=run.valuation_version,
                        ruler_versions_json="{}",
                        algorithm_version=run.algorithm_version,
                        input_fingerprint=f"fingerprint-{code}",
                        snapshot_at=NOW,
                    )
                )
        gate = DiscoveryStage(
            run_id=run.id,
            track=track,
            stage="track_gate",
            attempt=1,
            status="confirmed",
            total_count=len(codes),
            pass_count=len(codes),
            drop_count=0,
            insufficient_count=0,
            error=None,
            confirmed_at=NOW,
            input_fingerprint=run.input_fingerprint,
            ruler_version_id=ruler_id,
            algorithm_version=run.algorithm_version,
            common_stage_id=common.id,
            predecessor_stage_id=None,
            predecessor_stage=None,
            failure_kind=None,
            superseded_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(gate)
        session.flush()
        competition = DiscoveryStage(
            run_id=run.id,
            track=track,
            stage="competition",
            attempt=1,
            status="confirmed",
            total_count=len(codes),
            pass_count=len(codes),
            drop_count=0,
            insufficient_count=0,
            error=None,
            confirmed_at=NOW,
            input_fingerprint=run.input_fingerprint,
            ruler_version_id=ruler_id,
            algorithm_version=run.algorithm_version,
            common_stage_id=None,
            predecessor_stage_id=gate.id,
            predecessor_stage="track_gate",
            failure_kind=None,
            superseded_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(competition)
        session.flush()
        adjudication = DiscoveryStage(
            run_id=run.id,
            track=track,
            stage="adjudication",
            attempt=1,
            status=status,
            total_count=len(codes),
            pass_count=len(codes) if keep else 0,
            drop_count=0 if keep else len(codes),
            insufficient_count=0,
            error=None,
            confirmed_at=NOW if status == "confirmed" else None,
            input_fingerprint=run.input_fingerprint,
            ruler_version_id=ruler_id,
            algorithm_version=run.algorithm_version,
            common_stage_id=None,
            predecessor_stage_id=competition.id,
            predecessor_stage="competition",
            failure_kind=None,
            superseded_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(adjudication)
        session.flush()
        for rank, code in enumerate(codes, 1):
            session.add_all(
                [
                    DiscoveryResult(
                        run_id=run.id,
                        track=track,
                        stage="competition",
                        attempt=competition.attempt,
                        code=code,
                        verdict="pass",
                        reason="persisted competition result",
                        metrics_json="{}",
                        rank=rank,
                        created_at=NOW,
                    ),
                    DiscoveryResult(
                        run_id=run.id,
                        track=track,
                        stage="adjudication",
                        attempt=adjudication.attempt,
                        code=code,
                        verdict="pass" if keep else "drop",
                        reason="kept" if keep else "excluded",
                        metrics_json="{}",
                        rank=rank if keep else None,
                        created_at=NOW,
                    ),
                    DiscoveryDecision(
                        run_id=run.id,
                        track=track,
                        competition_attempt=competition.attempt,
                        competition_stage_id=competition.id,
                        stage="competition",
                        code=code,
                        decision="keep" if keep else "exclude",
                        exclusion_reason=None if keep else "not final",
                        keep_note="final" if keep else None,
                        created_at=NOW,
                    ),
                ]
            )
            if evidence:
                session.add(
                    DiscoveryEvidence(
                        run_id=run.id,
                        track=track,
                        competition_attempt=competition.attempt,
                        competition_stage_id=competition.id,
                        stage="competition",
                        code=code,
                        ruler_version_id=ruler_id,
                        rule_version="rules-v1",
                        kind="evidence",
                        rule_id="final",
                        input_fields_json="{}",
                        display_text="persisted evidence",
                        as_of="2026-09-17",
                        data_completeness="complete",
                        created_at=NOW,
                    )
                )
    session.commit()


def _finish_dispatch(app, job_id: int, owner_token: str, *, sync: bool):
    del sync
    with app.state.session_factory() as session:
        job = session.get(ResearchJob, job_id)
        assert job is not None
        job.status = "done"
        release(
            session,
            job_id,
            JOB_TYPE_RESEARCH,
            owner_token=owner_token,
        )
        session.commit()
    return None


def _post(client: TestClient, run_id: int, track: str, code: str):
    return client.post(
        f"/api/discoveries/{run_id}/research",
        json={"track": track, "code": code, "sync": True},
    )


def test_migration_adds_partial_unique_discovery_candidate_index(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    with TestClient(app):
        with app.state.session_factory() as session:
            sql = session.scalar(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' "
                    "AND name = 'ux_research_discovery_candidate'"
                )
            )
            assert "WHERE discovery_run_id IS NOT NULL" in sql
            run = _run(session)
            _seed_finalists(
                session, run, {"quality_discount": ["600000.SH"]}
            )
            first = ResearchJob(
                user_id=1,
                discovery_run_id=run.id,
                discovery_track="quality_discount",
                name="浦发银行",
                code="600000.SH",
                status="failed",
                error="failed",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(first)
            session.commit()
            session.add(
                ResearchJob(
                    user_id=1,
                    discovery_run_id=run.id,
                    discovery_track="garp",
                    name="浦发银行",
                    code="600000.SH",
                    status="queued",
                    error=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()


@pytest.mark.parametrize(
    ("status", "keep", "evidence"),
    [
        ("waiting_confirmation", True, True),
        ("confirmed", False, True),
        ("confirmed", True, False),
    ],
)
def test_only_final_kept_candidate_with_evidence_can_start(
    tmp_path: Path,
    monkeypatch,
    status: str,
    keep: bool,
    evidence: bool,
) -> None:
    monkeypatch.setattr(
        discovery_routes,
        "_dispatch_discovery_research",
        _finish_dispatch,
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            _seed_finalists(
                session,
                run,
                {"quality_discount": ["600000.SH"]},
                status=status,
                keep=keep,
                evidence=evidence,
            )
            run_id = run.id
        response = _post(
            client, run_id, "quality_discount", "600000.SH"
        )
        assert response.status_code == 409
        assert response.json()["code"] == "DISCOVERY_CANDIDATE_NOT_RESEARCHABLE"
        with app.state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ResearchJob)) == 0


def test_created_job_has_provenance_and_does_not_add_watchlist(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        discovery_routes,
        "_dispatch_discovery_research",
        _finish_dispatch,
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            _seed_finalists(
                session, run, {"quality_discount": ["600519.SH"]}
            )
            run_id = run.id
        response = _post(
            client, run_id, "quality_discount", "600519.SH"
        )
        assert response.status_code == 200
        with app.state.session_factory() as session:
            job = session.get(ResearchJob, response.json()["id"])
            assert job is not None
            assert (
                job.discovery_run_id,
                job.discovery_track,
                job.code,
            ) == (run_id, "quality_discount", "600519.SH")
            assert session.scalar(select(func.count()).select_from(Watchlist)) == 0


def test_same_run_code_is_idempotent_across_tracks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        discovery_routes,
        "_dispatch_discovery_research",
        _finish_dispatch,
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            _seed_finalists(
                session,
                run,
                {
                    "quality_discount": ["600519.SH"],
                    "garp": ["600519.SH"],
                },
            )
            run_id = run.id
        first = _post(
            client, run_id, "quality_discount", "600519.SH"
        )
        second = _post(client, run_id, "garp", "600519.SH")
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        with app.state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ResearchJob)) == 1


def test_concurrent_same_code_requests_return_one_job(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        discovery_routes,
        "_dispatch_discovery_research",
        _finish_dispatch,
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            _seed_finalists(session, run, {"garp": ["600000.SH"]})
            run_id = run.id
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda _: _post(
                        client, run_id, "garp", "600000.SH"
                    ),
                    range(2),
                )
            )
        assert [response.status_code for response in responses] == [200, 200]
        assert len({response.json()["id"] for response in responses}) == 1
        with app.state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ResearchJob)) == 1


def test_failed_and_canceled_jobs_still_consume_weekly_quota(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        discovery_routes,
        "_dispatch_discovery_research",
        _finish_dispatch,
    )
    app = _app(tmp_path)
    codes = ["600000.SH", "600001.SH", "600002.SH", "600003.SH"]
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            _seed_finalists(session, run, {"garp": codes})
            for code, status in zip(codes[:3], ("failed", "canceled", "done")):
                session.add(
                    ResearchJob(
                        user_id=1,
                        discovery_run_id=run.id,
                        discovery_track="garp",
                        name=f"公司-{code}",
                        code=code,
                        status=status,
                        error=status if status == "failed" else None,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            session.commit()
            run_id = run.id
        rejected = _post(client, run_id, "garp", codes[3])
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "DISCOVERY_RESEARCH_QUOTA"


def test_concurrent_third_and_fourth_company_cannot_both_pass(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        discovery_routes,
        "_dispatch_discovery_research",
        _finish_dispatch,
    )
    app = _app(tmp_path)
    codes = ["600000.SH", "600001.SH", "600002.SH", "600003.SH"]
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            _seed_finalists(session, run, {"deep_value": codes})
            for code in codes[:2]:
                session.add(
                    ResearchJob(
                        user_id=1,
                        discovery_run_id=run.id,
                        discovery_track="deep_value",
                        name=f"公司-{code}",
                        code=code,
                        status="done",
                        error=None,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            session.commit()
            run_id = run.id
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda code: _post(
                        client, run_id, "deep_value", code
                    ),
                    codes[2:],
                )
            )
        assert sorted(response.status_code for response in responses) == [200, 409]
        with app.state.session_factory() as session:
            accepted = session.scalar(
                select(func.count(func.distinct(ResearchJob.code)))
                .join(
                    DiscoveryRun,
                    ResearchJob.discovery_run_id == DiscoveryRun.id,
                )
                .where(
                    DiscoveryRun.user_id == 1,
                    DiscoveryRun.week_key == china_week_key(NOW),
                )
            )
            assert accepted == 3


def test_current_week_mutation_guard_applies_to_research(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        discovery_routes,
        "_dispatch_discovery_research",
        _finish_dispatch,
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            _seed_finalists(session, run, {"garp": ["600000.SH"]})
            run.status = "abandoned"
            session.commit()
            run_id = run.id
        rejected = _post(client, run_id, "garp", "600000.SH")
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "DISCOVERY_RUN_IMMUTABLE"


def test_heavy_job_conflict_does_not_consume_a_slot(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        discovery_routes,
        "_dispatch_discovery_research",
        _finish_dispatch,
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            _seed_finalists(session, run, {"garp": ["600000.SH"]})
            assert try_acquire(session, 999, JOB_TYPE_FUNNEL)
            session.commit()
            run_id = run.id
        rejected = _post(client, run_id, "garp", "600000.SH")
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "JOB_LOCKED"
        with app.state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ResearchJob)) == 0
            release(session, 999, JOB_TYPE_FUNNEL)
            session.commit()


def test_dispatch_failure_marks_job_failed_and_releases_exact_lease(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        discovery_routes,
        "_start_discovery_worker",
        lambda *args, **kwargs: RuntimeError("thread unavailable"),
    )
    app = _app(tmp_path)
    app.state.allow_sync = False
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            _seed_finalists(
                session, run, {"quality_discount": ["600519.SH"]}
            )
            run_id = run.id
        failed = client.post(
            f"/api/discoveries/{run_id}/research",
            json={"track": "quality_discount", "code": "600519.SH"},
        )
        assert failed.status_code == 500
        assert failed.json()["code"] == "DISCOVERY_RESEARCH_DISPATCH_FAILED"
        with app.state.session_factory() as session:
            job = session.scalar(select(ResearchJob))
            assert job is not None
            assert job.status == "failed"
            assert lock_status(session) is None
        repeated = _post(
            client, run_id, "quality_discount", "600519.SH"
        )
        assert repeated.status_code == 200
        assert repeated.json()["id"] == job.id


def test_ordinary_research_keeps_discovery_provenance_null(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/research",
            json={
                "name": "招商银行",
                "code": "600036.SH",
                "sync": True,
            },
        )
        assert response.status_code == 200
        with app.state.session_factory() as session:
            job = session.get(ResearchJob, response.json()["id"])
            assert job is not None
            assert job.discovery_run_id is None
            assert job.discovery_track is None
