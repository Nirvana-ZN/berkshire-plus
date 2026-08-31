from __future__ import annotations

from unittest.mock import MagicMock

import httpx2

from research.llm import LLMError, OpenAICompatClient, _chat_payload, _message_text


def test_glm_payload_enables_thinking() -> None:
    payload = _chat_payload("glm-5.3", "只返回 JSON")
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"
    assert payload["response_format"] == {"type": "json_object"}


def test_openai_payload_has_no_thinking() -> None:
    payload = _chat_payload("gpt-4o-mini", "只返回 JSON")
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload


def test_message_text_falls_back_to_reasoning_content() -> None:
    text = _message_text(
        {"choices": [{"message": {"content": "", "reasoning_content": '{"ping":"ok"}'}}]}
    )
    assert text == '{"ping":"ok"}'


def test_complete_sends_glm_thinking(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": '{"ping":"ok"}'}}]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)

    text = OpenAICompatClient(model="glm-5.3").complete("ping", name="ping")
    assert text == '{"ping":"ok"}'
    sent = mock_client.post.call_args.kwargs["json"]
    assert sent["model"] == "glm-5.3"
    assert sent["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "low"


def test_complete_retries_429_then_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("research.llm.time.sleep", lambda _seconds: None)
    busy = MagicMock()
    busy.status_code = 429
    busy.text = '{"error":{"code":"1302","message":"rate limited"}}'
    ok = MagicMock()
    ok.status_code = 200
    ok.json.return_value = {"choices": [{"message": {"content": '{"ping":"ok"}'}}]}
    mock_client = MagicMock()
    mock_client.post.side_effect = [busy, ok]
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)

    text = OpenAICompatClient(model="glm-5.3").complete("ping")
    assert text == '{"ping":"ok"}'
    assert mock_client.post.call_count == 2


def test_complete_surfaces_http_body_on_error(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("research.llm.time.sleep", lambda _seconds: None)
    busy = MagicMock()
    busy.status_code = 429
    busy.text = '{"error":{"code":"1302","message":"rate limited"}}'
    busy.json.return_value = {"error": {"code": "1302", "message": "rate limited"}}
    mock_client = MagicMock()
    mock_client.post.return_value = busy
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)

    try:
        OpenAICompatClient(model="glm-5.3").complete("ping")
    except LLMError as exc:
        assert "429" in str(exc)
        assert "rate limited" in str(exc)
        assert mock_client.post.call_count == 3
    else:
        raise AssertionError("expected LLMError")


def test_complete_does_not_retry_insufficient_balance(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("research.llm.time.sleep", lambda _seconds: None)
    busy = MagicMock()
    busy.status_code = 429
    busy.json.return_value = {
        "error": {"code": "1113", "message": "余额不足或无可用资源包,请充值。"}
    }
    mock_client = MagicMock()
    mock_client.post.return_value = busy
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)

    try:
        OpenAICompatClient(model="glm-5.3").complete("ping")
    except LLMError as exc:
        assert "余额不足" in str(exc)
        assert mock_client.post.call_count == 1
    else:
        raise AssertionError("expected LLMError")
