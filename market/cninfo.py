from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from discovery.fundamental_types import CoverageWindow, EvidenceRecord, evidence_identity

LOOKUP_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
DETAIL_URL = "https://www.cninfo.com.cn/new/disclosure/detail"
KEEP_TOKENS = ("分红", "分配", "派息", "不分配", "股权", "股东", "回购", "减持", "增持", "权益分派")
REPORT_TOKENS = ("半年度报告", "年度报告", "一季度报告", "三季度报告")
MAX_NOTICES = 8
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_notices(code: str, *, timeout: float = 20.0) -> list[dict[str, str]]:
    code6 = _code6(code)
    if not code6:
        return []
    import httpx2

    try:
        with httpx2.Client(timeout=timeout) as client:
            org = _lookup_org(client, code6)
            if org is None:
                return []
            stock = f"{org['code']},{org['orgId']}"
            column = _column(code)
            recent = _query(client, stock, column, searchkey="", page_size=30)
            dividends = _query(client, stock, column, searchkey="分配", page_size=10)
    except Exception:
        return []
    return _pick_notices(recent + dividends, org_id=str(org["orgId"]), code6=code6)


def _code6(code: str) -> str:
    raw = (code or "").strip().upper().split(".")[0]
    return raw if raw.isdigit() and len(raw) == 6 else ""


def _column(code: str) -> str:
    text = (code or "").strip().upper()
    raw = text.split(".")[0]
    if text.endswith(".SH") or raw.startswith("6"):
        return "sse"
    if text.endswith(".BJ") or raw.startswith(("8", "4")):
        return "bj"
    return "szse"


def _lookup_org(client: Any, code6: str) -> dict[str, str] | None:
    response = client.post(LOOKUP_URL, data={"keyWord": code6, "maxNum": "10"}, headers=_HTTP_HEADERS)
    if response.status_code >= 400:
        return None
    rows = response.json()
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("code") or "") == code6 and row.get("orgId"):
            return {"code": code6, "orgId": str(row["orgId"])}
    return None


def _query(client: Any, stock: str, column: str, *, searchkey: str, page_size: int) -> list[dict]:
    response = client.post(
        QUERY_URL,
        data={
            "pageNum": "1",
            "pageSize": str(page_size),
            "column": column,
            "tabName": "fulltext",
            "plate": "",
            "stock": stock,
            "searchkey": searchkey,
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        },
        headers=_HTTP_HEADERS,
    )
    if response.status_code >= 400:
        return []
    body = response.json()
    if not isinstance(body, dict):
        return []
    rows = body.get("announcements")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _pick_notices(rows: list[dict], *, org_id: str, code6: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    picked: list[dict[str, str]] = []
    for row in rows:
        notice_id = str(row.get("announcementId") or "").strip()
        title = _plain_title(row.get("announcementTitle"))
        if not notice_id or notice_id in seen or not title:
            continue
        if not _keep_title(title):
            continue
        seen.add(notice_id)
        picked.append(
            {
                "date": _notice_date(row.get("announcementTime")),
                "title": title,
                "url": (
                    f"{DETAIL_URL}?stockCode={code6}"
                    f"&announcementId={notice_id}&orgId={org_id}"
                ),
            }
        )
        if len(picked) >= MAX_NOTICES:
            break
    return picked


def _keep_title(title: str) -> bool:
    return any(token in title for token in KEEP_TOKENS) or any(
        token in title for token in REPORT_TOKENS
    )


def _plain_title(raw: object) -> str:
    text = str(raw or "")
    return text.replace("<em>", "").replace("</em>", "").strip()


def _notice_date(raw: object) -> str:
    try:
        stamp = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if stamp > 10**12:
        stamp //= 1000
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp, tz=timezone(timedelta(hours=8))).date().isoformat()


def fetch_event_evidence(
    code: str, start: date, end: date, *, before_request: Callable[[], None],
) -> tuple[tuple[EvidenceRecord, ...], CoverageWindow]:
    """Locate all interval notices, without certifying structured action details.

    Unlike fetch_notices this has no latest-eight cap and preserves page/total
    metadata. Every lookup/page passes the caller's RequestPolicy hook; its
    cancellation/budget exceptions propagate. Transport failures retain partial
    sources with incomplete coverage. A fully retrieved listing still remains
    incomplete until independently reviewed normalized details/coverage exist.
    No document-title extraction is promoted to a verified capital event.
    """
    window = CoverageWindow(code, 'capital_event', start, end, False, ())
    code6 = _code6(code)
    if not code6:
        return (), window
    import httpx2

    rows: dict[str, EvidenceRecord] = {}
    with httpx2.Client(timeout=20.0) as client:
        before_request()
        try:
            org = _lookup_org(client, code6)
        except Exception:
            return (), window
        if org is None:
            return (), window
        page = 1
        expected: tuple[int, int] | None = None
        while True:
            before_request()
            try:
                response = client.post(QUERY_URL, data={
                    'pageNum': str(page), 'pageSize': '30', 'column': _column(code),
                    'tabName': 'fulltext', 'plate': '', 'stock': f"{code6},{org['orgId']}",
                    'searchkey': '', 'secid': '', 'category': '', 'trade': '',
                    'seDate': f'{start.isoformat()}~{end.isoformat()}',
                    'sortName': '', 'sortType': '', 'isHLtitle': 'true',
                }, headers=_HTTP_HEADERS)
                if response.status_code >= 400:
                    break
                body = response.json()
                document_hash = sha256(response.content).hexdigest()
            except Exception:
                break
            if not isinstance(body, dict) or not isinstance(body.get('announcements'), list):
                break
            notices = body['announcements']
            observed = datetime.now(timezone.utc)
            for notice in notices:
                if not isinstance(notice, dict):
                    continue
                notice_id = str(notice.get('announcementId') or '').strip()
                title = _plain_title(notice.get('announcementTitle'))
                published_date = _notice_date(notice.get('announcementTime'))
                if not notice_id or notice_id in rows or not published_date or not title:
                    continue
                # CNINFO dates do not certify publication-time precision.
                published = datetime.fromisoformat(published_date).replace(tzinfo=timezone(timedelta(hours=8)))
                record = EvidenceRecord(
                    evidence_id='pending', code=code, kind='capital_notice',
                    period=published_date[:4], published_at=published, observed_at=observed,
                    source_updated_at=None, source_url=QUERY_URL, document_sha256=document_hash,
                    values={'_announcement_id': notice_id, '_title': title, '_listing_page': str(page),
                            '_total_announcements': str(body.get('totalAnnouncement')),
                            '_total_pages': str(body.get('totalpages')),
                            '_notice_url': f"{DETAIL_URL}?stockCode={code6}&announcementId={notice_id}&orgId={org['orgId']}"},
                    status='untrusted', basis_id='cninfo-announcement-listing', date_only=True,
                )
                rows[notice_id] = replace(record, evidence_id=evidence_identity(record))
            total, pages = body.get('totalAnnouncement'), body.get('totalpages')
            if (type(total) is not int or type(pages) is not int or total < 0 or pages < 0
                    or (total > 0 and pages != (total + 29) // 30)):
                break
            if expected is None:
                expected = (total, pages)
            if expected != (total, pages) or not notices or page >= pages:
                break
            page += 1
    result = tuple(rows.values())
    return result, replace(window, evidence_ids=tuple(row.evidence_id for row in result))
