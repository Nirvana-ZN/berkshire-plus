from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "app.db"


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


def init_db(db_path: Path | None = None) -> Engine:
    from app import models as _models  # noqa: F401

    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        seed_local_user(session)
    return engine
