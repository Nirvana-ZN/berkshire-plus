from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.envfile import OPENAI_API_KEY, write_env_value
from app.main import create_app
from market.eastmoney import FakeMarketClient
from market.quotes import FakeQuoteClient
from research.llm import FakeLLM
from tests.test_screen import _assert_error_envelope, _success_market

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "research"
SCREEN_DIR = ROOT / "screen"
INVENTED = "98765.43"

REQUIRED = {
    "verdict",
    "gates",
    "mirror_test",
    "price_bands",
    "memo",
    "data_grade",
    "as_of",
    "portfolio_fit",
}


def _engine(db_path: Path):
    return create_engine(f"sqlite:///{db_path.resolve().as_posix()}")


def _scalar(db_path: Path, sql: str, params: dict | None = None):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _rows(db_path: Path, sql: str):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return list(conn.execute(text(sql)).mappings())


def _quotes() -> FakeQuoteClient:
    return FakeQuoteClient(
        {
            "600036.SH": "40",
            "600519.SH": "1500",
            "000333.SZ": "60",
        }
    )


def _client(
    tmp_path: Path,
    *,
    market: FakeMarketClient | None = None,
    quote: FakeQuoteClient | None = None,
    llm: FakeLLM | None = None,
    env_name: str = ".env",
):
    return TestClient(
        create_app(
            db_path=tmp_path / "app.db",
            env_path=tmp_path / env_name,
            quote_client=quote if quote is not None else _quotes(),
            market_client=market if market is not None else _success_market(),
            llm_client=llm if llm is not None else FakeLLM(),
            reports_dir=tmp_path / "reports",
            allow_sync=True,
        )
    )


def _write_key(tmp_path: Path) -> None:
    write_env_value(tmp_path / ".env", OPENAI_API_KEY, "sk-test-research")


def _assert_structured(body: dict) -> None:
    assert REQUIRED <= set(body)
    assert body["verdict"] in {"pass", "fail", "gray"}
    assert len(body["gates"]) == 6
    assert {row["name"] for row in body["gates"]} == {
        "能力圈",
        "好生意",
        "护城河",
        "管理层",
        "安全边际",
        "决策纪律",
    }
    for row in body["gates"]:
        assert row["status"] in {"pass", "fail", "gray"}
        assert row["reason"]
    assert isinstance(body["mirror_test"], list)
    assert set(body["portfolio_fit"]) >= {"should_enter", "funding", "overlap"}
    assert set(body["memo"]) >= {"essence", "risks", "role"}
    assert body["memo"]["role"] in {"core", "satellite", "skip"}
    assert body["price_bands"]["mode"] in {"aggressive", "steady", "conservative", "watch"}
    assert body["data_grade"] in {"A", "B", "C"}
    assert "date" in body["as_of"]


def test_default_evaluate_writes_structured_and_markdown(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    reports = tmp_path / "reports"
    with _client(tmp_path) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "sync": True},
        )
        assert created.status_code == 200
        screen_id = created.json()["id"]
        data = client.get(f"/api/screens/{screen_id}").json()
        assert data["status"] == "done"
        jobs = data["jobs"]
        assert 1 <= len(jobs) <= 3
        assert len(jobs) == len(data["finalists"])
        assert all(job["status"] == "done" for job in jobs)
        listed = client.get("/api/jobs").json()["jobs"]
        assert any(item.get("type") == "research" for item in listed)
        first = client.get(f"/api/research/{jobs[0]['id']}")
        assert first.status_code == 200
        body = first.json()
        assert body["status"] == "done"
        _assert_structured(body["structured"])
        for key in REQUIRED:
            assert key in body
        assert "结论" in body["markdown"]
        assert "对照当前仓" in body["markdown"]
        again = client.get(f"/api/research/{jobs[0]['id']}")
        assert again.status_code == 200
        assert again.json()["markdown"] == body["markdown"]
        assert again.json()["structured"] == body["structured"]

    assert _scalar(db_path, "SELECT COUNT(*) FROM research_jobs") == len(jobs)
    stored = json.loads(_rows(db_path, "SELECT structured_json FROM research_artifacts")[0]["structured_json"])
    _assert_structured(stored)
    md_files = list(reports.glob("*.md"))
    assert md_files
    assert "决策备忘录" in md_files[0].read_text(encoding="utf-8")


def test_evaluate_without_key_is_400_and_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(OPENAI_API_KEY, raising=False)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": True, "sync": True},
        )
        assert created.status_code == 400
        body = created.json()
        _assert_error_envelope(body)
        assert "设置" in body["detail"]
        standalone = client.post(
            "/api/research",
            json={"name": "优质大盘", "code": "600036.SH", "sync": True},
        )
        assert standalone.status_code == 400
        _assert_error_envelope(standalone.json())
        assert "设置" in standalone.json()["detail"]
    assert _scalar(db_path, "SELECT COUNT(*) FROM screen_runs") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM research_jobs") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM research_artifacts") == 0


def test_screen_only_without_key_has_no_research_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(OPENAI_API_KEY, raising=False)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False, "sync": True},
        )
        assert created.status_code == 200
        data = client.get(f"/api/screens/{created.json()['id']}").json()
        assert data["status"] == "done"
        assert data["keep"]
        assert data["jobs"] == []
    assert _scalar(db_path, "SELECT COUNT(*) FROM research_jobs") == 0


def test_missing_quote_is_gray_without_invented_numbers(tmp_path: Path) -> None:
    _write_key(tmp_path)
    llm = FakeLLM(
        {
            "verdict": "pass",
            "gates": [
                {"name": name, "status": "pass", "reason": f"捏造 {INVENTED}"}
                for name in ("能力圈", "好生意", "护城河", "管理层", "安全边际", "决策纪律")
            ],
            "mirror_test": ["一", "二", "三", "四", "五"],
            "price_bands": {"mode": "steady", "aggressive": INVENTED, "steady": "a", "conservative": "b"},
            "memo": {"essence": INVENTED, "risks": "r", "role": "core"},
            "data_grade": "A",
            "as_of": {"date": "2020-01-01", "price": INVENTED},
            "portfolio_fit": {
                "should_enter": INVENTED,
                "funding": INVENTED,
                "overlap": INVENTED,
            },
        }
    )
    with _client(tmp_path, quote=FakeQuoteClient(), llm=llm) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": True, "sync": True},
        )
        data = client.get(f"/api/screens/{created.json()['id']}").json()
        assert data["jobs"]
        got = client.get(f"/api/research/{data['jobs'][0]['id']}").json()
        assert got["verdict"] == "gray"
        dumped = json.dumps(got, ensure_ascii=False)
        assert INVENTED not in dumped
        assert INVENTED not in got["markdown"]


def test_no_web_search_does_not_say_no_financials(tmp_path: Path) -> None:
    _write_key(tmp_path)
    with _client(tmp_path) as client:
        listed = client.get("/api/settings").json()
        assert listed["web_search"] is False
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": True, "sync": True},
        )
        job_id = client.get(f"/api/screens/{created.json()['id']}").json()["jobs"][0]["id"]
        body = client.get(f"/api/research/{job_id}").json()
        assert body["status"] == "done"
        assert "无财务" not in body["markdown"]
        assert "已抓取财务" in body["markdown"]


def test_one_child_failure_keeps_funnel_list(tmp_path: Path) -> None:
    _write_key(tmp_path)
    llm = FakeLLM(fail_names=("弹性小盘",))
    with _client(tmp_path, llm=llm) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": True, "sync": True},
        )
        screen_id = created.json()["id"]
        data = client.get(f"/api/screens/{screen_id}").json()
        keep_names = [row["name"] for row in data["keep"]]
        assert "优质大盘" in keep_names
        assert "弹性小盘" in keep_names
        jobs = {row["name"]: row for row in data["jobs"]}
        assert jobs["弹性小盘"]["status"] == "failed"
        assert jobs["优质大盘"]["status"] == "done"
        failed = client.get(f"/api/research/{jobs['弹性小盘']['id']}").json()
        assert failed["status"] == "failed"
        assert failed["structured"] == {}


def test_incomplete_mirror_cannot_be_pass(tmp_path: Path) -> None:
    _write_key(tmp_path)
    llm = FakeLLM(
        {
            "verdict": "pass",
            "gates": [
                {"name": name, "status": "pass", "reason": "通过"}
                for name in ("能力圈", "好生意", "护城河", "管理层", "安全边际", "决策纪律")
            ],
            "mirror_test": ["只写了一句。", "第二句。"],
            "price_bands": {
                "mode": "steady",
                "aggressive": "a",
                "steady": "b",
                "conservative": "c",
            },
            "memo": {"essence": "生意", "risks": "风险", "role": "satellite"},
            "data_grade": "B",
            "as_of": {"date": "2026-08-29", "price": 40},
            "portfolio_fit": {
                "should_enter": "对照当前仓可以进来。",
                "funding": "用现金买。",
                "overlap": "不叠逻辑。",
            },
        }
    )
    with _client(tmp_path, llm=llm) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": True, "sync": True},
        )
        job_id = client.get(f"/api/screens/{created.json()['id']}").json()["jobs"][0]["id"]
        body = client.get(f"/api/research/{job_id}").json()
        assert body["verdict"] != "pass"
        assert len(body["mirror_test"]) < 5


def test_standalone_research_uses_same_lock(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    gate = Event()
    llm = FakeLLM(gate=gate)
    with _client(tmp_path, llm=llm) as client:
        assert client.get("/api/health").status_code == 200
        engine = _engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO financials (code, period, period_type, roe, net_income) "
                    "VALUES ('600036.SH', '2024', 'annual', 18, 100)"
                )
            )
        first = client.post(
            "/api/research",
            json={"name": "优质大盘", "code": "600036.SH"},
        )
        assert first.status_code == 200
        lock = None
        for _ in range(80):
            lock = client.get("/api/health").json()["lock"]
            if lock:
                break
            time.sleep(0.05)
        assert lock is not None
        assert lock["type"] == "funnel"
        second = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False},
        )
        assert second.status_code == 409
        _assert_error_envelope(second.json())
        gate.set()
        deadline = time.time() + 5
        status = ""
        job_id = first.json()["id"]
        while time.time() < deadline:
            status = client.get(f"/api/research/{job_id}").json()["status"]
            if status in {"done", "failed"}:
                break
            time.sleep(0.05)
        assert status == "done"
        assert client.get("/api/health").json()["lock"] is None


def test_research_does_not_import_ledger_and_screen_has_no_http() -> None:
    for path in RESEARCH_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import ledger" not in source
        assert "from ledger" not in source
    for path in SCREEN_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "httpx" not in source
        assert "openai" not in source.lower()


def test_screen_and_research_views_are_not_json() -> None:
    screen = (ROOT / "web/src/views/ScreenView.vue").read_text(encoding="utf-8")
    research = (ROOT / "web/src/views/ResearchView.vue").read_text(encoding="utf-8")
    router = (ROOT / "web/src/router/index.ts").read_text(encoding="utf-8")
    assert "评估终选" in screen
    assert "evaluate" in screen
    assert "评估子任务" in screen
    assert "/research/" in screen
    assert "结论条" in research
    assert "已立为论文" in research
    assert "已更新论文" in research
    assert "镜子测试" in research
    assert "对照当前仓" in research
    assert "决策备忘录" in research
    assert "structured_json" not in research
    assert "<pre" not in research.lower()
    assert 'path: \'/research/:id\'' in router or 'path: "/research/:id"' in router
    assert "status.value = 'failed'" in research


def test_positions_view_has_per_holding_research_entry() -> None:
    source = (ROOT / "web/src/views/PositionsView.vue").read_text(encoding="utf-8")
    assert "评估" in source
    assert "/api/research" in source
    assert "/research/" in source
    assert "startResearch" in source
    assert "useRouter" in source
    table = source.split('<table class="book"')[1].split("</table>")[0]
    assert "评估" in table
    assert "论文" in table
    assert "openThesis" in source
    assert "thesis_status" in source
    assert "<button" not in table.lower()
    assert "<a " not in table.lower()
    assert "form.shares" not in table
    cash = table.split('class="cash-row"')[1]
    assert "评估" not in cash
    assert "startResearch" not in cash
    assert "openThesis" not in cash
    assert "无论文" not in cash
    assert "一键审视" in source
    assert "短体检，非六关" in source
    assert "补代码" in source
    assert "保存目标" in source
    assert "一键应用" not in source
    fn = source.split("async function startResearch")[1].split("async function startReview")[0]
    assert "prefillCode" in fn
    assert "if (!String(row.code || '').trim())" in fn
    assert fn.index("prefillCode") < fn.index("fetch")
    assert "fetch('/api/research'" in fn
    assert "name: row.name" in fn
    assert "code: row.code" in fn
    assert "data.detail || data.error" in fn
    assert "router.push(`/research/${data.id}`)" in fn
    assert "researchError" in fn
    assert "RouterLink" not in table


def test_evaluate_holds_lock_until_children_finish(tmp_path: Path) -> None:
    _write_key(tmp_path)
    gate = Event()
    llm = FakeLLM(gate=gate)
    with _client(tmp_path, llm=llm) as client:
        first = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": True},
        )
        assert first.status_code == 200
        first_id = first.json()["id"]
        screen_status = ""
        for _ in range(80):
            screen_status = client.get(f"/api/screens/{first_id}").json()["status"]
            if screen_status == "done":
                break
            time.sleep(0.05)
        assert screen_status == "done"
        jobs = []
        for _ in range(40):
            jobs = client.get(f"/api/screens/{first_id}").json()["jobs"]
            if jobs:
                break
            time.sleep(0.05)
        assert jobs
        assert any(job["status"] in {"queued", "running"} for job in jobs)
        lock = client.get("/api/health").json()["lock"]
        assert lock is not None
        assert lock["type"] == "funnel"
        second = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "另一行业", "evaluate": False},
        )
        assert second.status_code == 409
        _assert_error_envelope(second.json())
        listed = client.get("/api/jobs").json()["jobs"]
        assert any(item.get("type") == "research" for item in listed)
        gate.set()
        deadline = time.time() + 8
        while time.time() < deadline:
            jobs = client.get(f"/api/screens/{first_id}").json()["jobs"]
            lock_state = client.get("/api/health").json()["lock"]
            if (
                jobs
                and all(job["status"] in {"done", "failed"} for job in jobs)
                and lock_state is None
            ):
                break
            time.sleep(0.05)
        assert jobs and all(job["status"] in {"done", "failed"} for job in jobs)
        assert client.get("/api/health").json()["lock"] is None


def test_missing_financials_is_gray_without_invented_numbers(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    llm = FakeLLM(
        {
            "verdict": "pass",
            "gates": [
                {"name": name, "status": "pass", "reason": f"捏造 {INVENTED}"}
                for name in ("能力圈", "好生意", "护城河", "管理层", "安全边际", "决策纪律")
            ],
            "mirror_test": ["一", "二", "三", "四", "五"],
            "price_bands": {
                "mode": "steady",
                "aggressive": INVENTED,
                "steady": "a",
                "conservative": "b",
            },
            "memo": {"essence": INVENTED, "risks": "r", "role": "core"},
            "data_grade": "A",
            "as_of": {"date": "2020-01-01", "price": INVENTED},
            "portfolio_fit": {
                "should_enter": INVENTED,
                "funding": INVENTED,
                "overlap": INVENTED,
            },
        }
    )
    with _client(tmp_path, llm=llm, market=FakeMarketClient()) as client:
        assert client.get("/api/health").status_code == 200
        engine = _engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO financials (code, period, period_type, roe, net_income) "
                    "VALUES ('600036.SH', '2024', 'annual', 18, NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO financials (code, period, period_type, roe, net_income) "
                    "VALUES ('600036.SH', '2023', 'annual', NULL, 100)"
                )
            )
        created = client.post(
            "/api/research",
            json={"name": "优质大盘", "code": "600036.SH", "sync": True},
        )
        assert created.status_code == 200
        body = client.get(f"/api/research/{created.json()['id']}").json()
        assert body["verdict"] == "gray"
        dumped = json.dumps(body, ensure_ascii=False)
        assert INVENTED not in dumped
        assert INVENTED not in body["markdown"]
        assert llm.calls == []


def test_startup_clears_leftover_research_jobs(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO research_jobs "
                "(user_id, name, code, status, created_at, updated_at) "
                "VALUES (1, '残留评估', '600036.SH', 'running', '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            text("UPDATE job_lock SET job_id = 7, job_type = 'funnel' WHERE id = 1")
        )
    with _client(tmp_path) as client:
        assert client.get("/api/health").json()["lock"] is None
        leftover = _scalar(
            db_path,
            "SELECT status || '|' || error FROM research_jobs WHERE name = '残留评估'",
        )
        assert leftover == "failed|进程重启，任务中断"


def test_standalone_research_fetches_financials_when_cache_empty(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    llm = FakeLLM()
    with _client(tmp_path, llm=llm) as client:
        created = client.post(
            "/api/research",
            json={"name": "优质大盘", "code": "600036.SH", "sync": True},
        )
        assert created.status_code == 200
        body = client.get(f"/api/research/{created.json()['id']}").json()
        assert body["status"] == "done"
        assert llm.calls
        count = _scalar(
            db_path, "SELECT COUNT(*) FROM financials WHERE code = '600036.SH'"
        )
        assert int(count or 0) > 0
        assert "缺少可核验财务或现价" not in (body.get("markdown") or "")


def test_gray_portfolio_fit_names_current_holding(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path, quote=FakeQuoteClient()) as client:
        assert client.get("/api/health").status_code == 200
        engine = _engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO cash (user_id, balance, currency) "
                    "VALUES (1, 10000, 'CNY')"
                )
            )
        traded = client.post(
            "/api/trades",
            json={
                "date": "2026-08-28",
                "direction": "buy",
                "name": "招商银行",
                "shares": 100,
                "price": 10,
            },
        )
        assert traded.status_code == 200
        created = client.post(
            "/api/research",
            json={"name": "优质大盘", "code": "600036.SH", "sync": True},
        )
        assert created.status_code == 200
        body = client.get(f"/api/research/{created.json()['id']}").json()
        assert body["verdict"] == "gray"
        fit = body["portfolio_fit"]
        assert "招商银行" in fit["should_enter"]
        assert "招商银行" in fit["overlap"]
        assert "空仓" not in fit["should_enter"]
        assert "招商银行" in body["markdown"]

