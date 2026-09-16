from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from app.models import Quote, ResearchJob, Thesis
from market.eastmoney import FakeMarketClient
from market.quotes import FakeQuoteClient
from market.types import Constituent, FinancialRow
from screen.funnel import run_funnel

ROOT = Path(__file__).resolve().parents[1]
SCREEN_DIR = ROOT / "screen"
JOBS_DIR = ROOT / "jobs"


def _engine(db_path: Path):
    return create_engine(f"sqlite:///{db_path.resolve().as_posix()}")


def _scalar(db_path: Path, sql: str, params: dict | None = None):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _client(
    tmp_path: Path,
    *,
    market: FakeMarketClient | None = None,
    quote: FakeQuoteClient | None = None,
    env_name: str = ".env",
):
    return TestClient(
        create_app(
            db_path=tmp_path / "app.db",
            env_path=tmp_path / env_name,
            quote_client=quote if quote is not None else FakeQuoteClient(),
            market_client=market if market is not None else FakeMarketClient(),
            allow_sync=True,
        )
    )


def _assert_error_envelope(body: dict) -> None:
    assert set(body) >= {"error", "code", "detail"}
    assert body["error"]
    assert body["code"]
    assert body["detail"]


def _annual(
    code: str,
    *,
    roe: str = "15",
    ni: str = "100",
    ocf: str = "100",
    capex: str = "20",
    ebit: str = "40",
    interest: str = "5",
    gm: str = "40",
    nm: str = "10",
    shares: str = "100",
    start: int = 2016,
    years: int = 10,
    ma: bool | None = None,
) -> list[FinancialRow]:
    rows: list[FinancialRow] = []
    for offset in range(years):
        rows.append(
            FinancialRow(
                code=code,
                period=str(start + offset),
                period_type="annual",
                roe=Decimal(roe),
                net_income=Decimal(ni),
                ocf=Decimal(ocf),
                capex=Decimal(capex),
                ebit=Decimal(ebit),
                interest=Decimal(interest),
                gross_margin=Decimal(gm),
                net_margin=Decimal(nm),
                shares=Decimal(shares),
                share_change_ma=ma,
            )
        )
    return rows


def _snap(
    code: str,
    name: str,
    mcap: str | None,
    *,
    industry: str = "银行",
    peg: str | None = None,
) -> Constituent:
    return Constituent(
        code=code,
        name=name,
        mcap=None if mcap is None else Decimal(mcap),
        pe=Decimal("8"),
        industry=industry,
        peg=None if peg is None else Decimal(peg),
    )


def _success_market() -> FakeMarketClient:
    snaps = [
        _snap("600036.SH", "优质大盘", "300"),
        _snap("600519.SH", "优质中盘", "200"),
        _snap("000333.SZ", "弹性小盘", "80", industry="家电", peg="1.8"),
        _snap("600000.SH", "低ROE淘汰", "150"),
        _snap("600001.SH", "无市值", None),
        _snap("BAD", "无代码", "90"),
    ]
    rows = (
        _annual("600036.SH", roe="20")
        + _annual("600519.SH", roe="16")
        + _annual("000333.SZ", roe="16")
        + _annual("600000.SH", roe="3", gm="18", nm="6")
        + _annual("600001.SH", roe="16")
        + _annual("BAD", roe="16")
    )
    return FakeMarketClient(
        snaps,
        rows,
        listed_years={"600036.SH": 2002},
        valuations={
            "600036.SH": {
                "bps": Decimal("45.4"),
                "npl_pct": Decimal("0.94"),
                "eps": Decimal("2.98"),
            }
        },
    )


def test_screen_success_three_states_no_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    market = _success_market()
    db_path = tmp_path / "app.db"
    with _client(tmp_path, market=market) as client:
        created = client.post(
            "/api/screens/funnel",
            json={
                "scope_type": "行业",
                "scope_text": "银行",
                "evaluate": False,
                "sync": True,
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["status"] in {"done", "queued", "running"}
        screen_id = body["id"]
        got = client.get(f"/api/screens/{screen_id}")
        assert got.status_code == 200
        data = got.json()
        assert data["status"] == "done"
        assert data["error"] is None
        keep_names = [row["name"] for row in data["keep"]]
        drop_names = [row["name"] for row in data["drop"]]
        insuff_names = [row["name"] for row in data["insufficient"]]
        assert "优质大盘" in keep_names
        assert "优质中盘" in keep_names
        assert "弹性小盘" in keep_names
        assert "低ROE淘汰" in drop_names
        assert any(row["reason"] for row in data["drop"])
        assert "无市值" in insuff_names
        assert "无代码" in insuff_names
        assert all(row["name"] not in keep_names for row in data["insufficient"])
        finalist_names = [row["name"] for row in data["finalists"]]
        assert len(finalist_names) == 3
        assert len(finalist_names) == len(set(finalist_names))
        assert set(finalist_names) <= set(keep_names)
        roles = {row["name"]: row["role"] for row in data["keep"]}
        assert roles["优质大盘"] == "high_certainty"
        assert roles["优质中盘"] == "mid_elasticity"
        assert roles["弹性小盘"] == "high_elasticity"
        big = next(row for row in data["keep"] if row["name"] == "优质大盘")
        assert "price" in big
        assert "expected_price" in big
        assert "gap_pct" in big
        job = client.get(f"/api/jobs/{screen_id}")
        assert job.status_code == 200
        assert job.json()["status"] == "done"
        listed = client.get("/api/jobs")
        assert listed.status_code == 200
        assert any(item["id"] == screen_id for item in listed.json()["jobs"])
        assert client.get("/api/health").json()["lock"] is None

    assert _scalar(db_path, "SELECT COUNT(*) FROM screen_results") >= 6
    assert _scalar(db_path, "SELECT COUNT(*) FROM screen_runs") == 1


def test_keep_list_includes_price_expected_and_gap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    with _client(tmp_path, market=_success_market()) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False, "sync": True},
        )
        assert created.status_code == 200
        screen_id = created.json()["id"]
        factory = client.app.state.session_factory
        with factory() as session:
            now = datetime.now(timezone.utc)
            job = ResearchJob(
                user_id=1,
                name="优质大盘",
                code="600036.SH",
                status="done",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            session.add(
                Quote(
                    user_id=1,
                    name="优质大盘",
                    code="600036.SH",
                    price=Decimal("40"),
                    source="auto",
                    updated_at=now,
                    stale=False,
                )
            )
            session.add(
                Thesis(
                    user_id=1,
                    name="优质大盘",
                    research_job_id=job.id,
                    verdict="pass",
                    structured_json=json.dumps(
                        {
                            "verdict": "pass",
                            "price_bands": {
                                "mode": "steady",
                                "aggressive": "30",
                                "steady": "50",
                                "conservative": "60",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    last_event="established",
                )
            )
            session.commit()
        data = client.get(f"/api/screens/{screen_id}").json()
        big = next(row for row in data["keep"] if row["name"] == "优质大盘")
        assert big["price"] == 40
        assert big["expected_price"] == 50
        assert big["gap_pct"] == 25


def test_list_screen_boards(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        missing = client.get("/api/screens/boards")
        assert missing.status_code == 400
        _assert_error_envelope(missing.json())
        listed = client.get("/api/screens/boards", params={"scope_type": "行业", "q": "机械"})
        assert listed.status_code == 200
        names = [row["name"] for row in listed.json()["boards"]]
        assert "机械设备" in names
        assert "机械重工" in names


def test_latest_screen_404_when_empty(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        missing = client.get("/api/screens/latest")
        assert missing.status_code == 404
        _assert_error_envelope(missing.json())
        assert missing.json()["code"] == "NOT_FOUND"


def test_latest_screen_returns_last_run(tmp_path: Path) -> None:
    market = _success_market()
    with _client(tmp_path, market=market) as client:
        first = client.post(
            "/api/screens/funnel",
            json={
                "scope_type": "行业",
                "scope_text": "银行",
                "evaluate": False,
                "sync": True,
            },
        )
        assert first.status_code == 200
        second = client.post(
            "/api/screens/funnel",
            json={
                "scope_type": "主题",
                "scope_text": "保险",
                "evaluate": False,
                "sync": True,
            },
        )
        assert second.status_code == 200
        latest = client.get("/api/screens/latest")
        assert latest.status_code == 200
        data = latest.json()
        assert data["id"] == second.json()["id"]
        assert data["scope_type"] == "主题"
        assert data["scope_text"] == "保险"
        listed = client.get("/api/jobs").json()["jobs"]
        assert all("updated_at" in item for item in listed)
        stamps = [item["updated_at"] for item in listed]
        assert stamps == sorted(stamps, reverse=True)


def test_constituent_failure_keeps_original_error(tmp_path: Path) -> None:
    market = FakeMarketClient(constituent_error="指数成分接口 404 原文")
    with _client(tmp_path, market=market) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "指数", "scope_text": "不存在指数", "evaluate": False, "sync": True},
        )
        assert created.status_code == 200
        data = client.get(f"/api/screens/{created.json()['id']}").json()
        assert data["status"] == "failed"
        assert data["error"] == "指数成分接口 404 原文"
        assert data["keep"] == []
        assert data["drop"] == []
        assert data["insufficient"] == []
        assert data["finalists"] == []
        assert client.get("/api/health").json()["lock"] is None


def test_financial_endpoint_failure_keeps_original_error(tmp_path: Path) -> None:
    market = FakeMarketClient(
        [_snap("600036.SH", "招商银行", "300")],
        financial_error="财务接口 503 原文",
    )
    with _client(tmp_path, market=market) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "主题", "scope_text": "银行", "evaluate": False, "sync": True},
        )
        data = client.get(f"/api/screens/{created.json()['id']}").json()
        assert data["status"] == "failed"
        assert data["error"] == "财务接口 503 原文"
        assert data["keep"] == []
        assert data["finalists"] == []
        assert client.get("/api/health").json()["lock"] is None


def test_single_name_insufficient_job_done(tmp_path: Path) -> None:
    market = FakeMarketClient(
        [_snap("600036.SH", "缺ROE", "200")],
        [
            FinancialRow(
                code="600036.SH",
                period="2024",
                period_type="annual",
                net_income=Decimal("10"),
            )
        ],
    )
    with _client(tmp_path, market=market) as client:
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False, "sync": True},
        )
        data = client.get(f"/api/screens/{created.json()['id']}").json()
        assert data["status"] == "done"
        assert data["keep"] == []
        assert data["finalists"] == []
        assert data["insufficient"][0]["name"] == "缺ROE"
        assert data["insufficient"][0]["reason"]


def test_second_funnel_409_does_not_overwrite_first(tmp_path: Path) -> None:
    gate = Event()
    market = _success_market()
    market.gate = gate
    with _client(tmp_path, market=market) as client:
        first = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False},
        )
        assert first.status_code == 200
        first_id = first.json()["id"]
        lock = None
        for _ in range(40):
            lock = client.get("/api/health").json()["lock"]
            if lock:
                break
            time.sleep(0.05)
        assert lock is not None
        assert lock["type"] == "funnel"
        second = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "另一行业", "evaluate": False},
        )
        assert second.status_code == 409
        _assert_error_envelope(second.json())
        assert "funnel" in second.json()["detail"]
        jobs = client.get("/api/jobs").json()["jobs"]
        assert [item["id"] for item in jobs] == [first_id]
        gate.set()
        deadline = time.time() + 5
        status = ""
        while time.time() < deadline:
            status = client.get(f"/api/screens/{first_id}").json()["status"]
            if status == "done":
                break
            time.sleep(0.05)
        assert status == "done"
        data = client.get(f"/api/screens/{first_id}").json()
        assert {row["name"] for row in data["keep"]} >= {"优质大盘", "优质中盘", "弹性小盘"}


def test_quote_refresh_does_not_hold_lock(tmp_path: Path) -> None:
    market = _success_market()
    quote = FakeQuoteClient()
    with _client(tmp_path, market=market, quote=quote) as client:
        refreshed = client.post("/api/quotes/refresh", json={})
        assert refreshed.status_code == 200
        assert client.get("/api/health").json()["lock"] is None
        created = client.post(
            "/api/screens/funnel",
            json={"scope_type": "行业", "scope_text": "银行", "evaluate": False, "sync": True},
        )
        assert created.status_code == 200
        assert client.get(f"/api/screens/{created.json()['id']}").json()["status"] == "done"


def test_startup_clears_leftover_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    market = FakeMarketClient()
    with _client(tmp_path, market=market) as client:
        assert client.get("/api/health").status_code == 200
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO screen_runs (user_id, scope_type, scope_text, status, created_at, updated_at) "
                "VALUES (1, '行业', '残留', 'running', '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "UPDATE job_lock SET job_id = 99, job_type = 'funnel' WHERE id = 1"
            )
        )
    with _client(tmp_path, market=market) as client:
        assert client.get("/api/health").json()["lock"] is None
        leftover = _scalar(
            db_path,
            "SELECT status || '|' || error FROM screen_runs WHERE scope_text = '残留'",
        )
        assert leftover == "failed|进程重启，任务中断"


def test_funnel_rules_keep_drop_insufficient() -> None:
    snaps = [
        _snap("600036.SH", "保留甲", "500"),
        _snap("600519.SH", "保留乙", "400"),
        _snap("000001.SZ", "低ROE", "300"),
        _snap("NOTCODE", "坏代码", "200"),
    ]
    rows = (
        _annual("600036.SH", roe="18")
        + _annual("600519.SH", roe="17")
        + _annual("000001.SZ", roe="2", gm="16", nm="6")
        + _annual("NOTCODE", roe="18")
    )
    result = run_funnel(snaps, rows, as_of_year=2026)
    assert [row.name for row in result.keep] == ["保留甲", "保留乙"]
    assert any(row.name == "低ROE" and row.reason for row in result.drop)
    assert any(row.name == "坏代码" and row.verdict == "insufficient" for row in result.insufficient)
    names = [row.name for row in result.finalists]
    assert names
    assert len(names) == len(set(names))
    assert set(names) <= {"保留甲", "保留乙"}
    assert len(names) == min(3, len(result.keep))


def test_funnel_keep_limit_overflow() -> None:
    snaps = []
    rows: list[FinancialRow] = []
    for index in range(11):
        code = f"6000{index:02d}.SH"
        snaps.append(_snap(code, f"候选{index}", str(1000 - index)))
        rows.extend(_annual(code, roe="18"))
    result = run_funnel(snaps, rows, as_of_year=2026)
    assert len(result.keep) == 10
    assert any(row.reason == "未进入本轮保留名单" for row in result.drop)
    assert len(result.finalists) == 3
    assert len({row.name for row in result.finalists}) == 3


def test_funnel_fcf_drop_and_held_industry_sort() -> None:
    snaps = [
        _snap("600036.SH", "持仓同行", "200", industry="银行"),
        _snap("600519.SH", "未持仓", "200", industry="白酒"),
        _snap("000001.SZ", "负FCF", "180", industry="家电"),
    ]
    rows = (
        _annual("600036.SH", roe="18")
        + _annual("600519.SH", roe="18")
        + _annual("000001.SZ", roe="18", ocf="10", capex="40")
    )
    result = run_funnel(snaps, rows, held_industries=["银行"], as_of_year=2026)
    assert [row.name for row in result.keep] == ["未持仓", "持仓同行"]
    assert any(row.name == "负FCF" and "自由现金流" in (row.reason or "") for row in result.drop)


def test_funnel_share_expansion_unknown_ma_is_insufficient() -> None:
    snaps = [_snap("600036.SH", "扩股", "200")]
    common = dict(
        code="600036.SH",
        period_type="annual",
        roe=Decimal("18"),
        net_income=Decimal("100"),
        ocf=Decimal("100"),
        capex=Decimal("20"),
        ebit=Decimal("40"),
        interest=Decimal("5"),
        gross_margin=Decimal("40"),
        net_margin=Decimal("10"),
        share_change_ma=None,
    )
    rows = [
        FinancialRow(**common, period="2025", shares=Decimal("130")),
        FinancialRow(**common, period="2021", shares=Decimal("100")),
    ]
    result = run_funnel(snaps, rows, as_of_year=2026)
    assert result.drop == []
    assert any(row.name == "扩股" and row.verdict == "keep" for row in result.keep)
    gaps = (result.keep[0].metrics or {}).get("gaps") or []
    assert "无法识别股本膨胀是否并购" in gaps


def test_funnel_bank_skips_interest_only() -> None:
    snaps = [_snap("600036.SH", "银行", "200", industry="银行")]
    rows = _annual("600036.SH", roe="18", ocf="100", capex="20", gm="40", ebit="1", interest="10")
    result = run_funnel(snaps, rows, as_of_year=2026)
    assert any(row.name == "银行" and row.verdict == "keep" for row in result.keep)
    bad_fcf = _annual("600036.SH", roe="18", ocf="10", capex="40", gm="40")
    dropped = run_funnel(snaps, bad_fcf, as_of_year=2026)
    assert any(row.name == "银行" and "自由现金流" in (row.reason or "") for row in dropped.drop)


def test_funnel_retail_industry_does_not_exempt() -> None:
    snaps = [_snap("000001.SZ", "零售", "200", industry="互联网零售")]
    rows = _annual("000001.SZ", roe="25", ocf="120", ni="100", gm="8")
    result = run_funnel(snaps, rows, as_of_year=2026)
    assert any(row.name == "零售" and row.verdict == "drop" for row in result.drop)


def test_funnel_thin_model_exempts_gross_margin() -> None:
    snaps = [
        Constituent(
            code="000001.SZ",
            name="平台",
            mcap=Decimal("200"),
            pe=Decimal("8"),
            industry="互联网零售",
            business_model="平台佣金",
        )
    ]
    rows = _annual("000001.SZ", roe="25", ocf="120", ni="100", gm="8")
    result = run_funnel(snaps, rows, as_of_year=2026)
    assert any(row.name == "平台" and row.verdict == "keep" for row in result.keep)


def test_funnel_exemption_a_and_b_keep() -> None:
    snaps = [
        _snap("600036.SH", "豁免A", "200"),
        _snap("600519.SH", "豁免B", "180"),
    ]
    common_b = dict(
        code="600519.SH",
        period_type="annual",
        roe=Decimal("18"),
        net_income=Decimal("100"),
        ocf=Decimal("100"),
        capex=Decimal("20"),
        ebit=Decimal("40"),
        interest=Decimal("5"),
        gross_margin=Decimal("35"),
        shares=Decimal("100"),
    )
    rows = _annual("600036.SH", roe="5", gm="35", start=2022, years=4) + [
        FinancialRow(**common_b, period="2025", net_margin=Decimal("6")),
        FinancialRow(**common_b, period="2024", net_margin=Decimal("3")),
    ]
    result = run_funnel(
        snaps,
        rows,
        listed_years={"600036.SH": 2020, "600519.SH": 2002},
        as_of_year=2026,
    )
    names = {row.name for row in result.keep}
    assert "豁免A" in names
    assert "豁免B" in names


def test_funnel_empty_bucket_fills_from_mcap() -> None:
    snaps = [
        _snap("600036.SH", "甲", "300"),
        _snap("600519.SH", "乙", "200"),
        _snap("000001.SZ", "丙", "100"),
    ]
    rows = (
        _annual("600036.SH", roe="12")
        + _annual("600519.SH", roe="12")
        + _annual("000001.SZ", roe="12")
    )
    result = run_funnel(snaps, rows, as_of_year=2026)
    assert len(result.keep) == 3
    assert len(result.finalists) == 3
    assert {row.name for row in result.finalists} == {"甲", "乙", "丙"}


def test_funnel_share_expansion_non_ma_drops() -> None:
    snaps = [_snap("600036.SH", "扩股", "200")]
    common = dict(
        code="600036.SH",
        period_type="annual",
        roe=Decimal("18"),
        net_income=Decimal("100"),
        ocf=Decimal("100"),
        capex=Decimal("20"),
        ebit=Decimal("40"),
        interest=Decimal("5"),
        gross_margin=Decimal("40"),
        net_margin=Decimal("10"),
        share_change_ma=False,
    )
    rows = [
        FinancialRow(**common, period="2025", shares=Decimal("130")),
        FinancialRow(**common, period="2021", shares=Decimal("100")),
    ]
    result = run_funnel(snaps, rows, as_of_year=2026)
    assert result.keep == []
    assert any("股本膨胀" in (row.reason or "") for row in result.drop)


def test_screen_and_jobs_have_no_http_or_llm() -> None:
    for folder in (SCREEN_DIR, JOBS_DIR):
        for path in folder.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "httpx" not in source
            assert "openai" not in source.lower()


def test_screen_view_has_three_chinese_tables_not_json() -> None:
    source = (ROOT / "web/src/views/ScreenView.vue").read_text(encoding="utf-8")
    assert "选股" in source
    assert "保留" in source
    assert "淘汰" in source
    assert "数据不足" in source
    assert "终选" in source
    assert "/api/screens/funnel" in source
    assert "现价" in source
    assert "预期价格" in source
    assert "差距" in source
    assert "/api/ruler" in source
    assert "/api/screens/" in source
    assert "/api/screens/latest" in source
    assert "/api/screens/boards" in source
    assert "loadBoards" in source
    assert "suggest" in source
    assert "loadLatest" in source
    assert "activeId" in source
    assert "评估终选" in source
    assert "评估 Checklist" in source
    assert "/api/research" in source
    assert "startSingle" in source
    assert "今日推荐" not in source
    assert "收益排行" not in source
    assert "<pre" not in source.lower()
    assert "snapshot_json" not in source
