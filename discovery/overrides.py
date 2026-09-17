"""Pure effective-verdict values for persisted discovery results."""

from dataclasses import dataclass

from discovery.constants import TRACK_STAGES, TRACKS, VERDICTS

OVERRIDE_ACTIONS = frozenset({"forced_pass", "forced_drop"})


def _identity(track: object, stage: object, code: object) -> None:
    if type(track) is not str or track not in TRACKS:
        raise ValueError(f"unknown track: {track}")
    if type(stage) is not str or stage not in TRACK_STAGES:
        raise ValueError(f"unknown stage: {stage}")
    if type(code) is not str or not code.strip():
        raise ValueError("result code is required")


@dataclass(frozen=True, slots=True)
class StageResult:
    track: str
    stage: str
    code: str
    verdict: str

    def __post_init__(self) -> None:
        _identity(self.track, self.stage, self.code)
        if type(self.verdict) is not str or self.verdict not in VERDICTS:
            raise ValueError(f"unknown verdict: {self.verdict}")


@dataclass(frozen=True, slots=True)
class StageOverride:
    track: str
    stage: str
    code: str
    action: str
    reason: str

    def __post_init__(self) -> None:
        _identity(self.track, self.stage, self.code)
        if type(self.action) is not str or self.action not in OVERRIDE_ACTIONS:
            raise ValueError(f"unknown override action: {self.action}")
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("override reason is required")

    @property
    def forced_verdict(self) -> str:
        return "pass" if self.action == "forced_pass" else "drop"


def effective_verdict(
    result: StageResult, override: StageOverride | None
) -> str:
    """Return a frozen effective verdict without creating a decision."""

    if type(result) is not StageResult:
        raise TypeError("result must be StageResult")
    if override is None:
        return result.verdict
    if type(override) is not StageOverride:
        raise TypeError("override must be StageOverride or None")
    if (
        override.track,
        override.stage,
        override.code,
    ) != (result.track, result.stage, result.code):
        raise ValueError("override must identify the same result")
    return override.forced_verdict
