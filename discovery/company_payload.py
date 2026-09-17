"""Canonical, fill-scoped persistence for one discovery company fetch."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import fields
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from discovery.field_dictionary import FIELD_DICTIONARY, FieldSpec
from market.types import DiscoveryCompanyData, FinancialRow, SecurityState

SCHEMA_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FINANCIAL_FIELDS = tuple(
    field.name
    for field in fields(FinancialRow)
    if field.name not in {"code", "period", "period_type", "source_as_of"}
)
_ROOT_KEYS = {
    "schema_version",
    "captured_at",
    "code",
    "security_state",
    "source_as_of",
    "financial_rows",
    "valuation_points",
}


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Decimal values must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _plain(value: object) -> object:
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
            raise ValueError("captured_at must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if type(value) in {list, tuple}:
        return [_plain(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("payload keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    raise TypeError(f"unsupported payload value: {type(value).__name__}")


def _canonical(value: object) -> str:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def company_payload_digest(text: str) -> str:
    """Return the identity of exact canonical UTF-8 payload bytes."""

    if type(text) is not str:
        raise TypeError("company payload must be text")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_company_payload_digest(text: object, digest: object) -> bool:
    """Validate digest shape and exact payload identity."""

    return (
        type(text) is str
        and type(digest) is str
        and _DIGEST.fullmatch(digest) is not None
        and hmac.compare_digest(company_payload_digest(text), digest)
    )


def _source_metadata(spec: FieldSpec) -> dict[str, object]:
    return {
        "source": "discovery",
        "report": spec.report,
        "field": spec.field,
        "unit": spec.unit,
        "consolidated_basis": spec.consolidated_basis,
        "period_type": spec.period_type,
        "financial_sector_substitute": spec.financial_sector_substitute,
        "source_as_of": spec.source_as_of,
        "trusted": spec.trusted,
    }


def serialize_company_payload(
    bundle: DiscoveryCompanyData,
    *,
    captured_at: datetime,
) -> str:
    """Serialize exactly one successful typed fetch without request internals."""

    if bundle.code != bundle.security_state.code or any(
        row.code != bundle.code
        for row in (*bundle.financial_rows, *bundle.valuation_points)
    ):
        raise ValueError("discovery company bundle code mismatch")
    financial_rows = []
    for row in bundle.financial_rows:
        values = {field: getattr(row, field) for field in _FINANCIAL_FIELDS}
        financial_rows.append(
            {
                "period": row.period,
                "period_type": row.period_type,
                "source_as_of": row.source_as_of,
                "values": values,
                "field_sources": {
                    field: _source_metadata(FIELD_DICTIONARY[field])
                    for field, value in values.items()
                    if value is not None and field in FIELD_DICTIONARY
                },
            }
        )
    state = bundle.security_state
    payload = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured_at,
        "code": bundle.code,
        "security_state": {
            field.name: getattr(state, field.name) for field in fields(state)
        },
        "source_as_of": {
            field.name: getattr(bundle.source_as_of, field.name)
            for field in fields(bundle.source_as_of)
        },
        "financial_rows": financial_rows,
        "valuation_points": [
            {field.name: getattr(point, field.name) for field in fields(point)}
            for point in bundle.valuation_points
        ],
    }
    return _canonical(payload)


def _object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"invalid {label}")
    return value


def _text(value: object, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str or not value:
        raise ValueError(f"invalid {label}")
    return value


def _decimal(value: object, label: str) -> None:
    if value is None:
        return
    if type(value) is not str:
        raise ValueError(f"invalid {label}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"invalid {label}") from error
    if _decimal_text(parsed) != value:
        raise ValueError(f"invalid {label}")


def parse_company_payload(
    text: str,
    *,
    expected_code: str | None = None,
) -> dict[str, Any]:
    """Parse and fully validate persisted canonical payload JSON."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        value = json.loads(text, parse_constant=reject_constant)
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid discovery company payload JSON") from error
    payload = _object(value, _ROOT_KEYS, "discovery company payload")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported discovery company payload schema")
    code = _text(payload["code"], "payload code")
    if expected_code is not None and code != expected_code:
        raise ValueError("discovery company payload code mismatch")
    captured_at = _text(payload["captured_at"], "captured_at")
    try:
        parsed_at = datetime.fromisoformat(captured_at)
    except ValueError as error:
        raise ValueError("invalid captured_at") from error
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise ValueError("invalid captured_at")

    state_keys = {field.name for field in fields(SecurityState)}
    state = _object(payload["security_state"], state_keys, "security state")
    if state["code"] != code:
        raise ValueError("payload security code mismatch")
    for key in state_keys:
        if key in {"is_st", "is_delisting"}:
            if type(state[key]) is not bool:
                raise ValueError("invalid security state")
        elif key == "market_status_code":
            if state[key] is not None and type(state[key]) is not int:
                raise ValueError("invalid security state")
        else:
            _text(state[key], f"security {key}")

    source = _object(
        payload["source_as_of"],
        {"security", "valuation", "financial"},
        "source as-of",
    )
    _text(source["security"], "security source as-of")
    _text(source["valuation"], "valuation source as-of", nullable=True)
    _text(source["financial"], "financial source as-of", nullable=True)

    rows = payload["financial_rows"]
    if type(rows) is not list:
        raise ValueError("invalid financial rows")
    expected_values = set(_FINANCIAL_FIELDS)
    for row in rows:
        row = _object(
            row,
            {"period", "period_type", "source_as_of", "values", "field_sources"},
            "financial row",
        )
        _text(row["period"], "financial period")
        _text(row["period_type"], "financial period type")
        _text(row["source_as_of"], "financial source as-of", nullable=True)
        values = _object(row["values"], expected_values, "financial values")
        for field, field_value in values.items():
            if field == "share_change_ma":
                if field_value is not None and type(field_value) is not bool:
                    raise ValueError("invalid financial share_change_ma")
            else:
                _decimal(field_value, f"financial {field}")
        expected_sources = {
            field: _source_metadata(FIELD_DICTIONARY[field])
            for field, field_value in values.items()
            if field_value is not None and field in FIELD_DICTIONARY
        }
        if row["field_sources"] != json.loads(_canonical(expected_sources)):
            raise ValueError("invalid financial field sources")

    points = payload["valuation_points"]
    if type(points) is not list:
        raise ValueError("invalid valuation points")
    point_keys = {
        "code",
        "month",
        "trade_date",
        "pe",
        "pb",
        "price",
        "source",
        "ratio_method",
    }
    for point in points:
        point = _object(point, point_keys, "valuation point")
        if point["code"] != code:
            raise ValueError("valuation point code mismatch")
        for key in ("code", "month", "trade_date", "source", "ratio_method"):
            _text(point[key], f"valuation {key}")
        for key in ("pe", "pb", "price"):
            _decimal(point[key], f"valuation {key}")

    if _canonical(payload) != text:
        raise ValueError("discovery company payload is not canonical")
    return payload
