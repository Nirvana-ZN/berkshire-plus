from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ResearchArtifact, ResearchJob, Thesis

STALE_DAYS = 90
WATCH_DEVIATION = Decimal("0.15")
PLACEHOLDER = "无法完成"
MIRROR_MIN = 5
ELIGIBLE_VERDICTS = frozenset({"pass", "gray", "fail"})
STATUS_CURRENT = "current"
STATUS_STALE = "stale"
STATUS_NONE = "none"
PAPER_CURRENT = "current"
PAPER_UPDATED = "updated"
PAPER_GONE = "gone"
EVENT_ESTABLISHED = "established"
EVENT_UPDATED = "updated"
MARK_ESTABLISHED = "已立为论文"
MARK_UPDATED = "已更新论文"
_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_UNIT_AFTER = re.compile(r"(?i)^\s*(?:[xX倍]|PE|PB)")
_UNIT_BEFORE = re.compile(r"(?i)(?:PE|PB|市盈率|市净率|市盈|市净|倍)\s*$")


def eligible(structured: object, *, replacing: bool = False) -> bool:
    if not isinstance(structured, dict):
        return False
    if structured.get("verdict") not in ELIGIBLE_VERDICTS:
        return False
    lines = _mirror_lines(structured)
    if not lines:
        return False
    if any(PLACEHOLDER in line for line in lines):
        return False
    if replacing:
        return True
    return len(lines) >= MIRROR_MIN


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
    as_of_price = _as_decimal(as_of.get("price"))
    if mode == "watch":
        if as_of_price is not None and as_of_price > 0:
            if abs(price - as_of_price) / as_of_price > WATCH_DEVIATION:
                return STATUS_STALE
        return STATUS_CURRENT
    return STATUS_CURRENT


def band_bounds(
    structured: object, *, ref_price: Decimal | None = None
) -> tuple[Decimal | None, Decimal | None]:
    if not isinstance(structured, dict):
        return None, None
    bands = structured.get("price_bands") if isinstance(structured.get("price_bands"), dict) else {}
    if str(bands.get("mode") or "").strip() == "watch":
        return None, None
    cheap = first_finite_number(str(bands.get("aggressive") or ""))
    if cheap is None:
        cheap = first_finite_number(str(bands.get("conservative") or ""))
    rich = conservative_ceiling(str(bands.get("conservative") or ""), ref_price)
    return cheap, rich


def is_overvalued(
    structured: object,
    *,
    current_price: object = None,
    price_usable: bool = False,
) -> bool:
    if not isinstance(structured, dict) or not price_usable:
        return False
    price = _as_decimal(current_price)
    if price is None or price <= 0:
        return False
    bands = structured.get("price_bands") if isinstance(structured.get("price_bands"), dict) else {}
    if str(bands.get("mode") or "").strip() == "watch":
        return False
    as_of = structured.get("as_of") if isinstance(structured.get("as_of"), dict) else {}
    as_of_price = _as_decimal(as_of.get("price"))
    ceiling = expensive_line(bands, as_of_price or price)
    if ceiling is None:
        return False
    # 立论现价已高于解析上沿：带子是「更便宜才加仓」或最后一个数字是 PE/PB，不当买贵
    if as_of_price is not None and as_of_price > ceiling:
        return False
    return price > ceiling


def expensive_line(bands: object, ref_price: Decimal | None) -> Decimal | None:
    if not isinstance(bands, dict):
        return None
    over = conservative_ceiling(str(bands.get("overvalued") or ""), ref_price)
    if over is not None:
        return over
    ceiling = conservative_ceiling(str(bands.get("conservative") or ""), ref_price)
    cheap = first_finite_number(str(bands.get("aggressive") or ""))
    if ceiling is not None and cheap is not None and ceiling < cheap:
        return None
    if (
        ceiling is not None
        and cheap is None
        and ref_price is not None
        and ref_price > 0
        and ceiling < ref_price * Decimal("0.5")
    ):
        return None
    return ceiling


def live_verdict(
    structured: object,
    *,
    current_price: object = None,
    price_usable: bool = False,
) -> str | None:
    if not isinstance(structured, dict):
        return None
    verdict = str(structured.get("verdict") or "") or None
    if verdict == "pass" and is_overvalued(
        structured, current_price=current_price, price_usable=price_usable
    ):
        return "fail"
    return verdict


def expected_price(structured: object) -> Decimal | None:
    if not isinstance(structured, dict):
        return None
    bands = structured.get("price_bands") if isinstance(structured.get("price_bands"), dict) else {}
    if str(bands.get("mode") or "").strip() == "watch":
        return None
    return first_finite_number(str(bands.get("steady") or "")) or first_finite_number(
        str(bands.get("aggressive") or "")
    )


def gap_pct(price: Decimal | None, expected: Decimal | None) -> Decimal | None:
    if price is None or expected is None or price <= 0:
        return None
    return (expected - price) / price * Decimal("100")


def first_finite_number(text: str) -> Decimal | None:
    numbers = _finite_numbers(text)
    return numbers[0] if numbers else None


def last_finite_number(text: str) -> Decimal | None:
    numbers = _finite_numbers(text)
    return numbers[-1] if numbers else None


def conservative_ceiling(text: str, ref_price: Decimal | None) -> Decimal | None:
    numbers = _price_numbers(text)
    if not numbers:
        return None
    if ref_price is None or ref_price <= 0:
        return numbers[-1]
    low, high = ref_price * Decimal("0.2"), ref_price * Decimal("5")
    priced = [number for number in numbers if low <= number <= high]
    return priced[-1] if priced else None


def _finite_numbers(text: str) -> list[Decimal]:
    found: list[Decimal] = []
    for match in _NUMBER_RE.finditer(text or ""):
        number = _as_decimal(match.group())
        if number is not None:
            found.append(number)
    return found


def _price_numbers(text: str) -> list[Decimal]:
    found: list[Decimal] = []
    blob = text or ""
    for match in _NUMBER_RE.finditer(blob):
        after = blob[match.end() : match.end() + 6]
        before = blob[max(0, match.start() - 6) : match.start()]
        if _UNIT_AFTER.match(after) or _UNIT_BEFORE.search(before):
            continue
        number = _as_decimal(match.group())
        if number is not None:
            found.append(number)
    return found


def list_aftermath(
    session: Session,
    user_id: int,
    *,
    prices: dict[str, tuple[object, bool]] | None = None,
) -> list[dict[str, object]]:
    jobs = session.scalars(
        select(ResearchJob).where(ResearchJob.user_id == user_id, ResearchJob.status == "done")
    ).all()
    if not jobs:
        return []
    latest: dict[int, ResearchArtifact] = {}
    for artifact in session.scalars(
        select(ResearchArtifact).where(
            ResearchArtifact.research_job_id.in_([job.id for job in jobs])
        )
    ).all():
        prev = latest.get(artifact.research_job_id)
        if prev is None or artifact.id > prev.id:
            latest[artifact.research_job_id] = artifact
    theses = {
        row.name: row
        for row in session.scalars(select(Thesis).where(Thesis.user_id == user_id)).all()
    }
    quotes = prices or {}
    rows: list[dict[str, object]] = []
    for job in jobs:
        artifact = latest.get(job.id)
        if artifact is None:
            continue
        try:
            parsed = json.loads(artifact.structured_json)
        except json.JSONDecodeError:
            continue
        if not eligible(parsed):
            continue
        as_of = parsed.get("as_of") if isinstance(parsed.get("as_of"), dict) else {}
        then_price = _as_decimal(as_of.get("price"))
        cheap, rich = band_bounds(parsed, ref_price=then_price)
        raw_price, usable = quotes.get(job.name, (None, False))
        now_price = _as_decimal(raw_price) if usable else None
        usable = bool(usable) and now_price is not None
        thesis = theses.get(job.name)
        if thesis is None:
            paper = PAPER_GONE
        elif thesis.research_job_id == job.id:
            paper = PAPER_CURRENT
        else:
            paper = PAPER_UPDATED
        as_of_date = as_of.get("date")
        rows.append(
            {
                "research_id": job.id,
                "name": job.name,
                "as_of": str(as_of_date)[:10] if as_of_date else None,
                "then_verdict": str(parsed.get("verdict") or "") or None,
                "then_price": _json_bound(then_price),
                "cheap_bound": _json_bound(cheap),
                "rich_bound": _json_bound(rich),
                "current_price": _json_bound(now_price),
                "price_usable": usable,
                "paper": paper,
                "in_band": in_then_band(cheap, rich, now_price) if usable else None,
                "live_verdict": live_verdict(
                    parsed, current_price=now_price, price_usable=usable
                ),
            }
        )
    rows.sort(key=lambda row: (str(row.get("as_of") or ""), int(row["research_id"])), reverse=True)
    return rows


def in_then_band(
    cheap: Decimal | None, rich: Decimal | None, price: Decimal | None
) -> bool | None:
    if price is None:
        return None
    if cheap is None and rich is None:
        return None
    if cheap is not None and price < cheap:
        return False
    if rich is not None and price > rich:
        return False
    return True


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
        return {
            "thesis_status": STATUS_NONE,
            "thesis_research_id": None,
            "thesis_verdict": None,
            "overvalued": False,
            "cheap_bound": None,
            "rich_bound": None,
            "live_verdict": None,
        }
    structured = parse_structured(thesis)
    as_of = structured.get("as_of") if isinstance(structured.get("as_of"), dict) else {}
    cheap, rich = band_bounds(structured, ref_price=_as_decimal(as_of.get("price")) or _as_decimal(current_price))
    return {
        "thesis_status": status(
            structured,
            today=today,
            current_price=current_price,
            price_usable=price_usable,
        ),
        "thesis_research_id": thesis.research_job_id,
        "thesis_verdict": (thesis.verdict or "").strip().lower() or None,
        "overvalued": is_overvalued(
            structured, current_price=current_price, price_usable=price_usable
        ),
        "cheap_bound": _json_bound(cheap),
        "rich_bound": _json_bound(rich),
        "live_verdict": live_verdict(
            structured, current_price=current_price, price_usable=price_usable
        ),
    }


def _json_bound(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


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
    lines: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        parts = [part.strip() for part in re.split(r"[？?]+", text) if part.strip()]
        lines.extend(parts or [text])
    return lines


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
