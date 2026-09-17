"""Immutable, database-free discovery stage transitions."""

from dataclasses import dataclass, replace
from typing import Collection

from discovery.builds import AVAILABLE_BUILDS, _is_build_available
from discovery.constants import (
    COMMON_STAGE,
    FAILURE_KINDS,
    RUN_STATUSES,
    RUN_TERMINAL_STATUSES,
    STAGE_STATUSES,
    TRACKS,
    TRACK_STAGES,
)

_NORMAL_NEXT = {
    "queued": frozenset({"running"}),
    "running": frozenset(
        {
            "waiting_confirmation",
            "failed",
            "data_not_ready",
            "ruler_not_ready",
        }
    ),
    "waiting_confirmation": frozenset({"confirmed"}),
}


class WorkflowError(ValueError):
    """A rejected workflow mutation with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StageState:
    run_id: int
    track: str | None
    stage: str
    status: str
    attempt: int
    input_fingerprint: str
    ruler_version_id: int | str | None
    algorithm_version: str
    superseded: bool
    failure_kind: str | None = None

    def __post_init__(self) -> None:
        if type(self.run_id) is not int or self.run_id < 1:
            raise ValueError("run ID must be a positive integer")
        if type(self.stage) is not str or self.stage not in {
            COMMON_STAGE,
            *TRACK_STAGES,
        }:
            raise ValueError(f"unknown stage: {self.stage}")
        if self.stage == COMMON_STAGE:
            if self.track is not None:
                raise ValueError("common stage cannot have a track")
        elif type(self.track) is not str or self.track not in TRACKS:
            raise ValueError("track stage requires a known track")
        if type(self.status) is not str or self.status not in STAGE_STATUSES:
            raise ValueError(f"unknown stage status: {self.status}")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("attempt must be positive")
        if (
            type(self.input_fingerprint) is not str
            or not self.input_fingerprint
        ):
            raise ValueError("input fingerprint is required")
        if type(self.ruler_version_id) not in {int, str, type(None)}:
            raise ValueError("ruler version ID has invalid type")
        if type(self.algorithm_version) is not str or not self.algorithm_version:
            raise ValueError("algorithm version is required")
        if type(self.superseded) is not bool:
            raise ValueError("superseded must be boolean")
        if (
            self.status == "insufficient_candidates"
            and self.stage != "adjudication"
        ):
            raise ValueError(
                "insufficient candidates is only valid for adjudication"
            )
        if self.status == "ruler_not_ready" and self.stage == COMMON_STAGE:
            raise ValueError("common stage does not use a ruler")
        if self.status == "failed":
            if (
                type(self.failure_kind) is not str
                or self.failure_kind not in FAILURE_KINDS
            ):
                raise ValueError("failed status requires a valid failure kind")
        elif self.failure_kind is not None:
            raise ValueError("non-failed status cannot carry a failure kind")


def _validate_run_status(run_status: str) -> None:
    if type(run_status) is not str or run_status not in RUN_STATUSES:
        raise WorkflowError(
            "invalid_run_status", f"unknown run status: {run_status}"
        )
    if run_status == "abandoned":
        raise WorkflowError("run_abandoned", "abandoned run is immutable")


def _reject_terminal_run(run_status: str) -> None:
    if run_status in RUN_TERMINAL_STATUSES:
        raise WorkflowError(
            "run_terminal", f"{run_status} run is immutable"
        )


def _validate_run_mutation(run_status: str) -> None:
    _validate_run_status(run_status)
    _reject_terminal_run(run_status)


def _validate_predecessor(
    state: StageState, predecessor: StageState | None
) -> None:
    expected_stage = {
        "track_gate": COMMON_STAGE,
        "competition": "track_gate",
        "adjudication": "competition",
    }.get(state.stage)
    if expected_stage is None:
        if predecessor is not None:
            raise WorkflowError(
                "invalid_predecessor",
                "common stage has no predecessor",
            )
        return

    expected_track = None if expected_stage == COMMON_STAGE else state.track
    if (
        type(predecessor) is not StageState
        or predecessor.run_id != state.run_id
        or predecessor.track != expected_track
        or predecessor.stage != expected_stage
        or predecessor.status != "confirmed"
        or predecessor.superseded
    ):
        raise WorkflowError(
            "invalid_predecessor",
            f"{state.stage} requires its current confirmed predecessor",
        )


def transition_stage(
    state: StageState,
    target_status: str,
    *,
    run_status: str,
    predecessor: StageState | None,
    failure_kind: str | None = None,
) -> StageState:
    """Return the next immutable state or reject an invalid mutation."""

    _validate_run_status(run_status)
    if state.superseded:
        raise WorkflowError(
            "stage_superseded", "superseded stage is immutable"
        )
    if type(target_status) is not str or target_status not in STAGE_STATUSES:
        raise WorkflowError(
            "invalid_status", f"unknown target status: {target_status}"
        )
    _validate_predecessor(state, predecessor)
    if target_status == "failed":
        if (
            type(failure_kind) is not str
            or failure_kind not in FAILURE_KINDS
        ):
            raise WorkflowError(
                "invalid_failure_kind",
                "failed transition requires transient or permanent kind",
            )
    elif failure_kind is not None:
        raise WorkflowError(
            "invalid_failure_kind",
            "only failed status accepts a failure kind",
        )
    if state.status == target_status == "confirmed":
        return state

    _reject_terminal_run(run_status)
    allowed = _NORMAL_NEXT.get(state.status, frozenset())
    if state.stage == COMMON_STAGE:
        allowed = allowed - {"ruler_not_ready"}
    if (
        target_status == "insufficient_candidates"
        and state.stage == "adjudication"
        and state.status == "waiting_confirmation"
    ):
        allowed = allowed | {"insufficient_candidates"}
    if target_status not in allowed:
        raise WorkflowError(
            "invalid_transition",
            f"invalid transition: {state.status} -> {target_status}",
        )
    return replace(
        state,
        status=target_status,
        failure_kind=failure_kind,
    )


def retry_stage(
    state: StageState,
    *,
    input_fingerprint: str,
    ruler_version_id: int | str | None,
    run_status: str,
    predecessor: StageState | None,
    available_builds: Collection[str] | None = None,
) -> StageState:
    """Create the queued state for a retry without changing the failed state."""

    _validate_run_mutation(run_status)
    if state.superseded:
        raise WorkflowError(
            "stage_superseded", "superseded stage is immutable"
        )
    _validate_predecessor(state, predecessor)
    if state.status != "failed" or state.failure_kind != "transient":
        raise WorkflowError(
            "retry_not_allowed",
            "retry requires a transient failed stage",
        )
    if (
        type(input_fingerprint) is not str
        or state.input_fingerprint != input_fingerprint
    ):
        raise WorkflowError(
            "inputs_changed", "retry input fingerprint has changed"
        )
    if (
        type(state.ruler_version_id) is not type(ruler_version_id)
        or state.ruler_version_id != ruler_version_id
    ):
        raise WorkflowError("ruler_changed", "retry ruler version has changed")

    builds = AVAILABLE_BUILDS if available_builds is None else available_builds
    if not _is_build_available(state.algorithm_version, builds):
        raise WorkflowError(
            "algorithm_unavailable",
            "saved algorithm build is unavailable",
        )
    return replace(
        state,
        status="queued",
        attempt=state.attempt + 1,
        failure_kind=None,
    )
