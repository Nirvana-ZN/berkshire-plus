from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.envfile import OPENAI_API_KEY, write_env_value
from app.main import create_app
from market.quotes import FakeQuoteClient
from research.llm import FakeLLM

ROOT = Path(__file__).resolve().parents[1]
REVIEW_DIR = ROOT / "review"
FIXTURES = ROOT / "tests" / "fixtures" / "portfolio"


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


def _quotes() -> FakeQuoteClient:
    return FakeQuoteClient(
        {
            "600036.SH": "40",
            "600519.SH": "1500",
        }
    )


def _hold_reason(name: str = "") -> str:
    prefix = f"{name}：" if name else ""
    return (
        f"{prefix}买入论文还在吗：还在。"
        "今天此价格还会买吗：会。对照规则灯与目标，维持持有。"
    )


def _review_llm(**kwargs) -> FakeLLM:
    return FakeLLM(
        payload={"action": "hold", "reason": _hold_reason()},
        payloads={
            "__PORTFOLIO__": {
                "portfolio_actions": [
                    {
                        "action": "维持现金缓冲",
                        "names": ["招商银行"],
                        "cash_or_target_link": "对照现金与目标仓位，先不动仓。",
                    }
                ]
            }
        },
        **kwargs,
    )


def _write_key(tmp_path: Path) -> None:
    write_env_value(tmp_path / ".env", OPENAI_API_KEY, "sk-test-review")


def _client(
    tmp_path: Path,
    *,
    quote: FakeQuoteClient | None = None,
    llm: FakeLLM | None = None,
    env_name: str = ".env",
    yaml_dir: Path | None = FIXTURES,
):
    return TestClient(
        create_app(
            db_path=tmp_path / "app.db",
            env_path=tmp_path / env_name,
            quote_client=quote if quote is not None else _quotes(),
            llm_client=llm if llm is not None else _review_llm(),
            reports_dir=tmp_path / "reports",
            yaml_dir=yaml_dir,
            allow_sync=True,
        )
    )


def _assert_position_keys(row: dict) -> None:
    assert set(row) >= {
        "name",
        "action",
        "reason",
        "actual_weight",
        "target_weight",
        "rule_light",
        "short_exam",
    }
    assert row["action"] in {"hold", "add", "cut", "sell", "unknown"}
    assert row["rule_light"] in {"hit", "clear", "none"}
    assert isinstance(row["reason"], str) and row["reason"]
    assert "买入论文还在吗" in row["reason"]
    assert "今天此价格还会买吗" in row["reason"]


def _seed_research(
    db_path: Path,
    *,
    name: str,
    code: str = "600036.SH",
    status: str = "done",
    verdict: str | None = "pass",
    screen_run_id: int | None = None,
) -> int:
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO research_jobs "
                "(user_id, screen_run_id, name, code, status, created_at, updated_at) "
                "VALUES (1, :screen_run_id, :name, :code, :status, '2026-08-29', '2026-08-29')"
            ),
            {
                "screen_run_id": screen_run_id,
                "name": name,
                "code": code,
                "status": status,
            },
        )
        job_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
        assert job_id is not None
        if verdict is not None:
            conn.execute(
                text(
                    "INSERT INTO research_artifacts "
                    "(research_job_id, structured_json, markdown) "
                    "VALUES (:job_id, :payload, :markdown)"
                ),
                {
                    "job_id": job_id,
                    "payload": json.dumps({"verdict": verdict, "memo": {"essence": "假评估"}}, ensure_ascii=False),
                    "markdown": "假评估",
                },
            )
        return int(job_id)


def _seed_funnel_run(db_path: Path) -> int:
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO screen_runs "
                "(user_id, scope_type, scope_text, status, created_at, updated_at) "
                "VALUES (1, '行业', '银行', 'done', '2026-08-29', '2026-08-29')"
            )
        )
        run_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
        assert run_id is not None
        return int(run_id)


def test_review_success_persists_and_can_reopen(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    reports = tmp_path / "reports"
    with _client(tmp_path) as client:
        before_trades = int(_scalar(db_path, "SELECT COUNT(*) FROM trades") or 0)
        before_cash = _scalar(db_path, "SELECT balance FROM cash WHERE user_id = 1")
        before_targets = int(_scalar(db_path, "SELECT COUNT(*) FROM position_targets") or 0)
        before_rules = int(_scalar(db_path, "SELECT COUNT(*) FROM rules") or 0)
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        body = created.json()
        assert body["type"] == "review"
        review_id = body["id"]
        first = client.get(f"/api/reviews/{review_id}")
        assert first.status_code == 200
        data = first.json()
        assert data["status"] == "done"
        assert data["id"] == review_id
        names = {row["name"] for row in data["positions"]}
        assert names == {"招商银行", "贵州茅台"}
        for row in data["positions"]:
            _assert_position_keys(row)
            assert row["action"] != "unknown"
        assert 0 <= len(data["portfolio_actions"]) <= 3
        for item in data["portfolio_actions"]:
            assert set(item) >= {"action", "names", "cash_or_target_link"}
            assert item["action"]
            assert item["names"]
            assert item["cash_or_target_link"]
        again = client.get(f"/api/reviews/{review_id}")
        assert again.status_code == 200
        assert again.json()["positions"] == data["positions"]
        assert again.json()["portfolio_actions"] == data["portfolio_actions"]
        latest = client.get("/api/reviews/latest")
        assert latest.status_code == 200
        assert latest.json()["id"] == review_id
        listed = client.get("/api/reviews").json()["reviews"]
        assert listed[0]["id"] == review_id
        jobs = client.get("/api/jobs").json()["jobs"]
        assert any(item.get("type") == "review" and item.get("id") == review_id for item in jobs)
        assert client.get("/api/health").json()["lock"] is None

    assert int(_scalar(db_path, "SELECT COUNT(*) FROM trades") or 0) == before_trades
    assert _scalar(db_path, "SELECT balance FROM cash WHERE user_id = 1") == before_cash
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM position_targets") or 0) == before_targets
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM rules") or 0) == before_rules
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM portfolio_reviews") or 0) == 1
    stored = json.loads(
        _rows(db_path, "SELECT structured_json FROM portfolio_reviews")[0]["structured_json"]
    )
    assert {row["name"] for row in stored["positions"]} == {"招商银行", "贵州茅台"}
    md_files = list(reports.glob("*.md"))
    assert md_files
    assert "组合审视" in md_files[0].read_text(encoding="utf-8")


def _eligible_thesis_payload(**overrides) -> dict:
    body = {
        "verdict": "pass",
        "mirror_test": ["能看懂生意。", "管理层可靠。", "十年后更好。", "价格有边际。", "自己会持有。"],
        "price_bands": {
            "mode": "steady",
            "aggressive": "",
            "steady": "",
            "conservative": "10 到 20",
        },
        "as_of": {"date": "2026-08-29", "price": 40},
        "memo": {"essence": "假论文"},
    }
    body.update(overrides)
    return body


def _seed_thesis(
    db_path: Path,
    *,
    name: str,
    research_job_id: int,
    structured: dict | None = None,
) -> None:
    payload = structured or _eligible_thesis_payload()
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO theses "
                "(user_id, name, research_job_id, verdict, structured_json, last_event) "
                "VALUES (1, :name, :job_id, :verdict, :payload, 'established')"
            ),
            {
                "name": name,
                "job_id": research_job_id,
                "verdict": payload.get("verdict"),
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )


def test_review_cites_current_thesis(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        eval_id = _seed_research(db_path, name="招商银行", verdict="pass")
        _seed_thesis(db_path, name="招商银行", research_job_id=eval_id)
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        by_name = {row["name"]: row for row in data["positions"]}
        cmb = by_name["招商银行"]
        assert cmb["short_exam"] is False
        assert f"现行论文 {eval_id}" in cmb["reason"]
        assert f"引用现行论文 {eval_id}" in data["markdown"]
        assert "短体检，非六关" not in cmb["reason"]
        assert "无论文" not in cmb["reason"]
        assert "论文过期" not in cmb["reason"]
        assert by_name["贵州茅台"]["short_exam"] is True
        assert "无论文" in by_name["贵州茅台"]["reason"]


def test_latest_evaluation_is_not_thesis(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        eval_id = _seed_research(db_path, name="招商银行", verdict="pass")
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        cmb = next(row for row in data["positions"] if row["name"] == "招商银行")
        assert cmb["short_exam"] is True
        assert "无论文" in cmb["reason"]
        assert f"现行论文 {eval_id}" not in cmb["reason"]
        assert "最近评估" not in cmb["reason"]


def test_review_without_thesis_is_short_exam(tmp_path: Path) -> None:
    _write_key(tmp_path)
    with _client(tmp_path) as client:
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        assert data["positions"]
        for row in data["positions"]:
            assert row["short_exam"] is True
            assert "短体检，非六关" in row["reason"]
            assert "无论文" in row["reason"]
            assert row["action"] in {"hold", "add", "cut", "sell"}
        assert "短体检，非六关" in data["markdown"]
        assert "无论文" in data["markdown"]


def test_stale_thesis_is_short_exam_with_action(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        eval_id = _seed_research(db_path, name="招商银行", verdict="pass")
        _seed_thesis(
            db_path,
            name="招商银行",
            research_job_id=eval_id,
            structured=_eligible_thesis_payload(as_of={"date": "2026-01-01", "price": 40}),
        )
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        cmb = next(row for row in data["positions"] if row["name"] == "招商银行")
        assert cmb["short_exam"] is True
        assert "论文过期" in cmb["reason"]
        assert cmb["action"] in {"hold", "add", "cut", "sell"}
        assert cmb["action"] != "unknown"
        assert "短体检，非六关" in cmb["reason"]


def test_review_stale_when_conservative_floor_breached(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        eval_id = _seed_research(db_path, name="招商银行", verdict="pass")
        _seed_thesis(
            db_path,
            name="招商银行",
            research_job_id=eval_id,
            structured=_eligible_thesis_payload(
                as_of={"date": "2026-08-29", "price": 40},
                price_bands={
                    "mode": "steady",
                    "aggressive": "",
                    "steady": "",
                    "conservative": "50 到 60",
                },
            ),
        )
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        cmb = next(row for row in data["positions"] if row["name"] == "招商银行")
        assert cmb["short_exam"] is True
        assert "论文过期" in cmb["reason"]
        assert cmb["action"] in {"hold", "add", "cut", "sell"}
        assert cmb["action"] != "unknown"


def test_review_stale_when_watch_price_deviates(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        eval_id = _seed_research(db_path, name="招商银行", verdict="pass")
        _seed_thesis(
            db_path,
            name="招商银行",
            research_job_id=eval_id,
            structured=_eligible_thesis_payload(
                as_of={"date": "2026-08-29", "price": 30},
                price_bands={
                    "mode": "watch",
                    "aggressive": "",
                    "steady": "",
                    "conservative": "",
                },
            ),
        )
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        cmb = next(row for row in data["positions"] if row["name"] == "招商银行")
        assert cmb["short_exam"] is True
        assert "论文过期" in cmb["reason"]
        assert cmb["action"] in {"hold", "add", "cut", "sell"}
        assert cmb["action"] != "unknown"


def test_position_prompt_includes_thesis_body() -> None:
    source = (ROOT / "review/run.py").read_text(encoding="utf-8")
    fn = source.split("def _position_prompt")[1].split("def _portfolio_prompt")[0]
    assert "mirror_test" in fn
    assert "price_bands" in fn
    assert "as_of" in fn


def test_stale_quote_unknown_even_with_current_thesis(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    quote = _quotes()
    with _client(tmp_path, quote=quote) as client:
        assert client.get("/api/health").status_code == 200
        eval_id = _seed_research(db_path, name="招商银行", verdict="pass")
        _seed_thesis(db_path, name="招商银行", research_job_id=eval_id)
        quote.failures.add("600036.SH")
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        assert data["status"] == "done"
        cmb = next(row for row in data["positions"] if row["name"] == "招商银行")
        assert cmb["action"] == "unknown"
        assert "含过期价" in cmb["reason"]
        assert f"现行论文 {eval_id}" in cmb["reason"]
        assert cmb["short_exam"] is False


def test_stale_quote_forces_unknown_and_round_completes(tmp_path: Path) -> None:
    _write_key(tmp_path)
    quote = _quotes()
    with _client(tmp_path, quote=quote) as client:
        before = client.get("/api/positions").json()
        by_before = {row["name"]: row for row in before["positions"]}
        assert by_before["招商银行"]["stale"] is False
        quote.failures.add("600036.SH")
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        assert data["status"] == "done"
        assert data["has_stale"] is True
        by_name = {row["name"]: row for row in data["positions"]}
        assert by_name["招商银行"]["action"] == "unknown"
        assert "含过期价" in by_name["招商银行"]["reason"]
        assert "买入论文还在吗" in by_name["招商银行"]["reason"]
        assert "今天此价格还会买吗" in by_name["招商银行"]["reason"]
        assert by_name["贵州茅台"]["action"] == "hold"
        assert "含过期价" in data["markdown"]


def test_missing_quote_forces_unknown_and_round_completes(tmp_path: Path) -> None:
    _write_key(tmp_path)
    quote = FakeQuoteClient({"600519.SH": "1500"})
    with _client(tmp_path, quote=quote) as client:
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        assert data["status"] == "done"
        assert data["has_stale"] is True
        by_name = {row["name"]: row for row in data["positions"]}
        assert by_name["招商银行"]["action"] == "unknown"
        assert "含过期价" in by_name["招商银行"]["reason"]
        assert "买入论文还在吗" in by_name["招商银行"]["reason"]
        assert "今天此价格还会买吗" in by_name["招商银行"]["reason"]
        assert by_name["贵州茅台"]["action"] == "hold"
        book = client.get("/api/positions").json()
        prices = {row["name"]: row.get("price") for row in book["positions"]}
        assert prices["招商银行"] is None


def test_model_failure_on_one_name_is_unknown(tmp_path: Path) -> None:
    _write_key(tmp_path)
    llm = _review_llm(fail_names=["贵州茅台"])
    with _client(tmp_path, llm=llm) as client:
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        assert data["status"] == "done"
        by_name = {row["name"]: row for row in data["positions"]}
        assert by_name["贵州茅台"]["action"] == "unknown"
        assert "无法判断" in by_name["贵州茅台"]["reason"]
        assert "买入论文还在吗" in by_name["贵州茅台"]["reason"]
        assert "今天此价格还会买吗" in by_name["贵州茅台"]["reason"]
        assert by_name["招商银行"]["action"] == "hold"


def test_review_without_key_is_400_and_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(OPENAI_API_KEY, raising=False)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 400
        body = created.json()
        _assert_error_envelope(body)
        assert "设置" in body["detail"]
        latest = client.get("/api/reviews/latest")
        assert latest.status_code == 404
        _assert_error_envelope(latest.json())
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM portfolio_reviews") or 0) == 0


def test_funnel_and_research_conflict_when_review_lock_held(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        engine = _engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE job_lock SET job_id = 88, job_type = 'review' WHERE id = 1")
            )
        funnel = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False},
        )
        assert funnel.status_code == 409
        funnel_body = funnel.json()
        _assert_error_envelope(funnel_body)
        assert funnel_body["code"] == "JOB_LOCKED"
        assert funnel_body["job_type"] == "review"
        research = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH"},
        )
        assert research.status_code == 409
        research_body = research.json()
        _assert_error_envelope(research_body)
        assert research_body["code"] == "JOB_LOCKED"
        assert research_body["job_type"] == "review"
        lock = client.get("/api/health").json()["lock"]
        assert lock == {"type": "review", "job_id": 88}
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM screen_runs") or 0) == 0
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM research_jobs") or 0) == 0


def test_jobs_get_requires_type_when_ids_collide(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        engine = _engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO screen_runs "
                    "(id, user_id, scope_type, scope_text, status, created_at, updated_at) "
                    "VALUES (1, 1, '行业', '银行', 'done', '2026-08-29', '2026-08-29')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO research_jobs "
                    "(id, user_id, name, code, status, created_at, updated_at) "
                    "VALUES (1, 1, '招商银行', '600036.SH', 'done', '2026-08-29', '2026-08-29')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO portfolio_reviews "
                    "(id, user_id, status, has_stale, created_at, updated_at) "
                    "VALUES (1, 1, 'done', 0, '2026-08-29', '2026-08-29')"
                )
            )
        ambiguous = client.get("/api/jobs/1")
        assert ambiguous.status_code == 409
        body = ambiguous.json()
        _assert_error_envelope(body)
        assert body["code"] == "JOB_AMBIGUOUS"
        assert set(body["types"]) == {"funnel", "research", "review"}
        review = client.get("/api/jobs/1", params={"type": "review"})
        assert review.status_code == 200
        assert review.json()["id"] == 1
        assert review.json()["type"] == "review"
        assert review.json()["status"] == "done"
        assert review.json()["error"] is None
        assert "updated_at" in review.json()
        funnel = client.get("/api/jobs/1", params={"type": "funnel"})
        assert funnel.status_code == 200
        assert funnel.json()["type"] == "funnel"
        assert funnel.json()["name"] == "行业 银行"
        research = client.get("/api/jobs/1", params={"type": "research"})
        assert research.status_code == 200
        assert research.json()["type"] == "research"
        assert research.json()["name"] == "招商银行"
        bad = client.get("/api/jobs/1", params={"type": "other"})
        assert bad.status_code == 400
        _assert_error_envelope(bad.json())
        missing = client.get("/api/jobs/99", params={"type": "review"})
        assert missing.status_code == 404


def test_review_conflict_when_lock_held(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        engine = _engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE job_lock SET job_id = 99, job_type = 'funnel' WHERE id = 1")
            )
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 409
        body = created.json()
        _assert_error_envelope(body)
        assert body["code"] == "JOB_LOCKED"
        assert body["job_type"] == "funnel"
        assert "funnel" in body["detail"]
        lock = client.get("/api/health").json()["lock"]
        assert lock == {"type": "funnel", "job_id": 99}
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE job_lock SET job_id = 7, job_type = 'research' WHERE id = 1")
            )
        second = client.post("/api/reviews", json={"sync": True})
        assert second.status_code == 409
        assert second.json()["job_type"] == "research"
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM portfolio_reviews") or 0) == 0


def test_illegal_new_name_is_dropped_from_portfolio_actions(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    llm = FakeLLM(
        payload={"action": "hold", "reason": _hold_reason()},
        payloads={
            "__PORTFOLIO__": {
                "portfolio_actions": [
                    {
                        "action": "用现金买入",
                        "names": ["不通过票"],
                        "cash_or_target_link": "用现金买入不通过票",
                    },
                    {
                        "action": "用现金买入",
                        "names": ["灰色候选"],
                        "cash_or_target_link": "用现金买入灰色候选",
                    },
                    {
                        "action": "用现金买入",
                        "names": ["通过候选"],
                        "cash_or_target_link": "用现金买入通过候选",
                    },
                ]
            }
        },
    )
    with _client(tmp_path, llm=llm) as client:
        assert client.get("/api/health").status_code == 200
        run_id = _seed_funnel_run(db_path)
        _seed_research(
            db_path, name="不通过票", code="000001.SZ", verdict="fail", screen_run_id=run_id
        )
        _seed_research(
            db_path, name="灰色候选", code="000002.SZ", verdict="gray", screen_run_id=run_id
        )
        _seed_research(
            db_path, name="通过候选", code="000003.SZ", verdict="pass", screen_run_id=run_id
        )
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        dumped = json.dumps(data["portfolio_actions"], ensure_ascii=False)
        assert "不通过票" not in dumped
        assert any("灰色候选" in item["names"] for item in data["portfolio_actions"])
        assert any("通过候选" in item["names"] for item in data["portfolio_actions"])


def test_model_rebalance_note_is_dropped(tmp_path: Path) -> None:
    _write_key(tmp_path)
    llm = FakeLLM(
        payload={"action": "hold", "reason": _hold_reason()},
        payloads={
            "__PORTFOLIO__": {
                "portfolio_actions": [
                    {
                        "action": "rebalance_note",
                        "names": ["招商银行"],
                        "cash_or_target_link": "微调 0.18 个百分点",
                    },
                    {
                        "action": "维持现金缓冲",
                        "names": ["招商银行"],
                        "cash_or_target_link": "对照现金与目标仓位，先不动仓。",
                    },
                ]
            }
        },
    )
    with _client(tmp_path, llm=llm) as client:
        created = client.post("/api/reviews", json={"sync": True})
        assert created.status_code == 200
        data = client.get(f"/api/reviews/{created.json()['id']}").json()
        dumped = json.dumps(data["portfolio_actions"], ensure_ascii=False)
        assert "rebalance_note" not in dumped
        assert "微调 0.18" not in dumped
        assert any(item["action"] == "维持现金缓冲" for item in data["portfolio_actions"])


def test_review_module_does_not_import_ledger() -> None:
    for path in REVIEW_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import ledger" not in source
        assert "from ledger" not in source


def test_positions_and_home_have_review_entry_without_apply() -> None:
    positions = (ROOT / "web/src/views/PositionsView.vue").read_text(encoding="utf-8")
    home = (ROOT / "web/src/views/HomeView.vue").read_text(encoding="utf-8")
    assert "一键审视" in positions
    assert "/api/reviews" in positions
    assert "/api/reviews/latest" in positions
    assert "loadLatestReview" in positions
    assert "短体检，非六关" in positions
    assert "含过期价" in positions
    assert "一键应用" not in positions
    assert "portfolioActionLabel" in positions
    assert "再平衡提示" in positions
    assert "record_trade" not in positions
    assert "review: '审视'" in home or 'review: "审视"' in home
    assert "/api/reviews/latest" in home
    assert "一键应用" not in home
    assert "今日推荐" not in positions
    assert "收益排行" not in positions


def test_review_invalid_json_is_400(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        malformed = client.post(
            "/api/reviews",
            content=b"{",
            headers={"content-type": "application/json"},
        )
        assert malformed.status_code == 400
        body = malformed.json()
        _assert_error_envelope(body)
        assert body["code"] == "INVALID_REVIEW"
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM portfolio_reviews") or 0) == 0
