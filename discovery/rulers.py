"""Validated, immutable ruler payloads for independent discovery tracks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import RulerVersion
from discovery.builds import AVAILABLE_BUILDS
from discovery.tracks import SCOPE_TRACKS, TRACKS, scope_for_track, track_for_scope
from ledger.positions import LOCAL_USER_ID

DISCOVERY_PAYLOAD_SCOPE = "weekly-discovery"
RULER_NOT_READY = "ruler_not_ready"
DISCOVERY_DECIMAL_COMPONENT_LIMIT = 4096
# Bounds source text, coefficient digits, and exponent before any fixed-point
# formatting. Accepted values therefore format to at most roughly twice this.

JsonScalar: TypeAlias = None | bool | int | float | str
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[
    str, "FrozenJson"
]

_PAYLOAD_KEYS = frozenset(
    {
        "scope",
        "track",
        "fields",
        "formulas",
        "thresholds",
        "min_observations",
        "missing_policy",
        "sector_branches",
        "order",
        "industry_version",
        "algorithm_version",
    }
)
_DIRECTIONS = frozenset({"asc", "desc"})
_NULL_ORDERS = frozenset({"nulls_first", "nulls_last"})


class RulerNotReadyError(ValueError):
    code = RULER_NOT_READY


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be a nonblank canonical string")
    return value


def _canonical_decimal(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a decimal string")
    if len(value) > DISCOVERY_DECIMAL_COMPONENT_LIMIT:
        raise ValueError(f"{label} exceeds the decimal component limit")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a decimal string") from error
    if not amount.is_finite():
        raise ValueError(f"{label} must be finite")
    digits = amount.as_tuple().digits
    exponent = amount.as_tuple().exponent
    if (
        not isinstance(exponent, int)
        or len(digits) > DISCOVERY_DECIMAL_COMPONENT_LIMIT
        or abs(exponent) > DISCOVERY_DECIMAL_COMPONENT_LIMIT
        or abs(amount.adjusted()) > DISCOVERY_DECIMAL_COMPONENT_LIMIT
    ):
        raise ValueError(f"{label} exceeds the decimal component limit")
    if amount == 0:
        canonical = "0"
    else:
        canonical = format(amount, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
    if value != canonical:
        raise ValueError(f"{label} must be canonical: {canonical}")
    return value


def _reject_aliases_and_cycles(
    value: object,
    *,
    active: set[int],
    seen: set[int],
) -> None:
    if type(value) in {type(None), bool, int, str}:
        return
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("ruler payload numbers must be finite")
        return
    if not isinstance(value, Mapping) and type(value) not in {list, tuple}:
        raise TypeError(
            f"ruler payload contains unsupported {type(value).__name__}"
        )
    identity = id(value)
    if identity in active:
        raise TypeError("ruler payload must not contain a cycle")
    if identity in seen:
        raise TypeError("ruler payload must not contain mutable aliases")
    seen.add(identity)
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("ruler payload object keys must be strings")
                _reject_aliases_and_cycles(item, active=active, seen=seen)
        else:
            for item in value:
                _reject_aliases_and_cycles(item, active=active, seen=seen)
    finally:
        active.remove(identity)


def _freeze_json(value: object) -> FrozenJson:
    if type(value) in {type(None), bool, int, float, str}:
        return value  # type: ignore[return-value]
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if type(value) in {list, tuple}:
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"unsupported ruler value: {type(value).__name__}")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _mapping(
    value: object,
    label: str,
    *,
    nonempty: bool = True,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or (nonempty and not value):
        raise ValueError(f"{label} must be a non-empty object")
    if any(type(key) is not str for key in value):
        raise TypeError(f"{label} keys must be strings")
    return value


def _string_mapping(value: object, label: str) -> Mapping[str, str]:
    source = _mapping(value, label)
    result: dict[str, str] = {}
    for key, item in source.items():
        result[_text(key, f"{label} key")] = _text(item, f"{label}.{key}")
    return MappingProxyType(result)


def _scope(value: str) -> str:
    if value in TRACKS:
        return scope_for_track(value)
    if value in SCOPE_TRACKS:
        return value
    raise ValueError(f"unknown discovery scope: {value}")


@dataclass(frozen=True)
class TrackRuler:
    """One complete track ruler; ``scope`` is the database ledger scope."""

    scope: str
    fields: tuple[str, ...]
    formulas: Mapping[str, str]
    thresholds: Mapping[str, str]
    min_observations: Mapping[str, int]
    missing_policy: Mapping[str, str]
    sector_branches: Mapping[str, FrozenJson]
    order: tuple[tuple[str, str, str], ...]
    industry_version: str
    algorithm_version: str
    _allow_retired_algorithm: InitVar[bool] = False

    def __post_init__(self, _allow_retired_algorithm: bool) -> None:
        if type(_allow_retired_algorithm) is not bool:
            raise TypeError("allow_retired_algorithm must be a boolean")
        scope = _scope(self.scope)
        fields = tuple(self.fields)
        if not fields:
            raise ValueError("fields must not be empty")
        if any(
            type(field) is not str
            or not field
            or field != field.strip()
            for field in fields
        ):
            raise ValueError("fields must contain nonblank canonical strings")
        if len(fields) != len(set(fields)):
            raise ValueError("fields must not contain duplicates")

        formulas = _string_mapping(self.formulas, "formulas")
        threshold_source = _mapping(self.thresholds, "thresholds")
        thresholds = MappingProxyType(
            {
                _text(key, "threshold key"): _canonical_decimal(
                    value, f"thresholds.{key}"
                )
                for key, value in threshold_source.items()
            }
        )
        observation_source = _mapping(
            self.min_observations, "min_observations"
        )
        observations: dict[str, int] = {}
        for key, value in observation_source.items():
            name = _text(key, "min_observations key")
            if type(value) is not int or value < 1:
                raise ValueError(
                    f"min_observations.{name} must be a positive integer"
                )
            observations[name] = value

        policies = _string_mapping(self.missing_policy, "missing_policy")
        if set(policies) != set(fields):
            raise ValueError("missing_policy must cover every field exactly")

        _reject_aliases_and_cycles(
            self.sector_branches,
            active=set(),
            seen=set(),
        )
        branches = _mapping(self.sector_branches, "sector_branches")
        branch_values: dict[str, FrozenJson] = {}
        for key, value in branches.items():
            name = _text(key, "sector branch")
            if not isinstance(value, Mapping) or not value:
                raise ValueError(
                    f"sector_branches.{name} must be a non-empty object"
                )
            branch_values[name] = _freeze_json(value)

        if isinstance(self.order, (str, bytes)) or not isinstance(
            self.order, Sequence
        ):
            raise TypeError("order must be a sequence of triples")
        order: list[tuple[str, str, str]] = []
        for index, item in enumerate(self.order):
            if (
                isinstance(item, (str, bytes))
                or not isinstance(item, Sequence)
                or len(item) != 3
            ):
                raise ValueError(f"order[{index}] must be a triple")
            field = _text(item[0], f"order[{index}] field")
            direction = _text(item[1], f"order[{index}] direction")
            null_order = _text(item[2], f"order[{index}] null ordering")
            if field not in fields and field != "code":
                raise ValueError(f"order field is not declared: {field}")
            if direction not in _DIRECTIONS:
                raise ValueError(f"invalid order direction: {direction}")
            if null_order not in _NULL_ORDERS:
                raise ValueError(f"invalid null ordering: {null_order}")
            order.append((field, direction, null_order))
        if not order or len({item[0] for item in order}) != len(order):
            raise ValueError("order must contain unique fields")
        if order[-1] != ("code", "asc", "nulls_last"):
            raise ValueError(
                "order must end with code ascending and nulls_last"
            )

        industry_version = _text(
            self.industry_version, "industry_version"
        )
        algorithm_version = _text(
            self.algorithm_version, "algorithm_version"
        )
        if (
            not _allow_retired_algorithm
            and algorithm_version not in AVAILABLE_BUILDS
        ):
            raise ValueError(
                f"algorithm build is unavailable: {algorithm_version}"
            )

        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "formulas", formulas)
        object.__setattr__(self, "thresholds", thresholds)
        object.__setattr__(
            self, "min_observations", MappingProxyType(observations)
        )
        object.__setattr__(self, "missing_policy", policies)
        object.__setattr__(
            self, "sector_branches", MappingProxyType(branch_values)
        )
        object.__setattr__(self, "order", tuple(order))
        object.__setattr__(self, "industry_version", industry_version)
        object.__setattr__(self, "algorithm_version", algorithm_version)

    @property
    def track(self) -> str:
        return track_for_scope(self.scope)

    def payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "scope": DISCOVERY_PAYLOAD_SCOPE,
                "track": self.track,
                "fields": self.fields,
                "formulas": self.formulas,
                "thresholds": self.thresholds,
                "min_observations": self.min_observations,
                "missing_policy": self.missing_policy,
                "sector_branches": self.sector_branches,
                "order": self.order,
                "industry_version": self.industry_version,
                "algorithm_version": self.algorithm_version,
            }
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        allow_retired_algorithm: bool = False,
    ) -> "TrackRuler":
        if not isinstance(payload, Mapping):
            raise TypeError("ruler payload must be an object")
        _reject_aliases_and_cycles(payload, active=set(), seen=set())
        if set(payload) != _PAYLOAD_KEYS:
            missing = sorted(_PAYLOAD_KEYS - set(payload))
            extra = sorted(set(payload) - _PAYLOAD_KEYS)
            raise ValueError(
                f"ruler payload keys mismatch; missing={missing}, extra={extra}"
            )
        if payload["scope"] != DISCOVERY_PAYLOAD_SCOPE:
            raise ValueError("discovery payload scope is incompatible")
        track = payload["track"]
        if type(track) is not str or track not in TRACKS:
            raise ValueError("discovery payload track is invalid")
        fields_value = payload["fields"]
        if isinstance(fields_value, (str, bytes)) or not isinstance(
            fields_value, Sequence
        ):
            raise TypeError("fields must be a sequence")
        return cls(
            scope=scope_for_track(track),
            fields=tuple(fields_value),
            formulas=payload["formulas"],  # type: ignore[arg-type]
            thresholds=payload["thresholds"],  # type: ignore[arg-type]
            min_observations=payload["min_observations"],  # type: ignore[arg-type]
            missing_policy=payload["missing_policy"],  # type: ignore[arg-type]
            sector_branches=payload["sector_branches"],  # type: ignore[arg-type]
            order=payload["order"],  # type: ignore[arg-type]
            industry_version=payload["industry_version"],  # type: ignore[arg-type]
            algorithm_version=payload["algorithm_version"],  # type: ignore[arg-type]
            _allow_retired_algorithm=allow_retired_algorithm,
        )

    @classmethod
    def from_json(
        cls,
        payload_json: str,
        *,
        allow_retired_algorithm: bool = False,
    ) -> "TrackRuler":
        if type(payload_json) is not str:
            raise TypeError("ruler payload JSON must be text")

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON number: {value}")

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        try:
            value = json.loads(
                payload_json,
                parse_constant=reject_constant,
                object_pairs_hook=unique_object,
            )
        except json.JSONDecodeError as error:
            raise ValueError("ruler payload is not valid JSON") from error
        if type(value) is not dict:
            raise TypeError("ruler payload must be a JSON object")
        return cls.from_payload(
            value,
            allow_retired_algorithm=allow_retired_algorithm,
        )


def parse_payload(value: TrackRuler | Mapping[str, object] | str) -> TrackRuler:
    if isinstance(value, TrackRuler):
        if value.algorithm_version not in AVAILABLE_BUILDS:
            raise ValueError(
                f"algorithm build is unavailable: {value.algorithm_version}"
            )
        return value
    if isinstance(value, str):
        return TrackRuler.from_json(value)
    return TrackRuler.from_payload(value)


def _canonical_ruler_payload(ruler: TrackRuler) -> str:
    return json.dumps(
        _plain(ruler.payload()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_payload(
    value: TrackRuler | Mapping[str, object] | str,
) -> str:
    return _canonical_ruler_payload(parse_payload(value))


def payload_digest(
    value: TrackRuler | Mapping[str, object] | str,
) -> str:
    return hashlib.sha256(canonical_payload(value).encode("utf-8")).hexdigest()


def validate_ruler_version(
    row: RulerVersion,
    *,
    expected_track: str | None = None,
    expected_algorithm_version: str | None = None,
    allow_retired_algorithm: bool = False,
) -> TrackRuler:
    """Parse and validate one persisted discovery ruler end-to-end."""

    if row.scope not in SCOPE_TRACKS:
        raise ValueError("persisted discovery ruler integrity mismatch")
    try:
        ruler = TrackRuler.from_json(
            row.payload_json,
            allow_retired_algorithm=allow_retired_algorithm,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            "persisted discovery ruler integrity mismatch"
        ) from error
    canonical = _canonical_ruler_payload(ruler)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if (
        ruler.scope != row.scope
        or canonical != row.payload_json
        or row.digest != digest
        or (
            expected_track is not None
            and ruler.track != expected_track
        )
        or (
            expected_algorithm_version is not None
            and ruler.algorithm_version != expected_algorithm_version
        )
    ):
        raise ValueError("persisted discovery ruler integrity mismatch")
    return ruler


parse_ruler_version = validate_ruler_version


def _rows(
    session: Session,
    scope: str,
    *,
    user_id: int,
    digest: str | None = None,
) -> list[RulerVersion]:
    query = select(RulerVersion).where(
        RulerVersion.user_id == user_id,
        RulerVersion.scope == scope,
    )
    if digest is not None:
        query = query.where(RulerVersion.digest == digest)
    return session.scalars(
        query.order_by(RulerVersion.id.desc())
    ).all()


def _newest_valid(
    rows: Sequence[RulerVersion],
    *,
    track: str,
    algorithm_version: str | None,
) -> RulerVersion | None:
    for row in rows:
        try:
            validate_ruler_version(
                row,
                expected_track=track,
                expected_algorithm_version=algorithm_version,
            )
        except (TypeError, ValueError):
            continue
        return row
    return None


def latest(
    session: Session,
    track_or_scope: str,
    *,
    user_id: int = LOCAL_USER_ID,
    algorithm_version: str | None = None,
) -> RulerVersion | None:
    scope = _scope(track_or_scope)
    return _newest_valid(
        _rows(session, scope, user_id=user_id),
        track=track_for_scope(scope),
        algorithm_version=algorithm_version,
    )


def activate(
    session: Session,
    value: TrackRuler | Mapping[str, object] | str,
    reason: object,
    *,
    user_id: int = LOCAL_USER_ID,
) -> RulerVersion:
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise ValueError("ruler activation reason must not be blank")
    ruler = parse_payload(value)
    payload_json = canonical_payload(ruler)
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    existing = _newest_valid(
        _rows(
            session,
            ruler.scope,
            user_id=user_id,
            digest=digest,
        ),
        track=ruler.track,
        algorithm_version=ruler.algorithm_version,
    )
    if existing is not None:
        return existing
    session.execute(
        sqlite_insert(RulerVersion)
        .prefix_with("OR IGNORE")
        .values(
            user_id=user_id,
            scope=ruler.scope,
            digest=digest,
            reason=reason_text,
            payload_json=payload_json,
            activated_at=datetime.now(timezone.utc),
        )
    )
    row = _newest_valid(
        _rows(
            session,
            ruler.scope,
            user_id=user_id,
            digest=digest,
        ),
        track=ruler.track,
        algorithm_version=ruler.algorithm_version,
    )
    if row is None:
        raise ValueError("persisted discovery ruler integrity mismatch")
    return row


def load(row: RulerVersion) -> TrackRuler:
    return validate_ruler_version(row)


def readiness(
    session: Session,
    track_or_scope: str,
    *,
    user_id: int = LOCAL_USER_ID,
) -> Mapping[str, object]:
    scope = _scope(track_or_scope)
    track = track_for_scope(scope)
    row = latest(session, scope, user_id=user_id)
    if row is None:
        return MappingProxyType(
            {
                "track": track,
                "scope": scope,
                "status": RULER_NOT_READY,
                "active": None,
            }
        )
    try:
        ruler = validate_ruler_version(
            row,
            expected_track=track,
        )
    except (TypeError, ValueError):
        return MappingProxyType(
            {
                "track": track,
                "scope": scope,
                "status": RULER_NOT_READY,
                "active": None,
            }
        )
    return MappingProxyType(
        {
            "track": track,
            "scope": scope,
            "status": "ready",
            "active": MappingProxyType(
                {
                    "id": row.id,
                    "digest": row.digest,
                    "reason": row.reason,
                    "activated_at": row.activated_at.isoformat(),
                    "algorithm_version": ruler.algorithm_version,
                }
            ),
        }
    )


def require_ready(
    session: Session,
    track_or_scope: str,
    *,
    user_id: int = LOCAL_USER_ID,
) -> tuple[RulerVersion, TrackRuler]:
    scope = _scope(track_or_scope)
    track = track_for_scope(scope)
    row = latest(session, scope, user_id=user_id)
    if row is None:
        raise RulerNotReadyError(RULER_NOT_READY)
    try:
        return row, validate_ruler_version(
            row,
            expected_track=track,
        )
    except (TypeError, ValueError) as error:
        raise RulerNotReadyError(RULER_NOT_READY) from error


status = readiness
