from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from market.quotes import FakeQuoteClient

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "portfolio"
CASH_BALANCE = Decimal("123456.78")


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


def _priced_fake() -> FakeQuoteClient:
    return FakeQuoteClient({"600036.SH": "40", "600519.SH": "1500"})


def test_startup_seeds_weights_including_cash(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["lock"] is None
        body = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert by_name["招商银行"]["no_target"] is True
        assert by_name["招商银行"]["target"] is None
        assert by_name["贵州茅台"]["no_target"] is True
        assert by_name["贵州茅台"]["target"] is None
        assert body["cash"]["no_target"] is True
        assert body["cash"]["target"] is None
        assert body["cash"]["deviation"] is None

    names = {row["name"] for row in _rows(db_path, "SELECT name FROM position_targets")}
    assert names == set()
    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 5
    assert fake.calls


def test_existing_targets_are_not_overwritten_on_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        saved = client.post(
            "/api/targets",
            json={
                "targets": {
                    "招商银行": "0.2",
                    "贵州茅台": "0.3",
                    "__CASH__": "0.5",
                }
            },
        )
        assert 200 <= saved.status_code < 300
        assert _as_decimal(
            {row["name"]: row for row in saved.json()["positions"]}["招商银行"]["target"]
        ) == Decimal("0.2")

    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        body = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["target"]) == Decimal("0.2")
        assert _as_decimal(by_name["贵州茅台"]["target"]) == Decimal("0.3")
        assert _as_decimal(body["cash"]["target"]) == Decimal("0.5")
        assert by_name["招商银行"]["price"] is not None

    assert _as_decimal(
        _scalar(db_path, "SELECT target_weight FROM position_targets WHERE name = '招商银行'")
    ) == Decimal("0.2")


def test_invalid_sum_or_range_is_400_and_leaves_book(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        before = client.get("/api/positions").json()
        trade_count = _scalar(db_path, "SELECT COUNT(*) FROM trades")
        cash_before = _as_decimal(_scalar(db_path, "SELECT balance FROM cash"))
        targets_before = {
            row["name"]: _as_decimal(row["target_weight"])
            for row in _rows(db_path, "SELECT name, target_weight FROM position_targets")
        }

        bad_sum = client.post(
            "/api/targets",
            json={"targets": {"招商银行": "0.4", "__CASH__": "0.600002"}},
        )
        assert bad_sum.status_code == 400
        _assert_error_envelope(bad_sum.json())
        assert bad_sum.json()["code"] == "INVALID_TARGETS"

        out_of_range = client.post(
            "/api/targets",
            json={"targets": {"招商银行": "1.1", "__CASH__": "-0.1"}},
        )
        assert out_of_range.status_code == 400
        _assert_error_envelope(out_of_range.json())
        assert out_of_range.json()["code"] == "INVALID_TARGETS"

        negative_only = client.post(
            "/api/targets",
            json={
                "targets": {
                    "招商银行": "1",
                    "贵州茅台": "0.5",
                    "__CASH__": "-0.5",
                }
            },
        )
        assert negative_only.status_code == 400
        _assert_error_envelope(negative_only.json())
        assert negative_only.json()["code"] == "INVALID_TARGETS"

        after = client.get("/api/positions").json()
        assert after["positions"] == before["positions"]
        assert after["cash"]["balance"] == before["cash"]["balance"]

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == trade_count
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == cash_before
    targets_after = {
        row["name"]: _as_decimal(row["target_weight"])
        for row in _rows(db_path, "SELECT name, target_weight FROM position_targets")
    }
    assert targets_after == targets_before


def test_valid_sum_saves_and_does_not_change_trades(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        trade_count = _scalar(db_path, "SELECT COUNT(*) FROM trades")
        cash_before = _as_decimal(_scalar(db_path, "SELECT balance FROM cash"))
        response = client.post(
            "/api/targets",
            json={
                "targets": {
                    "招商银行": "0.2",
                    "贵州茅台": "0.3",
                    "__CASH__": "0.5",
                }
            },
        )
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["target"]) == Decimal("0.2")
        assert _as_decimal(by_name["贵州茅台"]["target"]) == Decimal("0.3")
        assert _as_decimal(body["cash"]["target"]) == Decimal("0.5")
        latest = client.get("/api/positions").json()
        assert latest["positions"] == body["positions"]
        assert latest["cash"]["target"] == body["cash"]["target"]

        bounds = client.post(
            "/api/targets",
            json={"targets": {"招商银行": "0", "贵州茅台": "0", "__CASH__": "1"}},
        )
        assert 200 <= bounds.status_code < 300
        bounded = {row["name"]: row for row in bounds.json()["positions"]}
        assert _as_decimal(bounded["招商银行"]["target"]) == Decimal("0")
        assert _as_decimal(bounds.json()["cash"]["target"]) == Decimal("1")

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == trade_count
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == cash_before


def test_tolerance_one_e_minus_six(tmp_path: Path) -> None:
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        within = client.post(
            "/api/targets",
            json={"targets": {"招商银行": "0.4", "__CASH__": "0.600001"}},
        )
        assert 200 <= within.status_code < 300
        over = client.post(
            "/api/targets",
            json={"targets": {"招商银行": "0.4", "__CASH__": "0.600002"}},
        )
        assert over.status_code == 400
        _assert_error_envelope(over.json())


def test_missing_target_still_listed_and_excluded_from_alert(tmp_path: Path) -> None:
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        response = client.post("/api/targets", json={"targets": {"__CASH__": 1}})
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert set(by_name) == {"招商银行", "贵州茅台"}
        assert by_name["招商银行"]["no_target"] is True
        assert by_name["贵州茅台"]["no_target"] is True
        assert by_name["招商银行"]["target"] is None
        assert by_name["招商银行"]["deviation"] is None
        assert "招商银行" not in body["rebalance"]
        assert "贵州茅台" not in body["rebalance"]
        assert "__CASH__" in body["rebalance"]


def test_unpriced_holding_has_no_seed_target(tmp_path: Path) -> None:
    fake = FakeQuoteClient({"600036.SH": "40"})
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        body = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert by_name["招商银行"]["no_target"] is True
        assert by_name["招商银行"]["target"] is None
        assert by_name["贵州茅台"]["weight"] is None
        assert body["cash"]["no_target"] is True


def test_rebalance_alert_threshold_is_strictly_over_three_pp(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = FakeQuoteClient({"600036.SH": "10"})
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
        assert client.post("/api/quotes/refresh", json={"name": "招商银行"}).status_code == 200
        # MV=1000, cash=9000, weight=0.1
        equal = client.post(
            "/api/targets",
            json={"targets": {"招商银行": "0.07", "__CASH__": "0.93"}},
        )
        assert 200 <= equal.status_code < 300
        assert equal.json()["rebalance"] == []
        over = client.post(
            "/api/targets",
            json={"targets": {"招商银行": "0.06", "__CASH__": "0.94"}},
        )
        assert 200 <= over.status_code < 300
        assert "招商银行" in over.json()["rebalance"]
        latest = client.get("/api/positions").json()
        assert "招商银行" in latest["rebalance"]


def test_no_weight_with_target_is_not_in_rebalance(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = FakeQuoteClient({"600036.SH": "40"})
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
        assert (
            client.post(
                "/api/instruments", json={"name": "招商银行", "code": "600036.SH"}
            ).status_code
            == 200
        )
        assert client.post("/api/quotes/refresh", json={}).status_code == 200
        response = client.post(
            "/api/targets",
            json={
                "targets": {
                    "招商银行": "0.5",
                    "未知票": "0.1",
                    "__CASH__": "0.4",
                }
            },
        )
        assert 200 <= response.status_code < 300
        body = response.json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert by_name["未知票"]["no_target"] is False
        assert by_name["未知票"]["weight"] is None
        assert by_name["未知票"]["deviation"] is None
        assert body["rebalance"] == []


def test_seed_uses_stale_weights_when_refresh_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    first = _priced_fake()
    with _client(tmp_path, fake=first, yaml_dir=FIXTURES) as client:
        assert client.get("/api/health").status_code == 200
    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM position_targets"))
    failed = FakeQuoteClient(
        {"600036.SH": "40", "600519.SH": "1500"},
        failures=("600036.SH", "600519.SH"),
    )
    with _client(tmp_path, fake=failed, yaml_dir=FIXTURES) as client:
        body = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in body["positions"]}
        assert by_name["招商银行"]["stale"] is True
        assert by_name["招商银行"]["no_target"] is True
        assert by_name["招商银行"]["target"] is None


def test_negative_cash_skips_seed(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = FakeQuoteClient({"600036.SH": "40"})
    with _client(tmp_path, fake=fake) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/trades", json=_trade_payload()).status_code == 200
        assert (
            client.post(
                "/api/instruments", json={"name": "招商银行", "code": "600036.SH"}
            ).status_code
            == 200
        )
        assert client.post("/api/quotes/refresh", json={}).status_code == 200
    assert _scalar(db_path, "SELECT COUNT(*) FROM position_targets") == 0
    with _client(tmp_path, fake=fake) as client:
        body = client.get("/api/positions").json()
        assert _as_decimal(body["cash"]["balance"]) < 0
        assert body["positions"][0]["no_target"] is True
        assert body["cash"]["no_target"] is True
    assert _scalar(db_path, "SELECT COUNT(*) FROM position_targets") == 0


def test_seed_after_refresh_without_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = FakeQuoteClient({"600036.SH": "10"})
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
        refreshed = client.post("/api/quotes/refresh", json={})
        assert refreshed.status_code == 200
        by_name = {row["name"]: row for row in refreshed.json()["positions"]}
        assert by_name["招商银行"]["no_target"] is True
        assert by_name["招商银行"]["target"] is None
        assert refreshed.json()["cash"]["no_target"] is True
    names = {row["name"] for row in _rows(db_path, "SELECT name FROM position_targets")}
    assert names == set()


def test_sell_all_moves_orphan_target_to_cash(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        assert (
            client.post(
                "/api/targets",
                json={"targets": {"招商银行": "0.15", "贵州茅台": "0.25"}},
            ).status_code
            == 200
        )
        before = client.get("/api/positions").json()
        by_name = {row["name"]: row for row in before["positions"]}
        sold = client.post(
            "/api/trades",
            json={
                "date": "2026-08-30",
                "direction": "sell",
                "name": "招商银行",
                "shares": by_name["招商银行"]["shares"],
                "price": 40,
            },
        )
        assert sold.status_code == 200
        after = sold.json()
        names = {row["name"] for row in after["positions"]}
        assert "招商银行" not in names
        assert after["cash"]["no_target"] is True
        assert after["cash"]["target"] is None
    leftover = {row["name"] for row in _rows(db_path, "SELECT name FROM position_targets")}
    assert leftover == {"贵州茅台"}


def test_save_targets_without_cash_allows_partial_sum(tmp_path: Path) -> None:
    fake = _priced_fake()
    with _client(tmp_path, fake=fake, yaml_dir=FIXTURES) as client:
        saved = client.post(
            "/api/targets",
            json={"targets": {"招商银行": "0.15", "贵州茅台": "0.20"}},
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["cash"]["no_target"] is True
        assert body["cash"]["target"] is None
        by_name = {row["name"]: row for row in body["positions"]}
        assert _as_decimal(by_name["招商银行"]["target"]) == Decimal("0.15")
        assert _as_decimal(by_name["贵州茅台"]["target"]) == Decimal("0.20")

        over_one = client.post(
            "/api/targets",
            json={"targets": {"招商银行": "0.60", "贵州茅台": "0.50"}},
        )
        assert over_one.status_code == 400
        assert over_one.json()["code"] == "INVALID_TARGETS"


def test_migrate_does_not_write_targets_and_ledger_has_no_http() -> None:
    for path in (ROOT / "migrate").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "position_targets" not in source
        assert "PositionTarget" not in source
    targets_src = (ROOT / "ledger" / "targets.py").read_text(encoding="utf-8")
    assert "httpx" not in targets_src
    assert "httpx2" not in targets_src
    assert "import httpx" not in targets_src


def test_positions_view_shows_targets_without_order_buttons() -> None:
    source = (ROOT / "web/src/views/PositionsView.vue").read_text(encoding="utf-8")
    assert "/api/targets" in source
    assert "目标仓位" in source
    assert "无目标" in source
    assert "再平衡" in source
    assert "保存目标" in source
    assert "不下单" in source
    assert ">改账<" not in source
    assert "保存账面" in source
    assert "row.no_target" in source
    assert "data.rebalance" in source
    assert "row.target" in source
    assert "0 到 1" in source
    table = source.split('<table class="book"')[1].split("</table>")[0]
    assert "form.shares" not in table
    assert "contenteditable" not in table.lower()
    assert "<button" not in table.lower()
    rebalance_block = source.split("再平衡")[1].split("</p>")[0]
    assert "<button" not in rebalance_block.lower()
    assert "下单" in source
    assert not any(
        "下单" in line and "<button" in line.lower() for line in source.splitlines()
    )
