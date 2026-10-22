from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from market.quotes import FakeQuoteClient

ROOT = Path(__file__).resolve().parents[1]


def _engine(db_path: Path):
    from app.db import create_db_engine
    return create_db_engine(db_path)



def _scalar(db_path: Path, sql: str, params: dict | None = None):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def _rows(db_path: Path, sql: str):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return list(conn.execute(text(sql)).mappings())


FIXTURES = ROOT / "tests" / "fixtures" / "portfolio"
CASH_BALANCE = Decimal("123456.78")
REPLAY_CASH = Decimal("-85200")


def _client(tmp_path: Path, *, db_name: str = "app.db", yaml_dir: Path | None = None):
    kwargs: dict[str, object] = {
        "db_path": tmp_path / db_name,
        "quote_client": FakeQuoteClient(),
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


def test_buy_writes_trade_updates_cost_and_cash(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        response = client.post("/api/trades", json=_trade_payload())
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["shares"]) == Decimal("100")
        assert _as_decimal(by_name["招商银行"]["cost"]) == Decimal("1000")
        assert _as_decimal(body["cash"]["balance"]) == Decimal("9000")
        assert body["cash"]["currency"] == "CNY"
        assert not body.get("warning")

        latest = client.get("/api/positions").json()
        assert latest["positions"] == body["positions"]
        assert latest["cash"] == body["cash"]

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 1
    assert _scalar(db_path, "SELECT direction FROM trades") == "buy"
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == Decimal("9000")


def test_chinese_buy_direction_is_accepted(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        response = client.post("/api/trades", json=_trade_payload(direction="买"))
        assert 200 <= response.status_code < 300
    assert _scalar(db_path, "SELECT direction FROM trades") == "buy"


def test_adjust_shares_cost_does_not_move_cash(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        response = client.post(
            "/api/positions/adjust",
            json={"name": "招商银行", "shares": 80, "unit_cost": 12},
        )
        assert response.status_code == 200
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["shares"]) == Decimal("80")
        assert _as_decimal(by_name["招商银行"]["cost"]) == Decimal("960")
        assert _as_decimal(body["cash"]["balance"]) == Decimal("9000")
        sell = client.post(
            "/api/trades",
            json=_trade_payload(date="2099-01-01", direction="sell", shares=90, price=12),
        )
        assert sell.status_code == 400
        assert sell.json()["code"] == "SELL_EXCEEDS"
        cash = client.post("/api/positions/cash", json={"balance": 50000})
        assert cash.status_code == 200
        assert _as_decimal(cash.json()["cash"]["balance"]) == Decimal("50000")
        assert _as_decimal(cash.json()["positions"][0]["shares"]) == Decimal("80")
    assert _scalar(db_path, "SELECT direction FROM trades ORDER BY id DESC LIMIT 1") == "set"


def test_buy_before_latest_set_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        assert (
            client.post(
                "/api/positions/adjust",
                json={"name": "招商银行", "shares": 120, "unit_cost": 10},
            ).status_code
            == 200
        )
        before = client.get("/api/positions").json()
        backfill = client.post(
            "/api/trades",
            json=_trade_payload(date="2026-06-01", shares=50, price=9),
        )
        assert backfill.status_code == 400
        assert backfill.json()["code"] == "BEFORE_SET"
        after = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in after["positions"]}
        assert _as_decimal(by_name["招商银行"]["shares"]) == Decimal("120")
        assert after["cash"] == before["cash"]
        later = client.post(
            "/api/trades",
            json=_trade_payload(date="2099-01-01", shares=50, price=10),
        )
        assert later.status_code == 200
        by_name = {row["name"]: row for row in later.json()["positions"]}
        assert _as_decimal(by_name["招商银行"]["shares"]) == Decimal("170")


def test_sell_reduces_average_cost_and_adds_cash(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        response = client.post(
            "/api/trades",
            json=_trade_payload(direction="sell", shares=40, price=12),
        )
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["shares"]) == Decimal("60")
        assert _as_decimal(by_name["招商银行"]["cost"]) == Decimal("600")
        assert _as_decimal(body["cash"]["balance"]) == Decimal("9480")

        latest = client.get("/api/positions").json()
        assert latest["positions"] == body["positions"]
        assert latest["cash"] == body["cash"]


def test_moving_average_cost_after_two_buys_and_partial_sell(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "100000")
        assert (
            client.post(
                "/api/trades", json=_trade_payload(shares=100, price=10)
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/trades",
                json=_trade_payload(date="2026-08-29", shares=100, price=20),
            ).status_code
            == 200
        )
        response = client.post(
            "/api/trades",
            json=_trade_payload(
                date="2026-08-30", direction="sell", shares=50, price=30
            ),
        )
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["shares"]) == Decimal("150")
        assert _as_decimal(by_name["招商银行"]["cost"]) == Decimal("2250")
        assert _as_decimal(body["cash"]["balance"]) == Decimal("98500")


def test_sell_exceeds_leaves_book_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        before_positions = client.get("/api/positions").json()
        response = client.post(
            "/api/trades",
            json=_trade_payload(direction="sell", shares=101, price=12),
        )
        assert response.status_code == 400
        body = response.json()
        _assert_error_envelope(body)
        assert body["code"] == "SELL_EXCEEDS"

        after = client.get("/api/positions").json()
        assert after["positions"] == before_positions["positions"]
        assert after["cash"] == before_positions["cash"]

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 1
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == Decimal("9000")


def test_buy_with_negative_cash_writes_and_warns(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        response = client.post("/api/trades", json=_trade_payload())
        assert 200 <= response.status_code < 300
        body = response.json()
        assert "warning" in body
        assert "现金不足" in str(body["warning"])
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["shares"]) == Decimal("100")
        assert _as_decimal(by_name["招商银行"]["cost"]) == Decimal("1000")
        assert _as_decimal(body["cash"]["balance"]) == Decimal("-1000")

        latest = client.get("/api/positions").json()
        assert _as_decimal(latest["positions"][0]["shares"]) == Decimal("100")
        assert _as_decimal(latest["cash"]["balance"]) == Decimal("-1000")

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 1
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == Decimal("-1000")


def test_sell_all_removes_name_from_positions(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        response = client.post(
            "/api/trades",
            json=_trade_payload(direction="sell", shares=100, price=11),
        )
        assert 200 <= response.status_code < 300
        body = response.json()
        names = {row["name"] for row in body["positions"]}
        assert "招商银行" not in names
        assert _as_decimal(body["cash"]["balance"]) == Decimal("10100")

        latest = client.get("/api/positions").json()
        assert "招商银行" not in {row["name"] for row in latest["positions"]}


def test_invalid_trade_payload_returns_400_envelope(tmp_path: Path) -> None:
    cases = [
        {k: v for k, v in _trade_payload().items() if k != "name"},
        _trade_payload(shares=0),
        _trade_payload(shares=-1),
        _trade_payload(price=-0.01),
        _trade_payload(price=0),
        _trade_payload(shares="NaN"),
        _trade_payload(price="Infinity"),
        _trade_payload(date="2026/08/28"),
        _trade_payload(date="not-a-date"),
        _trade_payload(direction="hold"),
    ]
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        for payload in cases:
            response = client.post("/api/trades", json=payload)
            assert response.status_code == 400, payload
            _assert_error_envelope(response.json())
            assert response.json()["code"] != "SELL_EXCEEDS"

    db_path = tmp_path / "app.db"
    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 0


def test_optional_note_is_stored(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        response = client.post(
            "/api/trades", json=_trade_payload(note="加仓")
        )
        assert 200 <= response.status_code < 300
    assert _scalar(db_path, "SELECT note FROM trades") == "加仓"


def test_string_shares_and_price_still_book(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        response = client.post(
            "/api/trades",
            json=_trade_payload(shares="100", price="10"),
        )
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["shares"]) == Decimal("100")
        assert _as_decimal(by_name["招商银行"]["cost"]) == Decimal("1000")
        assert _as_decimal(body["cash"]["balance"]) == Decimal("9000")


def test_fractional_price_matches_decimal_math(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        response = client.post(
            "/api/trades",
            json=_trade_payload(shares=100, price=35.25),
        )
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        expected_cost = Decimal("100") * Decimal("35.25")
        assert _as_decimal(by_name["招商银行"]["cost"]) == expected_cost
        assert _as_decimal(body["cash"]["balance"]) == Decimal("10000") - expected_cost


def test_sell_without_holding_is_sell_exceeds(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        _seed_cash(db_path, "10000")
        response = client.post(
            "/api/trades",
            json=_trade_payload(direction="sell", shares=10, price=12),
        )
        assert response.status_code == 400
        body = response.json()
        _assert_error_envelope(body)
        assert body["code"] == "SELL_EXCEEDS"
        assert "0" in str(body["detail"])
        assert "10" in str(body["detail"])

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 0
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == Decimal("10000")


def test_buy_after_migrate_moves_yaml_cash_not_replay(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path, yaml_dir=FIXTURES) as client:
        settings = client.get("/api/settings").json()
        assert settings["migrated"] is True
        before = client.get("/api/positions").json()
        assert _as_decimal(before["cash"]["balance"]) == CASH_BALANCE
        assert _as_decimal(before["cash"]["balance"]) != REPLAY_CASH
        response = client.post("/api/trades", json=_trade_payload(shares=100, price=10))
        assert 200 <= response.status_code < 300
        body = response.json()
        expected_cash = CASH_BALANCE - Decimal("1000")
        assert _as_decimal(body["cash"]["balance"]) == expected_cash
        assert _as_decimal(body["cash"]["balance"]) != REPLAY_CASH - Decimal("1000")
        latest = client.get("/api/positions").json()
        assert _as_decimal(latest["cash"]["balance"]) == expected_cash

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 6
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == CASH_BALANCE - Decimal(
        "1000"
    )


def test_positions_view_can_edit_shares_cost_and_cash() -> None:
    source = (ROOT / "web/src/views/PositionsView.vue").read_text(encoding="utf-8")
    assert "/api/trades" in source
    assert "/api/positions/adjust" in source
    assert "/api/positions/cash" in source
    assert "保存账面" in source
    assert "股数、成本单价、现金可直接改" in source
    assert "只读" not in source
    assert "applyBook" in source
    table = source.split('<table class="book"')[1].split("</table>")[0]
    assert "bookForm[row.name]!.shares" in table
    assert "bookForm[row.name]!.unit_cost" in table
    assert "cashForm" in table
    assert 'v-model="form' not in table
    assert "form.shares" not in table
