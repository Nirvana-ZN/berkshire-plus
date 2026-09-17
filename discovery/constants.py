"""Shared vocabulary for the weekly discovery workflow."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

NowProvider = Callable[[], datetime]

CHINA_MARKET_TIMEZONE = timezone(
    timedelta(hours=8),
    name="China Standard Time",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware_utc_now(now_provider: NowProvider = utc_now) -> datetime:
    value = now_provider()
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("discovery clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def china_week_key(
    value: datetime | None = None,
    *,
    now_provider: NowProvider = utc_now,
) -> str:
    current = (
        aware_utc_now(now_provider)
        if value is None
        else aware_utc_now(lambda: value)
    )
    year, week, _ = current.astimezone(
        CHINA_MARKET_TIMEZONE
    ).isocalendar()
    return f"{year}-W{week:02d}"


TRACKS = frozenset({"quality_discount", "deep_value", "garp"})
COMMON_STAGE = "common"
TRACK_STAGES = ("track_gate", "competition", "adjudication")

TERMINAL_FAILURES = frozenset(
    {
        "failed",
        "ruler_not_ready",
        "data_not_ready",
        "insufficient_candidates",
    }
)
STAGE_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_confirmation",
        "confirmed",
        *TERMINAL_FAILURES,
    }
)
RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_confirmation",
        "completed",
        "partially_completed",
        "failed",
        "abandoned",
    }
)
RUN_TERMINAL_STATUSES = frozenset(
    {"failed", "completed", "partially_completed", "abandoned"}
)
VERDICTS = frozenset({"pass", "drop", "insufficient"})
FAILURE_KINDS = frozenset({"transient", "permanent"})
