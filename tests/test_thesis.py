from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app.db import create_session_factory, init_db
from app.envfile import OPENAI_API_KEY, write_env_value
from app.main import create_app
from app.models import ResearchArtifact, ResearchJob, Thesis
from market.eastmoney import FakeMarketClient
from market.quotes import FakeQuoteClient
from research.evaluate import _gray_from_facts
from research.llm import FakeLLM
from research.thesis import (
    MARK_ESTABLISHED,
    MARK_UPDATED,
    eligible,
    expensive_line,
    expected_price,
    gap_pct,
    in_then_band,
    is_overvalued,
    list_aftermath,
    live_verdict,
    status,
    upsert,
)
from tests.test_evaluate import _input
from tests.test_screen import _success_market

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "portfolio"
RESEARCH_DIR = ROOT / "research"
MIRROR = ["能看懂生意。", "管理层可靠。", "十年后更好。", "价格有边际。", "自己会持有。"]


def _engine(db_path: Path):
    return create_engine(f"sqlite:///{db_path.resolve().as_posix()}")


def _scalar(db_path: Path, sql: str, params: dict | None = None):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _structured(
    *,
    verdict: str = "pass",
    mirror: list[str] | None = None,
    mode: str = "steady",
    conservative: str = "20 到 30",
    as_of_date: str = "2026-08-29",
    as_of_price: object = 40,
) -> dict:
    return {
        "verdict": verdict,
        "mirror_test": list(mirror or MIRROR),
        "price_bands": {
            "mode": mode,
            "aggressive": "",
            "steady": "",
            "conservative": conservative,
        },
        "as_of": {"date": as_of_date, "price": as_of_price},
        "memo": {"essence": "假论文", "risks": "r", "role": "satellite"},
    }


def _fail_payload() -> dict:
    return {
        "verdict": "fail",
        "gates": [
            {"name": name, "status": "fail", "reason": "不通过"}
            for name in ("能力圈", "好生意", "护城河", "管理层", "安全边际", "决策纪律")
        ],
        "mirror_test": list(MIRROR),
        "price_bands": {
            "mode": "watch",
            "aggressive": "",
            "steady": "",
            "conservative": "",
        },
        "memo": {"essence": "不买", "risks": "r", "role": "skip"},
        "data_grade": "B",
        "as_of": {"date": "2026-08-29", "price": 40},
        "portfolio_fit": {
            "should_enter": "不进。",
            "funding": "不用钱。",
            "overlap": "不叠。",
        },
    }


def _session(tmp_path: Path):
    engine = init_db(tmp_path / "app.db")
    return create_session_factory(engine)()


def _job(session, name: str = "招商银行") -> ResearchJob:
    job = ResearchJob(
        user_id=1,
        name=name,
        code="600036.SH",
        status="done",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(job)
    session.flush()
    return job


def _write_key(tmp_path: Path) -> None:
    write_env_value(tmp_path / ".env", OPENAI_API_KEY, "sk-test-thesis")


def _quotes() -> FakeQuoteClient:
    return FakeQuoteClient({"600036.SH": "40", "600519.SH": "1500"})


def _client(
    tmp_path: Path,
    *,
    llm: FakeLLM | None = None,
    quote: FakeQuoteClient | None = None,
    market: FakeMarketClient | None = None,
    yaml_dir: Path | None = FIXTURES,
):
    return TestClient(
        create_app(
            db_path=tmp_path / "app.db",
            env_path=tmp_path / ".env",
            quote_client=quote if quote is not None else _quotes(),
            market_client=market if market is not None else _success_market(),
            llm_client=llm if llm is not None else FakeLLM(),
            reports_dir=tmp_path / "reports",
            yaml_dir=yaml_dir,
            allow_sync=True,
        )
    )


def test_expected_price_and_gap_pct() -> None:
    structured = _structured(mode="steady", conservative="60")
    structured["price_bands"]["steady"] = "50"
    structured["price_bands"]["aggressive"] = "30"
    assert expected_price(structured) == Decimal("50")
    assert gap_pct(Decimal("40"), Decimal("50")) == Decimal("25")
    watch = _structured(mode="watch")
    assert expected_price(watch) is None


def test_eligible_pass_and_gray_with_real_mirror() -> None:
    assert eligible(_structured(verdict="pass"))
    assert eligible(_structured(verdict="gray"))
    assert eligible(_structured(verdict="fail"))


def test_fail_and_placeholder_are_not_eligible() -> None:
    assert not eligible(_gray_from_facts(_input(Path("."))))
    short = _structured(mirror=["一句。", "两句。"])
    assert not eligible(short)
    assert eligible(short, replacing=True)
    packed = _structured(
        verdict="gray",
        mirror=[
            "若未持仓还会买吗？利润下滑是波动还是拐点？估值补偿够吗？跌20%加还是卖？退出线在哪？"
        ],
    )
    assert eligible(packed)
    assert not eligible(_gray_from_facts(_input(Path("."))), replacing=True)


def test_pe_ratio_is_not_an_expensive_line() -> None:
    bands = {
        "mode": "steady",
        "aggressive": "",
        "steady": "",
        "conservative": "45元以下可持有，市盈率12",
        "overvalued": "",
    }
    assert expensive_line(bands, Decimal("40")) == Decimal("45")
    from research.thesis import conservative_ceiling

    assert conservative_ceiling("45元以下可持有，市盈率12", Decimal("40")) == Decimal("45")
    assert conservative_ceiling("市盈率12", Decimal("40")) is None


def test_inverted_bands_are_not_an_expensive_line() -> None:
    bands = {
        "mode": "steady",
        "aggressive": "26.5",
        "steady": "24.61",
        "conservative": "21.0",
        "overvalued": "",
    }
    assert expensive_line(bands, Decimal("24.61")) is None
    paper = _structured(as_of_price=24.61)
    paper["price_bands"] = bands
    assert not is_overvalued(paper, current_price=Decimal("24.61"), price_usable=True)
    assert live_verdict(paper, current_price=Decimal("24.61"), price_usable=True) == "pass"


def test_aftermath_compares_old_paper_to_now(tmp_path: Path) -> None:
    session = _session(tmp_path)
    try:
        old = _job(session)
        old_paper = _structured(conservative="20 到 30", as_of_price=25)
        session.add(
            ResearchArtifact(
                research_job_id=old.id,
                structured_json=json.dumps(old_paper, ensure_ascii=False),
                markdown="旧论文",
            )
        )
        upsert(session, user_id=1, name="招商银行", research_job_id=old.id, structured=old_paper)
        session.commit()
        new = _job(session)
        new_paper = _structured(verdict="fail", conservative="20 到 30", as_of_price=28)
        session.add(
            ResearchArtifact(
                research_job_id=new.id,
                structured_json=json.dumps(new_paper, ensure_ascii=False),
                markdown="新论文",
            )
        )
        upsert(session, user_id=1, name="招商银行", research_job_id=new.id, structured=new_paper)
        session.commit()
        items = list_aftermath(session, 1, prices={"招商银行": (Decimal("40"), True)})
        assert len(items) == 2
        newer, older = items
        assert newer["research_id"] == new.id
        assert newer["paper"] == "current"
        assert older["research_id"] == old.id
        assert older["paper"] == "updated"
        assert older["in_band"] is False
        assert older["live_verdict"] == "fail"
        assert in_then_band(Decimal("20"), Decimal("30"), Decimal("25")) is True
    finally:
        session.close()
    _write_key(tmp_path)
    with _client(tmp_path) as client:
        body = client.get("/api/aftermath").json()
        assert len(body["items"]) == 2
        assert {row["paper"] for row in body["items"]} == {"current", "updated"}


def test_views_show_aftermath() -> None:
    home = (ROOT / "web/src/views/HomeView.vue").read_text(encoding="utf-8")
    home_model = (
        ROOT / "web/src/views/homeViewModel.ts"
    ).read_text(encoding="utf-8")
    assert "事后簿" in home
    assert "/api/aftermath" in home_model
    assert "夏普" not in home
    assert "收益率" not in home


def test_upsert_overwrites_same_name(tmp_path: Path) -> None:
    session = _session(tmp_path)
    try:
        first = _job(session)
        upsert(
            session,
            user_id=1,
            name="招商银行",
            research_job_id=first.id,
            structured=_structured(),
        )
        session.commit()
        second = _job(session)
        upsert(
            session,
            user_id=1,
            name="招商银行",
            research_job_id=second.id,
            structured=_structured(verdict="gray"),
        )
        session.commit()
        found = session.scalars(select(Thesis)).all()
        assert len(found) == 1
        assert found[0].research_job_id == second.id
        assert found[0].verdict == "gray"
        assert found[0].last_event == "updated"
    finally:
        session.close()


def test_placeholder_does_not_upsert_or_clear(tmp_path: Path) -> None:
    session = _session(tmp_path)
    try:
        job = _job(session)
        gray = _gray_from_facts(_input(tmp_path))
        assert not eligible(gray)
        assert gray.get("verdict") != "fail"
        session.commit()
        assert session.scalars(select(Thesis)).first() is None
        upsert(
            session,
            user_id=1,
            name="招商银行",
            research_job_id=job.id,
            structured=_structured(),
        )
        session.commit()
        assert not eligible(gray)
        assert gray.get("verdict") != "fail"
        session.commit()
        kept = session.scalars(select(Thesis)).first()
        assert kept is not None
        assert kept.research_job_id == job.id
    finally:
        session.close()


def test_status_stale_after_90_days() -> None:
    structured = _structured(as_of_date="2026-05-31")
    assert status(structured, today=date(2026, 8, 31), price_usable=False) == "stale"
    still = _structured(as_of_date="2026-06-02")
    assert status(still, today=date(2026, 8, 31), price_usable=False) == "current"


def test_status_conservative_ceiling() -> None:
    structured = _structured(mode="steady", conservative="32 到 40 元")
    assert (
        status(
            structured,
            today=date(2026, 8, 31),
            current_price=Decimal("41"),
            price_usable=True,
        )
        == "current"
    )
    assert (
        status(
            structured,
            today=date(2026, 8, 31),
            current_price=Decimal("40"),
            price_usable=True,
        )
        == "current"
    )
    assert (
        status(
            structured,
            today=date(2026, 8, 31),
            current_price=Decimal("30"),
            price_usable=True,
        )
        == "current"
    )
    guoguang = _structured(
        mode="steady",
        conservative="9.5-11.0，停止加仓",
        as_of_date="2026-09-02",
        as_of_price=8.72,
    )
    assert (
        status(
            guoguang,
            today=date(2026, 9, 2),
            current_price=Decimal("8.72"),
            price_usable=True,
        )
        == "current"
    )
    assert (
        status(
            guoguang,
            today=date(2026, 9, 2),
            current_price=Decimal("12"),
            price_usable=True,
        )
        == "current"
    )
    sanjiu = _structured(
        mode="steady",
        conservative="≤17.51（PE 8.5x）或PB≤1.4x即≤19.21元，显著低估区重仓",
        as_of_date="2026-09-02",
        as_of_price=24.19,
    )
    assert (
        status(
            sanjiu,
            today=date(2026, 9, 2),
            current_price=Decimal("24.20"),
            price_usable=True,
        )
        == "current"
    )
    chalco = _structured(
        mode="steady",
        conservative="7.40以下（约1.5倍PB）才显著加仓",
        as_of_date="2026-09-02",
        as_of_price=9.61,
    )
    assert (
        status(
            chalco,
            today=date(2026, 9, 2),
            current_price=Decimal("9.60"),
            price_usable=True,
        )
        == "current"
    )
    nari = _structured(
        mode="steady",
        conservative="18.2以下（PE≈17.5倍，留足安全边际）",
        as_of_date="2026-09-02",
        as_of_price=22.69,
    )
    assert (
        status(
            nari,
            today=date(2026, 9, 2),
            current_price=Decimal("22.70"),
            price_usable=True,
        )
        == "current"
    )
    from research.thesis import conservative_ceiling

    assert conservative_ceiling("18.2以下（PE≈17.5倍，留足安全边际）", Decimal("22.69")) == Decimal(
        "18.2"
    )
    rich = _structured(mode="steady", conservative="32 到 40 元")
    assert is_overvalued(rich, current_price=Decimal("41"), price_usable=True)
    assert not is_overvalued(rich, current_price=Decimal("40"), price_usable=True)
    assert live_verdict(rich, current_price=Decimal("41"), price_usable=True) == "fail"
    assert live_verdict(rich, current_price=Decimal("40"), price_usable=True) == "pass"
    cmb = _structured(mode="steady", conservative="41", as_of_price=Decimal("41.07"))
    cmb["price_bands"]["overvalued"] = "48"
    assert not is_overvalued(cmb, current_price=Decimal("41.07"), price_usable=True)
    assert is_overvalued(cmb, current_price=Decimal("48.01"), price_usable=True)
    assert live_verdict(cmb, current_price=Decimal("41.07"), price_usable=True) == "pass"
    unparsed = _structured(mode="steady", conservative="无法给出数字")
    assert (
        status(
            unparsed,
            today=date(2026, 8, 31),
            current_price=Decimal("1"),
            price_usable=True,
        )
        == "current"
    )


def test_status_watch_deviation() -> None:
    structured = _structured(mode="watch", as_of_price=40)
    assert (
        status(
            structured,
            today=date(2026, 8, 31),
            current_price=Decimal("47"),
            price_usable=True,
        )
        == "stale"
    )
    assert (
        status(
            structured,
            today=date(2026, 8, 31),
            current_price=Decimal("46"),
            price_usable=True,
        )
        == "current"
    )
    no_as_of = _structured(mode="watch", as_of_price=None)
    assert (
        status(
            no_as_of,
            today=date(2026, 8, 31),
            current_price=Decimal("80"),
            price_usable=True,
        )
        == "current"
    )


def test_status_bad_json_or_missing_research_date_is_stale() -> None:
    today = date(2026, 8, 31)
    assert status("not-a-dict", today=today) == "stale"
    assert status({}, today=today) == "stale"
    assert status({"as_of": {}}, today=today) == "stale"
    assert status({"as_of": {"date": ""}}, today=today) == "stale"
    assert status({"as_of": {"date": "不是日期"}}, today=today) == "stale"
    assert status(_structured(as_of_date=""), today=today, price_usable=False) == "stale"


def test_stale_price_does_not_judge_band_or_deviation() -> None:
    band = _structured(mode="steady", conservative="100")
    assert (
        status(
            band,
            today=date(2026, 8, 31),
            current_price=Decimal("1"),
            price_usable=False,
        )
        == "current"
    )
    watch = _structured(mode="watch", as_of_price=40)
    assert (
        status(
            watch,
            today=date(2026, 8, 31),
            current_price=Decimal("80"),
            price_usable=False,
        )
        == "current"
    )


def test_research_does_not_import_ledger() -> None:
    for path in RESEARCH_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import ledger" not in source
        assert "from ledger" not in source


def test_eligible_research_upserts_and_marks_page(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        first = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert first.status_code == 200
        first_id = first.json()["id"]
        body = client.get(f"/api/research/{first_id}").json()
        assert body["status"] == "done"
        assert body["thesis_status"] == "current"
        assert body["thesis_research_id"] == first_id
        assert body["thesis_mark"] == MARK_ESTABLISHED
        book = client.get("/api/positions").json()
        cmb = next(row for row in book["positions"] if row["name"] == "招商银行")
        assert cmb["thesis_status"] == "current"
        assert cmb["thesis_research_id"] == first_id
        second = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert second.status_code == 200
        second_id = second.json()["id"]
        updated = client.get(f"/api/research/{second_id}").json()
        assert updated["thesis_mark"] == MARK_UPDATED
        assert updated["thesis_research_id"] == second_id
        old = client.get(f"/api/research/{first_id}").json()
        assert old["thesis_mark"] is None
        assert old["thesis_research_id"] == second_id
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM theses") or 0) == 1
    assert int(_scalar(db_path, "SELECT research_job_id FROM theses") or 0) == second_id


def test_fail_research_keeps_thesis(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        created = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert created.status_code == 200
        assert int(_scalar(db_path, "SELECT COUNT(*) FROM theses") or 0) == 1
    with _client(tmp_path, llm=FakeLLM(_fail_payload())) as client:
        failed = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert failed.status_code == 200
        job_id = failed.json()["id"]
        body = client.get(f"/api/research/{job_id}").json()
        assert body["verdict"] == "fail"
        assert body["thesis_status"] == "current"
        assert body["thesis_verdict"] == "fail"
        assert body["thesis_mark"] == MARK_UPDATED
        book = client.get("/api/positions").json()
        cmb = next(row for row in book["positions"] if row["name"] == "招商银行")
        assert cmb["thesis_status"] == "current"
        assert cmb["thesis_verdict"] == "fail"
        assert cmb["thesis_research_id"] == job_id
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM theses") or 0) == 1
    assert int(_scalar(db_path, "SELECT research_job_id FROM theses") or 0) == job_id


def test_short_mirror_updates_existing_thesis(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        first = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert first.status_code == 200
        first_id = first.json()["id"]
    short = {
        "verdict": "gray",
        "gates": [
            {
                "name": name,
                "status": "gray" if name == "好生意" else "pass",
                "reason": f"{name}可核验",
            }
            for name in ("能力圈", "好生意", "护城河", "管理层", "安全边际", "决策纪律")
        ],
        "mirror_test": ["若未持仓还会买吗", "利润下滑是波动还是拐点"],
        "price_bands": {
            "mode": "steady",
            "aggressive": "20",
            "steady": "24",
            "conservative": "30",
            "overvalued": "40",
        },
        "memo": {"essence": "品牌药", "risks": "中报下滑", "role": "core"},
        "data_grade": "B",
        "as_of": {"date": "2026-09-08", "price": 40},
        "portfolio_fit": {
            "should_enter": "已在仓内，先看趋势。",
            "funding": "不用新钱。",
            "overlap": "不叠。",
        },
    }
    with _client(tmp_path, llm=FakeLLM(short)) as client:
        again = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert again.status_code == 200
        job_id = again.json()["id"]
        body = client.get(f"/api/research/{job_id}").json()
        assert body["verdict"] == "gray"
        assert body["thesis_research_id"] == job_id
        assert body["thesis_verdict"] == "gray"
        assert body["thesis_mark"] == MARK_UPDATED
        book = client.get("/api/positions").json()
        cmb = next(row for row in book["positions"] if row["name"] == "招商银行")
        assert cmb["thesis_research_id"] == job_id
        assert cmb["thesis_verdict"] == "gray"
    assert int(_scalar(db_path, "SELECT research_job_id FROM theses") or 0) == job_id
    assert first_id != job_id


def test_placeholder_gray_does_not_change_thesis(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        created = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM quotes"))
    with _client(tmp_path, quote=FakeQuoteClient(), market=FakeMarketClient()) as client:
        gray = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert gray.status_code == 200
        body = client.get(f"/api/research/{gray.json()['id']}").json()
        assert body["verdict"] == "gray"
        assert "无法完成" in "".join(body.get("mirror_test") or [])
        assert body["thesis_research_id"] == job_id
        assert body["thesis_mark"] is None
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM theses") or 0) == 1
    assert int(_scalar(db_path, "SELECT research_job_id FROM theses") or 0) == job_id


def test_failed_job_does_not_change_thesis(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        created = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
    with _client(tmp_path, llm=FakeLLM(fail_names=["招商银行"])) as client:
        failed = client.post(
            "/api/research",
            json={"name": "招商银行", "code": "600036.SH", "sync": True},
        )
        assert failed.status_code == 200
        body = client.get(f"/api/research/{failed.json()['id']}").json()
        assert body["status"] == "failed"
        assert body["error"]
        assert body["thesis_research_id"] == job_id
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM theses") or 0) == 1
    assert int(_scalar(db_path, "SELECT research_job_id FROM theses") or 0) == job_id


def test_positions_and_watchlist_return_stale_thesis_status(tmp_path: Path) -> None:
    _write_key(tmp_path)
    db_path = tmp_path / "app.db"
    stale_payload = _structured(as_of_date="2026-01-01")
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        engine = _engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO research_jobs "
                    "(user_id, name, code, status, created_at, updated_at) "
                    "VALUES (1, '招商银行', '600036.SH', 'done', '2026-01-01', '2026-01-01')"
                )
            )
            held_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar())
            conn.execute(
                text(
                    "INSERT INTO theses "
                    "(user_id, name, research_job_id, verdict, structured_json, last_event) "
                    "VALUES (1, '招商银行', :job_id, 'pass', :payload, 'established')"
                ),
                {"job_id": held_id, "payload": json.dumps(stale_payload, ensure_ascii=False)},
            )
            conn.execute(
                text(
                    "INSERT INTO research_jobs "
                    "(user_id, name, code, status, created_at, updated_at) "
                    "VALUES (1, '优质大盘', '600036.SH', 'done', '2026-01-01', '2026-01-01')"
                )
            )
            watch_job = int(conn.execute(text("SELECT last_insert_rowid()")).scalar())
            conn.execute(
                text(
                    "INSERT INTO research_artifacts "
                    "(research_job_id, structured_json, markdown) "
                    "VALUES (:job_id, :payload, '假评估')"
                ),
                {
                    "job_id": watch_job,
                    "payload": json.dumps({"verdict": "pass"}, ensure_ascii=False),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO theses "
                    "(user_id, name, research_job_id, verdict, structured_json, last_event) "
                    "VALUES (1, '优质大盘', :job_id, 'pass', :payload, 'established')"
                ),
                {"job_id": watch_job, "payload": json.dumps(stale_payload, ensure_ascii=False)},
            )
        book = client.get("/api/positions").json()
        cmb = next(row for row in book["positions"] if row["name"] == "招商银行")
        assert cmb["thesis_status"] == "stale"
        assert cmb["thesis_research_id"] == held_id
        joined = client.post("/api/watchlist", json={"research_id": watch_job})
        assert joined.status_code == 200
        assert joined.json()["thesis_status"] == "stale"
        assert joined.json()["thesis_research_id"] == watch_job
        listed = client.get("/api/watchlist").json()["items"]
        item = next(row for row in listed if row["name"] == "优质大盘")
        assert item["thesis_status"] == "stale"
        assert item["thesis_research_id"] == watch_job
