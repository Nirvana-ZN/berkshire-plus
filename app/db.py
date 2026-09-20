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


def _ensure_ruler_scope(engine: Engine) -> None:
    """Migrate the scoped, append-only ledger without rewriting history."""

    from sqlalchemy import text

    with engine.begin() as conn:
        names = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(ruler_versions)")
            ).all()
        }
        if names and "scope" not in names:
            conn.execute(
                text(
                    "ALTER TABLE ruler_versions ADD COLUMN scope VARCHAR "
                    "NOT NULL DEFAULT 'funnel'"
                )
            )
        if names:
            conn.execute(
                text(
                    "UPDATE ruler_versions SET scope = 'funnel' "
                    "WHERE scope IS NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_ruler_versions_user_scope_id "
                    "ON ruler_versions(user_id, scope, id)"
                )
            )
            unique_sql = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' "
                    "AND name = 'ux_ruler_versions_user_scope_digest'"
                )
            ).scalar()
            if unique_sql is None or " WHERE " not in unique_sql.upper():
                if unique_sql is not None:
                    conn.execute(
                        text(
                            "DROP INDEX "
                            "ux_ruler_versions_user_scope_digest"
                        )
                    )
                cutoff = int(
                    conn.execute(
                        text("SELECT COALESCE(MAX(id), 0) FROM ruler_versions")
                    ).scalar_one()
                )
                # ponytail: legacy IDs may already be frozen into snapshots.
                # Exempt them forever; the persisted index SQL is the cutoff.
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX "
                        "ux_ruler_versions_user_scope_digest "
                        "ON ruler_versions(user_id, scope, digest) "
                        f"WHERE id > {cutoff}"
                    )
                )
            conn.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS "
                    "trg_ruler_versions_scope_default "
                    "AFTER INSERT ON ruler_versions "
                    "WHEN NEW.scope IS NULL "
                    "BEGIN "
                    "UPDATE ruler_versions SET scope = 'funnel' "
                    "WHERE id = NEW.id; "
                    "END"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS "
                    "trg_ruler_versions_funnel_ack_clear "
                    "AFTER INSERT ON ruler_versions "
                    "WHEN NEW.scope = 'funnel' "
                    "BEGIN "
                    "DELETE FROM settings WHERE key = 'ruler_gate_ack'; "
                    "END"
                )
            )
            columns = tuple(
                row[1]
                for row in conn.execute(
                    text("PRAGMA table_info(ruler_versions)")
                ).all()
            )
            changed = " OR ".join(
                (
                    "(NEW.scope IS NOT OLD.scope AND NOT "
                    "(OLD.scope IS NULL AND NEW.scope = 'funnel'))"
                    if column == "scope"
                    else f'NEW."{column}" IS NOT OLD."{column}"'
                )
                for column in columns
            )
            update_columns = ", ".join(f'"{column}"' for column in columns)
            # Ruler versions are historical facts: run deletion never
            # cascades here, and user deletion remains FK-restricted while
            # ruler rows exist. Production only appends ledger rows.
            conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_ruler_versions_immutable"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER "
                    "trg_ruler_versions_immutable "
                    f"BEFORE UPDATE OF {update_columns} ON ruler_versions "
                    f"WHEN {changed} "
                    "BEGIN "
                    "SELECT RAISE(ABORT, 'ruler version is immutable'); "
                    "END"
                )
            )
            conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_ruler_versions_append_only"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER trg_ruler_versions_append_only "
                    "BEFORE DELETE ON ruler_versions "
                    "BEGIN "
                    "SELECT RAISE(ABORT, "
                    "'ruler version ledger is append-only'); "
                    "END"
                )
            )


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
        "discovery_runs": {
            "trading_dates_json": "TEXT",
            "ruler_payloads_json": "TEXT",
        },
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


def _ensure_discovery_lineage(engine: Engine) -> None:
    """Add legacy columns; triggers replace unavailable SQLite ALTER FKs."""

    from sqlalchemy import text

    with engine.begin() as conn:
        common_names = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(discovery_common_stages)")
            ).all()
        }
        if common_names and "retry_of_stage_id" not in common_names:
            conn.execute(
                text(
                    "ALTER TABLE discovery_common_stages "
                    "ADD COLUMN retry_of_stage_id INTEGER"
                )
            )
        names = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(discovery_stages)")
            ).all()
        }
        for column, sql_type in {
            "common_stage_id": "INTEGER",
            "predecessor_stage_id": "INTEGER",
            "predecessor_stage": "VARCHAR",
            "retry_of_stage_id": "INTEGER",
            "reopen_of_stage_id": "INTEGER",
        }.items():
            if names and column not in names:
                conn.execute(
                    text(
                        "ALTER TABLE discovery_stages "
                        f"ADD COLUMN {column} {sql_type}"
                    )
                )

        # ponytail: only deterministic one-candidate legacy chains are safe to
        # recover; ambiguous rows intentionally remain null and fail closed.
        conn.execute(
            text(
                "UPDATE discovery_stages AS child "
                "SET common_stage_id = ("
                "SELECT parent.id FROM discovery_common_stages AS parent "
                "WHERE parent.run_id = child.run_id "
                "AND parent.status = 'confirmed' "
                "AND parent.superseded_at IS NULL "
                "AND parent.created_at <= child.created_at"
                ") WHERE child.stage = 'track_gate' "
                "AND child.common_stage_id IS NULL "
                "AND child.predecessor_stage_id IS NULL "
                "AND child.predecessor_stage IS NULL "
                "AND 1 = (SELECT COUNT(*) "
                "FROM discovery_common_stages AS parent "
                "WHERE parent.run_id = child.run_id "
                "AND parent.status = 'confirmed' "
                "AND parent.superseded_at IS NULL "
                "AND parent.created_at <= child.created_at)"
            )
        )
        for child_stage, parent_stage in (
            ("competition", "track_gate"),
            ("adjudication", "competition"),
        ):
            conn.execute(
                text(
                    "UPDATE discovery_stages AS child "
                    "SET predecessor_stage_id = ("
                    "SELECT parent.id FROM discovery_stages AS parent "
                    "WHERE parent.run_id = child.run_id "
                    "AND parent.track = child.track "
                    "AND parent.stage = :parent_stage "
                    "AND parent.status = 'confirmed' "
                    "AND parent.superseded_at IS NULL "
                    "AND parent.created_at <= child.created_at"
                    "), predecessor_stage = :parent_stage "
                    "WHERE child.stage = :child_stage "
                    "AND child.predecessor_stage_id IS NULL "
                    "AND child.predecessor_stage IS NULL "
                    "AND child.common_stage_id IS NULL "
                    "AND 1 = (SELECT COUNT(*) "
                    "FROM discovery_stages AS parent "
                    "WHERE parent.run_id = child.run_id "
                    "AND parent.track = child.track "
                    "AND parent.stage = :parent_stage "
                    "AND parent.status = 'confirmed' "
                    "AND parent.superseded_at IS NULL "
                    "AND parent.created_at <= child.created_at)"
                ),
                {
                    "child_stage": child_stage,
                    "parent_stage": parent_stage,
                },
            )

        invalid = """
            (NEW.stage = 'track_gate' AND (
                NEW.common_stage_id IS NULL
                OR NEW.predecessor_stage_id IS NOT NULL
                OR NEW.predecessor_stage IS NOT NULL
                OR NOT EXISTS (
                    SELECT 1 FROM discovery_common_stages p
                    WHERE p.id = NEW.common_stage_id
                      AND p.run_id = NEW.run_id
                      AND p.status = 'confirmed'
                      AND p.superseded_at IS NULL
                )
            ))
            OR (NEW.stage IN ('competition', 'adjudication') AND (
                NEW.common_stage_id IS NOT NULL
                OR NEW.predecessor_stage_id IS NULL
                OR NEW.predecessor_stage != CASE NEW.stage
                    WHEN 'competition' THEN 'track_gate'
                    ELSE 'competition'
                END
                OR NOT EXISTS (
                    SELECT 1 FROM discovery_stages p
                    WHERE p.id = NEW.predecessor_stage_id
                      AND p.run_id = NEW.run_id
                      AND p.track = NEW.track
                      AND p.stage = NEW.predecessor_stage
                      AND p.status = 'confirmed'
                      AND p.superseded_at IS NULL
                )
            ))
            OR NEW.stage NOT IN ('track_gate', 'competition', 'adjudication')
        """
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                "trg_discovery_stage_lineage_insert "
                "BEFORE INSERT ON discovery_stages "
                f"WHEN {invalid} "
                "BEGIN "
                "SELECT RAISE(ABORT, 'invalid discovery stage lineage'); "
                "END"
            )
        )

        common_origin_invalid = """
            (NEW.attempt = 1 AND NEW.retry_of_stage_id IS NOT NULL)
            OR (NEW.retry_of_stage_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM discovery_common_stages parent
                WHERE parent.id = NEW.retry_of_stage_id
                  AND parent.run_id = NEW.run_id
                  AND parent.status = 'failed'
                  AND parent.superseded_at IS NOT NULL
                  AND parent.attempt + 1 = NEW.attempt
                  AND parent.created_at <= NEW.created_at
            ))
        """
        for action in ("INSERT", "UPDATE"):
            update_columns = (
                " OF run_id, attempt, retry_of_stage_id, created_at"
                if action == "UPDATE"
                else ""
            )
            conn.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS "
                    f"trg_discovery_common_stage_origin_{action.lower()} "
                    f"BEFORE {action}{update_columns} "
                    "ON discovery_common_stages "
                    f"WHEN {common_origin_invalid} "
                    "BEGIN "
                    "SELECT RAISE(ABORT, "
                    "'invalid discovery common stage origin'); "
                    "END"
                )
            )

        track_origin_invalid = """
            (NEW.retry_of_stage_id IS NOT NULL
             AND NEW.reopen_of_stage_id IS NOT NULL)
            OR (NEW.attempt = 1 AND (
                NEW.retry_of_stage_id IS NOT NULL
                OR NEW.reopen_of_stage_id IS NOT NULL
            ))
            OR (NEW.retry_of_stage_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM discovery_stages parent
                WHERE parent.id = NEW.retry_of_stage_id
                  AND parent.run_id = NEW.run_id
                  AND parent.track = NEW.track
                  AND parent.stage = NEW.stage
                  AND parent.status = 'failed'
                  AND parent.superseded_at IS NOT NULL
                  AND parent.attempt + 1 = NEW.attempt
                  AND parent.created_at <= NEW.created_at
            ))
            OR (NEW.reopen_of_stage_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM discovery_stages parent
                WHERE parent.id = NEW.reopen_of_stage_id
                  AND parent.run_id = NEW.run_id
                  AND parent.track = NEW.track
                  AND parent.stage = NEW.stage
                  AND parent.status = 'confirmed'
                  AND parent.superseded_at IS NOT NULL
                  AND parent.attempt + 1 = NEW.attempt
                  AND parent.created_at <= NEW.created_at
            ))
        """
        for action in ("INSERT", "UPDATE"):
            update_columns = (
                " OF run_id, track, stage, attempt, retry_of_stage_id, "
                "reopen_of_stage_id, created_at"
                if action == "UPDATE"
                else ""
            )
            conn.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS "
                    f"trg_discovery_stage_origin_{action.lower()} "
                    f"BEFORE {action}{update_columns} ON discovery_stages "
                    f"WHEN {track_origin_invalid} "
                    "BEGIN "
                    "SELECT RAISE(ABORT, 'invalid discovery stage origin'); "
                    "END"
                )
            )
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                "trg_discovery_stage_lineage_update "
                "BEFORE UPDATE OF run_id, track, stage, common_stage_id, "
                "predecessor_stage_id, predecessor_stage "
                "ON discovery_stages "
                f"WHEN {invalid} "
                "BEGIN "
                "SELECT RAISE(ABORT, 'invalid discovery stage lineage'); "
                "END"
            )
        )


def _ensure_job_lock_columns(engine: Engine) -> None:
    """Add the discovery lease token without changing legacy lock semantics."""

    from sqlalchemy import text

    with engine.begin() as conn:
        names = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(job_lock)")).all()
        }
        if names and "owner_token" not in names:
            conn.execute(
                text("ALTER TABLE job_lock ADD COLUMN owner_token VARCHAR")
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


def _ensure_discovery_universe_seals(engine: Engine) -> None:
    """Seal ready universe rows; unfinished fills remain cascade-deletable."""

    from sqlalchemy import text

    with engine.begin() as conn:
        if not {
            "discovery_data_fills",
            "discovery_universe_securities",
        } <= {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            )
        }:
            return
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                "trg_discovery_universe_security_insert_immutable "
                "BEFORE INSERT ON discovery_universe_securities "
                "WHEN EXISTS ("
                "SELECT 1 FROM discovery_data_fills AS fill "
                "WHERE fill.id = NEW.fill_id "
                "AND fill.snapshot_status IN ('ready', 'unavailable')"
                ") BEGIN SELECT RAISE(ABORT, "
                "'ready discovery universe security is immutable'); END"
            )
        )
        conn.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_discovery_universe_security_immutable"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER "
                "trg_discovery_universe_security_immutable "
                "BEFORE UPDATE ON discovery_universe_securities "
                "WHEN EXISTS ("
                "SELECT 1 FROM discovery_data_fills AS fill "
                "WHERE fill.id = OLD.fill_id "
                "AND fill.snapshot_status IN ('ready', 'unavailable')"
                ") OR EXISTS ("
                "SELECT 1 FROM discovery_data_fills AS fill "
                "WHERE fill.id = NEW.fill_id "
                "AND fill.snapshot_status IN ('ready', 'unavailable')"
                ") BEGIN SELECT RAISE(ABORT, "
                "'ready discovery universe security is immutable'); END"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                "trg_discovery_universe_security_delete_immutable "
                "BEFORE DELETE ON discovery_universe_securities "
                "WHEN EXISTS ("
                "SELECT 1 FROM discovery_data_fills AS fill "
                "WHERE fill.id = OLD.fill_id "
                "AND fill.snapshot_status IN ('ready', 'unavailable')"
                ") BEGIN SELECT RAISE(ABORT, "
                "'ready discovery universe security is immutable'); END"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                "trg_discovery_ready_fill_snapshot_immutable "
                "BEFORE UPDATE OF snapshot_status ON discovery_data_fills "
                "WHEN (OLD.snapshot_status = 'ready' "
                "AND (NEW.snapshot_status IS NULL OR "
                "NEW.snapshot_status NOT IN ('ready', 'unavailable'))) "
                "OR (OLD.snapshot_status = 'unavailable' "
                "AND NEW.snapshot_status IS NOT OLD.snapshot_status) "
                "BEGIN SELECT RAISE(ABORT, "
                "'ready discovery fill snapshot is immutable'); END"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                "trg_discovery_ready_fill_append_only "
                "BEFORE DELETE ON discovery_data_fills "
                "WHEN OLD.snapshot_status IN ('ready', 'unavailable') "
                "BEGIN SELECT RAISE(ABORT, "
                "'ready discovery fill is append-only'); END"
            )
        )


def _ensure_discovery_checkpoint_columns(engine: Engine) -> None:
    """Add fill-scoped payload storage without blessing legacy checkpoints."""

    from sqlalchemy import text

    with engine.begin() as conn:
        names = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(discovery_data_checkpoints)")
            ).fetchall()
        }
        columns = {"payload_json": "TEXT", "payload_digest": "VARCHAR(64)"}
        for column, sql_type in columns.items():
            if names and column not in names:
                conn.execute(
                    text(
                        "ALTER TABLE discovery_data_checkpoints "
                        f"ADD COLUMN {column} {sql_type}"
                    )
                )
        if names:
            conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_discovery_checkpoint_payload_immutable"
                )
            )
            conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_discovery_checkpoint_status_immutable"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER "
                    "trg_discovery_checkpoint_payload_immutable "
                    "BEFORE UPDATE OF payload_json, payload_digest "
                    "ON discovery_data_checkpoints "
                    "WHEN (OLD.payload_json IS NOT NULL "
                    "OR OLD.payload_digest IS NOT NULL) "
                    "AND (NEW.payload_json IS NOT OLD.payload_json "
                    "OR NEW.payload_digest IS NOT OLD.payload_digest) "
                    "BEGIN "
                    "SELECT RAISE(ABORT, "
                    "'completed discovery payload is immutable'); "
                    "END"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER "
                    "trg_discovery_checkpoint_status_immutable "
                    "BEFORE UPDATE OF status "
                    "ON discovery_data_checkpoints "
                    "WHEN OLD.status = 'done' "
                    "AND OLD.payload_json IS NOT NULL "
                    "AND OLD.payload_digest IS NOT NULL "
                    "AND NEW.status IS NOT OLD.status "
                    "BEGIN "
                    "SELECT RAISE(ABORT, "
                    "'completed discovery status is immutable'); "
                    "END"
                )
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
    from app import discovery_models as _discovery_models  # noqa: F401
    from app import models as _models  # noqa: F401

    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    _ensure_ruler_scope(engine)
    _ensure_discovery_snapshot_columns(engine)
    _ensure_discovery_lineage(engine)
    _ensure_job_lock_columns(engine)
    _ensure_discovery_indexes(engine)
    _ensure_research_columns(engine)
    _ensure_listed_year(engine)
    _ensure_financial_columns(engine)
    _ensure_discovery_fill_columns(engine)
    _ensure_discovery_checkpoint_columns(engine)
    _ensure_discovery_universe_seals(engine)
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
