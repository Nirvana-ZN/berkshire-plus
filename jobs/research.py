from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.discovery_models import DiscoveryResult, DiscoveryRun
from app.envfile import TAVILY_API_KEY
from app.models import (
    Financial,
    Instrument,
    JobLock,
    Quote,
    ResearchArtifact,
    ResearchJob,
    Setting,
    Thesis,
)
from jobs.lock import JOB_TYPE_RESEARCH, LOCK_ROW_ID, release
from ledger.positions import LOCAL_USER_ID, load_positions
from ledger.quotes import BookSnapshot, project_book
from ledger.rules import sync_thesis_ceiling_rule
from market.sentiment import is_extreme_greed
from research.evaluate import EvalInput, has_min_financials, render_markdown, run_checklist
from research.thesis import (
    band_bounds,
    dynamic_sizing,
    eligible,
    financial_drift,
    load_by_names,
    parse_structured,
    price_usable,
    upsert,
)
from screen.funnel import ScreenRow
from screen.replay import apply_credibility_gate, gate_is_open

logger = logging.getLogger(__name__)

WEB_SEARCH_KEY = "web_search"
LLM_MODEL_KEY = "llm_model"


class ResearchLeaseLostError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_research_job(
    session: Session,
    *,
    name: str,
    code: str,
    screen_run_id: int | None = None,
    discovery_run_id: int | None = None,
    discovery_track: str | None = None,
) -> ResearchJob:
    now = _now()
    job = ResearchJob(
        user_id=LOCAL_USER_ID,
        screen_run_id=screen_run_id,
        discovery_run_id=discovery_run_id,
        discovery_track=discovery_track,
        name=name,
        code=code,
        status="queued",
        error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    return job


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
                "no_target": row.no_target,
                "price": _json_optional(row.price),
                "code": row.code,
            }
            for row in book.positions
        ],
        "cash_balance": _json_optional(book.cash_balance),
        "cash": {
            "weight": _json_optional(book.cash.weight),
            "target": _json_optional(book.cash.target),
            "no_target": book.cash.no_target,
        },
    }


def load_book_dict(session: Session) -> dict[str, object]:
    rows, cash = load_positions(session)
    book = project_book(session, rows, cash)
    payload = book_to_dict(book)
    positions = payload.get("positions")
    if isinstance(positions, list) and positions:
        theses = load_by_names(
            session,
            LOCAL_USER_ID,
            [str(item.get("name") or "") for item in positions if item.get("name")],
        )
        for item in positions:
            thesis = theses.get(str(item.get("name") or ""))
            if thesis is None:
                continue
            code = str(item.get("code") or "").strip()
            fins = load_financials(session, code) if code else ()
            usable = price_usable(item.get("price"), stale=bool(item.get("stale")))
            item["dynamic_sizing"] = dynamic_sizing(
                parse_structured(thesis),
                current_price=item.get("price"),
                price_usable=usable,
                actual_weight=item.get("weight"),
                financials=fins,
            )
    return payload


def load_financials(session: Session, code: str) -> list[dict[str, object]]:
    rows = session.scalars(select(Financial).where(Financial.code == code)).all()
    return [
        {
            "period": row.period,
            "period_type": row.period_type,
            "roe": _json_optional(row.roe),
            "roic": _json_optional(row.roic),
            "revenue": _json_optional(row.revenue),
            "net_income": _json_optional(row.net_income),
            "deducted_net_income": _json_optional(row.deducted_net_income),
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
            "goodwill": _json_optional(row.goodwill),
            "net_assets": _json_optional(row.net_assets),
            "cash": _json_optional(row.cash),
            "interest_debt": _json_optional(row.interest_debt),
        }
        for row in rows
    ]


def _has_bps(rows: list[dict[str, object]]) -> bool:
    return any(row.get("bps") is not None for row in rows)


def _has_interim(rows: list[dict[str, object]]) -> bool:
    return any(str(row.get("period_type") or "annual") != "annual" for row in rows)


def _has_deducted(rows: list[dict[str, object]]) -> bool:
    return any(row.get("deducted_net_income") is not None for row in rows)


def ensure_financials(session: Session, code: str, market) -> None:
    if not code:
        return
    cached = load_financials(session, code)
    session.rollback()
    # annual-only or pre-deducted cache is refetched so H1 and deducted_net_income land
    if (
        has_min_financials(cached)
        and _has_bps(cached)
        and _has_interim(cached)
        and _has_deducted(cached)
    ):
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


def _price_for_eval(
    quote_client,
    *,
    code: str,
    saved_price: Decimal | None,
) -> Decimal | None:
    price = fetch_price(quote_client, code)
    if price is None:
        price = fetch_price(quote_client, code)
    return price if price is not None else saved_price


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
        job = create_research_job(
            session,
            screen_run_id=run_id,
            name=row.name,
            code=row.code,
        )
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
    lock_owner_token: str | None = None,
) -> None:
    job = session.get(ResearchJob, job_id)
    if job is None:
        try:
            session.rollback()
            release(
                session,
                job_id,
                JOB_TYPE_RESEARCH,
                owner_token=lock_owner_token,
            )
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
            lock_owner_token=lock_owner_token,
            require_research_lease=True,
        )
    finally:
        try:
            session.rollback()
            release(
                session,
                job_id,
                JOB_TYPE_RESEARCH,
                owner_token=lock_owner_token,
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("释放任务锁失败")


def _industry_of(session: Session, code: str, industry: str = "") -> str:
    if industry:
        return industry
    inst = session.get(Instrument, code)
    return (inst.industry or "") if inst is not None else ""


def _load_industry_precedents(
    session: Session,
    *,
    user_id: int,
    code: str,
    industry: str,
    limit: int = 2,
) -> tuple[dict[str, object], ...]:
    if not industry or not code or limit <= 0:
        return ()
    peer_jobs = session.scalars(
        select(ResearchJob)
        .join(Instrument, Instrument.code == ResearchJob.code)
        .where(
            ResearchJob.user_id == user_id,
            ResearchJob.status == "done",
            ResearchJob.code != code,
            Instrument.industry == industry,
        )
        .order_by(ResearchJob.id.desc())
        .limit(limit * 4)
    ).all()
    if not peer_jobs:
        return ()
    seen_codes: set[str] = set()
    chosen_jobs: list[ResearchJob] = []
    for peer in peer_jobs:
        if peer.code in seen_codes:
            continue
        seen_codes.add(peer.code)
        chosen_jobs.append(peer)
        if len(chosen_jobs) >= limit:
            break
    artifacts: dict[int, ResearchArtifact] = {}
    for art in session.scalars(
        select(ResearchArtifact).where(
            ResearchArtifact.research_job_id.in_([j.id for j in chosen_jobs])
        )
    ).all():
        prev = artifacts.get(art.research_job_id)
        if prev is None or art.id > prev.id:
            artifacts[art.research_job_id] = art
    out: list[dict[str, object]] = []
    for peer in chosen_jobs:
        art = artifacts.get(peer.id)
        if art is None:
            continue
        try:
            parsed = json.loads(art.structured_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        failed_gates = [
            {"name": g.get("name"), "status": g.get("status"), "reason": g.get("reason")}
            for g in (parsed.get("gates") or ())
            if isinstance(g, dict) and g.get("status") in {"fail", "gray"}
        ]
        out.append(
            {
                "name": peer.name,
                "code": peer.code,
                "verdict": parsed.get("verdict"),
                "as_of": parsed.get("as_of"),
                "failed_gates": failed_gates,
                "price_bands": parsed.get("price_bands"),
            }
        )
    return tuple(out)


def _load_discovery_risk_flags(
    session: Session,
    *,
    code: str,
    discovery_run_id: int | None = None,
    discovery_track: str | None = None,
    financials: list[dict[str, object]] | None = None,
) -> tuple[str, ...]:
    seen: list[str] = []
    if code:
        run_id = discovery_run_id
        if run_id is None:
            latest_run = session.scalars(
                select(DiscoveryRun)
                .where(DiscoveryRun.status == "completed")
                .order_by(DiscoveryRun.id.desc())
            ).first()
            run_id = latest_run.id if latest_run is not None else None
        if run_id is not None:
            stmt = select(DiscoveryResult).where(
                DiscoveryResult.run_id == run_id,
                DiscoveryResult.stage == "track_gate",
                DiscoveryResult.code == code,
            )
            if discovery_track:
                stmt = stmt.where(DiscoveryResult.track == discovery_track)
            rows = session.scalars(stmt.order_by(DiscoveryResult.id.asc())).all()
            for row in rows:
                try:
                    metrics = json.loads(row.metrics_json)
                except json.JSONDecodeError:
                    continue
                if not isinstance(metrics, dict):
                    continue
                for flag in metrics.get("risk_flags") or ():
                    text = str(flag or "").strip()
                    if text and text not in seen:
                        seen.append(text)
    drift = financial_drift({"as_of": {"date": "2026-01-01"}}, financials or ())
    if drift is not None:
        reasons = drift.get("reasons") or ()
        if "deducted_profit_drop" in reasons and drift.get("deducted_net_income_yoy") is not None:
            seen.append(
                f"{drift['period']} vs {drift['base_period']} 扣非净利润同比 {float(drift['deducted_net_income_yoy']):.2%}（主业扣非显著下滑，谨防非经常性损益粉饰归母净利）"
            )
        elif "profit_drop" in reasons and drift.get("net_income_yoy") is not None:
            seen.append(
                f"{drift['period']} vs {drift['base_period']} 归母净利润同比 {float(drift['net_income_yoy']):.2%}（同报告期同比下滑，非季节性波动）"
            )
        if "ocf_turned_negative" in reasons:
            seen.append(
                f"{drift['period']} vs {drift['base_period']} 经营活动现金流由正转负"
            )
        if "consecutive_decline" in reasons:
            seen.append(
                f"{drift['period']} 与最近年报连续两期盈利同比下滑（警惕温水煮青蛙式基本面融冰）"
            )
    ann_roes = [
        (str(r.get("period")), float(r["roe"]))
        for r in (financials or ())
        if isinstance(r, dict)
        and str(r.get("period_type") or "annual") == "annual"
        and str(r.get("period") or "").isdigit()
        and r.get("roe") is not None
    ]
    ann_roes.sort(key=lambda x: int(x[0]))
    if len(ann_roes) >= 4:
        r0, r1, r2, r3 = ann_roes[-4][1], ann_roes[-3][1], ann_roes[-2][1], ann_roes[-1][1]
        if r0 > r1 > r2 > r3 and (r0 - r3) >= 4.0:
            seen.append(
                f"ROE连续3年下台阶（{ann_roes[-4][0]}年{r0:.1f}% → {ann_roes[-1][0]}年{r3:.1f}%，累计降{r0 - r3:.1f}个百分点，警惕估值陷阱）"
            )
    return tuple(seen)


def _claim_research_lease(
    session: Session,
    job_id: int,
    owner_token: str | None,
) -> None:
    token_clause = (
        JobLock.owner_token.is_(None)
        if owner_token is None
        else JobLock.owner_token == owner_token
    )
    claimed = session.execute(
        update(JobLock)
        .where(
            JobLock.id == LOCK_ROW_ID,
            JobLock.job_id == job_id,
            JobLock.job_type == JOB_TYPE_RESEARCH,
            token_clause,
        )
        .values(held_at=JobLock.held_at)
    )
    if int(claimed.rowcount or 0) != 1:
        raise ResearchLeaseLostError("research job lock ownership was lost")


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
    lock_owner_token: str | None = None,
    require_research_lease: bool = False,
) -> None:
    job_id = job.id
    user_id = job.user_id
    job_name = job.name
    job_code = job.code
    if require_research_lease:
        _claim_research_lease(session, job_id, lock_owner_token)
    job.status = "running"
    job.updated_at = _now()
    session.commit()
    row = session.get(Setting, LLM_MODEL_KEY)
    model = (row.value or "").strip() if row is not None else ""
    if model and hasattr(llm, "model"):
        llm.model = model
    try:
        ensure_financials(session, job_code, market)
        rows = load_financials(session, job_code)
        saved_price = _saved_price(
            session, name=job_name, code=job_code
        )
        industry_name = _industry_of(
            session, job_code, industry
        )
        precedents = _load_industry_precedents(
            session,
            user_id=user_id,
            code=job_code,
            industry=industry_name,
        )
        risk_flags = _load_discovery_risk_flags(
            session,
            code=job_code,
            discovery_run_id=job.discovery_run_id,
            discovery_track=job.discovery_track,
            financials=rows,
        )
        credibility_gate = gate_is_open(session)
        session.rollback()
        if not credibility_gate:
            credibility_gate = is_extreme_greed(session, market)
            session.commit()
        output = run_checklist(
            llm,
            EvalInput(
                name=job_name,
                code=job_code,
                financials=tuple(rows),
                price=_price_for_eval(
                    quote_client,
                    code=job_code,
                    saved_price=saved_price,
                ),
                book=book,
                web_search=web_search,
                as_of_date=as_of_date,
                job_id=job_id,
                reports_dir=reports_dir,
                valuation=fetch_valuation(market, job_code, rows),
                industry=industry_name,
                notices=tuple(_fetch_notices(market, job_code)),
                precedents=precedents,
                risk_flags=risk_flags,
            ),
        )
        if credibility_gate:
            apply_credibility_gate(output.structured)
            output = replace(
                output,
                markdown=render_markdown(
                    job_name, output.structured, web_search=web_search
                ),
            )
        if require_research_lease:
            _claim_research_lease(session, job_id, lock_owner_token)
        fresh = session.get(ResearchJob, job_id)
        if fresh is None:
            session.rollback()
            return
        session.add(
            ResearchArtifact(
                research_job_id=job_id,
                structured_json=json.dumps(output.structured, ensure_ascii=False),
                markdown=output.markdown,
                report_path=output.report_path,
            )
        )
        existing = session.scalars(
            select(Thesis).where(
                Thesis.user_id == user_id,
                Thesis.name == job_name,
            )
        ).first()
        if eligible(output.structured, replacing=existing is not None):
            upsert(
                session,
                user_id=user_id,
                name=job_name,
                research_job_id=job_id,
                structured=output.structured,
            )
            if output.structured.get("verdict") in {"pass", "gray"}:
                as_of_obj = output.structured.get("as_of")
                as_of_p = (
                    _as_decimal(as_of_obj.get("price"))
                    if isinstance(as_of_obj, dict)
                    else None
                )
                _, rich = band_bounds(output.structured, ref_price=as_of_p)
                if rich is not None and (as_of_p is None or rich >= as_of_p):
                    sync_thesis_ceiling_rule(
                        session,
                        name=job_name,
                        ceiling_price=rich,
                        research_job_id=job_id,
                        user_id=user_id,
                    )
        fresh.status = "done"
        fresh.error = None
        fresh.updated_at = _now()
        session.commit()
    except ResearchLeaseLostError:
        session.rollback()
    except Exception as exc:
        session.rollback()
        if require_research_lease:
            try:
                _claim_research_lease(session, job_id, lock_owner_token)
            except ResearchLeaseLostError:
                session.rollback()
                return
        fresh = session.get(ResearchJob, job_id)
        if fresh is not None:
            fresh.status = "failed"
            fresh.error = str(exc)
            fresh.updated_at = _now()
            session.commit()
        logger.exception("评估子任务失败: %s", job_name)
