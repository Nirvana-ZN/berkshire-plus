"""Independent weekly-discovery track identities."""

from __future__ import annotations

QUALITY_DISCOUNT = "quality_discount"
DEEP_VALUE = "deep_value"
GARP = "garp"

TRACKS = (QUALITY_DISCOUNT, DEEP_VALUE, GARP)

FUNNEL_SCOPE = "funnel"
DISCOVERY_SCOPE_PREFIX = "discovery:"
QUALITY_DISCOUNT_SCOPE = f"{DISCOVERY_SCOPE_PREFIX}{QUALITY_DISCOUNT}"
DEEP_VALUE_SCOPE = f"{DISCOVERY_SCOPE_PREFIX}{DEEP_VALUE}"
GARP_SCOPE = f"{DISCOVERY_SCOPE_PREFIX}{GARP}"

TRACK_SCOPES = {
    QUALITY_DISCOUNT: QUALITY_DISCOUNT_SCOPE,
    DEEP_VALUE: DEEP_VALUE_SCOPE,
    GARP: GARP_SCOPE,
}
SCOPE_TRACKS = {scope: track for track, scope in TRACK_SCOPES.items()}


def scope_for_track(track: str) -> str:
    try:
        return TRACK_SCOPES[track]
    except KeyError as error:
        raise ValueError(f"unknown discovery track: {track}") from error


def track_for_scope(scope: str) -> str:
    try:
        return SCOPE_TRACKS[scope]
    except KeyError as error:
        raise ValueError(f"unknown discovery scope: {scope}") from error
