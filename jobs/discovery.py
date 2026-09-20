"""Serialized discovery stage execution.

Run aggregate status remains derived by the workflow/API layer; this worker
only owns the exact stage attempt it claims.

Track executors are pure internal functions: they receive immutable persisted
inputs and return immutable DTOs. They never receive a Session, Connection, or
other database handle; this module alone validates and persists their output.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
from math import isfinite
import sys

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.discovery_models import (
    DiscoveryCommonResult,
    DiscoveryCommonStage,
    DiscoveryEvidence,
    DiscoveryOverride,
    DiscoveryResult,
    DiscoveryRun,
    DiscoveryStage,
)
from app.models import JobLock
from discovery.aggregate import aggregate_run_status
from discovery.common import CommonVerdict, classify_common
from discovery.constants import (
    TRACKS,
    VERDICTS,
    NowProvider,
    aware_utc_now,
    china_week_key,
    utc_now,
)
from discovery.snapshot import (
    FrozenRunInputs,
    FrozenValue,
    canonical_json,
    load_frozen_inputs,
)
from discovery.rulers import quality_discount_ruler
from discovery.tracks.quality_discount import (
    bounded_ratio,
    classify as classify_quality_discount,
    decimal_string,
)
from jobs.lock import (
    JOB_TYPE_DISCOVERY,
    acquire_discovery,
    release,
)


_classify_common = classify_common


class TransientDiscoveryError(RuntimeError):
    """An explicitly retryable error for unchanged frozen inputs."""


class PermanentDiscoveryError(ValueError):
    """A snapshot, data, or business invariant failure."""


class QueueStalledError(RuntimeError):
    """An executor returned without advancing its selected stage."""


class TrackExecutorContractError(PermanentDiscoveryError):
    """The injected track executor violated drain-owned transactions."""


class DiscoveryLockLostError(RuntimeError):
    """The drain no longer owns its exact committed global lock."""


@dataclass(frozen=True)
class StageExecutionOutcome:
    stage_id: int
    status: str
    claimed: bool
    processed: int = 0


@dataclass(frozen=True)
class DrainOutcome:
    run_id: int
    acquired: bool
    executed: int


@dataclass(frozen=True)
class TrackExecutionCandidate:
    code: str
    persisted_verdict: str
    effective_verdict: str
    reason: str | None
    metrics_json: str
    rank: int | None
    override_action: str | None = None
    override_reason: str | None = None


@dataclass(frozen=True)
class TrackExecutionContext:
    stage_id: int
    run_id: int
    track: str
    stage: str
    attempt: int
    input_fingerprint: str
    ruler_version_id: int
    algorithm_version: str
    common_stage_id: int | None
    predecessor_stage_id: int | None
    predecessor_stage: str | None
    frozen_inputs: FrozenRunInputs
    ruler_payload: Mapping[str, FrozenValue]
    candidates: tuple[TrackExecutionCandidate, ...]

    @property
    def candidate_codes(self) -> tuple[str, ...]:
        return tuple(candidate.code for candidate in self.candidates)


@dataclass(frozen=True)
class TrackExecutionResult:
    code: str
    verdict: str
    reason: str | None
    metrics_json: str
    rank: int | None


@dataclass(frozen=True)
class TrackExecutionEvidence:
    code: str
    rule_version: str
    kind: str
    rule_id: str
    input_fields_json: str
    display_text: str
    as_of: str
    data_completeness: str


@dataclass(frozen=True)
class TrackExecutionOutcome:
    status: str
    results: tuple[TrackExecutionResult, ...] = ()
    evidence: tuple[TrackExecutionEvidence, ...] = ()
    error: str | None = None
    failure_kind: str | None = None


TrackExecutor = Callable[[TrackExecutionContext], TrackExecutionOutcome]


def builtin_track_executor(
    context: TrackExecutionContext,
) -> TrackExecutionOutcome:
    """Dispatch only production track gates implemented by this build."""

    if context.track != "quality_discount" or context.stage != "track_gate":
        return TrackExecutionOutcome(
            status="ruler_not_ready",
            error="当前构建尚未实现该轨道阶段",
        )
    try:
        ruler = quality_discount_ruler(context.ruler_payload)
    except (TypeError, ValueError):
        return TrackExecutionOutcome(
            status="ruler_not_ready",
            error="冻结的质量折价尺子不完整或不兼容",
        )
    companies = {
        company.code: company for company in context.frozen_inputs.companies
    }
    if (
        len(companies) != len(context.frozen_inputs.companies)
        or len(context.candidate_codes) != len(set(context.candidate_codes))
        or not set(context.candidate_codes) <= set(companies)
    ):
        raise TrackExecutorContractError(
            "quality gate candidates do not match frozen companies"
        )
    verdicts = tuple(
        classify_quality_discount(companies[candidate.code], ruler)
        for candidate in context.candidates
    )
    complete = sum(verdict.verdict != "insufficient" for verdict in verdicts)
    coverage = bounded_ratio(complete, len(verdicts)) if verdicts else Decimal("1")
    if coverage is None:
        raise TrackExecutorContractError("quality coverage ratio is unavailable")
    required = Decimal(ruler.thresholds["readiness_min"])
    if coverage < required:
        return TrackExecutionOutcome(
            status="data_not_ready",
            error=(
                "quality_discount coverage "
                f"{decimal_string(coverage)} is below approved "
                f"{ruler.thresholds['readiness_min']}"
            ),
        )
    return TrackExecutionOutcome(
        status="waiting_confirmation",
        results=tuple(
            TrackExecutionResult(
                code=verdict.code,
                verdict=verdict.verdict,
                reason=verdict.primary_reason,
                metrics_json=canonical_json(verdict.metrics),
                rank=None,
            )
            for verdict in verdicts
        ),
    )


def _now(now_provider: NowProvider = utc_now) -> datetime:
    return aware_utc_now(now_provider)


def _run_is_mutable(
    run: DiscoveryRun | None,
    now_provider: NowProvider,
) -> bool:
    return (
        run is not None
        and run.current is True
        and run.status != "abandoned"
        and run.week_key == china_week_key(now_provider=now_provider)
    )


def _safe_error(error: BaseException) -> str:
    message = str(error).strip()
    return (message or type(error).__name__)[:2000]


def _failure_kind(error: BaseException) -> str:
    if isinstance(
        error,
        (
            TransientDiscoveryError,
            InterruptedError,
            TimeoutError,
            OperationalError,
        ),
    ):
        return "transient"
    return "permanent"


def _verify_run_lock(session: Session, run_id: int) -> None:
    if session.info.get("discovery_lock_run_id") != run_id:
        return
    lock = session.scalar(
        select(JobLock)
        .where(JobLock.id == 1)
        .execution_options(populate_existing=True)
    )
    if (
        lock is None
        or lock.job_id != run_id
        or lock.job_type != JOB_TYPE_DISCOVERY
        or lock.owner_token != session.info.get("discovery_lock_owner_token")
    ):
        raise DiscoveryLockLostError("discovery run lock ownership was lost")


def _commit_if_current(
    session: Session,
    run_id: int,
    now_provider: NowProvider,
) -> bool:
    if not _run_is_mutable(
        session.get(DiscoveryRun, run_id),
        now_provider,
    ):
        session.rollback()
        return False
    _verify_run_lock(session, run_id)
    session.commit()
    return True


def _persist_failure(
    session: Session,
    stage_id: int,
    error: BaseException,
    now_provider: NowProvider,
) -> StageExecutionOutcome:
    session.rollback()
    stage = session.get(DiscoveryCommonStage, stage_id)
    if stage is None:
        return StageExecutionOutcome(stage_id, "not_found", False)
    if not _run_is_mutable(
        session.get(DiscoveryRun, stage.run_id),
        now_provider,
    ):
        return StageExecutionOutcome(stage_id, "historical", False)
    if stage.status != "running" or stage.superseded_at is not None:
        return StageExecutionOutcome(stage_id, stage.status, False)
    stage.status = "failed"
    stage.error = _safe_error(error)
    stage.failure_kind = _failure_kind(error)
    stage.updated_at = _now(now_provider)
    if not _commit_if_current(
        session,
        stage.run_id,
        now_provider,
    ):
        return StageExecutionOutcome(stage_id, "historical", False)
    return StageExecutionOutcome(stage_id, "failed", True)


def execute_common_stage(
    session: Session,
    stage_id: int,
    *,
    now_provider: NowProvider = utc_now,
) -> StageExecutionOutcome:
    """Atomically claim and materialize one common-stage attempt."""

    if type(stage_id) is not int or stage_id < 1:
        raise ValueError("stage_id must be a positive integer")
    observed = session.get(DiscoveryCommonStage, stage_id)
    if observed is None:
        return StageExecutionOutcome(stage_id, "not_found", False)
    if not _run_is_mutable(
        session.get(DiscoveryRun, observed.run_id),
        now_provider,
    ):
        return StageExecutionOutcome(stage_id, "historical", False)
    claimed_at = _now(now_provider)
    claim = session.execute(
        update(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.id == stage_id,
            DiscoveryCommonStage.status == "queued",
            DiscoveryCommonStage.superseded_at.is_(None),
        )
        .values(
            status="running",
            error=None,
            failure_kind=None,
            updated_at=claimed_at,
        )
    )
    if not _commit_if_current(
        session,
        observed.run_id,
        now_provider,
    ):
        return StageExecutionOutcome(stage_id, "historical", False)
    if int(claim.rowcount or 0) != 1:
        session.expire_all()
        stage = session.get(DiscoveryCommonStage, stage_id)
        return StageExecutionOutcome(
            stage_id,
            "not_found" if stage is None else stage.status,
            False,
        )

    try:
        stage = session.get(DiscoveryCommonStage, stage_id)
        if stage is None:
            raise PermanentDiscoveryError("claimed common stage not found")
        if stage.superseded_at is not None:
            raise PermanentDiscoveryError("common stage is superseded")
        run = session.get(DiscoveryRun, stage.run_id)
        if run is None:
            raise PermanentDiscoveryError("discovery run not found")
        if not _run_is_mutable(run, now_provider):
            return StageExecutionOutcome(stage_id, "historical", True)
        if run.status not in {"queued", "running", "waiting_confirmation"}:
            raise PermanentDiscoveryError(
                f"discovery run cannot execute from status {run.status}"
            )

        frozen = load_frozen_inputs(session, run.id)
        if (
            stage.input_fingerprint != run.input_fingerprint
            or stage.input_fingerprint != frozen.input_fingerprint
        ):
            raise PermanentDiscoveryError(
                "common stage input fingerprint mismatch"
            )
        if (
            stage.algorithm_version != run.algorithm_version
            or stage.algorithm_version != frozen.algorithm_version
        ):
            raise PermanentDiscoveryError(
                "common stage algorithm version mismatch"
            )
        existing = session.scalar(
            select(func.count())
            .select_from(DiscoveryCommonResult)
            .where(
                DiscoveryCommonResult.run_id == stage.run_id,
                DiscoveryCommonResult.attempt == stage.attempt,
            )
        )
        if existing:
            raise PermanentDiscoveryError(
                "common stage already has historical results"
            )

        verdicts: list[CommonVerdict] = [
            classify_common(item, frozen.trading_dates)
            for item in frozen.companies
        ]
        session.rollback()
        stage = session.get(DiscoveryCommonStage, stage_id)
        if stage is None:
            raise PermanentDiscoveryError("claimed common stage not found")
        run = session.get(DiscoveryRun, stage.run_id)
        if not _run_is_mutable(run, now_provider):
            return StageExecutionOutcome(stage_id, "historical", True)
        if stage.status != "running" or stage.superseded_at is not None:
            raise PermanentDiscoveryError(
                "common stage changed during execution"
            )
        completed_at = _now(now_provider)
        session.add_all(
            DiscoveryCommonResult(
                run_id=stage.run_id,
                attempt=stage.attempt,
                code=item.code,
                verdict=item.verdict,
                reason=item.reason,
                metrics_json=canonical_json(item.metrics),
                created_at=completed_at,
            )
            for item in verdicts
        )
        counts = {
            verdict: sum(item.verdict == verdict for item in verdicts)
            for verdict in ("pass", "drop", "insufficient")
        }
        stage.total_count = len(verdicts)
        stage.pass_count = counts["pass"]
        stage.drop_count = counts["drop"]
        stage.insufficient_count = counts["insufficient"]
        stage.status = "waiting_confirmation"
        stage.error = None
        stage.failure_kind = None
        stage.updated_at = completed_at
        session.flush()
        if not _commit_if_current(
            session,
            stage.run_id,
            now_provider,
        ):
            return StageExecutionOutcome(stage_id, "historical", True)
        return StageExecutionOutcome(
            stage_id,
            "waiting_confirmation",
            True,
            processed=len(verdicts),
        )
    except Exception as error:
        return _persist_failure(
            session,
            stage_id,
            error,
            now_provider,
        )


def _next_queued_stage(
    session: Session, run_id: int
) -> tuple[str, int] | None:
    common = session.scalars(
        select(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.run_id == run_id,
            DiscoveryCommonStage.status == "queued",
            DiscoveryCommonStage.superseded_at.is_(None),
        )
        .order_by(
            DiscoveryCommonStage.created_at,
            DiscoveryCommonStage.id,
        )
        .limit(1)
    ).first()
    track = session.scalars(
        select(DiscoveryStage)
        .where(
            DiscoveryStage.run_id == run_id,
            DiscoveryStage.status == "queued",
            DiscoveryStage.superseded_at.is_(None),
        )
        .order_by(DiscoveryStage.created_at, DiscoveryStage.id)
        .limit(1)
    ).first()
    candidates = [
        (row.created_at, row.id, kind)
        for kind, row in (("common", common), ("track", track))
        if row is not None
    ]
    if not candidates:
        return None
    _, stage_id, kind = min(candidates)
    return kind, stage_id


def _persist_track_failure(
    session: Session,
    stage_id: int,
    error: BaseException,
    now_provider: NowProvider,
) -> None:
    """Best-effort failure persistence after discarding callback writes."""

    try:
        session.rollback()
    except BaseException:
        return
    try:
        stage = session.get(DiscoveryStage, stage_id)
        if (
            stage is not None
            and stage.superseded_at is None
            and stage.status in {"queued", "running"}
            and _run_is_mutable(
                session.get(DiscoveryRun, stage.run_id),
                now_provider,
            )
        ):
            stage.status = "failed"
            stage.error = _safe_error(error)
            stage.failure_kind = _failure_kind(error)
            stage.updated_at = _now(now_provider)
            if not _commit_if_current(
                session,
                stage.run_id,
                now_provider,
            ):
                return
    except BaseException:
        try:
            session.rollback()
        except BaseException:
            pass


def _plain_metrics(text: object) -> dict[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def validate(value: object) -> None:
        if value is None or type(value) in {bool, int, str}:
            return
        if type(value) is float:
            if isfinite(value):
                return
            raise ValueError("non-finite metric")
        if type(value) is list:
            for item in value:
                validate(item)
            return
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("non-string metric key")
            for item in value.values():
                validate(item)
            return
        raise ValueError("unsupported metric value")

    if type(text) is not str:
        raise ValueError("metrics JSON must be text")
    value = json.loads(text, parse_constant=reject_constant)
    if type(value) is not dict:
        raise ValueError("metrics JSON must be an object")
    validate(value)
    if canonical_json(value) != text:
        raise ValueError("metrics JSON must be canonical")
    return value


def _track_candidates(
    session: Session, stage: DiscoveryStage
) -> tuple[TrackExecutionCandidate, ...]:
    if stage.stage == "track_gate":
        parent = session.get(DiscoveryCommonStage, stage.common_stage_id)
        if parent is None:
            return ()
        rows = session.scalars(
            select(DiscoveryCommonResult)
            .where(
                DiscoveryCommonResult.run_id == stage.run_id,
                DiscoveryCommonResult.attempt == parent.attempt,
                DiscoveryCommonResult.verdict == "pass",
            )
            .order_by(DiscoveryCommonResult.code, DiscoveryCommonResult.id)
        ).all()
        return tuple(
            TrackExecutionCandidate(
                code=row.code,
                persisted_verdict=row.verdict,
                effective_verdict=row.verdict,
                reason=row.reason,
                metrics_json=row.metrics_json,
                rank=None,
            )
            for row in rows
        )
    if stage.stage == "adjudication":
        return ()
    if stage.predecessor_stage_id is None:
        return ()
    parent = session.get(DiscoveryStage, stage.predecessor_stage_id)
    if parent is None:
        return ()
    overrides = {
        row.code: row
        for row in session.scalars(
            select(DiscoveryOverride).where(
                DiscoveryOverride.run_id == stage.run_id,
                DiscoveryOverride.track == stage.track,
                DiscoveryOverride.stage == parent.stage,
                DiscoveryOverride.attempt == parent.attempt,
            )
        )
    }
    candidates = []
    for row in session.scalars(
        select(DiscoveryResult)
        .where(
            DiscoveryResult.run_id == stage.run_id,
            DiscoveryResult.track == stage.track,
            DiscoveryResult.stage == parent.stage,
            DiscoveryResult.attempt == parent.attempt,
        )
        .order_by(DiscoveryResult.code, DiscoveryResult.id)
    ):
        override = overrides.get(row.code)
        effective = (
            row.verdict
            if override is None
            else ("pass" if override.action == "forced_pass" else "drop")
        )
        if effective != "pass":
            continue
        candidates.append(
            TrackExecutionCandidate(
                code=row.code,
                persisted_verdict=row.verdict,
                effective_verdict=effective,
                reason=row.reason,
                metrics_json=row.metrics_json,
                rank=row.rank,
                override_action=(
                    None if override is None else override.action
                ),
                override_reason=(
                    None if override is None else override.reason
                ),
            )
        )
    return tuple(candidates)


def _validate_track_outcome(
    context: TrackExecutionContext,
    outcome: object,
) -> TrackExecutionOutcome:
    candidate_codes = context.candidate_codes
    snapshot_codes = {
        company.code for company in context.frozen_inputs.companies
    }
    if (
        len(candidate_codes) != len(set(candidate_codes))
        or not set(candidate_codes) <= snapshot_codes
        or any(
            candidate.effective_verdict != "pass"
            or candidate.persisted_verdict not in VERDICTS
            or (
                candidate.override_action is not None
                and candidate.override_action
                not in {"forced_pass", "forced_drop"}
            )
            or (
                candidate.reason is not None
                and type(candidate.reason) is not str
            )
            or (
                candidate.rank is not None
                and (
                    type(candidate.rank) is not int
                    or candidate.rank < 1
                )
            )
            for candidate in context.candidates
        )
    ):
        raise TrackExecutorContractError(
            "track execution context has invalid candidates"
        )
    for candidate in context.candidates:
        try:
            _plain_metrics(candidate.metrics_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise TrackExecutorContractError(
                f"track candidate metrics are invalid: {error}"
            ) from error
    if type(outcome) is not TrackExecutionOutcome:
        raise TrackExecutorContractError(
            "track executor must return TrackExecutionOutcome"
        )
    if outcome.status not in {
        "waiting_confirmation",
        "failed",
        "data_not_ready",
        "ruler_not_ready",
    }:
        raise TrackExecutorContractError(
            "track executor returned an invalid status"
        )
    if type(outcome.results) is not tuple or type(outcome.evidence) is not tuple:
        raise TrackExecutorContractError(
            "track executor artifacts must be immutable tuples"
        )
    if outcome.status == "failed":
        if (
            outcome.failure_kind not in {"transient", "permanent"}
            or type(outcome.error) is not str
            or not outcome.error.strip()
        ):
            raise TrackExecutorContractError(
                "failed track outcome requires failure kind and error"
            )
    elif outcome.failure_kind is not None:
        raise TrackExecutorContractError(
            "non-failed track outcome cannot carry failure kind"
        )
    if outcome.error is not None and type(outcome.error) is not str:
        raise TrackExecutorContractError("track outcome error must be text")
    if (
        outcome.status == "waiting_confirmation"
        and outcome.error is not None
    ):
        raise TrackExecutorContractError(
            "waiting track outcome cannot carry an error"
        )
    if outcome.status != "waiting_confirmation" and (
        outcome.results or outcome.evidence
    ):
        raise TrackExecutorContractError(
            "non-waiting track outcome cannot carry artifacts"
        )

    allowed_codes = set(candidate_codes)
    codes = []
    for row in outcome.results:
        if type(row) is not TrackExecutionResult:
            raise TrackExecutorContractError("invalid track result DTO")
        if type(row.code) is not str or row.code not in allowed_codes:
            raise TrackExecutorContractError(
                "track result code is outside the exact candidate set"
            )
        if row.verdict not in VERDICTS:
            raise TrackExecutorContractError(
                "track result has an invalid verdict"
            )
        if row.reason is not None and type(row.reason) is not str:
            raise TrackExecutorContractError(
                "track result reason must be text or null"
            )
        try:
            _plain_metrics(row.metrics_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise TrackExecutorContractError(
                f"track result metrics are invalid: {error}"
            ) from error
        if row.rank is not None and (
            type(row.rank) is not int or row.rank < 1
        ):
            raise TrackExecutorContractError(
                "track result rank must be positive or null"
            )
        codes.append(row.code)
    if len(codes) != len(set(codes)):
        raise TrackExecutorContractError("track result codes must be unique")
    if (
        outcome.status == "waiting_confirmation"
        and set(codes) != allowed_codes
    ):
        raise TrackExecutorContractError(
            "waiting track results must match the exact candidate set"
        )
    ranks = [row.rank for row in outcome.results if row.rank is not None]
    if ranks and (
        len(ranks) != len(outcome.results)
        or sorted(ranks) != list(range(1, len(ranks) + 1))
    ):
        raise TrackExecutorContractError(
            "track result ranks must be unique and contiguous"
        )

    evidence_keys = []
    for row in outcome.evidence:
        if (
            type(row) is not TrackExecutionEvidence
            or context.stage != "competition"
            or row.code not in allowed_codes
        ):
            raise TrackExecutorContractError(
                "invalid competition evidence DTO"
            )
        for value in (
            row.code,
            row.rule_version,
            row.kind,
            row.rule_id,
            row.display_text,
            row.as_of,
            row.data_completeness,
        ):
            if type(value) is not str or not value.strip():
                raise TrackExecutorContractError(
                    "competition evidence text is required"
                )
        try:
            _plain_metrics(row.input_fields_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise TrackExecutorContractError(
                f"competition evidence input is invalid: {error}"
            ) from error
        evidence_keys.append((row.code, row.kind, row.rule_id))
    if len(evidence_keys) != len(set(evidence_keys)):
        raise TrackExecutorContractError(
            "competition evidence identities must be unique"
        )
    return outcome


def _execute_track_stage(
    session: Session,
    stage_id: int,
    track_executor: TrackExecutor | None,
    now_provider: NowProvider,
) -> bool:
    """Run a pure callback, then validate and persist its immutable outcome."""

    stage = session.get(DiscoveryStage, stage_id)
    if stage is None:
        raise PermanentDiscoveryError("track stage not found")
    if not _run_is_mutable(
        session.get(DiscoveryRun, stage.run_id),
        now_provider,
    ):
        return False
    if stage.stage == "track_gate":
        parent = (
            session.get(DiscoveryCommonStage, stage.common_stage_id)
            if stage.common_stage_id is not None
            else None
        )
        valid_parent = (
            parent is not None
            and parent.run_id == stage.run_id
            and parent.status == "confirmed"
            and parent.superseded_at is None
        )
    else:
        parent = (
            session.get(DiscoveryStage, stage.predecessor_stage_id)
            if stage.predecessor_stage_id is not None
            else None
        )
        expected = (
            "track_gate" if stage.stage == "competition" else "competition"
        )
        valid_parent = (
            parent is not None
            and parent.run_id == stage.run_id
            and parent.track == stage.track
            and parent.stage == expected
            and stage.predecessor_stage == expected
            and parent.status == "confirmed"
            and parent.superseded_at is None
        )
    if not valid_parent:
        code = (
            "lineage_unavailable"
            if stage.common_stage_id is None
            and stage.predecessor_stage_id is None
            else "invalid_predecessor"
        )
        raise PermanentDiscoveryError(f"{code}: exact stage lineage is unavailable")
    frozen = load_frozen_inputs(session, stage.run_id)
    frozen_ruler = frozen.ruler_payloads.get(stage.track)
    if (
        stage.input_fingerprint != frozen.input_fingerprint
        or stage.algorithm_version != frozen.algorithm_version
        or stage.ruler_version_id is None
        or not isinstance(frozen_ruler, Mapping)
        or frozen_ruler.get("id") != stage.ruler_version_id
    ):
        raise PermanentDiscoveryError(
            "algorithm_unavailable: frozen ruler payload is unavailable"
        )
    existing = session.scalar(
        select(func.count())
        .select_from(DiscoveryResult)
        .where(
            DiscoveryResult.run_id == stage.run_id,
            DiscoveryResult.track == stage.track,
            DiscoveryResult.stage == stage.stage,
            DiscoveryResult.attempt == stage.attempt,
        )
    )
    if existing:
        raise PermanentDiscoveryError(
            "track stage already has historical results"
        )
    existing_evidence = session.scalar(
        select(func.count())
        .select_from(DiscoveryEvidence)
        .where(
            DiscoveryEvidence.competition_stage_id == stage.id,
            DiscoveryEvidence.run_id == stage.run_id,
            DiscoveryEvidence.track == stage.track,
            DiscoveryEvidence.competition_attempt == stage.attempt,
        )
    )
    if existing_evidence:
        raise PermanentDiscoveryError(
            "track stage already has historical evidence"
        )
    session.rollback()
    stage = session.get(DiscoveryStage, stage_id)
    if stage is None:
        raise PermanentDiscoveryError("track stage not found")
    if not _run_is_mutable(
        session.get(DiscoveryRun, stage.run_id),
        now_provider,
    ):
        return False
    claimed = session.execute(
        update(DiscoveryStage)
        .where(
            DiscoveryStage.id == stage.id,
            DiscoveryStage.status == "queued",
            DiscoveryStage.superseded_at.is_(None),
        )
        .values(
            status="running",
            error=None,
            failure_kind=None,
            updated_at=_now(now_provider),
        )
    )
    if not _commit_if_current(
        session,
        stage.run_id,
        now_provider,
    ):
        return False
    if int(claimed.rowcount or 0) != 1:
        raise QueueStalledError(f"track stage {stage_id} was not claimable")
    session.expire_all()
    stage = session.get(DiscoveryStage, stage_id)
    if stage is None:
        raise QueueStalledError(f"track stage {stage_id} disappeared")
    identity = (
        stage.id,
        stage.run_id,
        stage.track,
        stage.stage,
        stage.attempt,
        stage.input_fingerprint,
        stage.ruler_version_id,
        stage.algorithm_version,
        stage.common_stage_id,
        stage.predecessor_stage_id,
        stage.predecessor_stage,
        stage.retry_of_stage_id,
        stage.reopen_of_stage_id,
    )
    try:
        ruler_payload = frozen_ruler.get("payload")
        if not isinstance(ruler_payload, Mapping):
            raise PermanentDiscoveryError(
                "algorithm_unavailable: frozen ruler payload is unavailable"
            )
        context = TrackExecutionContext(
            stage_id=stage.id,
            run_id=stage.run_id,
            track=stage.track,
            stage=stage.stage,
            attempt=stage.attempt,
            input_fingerprint=stage.input_fingerprint,
            ruler_version_id=stage.ruler_version_id,
            algorithm_version=stage.algorithm_version,
            common_stage_id=stage.common_stage_id,
            predecessor_stage_id=stage.predecessor_stage_id,
            predecessor_stage=stage.predecessor_stage,
            frozen_inputs=frozen,
            ruler_payload=ruler_payload,
            candidates=_track_candidates(session, stage),
        )
        session.rollback()
        outcome = (
            TrackExecutionOutcome(status="waiting_confirmation")
            if context.stage == "adjudication"
            else (
                track_executor(context)
                if track_executor is not None
                else TrackExecutionOutcome(status="ruler_not_ready")
            )
        )
        outcome = _validate_track_outcome(context, outcome)

        session.expire_all()
        stage = session.get(DiscoveryStage, stage_id)
        if stage is None or stage.superseded_at is not None:
            raise QueueStalledError(
                f"track stage {stage_id} disappeared or was superseded"
            )
        if not _run_is_mutable(
            session.get(DiscoveryRun, stage.run_id),
            now_provider,
        ):
            session.rollback()
            return False
        current_identity = (
            stage.id,
            stage.run_id,
            stage.track,
            stage.stage,
            stage.attempt,
            stage.input_fingerprint,
            stage.ruler_version_id,
            stage.algorithm_version,
            stage.common_stage_id,
            stage.predecessor_stage_id,
            stage.predecessor_stage,
            stage.retry_of_stage_id,
            stage.reopen_of_stage_id,
        )
        if current_identity != identity or stage.status != "running":
            raise TrackExecutorContractError(
                "track stage identity or status changed during execution"
            )
        if session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(
                DiscoveryResult.run_id == stage.run_id,
                DiscoveryResult.track == stage.track,
                DiscoveryResult.stage == stage.stage,
                DiscoveryResult.attempt == stage.attempt,
            )
        ):
            raise TrackExecutorContractError(
                "track stage gained historical results during execution"
            )
        completed_at = _now(now_provider)
        session.add_all(
            DiscoveryResult(
                run_id=stage.run_id,
                track=stage.track,
                stage=stage.stage,
                attempt=stage.attempt,
                code=row.code,
                verdict=row.verdict,
                reason=row.reason,
                metrics_json=row.metrics_json,
                rank=row.rank,
                created_at=completed_at,
            )
            for row in outcome.results
        )
        session.add_all(
            DiscoveryEvidence(
                run_id=stage.run_id,
                track=stage.track,
                competition_attempt=stage.attempt,
                competition_stage_id=stage.id,
                stage="competition",
                code=row.code,
                ruler_version_id=stage.ruler_version_id,
                rule_version=row.rule_version,
                kind=row.kind,
                rule_id=row.rule_id,
                input_fields_json=row.input_fields_json,
                display_text=row.display_text,
                as_of=row.as_of,
                data_completeness=row.data_completeness,
                created_at=completed_at,
            )
            for row in outcome.evidence
        )
        counts = {
            verdict: sum(row.verdict == verdict for row in outcome.results)
            for verdict in VERDICTS
        }
        stage.status = outcome.status
        stage.total_count = len(outcome.results)
        stage.pass_count = counts["pass"]
        stage.drop_count = counts["drop"]
        stage.insufficient_count = counts["insufficient"]
        stage.error = (
            None
            if outcome.error is None
            else _safe_error(ValueError(outcome.error))
        )
        stage.failure_kind = outcome.failure_kind
        stage.updated_at = completed_at
        if not _commit_if_current(
            session,
            stage.run_id,
            now_provider,
        ):
            return False
        return True
    except BaseException as error:
        _persist_track_failure(
            session,
            stage_id,
            error,
            now_provider,
        )
        raise


def _release_run_lock(
    session: Session, run_id: int, owner_token: str
) -> None:
    """Release an exact lease with one bounded fresh-session retry."""

    original = sys.exception()
    cleanup_error: BaseException | None = None
    for attempt in range(2):
        if attempt == 0:
            target = session
        else:
            try:
                target = Session(bind=session.get_bind())
            except BaseException as error:
                cleanup_error = error
                break
        try:
            target.rollback()
            release(
                target,
                run_id,
                JOB_TYPE_DISCOVERY,
                owner_token=owner_token,
            )
            target.commit()
            return
        except BaseException as error:
            cleanup_error = error
            try:
                target.rollback()
            except BaseException:
                pass
        finally:
            if target is not session:
                try:
                    target.close()
                except BaseException:
                    pass
    if original is None and cleanup_error is not None:
        raise cleanup_error
    if original is not None and cleanup_error is not None:
        try:
            original.add_note(
                f"exact discovery lease cleanup failed: {cleanup_error}"
            )
        except (AttributeError, TypeError):
            pass


def _sync_run_status_while_locked(
    session: Session,
    run_id: int,
    now_provider: NowProvider,
) -> None:
    session.rollback()
    _verify_run_lock(session, run_id)
    run = session.get(DiscoveryRun, run_id)
    if not _run_is_mutable(run, now_provider):
        return
    common = session.scalars(
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
    stage_order = {"track_gate": 0, "competition": 1, "adjudication": 2}
    stages = session.scalars(
        select(DiscoveryStage).where(
            DiscoveryStage.run_id == run_id,
            DiscoveryStage.superseded_at.is_(None),
        )
    ).all()
    track_statuses: dict[str, str] = {}
    for track in TRACKS:
        candidates = [row for row in stages if row.track == track]
        track_statuses[track] = (
            "queued"
            if not candidates
            else max(
                candidates,
                key=lambda row: (
                    stage_order.get(row.stage, -1),
                    row.attempt,
                    row.id,
                ),
            ).status
        )
    status = aggregate_run_status(
        run.status,
        "queued" if common is None else common.status,
        track_statuses,
    )
    session.expire(run)
    if not _run_is_mutable(run, now_provider):
        session.rollback()
        return
    session.execute(
        update(DiscoveryRun)
        .where(
            DiscoveryRun.id == run_id,
            DiscoveryRun.current.is_(True),
            DiscoveryRun.status != "abandoned",
        )
        .values(status=status, updated_at=_now(now_provider))
    )
    _commit_if_current(session, run_id, now_provider)


def drain_run_queue(
    session: Session,
    run_id: int,
    *,
    track_executor: TrackExecutor | None = None,
    lock_already_held: bool = False,
    owner_token: str | None = None,
    now_provider: NowProvider = utc_now,
) -> DrainOutcome:
    """Drain queued common/track stages under the one global job lock."""

    if type(run_id) is not int or run_id < 1:
        raise ValueError("run_id must be a positive integer")
    lease_token = (
        owner_token
        if lock_already_held
        and type(owner_token) is str
        and bool(owner_token)
        else None
    )
    adopted = False
    executed = 0
    try:
        if lock_already_held:
            if lease_token is None:
                raise ValueError("discovery run lock cannot be adopted")
            lock = session.get(JobLock, 1)
            if (
                lock is None
                or lock.job_id != run_id
                or lock.job_type != JOB_TYPE_DISCOVERY
                or lock.owner_token != lease_token
            ):
                session.rollback()
                raise ValueError("discovery run lock cannot be adopted")
        else:
            lease_token = acquire_discovery(session, run_id)
            if lease_token is None:
                session.rollback()
                return DrainOutcome(run_id, False, 0)
            session.commit()

        adopted = True
        session.info["discovery_lock_run_id"] = run_id
        session.info["discovery_lock_owner_token"] = lease_token
        while True:
            session.expire_all()
            run = session.get(DiscoveryRun, run_id)
            if not _run_is_mutable(run, now_provider):
                return DrainOutcome(run_id, True, executed)
            selected = _next_queued_stage(session, run_id)
            if selected is None:
                return DrainOutcome(run_id, True, executed)
            kind, stage_id = selected
            if kind == "common":
                outcome = execute_common_stage(
                    session,
                    stage_id,
                    now_provider=now_provider,
                )
                if outcome.status == "historical":
                    return DrainOutcome(run_id, True, executed)
                if not outcome.claimed or outcome.status == "running":
                    raise QueueStalledError(f"common stage {stage_id} did not advance")
                if outcome.status == "failed":
                    return DrainOutcome(run_id, True, executed + 1)
            else:
                stage = session.get(DiscoveryStage, stage_id)
                if stage is None:
                    raise QueueStalledError(f"track stage {stage_id} disappeared")
                if stage.ruler_version_id is not None:
                    try:
                        frozen = load_frozen_inputs(session, run_id)
                        ruler = frozen.ruler_payloads.get(stage.track)
                        if (
                            not isinstance(ruler, Mapping)
                            or ruler.get("id") != stage.ruler_version_id
                        ):
                            raise ValueError("frozen ruler mismatch")
                    except (TypeError, ValueError) as error:
                        session.rollback()
                        stage = session.get(DiscoveryStage, stage_id)
                        if (
                            stage is None
                            or not _run_is_mutable(
                                session.get(
                                    DiscoveryRun,
                                    stage.run_id,
                                ),
                                now_provider,
                            )
                        ):
                            return DrainOutcome(run_id, True, executed)
                        stage.status = "failed"
                        stage.error = _safe_error(error)
                        stage.failure_kind = "permanent"
                        stage.updated_at = _now(now_provider)
                        if not _commit_if_current(
                            session,
                            stage.run_id,
                            now_provider,
                        ):
                            return DrainOutcome(run_id, True, executed)
                        executed += 1
                        continue
                if stage.ruler_version_id is None or (
                    track_executor is None
                    and stage.stage != "adjudication"
                ):
                    session.rollback()
                    stage = session.get(DiscoveryStage, stage_id)
                    if (
                        stage is None
                        or not _run_is_mutable(
                            session.get(DiscoveryRun, stage.run_id),
                            now_provider,
                        )
                    ):
                        return DrainOutcome(run_id, True, executed)
                    stage.status = "ruler_not_ready"
                    stage.error = (
                        "轨道尺子尚未就绪"
                        if stage.ruler_version_id is None
                        else "当前构建未注册轨道执行器"
                    )
                    stage.failure_kind = None
                    stage.updated_at = _now(now_provider)
                    if not _commit_if_current(
                        session,
                        stage.run_id,
                        now_provider,
                    ):
                        return DrainOutcome(run_id, True, executed)
                else:
                    try:
                        completed = _execute_track_stage(
                            session,
                            stage_id,
                            track_executor,
                            now_provider,
                        )
                        if not completed:
                            return DrainOutcome(run_id, True, executed)
                    except Exception as error:
                        session.rollback()
                        session.expire_all()
                        failed = session.get(DiscoveryStage, stage_id)
                        if (
                            failed is not None
                            and failed.status == "queued"
                            and failed.superseded_at is None
                            and _run_is_mutable(
                                session.get(
                                    DiscoveryRun,
                                    failed.run_id,
                                ),
                                now_provider,
                            )
                        ):
                            failed.status = "failed"
                            failed.error = _safe_error(error)
                            failed.failure_kind = _failure_kind(error)
                            failed.updated_at = _now(now_provider)
                            if not _commit_if_current(
                                session,
                                failed.run_id,
                                now_provider,
                            ):
                                return DrainOutcome(
                                    run_id,
                                    True,
                                    executed,
                                )
                            session.expire_all()
                            failed = session.get(
                                DiscoveryStage, stage_id
                            )
                        if failed is None or failed.status in {
                            "queued",
                            "running",
                        }:
                            raise
            executed += 1
    finally:
        try:
            if adopted:
                _sync_run_status_while_locked(
                    session,
                    run_id,
                    now_provider,
                )
        finally:
            session.info.pop("discovery_lock_run_id", None)
            session.info.pop("discovery_lock_owner_token", None)
            if lease_token is not None:
                _release_run_lock(session, run_id, lease_token)
