from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import PROJECT_ROOT, create_session_factory, init_db
from app.discovery_models import DiscoveryRun
from app.discovery_routes import (
    cancel_discovery_research_worker,
    create_discovery_router,
    resume_discovery_queues,
)
from app.envfile import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    TAVILY_API_KEY,
    has_env_value,
    hot_load,
    load_env_key,
    read_env_value,
    resolve_env_path,
    write_env_value,
)
from app.models import (
    PortfolioReview,
    Quote,
    ResearchArtifact,
    ResearchJob,
    ScreenResult,
    ScreenRun,
    Setting,
    Thesis,
    Watchlist,
)
from discovery.data_readiness import (
    TRACKS,
    ReadinessReport,
    TrackReadiness,
    discovery_data_readiness,
)
from discovery.snapshot import build_production_snapshot
from jobs.cheap import (
    begin_cheap_fill,
    cheap_fill_running,
    clear_cheap_fill,
    execute_cheap_fill,
    load_cheap_board,
)
from jobs.discovery_data import (
    DEFAULT_POLICY,
    discovery_data_status,
    execute_discovery_data_fill,
    start_discovery_data_fill,
)
from jobs.funnel import execute_funnel
from jobs.discovery import builtin_track_executor
from jobs.lock import (
    JOB_TYPE_DISCOVERY,
    JOB_TYPE_DISCOVERY_DATA,
    JOB_TYPE_FUNNEL,
    JOB_TYPE_RESEARCH,
    JOB_TYPE_REVIEW,
    clear_stale_lock,
    lock_status,
    release,
    try_acquire,
)
from jobs.research import execute_research
from jobs.review import execute_review
from ledger.positions import LOCAL_USER_ID, load_positions
from ledger.quotes import (
    QuoteError,
    QuotedPosition,
    apply_refresh,
    instrument_code_by_name,
    is_a_share_code,
    lookup_saved_instrument,
    project_book,
    rebalance_alerts,
    save_instrument,
    set_manual_price,
)
from ledger.rules import RuleError, RuleRow, delete_rule, list_rules, save_rule
from ledger.targets import TargetError, reclaim_orphan_targets, save_targets, seed_targets_if_empty
from ledger.trades import TradeError, record_trade, set_cash, set_position
from market.eastmoney import EastMoneyClient, MarketClient
from market.sentiment import load_sentiment
from market.types import MarketError
from market.quotes import QuoteClient, TencentQuoteClient
from research.llm import OpenAICompatClient
from screen.ruler import RulerError, activate as activate_ruler, require_matched, status as ruler_status
from research.thesis import (
    api_fields,
    expected_price,
    gap_pct,
    list_aftermath,
    load_by_names,
    parse_structured,
    price_usable,
    thesis_mark,
    utc_today,
)
from migrate.yaml_portfolio import is_migrated, migrate_yaml_portfolio, stored_yaml_dir

DEFAULT_DIST_PATH = PROJECT_ROOT / "web" / "dist"
LLM_MODEL_KEY = "llm_model"
WEB_SEARCH_KEY = "web_search"
SCOPE_TYPES = frozenset({"行业", "指数", "主题", "industry", "index", "theme"})
logger = logging.getLogger(__name__)


def _error_json(
    status: int, *, error: str, code: str, detail: str, **extra: object
) -> JSONResponse:
    body: dict[str, object] = {"error": error, "code": code, "detail": detail}
    body.update(extra)
    return JSONResponse(body, status_code=status)


def _json_number(value: Decimal) -> int | float:
    quantized = Decimal(value)
    if not quantized.is_finite():
        raise ValueError("non-finite decimal")
    if quantized == quantized.to_integral_value():
        return int(quantized)
    return float(quantized)


def _json_optional(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    quantized = Decimal(value)
    if not quantized.is_finite():
        return None
    return _json_number(quantized)


def _readiness_json(report: ReadinessReport) -> dict[str, object]:
    if set(report.tracks) != set(TRACKS):
        raise ValueError("readiness track set is incomplete")
    if not set(report.approved_thresholds) <= set(TRACKS):
        raise ValueError("readiness threshold track key is invalid")
    if any(
        not isinstance(value, Decimal)
        for value in report.approved_thresholds.values()
    ):
        raise TypeError("readiness thresholds must be Decimal values")
    if any(
        not isinstance(row, TrackReadiness) or row.track != name
        for name, row in report.tracks.items()
    ):
        raise TypeError("readiness tracks must contain matching TrackReadiness rows")
    for field_name, values in (
        ("missing_counts", report.missing_counts),
        ("company_insufficient", report.company_insufficient),
        ("observation_aggregates", report.observation_aggregates),
    ):
        if set(values) != set(TRACKS):
            raise ValueError(f"readiness {field_name} track set is incomplete")
    return {
        "status": report.status,
        "evaluated_as_of": report.evaluated_as_of,
        "tracks": {
            name: {
                "track": row.track,
                "ready": row.ready,
                "eligible": row.eligible,
                "complete": row.complete,
                "coverage": _json_number(row.coverage),
                "missing_fields": list(row.missing_fields),
                "reason": row.reason,
                "activated": row.activated,
                "threshold": (
                    _json_number(row.threshold)
                    if row.threshold is not None
                    else None
                ),
            }
            for name, row in report.tracks.items()
        },
        "approved_thresholds": {
            name: _json_number(value)
            for name, value in report.approved_thresholds.items()
        },
        "minimum_observations": {
            track: dict(fields)
            for track, fields in report.minimum_observations.items()
        },
        "valuation_policies": dict(report.valuation_policies),
        "sector_policies": dict(report.sector_policies),
        "missing_counts": {
            track: dict(counts) for track, counts in report.missing_counts.items()
        },
        "company_insufficient": {
            track: dict(counts)
            for track, counts in report.company_insufficient.items()
        },
        "observation_aggregates": {
            track: {
                field: {
                    "companies": aggregate.companies,
                    "observations": aggregate.observations,
                    "maximum": aggregate.maximum,
                }
                for field, aggregate in fields.items()
            }
            for track, fields in report.observation_aggregates.items()
        },
        "source_as_of": dict(report.source_as_of),
    }


def _resolve_research_target(session, market, name: str, code: str, query: str) -> tuple[str, str]:
    if name and code:
        if not is_a_share_code(code):
            raise MarketError(f"不是有效的 A 股代码: {code}")
        return name, code
    text = query or name or code
    saved = lookup_saved_instrument(session, text)
    if saved is not None:
        return saved
    found = market.lookup_equity(text)
    return found.name, found.code


def _positions_payload(
    session, rows, cash: Decimal
) -> dict[str, object]:
    book = project_book(session, rows, cash)
    theses = load_by_names(session, LOCAL_USER_ID, [row.name for row in book.positions])
    return {
        "positions": [_position_json(row, theses.get(row.name)) for row in book.positions],
        "cash": {
            "balance": _json_number(book.cash_balance),
            "currency": "CNY",
            "weight": _json_optional(book.cash.weight),
            "target": _json_optional(book.cash.target),
            "no_target": book.cash.no_target,
            "deviation": _json_optional(book.cash.deviation),
        },
        "rebalance": rebalance_alerts(book.positions, book.cash),
    }


def _position_json(row: QuotedPosition, thesis: Thesis | None = None) -> dict[str, object]:
    usable = price_usable(row.price, stale=row.stale)
    extra = api_fields(
        thesis,
        today=utc_today(),
        current_price=row.price,
        price_usable=usable,
    )
    return {
        "name": row.name,
        "shares": _json_number(row.shares),
        "cost": _json_number(row.cost),
        "price": _json_optional(row.price),
        "market_value": _json_optional(row.market_value),
        "pnl": _json_optional(row.pnl),
        "weight": _json_optional(row.weight),
        "stale": row.stale,
        "source": row.source,
        "code": row.code,
        "target": _json_optional(row.target),
        "no_target": row.no_target,
        "deviation": _json_optional(row.deviation),
        "rule_light": row.rule_light,
        "thesis_status": extra["thesis_status"],
        "thesis_research_id": extra["thesis_research_id"],
        "thesis_verdict": extra["thesis_verdict"],
        "overvalued": extra["overvalued"],
        "cheap_bound": extra["cheap_bound"],
        "rich_bound": extra["rich_bound"],
        "live_verdict": extra["live_verdict"],
    }


def _rule_json(row: RuleRow) -> dict[str, object]:
    return {
        "id": row.id,
        "name": row.name,
        "type": row.rule_type,
        "price": _json_number(row.price),
        "note": row.note,
    }


def _rules_payload(session) -> dict[str, object]:
    return {"rules": [_rule_json(row) for row in list_rules(session)]}


def _sync_targets_after_book(session):
    rows, cash = load_positions(session)
    reclaim_orphan_targets(session, {row.name for row in rows})
    seed_targets_if_empty(session, rows, cash)
    return load_positions(session)


def _fetch_prices(client: QuoteClient, codes: list[str]) -> dict[str, Decimal]:
    try:
        return client.fetch_prices(codes)
    except Exception:
        logger.exception("行情客户端失败")
        return {}


def _bulk_refresh(session, client: QuoteClient) -> None:
    rows, _cash = load_positions(session)
    mapping = instrument_code_by_name(session)
    targets = [(row.name, mapping.get(row.name)) for row in rows]
    codes = [
        code
        for _n, code in targets
        if code is not None and is_a_share_code(code)
    ]
    fetched = _fetch_prices(client, codes)
    apply_refresh(session, fetched=fetched, targets=targets, single=False)


def _as_optional_dir(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text)


def _resolve_yaml_dir(yaml_dir: Path | str | None) -> Path | None:
    if yaml_dir is not None:
        return _as_optional_dir(yaml_dir)
    return _as_optional_dir(os.environ.get("PORTFOLIO_YAML_DIR"))


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _evaluate_flag(payload: dict) -> bool:
    if "evaluate" not in payload:
        return True
    return _as_bool(payload.get("evaluate"))


def _missing_key_error(*, detail: str = "评估需要模型密钥，请到设置页配置") -> JSONResponse:
    return _error_json(
        400,
        error="未配置模型密钥",
        code="NO_API_KEY",
        detail=detail,
    )


def _setting_value(session, key: str) -> str:
    row = session.get(Setting, key)
    if row is None or row.value is None:
        return ""
    return row.value


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _secret_from_payload(payload: dict, field: str, *, code: str, detail: str):
    if field not in payload:
        return False, "", None
    raw = payload.get(field)
    if raw is None or not isinstance(raw, str):
        return True, "", _error_json(400, error="请求无效", code=code, detail=detail)
    secret = raw.strip()
    if not secret or "\n" in secret or "\r" in secret:
        return True, "", _error_json(400, error="请求无效", code=code, detail=detail)
    return True, secret, None


def _settings_body(request: Request, session) -> dict[str, object]:
    persisted_dir = stored_yaml_dir(session)
    body: dict[str, object] = {
        "yaml_dir": persisted_dir or request.app.state.yaml_dir,
        "migrated": is_migrated(session),
        "has_api_key": has_env_value(request.app.state.env_path, OPENAI_API_KEY),
        "has_search_key": has_env_value(request.app.state.env_path, TAVILY_API_KEY),
        "llm_model": _setting_value(session, LLM_MODEL_KEY),
        "llm_base_url": read_env_value(request.app.state.env_path, OPENAI_BASE_URL) or "",
        "web_search": _truthy(_setting_value(session, WEB_SEARCH_KEY)),
    }
    migrate_error = request.app.state.migrate_error
    if migrate_error:
        body.update(migrate_error)
    return body


def _screen_row_json(row: ScreenResult) -> dict[str, object]:
    metrics: dict[str, object] = {}
    if row.metrics_json:
        try:
            parsed = json.loads(row.metrics_json)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            metrics = parsed
    return {
        "code": row.code,
        "name": row.name,
        "verdict": row.verdict,
        "reason": row.reason,
        "role": metrics.get("role"),
        "finalist": bool(metrics.get("finalist")),
        "mcap": metrics.get("mcap"),
        "industry": metrics.get("industry"),
    }


def _attach_keep_gaps(session, screen_run_id: int, keep: list[dict[str, object]]) -> None:
    if not keep:
        return
    names = [str(row.get("name") or "") for row in keep if row.get("name")]
    quotes = {
        row.name: row
        for row in session.scalars(
            select(Quote).where(Quote.user_id == LOCAL_USER_ID, Quote.name.in_(names))
        ).all()
    }
    bands: dict[str, dict] = {}
    for name, thesis in load_by_names(session, LOCAL_USER_ID, names).items():
        structured = parse_structured(thesis)
        if structured:
            bands[name] = structured
    jobs = session.scalars(
        select(ResearchJob).where(ResearchJob.screen_run_id == screen_run_id)
    ).all()
    for job in jobs:
        artifact = session.scalars(
            select(ResearchArtifact)
            .where(ResearchArtifact.research_job_id == job.id)
            .order_by(ResearchArtifact.id.desc())
        ).first()
        if artifact is None:
            continue
        try:
            parsed = json.loads(artifact.structured_json)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            bands[job.name] = parsed
    for row in keep:
        name = str(row.get("name") or "")
        quote = quotes.get(name)
        price = quote.price if quote is not None else None
        expected = expected_price(bands.get(name))
        row["price"] = _json_optional(price)
        row["expected_price"] = _json_optional(expected)
        row["gap_pct"] = _json_optional(gap_pct(price, expected))


def _screen_payload(session, run: ScreenRun) -> dict[str, object]:
    rows = session.scalars(
        select(ScreenResult)
        .where(ScreenResult.screen_run_id == run.id)
        .order_by(ScreenResult.id)
    ).all()
    keep = [_screen_row_json(row) for row in rows if row.verdict == "keep"]
    drop = [_screen_row_json(row) for row in rows if row.verdict == "drop"]
    insufficient = [_screen_row_json(row) for row in rows if row.verdict == "insufficient"]
    _attach_keep_gaps(session, run.id, keep)
    finalists = [row for row in keep if row.get("finalist")]
    return {
        "id": run.id,
        "status": run.status,
        "scope_type": run.scope_type,
        "scope_text": run.scope_text,
        "error": run.error,
        "keep": keep,
        "drop": drop,
        "insufficient": insufficient,
        "finalists": finalists,
        "jobs": _research_jobs_payload(session, run.id),
    }


def _research_jobs_payload(session, screen_run_id: int) -> list[dict[str, object]]:
    jobs = session.scalars(
        select(ResearchJob)
        .where(ResearchJob.screen_run_id == screen_run_id)
        .order_by(ResearchJob.id)
    ).all()
    return [_research_job_json(job) for job in jobs[:3]]


def _research_job_json(job: ResearchJob) -> dict[str, object]:
    return {
        "id": job.id,
        "name": job.name,
        "code": job.code,
        "status": job.status,
        "error": job.error,
        "screen_id": job.screen_run_id,
    }


def _artifact_payload(session, job: ResearchJob) -> dict[str, object]:
    artifact = session.scalars(
        select(ResearchArtifact)
        .where(ResearchArtifact.research_job_id == job.id)
        .order_by(ResearchArtifact.id.desc())
    ).first()
    structured: dict[str, object] = {}
    markdown = ""
    report_path = None
    if artifact is not None:
        markdown = artifact.markdown
        report_path = artifact.report_path
        try:
            parsed = json.loads(artifact.structured_json)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            structured = parsed
    body: dict[str, object] = {
        "id": job.id,
        "name": job.name,
        "code": job.code,
        "status": job.status,
        "error": job.error,
        "screen_id": job.screen_run_id,
        "discovery_run_id": job.discovery_run_id,
        "discovery_track": job.discovery_track,
        "structured": structured,
        "markdown": markdown,
        "report_path": report_path,
    }
    if structured:
        body["verdict"] = structured.get("verdict")
        body["gates"] = structured.get("gates")
        body["mirror_test"] = structured.get("mirror_test")
        body["price_bands"] = structured.get("price_bands")
        body["memo"] = structured.get("memo")
        body["data_grade"] = structured.get("data_grade")
        body["as_of"] = structured.get("as_of")
        body["portfolio_fit"] = structured.get("portfolio_fit")
        body["valuation"] = structured.get("valuation") or {}
    thesis = session.scalars(
        select(Thesis).where(Thesis.user_id == job.user_id, Thesis.name == job.name)
    ).first()
    quote = session.get(Quote, (job.user_id, job.name))
    usable = False
    current_price = None
    if quote is not None:
        current_price = quote.price
        usable = price_usable(quote.price, stale=quote.stale)
    body.update(
        api_fields(
            thesis,
            today=utc_today(),
            current_price=current_price,
            price_usable=usable,
        )
    )
    body["thesis_mark"] = thesis_mark(thesis, job.id)
    return body


def _quote_map(session, names: list[str]) -> dict[str, Quote]:
    wanted = [name for name in names if name]
    if not wanted:
        return {}
    rows = session.scalars(
        select(Quote).where(Quote.user_id == LOCAL_USER_ID, Quote.name.in_(wanted))
    ).all()
    return {row.name: row for row in rows}


def _watchlist_json(
    row: Watchlist,
    thesis: Thesis | None = None,
    quote: Quote | None = None,
) -> dict[str, object]:
    usable = False
    current_price = None
    if quote is not None:
        current_price = quote.price
        usable = price_usable(quote.price, stale=quote.stale)
    extra = api_fields(
        thesis,
        today=utc_today(),
        current_price=current_price,
        price_usable=usable,
    )
    return {
        "id": row.id,
        "name": row.name,
        "code": row.code,
        "research_id": row.research_id,
        "thesis_status": extra["thesis_status"],
        "thesis_research_id": extra["thesis_research_id"],
        "thesis_verdict": extra["thesis_verdict"],
    }


def _watchlist_gate(session, research_id: int):
    job = session.get(ResearchJob, research_id)
    if job is None:
        return None, _error_json(
            404,
            error="未找到",
            code="NOT_FOUND",
            detail=f"没有评估 {research_id}",
        )
    if job.status != "done":
        return None, _error_json(
            400,
            error="无法加入观察池",
            code="WATCHLIST_REJECTED",
            detail="评估尚未完成，不能加入观察池",
        )
    payload = _artifact_payload(session, job)
    verdict = payload.get("verdict")
    if verdict == "fail":
        return None, _error_json(
            400,
            error="无法加入观察池",
            code="WATCHLIST_REJECTED",
            detail="结论为不通过，不能经评估入口加入观察池",
        )
    if verdict not in {"pass", "gray"}:
        return None, _error_json(
            400,
            error="无法加入观察池",
            code="WATCHLIST_REJECTED",
            detail="评估没有结论，不能加入观察池",
        )
    return job, None


def _iso_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _job_payload(run: ScreenRun) -> dict[str, object]:
    return {
        "id": run.id,
        "type": JOB_TYPE_FUNNEL,
        "status": run.status,
        "error": run.error,
        "screen_id": run.id,
        "name": f"{run.scope_type} {run.scope_text}".strip(),
        "updated_at": _iso_time(run.updated_at or run.created_at),
    }


def _research_job_payload(job: ResearchJob) -> dict[str, object]:
    return {
        "id": job.id,
        "type": "research",
        "status": job.status,
        "error": job.error,
        "screen_id": job.screen_run_id,
        "name": job.name,
        "updated_at": _iso_time(job.updated_at or job.created_at),
    }


def _review_job_payload(row: PortfolioReview) -> dict[str, object]:
    return {
        "id": row.id,
        "type": JOB_TYPE_REVIEW,
        "status": row.status,
        "error": row.error,
        "updated_at": _iso_time(row.updated_at or row.created_at),
    }


def _discovery_job_payload(run: DiscoveryRun) -> dict[str, object]:
    return {
        "id": run.id,
        "type": JOB_TYPE_DISCOVERY,
        "status": run.status,
        "error": run.error,
        "name": run.week_key,
        "updated_at": _iso_time(run.updated_at or run.created_at),
    }


_JOB_TYPE_NAMES = frozenset(
    {JOB_TYPE_FUNNEL, JOB_TYPE_RESEARCH, JOB_TYPE_REVIEW, JOB_TYPE_DISCOVERY}
)


def _jobs_with_id(session, job_id: int) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    run = session.get(ScreenRun, job_id)
    if run is not None:
        found.append(_job_payload(run))
    job = session.get(ResearchJob, job_id)
    if job is not None:
        found.append(_research_job_payload(job))
    review = session.get(PortfolioReview, job_id)
    if review is not None:
        found.append(_review_job_payload(review))
    discovery = session.get(DiscoveryRun, job_id)
    if discovery is not None:
        found.append(_discovery_job_payload(discovery))
    return found


def _review_payload(row: PortfolioReview) -> dict[str, object]:
    structured: dict[str, object] = {}
    if row.structured_json:
        try:
            parsed = json.loads(row.structured_json)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            structured = parsed
    positions = structured.get("positions")
    actions = structured.get("portfolio_actions")
    return {
        "id": row.id,
        "type": JOB_TYPE_REVIEW,
        "status": row.status,
        "error": row.error,
        "positions": positions if isinstance(positions, list) else [],
        "portfolio_actions": actions if isinstance(actions, list) else [],
        "markdown": row.markdown or "",
        "report_path": row.report_path,
        "has_stale": bool(row.has_stale or structured.get("has_stale")),
    }


def _conflict_locked(session) -> JSONResponse:
    current = lock_status(session)
    job_type = (current or {}).get("type") or "unknown"
    return JSONResponse(
        {
            "error": "任务进行中",
            "code": "JOB_LOCKED",
            "detail": f"当前任务类型: {job_type}",
            "job_type": job_type,
        },
        status_code=409,
    )


def create_app(
    *,
    db_path: Path | None = None,
    dist_path: Path | None = None,
    yaml_dir: Path | str | None = None,
    quote_client: QuoteClient | None = None,
    market_client: MarketClient | None = None,
    env_path: Path | str | None = None,
    allow_sync: bool = False,
    llm_client=None,
    reports_dir: Path | str | None = None,
    discovery_track_executor=None,
    discovery_snapshot_provider=None,
    discovery_clock=None,
) -> FastAPI:
    resolved_dist = dist_path if dist_path is not None else DEFAULT_DIST_PATH
    resolved_quote_client = quote_client if quote_client is not None else TencentQuoteClient()
    resolved_market_client = market_client if market_client is not None else EastMoneyClient()
    resolved_env = resolve_env_path(env_path)
    load_env_key(resolved_env, OPENAI_API_KEY)
    load_env_key(resolved_env, OPENAI_BASE_URL)
    load_env_key(resolved_env, TAVILY_API_KEY)
    resolved_llm = llm_client if llm_client is not None else OpenAICompatClient()
    resolved_reports = (
        Path(reports_dir) if reports_dir is not None else PROJECT_ROOT / "reports"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = init_db(db_path)
        session_factory = create_session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.quote_client = resolved_quote_client
        app.state.market_client = resolved_market_client
        app.state.llm_client = resolved_llm
        app.state.reports_dir = resolved_reports
        app.state.env_path = resolved_env
        app.state.allow_sync = allow_sync
        app.state.discovery_track_executor = (
            discovery_track_executor or builtin_track_executor
        )
        app.state.discovery_snapshot_provider = (
            discovery_snapshot_provider or build_production_snapshot
        )
        app.state.discovery_clock = discovery_clock or (
            lambda: datetime.now(timezone.utc)
        )
        app.state.discovery_workers_lock = threading.Lock()
        app.state.discovery_workers = set()
        app.state.discovery_worker_leases = {}
        app.state.discovery_shutting_down = False
        load_env_key(resolved_env, OPENAI_API_KEY)
        load_env_key(resolved_env, OPENAI_BASE_URL)
        load_env_key(resolved_env, TAVILY_API_KEY)
        resolved_yaml = _resolve_yaml_dir(yaml_dir)
        app.state.yaml_dir = str(resolved_yaml) if resolved_yaml is not None else None
        app.state.migrate_error = None
        with session_factory() as session:
            try:
                migrate_yaml_portfolio(session, resolved_yaml)
            except Exception as exc:
                session.rollback()
                logger.exception("YAML 迁入失败")
                app.state.migrate_error = {
                    "error": "迁入失败",
                    "code": "MIGRATE_FAILED",
                    "detail": str(exc),
                }
            try:
                _bulk_refresh(session, resolved_quote_client)
            except Exception:
                session.rollback()
                logger.exception("启动刷新现价失败")
            try:
                rows, cash = load_positions(session)
                seed_targets_if_empty(session, rows, cash)
            except Exception:
                session.rollback()
                logger.exception("初始目标写入失败")
            try:
                clear_stale_lock(
                    session,
                    now_provider=app.state.discovery_clock,
                )
                clear_cheap_fill(session)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("启动清理任务锁失败")
        try:
            resume_discovery_queues(app)
        except Exception:
            logger.exception("启动恢复发现队列失败")
        yield
        with app.state.discovery_workers_lock:
            app.state.discovery_shutting_down = True
            discovery_workers = tuple(app.state.discovery_workers)
            discovery_leases = {
                worker: app.state.discovery_worker_leases.get(worker)
                for worker in discovery_workers
            }
        for worker in discovery_workers:
            join = getattr(worker, "join", None)
            if callable(join):
                join(timeout=0.1)
        alive = [
            worker
            for worker in discovery_workers
            if callable(getattr(worker, "is_alive", None))
            and worker.is_alive()
        ]
        if alive:
            with session_factory() as session:
                for worker in alive:
                    lease = discovery_leases.get(worker)
                    if lease is not None and len(lease) == 2:
                        run_id, owner_token = lease
                        release(
                            session,
                            run_id,
                            JOB_TYPE_DISCOVERY,
                            owner_token=owner_token,
                        )
                session.commit()
            for worker in alive:
                lease = discovery_leases.get(worker)
                if (
                    lease is not None
                    and len(lease) == 3
                    and lease[1] == JOB_TYPE_RESEARCH
                ):
                    job_id, _job_type, owner_token = lease
                    cancel_discovery_research_worker(
                        session_factory,
                        job_id,
                        owner_token,
                    )
        engine.dispose()

    app = FastAPI(title="本机工作台", lifespan=lifespan)
    app.include_router(create_discovery_router())

    @app.get("/api/health")
    def health(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            return {"status": "ok", "lock": lock_status(session)}

    @app.get("/api/sentiment")
    def sentiment(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            body = load_sentiment(session, request.app.state.market_client)
            session.commit()
            return body

    def _run_cheap_fill() -> None:
        factory = app.state.session_factory
        market = app.state.market_client
        with factory() as session:
            execute_cheap_fill(session, market)

    def _run_discovery_data_fill(fill_id: int) -> None:
        factory = app.state.session_factory
        market = app.state.market_client
        with factory() as session:
            execute_discovery_data_fill(
                session,
                market,
                fill_id,
                policy=DEFAULT_POLICY,
            )

    @app.get("/api/discovery-data/status")
    def get_discovery_data_status(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            return discovery_data_status(session, policy=DEFAULT_POLICY) | {
                "readiness": _readiness_json(
                    discovery_data_readiness(session, as_of=date.today())
                )
            }

    @app.post("/api/discovery-data/fill")
    async def post_discovery_data_fill(request: Request):
        body = await request.body()
        if not body.strip():
            payload = {}
        else:
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _error_json(
                    400,
                    error="请求无效",
                    code="INVALID_DISCOVERY_DATA_FILL",
                    detail="无法解析请求",
                )
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_DISCOVERY_DATA_FILL",
                detail="请求体必须是对象",
            )
        sync = _as_bool(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        factory = request.app.state.session_factory
        with factory() as session:
            fill, dispatch = start_discovery_data_fill(
                session,
                policy=DEFAULT_POLICY,
            )
            if fill is None:
                session.rollback()
                current = lock_status(session)
                if current and current.get("type") == JOB_TYPE_DISCOVERY_DATA:
                    active_id = int(current["job_id"])
                    return discovery_data_status(session, policy=DEFAULT_POLICY) | {
                        "id": active_id
                    }
                return _conflict_locked(session)
            fill_id = fill.id
        if dispatch:
            if sync:
                _run_discovery_data_fill(fill_id)
            else:
                threading.Thread(
                    target=_run_discovery_data_fill,
                    args=(fill_id,),
                    daemon=True,
                ).start()
        with factory() as session:
            return discovery_data_status(session, policy=DEFAULT_POLICY)

    @app.get("/api/cheap")
    def cheap(request: Request, sort: str = Query("pe")):
        wanted = (sort or "pe").strip().lower()
        if wanted not in {"pe", "pb"}:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_CHEAP",
                detail="sort 只能是 pe 或 pb",
            )
        fill = _as_bool(request.query_params.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        started = False
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                body = load_cheap_board(
                    session,
                    request.app.state.market_client,
                    sort=wanted,
                    fill=fill,
                )
            except MarketError as exc:
                return _error_json(
                    502,
                    error="行情失败",
                    code="CHEAP_UNAVAILABLE",
                    detail=exc.message,
                )
            pending = int(body.get("pending") or 0)
            filling = cheap_fill_running(session)
            if pending and not fill and not filling:
                if begin_cheap_fill(session):
                    started = True
                    filling = True
            session.commit()
            body["filling"] = filling
        if started:
            threading.Thread(target=_run_cheap_fill, daemon=True).start()
        return body

    @app.get("/api/positions")
    def positions(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            rows, cash = load_positions(session)
            body = _positions_payload(session, rows, cash)
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.post("/api/trades")
    async def create_trade(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_TRADE",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_TRADE",
                detail="请求体必须是对象",
            )
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                recorded = record_trade(
                    session,
                    date=payload.get("date"),
                    direction=payload.get("direction"),
                    name=payload.get("name"),
                    shares=payload.get("shares"),
                    price=payload.get("price"),
                    note=payload.get("note"),
                )
            except TradeError as exc:
                return _error_json(
                    400,
                    error=exc.error,
                    code=exc.code,
                    detail=exc.detail,
                )
            except Exception as exc:
                logger.exception("记账失败")
                return _error_json(
                    500,
                    error="记账失败",
                    code="TRADE_FAILED",
                    detail=str(exc),
                )
            try:
                rows, cash = _sync_targets_after_book(session)
            except Exception:
                logger.exception("记账后同步目标仓位失败")
                rows, cash = load_positions(session)
            body = _positions_payload(session, rows, cash)
        if recorded.warning:
            body["warning"] = recorded.warning
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.post("/api/positions/adjust")
    async def adjust_position(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_TRADE",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_TRADE",
                detail="请求体必须是对象",
            )
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                set_position(
                    session,
                    name=payload.get("name"),
                    shares=payload.get("shares"),
                    unit_cost=payload.get("unit_cost"),
                )
            except TradeError as exc:
                return _error_json(
                    400,
                    error=exc.error,
                    code=exc.code,
                    detail=exc.detail,
                )
            try:
                rows, cash = _sync_targets_after_book(session)
            except Exception:
                logger.exception("账面调整后同步目标仓位失败")
                rows, cash = load_positions(session)
            body = _positions_payload(session, rows, cash)
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.post("/api/positions/cash")
    async def adjust_cash(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_TRADE",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_TRADE",
                detail="请求体必须是对象",
            )
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                set_cash(session, balance=payload.get("balance"))
            except TradeError as exc:
                return _error_json(
                    400,
                    error=exc.error,
                    code=exc.code,
                    detail=exc.detail,
                )
            try:
                rows, cash = _sync_targets_after_book(session)
            except Exception:
                logger.exception("现金调整后同步目标仓位失败")
                rows, cash = load_positions(session)
            body = _positions_payload(session, rows, cash)
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.post("/api/quotes/refresh")
    async def refresh_quotes(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_QUOTE",
                detail="无法解析请求",
            )
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_QUOTE",
                detail="请求体必须是对象",
            )
        name = None
        if "name" in payload:
            raw_name = payload.get("name")
            text = "" if raw_name is None else str(raw_name).strip()
            if not text:
                return _error_json(
                    400,
                    error="请求无效",
                    code="INVALID_QUOTE",
                    detail="缺少字段: name",
                )
            name = text
        factory = request.app.state.session_factory
        client: QuoteClient = request.app.state.quote_client
        with factory() as session:
            rows, cash = load_positions(session)
            held = {row.name: row for row in rows}
            if name is not None and name not in held:
                return _error_json(
                    400,
                    error="请求无效",
                    code="INVALID_QUOTE",
                    detail=f"没有持仓: {name}",
                )
            mapping = instrument_code_by_name(session)
            if name is not None:
                code = mapping.get(name)
                if code is None or not is_a_share_code(code):
                    return _error_json(
                        400,
                        error="无法解析代码",
                        code="NO_CODE",
                        detail=f"持仓 {name} 没有有效的 .SH/.SZ/.BJ 代码",
                    )
            targets = (
                [(name, mapping.get(name))]
                if name is not None
                else [(row.name, mapping.get(row.name)) for row in rows]
            )
            codes = [
                code
                for _n, code in targets
                if code is not None and is_a_share_code(code)
            ]
            fetched = _fetch_prices(client, codes)
            try:
                errors = apply_refresh(
                    session,
                    fetched=fetched,
                    targets=targets,
                    single=name is not None,
                )
            except QuoteError as exc:
                return _error_json(
                    400,
                    error=exc.error,
                    code=exc.code,
                    detail=exc.detail,
                )
            except Exception as exc:
                logger.exception("刷新现价失败")
                return _error_json(
                    500,
                    error="刷新失败",
                    code="QUOTE_FAILED",
                    detail=str(exc),
                )
            rows, cash = _sync_targets_after_book(session)
            body = _positions_payload(session, rows, cash)
        if errors:
            body["errors"] = [
                {
                    "name": item.name,
                    "error": item.error,
                    "code": item.code,
                    "detail": item.detail,
                }
                for item in errors
            ]
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.post("/api/instruments")
    async def create_instrument(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_CODE",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_CODE",
                detail="请求体必须是对象",
            )
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                save_instrument(
                    session,
                    name=payload.get("name"),
                    code=payload.get("code"),
                )
            except QuoteError as exc:
                return _error_json(
                    400,
                    error=exc.error,
                    code=exc.code,
                    detail=exc.detail,
                )
            except Exception as exc:
                logger.exception("补代码失败")
                return _error_json(
                    500,
                    error="补代码失败",
                    code="INSTRUMENT_FAILED",
                    detail=str(exc),
                )
            rows, cash = load_positions(session)
            body = _positions_payload(session, rows, cash)
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.post("/api/quotes/manual")
    async def manual_quote(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_PRICE",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_PRICE",
                detail="请求体必须是对象",
            )
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                set_manual_price(
                    session,
                    name=payload.get("name"),
                    price=payload.get("price"),
                )
            except QuoteError as exc:
                return _error_json(
                    400,
                    error=exc.error,
                    code=exc.code,
                    detail=exc.detail,
                )
            except Exception as exc:
                logger.exception("手工价失败")
                return _error_json(
                    500,
                    error="手工价失败",
                    code="QUOTE_FAILED",
                    detail=str(exc),
                )
            rows, cash = _sync_targets_after_book(session)
            body = _positions_payload(session, rows, cash)
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.post("/api/targets")
    async def replace_targets(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_TARGETS",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_TARGETS",
                detail="请求体必须是对象",
            )
        raw = payload.get("targets", payload)
        if not isinstance(raw, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_TARGETS",
                detail="targets 必须是名称到权重的映射",
            )
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                save_targets(session, raw)
            except TargetError as exc:
                return _error_json(
                    400,
                    error=exc.error,
                    code=exc.code,
                    detail=exc.detail,
                )
            except Exception as exc:
                logger.exception("保存目标失败")
                return _error_json(
                    500,
                    error="保存目标失败",
                    code="TARGET_FAILED",
                    detail=str(exc),
                )
            rows, cash = load_positions(session)
            body = _positions_payload(session, rows, cash)
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.get("/api/rules")
    def rules(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            body = _rules_payload(session)
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.post("/api/rules")
    async def create_rule(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_RULE",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_RULE",
                detail="请求体必须是对象",
            )
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                save_rule(
                    session,
                    name=payload.get("name"),
                    rule_type=payload.get("type"),
                    price=payload.get("price"),
                    note=payload.get("note"),
                )
            except RuleError as exc:
                return _error_json(
                    400,
                    error=exc.error,
                    code=exc.code,
                    detail=exc.detail,
                )
            except Exception as exc:
                logger.exception("保存规则失败")
                return _error_json(
                    500,
                    error="保存规则失败",
                    code="RULE_FAILED",
                    detail=str(exc),
                )
            body = _rules_payload(session)
            rows, cash = load_positions(session)
            body.update(_positions_payload(session, rows, cash))
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.delete("/api/rules/{rule_id}")
    def remove_rule(request: Request, rule_id: int):
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                delete_rule(session, rule_id)
            except RuleError as exc:
                status = 404 if exc.code == "NOT_FOUND" else 400
                return _error_json(
                    status,
                    error=exc.error,
                    code=exc.code,
                    detail=exc.detail,
                )
            except Exception as exc:
                logger.exception("删除规则失败")
                return _error_json(
                    500,
                    error="删除规则失败",
                    code="RULE_FAILED",
                    detail=str(exc),
                )
            body = _rules_payload(session)
            rows, cash = load_positions(session)
            body.update(_positions_payload(session, rows, cash))
        migrate_error = request.app.state.migrate_error
        if migrate_error:
            body.update(migrate_error)
        return body

    @app.get("/api/settings")
    def settings(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            return _settings_body(request, session)

    @app.post("/api/settings")
    async def update_settings(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_SETTINGS",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_SETTINGS",
                detail="请求体必须是对象",
            )

        key_provided, api_key, key_error = _secret_from_payload(
            payload, "api_key", code="EMPTY_API_KEY", detail="密钥不能为空"
        )
        if key_error is not None:
            return key_error
        search_provided, search_key, search_error = _secret_from_payload(
            payload, "search_api_key", code="EMPTY_SEARCH_KEY", detail="搜索密钥不能为空"
        )
        if search_error is not None:
            return search_error

        url_provided = "base_url" in payload
        base_url = ""
        if url_provided:
            raw_url = payload.get("base_url")
            if raw_url is None or not isinstance(raw_url, str):
                return _error_json(
                    400,
                    error="请求无效",
                    code="INVALID_BASE_URL",
                    detail="接口地址无效",
                )
            base_url = raw_url.strip().rstrip("/")
            if not base_url or "\n" in base_url or "\r" in base_url:
                return _error_json(
                    400,
                    error="请求无效",
                    code="INVALID_BASE_URL",
                    detail="接口地址无效",
                )

        factory = request.app.state.session_factory
        env_file = request.app.state.env_path
        with factory() as session:
            if "model" in payload:
                raw_model = payload.get("model")
                model = "" if raw_model is None else str(raw_model).strip()
                session.merge(Setting(key=LLM_MODEL_KEY, value=model))
            if "web_search" in payload:
                session.merge(
                    Setting(
                        key=WEB_SEARCH_KEY,
                        value="true" if _truthy(payload.get("web_search")) else "false",
                    )
                )
            try:
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception("保存设置失败")
                return _error_json(
                    500,
                    error="保存设置失败",
                    code="SETTINGS_FAILED",
                    detail=str(exc),
                )
            try:
                if key_provided:
                    write_env_value(env_file, OPENAI_API_KEY, api_key)
                    hot_load(OPENAI_API_KEY, api_key)
                if search_provided:
                    write_env_value(env_file, TAVILY_API_KEY, search_key)
                    hot_load(TAVILY_API_KEY, search_key)
                if url_provided:
                    write_env_value(env_file, OPENAI_BASE_URL, base_url)
                    hot_load(OPENAI_BASE_URL, base_url)
            except Exception as exc:
                logger.exception("保存设置失败")
                return _error_json(
                    500,
                    error="保存设置失败",
                    code="SETTINGS_FAILED",
                    detail=str(exc),
                )
            body = _settings_body(request, session)
        if key_provided or search_provided or url_provided:
            body["message"] = "已热加载"
        else:
            body["message"] = "已保存"
        return body

    def _run_funnel_job(run_id: int, evaluate: bool) -> None:
        factory = app.state.session_factory
        market = app.state.market_client
        with factory() as session:
            execute_funnel(
                session,
                run_id,
                market,
                evaluate=evaluate,
                llm=app.state.llm_client,
                quote_client=app.state.quote_client,
                reports_dir=app.state.reports_dir,
            )

    def _run_research_job(job_id: int) -> None:
        factory = app.state.session_factory
        with factory() as session:
            execute_research(
                session,
                job_id,
                llm=app.state.llm_client,
                quote_client=app.state.quote_client,
                reports_dir=app.state.reports_dir,
                market=app.state.market_client,
            )

    def _run_review_job(review_id: int) -> None:
        factory = app.state.session_factory
        with factory() as session:
            execute_review(
                session,
                review_id,
                llm=app.state.llm_client,
                quote_client=app.state.quote_client,
                reports_dir=app.state.reports_dir,
                market=app.state.market_client,
            )

    @app.get("/api/ruler")
    def get_ruler(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            return ruler_status(session)

    @app.post("/api/ruler")
    async def post_ruler(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                activate_ruler(session, payload.get("reason"))
                session.commit()
            except RulerError as exc:
                return _error_json(400, error=exc.error, code=exc.code, detail=exc.detail)
            return ruler_status(session)

    @app.post("/api/screens/funnel")
    async def create_funnel(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_SCREEN",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_SCREEN",
                detail="请求体必须是对象",
            )
        scope_type = str(payload.get("scope_type") or "").strip()
        scope_text = str(payload.get("scope_text") or "").strip()
        if scope_type not in SCOPE_TYPES:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_SCREEN",
                detail="范围类型仅行业 / 指数 / 主题",
            )
        if not scope_text:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_SCREEN",
                detail="范围文本不能为空",
            )
        evaluate = _evaluate_flag(payload)
        if evaluate and not has_env_value(request.app.state.env_path, OPENAI_API_KEY):
            return _missing_key_error()
        sync = _as_bool(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        factory = request.app.state.session_factory
        with factory() as session:
            try:
                require_matched(session)
            except RulerError as exc:
                return _error_json(400, error=exc.error, code=exc.code, detail=exc.detail)
            now = datetime.now(timezone.utc)
            run = ScreenRun(
                user_id=LOCAL_USER_ID,
                scope_type=scope_type,
                scope_text=scope_text,
                status="queued",
                error=None,
                snapshot_json=None,
                created_at=now,
                updated_at=now,
            )
            session.add(run)
            session.flush()
            if not try_acquire(session, run.id, JOB_TYPE_FUNNEL):
                session.rollback()
                return _conflict_locked(session)
            session.commit()
            run_id = run.id
        if sync:
            _run_funnel_job(run_id, evaluate)
        else:
            threading.Thread(
                target=_run_funnel_job, args=(run_id, evaluate), daemon=True
            ).start()
        with factory() as session:
            run = session.get(ScreenRun, run_id)
            assert run is not None
            return {"id": run.id, "status": run.status, "type": JOB_TYPE_FUNNEL}

    @app.get("/api/screens/boards")
    def list_screen_boards(
        request: Request,
        scope_type: str = Query(""),
        q: str = Query(""),
    ):
        kind = scope_type.strip()
        if kind not in SCOPE_TYPES:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_SCREEN",
                detail="范围类型仅行业 / 指数 / 主题",
            )
        try:
            boards = request.app.state.market_client.list_boards(kind, q)
        except MarketError as exc:
            return _error_json(
                502,
                error="行情不可用",
                code="MARKET_UNAVAILABLE",
                detail=str(exc),
            )
        return {"boards": boards}

    @app.get("/api/screens/latest")
    def latest_screen(request: Request):
        factory = request.app.state.session_factory
        with factory() as session:
            run = session.scalars(select(ScreenRun).order_by(ScreenRun.id.desc())).first()
            if run is None:
                return _error_json(
                    404,
                    error="未找到",
                    code="NOT_FOUND",
                    detail="还没有选股任务",
                )
            return _screen_payload(session, run)

    @app.get("/api/screens/{screen_id}")
    def get_screen(request: Request, screen_id: int):
        factory = request.app.state.session_factory
        with factory() as session:
            run = session.get(ScreenRun, screen_id)
            if run is None:
                return _error_json(
                    404,
                    error="未找到",
                    code="NOT_FOUND",
                    detail=f"没有选股任务 {screen_id}",
                )
            return _screen_payload(session, run)

    @app.get("/api/jobs")
    def list_jobs(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            runs = session.scalars(select(ScreenRun).order_by(ScreenRun.id.desc())).all()
            research = session.scalars(
                select(ResearchJob).order_by(ResearchJob.id.desc())
            ).all()
            reviews = session.scalars(
                select(PortfolioReview).order_by(PortfolioReview.id.desc())
            ).all()
            discoveries = session.scalars(
                select(DiscoveryRun).order_by(DiscoveryRun.id.desc())
            ).all()
            listed = [_job_payload(run) for run in runs]
            listed.extend(_research_job_payload(job) for job in research)
            listed.extend(_review_job_payload(row) for row in reviews)
            listed.extend(_discovery_job_payload(run) for run in discoveries)
            listed.sort(
                key=lambda item: (str(item.get("updated_at") or ""), item["type"], item["id"]),
                reverse=True,
            )
            return {"jobs": listed}

    @app.get("/api/jobs/{job_id}")
    def get_job(
        request: Request,
        job_id: int,
        job_type: str | None = Query(default=None, alias="type"),
    ):
        wanted = (job_type or "").strip() or None
        if wanted is not None and wanted not in _JOB_TYPE_NAMES:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_JOB",
                detail="type 只能是 funnel、research、review 或 discovery",
            )
        factory = request.app.state.session_factory
        with factory() as session:
            found = _jobs_with_id(session, job_id)
            if wanted is not None:
                found = [item for item in found if item.get("type") == wanted]
            if not found:
                return _error_json(
                    404,
                    error="未找到",
                    code="NOT_FOUND",
                    detail=f"没有任务 {job_id}",
                )
            if len(found) > 1:
                types = [str(item.get("type")) for item in found]
                return _error_json(
                    409,
                    error="任务不唯一",
                    code="JOB_AMBIGUOUS",
                    detail=(
                        f"编号 {job_id} 同时存在 { '、'.join(types) }，"
                        "请带 type=funnel|research|review|discovery 查询"
                    ),
                    types=types,
                )
            return found[0]

    @app.post("/api/research")
    async def create_research(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_RESEARCH",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_RESEARCH",
                detail="请求体必须是对象",
            )
        if not has_env_value(request.app.state.env_path, OPENAI_API_KEY):
            return _missing_key_error()
        name = str(payload.get("name") or "").strip()
        code = str(payload.get("code") or "").strip()
        query = str(payload.get("query") or "").strip()
        if not name and not code and not query:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_RESEARCH",
                detail="名称或代码不能为空",
            )
        raw_screen = payload.get("screen_run_id")
        screen_run_id = None
        if raw_screen is not None and str(raw_screen).strip() != "":
            try:
                screen_run_id = int(raw_screen)
            except (TypeError, ValueError):
                return _error_json(
                    400,
                    error="请求无效",
                    code="INVALID_RESEARCH",
                    detail="screen_run_id 无效",
                )
        sync = _as_bool(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        factory = request.app.state.session_factory
        market = request.app.state.market_client
        with factory() as session:
            try:
                name, code = _resolve_research_target(session, market, name, code, query)
            except MarketError as exc:
                return _error_json(
                    400,
                    error="解析失败",
                    code="INVALID_RESEARCH",
                    detail=str(exc),
                )
            now = datetime.now(timezone.utc)
            job = ResearchJob(
                user_id=LOCAL_USER_ID,
                screen_run_id=screen_run_id,
                name=name,
                code=code,
                status="queued",
                error=None,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            if not try_acquire(session, job.id, JOB_TYPE_RESEARCH):
                session.rollback()
                return _conflict_locked(session)
            session.commit()
            job_id = job.id
        if sync:
            _run_research_job(job_id)
        else:
            threading.Thread(
                target=_run_research_job, args=(job_id,), daemon=True
            ).start()
        with factory() as session:
            job = session.get(ResearchJob, job_id)
            assert job is not None
            return {"id": job.id, "status": job.status, "type": JOB_TYPE_RESEARCH}

    @app.get("/api/research/{research_id}")
    def get_research(request: Request, research_id: int):
        factory = request.app.state.session_factory
        with factory() as session:
            job = session.get(ResearchJob, research_id)
            if job is None:
                return _error_json(
                    404,
                    error="未找到",
                    code="NOT_FOUND",
                    detail=f"没有评估 {research_id}",
                )
            return _artifact_payload(session, job)

    @app.get("/api/aftermath")
    def get_aftermath(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            names = list(
                {
                    job.name
                    for job in session.scalars(
                        select(ResearchJob).where(
                            ResearchJob.user_id == LOCAL_USER_ID,
                            ResearchJob.status == "done",
                        )
                    ).all()
                }
            )
            quotes = _quote_map(session, names)
            prices = {
                name: (quote.price, price_usable(quote.price, stale=quote.stale))
                for name, quote in quotes.items()
            }
            return {"items": list_aftermath(session, LOCAL_USER_ID, prices=prices)}

    @app.get("/api/watchlist")
    def list_watchlist(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            rows = session.scalars(
                select(Watchlist)
                .where(Watchlist.user_id == LOCAL_USER_ID)
                .order_by(Watchlist.id)
            ).all()
            names = [row.name for row in rows]
            theses = load_by_names(session, LOCAL_USER_ID, names)
            quotes = _quote_map(session, names)
            return {
                "items": [
                    _watchlist_json(row, theses.get(row.name), quotes.get(row.name))
                    for row in rows
                ]
            }

    @app.post("/api/watchlist")
    async def create_watchlist(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_WATCHLIST",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_WATCHLIST",
                detail="请求体必须是对象",
            )
        raw_id = payload.get("research_id")
        if isinstance(raw_id, bool) or raw_id is None:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_WATCHLIST",
                detail="research_id 无效",
            )
        try:
            research_id = int(raw_id)
        except (TypeError, ValueError):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_WATCHLIST",
                detail="research_id 无效",
            )
        factory = request.app.state.session_factory
        with factory() as session:
            job, rejected = _watchlist_gate(session, research_id)
            if rejected is not None:
                return rejected
            assert job is not None
            existing = session.scalars(
                select(Watchlist).where(
                    Watchlist.user_id == LOCAL_USER_ID,
                    Watchlist.name == job.name,
                )
            ).first()
            if existing is not None:
                return _error_json(
                    409,
                    error="已在观察池",
                    code="WATCHLIST_DUPLICATE",
                    detail=f"{job.name} 已在观察池",
                )
            row = Watchlist(
                user_id=LOCAL_USER_ID,
                name=job.name,
                code=job.code,
                research_id=job.id,
                created_at=datetime.now(timezone.utc),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                orig = str(getattr(exc, "orig", exc)).upper()
                if "UNIQUE" in orig:
                    return _error_json(
                        409,
                        error="已在观察池",
                        code="WATCHLIST_DUPLICATE",
                        detail=f"{job.name} 已在观察池",
                    )
                raise
            session.refresh(row)
            theses = load_by_names(session, LOCAL_USER_ID, [row.name])
            quotes = _quote_map(session, [row.name])
            return _watchlist_json(row, theses.get(row.name), quotes.get(row.name))

    @app.delete("/api/watchlist/{item_id}")
    def delete_watchlist(request: Request, item_id: int):
        factory = request.app.state.session_factory
        with factory() as session:
            row = session.scalars(
                select(Watchlist).where(
                    Watchlist.id == item_id,
                    Watchlist.user_id == LOCAL_USER_ID,
                )
            ).first()
            if row is None:
                return _error_json(
                    404,
                    error="未找到",
                    code="NOT_FOUND",
                    detail=f"没有观察池项 {item_id}",
                )
            session.delete(row)
            session.commit()
            return {"ok": True}

    @app.post("/api/reviews")
    async def create_review(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_REVIEW",
                detail="无法解析请求",
            )
        if not isinstance(payload, dict):
            return _error_json(
                400,
                error="请求无效",
                code="INVALID_REVIEW",
                detail="请求体必须是对象",
            )
        if not has_env_value(request.app.state.env_path, OPENAI_API_KEY):
            return _missing_key_error(detail="审视需要模型密钥，请到设置页配置")
        sync = _as_bool(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        factory = request.app.state.session_factory
        with factory() as session:
            now = datetime.now(timezone.utc)
            row = PortfolioReview(
                user_id=LOCAL_USER_ID,
                status="queued",
                error=None,
                structured_json=None,
                markdown=None,
                report_path=None,
                has_stale=False,
                created_at=now,
                updated_at=now,
            )
            depth = str(payload.get("depth") or "reuse").strip()
            if depth not in {"reuse", "eval_first"}:
                depth = "reuse"
            row.structured_json = json.dumps({"depth": depth}, ensure_ascii=False)
            session.add(row)
            session.flush()
            if not try_acquire(session, row.id, JOB_TYPE_REVIEW):
                session.rollback()
                return _conflict_locked(session)
            session.commit()
            review_id = row.id
        if sync:
            _run_review_job(review_id)
        else:
            threading.Thread(
                target=_run_review_job, args=(review_id,), daemon=True
            ).start()
        with factory() as session:
            row = session.get(PortfolioReview, review_id)
            if row is None:
                return _error_json(
                    404,
                    error="未找到",
                    code="NOT_FOUND",
                    detail=f"没有审视 {review_id}",
                )
            return {"id": row.id, "status": row.status, "type": JOB_TYPE_REVIEW}

    @app.get("/api/reviews")
    def list_reviews(request: Request) -> dict[str, object]:
        factory = request.app.state.session_factory
        with factory() as session:
            rows = session.scalars(
                select(PortfolioReview).order_by(PortfolioReview.id.desc())
            ).all()
            return {"reviews": [_review_payload(row) for row in rows]}

    @app.get("/api/reviews/latest")
    def latest_review(request: Request):
        factory = request.app.state.session_factory
        with factory() as session:
            row = session.scalars(
                select(PortfolioReview).order_by(PortfolioReview.id.desc())
            ).first()
            if row is None:
                return _error_json(
                    404,
                    error="未找到",
                    code="NOT_FOUND",
                    detail="还没有审视",
                )
            return _review_payload(row)

    @app.get("/api/reviews/{review_id}")
    def get_review(request: Request, review_id: int):
        factory = request.app.state.session_factory
        with factory() as session:
            row = session.get(PortfolioReview, review_id)
            if row is None:
                return _error_json(
                    404,
                    error="未找到",
                    code="NOT_FOUND",
                    detail=f"没有审视 {review_id}",
                )
            return _review_payload(row)

    def _spa_index() -> Path | None:
        index = resolved_dist / "index.html"
        if resolved_dist.is_dir() and index.is_file():
            return index
        return None

    @app.get("/{full_path:path}")
    def serve_workbench(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return _error_json(
                404,
                error="Not Found",
                code="NOT_FOUND",
                detail="未知接口",
            )

        index = _spa_index()
        if index is None:
            return _error_json(
                404,
                error="Not Found",
                code="NOT_FOUND",
                detail="前端尚未构建，请使用 Vite 开发服务或先生成 web/dist",
            )

        try:
            if full_path:
                candidate = (resolved_dist / full_path).resolve()
                try:
                    candidate.relative_to(resolved_dist.resolve())
                except ValueError:
                    return FileResponse(index)
                if candidate.is_file():
                    return FileResponse(candidate)
                if Path(full_path).suffix:
                    return _error_json(
                        404,
                        error="Not Found",
                        code="NOT_FOUND",
                        detail="静态资源不存在",
                    )
            return FileResponse(index)
        except OSError:
            return _error_json(
                404,
                error="Not Found",
                code="NOT_FOUND",
                detail="无法读取静态资源",
            )

    return app


app = create_app()
