from __future__ import annotations

from unittest.mock import MagicMock

import httpx2

from market.cninfo import _pick_notices, fetch_notices


def test_fetch_notices_maps_titles(monkeypatch) -> None:
    lookup = MagicMock()
    lookup.status_code = 200
    lookup.json.return_value = [{"code": "600132", "orgId": "gssh0600132", "zwjc": "重庆啤酒"}]
    query = MagicMock()
    query.status_code = 200
    query.json.return_value = {
        "announcements": [
            {
                "announcementId": "1",
                "announcementTitle": "2026年半年度报告",
                "announcementTime": 1787328000000,
            },
            {
                "announcementId": "2",
                "announcementTitle": "<em>分配</em>预案：不分配不转增",
                "announcementTime": 1787241600000,
            },
            {
                "announcementId": "3",
                "announcementTitle": "关于召开业绩说明会的公告",
                "announcementTime": 1787155200000,
            },
        ]
    }
    mock_client = MagicMock()
    mock_client.post.side_effect = [lookup, query, query]
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)

    hits = fetch_notices("600132.SH")
    titles = [row["title"] for row in hits]
    assert "2026年半年度报告" in titles
    assert "分配预案：不分配不转增" in titles
    assert all("业绩说明会" not in row["title"] for row in hits)
    assert hits[0]["url"].startswith("https://www.cninfo.com.cn/new/disclosure/detail")


def test_fetch_notices_http_error_is_empty(monkeypatch) -> None:
    mock_client = MagicMock()
    mock_client.post.side_effect = RuntimeError("timeout")
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    monkeypatch.setattr(httpx2, "Client", lambda **_kwargs: mock_client)
    assert fetch_notices("600132.SH") == []


def test_pick_notices_dedupes() -> None:
    rows = [
        {"announcementId": "1", "announcementTitle": "权益分派实施公告", "announcementTime": 1},
        {"announcementId": "1", "announcementTitle": "权益分派实施公告", "announcementTime": 1},
    ]
    got = _pick_notices(rows, org_id="x", code6="600132")
    assert len(got) == 1
