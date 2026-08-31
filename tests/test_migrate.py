from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from market.quotes import FakeQuoteClient

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "portfolio"
APP_DIR = ROOT / "app"

CASH_BALANCE = Decimal("123456.78")
TRADE_COUNT = 5
# 回放：-800*35.25 +200*40 -50*1400 -100*200 +100*250 = -85200
REPLAY_CASH = Decimal("-85200")


def _engine(db_path: Path):
    return create_engine(f"sqlite:///{db_path.resolve().as_posix()}")


def _scalar(db_path: Path, sql: str):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def _rows(db_path: Path, sql: str):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return list(conn.execute(text(sql)).mappings())


def _client(tmp_path: Path, *, yaml_dir: Path | None = None, db_name: str = "app.db"):
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


def test_first_migrate_writes_trades_cash_rules_instruments(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path, yaml_dir=FIXTURES) as client:
        health = client.get("/api/health")
        assert 200 <= health.status_code < 300
        settings = client.get("/api/settings").json()
        positions = client.get("/api/positions").json()

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == TRADE_COUNT
    directions = {
        row["direction"] for row in _rows(db_path, "SELECT direction FROM trades")
    }
    assert directions == {"buy", "sell"}
    cash = _as_decimal(_scalar(db_path, "SELECT balance FROM cash WHERE currency = 'CNY'"))
    assert cash == CASH_BALANCE
    assert cash != REPLAY_CASH
    assert _scalar(db_path, "SELECT COUNT(*) FROM rules") == 2
    assert _scalar(db_path, "SELECT COUNT(*) FROM instruments") == 3
    assert _scalar(db_path, "SELECT name FROM instruments WHERE code = '600036.SH'") == "招商银行"

    assert settings["migrated"] is True
    assert Path(settings["yaml_dir"]) == FIXTURES

    by_name = {row["name"]: row for row in positions["positions"]}
    assert set(by_name) == {"招商银行", "贵州茅台"}
    assert "宁德时代" not in by_name
    assert _as_decimal(by_name["招商银行"]["shares"]) == Decimal("600")
    assert _as_decimal(by_name["招商银行"]["cost"]) == Decimal("21150")
    assert _as_decimal(by_name["贵州茅台"]["shares"]) == Decimal("50")
    assert _as_decimal(by_name["贵州茅台"]["cost"]) == Decimal("70000")
    assert _as_decimal(positions["cash"]["balance"]) == CASH_BALANCE
    assert positions["cash"]["currency"] == "CNY"


def test_env_portfolio_yaml_dir_migrates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PORTFOLIO_YAML_DIR", str(FIXTURES))
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        settings = client.get("/api/settings").json()
        positions = client.get("/api/positions").json()

    assert settings["migrated"] is True
    assert Path(settings["yaml_dir"]) == FIXTURES
    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == TRADE_COUNT
    assert _as_decimal(positions["cash"]["balance"]) == CASH_BALANCE
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) != REPLAY_CASH


def test_migrate_only_once_on_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with _client(tmp_path, yaml_dir=FIXTURES) as client:
        assert client.get("/api/settings").json()["migrated"] is True
    first_trades = _scalar(db_path, "SELECT COUNT(*) FROM trades")
    first_cash = _as_decimal(_scalar(db_path, "SELECT balance FROM cash"))

    with _client(tmp_path, yaml_dir=FIXTURES) as client:
        assert client.get("/api/settings").json()["migrated"] is True
        positions = client.get("/api/positions").json()

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == first_trades == TRADE_COUNT
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == first_cash == CASH_BALANCE
    assert _as_decimal(positions["cash"]["balance"]) == CASH_BALANCE


def test_existing_trades_skip_migrate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PORTFOLIO_YAML_DIR", raising=False)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200

    engine = _engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO trades (user_id, date, direction, name, shares, price) "
                "VALUES (1, '2020-01-01', 'buy', '已有持仓', 10, 1.5)"
            )
        )

    with _client(tmp_path, yaml_dir=FIXTURES) as client:
        settings = client.get("/api/settings").json()
        positions = client.get("/api/positions").json()

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 1
    names = [row["name"] for row in _rows(db_path, "SELECT name FROM trades")]
    assert names == ["已有持仓"]
    assert _scalar(db_path, "SELECT COUNT(*) FROM instruments") == 0
    assert settings["migrated"] is False
    by_name = {row["name"]: row for row in positions["positions"]}
    assert set(by_name) == {"已有持仓"}
    assert _as_decimal(by_name["已有持仓"]["shares"]) == Decimal("10")
    assert _as_decimal(by_name["已有持仓"]["cost"]) == Decimal("15")


def test_unconfigured_dir_does_not_migrate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("PORTFOLIO_YAML_DIR", raising=False)
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        health = client.get("/api/health")
        settings = client.get("/api/settings")
        positions = client.get("/api/positions")

    assert 200 <= health.status_code < 300
    assert settings.status_code == 200
    body = settings.json()
    assert body["migrated"] is False
    assert body.get("yaml_dir") in (None, "")
    assert "error" not in body or not body.get("error")
    assert positions.status_code == 200
    pos = positions.json()
    assert pos["positions"] == []
    assert _as_decimal(pos["cash"]["balance"]) == Decimal("0")
    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 0


def test_bad_yaml_does_not_mark_migrated_or_leave_trades(tmp_path: Path) -> None:
    yaml_dir = tmp_path / "bad-yaml"
    yaml_dir.mkdir()
    (yaml_dir / "trades.yaml").write_text("成交: [", encoding="utf-8")
    (yaml_dir / "cash.yaml").write_text("余额: 1\n", encoding="utf-8")
    (yaml_dir / "rules.yaml").write_text("规则: []\n", encoding="utf-8")
    (yaml_dir / "symbols.yaml").write_text("映射: {}\n", encoding="utf-8")
    db_path = tmp_path / "app.db"

    with _client(tmp_path, yaml_dir=yaml_dir) as client:
        health = client.get("/api/health")
        settings = client.get("/api/settings")
        positions = client.get("/api/positions")

    assert 200 <= health.status_code < 300
    assert settings.status_code == 200
    body = settings.json()
    assert body["migrated"] is False
    assert {"error", "code", "detail"} <= set(body)
    assert body["error"]
    assert body["code"]
    assert body["detail"]
    assert positions.status_code == 200
    pos_body = positions.json()
    assert {"error", "code", "detail"} <= set(pos_body)
    assert pos_body["error"]
    assert pos_body["code"]
    assert pos_body["detail"]
    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 0


def test_partial_write_failure_rolls_back_trades(tmp_path: Path) -> None:
    yaml_dir = tmp_path / "partial"
    yaml_dir.mkdir()
    (yaml_dir / "trades.yaml").write_text(
        (
            "成交:\n"
            "  - 日期: '2026-01-10'\n"
            "    方向: 买\n"
            "    名称: 招商银行\n"
            "    股数: 800\n"
            "    价格: 35.25\n"
        ),
        encoding="utf-8",
    )
    (yaml_dir / "cash.yaml").write_text("现金: 1\n", encoding="utf-8")
    (yaml_dir / "rules.yaml").write_text("规则: []\n", encoding="utf-8")
    (yaml_dir / "symbols.yaml").write_text("映射: {}\n", encoding="utf-8")
    db_path = tmp_path / "app.db"

    with _client(tmp_path, yaml_dir=yaml_dir) as client:
        body = client.get("/api/settings").json()

    assert body["migrated"] is False
    assert {"error", "code", "detail"} <= set(body)
    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 0


def test_restart_without_yaml_dir_returns_persisted_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("PORTFOLIO_YAML_DIR", raising=False)
    with _client(tmp_path, yaml_dir=FIXTURES) as client:
        first = client.get("/api/settings").json()
        assert first["migrated"] is True
        assert Path(first["yaml_dir"]) == FIXTURES

    with _client(tmp_path) as client:
        settings = client.get("/api/settings").json()
        positions = client.get("/api/positions").json()

    assert settings["migrated"] is True
    assert Path(settings["yaml_dir"]) == FIXTURES
    assert _as_decimal(positions["cash"]["balance"]) == CASH_BALANCE
    assert "error" not in settings or not settings.get("error")


def test_is_migrated_true_with_zero_trades_does_not_remigrate(tmp_path: Path) -> None:
    yaml_dir = tmp_path / "empty-trades"
    yaml_dir.mkdir()
    (yaml_dir / "trades.yaml").write_text("成交: []\n", encoding="utf-8")
    (yaml_dir / "cash.yaml").write_text("余额: 99\n", encoding="utf-8")
    (yaml_dir / "rules.yaml").write_text("规则: []\n", encoding="utf-8")
    (yaml_dir / "symbols.yaml").write_text("映射: {}\n", encoding="utf-8")
    db_path = tmp_path / "app.db"

    with _client(tmp_path, yaml_dir=yaml_dir) as client:
        first = client.get("/api/settings").json()
        assert first["migrated"] is True
        assert client.get("/api/health").status_code == 200
    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 0
    first_cash = _as_decimal(_scalar(db_path, "SELECT balance FROM cash"))
    assert first_cash == Decimal("99")

    with _client(tmp_path, yaml_dir=yaml_dir) as client:
        settings = client.get("/api/settings").json()
        assert settings["migrated"] is True
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/positions").status_code == 200

    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM cash") == 1
    assert _as_decimal(_scalar(db_path, "SELECT balance FROM cash")) == first_cash


def test_oversell_in_yaml_rejects_migrate(tmp_path: Path) -> None:
    yaml_dir = tmp_path / "oversell"
    yaml_dir.mkdir()
    (yaml_dir / "trades.yaml").write_text(
        (
            "成交:\n"
            "  - 日期: '2026-01-10'\n"
            "    方向: 买\n"
            "    名称: 招商银行\n"
            "    股数: 100\n"
            "    价格: 10\n"
            "  - 日期: '2026-01-11'\n"
            "    方向: 卖\n"
            "    名称: 招商银行\n"
            "    股数: 200\n"
            "    价格: 12\n"
        ),
        encoding="utf-8",
    )
    (yaml_dir / "cash.yaml").write_text("余额: 1\n", encoding="utf-8")
    (yaml_dir / "rules.yaml").write_text("规则: []\n", encoding="utf-8")
    (yaml_dir / "symbols.yaml").write_text("映射:\n  招商银行: '600036.SH'\n", encoding="utf-8")
    db_path = tmp_path / "app.db"

    with _client(tmp_path, yaml_dir=yaml_dir) as client:
        settings = client.get("/api/settings").json()
        positions = client.get("/api/positions").json()

    assert settings["migrated"] is False
    assert "卖出超过持仓" in settings["detail"]
    assert "卖出超过持仓" in positions["detail"]
    assert _scalar(db_path, "SELECT COUNT(*) FROM trades") == 0


def test_positions_view_renders_table_from_api() -> None:
    source = (ROOT / "web/src/views/PositionsView.vue").read_text(encoding="utf-8")
    assert "/api/positions" in source
    assert "<table" in source.lower()
    assert "名称" in source
    assert "股数" in source
    assert "成本单价" in source
    assert "成本总额" in source
    assert "现金" in source
    assert "data.positions" in source
    assert "row.shares" in source
    assert "data.cash" in source
    assert "data.error" in source
    assert "暂无持仓" in source


def test_settings_view_shows_path_and_migrated_state() -> None:
    source = (ROOT / "web/src/views/SettingsView.vue").read_text(encoding="utf-8")
    assert "/api/settings" in source
    assert "已迁入" in source
    assert "未迁入" in source
    assert "data.yaml_dir" in source or "迁入路径" in source
    assert "data.migrated" in source
