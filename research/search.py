from __future__ import annotations

import os

from app.envfile import TAVILY_API_KEY

TAVILY_URL = "https://api.tavily.com/search"
MAX_RESULTS = 5
CONTENT_LIMIT = 400


def fetch_hits(
    name: str,
    code: str,
    as_of_date: str,
    *,
    api_key: str | None = None,
) -> list[dict[str, str]]:
    key = (api_key or os.environ.get(TAVILY_API_KEY) or "").strip()
    if not key:
        return []
    year = (as_of_date or "")[:4]
    query = f"{name} {code} {year} 中报 年报 分红 销量 股权".strip()
    return tavily_search(query, api_key=key)


def tavily_search(query: str, *, api_key: str, timeout: float = 20.0) -> list[dict[str, str]]:
    if not query or not api_key:
        return []
    import httpx2

    try:
        with httpx2.Client(timeout=timeout) as client:
            response = client.post(
                TAVILY_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "max_results": MAX_RESULTS, "search_depth": "basic"},
            )
            if response.status_code >= 400:
                return []
            body = response.json()
    except Exception:
        return []
    return _hits_from_body(body)


def _hits_from_body(body: object) -> list[dict[str, str]]:
    if not isinstance(body, dict):
        return []
    rows = body.get("results")
    if not isinstance(rows, list):
        return []
    hits: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        content = str(row.get("content") or "").strip()
        if not title and not content:
            continue
        if len(content) > CONTENT_LIMIT:
            content = content[:CONTENT_LIMIT]
        hits.append({"title": title, "url": url, "content": content})
        if len(hits) >= MAX_RESULTS:
            break
    return hits
