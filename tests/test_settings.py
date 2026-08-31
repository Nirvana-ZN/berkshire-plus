from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.envfile import OPENAI_API_KEY, OPENAI_BASE_URL, read_env_value, write_env_value
from app.main import create_app
from market.quotes import FakeQuoteClient

ROOT = Path(__file__).resolve().parents[1]
SECRET = "sk-test-do-not-leak-xyz"

def _engine(db_path: Path):
    return create_engine(f"sqlite:///{db_path.resolve().as_posix()}")


def _rows(db_path: Path, sql: str):
    engine = _engine(db_path)
    with engine.connect() as conn:
        return list(conn.execute(text(sql)).mappings())


def _client(tmp_path: Path, *, env_name: str = ".env", db_name: str = "app.db"):
    return TestClient(
        create_app(
            db_path=tmp_path / db_name,
            env_path=tmp_path / env_name,
            quote_client=FakeQuoteClient(),
        )
    )


def _assert_error_envelope(body: dict) -> None:
    assert set(body) >= {"error", "code", "detail"}
    assert body["error"]
    assert body["code"]
    assert body["detail"]


def _assert_no_secret(payload: object, secret: str = SECRET) -> None:
    text = str(payload)
    assert secret not in text
    assert "OPENAI_API_KEY" not in text


def _assert_settings_table_has_no_secret(db_path: Path, secret: str = SECRET) -> None:
    rows = _rows(db_path, "SELECT key, value FROM settings")
    for row in rows:
        assert secret not in str(row["key"])
        assert secret not in str(row["value"])
        assert row["key"] not in {"OPENAI_API_KEY", "api_key"}


def test_save_key_and_model_writes_env_not_table(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(OPENAI_API_KEY, raising=False)
    env_path = tmp_path / ".env"
    write_env_value(env_path, "OTHER", "keep")
    db_path = tmp_path / "app.db"
    with _client(tmp_path) as client:
        response = client.post(
            "/api/settings",
            json={"api_key": SECRET, "model": "gpt-test", "web_search": True},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["has_api_key"] is True
        assert body["llm_model"] == "gpt-test"
        assert body["web_search"] is True
        assert "已热加载" in body["message"]
        assert SECRET not in response.text
        assert "api_key" not in body
        assert OPENAI_API_KEY not in body
        _assert_no_secret(body)

        got = client.get("/api/settings")
        assert got.status_code == 200
        listed = got.json()
        assert listed["has_api_key"] is True
        assert listed["llm_model"] == "gpt-test"
        assert listed["web_search"] is True
        assert SECRET not in got.text
        assert "api_key" not in listed
        _assert_no_secret(listed)

    assert read_env_value(env_path, OPENAI_API_KEY) == SECRET
    assert read_env_value(env_path, "OTHER") == "keep"
    assert os.environ[OPENAI_API_KEY] == SECRET
    _assert_settings_table_has_no_secret(db_path)
    model_rows = _rows(db_path, "SELECT key, value FROM settings WHERE key IN ('llm_model', 'web_search')")
    values = {row["key"]: row["value"] for row in model_rows}
    assert values["llm_model"] == "gpt-test"
    assert values["web_search"] == "true"


def test_empty_key_is_400_and_changes_nothing(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    write_env_value(env_path, OPENAI_API_KEY, "keep-me")
    write_env_value(env_path, "OTHER", "stay")
    db_path = tmp_path / "app.db"
    before_env = env_path.read_text(encoding="utf-8")
    with _client(tmp_path) as client:
        client.post("/api/settings", json={"model": "kept-model", "web_search": False})
        before_rows = _rows(db_path, "SELECT key, value FROM settings ORDER BY key")
        for blank in ("", "   ", "\t"):
            response = client.post(
                "/api/settings",
                json={"api_key": blank, "model": "should-not-save", "web_search": True},
            )
            assert response.status_code == 400
            _assert_error_envelope(response.json())
        assert env_path.read_text(encoding="utf-8") == before_env
        after_rows = _rows(db_path, "SELECT key, value FROM settings ORDER BY key")
        assert after_rows == before_rows
        listed = client.get("/api/settings").json()
        assert listed["llm_model"] == "kept-model"
        assert listed["web_search"] is False


def test_get_does_not_leak_saved_key(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        saved = client.post(
            "/api/settings",
            json={"api_key": SECRET, "model": "gpt-test", "web_search": False},
        )
        assert saved.status_code == 200
        listed = client.get("/api/settings")
        assert listed.status_code == 200
        body = listed.json()
        assert body["has_api_key"] is True
        assert SECRET not in listed.text
        assert "api_key" not in body
        dumped = listed.text
        assert SECRET not in dumped
        _assert_no_secret(body)


def test_model_only_does_not_touch_env(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    write_env_value(env_path, OPENAI_API_KEY, "original-key")
    write_env_value(env_path, "OTHER", "keep")
    before = env_path.read_text(encoding="utf-8")
    with _client(tmp_path) as client:
        response = client.post(
            "/api/settings",
            json={"model": "only-model", "web_search": True},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["llm_model"] == "only-model"
        assert body["web_search"] is True
        assert body["has_api_key"] is True
        assert body["message"] == "已保存"
        assert "api_key" not in body
    assert env_path.read_text(encoding="utf-8") == before
    assert read_env_value(env_path, OPENAI_API_KEY) == "original-key"


def test_gitignore_ignores_env_and_db() -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines() if line.strip()}
    assert ".env" in lines
    assert "data/app.db" in lines


def test_does_not_write_repo_env(tmp_path: Path) -> None:
    repo_env = ROOT / ".env"
    before = repo_env.read_bytes() if repo_env.is_file() else None
    with _client(tmp_path) as client:
        response = client.post(
            "/api/settings",
            json={"api_key": SECRET, "model": "gpt-test", "web_search": False},
        )
        assert response.status_code == 200
    after = repo_env.read_bytes() if repo_env.is_file() else None
    assert before == after
    assert (tmp_path / ".env").is_file()
    assert read_env_value(tmp_path / ".env", OPENAI_API_KEY) == SECRET


def test_settings_view_form_does_not_echo_key() -> None:
    source = (ROOT / "web/src/views/SettingsView.vue").read_text(encoding="utf-8")
    assert "/api/settings" in source
    assert 'type="password"' in source
    assert "已保存" in source
    assert "已热加载" in source or "data.message" in source
    assert "api_key" in source
    assert "llm_model" in source
    assert "llm_base_url" in source
    assert "接口地址" in source
    assert "web_search" in source
    assert "已迁入" in source
    assert "未迁入" in source
    assert "迁入路径" in source
    assert "if (typed)" in source
    assert "body.api_key = typed" in source
    assert 'value="********"' not in source
    assert 'value="****"' not in source
    assert "ready" in source


def test_web_search_defaults_false_and_model_may_be_empty(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        listed = client.get("/api/settings").json()
        assert listed["has_api_key"] is False
        assert listed["llm_model"] == ""
        assert listed["llm_base_url"] == ""
        assert listed["web_search"] is False
        saved = client.post("/api/settings", json={"model": "", "web_search": False})
        assert saved.status_code == 200
        body = saved.json()
        assert body["llm_model"] == ""
        assert body["web_search"] is False
        assert body["has_api_key"] is False


def test_startup_loads_key_from_env_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(OPENAI_API_KEY, raising=False)
    env_path = tmp_path / ".env"
    write_env_value(env_path, OPENAI_API_KEY, SECRET)
    with _client(tmp_path) as client:
        listed = client.get("/api/settings")
        assert listed.status_code == 200
        body = listed.json()
        assert body["has_api_key"] is True
        assert SECRET not in listed.text
        _assert_no_secret(body)
    assert os.environ[OPENAI_API_KEY] == SECRET


def test_read_env_strips_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfOPENAI_API_KEY=bom-key\n")
    assert read_env_value(path, OPENAI_API_KEY) == "bom-key"


def test_save_base_url_writes_env_and_hot_loads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(OPENAI_BASE_URL, raising=False)
    env_path = tmp_path / ".env"
    write_env_value(env_path, OPENAI_API_KEY, "keep-me")
    db_path = tmp_path / "app.db"
    url = "https://open.bigmodel.cn/api/coding/paas/v4"
    with _client(tmp_path) as client:
        response = client.post(
            "/api/settings",
            json={"model": "glm-5.3", "web_search": False, "base_url": url + "/"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["llm_base_url"] == url
        assert body["message"] == "已热加载"
        listed = client.get("/api/settings").json()
        assert listed["llm_base_url"] == url
        empty = client.post(
            "/api/settings",
            json={"model": "glm-5.3", "web_search": False, "base_url": "  "},
        )
        assert empty.status_code == 400
        _assert_error_envelope(empty.json())
    assert read_env_value(env_path, OPENAI_BASE_URL) == url
    assert os.environ[OPENAI_BASE_URL] == url
    _assert_settings_table_has_no_secret(db_path)
