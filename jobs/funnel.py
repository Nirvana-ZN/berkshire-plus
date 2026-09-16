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
from screen.replay import dump_snapshot, gate_is_open, last_finalist_count, snapshot_constituents

logger = logging.getLogger(__name__)

_LEGACY_FINANCIAL_FIELDS = (
    "roe",
    "net_income",
    "ocf",
    "capex",
    "ebit",
    "interest",
    "gross_margin",
    "net_margin",
    "shares",
    "share_change_ma",
    "bps",
    "npl",
    "eps",
)


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
    found = {snap.industry for snap in snapshots if snap.name in held_names and snap.industry}
    for inst in session.scalars(select(Instrument)).all():
        if inst.name in held_names and inst.industry:
            found.add(inst.industry)
    if len(found) >= len(held_names):
        return found
    for run in session.scalars(select(ScreenRun).order_by(ScreenRun.id.desc()).limit(30)):
        try:
            payload = json.loads(run.snapshot_json or "[]")
        except json.JSONDecodeError:
            continue
        for item in snapshot_constituents(payload):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            industry = str(item.get("industry") or "")
            if name in held_names and industry:
                found.add(industry)
    return found


def persist_industries(session: Session, snapshots: list[Constituent]) -> None:
    for snap in snapshots:
        existing = session.get(Instrument, snap.code)
        if existing is not None and snap.industry:
            existing.industry = snap.industry


def persist_financials(session: Session, rows: list[FinancialRow]) -> None:
    for row in rows:
        stored = session.get(
            Financial,
            (row.code, row.period, row.period_type),
        )
        if stored is None:
            stored = Financial(
                code=row.code,
                period=row.period,
                period_type=row.period_type,
            )
            session.add(stored)
        for field in _LEGACY_FINANCIAL_FIELDS:
            setattr(stored, field, getattr(row, field))


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
    try:
        run = session.get(ScreenRun, run_id)
        if run is None:
            return
        run.status = "running"
        run.updated_at = _now()
        session.commit()

        snapshots = market.fetch_constituents(run.scope_type, run.scope_text)
        codes = [item.code for item in snapshots]
        rows = market.fetch_financials(codes)
        persist_financials(session, rows)
        years = {}
        if hasattr(market, "fetch_listed_years"):
            years = market.fetch_listed_years(codes) or {}
        persist_listed_years(session, years)
        persist_industries(session, snapshots)
        held = held_industries(session, snapshots)
        extra: dict[str, object] = {}
        if gate_is_open(session):
            cap = last_finalist_count(session)
            if cap is not None:
                extra["finalist_limit"] = cap
        result = run_funnel(snapshots, rows, years, held, **extra)
        run.snapshot_json = json.dumps(
            dump_snapshot(snapshots, rows, years, held),
            ensure_ascii=False,
        )
        persist_results(session, run, result)
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
            except Exception as exc:
                logger.exception("终选评估失败")
                try:
                    session.rollback()
                except Exception:
                    pass
                run = session.get(ScreenRun, run_id)
                if run is not None:
                    run.error = f"终选评估失败: {exc}"
                    run.updated_at = _now()
                    session.commit()
        run = session.get(ScreenRun, run_id)
        if run is not None:
            run.status = "done"
            run.updated_at = _now()
            session.commit()
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
            release(session, run_id, JOB_TYPE_FUNNEL)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("释放任务锁失败")
