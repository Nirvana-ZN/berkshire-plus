from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    PortfolioReview,
    ResearchArtifact,
    ResearchJob,
    ScreenRun,
    Setting,
)
from jobs.lock import JOB_TYPE_REVIEW, release
from jobs.research import (
    create_research_job,
    _run_one as run_research_one,
    load_book_dict,
    load_financials,
    web_search_enabled,
)
from ledger.positions import LOCAL_USER_ID, load_positions
from ledger.quotes import (
    BookSnapshot,
    apply_refresh,
    instrument_code_by_name,
    is_a_share_code,
    project_book,
    quote_targets,
    rebalance_alerts,
)
from research.thesis import (
    dynamic_sizing,
    financial_drift,
    load_by_names,
    parse_structured,
    price_usable,
    status,
    utc_today,
)
from review.run import ReviewInput, run_review

logger = logging.getLogger(__name__)

LLM_MODEL_KEY = "llm_model"
CASH_NAME = "__CASH__"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_optional(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    number = Decimal(value)
    if not number.is_finite():
        return None
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def snapshot_to_dict(book: BookSnapshot) -> dict[str, object]:
    return {
        "positions": [
            {
                "name": row.name,
                "shares": _json_optional(row.shares),
                "cost": _json_optional(row.cost),
                "price": _json_optional(row.price),
                "weight": _json_optional(row.weight),
                "target": _json_optional(row.target),
                "no_target": row.no_target,
                "stale": row.stale,
                "rule_light": row.rule_light,
                "code": row.code,
                "deviation": _json_optional(row.deviation),
            }
            for row in book.positions
        ],
        "cash_balance": _json_optional(book.cash_balance),
        "cash": {
            "weight": _json_optional(book.cash.weight),
            "target": _json_optional(book.cash.target),
            "no_target": book.cash.no_target,
            "deviation": _json_optional(book.cash.deviation),
        },
        "cash_name": CASH_NAME,
        "rebalance": rebalance_alerts(book.positions, book.cash),
    }


def load_current_theses(
    session: Session,
    names: list[str],
    snapshot_positions: list[dict],
) -> dict[str, dict]:
    theses = load_by_names(session, LOCAL_USER_ID, names)
    by_snap = {
        str(item.get("name") or "").strip(): item
        for item in snapshot_positions
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    result: dict[str, dict] = {}
    for name, row in theses.items():
        structured = parse_structured(row)
        snap = by_snap.get(name, {})
        code = str(snap.get("code") or "").strip()
        fins = load_financials(session, code) if code else ()
        usable = price_usable(snap.get("price"), stale=bool(snap.get("stale")))
        stale = (
            status(
                structured,
                today=utc_today(),
                current_price=snap.get("price"),
                price_usable=usable,
                financials=fins,
            )
            == "stale"
        )
        result[name] = {
            "id": row.research_job_id,
            "verdict": row.verdict,
            "memo": structured.get("memo"),
            "structured": structured,
            "stale": stale,
            "financial_drift": financial_drift(structured, fins),
            "dynamic_sizing": dynamic_sizing(
                structured,
                current_price=snap.get("price"),
                price_usable=usable,
                actual_weight=snap.get("weight"),
                financials=fins,
            ),
        }
    return result


def load_funnel_candidates(session: Session) -> list[str]:
    run = session.scalars(
        select(ScreenRun)
        .where(ScreenRun.status == "done")
        .order_by(ScreenRun.id.desc())
    ).first()
    if run is None:
        return []
    jobs = session.scalars(
        select(ResearchJob)
        .where(ResearchJob.screen_run_id == run.id, ResearchJob.status == "done")
        .order_by(ResearchJob.id)
    ).all()
    names: list[str] = []
    seen: set[str] = set()
    for job in jobs:
        artifact = session.scalars(
            select(ResearchArtifact)
            .where(ResearchArtifact.research_job_id == job.id)
            .order_by(ResearchArtifact.id.desc())
        ).first()
        if artifact is None:
            continue
        try:
            structured = json.loads(artifact.structured_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(structured, dict):
            continue
        if structured.get("verdict") not in {"pass", "gray"}:
            continue
        if job.name in seen:
            continue
        seen.add(job.name)
        names.append(job.name)
    return names


def refresh_held_quotes(session: Session, quote_client) -> None:
    rows, _cash = load_positions(session)
    targets = quote_targets(session, [row.name for row in rows])
    codes = [
        code
        for _name, code in targets
        if code is not None and is_a_share_code(code)
    ]
    fetched: dict[str, Decimal] = {}
    if quote_client is not None and codes:
        try:
            raw = quote_client.fetch_prices(codes)
        except Exception:
            raw = {}
        if isinstance(raw, dict):
            fetched = raw
    apply_refresh(session, fetched=fetched, targets=targets, single=False)


def execute_review(
    session: Session,
    review_id: int,
    *,
    llm,
    quote_client,
    reports_dir: Path,
    market=None,
) -> None:
    row = session.get(PortfolioReview, review_id)
    if row is None:
        try:
            release(session, review_id, JOB_TYPE_REVIEW)
            session.commit()
        except Exception:
            session.rollback()
        return
    try:
        _run_one(
            session,
            row,
            llm=llm,
            quote_client=quote_client,
            reports_dir=reports_dir,
            market=market,
        )
    finally:
        try:
            release(session, review_id, JOB_TYPE_REVIEW)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("释放任务锁失败")


def _review_depth(row: PortfolioReview) -> str:
    try:
        payload = json.loads(row.structured_json or "")
    except json.JSONDecodeError:
        return "reuse"
    if isinstance(payload, dict) and payload.get("depth") == "eval_first":
        return "eval_first"
    return "reuse"


def _evaluate_missing_theses(
    session: Session,
    names: list[str],
    *,
    llm,
    quote_client,
    reports_dir: Path,
    market,
) -> None:
    have = load_by_names(session, LOCAL_USER_ID, names)
    mapping = instrument_code_by_name(session)
    book = load_book_dict(session)
    as_of = _now().date().isoformat()
    for name in names:
        if name in have:
            continue
        code = mapping.get(name)
        if not code or not is_a_share_code(code):
            continue
        job = create_research_job(session, name=name, code=code)
        session.commit()
        run_research_one(
            session,
            job,
            llm=llm,
            quote_client=quote_client,
            reports_dir=reports_dir,
            book=book,
            web_search=web_search_enabled(session),
            as_of_date=as_of,
            market=market,
        )


def _run_one(
    session: Session,
    row: PortfolioReview,
    *,
    llm,
    quote_client,
    reports_dir: Path,
    market=None,
) -> None:
    row.status = "running"
    row.updated_at = _now()
    session.commit()
    setting = session.get(Setting, LLM_MODEL_KEY)
    model = (setting.value or "").strip() if setting is not None else ""
    if model and hasattr(llm, "model"):
        llm.model = model
    try:
        refresh_held_quotes(session, quote_client)
        positions, cash = load_positions(session)
        snapshot = snapshot_to_dict(project_book(session, positions, cash))
        names = [
            str(item.get("name"))
            for item in snapshot.get("positions", [])
            if isinstance(item, dict) and item.get("name")
        ]
        if _review_depth(row) == "eval_first":
            _evaluate_missing_theses(
                session,
                names,
                llm=llm,
                quote_client=quote_client,
                reports_dir=reports_dir,
                market=market,
            )
        evaluations = load_current_theses(
            session,
            names,
            [
                item
                for item in snapshot.get("positions", [])
                if isinstance(item, dict)
            ],
        )
        funnel_names = tuple(load_funnel_candidates(session))
        output = run_review(
            llm,
            ReviewInput(
                snapshot=snapshot,
                evaluations=evaluations,
                funnel_names=funnel_names,
                as_of_date=_now().date().isoformat(),
                review_id=row.id,
                reports_dir=Path(reports_dir),
            ),
        )
        row.structured_json = json.dumps(output.structured, ensure_ascii=False)
        row.markdown = output.markdown
        row.report_path = output.report_path
        row.has_stale = output.has_stale
        row.status = "done"
        row.error = None
        row.updated_at = _now()
        session.commit()
    except Exception as exc:
        session.rollback()
        fresh = session.get(PortfolioReview, row.id)
        if fresh is not None:
            fresh.status = "failed"
            fresh.error = str(exc)
            fresh.updated_at = _now()
            session.commit()
        logger.exception("组合审视失败: %s", row.id)
