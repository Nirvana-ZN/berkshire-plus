from __future__ import annotations

import pytest

from market.eastmoney import EastMoneyClient, board_code, pick_board, pick_equity
from market.types import MarketError


def _rows(*names: str) -> list[dict[str, str]]:
    return [{"f12": f"BK{index:04d}", "f14": name} for index, name in enumerate(names, start=1)]


def test_pick_board_exact() -> None:
    row = pick_board("银行", _rows("证券Ⅱ", "银行", "保险Ⅱ"))
    assert row["f14"] == "银行"
    assert row["f12"] == "BK0002"


def test_pick_board_strips_eastmoney_suffix() -> None:
    row = pick_board("银行", _rows("银行Ⅱ", "证券Ⅱ"))
    assert row["f14"] == "银行Ⅱ"


def test_pick_board_alias_agriculture() -> None:
    row = pick_board("农业", _rows("种植业", "农林牧渔", "农业综合Ⅱ", "农化制品"))
    assert row["f14"] == "农林牧渔"


def test_pick_board_ambiguous_lists_nearby() -> None:
    with pytest.raises(MarketError) as caught:
        pick_board("农", _rows("种植业", "农林牧渔", "农业综合Ⅱ", "银行Ⅱ"))
    text = str(caught.value)
    assert "解析不到明确列表: 农" in text
    assert "农林牧渔" in text
    assert "农业综合Ⅱ" in text
    assert "银行Ⅱ" not in text


def test_pick_board_unknown_still_fails() -> None:
    with pytest.raises(MarketError, match="解析不到明确列表: 火星矿业"):
        pick_board("火星矿业", _rows("银行Ⅱ", "白酒Ⅱ"))


def test_board_code_agriculture() -> None:
    assert board_code("农业") == "BK0433"
    assert board_code("农林牧渔") == "BK0433"


def test_board_code_machinery() -> None:
    assert board_code("机械重工") == "BK1205"
    assert board_code("机械设备") == "BK1205"
    assert board_code("工程机械") == "BK0739"


def test_pick_board_alias_machinery() -> None:
    row = pick_board(
        "机械重工",
        _rows("银行Ⅱ", "机械设备", "工程机械", "专用设备"),
    )
    assert row["f14"] == "机械设备"


def test_industry_agriculture_skips_board_list() -> None:
    seen: list[str] = []

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            seen.append(url)
            assert "BK0433" in url
            return {
                "data": {
                    "diff": [
                        {
                            "f12": "002714",
                            "f13": "0",
                            "f14": "牧原股份",
                            "f20": "2000",
                            "f9": "15",
                            "f100": "农林牧渔",
                        }
                    ]
                }
            }

    rows = Stub().fetch_constituents("行业", "农业")
    assert len(rows) == 1
    assert rows[0].name == "牧原股份"
    assert all("t:2" not in url for url in seen)


def test_industry_machinery_skips_board_list() -> None:
    seen: list[str] = []

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            seen.append(url)
            assert "BK1205" in url
            return {
                "data": {
                    "diff": [
                        {
                            "f12": "000157",
                            "f13": "0",
                            "f14": "中联重科",
                            "f20": "800",
                            "f9": "12",
                            "f100": "机械设备",
                        }
                    ]
                }
            }

    rows = Stub().fetch_constituents("行业", "机械重工")
    assert len(rows) == 1
    assert rows[0].name == "中联重科"
    assert all("t:2" not in url for url in seen)


def test_list_industry_boards_includes_alias() -> None:
    class Stub(EastMoneyClient):
        def _clist_diff(self, **_kwargs):
            return [{"f12": "BK0433", "f14": "农林牧渔"}, {"f12": "BK1205", "f14": "机械设备"}]

    rows = Stub().list_boards("行业")
    names = [row["name"] for row in rows]
    assert "农林牧渔" in names
    assert "农业" in names
    assert "机械设备" in names
    assert "机械重工" in names
    assert [row["name"] for row in Stub().list_boards("行业", "机械重工")] == ["机械重工"]


def test_list_index_boards_from_catalog() -> None:
    class Stub(EastMoneyClient):
        def _clist_diff(self, **kwargs):
            assert "s:3" in kwargs["fs"]
            return [
                {"f12": "000300", "f13": 1, "f14": "沪深300"},
                {"f12": "000001", "f13": 1, "f14": "上证指数"},
            ]

    rows = Stub().list_boards("指数")
    assert {row["name"] for row in rows} >= {"沪深300", "上证指数"}
    assert Stub().list_boards("指数", "沪深") == [{"name": "沪深300", "code": "1.000300"}]


def test_industry_board_catalog_paginates() -> None:
    seen: list[str] = []

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            seen.append(url)
            if "t:2" in url:
                if "pn=1" in url:
                    return {
                        "data": {
                            "diff": [{"f12": "BK0001", "f14": "银行Ⅱ"}]
                            + [{"f12": f"BK{i:04d}", "f14": f"占位{i}"} for i in range(2, 101)]
                        }
                    }
                if "pn=2" in url:
                    return {
                        "data": {
                            "diff": [{"f12": "BK1406", "f14": "印刷包装机械"}]
                        }
                    }
                raise AssertionError(url)
            assert "BK1406" in url
            return {
                "data": {
                    "diff": [
                        {
                            "f12": "000821",
                            "f13": "0",
                            "f14": "京山轻机",
                            "f20": "50",
                            "f9": "20",
                            "f100": "印刷包装机械",
                        }
                    ]
                }
            }

    rows = Stub().fetch_constituents("行业", "印刷包装机械")
    assert rows[0].name == "京山轻机"
    assert any("pn=2" in url and "t:2" in url for url in seen)


def test_pick_equity_by_name_or_code() -> None:
    items = [
        {"Name": "上证指数", "Code": "000001", "QuoteID": "1.000001"},
        {"Name": "招商银行", "Code": "600036", "QuoteID": "1.600036"},
        {"Name": "平安银行", "Code": "000001", "QuoteID": "0.000001"},
    ]
    assert pick_equity("招商银行", items) == ("招商银行", "600036.SH")
    assert pick_equity("600036.SH", items) == ("招商银行", "600036.SH")
    assert pick_equity("600036", items) == ("招商银行", "600036.SH")


def test_lookup_equity_uses_codetable() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            assert "search-codetable.eastmoney.com" in url
            assert "赣锋锂业" in url or "%E8%B5%A3" in url
            return {
                "code": "0",
                "result": [
                    {"shortName": "赣锋锂业", "code": "002460", "market": 0},
                    {"shortName": "赣锋锂业", "code": "01772", "market": 116},
                ],
            }

    row = Stub().lookup_equity("赣锋锂业")
    assert row.name == "赣锋锂业"
    assert row.code == "002460.SZ"


def test_pick_equity_unknown_lists_nearby() -> None:
    items = [
        {"Name": "招商银行", "Code": "600036", "QuoteID": "1.600036"},
        {"Name": "兴业银行", "Code": "601166", "QuoteID": "1.601166"},
    ]
    with pytest.raises(MarketError) as caught:
        pick_equity("火星矿业", items)
    text = str(caught.value)
    assert "解析不到明确个股: 火星矿业" in text
    assert "招商银行" in text


def test_financials_map_bps_npl_and_shares() -> None:
    from decimal import Decimal

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            return {
                "success": True,
                "result": {
                    "data": [
                        {
                            "REPORT_TYPE": "年报",
                            "REPORT_YEAR": "2025",
                            "ROEJQ": "13.44",
                            "PARENTNETPROFIT": "150181000000",
                            "BPS": "43.43",
                            "NONPERLOAN": "0.94",
                            "EPSJB": "5.94",
                            "TOTAL_SHARE": "25219845601",
                        }
                    ]
                },
            }

    rows = Stub().fetch_financials(["600036.SH"])
    assert len(rows) == 1
    assert rows[0].period == "2025"
    assert rows[0].period_type == "annual"
    assert rows[0].bps == Decimal("43.43")
    assert rows[0].npl == Decimal("0.94")
    assert rows[0].eps == Decimal("5.94")
    assert rows[0].shares == Decimal("25219845601")


def test_financials_map_cash_flow_and_interest() -> None:
    from decimal import Decimal

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            return {
                "success": True,
                "result": {
                    "data": [
                        {
                            "REPORT_TYPE": "年报",
                            "REPORT_YEAR": "2025",
                            "PARENTNETPROFIT": "43945411000",
                            "NETCASH_OPERATE_PK": "53345930000",
                            "TOTAL_SHARE": "7629346190",
                            "PER_EBIT": "6.167102641848",
                            "INTSTCOVRATE": "25.006059246671",
                        }
                    ]
                },
            }

    row = Stub().fetch_financials(["000333.SZ"])[0]
    assert row.ocf == Decimal("53345930000")
    assert row.ebit is not None and Decimal("46e9") < row.ebit < Decimal("48e9")
    assert row.interest is not None and Decimal("1.8e9") < row.interest < Decimal("2e9")
    assert row.ocf / row.net_income > Decimal("1")


def test_financials_keep_interim_beside_annual() -> None:
    from decimal import Decimal

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            return {
                "success": True,
                "result": {
                    "data": [
                        {
                            "REPORT_TYPE": "中报",
                            "REPORT_YEAR": "2026",
                            "PARENTNETPROFIT": "796000000",
                            "EPSJB": "1.64",
                            "ROEJQ": "47.54",
                        },
                        {
                            "REPORT_TYPE": "年报",
                            "REPORT_YEAR": "2025",
                            "PARENTNETPROFIT": "1231000000",
                            "EPSJB": "2.54",
                            "ROEJQ": "81.68",
                        },
                    ]
                },
            }

    rows = Stub().fetch_financials(["600132.SH"])
    kinds = {(row.period, row.period_type, row.eps) for row in rows}
    assert ("2026H1", "interim", Decimal("1.64")) in kinds
    assert ("2025", "annual", Decimal("2.54")) in kinds


def test_fetch_valuation_maps_bank_fields() -> None:
    from decimal import Decimal

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            assert "MAINFINADATA" in url
            assert "pageSize=8" in url
            return {
                "success": True,
                "result": {
                    "data": [
                        {
                            "REPORT_TYPE": "一季报",
                            "BPS": "45.4",
                            "NONPERLOAN": "0.94",
                            "NON_PERFORMING_LOAN": "70251000000",
                            "EPSJB": "2.98",
                            "REPORT_YEAR": "2026",
                        },
                        {
                            "REPORT_TYPE": "年报",
                            "BPS": "43.43",
                            "NONPERLOAN": "0.94",
                            "EPSJB": "5.94",
                            "REPORT_YEAR": "2025",
                        },
                    ]
                },
            }

    got = Stub().fetch_valuation("600036.SH")
    assert got["bps"] == Decimal("45.4")
    assert got["npl_pct"] == Decimal("0.94")
    assert got["eps"] == Decimal("5.94")
    assert got["eps_period"] == "2025"
    assert got["period"] == "2026"


def test_fetch_valuation_failure_is_empty() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            raise RuntimeError("timeout")

    assert Stub().fetch_valuation("600036.SH") == {}
