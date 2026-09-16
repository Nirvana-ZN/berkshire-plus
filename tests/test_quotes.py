from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from market.quotes import FakeQuoteClient, parse_tencent_body

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "portfolio"
CASH_BALANCE = Decimal("123456.78")


def _engine(db_path: Path):
    return create_engine(f"sqlite:///{db_path.resolve().as_posix()}")


def _scalar(db_path: Path, sql: str, params: dict | None = None):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


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


def _seed_cash(db_path: Path, balance: str) -> None:
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cash (user_id, balance, currency) "
                "VALUES (1, :balance, 'CNY')"
            ),
            {"balance": balance},
        )


def _trade_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "date": "2026-08-28",
        "direction": "buy",
        "name": "招商银行",
        "shares": 100,
        "price": 10,
    }
    body.update(overrides)
    return body


def test_refresh_success_writes_quote_and_computes_book(tmp_path: Path) -> None:
    fake = FakeQuoteClient({"600036.SH": "40", "600519.SH": "1500"})
    db_path = tmp_path / "app.db"
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["lock"] is None
        before = client.get("/api/positions").json()
        by_before = {row["name"]: row for row in before["positions"]}
        assert _as_decimal(by_before["招商银行"]["price"]) == Decimal("40")
        assert _as_decimal(by_before["招商银行"]["market_value"]) == Decimal("24000")

        response = client.post("/api/quotes/refresh", json={})
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        cmb = by_name["招商银行"]
        moutai = by_name["贵州茅台"]
        assert _as_decimal(cmb["price"]) == Decimal("40")
        assert _as_decimal(cmb["market_value"]) == Decimal("24000")
        assert _as_decimal(cmb["pnl"]) == Decimal("2850")
        assert cmb["stale"] is False
        assert cmb["source"] == "auto"
        assert cmb["code"] == "600036.SH"
        assert _as_decimal(moutai["price"]) == Decimal("1500")
        assert _as_decimal(moutai["market_value"]) == Decimal("75000")
        assert _as_decimal(moutai["pnl"]) == Decimal("5000")
        denom = Decimal("24000") + Decimal("75000") + CASH_BALANCE
        assert abs(_as_decimal(cmb["weight"]) - Decimal("24000") / denom) < Decimal("1e-9")
        assert abs(_as_decimal(moutai["weight"]) - Decimal("75000") / denom) < Decimal(
            "1e-9"
        )
        assert not body.get("errors")
        assert client.get("/api/health").json()["lock"] is None

    assert _scalar(db_path, "SELECT COUNT(*) FROM quotes") == 2
    assert _scalar(db_path, "SELECT stale FROM quotes WHERE name = '招商银行'") in (
        0,
        False,
    )
    assert _scalar(db_path, "SELECT source FROM quotes WHERE name = '招商银行'") == "auto"
    assert fake.calls
    assert "httpx" not in Path(ROOT / "ledger" / "quotes.py").read_text(encoding="utf-8")


def test_refresh_failure_keeps_old_price_and_marks_stale(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    first = FakeQuoteClient({"600036.SH": "40", "600519.SH": "1500"})
    with _client(tmp_path, fake=first, yaml_dir=FIXTURES) as client:
        assert client.post("/api/quotes/refresh", json={}).status_code == 200
        assert _as_decimal(
            {row["name"]: row for row in client.get("/api/positions").json()["positions"]}[
                "招商银行"
            ]["price"]
        ) == Decimal("40")

    failed = FakeQuoteClient(
        {"600036.SH": "40", "600519.SH": "1500"},
        failures=("600036.SH",),
    )
    with _client(tmp_path, fake=failed, yaml_dir=FIXTURES) as client:
        response = client.post("/api/quotes/refresh", json={})
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert "招商银行" in by_name
        assert "贵州茅台" in by_name
        assert _as_decimal(by_name["招商银行"]["price"]) == Decimal("40")
        assert by_name["招商银行"]["stale"] is True
        assert _as_decimal(by_name["招商银行"]["market_value"]) == Decimal("24000")
        assert by_name["贵州茅台"]["stale"] is False
        assert client.get("/api/health").json()["lock"] is None
        latest = client.get("/api/positions").json()
        assert {row["name"] for row in latest["positions"]} == {"招商银行", "贵州茅台"}

    assert _scalar(db_path, "SELECT COUNT(*) FROM quotes") == 2
    assert _as_decimal(
        _scalar(db_path, "SELECT price FROM quotes WHERE name = '招商银行'")
    ) == Decimal("40")
    stale = _scalar(db_path, "SELECT stale FROM quotes WHERE name = '招商银行'")
    assert stale in (1, True)

    recovered = FakeQuoteClient({"600036.SH": "41", "600519.SH": "1500"})
    with _client(tmp_path, fake=recovered, yaml_dir=FIXTURES) as client:
        response = client.post("/api/quotes/refresh", json={})
        assert 200 <= response.status_code < 300
        cmb = {
            row["name"]: row for row in response.json()["positions"]
        }["招商银行"]
        assert cmb["stale"] is False
        assert _as_decimal(cmb["price"]) == Decimal("41")

    assert _scalar(db_path, "SELECT stale FROM quotes WHERE name = '招商银行'") in (
        0,
        False,
    )


def test_bulk_refresh_without_code_is_2xx_with_errors(tmp_path: Path) -> None:
    fake = FakeQuoteClient({"600036.SH": "40"})
    db_path = tmp_path / "app.db"
    with _client(tmp_path, fake=fake) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        assert (
            client.post(
                "/api/trades",
                json=_trade_payload(name="未知票", shares=10, price=8),
            ).status_code
            == 200
        )
        bound = client.post(
            "/api/instruments", json={"name": "招商银行", "code": "600036.SH"}
        )
        assert 200 <= bound.status_code < 300
        response = client.post("/api/quotes/refresh", json={})
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["price"]) == Decimal("40")
        assert by_name["未知票"]["price"] is None
        assert by_name["未知票"]["market_value"] is None
        assert by_name["未知票"]["pnl"] is None
        cash_after = _as_decimal(body["cash"]["balance"])
        denom = Decimal("4000") + cash_after
        assert by_name["未知票"]["weight"] is None
        assert by_name["未知票"]["market_value"] is None
        assert abs(_as_decimal(by_name["招商银行"]["weight"]) - Decimal("4000") / denom) < Decimal(
            "1e-9"
        )
        errors = body["errors"]
        assert any(item["name"] == "未知票" and item["code"] == "NO_CODE" for item in errors)
        assert all(item["error"] and item["detail"] for item in errors)

    assert _scalar(db_path, "SELECT COUNT(*) FROM quotes WHERE name = '未知票'") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM quotes WHERE name = '招商银行'") == 1


def test_single_refresh_without_code_is_400_and_writes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert (
            client.post(
                "/api/trades", json=_trade_payload(name="未知票")
            ).status_code
            == 200
        )
        response = client.post("/api/quotes/refresh", json={"name": "未知票"})
        assert response.status_code == 400
        _assert_error_envelope(response.json())
        assert response.json()["code"] == "NO_CODE"

    assert _scalar(db_path, "SELECT COUNT(*) FROM quotes") == 0


def test_bind_code_then_refresh(tmp_path: Path) -> None:
    fake = FakeQuoteClient({"601166.SH": "18.5"})
    db_path = tmp_path / "app.db"
    with _client(tmp_path, fake=fake) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert (
            client.post(
                "/api/trades", json=_trade_payload(name="兴业银行", shares=200, price=10)
            ).status_code
            == 200
        )
        illegal = client.post(
            "/api/instruments", json={"name": "兴业银行", "code": "601166.HK"}
        )
        assert illegal.status_code == 400
        _assert_error_envelope(illegal.json())
        assert illegal.json()["code"] == "INVALID_CODE"
        saved = client.post(
            "/api/instruments", json={"name": "兴业银行", "code": "601166.SH"}
        )
        assert 200 <= saved.status_code < 300
        response = client.post("/api/quotes/refresh", json={"name": "兴业银行"})
        assert 200 <= response.status_code < 300
        row = {item["name"]: item for item in response.json()["positions"]}["兴业银行"]
        assert _as_decimal(row["price"]) == Decimal("18.5")
        assert _as_decimal(row["market_value"]) == Decimal("3700")
        assert _as_decimal(row["pnl"]) == Decimal("1700")
        assert row["stale"] is False
        assert row["code"] == "601166.SH"

    assert _scalar(db_path, "SELECT code FROM instruments WHERE name = '兴业银行'") == (
        "601166.SH"
    )


def test_manual_price_source_distinct_from_auto(tmp_path: Path) -> None:
    fake = FakeQuoteClient({"600036.SH": "40"})
    db_path = tmp_path / "app.db"
    with _client(tmp_path, fake=fake) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        assert (
            client.post(
                "/api/trades",
                json=_trade_payload(name="手工票", shares=50, price=8),
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/instruments", json={"name": "招商银行", "code": "600036.SH"}
            ).status_code
            == 200
        )
        auto = client.post("/api/quotes/refresh", json={"name": "招商银行"})
        assert 200 <= auto.status_code < 300
        zero = client.post("/api/quotes/manual", json={"name": "手工票", "price": 0})
        assert zero.status_code == 400
        _assert_error_envelope(zero.json())
        assert zero.json()["code"] == "INVALID_PRICE"
        negative = client.post(
            "/api/quotes/manual", json={"name": "手工票", "price": -1}
        )
        assert negative.status_code == 400
        manual = client.post(
            "/api/quotes/manual", json={"name": "手工票", "price": "12.5"}
        )
        assert 200 <= manual.status_code < 300
        by_name = {row["name"]: row for row in manual.json()["positions"]}
        assert by_name["手工票"]["source"] == "manual"
        assert by_name["手工票"]["stale"] is False
        assert _as_decimal(by_name["手工票"]["price"]) == Decimal("12.5")
        assert _as_decimal(by_name["手工票"]["market_value"]) == Decimal("625")
        assert by_name["招商银行"]["source"] == "auto"
        assert by_name["招商银行"]["source"] != by_name["手工票"]["source"]
        latest = client.get("/api/positions").json()
        sources = {row["name"]: row["source"] for row in latest["positions"]}
        assert sources["手工票"] == "manual"
        assert sources["招商银行"] == "auto"

    assert _scalar(db_path, "SELECT source FROM quotes WHERE name = '手工票'") == "manual"
    assert _scalar(db_path, "SELECT stale FROM quotes WHERE name = '手工票'") in (
        0,
        False,
    )
    assert _scalar(db_path, "SELECT COUNT(*) FROM quotes WHERE name = '手工票'") == 1


def test_first_time_fetch_miss_is_2xx_with_quote_failed(tmp_path: Path) -> None:
    fake = FakeQuoteClient(failures=("600036.SH",))
    db_path = tmp_path / "app.db"
    with _client(tmp_path, fake=fake) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        assert (
            client.post(
                "/api/instruments", json={"name": "招商银行", "code": "600036.SH"}
            ).status_code
            == 200
        )
        response = client.post("/api/quotes/refresh", json={})
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert by_name["招商银行"]["price"] is None
        errors = body["errors"]
        assert any(
            item["name"] == "招商银行" and item["code"] == "QUOTE_FAILED" for item in errors
        )

    assert _scalar(db_path, "SELECT COUNT(*) FROM quotes WHERE name = '招商银行'") == 0


def test_empty_name_refresh_is_400_not_bulk(tmp_path: Path) -> None:
    fake = FakeQuoteClient({"600036.SH": "40"})
    db_path = tmp_path / "app.db"
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        calls_before = len(fake.calls)
        quotes_before = _scalar(db_path, "SELECT COUNT(*) FROM quotes")
        blank = client.post("/api/quotes/refresh", json={"name": "  "})
        assert blank.status_code == 400
        _assert_error_envelope(blank.json())
        assert blank.json()["code"] == "INVALID_QUOTE"
        malformed = client.post(
            "/api/quotes/refresh",
            content=b"{",
            headers={"content-type": "application/json"},
        )
        assert malformed.status_code == 400
        _assert_error_envelope(malformed.json())
        assert malformed.json()["code"] == "INVALID_QUOTE"
        assert len(fake.calls) == calls_before
        assert _scalar(db_path, "SELECT COUNT(*) FROM quotes") == quotes_before


def test_trade_response_keeps_existing_quote_fields(tmp_path: Path) -> None:
    fake = FakeQuoteClient({"600036.SH": "40"})
    db_path = tmp_path / "app.db"
    with _client(tmp_path, fake=fake) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        assert (
            client.post(
                "/api/instruments", json={"name": "招商银行", "code": "600036.SH"}
            ).status_code
            == 200
        )
        refreshed = client.post("/api/quotes/refresh", json={"name": "招商银行"})
        assert 200 <= refreshed.status_code < 300
        traded = client.post(
            "/api/trades", json=_trade_payload(shares=10, price=10)
        )
        assert traded.status_code == 200
        row = {item["name"]: item for item in traded.json()["positions"]}["招商银行"]
        assert _as_decimal(row["price"]) == Decimal("40")
        assert row["stale"] is False
        assert row["source"] == "auto"
        assert row["code"] == "600036.SH"
        assert row["market_value"] is not None


def test_tencent_client_returns_a_share_keys(monkeypatch) -> None:
    from unittest.mock import MagicMock

    import httpx2

    from market.quotes import TencentQuoteClient, to_tencent_symbol

    assert to_tencent_symbol("000001.SZ") == "sz000001"
    assert to_tencent_symbol("430047.BJ") == "bj430047"

    body = (
        'v_sh600036="1~招商银行~600036~35.25~35.20~35.18~123";'
        'v_sz000001="1~平安银行~000001~12.5~12~12~1";'
        'v_bj430047="1~诺德股份~430047~8.1~8~8~1";'
    ).encode("utf-8")
    response = MagicMock()
    response.content = body
    response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get.return_value = response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)

    fetched = TencentQuoteClient().fetch_prices(
        ["600036.SH", "000001.SZ", "430047.BJ"]
    )
    assert fetched["600036.SH"] == Decimal("35.25")
    assert fetched["000001.SZ"] == Decimal("12.5")
    assert fetched["430047.BJ"] == Decimal("8.1")
    url = mock_client.get.call_args.args[0]
    assert "sh600036" in url
    assert "sz000001" in url
    assert "bj430047" in url


def test_tencent_client_batches_long_symbol_lists(monkeypatch) -> None:
    from unittest.mock import MagicMock

    import httpx2

    from market.quotes import TENCENT_SYMBOL_BATCH, TencentQuoteClient

    codes = [f"{index:06d}.SH" for index in range(TENCENT_SYMBOL_BATCH + 1)]
    response = MagicMock()
    response.content = b""
    response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get.return_value = response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)

    TencentQuoteClient().fetch_prices(codes)
    assert mock_client.get.call_count == 2
    first_url = mock_client.get.call_args_list[0].args[0]
    second_url = mock_client.get.call_args_list[1].args[0]
    assert first_url.count(",") == TENCENT_SYMBOL_BATCH - 1
    assert second_url.count(",") == 0


def test_ledger_modules_do_not_import_http(tmp_path: Path) -> None:
    ledger_dir = ROOT / "ledger"
    for path in ledger_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import httpx" not in source
        assert "import httpx2" not in source
        assert "from httpx" not in source
        assert "from httpx2" not in source
    market_source = (ROOT / "market" / "quotes.py").read_text(encoding="utf-8")
    assert "sqlalchemy" not in market_source
    assert "app.models" not in market_source


def test_parse_tencent_body_extracts_positive_prices() -> None:
    body = (
        'v_sh600036="1~招商银行~600036~35.25~35.20~35.18~123";'
        'v_sz300750="";'
        'v_sh600519="1~贵州茅台~600519~0~1400";'
    )
    parsed = parse_tencent_body(body)
    assert parsed["sh600036"] == Decimal("35.25")
    assert "sz300750" not in parsed
    assert "sh600519" not in parsed


def test_positions_view_has_quote_columns_and_forms() -> None:
    source = (ROOT / "web/src/views/PositionsView.vue").read_text(encoding="utf-8")
    assert "/api/quotes/refresh" in source
    assert "/api/quotes/manual" in source
    assert "/api/instruments" in source
    assert "现价" in source
    assert "市值" in source
    assert "占比" in source
    assert "浮盈亏" in source
    assert "收益率" in source
    assert "过期" in source
    assert "刷新现价" in source
    assert "补代码" in source
    assert "手工价" in source
    assert "手工" in source
    assert 'v-if="row.stale"' in source
    assert "isManualQuote(row.source)" in source
    assert "data.errors" in source
    table = source.split('<table class="book"')[1].split("</table>")[0]
    price_cell = table.split("formatNumber(row.price)")[1].split("formatWeight")[0]
    assert "自动" not in price_cell
    assert "手工" in price_cell
    assert "<th>收益率</th>" in table
    assert "row.market_value" in table
    assert "form.shares" not in table
    assert "contenteditable" not in table.lower()
    assert 'v-model="form' not in table
