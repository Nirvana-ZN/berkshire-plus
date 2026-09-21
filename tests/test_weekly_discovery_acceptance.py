from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app.discovery_routes as discovery_routes
from app.envfile import OPENAI_API_KEY, write_env_value
from app.models import ResearchJob
from jobs.lock import JOB_TYPE_RESEARCH, release
from tests.discovery_helpers import SNAPSHOT_AT
from tests.test_discovery_overrides import _workflow_app


ROOT = Path(__file__).resolve().parents[1]
TRACKS = ("quality_discount", "deep_value", "garp")


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
        assert created.json()["algorithm_version"] == "discovery-v2"
        assert all(
            value == "ready"
            for value in created.json()["ruler_readiness"].values()
        )
        assert client.get(
            f"/api/discoveries/{run_id}/tracks/garp/stages/track_gate"
        ).status_code == 404

        safety = client.get(
            f"/api/discoveries/{run_id}/stages/safety"
        ).json()
        assert safety["current"]["status"] == "waiting_confirmation"
        assert client.post(
            f"/api/discoveries/{run_id}/stages/safety/confirm",
            json={"sync": True},
        ).status_code == 200

        frozen: dict[str, dict] = {}
        for track in TRACKS:
            gate_path = (
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/track_gate"
            )
            gate = client.get(gate_path).json()
            assert gate["current"]["status"] == "waiting_confirmation"
            assert gate["current"]["ruler_version_id"] is not None
            assert client.post(
                f"{gate_path}/confirm", json={"sync": True}
            ).status_code == 200

            competition_path = (
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/competition"
            )
            competition = client.get(competition_path).json()
            assert competition["current"]["status"] == "waiting_confirmation"
            excluded_code = competition["pass"][-1]["code"]
            excluded = client.post(
                f"/api/discoveries/{run_id}/overrides",
                json={
                    "track": track,
                    "stage": "competition",
                    "code": excluded_code,
                    "action": "forced_drop",
                    "reason": "seeded competition exclusion",
                },
            )
            assert excluded.status_code == 200
            assert client.post(
                f"{competition_path}/confirm", json={"sync": True}
            ).status_code == 200
            confirmed_competition = client.get(competition_path).json()
            assert [row["code"] for row in confirmed_competition["drop"]] == [
                excluded_code
            ]

            adjudication_path = (
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/adjudication"
            )
            adjudication = client.get(adjudication_path).json()
            assert adjudication["current"]["status"] == "waiting_confirmation"
            premature = client.post(
                f"{adjudication_path}/confirm", json={}
            )
            assert premature.status_code == 409
            assert premature.json()["code"] == "DISCOVERY_DECISIONS_INCOMPLETE"

            candidates = confirmed_competition["pass"]
            assert len(candidates) == 5
            for index, row in enumerate(candidates):
                decision = "keep" if index < 3 else "exclude"
                saved = client.post(
                    f"/api/discoveries/{run_id}/decisions",
                    json={
                        "track": track,
                        "code": row["code"],
                        "decision": decision,
                        "reason": (
                            "seeded adjudication exclusion"
                            if decision == "exclude"
                            else None
                        ),
                    },
                )
                assert saved.status_code == 200
            assert client.post(
                f"{adjudication_path}/confirm", json={}
            ).status_code == 200
            frozen[track] = client.get(adjudication_path).json()
            assert frozen[track]["current"]["status"] == "confirmed"
            assert len(frozen[track]["pass"]) == 3
            assert len(frozen[track]["drop"]) == 2
            assert len(frozen[track]["decisions"]) == 5
        return run_id, frozen

    with TestClient(app) as client:
        prior_run_id, _ = complete_week(client)
        now[0] += timedelta(days=7)
        run_id, frozen = complete_week(client)
        assert run_id != prior_run_id
        for payload in frozen.values():
            assert payload["changes"]
            assert all(
                change["membership"] == "continuous"
                for change in payload["changes"]
            )

        started = client.post(
            f"/api/discoveries/{run_id}/research",
            json={
                "track": "garp",
                "code": frozen["garp"]["pass"][0]["code"],
                "sync": True,
            },
        )
        assert started.status_code == 200
        assert dispatches == [(started.json()["id"], True)]
        assert client.get(
            f"/api/research/{started.json()['id']}"
        ).json()["status"] == "done"

    with TestClient(app) as reloaded:
        current = reloaded.get("/api/discoveries/current").json()
        assert current["id"] == run_id
        assert current["changes"]
        for track in TRACKS:
            restored = reloaded.get(
                f"/api/discoveries/{run_id}/tracks/{track}"
                "/stages/adjudication"
            ).json()
            assert restored["current"]["status"] == "confirmed"
            assert restored["pass"] == frozen[track]["pass"]
            assert restored["drop"] == frozen[track]["drop"]
            assert restored["decisions"] == frozen[track]["decisions"]
            assert restored["changes"] == frozen[track]["changes"]

    discovery_view = (
        ROOT / "web/src/views/DiscoveryView.vue"
    ).read_text(encoding="utf-8")
    assert "本周变化" in discovery_view
    assert "上周状态" in discovery_view
    assert "/research" in discovery_view
    assert "validateCandidateEvidence" in discovery_view
