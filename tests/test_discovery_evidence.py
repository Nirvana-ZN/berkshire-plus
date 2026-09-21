from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from discovery.evidence import (
    ALLOWED_RISK_CATEGORIES,
    FALLBACK_RISK_TEXT,
    build_evidence_card,
)


def ruler_payload() -> dict[str, object]:
    return {
        "track": "quality_discount",
        "fields": [
            "valuation_percentile",
            "roe_stability",
            "cash_conversion",
            "margin_change",
            "code",
        ],
        "formulas": {
            "valuation_percentile": "percentile_rank(current.pe,monthly.pe)",
            "roe_stability": "coefficient_of_variation(annual.roe)",
            "cash_conversion": "mean(annual.ocf/annual.net_income)",
            "margin_change": "latest(gross_margin)-earliest(gross_margin)",
        },
        "thresholds": {
            "valuation_percentile_max": "30",
            "roe_stability_max": "0.25",
            "cash_conversion_min": "1",
        },
        "missing_policy": {
            "valuation_percentile": "insufficient",
            "roe_stability": "insufficient",
            "cash_conversion": "insufficient",
            "margin_change": "skip",
            "code": "insufficient",
        },
        "order": [
            ["valuation_percentile", "asc", "nulls_last"],
            ["roe_stability", "asc", "nulls_last"],
            ["cash_conversion", "desc", "nulls_last"],
            ["code", "asc", "nulls_last"],
        ],
        "algorithm_version": "discovery-v1",
    }


def metrics(**changes: object) -> dict[str, object]:
    values = {
        "valuation_percentile": "20",
        "roe_stability": "0.1",
        "cash_conversion": "1.2",
        "margin_change": "1",
        "risk_flags": [],
    }
    values.update(changes)
    return values


def test_evidence_is_deterministic_and_carries_rule_inputs() -> None:
    first = build_evidence_card(
        "600000.SH",
        metrics(),
        ruler_payload(),
        rule_version="quality-v7",
        as_of="2026-09-17",
    )
    second = build_evidence_card(
        "600000.SH",
        metrics(),
        ruler_payload(),
        rule_version="quality-v7",
        as_of="2026-09-17",
    )

    assert first == second
    assert first.verdict == "pass"
    assert 3 <= len(first.items) <= 5
    assert all(item.rule_id and item.input_fields for item in first.items)
    assert first.items[-1].display_text == FALLBACK_RISK_TEXT
    assert first.items[-1].kind == "risk"


def test_risks_use_only_allowed_categories() -> None:
    cases = (
        metrics(valuation_percentile="29"),
        metrics(risk_flags=["margin_deterioration"]),
        metrics(risk_flags=["bank_asset_quality"]),
        metrics(margin_change=None),
    )
    categories = set()
    for values in cases:
        card = build_evidence_card(
            "600000.SH",
            values,
            ruler_payload(),
            rule_version="quality-v7",
            as_of="2026-09-17",
        )
        categories.update(
            item.category
            for item in card.items
            if item.kind == "risk" and item.category is not None
        )

    assert categories
    assert categories <= ALLOWED_RISK_CATEGORIES


def test_fewer_than_three_reliable_facts_is_insufficient_not_padded() -> None:
    card = build_evidence_card(
        "600000.SH",
        metrics(cash_conversion=None, margin_change=None),
        ruler_payload(),
        rule_version="quality-v7",
        as_of="2026-09-17",
    )

    facts = [item for item in card.items if item.kind == "fact"]
    assert (card.verdict, card.reason) == (
        "insufficient",
        "evidence_facts_insufficient",
    )
    assert len(facts) == 2
    assert all(item.input_fields != {"text": "generic"} for item in facts)


def test_builtin_competition_emits_evidence_for_every_entrant() -> None:
    from discovery.snapshot import FrozenRunInputs, canonical_json
    from jobs.discovery import (
        TrackExecutionCandidate,
        TrackExecutionContext,
        builtin_track_executor,
    )
    from tests.test_discovery_tracks_quality import (
        SNAPSHOT_AT,
        frozen_company,
        quality_payload,
    )

    first = frozen_company()
    second = replace(
        first,
        code="600001.SH",
        input_fingerprint="fingerprint-2",
    )
    frozen = FrozenRunInputs(
        run_id=1,
        input_fingerprint="run",
        data_version="data",
        financial_version="financial",
        valuation_version="valuation",
        ruler_version_ids=MappingProxyType(
            {"quality_discount": 7, "deep_value": None, "garp": None}
        ),
        ruler_payloads=MappingProxyType({}),
        algorithm_version="discovery-v1",
        trading_dates=MappingProxyType({"SH": ("2026-09-17",)}),
        snapshot_at=SNAPSHOT_AT,
        companies=(first, second),
    )
    candidates = tuple(
        TrackExecutionCandidate(
            code=code,
            persisted_verdict="pass",
            effective_verdict="pass",
            reason=None,
            metrics_json=canonical_json(
                {
                    "pe_percentile": "20",
                    "roe_cv": "0.1",
                    "cash_conversion": "1.2",
                    "roe_mean": "15",
                    "risk_flags": [],
                }
            ),
            rank=None,
        )
        for code in ("600001.SH", "600000.SH")
    )
    context = TrackExecutionContext(
        stage_id=11,
        run_id=1,
        track="quality_discount",
        stage="competition",
        attempt=3,
        input_fingerprint="run",
        ruler_version_id=7,
        algorithm_version="discovery-v1",
        common_stage_id=None,
        predecessor_stage_id=10,
        predecessor_stage="track_gate",
        frozen_inputs=frozen,
        ruler_payload=MappingProxyType(quality_payload()),
        candidates=candidates,
        ruler_digest="digest-v7",
    )

    outcome = builtin_track_executor(context)

    assert [(row.code, row.rank) for row in outcome.results] == [
        ("600000.SH", 1),
        ("600001.SH", 2),
    ]
    assert {row.code for row in outcome.evidence} == {
        "600000.SH",
        "600001.SH",
    }
    assert {row.rule_version for row in outcome.evidence} == {"digest-v7"}
