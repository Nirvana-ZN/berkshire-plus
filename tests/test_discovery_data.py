import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Setting
from market.cheap import METHOD, CheapRow, _cached, security_state_eligible
from market.eastmoney import EastMoneyClient, FakeMarketClient
from market.types import ValuationPoint


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


def test_fetch_valuation_history_maps_monthly_pe_pb() -> None:
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
            assert "secid=1.605303" in url
            return {"data": {"klines": ["2026-09-10,23,24,25,26,27,28"]}}

    by_code = {row.code: row for row in Stub().fetch_cheap_universe()}

    assert set(by_code) == {
        "600036.SH",
        "605303.SH",
        "603056.SH",
        "688555.SH",
        "601238.SH",
        "601091.SH",
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
    assert security_state_eligible(
        normal,
        [
            "2026-09-09",
            "2026-09-10",
            "2026-09-11",
            "2026-09-14",
            "2026-09-15",
            "2026-09-16",
        ],
    )
    suspended = by_code["605303.SH"]
    assert suspended.trading_status == "suspended"
    assert suspended.last_trade_date == "2026-09-10"
    assert suspended.is_delisting is None
    unavailable = by_code["601238.SH"]
    assert unavailable.trading_status is None
    assert unavailable.last_trade_date is None
    assert by_code["603056.SH"].is_delisting is None
    assert by_code["688555.SH"].is_delisting is True
    assert by_code["601091.SH"].is_delisting is None
    assert by_code["601091.SH"].trading_status is None


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
                    {"method": "v2", "fetched_at": 1000.0, "rows": [old_row]}
                ),
            )
        )
        session.commit()
        assert METHOD == "v3"
        assert _cached(session, 1001.0) is None
        setting = session.get(Setting, "cheap_universe")
        assert setting is not None
        setting.value = json.dumps(
            {"method": METHOD, "fetched_at": 1000.0, "rows": [old_row]}
        )
        session.commit()
        assert _cached(session, 1001.0) is None
