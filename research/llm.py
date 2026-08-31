from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import Protocol

from app.envfile import OPENAI_API_KEY

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete(self, prompt: str, *, name: str = "") -> str:
        """Return model text. Tests inject FakeLLM."""


class FakeLLM:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        payloads: Mapping[str, dict] | None = None,
        fail_names: Sequence[str] = (),
        error: Exception | None = None,
        raw: str | None = None,
        gate: object | None = None,
    ) -> None:
        self.payload = payload
        self.payloads = dict(payloads or {})
        self.fail_names = set(fail_names)
        self.error = error
        self.raw = raw
        self.gate = gate
        self.calls: list[str] = []

    def complete(self, prompt: str, *, name: str = "") -> str:
        self.calls.append(name or prompt[:40])
        if self.gate is not None:
            wait = getattr(self.gate, "wait", None)
            if callable(wait):
                wait(timeout=10)
        if self.error is not None:
            raise self.error
        if name in self.fail_names:
            raise LLMError(f"评估失败: {name}")
        if name in self.payloads:
            return json.dumps(self.payloads[name], ensure_ascii=False)
        if self.raw is not None:
            return self.raw
        if self.payload is not None:
            return json.dumps(self.payload, ensure_ascii=False)
        return json.dumps(_default_payload(), ensure_ascii=False)


class OpenAICompatClient:
    def __init__(
        self,
        *,
        model: str = "",
        base_url: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self.timeout = timeout

    def complete(self, prompt: str, *, name: str = "") -> str:
        key = os.environ.get(OPENAI_API_KEY) or ""
        if not key:
            raise LLMError("未配置模型密钥")
        model = self.model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        base = (os.environ.get("OPENAI_BASE_URL") or self.base_url or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        url = f"{base}/chat/completions"
        payload = _chat_payload(model, prompt)
        import httpx2

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with httpx2.Client(timeout=self.timeout) as client:
                    response = client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    if _retryable_http(response) and attempt < 2:
                        last_error = LLMError(_http_error(response))
                        time.sleep(2 * (attempt + 1))
                        continue
                    if response.status_code >= 400:
                        raise LLMError(_http_error(response))
                    body = response.json()
                return _message_text(body)
            except LLMError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= 2:
                    break
        if last_error is not None:
            raise LLMError(str(last_error)) from last_error
        raise LLMError("模型请求失败")


def _chat_payload(model: str, prompt: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    if model.lower().startswith("glm"):
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "low"
        payload["max_tokens"] = 8192
    return payload


def _error_body(response: object) -> dict[str, object] | None:
    json_fn = getattr(response, "json", None)
    if callable(json_fn):
        try:
            data = json_fn()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def _provider_error(response: object) -> tuple[str, str]:
    data = _error_body(response)
    if not data:
        return "", ""
    err = data.get("error")
    if not isinstance(err, dict):
        return "", ""
    return str(err.get("code") or ""), str(err.get("message") or "")


def _retryable_http(response: object) -> bool:
    if getattr(response, "status_code", 0) != 429:
        return False
    code, _message = _provider_error(response)
    return code not in {"1113", "1110"}


def _http_error(response: object) -> str:
    status = getattr(response, "status_code", "?")
    code, message = _provider_error(response)
    if code == "1113":
        return (
            "智谱账户余额不足或没有可用资源包。"
            "开放平台按量调用不能用 Coding Plan 额度，"
            "请到 open.bigmodel.cn 充值或购买资源包后再试"
        )
    if message:
        label = f"（{code}）" if code else ""
        return f"模型 HTTP {status}{label}: {message}"
    text = getattr(response, "text", "") or ""
    snippet = " ".join(text.split())[:240]
    if snippet:
        return f"模型 HTTP {status}: {snippet}"
    return f"模型 HTTP {status}"


def _message_text(body: object) -> str:
    try:
        message = body["choices"][0]["message"]  # type: ignore[index]
        for key in ("content", "reasoning_content"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError("模型返回无法解析") from exc
    raise LLMError("模型返回无法解析")


def _default_payload() -> dict[str, object]:
    gates = [
        "能力圈",
        "好生意",
        "护城河",
        "管理层",
        "安全边际",
        "决策纪律",
    ]
    return {
        "verdict": "pass",
        "gates": [{"name": item, "status": "pass", "reason": f"{item}通过"} for item in gates],
        "mirror_test": [
            "这是一家我能看懂的生意。",
            "管理层把股东当合伙人。",
            "十年后这家公司大概率更好。",
            "现在的价格有安全边际。",
            "我不需要靠别人提醒才会持有。",
        ],
        "price_bands": {
            "mode": "steady",
            "aggressive": "激进买入带",
            "steady": "稳健买入带",
            "conservative": "保守买入带",
        },
        "memo": {
            "essence": "主业清晰，能用已抓取财务理解。",
            "risks": "竞争与估值波动。",
            "role": "satellite",
        },
        "data_grade": "B",
        "as_of": {"date": "2026-08-29", "price": None},
        "portfolio_fit": {
            "should_enter": "对照当前仓与现金，可作卫星仓考虑。",
            "funding": "用现金买，不卖旧仓。",
            "overlap": "与现有持仓逻辑不重叠。",
        },
    }
