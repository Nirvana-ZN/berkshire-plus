from __future__ import annotations

from dataclasses import replace
import json
from types import MappingProxyType

import pytest

from discovery.evidence import (
    ALLOWED_RISK_CATEGORIES,
    EVIDENCE_RULE_VERSION,
    FALLBACK_RISK_TEXT,
    PersistedEvidenceItem,
    build_evidence_card,
    verify_canonical_evidence_card,
)

SOURCE_AS_OF = {
    "security": "2026-09-17",
    "financial": "2026-04-01",
    "valuation": "2026-08-31",
}


def ruler_payload() -> dict[str, object]:
    return {
        "track": "quality_discount",
        "fields": [
            "pe_percentile",
            "roe_cv",
            "cash_conversion",
            "margin_change",
            "code",
        ],
        "formulas": {
            "pe_percentile": "percentile_rank(current.pe,monthly.pe)",
            "roe_cv": "coefficient_of_variation(annual.roe)",
            "cash_conversion": "mean(annual.ocf/annual.net_income)",
            "margin_change": "latest(gross_margin)-earliest(gross_margin)",
        },
        "thresholds": {
            "pe_percentile_max": "30",
            "roe_cv_max": "0.25",
            "cash_conversion_min": "1",
        },
        "min_observations": {
            "valuation_pe": 60,
            "roe": 5,
            "cash_conversion": 5,
        },
        "missing_policy": {
            "pe_percentile": "insufficient",
            "roe_cv": "insufficient",
            "cash_conversion": "insufficient",
            "margin_change": "skip",
            "code": "insufficient",
        },
        "order": [
            ["pe_percentile", "asc", "nulls_last"],
            ["roe_cv", "asc", "nulls_last"],
            ["cash_conversion", "desc", "nulls_last"],
            ["code", "asc", "nulls_last"],
        ],
        "algorithm_version": "discovery-v1",
    }


def metrics(**changes: object) -> dict[str, object]:
    values = {
        "pe_percentile": "20",
        "roe_cv": "0.1",
        "cash_conversion": "1.2",
        "margin_change": "1",
        "valuation_pe_observations": "60",
        "roe_observations": "5",
        "cash_conversion_observations": "5",
        "risk_flags": [],
    }
    values.update(changes)
    return values


def test_evidence_is_deterministic_and_carries_rule_inputs() -> None:
    first = build_evidence_card(
        "600000.SH",
        metrics(),
        ruler_payload(),
        source_as_of=SOURCE_AS_OF,
    )
    second = build_evidence_card(
        "600000.SH",
        metrics(),
        ruler_payload(),
        source_as_of=SOURCE_AS_OF,
    )

    assert first == second
    assert first.verdict == "pass"
    assert 3 <= len(first.items) <= 5
    assert all(item.rule_id and item.input_fields for item in first.items)
    assert {item.rule_version for item in first.items} == {
        EVIDENCE_RULE_VERSION
    }
    facts = {item.rule_id: item for item in first.items if item.kind == "fact"}
    assert facts["quality_discount.fact.pe_percentile"].as_of == "2026-08-31"
    assert facts["quality_discount.fact.roe_cv"].as_of == "2026-04-01"
    assert facts["quality_discount.fact.cash_conversion"].as_of == "2026-04-01"
    assert first.items[-1].display_text == FALLBACK_RISK_TEXT
    assert first.items[-1].kind == "risk"


def test_canonical_verifier_rejects_semantic_evidence_forgery() -> None:
    from tests.test_discovery_tracks_quality import (
        _classify,
        frozen_company,
        quality_payload,
    )

    company = frozen_company(industry="银行")
    result = _classify(company)
    ruler = quality_payload()
    source_as_of = {
        key: company.as_of[key]
        for key in ("security", "financial", "valuation")
    }
    card = build_evidence_card(
        company.code,
        result.metrics,
        ruler,
        source_as_of=source_as_of,
    )
    items = tuple(
        PersistedEvidenceItem(
            code=company.code,
            ruler_version_id=7,
            rule_version=item.rule_version,
            kind=item.kind,
            rule_id=item.rule_id,
            input_fields=item.input_fields,
            display_text=item.display_text,
            as_of=item.as_of,
            data_completeness=item.data_completeness,
        )
        for item in card.items
    )

    def verify(
        candidate_items: tuple[PersistedEvidenceItem, ...],
        candidate_metrics=result.metrics,
    ) -> bool:
        return verify_canonical_evidence_card(
            track="quality_discount",
            ruler=ruler,
            ruler_version_id=7,
            code=company.code,
            metrics=candidate_metrics,
            verdict="pass",
            rank=1,
            reason=None,
            company_code=company.code,
            company_industry=company.industry,
            source_as_of=source_as_of,
            items=candidate_items,
        )

    assert verify(items)
    assert not verify(
        (
            replace(
                items[0],
                input_fields={**items[0].input_fields, "value": "999"},
            ),
            *items[1:],
        )
    )
    assert not verify(
        (
            replace(
                items[0],
                rule_id="quality_discount.fact.allowed_looking_forgery",
            ),
            *items[1:],
        )
    )
    assert not verify(
        (replace(items[0], code="600001.SH"), *items[1:])
    )
    assert not verify(items, {**result.metrics, "code": "600001.SH"})
    assert not verify(
        (replace(items[0], as_of="2020-01-01"), *items[1:])
    )


def test_risks_use_only_allowed_categories() -> None:
    cases = (
        metrics(pe_percentile="29"),
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
            source_as_of=SOURCE_AS_OF,
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
        metrics(cash_conversion_observations="4", margin_change="99"),
        ruler_payload(),
        source_as_of=SOURCE_AS_OF,
    )

    facts = [item for item in card.items if item.kind == "fact"]
    assert (card.verdict, card.reason) == (
        "insufficient",
        "evidence_facts_insufficient",
    )
    assert len(facts) == 2
    assert all(item.input_fields != {"text": "generic"} for item in facts)


def test_bank_card_uses_branch_specific_reliable_facts() -> None:
    from tests.test_discovery_tracks_quality import (
        _classify,
        frozen_company,
        quality_payload,
    )

    company = frozen_company(industry="银行")
    verdict = _classify(company)
    assert verdict.verdict == "pass"

    card = build_evidence_card(
        company.code,
        verdict.metrics,
        quality_payload(),
        source_as_of={
            key: company.as_of[key]
            for key in ("security", "financial", "valuation")
        },
    )

    facts = {item.rule_id: item for item in card.items if item.kind == "fact"}
    assert card.verdict == "pass"
    assert 3 <= len(card.items) <= 5
    assert set(facts) == {
        "quality_discount.fact.pb_percentile",
        "quality_discount.fact.roe_mean",
        "quality_discount.fact.bank_capital",
        "quality_discount.fact.bank_npl",
    }
    assert {
        "bank_capital_adequacy",
        "bank_core_tier1_capital_adequacy",
    } <= set(facts["quality_discount.fact.bank_capital"].input_fields)
    assert not any(
        field in item.rule_id
        for item in facts.values()
        for field in ("pe_percentile", "cash_conversion")
    )


def test_builtin_competition_emits_evidence_for_every_entrant() -> None:
    from discovery.snapshot import FrozenRunInputs, canonical_json
    from jobs.discovery import (
        TrackExecutionCandidate,
        TrackExecutionContext,
        TrackExecutionOutcome,
        TrackExecutorContractError,
        _validate_track_outcome,
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
    third = replace(
        first,
        code="600002.SH",
        input_fingerprint="fingerprint-3",
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
        companies=(first, second, third),
    )
    candidates = tuple(
        TrackExecutionCandidate(
            code=code,
            persisted_verdict="pass",
            effective_verdict="pass",
            reason=None,
            metrics_json=canonical_json(
                {
                        "code": code,
                    "pe_percentile": "20",
                    "roe_cv": "0.1",
                    "cash_conversion": "1.2",
                    "roe_mean": "15",
                    "branch": "nonfinancial",
                    "quality_branch": "nonfinancial",
                    "valuation_pe_observations": "60",
                    "roe_observations": "5",
                    "cash_conversion_observations": (
                        "1" if code == "600002.SH" else "5"
                    ),
                    "risk_flags": [],
                }
            ),
            rank=None,
        )
        for code in ("600002.SH", "600001.SH", "600000.SH")
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
    )

    outcome = builtin_track_executor(context)

    assert [(row.code, row.rank) for row in outcome.results] == [
        ("600000.SH", 1),
        ("600001.SH", 2),
        ("600002.SH", 3),
    ]
    assert {row.code for row in outcome.evidence} == {
        "600000.SH",
        "600001.SH",
        "600002.SH",
    }
    excluded = outcome.results[-1]
    assert (excluded.verdict, excluded.reason) == (
        "insufficient",
        "evidence_facts_insufficient",
    )
    assert {row.rule_version for row in outcome.evidence} == {
        EVIDENCE_RULE_VERSION
    }
    with pytest.raises(
        TrackExecutorContractError, match="unranked competition"
    ):
        _validate_track_outcome(
            context,
            replace(
                outcome,
                results=(
                    replace(outcome.results[0], rank=None),
                    *outcome.results[1:],
                ),
            ),
        )
    allowed = TrackExecutionOutcome(
        status="waiting_confirmation",
        results=(
            replace(
                outcome.results[0],
                verdict="insufficient",
                reason="industry_unknown",
                rank=None,
            ),
            replace(outcome.results[1], rank=1),
            replace(outcome.results[2], rank=2),
        ),
        evidence=outcome.evidence,
    )
    assert _validate_track_outcome(context, allowed) is allowed

    first_evidence = outcome.evidence[0]
    first_code_items = tuple(
        item for item in outcome.evidence if item.code == first_evidence.code
    )
    fallback = next(
        item
        for item in first_code_items
        if item.rule_id == "quality_discount.risk.none"
    )
    fabricated_inputs = json.loads(first_evidence.input_fields_json)
    fabricated_inputs["value"] = "999"
    malformed = (
        replace(
            outcome,
            evidence=tuple(
                item
                for item in outcome.evidence
                if item.code != "600000.SH"
            ),
        ),
        replace(
            outcome,
            evidence=(
                replace(first_evidence, kind="narrative"),
                *outcome.evidence[1:],
            ),
        ),
        replace(
            outcome,
            evidence=(
                replace(first_evidence, data_completeness="unknown"),
                *outcome.evidence[1:],
            ),
        ),
        replace(
            outcome,
            evidence=(
                replace(first_evidence, as_of="2026-09-16"),
                *outcome.evidence[1:],
            ),
        ),
        replace(
            outcome,
            evidence=(
                replace(
                    first_evidence,
                    input_fields_json=canonical_json(
                        {"metric": "1", "source": "financial"}
                    ),
                ),
                *outcome.evidence[1:],
            ),
        ),
        replace(
            outcome,
            evidence=(
                replace(
                    first_evidence,
                    input_fields_json=canonical_json(fabricated_inputs),
                ),
                *outcome.evidence[1:],
            ),
        ),
        replace(
            outcome,
            evidence=(
                replace(first_evidence, code="600001.SH"),
                *outcome.evidence[1:],
            ),
        ),
        replace(
            outcome,
            evidence=(
                replace(first_evidence, rule_version="ruler-digest"),
                *outcome.evidence[1:],
            ),
        ),
        replace(
            outcome,
            evidence=(
                replace(first_evidence, ruler_version_id=8),
                *outcome.evidence[1:],
            ),
        ),
        replace(
            outcome,
            evidence=tuple(
                item
                for item in outcome.evidence
                if item != first_code_items[0]
            ),
        ),
        replace(
            outcome,
            evidence=tuple(
                replace(item, display_text="not the fallback")
                if item == fallback
                else item
                for item in outcome.evidence
            ),
        ),
        replace(
            outcome,
            evidence=(
                *outcome.evidence,
                replace(
                    fallback,
                    rule_id=(
                        "quality_discount.risk.worsening_trend.synthetic"
                    ),
                    input_fields_json='{"risk_flag":"synthetic"}',
                    display_text="synthetic",
                ),
            ),
        ),
    )
    for invalid in malformed:
        with pytest.raises(TrackExecutorContractError):
            _validate_track_outcome(context, invalid)
