"""Week-over-week discovery changes from immutable persisted artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import threading
from types import SimpleNamespace
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.discovery_models import (
    DiscoveryCommonStage,
    DiscoveryDecision,
    DiscoveryInputSnapshot,
    DiscoveryResult,
    DiscoveryRun,
    DiscoveryStage,
)
from discovery.constants import TRACKS, common_ready
from discovery.snapshot import canonical_json, load_frozen_inputs


VERDICTS = frozenset({"pass", "drop", "insufficient"})
DECISIONS = frozenset({"keep", "exclude"})
TERMINAL_ADJUDICATION = frozenset({"confirmed", "insufficient_candidates"})
_TRACK_ORDER = {track: index for index, track in enumerate(TRACKS)}
# (database, run_id, code, stored input_fingerprint) -> (name, data fingerprint).
# (database, run_id, run input_fingerprint) -> run lineage from a full check.
# ponytail: process-wide and never evicted; they only hold selected companies
# (tens per track-week) and one small entry per run. A changed stored
# fingerprint misses the cache and re-runs the full frozen-input check, but a
# row whose JSON changes without its fingerprint is only caught after a restart.
_COMPANY_FACTS: dict[tuple[str, int, str, str], tuple[str, str]] = {}
_RUN_LINEAGE: dict[tuple[str, int, str | None], SimpleNamespace] = {}
_FILL_LOCK = threading.Lock()


class CorruptDiscoveryHistory(ValueError):
    pass


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _json_value(text: str) -> object:
    def reject(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    value = json.loads(text, parse_constant=reject)
    if canonical_json(value) != text:
        raise ValueError("persisted JSON is not canonical")
    return value


def _week_start(week_key: str) -> date:
    try:
        year, week = week_key.split("-W", 1)
        return date.fromisocalendar(int(year), int(week), 1)
    except (TypeError, ValueError) as error:
        raise ValueError("week_key is invalid") from error


def _consecutive(previous: str, current: str) -> bool:
    return (_week_start(current) - _week_start(previous)).days == 7


@dataclass(frozen=True)
class PersistedCompanyResult:
    """One company's persisted state in one independently completed track."""

    week_key: str
    run_id: int
    track: str
    stage: str
    code: str
    name: str
    input_fingerprint: str
    ruler_version_id: int
    verdict: str
    reason: str | None
    decision: str | None = None
    metrics_json: str | None = None
    rank: int | None = None
    frozen_input_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _week_start(self.week_key)
        for field in (
            "track",
            "stage",
            "code",
            "name",
            "input_fingerprint",
        ):
            _text(getattr(self, field), field)
        _positive_int(self.run_id, "run_id")
        _positive_int(self.ruler_version_id, "ruler_version_id")
        if self.track not in TRACKS:
            raise ValueError("track is invalid")
        if self.verdict not in VERDICTS:
            raise ValueError("verdict is invalid")
        if self.reason is not None and type(self.reason) is not str:
            raise TypeError("reason must be text or None")
        if self.decision is not None and self.decision not in DECISIONS:
            raise ValueError("decision is invalid")
        if self.metrics_json is not None:
            _json_value(self.metrics_json)
        if self.rank is not None and (
            type(self.rank) is not int or self.rank < 1
        ):
            raise ValueError("rank must be positive or None")
        if self.frozen_input_fingerprint is not None:
            _text(self.frozen_input_fingerprint, "frozen_input_fingerprint")

    @property
    def selected(self) -> bool:
        return self.verdict == "pass" and self.decision != "exclude"


@dataclass(frozen=True)
class WeeklyChange:
    track: str
    stage: str
    code: str
    name: str
    previous_run_id: int | None
    current_run_id: int
    previous_week_key: str | None
    current_week_key: str
    previous_state: str
    current_state: str
    membership: str
    cause: str | None
    reason_changed: bool
    previous_reason: str | None
    current_reason: str | None

    @property
    def classifications(self) -> tuple[str, ...]:
        values = [self.membership]
        if self.reason_changed:
            values.append("reason_changed")
        if self.cause is not None:
            values.append(self.cause)
        return tuple(values)

    @property
    def classification(self) -> str:
        if self.cause is not None:
            return self.cause
        if self.reason_changed:
            return "reason_changed"
        return self.membership

    def as_dict(self, *, compact: bool = False) -> dict[str, object]:
        value: dict[str, object] = {
            "track": self.track,
            "stage": self.stage,
            "code": self.code,
            "name": self.name,
            "classification": self.classification,
            "membership": self.membership,
            "cause": self.cause,
            "reason_changed": self.reason_changed,
            "previous_state": self.previous_state,
            "current_state": self.current_state,
        }
        if compact:
            return value
        return value | {
            "classifications": list(self.classifications),
            "previous_run_id": self.previous_run_id,
            "current_run_id": self.current_run_id,
            "previous_week_key": self.previous_week_key,
            "current_week_key": self.current_week_key,
            "previous_reason": self.previous_reason,
            "current_reason": self.current_reason,
        }


def _cause(
    previous: PersistedCompanyResult | None,
    current: PersistedCompanyResult | None,
) -> str | None:
    if previous is None or current is None:
        return None
    data_changed = previous.input_fingerprint != current.input_fingerprint
    rule_changed = previous.ruler_version_id != current.ruler_version_id
    if data_changed and rule_changed:
        return "both_changed"
    if data_changed:
        return "data_changed"
    if rule_changed:
        return "rule_changed"
    if previous.decision != current.decision:
        return "decision_changed"
    return None


def selection_metric_changes(previous: PersistedCompanyResult | None,
                             current: PersistedCompanyResult | None) -> dict[str, object]:
    """Explain candidate changes from persisted v8 metrics, without recomputation.

    Kept alongside WeeklyChange so its published historical DTO stays exact.
    Presence/status/value/IDs are compared independently; missing is never zero.
    """
    before = _json_value(previous.metrics_json) if previous and previous.metrics_json else {}
    after = _json_value(current.metrics_json) if current and current.metrics_json else {}
    if 'metric_results' not in before and 'metric_results' not in after:
        return {}
    left, right = before.get('metric_results', {}), after.get('metric_results', {})
    return {'metrics': {key: {'previous': left.get(key), 'current': right.get(key)}
                        for key in sorted(left.keys() | right.keys()) if left.get(key) != right.get(key)},
            'operating_state': {'previous': before.get('operating_state'), 'current': after.get('operating_state')},
            'ranking_audit': {'previous': before.get('ranking_audit'), 'current': after.get('ranking_audit')},
            'shadow_signals': {'previous': before.get('shadow_signals'), 'current': after.get('shadow_signals')},
            'collection_effect': 'shadow_only'}


def classify_weekly_changes(
    rows: Iterable[PersistedCompanyResult],
    *,
    current_run_id: int,
    run_order: Iterable[tuple[str, int]] | None = None,
) -> tuple[WeeklyChange, ...]:
    """Classify one track without executing any historical algorithm."""

    _positive_int(current_run_id, "current_run_id")
    values = tuple(rows)
    if any(type(row) is not PersistedCompanyResult for row in values):
        raise TypeError("history contains an invalid persisted result")
    supplied_order = tuple(run_order or ())
    if any(
        type(item) is not tuple
        or len(item) != 2
        or type(item[0]) is not str
        or type(item[1]) is not int
        or item[1] < 1
        for item in supplied_order
    ):
        raise ValueError("run order is invalid")
    if len({run_id for _week, run_id in supplied_order}) != len(supplied_order):
        raise ValueError("run order contains duplicate runs")
    if not values:
        if supplied_order and current_run_id not in {
            run_id for _week, run_id in supplied_order
        }:
            raise ValueError("current run is absent from history")
        return ()
    scopes = {(row.track, row.stage) for row in values}
    if len(scopes) != 1:
        raise ValueError("history must contain exactly one track and stage")
    by_key: dict[tuple[int, str], PersistedCompanyResult] = {}
    run_weeks: dict[int, str] = {}
    for row in values:
        key = (row.run_id, row.code)
        if key in by_key:
            raise ValueError("duplicate company result in weekly run")
        by_key[key] = row
        observed = run_weeks.setdefault(row.run_id, row.week_key)
        if observed != row.week_key:
            raise ValueError("one run cannot span multiple weeks")
    for week_key, run_id in supplied_order:
        _week_start(week_key)
        observed = run_weeks.setdefault(run_id, week_key)
        if observed != week_key:
            raise ValueError("run order conflicts with persisted rows")
    if current_run_id not in run_weeks:
        raise ValueError("current run is absent from history")
    current_order = (run_weeks[current_run_id], current_run_id)
    run_ids = [
        run_id
        for run_id in sorted(run_weeks, key=lambda item: (run_weeks[item], item))
        if (run_weeks[run_id], run_id) <= current_order
    ]
    current_index = run_ids.index(current_run_id)
    prior_run_ids = run_ids[:current_index]
    current_codes = {
        row.code for row in values if row.run_id == current_run_id
    }
    previously_selected = {
        row.code
        for row in values
        if row.run_id in prior_run_ids and row.selected
    }
    track, stage = next(iter(scopes))
    changes: list[WeeklyChange] = []
    for code in sorted(current_codes | previously_selected):
        current = by_key.get((current_run_id, code))
        previous = next(
            (
                row
                for run_id in reversed(prior_run_ids)
                if (row := by_key.get((run_id, code))) is not None
            ),
            None,
        )
        current_selected = current is not None and current.selected
        previous_selected = previous is not None and previous.selected
        previous_is_consecutive = (
            previous is not None
            and _consecutive(previous.week_key, run_weeks[current_run_id])
        )
        if not current_selected and not previous_selected:
            continue
        if not current_selected and not previous_is_consecutive:
            continue
        selected_before = any(
            (row := by_key.get((run_id, code))) is not None and row.selected
            for run_id in prior_run_ids
        )
        if current_selected:
            membership = (
                "continuous"
                if previous_selected
                and previous_is_consecutive
                else "reentered"
                if selected_before
                else "new"
            )
        else:
            membership = "dropped"
        reason_changed = (
            current_selected
            and previous_selected
            and previous is not None
            and current is not None
            and previous.reason != current.reason
        )
        representative = current or previous
        assert representative is not None
        changes.append(
            WeeklyChange(
                track=track,
                stage=stage,
                code=code,
                name=representative.name,
                previous_run_id=None if previous is None else previous.run_id,
                current_run_id=current_run_id,
                previous_week_key=(
                    None if previous is None else previous.week_key
                ),
                current_week_key=run_weeks[current_run_id],
                previous_state=(
                    "absent" if previous is None else previous.verdict
                ),
                current_state="absent" if current is None else current.verdict,
                membership=membership,
                cause=_cause(previous, current),
                reason_changed=reason_changed,
                previous_reason=None if previous is None else previous.reason,
                current_reason=None if current is None else current.reason,
            )
        )
    return tuple(changes)


def _data_fingerprint(row: object) -> str:
    semantic = {
        "code": row.code,
        "name": row.name,
        "industry": row.industry,
        "security_status": dict(row.security_status),
        "fields": dict(row.fields),
        "sources": dict(row.sources),
        "as_of": dict(row.as_of),
        "data_version": row.data_version,
        "financial_version": row.financial_version,
        "valuation_version": row.valuation_version,
    }
    return hashlib.sha256(canonical_json(semantic).encode()).hexdigest()


def _validate_track_lineage(
    session: Session,
    run: DiscoveryRun,
    track: str,
    stage: DiscoveryStage,
    frozen,
) -> DiscoveryCommonStage | DiscoveryStage:
    if (
        stage.run_id != run.id
        or stage.track != track
        or stage.superseded_at is not None
        or stage.input_fingerprint != frozen.input_fingerprint
        or stage.algorithm_version != frozen.algorithm_version
        or type(stage.ruler_version_id) is not int
        or stage.ruler_version_id < 1
        or stage.ruler_version_id != frozen.ruler_version_ids.get(track)
    ):
        raise CorruptDiscoveryHistory("persisted stage lineage is corrupt")
    if stage.stage == "track_gate":
        parent = session.get(DiscoveryCommonStage, stage.common_stage_id)
        if (
            parent is None
            or parent.run_id != run.id
            or parent.status != "confirmed"
            or parent.superseded_at is not None
            or parent.input_fingerprint != frozen.input_fingerprint
            or parent.algorithm_version != frozen.algorithm_version
            or stage.predecessor_stage_id is not None
            or stage.predecessor_stage is not None
        ):
            raise CorruptDiscoveryHistory("persisted common lineage is corrupt")
        return parent
    expected = "track_gate" if stage.stage == "competition" else "competition"
    parent = session.get(DiscoveryStage, stage.predecessor_stage_id)
    if (
        stage.stage not in {"competition", "adjudication"}
        or stage.common_stage_id is not None
        or stage.predecessor_stage != expected
        or parent is None
        or parent.stage != expected
        or parent.status != "confirmed"
    ):
        raise CorruptDiscoveryHistory("persisted predecessor lineage is corrupt")
    _validate_track_lineage(session, run, track, parent, frozen)
    return parent


def _common_complete(session: Session, run: DiscoveryRun) -> bool:
    stage = session.scalars(
        select(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.run_id == run.id,
            DiscoveryCommonStage.superseded_at.is_(None),
        )
        .order_by(DiscoveryCommonStage.attempt.desc(), DiscoveryCommonStage.id.desc())
    ).first()
    return stage is not None and common_ready(
        stage.pass_count, stage.drop_count, stage.total_count
    )


def _readable_stage(
    session: Session,
    run: DiscoveryRun,
    track: str,
    stage_name: str,
) -> DiscoveryStage | None:
    stage = session.scalars(
        select(DiscoveryStage)
        .where(
            DiscoveryStage.run_id == run.id,
            DiscoveryStage.track == track,
            DiscoveryStage.stage == stage_name,
            DiscoveryStage.superseded_at.is_(None),
        )
        .order_by(DiscoveryStage.attempt.desc(), DiscoveryStage.id.desc())
    ).first()
    readable = (
        TERMINAL_ADJUDICATION
        if stage_name == "adjudication"
        else {"waiting_confirmation", "confirmed"}
    )
    if stage is None or stage.status not in readable:
        return None
    return stage


def _passing_codes(
    session: Session, stage: DiscoveryStage
) -> set[str]:
    return set(
        session.scalars(
            select(DiscoveryResult.code).where(
                DiscoveryResult.run_id == stage.run_id,
                DiscoveryResult.track == stage.track,
                DiscoveryResult.stage == stage.stage,
                DiscoveryResult.attempt == stage.attempt,
                DiscoveryResult.verdict == "pass",
            )
        )
    )


def _completed_track_rows(
    session: Session,
    run: DiscoveryRun,
    track: str,
    stage_name: str,
    codes: set[str] | None = None,
) -> tuple[PersistedCompanyResult, ...] | None:
    """Return an exact persisted track state, or None for unsafe lineage.

    ``codes`` limits the returned rows; every result is still validated.
    """

    try:
        stage = _readable_stage(session, run, track, stage_name)
        if stage is None:
            return None
        results = session.scalars(
            select(DiscoveryResult)
            .where(
                DiscoveryResult.run_id == run.id,
                DiscoveryResult.track == track,
                DiscoveryResult.stage == stage_name,
                DiscoveryResult.attempt == stage.attempt,
            )
            .order_by(DiscoveryResult.code, DiscoveryResult.id)
        ).all()
        result_by_code = {row.code: row for row in results}
        wanted = (
            set(result_by_code) if codes is None else codes & set(result_by_code)
        )
        stored = dict(
            session.execute(
                select(
                    DiscoveryInputSnapshot.code,
                    DiscoveryInputSnapshot.input_fingerprint,
                ).where(
                    DiscoveryInputSnapshot.run_id == run.id,
                    DiscoveryInputSnapshot.code.in_(sorted(wanted)),
                )
            ).all()
        )
        database = str(session.get_bind().url)

        def facts_key(code: str) -> tuple[str, int, str, str]:
            return (database, run.id, code, stored.get(code, ""))

        # Page polls reach here; checking and parsing a full-market run's
        # frozen inputs took seconds per request, so do each once, and
        # don't let concurrent cold polls parse the same companies twice.
        run_key = (database, run.id, run.input_fingerprint)

        def missing() -> set[str]:
            return {code for code in wanted if facts_key(code) not in _COMPANY_FACTS}

        if missing() or run_key not in _RUN_LINEAGE:
            with _FILL_LOCK:
                if missing() or run_key not in _RUN_LINEAGE:
                    frozen = load_frozen_inputs(session, run.id, codes=missing())
                    for company in frozen.companies:
                        _COMPANY_FACTS[facts_key(company.code)] = (
                            company.name,
                            _data_fingerprint(company),
                        )
                    _RUN_LINEAGE[run_key] = SimpleNamespace(
                        input_fingerprint=frozen.input_fingerprint,
                        algorithm_version=frozen.algorithm_version,
                        ruler_version_ids=dict(frozen.ruler_version_ids),
                    )
        parent = _validate_track_lineage(
            session, run, track, stage, _RUN_LINEAGE[run_key]
        )
        if len(result_by_code) != len(results) or stage.total_count != len(results):
            raise CorruptDiscoveryHistory("persisted results are corrupt")
        decisions = ()
        if stage_name == "adjudication":
            assert isinstance(parent, DiscoveryStage)
            decisions = session.scalars(
                select(DiscoveryDecision)
                .where(
                    DiscoveryDecision.run_id == run.id,
                    DiscoveryDecision.track == track,
                    DiscoveryDecision.competition_stage_id == parent.id,
                    DiscoveryDecision.competition_attempt == parent.attempt,
                )
                .order_by(DiscoveryDecision.code, DiscoveryDecision.id)
            ).all()
        decision_by_code = {row.code: row for row in decisions}
        if stage_name == "adjudication" and (
            len(decision_by_code) != len(decisions)
            or set(result_by_code) != set(decision_by_code)
            or any(
                result_by_code[code].verdict
                != ("pass" if decision.decision == "keep" else "drop")
                for code, decision in decision_by_code.items()
            )
        ):
            raise CorruptDiscoveryHistory("persisted decisions are corrupt")
        primary_reasons: dict[str, str | None] = {}
        if stage_name == "adjudication":
            assert isinstance(parent, DiscoveryStage)
            competition_results = session.scalars(
                select(DiscoveryResult)
                .where(
                    DiscoveryResult.run_id == run.id,
                    DiscoveryResult.track == track,
                    DiscoveryResult.stage == "competition",
                    DiscoveryResult.attempt == parent.attempt,
                )
                .order_by(DiscoveryResult.code, DiscoveryResult.id)
            ).all()
            primary_reasons = {
                row.code: row.reason for row in competition_results
            }
            if (
                len(primary_reasons) != len(competition_results)
                or not set(result_by_code) <= set(primary_reasons)
            ):
                raise CorruptDiscoveryHistory(
                    "persisted competition reasons are corrupt"
                )
            for row in competition_results:
                _json_value(row.metrics_json)
        rows = []
        for result in results:
            if result.code not in wanted:
                continue
            facts = _COMPANY_FACTS.get(facts_key(result.code))
            if facts is None:
                raise CorruptDiscoveryHistory(
                    "persisted result has no frozen company"
                )
            name, data_fingerprint = facts
            decision = decision_by_code.get(result.code)
            rows.append(
                PersistedCompanyResult(
                    week_key=run.week_key,
                    run_id=run.id,
                    track=track,
                    stage=stage_name,
                    code=result.code,
                    name=name,
                    input_fingerprint=data_fingerprint,
                    frozen_input_fingerprint=stored[result.code],
                    ruler_version_id=stage.ruler_version_id,
                    verdict=result.verdict,
                    reason=(
                        primary_reasons[result.code]
                        if stage_name == "adjudication"
                        else result.reason
                    ),
                    decision=None if decision is None else decision.decision,
                    metrics_json=result.metrics_json,
                    rank=result.rank,
                )
            )
        return tuple(rows)
    except CorruptDiscoveryHistory:
        raise
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise CorruptDiscoveryHistory(
            "persisted discovery history is corrupt"
        ) from error


def load_weekly_changes(
    session: Session,
    current_run_id: int,
    *,
    track: str | None = None,
    stage: str = "adjudication",
) -> tuple[WeeklyChange, ...]:
    """Load safe ordered history; bad or incomplete track-weeks are absent."""

    _positive_int(current_run_id, "current_run_id")
    if track is not None and track not in TRACKS:
        raise ValueError("track is invalid")
    if stage not in {"track_gate", "competition", "adjudication"}:
        raise ValueError("stage is invalid")
    current = session.get(DiscoveryRun, current_run_id)
    if current is None:
        raise ValueError("current discovery run not found")
    runs = session.scalars(
        select(DiscoveryRun)
        .where(
            DiscoveryRun.user_id == current.user_id,
            DiscoveryRun.status != "abandoned",
            DiscoveryRun.week_key <= current.week_key,
        )
        .order_by(DiscoveryRun.week_key, DiscoveryRun.id)
    ).all()
    selected_by_week: dict[str, DiscoveryRun] = {}
    for run in runs:
        if run.week_key == current.week_key and run.id != current.id:
            continue
        selected_by_week[run.week_key] = run
    selected_by_week[current.week_key] = current
    ordered_runs = tuple(
        selected_by_week[key]
        for key in sorted(selected_by_week)
        if _common_complete(session, selected_by_week[key])
    )
    if current not in ordered_runs or len(ordered_runs) < 2:
        return ()
    requested = TRACKS if track is None else (track,)
    changes: list[WeeklyChange] = []
    for selected_track in requested:
        stages = {
            run.id: _readable_stage(session, run, selected_track, stage)
            for run in ordered_runs
        }
        if stages[current.id] is None:
            continue
        # Only a company selected in some week can become a change, and
        # page polls land here: frozen inputs of a full-market week took
        # seconds to parse per track, so load just the selected ones.
        selected: set[str] = set()
        for row in stages.values():
            if row is not None:
                selected |= _passing_codes(session, row)
        rows: list[PersistedCompanyResult] = []
        run_order = []
        current_loaded = False
        for run in ordered_runs:
            run_order.append((run.week_key, run.id))
            if stages[run.id] is None:
                continue
            persisted = _completed_track_rows(
                session, run, selected_track, stage, selected
            )
            if persisted is not None:
                rows.extend(persisted)
                current_loaded = current_loaded or run.id == current.id
        if not current_loaded:
            continue
        changes.extend(
            classify_weekly_changes(
                rows,
                current_run_id=current.id,
                run_order=run_order,
            )
        )
    return tuple(
        sorted(
            changes,
            key=lambda row: (_TRACK_ORDER[row.track], row.code),
        )
    )
