from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

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
