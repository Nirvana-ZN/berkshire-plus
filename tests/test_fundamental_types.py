"""Behavioral contracts for immutable selection facts and results."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256

import pytest

from discovery.fundamental_types import (
    CapitalEvent,
    CoverageWindow,
    EvidenceRecord,
    FundamentalSnapshot,
    MetricResult,
    OperatingResult,
    evidence_identity,
)
from tests.selection_helpers import evidence


AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_metric_keeps_unknown_distinct_from_zero():
    zero = MetricResult(Decimal("0"), "verified", None, ())
    unknown = MetricResult(None, "missing", "not_reported", ())
    assert zero != unknown
    with pytest.raises(FrozenInstanceError):
        zero.status = "missing"


@pytest.mark.parametrize("status", ["missing", "untrusted", "not_applicable"])
def test_metric_requires_value_only_for_verified_status(status):
    assert MetricResult(None, status, "unavailable", ()).value is None
    with pytest.raises(ValueError):
        MetricResult(Decimal("0"), status, None, ())
    with pytest.raises(ValueError):
        MetricResult(None, "verified", None, ())


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("sNaN"),
                                  Decimal("Infinity"), Decimal("-Infinity"),
                                  0.1, 1, "1"])
def test_metric_rejects_nonfinite_or_nondecimal_values(value):
    with pytest.raises(ValueError):
        MetricResult(value, "verified", None, ())


@pytest.mark.parametrize("status", ["unknown", "", "VERIFIED", None])
def test_evidence_and_metrics_reject_undefined_statuses(status):
    with pytest.raises(ValueError):
        replace(evidence("2025"), status=status)
    with pytest.raises(ValueError):
        MetricResult(None, status, None, ())


@pytest.mark.parametrize("field", ["published_at", "observed_at", "source_updated_at"])
def test_evidence_rejects_naive_timestamps(field):
    with pytest.raises(ValueError):
        replace(evidence("2025"), **{field: datetime(2026, 10, 1)})


@pytest.mark.parametrize("field", ["date_only", "archive_verified"])
@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_evidence_precision_and_archive_flags_require_booleans(field, value):
    with pytest.raises(ValueError):
        replace(evidence("2025"), **{field: value})


def test_date_only_preserves_explicit_timestamp_and_is_part_of_identity():
    precise = evidence("2025")
    date_only = replace(precise, date_only=True)
    assert date_only.published_at == precise.published_at
    assert evidence_identity(date_only) != evidence_identity(precise)


def test_evidence_copies_mapping_and_rejects_mutation():
    values = {"revenue": "100.00", "deducted_profit": None}
    row = replace(evidence("2025"), values=values)
    before = evidence_identity(row)
    values["revenue"] = "999"
    assert row.values == {"revenue": "100.00", "deducted_profit": None}
    assert evidence_identity(row) == before
    with pytest.raises(TypeError):
        row.values["revenue"] = "999"
    with pytest.raises(FrozenInstanceError):
        row.period = "2024"


@pytest.mark.parametrize("values", [{"amount": Decimal("1")}, {"amount": 1.0},
                                   {"amount": []}, {1: "1"}])
def test_evidence_requires_string_keys_and_string_or_null_values(values):
    with pytest.raises(ValueError):
        replace(evidence("2025"), values=values)


def test_identity_matches_compact_sorted_utf8_json_and_excludes_only_id():
    row = EvidenceRecord("placeholder", "000001.SZ", "financial", "2025",
                         AT, AT, None, "https://example.invalid/fixture", "a" * 64,
                         {"z": None, "a": "中文"}, "verified", "fixture-cny")
    # Written independently of the serializer to fix its public wire contract.
    canonical = (
        '{"archive_verified":false,"basis_id":"fixture-cny","code":"000001.SZ",'
        '"date_only":false,"document_sha256":"' + "a" * 64 + '",'
        '"kind":"financial","observed_at":"2026-10-01T00:00:00+00:00",'
        '"period":"2025","published_at":"2026-10-01T00:00:00+00:00",'
        '"source_updated_at":null,"source_url":"https://example.invalid/fixture",'
        '"status":"verified","supersedes_id":null,"values":{"a":"中文","z":null}}'
    )
    assert evidence_identity(row) == sha256(canonical.encode("utf-8")).hexdigest()
    assert evidence_identity(replace(row, evidence_id="different")) == evidence_identity(row)


def test_identity_normalizes_timezone_and_mapping_order():
    row = evidence("2025", revenue="100", profit="10")
    offset = timezone(timedelta(hours=8))
    equivalent = replace(row, published_at=AT.astimezone(offset),
                         observed_at=AT.astimezone(offset),
                         values={"profit": "10", "revenue": "100"})
    assert evidence_identity(equivalent) == evidence_identity(row)


@pytest.mark.parametrize("changes", [
    {"code": "000002.SZ"}, {"kind": "extension"}, {"period": "2024"},
    {"published_at": AT + timedelta(seconds=1)},
    {"observed_at": AT + timedelta(seconds=1)}, {"source_updated_at": AT},
    {"source_url": "https://example.invalid/revision"},
    {"document_sha256": "b" * 64}, {"values": {"revenue": "2"}},
    {"status": "untrusted"}, {"basis_id": "restated-cny"},
    {"supersedes_id": "previous"}, {"date_only": True}, {"archive_verified": True},
])
def test_every_fact_field_changes_identity_without_enforcing_id_in_constructor(changes):
    original = evidence("2025", revenue="1")
    revised = replace(original, **changes)
    assert revised.evidence_id == original.evidence_id
    assert evidence_identity(revised) != evidence_identity(original)


def test_mutable_evidence_id_inputs_are_detached():
    ids = ["source"]
    metric = MetricResult(Decimal("0"), "verified", None, ids)
    coverage = CoverageWindow("000001.SZ", "capital", date(2025, 1, 1),
                              date(2025, 12, 31), True, ids)
    ids.append("later")
    assert metric.evidence_ids == coverage.evidence_ids == ("source",)


def test_coverage_rejects_reversed_or_datetime_boundaries():
    with pytest.raises(ValueError):
        CoverageWindow("000001.SZ", "capital", date(2025, 12, 31),
                       date(2025, 1, 1), True, ())
    with pytest.raises(ValueError):
        CoverageWindow("000001.SZ", "capital", AT, AT, True, ())


def capital_event(**changes):
    row = CapitalEvent("event", "000001.SZ", "split", AT, 0,
                       Decimal("100"), Decimal("200"), Decimal("2"), "source")
    return replace(row, **changes)


def test_capital_event_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        capital_event(effective_at=AT.replace(tzinfo=None))


@pytest.mark.parametrize("field", ["shares_before", "shares_after", "factor"])
@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), 1.0])
def test_capital_event_requires_finite_decimal_amounts(field, value):
    with pytest.raises(ValueError):
        capital_event(**{field: value})


def snapshot(**changes):
    row = FundamentalSnapshot("000001.SZ", AT, (), (), (), (), (), None, "fixture")
    return replace(row, **changes)


def test_snapshot_detaches_mutable_collections_without_converting_units():
    annuals = [evidence("2025", revenue="123.45")]
    actions = [capital_event()]
    coverage = [CoverageWindow("000001.SZ", "capital", date(2025, 1, 1),
                               date(2025, 12, 31), True, ())]
    frozen = snapshot(annuals=annuals, interims=annuals, actions=actions,
                      extensions=annuals, coverage=coverage,
                      current_mcap=Decimal("123.45"))
    annuals.clear()
    actions.clear()
    coverage.clear()
    assert frozen.annuals == frozen.interims == frozen.extensions == (evidence("2025", revenue="123.45"),)
    assert len(frozen.actions) == len(frozen.coverage) == 1
    assert frozen.current_mcap == Decimal("123.45")
    assert frozen.annuals[0].values["revenue"] == "123.45"
    with pytest.raises(FrozenInstanceError):
        frozen.fingerprint = "modified"


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), 1.0])
def test_snapshot_rejects_nonfinite_or_nondecimal_market_cap(value):
    with pytest.raises(ValueError):
        snapshot(current_mcap=value)


def test_snapshot_rejects_naive_cutoff():
    with pytest.raises(ValueError):
        snapshot(cutoff_at=AT.replace(tzinfo=None))


@pytest.mark.parametrize("state", ["improving", "stable", "deteriorating", "unknown"])
def test_operating_result_detaches_reasons_and_metric_mapping(state):
    reasons = ["reported"]
    metrics = {"growth": MetricResult(Decimal("0"), "verified", None, ())}
    result = OperatingResult(state, "2026Q3", reasons, metrics)
    reasons.append("later")
    metrics.clear()
    assert result.reasons == ("reported",)
    assert result.metrics["growth"].value == Decimal("0")
    with pytest.raises(TypeError):
        result.metrics["growth"] = MetricResult(None, "missing", None, ())
    with pytest.raises(FrozenInstanceError):
        result.state = "unknown"


@pytest.mark.parametrize("state", ["verified", "missing", "", None])
def test_operating_result_rejects_undefined_states(state):
    with pytest.raises(ValueError):
        OperatingResult(state, None, (), {})


@pytest.mark.parametrize("kind", ["split", "bonus", "consolidation", "issuance",
                                 "rights_issue", "stock_compensation", "buyback_cancel"])
def test_capital_event_accepts_defined_event_kinds(kind):
    assert capital_event(kind=kind).kind == kind


@pytest.mark.parametrize("kind", ["dividend", "buyback", "", None])
def test_capital_event_rejects_undefined_event_kinds(kind):
    with pytest.raises(ValueError):
        capital_event(kind=kind)


@pytest.mark.parametrize("kind", ["financial", "capital_event", "buyback", "dividend", "future_source"])
def test_evidence_and_coverage_kinds_are_open_semantic_identifiers(kind):
    assert replace(evidence("2025"), kind=kind).kind == kind
    assert CoverageWindow("000001.SZ", kind, date(2025, 1, 1),
                          date(2025, 12, 31), False, ()).kind == kind


@pytest.mark.parametrize("kind", ["", "  ", None, []])
def test_evidence_and_coverage_reject_empty_or_nontext_kinds(kind):
    with pytest.raises(ValueError):
        replace(evidence("2025"), kind=kind)
    with pytest.raises(ValueError):
        CoverageWindow("000001.SZ", kind, date(2025, 1, 1),
                       date(2025, 12, 31), False, ())


@pytest.mark.parametrize("field", ["evidence_id", "code", "period", "source_url",
                                  "document_sha256", "basis_id"])
def test_evidence_rejects_missing_required_identifiers(field):
    with pytest.raises(ValueError):
        replace(evidence("2025"), **{field: ""})


@pytest.mark.parametrize("field,value", [
    ("annuals", ["not_evidence"]), ("interims", [{}]),
    ("extensions", [[]]), ("actions", [evidence("2025")]),
    ("coverage", ["not_coverage"]),
])
def test_snapshot_rejects_invalid_nested_facts(field, value):
    with pytest.raises(ValueError):
        snapshot(**{field: value})


def test_result_and_coverage_reject_invalid_nested_values():
    with pytest.raises(ValueError):
        OperatingResult("unknown", None, (), {"growth": {"value": "1"}})
    with pytest.raises(ValueError):
        OperatingResult("unknown", None, [[]], {})
    with pytest.raises(ValueError):
        MetricResult(None, "missing", None, [[]])
    with pytest.raises(ValueError):
        CoverageWindow("000001.SZ", "capital", date(2025, 1, 1),
                       date(2025, 12, 31), "false", ())


@pytest.mark.parametrize("sequence", [True, 0.1, "0"])
def test_capital_event_requires_integer_sequence(sequence):
    with pytest.raises(ValueError):
        capital_event(sequence=sequence)
