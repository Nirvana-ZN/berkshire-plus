from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.discovery_models import DiscoveryCommonStage, DiscoveryStage
from app.main import create_app
from jobs.discovery import confirm_waiting_layers
from market.quotes import FakeQuoteClient
from tests.test_discovery_research import TRACKS, _run
from tests.test_discovery_research import NOW


def test_mutation_routes_reject_human_rewrites(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "app.db",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        discovery_clock=lambda: NOW,
    )
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            run = _run(session)
            session.commit()
            run_id = run.id
        for path in (
            f"/api/discoveries/{run_id}/overrides",
            f"/api/discoveries/{run_id}/decisions",
            f"/api/discoveries/{run_id}/stages/safety/confirm",
            f"/api/discoveries/{run_id}/tracks/quality_discount/stages/track_gate/confirm",
        ):
            response = client.post(path, json={})
            assert response.status_code == 409
            assert response.json()["code"] == "DISCOVERY_RULE_ONLY"


def test_confirm_waiting_layers_queues_the_next_rule_stage(
    tmp_path: Path,
) -> None:
    app = create_app(
        db_path=tmp_path / "app.db",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        discovery_clock=lambda: NOW,
    )
    with TestClient(app):
        with app.state.session_factory() as session:
            run = _run(session)
            run.input_fingerprint = "fp-rule-only"
            common = DiscoveryCommonStage(
                run_id=run.id,
                attempt=1,
                status="waiting_confirmation",
                total_count=1,
                pass_count=1,
                drop_count=0,
                insufficient_count=0,
                error=None,
                confirmed_at=None,
                input_fingerprint="fp-rule-only",
                algorithm_version=run.algorithm_version,
                failure_kind=None,
                superseded_at=None,
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(common)
            session.flush()
            queued = confirm_waiting_layers(session, run, NOW)
            session.commit()
            assert queued is True
            session.refresh(common)
            assert common.status == "confirmed"
            children = session.scalars(
                select(DiscoveryStage).where(
                    DiscoveryStage.run_id == run.id,
                    DiscoveryStage.stage == "track_gate",
                )
            ).all()
            assert {row.track for row in children} == set(TRACKS)
            assert all(row.status == "queued" for row in children)
