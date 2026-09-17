"""Shared vocabulary for the weekly discovery workflow."""

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
