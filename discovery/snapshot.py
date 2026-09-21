"""Immutable discovery input snapshots and persisted historical replay.

Database IDs and ``snapshot_at`` are intentionally excluded from fingerprints:
they identify storage and capture time, not the inputs that affect selection.
Decimal, date, and datetime values deliberately normalize to canonical persisted
text; their original Python runtime types are not downstream input semantics.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from sqlalchemy import case, func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.discovery_models import (
    DiscoveryCommonResult,
    DiscoveryCommonStage,
    DiscoveryDecision,
    DiscoveryEvidence,
    DiscoveryInputSnapshot,
    DiscoveryOverride,
    DiscoveryResult,
    DiscoveryRun,
    DiscoveryStage,
)
from app.models import (
    DiscoveryDataCheckpoint,
    DiscoveryDataFill,
    DiscoveryFillTradingDate,
    DiscoveryUniverseSecurity,
    JobLock,
    RulerVersion,
)
from discovery.builds import BUILD_VERSION
from discovery.company_payload import (
    PAYLOAD_SCHEMA_RESTART_GUIDANCE,
    SCHEMA_VERSION as COMPANY_PAYLOAD_SCHEMA_VERSION,
    parse_company_payload,
    verify_company_payload_digest,
)
from discovery.constants import CHINA_MARKET_TIMEZONE
from discovery.rulers import (
    canonical_payload as canonical_ruler_payload,
    latest as latest_discovery_ruler,
    validate_ruler_version,
)
from discovery.tracks import TRACKS

JsonScalar: TypeAlias = None | bool | int | float | str
PlainValue: TypeAlias = JsonScalar | list["PlainValue"] | dict[str, "PlainValue"]
FrozenValue: TypeAlias = JsonScalar | tuple["FrozenValue", ...] | Mapping[
    str, "FrozenValue"
]


@dataclass(frozen=True)
class CompanySnapshotInput:
    code: str
    name: str
    industry: str | None
    security_status: Mapping[str, object]
    fields: Mapping[str, object]
    sources: Mapping[str, object]
    as_of: Mapping[str, object]


@dataclass(frozen=True)
class SnapshotVersions:
    data_version: str
    financial_version: str
    valuation_version: str
    ruler_version_ids: Mapping[str, int | None]
    algorithm_version: str


@dataclass(frozen=True)
class FrozenCompanyInput:
    code: str
    name: str
    industry: str | None
    security_status: Mapping[str, FrozenValue]
    fields: Mapping[str, FrozenValue]
    sources: Mapping[str, FrozenValue]
    as_of: Mapping[str, FrozenValue]
    data_version: str
    financial_version: str
    valuation_version: str
    ruler_version_ids: Mapping[str, int | None]
    algorithm_version: str
    input_fingerprint: str
    snapshot_at: datetime


@dataclass(frozen=True)
class FrozenRunInputs:
    run_id: int
    input_fingerprint: str
    data_version: str
    financial_version: str
    valuation_version: str
    ruler_version_ids: Mapping[str, int | None]
    ruler_payloads: Mapping[str, FrozenValue]
    algorithm_version: str
    trading_dates: Mapping[str, tuple[str, ...]]
    snapshot_at: datetime
    companies: tuple[FrozenCompanyInput, ...]


@dataclass(frozen=True)
class HistoricalReplay:
    common_stages: tuple[Mapping[str, FrozenValue], ...]
    common_results: tuple[Mapping[str, FrozenValue], ...]
    track_stages: tuple[Mapping[str, FrozenValue], ...]
    track_results: tuple[Mapping[str, FrozenValue], ...]
    overrides: tuple[Mapping[str, FrozenValue], ...]
    decisions: tuple[Mapping[str, FrozenValue], ...]
    evidence: tuple[Mapping[str, FrozenValue], ...]


@dataclass(frozen=True)
class ProductionSnapshot:
    fill_id: int
    companies: tuple[CompanySnapshotInput, ...]
    versions: SnapshotVersions
    trading_dates: Mapping[str, tuple[str, ...]]
    snapshot_at: datetime


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Decimal values must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _plain(value: object, active: set[int]) -> PlainValue:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("float values must be finite")
        return value
    if type(value) is Decimal:
        return _decimal_text(value)
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if type(value) is date:
        return value.isoformat()

    if not isinstance(value, Mapping) and type(value) not in {list, tuple}:
        raise TypeError(f"unsupported snapshot value: {type(value).__name__}")
    identity = id(value)
    if identity in active:
        raise TypeError("snapshot values must not contain cycles")
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            result: dict[str, PlainValue] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("snapshot object keys must be strings")
                result[key] = _plain(item, active)
            return result
        return [_plain(item, active) for item in value]
    finally:
        active.remove(identity)


def canonical_json(value: object) -> str:
    """Serialize supported snapshot values to deterministic UTF-8 JSON text."""

    return json.dumps(
        _plain(value, set()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _nonempty_text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _validated_versions(versions: SnapshotVersions) -> dict[str, object]:
    values = {
        "data_version": _nonempty_text(versions.data_version, "data_version"),
        "financial_version": _nonempty_text(
            versions.financial_version, "financial_version"
        ),
        "valuation_version": _nonempty_text(
            versions.valuation_version, "valuation_version"
        ),
        "algorithm_version": _nonempty_text(
            versions.algorithm_version, "algorithm_version"
        ),
    }
    ruler_ids = dict(versions.ruler_version_ids)
    if set(ruler_ids) != set(TRACKS) or any(
        value is not None and (type(value) is not int or value <= 0)
        for value in ruler_ids.values()
    ):
        raise ValueError(
            "ruler_version_ids must contain three positive IDs or nulls"
        )
    values["ruler_version_ids"] = {
        track: ruler_ids[track] for track in TRACKS
    }
    return values


def _validated_trading_dates(
    trading_dates: Mapping[str, Sequence[str | date]],
) -> dict[str, list[str]]:
    if not isinstance(trading_dates, Mapping) or not trading_dates:
        raise ValueError("trading dates must be a non-empty exchange mapping")
    result: dict[str, list[str]] = {}
    for exchange, raw_dates in trading_dates.items():
        _nonempty_text(exchange, "exchange")
        if isinstance(raw_dates, (str, bytes)) or not isinstance(
            raw_dates, Sequence
        ):
            raise TypeError("exchange trading dates must be a sequence")
        normalized: list[str] = []
        for raw in raw_dates:
            if type(raw) is date:
                parsed = raw.isoformat()
            elif type(raw) is str:
                try:
                    parsed = date.fromisoformat(raw).isoformat()
                except ValueError as error:
                    raise ValueError(f"invalid trading date: {raw}") from error
                if parsed != raw:
                    raise ValueError(f"trading date is not canonical: {raw}")
            else:
                raise TypeError("trading dates must be ISO strings or dates")
            normalized.append(parsed)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("each exchange needs unique trading dates")
        result[exchange] = sorted(normalized)
    return dict(sorted(result.items()))


def _validate_run_metadata(
    run: DiscoveryRun, version_values: Mapping[str, object]
) -> None:
    ruler_ids = version_values["ruler_version_ids"]
    assert isinstance(ruler_ids, dict)
    expected = {
        "data_version": version_values["data_version"],
        "financial_version": version_values["financial_version"],
        "valuation_version": version_values["valuation_version"],
        "algorithm_version": version_values["algorithm_version"],
        "quality_discount_ruler_version_id": ruler_ids["quality_discount"],
        "deep_value_ruler_version_id": ruler_ids["deep_value"],
        "garp_ruler_version_id": ruler_ids["garp"],
    }
    if any(getattr(run, key) != value for key, value in expected.items()):
        raise ValueError("run metadata does not match supplied snapshot metadata")


def freeze_inputs(
    session: Session,
    run: DiscoveryRun,
    universe: Iterable[CompanySnapshotInput],
    versions: SnapshotVersions,
    trading_dates: Mapping[str, Sequence[str | date]],
    *,
    snapshot_at: datetime,
) -> None:
    """Materialize one immutable snapshot row for every supplied company."""

    state = sa_inspect(run)
    if (
        state.session is not session
        or not state.persistent
        or state.deleted
        or run in session.deleted
        or run.id is None
    ):
        raise ValueError("run must be persistent in the supplied session")
    if run.input_fingerprint is not None:
        raise ValueError("run inputs are already frozen")
    with session.no_autoflush:
        exists = session.scalar(
            select(DiscoveryRun.id).where(DiscoveryRun.id == run.id)
        )
        has_snapshots = session.scalar(
            select(func.count())
            .select_from(DiscoveryInputSnapshot)
            .where(DiscoveryInputSnapshot.run_id == run.id)
        )
    if exists is None:
        raise ValueError("run does not exist")
    if has_snapshots:
        raise ValueError("run inputs are already frozen")
    if any(
        isinstance(item, DiscoveryInputSnapshot) and item.run_id == run.id
        for item in session.new
    ):
        raise ValueError("run inputs are already frozen")

    version_values = _validated_versions(versions)
    _validate_run_metadata(run, version_values)
    _frozen_ruler_payloads(run)
    version_values["ruler_payloads"] = _load_json(run.ruler_payloads_json)
    normalized_dates = _validated_trading_dates(trading_dates)
    if (
        type(snapshot_at) is not datetime
        or snapshot_at.tzinfo is None
        or snapshot_at.utcoffset() is None
    ):
        raise ValueError("snapshot_at must be a timezone-aware datetime")
    captured_at = snapshot_at.astimezone(timezone.utc)

    companies = list(universe)
    if not companies:
        raise ValueError("cannot freeze an empty discovery universe")
    codes = [item.code for item in companies]
    if len(codes) != len(set(codes)):
        raise ValueError("discovery universe contains duplicate codes")

    ruler_ids = version_values["ruler_version_ids"]
    assert isinstance(ruler_ids, dict)
    rows: list[dict[str, object]] = []
    fingerprints: list[tuple[str, str]] = []
    for item in companies:
        code = _nonempty_text(item.code, "company code")
        name = _nonempty_text(item.name, f"{code} name")
        if item.industry is not None and type(item.industry) is not str:
            raise TypeError(f"{code} industry must be a string or None")
        security = _plain(item.security_status, set())
        fields = _plain(item.fields, set())
        sources = _plain(item.sources, set())
        as_of = _plain(item.as_of, set())
        if not all(
            isinstance(value, dict)
            for value in (security, fields, sources, as_of)
        ):
            raise TypeError("company payloads must be mappings")
        semantic = {
            "code": code,
            "name": name,
            "industry": item.industry,
            "security_status": security,
            "fields": fields,
            "sources": sources,
            "as_of": as_of,
            **version_values,
        }
        fingerprint = _sha256(semantic)
        fingerprints.append((code, fingerprint))
        rows.append(
            {
                "run_id": run.id,
                "code": code,
                "name": name,
                "industry": item.industry,
                "security_status": canonical_json(security),
                "fields_json": canonical_json(fields),
                "sources_json": canonical_json(sources),
                "as_of_json": canonical_json(as_of),
                "data_version": version_values["data_version"],
                "financial_version": version_values["financial_version"],
                "valuation_version": version_values["valuation_version"],
                "ruler_versions_json": canonical_json(ruler_ids),
                "algorithm_version": version_values["algorithm_version"],
                "input_fingerprint": fingerprint,
                "snapshot_at": captured_at,
            }
        )

    run_fingerprint = _sha256(
        {
            "companies": [
                {"code": code, "input_fingerprint": fingerprint}
                for code, fingerprint in sorted(fingerprints)
            ],
            "trading_dates": normalized_dates,
            **version_values,
        }
    )
    session.add_all(DiscoveryInputSnapshot(**values) for values in rows)
    run.trading_dates_json = canonical_json(normalized_dates)
    run.input_fingerprint = run_fingerprint
    run.snapshot_at = captured_at


def _load_json(text: str) -> PlainValue:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    value = json.loads(text, parse_constant=reject_constant)
    return _plain(value, set())


def _frozen(value: PlainValue) -> FrozenValue:
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


def _object_json(text: str, label: str) -> Mapping[str, FrozenValue]:
    value = _load_json(text)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    frozen = _frozen(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _run_ruler_ids(run: DiscoveryRun) -> Mapping[str, int | None]:
    values = {
        "quality_discount": run.quality_discount_ruler_version_id,
        "deep_value": run.deep_value_ruler_version_id,
        "garp": run.garp_ruler_version_id,
    }
    if any(
        value is not None and (type(value) is not int or value <= 0)
        for value in values.values()
    ):
        raise ValueError("frozen run has invalid ruler metadata")
    return MappingProxyType(values)


def freeze_discovery_ruler_payloads(
    session: Session,
    ruler_version_ids: Mapping[str, int | None],
    algorithm_version: str,
    *,
    user_id: int = 1,
) -> str:
    """Resolve and canonically freeze exact compatible ruler payloads."""

    ids = dict(ruler_version_ids)
    if set(ids) != set(TRACKS):
        raise ValueError("ruler IDs must contain every discovery track")
    frozen: dict[str, object] = {}
    for track in TRACKS:
        ruler_id = ids[track]
        if ruler_id is None:
            frozen[track] = None
            continue
        if type(ruler_id) is not int or ruler_id < 1:
            raise ValueError("ruler IDs must be positive integers or null")
        row = session.get(RulerVersion, ruler_id)
        if row is None or row.user_id != user_id:
            raise ValueError(f"{track} ruler is unavailable")
        try:
            ruler = validate_ruler_version(
                row,
                expected_track=track,
                expected_algorithm_version=algorithm_version,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{track} ruler payload is incompatible"
            ) from error
        payload = _load_json(canonical_ruler_payload(ruler))
        if type(payload) is not dict:
            raise ValueError(f"{track} ruler payload is incompatible")
        frozen[track] = {
            "id": ruler_id,
            "payload": payload,
            "sha256": row.digest,
        }
    return canonical_json(frozen)


def _frozen_ruler_payloads(
    run: DiscoveryRun,
) -> Mapping[str, FrozenValue]:
    if not run.ruler_payloads_json:
        raise ValueError(
            "frozen input integrity error: ruler payloads are unavailable"
        )
    value = _load_json(run.ruler_payloads_json)
    if type(value) is not dict or set(value) != set(TRACKS):
        raise ValueError(
            "frozen input integrity error: ruler payload set is malformed"
        )
    if canonical_json(value) != run.ruler_payloads_json:
        raise ValueError(
            "frozen input integrity error: ruler payloads are not canonical"
        )
    ids = _run_ruler_ids(run)
    for track in TRACKS:
        entry = value[track]
        if entry is None:
            if ids[track] is not None:
                raise ValueError(
                    "frozen input integrity error: ruler payload is missing"
                )
            continue
        if (
            type(entry) is not dict
            or set(entry) != {"id", "payload", "sha256"}
            or entry["id"] != ids[track]
            or type(entry["payload"]) is not dict
            or entry["payload"].get("scope") != "weekly-discovery"
            or entry["payload"].get("track") != track
            or entry["payload"].get("algorithm_version")
            != run.algorithm_version
            or entry["sha256"] != _sha256(entry["payload"])
        ):
            raise ValueError(
                "frozen input integrity error: ruler payload mismatch"
            )
    frozen = _frozen(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _require_utc(value: object, label: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"frozen input integrity error: {label} is not UTC")
    return value


def load_frozen_inputs(session: Session, run_id: int) -> FrozenRunInputs:
    """Load only persisted run and snapshot rows in deterministic code order."""

    run = session.get(DiscoveryRun, run_id)
    if run is None:
        raise ValueError("discovery run not found")
    if not run.input_fingerprint or not run.trading_dates_json:
        raise ValueError("discovery run has incomplete frozen inputs")
    run_snapshot_at = _require_utc(run.snapshot_at, "run snapshot_at")
    run_rulers = _run_ruler_ids(run)
    run_ruler_payloads = _frozen_ruler_payloads(run)
    plain_ruler_payloads = _load_json(run.ruler_payloads_json)
    try:
        trading_value = _load_json(run.trading_dates_json)
        if not isinstance(trading_value, dict):
            raise TypeError
        normalized_dates = _validated_trading_dates(trading_value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(
            "frozen input integrity error: trading dates are malformed"
        ) from error
    if canonical_json(normalized_dates) != run.trading_dates_json:
        raise ValueError(
            "frozen input integrity error: trading dates are not canonical"
        )
    trading_dates = {
        exchange: tuple(values)
        for exchange, values in normalized_dates.items()
    }

    rows = session.scalars(
        select(DiscoveryInputSnapshot)
        .where(DiscoveryInputSnapshot.run_id == run_id)
        .order_by(DiscoveryInputSnapshot.code)
    ).all()
    if not rows:
        raise ValueError("discovery run has no company snapshots")
    companies: list[FrozenCompanyInput] = []
    fingerprints: list[tuple[str, str]] = []
    for row in rows:
        if not row.ruler_versions_json:
            raise ValueError(
                "discovery run has incomplete frozen inputs: missing row rulers"
            )
        row_snapshot_at = _require_utc(row.snapshot_at, f"{row.code} snapshot_at")
        if row_snapshot_at != run_snapshot_at:
            raise ValueError(
                f"frozen input integrity error: {row.code} timestamp mismatch"
            )
        row_rulers = _object_json(row.ruler_versions_json, "ruler_versions_json")
        if canonical_json(dict(row_rulers)) != row.ruler_versions_json:
            raise ValueError(
                "frozen input integrity error: row rulers are not canonical"
            )
        if dict(row_rulers) != dict(run_rulers):
            raise ValueError(
                "frozen input integrity error: snapshot ruler metadata mismatch"
            )
        if (
            row.data_version != run.data_version
            or row.financial_version != run.financial_version
            or row.valuation_version != run.valuation_version
            or row.algorithm_version != run.algorithm_version
        ):
            raise ValueError(
                "frozen input integrity error: snapshot version metadata mismatch"
            )
        security = _object_json(row.security_status, "security_status")
        fields = _object_json(row.fields_json, "fields_json")
        sources = _object_json(row.sources_json, "sources_json")
        as_of = _object_json(row.as_of_json, "as_of_json")
        for label, value, persisted in (
            ("security status", security, row.security_status),
            ("fields", fields, row.fields_json),
            ("sources", sources, row.sources_json),
            ("as-of", as_of, row.as_of_json),
        ):
            if canonical_json(dict(value)) != persisted:
                raise ValueError(
                    f"frozen input integrity error: {label} is not canonical"
                )
        semantic = {
            "code": row.code,
            "name": row.name,
            "industry": row.industry,
            "security_status": dict(security),
            "fields": dict(fields),
            "sources": dict(sources),
            "as_of": dict(as_of),
            "data_version": row.data_version,
            "financial_version": row.financial_version,
            "valuation_version": row.valuation_version,
            "ruler_version_ids": dict(row_rulers),
            "ruler_payloads": plain_ruler_payloads,
            "algorithm_version": row.algorithm_version,
        }
        fingerprint = _sha256(semantic)
        if fingerprint != row.input_fingerprint:
            raise ValueError(
                f"frozen input integrity error: {row.code} fingerprint mismatch"
            )
        fingerprints.append((row.code, fingerprint))
        companies.append(
            FrozenCompanyInput(
                code=row.code,
                name=row.name,
                industry=row.industry,
                security_status=security,
                fields=fields,
                sources=sources,
                as_of=as_of,
                data_version=row.data_version,
                financial_version=row.financial_version,
                valuation_version=row.valuation_version,
                ruler_version_ids=MappingProxyType(
                    {
                        key: int(value) if value is not None else None
                        for key, value in row_rulers.items()
                    }
                ),
                algorithm_version=row.algorithm_version,
                input_fingerprint=row.input_fingerprint,
                snapshot_at=row_snapshot_at,
            )
        )
    expected_run_fingerprint = _sha256(
        {
            "companies": [
                {"code": code, "input_fingerprint": fingerprint}
                for code, fingerprint in sorted(fingerprints)
            ],
            "trading_dates": normalized_dates,
            "data_version": run.data_version,
            "financial_version": run.financial_version,
            "valuation_version": run.valuation_version,
            "ruler_version_ids": dict(run_rulers),
            "ruler_payloads": plain_ruler_payloads,
            "algorithm_version": run.algorithm_version,
        }
    )
    if expected_run_fingerprint != run.input_fingerprint:
        raise ValueError("frozen input integrity error: run fingerprint mismatch")
    return FrozenRunInputs(
        run_id=run.id,
        input_fingerprint=run.input_fingerprint,
        data_version=run.data_version,
        financial_version=run.financial_version,
        valuation_version=run.valuation_version,
        ruler_version_ids=run_rulers,
        ruler_payloads=run_ruler_payloads,
        algorithm_version=run.algorithm_version,
        trading_dates=MappingProxyType(trading_dates),
        snapshot_at=run_snapshot_at,
        companies=tuple(companies),
    )


def resolve_discovery_rulers(
    session: Session,
    *,
    algorithm_version: str = BUILD_VERSION,
    user_id: int = 1,
) -> Mapping[str, int | None]:
    """Resolve only explicitly scoped, build-compatible discovery rulers."""

    resolved: dict[str, int | None] = {track: None for track in TRACKS}

    for track in TRACKS:
        row = latest_discovery_ruler(
            session,
            track,
            user_id=user_id,
            algorithm_version=algorithm_version,
        )
        if row is not None:
            resolved[track] = row.id
    return MappingProxyType(resolved)


def _snapshot_version(prefix: str, value: object) -> str:
    return f"{prefix}:{_sha256(value)[:20]}"


def build_production_snapshot(
    session: Session,
    *,
    algorithm_version: str = BUILD_VERSION,
    expected_lock_job_id: int | None = None,
    snapshot_at: datetime | None = None,
) -> ProductionSnapshot:
    """Bulk-read the latest completed discovery fill without hiding gaps."""

    lock = session.get(JobLock, 1)
    if lock is not None and lock.job_id is not None:
        adopted = (
            expected_lock_job_id is not None
            and lock.job_id == expected_lock_job_id
            and lock.job_type == "discovery"
        )
        if not adopted:
            raise ValueError("discovery data is being mutated")
    fill = session.scalars(
        select(DiscoveryDataFill)
        .order_by(DiscoveryDataFill.id.desc())
        .limit(1)
    ).first()
    if (
        fill is not None
        and fill.payload_schema_version != COMPANY_PAYLOAD_SCHEMA_VERSION
    ):
        raise ValueError(PAYLOAD_SCHEMA_RESTART_GUIDANCE)
    if (
        fill is not None
        and fill.status == "snapshot_unavailable"
        and fill.last_error
    ):
        raise ValueError(fill.last_error)
    if (
        fill is None
        or fill.status != "done"
        or fill.snapshot_status != "ready"
    ):
        raise ValueError("latest discovery data fill is not ready")

    securities = session.scalars(
        select(DiscoveryUniverseSecurity)
        .where(DiscoveryUniverseSecurity.fill_id == fill.id)
        .order_by(DiscoveryUniverseSecurity.code)
    ).all()
    if not securities:
        raise ValueError("completed discovery data snapshot is empty")
    checkpoints = session.scalars(
        select(DiscoveryDataCheckpoint)
        .where(DiscoveryDataCheckpoint.fill_id == fill.id)
        .order_by(DiscoveryDataCheckpoint.code)
    ).all()
    security_codes = {row.code for row in securities}
    checkpoint_codes = {row.code for row in checkpoints}
    if security_codes != checkpoint_codes:
        raise ValueError(
            "discovery checkpoint and security snapshots do not match"
        )
    cutoff = fill.updated_at.astimezone(timezone.utc)
    cutoff_date = cutoff.astimezone(CHINA_MARKET_TIMEZONE).date()
    fill_start = fill.started_at.astimezone(timezone.utc)
    payload_by_code: dict[str, dict[str, object]] = {}
    payload_text_by_code: dict[str, str] = {}
    payload_captured_at: dict[str, datetime] = {}
    restart_guidance = (
        "latest discovery fill checkpoint payload is not reproducible; "
        "restart discovery data fill"
    )

    def valid_financial_date(value: object) -> bool:
        if type(value) is not str or not value:
            return False
        try:
            return date.fromisoformat(value).isoformat() == value and (
                date.fromisoformat(value) <= cutoff_date
            )
        except ValueError:
            return False

    def valid_period(value: object) -> bool:
        if type(value) is not str or not value:
            return False
        try:
            return date.fromisoformat(value) <= cutoff_date
        except ValueError:
            pass
        if len(value) == 4 and value.isdigit():
            return int(value) <= cutoff_date.year
        if len(value) == 7 and value[4] == "-":
            try:
                year, month = (int(part) for part in value.split("-", 1))
                return date(year, month, 1) <= cutoff_date
            except ValueError:
                return False
        if (
            len(value) == 6
            and value[:4].isdigit()
            and value[4] == "Q"
            and value[5] in "1234"
        ):
            quarter_month = int(value[5]) * 3
            return date(int(value[:4]), quarter_month, 1) <= cutoff_date
        return False

    def valid_payload_date(value: object, *, nullable: bool = False) -> bool:
        if nullable and value is None:
            return True
        return valid_financial_date(value)

    if any(
        not (
            fill_start
            <= security.snapshot_at.astimezone(timezone.utc)
            <= cutoff
        )
        or not valid_payload_date(security.listing_date, nullable=True)
        or not valid_payload_date(security.last_trade_date, nullable=True)
        or not valid_payload_date(security.source_as_of, nullable=True)
        for security in securities
    ) or any(
        not (
            fill_start
            <= checkpoint.updated_at.astimezone(timezone.utc)
            <= cutoff
        )
        for checkpoint in checkpoints
    ):
        raise ValueError("discovery fill timestamps exceed fill cutoff")

    for checkpoint in checkpoints:
        if checkpoint.status != "done":
            continue
        if not verify_company_payload_digest(
            checkpoint.payload_json,
            checkpoint.payload_digest,
        ):
            raise ValueError(restart_guidance)
        try:
            payload = parse_company_payload(
                checkpoint.payload_json,
                expected_code=checkpoint.code,
            )
            captured_at = datetime.fromisoformat(
                payload["captured_at"]
            ).astimezone(timezone.utc)
            checkpoint_at = checkpoint.updated_at.astimezone(timezone.utc)
            if captured_at != checkpoint_at or captured_at > cutoff:
                raise ValueError("payload capture is outside fill cutoff")
            source_as_of = payload["source_as_of"]
            security_state = payload["security_state"]
            if not all(
                (
                    valid_payload_date(source_as_of["security"]),
                    valid_payload_date(
                        source_as_of["financial"], nullable=True
                    ),
                    valid_payload_date(
                        source_as_of["valuation"], nullable=True
                    ),
                    valid_payload_date(security_state["listing_date"]),
                    valid_payload_date(security_state["last_trade_date"]),
                    valid_payload_date(security_state["industry_as_of"]),
                )
            ):
                raise ValueError("payload source dates exceed fill cutoff")
            for financial in payload["financial_rows"]:
                if not (
                    valid_period(financial["period"])
                    and valid_payload_date(
                        financial["source_as_of"], nullable=True
                    )
                ):
                    raise ValueError("payload financial dates exceed fill cutoff")
            for valuation in payload["valuation_points"]:
                if not (
                    valid_period(valuation["month"])
                    and valid_payload_date(valuation["trade_date"])
                ):
                    raise ValueError("payload valuation dates exceed fill cutoff")
            if (
                len(payload["valuation_points"])
                != checkpoint.valuation_points
            ):
                raise ValueError("payload valuation count mismatch")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(restart_guidance) from error
        payload_by_code[checkpoint.code] = payload
        payload_text_by_code[checkpoint.code] = checkpoint.payload_json
        payload_captured_at[checkpoint.code] = captured_at

    dates = tuple(
        session.scalars(
            select(DiscoveryFillTradingDate.trade_date)
            .where(DiscoveryFillTradingDate.fill_id == fill.id)
            .order_by(DiscoveryFillTradingDate.trade_date)
        ).all()
    )
    if not dates:
        raise ValueError("completed discovery data calendar is empty")
    try:
        normalized_dates = tuple(
            date.fromisoformat(value).isoformat() for value in dates
        )
    except (TypeError, ValueError) as error:
        raise ValueError("discovery fill calendar is invalid") from error
    if (
        normalized_dates != dates
        or tuple(sorted(set(normalized_dates))) != dates
        or any(value > cutoff_date.isoformat() for value in normalized_dates)
    ):
        raise ValueError("discovery fill calendar exceeds fill cutoff")

    financial_by_code: dict[str, list[dict[str, object]]] = {
        code: [] for code in security_codes
    }
    valuation_by_code: dict[str, list[dict[str, object]]] = {
        code: [] for code in security_codes
    }
    for code, payload in payload_by_code.items():
        for row in payload["financial_rows"]:
            field_sources = row["field_sources"]
            trusted = {
                field: metadata
                for field, metadata in field_sources.items()
                if metadata["trusted"] is True
            }
            financial_by_code[code].append(
                {
                    "period": row["period"],
                    "period_type": row["period_type"],
                    "source_as_of": row["source_as_of"],
                    "values": {
                        field: value if field in trusted else None
                        for field, value in row["values"].items()
                    },
                    "provenance": {
                        field: ["discovery"] for field in trusted
                    },
                    "field_sources": trusted,
                }
            )
        financial_by_code[code].sort(
            key=lambda row: (row["period"], row["period_type"])
        )
        captured_at = payload_captured_at[code]
        valuation_by_code[code] = sorted(
            (
                {
                    **point,
                    "fetched_at": captured_at,
                }
                for point in payload["valuation_points"]
            ),
            key=lambda row: (row["trade_date"], row["month"]),
        )

    companies: list[CompanySnapshotInput] = []
    for security in securities:
        financial_history = financial_by_code[security.code]
        valuation_history = valuation_by_code[security.code]
        latest = valuation_history[-1] if valuation_history else None
        price = None if latest is None else latest["price"]
        price_source = None if latest is None else latest["source"]
        price_as_of = None if latest is None else latest["trade_date"]
        payload = payload_by_code.get(security.code)
        source_as_of = (
            {"financial": None, "valuation": None}
            if payload is None
            else payload["source_as_of"]
        )
        valuation_fetched_at = (
            None
            if payload is None
            else payload_captured_at[security.code]
        )
        companies.append(
            CompanySnapshotInput(
                code=security.code,
                name=security.name,
                industry=security.industry,
                security_status={
                    "exchange": security.exchange,
                    "listing_date": security.listing_date,
                    "is_st": security.is_st,
                    "is_delisting": security.is_delisting,
                    "trading_status": security.trading_status,
                    "is_suspended": security.is_suspended,
                    "last_trade_date": security.last_trade_date,
                    "industry_version": security.industry_version,
                    "industry_as_of": security.source_as_of,
                    "security_source": security.provenance,
                    "market_status_code": security.market_status_code,
                    "snapshot_at": security.snapshot_at,
                },
                fields={
                    "price": price,
                    "pe": None if latest is None else latest["pe"],
                    "pb": None if latest is None else latest["pb"],
                    "listing_date": security.listing_date,
                    "financial_history": financial_history,
                    "valuation_history": valuation_history,
                },
                sources={
                    "price": price_source,
                    "security": security.provenance,
                    "financial_history": "checkpoint_payload:field_sources",
                    "valuation_history": sorted(
                        {
                            str(row["source"])
                            for row in valuation_history
                        }
                    ),
                },
                as_of={
                    "price": price_as_of,
                    "security": security.source_as_of,
                    "financial": source_as_of["financial"],
                    "valuation": source_as_of["valuation"],
                    "valuation_fetched_at": valuation_fetched_at,
                    "fill": fill.updated_at,
                },
            )
        )

    exchanges = {
        exchange
        for row in securities
        if (exchange := row.exchange or row.code.rsplit(".", 1)[-1])
        in {"SH", "SZ"}
    }
    if not exchanges:
        raise ValueError("completed discovery data has no supported exchange")
    trading_dates = MappingProxyType(
        {exchange: dates for exchange in sorted(exchanges)}
    )
    rulers = resolve_discovery_rulers(
        session, algorithm_version=algorithm_version
    )
    boundary = {
        "fill_id": fill.id,
        "cutoff": cutoff,
        "checkpoints": [
            {
                "code": row.code,
                "status": row.status,
                "valuation_points": row.valuation_points,
                "payload_json": payload_text_by_code.get(row.code),
            }
            for row in checkpoints
        ],
        "securities": [
            {
                column.name: getattr(row, column.name)
                for column in DiscoveryUniverseSecurity.__table__.columns
            }
            for row in securities
        ],
        "trading_dates": dates,
    }
    versions = SnapshotVersions(
        data_version=(
            f"discovery-fill:{fill.id}:{cutoff.isoformat()}:"
            f"{_sha256(boundary)[:20]}"
        ),
        financial_version=_snapshot_version(
            f"financial:fill:{fill.id}:{cutoff.isoformat()}",
            {
                code: payload["financial_rows"]
                for code, payload in sorted(payload_by_code.items())
            },
        ),
        valuation_version=_snapshot_version(
            f"valuation:fill:{fill.id}:{cutoff.isoformat()}",
            {
                code: payload["valuation_points"]
                for code, payload in sorted(payload_by_code.items())
            },
        ),
        ruler_version_ids=rulers,
        algorithm_version=algorithm_version,
    )
    captured_at = cutoff
    return ProductionSnapshot(
        fill_id=fill.id,
        companies=tuple(companies),
        versions=versions,
        trading_dates=trading_dates,
        snapshot_at=captured_at,
    )


def _history_row(values: Mapping[str, object]) -> Mapping[str, FrozenValue]:
    plain = _plain(values, set())
    assert isinstance(plain, dict)
    frozen = _frozen(plain)
    assert isinstance(frozen, Mapping)
    return frozen


def _track_order(column: object):
    return case(
        {track: index for index, track in enumerate(TRACKS)},
        value=column,
        else_=len(TRACKS),
    )


def _stage_order(column: object):
    stages = ("common", "track_gate", "competition", "adjudication")
    return case(
        {stage: index for index, stage in enumerate(stages)},
        value=column,
        else_=len(stages),
    )


def load_historical_replay(session: Session, run_id: int) -> HistoricalReplay:
    """Read persisted history without importing or invoking rule execution."""

    if session.get(DiscoveryRun, run_id) is None:
        raise ValueError("discovery run not found")
    common_stages = session.scalars(
        select(DiscoveryCommonStage)
        .where(DiscoveryCommonStage.run_id == run_id)
        .order_by(DiscoveryCommonStage.attempt, DiscoveryCommonStage.id)
    ).all()
    track_stages = session.scalars(
        select(DiscoveryStage)
        .where(DiscoveryStage.run_id == run_id)
        .order_by(
            _track_order(DiscoveryStage.track),
            _stage_order(DiscoveryStage.stage),
            DiscoveryStage.attempt,
            DiscoveryStage.id,
        )
    ).all()
    common = session.execute(
        select(DiscoveryCommonResult, DiscoveryCommonStage)
        .join(
            DiscoveryCommonStage,
            (DiscoveryCommonStage.run_id == DiscoveryCommonResult.run_id)
            & (DiscoveryCommonStage.attempt == DiscoveryCommonResult.attempt),
        )
        .where(DiscoveryCommonResult.run_id == run_id)
        .order_by(
            DiscoveryCommonResult.attempt,
            DiscoveryCommonResult.code,
            DiscoveryCommonResult.id,
        )
    ).all()
    track = session.execute(
        select(DiscoveryResult, DiscoveryStage)
        .join(
            DiscoveryStage,
            (DiscoveryStage.run_id == DiscoveryResult.run_id)
            & (DiscoveryStage.track == DiscoveryResult.track)
            & (DiscoveryStage.stage == DiscoveryResult.stage)
            & (DiscoveryStage.attempt == DiscoveryResult.attempt),
        )
        .where(DiscoveryResult.run_id == run_id)
        .order_by(
            _track_order(DiscoveryResult.track),
            _stage_order(DiscoveryResult.stage),
            DiscoveryResult.attempt,
            DiscoveryResult.code,
            DiscoveryResult.id,
        )
    ).all()
    overrides = session.execute(
        select(DiscoveryOverride, DiscoveryStage)
        .join(
            DiscoveryStage,
            (DiscoveryStage.run_id == DiscoveryOverride.run_id)
            & (DiscoveryStage.track == DiscoveryOverride.track)
            & (DiscoveryStage.stage == DiscoveryOverride.stage)
            & (DiscoveryStage.attempt == DiscoveryOverride.attempt),
        )
        .where(DiscoveryOverride.run_id == run_id)
        .order_by(
            _track_order(DiscoveryOverride.track),
            _stage_order(DiscoveryOverride.stage),
            DiscoveryOverride.attempt,
            DiscoveryOverride.code,
            DiscoveryOverride.id,
        )
    ).all()
    decisions = session.execute(
        select(DiscoveryDecision, DiscoveryStage)
        .join(
            DiscoveryStage,
            DiscoveryStage.id == DiscoveryDecision.competition_stage_id,
        )
        .where(DiscoveryDecision.run_id == run_id)
        .order_by(
            _track_order(DiscoveryDecision.track),
            DiscoveryDecision.competition_attempt,
            DiscoveryDecision.code,
            DiscoveryDecision.id,
        )
    ).all()
    evidence = session.execute(
        select(DiscoveryEvidence, DiscoveryStage)
        .join(
            DiscoveryStage,
            DiscoveryStage.id == DiscoveryEvidence.competition_stage_id,
        )
        .where(DiscoveryEvidence.run_id == run_id)
        .order_by(
            _track_order(DiscoveryEvidence.track),
            DiscoveryEvidence.competition_attempt,
            DiscoveryEvidence.code,
            DiscoveryEvidence.kind,
            DiscoveryEvidence.rule_id,
            DiscoveryEvidence.id,
        )
    ).all()

    return HistoricalReplay(
        common_stages=tuple(
            _history_row(
                {
                    "stage": "common",
                    "attempt": stage.attempt,
                    "status": stage.status,
                    "total_count": stage.total_count,
                    "pass_count": stage.pass_count,
                    "drop_count": stage.drop_count,
                    "insufficient_count": stage.insufficient_count,
                    "error": stage.error,
                    "confirmed_at": stage.confirmed_at,
                    "input_fingerprint": stage.input_fingerprint,
                    "algorithm_version": stage.algorithm_version,
                    "retry_of_stage_id": stage.retry_of_stage_id,
                    "failure_kind": stage.failure_kind,
                    "superseded": stage.superseded_at is not None,
                    "superseded_at": stage.superseded_at,
                    "created_at": stage.created_at,
                    "updated_at": stage.updated_at,
                }
            )
            for stage in common_stages
        ),
        common_results=tuple(
            _history_row(
                {
                    "attempt": row.attempt,
                    "code": row.code,
                    "verdict": row.verdict,
                    "reason": row.reason,
                    "metrics": _load_json(row.metrics_json),
                    "created_at": row.created_at,
                    "stage_status": stage.status,
                    "superseded": stage.superseded_at is not None,
                }
            )
            for row, stage in common
        ),
        track_stages=tuple(
            _history_row(
                {
                    "track": stage.track,
                    "stage": stage.stage,
                    "attempt": stage.attempt,
                    "status": stage.status,
                    "total_count": stage.total_count,
                    "pass_count": stage.pass_count,
                    "drop_count": stage.drop_count,
                    "insufficient_count": stage.insufficient_count,
                    "error": stage.error,
                    "confirmed_at": stage.confirmed_at,
                    "input_fingerprint": stage.input_fingerprint,
                    "ruler_version_id": stage.ruler_version_id,
                    "algorithm_version": stage.algorithm_version,
                    "common_stage_id": stage.common_stage_id,
                    "predecessor_stage_id": stage.predecessor_stage_id,
                    "predecessor_stage": stage.predecessor_stage,
                    "retry_of_stage_id": stage.retry_of_stage_id,
                    "reopen_of_stage_id": stage.reopen_of_stage_id,
                    "failure_kind": stage.failure_kind,
                    "superseded": stage.superseded_at is not None,
                    "superseded_at": stage.superseded_at,
                    "created_at": stage.created_at,
                    "updated_at": stage.updated_at,
                }
            )
            for stage in track_stages
        ),
        track_results=tuple(
            _history_row(
                {
                    "track": row.track,
                    "stage": row.stage,
                    "attempt": row.attempt,
                    "code": row.code,
                    "verdict": row.verdict,
                    "reason": row.reason,
                    "metrics": _load_json(row.metrics_json),
                    "rank": row.rank,
                    "created_at": row.created_at,
                    "stage_status": stage.status,
                    "superseded": stage.superseded_at is not None,
                }
            )
            for row, stage in track
        ),
        overrides=tuple(
            _history_row(
                {
                    "track": row.track,
                    "stage": row.stage,
                    "attempt": row.attempt,
                    "code": row.code,
                    "action": row.action,
                    "reason": row.reason,
                    "created_at": row.created_at,
                    "stage_status": stage.status,
                    "superseded": stage.superseded_at is not None,
                }
            )
            for row, stage in overrides
        ),
        decisions=tuple(
            _history_row(
                {
                    "track": row.track,
                    "attempt": row.competition_attempt,
                    "competition_stage_id": row.competition_stage_id,
                    "code": row.code,
                    "decision": row.decision,
                    "exclusion_reason": row.exclusion_reason,
                    "keep_note": row.keep_note,
                    "created_at": row.created_at,
                    "stage_status": stage.status,
                    "superseded": stage.superseded_at is not None,
                }
            )
            for row, stage in decisions
        ),
        evidence=tuple(
            _history_row(
                {
                    "track": row.track,
                    "attempt": row.competition_attempt,
                    "competition_stage_id": row.competition_stage_id,
                    "code": row.code,
                    "ruler_version_id": row.ruler_version_id,
                    "rule_version": row.rule_version,
                    "kind": row.kind,
                    "rule_id": row.rule_id,
                    "input_fields": _load_json(row.input_fields_json),
                    "display_text": row.display_text,
                    "as_of": row.as_of,
                    "data_completeness": row.data_completeness,
                    "created_at": row.created_at,
                    "stage_status": stage.status,
                    "superseded": stage.superseded_at is not None,
                }
            )
            for row, stage in evidence
        ),
    )
