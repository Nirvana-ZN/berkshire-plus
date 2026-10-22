from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.discovery_routes as discovery_routes
from app.main import create_app
from jobs.discovery import builtin_track_executor, drain_discovery_subprocess
from market.quotes import FakeQuoteClient


def test_workbench_offloads_only_without_injected_clock(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BERKSHIRE_DISCOVERY_OFFLOAD", "1")
    app = create_app(
        db_path=tmp_path / "app.db",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
    )
    with TestClient(app):
        assert app.state.discovery_offload_drain is True
    injected = create_app(
        db_path=tmp_path / "clock.db",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        discovery_clock=lambda: datetime.now(timezone.utc),
    )
    with TestClient(injected):
        assert injected.state.discovery_offload_drain is False


def test_offloaded_drain_spawns_a_child_instead_of_running_inline(
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class _Process:
        pid = 0
        exitcode = 0

        def start(self) -> None:
            calls["started"] = True

        def join(self) -> None:
            calls["joined"] = True

    class _Context:
        def Process(self, *, target, args):
            calls["target"] = target
            calls["args"] = args
            return _Process()

    monkeypatch.setattr(
        "multiprocessing.get_context", lambda _method: _Context()
    )

    def _refuse(*_args, **_kwargs):
        raise AssertionError("drain ran inside the API process")

    monkeypatch.setattr(discovery_routes, "drain_run_queue", _refuse)
    app = SimpleNamespace(
        state=SimpleNamespace(
            discovery_offload_drain=True,
            engine=SimpleNamespace(
                url=SimpleNamespace(database="D:/data/app.db")
            ),
        )
    )
    discovery_routes._drain(app, 7, "token")
    assert calls["target"] is drain_discovery_subprocess
    assert calls["args"] == ("D:/data/app.db", 7, "token")
    assert calls["joined"] is True


def test_drain_discovery_subprocess_passes_track_executor(
    monkeypatch, tmp_path: Path
) -> None:
    from app.db import init_db

    db_path = tmp_path / "app.db"
    engine = init_db(db_path)
    engine.dispose()

    seen: dict[str, object] = {}

    def fake_drain(_session, run_id, **kwargs):
        seen.update(kwargs)
        from jobs.discovery import DrainOutcome

        return DrainOutcome(run_id, True, 0)

    monkeypatch.setattr("jobs.discovery.drain_run_queue", fake_drain)
    drain_discovery_subprocess(str(db_path), 1, "owner")
    assert seen.get("track_executor") is builtin_track_executor
