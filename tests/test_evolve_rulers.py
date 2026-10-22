"""Tests for aftermath-driven discovery ruler evolution."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.discovery_models import (
    DiscoveryInputSnapshot,
    DiscoveryResult,
    DiscoveryRun,
)
from app.models import (
    Base,
    Financial,
    PortfolioReview,
    Quote,
    ResearchArtifact,
    ResearchJob,
)
from discovery.builds import BUILD_VERSION
from discovery.rulers import activate
from ledger.positions import LOCAL_USER_ID
from scripts.calibrate_discovery import (
    DEEP_VALUE_BANDS,
    GARP_BANDS,
    HYPOTHESIS_BANDS,
    _deep_value_payload,
    _garp_payload,
    _quality_payload,
)
from scripts.evolve_rulers import evolve, main


def _seed_evolution_db(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
    industries = ["医药生物", "银行Ⅱ", "煤炭开采"]

    with Session(engine) as session:
        q_row = activate(
            session,
            _quality_payload(
                HYPOTHESIS_BANDS["base"],
                industry_version="sw-2021",
                industries=industries,
                valuation_ratio_method="PE_TTM/PB_MRQ",
            ),
            "base quality",
        )
        d_row = activate(
            session,
            _deep_value_payload(
                DEEP_VALUE_BANDS["base"],
                industry_version="sw-2021",
                valuation_ratio_method="PE_TTM/PB_MRQ",
            ),
            "base deep value",
        )
        g_row = activate(
            session,
            _garp_payload(
                GARP_BANDS["base"],
                industry_version="sw-2021",
                valuation_ratio_method="PE_TTM/PB_MRQ",
            ),
            "base garp",
        )
        run = DiscoveryRun(
            user_id=LOCAL_USER_ID,
            week_key="2026-W43",
            status="completed",
            algorithm_version=BUILD_VERSION,
            data_version="dv1",
            financial_version="fv1",
            valuation_version="vv1",
            input_fingerprint="fp1",
            quality_discount_ruler_version_id=q_row.id,
            deep_value_ruler_version_id=d_row.id,
            garp_ruler_version_id=g_row.id,
            snapshot_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        session.flush()

        for code, name in (
            ("300750.SZ", "宁德时代"),
            ("300627.SZ", "华测导航"),
            ("000538.SZ", "云南白药"),
        ):
            session.add(
                DiscoveryInputSnapshot(
                    run_id=run.id,
                    code=code,
                    name=name,
                    industry="医药生物",
                    security_status="{}",
                    fields_json="{}",
                    sources_json="{}",
                    as_of_json="{}",
                    data_version="dv1",
                    financial_version="fv1",
                    valuation_version="vv1",
                    ruler_versions_json="{}",
                    algorithm_version=BUILD_VERSION,
                    input_fingerprint="fp1",
                    snapshot_at=now,
                )
            )

        base_q_metrics = {
            "code": "300750.SZ",
            "branch": "nonfinancial",
            "quality_branch": "nonfinancial",
            "latest_fiscal_year": "2025",
            "listing_years": "8",
            "roe_mean": "19.0",
            "roe_downside_volatility": "0.1",
            "roe_recent_ratio": "1.1",
            "positive_net_income_observations": "5",
            "latest_net_income": "100",
            "latest_net_assets": "500",
            "cash_conversion": "1.5",
            "dilution_rate": "0",
            "debt_to_equity": "0.2",
            "cash_to_assets": "0.25",
            "interest_debt_to_assets": "0.05",
            "interest_coverage": "20",
            "margin_change": "1.0",
            "pe_percentile": "5",
            "pe_median_ratio": "0.6",
            "pb_percentile": "5",
            "pb_median_ratio": "0.6",
            "earnings_yield": "6.5",
            "deducted_profit_cagr": "0.3",
            "peg": "0.6",
            "roe_observations": "5",
            "roe_recent_available_count": "3",
            "profitability_available_count": "5",
            "positive_equity_available_count": "1",
            "cash_conversion_observations": "5",
            "dilution_available_count": "5",
            "leverage_available_count": "1",
            "interest_coverage_available_count": "1",
            "margin_available_count": "5",
            "valuation_pe_observations": "60",
            "valuation_pb_observations": "60",
            "growth_available_count": "5",
        }
        fp_q_metrics = {
            **base_q_metrics,
            "code": "300627.SZ",
            "cash_conversion": "1.02",
            "earnings_yield": "3.3",
            "peg": "1.2",
        }
        fn_q_metrics = {
            **base_q_metrics,
            "code": "000538.SZ",
            "roe_mean": "11.275",
            "triggered_evidence_rule_ids": ["quality.roe_mean_below_min"],
        }
        session.add_all(
            [
                DiscoveryResult(
                    run_id=run.id,
                    track="quality_discount",
                    stage="track_gate",
                    attempt=1,
                    code="300750.SZ",
                    verdict="pass",
                    reason=None,
                    metrics_json=json.dumps(base_q_metrics),
                    created_at=now,
                ),
                DiscoveryResult(
                    run_id=run.id,
                    track="quality_discount",
                    stage="track_gate",
                    attempt=1,
                    code="300627.SZ",
                    verdict="pass",
                    reason=None,
                    metrics_json=json.dumps(fp_q_metrics),
                    created_at=now,
                ),
                DiscoveryResult(
                    run_id=run.id,
                    track="quality_discount",
                    stage="track_gate",
                    attempt=1,
                    code="000538.SZ",
                    verdict="drop",
                    reason="roe_mean_below_min",
                    metrics_json=json.dumps(fn_q_metrics),
                    created_at=now,
                ),
            ]
        )

        for code, name, verdict, gate_status in (
            ("300750.SZ", "宁德时代", "pass", "pass"),
            ("300627.SZ", "华测导航", "fail", "fail"),
            ("000538.SZ", "云南白药", "pass", "pass"),
        ):
            job = ResearchJob(
                user_id=LOCAL_USER_ID,
                name=name,
                code=code,
                status="done",
                discovery_run_id=run.id,
                discovery_track="quality_discount",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            structured = {
                "verdict": verdict,
                "gates": [
                    {"name": "能力圈", "status": "pass", "reason": "ok"},
                    {"name": "好生意", "status": "pass", "reason": "ok"},
                    {"name": "护城河", "status": "pass", "reason": "ok"},
                    {"name": "管理层", "status": "pass", "reason": "ok"},
                    {"name": "安全边际", "status": gate_status, "reason": "估值检验"},
                    {"name": "决策纪律", "status": "pass", "reason": "ok"},
                ],
                "mirror_test": "一、二、三、四、五",
                "price_bands": {
                    "mode": "price",
                    "aggressive": "10",
                    "steady": "15",
                    "conservative": "20",
                    "overvalued": "30",
                },
                "memo": {"essence": "a", "risks": "b", "role": "core"},
                "data_grade": "A",
                "as_of": {"date": "2026-10-18", "price": "18"},
                "portfolio_fit": "fit",
            }
            session.add(
                ResearchArtifact(
                    research_job_id=job.id,
                    structured_json=json.dumps(structured, ensure_ascii=False),
                    markdown="# memo",
                )
            )
            session.add(
                Quote(
                    user_id=LOCAL_USER_ID,
                    name=name,
                    code=code,
                    price="18",
                    source="test",
                    stale=False,
                    updated_at=now,
                )
            )

        session.add_all(
            [
                Financial(
                    code="300750.SZ",
                    period="2025H1",
                    period_type="interim",
                    net_income=50,
                    ocf=70,
                ),
                Financial(
                    code="300750.SZ",
                    period="2026H1",
                    period_type="interim",
                    net_income=70,
                    ocf=95,
                ),
                Financial(
                    code="300627.SZ",
                    period="2025H1",
                    period_type="interim",
                    net_income=30,
                    ocf=13,
                ),
                Financial(
                    code="300627.SZ",
                    period="2026H1",
                    period_type="interim",
                    net_income=27,
                    ocf=5,
                ),
            ]
        )
        session.add(
            PortfolioReview(
                user_id=LOCAL_USER_ID,
                status="done",
                structured_json=json.dumps(
                    {
                        "positions": [
                            {
                                "name": "宁德时代",
                                "action": "cut",
                                "rule_light": "hit",
                                "reason": "超配再平衡",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                markdown="# review",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    engine.dispose()


def test_evolve_rulers_proposes_research_hypothesis_and_refuses_apply(
    tmp_path: Path,
) -> None:
    db = tmp_path / "evolve.db"
    _seed_evolution_db(db)

    before_bytes = hashlib.sha256(db.read_bytes()).hexdigest()
    report = evolve(db, tracks=("quality_discount",), apply=False)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_bytes

    assert report["status"] == "ready"
    assert report["applied"] is False
    assert report["min_support"] == 2
    q_report = report["tracks"]["quality_discount"]
    # Model disagreements remain hypotheses, regardless of the legacy support setting.
    assert q_report["changed"] is False
    assert q_report["has_shadow_staging"] is True
    assert q_report["threshold_changes"]["peg_release_max"]["status"] == "research_hypothesis"
    assert q_report["counts"] == {
        "baseline_track_pass": 2,
        "evolved_track_pass": 2,
        "shadow_track_pass": 1,
        "evolved_competition_pass": 2,
        "borda_competition_pass": 2,
        "research_disagreement_total": 1,
        "research_disagreement_excluded_by_hypothesis": 1,
        "research_disagreement_caught_by_interim": 1,
        "research_agreement_hit_by_interim": 0,
        "unresearched_flagged_by_interim": 0,
        "research_agreement_retained": 1,
        "research_agreement_total": 1,
        "near_miss_total": 1,
    }
    assert q_report["interim_deterioration_alerts"] == [
        {
            "code": "300627.SZ",
            "name": "华测导航",
            "researched_outcome": "fail",
            "period": "2026H1",
            "base_period": "2025H1",
            "triggered_interim_rules": ["interim_peg_growth_broken"],
            "profit_yoy": "-0.1",
            "revenue_yoy": None,
            "interim_cash_conversion": "0.19",
        }
    ]
    assert "peg_release_max" in q_report["threshold_changes"]
    assert "cash_conversion_min" not in q_report["threshold_changes"]
    assert any(
        item["threshold"] == "cash_conversion_min"
        and item["rejected_reason"] == "spurious_non_causal_correlation"
        for item in q_report["rejected_overfit_candidates"]
    )
    assert q_report["research_gate_disagreement"] == [
        {
            "code": "000538.SZ",
            "name": "云南白药",
            "discovery_verdict": "drop",
            "discovery_reason": "roe_mean_below_min",
            "triggered_rules": ["quality.roe_mean_below_min"],
        }
    ]
    assert q_report["near_miss_candidates"] == [
        {
            "code": "000538.SZ",
            "name": "云南白药",
            "competition_rank_in_near_miss": 1,
            "blocked_reason": "roe_mean_below_min",
            "blocked_rule": "quality.roe_mean_below_min",
            "researched_outcome": "pass",
        }
    ]

    output_file = tmp_path / "out.json"
    exit_code = main(
        [
            "--track",
            "quality_discount",
            "--db",
            str(db),
            "--min-support",
            "1",
            "--output",
            str(output_file),
            "--apply",
        ]
    )
    assert exit_code == 2
    saved = json.loads(output_file.read_text(encoding="utf-8"))
    assert saved["applied"] is False
    assert saved["proposal_kind"] == "research_hypothesis"
    assert saved["blocked_reasons"] == ["independent_validation_required"]
    assert saved["validation_report"]["eligible"] is False
    assert saved["validation_report"]["evidence_gaps"]
    assert saved["activated_ruler_version_ids"] == {}
    assert saved["tracks"]["quality_discount"]["threshold_changes"]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_bytes
