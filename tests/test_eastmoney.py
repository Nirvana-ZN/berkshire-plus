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


def _statement_payload(*rows: dict[str, object]) -> dict:
    return {"success": True, "result": {"data": list(rows)}}


def test_discovery_income_source_mapping() -> None:
    from decimal import Decimal

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            if "RPT_DMSK_FN_INCOME" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31 00:00:00",
                        "DATE_TYPE_CODE": "001",
                        "NOTICE_DATE": "2026-03-31 00:00:00",
                        "TOTAL_OPERATE_INCOME": "1000000000",
                        "PARENT_NETPROFIT": "100000000",
                        "DEDUCT_PARENT_NETPROFIT": "90000000",
                        "TOTAL_PROFIT": "120000000",
                        "INCOME_TAX": "20000000",
                    }
                )
            if "RPT_F10_FINANCE_GINCOME" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31 00:00:00",
                        "REPORT_TYPE": "年报",
                        "ASSET_IMPAIRMENT_INCOME": "-7000000",
                        "CREDIT_IMPAIRMENT_INCOME": "-3000000",
                        "ASSET_DISPOSAL_INCOME": "2000000",
                    }
                )
            return _statement_payload()

    row = Stub().fetch_financials(["000333.SZ"])[0]
    assert row.revenue == Decimal("1000000000")
    assert row.net_income == Decimal("100000000")
    assert row.deducted_net_income == Decimal("90000000")
    assert row.pretax_income == Decimal("120000000")
    assert row.income_tax == Decimal("20000000")
    assert row.impairment_loss == Decimal("10000000")
    assert row.asset_disposal_gain == Decimal("2000000")
    assert row.government_grants is None
    assert row.source_as_of == "2026-03-31"


def test_discovery_balance_source_mapping_and_visible_debt_sum() -> None:
    from decimal import Decimal

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            if "RPT_DMSK_FN_BALANCE" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31 00:00:00",
                        "DATE_TYPE_CODE": "001",
                        "TOTAL_ASSETS": "800000000",
                        "TOTAL_EQUITY": "600000000",
                        "MONETARYFUNDS": "200000000",
                    }
                )
            if "RPT_F10_FINANCE_GBALANCE" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31 00:00:00",
                        "REPORT_TYPE": "年报",
                        "SHORT_LOAN": "50000000",
                        "LONG_LOAN": "60000000",
                        "BOND_PAYABLE": "30000000",
                        "NONCURRENT_LIAB_1YEAR": "10000000",
                        "GOODWILL": "40000000",
                    }
                )
            return _statement_payload()

    row = Stub().fetch_financials(["000333.SZ"])[0]
    assert row.total_assets == Decimal("800000000")
    assert row.net_assets == Decimal("600000000")
    assert row.cash == Decimal("200000000")
    assert row.short_loan == Decimal("50000000")
    assert row.long_loan == Decimal("60000000")
    assert row.bond_payable == Decimal("30000000")
    assert row.noncurrent_liab_1year == Decimal("10000000")
    assert row.interest_debt == Decimal("150000000")
    assert row.goodwill == Decimal("40000000")


def test_discovery_cashflow_source_mapping() -> None:
    from decimal import Decimal

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            if "RPT_DMSK_FN_CASHFLOW" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31 00:00:00",
                        "DATE_TYPE_CODE": "001",
                        "NETCASH_OPERATE": "130000000",
                        "CONSTRUCT_LONG_ASSET": "80000000",
                    }
                )
            if "RPT_F10_FINANCE_GCASHFLOW" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31 00:00:00",
                        "REPORT_TYPE": "年报",
                        "OBTAIN_SUBSIDIARY_OTHER": "25000000",
                    }
                )
            return _statement_payload()

    row = Stub().fetch_financials(["000333.SZ"])[0]
    assert row.ocf == Decimal("130000000")
    assert row.capex == Decimal("80000000")
    assert row.acquisition_cash_paid == Decimal("25000000")


def test_discovery_financial_industry_source_mapping() -> None:
    from decimal import Decimal

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            if "RPT_F10_FINANCE_MAINFINADATA" in url:
                return _statement_payload(
                    {
                        "REPORT_TYPE": "年报",
                        "REPORT_YEAR": "2025",
                        "REPORT_DATE": "2025-12-31 00:00:00",
                        "NEWCAPITALADER": "18.24",
                        "HXYJBCZL": "14.16",
                        "NONPERLOAN": "0.94",
                        "NON_PERFORMING_LOAN": "68206000000",
                    }
                )
            return _statement_payload()

    row = Stub().fetch_financials(["600036.SH"])[0]
    assert row.capital_adequacy == Decimal("18.24")
    assert row.core_tier1_capital_adequacy == Decimal("14.16")
    assert row.npl == Decimal("0.94")
    assert row.nonperforming_loan_balance == Decimal("68206000000")
