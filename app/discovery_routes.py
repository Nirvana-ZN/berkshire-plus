from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from math import isfinite
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.discovery_models import (
    DiscoveryCommonResult,
    DiscoveryCommonStage,
    DiscoveryDecision,
    DiscoveryEvidence,
    DiscoveryInputSnapshot,
    DiscoveryOverride,
    DiscoveryResult,
    DiscoveryRun,
    DiscoveryStage,
)
from app.envfile import OPENAI_API_KEY, has_env_value
from app.models import JobLock, ResearchJob, RulerVersion
from discovery.aggregate import aggregate_run_status
from discovery.builds import AVAILABLE_BUILDS, BUILD_VERSION, _is_build_available
from discovery.constants import (
    TRACK_STAGES,
    TRACKS,
    aware_utc_now,
    china_week_key,
)
from discovery.diff import CorruptDiscoveryHistory, load_weekly_changes
from discovery.overrides import OVERRIDE_ACTIONS
from discovery.summaries import DiscoverySummaryError, build_stage_summary
from discovery.snapshot import (
    ProductionSnapshot,
    canonical_json,
    freeze_discovery_ruler_payloads,
    freeze_inputs,
    load_frozen_inputs,
)
from jobs.discovery import drain_run_queue
from jobs.lock import (
    ACTIVE_STATUSES,
    JOB_TYPE_DISCOVERY,
    JOB_TYPE_RESEARCH,
    LOCK_ROW_ID,
    acquire_discovery,
    ensure_lock_row,
    lock_status,
    release,
)
from jobs.research import create_research_job, execute_research
from ledger.positions import LOCAL_USER_ID

logger = logging.getLogger(__name__)
_TRACK_ORDER = ("quality_discount", "deep_value", "garp")
_STAGE_ORDER = {"track_gate": 0, "competition": 1, "adjudication": 2}
_DISCOVERY_RESEARCH_RESERVATION = "discovery_research_accept"
_DISCOVERY_RESEARCH_WEEKLY_QUOTA = 3
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
    return aware_utc_now(app.state.discovery_clock)


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


def _common_retry_target(
    session: Session, row: DiscoveryCommonStage | None
) -> DiscoveryCommonStage | None:
    if row is None:
        return None
    if row.status == "failed":
        return row
    origin = (
        session.get(DiscoveryCommonStage, row.retry_of_stage_id)
        if row.retry_of_stage_id is not None
        else None
    )
    return (
        origin
        if origin is not None
        and origin.run_id == row.run_id
        and origin.status == "failed"
        else None
    )


def _track_retry_target(
    session: Session, row: DiscoveryStage | None
) -> DiscoveryStage | None:
    if row is None:
        return None
    if row.status == "failed":
        return row
    origin = (
        session.get(DiscoveryStage, row.retry_of_stage_id)
        if row.retry_of_stage_id is not None
        else None
    )
    return (
        origin
        if origin is not None
        and origin.run_id == row.run_id
        and origin.track == row.track
        and origin.stage == row.stage
        and origin.status == "failed"
        else None
    )


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
        "ruler_readiness": {
            track: (
                "ready"
                if getattr(run, f"{track}_ruler_version_id") is not None
                else "ruler_not_ready"
            )
            for track in _TRACK_ORDER
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
        "retry_of_stage_id": row.retry_of_stage_id,
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
        "common_stage_id": row.common_stage_id,
        "predecessor_stage_id": row.predecessor_stage_id,
        "predecessor_stage": row.predecessor_stage,
        "retry_of_stage_id": row.retry_of_stage_id,
        "reopen_of_stage_id": row.reopen_of_stage_id,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _persisted_json(text: str) -> object:
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
        return value
    except (TypeError, ValueError) as error:
        raise CorruptDiscoveryResult(
            "persisted discovery JSON is corrupt"
        ) from error


def _metrics(text: str) -> Mapping[str, object]:
    value = _persisted_json(text)
    if type(value) is not dict:
        raise CorruptDiscoveryResult(
            "persisted discovery metrics are corrupt"
        )
    return value


def _override_json(row: DiscoveryOverride) -> dict[str, object]:
    return {
        "id": row.id,
        "track": row.track,
        "stage": row.stage,
        "attempt": row.attempt,
        "code": row.code,
        "action": row.action,
        "reason": row.reason,
        "created_at": _iso(row.created_at),
    }


def _decision_json(row: DiscoveryDecision) -> dict[str, object]:
    return {
        "id": row.id,
        "track": row.track,
        "competition_stage_id": row.competition_stage_id,
        "competition_attempt": row.competition_attempt,
        "code": row.code,
        "decision": row.decision,
        "exclusion_reason": row.exclusion_reason,
        "keep_note": row.keep_note,
        "created_at": _iso(row.created_at),
    }


def _evidence_json(row: DiscoveryEvidence) -> dict[str, object]:
    return {
        "id": row.id,
        "track": row.track,
        "competition_stage_id": row.competition_stage_id,
        "competition_attempt": row.competition_attempt,
        "code": row.code,
        "ruler_version_id": row.ruler_version_id,
        "rule_version": row.rule_version,
        "kind": row.kind,
        "rule_id": row.rule_id,
        "input_fields": _persisted_json(row.input_fields_json),
        "display_text": row.display_text,
        "as_of": row.as_of,
        "data_completeness": row.data_completeness,
        "created_at": _iso(row.created_at),
    }


def _result_json(
    row: object,
    name: str,
    override: DiscoveryOverride | None = None,
) -> dict[str, object]:
    verdict = (
        row.verdict
        if override is None
        else ("pass" if override.action == "forced_pass" else "drop")
    )
    return {
        "code": row.code,
        "name": name,
        "verdict": verdict,
        "original_verdict": row.verdict,
        "reason": row.reason,
        "metrics": dict(_metrics(row.metrics_json)),
        "override": None if override is None else _override_json(override),
        **({"rank": row.rank} if isinstance(row, DiscoveryResult) else {}),
        "created_at": _iso(row.created_at),
    }


def _frozen_name(names: Mapping[str, str], code: str) -> str:
    name = names.get(code)
    if type(name) is not str or not name.strip():
        raise CorruptDiscoveryResult(
            "persisted discovery result has no frozen company name"
        )
    return name


def _stage_changes(
    session: Session,
    run: DiscoveryRun,
    track: str,
    stage: str,
):
    try:
        return load_weekly_changes(
            session,
            run.id,
            track=track,
            stage=stage,
        )
    except CorruptDiscoveryHistory as error:
        raise CorruptDiscoveryResult(
            "persisted discovery history is corrupt"
        ) from error


def _compact_changes(session: Session, run: DiscoveryRun) -> list[dict[str, object]]:
    values = []
    for track in _TRACK_ORDER:
        stages = [
            row
            for row in _track_stages(session, run.id)
            if row.track == track
        ]
        if not stages:
            continue
        current = max(
            stages,
            key=lambda row: (
                _STAGE_ORDER.get(row.stage, -1),
                row.attempt,
                row.id,
            ),
        )
        changes = _stage_changes(session, run, track, current.stage)
        values.extend(
            change.as_dict(compact=True) for change in changes[:10]
        )
    return sorted(
        values,
        key=lambda row: (
            _TRACK_ORDER.index(str(row["track"])),
            _STAGE_ORDER.get(str(row["stage"]), -1),
            str(row["code"]),
        ),
    )


def _partition(
    rows: list[object],
    names: Mapping[str, str],
    overrides: Mapping[str, DiscoveryOverride] | None = None,
) -> dict[str, list[dict[str, object]]]:
    override_rows = overrides or {}
    return {
        verdict: [
            _result_json(
                row,
                _frozen_name(names, row.code),
                override_rows.get(row.code),
            )
            for row in rows
            if (
                row.verdict
                if row.code not in override_rows
                else (
                    "pass"
                    if override_rows[row.code].action == "forced_pass"
                    else "drop"
                )
            )
            == verdict
        ]
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
    names = dict(
        session.execute(
            select(DiscoveryInputSnapshot.code, DiscoveryInputSnapshot.name)
            .where(DiscoveryInputSnapshot.run_id == run.id)
        ).all()
    )
    current = next(
        (row for row in reversed(stages) if row.superseded_at is None), None
    )
    if current is None:
        current = stages[-1]
    attempts = []
    summaries: dict[int, dict[str, object]] = {}
    for stage in stages:
        rows = by_attempt.get(stage.attempt, [])
        summary = build_stage_summary(stage, rows)
        summaries[stage.id] = summary
        attempts.append(
            _common_stage_json(stage)
            | _partition(rows, names)
            | {"summary": summary}
        )
    current_rows = by_attempt.get(current.attempt, [])
    current_summary = summaries[current.id]
    return {
        "run": _run_payload(session, run),
        "current": _common_stage_json(current) | {"summary": current_summary},
        "attempts": attempts,
        **_partition(current_rows, names),
        "summary": current_summary,
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
    names = dict(
        session.execute(
            select(DiscoveryInputSnapshot.code, DiscoveryInputSnapshot.name)
            .where(DiscoveryInputSnapshot.run_id == run.id)
        ).all()
    )
    override_rows = session.scalars(
        select(DiscoveryOverride)
        .where(
            DiscoveryOverride.run_id == run.id,
            DiscoveryOverride.track == track,
            DiscoveryOverride.stage == stage,
        )
        .order_by(
            DiscoveryOverride.attempt,
            DiscoveryOverride.code,
            DiscoveryOverride.id,
        )
    ).all()
    override_rows_by_attempt: dict[int, list[DiscoveryOverride]] = {}
    for row in override_rows:
        override_rows_by_attempt.setdefault(row.attempt, []).append(row)
    ruler_ids = {
        row.ruler_version_id
        for row in stages
        if row.ruler_version_id is not None
    }
    rulers_by_id = (
        {
            row.id: row
            for row in session.scalars(
                select(RulerVersion).where(RulerVersion.id.in_(ruler_ids))
            ).all()
        }
        if ruler_ids
        else {}
    )
    decision_rows = session.scalars(
        select(DiscoveryDecision)
        .where(
            DiscoveryDecision.run_id == run.id,
            DiscoveryDecision.track == track,
        )
        .order_by(
            DiscoveryDecision.competition_attempt,
            DiscoveryDecision.code,
            DiscoveryDecision.id,
        )
    ).all()
    evidence_rows = session.scalars(
        select(DiscoveryEvidence)
        .where(
            DiscoveryEvidence.run_id == run.id,
            DiscoveryEvidence.track == track,
        )
        .order_by(
            DiscoveryEvidence.competition_attempt,
            DiscoveryEvidence.code,
            DiscoveryEvidence.kind,
            DiscoveryEvidence.rule_id,
            DiscoveryEvidence.id,
        )
    ).all()
    current = next(
        (row for row in reversed(stages) if row.superseded_at is None), None
    )
    if current is None:
        current = stages[-1]
    attempts = []
    summaries: dict[int, dict[str, object]] = {}
    for attempt in stages:
        rows = by_attempt.get(attempt.attempt, [])
        attempt_override_rows = override_rows_by_attempt.get(
            attempt.attempt, []
        )
        attempt_overrides = {
            row.code: row for row in attempt_override_rows
        }
        summary = build_stage_summary(
            attempt,
            rows,
            attempt_override_rows,
            rulers_by_id.get(attempt.ruler_version_id),
        )
        summaries[attempt.id] = summary
        attempts.append(
            _track_stage_json(attempt)
            | _partition(rows, names, attempt_overrides)
            | {
                "overrides": [
                    _override_json(row) for row in attempt_override_rows
                ],
                "summary": summary,
            }
        )
    current_override_rows = override_rows_by_attempt.get(current.attempt, [])
    current_overrides = {row.code: row for row in current_override_rows}
    current_rows = by_attempt.get(current.attempt, [])
    current_summary = summaries[current.id]
    changes = _stage_changes(session, run, track, stage)[:100]
    return {
        "run": _run_payload(session, run),
        "current": _track_stage_json(current) | {"summary": current_summary},
        "attempts": attempts,
        **_partition(current_rows, names, current_overrides),
        "overrides": [
            _override_json(row) for row in current_override_rows
        ],
        "decisions": [_decision_json(row) for row in decision_rows],
        "evidence": [_evidence_json(row) for row in evidence_rows],
        "summary": current_summary,
        "changes": [change.as_dict() for change in changes],
    }


def _owned_run(session: Session, run_id: int) -> DiscoveryRun | None:
    run = session.get(DiscoveryRun, run_id)
    return run if run is not None and run.user_id == LOCAL_USER_ID else None


def _is_mutable_run(run: DiscoveryRun, now: datetime) -> bool:
    return (
        run.status != "abandoned"
        and run.current is True
        and run.week_key == china_week_key(now)
    )


def _mutation_guard(
    run: DiscoveryRun, now: datetime
) -> JSONResponse | None:
    if _is_mutable_run(run, now):
        return None
    if run.week_key == china_week_key(now):
        return _error(
            409,
            error="批次不可修改",
            code="DISCOVERY_RUN_IMMUTABLE",
            detail="历史或已废弃批次不可修改",
        )
    return _error(
        409,
        error="历史批次不可修改",
        code="historical_run_immutable",
        detail="仅当前中国自然周的当前未废弃批次可修改",
    )


def _acquire_queued_work(
    app,
    session: Session,
    run: DiscoveryRun,
) -> tuple[str | None, JSONResponse | None]:
    now = _now(app)
    if not _is_mutable_run(run, now):
        return None, _mutation_guard(run, now)
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
        return None, None
    owner_token = acquire_discovery(session, run.id)
    if owner_token is None:
        session.rollback()
        return None, None
    _run, _commit_at, rejection = _fresh_mutation_checkpoint(
        app,
        session,
        run.id,
        owner_token=owner_token,
    )
    if rejection is not None:
        return None, rejection
    session.commit()
    return owner_token, None


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


def _release_committed_lock(
    factory, run_id: int, owner_token: str
) -> None:
    with factory() as session:
        release(
            session,
            run_id,
            JOB_TYPE_DISCOVERY,
            owner_token=owner_token,
        )
        session.commit()


def _fresh_mutation_checkpoint(
    app,
    session: Session,
    run_id: int,
    *,
    owner_token: str | None = None,
) -> tuple[DiscoveryRun | None, datetime, JSONResponse | None]:
    """Reauthorize a route mutation with a fresh app-clock reading."""

    session.expire_all()
    run = _owned_run(session, run_id)
    commit_at = _now(app)
    rejection = (
        _error(
            404,
            error="未找到",
            code="DISCOVERY_NOT_FOUND",
            detail=f"没有发现批次 {run_id}",
        )
        if run is None
        else _mutation_guard(run, commit_at)
    )
    if rejection is not None:
        session.rollback()
        if owner_token is not None:
            _release_committed_lock(
                app.state.session_factory,
                run_id,
                owner_token,
            )
    return run, commit_at, rejection


def _drain(app, run_id: int, owner_token: str) -> None:
    factory = app.state.session_factory
    with factory() as session:
        try:
            drain_run_queue(
                session,
                run_id,
                track_executor=app.state.discovery_track_executor,
                lock_already_held=True,
                owner_token=owner_token,
                now_provider=app.state.discovery_clock,
            )
        except Exception:
            logger.exception("发现队列执行失败")


def _start_discovery_worker(
    app,
    target,
    args: tuple[object, ...],
    *,
    lease: tuple[int, str] | tuple[int, str, str] | None = None,
) -> Exception | None:
    if (
        lease is None
        and target is _drain
        and len(args) == 3
        and type(args[1]) is int
        and type(args[2]) is str
    ):
        lease = (args[1], args[2])
    holder: list[threading.Thread] = []

    def run() -> None:
        try:
            target(*args)
        finally:
            with app.state.discovery_workers_lock:
                app.state.discovery_workers.discard(holder[0])
                app.state.discovery_worker_leases.pop(holder[0], None)

    with app.state.discovery_workers_lock:
        if app.state.discovery_shutting_down:
            return RuntimeError("application is shutting down")
        thread = threading.Thread(target=run, daemon=True)
        holder.append(thread)
        app.state.discovery_workers.add(thread)
        if lease is not None:
            app.state.discovery_worker_leases[thread] = lease
        try:
            thread.start()
        except Exception as error:  # noqa: BLE001 - thread start can fail broadly
            app.state.discovery_workers.discard(thread)
            app.state.discovery_worker_leases.pop(thread, None)
            return error
    return None


def _dispatch(
    app, run_id: int, owner_token: str, *, sync: bool
) -> JSONResponse | None:
    with app.state.session_factory() as session:
        run = _owned_run(session, run_id)
        rejection = (
            _error(
                404,
                error="未找到",
                code="DISCOVERY_NOT_FOUND",
                detail=f"没有发现批次 {run_id}",
            )
            if run is None
            else _mutation_guard(run, _now(app))
        )
    if rejection is not None:
        _release_committed_lock(
            app.state.session_factory, run_id, owner_token
        )
        return rejection
    if sync:
        _drain(app, run_id, owner_token)
        return None
    error = _start_discovery_worker(
        app,
        _drain,
        (app, run_id, owner_token),
    )
    if error is None:
        return None
    logger.error("发现队列线程启动失败: %s", error)
    _release_committed_lock(
        app.state.session_factory, run_id, owner_token
    )
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
    parent: DiscoveryCommonStage | DiscoveryStage,
    attempt: int = 1,
) -> DiscoveryStage:
    if stage == "track_gate":
        if not isinstance(parent, DiscoveryCommonStage):
            raise ValueError("track gate requires a common-stage parent")
        common_stage_id = parent.id
        predecessor_stage_id = None
        predecessor_stage = None
    else:
        if not isinstance(parent, DiscoveryStage):
            raise ValueError(f"{stage} requires a track-stage parent")
        common_stage_id = None
        predecessor_stage_id = parent.id
        predecessor_stage = parent.stage
    return DiscoveryStage(
        run_id=run.id,
        track=track,
        stage=stage,
        attempt=attempt,
        status="queued",
        total_count=0,
        pass_count=0,
        drop_count=0,
        insufficient_count=0,
        error=None,
        input_fingerprint=run.input_fingerprint,
        ruler_version_id=getattr(run, f"{track}_ruler_version_id"),
        algorithm_version=run.algorithm_version,
        common_stage_id=common_stage_id,
        predecessor_stage_id=predecessor_stage_id,
        predecessor_stage=predecessor_stage,
        failure_kind=None,
        superseded_at=None,
        created_at=created_at,
        updated_at=created_at,
    )


def _exact_parent(
    session: Session, row: DiscoveryStage
) -> DiscoveryCommonStage | DiscoveryStage | None:
    if row.stage == "track_gate":
        if row.common_stage_id is None:
            return None
        parent = session.get(DiscoveryCommonStage, row.common_stage_id)
        return (
            parent
            if parent is not None
            and parent.run_id == row.run_id
            and parent.status == "confirmed"
            and parent.superseded_at is None
            else None
        )
    if row.predecessor_stage_id is None:
        return None
    expected = "track_gate" if row.stage == "competition" else "competition"
    parent = session.get(DiscoveryStage, row.predecessor_stage_id)
    return (
        parent
        if parent is not None
        and parent.run_id == row.run_id
        and parent.track == row.track
        and parent.stage == expected
        and row.predecessor_stage == expected
        and parent.status == "confirmed"
        and parent.superseded_at is None
        else None
    )


def _discovery_research_name(
    session: Session,
    run: DiscoveryRun,
    *,
    track: str,
    code: str,
) -> str | None:
    adjudication = _latest_track(
        session, run.id, track, "adjudication"
    )
    if (
        adjudication is None
        or adjudication.status not in {"confirmed", "insufficient_candidates"}
    ):
        return None
    competition = _exact_parent(session, adjudication)
    if not isinstance(competition, DiscoveryStage):
        return None
    final_result = session.scalar(
        select(DiscoveryResult).where(
            DiscoveryResult.run_id == run.id,
            DiscoveryResult.track == track,
            DiscoveryResult.stage == "adjudication",
            DiscoveryResult.attempt == adjudication.attempt,
            DiscoveryResult.code == code,
            DiscoveryResult.verdict == "pass",
        )
    )
    decision = session.scalar(
        select(DiscoveryDecision).where(
            DiscoveryDecision.run_id == run.id,
            DiscoveryDecision.track == track,
            DiscoveryDecision.competition_attempt == competition.attempt,
            DiscoveryDecision.competition_stage_id == competition.id,
            DiscoveryDecision.code == code,
            DiscoveryDecision.decision == "keep",
        )
    )
    evidence = session.scalar(
        select(DiscoveryEvidence.id)
        .where(
            DiscoveryEvidence.run_id == run.id,
            DiscoveryEvidence.track == track,
            DiscoveryEvidence.competition_attempt == competition.attempt,
            DiscoveryEvidence.competition_stage_id == competition.id,
            DiscoveryEvidence.code == code,
        )
        .limit(1)
    )
    frozen = session.scalar(
        select(DiscoveryInputSnapshot).where(
            DiscoveryInputSnapshot.run_id == run.id,
            DiscoveryInputSnapshot.code == code,
        )
    )
    if (
        final_result is None
        or decision is None
        or evidence is None
        or frozen is None
    ):
        return None
    return frozen.name


def _existing_discovery_research(
    session: Session, run_id: int, code: str
) -> ResearchJob | None:
    return session.scalar(
        select(ResearchJob).where(
            ResearchJob.discovery_run_id == run_id,
            ResearchJob.code == code,
        )
    )


def _research_job_payload(job: ResearchJob) -> dict[str, object]:
    return {
        "id": job.id,
        "status": job.status,
        "type": JOB_TYPE_RESEARCH,
    }


def _acquire_discovery_research_reservation(
    session: Session, run_id: int, now: datetime
) -> str | None:
    ensure_lock_row(session)
    owner_token = uuid4().hex
    acquired = session.execute(
        update(JobLock)
        .where(
            JobLock.id == LOCK_ROW_ID,
            JobLock.job_id.is_(None),
        )
        .values(
            job_id=run_id,
            job_type=_DISCOVERY_RESEARCH_RESERVATION,
            held_at=now,
            owner_token=owner_token,
        )
    )
    return owner_token if int(acquired.rowcount or 0) == 1 else None


def _replace_research_reservation(
    session: Session,
    *,
    run_id: int,
    job_id: int,
    owner_token: str,
    now: datetime,
) -> bool:
    replaced = session.execute(
        update(JobLock)
        .where(
            JobLock.id == LOCK_ROW_ID,
            JobLock.job_id == run_id,
            JobLock.job_type == _DISCOVERY_RESEARCH_RESERVATION,
            JobLock.owner_token == owner_token,
        )
        .values(
            job_id=job_id,
            job_type=JOB_TYPE_RESEARCH,
            held_at=now,
        )
    )
    return int(replaced.rowcount or 0) == 1


def _release_discovery_research_reservation(
    factory, run_id: int, owner_token: str
) -> None:
    with factory() as session:
        release(
            session,
            run_id,
            _DISCOVERY_RESEARCH_RESERVATION,
            owner_token=owner_token,
        )
        session.commit()


def _run_discovery_research(
    app, job_id: int, owner_token: str
) -> None:
    failure = "research worker exited without releasing its lease"
    try:
        with app.state.session_factory() as session:
            execute_research(
                session,
                job_id,
                llm=app.state.llm_client,
                quote_client=app.state.quote_client,
                reports_dir=app.state.reports_dir,
                market=app.state.market_client,
                lock_owner_token=owner_token,
            )
    except Exception as error:  # noqa: BLE001 - worker boundary
        failure = str(error)
        logger.exception("发现评估任务执行失败")
    finally:
        _settle_discovery_research_worker(
            app.state.session_factory,
            job_id,
            owner_token,
            status="failed",
            error=failure,
        )


def _settle_discovery_research_worker(
    factory,
    job_id: int,
    owner_token: str,
    *,
    status: str,
    error: str,
    busy_timeout_ms: int | None = None,
) -> bool:
    with factory() as session:
        session.rollback()
        try:
            if busy_timeout_ms is not None:
                session.execute(
                    text(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
                )
            owns_lease = (
                select(JobLock.id)
                .where(
                    JobLock.id == LOCK_ROW_ID,
                    JobLock.job_id == job_id,
                    JobLock.job_type == JOB_TYPE_RESEARCH,
                    JobLock.owner_token == owner_token,
                )
                .exists()
            )
            session.execute(
                update(ResearchJob)
                .where(
                    ResearchJob.id == job_id,
                    ResearchJob.status.in_(ACTIVE_STATUSES),
                    owns_lease,
                )
                .values(
                    status=status,
                    error=error,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            released = release(
                session,
                job_id,
                JOB_TYPE_RESEARCH,
                owner_token=owner_token,
            )
            if released:
                session.commit()
            else:
                session.rollback()
        except OperationalError:
            session.rollback()
            return False
        return released


def cancel_discovery_research_worker(
    factory, job_id: int, owner_token: str
) -> bool:
    return _settle_discovery_research_worker(
        factory,
        job_id,
        owner_token,
        status="canceled",
        error="application shutdown canceled research worker",
        busy_timeout_ms=100,
    )


def _dispatch_discovery_research(
    app,
    job_id: int,
    owner_token: str,
    *,
    sync: bool,
) -> JSONResponse | None:
    if sync:
        _run_discovery_research(app, job_id, owner_token)
        return None
    error = _start_discovery_worker(
        app,
        _run_discovery_research,
        (app, job_id, owner_token),
        lease=(job_id, JOB_TYPE_RESEARCH, owner_token),
    )
    if error is None:
        return None
    logger.error("发现评估线程启动失败: %s", error)
    _settle_discovery_research_worker(
        app.state.session_factory,
        job_id,
        owner_token,
        status="failed",
        error=str(error),
    )
    return _error(
        500,
        error="任务启动失败",
        code="DISCOVERY_RESEARCH_DISPATCH_FAILED",
        detail=str(error),
    )


def _lineage_rejection(row: DiscoveryStage) -> JSONResponse:
    missing = (
        row.common_stage_id is None
        if row.stage == "track_gate"
        else row.predecessor_stage_id is None
    )
    return _error(
        409,
        error="阶段谱系不可用",
        code="lineage_unavailable" if missing else "invalid_predecessor",
        detail="阶段缺少可验证的精确上游身份",
    )


def _effective_competition_rows(
    session: Session, competition: DiscoveryStage
) -> list[tuple[DiscoveryResult, DiscoveryOverride | None]]:
    rows = session.scalars(
        select(DiscoveryResult)
        .where(
            DiscoveryResult.run_id == competition.run_id,
            DiscoveryResult.track == competition.track,
            DiscoveryResult.stage == "competition",
            DiscoveryResult.attempt == competition.attempt,
        )
        .order_by(DiscoveryResult.code, DiscoveryResult.id)
    ).all()
    overrides = {
        row.code: row
        for row in session.scalars(
            select(DiscoveryOverride).where(
                DiscoveryOverride.run_id == competition.run_id,
                DiscoveryOverride.track == competition.track,
                DiscoveryOverride.stage == "competition",
                DiscoveryOverride.attempt == competition.attempt,
            )
        ).all()
    }
    return [(row, overrides.get(row.code)) for row in rows]


def _confirm_common(
    app,
    session: Session,
    run: DiscoveryRun,
) -> tuple[str | None, JSONResponse | None]:
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
    owner_token = acquire_discovery(session, run.id)
    if owner_token is None:
        session.rollback()
        session.expire_all()
        refreshed = session.get(DiscoveryCommonStage, current.id)
        if refreshed is not None and refreshed.status == "confirmed":
            return False, None
        return False, _locked(session)
    run, now, rejection = _fresh_mutation_checkpoint(
        app,
        session,
        run.id,
        owner_token=owner_token,
    )
    if rejection is not None:
        return False, rejection
    assert run is not None
    current = session.get(DiscoveryCommonStage, current.id)
    if current is None:
        session.rollback()
        return False, _error(
            404,
            error="未找到",
            code="DISCOVERY_STAGE_NOT_FOUND",
            detail="没有共同排雷阶段",
        )
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
        _new_track_stage(
            run,
            track=track,
            stage="track_gate",
            created_at=now,
            parent=current,
        )
        for track in _TRACK_ORDER
    )
    run.status = "running"
    run.updated_at = now
    session.commit()
    return owner_token, None


def _confirm_track(
    app,
    session: Session,
    run: DiscoveryRun,
    track: str,
    stage: str,
) -> tuple[str | None, JSONResponse | None]:
    current = _latest_track(session, run.id, track, stage)
    if current is None:
        return False, _error(
            404,
            error="未找到",
            code="DISCOVERY_STAGE_NOT_FOUND",
            detail=f"没有 {track}/{stage} 阶段",
        )
    parent = _exact_parent(session, current)
    if parent is None:
        return False, _lineage_rejection(current)
    if current.status in {"confirmed", "insufficient_candidates"}:
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
    owner_token = acquire_discovery(session, run.id)
    if owner_token is None:
        session.rollback()
        session.expire_all()
        refreshed = session.get(DiscoveryStage, current.id)
        if refreshed is not None and refreshed.status in {
            "confirmed",
            "insufficient_candidates",
        }:
            return False, None
        return False, _locked(session)
    exact_stage_id = current.id
    run, _acquired_at, rejection = _fresh_mutation_checkpoint(
        app,
        session,
        run.id,
        owner_token=owner_token,
    )
    if rejection is not None:
        return False, rejection
    assert run is not None
    current = session.get(DiscoveryStage, exact_stage_id)
    if current is None:
        session.rollback()
        return False, _error(
            404,
            error="未找到",
            code="DISCOVERY_STAGE_NOT_FOUND",
            detail=f"没有 {track}/{stage} 阶段",
        )
    if stage == "adjudication":
        session.expire_all()
        current = session.get(DiscoveryStage, exact_stage_id)
        if current is None:
            session.rollback()
            return False, _error(
                404,
                error="未找到",
                code="DISCOVERY_STAGE_NOT_FOUND",
                detail=f"没有 {track}/{stage} 阶段",
            )
        if current.status in {"confirmed", "insufficient_candidates"}:
            release(
                session,
                run.id,
                JOB_TYPE_DISCOVERY,
                owner_token=owner_token,
            )
            session.commit()
            return False, None
        if (
            current.status != "waiting_confirmation"
            or current.superseded_at is not None
        ):
            session.rollback()
            return False, _error(
                409,
                error="阶段不可确认",
                code="DISCOVERY_STAGE_NOT_CONFIRMABLE",
                detail=f"{track}/{stage} 当前状态为 {current.status}",
            )
        parent = _exact_parent(session, current)
        if not isinstance(parent, DiscoveryStage):
            session.rollback()
            return False, _lineage_rejection(current)
        assert isinstance(parent, DiscoveryStage)
        entrants = [
            row
            for row, override in _effective_competition_rows(session, parent)
            if (
                row.verdict
                if override is None
                else ("pass" if override.action == "forced_pass" else "drop")
            )
            == "pass"
        ]
        decisions = session.scalars(
            select(DiscoveryDecision)
            .where(
                DiscoveryDecision.run_id == run.id,
                DiscoveryDecision.track == track,
                DiscoveryDecision.competition_stage_id == parent.id,
                DiscoveryDecision.competition_attempt == parent.attempt,
            )
            .order_by(DiscoveryDecision.code, DiscoveryDecision.id)
        ).all()
        entrant_codes = {row.code for row in entrants}
        decision_by_code = {row.code: row for row in decisions}
        if set(decision_by_code) != entrant_codes:
            session.rollback()
            return False, _error(
                409,
                error="裁决不完整",
                code="DISCOVERY_DECISIONS_INCOMPLETE",
                detail="每个有效入围公司必须恰有一个裁决",
            )
        keepers = sorted(
            (
                row
                for row in decisions
                if row.decision == "keep"
            ),
            key=lambda row: row.code,
        )
        if len(keepers) > 5:
            session.rollback()
            return False, _error(
                409,
                error="保留数量过多",
                code="DISCOVERY_TOO_MANY_KEEPERS",
                detail="每条轨道最多保留 5 家公司",
            )
        existing = session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(
                DiscoveryResult.run_id == run.id,
                DiscoveryResult.track == track,
                DiscoveryResult.stage == "adjudication",
                DiscoveryResult.attempt == current.attempt,
            )
        )
        if existing:
            session.rollback()
            return False, _error(
                409,
                error="历史冲突",
                code="DISCOVERY_RESULT_HISTORY_CONFLICT",
                detail="待确认裁决阶段已有持久化结果",
            )
        run, now, rejection = _fresh_mutation_checkpoint(
            app,
            session,
            run.id,
            owner_token=owner_token,
        )
        if rejection is not None:
            return False, rejection
        assert run is not None
        current = session.get(DiscoveryStage, exact_stage_id)
        if current is None:
            session.rollback()
            return False, _error(
                404,
                error="未找到",
                code="DISCOVERY_STAGE_NOT_FOUND",
                detail=f"没有 {track}/{stage} 阶段",
            )
        keeper_rank = {
            row.code: index
            for index, row in enumerate(keepers, start=1)
        }
        target = "confirmed" if len(keepers) >= 3 else "insufficient_candidates"
        changed = session.execute(
            update(DiscoveryStage)
            .where(
                DiscoveryStage.id == current.id,
                DiscoveryStage.status == "waiting_confirmation",
                DiscoveryStage.superseded_at.is_(None),
            )
            .values(
                status=target,
                total_count=len(decisions),
                pass_count=len(keepers),
                drop_count=len(decisions) - len(keepers),
                insufficient_count=0,
                confirmed_at=now,
                updated_at=now,
            )
        )
        if int(changed.rowcount or 0) != 1:
            session.rollback()
            session.expire_all()
            refreshed = session.get(DiscoveryStage, exact_stage_id)
            if refreshed is not None and refreshed.status in {
                "confirmed",
                "insufficient_candidates",
            }:
                return False, None
            return False, _error(
                409,
                error="阶段不可确认",
                code="DISCOVERY_STAGE_NOT_CONFIRMABLE",
                detail="裁决阶段状态已改变",
            )
        session.add_all(
            DiscoveryResult(
                run_id=run.id,
                track=track,
                stage="adjudication",
                attempt=current.attempt,
                code=decision.code,
                verdict="pass" if decision.decision == "keep" else "drop",
                reason=(
                    decision.keep_note
                    if decision.decision == "keep"
                    else decision.exclusion_reason
                ),
                metrics_json=canonical_json(
                    {
                        "decision": decision.decision,
                        "competition_stage_id": parent.id,
                        "competition_attempt": parent.attempt,
                    }
                ),
                rank=keeper_rank.get(decision.code),
                created_at=now,
            )
            for decision in sorted(decisions, key=lambda row: row.code)
        )
        session.flush()
        run.status = _aggregate(session, run)
        run.updated_at = now
        release(
            session,
            run.id,
            JOB_TYPE_DISCOVERY,
            owner_token=owner_token,
        )
        session.commit()
        return False, None
    assert next_stage is not None
    run, now, rejection = _fresh_mutation_checkpoint(
        app,
        session,
        run.id,
        owner_token=owner_token,
    )
    if rejection is not None:
        return False, rejection
    assert run is not None
    current = session.get(DiscoveryStage, exact_stage_id)
    if current is None:
        session.rollback()
        return False, _error(
            404,
            error="未找到",
            code="DISCOVERY_STAGE_NOT_FOUND",
            detail=f"没有 {track}/{stage} 阶段",
        )
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
            parent=current,
            attempt=(
                session.scalar(
                    select(func.max(DiscoveryStage.attempt)).where(
                        DiscoveryStage.run_id == run.id,
                        DiscoveryStage.track == track,
                        DiscoveryStage.stage == next_stage,
                    )
                )
                or 0
            )
            + 1,
        )
    )
    run.status = "running"
    run.updated_at = now
    session.commit()
    return owner_token, None


def resume_discovery_queues(app) -> None:
    """Adopt each resumable queue once after stale-lock cleanup."""

    factory = app.state.session_factory
    current_week = china_week_key(_now(app))
    with factory() as session:
        run_ids = list(
            session.scalars(
                select(DiscoveryRun.id)
                .where(
                    DiscoveryRun.user_id == LOCAL_USER_ID,
                    DiscoveryRun.week_key == current_week,
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
        with app.state.discovery_workers_lock:
            if app.state.discovery_shutting_down:
                return False
        with factory() as session:
            run = _owned_run(session, run_id)
            if run is None or not _is_mutable_run(run, _now(app)):
                return True
            owner_token = acquire_discovery(session, run_id)
            if owner_token is None:
                session.rollback()
                return False
            _run, _commit_at, rejection = _fresh_mutation_checkpoint(
                app,
                session,
                run_id,
                owner_token=owner_token,
            )
            if rejection is not None:
                return True
            session.commit()
        with factory() as session:
            run = _owned_run(session, run_id)
            still_current = (
                run is not None and _is_mutable_run(run, _now(app))
            )
        if not still_current:
            _release_committed_lock(factory, run_id, owner_token)
            return True
        worker = threading.current_thread()
        with app.state.discovery_workers_lock:
            stopping = app.state.discovery_shutting_down
            if not stopping and worker in app.state.discovery_workers:
                app.state.discovery_worker_leases[worker] = (
                    run_id,
                    owner_token,
                )
        if stopping:
            with factory() as session:
                release(
                    session,
                    run_id,
                    JOB_TYPE_DISCOVERY,
                    owner_token=owner_token,
                )
                session.commit()
            return False
        try:
            _drain(app, run_id, owner_token)
        finally:
            with app.state.discovery_workers_lock:
                if app.state.discovery_worker_leases.get(worker) == (
                    run_id,
                    owner_token,
                ):
                    app.state.discovery_worker_leases.pop(worker, None)
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

    @router.post("/{run_id}/research")
    async def create_discovery_research(request: Request, run_id: int):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_RESEARCH"
        )
        if rejected is not None:
            return rejected
        track = payload.get("track")
        code = payload.get("code")
        if (
            type(track) is not str
            or track not in TRACKS
            or type(code) is not str
            or not code.strip()
        ):
            return _error(
                400,
                error="请求无效",
                code="INVALID_DISCOVERY_RESEARCH",
                detail="track 或 code 无效",
            )
        code = code.strip()
        if not has_env_value(
            request.app.state.env_path, OPENAI_API_KEY
        ):
            return _error(
                400,
                error="未配置模型密钥",
                code="NO_API_KEY",
                detail="评估需要模型密钥，请到设置页配置",
            )
        sync = _truthy(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        factory = request.app.state.session_factory
        owner_token: str | None = None
        converted = False
        job_id: int | None = None
        try:
            with factory() as session:
                run = _owned_run(session, run_id)
                if run is None:
                    return _error(
                        404,
                        error="未找到",
                        code="DISCOVERY_NOT_FOUND",
                        detail=f"没有发现批次 {run_id}",
                    )
                immutable = _mutation_guard(run, _now(request.app))
                if immutable is not None:
                    return immutable
                name = _discovery_research_name(
                    session, run, track=track, code=code
                )
                if name is None:
                    return _error(
                        409,
                        error="候选不可评估",
                        code="DISCOVERY_CANDIDATE_NOT_RESEARCHABLE",
                        detail="仅可评估已有最终保留决定和证据的候选",
                    )
                existing = _existing_discovery_research(
                    session, run.id, code
                )
                if existing is not None:
                    return _research_job_payload(existing)
                try:
                    owner_token = _acquire_discovery_research_reservation(
                        session, run.id, _now(request.app)
                    )
                except OperationalError:
                    session.rollback()
                    existing = _existing_discovery_research(
                        session, run.id, code
                    )
                    if existing is not None:
                        return _research_job_payload(existing)
                    return _locked(session)
                if owner_token is None:
                    session.rollback()
                    existing = _existing_discovery_research(
                        session, run.id, code
                    )
                    if existing is not None:
                        return _research_job_payload(existing)
                    return _locked(session)

                session.expire_all()
                run = _owned_run(session, run_id)
                if run is None:
                    session.rollback()
                    return _error(
                        404,
                        error="未找到",
                        code="DISCOVERY_NOT_FOUND",
                        detail=f"没有发现批次 {run_id}",
                    )
                immutable = _mutation_guard(run, _now(request.app))
                if immutable is not None:
                    session.rollback()
                    return immutable
                name = _discovery_research_name(
                    session, run, track=track, code=code
                )
                if name is None:
                    session.rollback()
                    return _error(
                        409,
                        error="候选不可评估",
                        code="DISCOVERY_CANDIDATE_NOT_RESEARCHABLE",
                        detail="仅可评估已有最终保留决定和证据的候选",
                    )
                existing = _existing_discovery_research(
                    session, run.id, code
                )
                if existing is not None:
                    session.rollback()
                    return _research_job_payload(existing)
                accepted = session.scalar(
                    select(func.count(func.distinct(ResearchJob.code)))
                    .join(
                        DiscoveryRun,
                        ResearchJob.discovery_run_id == DiscoveryRun.id,
                    )
                    .where(
                        DiscoveryRun.user_id == run.user_id,
                        DiscoveryRun.week_key == run.week_key,
                    )
                )
                if int(accepted or 0) >= _DISCOVERY_RESEARCH_WEEKLY_QUOTA:
                    session.rollback()
                    return _error(
                        409,
                        error="本周评估名额已满",
                        code="DISCOVERY_RESEARCH_QUOTA",
                        detail="每个自然周最多评估 3 家不同公司",
                    )
                job = create_research_job(
                    session,
                    name=name,
                    code=code,
                    discovery_run_id=run.id,
                    discovery_track=track,
                )
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    existing = _existing_discovery_research(
                        session, run.id, code
                    )
                    if existing is not None:
                        return _research_job_payload(existing)
                    return _error(
                        409,
                        error="评估请求冲突",
                        code="DISCOVERY_RESEARCH_CONFLICT",
                        detail="候选评估请求已被其他会话接受",
                    )
                if not _replace_research_reservation(
                    session,
                    run_id=run.id,
                    job_id=job.id,
                    owner_token=owner_token,
                    now=_now(request.app),
                ):
                    session.rollback()
                    return _locked(session)
                session.commit()
                converted = True
                job_id = job.id
        finally:
            if owner_token is not None and not converted:
                _release_discovery_research_reservation(
                    factory, run_id, owner_token
                )

        assert job_id is not None and owner_token is not None
        failed = _dispatch_discovery_research(
            request.app,
            job_id,
            owner_token,
            sync=sync,
        )
        if failed is not None:
            return failed
        with factory() as session:
            job = session.get(ResearchJob, job_id)
            assert job is not None
            return _research_job_payload(job)

    @router.post("")
    async def create_discovery(request: Request):
        payload, rejected = await _object_body(request, "INVALID_DISCOVERY")
        if rejected is not None:
            return rejected
        sync = _truthy(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        now = _now(request.app)
        week_key = china_week_key(now)
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
                dispatch, failed = _acquire_queued_work(
                    request.app,
                    session,
                    existing,
                )
                if failed is not None:
                    return failed
                existing_id = existing.id
                if dispatch:
                    failed = _dispatch(
                        request.app, existing_id, dispatch, sync=sync
                    )
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
                ruler_payloads_json=None,
                input_fingerprint=None,
                error=None,
            )
            session.add(run)
            try:
                session.flush()
                owner_token = acquire_discovery(session, run.id)
                if owner_token is None:
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
                run.ruler_payloads_json = freeze_discovery_ruler_payloads(
                    session,
                    versions.ruler_version_ids,
                    versions.algorithm_version,
                    user_id=LOCAL_USER_ID,
                )
                freeze_inputs(
                    session,
                    run,
                    frozen.companies,
                    versions,
                    frozen.trading_dates,
                    snapshot_at=frozen.snapshot_at,
                )
                session.flush()
                run, commit_at, rejection = _fresh_mutation_checkpoint(
                    request.app,
                    session,
                    run.id,
                    owner_token=owner_token,
                )
                if rejection is not None:
                    return rejection
                assert run is not None
                run.created_at = commit_at
                run.updated_at = commit_at
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
                        created_at=commit_at,
                        updated_at=commit_at,
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
                    dispatch, failed = _acquire_queued_work(
                        request.app,
                        session,
                        winner,
                    )
                    if failed is not None:
                        return failed
                    winner_id = winner.id
                    if dispatch:
                        failed = _dispatch(
                            request.app, winner_id, dispatch, sync=sync
                        )
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
        dispatch_error = _dispatch(
            request.app, run_id, owner_token, sync=sync
        )
        if dispatch_error is not None:
            return dispatch_error
        return _read_after_dispatch(request.app, run_id)

    @router.get("/current")
    def current_discovery(request: Request):
        week_key = china_week_key(_now(request.app))
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
            try:
                changes = _compact_changes(session, run)
            except CorruptDiscoveryResult:
                return _error(
                    500,
                    error="结果损坏",
                    code="DISCOVERY_RESULT_CORRUPT",
                    detail="持久化周度变化无法安全读取",
                )
            return _run_payload(session, run) | {"changes": changes}

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
            except (CorruptDiscoveryResult, DiscoverySummaryError):
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
        now = _now(request.app)
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
            immutable = _mutation_guard(run, now)
            if immutable is not None:
                return immutable
            dispatch, failed = _confirm_common(
                request.app,
                session,
                run,
            )
            if failed is not None:
                return failed
            if not dispatch:
                dispatch, failed = _acquire_queued_work(
                    request.app,
                    session,
                    run,
                )
                if failed is not None:
                    return failed
        if dispatch:
            failed = _dispatch(request.app, run_id, dispatch, sync=sync)
            if failed is not None:
                return failed
        return _read_after_dispatch(request.app, run_id)

    @router.post("/{run_id}/stages/safety/retry")
    async def retry_safety(request: Request, run_id: int):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_RETRY"
        )
        if rejected is not None:
            return rejected
        sync = _truthy(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        now = _now(request.app)
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
            immutable = _mutation_guard(run, now)
            if immutable is not None:
                return immutable
            observed = _latest_common(session, run.id)
            retry_target = _common_retry_target(session, observed)
            if (
                observed is not None
                and retry_target is not None
                and observed.retry_of_stage_id == retry_target.id
                and (
                    observed.status in {"waiting_confirmation", "confirmed"}
                    or (
                        (held := lock_status(session)) is not None
                        and held.get("type") == JOB_TYPE_DISCOVERY
                        and held.get("job_id") == run.id
                    )
                )
            ):
                return _run_payload(session, run)
            owner_token = acquire_discovery(session, run.id)
            if owner_token is None:
                session.rollback()
                session.expire_all()
                latest = _latest_common(session, run.id)
                if (
                    retry_target is not None
                    and latest is not None
                    and latest.retry_of_stage_id == retry_target.id
                ):
                    return _run_payload(session, run)
                return _locked(session)
            run, _acquired_at, rejection = _fresh_mutation_checkpoint(
                request.app,
                session,
                run.id,
                owner_token=owner_token,
            )
            if rejection is not None:
                return rejection
            assert run is not None
            current = _latest_common(session, run.id)
            if current is None:
                session.rollback()
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_STAGE_NOT_FOUND",
                    detail="没有共同排雷阶段",
                )
            if (
                retry_target is not None
                and current.retry_of_stage_id == retry_target.id
            ):
                dispatch = owner_token if current.status == "queued" else None
                if dispatch is None:
                    release(
                        session,
                        run.id,
                        JOB_TYPE_DISCOVERY,
                        owner_token=owner_token,
                    )
                session.commit()
            else:
                if current.status in {"data_not_ready", "ruler_not_ready"}:
                    session.rollback()
                    return _error(
                        409,
                        error="终止失败不可重试",
                        code="DISCOVERY_RETRY_TERMINAL",
                        detail="数据或尺子未就绪需新建批次",
                    )
                if current.status != "failed":
                    session.rollback()
                    return _error(
                        409,
                        error="阶段不可重试",
                        code="DISCOVERY_RETRY_NOT_ALLOWED",
                        detail="仅当前瞬时失败阶段可重试",
                    )
                if current.failure_kind != "transient":
                    session.rollback()
                    return _error(
                        409,
                        error="永久失败不可重试",
                        code="DISCOVERY_RETRY_PERMANENT",
                        detail="仅瞬时失败可重试",
                    )
                if (
                    current.input_fingerprint != run.input_fingerprint
                    or not _is_build_available(
                        current.algorithm_version, AVAILABLE_BUILDS
                    )
                ):
                    session.rollback()
                    return _error(
                        409,
                        error="算法不可用",
                        code="algorithm_unavailable",
                        detail="冻结输入或保存构建不可用",
                    )
                try:
                    load_frozen_inputs(session, run.id)
                except (TypeError, ValueError):
                    session.rollback()
                    return _error(
                        409,
                        error="算法不可用",
                        code="algorithm_unavailable",
                        detail="冻结输入或尺子载荷不可用",
                    )
                current_id = current.id
                run, commit_at, rejection = _fresh_mutation_checkpoint(
                    request.app,
                    session,
                    run.id,
                    owner_token=owner_token,
                )
                if rejection is not None:
                    return rejection
                assert run is not None
                current = session.get(DiscoveryCommonStage, current_id)
                if current is None:
                    session.rollback()
                    return _error(
                        404,
                        error="未找到",
                        code="DISCOVERY_STAGE_NOT_FOUND",
                        detail="没有共同排雷阶段",
                    )
                current.superseded_at = commit_at
                session.add(
                    DiscoveryCommonStage(
                        run_id=run.id,
                        attempt=current.attempt + 1,
                        status="queued",
                        total_count=0,
                        pass_count=0,
                        drop_count=0,
                        insufficient_count=0,
                        error=None,
                        confirmed_at=None,
                        input_fingerprint=current.input_fingerprint,
                        algorithm_version=current.algorithm_version,
                        retry_of_stage_id=current.id,
                        failure_kind=None,
                        superseded_at=None,
                        created_at=commit_at,
                        updated_at=commit_at,
                    )
                )
                run.status = "running"
                run.updated_at = commit_at
                session.commit()
                dispatch = owner_token
        if dispatch:
            failed = _dispatch(request.app, run_id, dispatch, sync=sync)
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
            except (CorruptDiscoveryResult, DiscoverySummaryError):
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
        now = _now(request.app)
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
            immutable = _mutation_guard(run, now)
            if immutable is not None:
                return immutable
            dispatch, failed = _confirm_track(
                request.app,
                session,
                run,
                track,
                stage,
            )
            if failed is not None:
                return failed
            if not dispatch:
                dispatch, failed = _acquire_queued_work(
                    request.app,
                    session,
                    run,
                )
                if failed is not None:
                    return failed
        if dispatch:
            failed = _dispatch(request.app, run_id, dispatch, sync=sync)
            if failed is not None:
                return failed
        return _read_after_dispatch(request.app, run_id)

    @router.post("/{run_id}/tracks/{track}/stages/{stage}/retry")
    async def retry_track_stage(
        request: Request, run_id: int, track: str, stage: str
    ):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_RETRY"
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
        now = _now(request.app)
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
            immutable = _mutation_guard(run, now)
            if immutable is not None:
                return immutable
            observed = _latest_track(session, run.id, track, stage)
            retry_target = _track_retry_target(session, observed)
            if (
                observed is not None
                and retry_target is not None
                and observed.retry_of_stage_id == retry_target.id
                and (
                    observed.status
                    in {
                        "waiting_confirmation",
                        "confirmed",
                        "insufficient_candidates",
                    }
                    or (
                        (held := lock_status(session)) is not None
                        and held.get("type") == JOB_TYPE_DISCOVERY
                        and held.get("job_id") == run.id
                    )
                )
            ):
                return _run_payload(session, run)
            owner_token = acquire_discovery(session, run.id)
            if owner_token is None:
                session.rollback()
                session.expire_all()
                latest = _latest_track(
                    session, run.id, track, stage
                )
                if (
                    retry_target is not None
                    and latest is not None
                    and latest.retry_of_stage_id == retry_target.id
                ):
                    return _run_payload(session, run)
                return _locked(session)
            run, _acquired_at, rejection = _fresh_mutation_checkpoint(
                request.app,
                session,
                run.id,
                owner_token=owner_token,
            )
            if rejection is not None:
                return rejection
            assert run is not None
            current = _latest_track(session, run.id, track, stage)
            if current is None:
                session.rollback()
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_STAGE_NOT_FOUND",
                    detail="指定阶段不存在",
                )
            if (
                retry_target is not None
                and current.retry_of_stage_id == retry_target.id
            ):
                dispatch = owner_token if current.status == "queued" else None
                if dispatch is None:
                    release(
                        session,
                        run.id,
                        JOB_TYPE_DISCOVERY,
                        owner_token=owner_token,
                    )
                session.commit()
            else:
                if _exact_parent(session, current) is None:
                    session.rollback()
                    return _lineage_rejection(current)
                if current.status in {"data_not_ready", "ruler_not_ready"}:
                    session.rollback()
                    return _error(
                        409,
                        error="终止失败不可重试",
                        code="DISCOVERY_RETRY_TERMINAL",
                        detail="数据或尺子未就绪需新建批次",
                    )
                if current.status != "failed":
                    session.rollback()
                    return _error(
                        409,
                        error="阶段不可重试",
                        code="DISCOVERY_RETRY_NOT_ALLOWED",
                        detail="仅当前瞬时失败阶段可重试",
                    )
                if current.failure_kind != "transient":
                    session.rollback()
                    return _error(
                        409,
                        error="永久失败不可重试",
                        code="DISCOVERY_RETRY_PERMANENT",
                        detail="仅瞬时失败可重试",
                    )
                if (
                    current.input_fingerprint != run.input_fingerprint
                    or current.ruler_version_id
                    != getattr(run, f"{track}_ruler_version_id")
                    or not _is_build_available(
                        current.algorithm_version, AVAILABLE_BUILDS
                    )
                ):
                    session.rollback()
                    return _error(
                        409,
                        error="算法不可用",
                        code="algorithm_unavailable",
                        detail="冻结输入、尺子或保存构建不可用",
                    )
                try:
                    frozen = load_frozen_inputs(session, run.id)
                    ruler = frozen.ruler_payloads.get(track)
                    if (
                        current.ruler_version_id is None
                        or not isinstance(ruler, Mapping)
                        or ruler.get("id") != current.ruler_version_id
                    ):
                        raise ValueError("frozen ruler is unavailable")
                except (TypeError, ValueError):
                    session.rollback()
                    return _error(
                        409,
                        error="算法不可用",
                        code="algorithm_unavailable",
                        detail="冻结输入、尺子载荷或保存构建不可用",
                    )
                current_id = current.id
                run, commit_at, rejection = _fresh_mutation_checkpoint(
                    request.app,
                    session,
                    run.id,
                    owner_token=owner_token,
                )
                if rejection is not None:
                    return rejection
                assert run is not None
                current = session.get(DiscoveryStage, current_id)
                if current is None:
                    session.rollback()
                    return _error(
                        404,
                        error="未找到",
                        code="DISCOVERY_STAGE_NOT_FOUND",
                        detail="指定阶段不存在",
                    )
                current.superseded_at = commit_at
                session.add(
                    DiscoveryStage(
                        run_id=run.id,
                        track=track,
                        stage=stage,
                        attempt=current.attempt + 1,
                        status="queued",
                        total_count=0,
                        pass_count=0,
                        drop_count=0,
                        insufficient_count=0,
                        error=None,
                        confirmed_at=None,
                        input_fingerprint=current.input_fingerprint,
                        ruler_version_id=current.ruler_version_id,
                        algorithm_version=current.algorithm_version,
                        common_stage_id=current.common_stage_id,
                        predecessor_stage_id=current.predecessor_stage_id,
                        predecessor_stage=current.predecessor_stage,
                        retry_of_stage_id=current.id,
                        reopen_of_stage_id=None,
                        failure_kind=None,
                        superseded_at=None,
                        created_at=commit_at,
                        updated_at=commit_at,
                    )
                )
                run.status = "running"
                run.updated_at = commit_at
                session.commit()
                dispatch = owner_token
        if dispatch:
            failed = _dispatch(request.app, run_id, dispatch, sync=sync)
            if failed is not None:
                return failed
        return _read_after_dispatch(request.app, run_id)

    @router.post("/{run_id}/tracks/{track}/stages/{stage}/reopen")
    async def reopen_track_stage(
        request: Request, run_id: int, track: str, stage: str
    ):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_REOPEN"
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
        now = _now(request.app)
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
            immutable = _mutation_guard(run, now)
            if immutable is not None:
                return immutable
            if stage == "adjudication":
                return _error(
                    409,
                    error="阶段不可重开",
                    code="invalid_reopen_stage",
                    detail="最终裁决必须通过重开 competition 建立新决定集",
                )
            observed = _latest_track(session, run.id, track, stage)
            reopen_target_id = None if observed is None else observed.id
            if (
                observed is not None
                and observed.reopen_of_stage_id is not None
                and observed.status in {"queued", "running"}
                and (
                    (held := lock_status(session)) is not None
                    and held.get("type") == JOB_TYPE_DISCOVERY
                    and held.get("job_id") == run.id
                )
            ):
                return _run_payload(session, run)
            owner_token = acquire_discovery(session, run.id)
            if owner_token is None:
                session.rollback()
                session.expire_all()
                latest = _latest_track(
                    session, run.id, track, stage
                )
                if (
                    reopen_target_id is not None
                    and latest is not None
                    and latest.reopen_of_stage_id == reopen_target_id
                ):
                    return _run_payload(session, run)
                return _locked(session)
            run, _acquired_at, rejection = _fresh_mutation_checkpoint(
                request.app,
                session,
                run.id,
                owner_token=owner_token,
            )
            if rejection is not None:
                return rejection
            assert run is not None
            selected = _latest_track(session, run.id, track, stage)
            if selected is None:
                session.rollback()
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_STAGE_NOT_FOUND",
                    detail="指定阶段不存在",
                )
            if (
                reopen_target_id is not None
                and selected.reopen_of_stage_id == reopen_target_id
            ):
                if selected.status == "queued":
                    session.commit()
                    redispatch = owner_token
                else:
                    release(
                        session,
                        run.id,
                        JOB_TYPE_DISCOVERY,
                        owner_token=owner_token,
                    )
                    session.commit()
                    return _run_payload(session, run)
            else:
                redispatch = None
            if redispatch is not None:
                pass
            elif selected.status == "waiting_confirmation":
                session.rollback()
                return _error(
                    409,
                    error="阶段无需重开",
                    code="DISCOVERY_REOPEN_WAITING",
                    detail="待确认阶段可直接编辑覆盖",
                )
            elif selected.status != "confirmed":
                session.rollback()
                return _error(
                    409,
                    error="阶段不可重开",
                    code="DISCOVERY_REOPEN_NOT_ALLOWED",
                    detail="仅已确认阶段可重开",
                )
            elif _exact_parent(session, selected) is None:
                session.rollback()
                return _lineage_rejection(selected)
            elif (
                selected.input_fingerprint != run.input_fingerprint
                or selected.ruler_version_id
                != getattr(run, f"{track}_ruler_version_id")
                or not _is_build_available(
                    selected.algorithm_version, AVAILABLE_BUILDS
                )
            ):
                session.rollback()
                return _error(
                    409,
                    error="算法不可用",
                    code="algorithm_unavailable",
                    detail="冻结输入、尺子或保存构建不可用",
                )
            if redispatch is None:
                try:
                    frozen = load_frozen_inputs(session, run.id)
                    ruler = frozen.ruler_payloads.get(track)
                    if (
                        selected.ruler_version_id is None
                        or not isinstance(ruler, Mapping)
                        or ruler.get("id") != selected.ruler_version_id
                    ):
                        raise ValueError("frozen ruler is unavailable")
                except (TypeError, ValueError):
                    session.rollback()
                    return _error(
                        409,
                        error="算法不可用",
                        code="algorithm_unavailable",
                        detail="冻结输入、尺子载荷或保存构建不可用",
                    )
            if redispatch is None:
                selected_id = selected.id
                run, commit_at, rejection = _fresh_mutation_checkpoint(
                    request.app,
                    session,
                    run.id,
                    owner_token=owner_token,
                )
                if rejection is not None:
                    return rejection
                assert run is not None
                selected = session.get(DiscoveryStage, selected_id)
                if selected is None:
                    session.rollback()
                    return _error(
                        404,
                        error="未找到",
                        code="DISCOVERY_STAGE_NOT_FOUND",
                        detail="指定阶段不存在",
                    )
                current_rows = session.scalars(
                    select(DiscoveryStage).where(
                        DiscoveryStage.run_id == run.id,
                        DiscoveryStage.track == track,
                        DiscoveryStage.superseded_at.is_(None),
                    )
                ).all()
                affected_ids = {selected.id}
                while True:
                    before = len(affected_ids)
                    affected_ids.update(
                        row.id
                        for row in current_rows
                        if row.predecessor_stage_id in affected_ids
                    )
                    if len(affected_ids) == before:
                        break
                session.execute(
                    update(DiscoveryStage)
                    .where(
                        DiscoveryStage.id.in_(affected_ids),
                        DiscoveryStage.superseded_at.is_(None),
                    )
                    .values(
                        superseded_at=commit_at,
                        updated_at=commit_at,
                    )
                )
                session.add(
                    DiscoveryStage(
                        run_id=run.id,
                        track=track,
                        stage=stage,
                        attempt=selected.attempt + 1,
                        status="queued",
                        total_count=0,
                        pass_count=0,
                        drop_count=0,
                        insufficient_count=0,
                        error=None,
                        confirmed_at=None,
                        input_fingerprint=selected.input_fingerprint,
                        ruler_version_id=selected.ruler_version_id,
                        algorithm_version=selected.algorithm_version,
                        common_stage_id=selected.common_stage_id,
                        predecessor_stage_id=selected.predecessor_stage_id,
                        predecessor_stage=selected.predecessor_stage,
                        retry_of_stage_id=None,
                        reopen_of_stage_id=selected.id,
                        failure_kind=None,
                        superseded_at=None,
                        created_at=commit_at,
                        updated_at=commit_at,
                    )
                )
                run.status = "running"
                run.updated_at = commit_at
                session.commit()
                redispatch = owner_token
        assert redispatch is not None
        failed = _dispatch(
            request.app, run_id, redispatch, sync=sync
        )
        if failed is not None:
            return failed
        return _read_after_dispatch(request.app, run_id)

    @router.post("/{run_id}/overrides")
    async def save_override(request: Request, run_id: int):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_OVERRIDE"
        )
        if rejected is not None:
            return rejected
        track = payload.get("track")
        stage_name = payload.get("stage")
        code = payload.get("code")
        action = payload.get("action")
        reason = payload.get("reason")
        if (
            type(track) is not str
            or track not in TRACKS
            or type(stage_name) is not str
            or stage_name not in TRACK_STAGES
            or type(code) is not str
            or not code.strip()
            or type(action) is not str
            or action not in OVERRIDE_ACTIONS
            or type(reason) is not str
            or not reason.strip()
        ):
            return _error(
                400,
                error="覆盖无效",
                code="INVALID_DISCOVERY_OVERRIDE",
                detail="轨道、阶段、代码、动作和非空理由均为必填",
            )
        now = _now(request.app)
        with request.app.state.session_factory() as session:
            run = _owned_run(session, run_id)
            if run is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_NOT_FOUND",
                    detail=f"没有发现批次 {run_id}",
                )
            immutable = _mutation_guard(run, now)
            if immutable is not None:
                return immutable
            stage_row = _latest_track(
                session, run.id, track, stage_name
            )
            if stage_row is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_STAGE_NOT_FOUND",
                    detail="指定阶段不存在",
                )
            supplied_attempt = payload.get("attempt")
            if supplied_attempt is not None and (
                type(supplied_attempt) is not int
                or supplied_attempt != stage_row.attempt
            ):
                return _error(
                    409,
                    error="阶段已变化",
                    code="DISCOVERY_STALE_ATTEMPT",
                    detail="客户端 attempt 不是当前阶段",
                )
            stage_id = stage_row.id
            run, commit_at, rejection = _fresh_mutation_checkpoint(
                request.app,
                session,
                run.id,
            )
            if rejection is not None:
                return rejection
            assert run is not None
            stage_row = session.get(DiscoveryStage, stage_id)
            if stage_row is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_STAGE_NOT_FOUND",
                    detail="指定阶段不存在",
                )
            changed = session.execute(
                update(DiscoveryStage)
                .where(
                    DiscoveryStage.id == stage_row.id,
                    DiscoveryStage.status == "waiting_confirmation",
                    DiscoveryStage.superseded_at.is_(None),
                )
                .values(updated_at=DiscoveryStage.updated_at)
            )
            if int(changed.rowcount or 0) != 1:
                session.rollback()
                return _error(
                    409,
                    error="覆盖不可编辑",
                    code="DISCOVERY_OVERRIDE_NOT_EDITABLE",
                    detail="覆盖仅可在待确认阶段编辑",
                )
            exists = session.scalar(
                select(DiscoveryResult.id).where(
                    DiscoveryResult.run_id == run.id,
                    DiscoveryResult.track == track,
                    DiscoveryResult.stage == stage_name,
                    DiscoveryResult.attempt == stage_row.attempt,
                    DiscoveryResult.code == code,
                )
            )
            if exists is None:
                session.rollback()
                return _error(
                    409,
                    error="结果不存在",
                    code="DISCOVERY_RESULT_NOT_FOUND",
                    detail="代码不属于该精确阶段结果",
                )
            row = session.scalar(
                select(DiscoveryOverride).where(
                    DiscoveryOverride.run_id == run.id,
                    DiscoveryOverride.track == track,
                    DiscoveryOverride.stage == stage_name,
                    DiscoveryOverride.attempt == stage_row.attempt,
                    DiscoveryOverride.code == code,
                )
            )
            if row is None:
                row = DiscoveryOverride(
                    run_id=run.id,
                    track=track,
                    stage=stage_name,
                    attempt=stage_row.attempt,
                    code=code,
                    action=action,
                    reason=reason.strip(),
                    created_at=commit_at,
                )
                session.add(row)
            else:
                row.action = action
                row.reason = reason.strip()
                row.created_at = commit_at
            session.commit()
            session.refresh(row)
            return _override_json(row)

    @router.delete("/{run_id}/overrides/{override_id}")
    def delete_override(request: Request, run_id: int, override_id: int):
        now = _now(request.app)
        with request.app.state.session_factory() as session:
            run = _owned_run(session, run_id)
            if run is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_OVERRIDE_NOT_FOUND",
                    detail="覆盖不存在",
                )
            immutable = _mutation_guard(run, now)
            if immutable is not None:
                return immutable
            row = session.get(DiscoveryOverride, override_id)
            if row is None or row.run_id != run_id:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_OVERRIDE_NOT_FOUND",
                    detail="覆盖不存在",
                )
            stage_row = _latest_track(
                session, run.id, row.track, row.stage
            )
            if (
                stage_row is None
                or stage_row.attempt != row.attempt
            ):
                return _error(
                    409,
                    error="覆盖不可编辑",
                    code="DISCOVERY_OVERRIDE_NOT_EDITABLE",
                    detail="覆盖不属于当前阶段",
                )
            stage_id = stage_row.id
            run, _commit_at, rejection = _fresh_mutation_checkpoint(
                request.app,
                session,
                run.id,
            )
            if rejection is not None:
                return rejection
            assert run is not None
            row = session.get(DiscoveryOverride, override_id)
            stage_row = session.get(DiscoveryStage, stage_id)
            if row is None or stage_row is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_OVERRIDE_NOT_FOUND",
                    detail="覆盖不存在",
                )
            changed = session.execute(
                update(DiscoveryStage)
                .where(
                    DiscoveryStage.id == stage_row.id,
                    DiscoveryStage.status == "waiting_confirmation",
                    DiscoveryStage.superseded_at.is_(None),
                )
                .values(updated_at=DiscoveryStage.updated_at)
            )
            if int(changed.rowcount or 0) != 1:
                session.rollback()
                return _error(
                    409,
                    error="覆盖不可编辑",
                    code="DISCOVERY_OVERRIDE_NOT_EDITABLE",
                    detail="覆盖仅可在待确认阶段删除",
                )
            session.delete(row)
            session.commit()
            return {"deleted": True, "id": override_id}

    @router.post("/{run_id}/decisions")
    async def save_decision(request: Request, run_id: int):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_DECISION"
        )
        if rejected is not None:
            return rejected
        track = payload.get("track")
        code = payload.get("code")
        decision = payload.get("decision")
        reason = payload.get("reason")
        note = payload.get("note")
        if (
            type(track) is not str
            or track not in TRACKS
            or type(code) is not str
            or not code.strip()
            or type(decision) is not str
            or decision not in {"keep", "exclude"}
            or (
                decision == "exclude"
                and (type(reason) is not str or not reason.strip())
            )
            or (decision == "keep" and note is not None and type(note) is not str)
        ):
            return _error(
                400,
                error="裁决无效",
                code="INVALID_DISCOVERY_DECISION",
                detail="裁决必须是 keep/exclude，exclude 需要非空理由",
            )
        now = _now(request.app)
        with request.app.state.session_factory() as session:
            run = _owned_run(session, run_id)
            if run is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_NOT_FOUND",
                    detail=f"没有发现批次 {run_id}",
                )
            immutable = _mutation_guard(run, now)
            if immutable is not None:
                return immutable
            adjudication = _latest_track(
                session, run.id, track, "adjudication"
            )
            if adjudication is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_STAGE_NOT_FOUND",
                    detail="裁决阶段不存在",
                )
            parent = _exact_parent(session, adjudication)
            if not isinstance(parent, DiscoveryStage):
                return _lineage_rejection(adjudication)
            changed = session.execute(
                update(DiscoveryStage)
                .where(
                    DiscoveryStage.id == adjudication.id,
                    DiscoveryStage.status == "waiting_confirmation",
                    DiscoveryStage.superseded_at.is_(None),
                )
                .values(updated_at=DiscoveryStage.updated_at)
            )
            if int(changed.rowcount or 0) != 1:
                session.rollback()
                return _error(
                    409,
                    error="裁决不可编辑",
                    code="DISCOVERY_DECISION_NOT_EDITABLE",
                    detail="裁决仅可在最终阶段待确认时编辑",
                )
            entrants = {
                row.code
                for row, override in _effective_competition_rows(
                    session, parent
                )
                if (
                    row.verdict
                    if override is None
                    else (
                        "pass"
                        if override.action == "forced_pass"
                        else "drop"
                    )
                )
                == "pass"
            }
            if code not in entrants:
                session.rollback()
                return _error(
                    409,
                    error="非入围公司",
                    code="DISCOVERY_NOT_AN_ENTRANT",
                    detail="代码不是冻结覆盖后的竞争阶段入围公司",
                )
            parent_id = parent.id
            run, commit_at, rejection = _fresh_mutation_checkpoint(
                request.app,
                session,
                run.id,
            )
            if rejection is not None:
                return rejection
            assert run is not None
            parent = session.get(DiscoveryStage, parent_id)
            if parent is None:
                return _error(
                    409,
                    error="阶段谱系不可用",
                    code="invalid_predecessor",
                    detail="阶段缺少可验证的精确上游身份",
                )
            row = session.scalar(
                select(DiscoveryDecision).where(
                    DiscoveryDecision.run_id == run.id,
                    DiscoveryDecision.track == track,
                    DiscoveryDecision.competition_stage_id == parent.id,
                    DiscoveryDecision.competition_attempt == parent.attempt,
                    DiscoveryDecision.code == code,
                )
            )
            if row is None:
                row = DiscoveryDecision(
                    run_id=run.id,
                    track=track,
                    competition_attempt=parent.attempt,
                    competition_stage_id=parent.id,
                    stage="competition",
                    code=code,
                    decision=decision,
                    exclusion_reason=(
                        reason.strip() if decision == "exclude" else None
                    ),
                    keep_note=note if decision == "keep" else None,
                    created_at=commit_at,
                )
                session.add(row)
            else:
                row.decision = decision
                row.exclusion_reason = (
                    reason.strip() if decision == "exclude" else None
                )
                row.keep_note = note if decision == "keep" else None
                row.created_at = commit_at
            session.commit()
            session.refresh(row)
            return _decision_json(row)

    @router.post("/{run_id}/recompute")
    async def recompute_discovery(request: Request, run_id: int):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_RECOMPUTE"
        )
        if rejected is not None:
            return rejected
        sync = _truthy(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
        now = _now(request.app)
        week_key = china_week_key(now)
        factory = request.app.state.session_factory
        with factory() as session:
            source = _owned_run(session, run_id)
            if source is None:
                return _error(
                    404,
                    error="未找到",
                    code="DISCOVERY_NOT_FOUND",
                    detail=f"没有发现批次 {run_id}",
                )
            now = _now(request.app)
            week_key = china_week_key(now)
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
                if existing.replay_of_run_id != source.id:
                    return _error(
                        409,
                        error="本周批次冲突",
                        code="DISCOVERY_CURRENT_CONFLICT",
                        detail="本周已有其他当前发现批次",
                    )
                dispatch, failed = _acquire_queued_work(
                    request.app,
                    session,
                    existing,
                )
                if failed is not None:
                    return failed
                existing_id = existing.id
                if dispatch:
                    failed = _dispatch(
                        request.app, existing_id, dispatch, sync=sync
                    )
                    if failed is not None:
                        return failed
                    return _read_after_dispatch(
                        request.app, existing_id
                    )
                return _run_payload(session, existing)
            try:
                load_frozen_inputs(session, source.id)
                if not _is_build_available(
                    source.algorithm_version, AVAILABLE_BUILDS
                ):
                    raise ValueError("saved build unavailable")
                snapshots = session.scalars(
                    select(DiscoveryInputSnapshot)
                    .where(
                        DiscoveryInputSnapshot.run_id == source.id
                    )
                    .order_by(DiscoveryInputSnapshot.id)
                ).all()
                now = _now(request.app)
                week_key = china_week_key(now)
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
                    if existing.replay_of_run_id != source.id:
                        return _error(
                            409,
                            error="本周批次冲突",
                            code="DISCOVERY_CURRENT_CONFLICT",
                            detail="本周已有其他当前发现批次",
                        )
                    dispatch, failed = _acquire_queued_work(
                        request.app,
                        session,
                        existing,
                    )
                    if failed is not None:
                        return failed
                    existing_id = existing.id
                    if dispatch:
                        failed = _dispatch(
                            request.app,
                            existing_id,
                            dispatch,
                            sync=sync,
                        )
                        if failed is not None:
                            return failed
                        return _read_after_dispatch(
                            request.app,
                            existing_id,
                        )
                    return _run_payload(session, existing)
                replay = DiscoveryRun(
                    user_id=source.user_id,
                    week_key=week_key,
                    current=True,
                    status="queued",
                    created_at=now,
                    updated_at=now,
                    snapshot_at=source.snapshot_at,
                    data_version=source.data_version,
                    financial_version=source.financial_version,
                    valuation_version=source.valuation_version,
                    quality_discount_ruler_version_id=(
                        source.quality_discount_ruler_version_id
                    ),
                    deep_value_ruler_version_id=(
                        source.deep_value_ruler_version_id
                    ),
                    garp_ruler_version_id=source.garp_ruler_version_id,
                    algorithm_version=source.algorithm_version,
                    trading_dates_json=source.trading_dates_json,
                    ruler_payloads_json=source.ruler_payloads_json,
                    input_fingerprint=source.input_fingerprint,
                    error=None,
                    replay_of_run_id=source.id,
                )
                session.add(replay)
                session.flush()
                owner_token = acquire_discovery(session, replay.id)
                if owner_token is None:
                    session.rollback()
                    return _locked(session)
                replay, commit_at, rejection = _fresh_mutation_checkpoint(
                    request.app,
                    session,
                    replay.id,
                    owner_token=owner_token,
                )
                if rejection is not None:
                    return rejection
                assert replay is not None
                replay.created_at = commit_at
                replay.updated_at = commit_at
                session.add_all(
                    DiscoveryInputSnapshot(
                        run_id=replay.id,
                        code=row.code,
                        name=row.name,
                        industry=row.industry,
                        security_status=row.security_status,
                        fields_json=row.fields_json,
                        sources_json=row.sources_json,
                        as_of_json=row.as_of_json,
                        data_version=row.data_version,
                        financial_version=row.financial_version,
                        valuation_version=row.valuation_version,
                        ruler_versions_json=row.ruler_versions_json,
                        algorithm_version=row.algorithm_version,
                        input_fingerprint=row.input_fingerprint,
                        snapshot_at=row.snapshot_at,
                    )
                    for row in snapshots
                )
                session.add(
                    DiscoveryCommonStage(
                        run_id=replay.id,
                        attempt=1,
                        status="queued",
                        total_count=0,
                        pass_count=0,
                        drop_count=0,
                        insufficient_count=0,
                        error=None,
                        confirmed_at=None,
                        input_fingerprint=replay.input_fingerprint,
                        algorithm_version=replay.algorithm_version,
                        failure_kind=None,
                        superseded_at=None,
                        created_at=commit_at,
                        updated_at=commit_at,
                    )
                )
                session.commit()
                replay_id = replay.id
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                session.rollback()
                return _error(
                    409,
                    error="算法不可用",
                    code="algorithm_unavailable",
                    detail="保存构建、尺子或完整冻结输入不可用",
                )
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
                if (
                    winner is None
                    or winner.replay_of_run_id != source.id
                ):
                    return _error(
                        409,
                        error="本周批次冲突",
                        code="DISCOVERY_CURRENT_CONFLICT",
                        detail="本周已有其他当前发现批次",
                    )
                dispatch, failed = _acquire_queued_work(
                    request.app,
                    session,
                    winner,
                )
                if failed is not None:
                    return failed
                winner_id = winner.id
                if dispatch:
                    failed = _dispatch(
                        request.app, winner_id, dispatch, sync=sync
                    )
                    if failed is not None:
                        return failed
                    return _read_after_dispatch(
                        request.app, winner_id
                    )
                return _run_payload(session, winner)
        failed = _dispatch(
            request.app, replay_id, owner_token, sync=sync
        )
        if failed is not None:
            return failed
        return _read_after_dispatch(request.app, replay_id)

    @router.post("/{run_id}/abandon")
    async def abandon_discovery(request: Request, run_id: int):
        _payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_ABANDON"
        )
        if rejected is not None:
            return rejected
        now = _now(request.app)
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
            if (
                run.week_key == china_week_key(now)
                and (run.status == "abandoned" or not run.current)
            ):
                return _run_payload(session, run)
            immutable = _mutation_guard(run, now)
            if immutable is not None:
                return immutable
            owner_token = acquire_discovery(session, run.id)
            if owner_token is None:
                session.rollback()
                return _locked(session)
            run, commit_at, rejection = _fresh_mutation_checkpoint(
                request.app,
                session,
                run.id,
                owner_token=owner_token,
            )
            if rejection is not None:
                return rejection
            assert run is not None
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
                    updated_at=commit_at,
                )
            )
            if int(changed.rowcount or 0) != 1:
                session.rollback()
                session.expire_all()
                refreshed = _owned_run(session, run_id)
                if (
                    refreshed is not None
                    and refreshed.week_key == china_week_key(now)
                ):
                    return _run_payload(session, refreshed)
                if refreshed is None:
                    return _error(
                        404,
                        error="未找到",
                        code="DISCOVERY_NOT_FOUND",
                        detail=f"没有发现批次 {run_id}",
                    )
                rejection = _mutation_guard(refreshed, now)
                assert rejection is not None
                return rejection
            session.commit()
        _release_committed_lock(factory, run_id, owner_token)
        return _read_after_dispatch(request.app, run_id)

    return router

