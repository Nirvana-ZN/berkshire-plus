"""Serialized discovery stage execution.

Run aggregate status remains derived by the workflow/API layer; this worker
only owns the exact stage attempt it claims.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import sys

from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.discovery_models import (
    DiscoveryCommonResult,
    DiscoveryCommonStage,
    DiscoveryRun,
    DiscoveryStage,
)
from discovery.common import CommonVerdict, classify_common
from discovery.snapshot import canonical_json, load_frozen_inputs
from jobs.lock import JOB_TYPE_DISCOVERY, release, try_acquire


_classify_common = classify_common
TrackExecutor = Callable[[Session, int], object]


class TransientDiscoveryError(RuntimeError):
    """An explicitly retryable error for unchanged frozen inputs."""


class PermanentDiscoveryError(ValueError):
    """A snapshot, data, or business invariant failure."""


class QueueStalledError(RuntimeError):
    """An executor returned without advancing its selected stage."""


class TrackExecutorContractError(PermanentDiscoveryError):
    """The injected track executor violated drain-owned transactions."""


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _persist_failure(
    session: Session, stage_id: int, error: BaseException
) -> StageExecutionOutcome:
    session.rollback()
    stage = session.get(DiscoveryCommonStage, stage_id)
    if stage is None:
        return StageExecutionOutcome(stage_id, "not_found", False)
    if stage.status != "running" or stage.superseded_at is not None:
        return StageExecutionOutcome(stage_id, stage.status, False)
    stage.status = "failed"
    stage.error = _safe_error(error)
    stage.failure_kind = _failure_kind(error)
    stage.updated_at = _now()
    session.commit()
    return StageExecutionOutcome(stage_id, "failed", True)


def execute_common_stage(
    session: Session, stage_id: int
) -> StageExecutionOutcome:
    """Atomically claim and materialize one common-stage attempt."""

    if type(stage_id) is not int or stage_id < 1:
        raise ValueError("stage_id must be a positive integer")
    claimed_at = _now()
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
    session.commit()
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
        if run.current is not True:
            raise PermanentDiscoveryError("discovery run is not current")
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
        completed_at = _now()
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
        session.commit()
        return StageExecutionOutcome(
            stage_id,
            "waiting_confirmation",
            True,
            processed=len(verdicts),
        )
    except Exception as error:
        return _persist_failure(session, stage_id, error)


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
    session: Session, stage_id: int, error: BaseException
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
        ):
            stage.status = "failed"
            stage.error = _safe_error(error)
            stage.failure_kind = _failure_kind(error)
            stage.updated_at = _now()
            session.commit()
    except BaseException:
        try:
            session.rollback()
        except BaseException:
            pass


def _execute_track_stage(
    session: Session,
    stage_id: int,
    track_executor: TrackExecutor,
) -> None:
    """Run a callback whose writes are committed only by this drain."""

    def reject_callback_commit(_session: Session) -> None:
        raise TrackExecutorContractError(
            "track executor must not commit its transaction"
        )

    event.listen(session, "before_commit", reject_callback_commit)
    try:
        try:
            track_executor(session, stage_id)
        finally:
            event.remove(session, "before_commit", reject_callback_commit)
        session.flush()
        state = session.execute(
            select(
                DiscoveryStage.status,
                DiscoveryStage.failure_kind,
            ).where(
                DiscoveryStage.id == stage_id,
                DiscoveryStage.superseded_at.is_(None),
            )
        ).one_or_none()
        if state is None or state.status in {"queued", "running"}:
            raise QueueStalledError(
                f"track stage {stage_id} did not advance"
            )
        allowed = {
            "waiting_confirmation",
            "failed",
            "data_not_ready",
            "ruler_not_ready",
        }
        if (
            state.status not in allowed
            or (
                state.status == "failed"
                and state.failure_kind not in {"transient", "permanent"}
            )
            or (
                state.status != "failed"
                and state.failure_kind is not None
            )
        ):
            raise TrackExecutorContractError(
                f"track stage {stage_id} has invalid completion state"
            )
        session.commit()
    except BaseException as error:
        if event.contains(session, "before_commit", reject_callback_commit):
            event.remove(session, "before_commit", reject_callback_commit)
        _persist_track_failure(session, stage_id, error)
        raise


def _release_run_lock(session: Session, run_id: int) -> None:
    """Release from a clean transaction without masking the body error."""

    original = sys.exception()
    cleanup_error: BaseException | None = None
    try:
        session.rollback()
    except BaseException as error:
        cleanup_error = error
    try:
        release(session, run_id, JOB_TYPE_DISCOVERY)
        session.commit()
    except BaseException as error:
        if cleanup_error is None:
            cleanup_error = error
        try:
            session.rollback()
        except BaseException:
            pass
    if original is None and cleanup_error is not None:
        raise cleanup_error


def drain_run_queue(
    session: Session,
    run_id: int,
    *,
    track_executor: TrackExecutor | None = None,
) -> DrainOutcome:
    """Drain queued common/track stages under the one global job lock."""

    if type(run_id) is not int or run_id < 1:
        raise ValueError("run_id must be a positive integer")
    if not try_acquire(session, run_id, JOB_TYPE_DISCOVERY):
        session.rollback()
        return DrainOutcome(run_id, False, 0)
    session.commit()

    executed = 0
    try:
        while True:
            session.expire_all()
            selected = _next_queued_stage(session, run_id)
            if selected is None:
                return DrainOutcome(run_id, True, executed)
            kind, stage_id = selected
            if kind == "common":
                outcome = execute_common_stage(session, stage_id)
                if not outcome.claimed or outcome.status == "running":
                    raise QueueStalledError(
                        f"common stage {stage_id} did not advance"
                    )
            else:
                if track_executor is None:
                    raise QueueStalledError(
                        f"track stage {stage_id} has no executor"
                    )
                _execute_track_stage(session, stage_id, track_executor)
            executed += 1
    finally:
        _release_run_lock(session, run_id)
