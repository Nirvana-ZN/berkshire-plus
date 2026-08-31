from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Financial, ResearchArtifact, ResearchJob, Setting
from jobs.lock import release
from ledger.positions import LOCAL_USER_ID, load_positions
from ledger.quotes import BookSnapshot, project_book
from research.evaluate import EvalInput, has_min_financials, run_checklist
from research.thesis import clear_on_fail, eligible, upsert
from screen.funnel import ScreenRow

logger = logging.getLogger(__name__)

WEB_SEARCH_KEY = "web_search"
LLM_MODEL_KEY = "llm_model"


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


def book_to_dict(book: BookSnapshot) -> dict[str, object]:
    return {
        "positions": [
            {
                "name": row.name,
                "shares": _json_optional(row.shares),
                "weight": _json_optional(row.weight),
                "target": _json_optional(row.target),
                "price": _json_optional(row.price),
                "code": row.code,
            }
            for row in book.positions
        ],
        "cash_balance": _json_optional(book.cash_balance),
        "cash": {
            "weight": _json_optional(book.cash.weight),
            "target": _json_optional(book.cash.target),
        },
    }


def load_book_dict(session: Session) -> dict[str, object]:
    rows, cash = load_positions(session)
    return book_to_dict(project_book(session, rows, cash))


def load_financials(session: Session, code: str) -> list[dict[str, object]]:
    rows = session.scalars(select(Financial).where(Financial.code == code)).all()
    return [
        {
            "period": row.period,
            "period_type": row.period_type,
            "roe": _json_optional(row.roe),
            "net_income": _json_optional(row.net_income),
            "ocf": _json_optional(row.ocf),
            "capex": _json_optional(row.capex),
            "ebit": _json_optional(row.ebit),
            "interest": _json_optional(row.interest),
            "gross_margin": _json_optional(row.gross_margin),
            "net_margin": _json_optional(row.net_margin),
        }
        for row in rows
    ]


def ensure_financials(session: Session, code: str, market) -> None:
    if not code:
        return
    if has_min_financials(load_financials(session, code)):
        return
    if market is None:
        return
    rows = market.fetch_financials([code])
    from jobs.funnel import persist_financials

    persist_financials(session, rows)
    session.commit()


def fetch_price(quote_client, code: str) -> Decimal | None:
    if quote_client is None or not code:
        return None
    try:
        fetched = quote_client.fetch_prices([code])
    except Exception:
        return None
    price = fetched.get(code) if isinstance(fetched, dict) else None
    if price is None:
        return None
    if not isinstance(price, Decimal):
        try:
            price = Decimal(str(price))
        except Exception:
            return None
    if not price.is_finite() or price <= 0:
        return None
    return price


def web_search_enabled(session: Session) -> bool:
    row = session.get(Setting, WEB_SEARCH_KEY)
    return bool(row is not None and row.value == "true")


def evaluate_finalists(
    session: Session,
    run_id: int,
    finalists: list[ScreenRow],
    *,
    llm,
    quote_client,
    reports_dir: Path,
    market=None,
) -> None:
    book = load_book_dict(session)
    search = web_search_enabled(session)
    as_of = _now().date().isoformat()
    for row in finalists[:3]:
        job = ResearchJob(
            user_id=LOCAL_USER_ID,
            screen_run_id=run_id,
            name=row.name,
            code=row.code,
            status="queued",
            error=None,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(job)
        session.commit()
        _run_one(
            session,
            job,
            llm=llm,
            quote_client=quote_client,
            reports_dir=reports_dir,
            book=book,
            web_search=search,
            as_of_date=as_of,
            market=market,
        )


def execute_research(
    session: Session,
    job_id: int,
    *,
    llm,
    quote_client,
    reports_dir: Path,
    market=None,
) -> None:
    job = session.get(ResearchJob, job_id)
    if job is None:
        try:
            release(session, job_id)
            session.commit()
        except Exception:
            session.rollback()
        return
    try:
        _run_one(
            session,
            job,
            llm=llm,
            quote_client=quote_client,
            reports_dir=reports_dir,
            book=load_book_dict(session),
            web_search=web_search_enabled(session),
            as_of_date=_now().date().isoformat(),
            market=market,
        )
    finally:
        try:
            release(session, job_id)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("释放任务锁失败")


def _run_one(
    session: Session,
    job: ResearchJob,
    *,
    llm,
    quote_client,
    reports_dir: Path,
    book: dict[str, object],
    web_search: bool,
    as_of_date: str,
    market=None,
) -> None:
    job.status = "running"
    job.updated_at = _now()
    session.commit()
    row = session.get(Setting, LLM_MODEL_KEY)
    model = (row.value or "").strip() if row is not None else ""
    if model and hasattr(llm, "model"):
        llm.model = model
    try:
        ensure_financials(session, job.code, market)
        output = run_checklist(
            llm,
            EvalInput(
                name=job.name,
                code=job.code,
                financials=tuple(load_financials(session, job.code)),
                price=fetch_price(quote_client, job.code),
                book=book,
                web_search=web_search,
                as_of_date=as_of_date,
                job_id=job.id,
                reports_dir=reports_dir,
            ),
        )
        session.add(
            ResearchArtifact(
                research_job_id=job.id,
                structured_json=json.dumps(output.structured, ensure_ascii=False),
                markdown=output.markdown,
                report_path=output.report_path,
            )
        )
        verdict = str(output.structured.get("verdict") or "").strip().lower()
        if eligible(output.structured):
            upsert(
                session,
                user_id=job.user_id,
                name=job.name,
                research_job_id=job.id,
                structured=output.structured,
            )
        elif verdict == "fail":
            clear_on_fail(session, user_id=job.user_id, name=job.name)
        job.status = "done"
        job.error = None
        job.updated_at = _now()
        session.commit()
    except Exception as exc:
        session.rollback()
        fresh = session.get(ResearchJob, job.id)
        if fresh is not None:
            fresh.status = "failed"
            fresh.error = str(exc)
            fresh.updated_at = _now()
            session.commit()
        logger.exception("评估子任务失败: %s", job.name)
