"""Pure aggregate-status calculation for discovery runs."""

from collections.abc import Mapping

from discovery.constants import (
    RUN_STATUSES,
    STAGE_STATUSES,
    TERMINAL_FAILURES,
    TRACKS,
)


def aggregate_run_status(
    run_status: str,
    common_status: str,
    track_statuses: Mapping[str, str],
) -> str:
    """Derive run status using the design's explicit precedence."""

    if type(run_status) is not str or run_status not in RUN_STATUSES:
        raise ValueError(f"unknown run status: {run_status}")
    if type(common_status) is not str or common_status not in STAGE_STATUSES:
        raise ValueError(f"unknown common-stage status: {common_status}")
    if not isinstance(track_statuses, Mapping):
        raise ValueError("track statuses must contain exactly the three tracks")
    try:
        keys = tuple(track_statuses)
        statuses = tuple(track_statuses.values())
    except TypeError as error:
        raise ValueError("malformed track statuses") from error
    if (
        not all(type(key) is str for key in keys)
        or set(keys) != TRACKS
    ):
        raise ValueError("track statuses must contain exactly the three tracks")
    if any(
        type(status) is not str or status not in STAGE_STATUSES
        for status in statuses
    ):
        raise ValueError("unknown track status")

    terminal = TERMINAL_FAILURES | {"confirmed"}

    if run_status == "abandoned":
        return "abandoned"
    if common_status in TERMINAL_FAILURES or (
        all(status in TERMINAL_FAILURES for status in statuses)
    ):
        return "failed"
    if all(status == "confirmed" for status in statuses):
        return "completed"
    if (
        all(status in terminal for status in statuses)
        and any(status == "confirmed" for status in statuses)
    ):
        return "partially_completed"
    if common_status == "running" or "running" in statuses:
        return "running"
    if (
        common_status == "waiting_confirmation"
        or "waiting_confirmation" in statuses
    ):
        return "waiting_confirmation"
    return "queued"
