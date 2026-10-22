from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

from fastapi.testclient import TestClient

from app.envfile import OPENAI_API_KEY, write_env_value
from app.main import create_app
from app.db import create_db_engine
from market.eastmoney import FakeMarketClient
from market.quotes import FakeQuoteClient
from market.sentiment import (
    HIST_KEY,
    apply_prior_kline,
    drop_intraday_bar,
    parse_eastmoney_breadth,
    parse_kline,
    parse_limit_count,
    score_from_breadth,
    score_parts,
    zone_of,
    _hist_day,
    _upsert_hist,
)
from research.llm import FakeLLM

ROOT = Path(__file__).resolve().parents[1]


def _client(tmp_path: Path, *, market: FakeMarketClient | None = None):
    return TestClient(
        create_app(
            db_path=tmp_path / "app.db",
            env_path=tmp_path / ".env",
            quote_client=FakeQuoteClient(),
            market_client=market if market is not None else FakeMarketClient(),
            llm_client=FakeLLM(),
            reports_dir=tmp_path / "reports",
            allow_sync=True,
        )
    )


def test_score_from_breadth_zones() -> None:
    parts = score_parts(up=2000, down=2000, zt=40, dt=40)
    assert parts["factors"]["breadth"] == 50
    assert "limits" not in parts["factors"]
    assert parts["score"] == 50
    assert score_from_breadth(up=2000, down=2000, pct=Decimal("0")) == 50
    assert zone_of(50) == ("neutral", "中性")
    assert zone_of(26) == ("neutral", "中性偏冷")
    assert zone_of(41) == ("neutral", "中性偏冷")
    assert zone_of(42) == ("neutral", "中性")
    assert zone_of(58) == ("neutral", "中性")
    assert zone_of(59) == ("neutral", "中性偏热")
    assert zone_of(74) == ("neutral", "中性偏热")
    greed = score_from_breadth(up=4500, down=500, pct=Decimal("3"))
    assert greed >= 75
    assert zone_of(greed) == ("greed", "极端贪婪")
    fear = score_from_breadth(up=200, down=4000, pct=Decimal("-3"))
    assert fear <= 25
    assert zone_of(fear) == ("fear", "极端恐惧")


def test_kline_factors_use_history_percentile() -> None:
    closes = [100.0] * 230 + [100.0 + index for index in range(20)]
    amounts = [1.0] * 249 + [100.0]
    parts = score_parts(up=2500, down=2500, zt=40, dt=40, closes=closes, amounts=amounts)
    assert parts["factors"]["volume"] >= 90
    assert parts["factors"]["momentum"] >= 70


def test_missing_kline_reuses_prior_factors() -> None:
    prior = {
        "momentum": 13.5,
        "volatility": 40.4,
        "volume": 25.2,
        "fetched_at": time.time(),
    }
    body = apply_prior_kline(
        {"score": 63, "zone": "neutral", "label": "中性", "factors": {"breadth": 45.4, "limits": 81.3}},
        prior,
    )
    assert body["factors"]["momentum"] == 13.5
    assert body["score"] == 41


def test_stale_or_untimed_kline_is_not_reused() -> None:
    base = {"score": 63, "zone": "neutral", "label": "中性", "factors": {"breadth": 45.4, "limits": 81.3}}
    stale = apply_prior_kline(
        dict(base),
        {"momentum": 13.5, "fetched_at": time.time() - 8 * 24 * 3600},
    )
    assert "momentum" not in stale["factors"]
    untimed = apply_prior_kline(dict(base), {"momentum": 13.5})
    assert "momentum" not in untimed["factors"]


def test_breadth_limits_use_history_percentile_after_60_days() -> None:
    limits_hist = [95.0] * 59 + [80.0]
    parts = score_parts(up=2000, down=2000, zt=8, dt=2, limits_hist=limits_hist)
    assert parts["factors"]["limits"] < 10
    raw = score_parts(up=2000, down=2000, zt=8, dt=2)
    assert "limits" not in raw["factors"]


def test_hist_uses_kline_trading_day_once() -> None:
    assert _hist_day({"dates": ["2026-09-03", "2026-09-04"]}) == "2026-09-04"
    assert _hist_day({}) is None
    hist = _upsert_hist([], "2026-09-04", 50.0, 90.0)
    hist = _upsert_hist(hist, "2026-09-04", 51.0, None)
    assert [row["date"] for row in hist] == ["2026-09-04"]
    assert hist[0]["breadth"] == 51.0
    assert hist[0]["limits"] == 90.0


def test_drop_intraday_bar_before_close() -> None:
    raw = {
        "dates": ["2026-09-07", "2026-09-08"],
        "closes": [10.0, 11.0],
        "amounts": [200.0, 5.0],
    }
    cn = timezone(timedelta(hours=8))
    morning = drop_intraday_bar(
        raw,
        today=date(2026, 9, 8),
        now=datetime(2026, 9, 8, 9, 35, tzinfo=cn),
    )
    assert morning["closes"] == [10.0]
    assert morning["amounts"] == [200.0]
    closed = drop_intraday_bar(
        raw,
        today=date(2026, 9, 8),
        now=datetime(2026, 9, 8, 15, 0, tzinfo=cn),
    )
    assert closed["closes"] == [10.0, 11.0]


def test_parse_eastmoney_breadth() -> None:
    body = parse_eastmoney_breadth(
        {"data": {"diff": [{"f3": 1.2, "f104": 2100, "f105": 1900, "f106": 100}]}}
    )
    assert body["up"] == 2100
    assert body["down"] == 1900
    assert body["flat"] == 100
    assert body["pct"] == Decimal("1.2")


def test_parse_kline_and_limit_count() -> None:
    kline = parse_kline(
        {"data": {"klines": ["2024-01-02,1,10,11,9,100,200", "2024-01-03,10,12,13,10,110,300"]}}
    )
    assert kline["dates"] == ["2024-01-02", "2024-01-03"]
    assert kline["closes"] == [10.0, 12.0]
    assert kline["amounts"] == [200.0, 300.0]
    assert parse_limit_count({"data": {"tc": 88, "pool": [{}]}}) == 88


def test_get_sentiment_default_neutral(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        body = client.get("/api/sentiment").json()
        assert body["score"] == 50
        assert body["zone"] == "neutral"
        assert body["label"] == "中性"
        assert body["method"] == "v2.3"
        assert body["factors"]["breadth"] == 50
        assert "limits" not in body["factors"]


def test_weekend_loads_share_one_hist_row(tmp_path: Path) -> None:
    market = FakeMarketClient(
        breadth={
            "up": 2100,
            "down": 1900,
            "zt": 10,
            "dt": 2,
            "dates": ["2026-09-04"],
        }
    )
    db = tmp_path / "app.db"
    with _client(tmp_path, market=market) as client:
        assert client.get("/api/sentiment").status_code == 200
    engine = create_db_engine(db)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM settings WHERE key = 'sentiment'"))
    with _client(tmp_path, market=market) as client:
        assert client.get("/api/sentiment").status_code == 200
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT value FROM settings WHERE key = :key"),
            {"key": HIST_KEY},
        ).scalar()
    hist = json.loads(str(value))
    assert [row["date"] for row in hist] == ["2026-09-04"]


def test_greed_demotes_eval_pass(tmp_path: Path) -> None:
    write_env_value(tmp_path / ".env", OPENAI_API_KEY, "sk-test-sentiment")
    market = FakeMarketClient(breadth={"up": 4500, "down": 500, "pct": Decimal("3")})
    with _client(tmp_path, market=market) as client:
        created = client.post(
            "/api/research",
            json={"name": "甲", "code": "600036.SH", "sync": True},
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        body = client.get(f"/api/research/{job_id}").json()
        assert body["status"] == "done"
        assert body["structured"]["verdict"] != "pass"
        assert body["structured"]["price_bands"]["mode"] == "watch"


def test_dist_includes_sentiment() -> None:
    text = "".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "web" / "dist" / "assets").glob("*.js")
    )
    assert "情绪指数" in text
    assert "sentiment-meter" in text
    assert "权重 60%" not in text


def test_views_show_sentiment() -> None:
    home = (ROOT / "web/src/views/HomeView.vue").read_text(encoding="utf-8")
    home_model = (
        ROOT / "web/src/views/homeViewModel.ts"
    ).read_text(encoding="utf-8")
    screen = (ROOT / "web/src/views/ScreenView.vue").read_text(encoding="utf-8")
    panel = (ROOT / "web/src/components/SentimentPanel.vue").read_text(encoding="utf-8")
    assert "/api/sentiment" in home_model
    assert "情绪指数" in panel
    assert "sentiment-meter" in panel
    css = (ROOT / "web/src/assets/main.css").read_text(encoding="utf-8")
    assert "linear-gradient" in css
    assert ".sentiment.fear .sentiment-meter" not in css
    assert "上涨家数" not in panel
    assert "权重 60%" not in panel
    assert "SentimentPanel" in home
    assert "SentimentPanel" in screen
