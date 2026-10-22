"""Temporary-only schema compatibility and retained-history release boundaries."""

import sqlite3

import pytest
from sqlalchemy import text

from app.db import init_db


def test_unknown_schema_is_read_only_before_any_initialization(tmp_path):
    path = tmp_path / 'future.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE retained (id INTEGER PRIMARY KEY, payload TEXT)')
        conn.execute("INSERT INTO retained VALUES (1, 'frozen future bytes')")
        conn.execute('PRAGMA user_version=999')
    before = path.read_bytes()
    engine = init_db(path)
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql('SELECT payload FROM retained').scalar_one() == 'frozen future bytes'
            assert conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars().all() == ['retained']
            with pytest.raises(Exception, match='readonly|read.only'):
                conn.exec_driver_sql("INSERT INTO retained VALUES (2, 'must fail')")
    finally:
        engine.dispose()
    assert path.read_bytes() == before


def test_actual_old_connection_cannot_write_upgraded_schema(tmp_path):
    path = tmp_path / 'migrated.db'
    engine = init_db(path)
    try:
        # Represents an already-running old process: no new connector/UDF code.
        with sqlite3.connect(path) as old:
            assert old.execute('SELECT count(*) FROM users').fetchone()[0] == 1
            for statement in (
                "INSERT INTO settings(key,value) VALUES ('old-writer','bad')",
                "UPDATE users SET name='bad' WHERE id=1",
                'DELETE FROM users WHERE id=1',
            ):
                with pytest.raises(sqlite3.DatabaseError, match='selection_writer|incompatible writer'):
                    old.execute(statement)
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO settings(key,value) VALUES ('current-writer','ok')")
    finally:
        engine.dispose()


def test_selection_migration_is_idempotent_and_preserves_frozen_bytes(tmp_path):
    path = tmp_path / 'migration.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)')
        conn.execute("INSERT INTO users VALUES (1, 'legacy')")
        conn.execute('CREATE TABLE ruler_versions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, digest VARCHAR NOT NULL, reason VARCHAR NOT NULL, payload_json TEXT NOT NULL, activated_at DATETIME NOT NULL)')
        conn.execute("INSERT INTO ruler_versions VALUES (1,1,'original-sha','original reason','{\"frozen\":true}','2020-01-01 00:00:00')")
    first = init_db(path)
    with first.connect() as conn:
        original = conn.execute(text('SELECT id,user_id,digest,reason,payload_json,activated_at FROM ruler_versions ORDER BY id')).all()
        tables = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").scalars().all()
        assert {'tracking_requests', 'portfolio_exposure_versions', 'portfolio_exposure_selections',
                'portfolio_review_exposure_captures', 'selection_releases'} <= set(tables)
    first.dispose()
    second = init_db(path)
    try:
        with second.connect() as conn:
            assert conn.execute(text('SELECT id,user_id,digest,reason,payload_json,activated_at FROM ruler_versions ORDER BY id')).all() == original
            assert original[0][2:5] == ('original-sha', 'original reason', '{"frozen":true}')
            assert conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").scalars().all() == tables
    finally:
        second.dispose()


def test_unknown_schema_app_skips_startup_mutations_and_rejects_posts(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    path = tmp_path / 'future-app.db'
    init_db(path).dispose()
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA user_version=999')
    before = path.read_bytes()
    invoked = []
    for name in ('migrate_yaml_portfolio', '_bulk_refresh', 'seed_targets_if_empty',
                 'clear_stale_lock', 'resume_discovery_queues'):
        monkeypatch.setattr('app.main.' + name, lambda *args, _name=name, **kwargs: invoked.append(_name))
    with TestClient(create_app(db_path=path, env_path=tmp_path / '.env', yaml_dir=tmp_path,
                              reports_dir=tmp_path / 'reports')) as client:
        assert client.get('/api/health').status_code == 200
        result = client.post('/api/settings', json={'model': 'must-not-write'})
        assert result.status_code == 503
        assert result.json()['code'] == 'schema_read_only'
    assert invoked == []
    assert path.read_bytes() == before


def test_startup_preserves_stale_cache_without_promoting_completeness(tmp_path):
    from app.models import Financial, Setting
    from sqlalchemy.orm import Session
    from decimal import Decimal
    path = tmp_path / 'retained-cache.db'
    engine = init_db(path)
    with Session(engine) as session:
        session.add(Financial(code='000001.SZ', period='2025', period_type='annual',
                              revenue=Decimal('123456789.125'), roe=Decimal('10')))
        session.add(Setting(key='fin_fetch_version', value='old-cache-version'))
        session.commit()
    engine.dispose()
    again = init_db(path)
    try:
        with again.connect() as conn:
            assert conn.exec_driver_sql('SELECT revenue FROM financials').scalar_one() == '123456789.125'
            assert conn.exec_driver_sql('SELECT count(*) FROM financial_fetch_completeness').scalar_one() == 0
            assert conn.exec_driver_sql("SELECT value FROM settings WHERE key='fin_fetch_version'").scalar_one() == 'old-cache-version'
    finally:
        again.dispose()


def test_actual_pre_guard_startup_rejected_before_legacy_rebuild(tmp_path):
    """Run the prior committed program, not a simulated old-version boolean."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    from sqlalchemy.orm import Session
    from app.models import Financial, Setting
    from decimal import Decimal
    root = Path(__file__).resolve().parents[1]
    old_root = tmp_path / 'prior-program'
    (old_root / 'app').mkdir(parents=True)
    for name in ('__init__.py', 'db.py', 'models.py', 'discovery_models.py', 'selection_models.py'):
        original = subprocess.run(['git', 'show', 'e23a8da:app/' + name], cwd=root,
                                  check=True, capture_output=True).stdout
        (old_root / 'app' / name).write_bytes(original)
    path = tmp_path / 'upgraded.db'
    engine = init_db(path)
    with Session(engine) as session:
        session.add(Financial(code='000001.SZ', period='2025', period_type='annual', revenue=Decimal('123.4500')))
        session.add(Setting(key='fin_fetch_version', value='old-retained-cache'))
        session.commit()
    engine.dispose()
    before = path.read_bytes()
    with sqlite3.connect(path) as connection:
        schema_before = connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        rows_before = list(connection.iterdump())
    # Only app's historical code is isolated; unchanged pure imports resolve to
    # this checkout. The imported db/models/engine/migration sequence is old.
    result = subprocess.run([sys.executable, '-c',
        'from pathlib import Path; from app.db import init_db; import sys; init_db(Path(sys.argv[1]))', str(path)],
        cwd=old_root, env={**os.environ, 'PYTHONPATH': str(root)}, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'no such function: selection_writer_version' in result.stderr
    assert "UPDATE ruler_versions SET scope = 'funnel'" in result.stderr
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall() == schema_before
        assert list(connection.iterdump()) == rows_before
    assert path.read_bytes() == before
