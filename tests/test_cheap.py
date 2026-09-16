from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from market.cheap import SHOW_LIMIT, CheapRow, eligible_cheap
from market.eastmoney import EastMoneyClient, FakeMarketClient
from market.quotes import FakeQuoteClient
from market.types import Constituent, FinancialRow
from research.llm import FakeLLM
from screen.funnel import screen_keep

ROOT = Path(__file__).resolve().parents[1]


def _row(
    name: str,
    *,
    pe: Decimal | None = None,
    pb: Decimal | None = None,
    code: str = "000001.SZ",
    industry: str = "银行",
) -> CheapRow:
    return CheapRow(
        code=code,
        name=name,
        price=Decimal("10"),
        pe=pe,
        pb=pb,
        mcap=Decimal("1"),
        industry=industry,
    )


def _annual(code: str, *, roe: str = "15", ocf: str = "100") -> list[FinancialRow]:
    rows: list[FinancialRow] = []
    for year in range(2016, 2026):
        rows.append(
            FinancialRow(
                code=code,
                period=str(year),
                period_type="annual",
                roe=Decimal(roe),
                net_income=Decimal("100"),
                ocf=Decimal(ocf),
                capex=Decimal("20"),
                ebit=Decimal("40"),
                interest=Decimal("5"),
                gross_margin=Decimal("40"),
                net_margin=Decimal("10"),
                shares=Decimal("100"),
            )
        )
    return rows


def _snap(code: str, name: str, mcap: str = "100") -> Constituent:
    return Constituent(
        code=code,
        name=name,
        mcap=Decimal(mcap),
        pe=Decimal("8"),
        industry="银行",
    )


def _client(tmp_path: Path, *, market: FakeMarketClient | None = None) -> TestClient:
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


def test_eligible_cheap_drops_st_and_nonpositive_pe() -> None:
    rows = [
        _row("招商银行", pe=Decimal("6"), code="600036.SH"),
        _row("*ST假药", pe=Decimal("1"), code="000002.SZ"),
        _row("ST化工", pe=Decimal("2"), code="000003.SZ"),
        _row("亏损股", pe=Decimal("-3"), code="000004.SZ"),
        _row("无PE", pe=None, code="000005.SZ"),
        _row("零PE", pe=Decimal("0"), code="000006.SZ"),
        _row("平安银行", pe=Decimal("5"), code="000001.SZ"),
    ]
    out = eligible_cheap(rows, sort="pe")
    assert [row.name for row in out] == ["平安银行", "招商银行"]


def test_eligible_cheap_pb_sort_keeps_negative_pe() -> None:
    rows = [
        _row("贵价行", pe=Decimal("80"), pb=Decimal("1.2"), code="600000.SH"),
        _row("便宜行", pe=Decimal("-2"), pb=Decimal("0.4"), code="601166.SH"),
        _row("无PB", pe=Decimal("4"), pb=None, code="601398.SH"),
    ]
    out = eligible_cheap(rows, sort="pb")
    assert [row.name for row in out] == ["便宜行", "贵价行"]


def test_fetch_cheap_universe_uses_hsa_fs() -> None:
    seen: list[str] = []

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            seen.append(url)
            return {
                "data": {
                    "diff": [
                        {
                            "f12": "600036",
                            "f13": "1",
                            "f14": "招商银行",
                            "f2": "42.1",
                            "f9": "6.5",
                            "f23": "0.8",
                            "f20": "10000000000",
                            "f100": "银行",
                        }
                    ]
                }
            }

    rows = Stub().fetch_cheap_universe()
    assert len(rows) == 1
    assert rows[0].name == "招商银行"
    assert rows[0].code == "600036.SH"
    assert rows[0].pe == Decimal("6.5")
    assert rows[0].pb == Decimal("0.8")
    assert any("m:0+t:6" in url and "f23" in url for url in seen)
    assert all("b:BK" not in url for url in seen)


def test_api_cheap_returns_ranked_rows(tmp_path: Path) -> None:
    market = FakeMarketClient(
        cheap_universe=[
            _row("招商银行", pe=Decimal("6"), pb=Decimal("0.9"), code="600036.SH"),
            _row("*ST假药", pe=Decimal("1"), pb=Decimal("0.1"), code="000002.SZ"),
            _row("平安银行", pe=Decimal("5"), pb=Decimal("0.7"), code="000001.SZ"),
        ],
        financials=_annual("600036.SH") + _annual("000001.SZ"),
    )
    with _client(tmp_path, market=market) as client:
        body = client.get("/api/cheap?sync=true").json()
        assert body["sort"] == "pe"
        assert [row["name"] for row in body["rows"]] == ["平安银行", "招商银行"]
        assert body["rows"][0]["code"] == "000001.SZ"
        assert body["universe"] == 2
        assert body["scope_label"]
        pe = client.get("/api/cheap?sort=pb&sync=true").json()
        assert pe["sort"] == "pb"
        assert [row["name"] for row in pe["rows"]] == ["平安银行", "招商银行"]
        assert market.calls.count(("cheap",)) == 1


def test_screen_keep_does_not_cap_at_ten() -> None:
    snaps = [_snap(f"{index:06d}.SZ", f"优{index}", "10") for index in range(1, 13)]
    rows: list[FinancialRow] = []
    for snap in snaps:
        rows.extend(_annual(snap.code))
    keep = screen_keep(snaps, rows)
    assert len(keep) == 12


def test_api_cheap_drops_low_roe(tmp_path: Path) -> None:
    market = FakeMarketClient(
        cheap_universe=[
            _row("低ROE便宜", pe=Decimal("2"), pb=Decimal("0.3"), code="600000.SH"),
            _row("平安银行", pe=Decimal("5"), pb=Decimal("0.7"), code="000001.SZ"),
        ],
        financials=_annual("600000.SH", roe="3") + _annual("000001.SZ", roe="15"),
    )
    with _client(tmp_path, market=market) as client:
        body = client.get("/api/cheap?sync=true").json()
        names = [row["name"] for row in body["rows"]]
        assert names == ["平安银行"]
        assert body["pool"] == 2
        assert body["passed"] == 1


def test_api_cheap_keeps_quality_outside_cheapest_80(tmp_path: Path) -> None:
    junk = [
        _row(f"垃圾{index}", pe=Decimal(index), pb=Decimal("0.5"), code=f"30{index:04d}.SZ")
        for index in range(1, 81)
    ]
    quality = _row("贵价好银行", pe=Decimal("90"), pb=Decimal("1.2"), code="600036.SH")
    financials: list[FinancialRow] = []
    for row in junk:
        financials.extend(_annual(row.code, roe="3"))
    financials.extend(_annual(quality.code, roe="15"))
    market = FakeMarketClient(
        cheap_universe=junk + [quality],
        financials=financials,
    )
    with _client(tmp_path, market=market) as client:
        body = client.get("/api/cheap?sync=true").json()
        names = [row["name"] for row in body["rows"]]
        assert names == ["贵价好银行"]
        assert body["universe"] == 81
        assert body["pending"] == 0
        assert body["passed"] == 1


def test_api_cheap_applies_board_quality_bar(tmp_path: Path) -> None:
    market = FakeMarketClient(
        cheap_universe=[
            _row("平庸ROE", pe=Decimal("4"), pb=Decimal("0.5"), code="600000.SH"),
            _row("现金流一般", pe=Decimal("5"), pb=Decimal("0.6"), code="600001.SH"),
            _row("好公司", pe=Decimal("9"), pb=Decimal("1.1"), code="600036.SH"),
        ],
        # 两家都过得了去劣（ROE>=8、净现比>=0.7），但够不上榜上的 12% / 1.0
        financials=(
            _annual("600000.SH", roe="9")
            + _annual("600001.SH", roe="15", ocf="80")
            + _annual("600036.SH", roe="15")
        ),
    )
    with _client(tmp_path, market=market) as client:
        body = client.get("/api/cheap?sync=true").json()
        assert [row["name"] for row in body["rows"]] == ["好公司"]
        assert body["screened"] == 3
        assert body["passed"] == 1


def test_api_cheap_caps_industry_and_ranks_by_value_score(tmp_path: Path) -> None:
    market = FakeMarketClient(
        cheap_universe=[
            _row("银行甲", pe=Decimal("4"), code="600000.SH"),
            _row("银行乙", pe=Decimal("5"), code="600001.SH"),
            _row("银行丙", pe=Decimal("6"), code="600002.SH"),
            _row("白酒丁", pe=Decimal("20"), code="600003.SH", industry="白酒"),
        ],
        financials=(
            _annual("600000.SH", roe="12")
            + _annual("600001.SH", roe="20")
            + _annual("600002.SH", roe="30")
            + _annual("600003.SH", roe="40")
        ),
    )
    with _client(tmp_path, market=market) as client:
        body = client.get("/api/cheap?sync=true").json()
        # 按 ROE10/PE 排：丙 5.0 > 乙 4.0 > 甲 3.0 > 丁 2.0，
        # 但银行只让留 2 家，所以最便宜的甲被挤掉，丁顶上来
        assert [row["name"] for row in body["rows"]] == ["银行丙", "银行乙", "白酒丁"]
        assert body["rows"][0]["score"] == 5
        assert body["rows"][0]["roe10"] == 30
        assert body["passed"] == 4


def test_api_cheap_exposes_each_layer(tmp_path: Path) -> None:
    market = FakeMarketClient(
        cheap_universe=[
            _row("银行甲", pe=Decimal("4"), code="600000.SH"),
            _row("银行乙", pe=Decimal("5"), code="600001.SH"),
            _row("银行丙", pe=Decimal("6"), code="600002.SH"),
            _row("白酒丁", pe=Decimal("20"), code="600003.SH", industry="白酒"),
            _row("平庸戊", pe=Decimal("3"), code="600004.SH", industry="钢铁"),
        ],
        financials=(
            _annual("600000.SH", roe="12")
            + _annual("600001.SH", roe="20")
            + _annual("600002.SH", roe="30")
            + _annual("600003.SH", roe="40")
            + _annual("600004.SH", roe="9")
        ),
    )
    with _client(tmp_path, market=market) as client:
        body = client.get("/api/cheap?sync=true").json()
        layers = {layer["key"]: layer for layer in body["layers"]}
        assert [layer["key"] for layer in body["layers"]] == [
            "screened",
            "passed",
            "shortlist",
        ]
        # 平庸戊 ROE 9% 过得了去劣，但过不了榜上的 12%
        assert layers["screened"]["count"] == 5
        assert "平庸戊" in [row["name"] for row in layers["screened"]["rows"]]
        assert layers["passed"]["count"] == 4
        assert "平庸戊" not in [row["name"] for row in layers["passed"]["rows"]]
        # 银行甲被行业上限挤掉，但在上一层还看得见
        assert "银行甲" in [row["name"] for row in layers["passed"]["rows"]]
        assert layers["shortlist"]["count"] == 3
        assert "银行甲" not in [row["name"] for row in layers["shortlist"]["rows"]]
        assert layers["shortlist"]["rows"] == body["rows"]
        assert layers["screened"]["rows"][0]["ocf_ni"] == 1


def test_api_cheap_shortlist_caps_at_show_limit(tmp_path: Path) -> None:
    universe = [
        _row(
            f"票{index}",
            pe=Decimal("10"),
            code=f"{index:06d}.SZ",
            industry=f"行业{index}",
        )
        for index in range(1, 41)
    ]
    financials: list[FinancialRow] = []
    for index, row in enumerate(universe, start=1):
        financials.extend(_annual(row.code, roe=str(12 + index)))
    market = FakeMarketClient(cheap_universe=universe, financials=financials)
    with _client(tmp_path, market=market) as client:
        body = client.get("/api/cheap?sync=true").json()
        assert body["passed"] == 40
        assert body["shown"] == SHOW_LIMIT
        assert body["rows"][0]["name"] == "票40"


def test_api_cheap_reuses_persisted_board(tmp_path: Path) -> None:
    from jobs.cheap import _BOARD_CACHE

    market = FakeMarketClient(
        cheap_universe=[
            _row("平安银行", pe=Decimal("5"), pb=Decimal("0.7"), code="000001.SZ"),
            _row("招商银行", pe=Decimal("6"), pb=Decimal("0.9"), code="600036.SH"),
        ],
        financials=_annual("000001.SZ") + _annual("600036.SH"),
    )
    with _client(tmp_path, market=market) as client:
        first = client.get("/api/cheap?sync=true").json()
        assert [row["name"] for row in first["rows"]] == ["平安银行", "招商银行"]
        _BOARD_CACHE.clear()
        market.calls.clear()
        second = client.get("/api/cheap").json()
        assert [row["name"] for row in second["rows"]] == ["平安银行", "招商银行"]
        assert second["layers"][0]["count"] == first["layers"][0]["count"]
        assert not any(call and call[0] == "financials" for call in market.calls)


def test_api_cheap_rejects_unknown_sort(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        body = client.get("/api/cheap?sort=ps")
        assert body.status_code == 400


def test_views_show_cheap_board() -> None:
    home = (ROOT / "web/src/views/HomeView.vue").read_text(encoding="utf-8")
    app = (ROOT / "web/src/App.vue").read_text(encoding="utf-8")
    router = (ROOT / "web/src/router/index.ts").read_text(encoding="utf-8")
    page = (ROOT / "web/src/views/CheapView.vue").read_text(encoding="utf-8")
    assert 'to="/cheap"' in app
    assert 'to="/cheap"' in home
    assert "CheapView" in router
    assert "/api/cheap" in page
    assert "低估值" in page
    assert "去劣" in page
    assert "最便宜 80" not in page
    assert "/api/research" in page


def test_dist_includes_cheap() -> None:
    text = "".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "web" / "dist" / "assets").glob("*.js")
    )
    assert "低估值榜" in text
    assert "/api/cheap" in text
