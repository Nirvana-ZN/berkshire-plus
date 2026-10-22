from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from math import isfinite
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
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
from discovery.evidence import (
    PersistedEvidenceItem,
    verify_canonical_evidence_card,
)
from discovery.summaries import DiscoverySummaryError, build_stage_summary
from discovery.snapshot import (
    ProductionSnapshot,
    canonical_json,
    freeze_discovery_ruler_payloads,
    freeze_inputs,
    load_frozen_inputs,
)
from jobs.discovery import drain_discovery_subprocess, drain_run_queue
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
            else _common_stage_json(common)
            | _stage_token_json(session, common)
            | {"created": True}
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
                else _track_stage_json(row)
                | _stage_token_json(session, row)
                | {"created": True}
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


def _stage_mutation_version(
    session: Session,
    row: DiscoveryCommonStage | DiscoveryStage,
) -> str:
    overrides: list[dict[str, object]] = []
    if isinstance(row, DiscoveryStage):
        overrides = [
            {
                "id": override.id,
                "code": override.code,
                "action": override.action,
                "reason": override.reason,
            }
            for override in session.scalars(
                select(DiscoveryOverride)
                .where(
                    DiscoveryOverride.run_id == row.run_id,
                    DiscoveryOverride.track == row.track,
                    DiscoveryOverride.stage == row.stage,
                    DiscoveryOverride.attempt == row.attempt,
                )
                .order_by(DiscoveryOverride.code, DiscoveryOverride.id)
            ).all()
        ]
    value = canonical_json(
        {
            "id": row.id,
            "attempt": row.attempt,
            "status": row.status,
            "input_fingerprint": row.input_fingerprint,
            "ruler_version_id": (
                row.ruler_version_id
                if isinstance(row, DiscoveryStage)
                else None
            ),
            "algorithm_version": row.algorithm_version,
            "superseded_at": _iso(row.superseded_at),
            "stage_revision": _stage_revision(row),
            "overrides": overrides,
        }
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stage_token_json(
    session: Session,
    row: DiscoveryCommonStage | DiscoveryStage,
) -> dict[str, object]:
    return {
        "ruler_version_id": (
            row.ruler_version_id
            if isinstance(row, DiscoveryStage)
            else None
        ),
        "stage_revision": _stage_revision(row),
        "mutation_version": _stage_mutation_version(session, row),
    }


def _stage_revision(
    row: DiscoveryCommonStage | DiscoveryStage,
) -> int:
    updated_at = row.updated_at.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = updated_at - epoch
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )


def _next_stage_updated_at(
    row: DiscoveryCommonStage | DiscoveryStage,
    candidate: datetime,
) -> datetime:
    current = row.updated_at.astimezone(timezone.utc)
    candidate = candidate.astimezone(timezone.utc)
    return (
        candidate
        if candidate > current
        else current + timedelta(microseconds=1)
    )


_STAGE_EXPECTATION_KEYS = (
    "expected_stage_id",
    "expected_stage_attempt",
    "expected_stage_status",
    "expected_input_fingerprint",
    "expected_ruler_version_id",
    "expected_stage_revision",
    "expected_mutation_version",
)


def _has_stage_expectation(payload: Mapping[str, object]) -> bool:
    return any(key in payload for key in _STAGE_EXPECTATION_KEYS)


def _stage_precondition_rejection(
    session: Session,
    payload: Mapping[str, object],
    row: DiscoveryCommonStage | DiscoveryStage,
) -> JSONResponse | None:
    supplied = [key in payload for key in _STAGE_EXPECTATION_KEYS]
    if not any(supplied):
        # Legacy callers did not claim to act on a reviewed representation.
        return None
    expected_ruler = payload.get("expected_ruler_version_id")
    if (
        not all(supplied)
        or type(payload.get("expected_stage_id")) is not int
        or type(payload.get("expected_stage_attempt")) is not int
        or type(payload.get("expected_stage_status")) is not str
        or type(payload.get("expected_input_fingerprint")) is not str
        or (
            expected_ruler is not None
            and type(expected_ruler) is not int
        )
        or type(payload.get("expected_stage_revision")) is not int
        or type(payload.get("expected_mutation_version")) is not str
        or not payload["expected_input_fingerprint"]
        or not payload["expected_mutation_version"]
    ):
        return _error(
            400,
            error="阶段版本无效",
            code="INVALID_DISCOVERY_STAGE_VERSION",
            detail="阶段乐观并发字段必须完整且类型正确",
        )
    ruler_version_id = (
        row.ruler_version_id if isinstance(row, DiscoveryStage) else None
    )
    active = (
        _latest_track(session, row.run_id, row.track, row.stage)
        if isinstance(row, DiscoveryStage)
        else _latest_common(session, row.run_id)
    )
    actual_version = _stage_mutation_version(session, row)
    if (
        active is None
        or active.id != row.id
        or payload["expected_stage_id"] != row.id
        or payload["expected_stage_attempt"] != row.attempt
        or payload["expected_stage_status"] != row.status
        or payload["expected_input_fingerprint"] != row.input_fingerprint
        or expected_ruler != ruler_version_id
        or payload["expected_stage_revision"] != _stage_revision(row)
        or payload["expected_mutation_version"] != actual_version
    ):
        return _error(
            409,
            error="阶段已变化",
            code="DISCOVERY_STAGE_VERSION_MISMATCH",
            detail="当前活动阶段与客户端复核版本不一致",
            current_stage_id=row.id,
            current_stage_attempt=row.attempt,
            current_stage_status=row.status,
            current_stage_revision=_stage_revision(row),
            current_mutation_version=actual_version,
        )
    return None


def _run_precondition_rejection(
    payload: Mapping[str, object],
    run: DiscoveryRun,
) -> JSONResponse | None:
    supplied = (
        "expected_run_id" in payload,
        "expected_week_key" in payload,
    )
    if not any(supplied):
        return None
    if (
        not all(supplied)
        or type(payload.get("expected_run_id")) is not int
        or type(payload.get("expected_week_key")) is not str
    ):
        return _error(
            400,
            error="批次版本无效",
            code="INVALID_DISCOVERY_RUN_VERSION",
            detail="批次乐观并发字段必须完整且类型正确",
        )
    if (
        payload["expected_run_id"] != run.id
        or payload["expected_week_key"] != run.week_key
        or not run.current
        or run.status == "abandoned"
    ):
        return _error(
            409,
            error="批次已变化",
            code="DISCOVERY_RUN_VERSION_MISMATCH",
            detail="当前批次与客户端恢复目标不一致",
            current_run_id=run.id,
            current_week_key=run.week_key,
        )
    return None


def _stage_claim_conflict() -> JSONResponse:
    return _error(
        409,
        error="阶段已变化",
        code="DISCOVERY_STAGE_VERSION_MISMATCH",
        detail="阶段版本已被其他会话更新",
    )


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
    *,
    include_metrics: bool = True,
) -> dict[str, object]:
    verdict = (
        row.verdict
        if override is None
        else ("pass" if override.action == "forced_pass" else "drop")
    )
    # Full-market drop/insufficient rows are thousands of metric blobs.
    # The summary still reads every metric. The table only needs them for passes.
    stored_metrics = _metrics(row.metrics_json)
    metrics = dict(stored_metrics) if include_metrics else {}
    return {
        "code": row.code,
        "name": name,
        "verdict": verdict,
        "original_verdict": row.verdict,
        "reason": row.reason,
        "metrics": metrics,
        "selection_audit": {
            "operating_state": stored_metrics.get("operating_state"),
            "ranking_audit": stored_metrics.get("ranking_audit"),
            "missing": [f"{key}: {item.get('reason') or item.get('status')}"
                        for key, item in stored_metrics.get("metric_results", {}).items()
                        if isinstance(item, Mapping) and item.get("status") not in ("verified", "not_applicable")],
        } if "metric_results" in stored_metrics or "ranking_audit" in stored_metrics else None,
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


def _selection_change_labels(before, after, change):
    valuation = {'pe_deducted_ttm', 'deducted_peg', 'earnings_yield', 'normalized_pe',
                 'usable_net_cash_to_mcap', 'fcf_yield'}
    left, right = before.get('metric_results', {}), after.get('metric_results', {})
    changed = {key for key in left.keys() | right.keys() if left.get(key) != right.get(key)}
    # Existing bank metrics remain persisted top-level proxies, outside the
    # nonfinancial metric-results registry; compare those saved values too.
    bank_valuation = any(before.get(key) != after.get(key) for key in ('pb_percentile', 'pb', 'pb_tangible'))
    bank_fundamentals = any(before.get(key) != after.get(key) for key in
        ('roe_mean', 'bank_core_tier1_capital_adequacy', 'npl_change'))
    return [label for label, applies in (
        ('fundamental_changed', bool(changed - valuation) or bank_fundamentals
         or before.get('operating_state') != after.get('operating_state')),
        ('valuation_changed', bool(changed & valuation) or bank_valuation),
        ('rule_changed', change.cause in ('rule_changed', 'both_changed')),
        ('cap_excluded', change.current_reason in ('industry_cap', 'total_cap')),
    ) if applies]


def _change_json(session, change):
    value = change.as_dict()
    metrics = []
    for identity in (change.previous_run_id, change.current_run_id):
        if identity is None:
            return value
        run = session.get(DiscoveryRun, identity)
        if run is None or run.algorithm_version != 'discovery-v8':
            return value
        stage = session.scalar(select(DiscoveryStage).where(DiscoveryStage.run_id == identity,
            DiscoveryStage.track == change.track, DiscoveryStage.stage == change.stage,
            DiscoveryStage.superseded_at.is_(None)).order_by(DiscoveryStage.attempt.desc()))
        if stage is None:
            return value
        row = session.scalar(select(DiscoveryResult).where(DiscoveryResult.run_id == identity,
            DiscoveryResult.track == change.track, DiscoveryResult.stage == change.stage,
            DiscoveryResult.attempt == stage.attempt, DiscoveryResult.code == change.code))
        if row is None:
            return value
        metrics.append(_metrics(row.metrics_json))
    value['selection_changes'] = _selection_change_labels(*metrics, change)
    return value


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
    # ponytail: a full-market stage is ~3.5k metric blobs (~25MB) and the
    # page aborts the read at 5s. At or below this many rows, keep every
    # metric. Above it, only pass rows carry metrics; the summary still
    # covers the rest. Upgrade path: paginate the stage table.
    keep_all_metrics = len(rows) <= 200

    def effective(row: object) -> str:
        if row.code not in override_rows:
            return row.verdict
        return (
            "pass"
            if override_rows[row.code].action == "forced_pass"
            else "drop"
        )

    return {
        verdict: [
            _result_json(
                row,
                _frozen_name(names, row.code),
                override_rows.get(row.code),
                include_metrics=keep_all_metrics or effective(row) == "pass",
            )
            for row in rows
            if effective(row) == verdict
        ]
        for verdict in ("pass", "drop", "insufficient")
    }


def _selection_partition(session, run, stage, rows, names, overrides):
    """Attach only already persisted gate verdicts and frozen evidence cutoff."""
    partition = _partition(rows, names, overrides)
    if run.algorithm_version != 'discovery-v8':
        for group in partition.values():
            for item in group:
                item['selection_audit'] = None
        return partition
    snapshots = {row.code: row for row in session.scalars(select(DiscoveryInputSnapshot).where(
        DiscoveryInputSnapshot.run_id == run.id))}
    gate = stage if stage.stage == 'track_gate' else (
        session.get(DiscoveryStage, stage.predecessor_stage_id) if stage.predecessor_stage_id else None)
    if gate is not None and gate.stage == 'competition':
        gate = session.get(DiscoveryStage, gate.predecessor_stage_id)
    gate_rows = {} if gate is None else {row.code: row for row in session.scalars(
        select(DiscoveryResult).where(DiscoveryResult.run_id == run.id, DiscoveryResult.track == stage.track,
            DiscoveryResult.stage == 'track_gate', DiscoveryResult.attempt == gate.attempt))}
    gate_overrides = {} if gate is None else {row.code: row for row in session.scalars(
        select(DiscoveryOverride).where(DiscoveryOverride.run_id == run.id, DiscoveryOverride.track == stage.track,
            DiscoveryOverride.stage == 'track_gate', DiscoveryOverride.attempt == gate.attempt))}
    labels = {'pass': '通过', 'drop': '未通过', 'insufficient': '证据不足'}
    for group in partition.values():
        for item in group:
            audit = item['selection_audit']
            if audit is None:
                audit = item['selection_audit'] = {'missing': ['selection_audit_unavailable']}
            code = item['code']
            original = gate_rows.get(code)
            override = gate_overrides.get(code)
            verdict = original.verdict if original is not None else None
            if override is not None:
                verdict = 'pass' if override.action == 'forced_pass' else 'drop'
            audit['long_term_eligibility'] = labels.get(verdict, '不可用')
            snapshot = snapshots.get(code)
            dates = _persisted_json(snapshot.as_of_json) if snapshot is not None else {}
            audit['evidence_at'] = dates.get('decision_cutoff_at')
    return partition


# A finished stage's results never change (a retry is a new stage row), but
# summarizing a full-market stage re-validates ~440k decimals (~4 s); a page
# load reads every stage, so recomputing starves /current past its timeout.
# ponytail: process-wide and never evicted; one summary per stage attempt.
_STAGE_SUMMARIES: dict[tuple[object, ...], dict[str, object]] = {}


def _stage_summary(
    session: Session,
    stage: DiscoveryCommonStage | DiscoveryStage,
    rows: list,
    overrides: list | tuple = (),
    ruler: RulerVersion | None = None,
) -> dict[str, object]:
    if stage.status in ("queued", "running"):
        return build_stage_summary(stage, rows, overrides, ruler)
    key = (
        str(session.get_bind().url),
        type(stage).__name__,
        stage.id,
        stage.status,
        stage.updated_at,
        None if ruler is None else ruler.id,
        tuple((row.id, row.action) for row in overrides),
    )
    summary = _STAGE_SUMMARIES.get(key)
    if summary is None:
        summary = build_stage_summary(stage, rows, overrides, ruler)
        _STAGE_SUMMARIES[key] = summary
    return summary


def warm_current_stage_summaries(app) -> None:
    """Summarize this week's stages before the first page load asks for them."""
    with app.state.session_factory() as session:
        run = session.scalar(
            select(DiscoveryRun)
            .where(
                DiscoveryRun.user_id == LOCAL_USER_ID,
                DiscoveryRun.week_key == china_week_key(_now(app)),
                DiscoveryRun.current.is_(True),
            )
            .order_by(DiscoveryRun.id.desc())
        )
        if run is None:
            return
        # A corrupt stage reports itself when the page asks for it.
        unreadable = (CorruptDiscoveryResult, DiscoverySummaryError)
        with suppress(*unreadable):
            _common_payload(session, run)
        for track in TRACKS:
            for stage in TRACK_STAGES:
                with suppress(*unreadable):
                    _track_payload(session, run, track, stage)


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
        summary = _stage_summary(session, stage, rows)
        summaries[stage.id] = summary
        attempts.append(
            _common_stage_json(stage)
            | _stage_token_json(session, stage)
            | _partition(rows, names)
            | {"summary": summary}
        )
    current_rows = by_attempt.get(current.attempt, [])
    current_summary = summaries[current.id]
    return {
        "run": _run_payload(session, run),
        "current": _common_stage_json(current)
        | _stage_token_json(session, current)
        | {"summary": current_summary},
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
        summary = _stage_summary(
            session,
            attempt,
            rows,
            attempt_override_rows,
            rulers_by_id.get(attempt.ruler_version_id),
        )
        summaries[attempt.id] = summary
        attempts.append(
            _track_stage_json(attempt)
            | _stage_token_json(session, attempt)
            | _selection_partition(session, run, attempt, rows, names, attempt_overrides)
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
        "current": _track_stage_json(current)
        | _stage_token_json(session, current)
        | {"summary": current_summary},
        "attempts": attempts,
        **_selection_partition(session, run, current, current_rows, names, current_overrides),
        "overrides": [
            _override_json(row) for row in current_override_rows
        ],
        "decisions": [_decision_json(row) for row in decision_rows],
        "evidence": [_evidence_json(row) for row in evidence_rows],
        "summary": current_summary,
        "changes": [_change_json(session, change) for change in changes],
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


# ponytail: one job object for the process. KILL_ON_JOB_CLOSE reaps drain
# children when this process exits. If the job cannot be created, an orphan
# can keep writing until the next startup clears the stale lock.
_DRAIN_JOB: list[int] = []


def _assign_kill_on_close(pid: int | None) -> None:
    if os.name != "nt" or not pid:
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not _DRAIN_JOB:
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return

            class _Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _Io(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class _Extended(ctypes.Structure):
                _fields_ = [
                    ("Basic", _Basic),
                    ("IoInfo", _Io),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = _Extended()
            info.Basic.LimitFlags = 0x2000
            if not kernel32.SetInformationJobObject(
                job,
                9,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(job)
                return
            _DRAIN_JOB.append(job)
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)
        if process:
            kernel32.AssignProcessToJobObject(_DRAIN_JOB[0], process)
            kernel32.CloseHandle(process)
    except Exception:
        logger.warning("发现子进程未能绑定父进程退出", exc_info=True)


def _drain_out_of_process(app, run_id: int, owner_token: str) -> None:
    import multiprocessing

    db_path = app.state.engine.url.database
    if type(db_path) is not str or not db_path:
        logger.error("发现队列无法脱离 API 进程：数据库路径不可用")
        return
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(
        target=drain_discovery_subprocess,
        args=(db_path, run_id, owner_token),
    )
    process.start()
    _assign_kill_on_close(process.pid)
    process.join()
    if process.exitcode != 0:
        logger.error(
            "发现队列子进程退出 run=%s code=%s",
            run_id,
            process.exitcode,
        )


def _drain(app, run_id: int, owner_token: str) -> None:
    if getattr(app.state, "discovery_offload_drain", False):
        try:
            _drain_out_of_process(app, run_id, owner_token)
            if hasattr(app.state, "session_factory"):
                warm_current_stage_summaries(app)
        except Exception:
            logger.exception("发现队列执行失败")
        return
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


def _researchable_competition_entrants(
    session: Session, competition: DiscoveryStage
) -> list[DiscoveryResult]:
    """Return effective pass rows backed by a canonical frozen-input card."""

    evidence_by_code: dict[str, list[DiscoveryEvidence]] = {}
    for item in session.scalars(
        select(DiscoveryEvidence)
        .where(
            DiscoveryEvidence.run_id == competition.run_id,
            DiscoveryEvidence.track == competition.track,
            DiscoveryEvidence.stage == "competition",
            DiscoveryEvidence.competition_stage_id == competition.id,
            DiscoveryEvidence.competition_attempt == competition.attempt,
        )
        .order_by(
            DiscoveryEvidence.code,
            DiscoveryEvidence.kind,
            DiscoveryEvidence.rule_id,
            DiscoveryEvidence.id,
        )
    ):
        evidence_by_code.setdefault(item.code, []).append(item)
    rows = _effective_competition_rows(session, competition)
    try:
        frozen = load_frozen_inputs(
            session,
            competition.run_id,
            codes=(row.code for row, _override in rows),
        )
        ruler_entry = frozen.ruler_payloads.get(competition.track)
        if (
            not isinstance(ruler_entry, Mapping)
            or ruler_entry.get("id") != competition.ruler_version_id
            or not isinstance(ruler_entry.get("payload"), Mapping)
            or frozen.ruler_version_ids.get(competition.track)
            != competition.ruler_version_id
        ):
            return []
        ruler = ruler_entry["payload"]
    except (KeyError, TypeError, ValueError):
        return []
    companies = {company.code: company for company in frozen.companies}
    valid_codes: set[str] = set()
    for row, _override in rows:
        company = companies.get(row.code)
        if company is None:
            continue
        try:
            metrics = _persisted_json(row.metrics_json)
            if type(metrics) is not dict:
                continue
            items = [
                PersistedEvidenceItem(
                    code=item.code,
                    ruler_version_id=item.ruler_version_id,
                    rule_version=item.rule_version,
                    kind=item.kind,
                    rule_id=item.rule_id,
                    input_fields=_persisted_json(item.input_fields_json),
                    display_text=item.display_text,
                    as_of=item.as_of,
                    data_completeness=item.data_completeness,
                )
                for item in evidence_by_code.get(row.code, [])
            ]
        except (CorruptDiscoveryResult, TypeError, ValueError):
            continue
        if (
            sum(item.kind == "fact" for item in items) >= 3
            and verify_canonical_evidence_card(
                track=competition.track,
                ruler=ruler,
                ruler_version_id=competition.ruler_version_id,
                code=row.code,
                metrics=metrics,
                verdict=row.verdict,
                rank=row.rank,
                reason=row.reason,
                company_code=company.code,
                company_industry=company.industry,
                source_as_of={
                    key: company.as_of.get(key)
                    for key in ("security", "financial", "valuation")
                },
                items=items,
            )
        ):
            valid_codes.add(row.code)

    return [
        row
        for row, override in rows
        if (
            row.verdict
            if override is None
            else ("pass" if override.action == "forced_pass" else "drop")
        )
        == "pass"
        and row.reason != "evidence_facts_insufficient"
        and row.code in valid_codes
    ]


def _discovery_research_name(
    session: Session,
    run: DiscoveryRun,
    *,
    track: str,
    code: str,
) -> str | None:
    adjudication = _latest_track(session, run.id, track, "adjudication")
    if adjudication is None:
        return _confirmed_competition_name(session, run, track=track, code=code)
    if adjudication.status not in {"confirmed", "insufficient_candidates"}:
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
    eligible_codes = {
        row.code
        for row in _researchable_competition_entrants(
            session, competition
        )
    }
    frozen = session.scalar(
        select(DiscoveryInputSnapshot).where(
            DiscoveryInputSnapshot.run_id == run.id,
            DiscoveryInputSnapshot.code == code,
        )
    )
    if (
        final_result is None
        or decision is None
        or code not in eligible_codes
        or frozen is None
    ):
        return None
    return frozen.name


def _confirmed_competition_name(
    session: Session,
    run: DiscoveryRun,
    *,
    track: str,
    code: str,
) -> str | None:
    competition = _latest_track(session, run.id, track, "competition")
    if (
        not isinstance(competition, DiscoveryStage)
        or competition.status != "confirmed"
        or code
        not in {
            row.code
            for row in _researchable_competition_entrants(session, competition)
        }
    ):
        return None
    frozen = session.scalar(
        select(DiscoveryInputSnapshot).where(
            DiscoveryInputSnapshot.run_id == run.id,
            DiscoveryInputSnapshot.code == code,
        )
    )
    if frozen is None or not frozen.name.strip():
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
                            DiscoveryCommonStage.status.in_(
                                ("queued", "waiting_confirmation")
                            ),
                            DiscoveryCommonStage.superseded_at.is_(None),
                        )
                        .scalar_subquery()
                        + select(func.count())
                        .select_from(DiscoveryStage)
                        .where(
                            DiscoveryStage.run_id == DiscoveryRun.id,
                            DiscoveryStage.status.in_(
                                ("queued", "waiting_confirmation")
                            ),
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


def _data_not_ready_detail(error: BaseException) -> str:
    known = {
        "latest discovery data fill is not ready": (
            "全市场数据快照尚未完成，还不能开始本周发现"
        ),
        "discovery data is being mutated": (
            "全市场数据正在更新，完成前不能开始本周发现"
        ),
    }
    return known.get(str(error), str(error))


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
            existing = _existing_discovery_research(
                session, run.id, code
            )
            if existing is not None:
                return _research_job_payload(existing)
        if not has_env_value(
            request.app.state.env_path, OPENAI_API_KEY
        ):
            with factory() as session:
                existing = _existing_discovery_research(
                    session, run_id, code
                )
                if existing is not None:
                    return _research_job_payload(existing)
            return _error(
                400,
                error="未配置模型密钥",
                code="NO_API_KEY",
                detail="评估需要模型密钥，请到设置页配置",
            )
        sync = _truthy(payload.get("sync")) and bool(
            getattr(request.app.state, "allow_sync", False)
        )
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
                        detail="仅可评估通过轨道筛选的公司",
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
                        detail="仅可评估通过轨道筛选的公司",
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
                        DiscoveryRun.status != "abandoned",
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
        # The full-market snapshot takes minutes of CPU; on the event loop it
        # stalls every other request, including the page's 5 s polls.
        return await run_in_threadpool(create_discovery_blocking, request, payload)

    def create_discovery_blocking(request: Request, payload: Mapping):
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
                    detail=_data_not_ready_detail(error),
                )
        dispatch_error = _dispatch(
            request.app, run_id, owner_token, sync=sync
        )
        if dispatch_error is not None:
            return dispatch_error
        return _read_after_dispatch(request.app, run_id)

    def _rule_only(session: Session, run_id: int):
        run = _owned_run(session, run_id)
        if run is None:
            return _error(
                404,
                error="未找到",
                code="DISCOVERY_NOT_FOUND",
                detail=f"没有发现批次 {run_id}",
            )
        return _error(
            409,
            error="发现结果不可人工改写",
            code="DISCOVERY_RULE_ONLY",
            detail="每周发现只发布尺子结果，不能覆盖、裁决或手工确认",
        )

    @router.post("/{run_id}/overrides")
    async def reject_override(request: Request, run_id: int):
        with request.app.state.session_factory() as session:
            return _rule_only(session, run_id)

    @router.post("/{run_id}/decisions")
    async def reject_decision(request: Request, run_id: int):
        with request.app.state.session_factory() as session:
            return _rule_only(session, run_id)

    @router.post("/{run_id}/stages/safety/confirm")
    async def reject_safety_confirm(request: Request, run_id: int):
        with request.app.state.session_factory() as session:
            return _rule_only(session, run_id)

    @router.post("/{run_id}/tracks/{track}/stages/{stage}/confirm")
    async def reject_track_confirm(
        request: Request, run_id: int, track: str, stage: str
    ):
        del track, stage
        with request.app.state.session_factory() as session:
            return _rule_only(session, run_id)

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

    @router.post("/{run_id}/stages/safety/retry")
    async def retry_safety(request: Request, run_id: int):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_RETRY"
        )
        if rejected is not None:
            return rejected
        return await run_in_threadpool(retry_safety_blocking, request, run_id, payload)

    def retry_safety_blocking(request: Request, run_id: int, payload: Mapping):
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
            if observed is not None:
                stale = _stage_precondition_rejection(
                    session, payload, observed
                )
                if stale is not None:
                    return stale
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
            stale = _stage_precondition_rejection(
                session, payload, current
            )
            if stale is not None:
                session.rollback()
                return stale
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
                stale = _stage_precondition_rejection(
                    session, payload, current
                )
                if stale is not None:
                    session.rollback()
                    return stale
                expected_updated_at = current.updated_at
                changed = session.execute(
                    update(DiscoveryCommonStage)
                    .where(
                        DiscoveryCommonStage.id == current.id,
                        DiscoveryCommonStage.attempt == current.attempt,
                        DiscoveryCommonStage.input_fingerprint
                        == current.input_fingerprint,
                        DiscoveryCommonStage.updated_at
                        == expected_updated_at,
                        DiscoveryCommonStage.status == "failed",
                        DiscoveryCommonStage.superseded_at.is_(None),
                    )
                    .values(
                        superseded_at=commit_at,
                        updated_at=_next_stage_updated_at(
                            current, commit_at
                        ),
                    )
                )
                if int(changed.rowcount or 0) != 1:
                    session.rollback()
                    if _has_stage_expectation(payload):
                        return _stage_claim_conflict()
                    refreshed_run = _owned_run(session, run_id)
                    assert refreshed_run is not None
                    return _run_payload(session, refreshed_run)
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

    @router.post("/{run_id}/tracks/{track}/stages/{stage}/retry")
    async def retry_track_stage(
        request: Request, run_id: int, track: str, stage: str
    ):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_RETRY"
        )
        if rejected is not None:
            return rejected
        return await run_in_threadpool(
            retry_track_stage_blocking, request, run_id, track, stage, payload
        )

    def retry_track_stage_blocking(
        request: Request, run_id: int, track: str, stage: str, payload: Mapping
    ):
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
            if observed is not None:
                stale = _stage_precondition_rejection(
                    session, payload, observed
                )
                if stale is not None:
                    return stale
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
            stale = _stage_precondition_rejection(
                session, payload, current
            )
            if stale is not None:
                session.rollback()
                return stale
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
                stale = _stage_precondition_rejection(
                    session, payload, current
                )
                if stale is not None:
                    session.rollback()
                    return stale
                expected_updated_at = current.updated_at
                changed = session.execute(
                    update(DiscoveryStage)
                    .where(
                        DiscoveryStage.id == current.id,
                        DiscoveryStage.attempt == current.attempt,
                        DiscoveryStage.input_fingerprint
                        == current.input_fingerprint,
                        DiscoveryStage.ruler_version_id
                        == current.ruler_version_id,
                        DiscoveryStage.updated_at == expected_updated_at,
                        DiscoveryStage.status == "failed",
                        DiscoveryStage.superseded_at.is_(None),
                    )
                    .values(
                        superseded_at=commit_at,
                        updated_at=_next_stage_updated_at(
                            current, commit_at
                        ),
                    )
                )
                if int(changed.rowcount or 0) != 1:
                    session.rollback()
                    if _has_stage_expectation(payload):
                        return _stage_claim_conflict()
                    refreshed_run = _owned_run(session, run_id)
                    assert refreshed_run is not None
                    return _run_payload(session, refreshed_run)
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

    @router.post("/{run_id}/recompute")
    async def recompute_discovery(request: Request, run_id: int):
        payload, rejected = await _object_body(
            request, "INVALID_DISCOVERY_RECOMPUTE"
        )
        if rejected is not None:
            return rejected
        return await run_in_threadpool(recompute_blocking, request, run_id, payload)

    def recompute_blocking(request: Request, run_id: int, payload: Mapping):
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
        payload, rejected = await _object_body(
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
            stale = _run_precondition_rejection(payload, run)
            if stale is not None:
                return stale
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
            stale = _run_precondition_rejection(payload, run)
            if stale is not None:
                session.rollback()
                return stale
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

