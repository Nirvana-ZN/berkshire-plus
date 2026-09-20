from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from market.eastmoney import (
    BOARD_LIST_URL,
    CLIST_HOSTS,
    CLIST_PAGE_RETRY_ROUNDS,
    EastMoneyClient,
    board_code,
    pick_board,
    pick_equity,
)
from market.types import (
    DiscoveryRequestBudgetExhausted,
    DiscoveryRequestRuntimeExhausted,
    MarketError,
    TransientMarketError,
)


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


class _ClistResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self.payload


def _install_clist_transport(monkeypatch, get) -> None:
    import httpx2

    class Client:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def get(self, url: str):
            return get(url)

    monkeypatch.setattr(httpx2, "Client", Client)


def test_clist_retries_only_transient_current_page(monkeypatch) -> None:
    calls: dict[int, int] = {}

    def get(url: str):
        page = int(parse_qs(urlsplit(url).query)["pn"][0])
        calls[page] = calls.get(page, 0) + 1
        if page == 19 and calls[page] <= len(CLIST_HOSTS):
            raise OSError("disconnected")
        size = 100 if page < 19 else 1
        return _ClistResponse(
            {
                "data": {
                    "diff": [
                        {"f12": f"{page:02d}{index:04d}"}
                        for index in range(size)
                    ]
                }
            }
        )

    _install_clist_transport(monkeypatch, get)
    rows = EastMoneyClient()._clist_diff(fs="test", fields="f12", fid="f12")

    assert len(rows) == 1801
    assert [calls[page] for page in range(1, 19)] == [1] * 18
    assert calls[19] == len(CLIST_HOSTS) + 1


def test_clist_persistent_fetch_failure_is_bounded(monkeypatch) -> None:
    calls = 0

    def get(_url: str):
        nonlocal calls
        calls += 1
        raise OSError("disconnected")

    _install_clist_transport(monkeypatch, get)
    with pytest.raises(MarketError, match=r"page 1.*attempt 3/3"):
        EastMoneyClient()._clist_diff(fs="test", fields="f12", fid="f12")
    assert calls == len(CLIST_HOSTS) * CLIST_PAGE_RETRY_ROUNDS


def test_clist_nonobject_json_is_permanent_after_one_request(monkeypatch) -> None:
    calls = 0

    def get(_url: str):
        nonlocal calls
        calls += 1
        return _ClistResponse([])

    _install_clist_transport(monkeypatch, get)
    with pytest.raises(MarketError, match="接口返回不是对象") as caught:
        EastMoneyClient()._clist_diff(fs="test", fields="f12", fid="f12")
    assert not isinstance(caught.value, TransientMarketError)
    assert calls == 1


@pytest.mark.parametrize(
    "error_type",
    [DiscoveryRequestBudgetExhausted, DiscoveryRequestRuntimeExhausted],
)
def test_clist_request_stop_is_not_retried(error_type) -> None:
    calls = 0

    class Stub(EastMoneyClient):
        def _get_json(self, _url: str) -> dict:
            nonlocal calls
            calls += 1
            raise error_type("request stopped")

    with pytest.raises(error_type, match="request stopped"):
        Stub()._clist_diff(fs="test", fields="f12", fid="f12")
    assert calls == 1


def test_clist_malformed_payload_is_not_retried() -> None:
    calls = 0

    class Stub(EastMoneyClient):
        def _get_json(self, _url: str) -> dict:
            nonlocal calls
            calls += 1
            return {"data": {"diff": "not-a-list"}}

    with pytest.raises(MarketError, match="payload"):
        Stub()._clist_diff(fs="test", fields="f12", fid="f12")
    assert calls == 1


def test_clist_repeated_page_is_permanent_no_progress() -> None:
    calls: dict[int, int] = {}
    repeated = [{"f12": f"{index:06d}"} for index in range(100)]

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            page = int(parse_qs(urlsplit(url).query)["pn"][0])
            calls[page] = calls.get(page, 0) + 1
            return {"data": {"diff": repeated}}

    with pytest.raises(MarketError, match="no progress"):
        Stub()._clist_diff(fs="test", fields="f12", fid="f12")
    assert calls == {1: 1, 2: 1}


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


def test_legacy_financials_use_one_main_request_without_discovery_sources() -> None:
    seen: list[str] = []

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            seen.append(url)
            if "MAINFINADATA" not in url:
                raise MarketError("discovery statement source unavailable")
            return _statement_payload(
                {
                    "REPORT_TYPE": "年报",
                    "REPORT_YEAR": "2025",
                    "PARENTNETPROFIT": "10",
                }
            )

    row = Stub().fetch_financials(["000333.SZ"])[0]
    assert row.net_income == Decimal("10")
    assert len(seen) == 1
    assert "RPT_F10_FINANCE_MAINFINADATA" in seen[0]


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

    row = Stub()._discovery_financials_for("000333.SZ")[0]
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

    row = Stub()._discovery_financials_for("000333.SZ")[0]
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

    row = Stub()._discovery_financials_for("000333.SZ")[0]
    assert row.ocf == Decimal("130000000")
    assert row.capex == Decimal("80000000")
    assert row.acquisition_cash_paid == Decimal("25000000")


def test_discovery_financial_industry_source_mapping() -> None:
    from decimal import Decimal

    seen: list[str] = []

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            seen.append(url)
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
                        "TOTALOPERATEREVE": "337532000000",
                        "PARENTNETPROFIT": "150181000000",
                        "KCFJCXSYJLR": "150007000000",
                        "TOTAL_ASSETS_PK": "13070523000000",
                        "TOTAL_EQUITY_PK": "1280899000000",
                        "NETCASH_OPERATE_PK": "451457000000",
                    }
                )
            if (
                "RPT_F10_FINANCE_GBALANCE" in url
                or "RPT_F10_FINANCE_GCASHFLOW" in url
            ):
                return {
                    "success": False,
                    "message": "report inapplicable to bank",
                }
            return _statement_payload()

    row = Stub()._discovery_financials_for(
        "600036.SH", industry="银行Ⅱ"
    )[0]
    assert row.revenue == Decimal("337532000000")
    assert row.net_income == Decimal("150181000000")
    assert row.deducted_net_income == Decimal("150007000000")
    assert row.total_assets == Decimal("13070523000000")
    assert row.net_assets == Decimal("1280899000000")
    assert row.ocf == Decimal("451457000000")
    assert row.capital_adequacy == Decimal("18.24")
    assert row.core_tier1_capital_adequacy == Decimal("14.16")
    assert row.npl == Decimal("0.94")
    assert row.nonperforming_loan_balance == Decimal("68206000000")
    assert row.roic is None
    assert not any("RPT_F10_FINANCE_GBALANCE" in url for url in seen)
    assert not any("RPT_F10_FINANCE_GCASHFLOW" in url for url in seen)


def test_discovery_insurance_industry_uses_shared_branch_convention() -> None:
    seen: list[str] = []

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            seen.append(url)
            if "MAINFINADATA" in url:
                return _statement_payload(
                    {
                        "REPORT_TYPE": "年报",
                        "REPORT_YEAR": "2025",
                        "TOTALOPERATEREVE": "100",
                    }
                )
            if (
                "RPT_F10_FINANCE_GBALANCE" in url
                or "RPT_F10_FINANCE_GCASHFLOW" in url
            ):
                return {
                    "success": False,
                    "message": "report inapplicable to insurance",
                }
            return _statement_payload()

    row = Stub()._discovery_financials_for(
        "601336.SH", industry="保险Ⅱ"
    )[0]
    assert row.revenue == Decimal("100")
    assert not any("RPT_F10_FINANCE_GBALANCE" in url for url in seen)
    assert not any("RPT_F10_FINANCE_GCASHFLOW" in url for url in seen)


def test_discovery_strict_parser_rejects_legacy_aliases() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            if "MAINFINADATA" in url:
                return _statement_payload(
                    {
                        "REPORT_TYPE": "年报",
                        "REPORT_YEAR": "2025",
                        "TOTALSHARE": "2",
                        "NETPROFIT": "99",
                        "NETCASHFLOWOPERATE": "88",
                        "EBIT": "77",
                    }
                )
            if "RPT_DMSK_FN_INCOME" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31",
                        "DATE_TYPE_CODE": "001",
                        "TOTALOPERATEREVE": "66",
                        "PARENTNETPROFIT": "55",
                    }
                )
            if "RPT_DMSK_FN_BALANCE" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31",
                        "DATE_TYPE_CODE": "001",
                        "TOTALASSETS": "44",
                        "MONETARY_FUNDS": "33",
                    }
                )
            if "RPT_DMSK_FN_CASHFLOW" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31",
                        "DATE_TYPE_CODE": "001",
                        "NETCASH_OPERATE_PK": "22",
                        "CAPEX": "11",
                    }
                )
            return _statement_payload()

    strict = Stub()._discovery_financials_for("000333.SZ")[0]
    assert strict.revenue is None
    assert strict.net_income is None
    assert strict.total_assets is None
    assert strict.cash is None
    assert strict.ocf is None
    assert strict.capex is None
    assert strict.ebit is None

    legacy = Stub()._financials_for("000333.SZ")[0]
    assert legacy.net_income is not None
    assert legacy.ocf is not None
    assert legacy.ebit is not None


def test_discovery_missing_report_type_does_not_become_annual() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            if "RPT_DMSK_FN_INCOME" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2026-09-30",
                        "TOTAL_OPERATE_INCOME": "99",
                    }
                )
            return _statement_payload()

    assert Stub()._discovery_financials_for("000333.SZ") == []


def test_discovery_duplicate_period_uses_newest_valid_source_date() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            if "RPT_DMSK_FN_INCOME" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2025-12-31",
                        "DATE_TYPE_CODE": "001",
                        "UPDATE_DATE": "2026-03-01",
                        "TOTAL_OPERATE_INCOME": "10",
                    },
                    {
                        "REPORT_DATE": "2025-12-31",
                        "DATE_TYPE_CODE": "001",
                        "UPDATE_DATE": "2026-03-20",
                        "TOTAL_OPERATE_INCOME": "20",
                    },
                )
            return _statement_payload()

    row = Stub()._discovery_financials_for("000333.SZ")[0]
    assert row.revenue == Decimal("20")
    assert row.source_as_of == "2026-03-20"


def test_discovery_periods_sort_by_report_end_chronology() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            if "RPT_DMSK_FN_INCOME" in url:
                return _statement_payload(
                    {
                        "REPORT_DATE": "2026-03-31",
                        "DATE_TYPE_CODE": "003",
                        "TOTAL_OPERATE_INCOME": "10",
                    },
                    {
                        "REPORT_DATE": "2026-06-30",
                        "DATE_TYPE_CODE": "002",
                        "TOTAL_OPERATE_INCOME": "20",
                    },
                )
            return _statement_payload()

    rows = Stub()._discovery_financials_for("000333.SZ")
    assert [row.period for row in rows] == ["2026H1", "2026Q1"]


def _assert_within_one_percent(
    actual: object, disclosed: Decimal
) -> None:
    assert isinstance(actual, Decimal)
    denominator = max(abs(disclosed), Decimal("1"))
    assert abs(actual - disclosed) / denominator <= Decimal("0.01")


def _crosscheck_row(
    code: str, reports: dict[str, dict[str, object]]
):
    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            for report, row in reports.items():
                if f"reportName={report}" in url:
                    return _statement_payload(row)
            return _statement_payload()

    return Stub()._discovery_financials_for(code)[0]


@pytest.mark.parametrize(
    ("code", "reports", "disclosed"),
    [
        (
            "000333.SZ",
            {
                "RPT_F10_FINANCE_MAINFINADATA": {
                    "REPORT_TYPE": "年报",
                    "REPORT_YEAR": "2025",
                    "PER_EBIT": "6.167102641848",
                    "TOTAL_SHARE": "7629346190",
                },
                "RPT_DMSK_FN_INCOME": {
                    "REPORT_DATE": "2025-12-31",
                    "DATE_TYPE_CODE": "001",
                    "TOTAL_OPERATE_INCOME": "458502407000",
                    "PARENT_NETPROFIT": "43945411000",
                    "DEDUCT_PARENT_NETPROFIT": "41267233000",
                    "TOTAL_PROFIT": "53085343000",
                    "INCOME_TAX": "8565147000",
                },
                "RPT_DMSK_FN_BALANCE": {
                    "REPORT_DATE": "2025-12-31",
                    "DATE_TYPE_CODE": "001",
                    "TOTAL_ASSETS": "608791766000",
                    "TOTAL_EQUITY": "236424223000",
                    "MONETARYFUNDS": "85247150000",
                },
                "RPT_DMSK_FN_CASHFLOW": {
                    "REPORT_DATE": "2025-12-31",
                    "DATE_TYPE_CODE": "001",
                    "NETCASH_OPERATE": "53345930000",
                    "CONSTRUCT_LONG_ASSET": "11141889000",
                },
                "RPT_F10_FINANCE_GINCOME": {
                    "REPORT_DATE": "2025-12-31",
                    "REPORT_TYPE": "年报",
                    "ASSET_IMPAIRMENT_INCOME": "-1156469000",
                    "CREDIT_IMPAIRMENT_INCOME": "-355860000",
                    "ASSET_DISPOSAL_INCOME": "-76024000",
                },
                "RPT_F10_FINANCE_GBALANCE": {
                    "REPORT_DATE": "2025-12-31",
                    "REPORT_TYPE": "年报",
                    "SHORT_LOAN": "43904550000",
                    "LONG_LOAN": "12658843000",
                    "BOND_PAYABLE": "3194774000",
                    "NONCURRENT_LIAB_1YEAR": "5821777000",
                    "GOODWILL": "34256859000",
                },
                "RPT_F10_FINANCE_GCASHFLOW": {
                    "REPORT_DATE": "2025-12-31",
                    "REPORT_TYPE": "年报",
                    "OBTAIN_SUBSIDIARY_OTHER": "6155136000",
                },
            },
            {
                "revenue": Decimal("458502407000"),
                "net_income": Decimal("43945411000"),
                "deducted_net_income": Decimal("41267233000"),
                "pretax_income": Decimal("53085343000"),
                "income_tax": Decimal("8565147000"),
                "total_assets": Decimal("608791766000"),
                "net_assets": Decimal("236424223000"),
                "cash": Decimal("85247150000"),
                "short_loan": Decimal("43904550000"),
                "long_loan": Decimal("12658843000"),
                "bond_payable": Decimal("3194774000"),
                "noncurrent_liab_1year": Decimal("5821777000"),
                "interest_debt": Decimal("65579944000"),
                "ocf": Decimal("53345930000"),
                "capex": Decimal("11141889000"),
                "impairment_loss": Decimal("1512329000"),
                "asset_disposal_gain": Decimal("-76024000"),
                "goodwill": Decimal("34256859000"),
                "acquisition_cash_paid": Decimal("6155136000"),
            },
        ),
        (
            "600036.SH",
            {
                "RPT_F10_FINANCE_MAINFINADATA": {
                    "REPORT_TYPE": "年报",
                    "REPORT_YEAR": "2025",
                    "NEWCAPITALADER": "18.24",
                    "HXYJBCZL": "14.16",
                    "NONPERLOAN": "0.94",
                    "NON_PERFORMING_LOAN": "68206000000",
                },
                "RPT_DMSK_FN_INCOME": {
                    "REPORT_DATE": "2025-12-31",
                    "DATE_TYPE_CODE": "001",
                    "TOTAL_OPERATE_INCOME": "337532000000",
                    "PARENT_NETPROFIT": "150181000000",
                    "DEDUCT_PARENT_NETPROFIT": "150007000000",
                    "TOTAL_PROFIT": "178993000000",
                    "INCOME_TAX": "27867000000",
                },
                "RPT_DMSK_FN_BALANCE": {
                    "REPORT_DATE": "2025-12-31",
                    "DATE_TYPE_CODE": "001",
                    "TOTAL_ASSETS": "13070523000000",
                    "TOTAL_EQUITY": "1280899000000",
                    "MONETARYFUNDS": "575015000000",
                },
                "RPT_DMSK_FN_CASHFLOW": {
                    "REPORT_DATE": "2025-12-31",
                    "DATE_TYPE_CODE": "001",
                    "NETCASH_OPERATE": "451457000000",
                    "CONSTRUCT_LONG_ASSET": "28141000000",
                },
                "RPT_F10_FINANCE_GINCOME": {
                    "REPORT_DATE": "2025-12-31",
                    "REPORT_TYPE": "年报",
                    "CREDIT_IMPAIRMENT_LOSS": "39586000000",
                },
            },
            {
                "revenue": Decimal("337532000000"),
                "net_income": Decimal("150181000000"),
                "deducted_net_income": Decimal("150007000000"),
                "pretax_income": Decimal("178993000000"),
                "income_tax": Decimal("27867000000"),
                "total_assets": Decimal("13070523000000"),
                "net_assets": Decimal("1280899000000"),
                "cash": Decimal("575015000000"),
                "ocf": Decimal("451457000000"),
                "capex": Decimal("28141000000"),
                "capital_adequacy": Decimal("18.24"),
                "core_tier1_capital_adequacy": Decimal("14.16"),
                "npl": Decimal("0.94"),
                "nonperforming_loan_balance": Decimal("68206000000"),
                "impairment_loss": Decimal("39586000000"),
            },
        ),
        (
            "600519.SH",
            {
                "RPT_F10_FINANCE_MAINFINADATA": {
                    "REPORT_TYPE": "年报",
                    "REPORT_YEAR": "2025",
                    "PER_EBIT": "90.987558",
                    "TOTAL_SHARE": "1250081601",
                },
                "RPT_DMSK_FN_INCOME": {
                    "REPORT_DATE": "2025-12-31",
                    "DATE_TYPE_CODE": "001",
                    "TOTAL_OPERATE_INCOME": "172054171890.91",
                    "PARENT_NETPROFIT": "82320067101.68",
                    "DEDUCT_PARENT_NETPROFIT": "82293107655.25",
                    "TOTAL_PROFIT": "114755261605.08",
                    "INCOME_TAX": "29444936771.41",
                },
                "RPT_DMSK_FN_BALANCE": {
                    "REPORT_DATE": "2025-12-31",
                    "DATE_TYPE_CODE": "001",
                    "TOTAL_ASSETS": "303834844021.44",
                    "TOTAL_EQUITY": "253959253909.07",
                    "MONETARYFUNDS": "51690610946.5",
                },
                "RPT_DMSK_FN_CASHFLOW": {
                    "REPORT_DATE": "2025-12-31",
                    "DATE_TYPE_CODE": "001",
                    "NETCASH_OPERATE": "61522204989.35",
                    "CONSTRUCT_LONG_ASSET": "3127594916.41",
                },
                "RPT_F10_FINANCE_GINCOME": {
                    "REPORT_DATE": "2025-12-31",
                    "REPORT_TYPE": "年报",
                    "CREDIT_IMPAIRMENT_INCOME": "17234379.37",
                    "ASSET_DISPOSAL_INCOME": "3106625.19",
                },
                "RPT_F10_FINANCE_GBALANCE": {
                    "REPORT_DATE": "2025-12-31",
                    "REPORT_TYPE": "年报",
                    "NONCURRENT_LIAB_1YEAR": "44206237.05",
                },
            },
            {
                "revenue": Decimal("172054171890.91"),
                "net_income": Decimal("82320067101.68"),
                "deducted_net_income": Decimal("82293107655.25"),
                "pretax_income": Decimal("114755261605.08"),
                "income_tax": Decimal("29444936771.41"),
                "total_assets": Decimal("303834844021.44"),
                "net_assets": Decimal("253959253909.07"),
                "cash": Decimal("51690610946.5"),
                "noncurrent_liab_1year": Decimal("44206237.05"),
                "interest_debt": Decimal("44206237.05"),
                "ocf": Decimal("61522204989.35"),
                "capex": Decimal("3127594916.41"),
                "impairment_loss": Decimal("-17234379.37"),
                "asset_disposal_gain": Decimal("3106625.19"),
            },
        ),
    ],
)
def test_discovery_sources_match_disclosed_annual_reports(
    code: str,
    reports: dict[str, dict[str, object]],
    disclosed: dict[str, Decimal],
) -> None:
    row = _crosscheck_row(code, reports)
    for field, expected in disclosed.items():
        _assert_within_one_percent(getattr(row, field), expected)
    assert row.ebit is None
    assert row.normalized_effective_tax_rate is None
    assert row.invested_capital is None
    assert row.roic is None
    assert row.government_grants is None
    if code == "600036.SH":
        assert row.short_loan is None
        assert row.long_loan is None
        assert row.bond_payable is None
        assert row.noncurrent_liab_1year is None
        assert row.goodwill is None
        assert row.acquisition_cash_paid is None
        assert row.asset_disposal_gain is None
    if code == "600519.SH":
        assert row.goodwill is None
        assert row.acquisition_cash_paid is None
