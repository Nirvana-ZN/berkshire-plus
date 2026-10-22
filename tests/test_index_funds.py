import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from market.funds import parse_cn_date, parse_profile, parse_tracking_error, weekly_last
from market.quotes import FakeQuoteClient
from screen.index_funds import (
    benchmark_treasury,
    build_payload,
    judge_index,
    pick_representative,
    proxy_band,
    select_distinct,
    valuation_method,
)

TREASURY = Decimal("1.69")
TODAY = date(2026, 10, 13)

PROFILE_HTML = (
    "<table class=\"info w790\"><tr><th>基金全称</th><td style='width:300px;'>华泰柏瑞沪深300"
    "交易型开放式指数证券投资基金</td><th>基金简称</th><td>沪深300ETF华泰柏瑞</td></tr>"
    "<tr><th>基金代码</th><td>510300（主代码）<th>基金类型</th><td>指数型-股票</td></tr>"
    "<tr><th>发行日期</th><td>2012年04月05日</td><th>成立日期/规模</th>"
    "<td>2012年05月04日 / 329.686亿份</td></tr>"
    "<tr><th>管理费率</th><td>0.15%（每年）</td><th>托管费率</th><td>0.05%（每年）</td></tr>"
    "<tr><th>销售服务费率</th><td>---（每年）</td><th>最高认购费率</th><td>1.00%</td></tr>"
    "<tr><th>业绩比较基准</th><td style=\"width:300px\">沪深300指数</td>"
    "<th>跟踪标的</th><td>沪深300指数</td></tr></table>"
)
TRACKING_HTML = (
    '<tr> <th style="width: 30%">跟踪指数</th> <th style="width: 30%">年化跟踪误差</th> '
    "<th>同类平均跟踪误差</th> </tr> <tr> <td >沪深300指数</td><td >0.36%</td>"
    "<td >2.23%</td> </tr> </table>"
)


def test_parse_fund_pages() -> None:
    profile = parse_profile(PROFILE_HTML)
    assert profile["跟踪标的"] == "沪深300指数"
    assert profile["基金类型"] == "指数型-股票"
    assert profile["管理费率"] == "0.15%（每年）"
    assert str(parse_cn_date(profile["成立日期/规模"])) == "2012-05-04"
    assert parse_tracking_error(TRACKING_HTML) == Decimal("0.36")
    assert parse_tracking_error("<html>throttled</html>") is None
    days = [("20261012", Decimal("10")), ("20261013", Decimal("11")), ("20261014", None), ("20261019", Decimal("12"))]
    assert weekly_last(days) == [Decimal("11"), Decimal("12")]


def _history(low: str, high: str, n: int = 300) -> list[Decimal]:
    low_d, high_d = Decimal(low), Decimal(high)
    return [low_d + (high_d - low_d) * i / (n - 1) for i in range(n)]


def test_judge_index_gates() -> None:
    cheap = judge_index({"pe": 12}, "pe", _history("10", "20"), TREASURY)
    assert cheap["status"] == "pass" and Decimal(15) < cheap["percentile"] < Decimal(25)
    assert judge_index({"pe": 30}, "pe", _history("20", "40"), TREASURY)["status"] == "drop"
    young = judge_index({"pe": 12}, "pe", _history("10", "20", 100), TREASURY)
    assert young["status"] == "pass" and young["percentile"] is None
    assert judge_index({"pe": None}, "pe", _history("10", "20"), TREASURY)["status"] == "insufficient"
    pb_history = _history("0.5", "2")
    no_roe = judge_index({"pe": 40, "pb": 1.85}, "pb", pb_history, TREASURY)
    assert no_roe["status"] == "review" and no_roe["percentile"] is not None
    coal = judge_index({"pb": 1.85}, "pb", pb_history, TREASURY, [Decimal("0.12")] * 300)
    assert coal["status"] == "pass" and abs(coal["gate_yield"] - Decimal("6.486")) < Decimal("0.01")
    assert abs(coal["min_roe"] - Decimal("8.6765")) < Decimal("0.01")
    assert abs(coal["roe_headroom"] - Decimal("27.7")) < Decimal("0.2")
    # 国证地产: boom-era median 10%, last 3 years ~2% -> the lower one decides.
    property_roe = [Decimal("0.10")] * 200 + [Decimal("0.02")] * 156
    assert judge_index({"pb": 0.8}, "pb", pb_history, TREASURY, property_roe)["status"] == "drop"
    assert valuation_method("中证银行指数") == "pb"
    assert valuation_method("中证金融科技主题指数") == "pe"
    assert valuation_method("中证军工指数", pb_flag=True) == "review"
    assert valuation_method("中证光伏产业指数") == "review"
    assert valuation_method("中证动漫游戏指数") == "review"
    assert valuation_method("中证沪深港黄金产业股票指数") == "pb"
    extreme_pct = judge_index({"pe": 10}, "pe", _history("5", "10.2"), TREASURY)
    assert extreme_pct["status"] == "review" and "极高位" in (extreme_pct["reason"] or "")


def _static(code, name, *, kind="场外", fee="0.2", scale="10", established="2015-01-01"):
    return {"kind": kind, "code": code, "fund_code": code, "name": name,
            "fee": Decimal(fee), "scale": Decimal(scale), "established": established}


def test_pick_representative() -> None:
    funds = [
        _static("Y", "某沪深300指数Y", fee="0.1"),
        _static("TINY", "小沪深300指数A", fee="0.1", scale="1"),
        _static("NEW", "新沪深300指数A", fee="0.1", established="2026-06-01"),
        _static("A", "某沪深300指数A", fee="0.2", scale="10"),
        _static("ETF", "沪深300ETF", kind="场内", fee="0.2", scale="900") | {"turnover": Decimal("50000")},
        _static("QUIET", "冷门300ETF", kind="场内", fee="0.15", scale="5") | {"turnover": Decimal("300")},
    ]
    assert pick_representative(funds, TODAY)["code"] == "ETF"  # quiet ETF out; fee tie -> scale
    assert pick_representative(funds[:3], TODAY) is None


def test_select_distinct_drops_near_duplicates() -> None:
    base = {f"d{i:03d}": float((i * 7) % 11 - 5) for i in range(200)}
    twin = {d: v * 1.01 + 0.01 for d, v in base.items()}
    other = {f"d{i:03d}": float((i * 3) % 5 - 2) for i in range(200)}
    series = {"A": base, "B": twin, "C": other, "D": {}}
    candidates = [{"name": key} for key in "ABCD"]
    accepted, skipped = select_distinct(candidates, lambda c: series[c["name"]], limit=10)
    assert [c["name"] for c in accepted] == ["A", "C"]
    assert skipped[0]["skip"].startswith("与A相关") and skipped[1]["skip"] == "收益序列不足"
    assert len(select_distinct(candidates, lambda c: series[c["name"]], limit=1)[0]) == 1
    # A twin with a better valuation source (danjuan > csindex > None) replaces an unvalued twin
    valued_candidates = [
        {"name": "A", "valuation": None},
        {"name": "B", "valuation": {"source": "danjuan", "code": "SH000300"}},
        {"name": "C", "valuation": {"source": "csindex", "code": "000905"}},
    ]
    acc2, skip2 = select_distinct(valued_candidates, lambda c: series[c["name"]], limit=10)
    assert [c["name"] for c in acc2] == ["B", "C"]
    assert skip2[0]["name"] == "A" and skip2[0]["skip"].startswith("与B相关")
    assert proxy_band(2) == Decimal("0.07")
    assert proxy_band(40) == Decimal("0.10")
    assert proxy_band(80) == Decimal("0.15")
    assert benchmark_treasury("恒生科技指数", "海外", TREASURY) == (Decimal("3.10"), "10年期港债 3.10%")
    assert benchmark_treasury("纳斯达克100指数", "海外", TREASURY) == (Decimal("4.10"), "10年期美债 4.10%")
    assert benchmark_treasury("富时沙特阿拉伯指数", "海外", TREASURY) == (None, None)


def _item(name, index_name, fund, *, valuation=None, category="行业"):
    return {"name": name, "index_name": index_name, "category": category,
            "valuation": valuation, "fund": fund}


UNIVERSE = {
    "generated_at": "2026-10-13T00:00:00+00:00",
    "items": [
        _item("沪深300", "沪深300指数", _static("510300.SH", "沪深300ETF", kind="场内"),
              valuation={"source": "danjuan", "code": "SH000300"}, category="宽基"),
        _item("中证银行", "中证银行指数", _static("512800.SH", "银行ETF", kind="场内"),
              valuation={"source": "danjuan", "code": "SZ399986"}),
        _item("中证煤炭", "中证煤炭指数", _static("515220.SH", "煤炭ETF", kind="场内"),
              valuation={"source": "csindex", "code": "399998", "history": _history("8", "20"),
                         "pe_last": 9, "pe_date": "2026-10-13"}),
        _item("半导体", "中证全指半导体产品与设备指数", _static("512480.SH", "半导体ETF", kind="场内"),
              valuation={"source": "csindex", "code": "H30184", "history": _history("40", "120"),
                         "pe_last": 50, "pe_date": "2026-10-09"},
              category="主题"),
        _item("中证2000", "中证2000指数", _static("563300.SH", "2000ETF", kind="场内"),
              valuation={"source": "csindex", "code": "932000", "history": _history("20", "40")}),
        _item("临界", "中证临界指数", _static("515000.SH", "临界ETF", kind="场内"),
              valuation={"source": "csindex", "code": "930001", "history": _history("10", "30"),
                         "pe_last": 20, "pe_date": "2026-10-13"}),
        _item("过期", "中证过期指数", _static("515001.SH", "过期ETF", kind="场内"),
              valuation={"source": "csindex", "code": "930002", "history": _history("8", "20"),
                         "pe_last": 10, "pe_date": "2026-06-01"}),
        _item("中证军工", "中证军工指数", _static("512660.SH", "军工ETF", kind="场内"),
              valuation={"source": "danjuan", "code": "SZ399967"}),
        _item("恒生科技", "恒生科技指数", _static("513260.SH", "恒生科技ETF", kind="场内"),
              valuation={"source": "danjuan", "code": "HKHSTECH"}, category="海外"),
        _item("沙特", "富时沙特阿拉伯指数", _static("159329.SZ", "沙特ETF", kind="场内"),
              valuation={"source": "danjuan", "code": "HKHSTECH"}, category="海外"),
        _item("某海外指数", "某海外指数", _static("OTC1", "某海外指数A", scale="1"), category="海外"),
    ],
}


class FakeFundClient:
    def index_valuations(self):
        return [
            {"index_code": "SH000300", "name": "沪深300", "pe": 12, "pb": 1.4, "roe": 0.11, "yeild": 0.027, "pb_flag": False},
            {"index_code": "SZ399986", "name": "中证银行", "pe": 7, "pb": 0.6, "roe": 0.09, "yeild": 0.04, "pb_flag": True},
            {"index_code": "SZ399967", "name": "中证军工", "pe": 56, "pb": 3.0, "roe": 0.05, "yeild": 0.006, "pb_flag": True},
            {"index_code": "HKHSTECH", "name": "恒生科技", "pe": 22, "pb": 2.2, "roe": 0.10, "yeild": 0.01, "pb_flag": False},
        ]

    def valuation_history(self, code, metric):
        return {
            "SH000300": _history("10", "20"),
            "SZ399986": _history("0.5", "1.5"),
            "SZ399967": _history("2.0", "5.0"),
            "HKHSTECH": _history("15", "45"),
        }[code]

    def roe_history(self, code):
        return [Decimal("0.11")] * 300

    def index_closes(self, code, days=400):
        # +20% after 10-09; flat for anchors dated 10-13 and before 06-02
        return {"2026-06-01": Decimal(100), "2026-10-09": Decimal(100), "2026-10-13": Decimal(120)} | (
            {"2026-10-09": Decimal(120)} if code != "H30184" else {}
        )

    def csindex_pe(self, code, start, end):
        raise AssertionError("refresh must not call csindex")

    def etf_list(self):
        row = {"price": Decimal("4"), "iopv": Decimal("4"), "turnover": Decimal("5e8"), "market_cap": Decimal("9e10")}
        codes = ("510300.SH", "512800.SH", "515220.SH", "563300.SH", "515000.SH", "515001.SH", "512660.SH", "513260.SH", "159329.SZ")
        return [{"code": code, **row} for code in codes] + [
            {"code": "512480.SH", **row, "price": Decimal("4.1")}
        ]

    def tracking_error(self, code):
        return Decimal("0.36")


def test_refresh_ranks_universe(tmp_path: Path, monkeypatch) -> None:
    universe = tmp_path / "universe.json"
    universe.write_text(json.dumps(UNIVERSE, ensure_ascii=False, default=str), encoding="utf-8")
    monkeypatch.setattr("jobs.index_funds.UNIVERSE_PATH", universe)
    fake = FakeFundClient()
    app = create_app(
        db_path=tmp_path / "app.db",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        allow_sync=True,
        fund_client=fake,
    )
    with TestClient(app) as client:
        assert client.get("/api/index-funds/latest").json() == {"run": None, "result": None}
        assert client.post("/api/index-funds/refresh", json={"sync": True}).status_code == 200
        body = client.get("/api/index-funds/latest").json()

    assert body["run"]["status"] == "done", body["run"]
    rows = {r["name"]: r for r in body["result"]["indices"]}
    assert [(r["name"], r["status"]) for r in body["result"]["indices"]] == [
        ("中证银行", "pass"),  # PB branch: 11% ROE / 0.6 PB clears the spread
        ("沪深300", "pass"),
        ("恒生科技", "drop"),  # overseas index evaluated against HK 10Y benchmark (3.10%)
        ("半导体", "drop"),
        ("中证煤炭", "review"),  # cyclical by name, csindex has no PB
        ("沙特", "review"),  # unconfigured overseas market withheld from gate
        ("过期", "review"),
        ("中证军工", "review"),  # REVIEW_METHOD: neither PE nor PB is a clean anchor
        ("临界", "review"),
        ("中证2000", "insufficient"),
        ("某海外指数", "insufficient"),
    ]
    assert rows["中证煤炭"]["percentile"] is not None  # verdict withheld, position kept
    assert "超过 91 天" in rows["过期"]["reason"] and "可能翻转" in rows["临界"]["reason"]
    assert "10年期港债" in rows["恒生科技"]["note"]
    assert "当地国债" in rows["沙特"]["reason"] and "专门估值复核" in rows["中证军工"]["reason"]
    assert rows["半导体"]["note"].startswith("PE 很高") and rows["沪深300"]["note"] is None
    assert rows["某海外指数"]["reason"] == "无估值来源"
    assert abs(rows["半导体"]["current"] - 60) < 1e-9  # anchor 50 x index +20% since 10-09
    assert rows["半导体"]["pe_from"] == "2026-10-09"
    assert "锚点" in rows["中证2000"]["reason"]
    assert body["result"]["coverage"] == {"judged": 4, "total": 11}
    assert rows["沪深300"]["fund"]["status"] == "pass"
    assert rows["半导体"]["fund"]["reason"].startswith("溢价率")
    assert rows["某海外指数"]["fund"]["reason"].startswith("基金规模")

    stale = build_payload(
        {
            "items": [
                {
                    "index": UNIVERSE["items"][0],
                    "metric": "pe",
                    "values": {"pe": 12},
                    "history": _history("10", "20"),
                    "product": _static("510300.SH", "沪深300ETF") | {"tracking_error": Decimal("0.3"), "age_days": 900},
                }
            ],
            "treasury_date": "2026-08-01",
        },
        treasury=TREASURY,
        today=TODAY,
    )
    assert stale["indices"][0]["status"] == "review" and "国债收益率" in stale["indices"][0]["reason"]
