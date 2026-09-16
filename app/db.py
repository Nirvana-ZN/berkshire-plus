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


class Base(DeclarativeBase):
    pass


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.resolve().as_posix()}"


def _attach_busy_timeout(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_busy_timeout(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
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
        "bps": "NUMERIC(20, 8)",
        "npl": "NUMERIC(20, 8)",
        "eps": "NUMERIC(20, 8)",
        "revenue": "NUMERIC(20, 8)",
        "deducted_net_income": "NUMERIC(20, 8)",
        "total_assets": "NUMERIC(20, 8)",
        "net_assets": "NUMERIC(20, 8)",
        "cash": "NUMERIC(20, 8)",
        "short_loan": "NUMERIC(20, 8)",
        "long_loan": "NUMERIC(20, 8)",
        "bond_payable": "NUMERIC(20, 8)",
        "noncurrent_liab_1year": "NUMERIC(20, 8)",
        "interest_debt": "NUMERIC(20, 8)",
        "pretax_income": "NUMERIC(20, 8)",
        "income_tax": "NUMERIC(20, 8)",
        "normalized_effective_tax_rate": "NUMERIC(20, 8)",
        "invested_capital": "NUMERIC(20, 8)",
        "roic": "NUMERIC(20, 8)",
        "capital_adequacy": "NUMERIC(20, 8)",
        "core_tier1_capital_adequacy": "NUMERIC(20, 8)",
        "nonperforming_loan_balance": "NUMERIC(20, 8)",
        "impairment_loss": "NUMERIC(20, 8)",
        "government_grants": "NUMERIC(20, 8)",
        "asset_disposal_gain": "NUMERIC(20, 8)",
        "goodwill": "NUMERIC(20, 8)",
        "acquisition_cash_paid": "NUMERIC(20, 8)",
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
        conn.execute(
            text(
                "INSERT INTO settings (key, value) VALUES (:key, :value) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"key": FIN_FETCH_KEY, "value": FIN_FETCH_VERSION},
        )


def init_db(db_path: Path | None = None) -> Engine:
    from app import models as _models  # noqa: F401

    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    _ensure_listed_year(engine)
    _ensure_financial_columns(engine)
    _reset_stale_financials(engine)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        seed_local_user(session)
        from screen.ruler import seed_if_empty

        seed_if_empty(session)
    return engine
