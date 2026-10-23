from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from ledger.positions import load_positions
from ledger.quotes import BookSnapshot, project_book
from market.quotes import FakeQuoteClient

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "portfolio"
CASH_BALANCE = Decimal("123456.78")
TRADE_COUNT = 5


def _engine(db_path: Path):
    from app.db import create_db_engine
    return create_db_engine(db_path)



def _scalar(db_path: Path, sql: str, params: dict | None = None):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _rows(db_path: Path, sql: str, params: dict | None = None):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params or {}).mappings())


def _client(
    tmp_path: Path,
    *,
    fake: FakeQuoteClient | None = None,
    yaml_dir: Path | None = None,
    db_name: str = "app.db",
):
    kwargs: dict[str, object] = {
        "db_path": tmp_path / db_name,
        "quote_client": fake if fake is not None else FakeQuoteClient(),
        "env_path": tmp_path / ".env",
    }
    if yaml_dir is not None:
        kwargs["yaml_dir"] = yaml_dir
    return TestClient(create_app(**kwargs))


def _as_decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _assert_error_envelope(body: dict) -> None:
    assert set(body) >= {"error", "code", "detail"}
    assert body["error"]
    assert body["code"]
    assert body["detail"]


def _priced_fake(
    cmb: str = "40",
    moutai: str = "1500",
    *,
    failures: tuple[str, ...] = (),
) -> FakeQuoteClient:
    return FakeQuoteClient(
        {"600036.SH": cmb, "600519.SH": moutai},
        failures=failures,
    )


def _insert_rule(
    db_path: Path,
    *,
    name: str,
    rule_type: str,
    price: str,
    note: str | None = None,
) -> None:
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rules (user_id, name, type, price, note) "
                "VALUES (1, :name, :type, :price, :note)"
            ),
            {"name": name, "type": rule_type, "price": price, "note": note},
        )


def _trade_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "date": "2026-08-28",
        "direction": "buy",
        "name": "测试票",
        "shares": 10,
        "price": 8,
    }
    body.update(overrides)
    return body


def test_migrated_rules_and_hit_lights(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["lock"] is None
        listed = client.get("/api/rules")
        assert 200 <= listed.status_code < 300
        rules = listed.json()["rules"]
        assert len(rules) == 2
        by_name = {row["name"]: row for row in rules}
        assert by_name["招商银行"]["type"] == "加仓上限价"
        assert _as_decimal(by_name["招商银行"]["price"]) == Decimal("32")
        assert by_name["贵州茅台"]["type"] == "加仓上限价"
        assert _as_decimal(by_name["贵州茅台"]["price"]) == Decimal("1200")
        body = client.get("/api/positions").json()
        positions = {row["name"]: row for row in body["positions"]}
        assert positions["招商银行"]["rule_light"] == "hit"
        assert positions["贵州茅台"]["rule_light"] == "hit"
        assert _as_decimal(positions["招商银行"]["price"]) == Decimal("40")
        assert _as_decimal(positions["招商银行"]["market_value"]) == Decimal("24000")
        factory = client.app.state.session_factory
        with factory() as session:
            rows, cash = load_positions(session)
            book = project_book(session, rows, cash)
        assert isinstance(book, BookSnapshot)
        lights = {row.name: row.rule_light for row in book.positions}
        assert lights["招商银行"] == "hit"
        assert lights["贵州茅台"] == "hit"
        assert fake.calls

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == TRADE_COUNT
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == CASH_BALANCE


def test_price_below_ceiling_is_clear(tmp_path: Path) -> None:
    fake = _priced_fake("31", "1199")
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        body = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert by_name["招商银行"]["rule_light"] == "clear"
        assert by_name["贵州茅台"]["rule_light"] == "clear"
        assert _as_decimal(by_name["招商银行"]["price"]) == Decimal("31")


def test_none_without_ceiling_or_quote(tmp_path: Path) -> None:
    fake = FakeQuoteClient({"600036.SH": "40"})
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        before = client.get("/api/positions").json()
        by_before = {row["name"]: row for row in before["positions"]}
        assert by_before["招商银行"]["rule_light"] == "hit"
        assert by_before["贵州茅台"]["price"] is None
        assert by_before["贵州茅台"]["rule_light"] == "unknown"
        traded = client.post("/api/trades", json=_trade_payload())
        assert 200 <= traded.status_code < 300
        by_name = {row["name"]: row for row in traded.json()["positions"]}
        assert "测试票" in by_name
        assert by_name["测试票"]["rule_light"] == "none"
        assert by_name["测试票"]["price"] is None
        listed = client.get("/api/rules").json()["rules"]
        assert all(row["name"] != "测试票" for row in listed)


def test_empty_rules_do_not_500(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        listed = client.get("/api/rules")
        assert 200 <= listed.status_code < 300
        assert listed.json()["rules"] == []
        body = client.get("/api/positions")
        assert 200 <= body.status_code < 300
        assert body.json()["positions"] == []


def test_save_rule_lists_and_leaves_book(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        trade_count = _scalar(db_path, "SELECT COUNT(*) FROM trades")
        cash_before = _as_decimal(_scalar(db_path, "SELECT balance FROM cash"))
        saved = client.post(
            "/api/rules",
            json={
                "name": "测试票",
                "type": "加仓上限价",
                "price": "9.5",
                "note": "手工规则",
            },
        )
        assert 200 <= saved.status_code < 300
        rules = saved.json()["rules"]
        match = [
            row
            for row in rules
            if row["name"] == "测试票"
            and row["type"] == "加仓上限价"
            and _as_decimal(row["price"]) == Decimal("9.5")
        ]
        assert len(match) == 1
        assert match[0]["note"] == "手工规则"
        listed = client.get("/api/rules").json()["rules"]
        assert listed == rules
        after = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in after["positions"]}
        assert set(by_name) == {"招商银行", "贵州茅台"}
        assert _as_decimal(after["cash"]["balance"]) == cash_before

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == trade_count == TRADE_COUNT
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == cash_before
    assert _scalar(db_path, "SELECT COUNT(*) FROM rules") == 3


def test_save_ceiling_for_holding_updates_light(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = FakeQuoteClient({"600036.SH": "40"})
    with _client(tmp_path, fake=fake) as client:
        assert client.get("/api/health").status_code == 200
        engine = _engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO cash (user_id, balance, currency) "
                    "VALUES (1, '10000', 'CNY')"
                )
            )
        assert client.post("/api/trades", json=_trade_payload(name="兴业银行", shares=100, price=10)).status_code == 200
        assert (
            client.post(
                "/api/instruments", json={"name": "兴业银行", "code": "601166.SH"}
            ).status_code
            == 200
        )
        fake.prices["601166.SH"] = Decimal("18")
        assert client.post("/api/quotes/refresh", json={"name": "兴业银行"}).status_code == 200
        before = {
            row["name"]: row for row in client.get("/api/positions").json()["positions"]
        }
        assert before["兴业银行"]["rule_light"] == "none"
        saved = client.post(
            "/api/rules",
            json={"name": "兴业银行", "type": "加仓上限价", "price": "15"},
        )
        assert 200 <= saved.status_code < 300
        by_saved = {row["name"]: row for row in saved.json()["positions"]}
        assert by_saved["兴业银行"]["rule_light"] == "hit"
        latest = {
            row["name"]: row for row in client.get("/api/positions").json()["positions"]
        }
        assert latest["兴业银行"]["rule_light"] == "hit"
        assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 1


def test_save_rule_rejects_missing_nonpositive_unknown(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        before_rules = client.get("/api/rules").json()["rules"]
        trade_count = _scalar(db_path, "SELECT COUNT(*) FROM trades")
        cash_before = _as_decimal(_scalar(db_path, "SELECT balance FROM cash"))

        missing_name = client.post(
            "/api/rules",
            json={"type": "加仓上限价", "price": "10"},
        )
        assert missing_name.status_code == 400
        _assert_error_envelope(missing_name.json())
        assert missing_name.json()["code"] == "INVALID_RULE"

        blank_name = client.post(
            "/api/rules",
            json={"name": "  ", "type": "加仓上限价", "price": "10"},
        )
        assert blank_name.status_code == 400
        _assert_error_envelope(blank_name.json())

        missing = client.post("/api/rules", json={"name": "招商银行"})
        assert missing.status_code == 400
        _assert_error_envelope(missing.json())

        unknown = client.post(
            "/api/rules",
            json={"name": "招商银行", "type": "止损价", "price": "10"},
        )
        assert unknown.status_code == 400
        _assert_error_envelope(unknown.json())
        assert unknown.json()["code"] == "INVALID_TYPE"

        zero = client.post(
            "/api/rules",
            json={"name": "招商银行", "type": "加仓上限价", "price": "0"},
        )
        assert zero.status_code == 400
        _assert_error_envelope(zero.json())

        negative = client.post(
            "/api/rules",
            json={"name": "招商银行", "type": "加仓上限价", "price": "-1"},
        )
        assert negative.status_code == 400
        _assert_error_envelope(negative.json())

        after_rules = client.get("/api/rules").json()["rules"]
        assert after_rules == before_rules

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == trade_count
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == cash_before
    assert _scalar(db_path, "SELECT COUNT(*) FROM rules") == 2


def test_other_type_listed_but_ignored_by_light(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        assert client.get("/api/health").status_code == 200
        _insert_rule(
            db_path,
            name="招商银行",
            rule_type="止损价",
            price="1",
            note="其它类型",
        )
        listed = client.get("/api/rules").json()["rules"]
        types = {(row["name"], row["type"]) for row in listed}
        assert ("招商银行", "加仓上限价") in types
        assert ("招商银行", "止损价") in types
        body = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert by_name["招商银行"]["rule_light"] == "hit"

        traded = client.post("/api/trades", json=_trade_payload())
        assert 200 <= traded.status_code < 300
        _insert_rule(db_path, name="测试票", rule_type="止损价", price="3")
        latest = client.get("/api/positions").json()
        by_latest = {row["name"]: row for row in latest["positions"]}
        assert by_latest["测试票"]["rule_light"] == "none"
        names = [row["name"] for row in client.get("/api/rules").json()["rules"]]
        assert "测试票" in names


def test_any_ceiling_hit_wins(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake("40", "1500")
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        assert client.get("/api/health").status_code == 200
        _insert_rule(db_path, name="招商银行", rule_type="加仓上限价", price="50")
        body = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert by_name["招商银行"]["rule_light"] == "hit"


def test_equal_price_is_hit(tmp_path: Path) -> None:
    fake = _priced_fake("32", "1200")
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        body = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["price"]) == Decimal("32")
        assert by_name["招商银行"]["rule_light"] == "hit"
        assert by_name["贵州茅台"]["rule_light"] == "hit"


def test_stale_quote_still_lights(tmp_path: Path) -> None:
    first = _priced_fake()
    with _client(tmp_path, fake=first, yaml_dir=FIXTURES) as client:
        assert client.post("/api/quotes/refresh", json={}).status_code == 200

    failed = _priced_fake(failures=("600036.SH",))
    with _client(tmp_path, fake=failed, yaml_dir=FIXTURES) as client:
        body = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert by_name["招商银行"]["stale"] is True
        assert _as_decimal(by_name["招商银行"]["price"]) == Decimal("40")
        assert by_name["招商银行"]["rule_light"] == "hit"


def test_rules_module_has_no_http_and_migrate_keys_unchanged() -> None:
    rules_src = (ROOT / "ledger" / "rules.py").read_text(encoding="utf-8")
    assert "httpx" not in rules_src
    assert "httpx2" not in rules_src
    assert "fastapi" not in rules_src
    assert "import httpx" not in rules_src
    migrate_src = (ROOT / "migrate" / "yaml_portfolio.py").read_text(encoding="utf-8")
    assert 'item["名称"]' in migrate_src
    assert 'item["类型"]' in migrate_src
    assert 'item["价格"]' in migrate_src
    assert 'item.get("说明")' in migrate_src
    quotes_src = (ROOT / "ledger" / "quotes.py").read_text(encoding="utf-8")
    assert "class BookSnapshot" in quotes_src
    assert "rule_light" in quotes_src
    assert "project_book" in quotes_src
    main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "project_book" in main_src
    assert '"rule_light": row.rule_light' in main_src


def test_positions_view_rules_list_form_and_lights() -> None:
    source = (ROOT / "web/src/views/PositionsView.vue").read_text(encoding="utf-8")
    assert "/api/rules" in source
    assert "规则灯" in source
    assert "触及" in source
    assert "未触及" in source
    assert "无规则" in source
    assert "保存规则" in source
    assert "加仓上限价" in source
    assert "ruleForm" in source
    assert "ruleLightLabel" in source
    table = source.split('<table class="book"')[1].split("</table>")[0]
    assert "form.shares" not in table
    assert "contenteditable" not in table.lower()
    assert "<button" not in table.lower()
    assert "下单" not in table
    assert not any(
        "下单" in line and "<button" in line.lower() for line in source.splitlines()
    )
    assert "ruleLightLabel(row.rule_light)" in table
    assert "if (light === 'hit') return '触及'" in source
    assert "if (light === 'clear') return '未触及'" in source
    assert "data.positions" in source
    assert "<a " not in table.lower()
    assert "deleteRule" in source
    assert "DELETE" in source
    assert "删除" in source


def test_delete_rule_updates_light_and_leaves_book(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        listed = client.get("/api/rules").json()["rules"]
        cmb = next(row for row in listed if row["name"] == "招商银行")
        trade_count = _scalar(db_path, "SELECT COUNT(*) FROM trades")
        cash_before = _as_decimal(_scalar(db_path, "SELECT balance FROM cash"))
        deleted = client.delete(f"/api/rules/{cmb['id']}")
        assert 200 <= deleted.status_code < 300
        names = [row["name"] for row in deleted.json()["rules"]]
        assert "招商银行" not in names
        assert "贵州茅台" in names
        by_name = {row["name"]: row for row in deleted.json()["positions"]}
        assert by_name["招商银行"]["rule_light"] == "none"
        assert by_name["贵州茅台"]["rule_light"] == "hit"
        missing = client.delete("/api/rules/99999")
        assert missing.status_code == 404
        _assert_error_envelope(missing.json())
        assert missing.json()["code"] == "NOT_FOUND"
    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == trade_count == TRADE_COUNT
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == cash_before
    assert _scalar(db_path, "SELECT COUNT(*) FROM rules") == 1


def test_sync_thesis_ceiling_rule_preserves_manual_and_upserts_auto(tmp_path: Path) -> None:
    from ledger.rules import list_rules, sync_thesis_ceiling_rule

    with _client(tmp_path, yaml_dir=FIXTURES) as client:
        factory = client.app.state.session_factory
        with factory() as session:
            # Manual rule on 招商银行 (price=32) is preserved and not overwritten
            assert (
                sync_thesis_ceiling_rule(
                    session,
                    name="招商银行",
                    ceiling_price=Decimal("45"),
                    research_job_id=10,
                )
                is None
            )
            # New stock without manual rule gets an auto-synced rule
            created = sync_thesis_ceiling_rule(
                session,
                name="宁德时代",
                ceiling_price=Decimal("260"),
                research_job_id=11,
            )
            assert created is not None
            assert created.price == Decimal("260")
            assert "论文自动同步" in (created.note or "")
            # Subsequent thesis update updates the existing auto-synced rule in place
            updated = sync_thesis_ceiling_rule(
                session,
                name="宁德时代",
                ceiling_price=Decimal("280"),
                research_job_id=12,
            )
            session.commit()
            assert updated is not None
            assert updated.id == created.id
            assert updated.price == Decimal("280")
            all_rules = {r.name: r for r in list_rules(session)}
            assert all_rules["招商银行"].price == Decimal("32")
            assert all_rules["宁德时代"].price == Decimal("280")

