from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from math import isfinite

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.discovery_models import (
    DiscoveryCommonResult,
    DiscoveryCommonStage,
    DiscoveryResult,
    DiscoveryRun,
    DiscoveryStage,
)
from discovery.aggregate import aggregate_run_status
from discovery.builds import BUILD_VERSION
from discovery.constants import CHINA_MARKET_TIMEZONE, TRACK_STAGES, TRACKS
from discovery.snapshot import (
    ProductionSnapshot,
    freeze_inputs,
)
from jobs.discovery import drain_run_queue
from jobs.lock import JOB_TYPE_DISCOVERY, lock_status, release, try_acquire
from ledger.positions import LOCAL_USER_ID

logger = logging.getLogger(__name__)
_TRACK_ORDER = ("quality_discount", "deep_value", "garp")
_STAGE_ORDER = {"track_gate": 0, "competition": 1, "adjudication": 2}
SnapshotProvider = Callable[..., ProductionSnapshot]


class CorruptDiscoveryResult(ValueError):
    pass


def _error(
    status: int, *, error: str, code: str, detail: str, **extra: object
) -> JSONResponse:
    body: dict[str, object] = {"error": error, "code": code, "detail": detail}
    body.update(extra)
    return JSONResponse(body, status_code=status)


async def _object_body(request: Request, code: str):
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    raw = await request.body()
    if not raw.strip():
        return {}, None
    try:
        payload = json.loads(raw, parse_constant=reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, _error(
            400,
            error="请求无效",
            code=code,
            detail="无法解析请求",
        )
    if not isinstance(payload, dict):
        return None, _error(
            400,
            error="请求无效",
            code=code,
            detail="请求体必须是对象",
        )
    return payload, None


def _truthy(value: object) -> bool:
    return value is True or str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _now(app) -> datetime:
    value = app.state.discovery_clock()
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("discovery clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _week_key(value: datetime) -> str:
    year, week, _ = value.astimezone(CHINA_MARKET_TIMEZONE).isocalendar()
    return f"{year}-W{week:02d}"


def _latest_common(session: Session, run_id: int):
    return session.scalars(
        select(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.run_id == run_id,
            DiscoveryCommonStage.superseded_at.is_(None),
        )
        .order_by(
            DiscoveryCommonStage.attempt.desc(),
            DiscoveryCommonStage.id.desc(),
        )
        .limit(1)
    ).first()


def _track_stages(session: Session, run_id: int) -> list[DiscoveryStage]:
    return session.scalars(
        select(DiscoveryStage)
        .where(
            DiscoveryStage.run_id == run_id,
            DiscoveryStage.superseded_at.is_(None),
        )
        .order_by(DiscoveryStage.id)
    ).all()


def _latest_track(
    session: Session, run_id: int, track: str, stage: str
) -> DiscoveryStage | None:
    return session.scalars(
        select(DiscoveryStage)
        .where(
            DiscoveryStage.run_id == run_id,
            DiscoveryStage.track == track,
            DiscoveryStage.stage == stage,
            DiscoveryStage.superseded_at.is_(None),
        )
        .order_by(
            DiscoveryStage.attempt.desc(),
            DiscoveryStage.id.desc(),
        )
        .limit(1)
    ).first()


def _workflow_states(
    session: Session, run: DiscoveryRun
) -> tuple[DiscoveryCommonStage | None, dict[str, DiscoveryStage | None]]:
    common = _latest_common(session, run.id)
    rows = _track_stages(session, run.id)
    current: dict[str, DiscoveryStage | None] = {}
    for track in _TRACK_ORDER:
        candidates = [row for row in rows if row.track == track]
        if not candidates:
            current[track] = None
            continue
        current[track] = max(
            candidates,
            key=lambda row: (_STAGE_ORDER.get(row.stage, -1), row.attempt, row.id),
        )
    return common, current


def _aggregate(session: Session, run: DiscoveryRun) -> str:
    if run.status == "abandoned":
        return "abandoned"
    common, tracks = _workflow_states(session, run)
    return aggregate_run_status(
        run.status,
        "queued" if common is None else common.status,
        {
            track: "queued" if row is None else row.status
            for track, row in tracks.items()
        },
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _run_payload(session: Session, run: DiscoveryRun) -> dict[str, object]:
    common, tracks = _workflow_states(session, run)
    return {
        "id": run.id,
        "user_id": run.user_id,
        "week_key": run.week_key,
        "current": run.current,
        "status": _aggregate(session, run),
        "stored_status": run.status,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "snapshot_at": _iso(run.snapshot_at),
        "data_version": run.data_version,
        "financial_version": run.financial_version,
        "valuation_version": run.valuation_version,
        "ruler_version_ids": {
            "quality_discount": run.quality_discount_ruler_version_id,
            "deep_value": run.deep_value_ruler_version_id,
            "garp": run.garp_ruler_version_id,
        },
        "algorithm_version": run.algorithm_version,
        "input_fingerprint": run.input_fingerprint,
        "error": run.error,
        "safety": (
            {"stage": "safety", "status": "queued", "created": False}
            if common is None
            else _common_stage_json(common) | {"created": True}
        ),
        "tracks": {
            track: (
                {
                    "track": track,
                    "stage": None if run.status == "abandoned" else "track_gate",
                    "status": (
                        None if run.status == "abandoned" else "queued"
                    ),
                    "created": False,
                }
                if row is None
                else _track_stage_json(row) | {"created": True}
            )
            for track, row in tracks.items()
        },
    }


def _common_stage_json(row: DiscoveryCommonStage) -> dict[str, object]:
    return {
        "id": row.id,
        "stage": "safety",
        "attempt": row.attempt,
        "status": row.status,
        "total_count": row.total_count,
        "pass_count": row.pass_count,
        "drop_count": row.drop_count,
        "insufficient_count": row.insufficient_count,
        "error": row.error,
        "failure_kind": row.failure_kind,
        "confirmed_at": _iso(row.confirmed_at),
        "superseded": row.superseded_at is not None,
        "superseded_at": _iso(row.superseded_at),
        "input_fingerprint": row.input_fingerprint,
        "algorithm_version": row.algorithm_version,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _track_stage_json(row: DiscoveryStage) -> dict[str, object]:
    return {
        "id": row.id,
        "track": row.track,
        "stage": row.stage,
        "attempt": row.attempt,
        "status": row.status,
        "total_count": row.total_count,
        "pass_count": row.pass_count,
        "drop_count": row.drop_count,
        "insufficient_count": row.insufficient_count,
        "error": row.error,
        "failure_kind": row.failure_kind,
        "confirmed_at": _iso(row.confirmed_at),
        "superseded": row.superseded_at is not None,
        "superseded_at": _iso(row.superseded_at),
        "input_fingerprint": row.input_fingerprint,
        "ruler_version_id": row.ruler_version_id,
        "algorithm_version": row.algorithm_version,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _metrics(text: str) -> Mapping[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def validate(value: object) -> None:
        if value is None or type(value) in {bool, int, str}:
            return
        if type(value) is float:
            if isfinite(value):
                return
            raise ValueError("non-finite JSON number")
        if type(value) is list:
            for item in value:
                validate(item)
            return
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("non-string JSON key")
            for item in value.values():
                validate(item)
            return
        raise ValueError("unsupported JSON value")

    try:
        value = json.loads(text, parse_constant=reject_constant)
        validate(value)
        if type(value) is not dict:
            raise ValueError("metrics must be an object")
        return value
    except (TypeError, ValueError) as error:
        raise CorruptDiscoveryResult(
            "persisted discovery metrics are corrupt"
        ) from error


def _result_json(row: object) -> dict[str, object]:
    return {
        "code": row.code,
        "verdict": row.verdict,
        "reason": row.reason,
        "metrics": dict(_metrics(row.metrics_json)),
        **({"rank": row.rank} if isinstance(row, DiscoveryResult) else {}),
        "created_at": _iso(row.created_at),
    }


def _partition(rows: list[object]) -> dict[str, list[dict[str, object]]]:
    return {
        verdict: [_result_json(row) for row in rows if row.verdict == verdict]
        for verdict in ("pass", "drop", "insufficient")
    }


def _common_payload(session: Session, run: DiscoveryRun):
    stages = session.scalars(
        select(DiscoveryCommonStage)
        .where(DiscoveryCommonStage.run_id == run.id)
        .order_by(DiscoveryCommonStage.attempt, DiscoveryCommonStage.id)
    ).all()
    if not stages:
        return None
    result_rows = session.scalars(
        select(DiscoveryCommonResult)
        .where(DiscoveryCommonResult.run_id == run.id)
        .order_by(
            DiscoveryCommonResult.attempt,
            DiscoveryCommonResult.code,
            DiscoveryCommonResult.id,
        )
    ).all()
    by_attempt: dict[int, list[DiscoveryCommonResult]] = {}
    for row in result_rows:
        by_attempt.setdefault(row.attempt, []).append(row)
    current = next(
        (row for row in reversed(stages) if row.superseded_at is None), None
    )
    if current is None:
        current = stages[-1]
    attempts = []
    for stage in stages:
        attempts.append(
            _common_stage_json(stage) | _partition(by_attempt.get(stage.attempt, []))
        )
    return {
        "run": _run_payload(session, run),
        "current": _common_stage_json(current),
        "attempts": attempts,
        **_partition(by_attempt.get(current.attempt, [])),
    }


def _track_payload(
    session: Session, run: DiscoveryRun, track: str, stage: str
):
    stages = session.scalars(
        select(DiscoveryStage)
        .where(
            DiscoveryStage.run_id == run.id,
            DiscoveryStage.track == track,
            DiscoveryStage.stage == stage,
        )
        .order_by(DiscoveryStage.attempt, DiscoveryStage.id)
    ).all()
    if not stages:
        return None
    result_rows = session.scalars(
        select(DiscoveryResult)
        .where(
            DiscoveryResult.run_id == run.id,
            DiscoveryResult.track == track,
            DiscoveryResult.stage == stage,
        )
        .order_by(
            DiscoveryResult.attempt,
            DiscoveryResult.rank,
            DiscoveryResult.code,
            DiscoveryResult.id,
        )
    ).all()
    by_attempt: dict[int, list[DiscoveryResult]] = {}
    for row in result_rows:
        by_attempt.setdefault(row.attempt, []).append(row)
    current = next(
        (row for row in reversed(stages) if row.superseded_at is None), None
    )
    if current is None:
        current = stages[-1]
    attempts = []
    for attempt in stages:
        attempts.append(
            _track_stage_json(attempt)
            | _partition(by_attempt.get(attempt.attempt, []))
        )
    return {
        "run": _run_payload(session, run),
        "current": _track_stage_json(current),
        "attempts": attempts,
        **_partition(by_attempt.get(current.attempt, [])),
    }


def _owned_run(session: Session, run_id: int) -> DiscoveryRun | None:
    return session.scalar(
        select(DiscoveryRun).where(
            DiscoveryRun.id == run_id,
            DiscoveryRun.user_id == LOCAL_USER_ID,
        )
    )


def _acquire_queued_work(session: Session, run: DiscoveryRun) -> bool:
    queued = session.scalar(
        select(func.count())
        .select_from(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.run_id == run.id,
            DiscoveryCommonStage.status == "queued",
            DiscoveryCommonStage.superseded_at.is_(None),
        )
    ) + session.scalar(
        select(func.count())
        .select_from(DiscoveryStage)
        .where(
            DiscoveryStage.run_id == run.id,
            DiscoveryStage.status == "queued",
            DiscoveryStage.superseded_at.is_(None),
        )
    )
    if not queued:
        return False
    if not try_acquire(session, run.id, JOB_TYPE_DISCOVERY):
        session.rollback()
        return False
    session.commit()
    return True


def _locked(session: Session) -> JSONResponse:
    current = lock_status(session)
    job_type = (current or {}).get("type") or "unknown"
    return _error(
        409,
        error="任务进行中",
        code="JOB_LOCKED",
        detail=f"当前任务类型: {job_type}",
        job_type=job_type,
    )


def _release_committed_lock(factory, run_id: int) -> None:
    with factory() as session:
        release(session, run_id, JOB_TYPE_DISCOVERY)
        session.commit()


def _drain(app, run_id: int) -> None:
    factory = app.state.session_factory
    with factory() as session:
        try:
            drain_run_queue(
                session,
                run_id,
                track_executor=app.state.discovery_track_executor,
                lock_already_held=True,
            )
        except Exception:
            logger.exception("发现队列执行失败")


def _start_discovery_worker(app, target, args: tuple[object, ...]) -> Exception | None:
    holder: list[threading.Thread] = []

    def run() -> None:
        try:
            target(*args)
        finally:
            with app.state.discovery_workers_lock:
                app.state.discovery_workers.discard(holder[0])

    with app.state.discovery_workers_lock:
        if app.state.discovery_shutting_down:
            return RuntimeError("application is shutting down")
        thread = threading.Thread(target=run, daemon=False)
        holder.append(thread)
        try:
            thread.start()
        except Exception as error:
            return error
        app.state.discovery_workers.add(thread)
    return None


def _dispatch(app, run_id: int, *, sync: bool) -> JSONResponse | None:
    if sync:
        _drain(app, run_id)
        return None
    error = _start_discovery_worker(app, _drain, (app, run_id))
    if error is None:
        return None
    logger.error("发现队列线程启动失败: %s", error)
    _release_committed_lock(app.state.session_factory, run_id)
    return _error(
        500,
        error="任务启动失败",
        code="DISCOVERY_DISPATCH_FAILED",
        detail=str(error),
    )


def _read_after_dispatch(app, run_id: int):
    with app.state.session_factory() as session:
        run = _owned_run(session, run_id)
        if run is None:
            return _error(
                404,
                error="未找到",
                code="DISCOVERY_NOT_FOUND",
                detail=f"没有发现批次 {run_id}",
            )
        return _run_payload(session, run)


def _new_track_stage(
    run: DiscoveryRun,
    *,
    track: str,
    stage: str,
    created_at: datetime,
) -> DiscoveryStage:
    return DiscoveryStage(
        run_id=run.id,
        track=track,
        stage=stage,
        attempt=1,
        status="queued",
        total_count=0,
        pass_count=0,
        drop_count=0,
        insufficient_count=0,
        error=None,
        input_fingerprint=run.input_fingerprint,
        ruler_version_id=getattr(run, f"{track}_ruler_version_id"),
        algorithm_version=run.algorithm_version,
        failure_kind=None,
        superseded_at=None,
        created_at=created_at,
        updated_at=created_at,
    )


def _confirm_common(
    session: Session, run: DiscoveryRun, now: datetime
) -> tuple[bool, JSONResponse | None]:
    current = _latest_common(session, run.id)
    if current is None:
        return False, _error(
            404,
            error="未找到",
            code="DISCOVERY_STAGE_NOT_FOUND",
            detail="没有共同排雷阶段",
        )
    if current.status == "confirmed":
        return False, None
    if current.status != "waiting_confirmation":
        return False, _error(
            409,
            error="阶段不可确认",
            code="DISCOVERY_STAGE_NOT_CONFIRMABLE",
            detail=f"共同排雷当前状态为 {current.status}",
        )
    if not try_acquire(session, run.id, JOB_TYPE_DISCOVERY):
        session.rollback()
        session.expire_all()
        refreshed = session.get(DiscoveryCommonStage, current.id)
        if refreshed is not None and refreshed.status == "confirmed":
            return False, None
        return False, _locked(session)
    result = session.execute(
        update(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.id == current.id,
            DiscoveryCommonStage.status == "waiting_confirmation",
            DiscoveryCommonStage.superseded_at.is_(None),
        )
        .values(
            status="confirmed",
            confirmed_at=now,
            updated_at=now,
        )
    )
    if int(result.rowcount or 0) != 1:
        session.rollback()
        return False, None
    session.add_all(
        _new_track_stage(run, track=track, stage="track_gate", created_at=now)
        for track in _TRACK_ORDER
    )
    run.status = "running"
    run.updated_at = now
    session.commit()
    return True, None


def _confirm_track(
    session: Session,
    run: DiscoveryRun,
    track: str,
    stage: str,
    now: datetime,
) -> tuple[bool, JSONResponse | None]:
    current = _latest_track(session, run.id, track, stage)
    if current is None:
        return False, _error(
            404,
            error="未找到",
            code="DISCOVERY_STAGE_NOT_FOUND",
            detail=f"没有 {track}/{stage} 阶段",
        )
    if current.status == "confirmed":
        return False, None
    if current.status != "waiting_confirmation":
        return False, _error(
            409,
            error="阶段不可确认",
            code="DISCOVERY_STAGE_NOT_CONFIRMABLE",
            detail=f"{track}/{stage} 当前状态为 {current.status}",
        )
    next_stage = {
        "track_gate": "competition",
        "competition": "adjudication",
    }.get(stage)
    if next_stage is None:
        return False, _error(
            409,
            error="阶段不可确认",
            code="DISCOVERY_CONFIRMATION_UNAVAILABLE",
            detail="最终裁决确认属于后续任务",
        )
    if not try_acquire(session, run.id, JOB_TYPE_DISCOVERY):
        session.rollback()
        session.expire_all()
        refreshed = session.get(DiscoveryStage, current.id)
        if refreshed is not None and refreshed.status == "confirmed":
            return False, None
        return False, _locked(session)
    result = session.execute(
        update(DiscoveryStage)
        .where(
            DiscoveryStage.id == current.id,
            DiscoveryStage.status == "waiting_confirmation",
            DiscoveryStage.superseded_at.is_(None),
        )
        .values(
            status="confirmed",
            confirmed_at=now,
            updated_at=now,
        )
    )
    if int(result.rowcount or 0) != 1:
        session.rollback()
        return False, None
    session.add(
        _new_track_stage(
            run,
            track=track,
            stage=next_stage,
            created_at=now,
        )
    )
    run.status = "running"
    run.updated_at = now
    session.commit()
    return True, None


def resume_discovery_queues(app) -> None:
    """Adopt each resumable queue once after stale-lock cleanup."""

    factory = app.state.session_factory
    with factory() as session:
        run_ids = list(
            session.scalars(
                select(DiscoveryRun.id)
                .where(
                    DiscoveryRun.user_id == LOCAL_USER_ID,
                    DiscoveryRun.current.is_(True),
                    DiscoveryRun.status != "abandoned",
                    (
                        select(func.count())
                        .select_from(DiscoveryCommonStage)
                        .where(
                            DiscoveryCommonStage.run_id == DiscoveryRun.id,
                            DiscoveryCommonStage.status == "queued",
                            DiscoveryCommonStage.superseded_at.is_(None),
                        )
                        .scalar_subquery()
                        + select(func.count())
                        .select_from(DiscoveryStage)
                        .where(
                            DiscoveryStage.run_id == DiscoveryRun.id,
                            DiscoveryStage.status == "queued",
                            DiscoveryStage.superseded_at.is_(None),
                        )
                        .scalar_subquery()
                        > 0
                    ),
                )
                .order_by(DiscoveryRun.id)
            )
        )
    if not run_ids:
        return

    def resume_one(run_id: int) -> bool:
        with factory() as session:
            if not try_acquire(session, run_id, JOB_TYPE_DISCOVERY):
                session.rollback()
                return False
            session.commit()
        _drain(app, run_id)
        return True

    if bool(getattr(app.state, "allow_sync", False)):
        for run_id in run_ids:
            if not resume_one(run_id):
                return
        return

    def resume_all() -> None:
        for run_id in run_ids:
            if not resume_one(run_id):
                return

    error = _start_discovery_worker(app, resume_all, ())
    if error is not None:
        logger.error("启动恢复发现队列线程失败: %s", error)


def create_discovery_router() -> APIRouter:
    router = APIRouter(prefix="/api/discoveries")

    @router.post("")
    async def create_discovery(request: Request):
        payload, rejected = await _object_body(request, "INVALID_DISCOVERY")
        if rejected is not None:
            return rejected
        sync = _truthy(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        now = _now(request.app)
        week_key = _week_key(now)
        factory = request.app.state.session_factory
        with factory() as session:
            existing = session.scalar(
                select(DiscoveryRun)
                .where(
                    DiscoveryRun.user_id == LOCAL_USER_ID,
                    DiscoveryRun.week_key == week_key,
                    DiscoveryRun.current.is_(True),
                )
                .order_by(DiscoveryRun.id.desc())
            )
            if existing is not None:
                dispatch = _acquire_queued_work(session, existing)
                existing_id = existing.id
                if dispatch:
                    failed = _dispatch(request.app, existing_id, sync=sync)
                    if failed is not None:
                        return failed
                    return _read_after_dispatch(request.app, existing_id)
                return _run_payload(session, existing)
            run = DiscoveryRun(
                user_id=LOCAL_USER_ID,
                week_key=week_key,
                current=True,
                status="queued",
                created_at=now,
                updated_at=now,
                snapshot_at=None,
                data_version="pending",
                financial_version="pending",
                valuation_version="pending",
                quality_discount_ruler_version_id=None,
                deep_value_ruler_version_id=None,
                garp_ruler_version_id=None,
                algorithm_version=BUILD_VERSION,
                trading_dates_json=None,
                input_fingerprint=None,
                error=None,
            )
            session.add(run)
            try:
                session.flush()
                if not try_acquire(session, run.id, JOB_TYPE_DISCOVERY):
                    session.rollback()
                    return _locked(session)
                provider: SnapshotProvider = request.app.state.discovery_snapshot_provider
                frozen = provider(
                    session,
                    algorithm_version=BUILD_VERSION,
                    expected_lock_job_id=run.id,
                    snapshot_at=now,
                )
                versions = frozen.versions
                run.data_version = versions.data_version
                run.financial_version = versions.financial_version
                run.valuation_version = versions.valuation_version
                run.quality_discount_ruler_version_id = (
                    versions.ruler_version_ids["quality_discount"]
                )
                run.deep_value_ruler_version_id = (
                    versions.ruler_version_ids["deep_value"]
                )
                run.garp_ruler_version_id = versions.ruler_version_ids["garp"]
                run.algorithm_version = versions.algorithm_version
                freeze_inputs(
                    session,
                    run,
                    frozen.companies,
                    versions,
                    frozen.trading_dates,
                    snapshot_at=frozen.snapshot_at,
                )
                session.add(
                    DiscoveryCommonStage(
                        run_id=run.id,
                        attempt=1,
                        status="queued",
                        total_count=0,
                        pass_count=0,
                        drop_count=0,
                        insufficient_count=0,
                        error=None,
                        input_fingerprint=run.input_fingerprint,
                        algorithm_version=run.algorithm_version,
                        failure_kind=None,
                        superseded_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.commit()
                run_id = run.id
            except (IntegrityError, OperationalError):
                session.rollback()
                winner = session.scalar(
                    select(DiscoveryRun)
                    .where(
                        DiscoveryRun.user_id == LOCAL_USER_ID,
                        DiscoveryRun.week_key == week_key,
                        DiscoveryRun.current.is_(True),
                    )
                    .order_by(DiscoveryRun.id.desc())
                )
                if winner is not None:
                    dispatch = _acquire_queued_work(session, winner)
                    winner_id = winner.id
                    if dispatch:
                        failed = _dispatch(request.app, winner_id, sync=sync)
                        if failed is not None:
                            return failed
                        return _read_after_dispatch(request.app, winner_id)
                    return _run_payload(session, winner)
                return _error(
                    409,
                    error="创建冲突",
                    code="DISCOVERY_CREATE_CONFLICT",
                    detail="本周发现批次正在创建，请重试",
                )
            except (TypeError, ValueError, KeyError) as error:
                session.rollback()
                return _error(
                    409,
                    error="数据未就绪",
                    code="DATA_NOT_READY",
                    detail=str(error),
                )
        dispatch_error = _dispatch(request.app, run_id, sync=sync)
        if dispatch_error is not None:
            return dispatch_error
        return _read_after_dispatch(request.app, run_id)

    @router.get("/current")
    def current_discovery(request: Request):
        week_key = _week_key(_now(request.app))
        with request.app.state.session_factory() as session:
            run = session.scalar(
                select(DiscoveryRun)
                .where(
                    DiscoveryRun.user_id == LOCAL_USER_ID,
                    DiscoveryRun.week_key == week_key,
                    DiscoveryRun.current.is_(True),
                )
                .order_by(DiscoveryRun.id.desc())
            )
            if run is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_NOT_FOUND",
                    detail="本周尚未创建发现批次",
                )
            return _run_payload(session, run)

    @router.get("/{run_id}/stages/safety")
    def get_safety(request: Request, run_id: int):
        with request.app.state.session_factory() as session:
            run = _owned_run(session, run_id)
            if run is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_NOT_FOUND",
                    detail=f"没有发现批次 {run_id}",
                )
            try:
                body = _common_payload(session, run)
            except CorruptDiscoveryResult:
                return _error(
                    500,
                    error="结果损坏",
                    code="DISCOVERY_RESULT_CORRUPT",
                    detail="持久化阶段指标无法安全读取",
                )
            if body is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_STAGE_NOT_FOUND",
                    detail="没有共同排雷阶段",
                )
            return body

    @router.post("/{run_id}/stages/safety/confirm")
    async def confirm_safety(request: Request, run_id: int):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_CONFIRMATION"
        )
        if rejected is not None:
            return rejected
        sync = _truthy(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        factory = request.app.state.session_factory
        with factory() as session:
            run = _owned_run(session, run_id)
            if run is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_NOT_FOUND",
                    detail=f"没有发现批次 {run_id}",
                )
            if not run.current or run.status == "abandoned":
                return _error(
                    409,
                    error="批次不可修改",
                    code="DISCOVERY_RUN_IMMUTABLE",
                    detail="历史或已废弃批次不可确认",
                )
            dispatch, failed = _confirm_common(session, run, _now(request.app))
            if failed is not None:
                return failed
            if not dispatch:
                dispatch = _acquire_queued_work(session, run)
        if dispatch:
            failed = _dispatch(request.app, run_id, sync=sync)
            if failed is not None:
                return failed
        return _read_after_dispatch(request.app, run_id)

    @router.get("/{run_id}/tracks/{track}/stages/{stage}")
    def get_track_stage(
        request: Request, run_id: int, track: str, stage: str
    ):
        if track not in TRACKS or stage not in TRACK_STAGES:
            return _error(
                404,
                error="未找到",
                code="DISCOVERY_STAGE_NOT_FOUND",
                detail="未知轨道或阶段",
            )
        with request.app.state.session_factory() as session:
            run = _owned_run(session, run_id)
            if run is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_NOT_FOUND",
                    detail=f"没有发现批次 {run_id}",
                )
            try:
                body = _track_payload(session, run, track, stage)
            except CorruptDiscoveryResult:
                return _error(
                    500,
                    error="结果损坏",
                    code="DISCOVERY_RESULT_CORRUPT",
                    detail="持久化阶段指标无法安全读取",
                )
            if body is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_STAGE_NOT_FOUND",
                    detail=f"没有 {track}/{stage} 阶段",
                )
            return body

    @router.post("/{run_id}/tracks/{track}/stages/{stage}/confirm")
    async def confirm_track_stage(
        request: Request, run_id: int, track: str, stage: str
    ):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_CONFIRMATION"
        )
        if rejected is not None:
            return rejected
        if track not in TRACKS or stage not in TRACK_STAGES:
            return _error(
                404,
                error="未找到",
                code="DISCOVERY_STAGE_NOT_FOUND",
                detail="未知轨道或阶段",
            )
        sync = _truthy(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        factory = request.app.state.session_factory
        with factory() as session:
            run = _owned_run(session, run_id)
            if run is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_NOT_FOUND",
                    detail=f"没有发现批次 {run_id}",
                )
            if not run.current or run.status == "abandoned":
                return _error(
                    409,
                    error="批次不可修改",
                    code="DISCOVERY_RUN_IMMUTABLE",
                    detail="历史或已废弃批次不可确认",
                )
            dispatch, failed = _confirm_track(
                session, run, track, stage, _now(request.app)
            )
            if failed is not None:
                return failed
            if not dispatch:
                dispatch = _acquire_queued_work(session, run)
        if dispatch:
            failed = _dispatch(request.app, run_id, sync=sync)
            if failed is not None:
                return failed
        return _read_after_dispatch(request.app, run_id)

    @router.post("/{run_id}/abandon")
    async def abandon_discovery(request: Request, run_id: int):
        _payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_ABANDON"
        )
        if rejected is not None:
            return rejected
        factory = request.app.state.session_factory
        with factory() as session:
            run = _owned_run(session, run_id)
            if run is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_NOT_FOUND",
                    detail=f"没有发现批次 {run_id}",
                )
            if run.status == "abandoned" or not run.current:
                return _run_payload(session, run)
            if not try_acquire(session, run.id, JOB_TYPE_DISCOVERY):
                session.rollback()
                return _locked(session)
            now = _now(request.app)
            changed = session.execute(
                update(DiscoveryRun)
                .where(
                    DiscoveryRun.id == run.id,
                    DiscoveryRun.current.is_(True),
                    DiscoveryRun.status != "abandoned",
                )
                .values(
                    current=False,
                    status="abandoned",
                    updated_at=now,
                )
            )
            if int(changed.rowcount or 0) != 1:
                session.rollback()
                session.expire_all()
                return _run_payload(session, run)
            session.commit()
        _release_committed_lock(factory, run_id)
        return _read_after_dispatch(request.app, run_id)

    return router

