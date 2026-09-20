from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.main import create_app
from app.models import RulerVersion
from ledger.positions import LOCAL_USER_ID
from market.eastmoney import FakeMarketClient
from market.quotes import FakeQuoteClient
from screen.ruler import (
    RulerError,
    activate,
    current_hash,
    latest,
    require_matched,
)

ROOT = Path(__file__).resolve().parents[1]


def _client(tmp_path: Path):
    return TestClient(
        create_app(
            db_path=tmp_path / "app.db",
            env_path=tmp_path / ".env",
            quote_client=FakeQuoteClient(),
            market_client=FakeMarketClient(),
            allow_sync=True,
        )
    )


def _add_stale(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ruler_versions (user_id, digest, reason, payload_json, activated_at) "
                "VALUES (:user_id, :digest, :reason, :payload, :activated_at)"
            ),
            {
                "user_id": LOCAL_USER_ID,
                "digest": "stale-digest",
                "reason": "旧版",
                "payload": "{}",
                "activated_at": datetime.now(timezone.utc).isoformat(),
            },
        )


def test_fresh_db_matches_and_funnel_starts(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        body = client.get("/api/ruler").json()
        assert set(body) == {
            "digest",
            "matched",
            "active",
            "versions",
            "screen_id",
            "rows",
            "ruler_flips",
            "gate",
        }
        assert body["matched"] is True
        assert body["digest"] == current_hash()
        assert body["active"]["reason"] == "初始尺子"
        assert "scope" not in body["active"]
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False},
        )
        assert created.status_code == 200


def test_latest_defaults_to_funnel_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path):
        engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
        with Session(engine) as session:
            session.add(
                RulerVersion(
                    user_id=LOCAL_USER_ID,
                    scope="discovery:quality_discount",
                    digest="discovery",
                    reason="discovery only",
                    payload_json="{}",
                    activated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
            row = latest(session)
            assert row is not None
            assert row.scope == "funnel"
            assert row.digest == current_hash()


def test_screen_mutations_reject_non_funnel_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path):
        engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
        with Session(engine) as session:
            for action in (
                lambda: activate(
                    session,
                    "wrong owner",
                    "discovery:quality_discount",
                    payload={"forged": True},
                ),
                lambda: require_matched(
                    session,
                    "discovery:quality_discount",
                    payload={"forged": True},
                ),
            ):
                with pytest.raises(RulerError) as error:
                    action()
                assert error.value.code == "RULER_SCOPE"


def test_stale_ruler_blocks_until_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        _add_stale(db_path)
        listed = client.get("/api/ruler").json()
        assert listed["matched"] is False
        assert listed["versions"][0]["digest"] == "stale-digest"
        blocked = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False},
        )
        assert blocked.status_code == 400
        assert blocked.json()["code"] == "RULER_MISMATCH"
        empty = client.post("/api/ruler", json={"reason": "  "})
        assert empty.status_code == 400
        assert empty.json()["code"] == "RULER_REASON"
        still = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False},
        )
        assert still.status_code == 400
        activated = client.post("/api/ruler", json={"reason": "改 ROE 门槛"})
        assert activated.status_code == 200
        assert activated.json()["matched"] is True
        reasons = [row["reason"] for row in activated.json()["versions"]]
        assert "旧版" in reasons
        assert "初始尺子" in reasons
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False},
        )
        assert created.status_code == 200


def test_screen_view_asks_for_reason() -> None:
    source = (ROOT / "web/src/views/ScreenView.vue").read_text(encoding="utf-8")
    assert "/api/ruler" in source
    assert "改因" in source
    assert "RULER_MISMATCH" in source
