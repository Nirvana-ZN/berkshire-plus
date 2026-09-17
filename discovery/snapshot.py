"""Immutable discovery input snapshots and persisted historical replay.

Database IDs and ``snapshot_at`` are intentionally excluded from fingerprints:
they identify storage and capture time, not the inputs that affect selection.
Decimal, date, and datetime values deliberately normalize to canonical persisted
text; their original Python runtime types are not downstream input semantics.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from sqlalchemy import case, func, inspect as sa_inspect, select
from sqlalchemy.orm import Session

from app.discovery_models import (
    DiscoveryCommonResult,
    DiscoveryCommonStage,
    DiscoveryDecision,
    DiscoveryEvidence,
    DiscoveryInputSnapshot,
    DiscoveryResult,
    DiscoveryRun,
    DiscoveryStage,
)


TRACKS = ("quality_discount", "deep_value", "garp")
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
    ruler_version_ids: Mapping[str, int]
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
    ruler_version_ids: Mapping[str, int]
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
    ruler_version_ids: Mapping[str, int]
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
    decisions: tuple[Mapping[str, FrozenValue], ...]
    evidence: tuple[Mapping[str, FrozenValue], ...]


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
        type(value) is not int or value <= 0 for value in ruler_ids.values()
    ):
        raise ValueError("ruler_version_ids must contain three positive IDs")
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


def _run_ruler_ids(run: DiscoveryRun) -> Mapping[str, int]:
    values = {
        "quality_discount": run.quality_discount_ruler_version_id,
        "deep_value": run.deep_value_ruler_version_id,
        "garp": run.garp_ruler_version_id,
    }
    if any(type(value) is not int or value <= 0 for value in values.values()):
        raise ValueError("frozen run has incomplete ruler metadata")
    return MappingProxyType(values)  # type: ignore[arg-type]


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
                    {key: int(value) for key, value in row_rulers.items()}
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
        algorithm_version=run.algorithm_version,
        trading_dates=MappingProxyType(trading_dates),
        snapshot_at=run_snapshot_at,
        companies=tuple(companies),
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
        decisions=tuple(
            _history_row(
                {
                    "track": row.track,
                    "attempt": row.competition_attempt,
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
