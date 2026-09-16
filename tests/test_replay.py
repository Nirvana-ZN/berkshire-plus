from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.envfile import OPENAI_API_KEY, write_env_value
from app.main import create_app
from app.models import ScreenResult, ScreenRun, Setting
from ledger.positions import LOCAL_USER_ID
from market.eastmoney import FakeMarketClient
from market.quotes import FakeQuoteClient
from market.types import Constituent, FinancialRow
from research.llm import FakeLLM
from screen.replay import apply_credibility_gate, dump_snapshot
from screen.ruler import GATE_ACK_KEY

ROOT = Path(__file__).resolve().parents[1]


def _client(tmp_path: Path, *, llm: FakeLLM | None = None):
    return TestClient(
        create_app(
            db_path=tmp_path / "app.db",
            env_path=tmp_path / ".env",
            quote_client=FakeQuoteClient(),
            market_client=FakeMarketClient(),
            llm_client=llm or FakeLLM(),
            reports_dir=tmp_path / "reports",
            allow_sync=True,
        )
    )


def _annual(code: str) -> list[FinancialRow]:
    return [
        FinancialRow(
            code=code,
            period=str(2016 + offset),
            period_type="annual",
            roe=Decimal("20"),
            net_income=Decimal("100"),
            ocf=Decimal("100"),
            capex=Decimal("20"),
            ebit=Decimal("40"),
            interest=Decimal("5"),
            gross_margin=Decimal("40"),
            net_margin=Decimal("10"),
            shares=Decimal("100"),
            share_change_ma=False,
        )
        for offset in range(10)
    ]


def _keepers() -> tuple[list[Constituent], list[FinancialRow]]:
    snaps = [
        Constituent("600036.SH", "甲", Decimal("300"), Decimal("8"), "银行"),
        Constituent("600519.SH", "乙", Decimal("200"), Decimal("8"), "白酒"),
        Constituent("000333.SZ", "丙", Decimal("180"), Decimal("8"), "家电"),
    ]
    rows = _annual("600036.SH") + _annual("600519.SH") + _annual("000333.SZ")
    return snaps, rows


def _seed_run(session, *, digest: str | None, verdict: str) -> None:
    snaps, rows = _keepers()
    payload = dump_snapshot(snaps, rows, {"600036.SH": 2002}, set())
    if digest is not None:
        payload["ruler_digest"] = digest
    now = datetime.now(timezone.utc)
    run = ScreenRun(
        user_id=LOCAL_USER_ID,
        scope_type="行业",
        scope_text="银行",
        status="done",
        error=None,
        snapshot_json=json.dumps(payload, ensure_ascii=False),
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    session.flush()
    for snap in snaps:
        session.add(
            ScreenResult(
                screen_run_id=run.id,
                code=snap.code,
                name=snap.name,
                verdict=verdict,
                reason="seed",
                metrics_json=json.dumps({"finalist": False}, ensure_ascii=False),
            )
        )
    from jobs.funnel import persist_financials

    persist_financials(session, rows)
    session.commit()


def _clear_ack(session) -> None:
    row = session.get(Setting, GATE_ACK_KEY)
    if row is not None:
        session.delete(row)
        session.commit()


def test_ruler_flips_open_gate_until_ack(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        factory = client.app.state.session_factory
        with factory() as session:
            _seed_run(session, digest="old", verdict="drop")
            _clear_ack(session)
        body = client.get("/api/ruler").json()
        assert body["gate"] is True
        assert body["ruler_flips"] == 3
        assert {row["cause"] for row in body["rows"]} == {"ruler"}
        accepted = client.post("/api/ruler", json={"reason": "接受这次偏差"})
        assert accepted.status_code == 200
        assert accepted.json()["gate"] is False


def test_data_flips_do_not_open_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        factory = client.app.state.session_factory
        with factory() as session:
            _seed_run(session, digest=None, verdict="keep")
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
        with engine.begin() as conn:
            conn.execute(text("UPDATE financials SET roe = 1"))
        body = client.get("/api/ruler").json()
        assert body["gate"] is False
        assert body["rows"]
        assert all(row["cause"] == "data" for row in body["rows"])


def test_list_snapshot_is_not_reconstructable(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        factory = client.app.state.session_factory
        with factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                ScreenRun(
                    user_id=LOCAL_USER_ID,
                    scope_type="行业",
                    scope_text="银行",
                    status="done",
                    error=None,
                    snapshot_json=json.dumps([{"name": "甲", "industry": "银行"}]),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
        body = client.get("/api/ruler").json()
        assert body["rows"] == []
        assert body["gate"] is False


def test_apply_gate_demotes_pass_and_bands() -> None:
    structured = {
        "verdict": "pass",
        "price_bands": {"mode": "steady", "aggressive": "a", "steady": "s", "conservative": "c"},
    }
    apply_credibility_gate(structured)
    assert structured["verdict"] == "gray"
    assert structured["price_bands"]["mode"] == "watch"


def test_open_gate_blocks_new_eval_pass(tmp_path: Path) -> None:
    write_env_value(tmp_path / ".env", OPENAI_API_KEY, "sk-test-replay")
    with _client(tmp_path) as client:
        factory = client.app.state.session_factory
        with factory() as session:
            _seed_run(session, digest="old", verdict="drop")
            _clear_ack(session)
        created = client.post(
            "/api/research",
            json={"name": "甲", "code": "600036.SH", "sync": True},
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        body = client.get(f"/api/research/{job_id}").json()
        assert body["status"] == "done"
        assert body["structured"]["verdict"] != "pass"
        assert body["structured"]["price_bands"]["mode"] == "watch"


def test_screen_view_shows_replay() -> None:
    source = (ROOT / "web/src/views/ScreenView.vue").read_text(encoding="utf-8")
    assert "尺子偏了" in source
    assert "可信度闸" in source
    assert "snapshot_json" not in source
