import json
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import create_session_factory, init_db
from app.main import create_app
from app.models import (
    DiscoveryDataCheckpoint,
    DiscoveryDataFill,
    Financial,
    JobLock,
    Setting,
    ValuationHistory,
)
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


NEW_FINANCIAL_VALUES = {
    "revenue": Decimal("1000.01"),
    "deducted_net_income": Decimal("90.02"),
    "total_assets": Decimal("2000.03"),
    "net_assets": Decimal("800.04"),
    "cash": Decimal("300.05"),
    "short_loan": Decimal("10.06"),
    "long_loan": Decimal("20.07"),
    "bond_payable": Decimal("30.08"),
    "noncurrent_liab_1year": Decimal("40.09"),
    "interest_debt": Decimal("100.10"),
    "pretax_income": Decimal("120.11"),
    "income_tax": Decimal("20.12"),
    "normalized_effective_tax_rate": Decimal("16.13"),
    "invested_capital": Decimal("600.14"),
    "roic": Decimal("18.15"),
    "capital_adequacy": Decimal("14.16"),
    "core_tier1_capital_adequacy": Decimal("12.17"),
    "nonperforming_loan_balance": Decimal("9.18"),
    "impairment_loss": Decimal("8.19"),
    "government_grants": Decimal("7.20"),
    "asset_disposal_gain": Decimal("6.21"),
    "goodwill": Decimal("5.22"),
    "acquisition_cash_paid": Decimal("4.23"),
    "source_as_of": "2026-03-31",
}


def test_init_db_adds_discovery_tables_and_migrates_old_financial_schema(
    tmp_path,
) -> None:
    db_path = tmp_path / "old.db"
    old_engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    with old_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE financials (
                    code VARCHAR NOT NULL,
                    period VARCHAR NOT NULL,
                    period_type VARCHAR NOT NULL,
                    roe NUMERIC(20, 8),
                    net_income NUMERIC(20, 8),
                    ocf NUMERIC(20, 8),
                    capex NUMERIC(20, 8),
                    ebit NUMERIC(20, 8),
                    interest NUMERIC(20, 8),
                    gross_margin NUMERIC(20, 8),
                    net_margin NUMERIC(20, 8),
                    shares NUMERIC(20, 8),
                    share_change_ma BOOLEAN,
                    bps NUMERIC(20, 8),
                    npl NUMERIC(20, 8),
                    eps NUMERIC(20, 8),
                    PRIMARY KEY (code, period, period_type)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE valuation_history (
                    code VARCHAR NOT NULL,
                    month VARCHAR NOT NULL,
                    trade_date VARCHAR NOT NULL,
                    pe NUMERIC(20, 8),
                    pb NUMERIC(20, 8),
                    price NUMERIC(20, 8),
                    source VARCHAR NOT NULL,
                    ratio_method VARCHAR NOT NULL,
                    fetched_at DATETIME NOT NULL,
                    PRIMARY KEY (code, month)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO valuation_history (
                    code, month, trade_date, pe, pb, price, source,
                    ratio_method, fetched_at
                ) VALUES (
                    '600519.SH', '2026-08', '2026-08-31', 19.2, 7.1, 1460,
                    'eastmoney:RPT_VALUEANALYSIS_DET', 'PE_TTM/PB_MRQ',
                    '2026-09-16 08:00:00'
                )
                """
            )
        )
    old_engine.dispose()

    engine = init_db(db_path)
    schema = inspect(engine)
    assert set(NEW_FINANCIAL_VALUES) <= {
        column["name"] for column in schema.get_columns("financials")
    }
    assert {
        "valuation_series",
        "valuation_history",
        "discovery_data_fills",
        "discovery_data_checkpoints",
    } <= set(schema.get_table_names())
    assert {column["name"] for column in schema.get_columns("valuation_history")} == {
        "code",
        "month",
        "trade_date",
        "pe",
        "pb",
        "price",
        "source",
        "ratio_method",
        "fetched_at",
    }
    valuation_fks = schema.get_foreign_keys("valuation_history")
    assert any(
        fk["referred_table"] == "valuation_series"
        and fk["constrained_columns"] == ["code", "ratio_method"]
        for fk in valuation_fks
    )
    financial_types = {
        column["name"]: str(column["type"])
        for column in schema.get_columns("financials")
    }
    for field in (
        "revenue",
        "total_assets",
        "net_assets",
        "cash",
        "interest_debt",
        "pretax_income",
        "income_tax",
        "goodwill",
    ):
        assert financial_types[field] == "TEXT"
    checkpoint_unique = {
        tuple(item["column_names"])
        for item in schema.get_unique_constraints("discovery_data_checkpoints")
    }
    checkpoint_pk = tuple(
        schema.get_pk_constraint("discovery_data_checkpoints")["constrained_columns"]
    )
    assert ("fill_id", "code") in checkpoint_unique or checkpoint_pk == (
        "fill_id",
        "code",
    )
    assert any(
        fk["referred_table"] == "discovery_data_fills"
        for fk in schema.get_foreign_keys("discovery_data_checkpoints")
    )
    from app.models import ValuationSeries

    with Session(engine) as session:
        series = session.get(ValuationSeries, "600519.SH")
        assert series is not None
        assert series.ratio_method == "PE_TTM/PB_MRQ"
    engine.dispose()


def test_financial_new_fields_round_trip_through_db_and_snapshot(tmp_path) -> None:
    from jobs.cheap import _rows_from_db
    from jobs.discovery_data import persist_discovery_financials
    from screen.replay import _to_financial, dump_snapshot

    engine = init_db(tmp_path / "round-trip.db")
    row = FinancialRow(
        code="000333.SZ",
        period="2025",
        period_type="annual",
        **NEW_FINANCIAL_VALUES,
    )
    with Session(engine) as session:
        persist_discovery_financials(session, [row])
        session.commit()
        loaded = _rows_from_db(session, [row.code])[0]
    for field, expected in NEW_FINANCIAL_VALUES.items():
        assert getattr(loaded, field) == expected

    frozen = dump_snapshot([], [loaded], {}, set())["financials"][0]
    replayed = _to_financial(frozen)
    for field, expected in NEW_FINANCIAL_VALUES.items():
        assert getattr(replayed, field) == expected
    legacy = _to_financial(
        {"code": row.code, "period": row.period, "period_type": "annual"}
    )
    assert all(getattr(legacy, field) is None for field in NEW_FINANCIAL_VALUES)
    engine.dispose()


def test_legacy_then_discovery_updates_only_owned_financial_fields(tmp_path) -> None:
    from jobs.discovery_data import persist_discovery_financials
    from jobs.funnel import persist_financials

    engine = init_db(tmp_path / "legacy-discovery.db")
    legacy = FinancialRow(
        code="000333.SZ",
        period="2025",
        period_type="annual",
        roe=Decimal("15"),
        net_income=Decimal("100"),
        revenue=Decimal("999"),
    )
    discovery = FinancialRow(
        code=legacy.code,
        period=legacy.period,
        period_type=legacy.period_type,
        roe=None,
        net_income=Decimal("101"),
        revenue=Decimal("1000.01"),
        government_grants=Decimal("5"),
    )
    with Session(engine) as session:
        persist_financials(session, [legacy])
        session.commit()
        stored = session.get(
            Financial, (legacy.code, legacy.period, legacy.period_type)
        )
        assert stored is not None
        assert stored.revenue is None

        persist_discovery_financials(session, [discovery])
        session.commit()
        session.refresh(stored)
        assert stored.roe == Decimal("15")
        assert stored.net_income == Decimal("101")
        assert stored.revenue == Decimal("1000.01")
        assert stored.government_grants == Decimal("5")

        persist_discovery_financials(
            session,
            [replace(discovery, government_grants=None)],
        )
        session.commit()
        session.refresh(stored)
        assert stored.government_grants is None
    engine.dispose()


def test_discovery_then_legacy_preserves_discovery_financial_fields(tmp_path) -> None:
    from jobs.discovery_data import persist_discovery_financials
    from jobs.funnel import persist_financials

    engine = init_db(tmp_path / "discovery-legacy.db")
    discovery = FinancialRow(
        code="600036.SH",
        period="2025",
        period_type="annual",
        roe=Decimal("99"),
        net_income=Decimal("100"),
        revenue=Decimal("337532000000.12"),
        government_grants=Decimal("5"),
    )
    legacy = FinancialRow(
        code=discovery.code,
        period=discovery.period,
        period_type=discovery.period_type,
        roe=Decimal("18"),
        net_income=Decimal("102"),
        revenue=Decimal("999"),
    )
    with Session(engine) as session:
        persist_discovery_financials(session, [discovery])
        session.commit()
        persist_financials(session, [legacy])
        session.commit()
        stored = session.get(
            Financial, (legacy.code, legacy.period, legacy.period_type)
        )
        assert stored is not None
        assert stored.roe == Decimal("18")
        assert stored.net_income == Decimal("102")
        assert stored.revenue == Decimal("337532000000.12")
        assert stored.government_grants == Decimal("5")
    engine.dispose()


def test_large_cny_amount_round_trips_as_exact_text(tmp_path) -> None:
    from jobs.discovery_data import persist_discovery_financials

    engine = init_db(tmp_path / "large-money.db")
    amount = Decimal("45900000000000.12")
    row = FinancialRow(
        code="600036.SH",
        period="2025",
        period_type="annual",
        total_assets=amount,
    )
    with Session(engine) as session:
        persist_discovery_financials(session, [row])
        session.commit()
        stored = session.get(Financial, (row.code, row.period, row.period_type))
        assert stored is not None
        assert stored.total_assets == amount
    with engine.connect() as conn:
        storage_type, raw = conn.execute(
            text(
                "SELECT typeof(total_assets), total_assets FROM financials "
                "WHERE code = :code"
            ),
            {"code": row.code},
        ).one()
        assert storage_type == "text"
        assert raw == "45900000000000.12"
    engine.dispose()


def test_decimal_text_accepts_legacy_numeric_reads() -> None:
    from app.models import CanonicalDecimalText

    converter = CanonicalDecimalText()
    assert converter.process_result_value(12345.5, None) == Decimal("12345.5")


def test_init_db_converts_interim_discovery_money_columns_to_text(tmp_path) -> None:
    db_path = tmp_path / "numeric-money.db"
    old_engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    with old_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE financials (
                    code VARCHAR NOT NULL,
                    period VARCHAR NOT NULL,
                    period_type VARCHAR NOT NULL,
                    roe NUMERIC(20, 8),
                    net_income NUMERIC(20, 8),
                    ocf NUMERIC(20, 8),
                    capex NUMERIC(20, 8),
                    ebit NUMERIC(20, 8),
                    interest NUMERIC(20, 8),
                    gross_margin NUMERIC(20, 8),
                    net_margin NUMERIC(20, 8),
                    shares NUMERIC(20, 8),
                    share_change_ma BOOLEAN,
                    bps NUMERIC(20, 8),
                    npl NUMERIC(20, 8),
                    eps NUMERIC(20, 8),
                    revenue NUMERIC(20, 8),
                    PRIMARY KEY (code, period, period_type)
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO financials (code, period, period_type, revenue) "
                "VALUES ('600519.SH', '2025', 'annual', 12345.5)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE settings ("
                "key VARCHAR NOT NULL PRIMARY KEY, value VARCHAR NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO settings (key, value) "
                "VALUES ('fin_fetch_version', '3')"
            )
        )
    old_engine.dispose()

    engine = init_db(db_path)
    with engine.connect() as conn:
        columns = {
            row[1]: row[2]
            for row in conn.exec_driver_sql("PRAGMA table_info(financials)")
        }
    assert columns["revenue"] == "TEXT"
    with Session(engine) as session:
        stored = session.get(Financial, ("600519.SH", "2025", "annual"))
        assert stored is not None
        assert stored.revenue == Decimal("12345.5")
    engine.dispose()


def test_discovery_operational_models_round_trip(tmp_path) -> None:
    engine = init_db(tmp_path / "operations.db")
    plus_eight = timezone(timedelta(hours=8))
    now = datetime(2026, 9, 16, 20, tzinfo=plus_eight)
    retry = now + timedelta(hours=2)
    naive_updated = datetime(2026, 9, 16, 13)
    with Session(engine) as session:
        fill = DiscoveryDataFill(
            status="running",
            total=2,
            completed=1,
            failed=0,
            request_count=7,
            daily_budget=1000,
            budget_day="2026-09-16",
            daily_requests=7,
            started_at=now,
            updated_at=naive_updated,
            last_error=None,
        )
        session.add(fill)
        session.flush()
        checkpoint = DiscoveryDataCheckpoint(
            fill_id=fill.id,
            code="600519.SH",
            status="done",
            attempts=1,
            next_retry_at=retry,
            valuation_points=61,
            error=None,
            updated_at=now,
        )
        session.add(checkpoint)
        session.commit()
        session.expire_all()
        loaded_fill = session.get(DiscoveryDataFill, fill.id)
        loaded = session.get(DiscoveryDataCheckpoint, (fill.id, "600519.SH"))
        assert loaded_fill is not None
        assert loaded is not None
        assert loaded.valuation_points == 61
        assert loaded_fill.started_at == now.astimezone(timezone.utc)
        assert loaded_fill.updated_at == naive_updated.replace(tzinfo=timezone.utc)
        assert loaded.next_retry_at == retry.astimezone(timezone.utc)
        assert loaded.next_retry_at > loaded_fill.started_at
    engine.dispose()


def test_sqlite_foreign_keys_reject_orphans_and_cascade_checkpoints(tmp_path) -> None:
    engine = init_db(tmp_path / "foreign-keys.db")
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    with Session(engine) as session:
        session.add(
            DiscoveryDataCheckpoint(
                fill_id=999,
                code="600519.SH",
                status="pending",
                attempts=0,
                next_retry_at=None,
                valuation_points=0,
                error=None,
                updated_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        fill = DiscoveryDataFill(
            status="running",
            total=1,
            completed=0,
            failed=0,
            request_count=0,
            daily_budget=100,
            budget_day="2026-09-16",
            daily_requests=0,
            started_at=now,
            updated_at=now,
            last_error=None,
        )
        session.add(fill)
        session.flush()
        key = (fill.id, "600519.SH")
        session.add(
            DiscoveryDataCheckpoint(
                fill_id=fill.id,
                code=key[1],
                status="pending",
                attempts=0,
                next_retry_at=None,
                valuation_points=0,
                error=None,
                updated_at=now,
            )
        )
        session.commit()
        session.delete(fill)
        session.commit()
        assert session.get(DiscoveryDataCheckpoint, key) is None
    engine.dispose()


def test_persist_valuation_history_updates_same_method(tmp_path) -> None:
    from jobs.discovery_data import persist_valuation_history

    engine = init_db(tmp_path / "valuation.db")
    first_fetch = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)
    second_fetch = datetime(
        2026, 9, 16, 17, tzinfo=timezone(timedelta(hours=8))
    )
    first = ValuationPoint(
        code="600519.SH",
        month="2026-08",
        trade_date="2026-08-30",
        pe=Decimal("20"),
        pb=Decimal("7"),
        price=Decimal("1450"),
        source="eastmoney:RPT_VALUEANALYSIS_DET",
        ratio_method="PE_TTM/PB_MRQ",
    )
    updated = replace(
        first,
        trade_date="2026-08-31",
        pe=Decimal("19.2"),
        pb=Decimal("7.1"),
        price=Decimal("1460"),
    )
    with Session(engine) as session:
        persist_valuation_history(session, [first], fetched_at=first_fetch)
        session.commit()
        persist_valuation_history(session, [updated], fetched_at=second_fetch)
        session.commit()
        stored = session.get(ValuationHistory, (first.code, first.month))
        assert stored is not None
        assert (
            stored.trade_date,
            stored.pe,
            stored.pb,
            stored.price,
            stored.source,
            stored.ratio_method,
        ) == (
            updated.trade_date,
            updated.pe,
            updated.pb,
            updated.price,
            updated.source,
            updated.ratio_method,
        )
        assert stored.fetched_at == second_fetch.astimezone(timezone.utc)
    engine.dispose()


def test_persist_valuation_history_rejects_mixed_method_without_partial_write(
    tmp_path,
) -> None:
    from jobs.discovery_data import persist_valuation_history

    engine = init_db(tmp_path / "valuation-mixed.db")
    now = datetime.now(timezone.utc)
    existing = ValuationPoint(
        code="600519.SH",
        month="2026-08",
        trade_date="2026-08-31",
        pe=Decimal("19.2"),
        pb=Decimal("7.1"),
        price=Decimal("1460"),
        ratio_method="PE_TTM/PB_MRQ",
    )
    extra = replace(existing, code="000333.SZ")
    mixed = replace(
        existing,
        month="2026-07",
        trade_date="2026-07-31",
        ratio_method="PE_STATIC/PB_LF",
    )
    with Session(engine) as session:
        persist_valuation_history(session, [existing], fetched_at=now)
        session.commit()
        with pytest.raises(ValueError, match="ratio_method"):
            persist_valuation_history(session, [extra, mixed], fetched_at=now)
        session.rollback()
        assert session.get(ValuationHistory, (extra.code, extra.month)) is None
        stored = session.get(ValuationHistory, (existing.code, existing.month))
        assert stored is not None
        assert stored.ratio_method == existing.ratio_method
    engine.dispose()


def test_ratio_method_claim_is_atomic_across_sessions(tmp_path) -> None:
    from jobs.discovery_data import persist_valuation_history
    from app.models import ValuationSeries

    engine = init_db(tmp_path / "valuation-concurrent.db")
    now = datetime.now(timezone.utc)
    first = ValuationPoint(
        code="600519.SH",
        month="2026-08",
        trade_date="2026-08-31",
        pe=Decimal("19.2"),
        pb=Decimal("7.1"),
        ratio_method="PE_TTM/PB_MRQ",
    )
    different = replace(
        first,
        month="2026-07",
        trade_date="2026-07-31",
        ratio_method="PE_STATIC/PB_LF",
    )
    with Session(engine) as winner, Session(engine) as loser:
        assert loser.get(ValuationSeries, first.code) is None
        persist_valuation_history(winner, [first], fetched_at=now)
        winner.commit()
        with pytest.raises(ValueError, match="ratio_method"):
            persist_valuation_history(loser, [different], fetched_at=now)
        loser.rollback()
    with Session(engine) as session:
        session.add(
            ValuationHistory(
                code=first.code,
                month="2026-06",
                trade_date="2026-06-30",
                pe=Decimal("18"),
                pb=Decimal("6"),
                price=None,
                source=first.source,
                ratio_method=different.ratio_method,
                fetched_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    engine.dispose()


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
    assert revenue.financial_sector_substitute == "TOTALOPERATEREVE"
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

        def _discovery_financials_for(
            self, _code: str, *, industry: str | None = None
        ) -> list[FinancialRow]:
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


def test_bank_discovery_bundle_skips_rejected_industrial_details() -> None:
    seen: list[str] = []
    state = SecurityState(
        code="600036.SH",
        name="招商银行",
        exchange="SH",
        listing_date="2002-04-09",
        is_st=False,
        is_delisting=False,
        trading_status="trading",
        last_trade_date="2026-09-16",
        industry="银行Ⅱ",
        industry_version="eastmoney:push2delay:clist:f100:v1",
        industry_as_of="2026-09-16",
        source="eastmoney:stock/get:f57,f58,f107,f127,f189,f292",
    )

    class Stub(EastMoneyClient):
        def _security_state_for(self, _code: str) -> SecurityState:
            return state

        def _get_json(self, url: str) -> dict:
            seen.append(url)
            self._request_count += 1
            if "MAINFINADATA" in url:
                return {
                    "success": True,
                    "result": {
                        "data": [
                            {
                                "REPORT_TYPE": "年报",
                                "REPORT_YEAR": "2025",
                                "NOTICE_DATE": "2026-03-28",
                                "TOTALOPERATEREVE": "337532000000",
                                "PARENTNETPROFIT": "150181000000",
                                "KCFJCXSYJLR": "150007000000",
                                "TOTAL_ASSETS_PK": "13070523000000",
                                "TOTAL_EQUITY_PK": "1280899000000",
                                "NETCASH_OPERATE_PK": "451457000000",
                                "NEWCAPITALADER": "18.24",
                                "HXYJBCZL": "14.16",
                                "NONPERLOAN": "0.94",
                                "NON_PERFORMING_LOAN": "68206000000",
                            }
                        ]
                    },
                }
            if "RPT_F10_FINANCE_GINCOME" in url:
                return {
                    "success": True,
                    "result": {
                        "data": [
                            {
                                "REPORT_DATE": "2025-12-31",
                                "REPORT_TYPE": "年报",
                                "NOTICE_DATE": "2026-03-28",
                                "CREDIT_IMPAIRMENT_LOSS": "39586000000",
                            }
                        ]
                    },
                }
            if (
                "RPT_F10_FINANCE_GBALANCE" in url
                or "RPT_F10_FINANCE_GCASHFLOW" in url
            ):
                return {
                    "success": False,
                    "message": "report inapplicable to bank",
                }
            return {"success": True, "result": {"data": []}}

        def fetch_valuation_history(self, _code: str) -> list[ValuationPoint]:
            return []

    result = Stub().fetch_discovery_data("600036.SH")
    row = result.financial_rows[0]
    assert row.revenue == Decimal("337532000000")
    assert row.total_assets == Decimal("13070523000000")
    assert row.capital_adequacy == Decimal("18.24")
    assert row.core_tier1_capital_adequacy == Decimal("14.16")
    assert row.npl == Decimal("0.94")
    assert row.impairment_loss == Decimal("39586000000")
    assert row.short_loan is None
    assert row.goodwill is None
    assert row.acquisition_cash_paid is None
    assert row.roic is None
    assert not any("RPT_F10_FINANCE_GBALANCE" in url for url in seen)
    assert not any("RPT_F10_FINANCE_GCASHFLOW" in url for url in seen)


def test_bank_applicable_income_detail_failure_is_typed() -> None:
    state = SecurityState(
        code="600036.SH",
        name="招商银行",
        exchange="SH",
        listing_date="2002-04-09",
        is_st=False,
        is_delisting=False,
        trading_status="trading",
        last_trade_date="2026-09-16",
        industry="银行Ⅱ",
        industry_version="eastmoney:push2delay:clist:f100:v1",
        industry_as_of="2026-09-16",
        source="eastmoney:stock/get:f57,f58,f107,f127,f189,f292",
    )

    class Stub(EastMoneyClient):
        def _security_state_for(self, _code: str) -> SecurityState:
            return state

        def _get_json(self, url: str) -> dict:
            if "MAINFINADATA" in url:
                return {
                    "success": True,
                    "result": {
                        "data": [
                            {
                                "REPORT_TYPE": "年报",
                                "REPORT_YEAR": "2025",
                                "NEWCAPITALADER": "18.24",
                            }
                        ]
                    },
                }
            if "RPT_F10_FINANCE_GINCOME" in url:
                return {
                    "success": False,
                    "message": "bank impairment source unavailable",
                }
            return {"success": True, "result": {"data": []}}

        def fetch_valuation_history(self, _code: str) -> list[ValuationPoint]:
            raise AssertionError("failed bank source must stop the bundle")

    with pytest.raises(
        DiscoveryCompanyError, match="bank impairment source unavailable"
    ) as caught:
        Stub().fetch_discovery_data("600036.SH")
    assert caught.value.stage == "financials"


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


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.current = now
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def _security(code: str) -> SecurityState:
    return SecurityState(
        code=code,
        name=code,
        exchange=code[-2:],
        listing_date="2001-01-01",
        is_st=False,
        is_delisting=False,
        trading_status="trading",
        last_trade_date="2026-09-16",
        industry="测试",
        industry_version="test:v1",
        industry_as_of="2026-09-16",
        source="fake",
    )


def _bundle(
    code: str,
    *,
    request_count: int = 1,
    valuation_points: tuple[ValuationPoint, ...] = (),
) -> DiscoveryCompanyData:
    return DiscoveryCompanyData(
        code=code,
        security_state=_security(code),
        financial_rows=(
            FinancialRow(code=code, period="2025", period_type="annual"),
        ),
        valuation_points=valuation_points,
        request_count=request_count,
        source_as_of=DiscoverySourceAsOf(
            security="2026-09-16",
            valuation=None,
            financial="2026-03-31",
        ),
    )


def _fill(
    session: Session,
    codes: list[str],
    *,
    now: datetime,
    status: str = "running",
    daily_budget: int = 100,
) -> DiscoveryDataFill:
    fill = DiscoveryDataFill(
        status=status,
        total=len(codes),
        completed=0,
        failed=0,
        request_count=0,
        daily_budget=daily_budget,
        budget_day=now.date().isoformat(),
        daily_requests=0,
        started_at=now,
        updated_at=now,
        last_error=None,
    )
    session.add(fill)
    session.flush()
    session.add_all(
        [
            DiscoveryDataCheckpoint(
                fill_id=fill.id,
                code=code,
                status="pending",
                attempts=0,
                next_retry_at=None,
                valuation_points=0,
                error=None,
                updated_at=now,
            )
            for code in codes
        ]
    )
    session.commit()
    return fill


def test_discovery_fill_commits_success_before_later_company_failure(tmp_path) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "fill-commit.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    first, second = "000001.SZ", "000002.SZ"
    with factory() as session:
        fill_id = _fill(session, [first, second], now=now).id
    market = FakeMarketClient(
        discovery_data={first: _bundle(first)},
        discovery_errors={second: "statement unavailable"},
        discovery_request_counts={second: 2},
    )
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=1,
        daily_budget=100,
        max_runtime_seconds=60,
        company_request_reservation=3,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=FakeClock(now)
        )
    with factory() as session:
        assert session.get(Financial, (first, "2025", "annual")) is not None
        done = session.get(DiscoveryDataCheckpoint, (fill_id, first))
        failed = session.get(DiscoveryDataCheckpoint, (fill_id, second))
        fill = session.get(DiscoveryDataFill, fill_id)
        assert done is not None and done.status == "done"
        assert failed is not None and failed.status == "failed"
        assert failed.attempts == 1
        assert failed.error and "statement unavailable" in failed.error
        assert fill is not None
        assert (fill.status, fill.completed, fill.failed, fill.request_count) == (
            "done",
            1,
            1,
            3,
        )
    engine.dispose()


def test_discovery_fill_resume_skips_completed_and_retries_due_checkpoint(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "fill-resume.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    done_code, retry_code, pending_code = "000001.SZ", "000002.SZ", "000003.SZ"
    with factory() as session:
        fill = _fill(
            session,
            [done_code, retry_code, pending_code],
            now=now,
            status="paused",
        )
        fill_id = fill.id
        done = session.get(DiscoveryDataCheckpoint, (fill_id, done_code))
        retry = session.get(DiscoveryDataCheckpoint, (fill_id, retry_code))
        assert done is not None and retry is not None
        done.status = "done"
        done.attempts = 1
        retry.status = "retryable"
        retry.attempts = 1
        retry.next_retry_at = now - timedelta(seconds=1)
        fill.completed = 1
        session.commit()
    market = FakeMarketClient(
        discovery_data={
            retry_code: _bundle(retry_code),
            pending_code: _bundle(pending_code),
        }
    )
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=3,
        daily_budget=100,
        max_runtime_seconds=60,
        company_request_reservation=2,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=FakeClock(now)
        )
    assert [call for call in market.calls if call[0] == "discovery_data"] == [
        ("discovery_data", retry_code),
        ("discovery_data", pending_code),
    ]
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert (fill.status, fill.completed, fill.failed) == ("done", 3, 0)
    engine.dispose()


def test_discovery_fill_throttles_and_stops_on_durable_daily_budget(
    tmp_path,
) -> None:
    from jobs.discovery_data import (
        RequestPolicy,
        discovery_data_status,
        execute_discovery_data_fill,
    )

    engine = init_db(tmp_path / "fill-budget.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    codes = ["000001.SZ", "000002.SZ", "000003.SZ"]
    with factory() as session:
        fill_id = _fill(session, codes, now=now, daily_budget=4).id
    market = FakeMarketClient(
        discovery_data={code: _bundle(code, request_count=2) for code in codes}
    )
    clock = FakeClock(now)
    policy = RequestPolicy(
        requests_per_second=2,
        max_attempts=2,
        daily_budget=4,
        max_runtime_seconds=60,
        company_request_reservation=2,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=clock
        )
    assert clock.sleeps == [1.0]
    assert [call[1] for call in market.calls if call[0] == "discovery_data"] == codes[:2]
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        untouched = session.get(DiscoveryDataCheckpoint, (fill_id, codes[2]))
        assert fill is not None and untouched is not None
        assert (fill.status, fill.daily_requests, fill.completed) == ("paused", 4, 2)
        assert untouched.status == "pending"
        assert discovery_data_status(
            session, policy=policy, now=clock.now()
        )["eta_seconds"] == 1

    # Restart on the same local-policy day cannot reset persisted usage.
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=clock
        )
    assert [call[1] for call in market.calls if call[0] == "discovery_data"] == codes[:2]

    # Only a local-policy day change resets the durable daily counter.
    clock.advance(days=1)
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=clock
        )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert (fill.status, fill.daily_requests, fill.completed) == ("done", 2, 3)
    engine.dispose()


def test_discovery_failure_retries_to_cap_and_status_eta_needs_two_samples(
    tmp_path,
) -> None:
    from jobs.discovery_data import (
        RequestPolicy,
        discovery_data_status,
        execute_discovery_data_fill,
    )

    engine = init_db(tmp_path / "fill-retry.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill_id = _fill(session, [code], now=now).id
    market = FakeMarketClient(
        discovery_errors={code: "temporary"},
        discovery_request_counts={code: 1},
    )
    clock = FakeClock(now)
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=2,
        daily_budget=100,
        max_runtime_seconds=60,
        retry_delay_seconds=10,
        company_request_reservation=2,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=clock
        )
        status = discovery_data_status(session, policy=policy, now=clock.now())
        assert status["status"] == "paused"
        assert status["retryable"] == 1
        assert status["failed"] == 0
        assert status["eta_seconds"] is None
        assert status["requests_per_second"] == 100
        assert status["max_attempts"] == 2
        assert status["daily_budget"] == 100
        assert status["max_runtime_seconds"] == 60
    clock.advance(seconds=10)
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=clock
        )
    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        fill = session.get(DiscoveryDataFill, fill_id)
        assert checkpoint is not None and fill is not None
        assert (checkpoint.status, checkpoint.attempts) == ("failed", 2)
        assert checkpoint.next_retry_at is None
        assert fill.status == "done"
        assert fill.last_error and "temporary" in fill.last_error
    engine.dispose()


def test_discovery_empty_bundle_is_success_and_max_runtime_pauses_next(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "fill-runtime.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    first, second = "000001.SZ", "000002.SZ"
    with factory() as session:
        fill_id = _fill(session, [first, second], now=now).id
    clock = FakeClock(now)
    empty = replace(
        _bundle(first, request_count=1),
        financial_rows=(),
        valuation_points=(),
    )

    class SlowFake(FakeMarketClient):
        def fetch_discovery_data(self, code: str) -> DiscoveryCompanyData:
            result = super().fetch_discovery_data(code)
            clock.advance(seconds=61)
            return result

    market = SlowFake(
        discovery_data={
            first: empty,
            second: _bundle(second),
        }
    )
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=2,
        daily_budget=100,
        max_runtime_seconds=60,
        company_request_reservation=2,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=clock
        )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        empty_checkpoint = session.get(
            DiscoveryDataCheckpoint, (fill_id, first)
        )
        pending = session.get(DiscoveryDataCheckpoint, (fill_id, second))
        assert fill is not None and empty_checkpoint is not None and pending is not None
        assert (fill.status, fill.completed, fill.failed) == ("paused", 1, 0)
        assert empty_checkpoint.status == "done"
        assert empty_checkpoint.valuation_points == 0
        assert pending.status == "pending"
    assert [call[1] for call in market.calls if call[0] == "discovery_data"] == [
        first
    ]
    engine.dispose()


def _discovery_client(
    tmp_path: Path,
    market: FakeMarketClient,
    *,
    allow_sync: bool = True,
) -> TestClient:
    from market.quotes import FakeQuoteClient

    return TestClient(
        create_app(
            db_path=tmp_path / "app.db",
            env_path=tmp_path / ".env",
            quote_client=FakeQuoteClient(),
            market_client=market,
            allow_sync=allow_sync,
        )
    )


def test_discovery_data_api_start_status_and_get_is_read_only(tmp_path) -> None:
    from market.cheap import CheapRow

    code = "000001.SZ"
    market = FakeMarketClient(
        cheap_universe=[
            CheapRow(
                code=code,
                name="平安银行",
                price=Decimal("10"),
                pe=Decimal("5"),
                pb=Decimal("1"),
                mcap=Decimal("100"),
                industry="银行",
                exchange="SZ",
            )
        ],
        discovery_data={code: _bundle(code)},
    )
    with _discovery_client(tmp_path, market) as client:
        started = client.post("/api/discovery-data/fill", json={"sync": True})
        assert started.status_code == 200
        assert started.json()["status"] == "done"
        assert started.json()["type"] == "discovery_data"
        before = list(market.calls)
        status = client.get("/api/discovery-data/status")
        assert status.status_code == 200
        assert status.json()["completed"] == 1
        assert status.json()["eta_seconds"] is None
        assert client.get("/api/health").json()["lock"] is None
        assert market.calls == before


def test_discovery_data_duplicate_start_is_idempotent_and_lock_conflicts(
    tmp_path,
) -> None:
    from market.cheap import CheapRow

    code = "000001.SZ"
    gate = Event()
    market = FakeMarketClient(
        cheap_universe=[
            CheapRow(
                code=code,
                name="平安银行",
                price=Decimal("10"),
                pe=Decimal("5"),
                pb=Decimal("1"),
                mcap=Decimal("100"),
                industry="银行",
                exchange="SZ",
            )
        ],
        discovery_data={code: _bundle(code)},
        discovery_gate=gate,
    )
    db_path = tmp_path / "app.db"
    with _discovery_client(tmp_path, market, allow_sync=False) as client:
        first = client.post("/api/discovery-data/fill", json={})
        assert first.status_code == 200
        second = client.post("/api/discovery-data/fill", json={})
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        with client.app.state.session_factory() as session:
            assert session.query(DiscoveryDataFill).count() == 1
        gate.set()
        for _ in range(100):
            if client.get("/api/discovery-data/status").json()["status"] == "done":
                break
            time.sleep(0.01)
        assert client.get("/api/discovery-data/status").json()["status"] == "done"

    other_market = FakeMarketClient()
    with _discovery_client(tmp_path, other_market) as client:
        with client.app.state.session_factory() as session:
            lock = session.get(JobLock, 1)
            assert lock is not None
            lock.job_id = 77
            lock.job_type = "review"
            lock.held_at = datetime.now(timezone.utc)
            session.commit()
        blocked = client.post("/api/discovery-data/fill", json={"sync": True})
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "JOB_LOCKED"
        assert blocked.json()["job_type"] == "review"


def test_startup_pauses_interrupted_discovery_fill_and_preserves_done(
    tmp_path,
) -> None:
    db_path = tmp_path / "app.db"
    engine = init_db(db_path)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        fill = _fill(
            session, ["000001.SZ", "000002.SZ"], now=now, status="running"
        )
        running = session.get(
            DiscoveryDataCheckpoint, (fill.id, "000001.SZ")
        )
        done = session.get(DiscoveryDataCheckpoint, (fill.id, "000002.SZ"))
        assert running is not None and done is not None
        running.status = "running"
        done.status = "done"
        fill.completed = 1
        lock = session.get(JobLock, 1)
        if lock is None:
            lock = JobLock(
                id=1,
                job_id=None,
                job_type=None,
                held_at=None,
            )
            session.add(lock)
        lock.job_id = fill.id
        lock.job_type = "discovery_data"
        lock.held_at = now
        session.commit()
        fill_id = fill.id
    engine.dispose()

    market = FakeMarketClient(
        discovery_data={"000001.SZ": _bundle("000001.SZ")}
    )
    with _discovery_client(tmp_path, market) as client:
        status = client.get("/api/discovery-data/status").json()
        assert status["id"] == fill_id
        assert status["status"] == "paused"
        assert status["completed"] == 1
        with client.app.state.session_factory() as session:
            pending = session.get(
                DiscoveryDataCheckpoint, (fill_id, "000001.SZ")
            )
            done = session.get(
                DiscoveryDataCheckpoint, (fill_id, "000002.SZ")
            )
            assert pending is not None and pending.status == "pending"
            assert done is not None and done.status == "done"
            assert session.get(JobLock, 1).job_id is None
        resumed = client.post(
            "/api/discovery-data/fill", json={"sync": True}
        ).json()
        assert (resumed["status"], resumed["completed"]) == ("done", 2)
        assert [
            call for call in market.calls if call[0] == "discovery_data"
        ] == [("discovery_data", "000001.SZ")]
