"""Executable discovery-build validation."""

from collections.abc import Collection
from copy import deepcopy
from math import isfinite
from typing import TypeVar

BUILD_VERSION = "discovery-v2"
AVAILABLE_BUILDS = frozenset({BUILD_VERSION})

T = TypeVar("T")


class AlgorithmUnavailableError(ValueError):
    """The saved inputs cannot be recomputed faithfully."""

    code = "algorithm_unavailable"


def _is_build_available(
    algorithm_version: object, available_builds: object
) -> bool:
    try:
        return (
            type(algorithm_version) is str
            and bool(algorithm_version)
            and not isinstance(available_builds, (str, bytes))
            and isinstance(available_builds, Collection)
            and all(
                type(build) is str and bool(build)
                for build in available_builds
            )
            and algorithm_version in available_builds
        )
    except TypeError:
        return False


def validate_recomputation(
    algorithm_version: str,
    *,
    snapshot_complete: bool,
    ruler_payload_complete: bool,
    persisted_input_fingerprint: str,
    requested_input_fingerprint: str,
    persisted_ruler_version_id: int | str | None,
    requested_ruler_version_id: int | str | None,
    available_builds: Collection[str] | None = None,
) -> None:
    """Require every immutable artifact needed for faithful recomputation."""

    builds = AVAILABLE_BUILDS if available_builds is None else available_builds
    persisted_ruler_valid = (
        type(persisted_ruler_version_id) is int
        and persisted_ruler_version_id > 0
    ) or (
        type(persisted_ruler_version_id) is str
        and bool(persisted_ruler_version_id)
    )
    ruler_valid = (
        persisted_ruler_valid
        and type(requested_ruler_version_id)
        is type(persisted_ruler_version_id)
        and persisted_ruler_version_id == requested_ruler_version_id
    )
    valid = (
        _is_build_available(algorithm_version, builds)
        and snapshot_complete is True
        and ruler_payload_complete is True
        and type(persisted_input_fingerprint) is str
        and bool(persisted_input_fingerprint)
        and type(requested_input_fingerprint) is str
        and persisted_input_fingerprint == requested_input_fingerprint
        and ruler_valid
    )
    if not valid:
        raise AlgorithmUnavailableError(
            "saved algorithm build or immutable inputs are unavailable"
        )


def _validate_plain_data(value: object, active: set[int]) -> None:
    if type(value) in {type(None), bool, int, float, str}:
        if type(value) is float and not isfinite(value):
            raise TypeError("historical payload numbers must be finite")
        return
    if type(value) not in {dict, list}:
        raise TypeError("historical payload must be plain JSON-like data")

    identity = id(value)
    if identity in active:
        raise TypeError("historical payload must not contain cycles")
    active.add(identity)
    try:
        if type(value) is list:
            for item in value:
                _validate_plain_data(item, active)
        else:
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError(
                        "historical payload object keys must be strings"
                    )
                _validate_plain_data(item, active)
    finally:
        active.remove(identity)


def read_historical(persisted: T) -> T:
    """Copy a plain JSON-like persisted payload without executing old code."""

    _validate_plain_data(persisted, set())
    return deepcopy(persisted)
