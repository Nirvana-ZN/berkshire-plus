"""Serialized discovery stage execution.

Run aggregate status remains derived by the workflow/API layer; this worker
only owns the exact stage attempt it claims.

Track executors are pure internal functions: they receive immutable persisted
inputs and return immutable DTOs. They never receive a Session, Connection, or
other database handle; this module alone validates and persists their output.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
from math import isfinite
import sys

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.discovery_models import (
    DiscoveryCommonResult,
    DiscoveryCommonStage,
    DiscoveryEvidence,
    DiscoveryOverride,
    DiscoveryResult,
    DiscoveryRun,
    DiscoveryStage,
)
from app.models import JobLock
from discovery.aggregate import aggregate_run_status
from discovery.builds import AVAILABLE_BUILDS, _is_build_available
from discovery.rulers import execution_authorized
from discovery.common import CommonVerdict, classify_common
from discovery.competition import CompetitionCandidate, rank_borda_competition, rank_competition
from discovery.constants import (
    COMMON_READINESS_MIN,
    TRACKS,
    VERDICTS,
    common_ready,
    NowProvider,
    aware_utc_now,
    china_week_key,
    utc_now,
)
from discovery.evidence import (
    PersistedEvidenceItem,
    build_evidence_card,
    verify_canonical_evidence_card,
)
from discovery.snapshot import (
    FrozenRunInputs,
    FrozenValue,
    canonical_json,
    load_frozen_inputs,
)
from discovery.rulers import deep_value_ruler, garp_ruler, quality_discount_ruler
from discovery.selection_contract import is_candidate, rank_inputs, load_fundamentals, eligible_legacy_company
from discovery.ranking import rank_candidates, ranking_audit
from discovery.tracks.deep_value import classify as classify_deep_value
from discovery.tracks.garp import classify as classify_garp
from discovery.tracks.quality_discount import (
    bounded_ratio,
    classify as classify_quality_discount,
    decimal_string,
)
from jobs.lock import (
    JOB_TYPE_DISCOVERY,
    acquire_discovery,
    release,
)


_classify_common = classify_common

TRACK_ORDER = ("quality_discount", "deep_value", "garp")


def queue_next_track_stage(
    session: Session,
    run: DiscoveryRun,
    *,
    track: str,
    stage: str,
    parent: DiscoveryCommonStage | DiscoveryStage,
    created_at: datetime,
) -> DiscoveryStage:
    """Queue the next attempt of one track layer under a confirmed parent."""

    attempt = (
        session.scalar(
            select(func.max(DiscoveryStage.attempt)).where(
                DiscoveryStage.run_id == run.id,
                DiscoveryStage.track == track,
                DiscoveryStage.stage == stage,
            )
        )
        or 0
    ) + 1
    row = DiscoveryStage(
        run_id=run.id,
        track=track,
        stage=stage,
        attempt=attempt,
        status="queued",
        total_count=0,
        pass_count=0,
        drop_count=0,
        insufficient_count=0,
        error=None,
        input_fingerprint=run.input_fingerprint,
        ruler_version_id=getattr(run, f"{track}_ruler_version_id"),
        algorithm_version=run.algorithm_version,
        common_stage_id=(
            parent.id if isinstance(parent, DiscoveryCommonStage) else None
        ),
        predecessor_stage_id=(
            parent.id if isinstance(parent, DiscoveryStage) else None
        ),
        predecessor_stage=(
            parent.stage if isinstance(parent, DiscoveryStage) else None
        ),
        failure_kind=None,
        superseded_at=None,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(row)
    return row


def confirm_waiting_layers(
    session: Session,
    run: DiscoveryRun,
    now: datetime,
) -> bool:
    """Seal leftover waiting layers and queue the next rule-only stage."""

    queued = False
    for common in session.scalars(
        select(DiscoveryCommonStage).where(
            DiscoveryCommonStage.run_id == run.id,
            DiscoveryCommonStage.status == "waiting_confirmation",
            DiscoveryCommonStage.superseded_at.is_(None),
        )
    ):
        common.status = "confirmed"
        common.confirmed_at = now
        common.updated_at = now
        has_child = session.scalar(
            select(DiscoveryStage.id).where(
                DiscoveryStage.run_id == run.id,
                DiscoveryStage.common_stage_id == common.id,
            ).limit(1)
        )
        if has_child is None:
            for track in TRACK_ORDER:
                queue_next_track_stage(
                    session,
                    run,
                    track=track,
                    stage="track_gate",
                    parent=common,
                    created_at=now,
                )
            queued = True
    for stage in session.scalars(
        select(DiscoveryStage).where(
            DiscoveryStage.run_id == run.id,
            DiscoveryStage.status == "waiting_confirmation",
            DiscoveryStage.superseded_at.is_(None),
        )
    ):
        _register_competition_publication(session, stage, now)
        stage.status = "confirmed"
        stage.confirmed_at = now
        stage.updated_at = now
        if stage.stage != "track_gate":
            continue
        has_child = session.scalar(
            select(DiscoveryStage.id).where(
                DiscoveryStage.run_id == run.id,
                DiscoveryStage.track == stage.track,
                DiscoveryStage.stage == "competition",
                DiscoveryStage.predecessor_stage_id == stage.id,
            ).limit(1)
        )
        if has_child is None:
            queue_next_track_stage(
                session,
                run,
                track=stage.track,
                stage="competition",
                parent=stage,
                created_at=now,
            )
            queued = True
    return queued


def _register_competition_publication(session: Session, stage: DiscoveryStage, now: datetime) -> None:
    """Freeze the three variants before the first confirmed result is visible."""
    from discovery.builds import CANDIDATE_BUILD_VERSION
    if stage.stage == "competition" and stage.algorithm_version == CANDIDATE_BUILD_VERSION:
        from tracking.cohorts import publish_track_cohorts
        publish_track_cohorts(session, stage_id=stage.id, published_at=now)


class TransientDiscoveryError(RuntimeError):
    """An explicitly retryable error for unchanged frozen inputs."""


class PermanentDiscoveryError(ValueError):
    """A snapshot, data, or business invariant failure."""


class QueueStalledError(RuntimeError):
    """An executor returned without advancing its selected stage."""


class TrackExecutorContractError(PermanentDiscoveryError):
    """The injected track executor violated drain-owned transactions."""


class DiscoveryLockLostError(RuntimeError):
    """The drain no longer owns its exact committed global lock."""


@dataclass(frozen=True)
class StageExecutionOutcome:
    stage_id: int
    status: str
    claimed: bool
    processed: int = 0


@dataclass(frozen=True)
class DrainOutcome:
    run_id: int
    acquired: bool
    executed: int


@dataclass(frozen=True)
class TrackExecutionCandidate:
    code: str
    persisted_verdict: str
    effective_verdict: str
    reason: str | None
    metrics_json: str
    rank: int | None
    override_action: str | None = None
    override_reason: str | None = None
    extra_risk_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrackExecutionContext:
    stage_id: int
    run_id: int
    track: str
    stage: str
    attempt: int
    input_fingerprint: str
    ruler_version_id: int
    algorithm_version: str
    common_stage_id: int | None
    predecessor_stage_id: int | None
    predecessor_stage: str | None
    frozen_inputs: FrozenRunInputs
    ruler_payload: Mapping[str, FrozenValue]
    candidates: tuple[TrackExecutionCandidate, ...]

    @property
    def candidate_codes(self) -> tuple[str, ...]:
        return tuple(candidate.code for candidate in self.candidates)


@dataclass(frozen=True)
class TrackExecutionResult:
    code: str
    verdict: str
    reason: str | None
    metrics_json: str
    rank: int | None


@dataclass(frozen=True)
class TrackExecutionEvidence:
    code: str
    ruler_version_id: int
    rule_version: str
    kind: str
    rule_id: str
    input_fields_json: str
    display_text: str
    as_of: str
    data_completeness: str


@dataclass(frozen=True)
class TrackExecutionOutcome:
    status: str
    results: tuple[TrackExecutionResult, ...] = ()
    evidence: tuple[TrackExecutionEvidence, ...] = ()
    error: str | None = None
    failure_kind: str | None = None


TrackExecutor = Callable[[TrackExecutionContext], TrackExecutionOutcome]
_UNRANKED_COMPETITION_REASONS = frozenset({"industry_unknown"})
_TRACK_CLASSIFIERS = {
    "quality_discount": (quality_discount_ruler, classify_quality_discount),
    "deep_value": (deep_value_ruler, classify_deep_value),
    "garp": (garp_ruler, classify_garp),
}


def _verdict_is_scored(verdict: object) -> bool:
    """A listing-age screen is a decision, not missing market data.

    Companies listed under five years stay insufficient on the card.
    Counting them as unscored hides the rest of the track.
    """

    outcome = getattr(verdict, "verdict", None)
    if outcome != "insufficient":
        return True
    return getattr(verdict, "primary_reason", None) == (
        "listing_history_insufficient"
    )


def _dec_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else None
    except Exception:
        return None


def _competition_metrics(
    company: FrozenCompanyInput,
    raw_metrics: Mapping[str, object],
    extra_flags: Sequence[str] = (),
    *, ruler=None,
) -> dict[str, object]:
    out = dict(raw_metrics)
    candidate = ruler is not None and is_candidate(ruler)
    if candidate:
        company, _ = eligible_legacy_company(company, load_fundamentals(company, ruler))
    policy = ruler.ranking_contract['annual_risk_policy'] if candidate else {}
    def threshold(name, legacy):
        return Decimal(policy[name]) if candidate else Decimal(legacy)
    incomplete_risks = False
    operating = company.fields.get('operating_result')
    if isinstance(operating, Mapping):
        out['operating_result'] = operating
        out['operating_policy'] = company.fields.get('operating_policy')
        out['fundamental_fingerprint'] = company.fields.get('fundamental_fingerprint')
        out['freeze_new_positions'] = operating.get('state') in {'deteriorating', 'unknown'}
    flags = [str(f) for f in (raw_metrics.get("risk_flags") or ()) if f]
    for ef in extra_flags:
        if not ef:
            continue
        if str(ef).startswith("interim_yoy:"):
            text = str(ef).split(":", 1)[1]
            if candidate:
                value = _dec_or_none(text)
                if value is not None:
                    out['_interim_yoy'] = str(value)
                continue
            try:
                out["_interim_yoy"] = float(text)
            except ValueError:
                pass
            continue
        if ef not in flags:
            flags.append(str(ef))
    pb = _dec_or_none(company.fields.get("pb"))
    roe_mean = _dec_or_none(raw_metrics.get("roe_mean"))
    if pb is not None and pb > threshold("pb_min", "8") and roe_mean is not None and roe_mean > threshold("roe_mean_min", "50"):
        flags.append("thin_equity_high_pb")

    hist = company.fields.get("financial_history") or ()
    ann_vals: list[tuple[int, Mapping[str, object]]] = []
    for row in hist:
        if not isinstance(row, Mapping):
            continue
        p = str(row.get("period") or "")
        vals = row.get("values")
        if p.isdigit() and len(p) == 4 and isinstance(vals, Mapping):
            if candidate:
                sources = row.get('field_sources', {})
                provenance = row.get('provenance', {})
                vals = {key: value if isinstance(sources.get(key), Mapping)
                        and sources[key].get('trusted') is True
                        and tuple(provenance.get(key, ())) == ('discovery',) else None
                        for key, value in vals.items()}
            ann_vals.append((int(p), vals))
    ann_vals.sort(key=lambda x: x[0])

    if ann_vals:
        latest_vals = ann_vals[-1][1]
        gw = _dec_or_none(latest_vals.get("goodwill"))
        na = _dec_or_none(latest_vals.get("net_assets"))
        if gw is not None and gw > 0 and na is not None and na > 0 and (gw / na) > threshold("goodwill_to_equity_min", "0.25"):
            flags.append("goodwill_bloat")

        is_fin = raw_metrics.get('ranking_branch') == 'bank' if candidate else (
            raw_metrics.get("branch") == "financial"
            or raw_metrics.get("quality_branch") == "bank"
            or "银行" in (company.industry or "")
            or "保险" in (company.industry or "")
            or "证券" in (company.industry or "")
        )
        if not is_fin:
            l_ni = _dec_or_none(latest_vals.get("net_income"))
            l_ocf = _dec_or_none(latest_vals.get("ocf"))
            if l_ni is not None and l_ni > 0 and l_ocf is not None and (l_ocf / l_ni) < threshold("recent_cash_conversion_max", "0.40"):
                flags.append("recent_ocf_collapse")
            if len(ann_vals) >= 2:
                v0, v1 = ann_vals[-1][1], ann_vals[-2][1]
                ocf0, cap0 = _dec_or_none(v0.get("ocf")), _dec_or_none(v0.get("capex"))
                ocf1, cap1 = _dec_or_none(v1.get("ocf")), _dec_or_none(v1.get("capex"))
                if (
                    ocf0 is not None
                    and cap0 is not None
                    and ocf1 is not None
                    and cap1 is not None
                    and (ocf0 - cap0) < 0
                    and (ocf1 - cap1) < 0
                ):
                    flags.append("consecutive_negative_fcf")
            fcf_5 = [
                (_dec_or_none(v.get("ocf")) or Decimal("0"))
                - (_dec_or_none(v.get("capex")) or Decimal("0"))
                for _, v in ann_vals[-5:]
                if _dec_or_none(v.get("ocf")) is not None and _dec_or_none(v.get("capex")) is not None
            ]
            if len(fcf_5) >= 3 and sum(fcf_5, Decimal("0")) < 0:
                flags.append("negative_5y_fcf")

        if len(ann_vals) >= 2:
            dni_cur = _dec_or_none(ann_vals[-1][1].get("deducted_net_income"))
            dni_prev = _dec_or_none(ann_vals[-2][1].get("deducted_net_income"))
            if dni_cur is not None and dni_prev is not None and dni_prev > 0:
                if (dni_cur - dni_prev) / dni_prev <= threshold("deducted_drop_max", "-0.15"):
                    flags.append("annual_deducted_drop")

        roes = [
            _dec_or_none(v.get("roe"))
            for _, v in ann_vals[-4:]
            if _dec_or_none(v.get("roe")) is not None
        ]
        if len(roes) == 4 and roes[0] > roes[1] > roes[2] > roes[3] and (roes[0] - roes[3]) >= threshold("roe_decline_min", "4"):  # type: ignore[operator]
            flags.append("roe_step_down_3y")

    out['risk_flags'] = tuple(dict.fromkeys(flags))
    if candidate:
        # An unavailable annual risk input cannot certify an empty risk set.
        required = ('goodwill', 'net_assets', 'deducted_net_income', 'roe')
        if out.get('ranking_branch') != 'bank':
            required += ('net_income', 'ocf', 'capex')
        incomplete_risks = (len(ann_vals) < 5 or any(vals.get(field) is None
                            for _, vals in ann_vals[-5:] for field in required))
        formal = [flag for flag in out['risk_flags'] if not flag.startswith(('collection_', 'restricted_cash_'))]
        if incomplete_risks:
            formal.append('annual_risk_evidence_missing')
        out['verified_risk_types'] = tuple(sorted(set(formal)))
        out['operating_state'] = operating.get('state') if isinstance(operating, Mapping) else 'unknown'
    return out


def builtin_track_executor(
    context: TrackExecutionContext,
) -> TrackExecutionOutcome:
    """Dispatch production stages implemented by this build."""

    implementation = _TRACK_CLASSIFIERS.get(context.track)
    if implementation is None or context.stage not in {
        "track_gate",
        "competition",
    }:
        return TrackExecutionOutcome(
            status="ruler_not_ready",
            error="当前构建尚未实现该轨道阶段",
        )
    load_ruler, classifier = implementation
    try:
        ruler = load_ruler(context.ruler_payload)
    except (TypeError, ValueError):
        return TrackExecutionOutcome(
            status="ruler_not_ready",
            error=f"冻结的{context.track}尺子不完整或不兼容",
        )
    if context.stage == "competition":
        companies = {
            company.code: company for company in context.frozen_inputs.companies
        }
        if (
            len(companies) != len(context.frozen_inputs.companies)
            or len(context.candidate_codes) != len(set(context.candidate_codes))
            or not set(context.candidate_codes) <= set(companies)
        ):
            raise TrackExecutorContractError(
                "competition candidates do not match frozen companies"
            )
        metrics = {
            candidate.code: _competition_metrics(
                companies[candidate.code], _plain_metrics(candidate.metrics_json),
                candidate.extra_risk_flags, ruler=ruler)
            for candidate in context.candidates
        }
        entrants = tuple(CompetitionCandidate(
            code=candidate.code, industry=companies[candidate.code].industry,
            metrics=rank_inputs(metrics[candidate.code], ruler) if is_candidate(ruler) else metrics[candidate.code])
            for candidate in context.candidates)
        if is_candidate(ruler):
            ranked = rank_candidates(entrants, ruler.ranking_contract)
            audits = ranking_audit(entrants, ruler.ranking_contract)
            for code, audit in audits.items():
                metrics[code]['ranking_audit'] = audit
                metrics[code]['rank_score'] = audit['rank_score']
        else:
            ranked = rank_borda_competition(entrants, ruler.order)
        cards = {
            row.code: build_evidence_card(
                row.code,
                metrics[row.code],
                context.ruler_payload,
                source_as_of={
                    key: companies[row.code].as_of[key]
                    for key in ("security", "financial", "valuation")
                },
            )
            for row in ranked
        }
        return TrackExecutionOutcome(
            status="waiting_confirmation",
            results=tuple(
                TrackExecutionResult(
                    code=row.code,
                    verdict=(
                        "insufficient"
                        if row.rank is not None
                        and cards[row.code].verdict == "insufficient"
                        else row.verdict
                    ),
                    reason=(
                        cards[row.code].reason
                        if row.rank is not None
                        and cards[row.code].verdict == "insufficient"
                        else row.reason
                    ),
                    metrics_json=canonical_json(metrics[row.code]),
                    rank=row.rank,
                )
                for row in ranked
            ),
            evidence=tuple(
                TrackExecutionEvidence(
                    code=row.code,
                    ruler_version_id=context.ruler_version_id,
                    rule_version=item.rule_version,
                    kind=item.kind,
                    rule_id=item.rule_id,
                    input_fields_json=canonical_json(item.input_fields),
                    display_text=item.display_text,
                    as_of=item.as_of,
                    data_completeness=item.data_completeness,
                )
                for row in ranked
                for item in cards[row.code].items
            ),
        )
    companies = {
        company.code: company for company in context.frozen_inputs.companies
    }
    if (
        len(companies) != len(context.frozen_inputs.companies)
        or len(context.candidate_codes) != len(set(context.candidate_codes))
        or not set(context.candidate_codes) <= set(companies)
    ):
        raise TrackExecutorContractError(
            "track gate candidates do not match frozen companies"
        )
    verdicts = tuple(
        classifier(companies[candidate.code], ruler)
        for candidate in context.candidates
    )
    complete = sum(_verdict_is_scored(verdict) for verdict in verdicts)
    coverage = bounded_ratio(complete, len(verdicts)) if verdicts else Decimal("1")
    if coverage is None:
        raise TrackExecutorContractError("quality coverage ratio is unavailable")
    required = Decimal(ruler.thresholds["readiness_min"])
    if coverage < required:
        return TrackExecutionOutcome(
            status="data_not_ready",
            error=(
                f"{context.track} coverage "
                f"{decimal_string(coverage)} is below approved "
                f"{ruler.thresholds['readiness_min']}"
            ),
        )
    return TrackExecutionOutcome(
        status="waiting_confirmation",
        results=tuple(
            TrackExecutionResult(
                code=verdict.code,
                verdict=verdict.verdict,
                reason=verdict.primary_reason,
                metrics_json=canonical_json(verdict.metrics),
                rank=None,
            )
            for verdict in verdicts
        ),
    )


def _now(now_provider: NowProvider = utc_now) -> datetime:
    return aware_utc_now(now_provider)


def _run_is_mutable(
    run: DiscoveryRun | None,
    now_provider: NowProvider,
) -> bool:
    return (
        run is not None
        and run.current is True
        and run.status != "abandoned"
        and run.week_key == china_week_key(now_provider=now_provider)
    )


def _safe_error(error: BaseException) -> str:
    message = str(error).strip()
    return (message or type(error).__name__)[:2000]


def _failure_kind(error: BaseException) -> str:
    if isinstance(
        error,
        (
            TransientDiscoveryError,
            InterruptedError,
            TimeoutError,
            OperationalError,
        ),
    ):
        return "transient"
    return "permanent"


def _verify_run_lock(session: Session, run_id: int) -> None:
    if session.info.get("discovery_lock_run_id") != run_id:
        return
    lock = session.scalar(
        select(JobLock)
        .where(JobLock.id == 1)
        .execution_options(populate_existing=True)
    )
    if (
        lock is None
        or lock.job_id != run_id
        or lock.job_type != JOB_TYPE_DISCOVERY
        or lock.owner_token != session.info.get("discovery_lock_owner_token")
    ):
        raise DiscoveryLockLostError("discovery run lock ownership was lost")


def _commit_if_current(
    session: Session,
    run_id: int,
    now_provider: NowProvider,
) -> bool:
    if not _run_is_mutable(
        session.get(DiscoveryRun, run_id),
        now_provider,
    ):
        session.rollback()
        return False
    _verify_run_lock(session, run_id)
    # Flush owns SQLite's writer transaction before authority is re-read. A
    # rollback during a long callback must win before any results/cohorts commit.
    session.flush()
    if not execution_authorized(session, session.get(DiscoveryRun, run_id)):
        session.rollback()
        return False
    session.commit()
    return True


def _persist_failure(
    session: Session,
    stage_id: int,
    error: BaseException,
    now_provider: NowProvider,
) -> StageExecutionOutcome:
    session.rollback()
    stage = session.get(DiscoveryCommonStage, stage_id)
    if stage is None:
        return StageExecutionOutcome(stage_id, "not_found", False)
    if not _run_is_mutable(
        session.get(DiscoveryRun, stage.run_id),
        now_provider,
    ):
        return StageExecutionOutcome(stage_id, "historical", False)
    if stage.status != "running" or stage.superseded_at is not None:
        return StageExecutionOutcome(stage_id, stage.status, False)
    stage.status = "failed"
    stage.error = _safe_error(error)
    stage.failure_kind = _failure_kind(error)
    stage.updated_at = _now(now_provider)
    if not _commit_if_current(
        session,
        stage.run_id,
        now_provider,
    ):
        return StageExecutionOutcome(stage_id, "historical", False)
    return StageExecutionOutcome(stage_id, "failed", True)


def execute_common_stage(
    session: Session,
    stage_id: int,
    *,
    now_provider: NowProvider = utc_now,
) -> StageExecutionOutcome:
    """Atomically claim and materialize one common-stage attempt."""

    if type(stage_id) is not int or stage_id < 1:
        raise ValueError("stage_id must be a positive integer")
    observed = session.get(DiscoveryCommonStage, stage_id)
    if observed is None:
        return StageExecutionOutcome(stage_id, "not_found", False)
    if not _run_is_mutable(
        session.get(DiscoveryRun, observed.run_id),
        now_provider,
    ):
        return StageExecutionOutcome(stage_id, "historical", False)
    claimed_at = _now(now_provider)
    claim = session.execute(
        update(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.id == stage_id,
            DiscoveryCommonStage.status == "queued",
            DiscoveryCommonStage.superseded_at.is_(None),
        )
        .values(
            status="running",
            error=None,
            failure_kind=None,
            updated_at=claimed_at,
        )
    )
    if not _commit_if_current(
        session,
        observed.run_id,
        now_provider,
    ):
        return StageExecutionOutcome(stage_id, "historical", False)
    if int(claim.rowcount or 0) != 1:
        session.expire_all()
        stage = session.get(DiscoveryCommonStage, stage_id)
        return StageExecutionOutcome(
            stage_id,
            "not_found" if stage is None else stage.status,
            False,
        )

    try:
        stage = session.get(DiscoveryCommonStage, stage_id)
        if stage is None:
            raise PermanentDiscoveryError("claimed common stage not found")
        if stage.superseded_at is not None:
            raise PermanentDiscoveryError("common stage is superseded")
        run = session.get(DiscoveryRun, stage.run_id)
        if run is None:
            raise PermanentDiscoveryError("discovery run not found")
        if not _run_is_mutable(run, now_provider):
            return StageExecutionOutcome(stage_id, "historical", True)
        if run.status not in {"queued", "running", "waiting_confirmation"}:
            raise PermanentDiscoveryError(
                f"discovery run cannot execute from status {run.status}"
            )

        frozen = load_frozen_inputs(session, run.id)
        if (
            stage.input_fingerprint != run.input_fingerprint
            or stage.input_fingerprint != frozen.input_fingerprint
        ):
            raise PermanentDiscoveryError(
                "common stage input fingerprint mismatch"
            )
        if (
            stage.algorithm_version != run.algorithm_version
            or stage.algorithm_version != frozen.algorithm_version
        ):
            raise PermanentDiscoveryError(
                "common stage algorithm version mismatch"
            )
        if not _is_build_available(
            stage.algorithm_version,
            AVAILABLE_BUILDS,
        ):
            raise PermanentDiscoveryError(
                "algorithm_unavailable: saved build is unavailable"
            )
        existing = session.scalar(
            select(func.count())
            .select_from(DiscoveryCommonResult)
            .where(
                DiscoveryCommonResult.run_id == stage.run_id,
                DiscoveryCommonResult.attempt == stage.attempt,
            )
        )
        if existing:
            raise PermanentDiscoveryError(
                "common stage already has historical results"
            )

        verdicts: list[CommonVerdict] = [
            classify_common(item, frozen.trading_dates)
            for item in frozen.companies
        ]
        session.rollback()
        stage = session.get(DiscoveryCommonStage, stage_id)
        if stage is None:
            raise PermanentDiscoveryError("claimed common stage not found")
        run = session.get(DiscoveryRun, stage.run_id)
        if not _run_is_mutable(run, now_provider):
            return StageExecutionOutcome(stage_id, "historical", True)
        if stage.status != "running" or stage.superseded_at is not None:
            raise PermanentDiscoveryError(
                "common stage changed during execution"
            )
        completed_at = _now(now_provider)
        session.add_all(
            DiscoveryCommonResult(
                run_id=stage.run_id,
                attempt=stage.attempt,
                code=item.code,
                verdict=item.verdict,
                reason=item.reason,
                metrics_json=canonical_json(item.metrics),
                created_at=completed_at,
            )
            for item in verdicts
        )
        counts = {
            verdict: sum(item.verdict == verdict for item in verdicts)
            for verdict in ("pass", "drop", "insufficient")
        }
        stage.total_count = len(verdicts)
        stage.pass_count = counts["pass"]
        stage.drop_count = counts["drop"]
        stage.insufficient_count = counts["insufficient"]
        stage.failure_kind = None
        stage.updated_at = completed_at
        if not common_ready(counts["pass"], counts["drop"], len(verdicts)):
            stage.status = "data_not_ready"
            stage.error = (
                f"共同排雷有结论的公司 {counts['pass'] + counts['drop']}/"
                f"{len(verdicts)}，低于 {COMMON_READINESS_MIN}"
            )
            if not _commit_if_current(session, stage.run_id, now_provider):
                return StageExecutionOutcome(stage_id, "historical", True)
            return StageExecutionOutcome(
                stage_id, "data_not_ready", True, processed=len(verdicts)
            )
        stage.status = "confirmed"
        stage.confirmed_at = completed_at
        stage.error = None
        session.flush()
        for track in TRACK_ORDER:
            queue_next_track_stage(
                session,
                run,
                track=track,
                stage="track_gate",
                parent=stage,
                created_at=completed_at,
            )
        if not _commit_if_current(
            session,
            stage.run_id,
            now_provider,
        ):
            return StageExecutionOutcome(stage_id, "historical", True)
        return StageExecutionOutcome(
            stage_id,
            "confirmed",
            True,
            processed=len(verdicts),
        )
    except Exception as error:
        return _persist_failure(
            session,
            stage_id,
            error,
            now_provider,
        )


def _next_queued_stage(
    session: Session, run_id: int
) -> tuple[str, int] | None:
    common = session.scalars(
        select(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.run_id == run_id,
            DiscoveryCommonStage.status == "queued",
            DiscoveryCommonStage.superseded_at.is_(None),
        )
        .order_by(
            DiscoveryCommonStage.created_at,
            DiscoveryCommonStage.id,
        )
        .limit(1)
    ).first()
    track = session.scalars(
        select(DiscoveryStage)
        .where(
            DiscoveryStage.run_id == run_id,
            DiscoveryStage.status == "queued",
            DiscoveryStage.superseded_at.is_(None),
        )
        .order_by(DiscoveryStage.created_at, DiscoveryStage.id)
        .limit(1)
    ).first()
    candidates = [
        (row.created_at, row.id, kind)
        for kind, row in (("common", common), ("track", track))
        if row is not None
    ]
    if not candidates:
        return None
    _, stage_id, kind = min(candidates)
    return kind, stage_id


def _persist_track_failure(
    session: Session,
    stage_id: int,
    error: BaseException,
    now_provider: NowProvider,
) -> None:
    """Best-effort failure persistence after discarding callback writes."""

    try:
        session.rollback()
    except BaseException:
        return
    try:
        stage = session.get(DiscoveryStage, stage_id)
        if (
            stage is not None
            and stage.superseded_at is None
            and stage.status in {"queued", "running"}
            and _run_is_mutable(
                session.get(DiscoveryRun, stage.run_id),
                now_provider,
            )
        ):
            stage.status = "failed"
            stage.error = _safe_error(error)
            stage.failure_kind = _failure_kind(error)
            stage.updated_at = _now(now_provider)
            if not _commit_if_current(
                session,
                stage.run_id,
                now_provider,
            ):
                return
    except BaseException:
        try:
            session.rollback()
        except BaseException:
            pass


def _plain_metrics(text: object) -> dict[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def validate(value: object) -> None:
        if value is None or type(value) in {bool, int, str}:
            return
        if type(value) is float:
            if isfinite(value):
                return
            raise ValueError("non-finite metric")
        if type(value) is list:
            for item in value:
                validate(item)
            return
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("non-string metric key")
            for item in value.values():
                validate(item)
            return
        raise ValueError("unsupported metric value")

    if type(text) is not str:
        raise ValueError("metrics JSON must be text")
    value = json.loads(text, parse_constant=reject_constant)
    if type(value) is not dict:
        raise ValueError("metrics JSON must be an object")
    validate(value)
    if canonical_json(value) != text:
        raise ValueError("metrics JSON must be canonical")
    return value


def _track_candidates(
    session: Session, stage: DiscoveryStage
) -> tuple[TrackExecutionCandidate, ...]:
    if stage.stage == "track_gate":
        parent = session.get(DiscoveryCommonStage, stage.common_stage_id)
        if parent is None:
            return ()
        rows = session.scalars(
            select(DiscoveryCommonResult)
            .where(
                DiscoveryCommonResult.run_id == stage.run_id,
                DiscoveryCommonResult.attempt == parent.attempt,
                DiscoveryCommonResult.verdict == "pass",
            )
            .order_by(DiscoveryCommonResult.code, DiscoveryCommonResult.id)
        ).all()
        return tuple(
            TrackExecutionCandidate(
                code=row.code,
                persisted_verdict=row.verdict,
                effective_verdict=row.verdict,
                reason=row.reason,
                metrics_json=row.metrics_json,
                rank=None,
            )
            for row in rows
        )
    if stage.stage == "adjudication":
        return ()
    if stage.predecessor_stage_id is None:
        return ()
    parent = session.get(DiscoveryStage, stage.predecessor_stage_id)
    if parent is None:
        return ()
    overrides = {
        row.code: row
        for row in session.scalars(
            select(DiscoveryOverride).where(
                DiscoveryOverride.run_id == stage.run_id,
                DiscoveryOverride.track == stage.track,
                DiscoveryOverride.stage == parent.stage,
                DiscoveryOverride.attempt == parent.attempt,
            )
        )
    }
    passed_rows = []
    for row in session.scalars(
        select(DiscoveryResult)
        .where(
            DiscoveryResult.run_id == stage.run_id,
            DiscoveryResult.track == stage.track,
            DiscoveryResult.stage == parent.stage,
            DiscoveryResult.attempt == parent.attempt,
        )
        .order_by(DiscoveryResult.code, DiscoveryResult.id)
    ):
        override = overrides.get(row.code)
        effective = (
            row.verdict
            if override is None
            else ("pass" if override.action == "forced_pass" else "drop")
        )
        if effective != "pass":
            continue
        passed_rows.append((row, override, effective))

    interim_flags_by_code: dict[str, tuple[str, ...]] = {}
    if stage.stage == "competition" and passed_rows:
        frozen = load_frozen_inputs(session, stage.run_id)
        for company in frozen.companies:
            result = company.fields.get('operating_result')
            if isinstance(result, Mapping):
                if result.get('state') == 'deteriorating':
                    period = str(result.get('period') or '')
                    flag = 'annual_operating_deterioration' if len(period) == 4 else 'interim_deterioration'
                    interim_flags_by_code[company.code] = (flag,)
                elif result.get('state') == 'unknown':
                    interim_flags_by_code[company.code] = ('operating_unknown',)

    candidates = []
    for row, override, effective in passed_rows:
        candidates.append(
            TrackExecutionCandidate(
                code=row.code,
                persisted_verdict=row.verdict,
                effective_verdict=effective,
                reason=row.reason,
                metrics_json=row.metrics_json,
                rank=row.rank,
                override_action=(
                    None if override is None else override.action
                ),
                override_reason=(
                    None if override is None else override.reason
                ),
                extra_risk_flags=interim_flags_by_code.get(row.code, ()),
            )
        )
    return tuple(candidates)


def _validate_track_outcome(
    context: TrackExecutionContext,
    outcome: object,
) -> TrackExecutionOutcome:
    candidate_codes = context.candidate_codes
    snapshot_codes = {
        company.code for company in context.frozen_inputs.companies
    }
    if (
        len(candidate_codes) != len(set(candidate_codes))
        or not set(candidate_codes) <= snapshot_codes
        or any(
            candidate.effective_verdict != "pass"
            or candidate.persisted_verdict not in VERDICTS
            or (
                candidate.override_action is not None
                and candidate.override_action
                not in {"forced_pass", "forced_drop"}
            )
            or (
                candidate.reason is not None
                and type(candidate.reason) is not str
            )
            or (
                candidate.rank is not None
                and (
                    type(candidate.rank) is not int
                    or candidate.rank < 1
                )
            )
            for candidate in context.candidates
        )
    ):
        raise TrackExecutorContractError(
            "track execution context has invalid candidates"
        )
    for candidate in context.candidates:
        try:
            _plain_metrics(candidate.metrics_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise TrackExecutorContractError(
                f"track candidate metrics are invalid: {error}"
            ) from error
    if type(outcome) is not TrackExecutionOutcome:
        raise TrackExecutorContractError(
            "track executor must return TrackExecutionOutcome"
        )
    if outcome.status not in {
        "waiting_confirmation",
        "failed",
        "data_not_ready",
        "ruler_not_ready",
    }:
        raise TrackExecutorContractError(
            "track executor returned an invalid status"
        )
    if type(outcome.results) is not tuple or type(outcome.evidence) is not tuple:
        raise TrackExecutorContractError(
            "track executor artifacts must be immutable tuples"
        )
    if outcome.status == "failed":
        if (
            outcome.failure_kind not in {"transient", "permanent"}
            or type(outcome.error) is not str
            or not outcome.error.strip()
        ):
            raise TrackExecutorContractError(
                "failed track outcome requires failure kind and error"
            )
    elif outcome.failure_kind is not None:
        raise TrackExecutorContractError(
            "non-failed track outcome cannot carry failure kind"
        )
    if outcome.error is not None and type(outcome.error) is not str:
        raise TrackExecutorContractError("track outcome error must be text")
    if (
        outcome.status == "waiting_confirmation"
        and outcome.error is not None
    ):
        raise TrackExecutorContractError(
            "waiting track outcome cannot carry an error"
        )
    if outcome.status != "waiting_confirmation" and (
        outcome.results or outcome.evidence
    ):
        raise TrackExecutorContractError(
            "non-waiting track outcome cannot carry artifacts"
        )

    allowed_codes = set(candidate_codes)
    codes = []
    for row in outcome.results:
        if type(row) is not TrackExecutionResult:
            raise TrackExecutorContractError("invalid track result DTO")
        if type(row.code) is not str or row.code not in allowed_codes:
            raise TrackExecutorContractError(
                "track result code is outside the exact candidate set"
            )
        if row.verdict not in VERDICTS:
            raise TrackExecutorContractError(
                "track result has an invalid verdict"
            )
        if row.reason is not None and type(row.reason) is not str:
            raise TrackExecutorContractError(
                "track result reason must be text or null"
            )
        try:
            _plain_metrics(row.metrics_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise TrackExecutorContractError(
                f"track result metrics are invalid: {error}"
            ) from error
        if row.rank is not None and (
            type(row.rank) is not int or row.rank < 1
        ):
            raise TrackExecutorContractError(
                "track result rank must be positive or null"
            )
        codes.append(row.code)
    if len(codes) != len(set(codes)):
        raise TrackExecutorContractError("track result codes must be unique")
    if (
        outcome.status == "waiting_confirmation"
        and set(codes) != allowed_codes
    ):
        raise TrackExecutorContractError(
            "waiting track results must match the exact candidate set"
        )
    ranks = [row.rank for row in outcome.results if row.rank is not None]
    unranked = [row for row in outcome.results if row.rank is None]
    invalid_competition_rank = context.stage == "competition" and (
        any(
            row.verdict != "insufficient"
            or row.reason not in _UNRANKED_COMPETITION_REASONS
            for row in unranked
        )
        or sorted(ranks) != list(range(1, len(ranks) + 1))
    )
    invalid_other_rank = (
        context.stage != "competition"
        and bool(ranks)
        and (
            len(ranks) != len(outcome.results)
            or sorted(ranks) != list(range(1, len(ranks) + 1))
        )
    )
    if invalid_competition_rank or invalid_other_rank:
        raise TrackExecutorContractError(
            "unranked competition results require an allowed "
            "pre-competition insufficiency; all other ranks must be "
            "unique and contiguous"
        )

    if context.stage != "competition":
        if outcome.evidence:
            raise TrackExecutorContractError(
                "only competition outcomes may carry evidence"
            )
        return outcome

    evidence_by_code: dict[str, list[PersistedEvidenceItem]] = {}
    for row in outcome.evidence:
        if (
            type(row) is not TrackExecutionEvidence
            or row.code not in allowed_codes
        ):
            raise TrackExecutorContractError(
                "invalid competition evidence DTO"
            )
        try:
            input_fields = _plain_metrics(row.input_fields_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise TrackExecutorContractError(
                f"competition evidence input is invalid: {error}"
            ) from error
        evidence_by_code.setdefault(row.code, []).append(
            PersistedEvidenceItem(
                code=row.code,
                ruler_version_id=row.ruler_version_id,
                rule_version=row.rule_version,
                kind=row.kind,
                rule_id=row.rule_id,
                input_fields=input_fields,
                display_text=row.display_text,
                as_of=row.as_of,
                data_completeness=row.data_completeness,
            )
        )

    companies = {
        company.code: company for company in context.frozen_inputs.companies
    }
    for row in outcome.results:
        company = companies.get(row.code)
        try:
            metrics = _plain_metrics(row.metrics_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics = {}
        if company is None or not verify_canonical_evidence_card(
            track=context.track,
            ruler=context.ruler_payload,
            ruler_version_id=context.ruler_version_id,
            code=row.code,
            metrics=metrics,
            verdict=row.verdict,
            rank=row.rank,
            reason=row.reason,
            company_code=company.code if company is not None else "",
            company_industry=company.industry if company is not None else None,
            source_as_of=(
                {
                    key: company.as_of.get(key)
                    for key in ("security", "financial", "valuation")
                }
                if company is not None
                else {}
            ),
            items=evidence_by_code.get(row.code, []),
        ):
            raise TrackExecutorContractError(
                "competition evidence card is not canonical"
            )
    return outcome


def _execute_track_stage(
    session: Session,
    stage_id: int,
    track_executor: TrackExecutor | None,
    now_provider: NowProvider,
    preloaded: tuple[tuple[TrackExecutionCandidate, ...], FrozenRunInputs] | None = None,
) -> bool:
    """Run a pure callback, then validate and persist its immutable outcome."""

    stage = session.get(DiscoveryStage, stage_id)
    if stage is None:
        raise PermanentDiscoveryError("track stage not found")
    if not _run_is_mutable(
        session.get(DiscoveryRun, stage.run_id),
        now_provider,
    ):
        return False
    if stage.stage == "track_gate":
        parent = (
            session.get(DiscoveryCommonStage, stage.common_stage_id)
            if stage.common_stage_id is not None
            else None
        )
        valid_parent = (
            parent is not None
            and parent.run_id == stage.run_id
            and parent.status == "confirmed"
            and parent.superseded_at is None
        )
    else:
        parent = (
            session.get(DiscoveryStage, stage.predecessor_stage_id)
            if stage.predecessor_stage_id is not None
            else None
        )
        expected = (
            "track_gate" if stage.stage == "competition" else "competition"
        )
        valid_parent = (
            parent is not None
            and parent.run_id == stage.run_id
            and parent.track == stage.track
            and parent.stage == expected
            and stage.predecessor_stage == expected
            and parent.status == "confirmed"
            and parent.superseded_at is None
        )
    if not valid_parent:
        code = (
            "lineage_unavailable"
            if stage.common_stage_id is None
            and stage.predecessor_stage_id is None
            else "invalid_predecessor"
        )
        raise PermanentDiscoveryError(f"{code}: exact stage lineage is unavailable")
    if preloaded is None:
        candidates = _track_candidates(session, stage)
        frozen = load_frozen_inputs(
            session,
            stage.run_id,
            codes=(candidate.code for candidate in candidates),
        )
    else:
        candidates, frozen = preloaded
    frozen_ruler = frozen.ruler_payloads.get(stage.track)
    if (
        not _is_build_available(stage.algorithm_version, AVAILABLE_BUILDS)
        or stage.input_fingerprint != frozen.input_fingerprint
        or stage.algorithm_version != frozen.algorithm_version
        or stage.ruler_version_id is None
        or not isinstance(frozen_ruler, Mapping)
        or frozen_ruler.get("id") != stage.ruler_version_id
    ):
        raise PermanentDiscoveryError(
            "algorithm_unavailable: frozen ruler payload is unavailable"
        )
    existing = session.scalar(
        select(func.count())
        .select_from(DiscoveryResult)
        .where(
            DiscoveryResult.run_id == stage.run_id,
            DiscoveryResult.track == stage.track,
            DiscoveryResult.stage == stage.stage,
            DiscoveryResult.attempt == stage.attempt,
        )
    )
    if existing:
        raise PermanentDiscoveryError(
            "track stage already has historical results"
        )
    existing_evidence = session.scalar(
        select(func.count())
        .select_from(DiscoveryEvidence)
        .where(
            DiscoveryEvidence.competition_stage_id == stage.id,
            DiscoveryEvidence.run_id == stage.run_id,
            DiscoveryEvidence.track == stage.track,
            DiscoveryEvidence.competition_attempt == stage.attempt,
        )
    )
    if existing_evidence:
        raise PermanentDiscoveryError(
            "track stage already has historical evidence"
        )
    session.rollback()
    stage = session.get(DiscoveryStage, stage_id)
    if stage is None:
        raise PermanentDiscoveryError("track stage not found")
    if not _run_is_mutable(
        session.get(DiscoveryRun, stage.run_id),
        now_provider,
    ):
        return False
    claimed = session.execute(
        update(DiscoveryStage)
        .where(
            DiscoveryStage.id == stage.id,
            DiscoveryStage.status == "queued",
            DiscoveryStage.superseded_at.is_(None),
        )
        .values(
            status="running",
            error=None,
            failure_kind=None,
            updated_at=_now(now_provider),
        )
    )
    if not _commit_if_current(
        session,
        stage.run_id,
        now_provider,
    ):
        return False
    if int(claimed.rowcount or 0) != 1:
        raise QueueStalledError(f"track stage {stage_id} was not claimable")
    session.expire_all()
    stage = session.get(DiscoveryStage, stage_id)
    if stage is None:
        raise QueueStalledError(f"track stage {stage_id} disappeared")
    identity = (
        stage.id,
        stage.run_id,
        stage.track,
        stage.stage,
        stage.attempt,
        stage.input_fingerprint,
        stage.ruler_version_id,
        stage.algorithm_version,
        stage.common_stage_id,
        stage.predecessor_stage_id,
        stage.predecessor_stage,
        stage.retry_of_stage_id,
        stage.reopen_of_stage_id,
    )
    try:
        ruler_payload = frozen_ruler.get("payload")
        if not isinstance(ruler_payload, Mapping):
            raise PermanentDiscoveryError(
                "algorithm_unavailable: frozen ruler payload is unavailable"
            )
        context = TrackExecutionContext(
            stage_id=stage.id,
            run_id=stage.run_id,
            track=stage.track,
            stage=stage.stage,
            attempt=stage.attempt,
            input_fingerprint=stage.input_fingerprint,
            ruler_version_id=stage.ruler_version_id,
            algorithm_version=stage.algorithm_version,
            common_stage_id=stage.common_stage_id,
            predecessor_stage_id=stage.predecessor_stage_id,
            predecessor_stage=stage.predecessor_stage,
            frozen_inputs=frozen,
            ruler_payload=ruler_payload,
            candidates=candidates,
        )
        session.rollback()
        outcome = (
            TrackExecutionOutcome(status="waiting_confirmation")
            if context.stage == "adjudication"
            else (
                track_executor(context)
                if track_executor is not None
                else TrackExecutionOutcome(status="ruler_not_ready")
            )
        )
        outcome = _validate_track_outcome(context, outcome)

        session.expire_all()
        stage = session.get(DiscoveryStage, stage_id)
        if stage is None or stage.superseded_at is not None:
            raise QueueStalledError(
                f"track stage {stage_id} disappeared or was superseded"
            )
        if not _run_is_mutable(
            session.get(DiscoveryRun, stage.run_id),
            now_provider,
        ):
            session.rollback()
            return False
        current_identity = (
            stage.id,
            stage.run_id,
            stage.track,
            stage.stage,
            stage.attempt,
            stage.input_fingerprint,
            stage.ruler_version_id,
            stage.algorithm_version,
            stage.common_stage_id,
            stage.predecessor_stage_id,
            stage.predecessor_stage,
            stage.retry_of_stage_id,
            stage.reopen_of_stage_id,
        )
        if current_identity != identity or stage.status != "running":
            raise TrackExecutorContractError(
                "track stage identity or status changed during execution"
            )
        if session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(
                DiscoveryResult.run_id == stage.run_id,
                DiscoveryResult.track == stage.track,
                DiscoveryResult.stage == stage.stage,
                DiscoveryResult.attempt == stage.attempt,
            )
        ):
            raise TrackExecutorContractError(
                "track stage gained historical results during execution"
            )
        completed_at = _now(now_provider)
        session.add_all(
            DiscoveryResult(
                run_id=stage.run_id,
                track=stage.track,
                stage=stage.stage,
                attempt=stage.attempt,
                code=row.code,
                verdict=row.verdict,
                reason=row.reason,
                metrics_json=row.metrics_json,
                rank=row.rank,
                created_at=completed_at,
            )
            for row in outcome.results
        )
        session.add_all(
            DiscoveryEvidence(
                run_id=stage.run_id,
                track=stage.track,
                competition_attempt=stage.attempt,
                competition_stage_id=stage.id,
                stage="competition",
                code=row.code,
                ruler_version_id=stage.ruler_version_id,
                rule_version=row.rule_version,
                kind=row.kind,
                rule_id=row.rule_id,
                input_fields_json=row.input_fields_json,
                display_text=row.display_text,
                as_of=row.as_of,
                data_completeness=row.data_completeness,
                created_at=completed_at,
            )
            for row in outcome.evidence
        )
        counts = {
            verdict: sum(row.verdict == verdict for row in outcome.results)
            for verdict in VERDICTS
        }
        # Discovery is rule-only: a finished layer is final and the next one
        # queues in the same commit. Competition is the last layer.
        finished = outcome.status == "waiting_confirmation"
        if finished:
            _register_competition_publication(session, stage, completed_at)
        stage.status = "confirmed" if finished else outcome.status
        if finished:
            stage.confirmed_at = completed_at
            if stage.stage == "track_gate":
                queue_next_track_stage(
                    session,
                    session.get(DiscoveryRun, stage.run_id),
                    track=stage.track,
                    stage="competition",
                    parent=stage,
                    created_at=completed_at,
                )
        stage.total_count = len(outcome.results)
        stage.pass_count = counts["pass"]
        stage.drop_count = counts["drop"]
        stage.insufficient_count = counts["insufficient"]
        stage.error = (
            None
            if outcome.error is None
            else _safe_error(ValueError(outcome.error))
        )
        stage.failure_kind = outcome.failure_kind
        stage.updated_at = completed_at
        if not _commit_if_current(
            session,
            stage.run_id,
            now_provider,
        ):
            return False
        return True
    except BaseException as error:
        _persist_track_failure(
            session,
            stage_id,
            error,
            now_provider,
        )
        raise


def _release_run_lock(
    session: Session, run_id: int, owner_token: str
) -> None:
    """Release an exact lease with one bounded fresh-session retry."""

    original = sys.exception()
    cleanup_error: BaseException | None = None
    for attempt in range(2):
        if attempt == 0:
            target = session
        else:
            try:
                target = Session(bind=session.get_bind())
            except BaseException as error:
                cleanup_error = error
                break
        try:
            target.rollback()
            release(
                target,
                run_id,
                JOB_TYPE_DISCOVERY,
                owner_token=owner_token,
            )
            target.commit()
            return
        except BaseException as error:
            cleanup_error = error
            try:
                target.rollback()
            except BaseException:
                pass
        finally:
            if target is not session:
                try:
                    target.close()
                except BaseException:
                    pass
    if original is None and cleanup_error is not None:
        raise cleanup_error
    if original is not None and cleanup_error is not None:
        try:
            original.add_note(
                f"exact discovery lease cleanup failed: {cleanup_error}"
            )
        except (AttributeError, TypeError):
            pass


def _sync_run_status_while_locked(
    session: Session,
    run_id: int,
    now_provider: NowProvider,
) -> None:
    session.rollback()
    _verify_run_lock(session, run_id)
    run = session.get(DiscoveryRun, run_id)
    if not _run_is_mutable(run, now_provider):
        return
    common = session.scalars(
        select(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.run_id == run_id,
            DiscoveryCommonStage.superseded_at.is_(None),
        )
        .order_by(
            DiscoveryCommonStage.attempt.desc(),
            DiscoveryCommonStage.id.desc(),
        )
        .limit(1)
    ).first()
    stage_order = {"track_gate": 0, "competition": 1, "adjudication": 2}
    stages = session.scalars(
        select(DiscoveryStage).where(
            DiscoveryStage.run_id == run_id,
            DiscoveryStage.superseded_at.is_(None),
        )
    ).all()
    track_statuses: dict[str, str] = {}
    for track in TRACKS:
        candidates = [row for row in stages if row.track == track]
        track_statuses[track] = (
            "queued"
            if not candidates
            else max(
                candidates,
                key=lambda row: (
                    stage_order.get(row.stage, -1),
                    row.attempt,
                    row.id,
                ),
            ).status
        )
    status = aggregate_run_status(
        run.status,
        "queued" if common is None else common.status,
        track_statuses,
    )
    session.expire(run)
    if not _run_is_mutable(run, now_provider):
        session.rollback()
        return
    session.execute(
        update(DiscoveryRun)
        .where(
            DiscoveryRun.id == run_id,
            DiscoveryRun.current.is_(True),
            DiscoveryRun.status != "abandoned",
        )
        .values(status=status, updated_at=_now(now_provider))
    )
    _commit_if_current(session, run_id, now_provider)


def drain_run_queue(
    session: Session,
    run_id: int,
    *,
    track_executor: TrackExecutor | None = None,
    lock_already_held: bool = False,
    owner_token: str | None = None,
    now_provider: NowProvider = utc_now,
) -> DrainOutcome:
    """Drain queued common/track stages under the one global job lock."""

    if type(run_id) is not int or run_id < 1:
        raise ValueError("run_id must be a positive integer")
    lease_token = (
        owner_token
        if lock_already_held
        and type(owner_token) is str
        and bool(owner_token)
        else None
    )
    adopted = False
    executed = 0
    try:
        if lock_already_held:
            if lease_token is None:
                raise ValueError("discovery run lock cannot be adopted")
            lock = session.get(JobLock, 1)
            if (
                lock is None
                or lock.job_id != run_id
                or lock.job_type != JOB_TYPE_DISCOVERY
                or lock.owner_token != lease_token
            ):
                session.rollback()
                raise ValueError("discovery run lock cannot be adopted")
        else:
            lease_token = acquire_discovery(session, run_id)
            if lease_token is None:
                session.rollback()
                return DrainOutcome(run_id, False, 0)
            session.commit()

        adopted = True
        session.info["discovery_lock_run_id"] = run_id
        session.info["discovery_lock_owner_token"] = lease_token
        while True:
            session.expire_all()
            run = session.get(DiscoveryRun, run_id)
            if not _run_is_mutable(run, now_provider) or not execution_authorized(session, run):
                session.rollback()
                return DrainOutcome(run_id, True, executed)
            confirm_waiting_layers(session, run, _now(now_provider))
            if not _commit_if_current(session, run_id, now_provider):
                return DrainOutcome(run_id, True, executed)
            selected = _next_queued_stage(session, run_id)
            if selected is None:
                return DrainOutcome(run_id, True, executed)
            kind, stage_id = selected
            if kind == "common":
                outcome = execute_common_stage(
                    session,
                    stage_id,
                    now_provider=now_provider,
                )
                if outcome.status == "historical":
                    return DrainOutcome(run_id, True, executed)
                if not outcome.claimed or outcome.status == "running":
                    raise QueueStalledError(f"common stage {stage_id} did not advance")
                if outcome.status in {"failed", "data_not_ready"}:
                    return DrainOutcome(run_id, True, executed + 1)
            else:
                stage = session.get(DiscoveryStage, stage_id)
                if stage is None:
                    raise QueueStalledError(f"track stage {stage_id} disappeared")
                preloaded = None
                if stage.ruler_version_id is not None:
                    try:
                        candidates = _track_candidates(session, stage)
                        frozen = load_frozen_inputs(
                            session,
                            run_id,
                            codes=(
                                candidate.code for candidate in candidates
                            ),
                        )
                        preloaded = (candidates, frozen)
                        ruler = frozen.ruler_payloads.get(stage.track)
                        if (
                            not isinstance(ruler, Mapping)
                            or ruler.get("id") != stage.ruler_version_id
                        ):
                            raise ValueError("frozen ruler mismatch")
                    except (TypeError, ValueError) as error:
                        session.rollback()
                        stage = session.get(DiscoveryStage, stage_id)
                        if (
                            stage is None
                            or not _run_is_mutable(
                                session.get(
                                    DiscoveryRun,
                                    stage.run_id,
                                ),
                                now_provider,
                            )
                        ):
                            return DrainOutcome(run_id, True, executed)
                        stage.status = "failed"
                        stage.error = _safe_error(error)
                        stage.failure_kind = "permanent"
                        stage.updated_at = _now(now_provider)
                        if not _commit_if_current(
                            session,
                            stage.run_id,
                            now_provider,
                        ):
                            return DrainOutcome(run_id, True, executed)
                        executed += 1
                        continue
                if stage.ruler_version_id is None or (
                    track_executor is None
                    and stage.stage != "adjudication"
                ):
                    session.rollback()
                    stage = session.get(DiscoveryStage, stage_id)
                    if (
                        stage is None
                        or not _run_is_mutable(
                            session.get(DiscoveryRun, stage.run_id),
                            now_provider,
                        )
                    ):
                        return DrainOutcome(run_id, True, executed)
                    stage.status = "ruler_not_ready"
                    stage.error = (
                        "轨道尺子尚未就绪"
                        if stage.ruler_version_id is None
                        else "当前构建未注册轨道执行器"
                    )
                    stage.failure_kind = None
                    stage.updated_at = _now(now_provider)
                    if not _commit_if_current(
                        session,
                        stage.run_id,
                        now_provider,
                    ):
                        return DrainOutcome(run_id, True, executed)
                else:
                    try:
                        completed = _execute_track_stage(
                            session,
                            stage_id,
                            track_executor,
                            now_provider,
                            preloaded=preloaded,
                        )
                        if not completed:
                            return DrainOutcome(run_id, True, executed)
                    except Exception as error:
                        session.rollback()
                        session.expire_all()
                        failed = session.get(DiscoveryStage, stage_id)
                        if (
                            failed is not None
                            and failed.status == "queued"
                            and failed.superseded_at is None
                            and _run_is_mutable(
                                session.get(
                                    DiscoveryRun,
                                    failed.run_id,
                                ),
                                now_provider,
                            )
                        ):
                            failed.status = "failed"
                            failed.error = _safe_error(error)
                            failed.failure_kind = _failure_kind(error)
                            failed.updated_at = _now(now_provider)
                            if not _commit_if_current(
                                session,
                                failed.run_id,
                                now_provider,
                            ):
                                return DrainOutcome(
                                    run_id,
                                    True,
                                    executed,
                                )
                            session.expire_all()
                            failed = session.get(
                                DiscoveryStage, stage_id
                            )
                        if failed is None or failed.status in {
                            "queued",
                            "running",
                        }:
                            raise
            executed += 1
    finally:
        try:
            if adopted:
                _sync_run_status_while_locked(
                    session,
                    run_id,
                    now_provider,
                )
        finally:
            session.info.pop("discovery_lock_run_id", None)
            session.info.pop("discovery_lock_owner_token", None)
            if lease_token is not None:
                _release_run_lock(session, run_id, lease_token)


def drain_discovery_subprocess(
    db_path: str, run_id: int, owner_token: str
) -> None:
    """Run one discovery drain outside the API process.

    The API process must stay responsive while a stage canonicalizes the
    frozen market. This entrypoint is spawned, opens its own connection,
    and adopts the lock the parent already holds.
    """

    from pathlib import Path

    from app.db import create_db_engine, create_session_factory

    engine = create_db_engine(Path(db_path))
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            drain_run_queue(
                session,
                run_id,
                track_executor=builtin_track_executor,
                lock_already_held=True,
                owner_token=owner_token,
            )
    finally:
        engine.dispose()
