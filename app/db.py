from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "app.db"
FIN_FETCH_KEY = "fin_fetch_version"
FIN_FETCH_VERSION = "3"
_DISCOVERY_CNY_COLUMNS = (
    "net_income",
    "ocf",
    "capex",
    "revenue",
    "deducted_net_income",
    "total_assets",
    "net_assets",
    "cash",
    "short_loan",
    "long_loan",
    "bond_payable",
    "noncurrent_liab_1year",
    "interest_debt",
    "pretax_income",
    "income_tax",
    "invested_capital",
    "nonperforming_loan_balance",
    "impairment_loss",
    "government_grants",
    "asset_disposal_gain",
    "goodwill",
    "acquisition_cash_paid",
)


class Base(DeclarativeBase):
    pass


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.resolve().as_posix()}"


def _attach_busy_timeout(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_busy_timeout(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_db_engine(db_path: Path | None = None) -> Engine:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        _sqlite_url(path),
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,
    )
    _attach_busy_timeout(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)


def seed_local_user(session: Session) -> None:
    from app.models import User

    existing = session.get(User, 1)
    if existing is not None:
        return
    session.add(User(id=1, name="本机用户"))
    session.commit()


def _ensure_discovery_indexes(engine: Engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_discovery_current_week "
                "ON discovery_runs(user_id, week_key) WHERE current = 1"
            )
        )


def _ensure_discovery_snapshot_columns(engine: Engine) -> None:
    """Add Task 3 provenance columns without inventing historical values."""

    from sqlalchemy import text

    wanted = {
        "discovery_runs": {"trading_dates_json": "TEXT"},
        "discovery_input_snapshots": {"ruler_versions_json": "TEXT"},
    }
    with engine.begin() as conn:
        for table, columns in wanted.items():
            names = {
                row[1]
                for row in conn.execute(
                    text(f'PRAGMA table_info("{table}")')
                ).all()
            }
            for column, sql_type in columns.items():
                if names and column not in names:
                    conn.execute(
                        text(
                            f'ALTER TABLE "{table}" ADD COLUMN '
                            f'"{column}" {sql_type}'
                        )
                    )


def _ensure_research_columns(engine: Engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        names = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(research_jobs)")).all()
        }
        if "discovery_run_id" not in names:
            conn.execute(
                text(
                    "ALTER TABLE research_jobs ADD COLUMN discovery_run_id "
                    "INTEGER REFERENCES discovery_runs(id) ON DELETE SET NULL"
                )
            )
        if "discovery_track" not in names:
            conn.execute(
                text(
                    "ALTER TABLE research_jobs ADD COLUMN discovery_track VARCHAR"
                )
            )
        invalid_provenance = """
            (NEW.discovery_track IS NOT NULL AND NEW.discovery_track NOT IN (
                'quality_discount', 'deep_value', 'garp'
            ))
            OR (
                NEW.discovery_run_id IS NOT NULL
                AND (
                    NEW.discovery_track IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM discovery_runs
                        WHERE id = NEW.discovery_run_id
                          AND user_id = NEW.user_id
                    )
                )
            )
        """
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                "trg_research_jobs_discovery_provenance_insert "
                "BEFORE INSERT ON research_jobs "
                f"WHEN {invalid_provenance} "
                "BEGIN "
                "SELECT RAISE(ABORT, 'invalid discovery research provenance'); "
                "END"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                "trg_research_jobs_discovery_provenance_update "
                "BEFORE UPDATE OF user_id, discovery_run_id, discovery_track "
                "ON research_jobs "
                f"WHEN {invalid_provenance} "
                "BEGIN "
                "SELECT RAISE(ABORT, 'invalid discovery research provenance'); "
                "END"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                "trg_discovery_runs_research_ownership_update "
                "BEFORE UPDATE OF user_id ON discovery_runs "
                "WHEN EXISTS ("
                "SELECT 1 FROM research_jobs "
                "WHERE discovery_run_id = OLD.id AND user_id != NEW.user_id"
                ") "
                "BEGIN "
                "SELECT RAISE(ABORT, 'discovery research owner mismatch'); "
                "END"
            )
        )


def _ensure_listed_year(engine: Engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(instruments)")).fetchall()
        names = {row[1] for row in rows}
        if "listed_year" not in names:
            conn.execute(text("ALTER TABLE instruments ADD COLUMN listed_year INTEGER"))
        if "industry" not in names:
            conn.execute(text("ALTER TABLE instruments ADD COLUMN industry VARCHAR"))


def _ensure_financial_columns(engine: Engine) -> None:
    from sqlalchemy import text

    wanted = {
        "roe": "NUMERIC(20, 8)",
        "net_income": "TEXT",
        "ocf": "TEXT",
        "capex": "TEXT",
        "ebit": "NUMERIC(20, 8)",
        "interest": "NUMERIC(20, 8)",
        "gross_margin": "NUMERIC(20, 8)",
        "net_margin": "NUMERIC(20, 8)",
        "shares": "NUMERIC(20, 8)",
        "share_change_ma": "BOOLEAN",
        "bps": "NUMERIC(20, 8)",
        "npl": "NUMERIC(20, 8)",
        "eps": "NUMERIC(20, 8)",
        "revenue": "TEXT",
        "deducted_net_income": "TEXT",
        "total_assets": "TEXT",
        "net_assets": "TEXT",
        "cash": "TEXT",
        "short_loan": "TEXT",
        "long_loan": "TEXT",
        "bond_payable": "TEXT",
        "noncurrent_liab_1year": "TEXT",
        "interest_debt": "TEXT",
        "pretax_income": "TEXT",
        "income_tax": "TEXT",
        "normalized_effective_tax_rate": "NUMERIC(30, 8)",
        "invested_capital": "TEXT",
        "roic": "NUMERIC(30, 8)",
        "capital_adequacy": "NUMERIC(30, 8)",
        "core_tier1_capital_adequacy": "NUMERIC(30, 8)",
        "nonperforming_loan_balance": "TEXT",
        "impairment_loss": "TEXT",
        "government_grants": "TEXT",
        "asset_disposal_gain": "TEXT",
        "goodwill": "TEXT",
        "acquisition_cash_paid": "TEXT",
        "source_as_of": "VARCHAR",
    }
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(financials)")).fetchall()
        names = {row[1] for row in rows}
        if not names:
            return
        for column, sql_type in wanted.items():
            if column not in names:
                conn.execute(
                    text(f"ALTER TABLE financials ADD COLUMN {column} {sql_type}")
                )


def _ensure_discovery_fill_columns(engine: Engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        names = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(discovery_data_fills)")
            ).fetchall()
        }
        columns = {
            "run_started_at": "DATETIME",
            "last_request_at": "DATETIME",
            "snapshot_status": "VARCHAR",
        }
        for column, sql_type in columns.items():
            if names and column not in names:
                conn.execute(
                    text(
                        "ALTER TABLE discovery_data_fills "
                        f"ADD COLUMN {column} {sql_type}"
                    )
                )


def _classify_discovery_fill_snapshots(engine: Engine) -> None:
    from sqlalchemy import text

    guidance = "snapshot unavailable or mismatched; start a new fill"
    with engine.begin() as conn:
        fills = conn.execute(
            text(
                "SELECT id, status, total FROM discovery_data_fills "
                "WHERE snapshot_status IS NULL"
            )
        ).all()
        for fill_id, status, total in fills:
            checkpoints = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT code FROM discovery_data_checkpoints "
                        "WHERE fill_id = :fill_id"
                    ),
                    {"fill_id": fill_id},
                )
            }
            snapshots = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT code FROM discovery_universe_securities "
                        "WHERE fill_id = :fill_id"
                    ),
                    {"fill_id": fill_id},
                )
            }
            has_dates = (
                conn.execute(
                    text(
                        "SELECT 1 FROM discovery_fill_trading_dates "
                        "WHERE fill_id = :fill_id LIMIT 1"
                    ),
                    {"fill_id": fill_id},
                ).first()
                is not None
            )
            if checkpoints:
                ready = checkpoints == snapshots and has_dates
            else:
                ready = int(total or 0) == 0 and not snapshots and has_dates
            conn.execute(
                text(
                    "UPDATE discovery_data_fills SET "
                    "snapshot_status = :snapshot_status, "
                    "status = :status, last_error = :last_error "
                    "WHERE id = :fill_id"
                ),
                {
                    "fill_id": fill_id,
                    "snapshot_status": "ready" if ready else "unavailable",
                    "status": status if ready else "snapshot_unavailable",
                    "last_error": None if ready else guidance,
                },
            )


def _ensure_financial_text_storage(engine: Engine) -> None:
    from sqlalchemy import MetaData, text

    from app.models import Financial

    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(financials)")).all()
        types = {row[1]: str(row[2]).upper() for row in rows}
        if all(types.get(column) == "TEXT" for column in _DISCOVERY_CNY_COLUMNS):
            return

        replacement = Financial.__table__.to_metadata(
            MetaData(),
            name="financials_text_migration",
        )
        replacement.drop(conn, checkfirst=True)
        replacement.create(conn)
        columns = [column.name for column in Financial.__table__.columns]
        names = ", ".join(f'"{column}"' for column in columns)
        values = ", ".join(
            (
                f'CAST("{column}" AS TEXT)'
                if column in _DISCOVERY_CNY_COLUMNS
                else f'"{column}"'
            )
            for column in columns
        )
        conn.execute(
            text(
                f"INSERT INTO financials_text_migration ({names}) "
                f"SELECT {values} FROM financials"
            )
        )
        conn.execute(text("DROP TABLE financials"))
        conn.execute(
            text(
                "ALTER TABLE financials_text_migration "
                "RENAME TO financials"
            )
        )


def _ensure_valuation_series(engine: Engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        methods = conn.execute(
            text(
                "SELECT code, MIN(ratio_method), MAX(ratio_method) "
                "FROM valuation_history GROUP BY code"
            )
        ).all()
        for code, first, last in methods:
            if first != last:
                raise ValueError(f"{code} has mixed valuation ratio_method values")
            claimed = conn.execute(
                text("SELECT ratio_method FROM valuation_series WHERE code = :code"),
                {"code": code},
            ).scalar()
            if claimed is not None and claimed != first:
                raise ValueError(
                    f"{code} ratio_method mismatch: {claimed} != {first}"
                )
            conn.execute(
                text(
                    "INSERT INTO valuation_series (code, ratio_method) "
                    "VALUES (:code, :ratio_method) ON CONFLICT(code) DO NOTHING"
                ),
                {"code": code, "ratio_method": first},
            )

        foreign_keys = conn.execute(
            text("PRAGMA foreign_key_list(valuation_history)")
        ).all()
        has_series_fk = {
            (row[2], row[3], row[4]) for row in foreign_keys
        } >= {
            ("valuation_series", "code", "code"),
            ("valuation_series", "ratio_method", "ratio_method"),
        }
        if has_series_fk:
            return

        conn.execute(
            text(
                """
                CREATE TABLE valuation_history_new (
                    code VARCHAR NOT NULL,
                    month VARCHAR NOT NULL,
                    trade_date VARCHAR NOT NULL,
                    pe NUMERIC(20, 8),
                    pb NUMERIC(20, 8),
                    price NUMERIC(20, 8),
                    source VARCHAR NOT NULL,
                    ratio_method VARCHAR NOT NULL,
                    fetched_at DATETIME NOT NULL,
                    PRIMARY KEY (code, month),
                    CONSTRAINT fk_valuation_history_series_method
                        FOREIGN KEY (code, ratio_method)
                        REFERENCES valuation_series (code, ratio_method)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO valuation_history_new (
                    code, month, trade_date, pe, pb, price, source,
                    ratio_method, fetched_at
                )
                SELECT code, month, trade_date, pe, pb, price, source,
                       ratio_method, fetched_at
                FROM valuation_history
                """
            )
        )
        conn.execute(text("DROP TABLE valuation_history"))
        conn.execute(text("ALTER TABLE valuation_history_new RENAME TO valuation_history"))


def _reset_stale_financials(engine: Engine) -> None:
    """财务只是东财的缓存；取数口径改了就整体丢掉，按需重拉。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        current = conn.execute(
            text("SELECT value FROM settings WHERE key = :key"),
            {"key": FIN_FETCH_KEY},
        ).scalar()
        if current == FIN_FETCH_VERSION:
            return
        conn.execute(text("DELETE FROM financials"))
        conn.execute(text("DELETE FROM financial_fetch_completeness"))
        conn.execute(text("DELETE FROM financial_field_provenance"))
        conn.execute(
            text(
                "INSERT INTO settings (key, value) VALUES (:key, :value) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"key": FIN_FETCH_KEY, "value": FIN_FETCH_VERSION},
        )


def _infer_legacy_financial_completeness(engine: Engine) -> None:
    """Conservatively recognize rows that only the legacy fetch could supply."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT OR IGNORE INTO financial_fetch_completeness (
                    code, mode, completed_at
                )
                SELECT DISTINCT code, 'legacy', CURRENT_TIMESTAMP
                FROM financials
                WHERE period_type = 'annual'
                  AND (
                    roe IS NOT NULL OR ebit IS NOT NULL
                    OR interest IS NOT NULL OR gross_margin IS NOT NULL
                    OR net_margin IS NOT NULL OR share_change_ma IS NOT NULL
                    OR bps IS NOT NULL OR eps IS NOT NULL
                  )
                """
            )
        )


def init_db(db_path: Path | None = None) -> Engine:
    from app import models as _models  # noqa: F401
    from app import discovery_models as _discovery_models  # noqa: F401

    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    _ensure_discovery_snapshot_columns(engine)
    _ensure_discovery_indexes(engine)
    _ensure_research_columns(engine)
    _ensure_listed_year(engine)
    _ensure_financial_columns(engine)
    _ensure_discovery_fill_columns(engine)
    _classify_discovery_fill_snapshots(engine)
    _ensure_financial_text_storage(engine)
    _ensure_valuation_series(engine)
    _reset_stale_financials(engine)
    _infer_legacy_financial_completeness(engine)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        seed_local_user(session)
        from screen.ruler import seed_if_empty

        seed_if_empty(session)
    return engine
