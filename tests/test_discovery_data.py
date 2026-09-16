import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Setting
from market.cheap import (
    CACHE_KEY,
    DELISTING_NAME_METHOD,
    METHOD,
    CheapRow,
    _cached,
    eligible_cheap,
    is_st_name,
    load_cheap_universe,
    security_state_eligible,
)
from market.eastmoney import EastMoneyClient, FakeMarketClient
from market.types import (
    DiscoveryCompanyData,
    DiscoveryCompanyError,
    DiscoverySourceAsOf,
    FinancialRow,
    MarketError,
    SecurityState,
    ValuationPoint,
)


# Live probe 2026-09-16, RPT_VALUEANALYSIS_DET, pageSize=500:
# 600036/600519/000333 each needed 3 of 5 API pages to cross 2021-09-01;
# each returned 1,500 rows (2020-07-14..2026-09-15), 61 in-range
# months, 61 positive PE_TTM, and 61 positive PB_MRQ. Exact mapped fields:
# TRADE_DATE, PE_TTM, PB_MRQ, CLOSE_PRICE.
#
# The HS-A clist probe returned 5,559 SH/SZ rows and exact fields f12 code,
# f13 market, f14 name, f2 price, f26 listing date, f100 industry, f124
# quote timestamp, and f292 market status. f26 matched ORGINFO LISTING_DATE
# for 600036 (2002-04-09), 600519 (2001-08-27), and 000333 (2013-09-18).
# Observed raw f292 counts were 2=5,206, 6=14, 7=334, and 9=5.
# Those codes are preserved but not treated as a status dictionary: f292=6
# only selects 14 K-line probes, and the returned dates determine suspension.
# ST/*ST is explicit in f14. East Money returns no taxonomy edition; the
# stored industry version names our f100 mapping, and industry_as_of records
# our universe snapshot date rather than claiming an East Money edition.

# Financial source probe 2026-09-16:
# RPT_DMSK_FN_{INCOME,BALANCE,CASHFLOW} returned CNY statement totals.
# Detailed RPT_F10_FINANCE_G{INCOME,BALANCE,GCASHFLOW} supplied debt,
# goodwill, impairment/disposal, and acquisition cash. MAINFINADATA supplied
# bank NEWCAPITALADER/HXYJBCZL/NONPERLOAN. OTHER_INCOME was not accepted as
# government grants because the disclosed line includes unrelated other income.
#
# 2025 annual-report PDF evidence used by the reproducible <=1% fixture
# assertions in test_eastmoney.py:
# - Industrial 美的 000333, AN202603311820893096: revenue 458,502,407,000;
#   assets 608,791,766,000; equity 236,424,223,000; OCF 53,345,930,000;
#   capex 11,141,889,000; all four debt components and goodwill also matched.
# - Bank 招商 600036, AN202603271820811738: revenue 337,532m; assets
#   13,070,523m; OCF 451,457m; capital/core-tier-1/NPL 18.24%/14.16%/0.94%.
# - Asset-light 茅台 600519, AN202604161821275610: revenue
#   172,054,171,890.91; assets 303,834,844,021.44; OCF 61,522,204,989.35;
#   capex 3,127,594,916.41; pretax/tax also matched exactly.


def test_fetch_valuation_history_maps_monthly_pe_pb() -> None:
    cutoff = (
        date(date.today().year - 5, date.today().month, 1) - timedelta(days=1)
    ).isoformat()

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            assert "RPT_VALUEANALYSIS_DET" in url
            assert "SECURITY_CODE%3D%22600519%22" in url
            return {
                "success": True,
                "result": {
                    "pages": 1,
                    "data": [
                        {
                            "TRADE_DATE": "2026-08-31 00:00:00",
                            "PE_TTM": "19.2",
                            "PB_MRQ": "7.1",
                            "CLOSE_PRICE": "1460.0",
                        },
                        {
                            "TRADE_DATE": "2026-08-01 00:00:00",
                            "PE_TTM": "20.0",
                            "PB_MRQ": "7.3",
                            "CLOSE_PRICE": "1450.0",
                        },
                        {
                            "TRADE_DATE": "2026-07-31 00:00:00",
                            "PE_TTM": "-1",
                            "PB_MRQ": "7.0",
                            "CLOSE_PRICE": "1440.0",
                        },
                        {
                            "TRADE_DATE": cutoff,
                            "PE_TTM": "18",
                            "PB_MRQ": "6",
                            "CLOSE_PRICE": "1400",
                        },
                    ],
                },
            }

    rows = Stub().fetch_valuation_history("600519.SH")

    assert [
        (row.month, row.trade_date, row.pe, row.pb, row.price)
        for row in rows
    ] == [
        (
            "2026-08",
            "2026-08-31",
            Decimal("19.2"),
            Decimal("7.1"),
            Decimal("1460.0"),
        ),
        ("2026-07", "2026-07-31", None, Decimal("7.0"), Decimal("1440.0")),
    ]
    assert rows[0].source == "eastmoney:RPT_VALUEANALYSIS_DET"
    assert rows[0].ratio_method == "PE_TTM/PB_MRQ"


def _month_starts() -> list[str]:
    today = date.today()
    year, month = today.year - 5, today.month
    rows = []
    while (year, month) <= (today.year, today.month):
        rows.append(f"{year:04d}-{month:02d}-01 00:00:00")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return rows


def test_fetch_valuation_history_paginates_before_month_deduplication() -> None:
    months = list(reversed(_month_starts()))
    year = date.today().year
    pages = {
        1: [
            {"TRADE_DATE": day, "PE_TTM": "123456.789", "PB_MRQ": "0"}
            for day in months[:30]
        ]
        + [{"TRADE_DATE": f"{year}-08-01", "PE_TTM": "20", "PB_MRQ": "3"}],
        2: [
            {"TRADE_DATE": day, "PE_TTM": "10", "PB_MRQ": "2"}
            for day in months[30:]
        ]
        + [{"TRADE_DATE": f"{year}-08-31", "PE_TTM": "21", "PB_MRQ": "4"}],
    }

    class Stub(EastMoneyClient):
        def __init__(self) -> None:
            super().__init__()
            self.requested_pages: list[int] = []

        def _get_json(self, url: str) -> dict:
            page = int(parse_qs(urlsplit(url).query)["pageNumber"][0])
            self.requested_pages.append(page)
            return {"success": True, "result": {"pages": 2, "data": pages[page]}}

    client = Stub()
    rows = client.fetch_valuation_history("600519.SH")

    assert client.requested_pages == [1, 2]
    assert len(rows) >= 61
    august = next(row for row in rows if row.month == f"{year}-08")
    assert august.trade_date == f"{year}-08-31"
    assert august.pe == Decimal("21")
    assert august.pb == Decimal("4")
    assert any(row.pe == Decimal("123456.789") for row in rows)
    assert any(row.pb is None for row in rows)


def test_fetch_valuation_history_returns_declared_recent_listing_pages() -> None:
    pages = {
        1: [
            {
                "TRADE_DATE": "2026-09-16 00:00:00",
                "PE_TTM": "20",
                "PB_MRQ": "2",
            },
            {
                "TRADE_DATE": "2026-08-31 00:00:00",
                "PE_TTM": "21",
                "PB_MRQ": "2.1",
            },
        ],
        2: [
            {
                "TRADE_DATE": "2025-12-31 00:00:00",
                "PE_TTM": "22",
                "PB_MRQ": "2.2",
            },
            {
                "TRADE_DATE": "2025-11-28 00:00:00",
                "PE_TTM": "23",
                "PB_MRQ": "2.3",
            },
        ],
    }

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            page = int(parse_qs(urlsplit(url).query)["pageNumber"][0])
            return {"success": True, "result": {"pages": 2, "data": pages[page]}}

    rows = Stub().fetch_valuation_history("001389.SZ")

    assert [row.trade_date for row in rows] == [
        "2026-09-16",
        "2026-08-31",
        "2025-12-31",
        "2025-11-28",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "result": []},
        {"success": True, "result": {"data": {}}},
        {"success": True, "result": {"pages": "2", "data": []}},
        {"success": True, "result": {"pages": 1, "data": [None]}},
    ],
)
def test_fetch_valuation_history_rejects_malformed_pages(payload: dict) -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, _url: str) -> dict:
            return payload

    with pytest.raises(MarketError):
        Stub().fetch_valuation_history("600519.SH")


def test_fetch_valuation_history_rejects_repeated_full_page() -> None:
    calls = 0
    page = [
        {
            "TRADE_DATE": "2026-09-15 00:00:00",
            "PE_TTM": "20",
            "PB_MRQ": "7",
            "CLOSE_PRICE": str(index + 1),
        }
        for index in range(500)
    ]

    class Stub(EastMoneyClient):
        def _get_json(self, _url: str) -> dict:
            nonlocal calls
            calls += 1
            return {"success": True, "result": {"data": page}}

    with pytest.raises(MarketError, match="重复|进展"):
        Stub().fetch_valuation_history("600519.SH")
    assert calls == 2


def test_fetch_valuation_history_stops_at_conservative_page_cap() -> None:
    calls = 0

    class Stub(EastMoneyClient):
        def _get_json(self, _url: str) -> dict:
            nonlocal calls
            calls += 1
            if calls > 25:
                raise AssertionError("valuation pagination did not stop")
            day = (date.today() - timedelta(days=calls)).isoformat()
            return {
                "success": True,
                "result": {
                    "data": [
                        {
                            "TRADE_DATE": day,
                            "PE_TTM": "20",
                            "PB_MRQ": "7",
                            "CLOSE_PRICE": str(index + 1),
                        }
                        for index in range(500)
                    ]
                },
            }

    with pytest.raises(MarketError, match="上限"):
        Stub().fetch_valuation_history("600519.SH")
    assert calls <= 25


def test_fake_market_exposes_valuation_history_and_trading_dates() -> None:
    point = ValuationPoint(
        code="600519.SH",
        month="2026-08",
        trade_date="2026-08-31",
        pe=Decimal("19.2"),
        pb=Decimal("7.1"),
    )
    dates = ["2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]
    client = FakeMarketClient(valuation_history=[point], trading_dates=dates)

    assert client.fetch_valuation_history("600519.SH") == [point]
    assert client.fetch_valuation_history("000333.SZ") == []
    assert client.fetch_trading_dates("2026-09-15", limit=3) == dates[-3:]


def test_fetch_trading_dates_reuses_existing_index_kline_parser() -> None:
    class Stub(EastMoneyClient):
        def _get_json_direct(self, url: str) -> dict:
            assert "secid=0.399317" in url
            assert "end=20260916" in url
            assert "lmt=6" in url
            return {
                "data": {
                    "klines": [
                        "2026-09-09,1,2,3,4,5,6",
                        "2026-09-10,1,2,3,4,5,6",
                        "2026-09-11,1,2,3,4,5,6",
                        "2026-09-14,1,2,3,4,5,6",
                        "2026-09-15,1,2,3,4,5,6",
                        "2026-09-16,1,2,3,4,5,6",
                    ]
                }
            }

    assert Stub().fetch_trading_dates("2026-09-16") == [
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
    ]


def test_cheap_universe_freezes_fields_and_live_row_is_eligible() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, _url: str) -> dict:
            return {
                "data": {
                    "diff": [
                        {
                            "f12": "600036",
                            "f13": 1,
                            "f14": "招商银行",
                            "f2": "42",
                            "f9": "6",
                            "f23": "0.8",
                            "f20": "100",
                            "f26": 20020409,
                            "f100": "银行Ⅱ",
                            # Quote timestamp is deliberately one day older
                            # than the frozen exchange date below.
                            "f124": 1789430400,
                            "f292": 2,
                        },
                        {
                            "f12": "605303",
                            "f13": 1,
                            "f14": "园林股份",
                            "f2": "-",
                            "f9": "10",
                            "f23": "1",
                            "f20": "50",
                            "f26": 20210301,
                            "f100": "基础建设",
                            "f124": 1789518620,
                            "f292": 6,
                        },
                        {
                            "f12": "600001",
                            "f13": 1,
                            "f14": "S*ST公司",
                            "f2": "10",
                            "f9": "5",
                            "f23": "1",
                            "f20": "50",
                            "f26": 20000101,
                            "f100": "化工",
                            "f292": 2,
                        },
                        {
                            "f12": "603056",
                            "f13": 1,
                            "f14": "德邦股份",
                            "f2": "-",
                            "f9": "-",
                            "f23": "-",
                            "f20": "-",
                            "f26": 20180116,
                            "f100": "-",
                            "f124": 1789516800,
                            "f292": 7,
                        },
                        {
                            "f12": "601238",
                            "f13": 1,
                            "f14": "广汽集团",
                            "f2": "-",
                            "f9": "10",
                            "f23": "1",
                            "f20": "50",
                            "f26": 20120329,
                            "f100": "乘用车",
                            "f124": 1789518616,
                            "f292": 6,
                        },
                        {
                            "f12": "688555",
                            "f13": 1,
                            "f14": "退市泽达",
                            "f2": "-",
                            "f9": "-",
                            "f23": "-",
                            "f20": "-",
                            "f26": 20200623,
                            "f100": "-",
                            "f124": 1789516800,
                            "f292": 7,
                        },
                        {
                            "f12": "601091",
                            "f13": 1,
                            "f14": "沈鼓集团",
                            "f2": "-",
                            "f9": "-",
                            "f23": "-",
                            "f20": "-",
                            "f26": 20260917,
                            "f100": "通用设备",
                            "f124": 1789516800,
                            "f292": 9,
                        },
                        {
                            "f12": "000004",
                            "f13": 0,
                            "f14": "国华退",
                            "f2": "-",
                            "f9": "10",
                            "f23": "1",
                            "f20": "50",
                            "f26": 19910114,
                            "f100": "软件开发",
                            "f124": 1789516800,
                            "f292": 6,
                        },
                        {
                            "f12": "430047",
                            "f13": 0,
                            "f14": "诺思兰德",
                            "f2": "20",
                            "f26": 20201124,
                            "f100": "生物制品",
                            "f124": 1789516800,
                            "f292": 2,
                        },
                    ]
                }
            }

        def _get_json_direct(self, url: str) -> dict:
            if "secid=0.399317" in url:
                return {
                    "data": {
                        "klines": [
                            "2026-09-09,1,2,3,4,5,6",
                            "2026-09-10,1,2,3,4,5,6",
                            "2026-09-11,1,2,3,4,5,6",
                            "2026-09-14,1,2,3,4,5,6",
                            "2026-09-15,1,2,3,4,5,6",
                            "2026-09-16,1,2,3,4,5,6",
                        ]
                    }
                }
            if "secid=1.601238" in url:
                raise RuntimeError("stock kline unavailable")
            if "secid=0.000004" in url:
                return {"data": {"klines": ["2026-09-10,1,2,3,4,5,6"]}}
            assert "secid=1.605303" in url
            return {"data": {"klines": ["2026-09-10,23,24,25,26,27,28"]}}

    by_code = {row.code: row for row in Stub().fetch_cheap_universe()}
    frozen_dates = [
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
    ]

    assert set(by_code) == {
        "600036.SH",
        "605303.SH",
        "603056.SH",
        "688555.SH",
        "601238.SH",
        "601091.SH",
        "000004.SZ",
        "600001.SH",
    }
    normal = by_code["600036.SH"]
    assert normal.exchange == "SH"
    assert normal.listing_date == "2002-04-09"
    assert normal.is_st is False
    assert normal.is_delisting is False
    assert normal.trading_status == "trading"
    assert normal.last_trade_date == "2026-09-16"
    assert normal.industry == "银行Ⅱ"
    assert normal.industry_version == "eastmoney:push2delay:clist:f100:v1"
    assert normal.industry_as_of == date.today().isoformat()
    assert security_state_eligible(normal, frozen_dates)
    assert by_code["600001.SH"].is_st is True
    assert not security_state_eligible(by_code["600001.SH"], frozen_dates)
    suspended = by_code["605303.SH"]
    assert suspended.trading_status == "suspended"
    assert suspended.last_trade_date == "2026-09-10"
    assert suspended.is_delisting is False
    assert security_state_eligible(suspended, frozen_dates)
    marked = by_code["000004.SZ"]
    assert marked.trading_status == "suspended"
    assert marked.is_delisting is True
    assert not security_state_eligible(marked, frozen_dates)
    unavailable = by_code["601238.SH"]
    assert unavailable.trading_status is None
    assert unavailable.last_trade_date is None
    assert by_code["603056.SH"].is_delisting is False
    assert by_code["688555.SH"].is_delisting is True
    assert not security_state_eligible(by_code["688555.SH"], frozen_dates)
    assert by_code["601091.SH"].is_delisting is False
    assert by_code["601091.SH"].trading_status is None
    assert (
        DELISTING_NAME_METHOD
        == "exchange-rules:sh-prefix-tuishi:sz-suffix-tui:v1"
    )


def test_suspended_security_uses_latest_five_frozen_exchange_dates() -> None:
    dates = [
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
    ]
    row = CheapRow(
        code="600036.SH",
        name="招商银行",
        price=None,
        pe=Decimal("6"),
        pb=Decimal("0.8"),
        mcap=Decimal("100"),
        industry="银行Ⅱ",
        exchange="SH",
        listing_date="2002-04-09",
        is_st=False,
        is_delisting=False,
        trading_status="suspended",
        last_trade_date="2026-09-09",
        industry_version="verified-taxonomy",
        industry_as_of="2026-09-15",
        security_source="eastmoney:push2delay",
        market_status_code=6,
    )

    assert security_state_eligible(row, dates)
    assert not security_state_eligible(replace(row, last_trade_date="2026-09-08"), dates)
    assert not security_state_eligible(
        replace(row, industry_version=None, industry_as_of=None), dates
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ST化工", True),
        ("*ST假药", True),
        ("S*ST公司", True),
        ("SST公司", True),
        ("招商银行", False),
        ("BEST股份", False),
        ("FIRST服务", False),
    ],
)
def test_st_classifier_is_shared_without_false_positives(
    name: str, expected: bool
) -> None:
    row = CheapRow(
        code="600000.SH",
        name=name,
        price=Decimal("10"),
        pe=Decimal("5"),
        pb=Decimal("1"),
        mcap=Decimal("100"),
        industry="化工",
    )

    assert is_st_name(name) is expected
    assert (eligible_cheap([row]) == []) is expected


def test_calendar_failure_does_not_store_degraded_universe() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, _url: str) -> dict:
            return {
                "data": {
                    "diff": [
                        {
                            "f12": "600036",
                            "f13": 1,
                            "f14": "招商银行",
                            "f2": "42",
                            "f9": "6",
                            "f23": "0.8",
                            "f20": "100",
                            "f26": 20020409,
                            "f100": "银行Ⅱ",
                            "f292": 2,
                        }
                    ]
                }
            }

        def _get_json_direct(self, _url: str) -> dict:
            raise RuntimeError("calendar unavailable")

    engine = create_engine("sqlite://")
    Setting.__table__.create(engine)
    with Session(engine) as session:
        with pytest.raises(MarketError, match="calendar unavailable"):
            load_cheap_universe(session, Stub())
        assert session.get(Setting, CACHE_KEY) is None


def test_old_cached_universe_without_security_metadata_is_rejected() -> None:
    engine = create_engine("sqlite://")
    Setting.__table__.create(engine)
    old_row = {
        "code": "600036.SH",
        "name": "招商银行",
        "price": 42,
        "pe": 6,
        "pb": 0.8,
        "mcap": 100,
        "industry": "银行",
    }
    with Session(engine) as session:
        session.add(
            Setting(
                key="cheap_universe",
                value=json.dumps(
                    {"method": "v4", "fetched_at": 1000.0, "rows": [old_row]}
                ),
            )
        )
        session.commit()
        assert METHOD == "v5"
        assert _cached(session, 1001.0) is None
        setting = session.get(Setting, "cheap_universe")
        assert setting is not None
        setting.value = json.dumps(
            {"method": METHOD, "fetched_at": 1000.0, "rows": [old_row]}
        )
        session.commit()
        assert _cached(session, 1001.0) is None


def test_field_dictionary_is_immutable_and_marks_unavailable_sources() -> None:
    from dataclasses import FrozenInstanceError

    from discovery.field_dictionary import FIELD_DICTIONARY

    revenue = FIELD_DICTIONARY["revenue"]
    assert revenue.report == "RPT_DMSK_FN_INCOME"
    assert revenue.field == "TOTAL_OPERATE_INCOME"
    assert revenue.unit == "CNY"
    assert revenue.consolidated_basis == "consolidated"
    assert revenue.period_type == "cumulative"
    assert revenue.derivation_formula is None
    assert revenue.financial_sector_substitute == "TOTAL_OPERATE_INCOME"
    assert revenue.source_as_of == "NOTICE_DATE/UPDATE_DATE"
    with pytest.raises(FrozenInstanceError):
        revenue.field = "GUESSED"  # type: ignore[misc]
    with pytest.raises(TypeError):
        FIELD_DICTIONARY["revenue"] = revenue  # type: ignore[index]

    grants = FIELD_DICTIONARY["government_grants"]
    assert grants.field is None
    assert grants.trusted is False
    assert "OTHER_INCOME" in grants.missing_policy
    assert FIELD_DICTIONARY["ebit"].trusted is False
    assert FIELD_DICTIONARY["invested_capital"].field is None
    assert FIELD_DICTIONARY["roic"].trusted is False
    assert "financial-industry" in FIELD_DICTIONARY["roic"].missing_policy


def test_discovery_roic_stays_none_without_verified_excess_cash() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            if "MAINFINADATA" in url:
                return {
                    "success": True,
                    "result": {
                        "data": [
                            {
                                "REPORT_TYPE": "年报",
                                "REPORT_YEAR": year,
                                "REPORT_DATE": f"{year}-12-31 00:00:00",
                                "PER_EBIT": ebit,
                                "TOTAL_SHARE": "1",
                            }
                            for year, ebit in [
                                ("2025", "120000000"),
                                ("2024", "100000000"),
                                ("2023", "90000000"),
                            ]
                        ]
                    },
                }
            if "RPT_DMSK_FN_INCOME" in url:
                return {
                    "success": True,
                    "result": {
                        "data": [
                            {
                                "REPORT_DATE": f"{year}-12-31 00:00:00",
                                "DATE_TYPE_CODE": "001",
                                "TOTAL_PROFIT": pretax,
                                "INCOME_TAX": tax,
                            }
                            for year, pretax, tax in [
                                ("2025", "120000000", "20000000"),
                                ("2024", "100000000", "15000000"),
                                ("2023", "100000000", "20000000"),
                            ]
                        ]
                    },
                }
            if "RPT_DMSK_FN_BALANCE" in url:
                return {
                    "success": True,
                    "result": {
                        "data": [
                            {
                                "REPORT_DATE": f"{year}-12-31 00:00:00",
                                "DATE_TYPE_CODE": "001",
                                "TOTAL_EQUITY": equity,
                                "MONETARYFUNDS": cash,
                            }
                            for year, equity, cash in [
                                ("2025", "600000000", "200000000"),
                                ("2024", "500000000", "100000000"),
                                ("2023", "400000000", "80000000"),
                            ]
                        ]
                    },
                }
            if "RPT_F10_FINANCE_GBALANCE" in url:
                return {
                    "success": True,
                    "result": {
                        "data": [
                            {
                                "REPORT_DATE": f"{year}-12-31 00:00:00",
                                "REPORT_TYPE": "年报",
                                "SHORT_LOAN": debt,
                            }
                            for year, debt in [
                                ("2025", "150000000"),
                                ("2024", "100000000"),
                                ("2023", "80000000"),
                            ]
                        ]
                    },
                }
            return {"success": True, "result": {"data": []}}

    rows = Stub()._discovery_financials_for("000333.SZ")
    current = next(row for row in rows if row.period == "2025")
    assert current.pretax_income == Decimal("120000000")
    assert current.income_tax == Decimal("20000000")
    assert current.net_assets == Decimal("600000000")
    assert current.interest_debt == Decimal("150000000")
    assert current.cash == Decimal("200000000")
    assert current.ebit is None
    assert current.normalized_effective_tax_rate is None
    assert current.invested_capital is None
    assert current.roic is None


def test_fake_fetch_discovery_data_returns_typed_bundle_or_error() -> None:
    state = SecurityState(
        code="600519.SH",
        name="贵州茅台",
        exchange="SH",
        listing_date="2001-08-27",
        is_st=False,
        is_delisting=False,
        trading_status="trading",
        last_trade_date="2026-09-16",
        industry="白酒",
        industry_version="eastmoney:push2delay:clist:f100:v1",
        industry_as_of="2026-09-16",
        source="eastmoney:push2delay",
        market_status_code=2,
    )
    bundle = DiscoveryCompanyData(
        code="600519.SH",
        security_state=state,
        financial_rows=(
            FinancialRow(code="600519.SH", period="2025", period_type="annual"),
        ),
        valuation_points=(),
        request_count=8,
        source_as_of=DiscoverySourceAsOf(
            security="2026-09-16",
            valuation=None,
            financial="2026-03-31",
        ),
    )
    client = FakeMarketClient(
        discovery_data={"600519.SH": bundle},
        discovery_errors={"000333.SZ": "statement unavailable"},
    )

    assert client.fetch_discovery_data("600519.SH") == bundle
    with pytest.raises(DiscoveryCompanyError, match="statement unavailable") as caught:
        client.fetch_discovery_data("000333.SZ")
    assert caught.value.code == "000333.SZ"


def test_real_fetch_discovery_data_keeps_company_and_stage_on_error() -> None:
    state = SecurityState(
        code="000333.SZ",
        name="美的集团",
        exchange="SZ",
        listing_date="2013-09-18",
        is_st=False,
        is_delisting=False,
        trading_status="trading",
        last_trade_date="2026-09-16",
        industry="白色家电",
        industry_version="eastmoney:push2delay:clist:f100:v1",
        industry_as_of="2026-09-16",
        source="eastmoney:stock/get:f57,f58,f107,f127,f189,f292",
        market_status_code=5,
    )

    class Stub(EastMoneyClient):
        def _security_state_for(self, _code: str) -> SecurityState:
            return state

        def _discovery_financials_for(self, _code: str) -> list[FinancialRow]:
            raise MarketError("income endpoint unavailable")

    with pytest.raises(DiscoveryCompanyError, match="income endpoint unavailable") as caught:
        Stub().fetch_discovery_data("000333.SZ")
    assert caught.value.code == "000333.SZ"
    assert caught.value.stage == "financials"
    assert caught.value.request_count == 0


@pytest.mark.parametrize(
    "failed_report",
    [
        "RPT_F10_FINANCE_GINCOME",
        "RPT_F10_FINANCE_GBALANCE",
        "RPT_F10_FINANCE_GCASHFLOW",
    ],
)
def test_discovery_detail_source_failure_is_typed_without_partial_success(
    failed_report: str,
) -> None:
    state = SecurityState(
        code="000333.SZ",
        name="美的集团",
        exchange="SZ",
        listing_date="2013-09-18",
        is_st=False,
        is_delisting=False,
        trading_status="trading",
        last_trade_date="2026-09-16",
        industry="白色家电",
        industry_version="eastmoney:push2delay:clist:f100:v1",
        industry_as_of="2026-09-16",
        source="eastmoney:stock/get:f57,f58,f107,f127,f189,f292",
    )

    class Stub(EastMoneyClient):
        def _security_state_for(self, _code: str) -> SecurityState:
            return state

        def _get_json(self, url: str) -> dict:
            self._request_count += 1
            if failed_report in url:
                return {"success": False, "message": "verified source unavailable"}
            return {
                "success": True,
                "result": {
                    "data": [
                        {
                            "REPORT_TYPE": "年报",
                            "REPORT_YEAR": "2025",
                            "REPORT_DATE": "2025-12-31",
                            "DATE_TYPE_CODE": "001",
                            "TOTAL_OPERATE_INCOME": "100",
                        }
                    ]
                },
            }

        def fetch_valuation_history(self, _code: str) -> list[ValuationPoint]:
            raise AssertionError("partial company bundle must not continue")

    with pytest.raises(
        DiscoveryCompanyError, match="verified source unavailable"
    ) as caught:
        Stub().fetch_discovery_data("000333.SZ")
    assert caught.value.stage == "financials"
    assert caught.value.request_count > 0


def test_real_discovery_path_reports_per_source_dates_and_request_count() -> None:
    latest_trade = (date.today() - timedelta(days=1)).isoformat()
    old_trade = f"{date.today().year - 6}-01-01"

    class Stub(EastMoneyClient):
        def _get_json_direct(self, url: str) -> dict:
            self._request_count += 1
            if "stock/get" in url:
                return {
                    "data": {
                        "f57": "000333",
                        "f58": "美的集团",
                        "f107": 0,
                        "f127": "白色家电",
                        "f189": 20130918,
                        "f292": 5,
                    }
                }
            if "secid=0.399317" in url:
                return {
                    "data": {
                        "klines": [
                            f"{latest_trade},1,2,3,4,5,6",
                        ]
                    }
                }
            return {"data": {"klines": [f"{latest_trade},1,2,3,4,5,6"]}}

        def _get_json(self, url: str) -> dict:
            self._request_count += 1
            if "RPT_VALUEANALYSIS_DET" in url:
                return {
                    "success": True,
                    "result": {
                        "pages": 1,
                        "data": [
                            {
                                "TRADE_DATE": latest_trade,
                                "PE_TTM": "12",
                                "PB_MRQ": "2",
                            },
                            {
                                "TRADE_DATE": old_trade,
                                "PE_TTM": "10",
                                "PB_MRQ": "1",
                            },
                        ],
                    },
                }
            report_date = {
                "RPT_F10_FINANCE_MAINFINADATA": "2026-03-28",
                "RPT_DMSK_FN_INCOME": "2026-03-31",
                "RPT_DMSK_FN_BALANCE": "2026-03-30",
                "RPT_DMSK_FN_CASHFLOW": "2026-03-30",
                "RPT_F10_FINANCE_GINCOME": "2026-04-01",
                "RPT_F10_FINANCE_GBALANCE": "2026-03-29",
                "RPT_F10_FINANCE_GCASHFLOW": "2026-03-29",
            }
            report = next(name for name in report_date if name in url)
            return {
                "success": True,
                "result": {
                    "data": [
                        {
                            "REPORT_TYPE": "年报",
                            "REPORT_YEAR": "2025",
                            "REPORT_DATE": "2025-12-31",
                            "DATE_TYPE_CODE": "001",
                            "NOTICE_DATE": report_date[report],
                            "UPDATE_DATE": report_date[report],
                            "TOTAL_OPERATE_INCOME": "100",
                        }
                    ]
                },
            }

    result = Stub().fetch_discovery_data("000333.SZ")
    assert result.request_count == 11
    assert result.source_as_of == DiscoverySourceAsOf(
        security=date.today().isoformat(),
        valuation=latest_trade,
        financial="2026-04-01",
    )
