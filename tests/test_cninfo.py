from __future__ import annotations

from unittest.mock import MagicMock
from datetime import date
import json

import pytest

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


def event_transport(monkeypatch, pages):
    """Replace only the HTTP boundary; preserve actual lookup/page parsing."""
    calls = []
    gates = []

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body
            self.content = json.dumps(body, ensure_ascii=False).encode('utf-8')

        def json(self):
            return self.body

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, *, data, headers):
            # A missing policy check means an unauthorized network request.
            assert len(gates) == len(calls) + 1
            calls.append((url, data))
            if len(calls) == 1:
                return Response([{'code': '000001', 'orgId': 'fixture-org'}])
            page = pages[int(data['pageNum']) - 1]
            if isinstance(page, Exception):
                raise page
            return Response(page)

    monkeypatch.setattr(httpx2, 'Client', lambda **_kwargs: Client())
    return calls, lambda: gates.append('request')


def event_page(ids, total=31, pages=2):
    return {'totalAnnouncement': total, 'totalpages': pages, 'announcements': [
        {'announcementId': str(i), 'announcementTitle': '回购股份注销实施公告',
         'announcementTime': 1780243200000} for i in ids]}


def test_event_range_fetch_is_paginated_deduped_and_listing_remains_incomplete(monkeypatch):
    from market.cninfo import fetch_event_evidence
    calls, gate = event_transport(monkeypatch, [event_page(range(30)), event_page([29, 30])])
    rows, coverage = fetch_event_evidence('000001.SZ', date(2026, 1, 1), date(2026, 6, 30), before_request=gate)
    assert len(rows) == 31
    assert all(row.status == 'untrusted' and row.kind == 'capital_notice' for row in rows)
    assert all(row.date_only for row in rows)
    assert coverage.complete is False  # Listing pages cannot verify action details.
    assert coverage.evidence_ids == tuple(row.evidence_id for row in rows)
    assert len(calls) == 3
    assert calls[1][1]['seDate'] == '2026-01-01~2026-06-30'
    assert calls[2][1]['pageNum'] == '2'


def test_interrupted_event_pagination_retains_sources_and_incomplete_coverage(monkeypatch):
    from market.cninfo import fetch_event_evidence
    calls, gate = event_transport(monkeypatch, [event_page(range(30)), RuntimeError('timeout')])
    rows, coverage = fetch_event_evidence('000001.SZ', date(2026, 1, 1), date(2026, 6, 30), before_request=gate)
    assert len(rows) == 30
    assert coverage.complete is False
    assert len(calls) == 3


def test_request_policy_interruption_propagates_before_network(monkeypatch):
    from market.cninfo import fetch_event_evidence
    calls, _gate = event_transport(monkeypatch, [])

    def cancelled():
        raise RuntimeError('request budget exhausted')

    with pytest.raises(RuntimeError, match='budget'):
        fetch_event_evidence('000001.SZ', date(2026, 1, 1), date(2026, 6, 30), before_request=cancelled)
    assert calls == []


@pytest.mark.parametrize('body', [
    {'announcements': []}, event_page([], total=1, pages=1),
    event_page([], total=0, pages=0), {'totalAnnouncement': 'bad', 'announcements': []},
])
def test_missing_or_empty_listing_metadata_cannot_certify_zero_events(monkeypatch, body):
    from market.cninfo import fetch_event_evidence
    _calls, gate = event_transport(monkeypatch, [body])
    rows, coverage = fetch_event_evidence('000001.SZ', date(2026, 1, 1), date(2026, 6, 30), before_request=gate)
    assert rows == ()
    assert coverage.complete is False
