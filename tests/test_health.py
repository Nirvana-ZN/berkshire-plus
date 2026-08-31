from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from market.quotes import FakeQuoteClient

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"


def _assert_single_user(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users ORDER BY id")).fetchall()
    assert rows == [(1,)]


def test_health_ok(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "app.db",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
    )
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert 200 <= response.status_code < 300
    body = response.json()
    assert body["status"] == "ok"
    assert body["lock"] is None


def test_seed_user_id_1_on_first_start(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with TestClient(
        create_app(
            db_path=db_path,
            quote_client=FakeQuoteClient(),
            env_path=tmp_path / ".env",
        )
    ) as client:
        assert client.get("/api/health").status_code == 200
    _assert_single_user(db_path)


def test_restart_keeps_single_user_id_1(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with TestClient(
        create_app(
            db_path=db_path,
            quote_client=FakeQuoteClient(),
            env_path=tmp_path / ".env",
        )
    ) as client:
        assert client.get("/api/health").status_code == 200
    with TestClient(
        create_app(
            db_path=db_path,
            quote_client=FakeQuoteClient(),
            env_path=tmp_path / ".env",
        )
    ) as client:
        assert client.get("/api/health").status_code == 200
    _assert_single_user(db_path)


def test_open_workbench_chinese_entries_no_login() -> None:
    home = (ROOT / "web/src/views/HomeView.vue").read_text(encoding="utf-8")
    app_vue = (ROOT / "web/src/App.vue").read_text(encoding="utf-8")
    router = (ROOT / "web/src/router/index.ts").read_text(encoding="utf-8")
    combined = "\n".join([home, app_vue, router])

    assert "看仓" in combined
    assert "选股" in combined
    assert 'to="/positions"' in home
    assert 'to="/screen"' in home
    assert "不构成投资建议" in app_vue
    assert '"/login"' not in router
    assert '"/register"' not in router
    assert not list((ROOT / "web/src/views").glob("*Login*"))
    assert not list((ROOT / "web/src/views").glob("*Register*"))


def test_main_pages_show_disclaimer() -> None:
    app_vue = (ROOT / "web/src/App.vue").read_text(encoding="utf-8")
    router = (ROOT / "web/src/router/index.ts").read_text(encoding="utf-8")
    assert "不构成投资建议" in app_vue
    for path in ("/", "/positions", "/screen", "/settings"):
        assert path in router


def test_empty_screen_shell_does_not_invent_list() -> None:
    screen = (ROOT / "web/src/views/ScreenView.vue").read_text(encoding="utf-8")
    app_vue = (ROOT / "web/src/App.vue").read_text(encoding="utf-8")
    assert "选股" in screen
    assert "尚未运行漏斗" in screen
    assert "不构成投资建议" in app_vue
    assert "今日推荐" not in screen
    assert "收益排行" not in screen
    assert "<ul" not in screen.lower()
    assert "<ol" not in screen.lower()


def test_scaffold_has_router_without_pinia() -> None:
    package = (ROOT / "web/package.json").read_text(encoding="utf-8")
    assert "vue-router" in package
    assert "pinia" not in package.lower()
    vite = (ROOT / "web/vite.config.ts").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8765" in vite
    assert "0.0.0.0" not in vite


def test_process_does_not_bind_all_interfaces() -> None:
    start = (APP_DIR / "__main__.py").read_text(encoding="utf-8")
    assert 'host="127.0.0.1"' in start
    assert "8765" in start
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "0.0.0.0" not in text, f"{path} 不得绑定全部接口"


def _assert_error_envelope(body: dict) -> None:
    assert set(body) >= {"error", "code", "detail"}
    assert body["error"]
    assert body["code"]
    assert body["detail"]


def test_fastapi_hosts_spa_when_dist_exists(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        (
            "<!doctype html><html lang='zh-CN'><body>"
            "<a href='/positions'>看仓</a>"
            "<a href='/screen'>选股</a>"
            "<p>不构成投资建议</p>"
            "</body></html>"
        ),
        encoding="utf-8",
    )
    js_bytes = b"window.__WORKBENCH__=1;"
    (assets / "app.js").write_bytes(js_bytes)
    app = create_app(
        db_path=tmp_path / "app.db",
        dist_path=dist,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
    )
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "看仓" in home.text
        assert "选股" in home.text
        assert "不构成投资建议" in home.text
        screen = client.get("/screen")
        assert screen.status_code == 200
        assert "不构成投资建议" in screen.text
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.content == js_bytes
        assert b"<!doctype" not in asset.content.lower()
        unknown_api = client.get("/api/no-such")
        assert unknown_api.status_code == 404
        _assert_error_envelope(unknown_api.json())
        missing_asset = client.get("/assets/missing.js")
        assert missing_asset.status_code == 404
        _assert_error_envelope(missing_asset.json())
        assert client.get("/api/health").status_code == 200


def test_missing_dist_root_is_envelope_404(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "app.db",
        dist_path=tmp_path / "no-dist",
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
    )
    with TestClient(app) as client:
        missing = client.get("/")
        assert missing.status_code == 404
        _assert_error_envelope(missing.json())
        health = client.get("/api/health")
        assert 200 <= health.status_code < 300


def test_create_app_default_db_path_seeds_user(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "app.db"
    monkeypatch.setattr("app.db.DEFAULT_DB_PATH", db_path)
    with TestClient(
        create_app(
            quote_client=FakeQuoteClient(),
            env_path=tmp_path / ".env",
        )
    ) as client:
        assert client.get("/api/health").status_code == 200
    _assert_single_user(db_path)
