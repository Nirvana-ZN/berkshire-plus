import json
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import create_session_factory, init_db
from app.main import create_app
from discovery.company_payload import SCHEMA_VERSION
from app.models import (
    DiscoveryDataCheckpoint,
    DiscoveryDataFill,
    DiscoveryFillTradingDate,
    DiscoveryUniverseSecurity,
    Financial,
    FinancialFetchCompleteness,
    FinancialFieldProvenance,
    JobLock,
    Setting,
    ValuationHistory,
)
from market.universe import (
    DELISTING_NAME_METHOD,
    UniverseRow,
    is_st_name,
    security_state_eligible,
)
from market.eastmoney import BOARD_LIST_URL, EastMoneyClient, FakeMarketClient
from market.types import (
    DiscoveryCompanyData,
    DiscoveryCompanyError,
    DiscoveryRequestBudgetExhausted,
    DiscoveryRequestRuntimeExhausted,
    DiscoverySourceAsOf,
    FinancialRow,
    MarketError,
    SecurityState,
    TransientMarketError,
    ValuationPoint,
)

NEW_FINANCIAL_VALUES = {
    "interest_coverage": Decimal("21.533919468562"),
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
    "capital_adequacy": Decimal("14.16"),
    "core_tier1_capital_adequacy": Decimal("12.17"),
    "nonperforming_loan_balance": Decimal("9.18"),
    "impairment_loss": Decimal("8.19"),
    "other_income": Decimal("6.20"),
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
        "discovery_fill_trading_dates",
        "discovery_universe_securities",
        "financial_fetch_completeness",
        "financial_field_provenance",
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
        "net_income",
        "ocf",
        "capex",
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
    assert tuple(
        schema.get_pk_constraint("financial_field_provenance")[
            "constrained_columns"
        ]
    ) == ("code", "period", "period_type", "field", "source")
    from app.models import ValuationSeries

    with Session(engine) as session:
        series = session.get(ValuationSeries, "600519.SH")
        assert series is not None
        assert series.ratio_method == "PE_TTM/PB_MRQ"
    engine.dispose()


def test_financial_new_fields_round_trip_through_db_and_snapshot(tmp_path) -> None:
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
        loaded = session.get(Financial, (row.code, row.period, row.period_type))
        coverage_source = session.get(
            FinancialFieldProvenance,
            (row.code, row.period, row.period_type, "interest_coverage", "discovery"),
        )
        other_income_source = session.get(
            FinancialFieldProvenance,
            (row.code, row.period, row.period_type, "other_income", "discovery"),
        )
    for field, expected in NEW_FINANCIAL_VALUES.items():
        assert getattr(loaded, field) == expected
    assert coverage_source is not None
    assert (
        coverage_source.source_report,
        coverage_source.source_field,
    ) == ("RPT_F10_FINANCE_MAINFINADATA", "INTSTCOVRATE")
    assert other_income_source is not None
    assert (
        other_income_source.source_report,
        other_income_source.source_field,
    ) == ("RPT_F10_FINANCE_GINCOME", "OTHER_INCOME")

    frozen = dump_snapshot([], [loaded], {}, set())["financials"][0]
    replayed = _to_financial(frozen)
    for field, expected in NEW_FINANCIAL_VALUES.items():
        assert getattr(replayed, field) == expected
    legacy = _to_financial(
        {"code": row.code, "period": row.period, "period_type": "annual"}
    )
    assert all(getattr(legacy, field) is None for field in NEW_FINANCIAL_VALUES)
    engine.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ebit", Decimal("1")),
        ("interest", Decimal("1")),
        ("roic", Decimal("1")),
        ("government_grants", Decimal("1")),
    ],
)
def test_discovery_persistence_rejects_untrusted_financial_fields(
    tmp_path,
    field: str,
    value: Decimal,
) -> None:
    from jobs.discovery_data import persist_discovery_financials

    engine = init_db(tmp_path / f"untrusted-{field}.db")
    row = FinancialRow(
        code="000333.SZ",
        period="2025",
        period_type="annual",
        **{field: value},
    )
    with Session(engine) as session:
        with pytest.raises(ValueError, match=field):
            persist_discovery_financials(session, [row])
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
        other_income=Decimal("5"),
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
        assert stored.other_income == Decimal("5")

        persist_discovery_financials(
            session,
            [
                replace(
                    discovery,
                    net_income=None,
                    other_income=None,
                )
            ],
        )
        session.commit()
        session.refresh(stored)
        assert stored.net_income == Decimal("101")
        assert stored.other_income is None
        assert {
            row.mode
            for row in session.scalars(
                select(FinancialFetchCompleteness).where(
                    FinancialFetchCompleteness.code == legacy.code
                )
            )
        } == {"legacy", "discovery"}
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
        capex=Decimal("7"),
        revenue=Decimal("337532000000.12"),
        other_income=Decimal("5"),
    )
    legacy = FinancialRow(
        code=discovery.code,
        period=discovery.period,
        period_type=discovery.period_type,
        roe=Decimal("18"),
        net_income=Decimal("102"),
        capex=Decimal("9"),
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
        assert stored.roe == Decimal("99")
        assert stored.net_income == Decimal("100")
        assert stored.capex == Decimal("7")
        assert stored.revenue == Decimal("337532000000.12")
        assert stored.other_income == Decimal("5")
        assert session.get(
            FinancialFieldProvenance,
            (
                legacy.code,
                legacy.period,
                legacy.period_type,
                "capex",
                "discovery",
            ),
        ) is not None
        assert {
            row.mode
            for row in session.scalars(
                select(FinancialFetchCompleteness).where(
                    FinancialFetchCompleteness.code == legacy.code
                )
            )
        } == {"legacy", "discovery"}
    engine.dispose()


@pytest.mark.parametrize("strict_first", [False, True])
@pytest.mark.parametrize(
    ("strict_capex", "expected_complete"),
    [(None, 0), (Decimal("7"), 1)],
)
def test_readiness_requires_strict_field_provenance_in_both_write_orders(
    tmp_path,
    strict_first: bool,
    strict_capex: Decimal | None,
    expected_complete: int,
) -> None:
    from discovery.data_readiness import discovery_data_readiness
    from jobs.discovery_data import persist_discovery_financials
    from jobs.funnel import persist_financials

    engine = init_db(
        tmp_path / f"provenance-{strict_first}-{strict_capex}.db"
    )
    code = "000333.SZ"
    legacy = FinancialRow(
        code=code,
        period="2025",
        period_type="annual",
        capex=Decimal("9"),
    )
    strict = FinancialRow(
        code=code,
        period="2025",
        period_type="annual",
        capex=strict_capex,
        source_as_of="2026-03-31",
    )
    with Session(engine) as session:
        _store_readiness_universe(session, [_readiness_row(code)])
        writers = (
            (persist_discovery_financials, [strict]),
            (persist_financials, [legacy]),
        )
        if not strict_first:
            writers = tuple(reversed(writers))
        for writer, rows in writers:
            writer(session, rows)
            session.flush()
        session.commit()

        stored = session.get(Financial, (code, "2025", "annual"))
        assert stored is not None
        assert stored.capex is not None
        if strict_capex is None:
            assert stored.capex == legacy.capex
        else:
            assert stored.capex == strict_capex
        provenance = session.get(
            FinancialFieldProvenance,
            (code, "2025", "annual", "capex", "discovery"),
        )
        assert (provenance is not None) is (strict_capex is not None)
        report = discovery_data_readiness(
            session, as_of=date(2026, 9, 16)
        )
        assert report.tracks["deep_value"].complete == expected_complete
    engine.dispose()


def test_strict_value_and_provenance_rollback_together(tmp_path) -> None:
    from jobs.discovery_data import persist_discovery_financials
    from jobs.funnel import persist_financials

    engine = init_db(tmp_path / "financial-provenance-rollback.db")
    code = "000333.SZ"
    strict = FinancialRow(
        code=code,
        period="2025",
        period_type="annual",
        capex=Decimal("7"),
        source_as_of="2026-03-31",
    )
    with Session(engine) as session:
        persist_financials(
            session,
            [replace(strict, capex=Decimal("9"), source_as_of=None)],
        )
        session.commit()
        persist_discovery_financials(session, [strict])
        session.flush()
        stored = session.get(Financial, (code, "2025", "annual"))
        assert stored is not None and stored.capex == Decimal("7")
        assert session.get(
            FinancialFieldProvenance,
            (code, "2025", "annual", "capex", "discovery"),
        ) is not None
        session.rollback()

    with Session(engine) as session:
        stored = session.get(Financial, (code, "2025", "annual"))
        provenance = session.get(
            FinancialFieldProvenance,
            (code, "2025", "annual", "capex", "discovery"),
        )
        assert stored is not None and stored.capex == Decimal("9")
        assert provenance is None
    engine.dispose()




def test_existing_financial_rows_infer_only_safe_legacy_completeness(tmp_path) -> None:
    db_path = tmp_path / "legacy-completeness.db"
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
                    revenue TEXT,
                    PRIMARY KEY (code, period, period_type)
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO financials "
                "(code, period, period_type, roe, net_income) VALUES "
                "('000001.SZ', '2025', 'annual', 12, 100)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO financials "
                "(code, period, period_type, revenue) VALUES "
                "('600036.SH', '2025', 'annual', '337532000000.12')"
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
                "VALUES ('fin_fetch_version', '4')"
            )
        )
    old_engine.dispose()

    engine = init_db(db_path)
    with Session(engine) as session:
        assert set(
            session.scalars(
                select(FinancialFetchCompleteness.code).where(
                    FinancialFetchCompleteness.mode == "legacy"
                )
            )
        ) == {"000001.SZ"}
    engine.dispose()


def test_large_cny_amount_round_trips_as_exact_text(tmp_path) -> None:
    from jobs.discovery_data import persist_discovery_financials

    engine = init_db(tmp_path / "large-money.db")
    amount = Decimal("45900000000000.123456789012345678")
    row = FinancialRow(
        code="600036.SH",
        period="2025",
        period_type="annual",
        net_income=amount,
        ocf=amount + Decimal("0.000000000000000001"),
        capex=amount + Decimal("0.000000000000000002"),
        total_assets=amount,
    )
    with Session(engine) as session:
        persist_discovery_financials(session, [row])
        session.commit()
        stored = session.get(Financial, (row.code, row.period, row.period_type))
        assert stored is not None
        assert stored.net_income == row.net_income
        assert stored.ocf == row.ocf
        assert stored.capex == row.capex
        assert stored.total_assets == amount
    with engine.connect() as conn:
        storage_types = conn.execute(
            text(
                "SELECT typeof(net_income), typeof(ocf), typeof(capex), "
                "typeof(total_assets), net_income FROM financials "
                "WHERE code = :code"
            ),
            {"code": row.code},
        ).one()
        assert storage_types[:4] == ("text", "text", "text", "text")
        assert storage_types[4] == "45900000000000.123456789012345678"
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
                "INSERT INTO financials ("
                "code, period, period_type, net_income, ocf, capex, revenue"
                ") VALUES ("
                "'600519.SH', '2025', 'annual', "
                "82320067101.68, 61522204989.35, 3127594916.41, 12345.5"
                ")"
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
                "VALUES ('fin_fetch_version', '4')"
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
    assert columns["net_income"] == "TEXT"
    assert columns["ocf"] == "TEXT"
    assert columns["capex"] == "TEXT"
    with Session(engine) as session:
        stored = session.get(Financial, ("600519.SH", "2025", "annual"))
        assert stored is not None
        assert stored.revenue == Decimal("12345.5")
        assert stored.net_income == Decimal("82320067101.68")
        assert stored.ocf == Decimal("61522204989.35")
        assert stored.capex == Decimal("3127594916.41")
    engine.dispose()
    migrated_again = init_db(db_path)
    with migrated_again.connect() as conn:
        assert {
            row[1]: row[2]
            for row in conn.exec_driver_sql("PRAGMA table_info(financials)")
        }["net_income"] == "TEXT"
        assert conn.execute(
            text(
                "SELECT net_income FROM financials "
                "WHERE code = '600519.SH'"
            )
        ).scalar_one() == "82320067101.68"
    migrated_again.dispose()


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
    from app.models import ValuationSeries
    from jobs.discovery_data import persist_valuation_history

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


def test_discovery_prices_stop_at_the_security_snapshot_day() -> None:
    rows = [
        {"TRADE_DATE": f"2026-09-{day} 00:00:00", "PE_TTM": "10", "PB_MRQ": "2",
         "CLOSE_PRICE": price}
        for day, price in (("29", "12"), ("28", "11"))
    ] + [
        {"TRADE_DATE": day, "PE_TTM": "10", "PB_MRQ": "2", "CLOSE_PRICE": "9"}
        for day in reversed(_month_starts())
    ]
    through: list[str | None] = []

    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            return {"success": True, "result": {"pages": 1, "data": rows}}

        def _security_state_for(self, _code: str) -> SecurityState:
            return SecurityState(
                code="600519.SH",
                name="贵州茅台",
                exchange="SH",
                listing_date="2001-08-27",
                is_st=False,
                is_delisting=False,
                trading_status="trading",
                last_trade_date="2026-09-28",
                industry="白酒Ⅱ",
                industry_version="eastmoney:push2delay:clist:f100:v1",
                industry_as_of="2026-09-28",
                source="eastmoney:stock/get:f57,f58,f107,f127,f189,f292",
            )

        def _discovery_financials_for(self, _code: str, **_kwargs) -> list:
            return []

    latest = Stub().fetch_valuation_history("600519.SH", through="2026-09-28")[0]
    assert (latest.trade_date, latest.price) == ("2026-09-28", Decimal("11"))
    assert Stub().fetch_valuation_history("600519.SH")[0].trade_date > "2026-09-28"

    class Recording(Stub):
        def fetch_valuation_history(self, _code: str, **kwargs) -> list:
            through.append(kwargs.get("through"))
            raise MarketError("stop after recording")

    with pytest.raises(DiscoveryCompanyError):
        Recording().fetch_discovery_data("600519.SH")
    assert through == ["2026-09-28"]


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


def test_universe_freezes_fields_and_live_row_is_eligible() -> None:
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

    by_code = {row.code: row for row in Stub().fetch_universe()}
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


def test_priced_suspension_candidates_are_probed_and_request_gated(
    monkeypatch,
) -> None:
    callbacks = 0
    urls: list[str] = []

    def before_request() -> None:
        nonlocal callbacks
        callbacks += 1

    def get(url: str):
        urls.append(url)
        if "clist/get" in url:
            return _HttpResponse(
                {
                    "data": {
                        "diff": [
                            {
                                "f12": code,
                                "f13": 1,
                                "f14": code,
                                "f2": "10",
                                "f9": "5",
                                "f23": "1",
                                "f20": "100",
                                "f26": 20000101,
                                "f100": "测试",
                                "f292": status,
                            }
                            for code, status in (
                                ("600001", 6),
                                ("600002", 6),
                                ("600003", 6),
                                ("600004", 2),
                            )
                        ]
                    }
                }
            )
        if "secid=0.399317" in url:
            return _HttpResponse(
                {
                    "data": {
                        "klines": [
                            "2026-09-19,1,2,3,4,5,6",
                            "2026-09-20,1,2,3,4,5,6",
                        ]
                    }
                }
            )
        if "secid=1.600001" in url:
            return _HttpResponse(
                {"data": {"klines": ["2026-09-19,1,2,3,4,5,6"]}}
            )
        if "secid=1.600002" in url:
            return _HttpResponse(
                {"data": {"klines": ["2026-09-20,1,2,3,4,5,6"]}}
            )
        assert "secid=1.600003" in url
        return _HttpResponse({"data": {"klines": []}})

    _install_http_transport(monkeypatch, get)
    client = EastMoneyClient()
    rows = {
        row.code: row
        for row in client.fetch_universe(
            as_of=date(2026, 9, 20),
            before_request=before_request,
        )
    }

    assert (
        rows["600001.SH"].trading_status,
        rows["600001.SH"].last_trade_date,
    ) == ("suspended", "2026-09-19")
    assert (
        rows["600002.SH"].trading_status,
        rows["600002.SH"].last_trade_date,
    ) == ("trading", "2026-09-20")
    assert (
        rows["600003.SH"].trading_status,
        rows["600003.SH"].last_trade_date,
    ) == (None, None)
    assert (
        rows["600004.SH"].trading_status,
        rows["600004.SH"].last_trade_date,
    ) == ("trading", "2026-09-20")
    assert callbacks == client.request_count == len(urls) == 5


def test_suspended_security_uses_latest_five_frozen_exchange_dates() -> None:
    dates = [
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
    ]
    row = UniverseRow(
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
def test_st_classifier_has_no_false_positives(name: str, expected: bool) -> None:
    assert is_st_name(name) is expected


def test_calendar_failure_fails_the_universe_without_leaking_urls() -> None:
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
            raise RuntimeError("GET https://secret.example/calendar unavailable")

    with pytest.raises(MarketError, match="calendar fetch failed") as caught:
        Stub().fetch_universe()
    assert "https://" not in str(caught.value)




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
    assert FIELD_DICTIONARY["roe"].field == "ROEJQ"
    assert FIELD_DICTIONARY["shares"].field == "TOTAL_SHARE"
    assert FIELD_DICTIONARY["gross_margin"].field == "XSMLL"
    assert FIELD_DICTIONARY["net_margin"].field == "XSJLL"
    coverage = FIELD_DICTIONARY["interest_coverage"]
    assert coverage.field == "INTSTCOVRATE"
    assert coverage.unit == "rate"
    other_income = FIELD_DICTIONARY["other_income"]
    assert other_income.report == "RPT_F10_FINANCE_GINCOME"
    assert other_income.field == "OTHER_INCOME"
    assert "government grants" in other_income.missing_policy
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

        def fetch_valuation_history(self, _code: str, **_kwargs) -> list[ValuationPoint]:
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

        def fetch_valuation_history(self, _code: str, **_kwargs) -> list[ValuationPoint]:
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

        def fetch_valuation_history(self, _code: str, **_kwargs) -> list[ValuationPoint]:
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


class _HttpResponse:
    def __init__(
        self, payload: object | None = None, *, status_code: int = 200
    ) -> None:
        self.payload = {"ok": True} if payload is None else payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


def _install_http_transport(monkeypatch, get) -> None:
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


def test_direct_json_transport_and_decode_failures_are_transient(
    monkeypatch,
) -> None:
    failures = [httpx2.ConnectError("disconnected"), ValueError("invalid json")]

    def get(_url: str):
        failure = failures.pop(0)
        if isinstance(failure, httpx2.TransportError):
            raise failure

        class InvalidJsonResponse(_HttpResponse):
            def json(self) -> object:
                raise failure

        return InvalidJsonResponse()

    _install_http_transport(monkeypatch, get)
    client = EastMoneyClient()
    for expected in ("ConnectError", "ValueError"):
        with pytest.raises(TransientMarketError, match=expected):
            client._get_json_direct("https://example.test/private-path")
    assert client.request_count == 2


def test_direct_json_valid_nonobject_is_permanent_and_not_retried(
    monkeypatch,
) -> None:
    calls = 0

    def get(_url: str):
        nonlocal calls
        calls += 1

        class NonObjectResponse(_HttpResponse):
            def json(self) -> object:
                return []

        return NonObjectResponse()

    _install_http_transport(monkeypatch, get)
    with pytest.raises(MarketError, match="接口返回不是对象") as caught:
        EastMoneyClient()._get_json_direct_retry(
            "https://example.test/private-path"
        )
    assert not isinstance(caught.value, TransientMarketError)
    assert calls == 1


def test_direct_json_404_is_permanent_and_not_retried(monkeypatch) -> None:
    calls = 0

    def get(_url: str):
        nonlocal calls
        calls += 1
        return _HttpResponse(status_code=404)

    _install_http_transport(monkeypatch, get)
    with pytest.raises(MarketError, match="HTTP 404") as caught:
        EastMoneyClient()._get_json_direct_retry(
            "https://example.test/private-path"
        )
    assert not isinstance(caught.value, TransientMarketError)
    assert calls == 1


@pytest.mark.parametrize("status_code", [429, 503])
def test_direct_json_retry_retries_only_retryable_http_statuses(
    monkeypatch, status_code
) -> None:
    calls = 0

    def get(_url: str):
        nonlocal calls
        calls += 1
        return _HttpResponse(status_code=status_code if calls < 3 else 200)

    _install_http_transport(monkeypatch, get)
    assert EastMoneyClient()._get_json_direct_retry(
        "https://example.test/private-path"
    ) == {"ok": True}
    assert calls == 3


def test_direct_json_retry_counts_each_transport(monkeypatch) -> None:
    callbacks = 0
    transports = 0

    def before_request() -> None:
        nonlocal callbacks
        callbacks += 1

    def get(_url: str):
        nonlocal transports
        transports += 1
        if transports < 3:
            raise httpx2.ConnectError("disconnected")
        return _HttpResponse()

    _install_http_transport(monkeypatch, get)
    client = EastMoneyClient()
    assert client._get_json_direct_retry(
        "https://example.test/private-path",
        before_request=before_request,
    ) == {"ok": True}
    assert callbacks == transports == client.request_count == 3


def test_company_security_fallback_does_not_retry_calendar(
    monkeypatch,
) -> None:
    calendar_calls = 0

    def get(url: str):
        nonlocal calendar_calls
        if "stock/get" in url:
            return _HttpResponse(
                {
                    "data": {
                        "f57": "600000",
                        "f58": "浦发银行",
                        "f107": 1,
                        "f127": "银行",
                        "f189": 19991110,
                        "f292": 2,
                    }
                }
            )
        assert "secid=0.399317" in url
        calendar_calls += 1
        return _HttpResponse(status_code=503)

    _install_http_transport(monkeypatch, get)
    client = EastMoneyClient()
    with pytest.raises(TransientMarketError, match="calendar"):
        client._security_state_for("600000.SH", as_of=date(2026, 9, 16))
    assert calendar_calls == 1
    assert client.request_count == 2


@pytest.mark.parametrize(
    "error_type",
    [DiscoveryRequestBudgetExhausted, DiscoveryRequestRuntimeExhausted],
)
def test_direct_json_retry_stops_before_transport_on_gate_error(
    monkeypatch, error_type
) -> None:
    callbacks = 0
    transported = False

    def before_request() -> None:
        nonlocal callbacks
        callbacks += 1
        raise error_type("request stopped")

    def get(_url: str):
        nonlocal transported
        transported = True
        return _HttpResponse()

    _install_http_transport(monkeypatch, get)
    with pytest.raises(error_type, match="request stopped"):
        EastMoneyClient()._get_json_direct_retry(
            "https://example.test/private-path",
            before_request=before_request,
        )
    assert callbacks == 1
    assert transported is False


def test_discovery_request_gate_persists_and_spaces_every_fallback_attempt(
    tmp_path, monkeypatch
) -> None:
    from jobs.discovery_data import DiscoveryRequestGate, RequestPolicy

    engine = init_db(tmp_path / "request-boundary.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with factory() as session:
        fill_id = _fill(session, [], now=now, daily_budget=10).id
    clock = FakeClock(now)
    observations: list[tuple[datetime, int, int]] = []

    def get(_url: str):
        with factory() as observer:
            fill = observer.get(DiscoveryDataFill, fill_id)
            assert fill is not None
            observations.append(
                (clock.now(), fill.daily_requests, fill.request_count)
            )
        if len(observations) < 3:
            raise RuntimeError("try fallback host")
        return _HttpResponse()

    _install_http_transport(monkeypatch, get)
    policy = RequestPolicy(
        requests_per_second=0.5,
        max_attempts=2,
        daily_budget=10,
        max_runtime_seconds=60,
    )
    with factory() as session:
        gate = DiscoveryRequestGate(
            session, fill_id, policy=policy, clock=clock
        )
        assert EastMoneyClient()._get_json(
            BOARD_LIST_URL, before_request=gate
        ) == {"ok": True}

    assert observations == [
        (now, 1, 1),
        (now + timedelta(seconds=2), 2, 2),
        (now + timedelta(seconds=4), 3, 3),
    ]
    assert clock.sleeps == [2.0, 2.0]
    engine.dispose()


def test_discovery_request_gate_resets_at_midnight_between_fallbacks(
    tmp_path, monkeypatch
) -> None:
    from jobs.discovery_data import DiscoveryRequestGate, RequestPolicy

    engine = init_db(tmp_path / "request-midnight.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 15, 59, 59, tzinfo=timezone.utc)
    with factory() as session:
        fill_id = _fill(session, [], now=now, daily_budget=1).id
    clock = FakeClock(now)
    calls = 0

    def get(_url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("cross midnight")
        return _HttpResponse()

    _install_http_transport(monkeypatch, get)
    policy = RequestPolicy(
        requests_per_second=0.5,
        max_attempts=2,
        daily_budget=1,
        max_runtime_seconds=60,
    )
    with factory() as session:
        gate = DiscoveryRequestGate(
            session, fill_id, policy=policy, clock=clock
        )
        EastMoneyClient()._get_json(BOARD_LIST_URL, before_request=gate)
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert fill.budget_day == "2026-09-17"
        assert fill.daily_requests == 1
        assert fill.request_count == 2
    assert calls == 2
    engine.dispose()


def test_discovery_company_bundle_resets_budget_between_midnight_requests(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "company-midnight.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 15, 59, 59, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill_id = _fill(session, [code], now=now, daily_budget=1).id
    clock = FakeClock(now)
    policy = RequestPolicy(
        requests_per_second=0.5,
        max_attempts=2,
        daily_budget=1,
        max_runtime_seconds=60,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            FakeMarketClient(
                discovery_data={code: _bundle(code, request_count=2)}
            ),
            fill_id,
            policy=policy,
            clock=clock,
        )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (fill_id, code)
        )
        assert fill is not None and checkpoint is not None
        assert (fill.status, fill.request_count) == ("done", 2)
        assert (fill.budget_day, fill.daily_requests) == ("2026-09-17", 1)
        assert checkpoint.status == "done"
    engine.dispose()


def test_discovery_request_gate_charges_crash_before_transport_returns(
    tmp_path, monkeypatch
) -> None:
    from jobs.discovery_data import DiscoveryRequestGate, RequestPolicy

    engine = init_db(tmp_path / "request-crash.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with factory() as session:
        fill_id = _fill(session, [], now=now, daily_budget=10).id
    clock = FakeClock(now)
    calls = 0

    def get(_url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fallback")
        raise KeyboardInterrupt("simulated process crash")

    _install_http_transport(monkeypatch, get)
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=2,
        daily_budget=10,
        max_runtime_seconds=60,
    )
    with factory() as session:
        gate = DiscoveryRequestGate(
            session, fill_id, policy=policy, clock=clock
        )
        with pytest.raises(KeyboardInterrupt):
            EastMoneyClient()._get_json(
                BOARD_LIST_URL, before_request=gate
            )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert (fill.daily_requests, fill.request_count) == (2, 2)
    engine.dispose()


def test_discovery_request_gate_stops_before_transport_when_budget_exhausted(
    tmp_path, monkeypatch
) -> None:
    from jobs.discovery_data import (
        DiscoveryRequestBudgetExhausted,
        DiscoveryRequestGate,
        RequestPolicy,
    )

    engine = init_db(tmp_path / "request-budget-stop.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with factory() as session:
        fill = _fill(session, [], now=now, daily_budget=1)
        fill.daily_requests = 1
        session.commit()
        fill_id = fill.id
    transported = False

    def get(_url: str):
        nonlocal transported
        transported = True
        return _HttpResponse()

    _install_http_transport(monkeypatch, get)
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=2,
        daily_budget=1,
        max_runtime_seconds=60,
    )
    with factory() as session:
        gate = DiscoveryRequestGate(
            session, fill_id, policy=policy, clock=FakeClock(now)
        )
        with pytest.raises(DiscoveryRequestBudgetExhausted):
            EastMoneyClient()._get_json_direct(
                "https://example.test/data", before_request=gate
            )
    assert transported is False
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert (fill.daily_requests, fill.request_count) == (1, 0)
        assert fill.status == "paused"
    engine.dispose()


def test_fetch_discovery_data_scopes_gate_into_http_boundary(
    monkeypatch,
) -> None:
    callbacks = 0
    transports = 0

    def before_request() -> None:
        nonlocal callbacks
        callbacks += 1

    def get(_url: str):
        nonlocal transports
        transports += 1
        return _HttpResponse()

    _install_http_transport(monkeypatch, get)

    class Stub(EastMoneyClient):
        def _security_state_for(
            self, _code: str, *, before_request=None
        ) -> SecurityState:
            assert before_request is not None
            self._get_json_direct(
                "https://example.test/security",
                before_request=before_request,
            )
            raise MarketError("stop after boundary probe")

    with pytest.raises(DiscoveryCompanyError, match="boundary probe") as caught:
        Stub().fetch_discovery_data(
            "600519.SH", before_request=before_request
        )
    assert callbacks == transports == caught.value.request_count == 1


def test_discovery_worker_rejects_typed_request_count_audit_mismatch(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "request-audit.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill_id = _fill(session, [code], now=now).id

    class BadAuditFake(FakeMarketClient):
        def fetch_discovery_data(
            self, code: str, *, before_request=None
        ) -> DiscoveryCompanyData:
            assert before_request is not None
            before_request()
            return _bundle(code, request_count=2)

    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=1,
        daily_budget=10,
        max_runtime_seconds=60,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            BadAuditFake(),
            fill_id,
            policy=policy,
            clock=FakeClock(now),
        )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (fill_id, code)
        )
        assert fill is not None and checkpoint is not None
        assert (fill.request_count, fill.failed) == (1, 1)
        assert checkpoint.status == "failed"
        assert checkpoint.error and "request_audit" in checkpoint.error
    engine.dispose()


def _universe_row(code: str, *, suspended: bool = False) -> dict[str, object]:
    return {
        "f12": code,
        "f13": 1,
        "f14": f"公司{code}",
        "f2": "-",
        "f9": "10",
        "f23": "1",
        "f20": "100",
        "f26": 20000101,
        "f100": "测试",
        "f292": 6 if suspended else None,
    }


def test_live_universe_retries_calendar_without_refetching_56_pages(
    monkeypatch,
) -> None:
    page_calls: dict[int, int] = {}
    calendar_calls = 0

    def get(url: str):
        nonlocal calendar_calls
        if "clist/get" in url:
            page = int(parse_qs(urlsplit(url).query)["pn"][0])
            page_calls[page] = page_calls.get(page, 0) + 1
            size = 100 if page < 56 else 1
            return _HttpResponse(
                {
                    "data": {
                        "diff": [
                            {
                                **_universe_row(
                                    f"{600000 + (page - 1) * 100 + index:06d}"
                                ),
                                "f2": "10",
                                "f292": 2,
                            }
                            for index in range(size)
                        ]
                    }
                }
            )
        assert "secid=0.399317" in url
        calendar_calls += 1
        if calendar_calls == 1:
            raise httpx2.ConnectError("disconnected")
        return _HttpResponse(
            {
                "data": {
                    "klines": [
                        "2026-09-15,1,2,3,4,5,6",
                        "2026-09-16,1,2,3,4,5,6",
                    ]
                }
            }
        )

    _install_http_transport(monkeypatch, get)
    client = EastMoneyClient()
    rows = client.fetch_universe(as_of=date(2026, 9, 16))

    assert len(rows) == 5501
    assert page_calls == {page: 1 for page in range(1, 57)}
    assert calendar_calls == 2
    assert client.request_count == 58


def test_calendar_transient_failure_is_bounded_and_contextual(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def get(url: str):
        calls.append(url)
        raise httpx2.ConnectError(
            "GET https://secret.example/calendar disconnected"
        )

    _install_http_transport(monkeypatch, get)
    with pytest.raises(TransientMarketError, match="calendar") as caught:
        EastMoneyClient().fetch_trading_dates(
            "2026-09-16", retry_transient=True
        )
    assert len(calls) == 6
    assert sum("push2his.eastmoney.com" in url for url in calls) == 3
    assert sum("datacenter-web.eastmoney.com" in url for url in calls) == 3
    assert "https://" not in str(caught.value)


def _valuation_date_payload(
    code: str, dates: list[str]
) -> dict[str, object]:
    return {
        "success": True,
        "result": {
            "data": [
                {"SECURITY_CODE": code, "TRADE_DATE": f"{day} 00:00:00"}
                for day in dates
            ]
        },
    }


def test_valuation_calendar_query_uses_verified_minimum_contract(
    monkeypatch,
) -> None:
    urls: list[str] = []
    expected = [
        "2026-09-18",
        "2026-09-17",
        "2026-09-16",
        "2026-09-15",
        "2026-09-14",
        "2026-09-11",
    ]

    def get(url: str):
        urls.append(url)
        return _HttpResponse(_valuation_date_payload("600519", expected))

    _install_http_transport(monkeypatch, get)
    assert EastMoneyClient()._valuation_trade_dates(
        "600519.SH", "2026-09-20", limit=6, retry_transient=True
    ) == expected

    assert len(urls) == 1
    parsed = urlsplit(urls[0])
    query = parse_qs(parsed.query)
    assert parsed.netloc == "datacenter-web.eastmoney.com"
    assert query == {
        "reportName": ["RPT_VALUEANALYSIS_DET"],
        "columns": ["SECURITY_CODE,TRADE_DATE"],
        "pageSize": ["12"],
        "pageNumber": ["1"],
        "sortColumns": ["TRADE_DATE"],
        "sortTypes": ["-1"],
        "filter": [
            '(SECURITY_CODE="600519")(TRADE_DATE<=\'2026-09-20\')'
        ],
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            _valuation_date_payload(
                "600519", ["2026-09-18", "2026-09-18"]
            ),
            "严格降序且唯一",
        ),
        (
            {
                "success": True,
                "result": {
                    "data": [
                        {
                            "SECURITY_CODE": "600519",
                            "TRADE_DATE": "09/18/2026",
                        }
                    ]
                },
            },
            "无效交易日期",
        ),
    ],
)
def test_valuation_calendar_rejects_untrusted_dates(
    monkeypatch, payload, message
) -> None:
    calls = 0

    def get(_url: str):
        nonlocal calls
        calls += 1
        return _HttpResponse(payload)

    _install_http_transport(monkeypatch, get)
    with pytest.raises(MarketError, match=message) as caught:
        EastMoneyClient()._valuation_trade_dates(
            "600519.SH", "2026-09-20", limit=2, retry_transient=True
        )
    assert not isinstance(caught.value, TransientMarketError)
    assert calls == 1


def test_valuation_calendar_filters_future_rows_before_exact_limit(
    monkeypatch,
) -> None:
    def get(_url: str):
        return _HttpResponse(
            _valuation_date_payload(
                "600519",
                ["2026-09-21", "2026-09-18", "2026-09-17"],
            )
        )

    _install_http_transport(monkeypatch, get)
    assert EastMoneyClient()._valuation_trade_dates(
        "600519.SH", "2026-09-20", limit=2, retry_transient=True
    ) == ["2026-09-18", "2026-09-17"]


def test_valuation_calendar_future_filtering_shortage_fails_closed(
    monkeypatch,
) -> None:
    def get(_url: str):
        return _HttpResponse(
            _valuation_date_payload(
                "600519", ["2026-09-21", "2026-09-18"]
            )
        )

    _install_http_transport(monkeypatch, get)
    with pytest.raises(MarketError, match="不足请求条数"):
        EastMoneyClient()._valuation_trade_dates(
            "600519.SH", "2026-09-20", limit=2, retry_transient=True
        )


def test_calendar_transient_kline_uses_matching_liquid_proxies(
    monkeypatch,
) -> None:
    calls: list[str] = []
    expected = ["2026-09-18", "2026-09-17", "2026-09-16"]

    def get(url: str):
        calls.append(url)
        if "push2his.eastmoney.com" in url:
            raise httpx2.ConnectError("disconnected")
        code = parse_qs(urlsplit(url).query)["filter"][0].split('"')[1]
        return _HttpResponse(_valuation_date_payload(code, expected))

    _install_http_transport(monkeypatch, get)
    client = EastMoneyClient()
    assert client.fetch_trading_dates(
        "2026-09-20", limit=3, retry_transient=True
    ) == list(reversed(expected))
    assert len(calls) == client.request_count == 5
    assert sum("push2his.eastmoney.com" in url for url in calls) == 3
    assert sum("datacenter-web.eastmoney.com" in url for url in calls) == 2


@pytest.mark.parametrize("truncated_code", [None, "000001"])
def test_calendar_matching_or_single_truncated_proxy_fails_closed(
    monkeypatch, truncated_code
) -> None:
    calls = 0

    def get(url: str):
        nonlocal calls
        calls += 1
        if "push2his.eastmoney.com" in url:
            raise httpx2.ConnectError("disconnected")
        code = parse_qs(urlsplit(url).query)["filter"][0].split('"')[1]
        dates = ["2026-09-18", "2026-09-17", "2026-09-16"]
        if truncated_code is None or code == truncated_code:
            dates = dates[:2]
        return _HttpResponse(_valuation_date_payload(code, dates))

    _install_http_transport(monkeypatch, get)
    with pytest.raises(MarketError, match="不足请求条数"):
        EastMoneyClient().fetch_trading_dates(
            "2026-09-20", limit=3, retry_transient=True
        )
    assert calls == (4 if truncated_code is None else 5)


def test_calendar_proxy_disagreement_is_rejected(monkeypatch) -> None:
    calls = 0

    def get(url: str):
        nonlocal calls
        calls += 1
        if "push2his.eastmoney.com" in url:
            raise httpx2.ConnectError("disconnected")
        code = parse_qs(urlsplit(url).query)["filter"][0].split('"')[1]
        dates = (
            ["2026-09-18", "2026-09-17"]
            if code == "600519"
            else ["2026-09-18", "2026-09-16"]
        )
        return _HttpResponse(_valuation_date_payload(code, dates))

    _install_http_transport(monkeypatch, get)
    with pytest.raises(MarketError, match="代理交易日不一致"):
        EastMoneyClient().fetch_trading_dates(
            "2026-09-20", limit=2, retry_transient=True
        )
    assert calls == 5


def test_calendar_proxy_failure_propagates_after_bounded_retries(
    monkeypatch,
) -> None:
    calls = 0

    def get(url: str):
        nonlocal calls
        calls += 1
        if "push2his.eastmoney.com" in url:
            raise httpx2.ConnectError("calendar disconnected")
        code = parse_qs(urlsplit(url).query)["filter"][0].split('"')[1]
        if code == "000001":
            return _HttpResponse(status_code=503)
        return _HttpResponse(
            _valuation_date_payload(
                code, ["2026-09-18", "2026-09-17"]
            )
        )

    _install_http_transport(monkeypatch, get)
    with pytest.raises(TransientMarketError):
        EastMoneyClient().fetch_trading_dates(
            "2026-09-20", limit=2, retry_transient=True
        )
    assert calls == 7


def test_calendar_kline_success_never_calls_valuation_fallback(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def get(url: str):
        calls.append(url)
        assert "push2his.eastmoney.com" in url
        return _HttpResponse(
            {
                "data": {
                    "klines": [
                        "2026-09-17,1,2,3,4,5,6",
                        "2026-09-18,1,2,3,4,5,6",
                    ]
                }
            }
        )

    _install_http_transport(monkeypatch, get)
    assert EastMoneyClient().fetch_trading_dates(
        "2026-09-20", limit=2, retry_transient=True
    ) == ["2026-09-17", "2026-09-18"]
    assert len(calls) == 1


def test_calendar_permanent_kline_error_never_calls_valuation_fallback(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def get(url: str):
        calls.append(url)
        assert "push2his.eastmoney.com" in url
        return _HttpResponse({"data": {"klines": "not-a-list"}})

    _install_http_transport(monkeypatch, get)
    with pytest.raises(MarketError, match="交易日接口返回格式错误"):
        EastMoneyClient().fetch_trading_dates(
            "2026-09-20", limit=2, retry_transient=True
        )
    assert len(calls) == 1


def test_calendar_budget_stop_aborts_between_proxies(monkeypatch) -> None:
    gate_calls = 0
    transports = 0

    def before_request() -> None:
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 5:
            raise DiscoveryRequestBudgetExhausted("request stopped")

    def get(url: str):
        nonlocal transports
        transports += 1
        if "push2his.eastmoney.com" in url:
            raise httpx2.ConnectError("disconnected")
        code = parse_qs(urlsplit(url).query)["filter"][0].split('"')[1]
        return _HttpResponse(
            _valuation_date_payload(
                code, ["2026-09-18", "2026-09-17"]
            )
        )

    _install_http_transport(monkeypatch, get)
    with pytest.raises(DiscoveryRequestBudgetExhausted):
        EastMoneyClient().fetch_trading_dates(
            "2026-09-20",
            limit=2,
            retry_transient=True,
            before_request=before_request,
        )
    assert gate_calls == 5
    assert transports == 4


def test_malformed_calendar_payload_is_not_retried(monkeypatch) -> None:
    calls = 0

    def get(_url: str):
        nonlocal calls
        calls += 1
        return _HttpResponse({"data": {"klines": "not-a-list"}})

    _install_http_transport(monkeypatch, get)
    with pytest.raises(MarketError, match="交易日接口返回格式错误") as caught:
        EastMoneyClient().fetch_trading_dates("2026-09-16")
    assert not isinstance(caught.value, TransientMarketError)
    assert calls == 1


def test_suspended_kline_transient_uses_same_stock_valuation_date(
    monkeypatch,
) -> None:
    stock_kline_calls = 0

    def get(url: str):
        nonlocal stock_kline_calls
        if "clist/get" in url:
            return _HttpResponse(
                {
                    "data": {
                        "diff": [
                            {
                                **_universe_row("601999", suspended=True),
                                "f2": "10",
                            }
                        ]
                    }
                }
            )
        if "secid=0.399317" in url:
            return _HttpResponse(
                {
                    "data": {
                        "klines": [
                            "2026-09-15,1,2,3,4,5,6",
                            "2026-09-16,1,2,3,4,5,6",
                        ]
                    }
                }
            )
        if "datacenter-web.eastmoney.com" in url:
            query = parse_qs(urlsplit(url).query)
            assert 'SECURITY_CODE="601999"' in query["filter"][0]
            return _HttpResponse(
                _valuation_date_payload("601999", ["2026-09-15"])
            )
        assert "secid=1.601999" in url
        stock_kline_calls += 1
        raise httpx2.ConnectError("disconnected")

    _install_http_transport(monkeypatch, get)
    client = EastMoneyClient()
    rows = client.fetch_universe(as_of=date(2026, 9, 16))
    assert [(row.trading_status, row.last_trade_date) for row in rows] == [
        ("suspended", "2026-09-15")
    ]
    assert stock_kline_calls == 3
    assert client.request_count == 6


def test_suspended_kline_fallback_with_no_history_is_safe_none(
    monkeypatch,
) -> None:
    calls = 0

    def get(url: str):
        nonlocal calls
        calls += 1
        if "push2his.eastmoney.com" in url:
            raise httpx2.ConnectError("disconnected")
        query = parse_qs(urlsplit(url).query)
        assert query["pageSize"] == ["2"]
        assert 'SECURITY_CODE="601999"' in query["filter"][0]
        return _HttpResponse(_valuation_date_payload("601999", []))

    _install_http_transport(monkeypatch, get)
    assert EastMoneyClient()._last_trade_date(
        "601999.SH", "2026-09-20", retry_transient=True
    ) is None
    assert calls == 4


def test_universe_status_names_calendar_without_leaking_url(
    tmp_path, monkeypatch
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "universe-calendar-error.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with factory() as session:
        fill_id = _fill(session, [], now=now, daily_budget=10).id

    def get(url: str):
        if "clist/get" in url:
            return _HttpResponse(
                {
                    "data": {
                        "diff": [
                            {
                                **_universe_row("600000"),
                                "f2": "10",
                                "f292": 2,
                            }
                        ]
                    }
                }
            )
        raise httpx2.ConnectError(
            "GET https://secret.example/calendar disconnected"
        )

    _install_http_transport(monkeypatch, get)
    with factory() as session:
        execute_discovery_data_fill(
            session,
            EastMoneyClient(),
            fill_id,
            policy=RequestPolicy(
                requests_per_second=100,
                max_attempts=2,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            clock=FakeClock(now),
        )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert (fill.status, fill.request_count) == ("paused", 7)
        assert fill.last_error is not None
        assert "universe: calendar" in fill.last_error
        assert "https://" not in fill.last_error
    engine.dispose()


def test_first_run_universe_gates_pages_calendar_and_suspended_kline(
    tmp_path, monkeypatch
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "universe-boundary.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with factory() as session:
        fill_id = _fill(session, [], now=now, daily_budget=20).id
    clock = FakeClock(now)
    observations: list[tuple[datetime, int]] = []

    def get(url: str):
        with factory() as observer:
            fill = observer.get(DiscoveryDataFill, fill_id)
            assert fill is not None
            observations.append((clock.now(), fill.request_count))
        if "clist/get" in url:
            page = int(parse_qs(urlsplit(url).query)["pn"][0])
            rows = (
                [_universe_row(f"{600000 + index:06d}") for index in range(100)]
                if page == 1
                else [_universe_row("601999", suspended=True)]
            )
            return _HttpResponse({"data": {"diff": rows}})
        if "secid=0.399317" in url:
            return _HttpResponse(
                {
                    "data": {
                        "klines": [
                            "2026-09-15,1,2,3,4,5,6",
                            "2026-09-16,1,2,3,4,5,6",
                        ]
                    }
                }
            )
        assert "secid=1.601999" in url
        return _HttpResponse(
            {"data": {"klines": ["2026-09-15,1,2,3,4,5,6"]}}
        )

    _install_http_transport(monkeypatch, get)
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=2,
        daily_budget=20,
        max_runtime_seconds=0.035,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            EastMoneyClient(),
            fill_id,
            policy=policy,
            clock=clock,
        )
    assert observations == [
        (now, 1),
        (now + timedelta(seconds=0.01), 2),
        (now + timedelta(seconds=0.02), 3),
        (now + timedelta(seconds=0.03), 4),
    ]
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert (fill.status, fill.total, fill.request_count) == (
            "paused",
            101,
            4,
        )
        assert session.query(DiscoveryDataCheckpoint).filter_by(
            fill_id=fill_id
        ).count() == 101
    engine.dispose()


def test_universe_budget_exhaustion_stops_transport_and_remains_resumable(
    tmp_path, monkeypatch
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "universe-budget.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with factory() as session:
        fill_id = _fill(session, [], now=now, daily_budget=2).id
    clock = FakeClock(now)
    transports = 0

    def get(url: str):
        nonlocal transports
        transports += 1
        page = int(parse_qs(urlsplit(url).query)["pn"][0])
        return _HttpResponse(
            {
                "data": {
                    "diff": [
                        _universe_row(f"{600000 + page * 100 + index:06d}")
                        for index in range(100)
                    ]
                }
            }
        )

    _install_http_transport(monkeypatch, get)
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=2,
        daily_budget=2,
        max_runtime_seconds=60,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            EastMoneyClient(),
            fill_id,
            policy=policy,
            clock=clock,
        )
    assert transports == 2
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert (fill.status, fill.total, fill.request_count) == (
            "paused",
            0,
            2,
        )
        assert session.query(DiscoveryDataCheckpoint).filter_by(
            fill_id=fill_id
        ).count() == 0

    # Explicit same-day resume re-enters initialization but the gate rejects
    # before transport, preserving the empty checkpoint state for another day.
    with factory() as session:
        execute_discovery_data_fill(
            session,
            EastMoneyClient(),
            fill_id,
            policy=policy,
            clock=clock,
        )
    assert transports == 2
    engine.dispose()


def test_universe_crash_after_second_transport_keeps_exact_accounting(
    tmp_path, monkeypatch
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "universe-crash.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with factory() as session:
        fill_id = _fill(session, [], now=now, daily_budget=20).id
    clock = FakeClock(now)
    transports = 0

    def get(url: str):
        nonlocal transports
        transports += 1
        if transports == 2:
            raise KeyboardInterrupt("simulated universe crash")
        return _HttpResponse(
            {
                "data": {
                    "diff": [
                        _universe_row(f"{600000 + index:06d}")
                        for index in range(100)
                    ]
                }
            }
        )

    _install_http_transport(monkeypatch, get)
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=2,
        daily_budget=20,
        max_runtime_seconds=60,
    )
    with factory() as session:
        with pytest.raises(KeyboardInterrupt):
            execute_discovery_data_fill(
                session,
                EastMoneyClient(),
                fill_id,
                policy=policy,
                clock=clock,
            )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert (fill.status, fill.total, fill.request_count) == (
            "paused",
            0,
            2,
        )
        assert fill.daily_requests == 2
    assert transports == 2
    engine.dispose()


def test_universe_without_gate_keeps_old_override_contract() -> None:
    calls = 0

    class LegacyStub(EastMoneyClient):
        def _get_json(self, _url: str) -> dict:
            nonlocal calls
            calls += 1
            return {
                "data": {
                    "diff": [
                        {
                            **_universe_row("600000"),
                            "f2": "10",
                            "f292": None,
                        }
                    ]
                }
            }

    rows = LegacyStub().fetch_universe()
    assert [row.code for row in rows] == ["600000.SH"]
    assert calls == 1


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


def _complete_universe_row(code: str) -> UniverseRow:
    state = _security(code)
    return UniverseRow(
        code=code,
        name=state.name,
        price=Decimal("10"),
        pe=Decimal("5"),
        pb=Decimal("1"),
        mcap=Decimal("100"),
        industry=state.industry,
        exchange=state.exchange,
        listing_date=state.listing_date,
        is_st=state.is_st,
        is_delisting=state.is_delisting,
        trading_status=state.trading_status,
        last_trade_date=state.last_trade_date,
        industry_version=state.industry_version,
        industry_as_of=state.industry_as_of,
        security_source=state.source,
        market_status_code=state.market_status_code,
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


def _complete_checkpoint_payload(
    checkpoint: DiscoveryDataCheckpoint,
    now: datetime,
) -> None:
    from discovery.company_payload import (
        company_payload_digest,
        serialize_company_payload,
    )

    checkpoint.payload_json = serialize_company_payload(
        _bundle(checkpoint.code),
        captured_at=now,
    )
    checkpoint.payload_digest = company_payload_digest(
        checkpoint.payload_json
    )
    checkpoint.status = "done"
    checkpoint.updated_at = now


def test_checkpoint_payload_schema_migrates_additively_and_idempotently(
    tmp_path,
) -> None:
    db_path = tmp_path / "checkpoint-payload-migration.db"
    old_engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    with old_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE discovery_data_checkpoints (
                    fill_id INTEGER NOT NULL,
                    code VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_retry_at DATETIME,
                    valuation_points INTEGER NOT NULL,
                    error TEXT,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (fill_id, code)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO discovery_data_checkpoints
                VALUES (7, '600000.SH', 'done', 1, NULL, 0, NULL,
                        '2026-09-16 12:00:00')
                """
            )
        )
    old_engine.dispose()

    for _ in range(2):
        engine = init_db(db_path)
        with engine.connect() as conn:
            columns = {
                row[1]: row[3]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(discovery_data_checkpoints)"
                )
            }
            assert columns["payload_json"] == 0
            assert columns["payload_digest"] == 0
            assert conn.execute(
                text(
                    "SELECT payload_json, payload_digest "
                    "FROM discovery_data_checkpoints "
                    "WHERE fill_id = 7 AND code = '600000.SH'"
                )
            ).one() == (None, None)
        engine.dispose()


def test_discovery_company_payload_round_trips_exact_strict_bundle() -> None:
    from discovery.company_payload import (
        parse_company_payload,
        serialize_company_payload,
    )

    captured_at = datetime(2026, 9, 16, 12, 34, 56, 123456, tzinfo=timezone.utc)
    bundle = DiscoveryCompanyData(
        code="600036.SH",
        security_state=_security("600036.SH"),
        financial_rows=(
            FinancialRow(
                code="600036.SH",
                period="2025",
                period_type="annual",
                revenue=Decimal("337532000000.123456789012345678"),
                net_income=None,
                capital_adequacy=Decimal("18.24"),
                core_tier1_capital_adequacy=Decimal("14.16"),
                nonperforming_loan_balance=Decimal("68206000000"),
                source_as_of="2026-03-28",
            ),
        ),
        valuation_points=(
            ValuationPoint(
                code="600036.SH",
                month="2026-08",
                trade_date="2026-08-31",
                pe=Decimal("6.123456789012345678"),
                pb=Decimal("0.9"),
                price=Decimal("42.01"),
                source="eastmoney:RPT_VALUEANALYSIS_DET",
                ratio_method="PE_TTM/PB_MRQ",
            ),
        ),
        request_count=9,
        source_as_of=DiscoverySourceAsOf(
            security="2026-09-16",
            financial="2026-03-28",
            valuation="2026-08-31",
        ),
    )

    encoded = serialize_company_payload(bundle, captured_at=captured_at)
    parsed = parse_company_payload(encoded)
    from discovery.company_payload import company_payload_digest

    assert company_payload_digest(encoded) == (
        "360375e659a0b75374bf2daec37a170fe8bd9f37ae9c4f217ce01c3ca9e019d7"
    )
    row = parsed["financial_rows"][0]
    assert row["values"]["revenue"] == "337532000000.123456789012345678"
    assert row["values"]["net_income"] is None
    assert row["field_sources"]["capital_adequacy"] == {
        "consolidated_basis": "consolidated",
        "field": "NEWCAPITALADER",
        "financial_sector_substitute": "NEWCAPITALADER",
        "period_type": "period_end",
        "report": "RPT_F10_FINANCE_MAINFINADATA",
        "source": "discovery",
        "source_as_of": "NOTICE_DATE/UPDATE_DATE",
        "trusted": True,
        "unit": "percent",
    }
    assert parsed["valuation_points"][0]["pe"] == "6.123456789012345678"
    assert parsed["captured_at"] == "2026-09-16T12:34:56.123456Z"
    assert serialize_company_payload(bundle, captured_at=captured_at) == encoded

    missing = json.loads(encoded)
    del missing["financial_rows"][0]["values"]["net_income"]
    with pytest.raises(ValueError, match="financial values"):
        parse_company_payload(json.dumps(missing, separators=(",", ":"), sort_keys=True))


@pytest.mark.parametrize(
    "field",
    ["ebit", "interest", "roic", "government_grants"],
)
def test_discovery_company_payload_rejects_untrusted_fields(field: str) -> None:
    from discovery.company_payload import serialize_company_payload

    bundle = _bundle("600519.SH")
    row = replace(bundle.financial_rows[0], **{field: Decimal("1")})
    with pytest.raises(ValueError, match=field):
        serialize_company_payload(
            replace(bundle, financial_rows=(row,)),
            captured_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        '{"captured_at":"2026-09-16T12:00:00Z","code":"600000.SH","x":NaN}',
        '{"captured_at":"2026-09-16T12:00:00Z","code":"600000.SH"}',
    ],
)
def test_discovery_company_payload_rejects_malformed_or_nonfinite(
    payload: str,
) -> None:
    from discovery.company_payload import parse_company_payload

    with pytest.raises(ValueError):
        parse_company_payload(payload)


def test_discovery_company_payload_rejects_nonfinite_decimal() -> None:
    from discovery.company_payload import serialize_company_payload

    bundle = replace(
        _bundle("600000.SH"),
        financial_rows=(
            FinancialRow(
                code="600000.SH",
                period="2025",
                period_type="annual",
                net_income=Decimal("NaN"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="finite"):
        serialize_company_payload(
            bundle,
            captured_at=datetime(2026, 9, 16, 12, tzinfo=timezone.utc),
        )


def test_company_persistence_crash_rolls_back_payload_and_global_rows(
    tmp_path, monkeypatch
) -> None:
    from jobs import discovery_data

    engine = init_db(tmp_path / "checkpoint-payload-rollback.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "600000.SH"
    with factory() as session:
        fill_id = _fill(session, [code], now=now).id

    def crash(*_args, **_kwargs):
        raise RuntimeError("valuation persistence crashed")

    monkeypatch.setattr(discovery_data, "persist_valuation_history", crash)
    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        assert checkpoint is not None
        checkpoint.status = "running"
        bundle = replace(
            _bundle(code),
            financial_rows=(
                FinancialRow(
                    code=code,
                    period="2025",
                    period_type="annual",
                    net_income=Decimal("100.123456789012345678"),
                ),
            ),
        )
        with pytest.raises(RuntimeError, match="crashed"):
            discovery_data._persist_company(session, checkpoint, bundle, now)
        session.rollback()

    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        assert checkpoint is not None
        assert checkpoint.status == "pending"
        assert checkpoint.payload_json is None
        assert session.get(Financial, (code, "2025", "annual")) is None
    engine.dispose()


def test_real_fill_execution_payload_feeds_weekly_snapshot(tmp_path) -> None:
    from discovery.snapshot import build_production_snapshot
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "execute-to-weekly-snapshot.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "600000.SH"
    point = ValuationPoint(
        code=code,
        month="2026-08",
        trade_date="2026-08-31",
        pe=Decimal("7.123456789"),
        pb=Decimal("0.8"),
        price=Decimal("12.34"),
    )
    bundle = replace(
        _bundle(code, valuation_points=(point,)),
        financial_rows=(
            FinancialRow(
                code=code,
                period="2025",
                period_type="annual",
                net_income=Decimal("123.456789012345678"),
                capital_adequacy=Decimal("18.24"),
                source_as_of="2026-03-31",
            ),
        ),
        source_as_of=DiscoverySourceAsOf(
            security="2026-09-16",
            financial="2026-03-31",
            valuation="2026-08-31",
        ),
    )
    with factory() as session:
        fill_id = _fill(session, [code], now=now).id
        execute_discovery_data_fill(
            session,
            FakeMarketClient(discovery_data={code: bundle}),
            fill_id,
            policy=RequestPolicy(
                requests_per_second=100,
                max_attempts=1,
                daily_budget=100,
                max_runtime_seconds=60,
            ),
            clock=FakeClock(now),
        )
    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        assert checkpoint is not None and checkpoint.payload_json is not None
        assert checkpoint.payload_digest is not None
        company = build_production_snapshot(session).companies[0]
    assert company.fields["financial_history"][0]["values"]["net_income"] == (
        "123.456789012345678"
    )
    assert company.fields["financial_history"][0]["values"][
        "capital_adequacy"
    ] == "18.24"
    assert company.fields["valuation_history"][0]["price"] == "12.34"
    engine.dispose()


@pytest.mark.parametrize("legacy_status", ["paused", "done"])
def test_init_preserves_legacy_snapshot_and_explicit_start_replaces(
    tmp_path, legacy_status: str
) -> None:
    from jobs.discovery_data import start_discovery_data_fill

    db_path = tmp_path / f"legacy-{legacy_status}.db"
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with factory() as session:
        fill = _fill(session, ["600000.SH"], now=now, status=legacy_status)
        fill.completed = 1
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (fill.id, "600000.SH")
        )
        assert checkpoint is not None
        checkpoint.status = "done"
        checkpoint.payload_json = None
        checkpoint.payload_digest = None
        session.commit()
        legacy_id = fill.id
    engine.dispose()

    for _ in range(2):
        engine = init_db(db_path)
        with create_session_factory(engine)() as session:
            legacy = session.get(DiscoveryDataFill, legacy_id)
            assert legacy is not None
            assert legacy.status == legacy_status
            assert legacy.snapshot_status == "ready"
            assert legacy.last_error is None
        engine.dispose()

    engine = init_db(db_path)
    with create_session_factory(engine)() as session:
        replacement, dispatch = start_discovery_data_fill(session, now=now)
        assert replacement is not None and replacement.id != legacy_id
        assert dispatch is True
        legacy = session.get(DiscoveryDataFill, legacy_id)
        assert legacy is not None
        assert legacy.status == (
            "snapshot_unavailable"
            if legacy_status == "paused"
            else "done"
        )
    engine.dispose()


def test_start_replaces_active_fill_with_incomplete_done_payload(tmp_path) -> None:
    from jobs.discovery_data import start_discovery_data_fill

    engine = init_db(tmp_path / "active-incomplete-payload.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with factory() as session:
        legacy = _fill(
            session, ["600000.SH"], now=now, status="paused"
        )
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (legacy.id, "600000.SH")
        )
        assert checkpoint is not None
        checkpoint.status = "done"
        legacy.completed = 1
        session.commit()
        legacy_id = legacy.id

        replacement, dispatch = start_discovery_data_fill(session, now=now)
        assert replacement is not None and replacement.id != legacy_id
        assert dispatch is True
        legacy = session.get(DiscoveryDataFill, legacy_id)
        assert legacy is not None
        assert legacy.status == "snapshot_unavailable"
    engine.dispose()


def test_done_checkpoint_payload_and_digest_are_database_immutable(
    tmp_path,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from discovery.company_payload import (
        company_payload_digest,
        serialize_company_payload,
    )

    db_path = tmp_path / "immutable-checkpoint-payload.db"
    engine = init_db(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_discovery_checkpoint_payload_immutable"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER "
                "trg_discovery_checkpoint_payload_immutable "
                "BEFORE UPDATE OF payload_json, payload_digest "
                "ON discovery_data_checkpoints "
                "WHEN OLD.status = 'done' "
                "AND OLD.payload_json IS NOT NULL "
                "AND OLD.payload_digest IS NOT NULL "
                "AND (NEW.payload_json IS NOT OLD.payload_json "
                "OR NEW.payload_digest IS NOT OLD.payload_digest) "
                "BEGIN SELECT RAISE(ABORT, "
                "'completed discovery payload is immutable'); END"
            )
        )
    engine.dispose()
    for _ in range(2):
        engine = init_db(db_path)
        engine.dispose()
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "600000.SH"
    with factory() as session:
        fill_id = _fill(session, [code], now=now).id
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        assert checkpoint is not None
        checkpoint.payload_json = serialize_company_payload(
            _bundle(code), captured_at=now
        )
        checkpoint.payload_digest = company_payload_digest(
            checkpoint.payload_json
        )
        checkpoint.status = "done"
        session.commit()
        original_payload = checkpoint.payload_json
        original_digest = checkpoint.payload_digest

        checkpoint.error = "must roll back"
        checkpoint.payload_json = f"{original_payload} "
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()

        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                text(
                    "UPDATE discovery_data_checkpoints "
                    "SET payload_digest = :digest "
                    "WHERE fill_id = :fill_id AND code = :code"
                ),
                {
                    "digest": "0" * 64,
                    "fill_id": fill_id,
                    "code": code,
                },
            )
        session.rollback()

        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                text(
                    "UPDATE discovery_data_checkpoints SET status = 'running' "
                    "WHERE fill_id = :fill_id AND code = :code"
                ),
                {"fill_id": fill_id, "code": code},
            )
        session.rollback()

        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                text(
                    "UPDATE discovery_data_checkpoints "
                    "SET status = 'running', payload_json = :payload "
                    "WHERE fill_id = :fill_id AND code = :code"
                ),
                {
                    "payload": f"{original_payload} ",
                    "fill_id": fill_id,
                    "code": code,
                },
            )
        session.rollback()

        session.execute(
            text(
                "UPDATE discovery_data_checkpoints SET payload_json = payload_json "
                "WHERE fill_id = :fill_id AND code = :code"
            ),
            {"fill_id": fill_id, "code": code},
        )
        session.commit()

    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        assert checkpoint is not None
        assert checkpoint.payload_json == original_payload
        assert checkpoint.payload_digest == original_digest
        assert checkpoint.status == "done"
        assert checkpoint.error is None
    engine.dispose()


@pytest.mark.parametrize("sealed_field", ["payload_json", "payload_digest"])
def test_partial_checkpoint_identity_cannot_be_rewritten(
    tmp_path, sealed_field: str
) -> None:
    from sqlalchemy.exc import IntegrityError

    engine = init_db(tmp_path / f"partial-seal-{sealed_field}.db")
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        fill = _fill(session, [], now=now)
        checkpoint = DiscoveryDataCheckpoint(
            fill_id=fill.id,
            code="600000.SH",
            status="running",
            attempts=1,
            next_retry_at=None,
            valuation_points=0,
            error=None,
            payload_json="{}" if sealed_field == "payload_json" else None,
            payload_digest="0" * 64 if sealed_field == "payload_digest" else None,
            updated_at=now,
        )
        session.add(checkpoint)
        session.commit()
        setattr(checkpoint, sealed_field, "1" * 64)
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()
    engine.dispose()


def _snapshot_security(
    fill_id: int,
    code: str,
    now: datetime,
) -> DiscoveryUniverseSecurity:
    state = _security(code)
    return DiscoveryUniverseSecurity(
        fill_id=fill_id,
        code=state.code,
        name=state.name,
        industry=state.industry,
        exchange=state.exchange,
        listing_date=state.listing_date,
        is_st=state.is_st,
        is_delisting=state.is_delisting,
        trading_status=state.trading_status,
        is_suspended=state.trading_status == "suspended",
        last_trade_date=state.last_trade_date,
        industry_version=state.industry_version,
        source_as_of=state.industry_as_of,
        provenance=state.source,
        market_status_code=state.market_status_code,
        snapshot_at=now,
    )


def _fill(
    session: Session,
    codes: list[str],
    *,
    now: datetime,
    status: str = "running",
    daily_budget: int = 100,
    with_snapshot: bool = True,
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
        snapshot_status=None,
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
    if codes and with_snapshot:
        session.add_all(
            [
                _snapshot_security(fill.id, code, now)
                for code in codes
            ]
        )
        session.add_all(
            [
                DiscoveryFillTradingDate(
                    fill_id=fill.id, trade_date=trade_date
                )
                for trade_date in (
                    "2026-09-09",
                    "2026-09-10",
                    "2026-09-11",
                    "2026-09-14",
                    "2026-09-15",
                    "2026-09-16",
                )
            ]
        )
        session.flush()
        fill.snapshot_status = "ready"
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
        assert fill.payload_schema_version == SCHEMA_VERSION
        fill_id = fill.id
        done = session.get(DiscoveryDataCheckpoint, (fill_id, done_code))
        retry = session.get(DiscoveryDataCheckpoint, (fill_id, retry_code))
        assert done is not None and retry is not None
        _complete_checkpoint_payload(done, now)
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
        assert fill.payload_schema_version == SCHEMA_VERSION
        assert (fill.status, fill.completed, fill.failed) == ("done", 3, 0)
    engine.dispose()


@pytest.mark.parametrize(
    "snapshot_codes",
    [
        ("000001.SZ",),
        ("000001.SZ", "000002.SZ", "000003.SZ"),
        ("000001.SZ", "000003.SZ"),
    ],
    ids=("missing", "extra", "changed"),
)
def test_resumable_fill_rejects_checkpoint_snapshot_mismatch(
    tmp_path,
    snapshot_codes: tuple[str, ...],
) -> None:
    from discovery.data_readiness import discovery_data_readiness
    from jobs.discovery_data import (
        RequestPolicy,
        discovery_data_status,
        execute_discovery_data_fill,
        start_discovery_data_fill,
    )

    engine = init_db(tmp_path / f"snapshot-mismatch-{len(snapshot_codes)}.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    checkpoint_codes = ("000001.SZ", "000002.SZ")
    with factory() as session:
        fill = _fill(
            session,
            list(checkpoint_codes),
            now=now,
            status="paused",
            with_snapshot=False,
        )
        fill_id = fill.id
        done = session.get(
            DiscoveryDataCheckpoint, (fill_id, checkpoint_codes[0])
        )
        assert done is not None
        _complete_checkpoint_payload(done, now)
        done.attempts = 2
        done.valuation_points = 60
        fill.completed = 1
        session.add_all(
            [
                _snapshot_security(fill_id, code, now)
                for code in snapshot_codes
            ]
        )
        session.add_all(
            [
                DiscoveryFillTradingDate(
                    fill_id=fill_id, trade_date=trade_date
                )
                for trade_date in ("2026-09-15", "2026-09-16")
            ]
        )
        session.flush()
        fill.snapshot_status = "ready"
        session.commit()

    market = FakeMarketClient(
        universe=[_readiness_row(code) for code in snapshot_codes],
        trading_dates=["2026-09-15", "2026-09-16"],
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            market,
            fill_id,
            policy=RequestPolicy(requests_per_second=100),
            clock=FakeClock(now),
        )
    assert market.calls == []
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        done = session.get(
            DiscoveryDataCheckpoint, (fill_id, checkpoint_codes[0])
        )
        pending = session.get(
            DiscoveryDataCheckpoint, (fill_id, checkpoint_codes[1])
        )
        assert fill is not None and done is not None and pending is not None
        assert fill.status == "snapshot_unavailable"
        assert fill.snapshot_status == "unavailable"
        assert fill.last_error and "new fill" in fill.last_error
        assert (done.status, done.attempts, done.valuation_points) == (
            "done",
            2,
            60,
        )
        assert (pending.status, pending.attempts) == ("pending", 0)
        status = discovery_data_status(session, now=now)
        assert status["restart_required"] is True
        assert "new fill" in str(status["guidance"])
        assert discovery_data_readiness(
            session, as_of=now.date()
        ).observations == {}
        replacement, dispatch = start_discovery_data_fill(
            session, now=now + timedelta(seconds=1)
        )
        assert replacement is not None and dispatch
        assert replacement.id != fill_id
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
    )
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=clock
        )
    assert clock.sleeps == [0.5, 0.5, 0.5, 0.5]
    assert [call[1] for call in market.calls if call[0] == "discovery_data"] == codes[:2]
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        untouched = session.get(DiscoveryDataCheckpoint, (fill_id, codes[2]))
        assert fill is not None and untouched is not None
        assert (fill.status, fill.daily_requests, fill.completed) == ("paused", 4, 2)
        assert fill.last_error == "daily request budget reached"
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
        assert fill.last_error is None
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
        def fetch_discovery_data(
            self, code: str, *, before_request=None
        ) -> DiscoveryCompanyData:
            result = super().fetch_discovery_data(
                code, before_request=before_request
            )
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
        assert fill.last_error == "maximum run time reached"
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
    from market.universe import UniverseRow

    code = "000001.SZ"
    market = FakeMarketClient(
        universe=[_complete_universe_row(code)],
        trading_dates=["2026-09-15", "2026-09-16"],
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
    from market.universe import UniverseRow

    code = "000001.SZ"
    entered = Event()
    release_fetch = Event()

    class BlockingMarket(FakeMarketClient):
        def fetch_discovery_data(self, code, *, before_request=None):
            entered.set()
            assert release_fetch.wait(30)
            return super().fetch_discovery_data(
                code,
                before_request=before_request,
            )

    market = BlockingMarket(
        universe=[_complete_universe_row(code)],
        trading_dates=["2026-09-15", "2026-09-16"],
        discovery_data={code: _bundle(code)},
    )
    with _discovery_client(tmp_path, market, allow_sync=False) as client:
        first = client.post("/api/discovery-data/fill", json={})
        assert first.status_code == 200
        assert entered.wait(5)
        try:
            second = client.post("/api/discovery-data/fill", json={})
            assert second.status_code == 200
            assert second.json()["id"] == first.json()["id"]
            with client.app.state.session_factory() as session:
                assert session.query(DiscoveryDataFill).count() == 1
        finally:
            release_fetch.set()
        for _ in range(400):
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


def test_startup_resumes_interrupted_discovery_fill_and_preserves_done(
    tmp_path,
) -> None:
    import time
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
        _complete_checkpoint_payload(done, now)
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
        deadline = time.monotonic() + 10
        status = client.get("/api/discovery-data/status").json()
        while status["status"] != "done" and time.monotonic() < deadline:
            time.sleep(0.05)
            status = client.get("/api/discovery-data/status").json()
        assert status["id"] == fill_id
        assert (status["status"], status["completed"]) == ("done", 2)
        with client.app.state.session_factory() as session:
            assert session.get(JobLock, 1).job_id is None
        assert [
            call for call in market.calls if call[0] == "discovery_data"
        ] == [("discovery_data", "000001.SZ")]


def test_paused_fill_resumes_only_when_it_can_progress_unattended(
    tmp_path,
) -> None:
    from jobs.discovery_data import fill_resume_delay

    engine = init_db(tmp_path / "resume.db")
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        fill = _fill(session, ["000001.SZ"], now=now, status="paused")
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill.id, "000001.SZ"))
        assert checkpoint is not None

        def delay(error: str | None) -> float | None:
            fill.last_error = error
            session.flush()
            return fill_resume_delay(session, fill.id, now)

        assert delay("maximum run time reached") == 0.0
        assert delay("进程重启，任务已暂停") == 0.0
        assert delay("universe: boom") is None
        fill.daily_requests = fill.daily_budget
        assert delay("maximum run time reached") is None
        fill.daily_requests = 0

        checkpoint.status = "retryable"
        checkpoint.next_retry_at = now + timedelta(minutes=5)
        assert delay("000001.SZ financials: timeout") == 300.0
        checkpoint.status = "failed"
        assert delay("000001.SZ financials: timeout") is None
        fill.status = "done"
        assert delay("maximum run time reached") is None
    engine.dispose()


def test_startup_resets_running_checkpoint_when_fill_already_paused(
    tmp_path,
) -> None:
    db_path = tmp_path / "app.db"
    engine = init_db(db_path)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        fill = _fill(
            session, ["000001.SZ"], now=now, status="paused"
        )
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (fill.id, "000001.SZ")
        )
        assert checkpoint is not None
        checkpoint.status = "running"
        fill.last_error = "daily request budget reached"
        session.commit()
        fill_id = fill.id
    engine.dispose()

    with _discovery_client(tmp_path, FakeMarketClient()) as client:
        assert client.get("/api/discovery-data/status").json()["status"] == "paused"
        with client.app.state.session_factory() as session:
            checkpoint = session.get(
                DiscoveryDataCheckpoint, (fill_id, "000001.SZ")
            )
            assert checkpoint is not None
            assert checkpoint.status == "pending"


def test_request_deadline_stops_mid_bundle_before_second_transport(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "deadline.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill_id = _fill(session, [code], now=now).id
    clock = FakeClock(now)
    transports: list[datetime] = []

    class MidBundleMarket(FakeMarketClient):
        def fetch_discovery_data(
            self, code: str, *, before_request=None
        ) -> DiscoveryCompanyData:
            assert before_request is not None
            before_request()
            transports.append(clock.now())
            before_request()
            transports.append(clock.now())
            return _bundle(code, request_count=2)

    policy = RequestPolicy(
        requests_per_second=0.5,
        max_attempts=2,
        daily_budget=10,
        max_runtime_seconds=1,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            MidBundleMarket(),
            fill_id,
            policy=policy,
            clock=clock,
        )
    assert transports == [now]
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (fill_id, code)
        )
        assert fill is not None and checkpoint is not None
        assert (fill.status, fill.request_count) == ("paused", 1)
        assert fill.last_request_at == now
        assert checkpoint.status == "pending"
    engine.dispose()


def test_immediate_resume_waits_from_persisted_last_request(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "resume-pacing.db")
    factory = create_session_factory(engine)
    first_request = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    resumed_at = first_request + timedelta(seconds=0.5)
    code = "000001.SZ"
    with factory() as session:
        fill = _fill(
            session, [code], now=first_request, status="paused"
        )
        fill.last_request_at = first_request
        session.commit()
        fill_id = fill.id
    clock = FakeClock(resumed_at)
    transport_times: list[datetime] = []

    class ResumeMarket(FakeMarketClient):
        def fetch_discovery_data(
            self, code: str, *, before_request=None
        ) -> DiscoveryCompanyData:
            assert before_request is not None
            before_request()
            transport_times.append(clock.now())
            return _bundle(code)

    policy = RequestPolicy(
        requests_per_second=0.5,
        max_attempts=2,
        daily_budget=10,
        max_runtime_seconds=10,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            ResumeMarket(),
            fill_id,
            policy=policy,
            clock=clock,
        )
    assert clock.sleeps == [1.5]
    assert transport_times == [first_request + timedelta(seconds=2)]
    engine.dispose()


def test_discovery_bundle_reuses_verified_security_without_security_http(
    monkeypatch,
) -> None:
    state = replace(_security("000001.SZ"), industry="银行")
    callbacks = 0
    urls: list[str] = []

    def before_request() -> None:
        nonlocal callbacks
        callbacks += 1

    def get(url: str):
        urls.append(url)
        assert "stock/get" not in url
        assert "stock/kline/get" not in url
        if "RPT_VALUEANALYSIS_DET" in url:
            return _HttpResponse(
                {
                    "success": True,
                    "result": {
                        "pages": 1,
                        "data": [
                            {
                                "TRADE_DATE": "2026-09-16",
                                "PE_TTM": "10",
                                "PB_MRQ": "1",
                            },
                            {
                                "TRADE_DATE": "2020-01-01",
                                "PE_TTM": "9",
                                "PB_MRQ": "0.9",
                            },
                        ],
                    },
                }
            )
        return _HttpResponse(
            {
                "success": True,
                "result": {
                    "data": [
                        {
                            "REPORT_TYPE": "年报",
                            "REPORT_YEAR": "2025",
                            "REPORT_DATE": "2025-12-31",
                            "DATE_TYPE_CODE": "001",
                            "NOTICE_DATE": "2026-03-31",
                            "TOTAL_OPERATE_INCOME": "100",
                        }
                    ]
                },
            }
        )

    _install_http_transport(monkeypatch, get)
    result = EastMoneyClient().fetch_discovery_data(
        state.code,
        security_state=state,
        before_request=before_request,
    )

    assert result.security_state is state
    assert result.source_as_of.security == state.industry_as_of
    assert result.request_count == callbacks == len(urls) == 6
    assert any("MAINFINADATA" in url for url in urls)
    assert any("RPT_VALUEANALYSIS_DET" in url for url in urls)
    assert not any("RPT_F10_FINANCE_GBALANCE" in url for url in urls)
    assert not any("RPT_F10_FINANCE_GCASHFLOW" in url for url in urls)


def test_discovery_bundle_rejects_invalid_supplied_security_without_http() -> None:
    invalid = replace(_security("000001.SZ"), exchange="SH")
    client = EastMoneyClient()

    with pytest.raises(DiscoveryCompanyError, match="exchange") as caught:
        client.fetch_discovery_data(
            invalid.code,
            security_state=invalid,
            before_request=lambda: pytest.fail("must not request"),
        )

    assert caught.value.stage == "security_state"
    assert caught.value.request_count == client.request_count == 0


def test_worker_persists_exact_fill_security_and_supports_legacy_client(
    tmp_path,
) -> None:
    from discovery.company_payload import parse_company_payload
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "frozen-security.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill = _fill(session, [code], now=now, with_snapshot=False)
        fill_id = fill.id
        snapshot = _snapshot_security(fill_id, code, now)
        snapshot.name = "冻结名称"
        snapshot.industry = "银行"
        session.add(snapshot)
        session.add(
            DiscoveryFillTradingDate(
                fill_id=fill_id, trade_date="2026-09-16"
            )
        )
        session.flush()
        fill.snapshot_status = "ready"
        session.commit()

    market = FakeMarketClient(
        discovery_data={code: _bundle(code, request_count=1)}
    )
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=1,
        daily_budget=10,
        max_runtime_seconds=60,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session, market, fill_id, policy=policy, clock=FakeClock(now)
        )
    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        assert checkpoint is not None and checkpoint.payload_json is not None
        payload = parse_company_payload(
            checkpoint.payload_json, expected_code=code
        )
        assert payload["security_state"]["name"] == "冻结名称"
        assert payload["security_state"]["industry"] == "银行"
        assert len(market.discovery_security_states) == 1
        supplied = market.discovery_security_states[0]
        assert supplied is not None
        assert (supplied.name, supplied.industry) == ("冻结名称", "银行")

    class LegacyMarket:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_discovery_data(
            self, requested: str, *, before_request=None
        ) -> DiscoveryCompanyData:
            self.calls += 1
            assert before_request is not None
            before_request()
            return _bundle(requested)

    with factory() as session:
        second_fill = _fill(session, ["000002.SZ"], now=now)
        second_id = second_fill.id
    legacy = LegacyMarket()
    with factory() as session:
        execute_discovery_data_fill(
            session, legacy, second_id, policy=policy, clock=FakeClock(now)
        )
    assert legacy.calls == 1
    engine.dispose()


def test_worker_rejects_frozen_security_mutation_race(tmp_path) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "security-race.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill_id = _fill(session, [code], now=now).id

    class MutatingMarket:
        def fetch_discovery_data(
            self,
            requested: str,
            *,
            before_request=None,
            security_state=None,
        ) -> DiscoveryCompanyData:
            assert security_state is not None
            with factory() as other:
                row = other.get(
                    DiscoveryUniverseSecurity, (fill_id, requested)
                )
                assert row is not None
                row.industry = "被篡改"
                other.commit()
            return replace(
                _bundle(requested, request_count=0),
                security_state=security_state,
                source_as_of=replace(
                    _bundle(requested).source_as_of,
                    security=security_state.industry_as_of,
                ),
            )

    with factory() as session:
        execute_discovery_data_fill(
            session,
            MutatingMarket(),
            fill_id,
            policy=RequestPolicy(
                requests_per_second=100,
                max_attempts=1,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            clock=FakeClock(now),
        )
    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        assert checkpoint is not None
        assert checkpoint.status == "failed"
        assert checkpoint.error and "immutable" in checkpoint.error
        assert session.get(Financial, (code, "2025", "annual")) is None
    engine.dispose()


def test_worker_keeps_frozen_security_fill_scoped_and_does_not_retry_typeerror(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "security-fill-scope.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    fill_ids: list[int] = []
    for name in ("第一批", "第二批"):
        with factory() as session:
            fill = _fill(
                session, [code], now=now, with_snapshot=False
            )
            row = _snapshot_security(fill.id, code, now)
            row.name = name
            session.add(row)
            session.add(
                DiscoveryFillTradingDate(
                    fill_id=fill.id, trade_date="2026-09-16"
                )
            )
            session.flush()
            fill.snapshot_status = "ready"
            session.commit()
            fill_ids.append(fill.id)

    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=1,
        daily_budget=10,
        max_runtime_seconds=60,
    )
    market = FakeMarketClient(
        discovery_data={code: _bundle(code, request_count=0)}
    )
    for fill_id in fill_ids:
        with factory() as session:
            execute_discovery_data_fill(
                session,
                market,
                fill_id,
                policy=policy,
                clock=FakeClock(now),
            )
    assert [
        state.name
        for state in market.discovery_security_states
        if state is not None
    ] == ["第一批", "第二批"]

    class TypeErrorMarket:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_discovery_data(
            self,
            requested: str,
            *,
            before_request=None,
            security_state=None,
        ) -> DiscoveryCompanyData:
            self.calls += 1
            assert security_state is not None
            raise TypeError("raised inside market client")

    with factory() as session:
        bad_fill_id = _fill(session, ["000002.SZ"], now=now).id
    bad = TypeErrorMarket()
    with factory() as session:
        execute_discovery_data_fill(
            session,
            bad,
            bad_fill_id,
            policy=policy,
            clock=FakeClock(now),
        )
    with factory() as session:
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (bad_fill_id, "000002.SZ")
        )
        assert checkpoint is not None
        assert checkpoint.status == "failed"
        assert checkpoint.error and "raised inside market client" in checkpoint.error
    assert bad.calls == 1
    engine.dispose()


def test_ready_universe_security_is_database_immutable_and_fill_append_only(
    tmp_path,
) -> None:
    engine = init_db(tmp_path / "sealed-universe.db")
    init_db(tmp_path / "sealed-universe.db").dispose()
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with Session(engine) as session:
        fill = _fill(
            session, [code], now=now, with_snapshot=False
        )
        row = _snapshot_security(fill.id, code, now)
        session.add(row)
        session.commit()

        row.name = "初始化可改"
        session.commit()
        fill.snapshot_status = "ready"
        session.commit()
        fill_id = fill.id

    with Session(engine) as session:
        row = session.get(DiscoveryUniverseSecurity, (fill_id, code))
        assert row is not None
        row.name = "ORM 篡改"
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()
        row = session.get(DiscoveryUniverseSecurity, (fill_id, code))
        assert row is not None
        session.delete(row)
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()
        session.add(_snapshot_security(fill_id, "000002.SZ", now))
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()

    updates = {
        "fill_id": "fill_id + 1000",
        "code": "'000002.SZ'",
        "name": "'篡改'",
        "industry": "'篡改行业'",
        "exchange": "'SH'",
        "listing_date": "'2002-01-01'",
        "is_st": "1",
        "is_delisting": "1",
        "trading_status": "'suspended'",
        "is_suspended": "1",
        "last_trade_date": "'2026-09-15'",
        "industry_version": "'tampered:v2'",
        "source_as_of": "'2026-09-15'",
        "provenance": "'tampered'",
        "market_status_code": "99",
        "snapshot_at": "'2026-09-15 00:00:00'",
    }
    for column, value in updates.items():
        with pytest.raises(IntegrityError, match="immutable"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE discovery_universe_securities "
                        f"SET {column} = {value} "
                        "WHERE fill_id = :fill_id AND code = :code"
                    ),
                    {"fill_id": fill_id, "code": code},
                )

    with pytest.raises(IntegrityError, match="immutable"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM discovery_universe_securities "
                    "WHERE fill_id = :fill_id AND code = :code"
                ),
                {"fill_id": fill_id, "code": code},
            )
    with pytest.raises(IntegrityError, match="append-only"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM discovery_data_fills WHERE id = :fill_id"
                ),
                {"fill_id": fill_id},
            )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE discovery_data_fills "
                "SET snapshot_status = 'unavailable' WHERE id = :fill_id"
            ),
            {"fill_id": fill_id},
        )
    with pytest.raises(IntegrityError, match="immutable"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE discovery_universe_securities SET name = 'still sealed' "
                    "WHERE fill_id = :fill_id AND code = :code"
                ),
                {"fill_id": fill_id, "code": code},
            )

    with Session(engine) as session:
        pending = _fill(
            session, ["000002.SZ"], now=now, with_snapshot=False
        )
        session.add(_snapshot_security(pending.id, "000002.SZ", now))
        session.commit()
        pending_id = pending.id
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM discovery_data_fills WHERE id = :fill_id"),
            {"fill_id": pending_id},
        )
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM discovery_universe_securities "
                "WHERE fill_id = :fill_id"
            ),
            {"fill_id": pending_id},
        ).scalar_one() == 0
    engine.dispose()


def test_universe_security_update_seal_checks_old_and_new_parent(
    tmp_path,
) -> None:
    engine = init_db(tmp_path / "universe-move-seal.db")
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        pending_a = _fill(session, [], now=now)
        pending_b = _fill(session, [], now=now)
        pending_c = _fill(session, [], now=now)
        ready_a = _fill(session, ["000001.SZ"], now=now)
        ready_b = _fill(session, ["000002.SZ"], now=now)
        unavailable = _fill(session, ["000003.SZ"], now=now)
        unavailable.snapshot_status = "unavailable"
        session.commit()
        session.add_all(
            [
                _snapshot_security(
                    pending_a.id, "000010.SZ", now
                ),
                _snapshot_security(
                    pending_c.id, "000020.SZ", now
                ),
                _snapshot_security(
                    pending_c.id, "000030.SZ", now
                ),
            ]
        )
        session.commit()
        ids = {
            "pending_a": pending_a.id,
            "pending_b": pending_b.id,
            "pending_c": pending_c.id,
            "ready_a": ready_a.id,
            "ready_b": ready_b.id,
            "unavailable": unavailable.id,
        }

    # Pending -> pending, including PK/code changes, is initialization work.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE discovery_universe_securities "
                "SET fill_id = :new_fill, code = '000011.SZ' "
                "WHERE fill_id = :old_fill AND code = '000010.SZ'"
            ),
            {
                "old_fill": ids["pending_a"],
                "new_fill": ids["pending_b"],
            },
        )

    moves = (
        # pending -> ready
        (ids["pending_c"], "000020.SZ", ids["ready_a"]),
        # ready -> pending
        (ids["ready_a"], "000001.SZ", ids["pending_a"]),
        # ready -> ready, different fill
        (ids["ready_b"], "000002.SZ", ids["ready_a"]),
        # pending -> immutable terminal snapshot
        (ids["pending_c"], "000030.SZ", ids["unavailable"]),
        # immutable terminal snapshot -> pending
        (ids["unavailable"], "000003.SZ", ids["pending_a"]),
    )
    for old_fill, code, new_fill in moves:
        with pytest.raises(IntegrityError, match="immutable"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE discovery_universe_securities "
                        "SET fill_id = :new_fill "
                        "WHERE fill_id = :old_fill AND code = :code"
                    ),
                    {
                        "old_fill": old_fill,
                        "new_fill": new_fill,
                        "code": code,
                    },
                )

    # Missing parents are still rejected by the FK, not treated as mutable.
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE discovery_universe_securities "
                    "SET fill_id = 999999 "
                    "WHERE fill_id = :fill_id AND code = '000011.SZ'"
                ),
                {"fill_id": ids["pending_b"]},
            )
    engine.dispose()


def test_upgrade_seals_existing_ready_snapshot_without_rewriting_it(
    tmp_path,
) -> None:
    db_path = tmp_path / "upgrade-sealed-universe.db"
    engine = init_db(db_path)
    trigger_names = (
        "trg_discovery_universe_security_insert_immutable",
        "trg_discovery_universe_security_immutable",
        "trg_discovery_universe_security_delete_immutable",
        "trg_discovery_ready_fill_snapshot_immutable",
        "trg_discovery_ready_fill_append_only",
    )
    with engine.begin() as conn:
        for name in trigger_names:
            conn.execute(text(f'DROP TRIGGER IF EXISTS "{name}"'))
        conn.execute(
            text(
                "CREATE TRIGGER "
                "trg_discovery_universe_security_immutable "
                "BEFORE UPDATE ON discovery_universe_securities "
                "WHEN EXISTS ("
                "SELECT 1 FROM discovery_data_fills AS fill "
                "WHERE fill.id = OLD.fill_id "
                "AND fill.snapshot_status IN ('ready', 'unavailable')"
                ") BEGIN SELECT RAISE(ABORT, "
                "'ready discovery universe security is immutable'); END"
            )
        )
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        fill = _fill(session, ["000001.SZ"], now=now)
        fill_id = fill.id
    engine.dispose()

    init_db(db_path).dispose()
    engine = init_db(db_path)
    with Session(engine) as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        row = session.get(
            DiscoveryUniverseSecurity, (fill_id, "000001.SZ")
        )
        assert fill is not None and row is not None
        assert fill.snapshot_status == "ready"
        assert row.name == "000001.SZ"
    with engine.connect() as conn:
        trigger_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = "
                "'trg_discovery_universe_security_immutable'"
            )
        ).scalar_one()
        assert "NEW.fill_id" in trigger_sql
    with pytest.raises(IntegrityError, match="immutable"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE discovery_universe_securities SET name = 'changed' "
                    "WHERE fill_id = :fill_id"
                ),
                {"fill_id": fill_id},
            )
    engine.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("industry", ""),
        ("exchange", None),
        ("listing_date", None),
        ("is_st", None),
        ("is_delisting", None),
        ("trading_status", None),
        ("last_trade_date", None),
        ("industry_version", None),
        ("source_as_of", None),
        ("provenance", None),
        ("is_suspended", None),
    ],
)
def test_incomplete_frozen_security_fails_permanently_without_requests(
    tmp_path,
    field: str,
    value: object,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(
        tmp_path / f"incomplete-frozen-security-{field}.db"
    )
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill = _fill(session, [code], now=now, with_snapshot=False)
        row = _snapshot_security(fill.id, code, now)
        setattr(row, field, value)
        session.add(row)
        session.add(
            DiscoveryFillTradingDate(
                fill_id=fill.id, trade_date="2026-09-16"
            )
        )
        session.flush()
        fill.snapshot_status = "ready"
        session.commit()
        fill_id = fill.id

    market = FakeMarketClient(
        discovery_data={code: _bundle(code, request_count=1)}
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            market,
            fill_id,
            policy=RequestPolicy(
                requests_per_second=100,
                max_attempts=3,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            clock=FakeClock(now),
        )
    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        fill = session.get(DiscoveryDataFill, fill_id)
        assert checkpoint is not None and fill is not None
        assert checkpoint.status == "failed"
        assert checkpoint.attempts == 1
        assert checkpoint.payload_json is None
        assert checkpoint.error and "security_snapshot_incomplete" in checkpoint.error
        assert (fill.total, fill.failed, fill.request_count) == (1, 1, 0)
        assert session.get(Financial, (code, "2025", "annual")) is None
    assert market.calls == []
    engine.dispose()


def test_legacy_client_security_mismatch_is_permanent_and_not_persisted(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "legacy-security-mismatch.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill_id = _fill(session, [code], now=now).id

    class LegacyMismatch:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_discovery_data(
            self, requested: str, *, before_request=None
        ) -> DiscoveryCompanyData:
            self.calls += 1
            assert before_request is not None
            before_request()
            return replace(
                _bundle(requested),
                security_state=replace(
                    _security(requested), industry="实时行业"
                ),
            )

    market = LegacyMismatch()
    with factory() as session:
        execute_discovery_data_fill(
            session,
            market,
            fill_id,
            policy=RequestPolicy(
                requests_per_second=100,
                max_attempts=3,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            clock=FakeClock(now),
        )
    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        fill = session.get(DiscoveryDataFill, fill_id)
        assert checkpoint is not None and fill is not None
        assert checkpoint.status == "failed"
        assert checkpoint.attempts == 1
        assert checkpoint.payload_json is None
        assert checkpoint.error and "security_snapshot_mismatch" in checkpoint.error
        assert fill.request_count == 1
        assert session.get(Financial, (code, "2025", "annual")) is None
    assert market.calls == 1
    engine.dispose()


def test_fill_boundary_is_the_last_session_with_published_valuations() -> None:
    from jobs.discovery_data import _completed_session_date

    beijing = timezone(timedelta(hours=8))
    assert _completed_session_date(
        datetime(2026, 9, 22, 10, 17, tzinfo=beijing)
    ) == date(2026, 9, 21)
    assert _completed_session_date(
        datetime(2026, 9, 22, 15, 59, tzinfo=beijing)
    ) == date(2026, 9, 21)
    assert _completed_session_date(
        datetime(2026, 9, 22, 16, 0, tzinfo=beijing)
    ) == date(2026, 9, 22)
    assert _completed_session_date(
        datetime(2026, 9, 22, 23, 59, tzinfo=beijing)
    ) == date(2026, 9, 22)


def test_china_market_date_drives_universe_and_snapshot_cutoff(tmp_path) -> None:
    from discovery.constants import china_market_date
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    midnight = datetime(2026, 9, 19, 16, tzinfo=timezone.utc)
    early_morning = datetime(2026, 9, 19, 23, 59, tzinfo=timezone.utc)
    assert china_market_date(midnight) == date(2026, 9, 20)
    assert china_market_date(early_morning) == date(2026, 9, 20)

    engine = init_db(tmp_path / "china-date.db")
    factory = create_session_factory(engine)
    code = "000001.SZ"
    with factory() as session:
        fill_id = _fill(session, [], now=midnight).id
    state = replace(
        _security(code),
        last_trade_date="2026-09-20",
        industry_as_of="2026-09-20",
    )
    market = FakeMarketClient(
        universe=[
            UniverseRow(
                code=code,
                name=state.name,
                price=Decimal("10"),
                pe=Decimal("5"),
                pb=Decimal("1"),
                mcap=Decimal("100"),
                industry=state.industry,
                exchange=state.exchange,
                listing_date=state.listing_date,
                is_st=state.is_st,
                is_delisting=state.is_delisting,
                trading_status=state.trading_status,
                last_trade_date=state.last_trade_date,
                industry_version=state.industry_version,
                industry_as_of=state.industry_as_of,
                security_source=state.source,
            )
        ],
        trading_dates=["2026-09-20"],
        discovery_data={
            code: replace(
                _bundle(code, request_count=1),
                security_state=state,
                source_as_of=replace(
                    _bundle(code).source_as_of,
                    security=state.industry_as_of,
                ),
            )
        },
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            market,
            fill_id,
            policy=RequestPolicy(
                requests_per_second=100,
                max_attempts=1,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            clock=FakeClock(midnight),
        )
    assert market.universe_as_of == [date(2026, 9, 19)]
    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        assert checkpoint is not None and checkpoint.status == "done"

        future_code = "000002.SZ"
        future = _fill(
            session, [future_code], now=midnight, with_snapshot=False
        )
        row = _snapshot_security(future.id, future_code, midnight)
        row.last_trade_date = "2026-09-20"
        row.source_as_of = "2026-09-21"
        session.add(row)
        session.add(
            DiscoveryFillTradingDate(
                fill_id=future.id, trade_date="2026-09-20"
            )
        )
        session.flush()
        future.snapshot_status = "ready"
        session.commit()
        future_id = future.id
    future_market = FakeMarketClient(
        discovery_data={future_code: _bundle(future_code)}
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            future_market,
            future_id,
            policy=RequestPolicy(
                requests_per_second=100,
                max_attempts=3,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            clock=FakeClock(midnight),
        )
    with factory() as session:
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (future_id, future_code)
        )
        assert checkpoint is not None
        assert checkpoint.status == "failed"
        assert checkpoint.error and "fill cutoff" in checkpoint.error
    assert future_market.calls == []
    engine.dispose()


def test_legacy_universe_client_without_as_of_remains_compatible(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    engine = init_db(tmp_path / "legacy-universe-as-of.db")
    factory = create_session_factory(engine)
    with factory() as session:
        fill_id = _fill(session, [], now=now).id

    class LegacyUniverse(FakeMarketClient):
        def __init__(self) -> None:
            super().__init__(
                universe=[_complete_universe_row(code)],
                trading_dates=["2026-09-16"],
                discovery_data={code: _bundle(code)},
            )
            self.legacy_calls = 0

        def fetch_universe(
            self, *, before_request=None
        ) -> list[UniverseRow]:
            self.legacy_calls += 1
            return super().fetch_universe(
                before_request=before_request,
                as_of=date(2026, 9, 16),
            )

    market = LegacyUniverse()
    with factory() as session:
        execute_discovery_data_fill(
            session,
            market,
            fill_id,
            policy=RequestPolicy(
                requests_per_second=100,
                max_attempts=1,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            clock=FakeClock(now),
        )
    with factory() as session:
        checkpoint = session.get(DiscoveryDataCheckpoint, (fill_id, code))
        assert checkpoint is not None and checkpoint.status == "done"
    assert market.legacy_calls == 1
    engine.dispose()


def test_wrong_type_release_cannot_clear_colliding_job_id(tmp_path) -> None:
    from jobs.lock import (
        JOB_TYPE_FUNNEL,
        JOB_TYPE_REVIEW,
        ensure_lock_row,
        release,
    )

    engine = init_db(tmp_path / "typed-release.db")
    with Session(engine) as session:
        lock = ensure_lock_row(session)
        lock.job_id = 7
        lock.job_type = JOB_TYPE_REVIEW
        lock.held_at = datetime.now(timezone.utc)
        session.commit()

        release(session, 7, JOB_TYPE_FUNNEL)
        session.commit()
        assert session.get(JobLock, 1).job_type == JOB_TYPE_REVIEW

        release(session, 7, JOB_TYPE_REVIEW)
        session.commit()
        assert session.get(JobLock, 1).job_id is None
    engine.dispose()


@pytest.mark.parametrize("terminal_status", ["done", "failed"])
def test_start_discovery_fill_returns_terminal_fill_until_its_lock_is_released(
    tmp_path,
    terminal_status,
) -> None:
    from jobs.discovery_data import start_discovery_data_fill
    from jobs.lock import JOB_TYPE_DISCOVERY_DATA, ensure_lock_row

    engine = init_db(tmp_path / f"terminal-lock-{terminal_status}.db")
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        fill = _fill(session, [], now=now, status=terminal_status)
        fill_id = fill.id
        lock = ensure_lock_row(session)
        lock.job_id = fill_id
        lock.job_type = JOB_TYPE_DISCOVERY_DATA
        lock.held_at = now
        session.commit()

        existing, dispatch = start_discovery_data_fill(session, now=now)

        assert existing is not None
        assert existing.id == fill_id
        assert dispatch is False
        assert session.query(DiscoveryDataFill).count() == 1
    engine.dispose()


def test_discovery_fill_post_rejects_malformed_json_but_accepts_empty_and_object(
    tmp_path,
) -> None:
    market = FakeMarketClient()
    with _discovery_client(tmp_path, market) as client:
        malformed = client.post(
            "/api/discovery-data/fill",
            content=b"{",
            headers={"content-type": "application/json"},
        )
        assert malformed.status_code == 400
        assert malformed.json()["code"] == "INVALID_DISCOVERY_DATA_FILL"
        with client.app.state.session_factory() as session:
            assert session.query(DiscoveryDataFill).count() == 0

        empty = client.post("/api/discovery-data/fill", content=b"")
        assert empty.status_code == 200
        valid = client.post("/api/discovery-data/fill", json={})
        assert valid.status_code == 200


def test_successful_retry_clears_checkpoint_and_fill_errors(tmp_path) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "retry-clears-error.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill = _fill(session, [code], now=now, status="paused")
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (fill.id, code)
        )
        assert checkpoint is not None
        checkpoint.status = "retryable"
        checkpoint.attempts = 1
        checkpoint.next_retry_at = now
        checkpoint.error = "temporary"
        fill.last_error = "temporary"
        session.commit()
        fill_id = fill.id
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=2,
        daily_budget=10,
        max_runtime_seconds=60,
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            FakeMarketClient(discovery_data={code: _bundle(code)}),
            fill_id,
            policy=policy,
            clock=FakeClock(now),
        )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        checkpoint = session.get(
            DiscoveryDataCheckpoint, (fill_id, code)
        )
        assert fill is not None and checkpoint is not None
        assert fill.last_error is None
        assert checkpoint.error is None
        assert checkpoint.status == "done"
    engine.dispose()


def test_recovered_success_does_not_hide_other_terminal_failure(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "mixed-recovery.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    failed_code = "000001.SZ"
    recovered_code = "000002.SZ"
    with factory() as session:
        fill = _fill(
            session,
            [failed_code, recovered_code],
            now=now,
            status="paused",
        )
        for code in (failed_code, recovered_code):
            checkpoint = session.get(
                DiscoveryDataCheckpoint, (fill.id, code)
            )
            assert checkpoint is not None
            checkpoint.status = "retryable"
            checkpoint.attempts = 1
            checkpoint.next_retry_at = now
            checkpoint.error = f"old {code}"
        fill.last_error = "stale pause"
        session.commit()
        fill_id = fill.id
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=2,
        daily_budget=10,
        max_runtime_seconds=60,
    )
    market = FakeMarketClient(
        discovery_data={recovered_code: _bundle(recovered_code)},
        discovery_errors={failed_code: "permanent"},
    )
    with factory() as session:
        execute_discovery_data_fill(
            session,
            market,
            fill_id,
            policy=policy,
            clock=FakeClock(now),
        )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        failed = session.get(
            DiscoveryDataCheckpoint, (fill_id, failed_code)
        )
        recovered = session.get(
            DiscoveryDataCheckpoint, (fill_id, recovered_code)
        )
        assert fill is not None and failed is not None and recovered is not None
        assert failed.status == "failed"
        assert recovered.status == "done"
        assert fill.last_error == failed.error
        assert fill.last_error and failed_code in fill.last_error
    engine.dispose()


def test_first_attempt_success_clears_stale_pause_error_when_clean(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    engine = init_db(tmp_path / "clean-resume.db")
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    with factory() as session:
        fill = _fill(session, [code], now=now, status="paused")
        fill.last_error = "maximum run time reached"
        session.commit()
        fill_id = fill.id
    with factory() as session:
        execute_discovery_data_fill(
            session,
            FakeMarketClient(discovery_data={code: _bundle(code)}),
            fill_id,
            policy=RequestPolicy(
                requests_per_second=100,
                max_attempts=2,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            clock=FakeClock(now),
        )
    with factory() as session:
        fill = session.get(DiscoveryDataFill, fill_id)
        assert fill is not None
        assert fill.status == "done"
        assert fill.last_error is None
    engine.dispose()


def test_eta_ignores_completed_checkpoints_before_current_worker_run(
    tmp_path,
) -> None:
    from jobs.discovery_data import RequestPolicy, discovery_data_status

    engine = init_db(tmp_path / "eta-current-run.db")
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        fill = _fill(
            session,
            ["000001.SZ", "000002.SZ", "000003.SZ"],
            now=now,
            status="running",
        )
        fill.run_started_at = now
        first = session.get(
            DiscoveryDataCheckpoint, (fill.id, "000001.SZ")
        )
        second = session.get(
            DiscoveryDataCheckpoint, (fill.id, "000002.SZ")
        )
        assert first is not None and second is not None
        first.status = second.status = "done"
        first.updated_at = now - timedelta(days=1, seconds=10)
        second.updated_at = now
        fill.completed = 2
        session.commit()
        status = discovery_data_status(
            session,
            policy=RequestPolicy(
                requests_per_second=0.5,
                max_attempts=2,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            now=now,
        )
        assert status["eta_seconds"] is None
    engine.dispose()


def test_init_db_adds_discovery_request_timing_columns(tmp_path) -> None:
    from jobs.discovery_data import RequestPolicy, execute_discovery_data_fill

    db_path = tmp_path / "old-fill.db"
    old_engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    with old_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE discovery_data_fills (
                    id INTEGER NOT NULL PRIMARY KEY,
                    status VARCHAR NOT NULL,
                    total INTEGER NOT NULL,
                    completed INTEGER NOT NULL,
                    failed INTEGER NOT NULL,
                    request_count INTEGER NOT NULL,
                    daily_budget INTEGER NOT NULL,
                    budget_day VARCHAR,
                    daily_requests INTEGER NOT NULL,
                    started_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    last_error TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO discovery_data_fills (
                    id, status, total, completed, failed, request_count,
                    daily_budget, budget_day, daily_requests, started_at,
                    updated_at, last_error
                ) VALUES (
                    42, 'paused', 1, 0, 0, 3,
                    10, '2026-09-16', 3, '2026-09-16 12:00:00',
                    '2026-09-16 12:00:00', 'process stopped'
                )
                """
            )
        )
    old_engine.dispose()

    engine = init_db(db_path)
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("discovery_data_fills")
    }
    assert {
        "last_request_at",
        "run_started_at",
        "payload_schema_version",
    } <= columns
    factory = create_session_factory(engine)
    code = "000001.SZ"
    with factory() as session:
        fill = session.get(DiscoveryDataFill, 42)
        assert fill is not None
        assert fill.request_count == 3
        assert fill.last_request_at is None
        assert fill.payload_schema_version is None
        session.add(
            DiscoveryDataCheckpoint(
                fill_id=fill.id,
                code=code,
                status="pending",
                attempts=0,
                next_retry_at=None,
                valuation_points=0,
                error=None,
                updated_at=fill.updated_at,
            )
        )
        session.commit()
    now = datetime(2026, 9, 16, 13, tzinfo=timezone.utc)
    clock = FakeClock(now)
    transports: list[datetime] = []

    class MigratedFillMarket(FakeMarketClient):
        def fetch_discovery_data(
            self, code: str, *, before_request=None
        ) -> DiscoveryCompanyData:
            assert before_request is not None
            before_request()
            transports.append(clock.now())
            return _bundle(code)

    with factory() as session:
        execute_discovery_data_fill(
            session,
            MigratedFillMarket(),
            42,
            policy=RequestPolicy(
                requests_per_second=0.5,
                max_attempts=2,
                daily_budget=10,
                max_runtime_seconds=60,
            ),
            clock=clock,
        )
    assert clock.sleeps == []
    assert transports == []
    with factory() as session:
        fill = session.get(DiscoveryDataFill, 42)
        assert fill is not None
        assert fill.status == "snapshot_unavailable"
        assert fill.snapshot_status == "unavailable"
    engine.dispose()


def test_init_db_preserves_completed_presnapshot_fill_but_requires_restart(
    tmp_path,
) -> None:
    from discovery.data_readiness import discovery_data_readiness
    from jobs.discovery_data import (
        discovery_data_status,
        start_discovery_data_fill,
    )

    db_path = tmp_path / "completed-presnapshot-fill.db"
    old_engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    with old_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE discovery_data_fills (
                    id INTEGER NOT NULL PRIMARY KEY,
                    status VARCHAR NOT NULL,
                    total INTEGER NOT NULL,
                    completed INTEGER NOT NULL,
                    failed INTEGER NOT NULL,
                    request_count INTEGER NOT NULL,
                    daily_budget INTEGER NOT NULL,
                    budget_day VARCHAR,
                    daily_requests INTEGER NOT NULL,
                    started_at DATETIME NOT NULL,
                    run_started_at DATETIME,
                    last_request_at DATETIME,
                    updated_at DATETIME NOT NULL,
                    last_error TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE discovery_data_checkpoints (
                    fill_id INTEGER NOT NULL,
                    code VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_retry_at DATETIME,
                    valuation_points INTEGER NOT NULL,
                    error TEXT,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (fill_id, code)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO discovery_data_fills (
                    id, status, total, completed, failed, request_count,
                    daily_budget, budget_day, daily_requests, started_at,
                    updated_at
                ) VALUES (
                    42, 'done', 1, 1, 0, 10, 1000, '2026-09-16', 10,
                    '2026-09-16 12:00:00', '2026-09-16 13:00:00'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO discovery_data_checkpoints (
                    fill_id, code, status, attempts, valuation_points,
                    updated_at
                ) VALUES (
                    42, '000001.SZ', 'done', 2, 60,
                    '2026-09-16 13:00:00'
                )
                """
            )
        )
    old_engine.dispose()

    engine = init_db(db_path)
    now = datetime(2026, 9, 16, 14, tzinfo=timezone.utc)
    with Session(engine) as session:
        legacy = session.get(DiscoveryDataFill, 42)
        assert legacy is not None
        assert legacy.status == "done"
        assert legacy.snapshot_status is None
        status = discovery_data_status(session, now=now)
        assert status["status"] == "snapshot_unavailable"
        assert status["snapshot_status"] == "unavailable"
        assert status["restart_required"] is True
        assert "payload schema" in str(status["guidance"])
        assert discovery_data_readiness(
            session, as_of=now.date()
        ).observations == {}
        replacement, dispatch = start_discovery_data_fill(
            session, now=now
        )
        assert replacement is not None and dispatch
        assert replacement.id != legacy.id
        assert replacement.payload_schema_version == SCHEMA_VERSION
    engine.dispose()


def test_legacy_payload_schema_is_o1_unavailable_everywhere(
    tmp_path,
    monkeypatch,
) -> None:
    from discovery.company_payload import (
        PAYLOAD_SCHEMA_BLOCK_REASON,
        PAYLOAD_SCHEMA_RESTART_GUIDANCE,
    )
    from discovery.data_readiness import discovery_data_readiness
    from discovery.snapshot import build_production_snapshot
    from jobs import discovery_data
    from scripts.calibrate_discovery import calibrate

    db_path = tmp_path / "legacy-payload-schema.db"
    engine = init_db(db_path)
    now = datetime(2026, 9, 16, 14, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(
            DiscoveryDataFill(
                status="done",
                total=5559,
                completed=5559,
                failed=0,
                request_count=0,
                daily_budget=1000,
                budget_day="2026-09-16",
                daily_requests=0,
                started_at=now,
                updated_at=now,
                last_error=None,
                snapshot_status="ready",
                payload_schema_version=1,
            )
        )
        session.commit()

        def forbidden_parse(*_args, **_kwargs):
            raise AssertionError("status must not parse company payloads")

        monkeypatch.setattr(
            discovery_data,
            "parse_company_payload",
            forbidden_parse,
        )
        monkeypatch.setattr(
            discovery_data,
            "_checkpoint_counts",
            forbidden_parse,
        )
        status = discovery_data.discovery_data_status(session, now=now)
        assert status["status"] == "snapshot_unavailable"
        assert status["snapshot_status"] == "unavailable"
        assert status["restart_required"] is True
        assert status["guidance"] == PAYLOAD_SCHEMA_RESTART_GUIDANCE
        with pytest.raises(ValueError, match="payload schema"):
            build_production_snapshot(session)
        assert discovery_data_readiness(
            session,
            as_of=now.date(),
        ).observations == {}

    artifact = calibrate(db_path, track="quality_discount")
    assert artifact["blocked_reasons"] == [PAYLOAD_SCHEMA_BLOCK_REASON]
    with engine.begin() as conn:
        with pytest.raises(IntegrityError, match="payload schema is immutable"):
            conn.execute(
                text(
                    "UPDATE discovery_data_fills "
                    "SET payload_schema_version = 2"
                )
            )
    engine.dispose()


def _readiness_row(
    code: str,
    *,
    listing_date: str = "2000-01-01",
    industry: str = "白色家电",
) -> UniverseRow:
    return UniverseRow(
        code=code,
        name=code,
        price=Decimal("10"),
        pe=Decimal("10"),
        pb=Decimal("1"),
        mcap=Decimal("100"),
        industry=industry,
        exchange=code[-2:],
        listing_date=listing_date,
        is_st=False,
        is_delisting=False,
        trading_status="trading",
        last_trade_date="2026-09-16",
        industry_version="test:v1",
        industry_as_of="2026-09-16",
        security_source="fake",
        market_status_code=2,
    )


def _store_readiness_universe(
    session: Session,
    rows: list[UniverseRow],
    *,
    fetched_at: float = 1789560000.0,
    trading_dates: tuple[str, ...] = (
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
    ),
) -> None:
    stamp = datetime.fromtimestamp(fetched_at, timezone.utc)
    fill = DiscoveryDataFill(
        status="done",
        total=len(rows),
        completed=len(rows),
        failed=0,
        request_count=0,
        daily_budget=1000,
        budget_day=stamp.date().isoformat(),
        daily_requests=0,
        started_at=stamp,
        updated_at=stamp,
        last_error=None,
        snapshot_status=None,
    )
    session.add(fill)
    session.flush()
    session.add_all(
        [
            DiscoveryUniverseSecurity(
                fill_id=fill.id,
                code=row.code,
                name=row.name,
                industry=row.industry,
                exchange=row.exchange,
                listing_date=row.listing_date,
                is_st=row.is_st,
                is_delisting=row.is_delisting,
                trading_status=row.trading_status,
                is_suspended=row.trading_status == "suspended",
                last_trade_date=row.last_trade_date,
                industry_version=row.industry_version,
                source_as_of=row.industry_as_of,
                provenance=row.security_source,
                market_status_code=row.market_status_code,
                snapshot_at=stamp,
            )
            for row in rows
        ]
    )
    session.add_all(
        [
            DiscoveryFillTradingDate(fill_id=fill.id, trade_date=trade_date)
            for trade_date in trading_dates
        ]
    )
    session.add_all(
        [
            DiscoveryDataCheckpoint(
                fill_id=fill.id,
                code=row.code,
                status="done",
                attempts=1,
                next_retry_at=None,
                valuation_points=0,
                error=None,
                updated_at=stamp,
            )
            for row in rows
        ]
    )
    session.flush()
    fill.snapshot_status = "ready"
    session.commit()


def _store_readiness_financial(
    session: Session,
    code: str,
    *,
    period: str = "2025",
    **fields: Decimal | str | None,
) -> None:
    source_as_of = str(fields.pop("source_as_of", "2026-03-31"))
    session.add(
        Financial(
            code=code,
            period=period,
            period_type="annual",
            source_as_of=source_as_of,
            **fields,
        )
    )
    session.add_all(
        [
            FinancialFieldProvenance(
                code=code,
                period=period,
                period_type="annual",
                field=field,
                source="discovery",
            )
            for field in (
                "roe",
                "capex",
                "revenue",
                "roic",
                "capital_adequacy",
                "npl",
            )
            if fields.get(field) is not None
        ]
    )


def _store_readiness_valuations(
    session: Session,
    code: str,
    *,
    count: int = 60,
    ratio_method: str = "PE_TTM/PB_MRQ",
    fetched_at: datetime = datetime(2026, 9, 16, 12, tzinfo=timezone.utc),
    start_year: int = 2021,
    start_month: int = 10,
    pe: Decimal | None = Decimal("10"),
    pb: Decimal | None = Decimal("1"),
) -> None:
    from jobs.discovery_data import persist_valuation_history

    persist_valuation_history(
        session,
        [
            ValuationPoint(
                code=code,
                month=(
                    f"{start_year + (start_month - 1 + index) // 12:04d}-"
                    f"{(start_month - 1 + index) % 12 + 1:02d}"
                ),
                trade_date=(
                    f"{start_year + (start_month - 1 + index) // 12:04d}-"
                    f"{(start_month - 1 + index) % 12 + 1:02d}-01"
                ),
                pe=pe,
                pb=pb,
                ratio_method=ratio_method,
            )
            for index in range(count)
        ],
        fetched_at=fetched_at,
    )


def test_backfill_snapshot_drives_readiness(tmp_path) -> None:
    from discovery.data_readiness import discovery_data_readiness
    from jobs.discovery_data import (
        RequestPolicy,
        execute_discovery_data_fill,
        start_discovery_data_fill,
    )

    engine = init_db(tmp_path / "fill-readiness.db")
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    code = "000001.SZ"
    frozen_dates = [
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
    ]
    market = FakeMarketClient(
        universe=[_readiness_row(code)],
        trading_dates=frozen_dates,
        discovery_data={code: _bundle(code, request_count=1)},
    )
    policy = RequestPolicy(
        requests_per_second=100,
        max_attempts=1,
        daily_budget=100,
        max_runtime_seconds=60,
    )
    with Session(engine) as session:
        fill, dispatch = start_discovery_data_fill(
            session, policy=policy, now=now
        )
        assert fill is not None and dispatch
        fill_id = fill.id
        execute_discovery_data_fill(
            session,
            market,
            fill_id,
            policy=policy,
            clock=FakeClock(now),
        )
        report = discovery_data_readiness(session, as_of=now.date())
        assert all(track.eligible == 1 for track in report.tracks.values())
        snapshot = session.get(DiscoveryUniverseSecurity, (fill_id, code))
        assert snapshot is not None
        assert snapshot.name == code
        assert snapshot.provenance == "fake"
        assert session.scalars(
            select(DiscoveryFillTradingDate.trade_date)
            .where(DiscoveryFillTradingDate.fill_id == fill_id)
            .order_by(DiscoveryFillTradingDate.trade_date)
        ).all() == frozen_dates
    engine.dispose()


def test_readiness_uses_five_frozen_dates_for_suspension(tmp_path) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-suspension.db")
    recent = replace(
        _readiness_row("000001.SZ"),
        trading_status="suspended",
        last_trade_date="2026-09-10",
    )
    stale = replace(
        _readiness_row("000002.SZ"),
        trading_status="suspended",
        last_trade_date="2026-09-09",
    )
    unknown = replace(
        _readiness_row("000003.SZ"),
        trading_status=None,
        last_trade_date=None,
    )
    with Session(engine) as session:
        _store_readiness_universe(session, [recent, stale, unknown])
        report = discovery_data_readiness(session, as_of=date(2026, 9, 16))
    assert set(report.observations) == {recent.code}
    assert all(track.eligible == 1 for track in report.tracks.values())
    engine.dispose()


def test_readiness_quality_uses_eligible_denominator_and_one_ratio_method(
    tmp_path,
) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-quality.db")
    old_ready = "000001.SZ"
    old_mixed = "000002.SZ"
    recent = "000003.SZ"
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(old_ready),
                _readiness_row(old_mixed),
                _readiness_row(recent, listing_date="2024-01-01"),
            ],
        )
        _store_readiness_valuations(session, old_ready)
        session.commit()
    # A damaged legacy series must not become complete by blending methods.
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for index in range(60):
            year = 2021 + (9 + index) // 12
            month = (9 + index) % 12 + 1
            conn.execute(
                text(
                    """
                    INSERT INTO valuation_history (
                        code, month, trade_date, pe, pb, price, source,
                        ratio_method, fetched_at
                    ) VALUES (
                        :code, :month, :trade_date, 10, 1, 10, 'legacy',
                        :ratio_method, '2026-09-16 12:00:00'
                    )
                    """
                ),
                {
                    "code": old_mixed,
                    "month": f"{year:04d}-{month:02d}",
                    "trade_date": f"{year:04d}-{month:02d}-28",
                    "ratio_method": "method-a" if index < 30 else "method-b",
                },
            )
    with Session(engine) as session:
        report = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"quality_discount": Decimal("0.75")},
            minimum_observations={
                "quality_discount": {"pe": 60, "pb": 60}
            },
            valuation_policies={"quality_discount": "both"},
        )
        approved = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"quality_discount": Decimal("0.5")},
            minimum_observations={
                "quality_discount": {"pe": 60, "pb": 60}
            },
            valuation_policies={"quality_discount": "both"},
        )

    quality = report.tracks["quality_discount"]
    assert quality.eligible == 2
    assert quality.complete == 1
    assert quality.coverage == Decimal("0.5")
    assert quality.ready is False
    assert quality.reason == "data_not_ready"
    assert quality.missing_fields == ("valuation_pb", "valuation_pe")
    assert report.missing_counts["quality_discount"] == {
        "valuation_pb": 1,
        "valuation_pe": 1,
    }
    assert report.company_insufficient["quality_discount"] == {
        "recent_listing": 1
    }
    assert report.status == "data_not_ready"
    assert approved.tracks["quality_discount"].ready is True
    assert approved.tracks["quality_discount"].reason == "ready"
    assert approved.status == "partially_activated"
    engine.dispose()


def test_quality_readiness_keeps_unsupported_insurer_in_denominator(
    tmp_path,
) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-quality-branches.db")
    industrial = "000001.SZ"
    insurer = "600001.SH"
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(industrial, industry="制造业"),
                _readiness_row(insurer, industry="保险Ⅱ"),
            ],
        )
        _store_readiness_valuations(session, industrial)
        _store_readiness_valuations(session, insurer)
        session.commit()
        strict = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"quality_discount": Decimal("0.75")},
            minimum_observations={
                "quality_discount": {"pe": 60, "pb": 60}
            },
            valuation_policies={"quality_discount": "both"},
        )
        approved = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"quality_discount": Decimal("0.5")},
            minimum_observations={
                "quality_discount": {"pe": 60, "pb": 60}
            },
            valuation_policies={"quality_discount": "both"},
        )

    quality = strict.tracks["quality_discount"]
    assert quality.eligible == 2
    assert quality.complete == 1
    assert quality.coverage == Decimal("0.5")
    assert quality.ready is False
    assert quality.reason == "data_not_ready"
    assert strict.missing_counts["quality_discount"] == {
        "industry_branch_insurance_unsupported": 1
    }
    assert strict.company_insufficient["quality_discount"] == {
        "industry_branch_insurance_unsupported": 1
    }
    unsupported = strict.observation_aggregates["quality_discount"][
        "industry_branch_insurance_unsupported"
    ]
    assert unsupported.companies == 1
    assert unsupported.observations == 1
    assert unsupported.maximum == 1
    assert strict.observations[industrial].branch == "nonfinancial"
    assert strict.observations[insurer].branch == "insurance"
    assert approved.tracks["quality_discount"].ready is True
    assert approved.tracks["quality_discount"].coverage == Decimal("0.5")
    engine.dispose()


def test_quality_readiness_applies_build_branch_contract(tmp_path) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-quality-contract.db")
    codes = {
        "bank": "600001.SH",
        "insurance": "600002.SH",
        "other_financial": "600003.SH",
        "nonfinancial": "000001.SZ",
    }
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(codes["bank"], industry="银行Ⅱ"),
                _readiness_row(codes["insurance"], industry="保险Ⅱ"),
                _readiness_row(codes["other_financial"], industry="证券Ⅱ"),
                _readiness_row(codes["nonfinancial"], industry="制造业"),
            ],
        )
        _store_readiness_valuations(session, codes["bank"], pe=None)
        for branch in ("insurance", "other_financial", "nonfinancial"):
            _store_readiness_valuations(session, codes[branch])
        session.commit()
        report = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"quality_discount": Decimal("0.5")},
            minimum_observations={
                "quality_discount": {"pe": 60, "pb": 60}
            },
            valuation_policies={"quality_discount": "both"},
        )

    quality = report.tracks["quality_discount"]
    assert quality.eligible == 4
    assert quality.complete == 2
    assert quality.coverage == Decimal("0.5")
    assert quality.ready is True
    assert report.missing_counts["quality_discount"] == {
        "industry_branch_insurance_unsupported": 1,
        "industry_branch_other_financial_unsupported": 1,
    }
    assert report.company_insufficient["quality_discount"] == {
        "industry_branch_insurance_unsupported": 1,
        "industry_branch_other_financial_unsupported": 1,
    }
    assert {
        observation.branch for observation in report.observations.values()
    } == {"bank", "insurance", "other_financial", "nonfinancial"}
    engine.dispose()


def test_readiness_fields_are_track_and_financial_branch_specific(tmp_path) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-fields.db")
    complete = "000001.SZ"
    missing_industrial = "000002.SZ"
    missing_capital = "600001.SH"
    missing_npl = "600002.SH"
    unknown = "000003.SZ"
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(complete),
                _readiness_row(missing_industrial),
                _readiness_row(missing_capital, industry="银行Ⅱ"),
                _readiness_row(missing_npl, industry="保险Ⅱ"),
                _readiness_row(unknown, industry="-"),
            ],
        )
        _store_readiness_financial(
            session,
            complete,
            capex=Decimal("1"),
            revenue=Decimal("100"),
            roe=Decimal("12"),
            government_grants=None,
        )
        _store_readiness_financial(
            session,
            missing_industrial,
            capex=None,
            revenue=None,
            roic=None,
            government_grants=None,
        )
        _store_readiness_financial(
            session,
            missing_capital,
            capital_adequacy=None,
            npl=Decimal("1"),
            revenue=Decimal("100"),
            roic=None,
        )
        _store_readiness_financial(
            session,
            missing_npl,
            capital_adequacy=Decimal("12"),
            npl=None,
            revenue=Decimal("100"),
            roic=None,
        )
        _store_readiness_financial(
            session,
            unknown,
            capex=Decimal("1"),
            revenue=Decimal("100"),
            roe=Decimal("12"),
        )
        for code in (
            complete,
            missing_industrial,
            missing_capital,
            missing_npl,
            unknown,
        ):
            _store_readiness_valuations(session, code)
        session.commit()

        report = discovery_data_readiness(session, as_of=date(2026, 9, 16))

    quality = report.tracks["quality_discount"]
    deep_value = report.tracks["deep_value"]
    garp = report.tracks["garp"]
    assert quality.eligible == 4
    assert quality.complete == 3
    assert quality.coverage == Decimal("0.75")
    assert quality.missing_fields == (
        "industry_branch_insurance_unsupported",
    )
    assert report.missing_counts["quality_discount"] == {
        "industry_branch_insurance_unsupported": 1
    }
    assert report.company_insufficient["quality_discount"] == {
        "industry_branch_insurance_unsupported": 1
    }
    assert deep_value.missing_fields == (
        "capex",
        "capital_adequacy",
    )
    assert report.missing_counts["deep_value"] == {
        "capital_adequacy": 1,
        "capex": 1,
    }
    assert "government_grants" not in deep_value.missing_fields
    assert garp.missing_fields == (
        "financial_return_metric_policy",
        "revenue",
        "roe",
    )
    assert report.missing_counts["garp"] == {
        "financial_return_metric_policy": 2,
        "revenue": 1,
        "roe": 1,
    }
    assert "capex" not in garp.missing_fields
    assert garp.eligible == 4
    assert report.company_insufficient["garp"] == {
        "financial_return_metric_policy_unapproved": 2
    }
    assert all(track.ready is False for track in report.tracks.values())
    assert all(
        track.reason == "not_activated"
        for track in report.tracks.values()
    )
    assert report.status == "not_activated"
    engine.dispose()


def test_readiness_zero_eligible_has_exact_zero_coverage(tmp_path) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-empty.db")
    with Session(engine) as session:
        report = discovery_data_readiness(session, as_of=date(2026, 9, 16))

    for track in report.tracks.values():
        assert track.eligible == 0
        assert track.complete == 0
        assert track.coverage == Decimal("0")
        assert track.ready is False
        assert track.reason == "not_activated"
    assert report.status == "not_activated"
    engine.dispose()


@pytest.mark.parametrize(
    "track", ["quality_discount", "deep_value", "garp"]
)
def test_readiness_ratio_is_exact_and_context_isolated(
    track: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import Counter
    from decimal import DefaultContext, Inexact, ROUND_DOWN, localcontext

    from discovery.data_readiness import _track

    monkeypatch.setattr(DefaultContext, "prec", 1)
    monkeypatch.setattr(DefaultContext, "rounding", ROUND_DOWN)
    monkeypatch.setitem(DefaultContext.traps, Inexact, True)
    monkeypatch.setitem(DefaultContext.flags, Inexact, True)
    default_before = DefaultContext.flags.copy()
    with localcontext() as hostile:
        hostile.prec = 1
        hostile.rounding = ROUND_DOWN
        hostile.traps[Inexact] = True
        hostile.clear_flags()
        before = hostile.flags.copy()
        strict = _track(
            track,
            eligible=3,
            complete=2,
            missing=Counter(),
            threshold=Decimal("0.68"),
            activated=True,
        )
        permissive = _track(
            track,
            eligible=3,
            complete=2,
            missing=Counter(),
            threshold=Decimal("0.66"),
            activated=True,
        )
        rounded_boundary = _track(
            track,
            eligible=3,
            complete=2,
            missing=Counter(),
            threshold=Decimal("0.6666666666666666666666666667"),
            activated=True,
        )
        exact_endpoint = _track(
            track,
            eligible=2,
            complete=1,
            missing=Counter(),
            threshold=Decimal("0.5"),
            activated=True,
        )
        full = _track(
            track,
            eligible=3,
            complete=3,
            missing=Counter(),
            threshold=Decimal("1"),
            activated=True,
        )
        zero = _track(
            track,
            eligible=0,
            complete=0,
            missing=Counter(),
            threshold=Decimal("0"),
            activated=True,
        )
        assert hostile.flags == before
        assert DefaultContext.flags == default_before

    assert strict.coverage == Decimal("0.6666666666666666666666666667")
    assert strict.ready is False
    assert strict.reason == "data_not_ready"
    assert permissive.coverage == strict.coverage
    assert permissive.ready is True
    assert rounded_boundary.coverage == rounded_boundary.threshold
    assert rounded_boundary.ready is False
    assert exact_endpoint.coverage == Decimal("0.5")
    assert exact_endpoint.ready is True
    assert full.coverage == Decimal("1")
    assert full.ready is True
    assert zero.coverage == Decimal("0")
    assert zero.ready is True


def test_discovery_status_serializes_decimal_readiness_without_dispatch(
    tmp_path, monkeypatch
) -> None:
    from sqlalchemy import event

    class FixtureDay(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 9, 16)

    # The fixture's 60 monthly valuations fit the five-year window only up to
    # 2026-10-01; the endpoint reads the wall clock.
    monkeypatch.setattr("app.main.date", FixtureDay)
    market = FakeMarketClient()
    with _discovery_client(tmp_path, market) as client:
        first, second = "000001.SZ", "000002.SZ"
        with client.app.state.session_factory() as session:
            _store_readiness_universe(
                session, [_readiness_row(first), _readiness_row(second)]
            )
            _store_readiness_valuations(session, first)
            _store_readiness_financial(
                session,
                first,
                capex=Decimal("1"),
                revenue=Decimal("1"),
                roe=Decimal("1"),
            )
            _store_readiness_financial(
                session,
                second,
                capex=Decimal("1"),
                revenue=Decimal("1"),
                roe=Decimal("1"),
            )
            session.commit()
        before = list(market.calls)
        selects: list[str] = []

        def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        event.listen(
            client.app.state.engine,
            "before_cursor_execute",
            count_selects,
        )
        try:
            response = client.get("/api/discovery-data/status")
        finally:
            event.remove(
                client.app.state.engine,
                "before_cursor_execute",
                count_selects,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "done"
        assert body["readiness"]["status"] == "not_activated"
        assert body["readiness"]["evaluated_as_of"] == "2026-09-16"
        assert body["readiness"]["tracks"]["quality_discount"]["coverage"] == 0.5
        assert body["readiness"]["tracks"]["quality_discount"]["ready"] is False
        assert all(
            track["activated"] is False
            for track in body["readiness"]["tracks"].values()
        )
        assert body["readiness"]["source_as_of"] == {
            "universe": "2026-09-16T12:00:00+00:00",
            "security": "2026-09-16",
            "financial": "2026-03-31",
            "valuation": "2026-09-01",
            "valuation_fetched_at": "2026-09-16T12:00:00+00:00",
        }
        assert len(selects) <= 6
        assert market.calls == before


def test_readiness_as_of_excludes_future_records_and_future_listing_is_invalid(
    tmp_path,
) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-as-of.db")
    past = "000001.SZ"
    future_data = "000002.SZ"
    future_listing = "000003.SZ"
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(past),
                _readiness_row(future_data),
                _readiness_row(future_listing, listing_date="2027-01-01"),
            ],
        )
        _store_readiness_financial(
            session,
            past,
            capex=Decimal("1"),
            revenue=Decimal("1"),
            roe=Decimal("1"),
            source_as_of="2026-03-31",
        )
        _store_readiness_financial(
            session,
            future_data,
            period="2027",
            capex=Decimal("1"),
            revenue=Decimal("1"),
            roe=Decimal("1"),
            source_as_of="2027-03-31",
        )
        _store_readiness_valuations(session, past)
        _store_readiness_valuations(
            session,
            past,
            count=1,
            start_year=2021,
            start_month=9,
        )
        _store_readiness_valuations(
            session,
            future_data,
            count=1,
            start_year=2027,
            start_month=1,
            fetched_at=datetime(2027, 1, 2, tzinfo=timezone.utc),
        )
        session.commit()

        report = discovery_data_readiness(session, as_of=date(2026, 9, 16))
        _store_readiness_universe(
            session,
            [_readiness_row(past)],
            fetched_at=datetime(
                2027, 1, 1, tzinfo=timezone.utc
            ).timestamp(),
        )
        future_universe = discovery_data_readiness(
            session, as_of=date(2026, 9, 16)
        )

    assert report.evaluated_as_of == "2026-09-16"
    assert report.observations[past].valuation["PE_TTM/PB_MRQ"].pe == 60
    assert report.observations[future_data].financial["capex"] == 0
    assert report.observations[future_data].valuation == {}
    assert report.company_insufficient["quality_discount"] == {
        "invalid_listing_date": 1
    }
    assert "recent_listing" not in report.company_insufficient["quality_discount"]
    assert report.missing_counts["quality_discount"]["listing_date"] == 1
    assert report.source_as_of["financial"] == "2026-03-31"
    assert report.source_as_of["valuation"] == "2026-09-01"
    assert report.source_as_of["valuation_fetched_at"] == (
        "2026-09-16T12:00:00+00:00"
    )
    assert future_universe.observations.keys() == report.observations.keys()
    assert future_universe.source_as_of["universe"] == (
        "2026-09-16T12:00:00+00:00"
    )
    engine.dispose()


def test_readiness_counts_aligned_financial_history_without_row_stitching(
    tmp_path,
) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-aligned.db")
    aligned = "000001.SZ"
    single = "000002.SZ"
    split = "000003.SZ"
    missing = "000004.SZ"
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(aligned),
                _readiness_row(single),
                _readiness_row(split),
                _readiness_row(missing),
            ],
        )
        for period in ("2024", "2025"):
            _store_readiness_financial(
                session,
                aligned,
                period=period,
                revenue=Decimal("1"),
                roe=Decimal("1"),
            )
        _store_readiness_financial(
            session,
            single,
            revenue=Decimal("1"),
            roe=Decimal("1"),
        )
        for period in ("2022", "2023"):
            _store_readiness_financial(
                session, split, period=period, revenue=Decimal("1")
            )
        for period in ("2024", "2025"):
            _store_readiness_financial(
                session, split, period=period, roe=Decimal("1")
            )
        session.commit()

        report = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"garp": Decimal("0.2")},
            minimum_observations={"garp": {"revenue": 2, "roe": 2}},
        )

    assert report.observations[aligned].financial["revenue"] == 2
    assert report.observations[single].financial["revenue"] == 1
    assert report.observations[split].financial["revenue"] == 2
    assert report.observations[split].financial["roe"] == 2
    assert report.observations[split].aligned["garp"] == 0
    assert report.observations[missing].financial["revenue"] == 0
    assert report.tracks["garp"].complete == 1
    assert report.tracks["garp"].coverage == Decimal("0.25")
    assert report.tracks["garp"].activated is True
    assert report.tracks["garp"].ready is True
    engine.dispose()


def test_readiness_uses_explicit_bank_insurance_and_financial_branches(
    tmp_path,
) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-branches.db")
    bank = "600001.SH"
    bank_missing_npl = "600005.SH"
    insurance = "600002.SH"
    insurance_missing = "600003.SH"
    other_financial = "600004.SH"
    nonfinancial = "000001.SZ"
    unknown = "000002.SZ"
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(bank, industry="银行Ⅱ"),
                _readiness_row(bank_missing_npl, industry="银行Ⅱ"),
                _readiness_row(insurance, industry="保险Ⅱ"),
                _readiness_row(insurance_missing, industry="保险Ⅱ"),
                _readiness_row(other_financial, industry="证券Ⅱ"),
                _readiness_row(nonfinancial),
                _readiness_row(unknown, industry="-"),
            ],
        )
        _store_readiness_financial(
            session,
            bank,
            capital_adequacy=Decimal("12"),
            npl=Decimal("1"),
        )
        _store_readiness_financial(
            session,
            bank_missing_npl,
            capital_adequacy=Decimal("12"),
            npl=None,
        )
        _store_readiness_financial(
            session,
            insurance,
            capital_adequacy=Decimal("12"),
            npl=None,
        )
        _store_readiness_financial(session, insurance_missing, npl=None)
        _store_readiness_financial(
            session,
            nonfinancial,
            capex=Decimal("1"),
            revenue=Decimal("1"),
            roe=Decimal("1"),
        )
        session.commit()

        report = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"deep_value": Decimal("0.4")},
            minimum_observations={
                "deep_value": {
                    "capex": 1,
                    "capital_adequacy": 1,
                    "npl": 1,
                }
            },
        )

    deep = report.tracks["deep_value"]
    assert deep.eligible == 6
    assert deep.complete == 3
    assert deep.ready is True
    assert report.observations[bank].branch == "bank"
    assert report.observations[insurance].branch == "insurance"
    assert report.observations[other_financial].branch == "other_financial"
    assert report.observations[nonfinancial].branch == "nonfinancial"
    assert unknown not in report.observations
    assert report.missing_counts["deep_value"] == {
        "capital_adequacy": 1,
        "financial_branch_source": 1,
        "npl": 1,
    }
    assert report.observations[insurance].financial["npl"] == 0
    assert report.company_insufficient["deep_value"] == {
        "unsupported_financial_branch": 1
    }
    assert report.company_insufficient["garp"] == {
        "financial_return_metric_policy_unapproved": 5
    }
    engine.dispose()


def test_quality_counts_pe_pb_separately_and_applies_declared_policy(
    tmp_path,
) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-valuation-policy.db")
    both = "000001.SZ"
    pb_only = "000002.SZ"
    split_methods = "000003.SZ"
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(both),
                _readiness_row(pb_only),
                _readiness_row(split_methods),
            ],
        )
        _store_readiness_valuations(session, both)
        _store_readiness_valuations(session, pb_only, pe=None)
        session.commit()
    # Corrupt legacy data proves method counts are never stitched together.
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for index in range(60):
            year = 2021 + (9 + index) // 12
            month = (9 + index) % 12 + 1
            conn.execute(
                text(
                    """
                    INSERT INTO valuation_history (
                        code, month, trade_date, pe, pb, price, source,
                        ratio_method, fetched_at
                    ) VALUES (
                        :code, :month, :trade_date, :pe, :pb, 10, 'legacy',
                        :ratio_method, '2026-09-16 12:00:00'
                    )
                    """
                ),
                {
                    "code": split_methods,
                    "month": f"{year:04d}-{month:02d}",
                    "trade_date": f"{year:04d}-{month:02d}-01",
                    "pe": "10" if index < 30 else None,
                    "pb": None if index < 30 else "1",
                    "ratio_method": "method-a" if index < 30 else "method-b",
                },
            )
    with Session(engine) as session:
        pb_report = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"quality_discount": Decimal("0.6")},
            minimum_observations={"quality_discount": {"pb": 60}},
            valuation_policies={"quality_discount": "pb"},
        )
        both_report = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"quality_discount": Decimal("0.3")},
            minimum_observations={
                "quality_discount": {"pe": 30, "pb": 30}
            },
            valuation_policies={"quality_discount": "both"},
        )

    pb_counts = pb_report.observations[pb_only].valuation["PE_TTM/PB_MRQ"]
    assert pb_counts.pe == 0
    assert pb_counts.pb == 60
    assert pb_report.tracks["quality_discount"].complete == 2
    assert pb_report.tracks["quality_discount"].ready is True
    assert both_report.tracks["quality_discount"].complete == 1
    assert both_report.missing_counts["quality_discount"] == {
        "valuation_method_alignment": 1,
        "valuation_pe": 1,
    }
    engine.dispose()


def test_readiness_activation_is_per_track_and_configuration_is_strict(
    tmp_path,
) -> None:
    from app.main import _readiness_json
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-activation.db")
    code = "000001.SZ"
    with Session(engine) as session:
        _store_readiness_universe(session, [_readiness_row(code)])
        _store_readiness_valuations(session, code)
        _store_readiness_financial(
            session,
            code,
            capex=Decimal("1"),
            revenue=Decimal("1"),
            roe=Decimal("1"),
        )
        session.commit()
        partial = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"quality_discount": Decimal("1")},
            minimum_observations={
                "quality_discount": {"pe": 60, "pb": 60}
            },
            valuation_policies={"quality_discount": "both"},
        )
        threshold_only = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"quality_discount": Decimal("1")},
        )
        with pytest.raises(ValueError, match="unknown discovery track"):
            discovery_data_readiness(
                session,
                as_of=date(2026, 9, 16),
                approved_thresholds={"bogus": Decimal("1")},
            )
        with pytest.raises(ValueError, match="must be a Decimal"):
            discovery_data_readiness(
                session,
                as_of=date(2026, 9, 16),
                approved_thresholds={"garp": "0.5"},  # type: ignore[dict-item]
            )
        with pytest.raises(ValueError, match="unknown observation field"):
            discovery_data_readiness(
                session,
                as_of=date(2026, 9, 16),
                minimum_observations={"garp": {"profit": 2}},
            )
        with pytest.raises(ValueError, match="ROE fallback"):
            discovery_data_readiness(
                session,
                as_of=date(2026, 9, 16),
                minimum_observations={"garp": {"roic": 2}},
            )
        with pytest.raises(ValueError, match="valuation policy"):
            discovery_data_readiness(
                session,
                as_of=date(2026, 9, 16),
                valuation_policies={"deep_value": "pb"},
            )

    assert partial.tracks["quality_discount"].activated is True
    assert partial.tracks["quality_discount"].ready is True
    assert partial.tracks["deep_value"].activated is False
    assert partial.tracks["garp"].activated is False
    assert partial.status == "partially_activated"
    assert threshold_only.tracks["quality_discount"].activated is False
    assert threshold_only.status == "not_activated"
    body = _readiness_json(partial)
    assert "threshold_activated" not in body
    assert body["tracks"]["quality_discount"]["activated"] is True
    with pytest.raises(ValueError, match="track set"):
        _readiness_json(
            replace(partial, tracks={"quality_discount": partial.tracks["quality_discount"]})
        )
    with pytest.raises(ValueError, match="threshold track key"):
        _readiness_json(
            replace(partial, approved_thresholds={"bogus": Decimal("1")})
        )
    engine.dispose()


def test_partial_observation_minimums_merge_over_measurement_defaults(
    tmp_path,
) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-partial-minimums.db")
    industrial = "000001.SZ"
    bank = "600001.SH"
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(industrial),
                _readiness_row(bank, industry="银行Ⅱ"),
            ],
        )
        for period in ("2024", "2025"):
            _store_readiness_financial(
                session,
                industrial,
                period=period,
                capex=Decimal("1"),
                revenue=Decimal("1"),
                roe=Decimal("1"),
            )
        _store_readiness_financial(
            session,
            bank,
            capital_adequacy=Decimal("12"),
            npl=Decimal("1"),
        )
        session.commit()

        report = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={
                "deep_value": Decimal("1"),
                "garp": Decimal("1"),
            },
            minimum_observations={
                "deep_value": {"capex": 2},
                "garp": {"revenue": 2},
            },
            sector_policies={"garp": "exclude_financial"},
        )

    assert report.minimum_observations["deep_value"] == {
        "capex": 2,
        "capital_adequacy": 1,
        "npl": 1,
    }
    assert report.minimum_observations["garp"] == {
        "revenue": 2,
        "roe": 1,
    }
    assert report.tracks["deep_value"].activated is True
    assert report.tracks["deep_value"].complete == 2
    assert report.tracks["garp"].activated is True
    assert report.tracks["garp"].ready is True
    assert report.tracks["garp"].eligible == 1
    engine.dispose()


def test_garp_financials_require_explicit_sector_policy(tmp_path) -> None:
    from discovery.data_readiness import discovery_data_readiness

    engine = init_db(tmp_path / "readiness-garp-sector.db")
    industrial = "000001.SZ"
    bank = "600001.SH"
    with Session(engine) as session:
        _store_readiness_universe(
            session,
            [
                _readiness_row(industrial),
                _readiness_row(bank, industry="银行Ⅱ"),
            ],
        )
        _store_readiness_financial(
            session,
            industrial,
            revenue=Decimal("1"),
            roe=Decimal("1"),
        )
        _store_readiness_financial(
            session,
            bank,
            revenue=Decimal("1"),
            roe=Decimal("12"),
            roic=None,
        )
        session.commit()

        unapproved = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"garp": Decimal("1")},
            minimum_observations={"garp": {"revenue": 1}},
        )
        excluded = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"garp": Decimal("1")},
            minimum_observations={"garp": {"revenue": 1}},
            sector_policies={"garp": "exclude_financial"},
        )
        fallback = discovery_data_readiness(
            session,
            as_of=date(2026, 9, 16),
            approved_thresholds={"garp": Decimal("1")},
            minimum_observations={"garp": {"revenue": 1, "roe": 1}},
            sector_policies={"garp": "roe_fallback"},
        )

    garp = unapproved.tracks["garp"]
    assert garp.eligible == 2
    assert garp.complete == 1
    assert garp.activated is False
    assert "financial_return_metric_policy" in garp.missing_fields
    assert unapproved.company_insufficient["garp"] == {
        "financial_return_metric_policy_unapproved": 1
    }
    assert excluded.tracks["garp"].eligible == 1
    assert excluded.tracks["garp"].complete == 1
    assert excluded.tracks["garp"].activated is True
    assert fallback.tracks["garp"].eligible == 2
    assert fallback.tracks["garp"].complete == 2
    assert fallback.tracks["garp"].activated is True
    assert fallback.tracks["garp"].ready is True
    engine.dispose()


def test_discovery_status_omits_company_observations_and_stays_compact(
    tmp_path,
) -> None:
    market = FakeMarketClient()
    with _discovery_client(tmp_path, market) as client:
        with client.app.state.session_factory() as session:
            _store_readiness_universe(
                session,
                [
                    _readiness_row(f"{index:06d}.SZ")
                    for index in range(1, 201)
                ],
            )

        response = client.get("/api/discovery-data/status")

        assert response.status_code == 200
        readiness = response.json()["readiness"]
        assert "observations" not in readiness
        assert "observation_aggregates" in readiness
        assert len(response.content) < 8_000
