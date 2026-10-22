from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import MappingProxyType

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.discovery_routes as discovery_routes
from app.envfile import OPENAI_API_KEY, write_env_value
from app.models import ResearchJob
from discovery.builds import BUILD_VERSION
from discovery.snapshot import FrozenRunInputs
from jobs.lock import JOB_TYPE_RESEARCH, release
from tests.discovery_helpers import SNAPSHOT_AT
from tests.test_discovery_overrides import _workflow_app


ROOT = Path(__file__).resolve().parents[1]
TRACKS = ("quality_discount", "deep_value", "garp")


def test_production_v2_classifiers_and_dispatch_diverge_by_track() -> None:
    from discovery.rulers import (
        deep_value_ruler,
        garp_ruler,
        quality_discount_ruler,
    )
    from discovery.tracks.deep_value import classify as classify_deep_value
    from discovery.tracks.garp import classify as classify_garp
    from discovery.tracks.quality_discount import (
        classify as classify_quality_discount,
    )
    from jobs.discovery import (
        TrackExecutionCandidate,
        TrackExecutionContext,
        builtin_track_executor,
    )
    from tests.test_discovery_tracks_deep_value import deep_value_payload
    from tests.test_discovery_tracks_garp import (
        _company,
        garp_payload,
    )
    from tests.test_discovery_tracks_quality import quality_payload

    company = _company()
    payloads = {
        "quality_discount": quality_payload(),
        "deep_value": deep_value_payload(normalized_pe_max="8"),
        "garp": garp_payload(),
    }
    classifiers = {
        "quality_discount": (
            quality_discount_ruler,
            classify_quality_discount,
        ),
        "deep_value": (deep_value_ruler, classify_deep_value),
        "garp": (garp_ruler, classify_garp),
    }
    pure_verdicts = {
        track: classify(company, load_ruler(payloads[track])).verdict
        for track, (load_ruler, classify) in classifiers.items()
    }
    assert pure_verdicts == {
        "quality_discount": "pass",
        "deep_value": "drop",
        "garp": "pass",
    }
    assert {
        payload["algorithm_version"] for payload in payloads.values()
    } == {BUILD_VERSION}

    frozen = FrozenRunInputs(
        run_id=1,
        input_fingerprint="same-frozen-company",
        data_version="data-v1",
        financial_version="financial-v1",
        valuation_version="valuation-v1",
        ruler_version_ids=MappingProxyType(
            {track: index for index, track in enumerate(TRACKS, 1)}
        ),
        ruler_payloads=MappingProxyType({}),
        algorithm_version=BUILD_VERSION,
        trading_dates=MappingProxyType({"SH": ("2026-09-17",)}),
        snapshot_at=company.snapshot_at,
        companies=(company,),
    )
    candidate = TrackExecutionCandidate(
        code=company.code,
        persisted_verdict="pass",
        effective_verdict="pass",
        reason=None,
        metrics_json="{}",
        rank=None,
    )
    dispatched = {}
    for ruler_id, track in enumerate(TRACKS, 1):
        outcome = builtin_track_executor(
            TrackExecutionContext(
                stage_id=ruler_id,
                run_id=1,
                track=track,
                stage="track_gate",
                attempt=1,
                input_fingerprint=frozen.input_fingerprint,
                ruler_version_id=ruler_id,
                algorithm_version=BUILD_VERSION,
                common_stage_id=1,
                predecessor_stage_id=None,
                predecessor_stage=None,
                frozen_inputs=frozen,
                ruler_payload=MappingProxyType(payloads[track]),
                candidates=(candidate,),
            )
        )
        assert outcome.status == "waiting_confirmation"
        dispatched[track] = outcome.results[0].verdict
    assert dispatched == pure_verdicts


def test_seeded_weekly_discovery_full_flow_survives_reload(
    tmp_path: Path, monkeypatch
) -> None:
    now = [SNAPSHOT_AT]
    dispatches: list[tuple[int, bool]] = []

    def forbid_external_call(*_args, **_kwargs):
        raise AssertionError("acceptance test attempted a real external call")

    def finish_stubbed_research(app, job_id: int, owner_token: str, *, sync: bool):
        dispatches.append((job_id, sync))
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

    monkeypatch.setattr(
        "market.eastmoney.EastMoneyClient._get_json",
        forbid_external_call,
    )
    monkeypatch.setattr(
        discovery_routes,
        "_dispatch_discovery_research",
        finish_stubbed_research,
    )
    write_env_value(tmp_path / ".env", OPENAI_API_KEY, "sk-seeded-test")
    app = _workflow_app(tmp_path, clock=lambda: now[0])

    def complete_week(client: TestClient) -> tuple[int, dict[str, dict]]:
        created = client.post("/api/discoveries", json={"sync": True})
        assert created.status_code == 200
        run_id = created.json()["id"]
        assert created.json()["algorithm_version"] == "discovery-v7"
        assert all(
            value == "ready"
            for value in created.json()["ruler_readiness"].values()
        )
        safety = client.get(
            f"/api/discoveries/{run_id}/stages/safety"
        ).json()
        assert safety["current"]["status"] == "confirmed"
        blocked = client.post(
            f"/api/discoveries/{run_id}/stages/safety/confirm",
            json={"sync": True},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "DISCOVERY_RULE_ONLY"

        frozen: dict[str, dict] = {}
        for track in TRACKS:
            gate_path = (
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/track_gate"
            )
            gate = client.get(gate_path).json()
            assert gate["current"]["status"] == "confirmed"
            assert gate["current"]["ruler_version_id"] is not None

            competition_path = (
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/competition"
            )
            competition = client.get(competition_path).json()
            assert competition["current"]["status"] == "confirmed"
            assert client.post(
                f"{competition_path}/confirm", json={"sync": True}
            ).json()["code"] == "DISCOVERY_RULE_ONLY"
            assert client.get(
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/adjudication"
            ).status_code == 404
            frozen[track] = competition
        return run_id, frozen

    with TestClient(app) as client:
        prior_run_id, _ = complete_week(client)
        now[0] += timedelta(days=7)
        run_id, frozen = complete_week(client)
        assert run_id != prior_run_id
        for track, payload in frozen.items():
            changes = {
                change["code"]: change for change in payload["changes"]
            }
            assert set(changes) == {
                row["code"] for row in payload["pass"]
            }
            for change in changes.values():
                expected = {
                    "track": track,
                    "stage": "competition",
                    "previous_run_id": prior_run_id,
                    "current_run_id": run_id,
                    "previous_week_key": "2026-W38",
                    "current_week_key": "2026-W39",
                    "previous_state": "pass",
                    "current_state": "pass",
                    "membership": "continuous",
                    "cause": None,
                    "reason_changed": False,
                    "classification": "continuous",
                    "classifications": ["continuous"],
                    "previous_reason": None,
                    "current_reason": None,
                }
                assert {
                    key: change[key] for key in expected
                } == expected

        quality_codes = [
            row["code"] for row in frozen["quality_discount"]["pass"]
        ]

        def start_research(code: str):
            return client.post(
                f"/api/discoveries/{run_id}/research",
                json={
                    "track": "quality_discount",
                    "code": code,
                    "sync": True,
                },
            )

        started = start_research(quality_codes[0])
        repeated = start_research(quality_codes[0])
        second = start_research(quality_codes[1])
        third = start_research(quality_codes[2])
        quota = start_research(quality_codes[3])
        assert started.status_code == repeated.status_code == 200
        assert repeated.json()["id"] == started.json()["id"]
        assert second.status_code == third.status_code == 200
        assert quota.status_code == 409
        assert quota.json()["code"] == "DISCOVERY_RESEARCH_QUOTA"
        assert dispatches == [
            (started.json()["id"], True),
            (second.json()["id"], True),
            (third.json()["id"], True),
        ]

        research = client.get(
            f"/api/research/{started.json()['id']}"
        ).json()
        assert research["status"] == "done"
        assert research["discovery_run_id"] == run_id
        assert research["discovery_track"] == "quality_discount"
        assert research["code"] == quality_codes[0]
        with app.state.session_factory() as session:
            jobs = session.scalars(
                select(ResearchJob)
                .where(ResearchJob.discovery_run_id == run_id)
                .order_by(ResearchJob.id)
            ).all()
            assert [
                (job.discovery_run_id, job.discovery_track, job.code)
                for job in jobs
            ] == [
                (run_id, "quality_discount", code)
                for code in quality_codes[:3]
            ]

    with TestClient(app) as reloaded:
        current = reloaded.get("/api/discoveries/current").json()
        assert current["id"] == run_id
        assert current["changes"]
        for track in TRACKS:
            restored = reloaded.get(
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/competition"
            ).json()
            assert restored["current"]["status"] == "confirmed"
            assert restored["pass"] == frozen[track]["pass"]
            assert restored["drop"] == frozen[track]["drop"]
            assert restored["changes"] == frozen[track]["changes"]

    discovery_view = (
        ROOT / "web/src/views/DiscoveryView.vue"
    ).read_text(encoding="utf-8")
    assert "本周变化" in discovery_view
    assert "上周状态" in discovery_view
    assert "/research" in discovery_view
    assert "validateCandidateEvidence" in discovery_view
