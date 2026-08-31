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
from app.models import ResearchJob, Thesis
from market.eastmoney import FakeMarketClient
from market.quotes import FakeQuoteClient
from research.evaluate import _gray_from_facts
from research.llm import FakeLLM
from research.thesis import (
    MARK_ESTABLISHED,
    MARK_UPDATED,
    clear_on_fail,
    eligible,
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


def test_eligible_pass_and_gray_with_real_mirror() -> None:
    assert eligible(_structured(verdict="pass"))
    assert eligible(_structured(verdict="gray"))


def test_fail_and_placeholder_are_not_eligible() -> None:
    assert not eligible(_structured(verdict="fail"))
    assert not eligible(_gray_from_facts(_input(Path("."))))
    short = _structured(mirror=["一句。", "两句。"])
    assert not eligible(short)


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


def test_fail_clears_current_thesis(tmp_path: Path) -> None:
    session = _session(tmp_path)
    try:
        job = _job(session)
        upsert(
            session,
            user_id=1,
            name="招商银行",
            research_job_id=job.id,
            structured=_structured(),
        )
        session.commit()
        clear_on_fail(session, user_id=1, name="招商银行")
        session.commit()
        assert session.scalars(select(Thesis)).first() is None
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


def test_status_conservative_floor() -> None:
    structured = _structured(mode="steady", conservative="32 到 40 元")
    assert (
        status(
            structured,
            today=date(2026, 8, 31),
            current_price=Decimal("30"),
            price_usable=True,
        )
        == "stale"
    )
    assert (
        status(
            structured,
            today=date(2026, 8, 31),
            current_price=Decimal("32"),
            price_usable=True,
        )
        == "current"
    )
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


def test_fail_research_clears_thesis(tmp_path: Path) -> None:
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
        body = client.get(f"/api/research/{failed.json()['id']}").json()
        assert body["verdict"] == "fail"
        assert body["thesis_status"] == "none"
        assert body["thesis_mark"] is None
        book = client.get("/api/positions").json()
        cmb = next(row for row in book["positions"] if row["name"] == "招商银行")
        assert cmb["thesis_status"] == "none"
        assert cmb["thesis_research_id"] is None
    assert int(_scalar(db_path, "SELECT COUNT(*) FROM theses") or 0) == 0


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
