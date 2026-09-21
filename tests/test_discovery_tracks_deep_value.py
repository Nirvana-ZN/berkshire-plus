from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from decimal import DefaultContext, Decimal, getcontext, setcontext
from pathlib import Path
from types import MappingProxyType

import pytest

from discovery.builds import BUILD_VERSION
from discovery.snapshot import FrozenRunInputs, canonical_json
from tests.test_discovery_tracks_quality import (
    _annual,
    _metadata,
    frozen_company,
)


FIELDS = [
    "listing_years",
    "normalized_earnings",
    "normalized_pe",
    "cumulative_fcf",
    "normalized_fcf",
    "net_debt_to_normalized_fcf",
    "interest_coverage",
    "dilution_rate",
    "revenue_decline",
    "one_time_profit_share",
    "impairment_share",
    "non_core_income_proxy_share",
    "pb_percentile",
    "pb_median_ratio",
    "roe_mean",
    "npl_change",
    "bank_capital_adequacy",
    "bank_core_tier1_capital_adequacy",
    "code",
]

FORMULAS = {
    "normalized_earnings": "median(annual.deducted_net_income)",
    "normalized_pe": "current.price*latest(annual.shares)/normalized_earnings",
    "cumulative_fcf": "sum(annual.ocf-annual.capex)",
    "normalized_fcf": "median(annual.ocf-annual.capex)",
    "net_debt_to_normalized_fcf": (
        "(latest(annual.interest_debt)-latest(annual.cash))/normalized_fcf"
    ),
    "interest_coverage": "latest(annual.interest_coverage)",
    "dilution_rate": "latest(annual.shares)/earliest(annual.shares)-1",
    "revenue_decline": (
        "max(0,(earliest(annual.revenue)-latest(annual.revenue))/"
        "earliest(annual.revenue))"
    ),
    "one_time_profit_share": (
        "max(0,latest(annual.net_income)-latest(annual.deducted_net_income))/"
        "copy_abs(normalized_earnings)"
    ),
    "impairment_share": (
        "copy_abs(latest(annual.impairment_loss))/copy_abs(normalized_earnings)"
    ),
    "non_core_income_proxy_share": (
        "(copy_abs(latest(annual.other_income))+"
        "copy_abs(latest(annual.asset_disposal_gain)))/"
        "copy_abs(normalized_earnings)"
    ),
    "pb_percentile": "percentile_rank(current.pb,monthly.pb)",
    "pb_median_ratio": "current.pb/median(monthly.pb)",
    "roe_mean": "mean(annual.roe)",
    "npl_change": "latest(annual.npl)-earliest(annual.npl)",
    "bank_capital_adequacy": "latest(annual.capital_adequacy)",
    "bank_core_tier1_capital_adequacy": (
        "latest(annual.core_tier1_capital_adequacy)"
    ),
}


def deep_value_payload(**threshold_overrides: str) -> dict[str, object]:
    thresholds = {
        "listing_years_min": "5",
        "normalized_earnings_min": "0",
        "normalized_pe_max": "12",
        "cumulative_fcf_min": "0",
        "normalized_fcf_min": "0",
        "net_debt_to_normalized_fcf_max": "3",
        "interest_coverage_min": "2",
        "dilution_rate_max": "0.1",
        "revenue_decline_max": "0.2",
        "one_time_profit_share_max": "0.3",
        "impairment_share_max": "0.3",
        "non_core_income_proxy_share_max": "0.2",
        "pb_percentile_max": "30",
        "pb_median_ratio_max": "0.8",
        "bank_roe_min": "8",
        "bank_npl_increase_max": "0.2",
        "bank_capital_adequacy_min": "12",
        "bank_core_tier1_min": "9",
        "readiness_min": "0.8",
    }
    thresholds.update(threshold_overrides)
    return {
        "scope": "weekly-discovery",
        "track": "deep_value",
        "fields": FIELDS,
        "formulas": FORMULAS,
        "thresholds": thresholds,
        "min_observations": {
            "normalized_earnings": 5,
            "fcf": 5,
            "net_debt": 1,
            "interest_coverage": 1,
            "dilution": 2,
            "revenue": 2,
            "one_time_profit": 1,
            "impairment": 1,
            "non_core_income_proxy": 1,
            "valuation_pb": 60,
            "bank_roe": 5,
            "bank_npl": 2,
            "bank_capital": 1,
        },
        "missing_policy": {field: "insufficient" for field in FIELDS},
        "sector_branches": {
            "common": {
                "valuation_ratio_method": "PE_TTM/PB_MRQ",
                "field_domain_policy": "deep-value-domain-v1",
                "lookback_windows": {
                    "normalized_earnings": 5,
                    "fcf": 5,
                    "net_debt": 1,
                    "interest_coverage": 1,
                    "dilution": 5,
                    "revenue": 5,
                    "one_time_profit": 1,
                    "impairment": 1,
                    "non_core_income_proxy": 1,
                    "bank_roe": 5,
                    "bank_npl": 5,
                    "bank_capital": 5,
                },
                "latest_annual_staleness_years": 1,
            },
            "branch_policies": {
                "bank": "financial",
                "insurance": "unsupported",
                "other_financial": "unsupported",
                "nonfinancial": "nonfinancial",
                "unknown": "insufficient",
            },
            "nonfinancial": {
                "gate_order": [
                    "listing_age",
                    "normalized_earnings",
                    "normalized_valuation",
                    "positive_cumulative_fcf",
                    "net_debt",
                    "interest_coverage",
                    "dilution",
                    "revenue_decline",
                    "one_time_profit",
                    "impairment",
                    "non_core_income_proxy",
                ],
                "evidence_fields": [
                    "normalized_pe",
                    "cumulative_fcf",
                    "net_debt_to_normalized_fcf",
                    "interest_coverage",
                ],
            },
            "financial": {
                "gate_order": [
                    "listing_age",
                    "bank_valuation",
                    "bank_roe",
                    "bank_npl_trend",
                    "bank_capital",
                ],
                "evidence_fields": [
                    "pb_percentile",
                    "roe_mean",
                    "npl_change",
                    "bank_capital_adequacy",
                ],
            },
        },
        "order": [
            ["normalized_pe", "asc", "nulls_last"],
            ["pb_percentile", "asc", "nulls_last"],
            ["net_debt_to_normalized_fcf", "asc", "nulls_last"],
            ["code", "asc", "nulls_last"],
        ],
        "industry_version": "test-industry-v1",
        "algorithm_version": BUILD_VERSION,
    }


def _deep_annual(
    year: int,
    **changes: str | None,
) -> dict[str, object]:
    values = {
        "cash": "20",
        "capex": "20",
        "revenue": "200",
        "impairment_loss": "5",
        "other_income": "5",
        "asset_disposal_gain": "5",
    }
    values.update(changes)
    row = _annual(year)
    row_values = dict(row["values"])
    row_values.update(values)
    row["values"] = row_values
    row["provenance"] = {
        field: ("discovery",)
        for field, value in row_values.items()
        if value is not None
    }
    row["field_sources"] = {
        field: _metadata(field)
        for field, value in row_values.items()
        if value is not None
    }
    return row


def _company(*, industry: str = "制造业", annual=None):
    return frozen_company(
        industry=industry,
        annual=annual
        or tuple(_deep_annual(year) for year in range(2021, 2026)),
    )


def _classify(company=None, payload=None):
    from discovery.rulers import deep_value_ruler
    from discovery.tracks.deep_value import classify

    return classify(
        company or _company(),
        deep_value_ruler(payload or deep_value_payload()),
    )


def test_deep_value_ruler_is_complete_and_semantically_strict() -> None:
    from discovery.rulers import deep_value_ruler

    assert deep_value_ruler(deep_value_payload()).track == "deep_value"
    for mutation in (
        lambda value: value["thresholds"].pop("cumulative_fcf_min"),
        lambda value: value["min_observations"].pop("fcf"),
        lambda value: value["missing_policy"].pop("normalized_earnings"),
        lambda value: value["formulas"].__setitem__(
            "normalized_earnings", "latest(net_income)"
        ),
        lambda value: value["sector_branches"]["financial"][
            "gate_order"
        ].append("industrial_fcf"),
        lambda value: value["sector_branches"]["common"][
            "lookback_windows"
        ].pop("fcf"),
        lambda value: value["order"].pop(1),
    ):
        payload = json.loads(json.dumps(deep_value_payload()))
        mutation(payload)
        with pytest.raises((TypeError, ValueError)):
            deep_value_ruler(payload)


def test_nonfinancial_normalizes_deducted_profit_and_requires_positive_fcf() -> None:
    annual = tuple(
        _deep_annual(
            year,
            net_income="1000" if year == 2025 else "100",
            deducted_net_income="100",
            ocf="10",
            capex="20",
        )
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "drop",
        "cumulative_fcf_not_positive",
    )
    assert verdict.metrics["normalized_earnings"] == "100"
    assert verdict.metrics["cumulative_fcf"] == "-50"
    assert verdict.metrics["normalized_fcf"] == "-10"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"cash": "0", "interest_debt": "400"}, "net_debt_to_normalized_fcf_above_max"),
        ({"interest_coverage": "1"}, "interest_coverage_below_min"),
        ({"shares": "120"}, "dilution_rate_above_max"),
        ({"revenue": "100"}, "revenue_decline_above_max"),
        ({"net_income": "200", "deducted_net_income": "100"}, "one_time_profit_share_above_max"),
        ({"impairment_loss": "40"}, "impairment_share_above_max"),
        (
            {"other_income": "15", "asset_disposal_gain": "15"},
            "non_core_income_proxy_share_above_max",
        ),
    ],
)
def test_nonfinancial_deep_value_gates(
    changes: dict[str, str],
    reason: str,
) -> None:
    annual = tuple(
        _deep_annual(year, **(changes if year == 2025 else {}))
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == ("drop", reason)


def test_missing_required_nonfinancial_field_is_insufficient() -> None:
    annual = tuple(
        _deep_annual(year, other_income=None)
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "non_core_income_proxy_insufficient",
    )


@pytest.mark.parametrize(
    ("missing", "reason", "metric"),
    [
        ("shares", "normalized_pe_insufficient", "normalized_pe"),
        ("revenue", "revenue_insufficient", "revenue_decline"),
        (
            "interest_debt",
            "net_debt_insufficient",
            "net_debt_to_normalized_fcf",
        ),
        (
            "interest_coverage",
            "interest_coverage_insufficient",
            "interest_coverage",
        ),
        (
            "net_income",
            "one_time_profit_insufficient",
            "one_time_profit_share",
        ),
        ("impairment_loss", "impairment_insufficient", "impairment_share"),
        (
            "asset_disposal_gain",
            "non_core_income_proxy_insufficient",
            "non_core_income_proxy_share",
        ),
    ],
)
def test_nonfinancial_current_gates_require_latest_fiscal_endpoint(
    missing: str,
    reason: str,
    metric: str,
) -> None:
    annual = tuple(
        _deep_annual(year, **({missing: None} if year == 2025 else {}))
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        reason,
    )
    assert verdict.metrics[metric] is None


def test_financial_branch_uses_pb_roe_npl_and_verified_capital_only() -> None:
    annual = tuple(
        _deep_annual(
            year,
            ocf="-999",
            capex="999",
            cash="0",
            interest_debt="999999",
            interest_coverage="0.001",
            npl=(
                format(
                    Decimal("1.4") - Decimal(year - 2021) / Decimal(10),
                    "f",
                ).rstrip("0").rstrip(".")
            ),
        )
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(industry="银行", annual=annual))
    assert verdict.verdict == "pass"
    assert verdict.metrics["branch"] == "financial"
    assert verdict.metrics["cumulative_fcf"] is None
    assert verdict.metrics["net_debt_to_normalized_fcf"] is None
    assert verdict.metrics["pb_percentile"] is not None
    assert verdict.metrics["roe_mean"] == "15"
    assert verdict.metrics["npl_change"] == "-0.4"
    assert verdict.metrics["bank_capital_adequacy"] == "14"


def test_branch_selection_precedes_branch_specific_domain_validation() -> None:
    bank = tuple(
        _deep_annual(
            year,
            capex="-1",
            shares="-1",
            cash="-1",
            interest_debt="-1",
            revenue="-1",
        )
        for year in range(2021, 2026)
    )
    assert _classify(_company(industry="银行", annual=bank)).verdict == "pass"

    industrial = tuple(
        _deep_annual(
            year,
            npl="-1",
        )
        for year in range(2021, 2026)
    )
    assert _classify(_company(annual=industrial)).verdict == "pass"

    unsupported = _classify(_company(industry="保险", annual=bank))
    assert unsupported.primary_reason == "industry_branch_insurance_unsupported"


def test_financial_missing_capital_is_insufficient() -> None:
    annual = tuple(
        _deep_annual(year, capital_adequacy=None)
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(industry="银行", annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        "bank_capital_adequacy_insufficient",
    )


@pytest.mark.parametrize(
    ("missing", "reason", "metric"),
    [
        ("npl", "bank_npl_insufficient", "npl_change"),
        (
            "capital_adequacy",
            "bank_capital_adequacy_insufficient",
            "bank_capital_adequacy",
        ),
        (
            "core_tier1_capital_adequacy",
            "bank_core_tier1_capital_adequacy_insufficient",
            "bank_core_tier1_capital_adequacy",
        ),
    ],
)
def test_bank_latest_required_year_never_falls_back_to_stale_fields(
    missing: str,
    reason: str,
    metric: str,
) -> None:
    annual = tuple(
        _deep_annual(year, **({missing: None} if year == 2025 else {}))
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(industry="银行", annual=annual))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        reason,
    )
    assert verdict.metrics[metric] is None


def test_bank_latest_accepted_stale_year_is_used_when_ruler_allows_it() -> None:
    annual = tuple(_deep_annual(year) for year in range(2020, 2025))
    verdict = _classify(_company(industry="银行", annual=annual))
    assert verdict.verdict == "pass"
    assert verdict.metrics["latest_fiscal_year"] == "2024"
    assert verdict.metrics["latest_fiscal_year_staleness"] == "1"
    assert verdict.metrics["bank_capital_adequacy"] == "14"


@pytest.mark.parametrize(
    ("industry", "reason"),
    [
        ("保险", "industry_branch_insurance_unsupported"),
        ("证券", "industry_branch_other_financial_unsupported"),
        ("-", "industry_branch_unknown"),
    ],
)
def test_unsupported_and_unknown_branches_are_insufficient(
    industry: str,
    reason: str,
) -> None:
    verdict = _classify(_company(industry=industry))
    assert (verdict.verdict, verdict.primary_reason) == (
        "insufficient",
        reason,
    )


def test_exact_calendar_windows_staleness_and_domains_are_enforced() -> None:
    old = tuple(
        _deep_annual(year, deducted_net_income="9999" if year == 2020 else "100")
        for year in range(2020, 2026)
    )
    recent = _classify(_company(annual=old))
    assert recent.metrics["normalized_earnings"] == "100"
    assert recent.metrics["normalized_earnings_available_years"] == (
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    )

    stale = tuple(_deep_annual(year) for year in range(2019, 2024))
    assert _classify(_company(annual=stale)).primary_reason == (
        "financial_history_stale"
    )

    invalid = list(_company().fields["financial_history"])
    invalid[0] = _deep_annual(2021, capex="-1")
    assert _classify(_company(annual=tuple(invalid))).primary_reason == (
        "capex_negative"
    )


def test_ruler_order_controls_primary_reason_and_keeps_structured_metrics() -> None:
    annual = tuple(
        _deep_annual(year, ocf="10", capex="20", interest_debt="400", cash="0")
        for year in range(2021, 2026)
    )
    verdict = _classify(_company(annual=annual))
    assert verdict.primary_reason == "cumulative_fcf_not_positive"
    assert verdict.evidence_rule_ids[:2] == (
        "deep_value.cumulative_fcf_not_positive",
        "deep_value.normalized_fcf_not_positive",
    )
    assert verdict.metrics["cumulative_fcf"] == "-50"
    assert verdict.metrics["net_debt_to_normalized_fcf"] is None


def test_deep_value_decimal_arithmetic_is_isolated_and_bounded() -> None:
    huge = "1" + "0" * 100
    annual = tuple(
        _deep_annual(
            year,
            ocf=huge,
            capex="0",
            deducted_net_income="1",
            shares=huge,
        )
        for year in range(2021, 2026)
    )
    saved = getcontext().copy()
    saved_default = DefaultContext.copy()
    hostile = saved.copy()
    hostile.prec = 2
    hostile.Emax = 9
    hostile.Emin = -9
    hostile.clear_flags()
    expected_flags = hostile.flags.copy()
    try:
        setcontext(hostile)
        DefaultContext.prec = 3
        verdict = _classify(_company(annual=annual))
        assert verdict.metrics["normalized_pe"] == "1" + "0" * 101
        assert verdict.metrics["cumulative_fcf"] == "5" + "0" * 100
        assert getcontext().flags == expected_flags
    finally:
        setcontext(saved)
        DefaultContext.prec = saved_default.prec
        DefaultContext.rounding = saved_default.rounding
        DefaultContext.Emin = saved_default.Emin
        DefaultContext.Emax = saved_default.Emax
        DefaultContext.capitals = saved_default.capitals
        DefaultContext.clamp = saved_default.clamp
        DefaultContext.traps = saved_default.traps.copy()
        DefaultContext.flags = saved_default.flags.copy()


def test_same_company_can_differ_between_quality_and_deep_value() -> None:
    from tests.test_discovery_tracks_quality import _classify as quality_classify

    company = _company()
    assert quality_classify(company).verdict == "pass"
    assert _classify(
        company,
        deep_value_payload(normalized_pe_max="8"),
    ).verdict == "drop"


def test_builtin_dispatches_deep_value_gate_and_competition() -> None:
    from jobs.discovery import (
        TrackExecutionCandidate,
        TrackExecutionContext,
        TrackExecutorContractError,
        _validate_track_outcome,
        builtin_track_executor,
    )

    company = _company()
    frozen = FrozenRunInputs(
        run_id=1,
        input_fingerprint="run",
        data_version="data",
        financial_version="financial",
        valuation_version="valuation",
        ruler_version_ids=MappingProxyType(
            {"quality_discount": None, "deep_value": 2, "garp": None}
        ),
        ruler_payloads=MappingProxyType({}),
        algorithm_version=BUILD_VERSION,
        trading_dates=MappingProxyType({"SH": ("2026-09-17",)}),
        snapshot_at=company.snapshot_at,
        companies=(company,),
    )
    candidate = TrackExecutionCandidate(
        code=company.code,
        persisted_verdict="pass",
        effective_verdict="pass",
        reason=None,
        metrics_json="{}",
        rank=None,
    )
    gate = TrackExecutionContext(
        stage_id=1,
        run_id=1,
        track="deep_value",
        stage="track_gate",
        attempt=1,
        input_fingerprint="run",
        ruler_version_id=2,
        algorithm_version=BUILD_VERSION,
        common_stage_id=1,
        predecessor_stage_id=None,
        predecessor_stage=None,
        frozen_inputs=frozen,
        ruler_payload=MappingProxyType(deep_value_payload()),
        candidates=(candidate,),
    )
    inactive = builtin_track_executor(
        replace(gate, ruler_payload=MappingProxyType({}))
    )
    assert (
        inactive.status,
        inactive.results,
        inactive.evidence,
    ) == ("ruler_not_ready", (), ())
    outcome = builtin_track_executor(gate)
    assert outcome.status == "waiting_confirmation"
    assert outcome.results[0].verdict == "pass"

    competition = replace(
        gate,
        stage="competition",
        common_stage_id=None,
        predecessor_stage_id=1,
        predecessor_stage="track_gate",
        candidates=(
            replace(
                candidate,
                metrics_json=outcome.results[0].metrics_json,
            ),
        ),
    )
    ranked = builtin_track_executor(competition)
    assert ranked.status == "waiting_confirmation"
    assert ranked.results[0].rank == 1
    assert len(ranked.evidence) >= 3
    assert all(row.rule_id.startswith("deep_value.") for row in ranked.evidence)
    normalized_pe = next(
        row
        for row in ranked.evidence
        if row.rule_id == "deep_value.fact.normalized_pe"
    )
    inputs = json.loads(normalized_pe.input_fields_json)
    assert inputs["sources"] == ["financial", "valuation"]
    assert inputs["source_as_of"] == {
        "financial": company.as_of["financial"],
        "valuation": company.as_of["valuation"],
    }
    assert "source" not in inputs
    assert normalized_pe.as_of == company.as_of["financial"]
    assert _validate_track_outcome(competition, ranked) is ranked
    with pytest.raises(
        TrackExecutorContractError,
        match="canonical|source-bounded",
    ):
        _validate_track_outcome(
            competition,
            replace(
                ranked,
                evidence=tuple(
                    replace(
                        row,
                        input_fields_json=canonical_json(
                            inputs
                            | {
                                "sources": ["valuation"],
                            }
                        ),
                    )
                    if row == normalized_pe
                    else row
                    for row in ranked.evidence
                ),
            ),
        )


@pytest.mark.parametrize(
    ("latest", "expected_rule", "expected_category", "threshold"),
    [
        (
            {"shares": "109"},
            "deep_value.risk.metric_near_threshold.dilution_rate",
            "metric_near_threshold",
            ("dilution_rate_max", "0.1"),
        ),
        (
            {"revenue": "162"},
            "deep_value.risk.metric_near_threshold.revenue_decline",
            "metric_near_threshold",
            ("revenue_decline_max", "0.2"),
        ),
    ],
)
def test_passing_nonfinancial_competition_emits_threshold_risk(
    latest: dict[str, str],
    expected_rule: str,
    expected_category: str,
    threshold: tuple[str, str],
) -> None:
    from discovery.evidence import build_evidence_card

    annual = tuple(
        _deep_annual(year, **(latest if year == 2025 else {}))
        for year in range(2021, 2026)
    )
    company = _company(annual=annual)
    payload = deep_value_payload(
        normalized_pe_max="20" if "shares" in latest else "12"
    )
    verdict = _classify(company, payload)
    assert verdict.verdict == "pass"
    card = build_evidence_card(
        company.code,
        verdict.metrics,
        payload,
        source_as_of={
            key: company.as_of[key]
            for key in ("security", "financial", "valuation")
        },
    )
    risk = next(item for item in card.items if item.kind == "risk")
    assert (risk.rule_id, risk.category) == (
        expected_rule,
        expected_category,
    )
    assert risk.input_fields[threshold[0]] == threshold[1]


def test_passing_bank_npl_worsening_emits_frozen_threshold_trend_risk() -> None:
    from discovery.evidence import build_evidence_card

    annual = tuple(
        _deep_annual(
            year,
            npl="1.1" if year == 2025 else "1",
        )
        for year in range(2021, 2026)
    )
    company = _company(industry="银行", annual=annual)
    verdict = _classify(company)
    assert verdict.verdict == "pass"
    card = build_evidence_card(
        company.code,
        verdict.metrics,
        deep_value_payload(),
        source_as_of={
            key: company.as_of[key]
            for key in ("security", "financial", "valuation")
        },
    )
    risk = next(item for item in card.items if item.kind == "risk")
    assert (
        risk.rule_id,
        risk.category,
        risk.input_fields["npl_change"],
        risk.input_fields["bank_npl_increase_max"],
    ) == (
        "deep_value.risk.worsening_trend.npl_change_worsening",
        "worsening_trend",
        "0.1",
        "0.2",
    )


def test_bank_roe_fact_and_near_threshold_use_bank_observations() -> None:
    from discovery.evidence import build_evidence_card

    annual = tuple(
        _deep_annual(year, roe="8.5")
        for year in range(2021, 2026)
    )
    company = _company(industry="银行", annual=annual)
    verdict = _classify(company)
    assert verdict.verdict == "pass"
    card = build_evidence_card(
        company.code,
        verdict.metrics,
        deep_value_payload(),
        source_as_of={
            key: company.as_of[key]
            for key in ("security", "financial", "valuation")
        },
    )
    roe_fact = next(
        item
        for item in card.items
        if item.rule_id == "deep_value.fact.roe_mean"
    )
    risk = next(item for item in card.items if item.kind == "risk")
    assert roe_fact.input_fields["bank_roe_observations"] == "5"
    assert roe_fact.input_fields["min_observations.bank_roe"] == 5
    assert (
        risk.rule_id,
        risk.category,
        risk.input_fields["bank_roe_min"],
    ) == (
        "deep_value.risk.metric_near_threshold.roe_mean",
        "metric_near_threshold",
        "8",
    )


def test_evidence_threshold_arithmetic_is_bounded_and_context_isolated() -> None:
    from discovery.evidence import FALLBACK_RISK_TEXT, build_evidence_card

    payload = deep_value_payload(normalized_pe_max="1" + "0" * 3000)
    company = _company()
    verdict = _classify(company, payload)
    assert verdict.verdict == "pass"
    saved = getcontext().copy()
    hostile = saved.copy()
    hostile.prec = 2
    hostile.Emax = 9
    hostile.Emin = -9
    hostile.clear_flags()
    expected_flags = hostile.flags.copy()
    try:
        setcontext(hostile)
        card = build_evidence_card(
            company.code,
            verdict.metrics,
            payload,
            source_as_of={
                key: company.as_of[key]
                for key in ("security", "financial", "valuation")
            },
        )
        assert getcontext().flags == expected_flags
        assert card.items[-1].display_text == FALLBACK_RISK_TEXT
    finally:
        setcontext(saved)


def test_calibrator_deep_value_is_deterministic_blocked_and_read_only(
    tmp_path: Path,
) -> None:
    from scripts.calibrate_discovery import calibrate

    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    first = calibrate(db, track="deep_value")
    second = calibrate(db, track="deep_value")
    assert first == second
    assert first["track"] == "deep_value"
    assert first["status"] == "blocked"
    assert first["approval_ready"] is False
    assert first["proposal"] is None
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_calibrator_deep_value_emits_approval_ready_proxy_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from discovery.snapshot import (
        CompanySnapshotInput,
        ProductionSnapshot,
        SnapshotVersions,
    )
    from scripts import calibrate_discovery
    from tests.test_discovery_tracks_quality import _calibration_preflight_db

    db = tmp_path / "production-contract.db"
    _calibration_preflight_db(db)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE discovery_data_fills SET total = 2 WHERE id = 7"
        )
        connection.execute(
            "INSERT INTO discovery_data_checkpoints VALUES "
            "(7, '600001.SH', 'done', 60, '{}')"
        )
        connection.execute(
            "INSERT INTO discovery_universe_securities VALUES "
            "(7, '600001.SH')"
        )

    def recode(company, code: str) -> CompanySnapshotInput:
        fields = dict(company.fields)
        fields["valuation_history"] = tuple(
            dict(row, code=code) for row in fields["valuation_history"]
        )
        return CompanySnapshotInput(
            code=code,
            name=company.name,
            industry=company.industry,
            security_status=company.security_status,
            fields=MappingProxyType(fields),
            sources=company.sources,
            as_of=company.as_of,
        )

    snapshot = ProductionSnapshot(
        fill_id=7,
        companies=(
            recode(_company(), "600000.SH"),
            recode(_company(industry="银行"), "600001.SH"),
        ),
        versions=SnapshotVersions(
            data_version="data",
            financial_version="financial",
            valuation_version="valuation",
            ruler_version_ids=MappingProxyType(
                {"quality_discount": None, "deep_value": None, "garp": None}
            ),
            algorithm_version=BUILD_VERSION,
        ),
        trading_dates=MappingProxyType({"SH": ("2026-09-15",)}),
        snapshot_at=_company().snapshot_at,
    )
    monkeypatch.setattr(
        calibrate_discovery,
        "build_production_snapshot",
        lambda _session: snapshot,
    )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    artifact = calibrate_discovery.calibrate(db, track="deep_value")
    assert artifact["status"] == "ready"
    assert artifact["approval_ready"] is True
    assert artifact["proposal"] == artifact["hypotheses"]["base"]
    assert set(artifact["hypotheses"]) == {
        "conservative",
        "base",
        "relaxed",
    }
    assert artifact["proposal"]["track"] == "deep_value"
    assert artifact["proposal"]["formulas"]["non_core_income_proxy_share"] == (
        "(copy_abs(latest(annual.other_income))+"
        "copy_abs(latest(annual.asset_disposal_gain)))/"
        "copy_abs(normalized_earnings)"
    )
    assert artifact["provenance_coverage"]["other_income"]["trusted"] == 1
    assert "conservative non-core-income proxy" in artifact["note"]
    assert "government grant" not in artifact["note"].lower()
    assert artifact["candidate_counts"]["base"]["branches"]["bank"]["pass"] == 1
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_calibrator_deep_value_blocks_missing_trusted_proxy_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from discovery.snapshot import (
        CompanySnapshotInput,
        ProductionSnapshot,
        SnapshotVersions,
    )
    from scripts import calibrate_discovery
    from tests.test_discovery_tracks_quality import _calibration_preflight_db

    db = tmp_path / "missing-trusted.db"
    _calibration_preflight_db(db)
    company = _company(
        annual=tuple(
            _deep_annual(year, other_income=None)
            for year in range(2021, 2026)
        )
    )
    snapshot = ProductionSnapshot(
        fill_id=7,
        companies=(
            CompanySnapshotInput(
                code=company.code,
                name=company.name,
                industry=company.industry,
                security_status=company.security_status,
                fields=company.fields,
                sources=company.sources,
                as_of=company.as_of,
            ),
        ),
        versions=SnapshotVersions(
            data_version="data",
            financial_version="financial",
            valuation_version="valuation",
            ruler_version_ids=MappingProxyType(
                {"quality_discount": None, "deep_value": None, "garp": None}
            ),
            algorithm_version=BUILD_VERSION,
        ),
        trading_dates=MappingProxyType({"SH": ("2026-09-15",)}),
        snapshot_at=company.snapshot_at,
    )
    monkeypatch.setattr(
        calibrate_discovery,
        "build_production_snapshot",
        lambda _session: snapshot,
    )
    artifact = calibrate_discovery.calibrate(db, track="deep_value")
    assert artifact["status"] == "blocked"
    assert artifact["approval_ready"] is False
    assert artifact["blocked_reasons"] == [
        "trusted_field_coverage_below_readiness"
    ]
