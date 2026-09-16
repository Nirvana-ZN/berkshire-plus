from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.envfile import TAVILY_API_KEY
from app.models import Financial, Instrument, Quote, ResearchArtifact, ResearchJob, Setting, Thesis
from jobs.lock import JOB_TYPE_RESEARCH, release
from ledger.positions import LOCAL_USER_ID, load_positions
from ledger.quotes import BookSnapshot, project_book
from market.sentiment import is_extreme_greed
from research.evaluate import EvalInput, has_min_financials, render_markdown, run_checklist
from research.thesis import eligible, upsert
from screen.funnel import ScreenRow
from screen.replay import apply_credibility_gate, gate_is_open

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
            "shares": _json_optional(row.shares),
            "bps": _json_optional(row.bps),
            "npl_pct": _json_optional(row.npl),
            "eps": _json_optional(row.eps),
        }
        for row in rows
    ]


def _has_bps(rows: list[dict[str, object]]) -> bool:
    return any(row.get("bps") is not None for row in rows)


def _has_interim(rows: list[dict[str, object]]) -> bool:
    return any(str(row.get("period_type") or "annual") != "annual" for row in rows)


def ensure_financials(session: Session, code: str, market) -> None:
    if not code:
        return
    cached = load_financials(session, code)
    # annual-only cache is from before interim rows; refetch so H1 lands
    if has_min_financials(cached) and _has_bps(cached) and _has_interim(cached):
        return
    if market is None:
        return
    rows = market.fetch_financials([code])
    from jobs.funnel import persist_financials

    persist_financials(session, rows)
    session.commit()


def fetch_valuation(market, code: str, financials: list[dict[str, object]] | None = None) -> dict[str, object]:
    raw: dict[str, object] = {}
    if market is not None and code and hasattr(market, "fetch_valuation"):
        try:
            fetched = market.fetch_valuation(code)
        except Exception:
            fetched = {}
        if isinstance(fetched, dict):
            raw.update(fetched)
    if not any(raw.get(key) is not None for key in ("bps", "npl_pct", "eps")):
        raw.update(_valuation_from_financials(financials or []))
    out: dict[str, object] = {}
    for key in ("bps", "npl_pct", "eps", "pb", "pe"):
        if key in raw:
            number = _json_optional(_as_decimal(raw.get(key)))
            if number is not None:
                out[key] = number
    period = raw.get("period")
    if period is not None and str(period).strip():
        out["period"] = str(period).strip()[:4]
    eps_period = raw.get("eps_period")
    if eps_period is not None and str(eps_period).strip():
        out["eps_period"] = str(eps_period).strip()[:4]
    return out


def _valuation_from_financials(rows: list[dict[str, object]]) -> dict[str, object]:
    best: dict[str, object] | None = None
    for row in rows:
        if row.get("bps") is None and row.get("npl_pct") is None and row.get("eps") is None:
            continue
        if best is None or str(row.get("period") or "") > str(best.get("period") or ""):
            best = row
    if best is None:
        return {}
    out: dict[str, object] = {}
    for key in ("bps", "npl_pct", "eps"):
        if best.get(key) is not None:
            out[key] = best[key]
    if best.get("period") is not None:
        out["period"] = best["period"]
    return out


def _fetch_notices(market, code: str) -> list[dict[str, str]]:
    if market is None or not code or not hasattr(market, "fetch_notices"):
        return []
    try:
        rows = market.fetch_notices(code)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _as_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    return number if number.is_finite() else None


def _saved_price(session: Session, *, name: str, code: str) -> Decimal | None:
    row = session.get(Quote, (LOCAL_USER_ID, name))
    if row is None and code:
        row = session.scalars(
            select(Quote).where(Quote.user_id == LOCAL_USER_ID, Quote.code == code)
        ).first()
    if row is None:
        return None
    price = Decimal(row.price)
    if not price.is_finite() or price <= 0:
        return None
    return price


def _price_for_eval(session: Session, quote_client, *, name: str, code: str) -> Decimal | None:
    price = fetch_price(quote_client, code)
    if price is None:
        price = fetch_price(quote_client, code)
    if price is not None:
        return price
    return _saved_price(session, name=name, code=code)


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
    if row is None or (row.value or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    return bool((os.environ.get(TAVILY_API_KEY) or "").strip())


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
            industry=row.industry,
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
            release(session, job_id, JOB_TYPE_RESEARCH)
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
            release(session, job_id, JOB_TYPE_RESEARCH)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("释放任务锁失败")


def _industry_of(session: Session, code: str, industry: str = "") -> str:
    if industry:
        return industry
    inst = session.get(Instrument, code)
    return (inst.industry or "") if inst is not None else ""


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
    industry: str = "",
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
        rows = load_financials(session, job.code)
        output = run_checklist(
            llm,
            EvalInput(
                name=job.name,
                code=job.code,
                financials=tuple(rows),
                price=_price_for_eval(session, quote_client, name=job.name, code=job.code),
                book=book,
                web_search=web_search,
                as_of_date=as_of_date,
                job_id=job.id,
                reports_dir=reports_dir,
                valuation=fetch_valuation(market, job.code, rows),
                industry=_industry_of(session, job.code, industry),
                notices=tuple(_fetch_notices(market, job.code)),
            ),
        )
        if gate_is_open(session) or is_extreme_greed(session, market):
            apply_credibility_gate(output.structured)
            output = replace(
                output,
                markdown=render_markdown(
                    job.name, output.structured, web_search=web_search
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
        existing = session.scalars(
            select(Thesis).where(
                Thesis.user_id == job.user_id,
                Thesis.name == job.name,
            )
        ).first()
        if eligible(output.structured, replacing=existing is not None):
            upsert(
                session,
                user_id=job.user_id,
                name=job.name,
                research_job_id=job.id,
                structured=output.structured,
            )
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
