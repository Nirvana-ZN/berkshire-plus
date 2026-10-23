from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Financial, ResearchArtifact, ResearchJob, Thesis
from ledger.exposure import exposure_report, proposal_priority, read_exposure_context

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
_UNIT_AFTER = re.compile(r"(?i)^\s*(?:[xX倍%％年股]|PE|PB)")
_BAND_KEYS = ("aggressive", "steady", "conservative")
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


_PERIOD_SUFFIX_RANK = {"Q1": 1, "H1": 2, "Q3": 3}
DRIFT_PROFIT_DROP = Decimal("-0.15")


def _period_key(period: str) -> tuple[int, int] | None:
    text = period.strip().upper()
    if len(text) == 4 and text.isdigit():
        return int(text), 4
    if len(text) == 6 and text[:4].isdigit() and text[4:] in _PERIOD_SUFFIX_RANK:
        return int(text[:4]), _PERIOD_SUFFIX_RANK[text[4:]]
    return None


def _baseline_period_key(structured: Mapping[str, Any]) -> tuple[int, int] | None:
    val = structured.get("valuation")
    candidates: list[tuple[int, int]] = []
    if isinstance(val, Mapping):
        basis = str(val.get("pe_ttm_basis") or "")
        for token in re.findall(r"\d{4}(?:Q1|H1|Q3)?", basis.upper()):
            key = _period_key(token)
            if key is not None:
                candidates.append(key)
        eps_key = _period_key(str(val.get("eps_period") or ""))
        if eps_key is not None:
            candidates.append(eps_key)
        if not candidates:
            period_key = _period_key(str(val.get("period") or ""))
            if period_key is not None:
                candidates.append(period_key)
    if candidates:
        return max(candidates)
    as_of = structured.get("as_of") if isinstance(structured.get("as_of"), Mapping) else {}
    as_of_date = _as_date(as_of.get("date"))
    if as_of_date is not None:
        return (as_of_date.year - 1, 4)
    return None


def financial_drift(
    structured: object,
    financials: Sequence[Mapping[str, Any]] = (),
    *,
    since_baseline: bool = True,
) -> dict[str, Any] | None:
    """Detect financial report deterioration (profit YoY <= -15% or OCF flip negative)."""

    if not financials:
        return None
    baseline = (
        _baseline_period_key(structured)
        if since_baseline and isinstance(structured, Mapping)
        else (0, 0)
    )
    if baseline is None:
        return None
    by_period: dict[str, Mapping[str, Any]] = {}
    for row in financials:
        if not isinstance(row, Mapping):
            continue
        p = str(row.get("period") or "").strip().upper()
        if _period_key(p) is not None:
            by_period[p] = row
    newer = sorted(
        (p for p in by_period if (_period_key(p) or (0, 0)) > baseline),
        key=lambda p: _period_key(p) or (0, 0),
    )
    for period in reversed(newer):
        prev_period = f"{int(period[:4]) - 1}{period[4:]}"
        if prev_period not in by_period:
            continue
        cur = by_period[period]
        prev = by_period[prev_period]
        cur_ni = _as_decimal(cur.get("net_income"))
        prev_ni = _as_decimal(prev.get("net_income"))
        ni_yoy = (
            (cur_ni - prev_ni) / abs(prev_ni)
            if cur_ni is not None and prev_ni is not None and prev_ni != 0
            else None
        )
        cur_dni = _as_decimal(cur.get("deducted_net_income"))
        prev_dni = _as_decimal(prev.get("deducted_net_income"))
        dni_yoy = (
            (cur_dni - prev_dni) / abs(prev_dni)
            if cur_dni is not None and prev_dni is not None and prev_dni != 0
            else None
        )
        cur_ocf = _as_decimal(cur.get("ocf"))
        prev_ocf = _as_decimal(prev.get("ocf"))
        reasons: list[str] = []
        if ni_yoy is not None and ni_yoy <= DRIFT_PROFIT_DROP:
            reasons.append("profit_drop")
        if dni_yoy is not None and dni_yoy <= DRIFT_PROFIT_DROP:
            if "profit_drop" not in reasons:
                reasons.append("profit_drop")
            reasons.append("deducted_profit_drop")
        if cur_ocf is not None and cur_ocf < 0 and prev_ocf is not None and prev_ocf > 0:
            reasons.append("ocf_turned_negative")
        worst_yoy = (
            dni_yoy
            if (dni_yoy is not None and (ni_yoy is None or dni_yoy < ni_yoy))
            else ni_yoy
        )
        if not reasons and len(period) == 6 and worst_yoy is not None and worst_yoy <= Decimal("-0.08"):
            ann_y1 = prev_period[:4]
            ann_y0 = str(int(ann_y1) - 1)
            if ann_y1 in by_period and ann_y0 in by_period:
                a1 = by_period[ann_y1]
                a0 = by_period[ann_y0]
                a1_p = _as_decimal(a1.get("deducted_net_income")) or _as_decimal(a1.get("net_income"))
                a0_p = _as_decimal(a0.get("deducted_net_income")) or _as_decimal(a0.get("net_income"))
                if a1_p is not None and a0_p is not None and a0_p > 0:
                    ann_yoy = (a1_p - a0_p) / a0_p
                    if ann_yoy <= Decimal("-0.08"):
                        reasons.append("consecutive_decline")
        if reasons:
            eff_yoy = (
                dni_yoy
                if (dni_yoy is not None and (ni_yoy is None or dni_yoy < ni_yoy))
                else ni_yoy
            )
            out: dict[str, Any] = {
                "period": period,
                "base_period": prev_period,
                "reasons": reasons,
                "net_income_yoy": _json_bound(eff_yoy.quantize(Decimal("0.0001"))) if eff_yoy is not None else None,
            }
            if dni_yoy is not None:
                out["deducted_net_income_yoy"] = _json_bound(dni_yoy.quantize(Decimal("0.0001")))
            return out
        return None
    return None


def status(
    structured: object,
    *,
    today: date | None = None,
    current_price: object = None,
    price_usable: bool = False,
    financials: Sequence[Mapping[str, Any]] = (),
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
    if financial_drift(structured, financials) is not None:
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
    rows = _band_prices(bands, ref_price)
    cheap = min(row[0] for row in rows) if rows else None
    return cheap, expensive_line(bands, ref_price)


def _band_prices(bands: dict, ref_price: Decimal | None) -> list[list[Decimal]]:
    computed = _computed_bands(bands)
    if computed is not None:
        return [[value] for value in computed]
    rows = []
    for key in _BAND_KEYS:
        numbers = _price_numbers(str(bands.get(key) or ""))
        if ref_price is not None and ref_price > 0:
            low, high = ref_price * Decimal("0.2"), ref_price * Decimal("5")
            numbers = [number for number in numbers if low <= number <= high]
        if numbers:
            rows.append(numbers)
    return rows


def _computed_bands(bands):
    """Exact deterministic v1 prices bypass legacy prose/quote heuristics."""
    if not isinstance(bands, Mapping) or bands.get('computed_version') != 'valuation-scenarios-v1':
        return None
    values = tuple(_as_decimal(bands.get(key)) for key in _BAND_KEYS)
    if all(value is not None and value > 0 for value in values) and values[0] < values[1] < values[2]:
        return values
    return None


def _price_check(
    structured: object,
    *,
    current_price: object = None,
    price_usable: bool = False,
) -> bool | None:
    """现价是否高于价格带上沿；没有现价或带子给不出可信上沿时为 None。"""
    if not isinstance(structured, dict) or not price_usable:
        return None
    price = _as_decimal(current_price)
    if price is None or price <= 0:
        return None
    bands = structured.get("price_bands") if isinstance(structured.get("price_bands"), dict) else {}
    if str(bands.get("mode") or "").strip() == "watch":
        return False
    as_of = structured.get("as_of") if isinstance(structured.get("as_of"), dict) else {}
    as_of_price = _as_decimal(as_of.get("price"))
    ceiling = expensive_line(bands, as_of_price or price)
    if ceiling is None:
        return None
    # 立论现价已高于解析上沿：带子是「更便宜才加仓」或最后一个数字是 PE/PB，上沿不可信
    if _computed_bands(bands) is None and as_of_price is not None and as_of_price > ceiling:
        return None
    return price > ceiling


def is_overvalued(
    structured: object,
    *,
    current_price: object = None,
    price_usable: bool = False,
) -> bool:
    return _price_check(
        structured, current_price=current_price, price_usable=price_usable
    ) is True


def expensive_line(bands: object, ref_price: Decimal | None) -> Decimal | None:
    if not isinstance(bands, dict):
        return None
    computed = _computed_bands(bands)
    if computed is not None:
        return computed[2]
    over = conservative_ceiling(str(bands.get("overvalued") or ""), ref_price)
    if over is not None:
        return over
    # 模型对三档高低的理解不一致（有的激进最高、有的保守最高），上沿取带内最高价
    rows = _band_prices(bands, ref_price)
    if not rows:
        return None
    ceiling = max(max(row) for row in rows)
    if ref_price is not None and ref_price > 0 and ceiling < ref_price * Decimal("0.5"):
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
    over = _price_check(
        structured, current_price=current_price, price_usable=price_usable
    )
    if over is None:
        return None
    return "fail" if verdict == "pass" and over else verdict


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
    codes = {job.code for job in jobs if job.code}
    fins_by_code: dict[str, list[dict[str, object]]] = {}
    if codes:
        for f_row in session.scalars(select(Financial).where(Financial.code.in_(codes))).all():
            fins_by_code.setdefault(f_row.code, []).append(
                {
                    "period": f_row.period,
                    "period_type": f_row.period_type,
                    "net_income": f_row.net_income,
                    "deducted_net_income": f_row.deducted_net_income,
                    "ocf": f_row.ocf,
                }
            )
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
        drift = financial_drift(
            parsed,
            fins_by_code.get(job.code or "", ()),
            since_baseline=False,
        )
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
                "financial_drift": drift,
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


def dynamic_sizing(
    structured: object,
    *,
    current_price: object = None,
    price_usable: bool = False,
    actual_weight: object = None,
    financials: Sequence[Mapping[str, Any]] = (),
) -> dict[str, object] | None:
    """Evaluate dynamic position sizing from role tier, valuation bands, and earnings drift."""

    if not isinstance(structured, dict) or not structured:
        return None
    verdict = str(structured.get("verdict") or "").strip().lower()
    memo = structured.get("memo") if isinstance(structured.get("memo"), dict) else {}
    role = str(memo.get("role") or "").strip().lower()
    if verdict == "fail" or role == "skip":
        role_zh, std_w, max_w = "不买/清仓", Decimal("0"), Decimal("0")
    elif role == "core" and verdict == "pass":
        role_zh, std_w, max_w = "核心仓", Decimal("0.10"), Decimal("0.15")
    else:
        role_zh, std_w, max_w = "卫星仓", Decimal("0.05"), Decimal("0.08")

    weight = _as_decimal(actual_weight)
    as_of = structured.get("as_of") if isinstance(structured.get("as_of"), dict) else {}
    ref_price = _as_decimal(as_of.get("price")) or _as_decimal(current_price)
    cheap, rich = band_bounds(structured, ref_price=ref_price)
    steady = expected_price(structured)
    price = _as_decimal(current_price) if price_usable else None
    over_cap = bool(weight is not None and max_w > 0 and weight > max_w)
    drift = financial_drift(structured, financials, since_baseline=False)
    operating = structured.get('operating_result')
    state = operating.get('state', 'unknown') if isinstance(operating, Mapping) else 'unknown'
    priority = proposal_priority('skip' if role == 'skip' else verdict,
                                'deteriorating' if drift else state, structured.get('data_grade', 'C'))
    if drift is not None and priority != 'exit_review':
        cap = min(weight, max_w) if weight is not None else Decimal("0")
        return {
            "role": role or "satellite",
            "role_zh": role_zh,
            "zone": "drift_freeze",
            "action": "freeze",
            "suggested_min": 0,
            "suggested_max": _json_bound(cap.quantize(Decimal("0.0001"))),
            "role_cap": _json_bound(max_w),
            "steady_bound": _json_bound(steady),
            "over_cap": over_cap,
            "drift": drift,
            "label": f"{role_zh}·冻结加仓（{drift['period']}同比恶化）",
        }

    if priority == 'exit_review':
        zone, action, s_min, s_max = "exit", "trim_or_exit", Decimal("0"), Decimal("0")
        label = f"{role_zh}·建议退出"
    elif price is not None and price > 0 and is_overvalued(
        structured, current_price=price, price_usable=True
    ):
        zone, action, s_min, s_max = "overvalued", "stop_or_trim", Decimal("0"), std_w
        label = f"{role_zh}·超上限禁加（>{_json_bound(rich)}）"
    elif verdict == "gray":
        zone, action = "gray_hold", "hold_no_add"
        s_min, s_max = Decimal("0"), std_w
        label = f"{role_zh}·灰区观察（禁加仓）"
    elif priority == 'freeze_add':
        zone, action, s_min = 'evidence_freeze', 'hold_no_add', Decimal('0')
        s_max = min(weight, max_w) if weight is not None else Decimal('0')
        label = f'{role_zh}·信息等级或经营状态不支持新增'
    elif price is not None and cheap is not None and price <= cheap:
        zone, action, s_min, s_max = "aggressive", "add_to_cap", std_w, max_w
        label = f"{role_zh}·击球加仓区（{int(std_w * 100)}%-{int(max_w * 100)}%）"
    elif price is not None and steady is not None and price <= steady:
        zone, action = "steady", "normal_config"
        s_min, s_max = (std_w * Decimal("0.6")).quantize(Decimal("0.0001")), std_w
        label = f"{role_zh}·合理配置区（上限{int(std_w * 100)}%）"
    elif price is not None and rich is not None and price <= rich:
        zone, action, s_min, s_max = "hold_corridor", "hold_no_add", Decimal("0"), max_w
        label = f"{role_zh}·持有观察区（不追高补仓）"
    else:
        zone, action, s_min, s_max = "watch", "hold_no_add", Decimal("0"), std_w
        label = f"{role_zh}·观望持有（上限{int(std_w * 100)}%）"

    return {
        "role": role or "satellite",
        "role_zh": role_zh,
        "zone": zone,
        "action": action,
        "suggested_min": _json_bound(s_min),
        "suggested_max": _json_bound(s_max),
        "role_cap": _json_bound(max_w),
        "steady_bound": _json_bound(steady),
        "over_cap": over_cap,
        "label": label,
    }


def _fmt_pct(value: object) -> str:
    d = _as_decimal(value)
    if d is None:
        return "—"
    pct = d * Decimal("100")
    return f"{int(pct)}%" if pct == pct.to_integral_value() else f"{float(pct):.2f}%"


def _fmt_price(value: object) -> str:
    d = _as_decimal(value)
    if d is None:
        return "—"
    return f"{int(d)}" if d == d.to_integral_value() else f"{float(d):.2f}".rstrip("0").rstrip(".")


def portfolio_optimization(
    positions: Sequence[Mapping[str, Any]],
    unheld_candidates: Sequence[Mapping[str, Any]] = (),
    *,
    sentiment: Mapping[str, Any] | None = None,
    exposure_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build actionable reduce, add, and build recommendations across holdings, pass theses, and market sentiment."""

    sent_zone = str((sentiment or {}).get("zone") or "").strip().lower()
    sent_score = (sentiment or {}).get("score")
    sent_label = str((sentiment or {}).get("label") or "").strip()
    is_greed = sent_zone == "greed"
    is_fear = sent_zone == "fear"
    sentiment_regime: dict[str, object] | None = None
    if isinstance(sent_score, (int, float)) and sent_label:
        if is_greed:
            s_note = "极端贪婪期：暂停合理区追买，仅保留激进价以下击球机会，优先兑现超配与高估持仓。"
        elif is_fear:
            s_note = "极端恐惧期：优质标的进入高赔率击球窗口，可动用机动现金向角色上限积极配置。"
        else:
            s_note = "情绪中性期：按个股三档价格带与角色上限常态化执行。"
        sentiment_regime = {
            "score": int(sent_score),
            "zone": sent_zone or "neutral",
            "label": sent_label,
            "note": s_note,
        }

    reduce_list: list[dict[str, object]] = []
    add_list: list[dict[str, object]] = []

    for pos in positions:
        ds = pos.get("dynamic_sizing") if isinstance(pos.get("dynamic_sizing"), Mapping) else {}
        if not ds:
            continue
        name = str(pos.get("name") or "")
        code = pos.get("code")
        res_id = pos.get("thesis_research_id")
        zone = str(ds.get("zone") or "")
        role = str(ds.get("role") or "satellite")
        role_zh = str(ds.get("role_zh") or "卫星仓")
        std_cap = Decimal("0.10") if role == "core" else Decimal("0.05")
        role_cap = _as_decimal(ds.get("role_cap")) or Decimal("0")
        s_max = _as_decimal(ds.get("suggested_max")) or Decimal("0")
        w = _as_decimal(pos.get("weight"))
        price = _as_decimal(pos.get("price"))
        cheap = _as_decimal(pos.get("cheap_bound"))
        steady = _as_decimal(ds.get("steady_bound"))
        rich = _as_decimal(pos.get("rich_bound"))
        base = {
            "name": name,
            "code": code,
            "research_id": res_id,
            "role_zh": role_zh,
            "current_weight": _json_bound(w),
            "price": _json_bound(price),
            "cheap_bound": _json_bound(cheap),
            "steady_bound": _json_bound(steady),
            "rich_bound": _json_bound(rich),
            '_proposed_weight': role_cap if is_fear else s_max,
        }
        if zone == "exit":
            reduce_list.append({
                **base,
                "tag": "逻辑失效·建议退出",
                "severity": "fail",
                "target_range": "0%",
                "reason": f"论文未通过或建议不买，当前占比 {_fmt_pct(w)}，建议择机退出。",
            })
        elif zone == "overvalued" or pos.get("overvalued"):
            reduce_list.append({
                **base,
                "tag": "高估超限·逢高减仓",
                "severity": "fail",
                "target_range": f"≤{_fmt_pct(std_cap)}",
                "reason": f"现价 {_fmt_price(price)} 已突破保守上限 {_fmt_price(rich)}，停止加仓并建议逢高减至 ≤{_fmt_pct(std_cap)}。",
            })
        elif zone == "drift_freeze":
            drift = (
                ds.get("drift")
                if isinstance(ds.get("drift"), Mapping)
                else (pos.get("financial_drift") if isinstance(pos.get("financial_drift"), Mapping) else {})
            )
            yoy = drift.get("net_income_yoy")
            yoy_str = f"净利同比 {float(yoy) * 100:.1f}%" if yoy is not None else "现金流转负"
            reduce_list.append({
                **base,
                "tag": "财报恶化·禁补逢高减",
                "severity": "fail",
                "target_range": f"≤{_fmt_pct(std_cap)}",
                "reason": f"{drift.get('period', '最新期')}{yoy_str}触发财报冻结；当前占比 {_fmt_pct(w)} 接近上限，严禁补仓，反弹近 {_fmt_price(rich)} 优先减至 ≤{_fmt_pct(std_cap)}。",
            })
        elif w is not None and role_cap > 0 and w > role_cap:
            excess = w - role_cap
            rule_hit = "，且已触及加仓上限价规则" if pos.get("rule_light") == "hit" else ""
            reduce_list.append({
                **base,
                "tag": "超角色上限·逢高瘦身",
                "severity": "warn",
                "target_range": f"{_fmt_pct(std_cap)}~{_fmt_pct(role_cap)}",
                "reason": f"当前占比 {_fmt_pct(w)} 超过{role_zh}单票上限 {_fmt_pct(role_cap)}（超配 +{_fmt_pct(excess)}）{rule_hit}，建议逢高瘦身至 ≤{_fmt_pct(role_cap)}。",
            })
        elif zone == "gray_hold":
            reduce_list.append({
                **base,
                "tag": "灰区存疑·择机换仓",
                "severity": "warn",
                "target_range": f"0%~{_fmt_pct(std_cap)}",
                "reason": f"论文结论为灰色地带且现价 {_fmt_price(price)} 高于稳健价 {_fmt_price(steady)}，禁止加仓，可作为新核心标的建仓的换仓资金来源。",
            })
        elif zone in ("aggressive", "steady") and (w is None or w < s_max):
            if is_greed and zone == "steady":
                add_list.append({
                    **base,
                    "ready": False,
                    "tag": f"贪婪期暂缓·等≤{_fmt_price(cheap)}",
                    "severity": "warn",
                    "target_range": f"≤{_fmt_pct(std_cap)}",
                    "reason": f"现价 {_fmt_price(price)} 虽在稳健价 {_fmt_price(steady)} 以下，但市场处于极端贪婪（{sent_score}分），暂停合理区补仓，仅在回落至激进价 ≤{_fmt_price(cheap)} 时加仓。",
                })
            else:
                target_range = (
                    f"{_fmt_pct(std_cap)}~{_fmt_pct(role_cap)}"
                    if (zone == "aggressive" or is_fear)
                    else f"≤{_fmt_pct(std_cap)}"
                )
                fear_tag = "（恐惧击球）" if is_fear else ""
                add_list.append({
                    **base,
                    "ready": True,
                    "tag": ("可立即加仓·击球区" if zone == "aggressive" else "可立即加仓·合理区") + fear_tag,
                    "severity": "pass",
                    "target_range": target_range,
                    "reason": f"现价 {_fmt_price(price)} 处于买点区间（稳健价 {_fmt_price(steady)} / 激进价 {_fmt_price(cheap)}），可从 {_fmt_pct(w)} 补至 {target_range}。",
                })
        elif zone == "hold_corridor" and (w is None or w < role_cap):
            gap = ((steady - price) / price * Decimal("100")) if (steady is not None and price is not None and price > 0) else None
            gap_str = f"（距今 {float(gap):.1f}%）" if gap is not None else ""
            add_list.append({
                **base,
                "ready": False,
                "tag": f"等回调加仓（≤{_fmt_price(steady)}）",
                "severity": "neutral",
                "target_range": f"{_fmt_pct(std_cap)}~{_fmt_pct(role_cap)}",
                "reason": f"当前占比 {_fmt_pct(w)} 低于{role_zh}上限（标准 {_fmt_pct(std_cap)} / 极限 {_fmt_pct(role_cap)}）；现价 {_fmt_price(price)} 处观察走廊，回落至稳健价 ≤{_fmt_price(steady)}{gap_str}可加至 {_fmt_pct(std_cap)}，≤{_fmt_price(cheap)} 可加至 {_fmt_pct(role_cap)}。",
            })

    build_list: list[dict[str, object]] = []
    for cand in unheld_candidates:
        structured = cand.get("structured") if isinstance(cand.get("structured"), dict) else {}
        if not structured or str(structured.get("verdict") or "").strip().lower() != "pass":
            continue
        fins = cand.get("financials") or ()
        if financial_drift(structured, fins, since_baseline=False) is not None:
            continue
        as_of = structured.get("as_of") if isinstance(structured.get("as_of"), dict) else {}
        q_price = _as_decimal(cand.get("price"))
        price = q_price if (q_price is not None and q_price > 0) else _as_decimal(as_of.get("price"))
        price_src = "现价" if (q_price is not None and q_price > 0) else "立论价"
        ds = dynamic_sizing(
            structured,
            current_price=price,
            price_usable=price is not None and price > 0,
            financials=fins,
        )
        if not ds:
            continue
        zone = str(ds.get("zone") or "")
        role = str(ds.get("role") or "satellite")
        role_zh = str(ds.get("role_zh") or "卫星仓")
        std_cap = Decimal("0.10") if role == "core" else Decimal("0.05")
        role_cap = _as_decimal(ds.get("role_cap")) or Decimal("0")
        cheap, rich = band_bounds(structured, ref_price=_as_decimal(as_of.get("price")) or price)
        steady = expected_price(structured)
        gap = ((steady - price) / price * Decimal("100")) if (steady is not None and price is not None and price > 0) else Decimal("-999")
        base_cand = {
            "name": cand.get("name"),
            "code": cand.get("code"),
            "research_id": cand.get("research_id"),
            "role_zh": role_zh,
            "price": _json_bound(price),
            "price_source": price_src,
            "cheap_bound": _json_bound(cheap),
            "steady_bound": _json_bound(steady),
            "rich_bound": _json_bound(rich),
            '_proposed_weight': role_cap if is_fear else _as_decimal(ds.get('suggested_max')),
            "_sort_key": (0 if zone in ("aggressive", "steady") else 1, 0 if role == "core" else 1, -gap),
        }
        if zone in ("aggressive", "steady"):
            if is_greed and zone == "steady":
                t_min = _fmt_pct(ds.get("suggested_min"))
                t_max = _fmt_pct(ds.get("suggested_max"))
                build_list.append({
                    **base_cand,
                    "ready": False,
                    "tag": f"贪婪期暂缓·等≤{_fmt_price(cheap)}",
                    "severity": "warn",
                    "target_range": f"{t_min}~{t_max}",
                    "reason": f"{role_zh}候选（评估通过），{price_src} {_fmt_price(price)} 低于稳健买点 {_fmt_price(steady)}，但当前市场极端贪婪（{sent_score}分），暂停合理区建仓，等激进价 ≤{_fmt_price(cheap)}。",
                })
            else:
                t_min = _fmt_pct(std_cap if is_fear else ds.get("suggested_min"))
                t_max = _fmt_pct(role_cap if is_fear else ds.get("suggested_max"))
                fear_tag = "（恐惧击球）" if is_fear else ""
                build_list.append({
                    **base_cand,
                    "ready": True,
                    "tag": ("可立即建仓·击球区" if zone == "aggressive" else "可立即建仓·合理区") + fear_tag,
                    "severity": "pass",
                    "target_range": f"{t_min}~{t_max}",
                    "reason": f"{role_zh}候选（评估通过），{price_src} {_fmt_price(price)} 低于稳健买点 {_fmt_price(steady)}（激进价 {_fmt_price(cheap)} / 保守上限 {_fmt_price(rich)}），建议建仓 {t_min}~{t_max}。",
                })
        elif zone == "hold_corridor":
            gap_str = f"（差 {float(gap):.1f}%）" if gap > Decimal("-900") else ""
            t_min = _fmt_pct((std_cap * Decimal("0.6")).quantize(Decimal("0.0001")))
            t_max = _fmt_pct(std_cap)
            build_list.append({
                **base_cand,
                "ready": False,
                "tag": f"等买点建仓（≤{_fmt_price(steady)}）",
                "severity": "neutral",
                "target_range": f"{t_min}~{t_max}",
                "reason": f"{role_zh}候选（评估通过），{price_src} {_fmt_price(price)} 处观察走廊，距稳健买点 ≤{_fmt_price(steady)}{gap_str}，回落至买点可建仓 {t_min}~{t_max}。",
            })

    build_list.sort(key=lambda item: item.pop("_sort_key", (9, 9, 999)))
    common_exposure = None
    if exposure_context is not None:
        weights, _labels, _policy = read_exposure_context(exposure_context)
        deltas = {}
        for item in add_list + build_list:
            code = item.get('code') or f"name:{item['name']}"
            proposed = item['_proposed_weight']
            if item.get('ready') and proposed is not None:
                delta = proposed - weights.get(code, Decimal('0'))
                if delta > 0:
                    deltas[code] = max(deltas.get(code, Decimal('0')), delta)
        common_exposure = exposure_report(exposure_context, deltas)
        if not common_exposure['allowed']:
            for item in add_list + build_list:
                if item.get('ready'):
                    item.update(ready=False, restriction='shared_exposure_limit', severity='warn', tag='共同风险约束·暂停新增')
                    item['reason'] = '组合同时新增后的共同风险超限或部分权重缺失。' + str(item['reason'])
    for item in reduce_list + add_list + build_list:
        proposed = item.pop('_proposed_weight', None)
        if item in add_list + build_list:
            item['proposed_weight'] = str(proposed) if proposed is not None else None
    return {
        "reduce": reduce_list,
        "add": add_list,
        "build": build_list,
        "sentiment_regime": sentiment_regime,
        'common_exposure': common_exposure,
    }


def api_fields(
    thesis: Thesis | None,
    *,
    today: date | None = None,
    current_price: object = None,
    price_usable: bool = False,
    actual_weight: object = None,
    financials: Sequence[Mapping[str, Any]] = (),
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
            "financial_drift": None,
            "dynamic_sizing": None,
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
            financials=financials,
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
        "financial_drift": financial_drift(structured, financials, since_baseline=False),
        "dynamic_sizing": dynamic_sizing(
            structured,
            current_price=current_price,
            price_usable=price_usable,
            actual_weight=actual_weight,
            financials=financials,
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
