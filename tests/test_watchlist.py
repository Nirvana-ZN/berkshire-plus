from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from market.quotes import FakeQuoteClient

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "research"


def _engine(db_path: Path):
    return create_engine(f"sqlite:///{db_path.resolve().as_posix()}")


def _scalar(db_path: Path, sql: str, params: dict | None = None):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _rows(db_path: Path, sql: str, params: dict | None = None):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params or {}).mappings())


def _assert_error_envelope(body: dict) -> None:
    assert set(body) >= {"error", "code", "detail"}
    assert body["error"]
    assert body["code"]
    assert body["detail"]


def _client(tmp_path: Path):
    return TestClient(
        create_app(
            db_path=tmp_path / "app.db",
            quote_client=FakeQuoteClient(),
            env_path=tmp_path / ".env",
        )
    )


def _seed_cash(db_path: Path, balance: str = "10000") -> None:
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cash (user_id, balance, currency) "
                "VALUES (1, :balance, 'CNY')"
            ),
            {"balance": balance},
        )


def _seed_research(
    db_path: Path,
    *,
    name: str,
    code: str = "600036.SH",
    status: str = "done",
    verdict: str | None = "pass",
    structured: dict | None = None,
) -> int:
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO research_jobs "
                "(user_id, name, code, status, created_at, updated_at) "
                "VALUES (1, :name, :code, :status, '2026-08-29', '2026-08-29')"
            ),
            {"name": name, "code": code, "status": status},
        )
        job_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
        assert job_id is not None
        if verdict is not None or structured is not None:
            body = dict(structured or {})
            if verdict is not None:
                body["verdict"] = verdict
            conn.execute(
                text(
                    "INSERT INTO research_artifacts "
                    "(research_job_id, structured_json, markdown) "
                    "VALUES (:job_id, :payload, :markdown)"
                ),
                {
                    "job_id": job_id,
                    "payload": json.dumps(body, ensure_ascii=False),
                    "markdown": "假评估",
                },
            )
        return int(job_id)


def _trade_count(db_path: Path) -> int:
    return int(_scalar(db_path, "SELECT COUNT(*) FROM trades") or 0)


def _watchlist_count(db_path: Path) -> int:
    return int(_scalar(db_path, "SELECT COUNT(*) FROM watchlist") or 0)


def test_pass_research_joins_watchlist_without_trade(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        research_id = _seed_research(db_path, name="优质大盘", verdict="pass")
        before_trades = _trade_count(db_path)
        before_positions = client.get("/api/positions").json()
        response = client.post("/api/watchlist", json={"research_id": research_id})
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "优质大盘"
        assert body["code"] == "600036.SH"
        assert body["research_id"] == research_id
        listed = client.get("/api/watchlist").json()
        assert [item["name"] for item in listed["items"]] == ["优质大盘"]
        jobs = client.get("/api/jobs").json()["jobs"]
        assert any(item.get("name") == "优质大盘" for item in jobs)
        after_positions = client.get("/api/positions").json()
        assert after_positions["positions"] == before_positions["positions"]
        assert after_positions["cash"] == before_positions["cash"]
    assert _watchlist_count(db_path) == 1
    assert _trade_count(db_path) == before_trades


def test_gray_research_joins_watchlist_without_trade(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        research_id = _seed_research(db_path, name="灰色候选", verdict="gray")
        before_trades = _trade_count(db_path)
        response = client.post("/api/watchlist", json={"research_id": research_id})
        assert response.status_code == 200
        assert response.json()["name"] == "灰色候选"
        listed = client.get("/api/watchlist").json()["items"]
        assert listed[0]["name"] == "灰色候选"
    assert _watchlist_count(db_path) == 1
    assert _trade_count(db_path) == before_trades


def test_fail_research_is_rejected_and_book_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path)
        before = client.get("/api/positions").json()
        research_id = _seed_research(db_path, name="不通过票", verdict="fail")
        response = client.post(
            "/api/watchlist",
            json={"research_id": research_id, "verdict": "pass", "name": "伪装通过"},
        )
        assert response.status_code == 400
        _assert_error_envelope(response.json())
        assert response.json()["code"] == "WATCHLIST_REJECTED"
        after = client.get("/api/positions").json()
        assert after["positions"] == before["positions"]
        assert after["cash"] == before["cash"]
        assert client.get("/api/watchlist").json()["items"] == []
    assert _watchlist_count(db_path) == 0
    assert _trade_count(db_path) == 0


def test_duplicate_name_is_409_and_stays_one_row(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        first = _seed_research(db_path, name="同名票", verdict="pass")
        second = _seed_research(db_path, name="同名票", code="000333.SZ", verdict="gray")
        assert client.post("/api/watchlist", json={"research_id": first}).status_code == 200
        before_trades = _trade_count(db_path)
        again = client.post("/api/watchlist", json={"research_id": second})
        assert again.status_code == 409
        _assert_error_envelope(again.json())
        assert again.json()["code"] == "WATCHLIST_DUPLICATE"
        listed = client.get("/api/watchlist").json()["items"]
        assert len(listed) == 1
        assert listed[0]["research_id"] == first
    assert _watchlist_count(db_path) == 1
    assert _trade_count(db_path) == before_trades


def test_incomplete_or_no_verdict_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        running = _seed_research(
            db_path, name="进行中", status="running", verdict=None, structured=None
        )
        unfinished = client.post("/api/watchlist", json={"research_id": running})
        assert unfinished.status_code == 400
        _assert_error_envelope(unfinished.json())
        empty = _seed_research(db_path, name="无结论", status="done", verdict=None)
        missing = client.post("/api/watchlist", json={"research_id": empty})
        assert missing.status_code == 400
        _assert_error_envelope(missing.json())
        assert client.get("/api/watchlist").json()["items"] == []
    assert _watchlist_count(db_path) == 0
    assert _trade_count(db_path) == 0


def test_prefill_does_not_write_trades(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        research_id = _seed_research(db_path, name="预填票", verdict="pass")
        assert client.post("/api/watchlist", json={"research_id": research_id}).status_code == 200
        before = _trade_count(db_path)
        book = client.get("/api/positions").json()
        assert "预填票" not in {row["name"] for row in book["positions"]}
        listed = client.get("/api/watchlist").json()["items"]
        assert listed[0]["name"] == "预填票"
    assert _trade_count(db_path) == before == 0
    source = (ROOT / "web/src/views/PositionsView.vue").read_text(encoding="utf-8")
    assert "route.query.name" in source
    assert "form.value.name = rawName" in source
    assert "applyPrefill" in source
    mounted = source.split("onMounted(async () => {")[1].split("})")[0]
    assert "/api/trades" not in mounted
    assert "submitTrade" not in mounted
    assert "applyPrefill" in mounted


def test_prefill_then_hand_record_uses_existing_trades(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path)
        research_id = _seed_research(db_path, name="招商银行", verdict="pass")
        assert client.post("/api/watchlist", json={"research_id": research_id}).status_code == 200
        assert _trade_count(db_path) == 0
        bought = client.post(
            "/api/trades",
            json={
                "date": "2026-08-29",
                "direction": "buy",
                "name": "招商银行",
                "shares": 100,
                "price": 10,
            },
        )
        assert bought.status_code == 200
        by_name = {row["name"]: row for row in bought.json()["positions"]}
        assert Decimal(str(by_name["招商银行"]["shares"])) == Decimal("100")
        exceeded = client.post(
            "/api/trades",
            json={
                "date": "2026-08-29",
                "direction": "sell",
                "name": "招商银行",
                "shares": 101,
                "price": 12,
            },
        )
        assert exceeded.status_code == 400
        _assert_error_envelope(exceeded.json())
        assert exceeded.json()["code"] == "SELL_EXCEEDS"
    assert _trade_count(db_path) == 1
    assert _watchlist_count(db_path) == 1


def test_delete_watchlist_does_not_trade(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        research_id = _seed_research(db_path, name="待删", verdict="gray")
        created = client.post("/api/watchlist", json={"research_id": research_id})
        item_id = created.json()["id"]
        before = _trade_count(db_path)
        deleted = client.delete(f"/api/watchlist/{item_id}")
        assert deleted.status_code == 200
        assert client.get("/api/watchlist").json()["items"] == []
    assert _watchlist_count(db_path) == 0
    assert _trade_count(db_path) == before


def test_name_copied_from_research_not_client(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        research_id = _seed_research(db_path, name="评估原名", verdict="pass")
        response = client.post(
            "/api/watchlist",
            json={"research_id": research_id, "name": "客户端改名", "code": "000001.SZ"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "评估原名"
        assert response.json()["code"] == "600036.SH"
    names = [row["name"] for row in _rows(db_path, "SELECT name FROM watchlist")]
    assert names == ["评估原名"]


def test_bool_research_id_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_research(db_path, name="优质大盘", verdict="pass")
        response = client.post("/api/watchlist", json={"research_id": True})
        assert response.status_code == 400
        _assert_error_envelope(response.json())
        assert response.json()["code"] == "INVALID_WATCHLIST"
    assert _watchlist_count(db_path) == 0
    for path in RESEARCH_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import ledger" not in source
        assert "from ledger" not in source


def test_views_prefill_and_watchlist_without_direct_book_write() -> None:
    research = (ROOT / "web/src/views/ResearchView.vue").read_text(encoding="utf-8")
    positions = (ROOT / "web/src/views/PositionsView.vue").read_text(encoding="utf-8")
    home = (ROOT / "web/src/views/HomeView.vue").read_text(encoding="utf-8")
    assert "加入观察池" in research
    assert "verdict.value === 'pass' || verdict.value === 'gray'" in research
    assert "/api/watchlist" in research
    assert "预填记账" in research
    assert "path: '/positions'" in research or 'path: "/positions"' in research
    assert "/api/trades" not in research
    assert "/api/targets" not in research
    assert "record_trade" not in research
    assert "route.query.name" in positions
    assert "form.value.name = rawName" in positions
    assert "/api/trades" in positions
    assert "submitTrade" in positions
    assert "/api/health" in home
    assert "/api/jobs" in home
    assert "/api/watchlist" in home
    assert "watchBody.items" in home
    assert "jobsBody.jobs" in home
    assert "观察池待办" in home
    assert "thesis_status" in home
    assert "thesis_research_id != null" in home
    assert "无论文" in home
    assert "任务锁" in home
    assert "最近任务" in home
    assert "预填记账" in home
    assert "loadDesk" not in home
    assert "/api/trades" not in home
