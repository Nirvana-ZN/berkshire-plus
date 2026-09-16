from __future__ import annotations

from unittest.mock import MagicMock

import httpx2

from research.search import fetch_hits, tavily_search


def test_tavily_search_maps_results(monkeypatch) -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "results": [
            {"title": "中报", "url": "https://example.com/a", "content": "销量下降 3%"},
            {"title": "", "url": "https://example.com/b", "content": ""},
        ]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)

    hits = tavily_search("重庆啤酒 2026 中报", api_key="tvly-test")
    assert hits == [
        {"title": "中报", "url": "https://example.com/a", "content": "销量下降 3%"}
    ]
    sent = mock_client.post.call_args
    assert sent.args[0] == "https://api.tavily.com/search"
    assert sent.kwargs["headers"]["Authorization"] == "Bearer tvly-test"
    assert sent.kwargs["json"]["max_results"] == 5


def test_tavily_search_http_error_is_empty(monkeypatch) -> None:
    response = MagicMock()
    response.status_code = 401
    mock_client = MagicMock()
    mock_client.post.return_value = response
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)
    assert tavily_search("query", api_key="tvly-test") == []


def test_fetch_hits_without_key_is_empty(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert fetch_hits("重庆啤酒", "600132.SH", "2026-09-02") == []
