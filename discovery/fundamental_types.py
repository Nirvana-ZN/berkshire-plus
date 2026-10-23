"""Immutable selection evidence and calculation contracts.

Amounts retain their declared units. Identity checks on persisted evidence and
date-only availability rules belong to the repository boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from types import MappingProxyType


_STATUSES = frozenset({"verified", "missing", "untrusted", "not_applicable"})
_OPERATING_STATES = frozenset({"improving", "stable", "deteriorating", "unknown"})
_CAPITAL_KINDS = frozenset({
    "split", "bonus", "consolidation", "issuance", "rights_issue",
    "stock_compensation", "buyback_cancel",
})


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _optional_text(value: object, name: str) -> None:
    if value is not None:
        _text(value, name)


def _enum(value: object, allowed: frozenset[str], name: str) -> None:
    _text(value, name)
    if value not in allowed:
        raise ValueError(f"invalid {name}")


def _aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")


def _boolean(value: object, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _items(value: object, item_type: type, name: str) -> tuple:
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(f"{name} must be a sequence of {item_type.__name__}")
    try:
        result = tuple(value)
    except TypeError as error:
        raise ValueError(f"{name} must be a sequence") from error
    for item in result:
        if not isinstance(item, item_type):
            raise ValueError(f"invalid {name} item")
        if item_type is str:
            _text(item, name)
    return result


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    code: str
    kind: str
    period: str
    published_at: datetime
    observed_at: datetime
    source_updated_at: datetime | None
    source_url: str
    document_sha256: str
    values: Mapping[str, str | None]
    status: str
    basis_id: str
    supersedes_id: str | None = None
    date_only: bool = False
    archive_verified: bool = False

    def __post_init__(self) -> None:
        for name in ("evidence_id", "code", "kind", "period", "source_url",
                     "document_sha256", "basis_id"):
            _text(getattr(self, name), name)
        _enum(self.status, _STATUSES, "evidence status")
        _optional_text(self.supersedes_id, "supersedes_id")
        _aware(self.published_at, "published_at")
        _aware(self.observed_at, "observed_at")
        if self.source_updated_at is not None:
            _aware(self.source_updated_at, "source_updated_at")
        _boolean(self.date_only, "date_only")
        _boolean(self.archive_verified, "archive_verified")
        if not isinstance(self.values, Mapping):
            raise ValueError("values must be a mapping")
        values = dict(self.values)
        for key, value in values.items():
            _text(key, "values key")
            if value is not None and not isinstance(value, str):
                raise ValueError("evidence values must be text or None")
        object.__setattr__(self, "values", MappingProxyType(values))


@dataclass(frozen=True)
class CoverageWindow:
    code: str
    kind: str
    start: date
    end: date
    complete: bool
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.code, "code")
        _text(self.kind, "kind")
        if type(self.start) is not date or type(self.end) is not date:
            raise ValueError("coverage boundaries must be dates")
        if self.start > self.end:
            raise ValueError("coverage start must not follow end")
        _boolean(self.complete, "complete")
        object.__setattr__(self, "evidence_ids", _items(self.evidence_ids, str, "evidence_ids"))


@dataclass(frozen=True)
class CapitalEvent:
    event_id: str
    code: str
    kind: str
    effective_at: datetime
    sequence: int
    shares_before: Decimal
    shares_after: Decimal
    factor: Decimal
    evidence_id: str

    def __post_init__(self) -> None:
        for name in ("event_id", "code", "evidence_id"):
            _text(getattr(self, name), name)
        _enum(self.kind, _CAPITAL_KINDS, "capital event kind")
        _aware(self.effective_at, "effective_at")
        if type(self.sequence) is not int:
            raise ValueError("sequence must be an integer")
        for name in ("shares_before", "shares_after", "factor"):
            _decimal(getattr(self, name), name)


@dataclass(frozen=True)
class MetricResult:
    value: Decimal | None
    status: str
    reason: str | None
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _enum(self.status, _STATUSES, "metric status")
        if self.value is not None:
            _decimal(self.value, "metric value")
        if (self.status == "verified") != (self.value is not None):
            raise ValueError("value/status mismatch")
        _optional_text(self.reason, "reason")
        object.__setattr__(self, "evidence_ids", _items(self.evidence_ids, str, "evidence_ids"))


@dataclass(frozen=True)
class OperatingResult:
    state: str
    period: str | None
    reasons: tuple[str, ...]
    metrics: Mapping[str, MetricResult]

    def __post_init__(self) -> None:
        _enum(self.state, _OPERATING_STATES, "operating state")
        _optional_text(self.period, "period")
        object.__setattr__(self, "reasons", _items(self.reasons, str, "reasons"))
        if not isinstance(self.metrics, Mapping):
            raise ValueError("metrics must be a mapping")
        metrics = dict(self.metrics)
        for key, value in metrics.items():
            _text(key, "metric name")
            if not isinstance(value, MetricResult):
                raise ValueError("metrics must contain MetricResult values")
        object.__setattr__(self, "metrics", MappingProxyType(metrics))


@dataclass(frozen=True)
class FundamentalSnapshot:
    code: str
    cutoff_at: datetime
    annuals: tuple[EvidenceRecord, ...]
    interims: tuple[EvidenceRecord, ...]
    actions: tuple[CapitalEvent, ...]
    extensions: tuple[EvidenceRecord, ...]
    coverage: tuple[CoverageWindow, ...]
    current_mcap: Decimal | None
    fingerprint: str

    def __post_init__(self) -> None:
        _text(self.code, "code")
        _text(self.fingerprint, "fingerprint")
        _aware(self.cutoff_at, "cutoff_at")
        for name, item_type in (("annuals", EvidenceRecord), ("interims", EvidenceRecord),
                                ("actions", CapitalEvent), ("extensions", EvidenceRecord),
                                ("coverage", CoverageWindow)):
            object.__setattr__(self, name, _items(getattr(self, name), item_type, name))
        if self.current_mcap is not None:
            _decimal(self.current_mcap, "current_mcap")


def evidence_identity(record: EvidenceRecord) -> str:
    """Hash every evidence field except its ID using canonical UTC JSON."""
    payload = {}
    for field in fields(record):
        if field.name == "evidence_id":
            continue
        value = getattr(record, field.name)
        if isinstance(value, datetime):
            value = value.astimezone(timezone.utc).isoformat()
        elif isinstance(value, Mapping):
            value = dict(value)
        payload[field.name] = value
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def validate_manual_verification(record: EvidenceRecord) -> None:
    """Reject invalid asserted manual trust without changing retained content.

    Internal/provider records need not carry a manual receipt. When one exists,
    a verified version must have observed the review itself, not just the source.
    Readers call this only after cutoff/version selection so a later honest
    correction can supersede an invalid legacy version without rewriting it.
    """
    if record.status != 'verified' or '_verification' not in record.values:
        return
    try:
        receipt = json.loads(record.values['_verification'])
        if not isinstance(receipt, dict) or receipt.get('method') != 'human_review':
            raise ValueError('invalid manual receipt')
        _text(receipt.get('reviewer'), 'reviewer')
        reviewed = datetime.fromisoformat(receipt['reviewed_at'].replace('Z', '+00:00'))
        _aware(reviewed, 'reviewed_at')
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f'manual_review_invalid:{record.evidence_id}') from error
    if reviewed > record.observed_at:
        raise ValueError(f'manual_review_after_observation:{record.evidence_id}')
