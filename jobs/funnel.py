from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Financial, Instrument, ScreenResult, ScreenRun
from jobs.lock import JOB_TYPE_FUNNEL, release
from jobs.research import evaluate_finalists
from ledger.positions import load_positions
from market.types import Constituent, FinancialRow, MarketError
from screen.funnel import FunnelResult, ScreenRow, run_funnel

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if not value.is_finite():
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def held_industries(session: Session, snapshots: list[Constituent]) -> set[str]:
    rows, _cash = load_positions(session)
    held_names = {row.name for row in rows}
    return {snap.industry for snap in snapshots if snap.name in held_names and snap.industry}


def persist_financials(session: Session, rows: list[FinancialRow]) -> None:
    for row in rows:
        session.merge(
            Financial(
                code=row.code,
                period=row.period,
                period_type=row.period_type,
                roe=row.roe,
                net_income=row.net_income,
                ocf=row.ocf,
                capex=row.capex,
                ebit=row.ebit,
                interest=row.interest,
                gross_margin=row.gross_margin,
                net_margin=row.net_margin,
                shares=row.shares,
                share_change_ma=row.share_change_ma,
            )
        )


def persist_listed_years(session: Session, years: dict[str, int]) -> None:
    for code, year in years.items():
        existing = session.get(Instrument, code)
        if existing is not None:
            existing.listed_year = year


def persist_results(session: Session, run: ScreenRun, result: FunnelResult) -> None:
    session.execute(ScreenResult.__table__.delete().where(ScreenResult.screen_run_id == run.id))
    for row in (*result.keep, *result.drop, *result.insufficient):
        session.add(_to_result(run.id, row))


def _to_result(run_id: int, row: ScreenRow) -> ScreenResult:
    metrics = {
        "role": row.role,
        "finalist": row.finalist,
        "mcap": _json_number(row.mcap),
        "pe": _json_number(row.pe),
        "peg": _json_number(row.peg),
        "industry": row.industry,
        "roe_mean": _json_number(row.roe_mean),
        "ocf_ni": _json_number(row.ocf_ni),
    }
    return ScreenResult(
        screen_run_id=run_id,
        code=row.code,
        name=row.name,
        verdict=row.verdict,
        reason=row.reason,
        metrics_json=json.dumps(metrics, ensure_ascii=False),
    )


def execute_funnel(
    session: Session,
    run_id: int,
    market,
    *,
    evaluate: bool = False,
    llm=None,
    quote_client=None,
    reports_dir: Path | None = None,
) -> None:
    run = session.get(ScreenRun, run_id)
    if run is None:
        return
    try:
        run.status = "running"
        run.updated_at = _now()
        session.commit()

        snapshots = market.fetch_constituents(run.scope_type, run.scope_text)
        run.snapshot_json = json.dumps(
            [
                {
                    "code": item.code,
                    "name": item.name,
                    "mcap": _json_number(item.mcap),
                    "pe": _json_number(item.pe),
                    "industry": item.industry,
                    "peg": _json_number(item.peg),
                }
                for item in snapshots
            ],
            ensure_ascii=False,
        )
        codes = [item.code for item in snapshots]
        rows = market.fetch_financials(codes)
        persist_financials(session, rows)
        years = {}
        if hasattr(market, "fetch_listed_years"):
            years = market.fetch_listed_years(codes) or {}
        persist_listed_years(session, years)
        held = held_industries(session, snapshots)
        result = run_funnel(snapshots, rows, years, held)
        persist_results(session, run, result)
        run.status = "done"
        run.error = None
        run.updated_at = _now()
        session.commit()
        if evaluate and result.finalists:
            try:
                evaluate_finalists(
                    session,
                    run_id,
                    result.finalists,
                    llm=llm,
                    quote_client=quote_client,
                    reports_dir=reports_dir or Path("reports"),
                    market=market,
                )
            except Exception:
                logger.exception("终选评估失败")
                try:
                    session.rollback()
                except Exception:
                    pass
    except MarketError as exc:
        session.rollback()
        run = session.get(ScreenRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error = exc.message
            run.updated_at = _now()
            session.commit()
    except Exception as exc:
        logger.exception("漏斗任务失败")
        session.rollback()
        run = session.get(ScreenRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error = str(exc)
            run.updated_at = _now()
            session.commit()
    finally:
        try:
            release(session, run_id)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("释放任务锁失败")
