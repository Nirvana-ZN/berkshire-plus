from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Thesis

STALE_DAYS = 90
WATCH_DEVIATION = Decimal("0.15")
PLACEHOLDER = "无法完成"
MIRROR_MIN = 5
ELIGIBLE_VERDICTS = frozenset({"pass", "gray"})
STATUS_CURRENT = "current"
STATUS_STALE = "stale"
STATUS_NONE = "none"
EVENT_ESTABLISHED = "established"
EVENT_UPDATED = "updated"
MARK_ESTABLISHED = "已立为论文"
MARK_UPDATED = "已更新论文"
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def eligible(structured: object) -> bool:
    if not isinstance(structured, dict):
        return False
    if structured.get("verdict") not in ELIGIBLE_VERDICTS:
        return False
    lines = _mirror_lines(structured)
    if len(lines) < MIRROR_MIN:
        return False
    if any(not line for line in lines):
        return False
    if any(PLACEHOLDER in line for line in lines):
        return False
    return True


def upsert(
    session: Session,
    *,
    user_id: int,
    name: str,
    research_job_id: int,
    structured: dict[str, Any],
) -> Thesis:
    name = name.strip()
    payload = json.dumps(structured, ensure_ascii=False)
    verdict = str(structured.get("verdict") or "")
    existing = session.scalars(
        select(Thesis).where(Thesis.user_id == user_id, Thesis.name == name)
    ).first()
    if existing is not None:
        existing.research_job_id = research_job_id
        existing.verdict = verdict
        existing.structured_json = payload
        existing.last_event = EVENT_UPDATED
        return existing
    row = Thesis(
        user_id=user_id,
        name=name,
        research_job_id=research_job_id,
        verdict=verdict,
        structured_json=payload,
        last_event=EVENT_ESTABLISHED,
    )
    session.add(row)
    return row


def clear_on_fail(session: Session, *, user_id: int, name: str) -> None:
    name = name.strip()
    existing = session.scalars(
        select(Thesis).where(Thesis.user_id == user_id, Thesis.name == name)
    ).first()
    if existing is not None:
        session.delete(existing)


def status(
    structured: object,
    *,
    today: date | None = None,
    current_price: object = None,
    price_usable: bool = False,
) -> str:
    if not isinstance(structured, dict) or not structured:
        return STATUS_STALE
    now = today or utc_today()
    as_of = structured.get("as_of") if isinstance(structured.get("as_of"), dict) else {}
    research_day = _as_date(as_of.get("date"))
    if research_day is None:
        return STATUS_STALE
    if (now - research_day).days > STALE_DAYS:
        return STATUS_STALE
    bands = structured.get("price_bands") if isinstance(structured.get("price_bands"), dict) else {}
    mode = str(bands.get("mode") or "").strip()
    price = _as_decimal(current_price) if price_usable else None
    if price is None or not price_usable:
        return STATUS_CURRENT
    if mode == "watch":
        as_of_price = _as_decimal(as_of.get("price"))
        if as_of_price is not None and as_of_price > 0:
            if abs(price - as_of_price) / as_of_price > WATCH_DEVIATION:
                return STATUS_STALE
        return STATUS_CURRENT
    floor = first_finite_number(str(bands.get("conservative") or ""))
    if floor is not None and price < floor:
        return STATUS_STALE
    return STATUS_CURRENT


def first_finite_number(text: str) -> Decimal | None:
    for match in _NUMBER_RE.finditer(text or ""):
        number = _as_decimal(match.group())
        if number is not None:
            return number
    return None


def load_by_names(session: Session, user_id: int, names: list[str]) -> dict[str, Thesis]:
    wanted = [name.strip() for name in names if str(name or "").strip()]
    if not wanted:
        return {}
    rows = session.scalars(
        select(Thesis).where(Thesis.user_id == user_id, Thesis.name.in_(wanted))
    ).all()
    return {row.name: row for row in rows}


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def parse_structured(row: Thesis) -> dict[str, Any]:
    try:
        parsed = json.loads(row.structured_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def api_fields(
    thesis: Thesis | None,
    *,
    today: date | None = None,
    current_price: object = None,
    price_usable: bool = False,
) -> dict[str, object]:
    if thesis is None:
        return {"thesis_status": STATUS_NONE, "thesis_research_id": None}
    return {
        "thesis_status": status(
            parse_structured(thesis),
            today=today,
            current_price=current_price,
            price_usable=price_usable,
        ),
        "thesis_research_id": thesis.research_job_id,
    }


def thesis_mark(thesis: Thesis | None, research_job_id: int) -> str | None:
    if thesis is None or thesis.research_job_id != research_job_id:
        return None
    if thesis.last_event == EVENT_UPDATED:
        return MARK_UPDATED
    return MARK_ESTABLISHED


def price_usable(price: object, *, stale: bool) -> bool:
    if stale:
        return False
    number = _as_decimal(price)
    return number is not None and number > 0


def _mirror_lines(structured: dict[str, Any]) -> list[str]:
    raw = structured.get("mirror_test")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw]


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _as_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None
