from __future__ import annotations

import os
from pathlib import Path

from app.db import PROJECT_ROOT

DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
OPENAI_API_KEY = "OPENAI_API_KEY"
OPENAI_BASE_URL = "OPENAI_BASE_URL"
TAVILY_API_KEY = "TAVILY_API_KEY"


def resolve_env_path(env_path: Path | str | None = None) -> Path:
    if env_path is None:
        return DEFAULT_ENV_PATH
    return Path(env_path)


def read_env_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8-sig")
    found: str | None = None
    for raw in text.splitlines():
        parsed = _parse_env_line(raw)
        if parsed is None:
            continue
        name, value = parsed
        if name == key:
            found = value
    if found is None:
        return None
    return found or None


def has_env_value(path: Path, key: str) -> bool:
    return read_env_value(path, key) is not None


def write_env_value(path: Path, key: str, value: str) -> None:
    existing = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    newline = "\n"
    if "\r\n" in existing:
        newline = "\r\n"
    lines = existing.splitlines()
    encoded = f"{key}={_encode_value(value)}"
    replaced = False
    out: list[str] = []
    for line in lines:
        parsed = _parse_env_line(line)
        if parsed is not None and parsed[0] == key:
            out.append(encoded)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1] != "":
            out.append(encoded)
        else:
            if out and out[-1] == "":
                out[-1] = encoded
            else:
                out.append(encoded)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = newline.join(out)
    if body and not body.endswith(("\n", "\r")):
        body += newline
    path.write_text(body, encoding="utf-8")


def hot_load(key: str, value: str) -> None:
    os.environ[key] = value


def load_env_key(path: Path, key: str) -> str | None:
    value = read_env_value(path, key)
    if value is not None:
        hot_load(key, value)
    return value


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None
    name, _, raw_value = stripped.partition("=")
    name = name.strip()
    if not name:
        return None
    return name, _decode_value(raw_value)


def _encode_value(value: str) -> str:
    if any(ch in value for ch in (" ", "\t", "#", "'", '"')):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _decode_value(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        inner = text[1:-1]
        return inner.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    comment_at = text.find(" #")
    if comment_at >= 0:
        text = text[:comment_at].rstrip()
    return text.strip()
