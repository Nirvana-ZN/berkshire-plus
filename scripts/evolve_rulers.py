"""Evolve discovery-track rulers from deep-research and portfolio-aftermath outcomes.

Reads the latest completed discovery run and downstream research/aftermath/review
outcomes, proposes pareto-safe threshold tightenings that drop false positives
(downstream fail / overvalued / fundamental cut-sell) while preserving protected
candidates (downstream pass / gray), and optionally activates evolved rulers via
discovery.rulers.activate().
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.discovery_models import (
    DiscoveryInputSnapshot,
    DiscoveryResult,
    DiscoveryRun,
)
from app.models import (
    DiscoveryDataCheckpoint,
    Financial,
    PortfolioReview,
    Quote,
    ResearchArtifact,
    ResearchJob,
    RulerVersion,
)
from discovery.builds import BUILD_VERSION
from discovery.competition import (
    CompetitionCandidate,
    rank_borda_competition,
    rank_competition,
)
from discovery.rulers import (
    activate as activate_ruler,
    canonical_payload,
    deep_value_ruler,
    garp_ruler,
    latest as latest_ruler,
    load as load_ruler,
    payload_digest,
    quality_discount_ruler,
)
from discovery.tracks import (
    deep_value as dv,
    garp as gp,
    quality_discount as qd,
)
from ledger.positions import LOCAL_USER_ID
from research.thesis import list_aftermath, price_usable
from scripts.calibrate_discovery import (
    DEEP_VALUE_BANDS,
    DEFAULT_DB,
    GARP_BANDS,
    HYPOTHESIS_BANDS,
    _paths_alias,
    _read_only_connection,
)

TRACK_SPECS: Mapping[str, tuple[Any, Any, Mapping[str, Mapping[str, str]]]] = {
    "quality_discount": (qd, quality_discount_ruler, HYPOTHESIS_BANDS),
    "deep_value": (dv, deep_value_ruler, DEEP_VALUE_BANDS),
    "garp": (gp, garp_ruler, GARP_BANDS),
}

# Anti-overfitting guards:
# 1. At most 2 thresholds per track may deviate from the base band (prevents one-way ratchet).
# 2. Each eliminated false positive may cause at most 3 unresearched collateral drops.
# 3. Minimum evidence support (Bayesian shrinkage): by default >= 2 independent FPs are required
#    to promote a threshold tightening from 'shadow_staging' to 'confirmed' live activation.
MAX_CHANGED_THRESHOLDS_PER_TRACK = 2
MAX_COLLATERAL_PER_FP = 3
DEFAULT_MIN_SUPPORT = 2
MAX_NEAR_MISS_REPORT = 15

_VALUATION_THRESHOLDS = frozenset(
    {
        "pe_percentile_max",
        "pe_median_ratio_max",
        "pb_percentile_max",
        "pb_median_ratio_max",
        "normalized_pe_max",
        "earnings_yield_spread_min",
        "peg_release_max",
        "peg_max",
        "pe_max",
        "peg_growth_cap",
    }
)
_QUALITY_MOAT_THRESHOLDS = frozenset(
    {
        "roe_mean_min",
        "roe_downside_volatility_max",
        "roe_recent_ratio_min",
        "bank_roe_min",
        "return_on_capital_min",
        "margin_deterioration_max",
        "gross_margin_deterioration_max",
        "net_margin_deterioration_max",
        "revenue_cagr_min",
        "deducted_profit_cagr_min",
        "revenue_period_growth_min",
        "deducted_profit_period_growth_min",
        "revenue_growth_slowdown_max",
        "deducted_profit_growth_slowdown_max",
        "revenue_decline_max",
        "earnings_decline_max",
    }
)
_GOVERNANCE_BALANCE_THRESHOLDS = frozenset(
    {
        "dilution_rate_max",
        "cash_to_assets_min",
        "interest_debt_to_assets_min",
        "debt_to_equity_max",
        "interest_coverage_min",
        "interest_debt_increase_to_revenue_max",
        "goodwill_increase_to_revenue_max",
        "acquisition_cash_to_revenue_max",
        "impairment_share_max",
        "one_time_profit_share_max",
        "non_core_income_proxy_share_max",
        "bank_npl_max",
        "bank_npl_increase_max",
        "bank_capital_adequacy_min",
        "bank_core_tier1_min",
    }
)
_CASHFLOW_DEBT_THRESHOLDS = frozenset(
    {
        "cash_conversion_min",
        "cumulative_fcf_min",
        "normalized_fcf_min",
        "net_debt_to_normalized_fcf_max",
        "interest_debt_increase_to_revenue_max",
        "cash_to_assets_min",
        "interest_debt_to_assets_min",
    }
)
_CASHFLOW_REASON_KEYWORDS = (
    "OCF",
    "经营现金流",
    "自由现金流",
    "资本开支",
    "有息负债",
    "举债",
    "存贷",
)
_SLOWDOWN_REASON_KEYWORDS = ("失速", "下滑", "降速", "负增长")


def _causal_allowed_thresholds(outcome: Mapping[str, Any]) -> frozenset[str]:
    """Return threshold keys causally aligned with a false positive's failed gates."""

    allowed: set[str] = set()
    failed_gates = set(outcome.get("failed_gates") or ())
    reasons_text = " ".join(str(r) for r in (outcome.get("failed_gate_reasons") or ()))

    if failed_gates & {"好生意", "护城河"}:
        allowed.update(_QUALITY_MOAT_THRESHOLDS)
    if "管理层" in failed_gates:
        allowed.update(_GOVERNANCE_BALANCE_THRESHOLDS)
        allowed.update(_CASHFLOW_DEBT_THRESHOLDS)
    if "安全边际" in failed_gates or outcome.get("in_band") == "overvalued":
        allowed.update(_VALUATION_THRESHOLDS)
        if any(kw in reasons_text for kw in _CASHFLOW_REASON_KEYWORDS):
            allowed.update(_CASHFLOW_DEBT_THRESHOLDS)
        if any(kw in reasons_text for kw in _SLOWDOWN_REASON_KEYWORDS):
            allowed.update(
                {
                    "revenue_growth_slowdown_max",
                    "deducted_profit_growth_slowdown_max",
                    "revenue_decline_max",
                    "earnings_decline_max",
                }
            )
    if not allowed:
        # Fallback if a fail came purely from portfolio exit without gate tags
        allowed.update(_VALUATION_THRESHOLDS | _QUALITY_MOAT_THRESHOLDS | _CASHFLOW_DEBT_THRESHOLDS)
    return frozenset(allowed)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def _candidate_thresholds(current_str: str, conservative_str: str) -> tuple[str, ...]:
    """Generate coarse economic grid steps (no fine 1/4-band quantile sniping)."""

    current = _decimal(current_str)
    conservative = _decimal(conservative_str)
    if current is None or conservative is None or current == conservative:
        return ()
    step = conservative - current
    raw = (
        current + step / Decimal(2),
        conservative,
        conservative + step / Decimal(2),
        conservative + step,
    )
    seen: list[str] = []
    for item in raw:
        if item < 0:
            continue
        formatted = _format_decimal(item)
        if formatted != current_str and formatted not in seen:
            seen.append(formatted)
    return tuple(seen)


def _eval_company_metrics(
    track: str,
    ruler: Any,
    metrics: Mapping[str, object],
) -> tuple[str, str | None]:
    mod, _, _ = TRACK_SPECS[track]
    branch_key = "quality_branch" if track == "quality_discount" else "branch"
    branch = metrics.get(branch_key)
    if (
        not isinstance(branch, str)
        or branch not in ruler.sector_branches
        or metrics.get("latest_fiscal_year") is None
    ):
        return "insufficient", "branch_or_history_unavailable"
    calc = {
        key: _decimal(metrics.get(key))
        for key in mod._NUMERIC_METRICS
    }
    counts = {
        key[:-16]: int(val)
        for key, val in metrics.items()
        if key.endswith("_available_count") and isinstance(val, (str, int))
    }
    if "profitability" in counts:
        counts["net_income"] = counts["profitability"]
    if "positive_equity" in counts:
        counts["net_assets"] = counts["positive_equity"]
    counts.update(
        {
            key[:-13]: int(val)
            for key, val in metrics.items()
            if key.endswith("_observations") and isinstance(val, (str, int))
        }
    )
    gates = ruler.sector_branches[branch]["gate_order"]
    for gate in gates:
        if track == "quality_discount":
            failures = mod._gate_failures(gate, None, ruler, branch, calc, counts)
        elif track == "deep_value":
            failures = mod._gate_failures(gate, ruler, calc, counts)
        else:
            label = metrics.get("return_metric_label")
            failures = mod._gate_failures(
                gate,
                ruler,
                calc,
                counts,
                str(label) if label is not None else None,
            )
        if failures:
            return failures[0]
    return "pass", None


_INTERIM_SUFFIX_RANK = {"Q1": 1, "H1": 2, "Q3": 3}
_INTERIM_VALUE_KEYS = ("revenue", "deducted_net_income", "net_income", "ocf")


def _load_interim_by_code(
    session: Session, codes: Sequence[str]
) -> dict[str, dict[str, dict[str, Decimal | None]]]:
    """Load non-annual financial rows (H1/Q1/Q3) from checkpoints and financials table."""

    if not codes:
        return {}
    by_code: dict[str, dict[str, dict[str, Decimal | None]]] = {}
    ck_rows = session.execute(
        select(DiscoveryDataCheckpoint.code, DiscoveryDataCheckpoint.payload_json)
        .where(
            DiscoveryDataCheckpoint.code.in_(codes),
            DiscoveryDataCheckpoint.payload_json.is_not(None),
        )
        .order_by(DiscoveryDataCheckpoint.fill_id.asc())
    ).all()
    for code, payload_json in ck_rows:
        if not payload_json:
            continue
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        periods = by_code.setdefault(code, {})
        for row in payload.get("financial_rows") or ():
            if not isinstance(row, Mapping):
                continue
            period = str(row.get("period") or "").strip().upper()
            if len(period) != 6 or not period[:4].isdigit() or period[4:] not in _INTERIM_SUFFIX_RANK:
                continue
            vals = row.get("values")
            if not isinstance(vals, Mapping):
                continue
            periods[period] = {k: _decimal(vals.get(k)) for k in _INTERIM_VALUE_KEYS}

    fin_rows = session.scalars(
        select(Financial).where(Financial.code.in_(codes))
    ).all()
    for fin in fin_rows:
        period = str(fin.period or "").strip().upper()
        if len(period) != 6 or not period[:4].isdigit() or period[4:] not in _INTERIM_SUFFIX_RANK:
            continue
        slot = by_code.setdefault(fin.code, {}).setdefault(
            period, {k: None for k in _INTERIM_VALUE_KEYS}
        )
        if slot.get("net_income") is None and fin.net_income is not None:
            slot["net_income"] = _decimal(fin.net_income)
        if slot.get("ocf") is None and fin.ocf is not None:
            slot["ocf"] = _decimal(fin.ocf)
    return by_code


def _evaluate_interim_deterioration(
    track: str,
    ruler: Any,
    metrics: Mapping[str, object],
    interim_by_period: Mapping[str, Mapping[str, Decimal | None]],
) -> dict[str, object] | None:
    """Detect post-annual interim (H1/Q1/Q3) fundamental deterioration."""

    latest_fy = str(metrics.get("latest_fiscal_year") or "").strip()
    if len(latest_fy) != 4 or not latest_fy.isdigit() or not interim_by_period:
        return None
    candidates = sorted(
        (
            p
            for p in interim_by_period
            if len(p) == 6
            and p[:4].isdigit()
            and int(p[:4]) > int(latest_fy)
            and f"{latest_fy}{p[4:]}" in interim_by_period
        ),
        key=lambda p: (int(p[:4]), _INTERIM_SUFFIX_RANK.get(p[4:], 0)),
    )
    if not candidates:
        return None
    latest_int = candidates[-1]
    prev_int = f"{latest_fy}{latest_int[4:]}"
    cur = interim_by_period[latest_int]
    prev = interim_by_period[prev_int]

    cur_profit = (
        cur.get("deducted_net_income")
        if cur.get("deducted_net_income") is not None
        else cur.get("net_income")
    )
    prev_profit = (
        prev.get("deducted_net_income")
        if prev.get("deducted_net_income") is not None
        else prev.get("net_income")
    )
    profit_yoy = (
        (cur_profit - prev_profit) / prev_profit
        if cur_profit is not None and prev_profit is not None and prev_profit > 0
        else None
    )
    cur_rev = cur.get("revenue")
    prev_rev = prev.get("revenue")
    revenue_yoy = (
        (cur_rev - prev_rev) / prev_rev
        if cur_rev is not None and prev_rev is not None and prev_rev > 0
        else None
    )
    cur_ocf = cur.get("ocf")
    cur_ni = cur.get("net_income") if cur.get("net_income") is not None else cur_profit
    interim_cc = (
        cur_ocf / cur_ni
        if cur_ocf is not None and cur_ni is not None and cur_ni > 0
        else None
    )

    triggered: list[str] = []
    if (
        cur_ocf is not None
        and cur_ocf < 0
        and interim_cc is not None
        and interim_cc <= Decimal("-0.5")
        and (revenue_yoy is None or revenue_yoy < 0)
    ):
        triggered.append("interim_ocf_negative")
    if track == "quality_discount":
        ey = _decimal(metrics.get("earnings_yield"))
        ty = _decimal(ruler.thresholds.get("treasury_yield"))
        spread = _decimal(ruler.thresholds.get("earnings_yield_spread_min"))
        ey_floor = (ty + spread) if (ty is not None and spread is not None) else None
        if (
            ey is not None
            and ey_floor is not None
            and ey < ey_floor
            and profit_yoy is not None
            and profit_yoy < 0
        ):
            triggered.append("interim_peg_growth_broken")
    elif track == "garp":
        rev_cagr = _decimal(metrics.get("revenue_cagr"))
        rev_min = _decimal(ruler.thresholds.get("revenue_cagr_min"))
        slowdown_max = _decimal(ruler.thresholds.get("revenue_growth_slowdown_max"))
        if (
            rev_cagr is not None
            and revenue_yoy is not None
            and rev_min is not None
            and slowdown_max is not None
            and revenue_yoy < rev_min
            and rev_cagr - revenue_yoy > slowdown_max
        ):
            triggered.append("interim_garp_growth_stalled")
    if not triggered:
        return None
    return {
        "period": latest_int,
        "base_period": prev_int,
        "triggered_interim_rules": triggered,
        "profit_yoy": (
            _format_decimal(profit_yoy.quantize(Decimal("0.0001")))
            if profit_yoy is not None
            else None
        ),
        "revenue_yoy": (
            _format_decimal(revenue_yoy.quantize(Decimal("0.0001")))
            if revenue_yoy is not None
            else None
        ),
        "interim_cash_conversion": (
            _format_decimal(interim_cc.quantize(Decimal("0.01")))
            if interim_cc is not None
            else None
        ),
    }


def _load_downstream_outcomes(session: Session) -> dict[str, dict[str, Any]]:
    jobs = session.scalars(
        select(ResearchJob)
        .where(
            ResearchJob.user_id == LOCAL_USER_ID,
            ResearchJob.status == "done",
        )
        .order_by(ResearchJob.id)
    ).all()
    if not jobs:
        return {}
    artifacts: dict[int, ResearchArtifact] = {}
    for art in session.scalars(
        select(ResearchArtifact).where(
            ResearchArtifact.research_job_id.in_([j.id for j in jobs])
        )
    ).all():
        prev = artifacts.get(art.research_job_id)
        if prev is None or art.id > prev.id:
            artifacts[art.research_job_id] = art

    names = sorted({j.name for j in jobs if j.name})
    quotes = {
        q.name: (q.price, price_usable(q.price, stale=q.stale))
        for q in session.scalars(
            select(Quote).where(
                Quote.user_id == LOCAL_USER_ID,
                Quote.name.in_(names),
            )
        ).all()
    }
    aftermath_by_id = {
        int(item["research_id"]): item
        for item in list_aftermath(session, LOCAL_USER_ID, prices=quotes)
    }

    review_actions: dict[str, dict[str, str]] = {}
    latest_review = session.scalars(
        select(PortfolioReview)
        .where(
            PortfolioReview.user_id == LOCAL_USER_ID,
            PortfolioReview.status == "done",
        )
        .order_by(PortfolioReview.id.desc())
    ).first()
    if latest_review is not None and latest_review.structured_json:
        try:
            rev_data = json.loads(latest_review.structured_json)
            for pos in rev_data.get("positions", ()):
                if isinstance(pos, Mapping) and pos.get("name"):
                    review_actions[str(pos["name"])] = {
                        "action": str(pos.get("action") or ""),
                        "rule_light": str(pos.get("rule_light") or "none"),
                    }
        except json.JSONDecodeError:
            pass

    outcomes: dict[str, dict[str, Any]] = {}
    for job in jobs:
        art = artifacts.get(job.id)
        if art is None:
            continue
        try:
            parsed = json.loads(art.structured_json)
        except json.JSONDecodeError:
            continue
        then_verdict = str(parsed.get("verdict") or "")
        after = aftermath_by_id.get(job.id, {})
        live = str(after.get("live_verdict") or then_verdict)
        rev = review_actions.get(job.name, {})
        rev_action = rev.get("action", "")
        rev_light = rev.get("rule_light", "none")
        # A review 'sell' (or 'cut' when thesis is fail) is a full exit; a 'cut' on a
        # pass/gray thesis is a partial trim/rebalance, not a selection false positive.
        fundamental_exit = rev_action == "sell" and rev_light != "hit"
        failed_gates = [
            str(g.get("name"))
            for g in parsed.get("gates", ())
            if isinstance(g, Mapping) and g.get("status") == "fail"
        ]
        failed_gate_reasons = [
            str(g.get("reason") or g.get("conclusion") or "")
            for g in parsed.get("gates", ())
            if isinstance(g, Mapping) and g.get("status") == "fail"
        ]
        effective = (
            "fail"
            if (then_verdict == "fail" or live == "fail" or fundamental_exit)
            else then_verdict
        )
        outcomes[job.code] = {
            "research_id": job.id,
            "code": job.code,
            "name": job.name,
            "then_verdict": then_verdict,
            "live_verdict": live,
            "in_band": after.get("in_band"),
            "review_action": rev_action or None,
            "review_rule_light": rev_light if rev_action else None,
            "failed_gates": failed_gates,
            "failed_gate_reasons": failed_gate_reasons,
            "effective_outcome": effective,
        }
    return outcomes


def _evolve_single_track(
    session: Session,
    run: DiscoveryRun,
    track: str,
    outcomes: Mapping[str, Mapping[str, Any]],
    industries: Mapping[str, str | None],
    names: Mapping[str, str] = {},
    *,
    min_support: int = DEFAULT_MIN_SUPPORT,
) -> dict[str, object]:
    _, validate_fn, bands = TRACK_SPECS[track]
    active_row = latest_ruler(session, track)
    if active_row is None:
        return {
            "track": track,
            "status": "blocked",
            "blocked_reasons": ["active_ruler_missing"],
        }
    active_ruler = load_ruler(active_row)
    base_payload = json.loads(canonical_payload(active_ruler))
    current_thresholds = dict(base_payload["thresholds"])
    conservative_thresholds = bands["conservative"]
    relaxed_payload = json.loads(json.dumps(base_payload))
    relaxed_payload["thresholds"] = dict(bands["relaxed"])
    relaxed_ruler = validate_fn(relaxed_payload)

    stage_rows = session.scalars(
        select(DiscoveryResult)
        .where(
            DiscoveryResult.run_id == run.id,
            DiscoveryResult.track == track,
            DiscoveryResult.stage == "track_gate",
        )
        .order_by(DiscoveryResult.code)
    ).all()
    if not stage_rows:
        return {
            "track": track,
            "status": "blocked",
            "blocked_reasons": ["no_track_gate_results"],
        }

    # Keep rows that passed track_gate under the baseline ruler for fast pareto evaluation,
    # researched rows that were dropped (false negatives), and single-rule near-miss drops
    # that pass under the relaxed calibration band (for Reject Inference).
    passed_rows: list[tuple[str, dict[str, object]]] = []
    false_negatives: list[dict[str, object]] = []
    raw_near_misses: list[tuple[str, str | None, str, dict[str, object]]] = []
    for row in stage_rows:
        metrics = json.loads(row.metrics_json)
        out = outcomes.get(row.code)
        triggered = list(metrics.get("triggered_evidence_rule_ids") or ())
        if row.verdict == "pass":
            passed_rows.append((row.code, metrics))
        else:
            if out is not None and out["effective_outcome"] == "pass":
                false_negatives.append(
                    {
                        "code": row.code,
                        "name": out["name"],
                        "discovery_verdict": row.verdict,
                        "discovery_reason": row.reason,
                        "triggered_rules": triggered,
                    }
                )
            if (
                row.verdict == "drop"
                and len(triggered) == 1
                and _eval_company_metrics(track, relaxed_ruler, metrics)[0] == "pass"
            ):
                raw_near_misses.append((row.code, row.reason, str(triggered[0]), metrics))

    near_miss_order = [
        v.code
        for v in rank_competition(
            [
                CompetitionCandidate(code, industries.get(code), m)
                for code, _, _, m in raw_near_misses
            ],
            active_ruler.order,
        )
    ]
    near_miss_by_code = {code: (rsn, rule, m) for code, rsn, rule, m in raw_near_misses}
    gate_stats: dict[str, dict[str, int]] = {}
    near_miss_candidates: list[dict[str, object]] = []
    unresearched_nm_count = 0
    for rank_idx, nm_code in enumerate(near_miss_order, start=1):
        rsn, rule, _ = near_miss_by_code[nm_code]
        nm_out = outcomes.get(nm_code)
        eff = str(nm_out["effective_outcome"]) if nm_out is not None else None
        bucket = gate_stats.setdefault(
            rule,
            {"total_near_miss": 0, "researched_pass_or_gray": 0, "researched_fail": 0},
        )
        bucket["total_near_miss"] += 1
        if eff in {"pass", "gray"}:
            bucket["researched_pass_or_gray"] += 1
        elif eff == "fail":
            bucket["researched_fail"] += 1
        if nm_out is not None or unresearched_nm_count < MAX_NEAR_MISS_REPORT:
            if nm_out is None:
                unresearched_nm_count += 1
            near_miss_candidates.append(
                {
                    "code": nm_code,
                    "name": (
                        str(nm_out["name"])
                        if nm_out is not None
                        else names.get(nm_code, nm_code)
                    ),
                    "competition_rank_in_near_miss": rank_idx,
                    "blocked_reason": rsn,
                    "blocked_rule": rule,
                    "researched_outcome": eff,
                }
            )
    near_miss_gate_summary = {
        rule: {
            **stats,
            "relaxation_signal": (
                "consider_relaxing"
                if stats["researched_pass_or_gray"] >= min_support
                and stats["researched_fail"] == 0
                else "hold_or_audit"
            ),
        }
        for rule, stats in sorted(
            gate_stats.items(), key=lambda item: (-item[1]["total_near_miss"], item[0])
        )
    }

    false_positives: list[tuple[str, dict[str, object], Mapping[str, Any]]] = []
    protected: list[tuple[str, dict[str, object], Mapping[str, Any]]] = []
    interim_by_code = _load_interim_by_code(
        session, [code for code, _ in passed_rows]
    )
    interim_alerts_by_code: dict[str, dict[str, object]] = {}
    interim_deterioration_alerts: list[dict[str, object]] = []
    for code, metrics in passed_rows:
        out = outcomes.get(code)
        alert = _evaluate_interim_deterioration(
            track,
            active_ruler,
            metrics,
            interim_by_code.get(code, {}),
        )
        if alert is not None:
            interim_alerts_by_code[code] = alert
            eff_out = str(out["effective_outcome"]) if out is not None else None
            interim_deterioration_alerts.append(
                {
                    "code": code,
                    "name": (
                        str(out["name"])
                        if out is not None
                        else names.get(code, code)
                    ),
                    "researched_outcome": eff_out,
                    **alert,
                }
            )
        if out is None:
            continue
        if out["effective_outcome"] == "fail":
            false_positives.append((code, metrics, out))
        elif out["effective_outcome"] in {"pass", "gray"}:
            protected.append((code, metrics, out))

    selected_changes: dict[str, dict[str, object]] = {}
    shadow_flagged_candidates: list[dict[str, object]] = []
    rejected_overfit_candidates: list[dict[str, object]] = []
    working_payload = json.loads(json.dumps(base_payload))
    confirmed_payload = json.loads(json.dumps(base_payload))
    remaining_fps = list(false_positives)
    base_band_thresholds = bands["base"]
    prior_deviations = {
        k
        for k, v in current_thresholds.items()
        if k in base_band_thresholds and str(v) != str(base_band_thresholds[k])
    }

    # Greedily select minimal threshold tightenings that eliminate remaining FPs
    # while enforcing 5 anti-overfitting guards:
    #   (1) Causal gate alignment between deep-research failure reason and threshold
    #   (2) Coarse economic grid steps (no 1/4-step quantile sniping)
    #   (3) Max collateral drop ratio (<= MAX_COLLATERAL_PER_FP unresearched drops per FP)
    #   (4) Complexity budget (<= MAX_CHANGED_THRESHOLDS_PER_TRACK deviations from base band)
    #   (5) Minimum evidence support (min_support): single-sample rules stay in shadow_staging
    while remaining_fps:
        current_deviations = prior_deviations | set(selected_changes)
        current_ruler = validate_fn(working_payload)
        current_pass_codes = {
            c
            for c, m in passed_rows
            if _eval_company_metrics(track, current_ruler, m)[0] == "pass"
        }
        best_choice: tuple[tuple[int, int, int, str, str], str, str, list[str], dict[str, str], list[tuple[str, str]]] | None = None
        for key in sorted(current_thresholds):
            if (
                key not in current_deviations
                and len(current_deviations) >= MAX_CHANGED_THRESHOLDS_PER_TRACK
            ):
                continue
            cur_val = str(working_payload["thresholds"][key])
            cons_val = str(conservative_thresholds.get(key, cur_val))
            for idx, cand_val in enumerate(_candidate_thresholds(cur_val, cons_val)):
                trial_payload = json.loads(json.dumps(working_payload))
                trial_payload["thresholds"][key] = cand_val
                try:
                    trial_ruler = validate_fn(trial_payload)
                except (TypeError, ValueError):
                    continue
                if any(
                    _eval_company_metrics(track, trial_ruler, pm)[0] != "pass"
                    for _, pm, _ in protected
                ):
                    continue
                causally_dropped: list[str] = []
                spurious_dropped: list[str] = []
                drop_reasons: dict[str, str] = {}
                for fp_code, fp_m, fp_out in remaining_fps:
                    verdict, reason = _eval_company_metrics(track, trial_ruler, fp_m)
                    if verdict != "pass":
                        drop_reasons[fp_code] = str(reason or verdict)
                        if key in _causal_allowed_thresholds(fp_out):
                            causally_dropped.append(fp_code)
                        else:
                            spurious_dropped.append(fp_code)
                if not causally_dropped:
                    if spurious_dropped and idx == 0:
                        rejected_overfit_candidates.append(
                            {
                                "threshold": key,
                                "from": cur_val,
                                "to": cand_val,
                                "rejected_reason": "spurious_non_causal_correlation",
                                "would_drop_codes": spurious_dropped,
                            }
                        )
                    continue
                collateral_items: list[tuple[str, str]] = []
                total_pass = 0
                for c, m in passed_rows:
                    verdict, reason = _eval_company_metrics(track, trial_ruler, m)
                    if verdict == "pass":
                        total_pass += 1
                    elif c in current_pass_codes and c not in outcomes:
                        collateral_items.append((c, str(reason or verdict)))
                collateral_drops = len(collateral_items)
                if collateral_drops > MAX_COLLATERAL_PER_FP * len(causally_dropped):
                    if idx == 0:
                        rejected_overfit_candidates.append(
                            {
                                "threshold": key,
                                "from": cur_val,
                                "to": cand_val,
                                "rejected_reason": "excessive_collateral_drops",
                                "collateral_drops": collateral_drops,
                                "would_drop_codes": causally_dropped,
                            }
                        )
                    continue
                # Prefer: more causal FPs dropped -> higher total_pass -> smaller step index -> lexical tie-break
                score = (len(causally_dropped), total_pass, -idx, key, cand_val)
                if best_choice is None or score > best_choice[0]:
                    best_choice = (
                        score,
                        key,
                        cand_val,
                        causally_dropped,
                        drop_reasons,
                        collateral_items,
                    )
                break  # First valid step for this key is its minimal tightening
        if best_choice is None:
            break
        _, chosen_key, chosen_val, dropped_codes, drop_reasons, collateral_items = best_choice
        working_payload["thresholds"][chosen_key] = chosen_val
        is_confirmed = len(dropped_codes) >= min_support
        if is_confirmed:
            confirmed_payload["thresholds"][chosen_key] = chosen_val
        ref_names = [ str(outcomes[c]["name"]) for c in dropped_codes ]
        if not is_confirmed:
            for col_code, col_reason in collateral_items:
                shadow_flagged_candidates.append(
                    {
                        "code": col_code,
                        "name": names.get(col_code, col_code),
                        "triggered_threshold": f"{chosen_key}: {current_thresholds[chosen_key]}->{chosen_val}",
                        "drop_reason": col_reason,
                        "reference_false_positives": ref_names,
                        "warning": f"同病相怜预警：触发与已证伪样本【{','.join(ref_names)}】相同的 {col_reason}",
                    }
                )
        selected_changes[chosen_key] = {
            "from": current_thresholds[chosen_key],
            "to": chosen_val,
            "conservative_reference": conservative_thresholds.get(chosen_key),
            "status": "confirmed" if is_confirmed else "shadow_staging",
            "collateral_unresearched_drops": len(collateral_items),
            "collateral_codes": [c for c, _ in collateral_items],
            "evidence_confidence": (
                "confirmed" if len(dropped_codes) >= 2 else "single_sample_caution"
            ),
            "dropped_false_positives": [
                {
                    "code": c,
                    "name": outcomes[c]["name"],
                    "drop_reason": drop_reasons[c],
                    "failed_gates": outcomes[c]["failed_gates"],
                }
                for c in dropped_codes
            ],
        }
        remaining_fps = [
            item for item in remaining_fps if item[0] not in set(dropped_codes)
        ]

    confirmed_changes = {
        k: v for k, v in selected_changes.items() if v["status"] == "confirmed"
    }
    evolved_ruler = validate_fn(confirmed_payload)
    evolved_json = canonical_payload(evolved_ruler)
    shadow_ruler = validate_fn(working_payload)
    shadow_json = canonical_payload(shadow_ruler)
    evolved_survived = [
        (code, metrics)
        for code, metrics in passed_rows
        if _eval_company_metrics(track, evolved_ruler, metrics)[0] == "pass"
    ]
    shadow_survived = [
        (code, metrics)
        for code, metrics in passed_rows
        if _eval_company_metrics(track, shadow_ruler, metrics)[0] == "pass"
    ]
    comp_candidates = [
        CompetitionCandidate(code, industries.get(code), metrics)
        for code, metrics in evolved_survived
    ]
    comp_verdicts = rank_competition(
        comp_candidates,
        evolved_ruler.order,
    )
    comp_pass_codes = [v.code for v in comp_verdicts if v.verdict == "pass"]
    borda_verdicts = rank_borda_competition(
        comp_candidates,
        evolved_ruler.order,
    )
    borda_pass_codes = [v.code for v in borda_verdicts if v.verdict == "pass"]
    lex_by_code = {v.code: v for v in comp_verdicts}
    borda_by_code = {v.code: v for v in borda_verdicts}
    borda_promoted = [
        {
            "code": v.code,
            "name": names.get(v.code, v.code),
            "borda_rank": v.rank,
            "lexicographic_rank": lex_by_code[v.code].rank,
            "researched_outcome": (
                str(outcomes[v.code]["effective_outcome"])
                if v.code in outcomes
                else None
            ),
        }
        for v in borda_verdicts
        if v.verdict == "pass" and lex_by_code[v.code].verdict != "pass"
    ]
    borda_demoted = [
        {
            "code": v.code,
            "name": names.get(v.code, v.code),
            "lexicographic_rank": v.rank,
            "borda_rank": borda_by_code[v.code].rank,
            "researched_outcome": (
                str(outcomes[v.code]["effective_outcome"])
                if v.code in outcomes
                else None
            ),
        }
        for v in comp_verdicts
        if v.verdict == "pass" and borda_by_code[v.code].verdict != "pass"
    ]

    change_summaries = [
        f"{k} {info['from']}->{info['to']} [{info['status']}] (拦截{','.join( d['name'] for d in info['dropped_false_positives'] )})"
        for k, info in selected_changes.items()
    ]
    reason_text = (
        f"事后反馈自动进化（基于 run {run.id} 与 {len(outcomes)} 家深研/持仓结果，min_support={min_support}）："
        + ("；".join(change_summaries) if change_summaries else "当前尺子已无帕累托可收紧空间")
        + f"。正式通过 {len(passed_rows)}->{len(evolved_survived)}（含影子观察则为 {len(shadow_survived)}），受保护样本 {len(protected)}/{len(protected)} 保留。"
    )

    return {
        "track": track,
        "status": "ready",
        "min_support": min_support,
        "baseline_ruler_version_id": active_row.id,
        "baseline_digest": active_row.digest,
        "evolved_digest": payload_digest(evolved_ruler),
        "shadow_digest": payload_digest(shadow_ruler),
        "changed": bool(confirmed_changes),
        "has_shadow_staging": len(selected_changes) > len(confirmed_changes),
        "threshold_changes": selected_changes,
        "shadow_flagged_candidates": shadow_flagged_candidates,
        "interim_deterioration_alerts": interim_deterioration_alerts,
        "rejected_overfit_candidates": rejected_overfit_candidates,
        "near_miss_gate_summary": near_miss_gate_summary,
        "near_miss_candidates": near_miss_candidates,
        "borda_vs_lexicographic": {
            "borda_promoted": borda_promoted,
            "borda_demoted": borda_demoted,
        },
        "evolved_survived_codes": [code for code, _ in evolved_survived],
        "counts": {
            "baseline_track_pass": len(passed_rows),
            "evolved_track_pass": len(evolved_survived),
            "shadow_track_pass": len(shadow_survived),
            "evolved_competition_pass": len(comp_pass_codes),
            "borda_competition_pass": len(borda_pass_codes),
            "false_positives_total": len(false_positives),
            "false_positives_eliminated": len(false_positives) - len(remaining_fps),
            "false_positives_caught_by_interim": sum(
                1 for code, _, _ in false_positives if code in interim_alerts_by_code
            ),
            "protected_hit_by_interim": sum(
                1 for code, _, _ in protected if code in interim_alerts_by_code
            ),
            "unresearched_flagged_by_interim": sum(
                1
                for item in interim_deterioration_alerts
                if item["researched_outcome"] is None
            ),
            "protected_retained": len(protected),
            "protected_total": len(protected),
            "near_miss_total": len(raw_near_misses),
        },
        "false_positives": [
            {
                "code": code,
                "name": out["name"],
                "then_verdict": out["then_verdict"],
                "live_verdict": out["live_verdict"],
                "review_action": out["review_action"],
                "failed_gates": out["failed_gates"],
                "eliminated": code not in {c for c, _, _ in remaining_fps},
                "interim_deterioration": interim_alerts_by_code.get(code),
            }
            for code, _, out in false_positives
        ],
        "protected_samples": [
            {
                "code": code,
                "name": out["name"],
                "effective_outcome": out["effective_outcome"],
            }
            for code, _, out in protected
        ],
        "false_negatives": false_negatives,
        "recommended_reason": reason_text,
        "proposal": json.loads(evolved_json),
        "shadow_proposal": json.loads(shadow_json),
    }


def evolve(
    db_path: Path | str = DEFAULT_DB,
    *,
    tracks: Sequence[str] = ("quality_discount", "deep_value", "garp"),
    min_support: int = DEFAULT_MIN_SUPPORT,
    apply: bool = False,
) -> dict[str, object]:
    """Compute (and optionally activate) aftermath-evolved discovery rulers."""

    if min_support < 1:
        raise ValueError("min_support must be >= 1")
    path = Path(db_path)
    if not path.is_file():
        return {"status": "blocked", "blocked_reasons": ["database_missing"]}
    if apply:
        engine = create_engine(f"sqlite:///{path.resolve()}")
    else:
        engine = create_engine(
            "sqlite://",
            creator=lambda: _read_only_connection(path),
            poolclass=StaticPool,
        )
    try:
        with Session(engine) as session:
            run = session.scalars(
                select(DiscoveryRun)
                .where(DiscoveryRun.status == "completed")
                .order_by(DiscoveryRun.id.desc())
            ).first()
            if run is None:
                return {
                    "status": "blocked",
                    "blocked_reasons": ["no_completed_discovery_run"],
                }
            outcomes = _load_downstream_outcomes(session)
            if not outcomes:
                return {
                    "status": "blocked",
                    "blocked_reasons": ["no_downstream_outcomes"],
                }
            snapshots = session.scalars(
                select(DiscoveryInputSnapshot).where(
                    DiscoveryInputSnapshot.run_id == run.id
                )
            ).all()
            industries = {row.code: row.industry for row in snapshots}
            names = {row.code: row.name for row in snapshots}
            track_reports: dict[str, dict[str, object]] = {}
            activated_ids: dict[str, int] = {}
            for track in tracks:
                if track not in TRACK_SPECS:
                    raise ValueError(f"unsupported discovery track: {track}")
                report = _evolve_single_track(
                    session,
                    run,
                    track,
                    outcomes,
                    industries,
                    names,
                    min_support=min_support,
                )
                if apply and report.get("status") == "ready" and report.get("changed"):
                    row = activate_ruler(
                        session,
                        report["proposal"],  # type: ignore[arg-type]
                        report["recommended_reason"],
                    )
                    activated_ids[track] = row.id
                    report["activated_ruler_version_id"] = row.id
                track_reports[track] = report
            if apply and activated_ids:
                session.commit()
            track_membership: dict[str, list[str]] = {}
            shadow_codes: set[str] = set()
            interim_codes: set[str] = set()
            for track_name, rep in track_reports.items():
                for c in rep.get("evolved_survived_codes") or ():
                    track_membership.setdefault(str(c), []).append(track_name)
                for item in rep.get("shadow_flagged_candidates") or ():
                    if isinstance(item, Mapping) and item.get("code"):
                        shadow_codes.add(str(item["code"]))
                for item in rep.get("interim_deterioration_alerts") or ():
                    if isinstance(item, Mapping) and item.get("code"):
                        interim_codes.add(str(item["code"]))
            cross_track_consensus = [
                {
                    "code": c,
                    "name": names.get(c, c),
                    "tracks": matched_tracks,
                    "track_count": len(matched_tracks),
                    "researched_outcome": (
                        str(outcomes[c]["effective_outcome"])
                        if c in outcomes
                        else None
                    ),
                    "shadow_flagged": c in shadow_codes,
                    "interim_deteriorated": c in interim_codes,
                }
                for c, matched_tracks in sorted(
                    track_membership.items(),
                    key=lambda kv: (-len(kv[1]), kv[0]),
                )
                if len(matched_tracks) >= 2
            ]
            return {
                "schema_version": 1,
                "algorithm_build": BUILD_VERSION,
                "status": (
                    "ready"
                    if all(r.get("status") == "ready" for r in track_reports.values())
                    else "blocked"
                ),
                "source_discovery_run_id": run.id,
                "source_week_key": run.week_key,
                "downstream_outcomes_count": len(outcomes),
                "min_support": min_support,
                "applied": apply,
                "activated_ruler_version_ids": activated_ids,
                "cross_track_consensus": cross_track_consensus,
                "tracks": track_reports,
            }
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--track",
        choices=["quality_discount", "deep_value", "garp", "all"],
        default="all",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--min-support",
        type=int,
        default=DEFAULT_MIN_SUPPORT,
        help="Minimum number of eliminated FPs required to promote a rule from shadow_staging to confirmed (default: 2).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Activate confirmed evolved rulers in ruler_versions when changes exist.",
    )
    args = parser.parse_args(argv)
    if args.output is not None and _paths_alias(args.db, args.output):
        parser.error("--output must not alias --db")
    if args.min_support < 1:
        parser.error("--min-support must be >= 1")
    tracks = (
        ("quality_discount", "deep_value", "garp")
        if args.track == "all"
        else (args.track,)
    )
    artifact = evolve(
        args.db,
        tracks=tracks,
        min_support=args.min_support,
        apply=args.apply,
    )
    rendered = (
        json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if artifact.get("status") == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
