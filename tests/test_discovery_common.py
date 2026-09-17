from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest
from sqlalchemy import event, func, select

from app.db import create_session_factory, init_db
from tests.discovery_helpers import (
    SNAPSHOT_AT,
    add_unfrozen_run,
    company,
    versions,
)


TRADING_DATES = {
    "SH": [
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
    ],
    "SZ": [
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
    ],
}


def _freeze_run(session, companies, *, week_key="2026-W38"):
    from discovery.snapshot import freeze_inputs

    run = add_unfrozen_run(session, week_key=week_key)
    freeze_inputs(
        session,
        run,
        companies,
        versions(),
        TRADING_DATES,
        snapshot_at=SNAPSHOT_AT,
    )
    run.current = True
    run.status = "running"
    session.flush()
    return run


def _add_common_stage(
    session,
    run,
    *,
    attempt=1,
    status="queued",
    created_at=SNAPSHOT_AT,
    superseded_at=None,
):
    from app.discovery_models import DiscoveryCommonStage

    stage = DiscoveryCommonStage(
        run_id=run.id,
        attempt=attempt,
        status=status,
        total_count=0,
        pass_count=0,
        drop_count=0,
        insufficient_count=0,
        error=None,
        input_fingerprint=run.input_fingerprint,
        algorithm_version=run.algorithm_version,
        failure_kind=None,
        superseded_at=superseded_at,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(stage)
    session.flush()
    return stage


def _add_track_stage(
    session,
    run,
    *,
    track,
    created_at,
    status="queued",
    superseded_at=None,
):
    from sqlalchemy import func, select

    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
        DiscoveryStage,
    )

    parent = session.scalars(
        select(DiscoveryCommonStage)
        .where(
            DiscoveryCommonStage.run_id == run.id,
            DiscoveryCommonStage.status == "confirmed",
            DiscoveryCommonStage.superseded_at.is_(None),
        )
        .order_by(DiscoveryCommonStage.attempt.desc())
    ).first()
    if parent is None:
        parent = _add_common_stage(
            session,
            run,
            attempt=(
                session.scalar(
                    select(func.max(DiscoveryCommonStage.attempt)).where(
                        DiscoveryCommonStage.run_id == run.id
                    )
                )
                or 0
            )
            + 1,
            status="confirmed",
            created_at=created_at - timedelta(seconds=1),
        )
    parent.status = "confirmed"
    if not session.scalar(
        select(DiscoveryCommonResult.id).where(
            DiscoveryCommonResult.run_id == run.id,
            DiscoveryCommonResult.attempt == parent.attempt,
            DiscoveryCommonResult.code == "600000.SH",
        )
    ):
        session.add(
            DiscoveryCommonResult(
                run_id=run.id,
                attempt=parent.attempt,
                code="600000.SH",
                verdict="pass",
                reason=None,
                metrics_json="{}",
                created_at=created_at,
            )
        )
        parent.total_count = 1
        parent.pass_count = 1
    stage = DiscoveryStage(
        run_id=run.id,
        track=track,
        stage="track_gate",
        attempt=1,
        status=status,
        total_count=0,
        pass_count=0,
        drop_count=0,
        insufficient_count=0,
        error=None,
        input_fingerprint=run.input_fingerprint,
        ruler_version_id=getattr(run, f"{track}_ruler_version_id"),
        algorithm_version=run.algorithm_version,
        common_stage_id=parent.id,
        predecessor_stage_id=None,
        predecessor_stage=None,
        failure_kind=None,
        superseded_at=superseded_at,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(stage)
    session.flush()
    return stage


def _loaded_company(tmp_path, item=None):
    from discovery.snapshot import load_frozen_inputs

    engine = init_db(tmp_path / "classifier.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [item or company("600000.SH")])
        session.commit()
        frozen = load_frozen_inputs(session, run.id).companies[0]
    engine.dispose()
    return frozen


@pytest.mark.parametrize(
    ("code", "exchange", "reason"),
    [
        ("bad", "SH", "invalid_a_share_code"),
        ("830001.BJ", "BJ", "unsupported_exchange"),
        ("600000.SH", "SZ", "exchange_code_mismatch"),
    ],
)
def test_classifier_drops_invalid_or_non_sh_sz_codes(
    tmp_path, code, exchange, reason
) -> None:
    from discovery.common import classify_common

    frozen = _loaded_company(tmp_path)
    verdict = classify_common(
        replace(
            frozen,
            code=code,
            security_status={**frozen.security_status, "exchange": exchange},
        ),
        TRADING_DATES,
    )
    assert (verdict.verdict, verdict.reason) == ("drop", reason)


@pytest.mark.parametrize(
    ("field", "reason"),
    [("is_st", "st_security"), ("is_delisting", "delisting_security")],
)
def test_classifier_drops_known_st_and_delisting(
    tmp_path, field, reason
) -> None:
    from discovery.common import classify_common

    frozen = _loaded_company(tmp_path)
    verdict = classify_common(
        replace(
            frozen,
            security_status={**frozen.security_status, field: True},
            fields={**frozen.fields, "price": None},
        ),
        TRADING_DATES,
    )
    assert (verdict.verdict, verdict.reason) == ("drop", reason)


def test_classifier_reuses_exchange_calendar_security_semantics(tmp_path) -> None:
    from discovery.common import classify_common

    frozen = _loaded_company(tmp_path)
    recent_suspension = replace(
        frozen,
        security_status={
            **frozen.security_status,
            "trading_status": "suspended",
            "last_trade_date": "2026-09-11",
        },
        as_of={**frozen.as_of, "price": "2026-09-11"},
    )
    old_suspension = replace(
        recent_suspension,
        security_status={
            **recent_suspension.security_status,
            "last_trade_date": "2026-09-10",
        },
        as_of={**recent_suspension.as_of, "price": "2026-09-10"},
    )
    stale_trading = replace(
        frozen,
        security_status={
            **frozen.security_status,
            "last_trade_date": "2026-09-16",
        },
        as_of={**frozen.as_of, "price": "2026-09-16"},
    )

    assert classify_common(frozen, TRADING_DATES).verdict == "pass"
    assert classify_common(recent_suspension, TRADING_DATES).verdict == "pass"
    assert (
        classify_common(old_suspension, TRADING_DATES).reason
        == "suspension_too_old"
    )
    assert (
        classify_common(stale_trading, TRADING_DATES).reason
        == "trading_date_not_current"
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"security_status": {"security_source": None}}, "security_provenance_missing"),
        ({"security_status": {"is_st": None}}, "st_state_unknown"),
        ({"security_status": {"trading_status": "halted"}}, "trading_status_unknown"),
        ({"fields": {"price": None}}, "price_missing"),
        ({"fields": {"price": "NaN"}}, "price_invalid"),
        ({"fields": {"price": 0}}, "price_invalid"),
        ({"sources": {"price": None}}, "price_provenance_missing"),
        ({"as_of": {"price": "not-a-date"}}, "price_as_of_invalid"),
        ({"industry": None}, "industry_missing"),
        ({"security_status": {"industry_version": None}}, "industry_provenance_missing"),
        ({"security_status": {"last_trade_date": "not-a-date"}}, "last_trade_date_invalid"),
    ],
)
def test_classifier_fails_closed_on_missing_or_malformed_shared_inputs(
    tmp_path, change, reason
) -> None:
    from discovery.common import classify_common

    frozen = _loaded_company(tmp_path)
    updates = dict(change)
    for mapping_name in ("security_status", "fields", "sources", "as_of"):
        if mapping_name in updates:
            updates[mapping_name] = {
                **getattr(frozen, mapping_name),
                **updates[mapping_name],
            }
    verdict = classify_common(replace(frozen, **updates), TRADING_DATES)
    assert (verdict.verdict, verdict.reason) == ("insufficient", reason)


def test_classifier_ignores_recent_listing_and_track_only_financials(
    tmp_path,
) -> None:
    from discovery.common import classify_common

    item = company("600000.SH", recent_listing=True)
    item = replace(
        item,
        fields={
            **item.fields,
            "revenue": None,
            "ocf": object.__name__,
            "interest_debt": -1,
            "net_assets": None,
        },
    )
    frozen = _loaded_company(tmp_path, item)
    verdict = classify_common(frozen, TRADING_DATES)

    assert verdict.verdict == "pass"
    assert "revenue" not in verdict.metrics
    assert "ocf" not in verdict.metrics
    with pytest.raises(TypeError):
        verdict.metrics["new"] = "value"  # type: ignore[index]
    assert json.loads(json.dumps(verdict.metrics)) == dict(verdict.metrics)


def test_classifier_rejects_unknown_or_malformed_exchange_calendar(tmp_path) -> None:
    from discovery.common import classify_common

    frozen = _loaded_company(tmp_path)
    missing = classify_common(frozen, {"SZ": TRADING_DATES["SZ"]})
    malformed = classify_common(
        frozen,
        {"SH": ["2026-09-17", "2026-09-16"], "SZ": TRADING_DATES["SZ"]},
    )
    assert missing.reason == "exchange_calendar_missing"
    assert malformed.reason == "exchange_calendar_invalid"


def test_classifier_requires_exact_canonical_code(tmp_path) -> None:
    from discovery.common import classify_common

    frozen = _loaded_company(tmp_path)
    canonical = classify_common(frozen, TRADING_DATES)
    variant_code = " 600000.sh "
    variant = classify_common(
        replace(frozen, code=variant_code), TRADING_DATES
    )

    assert canonical.verdict == "pass"
    assert (variant.code, variant.verdict, variant.reason) == (
        variant_code,
        "drop",
        "invalid_a_share_code",
    )
    assert sum(
        verdict.verdict == "pass" for verdict in (canonical, variant)
    ) == 1


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("calendar", "exchange_calendar_future"),
        ("listing_date", "listing_date_future"),
        ("last_trade_date", "last_trade_date_future"),
        ("industry_as_of", "industry_as_of_future"),
        ("price_as_of", "price_as_of_future"),
    ],
)
def test_classifier_rejects_each_future_as_of_date(
    tmp_path, field, reason
) -> None:
    from discovery.common import classify_common

    frozen = _loaded_company(tmp_path)
    dates = TRADING_DATES
    updates = {}
    if field == "calendar":
        dates = {
            **TRADING_DATES,
            "SH": [*TRADING_DATES["SH"][:-1], "2099-01-01"],
        }
    elif field in {"listing_date", "last_trade_date", "industry_as_of"}:
        updates["security_status"] = {
            **frozen.security_status,
            field: "2099-01-01",
        }
    else:
        updates["as_of"] = {**frozen.as_of, "price": "2099-01-01"}

    verdict = classify_common(replace(frozen, **updates), dates)
    assert (verdict.verdict, verdict.reason) == ("insufficient", reason)


def test_classifier_rejects_self_consistent_future_bundle(tmp_path) -> None:
    from discovery.common import classify_common

    frozen = _loaded_company(tmp_path)
    future = replace(
        frozen,
        security_status={
            **frozen.security_status,
            "listing_date": "2099-01-01",
            "last_trade_date": "2099-01-01",
            "industry_as_of": "2099-01-01",
        },
        as_of={**frozen.as_of, "price": "2099-01-01"},
    )
    dates = {
        **TRADING_DATES,
        "SH": ["2098-12-28", "2098-12-29", "2099-01-01"],
    }

    verdict = classify_common(future, dates)
    assert verdict.verdict == "insufficient"
    assert verdict.reason is not None


def test_execute_common_stage_persists_canonical_complete_results(tmp_path) -> None:
    from app.discovery_models import DiscoveryCommonResult, DiscoveryCommonStage
    from jobs.discovery import execute_common_stage

    engine = init_db(tmp_path / "execute.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        rows = [
            company("600000.SH"),
            company(
                "000001.SZ",
                security_status={
                    **company("000001.SZ").security_status,
                    "is_st": True,
                },
            ),
            company("000002.SZ", industry=None),
        ]
        run = _freeze_run(session, rows)
        stage = _add_common_stage(session, run)
        session.commit()

        outcome = execute_common_stage(session, stage.id)
        assert outcome.claimed
        assert outcome.status == "waiting_confirmation"
        persisted = session.get(DiscoveryCommonStage, stage.id)
        assert persisted is not None
        assert (
            persisted.total_count,
            persisted.pass_count,
            persisted.drop_count,
            persisted.insufficient_count,
        ) == (3, 1, 1, 1)
        results = session.scalars(
            select(DiscoveryCommonResult).order_by(DiscoveryCommonResult.code)
        ).all()
        assert [(row.code, row.verdict, row.reason) for row in results] == [
            ("000001.SZ", "drop", "st_security"),
            ("000002.SZ", "insufficient", "industry_missing"),
            ("600000.SH", "pass", None),
        ]
        assert all(
            row.metrics_json
            == json.dumps(
                json.loads(row.metrics_json),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for row in results
        )

        again = execute_common_stage(session, stage.id)
        assert not again.claimed
        assert again.status == "waiting_confirmation"
        assert session.scalar(
            select(func.count()).select_from(DiscoveryCommonResult)
        ) == 3
    engine.dispose()


def test_execute_claim_prevents_a_second_session_running_same_attempt(
    tmp_path, monkeypatch
) -> None:
    from jobs import discovery as job

    engine = init_db(tmp_path / "claim.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as setup:
        run = _freeze_run(setup, [company("600000.SH")])
        stage = _add_common_stage(setup, run)
        stage_id = stage.id
        setup.commit()

    real_classifier = job.classify_common
    inner_outcomes = []

    def classify_with_competitor(item, dates):
        with SessionLocal() as competitor:
            inner_outcomes.append(job.execute_common_stage(competitor, stage_id))
        return real_classifier(item, dates)

    monkeypatch.setattr(job, "classify_common", classify_with_competitor)
    with SessionLocal() as worker:
        outer = job.execute_common_stage(worker, stage_id)
    assert outer.claimed
    assert len(inner_outcomes) == 1
    assert not inner_outcomes[0].claimed
    assert inner_outcomes[0].status == "running"
    engine.dispose()


def test_execute_reads_only_frozen_inputs_after_source_mutation(tmp_path) -> None:
    from app.discovery_models import DiscoveryCommonResult
    from app.models import (
        Financial,
        Setting,
        ValuationHistory,
        ValuationSeries,
    )
    from jobs.discovery import execute_common_stage

    engine = init_db(tmp_path / "frozen-only.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_common_stage(session, run)
        session.add(
            Financial(
                code="600000.SH",
                period="2025-12-31",
                period_type="annual",
                revenue=Decimal("999"),
            )
        )
        session.add(ValuationSeries(code="600000.SH", ratio_method="pe"))
        session.flush()
        session.add(
            ValuationHistory(
                code="600000.SH",
                month="2026-09",
                trade_date="2026-09-17",
                price=Decimal("999"),
                source="mutated",
                ratio_method="pe",
                fetched_at=SNAPSHOT_AT,
            )
        )
        session.merge(Setting(key="cheap_universe", value='{"mutated":true}'))
        session.commit()

        forbidden = ("financials", "valuation_history", "settings")

        def guard(_conn, _cursor, statement, _parameters, _context, _many):
            lowered = statement.lower()
            if any(name in lowered for name in forbidden):
                raise AssertionError(f"live source queried: {statement}")

        event.listen(engine, "before_cursor_execute", guard)
        try:
            outcome = execute_common_stage(session, stage.id)
        finally:
            event.remove(engine, "before_cursor_execute", guard)
        assert outcome.status == "waiting_confirmation"
        result = session.scalar(select(DiscoveryCommonResult))
        assert result is not None
        assert json.loads(result.metrics_json)["price"] == "12.5"
    engine.dispose()


@pytest.mark.parametrize(
    ("error_factory", "failure_kind"),
    [
        (lambda: ValueError("broken snapshot"), "permanent"),
        (lambda: RuntimeError("programmer bug"), "permanent"),
        (lambda: InterruptedError("interrupted"), "transient"),
    ],
)
def test_execute_failure_is_persisted_without_partial_results(
    tmp_path, monkeypatch, error_factory, failure_kind
) -> None:
    from app.discovery_models import DiscoveryCommonResult, DiscoveryCommonStage
    from jobs import discovery as job

    engine = init_db(tmp_path / f"failure-{failure_kind}.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(
            session, [company("600000.SH"), company("000001.SZ")]
        )
        stage = _add_common_stage(session, run)
        stage_id = stage.id
        session.commit()
        calls = 0

        def fail_on_second(item, dates):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise error_factory()
            return job._classify_common(item, dates)

        monkeypatch.setattr(job, "classify_common", fail_on_second)
        outcome = job.execute_common_stage(session, stage_id)
        assert outcome.status == "failed"
        failed = session.get(DiscoveryCommonStage, stage_id)
        assert failed is not None
        assert failed.failure_kind == failure_kind
        assert failed.error == str(error_factory())
        assert session.scalars(select(DiscoveryCommonResult)).all() == []
    engine.dispose()


def test_execute_rejects_snapshot_or_stage_identity_mismatch(tmp_path) -> None:
    from app.discovery_models import DiscoveryCommonStage
    from jobs.discovery import execute_common_stage

    engine = init_db(tmp_path / "identity.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_common_stage(session, run)
        stage.input_fingerprint = "tampered"
        stage_id = stage.id
        session.commit()
        outcome = execute_common_stage(session, stage_id)
        failed = session.get(DiscoveryCommonStage, stage_id)
        assert outcome.status == "failed"
        assert failed is not None
        assert failed.failure_kind == "permanent"
        assert "fingerprint" in (failed.error or "")
    engine.dispose()


def test_drain_serializes_cross_table_order_and_releases_lock(
    tmp_path, monkeypatch
) -> None:
    from app.discovery_models import DiscoveryCommonStage
    from app.models import JobLock
    from jobs import discovery as job

    engine = init_db(tmp_path / "drain.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [company("600000.SH")])
        early = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT - timedelta(minutes=1),
        )
        common = session.get(DiscoveryCommonStage, early.common_stage_id)
        assert common is not None and common.status == "confirmed"
        late = _add_track_stage(
            session,
            run,
            track="deep_value",
            created_at=SNAPSHOT_AT + timedelta(minutes=1),
        )
        skipped = _add_track_stage(
            session,
            run,
            track="garp",
            created_at=SNAPSHOT_AT - timedelta(minutes=2),
            superseded_at=SNAPSHOT_AT,
        )
        run_id = run.id
        session.commit()

        order = []
        def track_executor(context):
            order.append(("track", context.stage_id))
            return _passing_track_outcome(context)

        outcome = job.drain_run_queue(
            session, run_id, track_executor=track_executor
        )
        assert outcome.acquired
        assert outcome.executed == 2
        assert order == [
            ("track", early.id),
            ("track", late.id),
        ]
        assert session.get(type(skipped), skipped.id).status == "queued"
        lock = session.get(JobLock, 1)
        assert lock is not None
        assert (lock.job_id, lock.job_type) == (None, None)
    engine.dispose()


def _add_track_result(session, stage, *, code="600000.SH"):
    from app.discovery_models import DiscoveryResult

    result = DiscoveryResult(
        run_id=stage.run_id,
        track=stage.track,
        stage=stage.stage,
        attempt=stage.attempt,
        code=code,
        verdict="pass",
        reason=None,
        metrics_json="{}",
        rank=None,
        created_at=SNAPSHOT_AT,
    )
    session.add(result)
    stage.total_count += 1
    stage.pass_count += 1
    return result


def _passing_track_outcome(context, *, codes=None):
    from jobs.discovery import TrackExecutionOutcome, TrackExecutionResult

    return TrackExecutionOutcome(
        status="waiting_confirmation",
        results=tuple(
            TrackExecutionResult(
                code=code,
                verdict="pass",
                reason=None,
                metrics_json="{}",
                rank=None,
            )
            for code in (
                context.candidate_codes if codes is None else tuple(codes)
            )
        ),
    )


def test_drain_commits_successful_track_callback_atomically(tmp_path) -> None:
    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "track-success.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        session.commit()

        def execute(context):
            return _passing_track_outcome(context)

        outcome = drain_run_queue(
            session, run_id, track_executor=execute
        )
        assert outcome.executed == 1

    with SessionLocal() as verify:
        assert verify.get(DiscoveryStage, stage_id).status == (
            "waiting_confirmation"
        )
        assert verify.scalar(
            select(func.count()).select_from(DiscoveryResult)
        ) == 1
    engine.dispose()


def test_track_executor_is_pure_and_drain_persists_its_outcome(
    tmp_path,
) -> None:
    from dataclasses import FrozenInstanceError

    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from jobs.discovery import (
        TrackExecutionContext,
        TrackExecutionOutcome,
        TrackExecutionResult,
        drain_run_queue,
    )

    engine = init_db(tmp_path / "pure-track-executor.db")
    factory = create_session_factory(engine)
    seen = []
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        session.commit()

        def execute(context):
            assert isinstance(context, TrackExecutionContext)
            assert not hasattr(context, "session")
            assert context.stage_id == stage_id
            assert context.candidate_codes == ("600000.SH",)
            with pytest.raises(FrozenInstanceError):
                context.stage_id = 999
            seen.append(context)
            return TrackExecutionOutcome(
                status="waiting_confirmation",
                results=(
                    TrackExecutionResult(
                        code="600000.SH",
                        verdict="pass",
                        reason=None,
                        metrics_json="{}",
                        rank=None,
                    ),
                ),
            )

        outcome = drain_run_queue(
            session, run_id, track_executor=execute
        )
        assert outcome.executed == 1
        assert len(seen) == 1
        persisted = session.get(DiscoveryStage, stage_id)
        assert persisted is not None
        assert (
            persisted.status,
            persisted.total_count,
            persisted.pass_count,
        ) == ("waiting_confirmation", 1, 1)
        assert session.scalar(
            select(func.count()).select_from(DiscoveryResult)
        ) == 1
    engine.dispose()


@pytest.mark.parametrize(
    ("status", "selected_codes", "expected_status"),
    [
        ("waiting_confirmation", ("600000.SH",), "failed"),
        ("waiting_confirmation", (), "failed"),
        (
            "waiting_confirmation",
            ("600001.SH", "600000.SH"),
            "waiting_confirmation",
        ),
        ("data_not_ready", (), "data_not_ready"),
        ("ruler_not_ready", (), "ruler_not_ready"),
    ],
)
def test_waiting_track_outcome_requires_exact_candidate_code_set(
    tmp_path,
    status: str,
    selected_codes: tuple[str, ...],
    expected_status: str,
) -> None:
    from sqlalchemy import func, select

    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
        DiscoveryResult,
        DiscoveryStage,
    )
    from jobs.discovery import (
        TrackExecutionOutcome,
        TrackExecutionResult,
        drain_run_queue,
    )

    engine = init_db(
        tmp_path / f"exact-track-results-{status}-{len(selected_codes)}.db"
    )
    factory = create_session_factory(engine)
    with factory() as session:
        run = _freeze_run(
            session,
            [company("600000.SH"), company("600001.SH")],
        )
        selected = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        sibling = _add_track_stage(
            session,
            run,
            track="deep_value",
            created_at=SNAPSHOT_AT + timedelta(seconds=1),
        )
        common = session.get(
            DiscoveryCommonStage, selected.common_stage_id
        )
        assert common is not None
        session.add(
            DiscoveryCommonResult(
                run_id=run.id,
                attempt=common.attempt,
                code="600001.SH",
                verdict="pass",
                reason=None,
                metrics_json="{}",
                created_at=SNAPSHOT_AT,
            )
        )
        common.total_count = 2
        common.pass_count = 2
        run_id, selected_id, sibling_id = (
            run.id,
            selected.id,
            sibling.id,
        )
        session.commit()

        def execute(context):
            if context.stage_id != selected_id:
                return _passing_track_outcome(context)
            return TrackExecutionOutcome(
                status=status,
                results=tuple(
                    TrackExecutionResult(
                        code=code,
                        verdict="pass",
                        reason=None,
                        metrics_json="{}",
                        rank=None,
                    )
                    for code in selected_codes
                ),
            )

        outcome = drain_run_queue(
            session, run_id, track_executor=execute
        )
        assert outcome.executed == 2
        selected_stage = session.get(DiscoveryStage, selected_id)
        assert selected_stage is not None
        assert selected_stage.status == expected_status
        assert selected_stage.failure_kind == (
            "permanent" if expected_status == "failed" else None
        )
        assert session.get(DiscoveryStage, sibling_id).status == (
            "waiting_confirmation"
        )
        assert session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(
                DiscoveryResult.run_id == run_id,
                DiscoveryResult.track == "quality_discount",
            )
        ) == (
            2 if expected_status == "waiting_confirmation" else 0
        )
        assert session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(
                DiscoveryResult.run_id == run_id,
                DiscoveryResult.track == "deep_value",
            )
        ) == 2
    engine.dispose()


def test_waiting_track_outcome_accepts_empty_exact_candidate_set(
    tmp_path,
) -> None:
    from sqlalchemy import select

    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
        DiscoveryStage,
    )
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "empty-exact-track-results.db")
    factory = create_session_factory(engine)
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        common = session.get(DiscoveryCommonStage, stage.common_stage_id)
        assert common is not None
        row = session.scalar(
            select(DiscoveryCommonResult).where(
                DiscoveryCommonResult.run_id == run.id,
                DiscoveryCommonResult.attempt == common.attempt,
            )
        )
        assert row is not None
        session.delete(row)
        common.total_count = 0
        common.pass_count = 0
        run_id, stage_id = run.id, stage.id
        session.commit()

        outcome = drain_run_queue(
            session,
            run_id,
            track_executor=_passing_track_outcome,
        )
        assert outcome.executed == 1
        persisted = session.get(DiscoveryStage, stage_id)
        assert persisted is not None
        assert (
            persisted.status,
            persisted.total_count,
            persisted.pass_count,
        ) == ("waiting_confirmation", 0, 0)
    engine.dispose()


def test_drain_persists_validated_competition_evidence(tmp_path) -> None:
    from sqlalchemy import select

    from app.discovery_models import DiscoveryEvidence, DiscoveryStage
    from jobs.discovery import (
        TrackExecutionEvidence,
        TrackExecutionOutcome,
        TrackExecutionResult,
        drain_run_queue,
    )

    engine = init_db(tmp_path / "competition-evidence.db")
    factory = create_session_factory(engine)
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        gate = _add_track_stage(
            session,
            run,
            track="quality_discount",
            status="confirmed",
            created_at=SNAPSHOT_AT,
        )
        _add_track_result(session, gate)
        competition = DiscoveryStage(
            run_id=run.id,
            track=gate.track,
            stage="competition",
            attempt=1,
            status="queued",
            total_count=0,
            pass_count=0,
            drop_count=0,
            insufficient_count=0,
            input_fingerprint=run.input_fingerprint,
            ruler_version_id=gate.ruler_version_id,
            algorithm_version=run.algorithm_version,
            common_stage_id=None,
            predecessor_stage_id=gate.id,
            predecessor_stage="track_gate",
            created_at=SNAPSHOT_AT + timedelta(seconds=1),
            updated_at=SNAPSHOT_AT + timedelta(seconds=1),
        )
        session.add(competition)
        session.commit()

        def execute(context):
            assert context.candidate_codes == ("600000.SH",)
            return TrackExecutionOutcome(
                status="waiting_confirmation",
                results=(
                    TrackExecutionResult(
                        code="600000.SH",
                        verdict="pass",
                        reason=None,
                        metrics_json="{}",
                        rank=1,
                    ),
                ),
                evidence=(
                    TrackExecutionEvidence(
                        code="600000.SH",
                        rule_version="rules-v1",
                        kind="quality",
                        rule_id="roe",
                        input_fields_json='{"roe":"12"}',
                        display_text="ROE 通过",
                        as_of="2026-09-18",
                        data_completeness="complete",
                    ),
                ),
            )

        outcome = drain_run_queue(
            session, run.id, track_executor=execute
        )
        assert outcome.executed == 1
        evidence = session.scalar(select(DiscoveryEvidence))
        assert evidence is not None
        assert (
            evidence.competition_stage_id,
            evidence.competition_attempt,
            evidence.ruler_version_id,
            evidence.code,
        ) == (
            competition.id,
            competition.attempt,
            competition.ruler_version_id,
            "600000.SH",
        )
    engine.dispose()


def test_drain_marks_throwing_pure_executor_failed_without_writes(
    tmp_path,
) -> None:
    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from app.models import JobLock
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "track-flush-error.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        session.commit()

        def execute(_context):
            raise RuntimeError("track programmer bug")

        outcome = drain_run_queue(session, run_id, track_executor=execute)
        assert outcome.executed == 1

    with SessionLocal() as verify:
        stage = verify.get(DiscoveryStage, stage_id)
        assert (stage.status, stage.failure_kind, stage.error) == (
            "failed",
            "permanent",
            "track programmer bug",
        )
        assert verify.scalars(select(DiscoveryResult)).all() == []
        lock = verify.get(JobLock, 1)
        assert lock is not None
        assert (lock.job_id, lock.job_type) == (None, None)
    engine.dispose()


def test_drain_integrity_error_rolls_back_and_releases_lock(tmp_path) -> None:
    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from app.models import JobLock
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "track-integrity.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        session.commit()

        def execute(context):
            return _passing_track_outcome(
                context,
                codes=("600000.SH", "600000.SH"),
            )

        outcome = drain_run_queue(session, run_id, track_executor=execute)
        assert outcome.executed == 1

    with SessionLocal() as verify:
        stage = verify.get(DiscoveryStage, stage_id)
        assert (stage.status, stage.failure_kind) == ("failed", "permanent")
        assert verify.scalars(select(DiscoveryResult)).all() == []
        assert verify.get(JobLock, 1).job_id is None
    engine.dispose()


def test_drain_rejects_obsolete_session_executor_signature(tmp_path) -> None:
    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from app.models import JobLock
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "track-commit.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        session.commit()

        def execute(_session, _stage_id):
            raise AssertionError("obsolete executor must not be called")

        outcome = drain_run_queue(session, run_id, track_executor=execute)
        assert outcome.executed == 1

    with SessionLocal() as verify:
        stage = verify.get(DiscoveryStage, stage_id)
        assert stage.status == "failed"
        assert verify.scalars(select(DiscoveryResult)).all() == []
        assert verify.get(JobLock, 1).job_id is None
    engine.dispose()


def test_drain_does_nothing_when_global_lock_is_held(tmp_path) -> None:
    from app.discovery_models import DiscoveryCommonStage
    from jobs.discovery import drain_run_queue
    from jobs.lock import JOB_TYPE_FUNNEL, try_acquire

    engine = init_db(tmp_path / "held.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as setup:
        run = _freeze_run(setup, [company("600000.SH")])
        stage = _add_common_stage(setup, run)
        run_id, stage_id = run.id, stage.id
        setup.commit()
    with SessionLocal() as owner:
        assert try_acquire(owner, 999, JOB_TYPE_FUNNEL)
        owner.commit()
        with SessionLocal() as contender:
            outcome = drain_run_queue(contender, run_id)
            assert not outcome.acquired
            assert outcome.executed == 0
            assert contender.get(DiscoveryCommonStage, stage_id).status == "queued"
        owner.expire_all()
        lock = owner.get(__import__("app.models", fromlist=["JobLock"]).JobLock, 1)
        assert (lock.job_id, lock.job_type) == (999, JOB_TYPE_FUNNEL)
    engine.dispose()


def test_drain_fails_safely_if_track_executor_does_not_advance(tmp_path) -> None:
    from app.models import JobLock
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "stall.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [company("600000.SH")])
        _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id = run.id
        session.commit()
        outcome = drain_run_queue(
            session, run_id, track_executor=lambda _context: None
        )
        assert outcome.executed == 1
        session.expire_all()
        lock = session.get(JobLock, 1)
        assert lock is not None
        assert (lock.job_id, lock.job_type) == (None, None)
    engine.dispose()


def test_startup_recovery_fails_only_running_then_drain_resumes_queued(
    tmp_path,
) -> None:
    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
        DiscoveryStage,
    )
    from app.models import JobLock
    from jobs.discovery import drain_run_queue
    from jobs.lock import JOB_TYPE_DISCOVERY, clear_stale_lock, ensure_lock_row

    engine = init_db(tmp_path / "recovery.db")
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        run = _freeze_run(session, [company("600000.SH")])
        interrupted = _add_common_stage(
            session, run, attempt=1, status="running"
        )
        interrupted.error = "original diagnostic"
        queued = _add_common_stage(
            session,
            run,
            attempt=2,
            status="queued",
            created_at=SNAPSHOT_AT + timedelta(seconds=1),
        )
        terminal = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
            status="confirmed",
        )
        running_track = _add_track_stage(
            session,
            run,
            track="deep_value",
            created_at=SNAPSHOT_AT,
            status="running",
        )
        superseded = _add_track_stage(
            session,
            run,
            track="garp",
            created_at=SNAPSHOT_AT,
            status="running",
            superseded_at=SNAPSHOT_AT,
        )
        lock = ensure_lock_row(session)
        lock.job_id = run.id
        lock.job_type = JOB_TYPE_DISCOVERY
        lock.held_at = SNAPSHOT_AT
        ids = (
            interrupted.id,
            queued.id,
            terminal.id,
            running_track.id,
            superseded.id,
        )
        run_id = run.id
        session.commit()

        clear_stale_lock(session)
        session.expire_all()
        first = session.get(DiscoveryCommonStage, ids[0])
        second = session.get(DiscoveryCommonStage, ids[1])
        done = session.get(DiscoveryStage, ids[2])
        track = session.get(DiscoveryStage, ids[3])
        old = session.get(DiscoveryStage, ids[4])
        assert (first.status, first.failure_kind, first.error) == (
            "failed",
            "transient",
            "original diagnostic",
        )
        assert second.status == "queued"
        assert done.status == "confirmed"
        assert (track.status, track.failure_kind) == ("failed", "transient")
        assert (old.status, old.failure_kind) == ("running", None)
        assert first.updated_at.tzinfo == timezone.utc
        assert session.get(JobLock, 1).job_id is None

        outcome = drain_run_queue(session, run_id)
        assert outcome.executed == 1
        assert session.get(DiscoveryCommonStage, ids[1]).status == (
            "waiting_confirmation"
        )
        assert session.scalar(
            select(func.count())
            .select_from(DiscoveryCommonResult)
            .where(DiscoveryCommonResult.attempt == 2)
        ) == 1
        assert session.get(DiscoveryCommonStage, ids[0]).status == "failed"
    engine.dispose()


@pytest.mark.parametrize(
    ("fault", "metrics", "code", "verdict", "rank"),
    [
        ("metrics", '{"bad":NaN}', "600000.SH", "pass", 1),
        ("rank", "{}", "600000.SH", "pass", 0),
        ("code", "{}", "000001.SZ", "pass", 1),
        ("verdict", "{}", "600000.SH", "maybe", 1),
    ],
)
def test_track_executor_malformed_output_rolls_back_and_fails_permanent(
    tmp_path,
    fault: str,
    metrics: str,
    code: str,
    verdict: str,
    rank: int,
) -> None:
    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / f"malformed-{fault}.db")
    factory = create_session_factory(engine)
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        session.commit()

        def malformed(_context):
            from jobs.discovery import (
                TrackExecutionOutcome,
                TrackExecutionResult,
            )

            return TrackExecutionOutcome(
                status="waiting_confirmation",
                results=(
                    TrackExecutionResult(
                    code=code,
                    verdict=verdict,
                    reason=None,
                    metrics_json=metrics,
                        rank=rank,
                    ),
                ),
            )

        outcome = drain_run_queue(
            session, run_id, track_executor=malformed
        )
        assert outcome.acquired
        failed = session.get(DiscoveryStage, stage_id)
        assert failed is not None
        assert (failed.status, failed.failure_kind) == (
            "failed",
            "permanent",
        )
        assert session.scalar(
            select(func.count()).select_from(DiscoveryResult)
        ) == 0
    engine.dispose()


def test_malformed_pure_outcome_preserves_history_and_continues(
    tmp_path,
) -> None:
    from sqlalchemy import func, select

    from app.discovery_models import (
        DiscoveryCommonStage,
        DiscoveryResult,
        DiscoveryRun,
        DiscoveryStage,
    )
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "pure-executor-history.db")
    factory = create_session_factory(engine)
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        history = _add_track_stage(
            session,
            run,
            track="garp",
            status="confirmed",
            superseded_at=SNAPSHOT_AT,
            created_at=SNAPSHOT_AT - timedelta(minutes=1),
        )
        prior = _add_track_result(session, history)
        selected = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        sibling = _add_track_stage(
            session,
            run,
            track="deep_value",
            created_at=SNAPSHOT_AT + timedelta(seconds=1),
        )
        run_id = run.id
        prior_id = prior.id
        selected_id = selected.id
        sibling_id = sibling.id
        common_id = selected.common_stage_id
        original_fingerprint = run.input_fingerprint
        session.commit()
        historical_stage = tuple(
            getattr(history, column.name)
            for column in history.__table__.columns
        )
        historical_result = tuple(
            getattr(prior, column.name)
            for column in prior.__table__.columns
        )
        common = session.get(DiscoveryCommonStage, common_id)
        assert common is not None
        common_history = tuple(
            getattr(common, column.name)
            for column in common.__table__.columns
        )
        sibling_identity = (
            sibling.run_id,
            sibling.track,
            sibling.stage,
            sibling.attempt,
            sibling.input_fingerprint,
            sibling.ruler_version_id,
            sibling.algorithm_version,
            sibling.common_stage_id,
            sibling.predecessor_stage_id,
        )

        def execute(context):
            if context.stage_id != selected_id:
                return _passing_track_outcome(context)
            return _passing_track_outcome(
                context,
                codes=("000001.SZ",),
            )

        outcome = drain_run_queue(
            session, run_id, track_executor=execute
        )
        assert outcome.executed == 2
        rejected = session.get(DiscoveryStage, selected_id)
        continued = session.get(DiscoveryStage, sibling_id)
        assert rejected is not None
        assert (rejected.status, rejected.failure_kind) == (
            "failed",
            "permanent",
        )
        assert continued is not None
        assert continued.status == "waiting_confirmation"
        assert session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(
                DiscoveryResult.run_id == run_id,
                DiscoveryResult.track == "quality_discount",
            )
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(
                DiscoveryResult.run_id == run_id,
                DiscoveryResult.track == "deep_value",
            )
        ) == 1
        persisted_prior = session.get(DiscoveryResult, prior_id)
        assert persisted_prior is not None
        assert tuple(
            getattr(persisted_prior, column.name)
            for column in persisted_prior.__table__.columns
        ) == historical_result
        persisted_history = session.get(DiscoveryStage, history.id)
        assert persisted_history is not None
        assert tuple(
            getattr(persisted_history, column.name)
            for column in persisted_history.__table__.columns
        ) == historical_stage
        assert session.get(DiscoveryRun, run_id).input_fingerprint == (
            original_fingerprint
        )
        persisted_common = session.get(DiscoveryCommonStage, common_id)
        assert persisted_common is not None
        assert tuple(
            getattr(persisted_common, column.name)
            for column in persisted_common.__table__.columns
        ) == common_history
        assert (
            continued.run_id,
            continued.track,
            continued.stage,
            continued.attempt,
            continued.input_fingerprint,
            continued.ruler_version_id,
            continued.algorithm_version,
            continued.common_stage_id,
            continued.predecessor_stage_id,
        ) == sibling_identity
    engine.dispose()


def test_old_discovery_worker_cannot_commit_or_release_new_lease(
    tmp_path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from app.models import JobLock
    from jobs.discovery import DiscoveryLockLostError, drain_run_queue
    from jobs.lock import acquire_discovery, clear_stale_lock

    engine = init_db(tmp_path / "lease-aba.db")
    factory = create_session_factory(engine)
    entered = Event()
    resume = Event()
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        old_token = acquire_discovery(session, run.id)
        assert old_token
        run_id, stage_id = run.id, stage.id
        session.commit()

    def blocked(context):
        entered.set()
        assert resume.wait(5)
        return _passing_track_outcome(context)

    def work():
        with factory() as session:
            return drain_run_queue(
                session,
                run_id,
                track_executor=blocked,
                lock_already_held=True,
                owner_token=old_token,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(work)
        assert entered.wait(5)
        with factory() as session:
            clear_stale_lock(session)
            new_token = acquire_discovery(session, run_id)
            assert new_token and new_token != old_token
            session.commit()
        resume.set()
        with pytest.raises(DiscoveryLockLostError):
            future.result(timeout=5)

    with factory() as session:
        lock = session.get(JobLock, 1)
        assert lock is not None and lock.owner_token == new_token
        assert session.scalar(
            select(func.count()).select_from(DiscoveryResult)
        ) == 0
        failed = session.get(DiscoveryStage, stage_id)
        assert failed is not None
        assert (failed.status, failed.failure_kind) == (
            "failed",
            "transient",
        )
    engine.dispose()


def test_adopted_lease_is_released_when_adoption_and_first_release_fail(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    from app.discovery_models import DiscoveryStage
    from app.models import JobLock
    from jobs import discovery as discovery_job
    from jobs.lock import acquire_discovery

    engine = init_db(tmp_path / "adoption-finalizer.db")
    factory = create_session_factory(engine)
    with factory() as setup:
        run = _freeze_run(setup, [company("600000.SH")])
        stage = _add_track_stage(
            setup,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        setup.commit()
        token = acquire_discovery(setup, run_id)
        assert token
        setup.commit()

    with factory() as worker:
        original_get = worker.get

        def broken_get(model, ident, *args, **kwargs):
            if model is JobLock:
                raise OperationalError(
                    "SELECT job_lock",
                    {},
                    RuntimeError("database is locked"),
                )
            return original_get(model, ident, *args, **kwargs)

        monkeypatch.setattr(worker, "get", broken_get)
        original_release = discovery_job.release
        release_calls = 0

        def flaky_release(*args, **kwargs):
            nonlocal release_calls
            release_calls += 1
            if release_calls == 1:
                raise OperationalError(
                    "UPDATE job_lock",
                    {},
                    RuntimeError("database is locked"),
                )
            return original_release(*args, **kwargs)

        monkeypatch.setattr(discovery_job, "release", flaky_release)
        with pytest.raises(OperationalError):
            discovery_job.drain_run_queue(
                worker,
                run_id,
                track_executor=lambda *_: None,
                lock_already_held=True,
                owner_token=token,
            )
        assert release_calls == 2

    with factory() as session:
        lock = session.get(JobLock, 1)
        assert lock is not None
        assert (lock.job_id, lock.job_type, lock.owner_token) == (
            None,
            None,
            None,
        )
        unchanged = session.get(DiscoveryStage, stage_id)
        assert unchanged is not None and unchanged.status == "queued"
    engine.dispose()


def test_exclusive_sqlite_adoption_failure_releases_exact_lease(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy.exc import OperationalError

    from app.discovery_models import DiscoveryStage
    from app.models import JobLock
    from jobs import discovery as discovery_job
    from jobs.lock import acquire_discovery

    db_path = tmp_path / "exclusive-adoption-finalizer.db"
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    with factory() as setup:
        run = _freeze_run(setup, [company("600000.SH")])
        stage = _add_track_stage(
            setup,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        setup.commit()
        token = acquire_discovery(setup, run_id)
        assert token
        setup.commit()

    release_failed = Event()
    prepared = Event()
    start = Event()
    release_calls = 0
    original_release = discovery_job.release

    def observed_release(*args, **kwargs):
        nonlocal release_calls
        release_calls += 1
        try:
            return original_release(*args, **kwargs)
        except OperationalError:
            release_failed.set()
            raise

    monkeypatch.setattr(discovery_job, "release", observed_release)

    def work():
        with factory() as worker:
            worker.connection().exec_driver_sql("PRAGMA busy_timeout=0")
            prepared.set()
            assert start.wait(5)
            return discovery_job.drain_run_queue(
                worker,
                run_id,
                track_executor=lambda _context: None,
                lock_already_held=True,
                owner_token=token,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(work)
        assert prepared.wait(5)
        blocker = sqlite3.connect(db_path)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            start.set()
            assert release_failed.wait(10)
            blocker.rollback()
            with pytest.raises(OperationalError):
                future.result(timeout=10)
        finally:
            blocker.close()
    assert release_calls == 2

    with factory() as session:
        lock = session.get(JobLock, 1)
        assert lock is not None
        assert (lock.job_id, lock.job_type, lock.owner_token) == (
            None,
            None,
            None,
        )
        unchanged = session.get(DiscoveryStage, stage_id)
        assert unchanged is not None and unchanged.status == "queued"
    engine.dispose()


def test_pure_executor_has_no_database_handle_and_tracks_continue(
    tmp_path,
) -> None:
    from sqlalchemy import func, select

    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from app.models import JobLock
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "connection-commit-seal.db")
    factory = create_session_factory(engine)
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        first = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        second = _add_track_stage(
            session,
            run,
            track="deep_value",
            created_at=SNAPSHOT_AT + timedelta(seconds=1),
        )
        run_id, first_id, second_id = run.id, first.id, second.id
        session.commit()

        def execute(context):
            assert not hasattr(context, "commit")
            assert not hasattr(context, "connection")
            assert not hasattr(context, "get")
            if context.stage_id == first_id:
                raise RuntimeError("pure executor failed")
            return _passing_track_outcome(context)

        outcome = drain_run_queue(
            session, run_id, track_executor=execute
        )
        assert outcome.executed == 2
        assert session.get(DiscoveryStage, first_id).status == "failed"
        assert session.get(DiscoveryStage, second_id).status == (
            "waiting_confirmation"
        )
        assert session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(DiscoveryResult.track == "quality_discount")
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(DiscoveryResult)
            .where(DiscoveryResult.track == "deep_value")
        ) == 1
        lock = session.get(JobLock, 1)
        assert lock is not None and lock.job_id is None
    engine.dispose()


def test_china_week_key_flips_at_monday_midnight_utc_plus_8() -> None:
    from discovery.constants import china_week_key

    assert china_week_key(
        datetime(2026, 9, 20, 15, 59, 59, tzinfo=timezone.utc)
    ) == "2026-W38"
    assert china_week_key(
        datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    ) == "2026-W39"


def test_startup_recovery_preserves_prior_week_running_stages(
    tmp_path,
) -> None:
    from app.discovery_models import DiscoveryCommonStage, DiscoveryStage
    from app.models import JobLock
    from jobs.lock import JOB_TYPE_DISCOVERY, clear_stale_lock, ensure_lock_row

    engine = init_db(tmp_path / "week-aware-recovery.db")
    factory = create_session_factory(engine)
    now = SNAPSHOT_AT + timedelta(days=7)
    with factory() as session:
        old_run = _freeze_run(
            session, [company("600000.SH")], week_key="2026-W38"
        )
        current_run = _freeze_run(
            session, [company("600000.SH")], week_key="2026-W39"
        )
        old_common = _add_common_stage(
            session, old_run, status="running"
        )
        old_track = _add_track_stage(
            session,
            old_run,
            track="quality_discount",
            status="running",
            created_at=SNAPSHOT_AT,
        )
        current_common = _add_common_stage(
            session, current_run, status="running"
        )
        current_track = _add_track_stage(
            session,
            current_run,
            track="quality_discount",
            status="running",
            created_at=SNAPSHOT_AT,
        )
        lock = ensure_lock_row(session)
        lock.job_id = old_run.id
        lock.job_type = JOB_TYPE_DISCOVERY
        lock.owner_token = "stale"
        session.commit()
        old_common_before = tuple(
            getattr(old_common, column.name)
            for column in old_common.__table__.columns
        )
        old_track_before = tuple(
            getattr(old_track, column.name)
            for column in old_track.__table__.columns
        )
        ids = (
            old_common.id,
            old_track.id,
            current_common.id,
            current_track.id,
        )

        clear_stale_lock(session, now_provider=lambda: now)
        session.expire_all()

        preserved_common = session.get(DiscoveryCommonStage, ids[0])
        preserved_track = session.get(DiscoveryStage, ids[1])
        assert tuple(
            getattr(preserved_common, column.name)
            for column in preserved_common.__table__.columns
        ) == old_common_before
        assert tuple(
            getattr(preserved_track, column.name)
            for column in preserved_track.__table__.columns
        ) == old_track_before
        assert (
            session.get(DiscoveryCommonStage, ids[2]).status,
            session.get(DiscoveryCommonStage, ids[2]).failure_kind,
        ) == ("failed", "transient")
        assert (
            session.get(DiscoveryStage, ids[3]).status,
            session.get(DiscoveryStage, ids[3]).failure_kind,
        ) == ("failed", "transient")
        assert session.get(JobLock, 1).job_id is None
    engine.dispose()


def test_common_rollover_after_classification_commits_only_running_claim(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from app.discovery_models import (
        DiscoveryCommonResult,
        DiscoveryCommonStage,
        DiscoveryRun,
    )
    from app.main import create_app
    from app.models import JobLock
    from jobs import discovery as discovery_job
    from jobs.discovery import drain_run_queue
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "common-rollover.db"
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    now = [SNAPSHOT_AT]
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_common_stage(session, run)
        run_id, stage_id = run.id, stage.id
        session.commit()
        run_before = tuple(
            getattr(run, column.name)
            for column in run.__table__.columns
        )
        original = discovery_job.classify_common

        def rollover(item, dates):
            result = original(item, dates)
            now[0] += timedelta(days=7)
            return result

        monkeypatch.setattr(discovery_job, "classify_common", rollover)
        outcome = drain_run_queue(
            session,
            run_id,
            now_provider=lambda: now[0],
        )
        assert outcome.acquired
        assert session.get(DiscoveryCommonStage, stage_id).status == "running"
        assert session.scalar(
            select(func.count()).select_from(DiscoveryCommonResult)
        ) == 0
        assert tuple(
            getattr(session.get(DiscoveryRun, run_id), column.name)
            for column in DiscoveryRun.__table__.columns
        ) == run_before
        assert session.get(JobLock, 1).job_id is None
    engine.dispose()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        discovery_clock=lambda: now[0],
    )
    with TestClient(app) as client:
        history = client.get(
            f"/api/discoveries/{run_id}/stages/safety"
        )
        assert history.status_code == 200
        assert history.json()["current"]["status"] == "running"


def test_track_rollover_after_executor_commits_only_running_claim(
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from app.discovery_models import DiscoveryResult, DiscoveryRun, DiscoveryStage
    from app.main import create_app
    from app.models import JobLock
    from jobs.discovery import drain_run_queue
    from market.quotes import FakeQuoteClient

    db_path = tmp_path / "track-rollover.db"
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    now = [SNAPSHOT_AT]
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        session.commit()
        run_before = tuple(
            getattr(run, column.name)
            for column in run.__table__.columns
        )

        def rollover(context):
            now[0] += timedelta(days=7)
            return _passing_track_outcome(context)

        outcome = drain_run_queue(
            session,
            run_id,
            track_executor=rollover,
            now_provider=lambda: now[0],
        )
        assert outcome.acquired
        assert session.get(DiscoveryStage, stage_id).status == "running"
        assert session.scalar(
            select(func.count()).select_from(DiscoveryResult)
        ) == 0
        assert tuple(
            getattr(session.get(DiscoveryRun, run_id), column.name)
            for column in DiscoveryRun.__table__.columns
        ) == run_before
        assert session.get(JobLock, 1).job_id is None
    engine.dispose()

    app = create_app(
        db_path=db_path,
        quote_client=FakeQuoteClient(),
        env_path=tmp_path / ".env",
        discovery_clock=lambda: now[0],
    )
    with TestClient(app) as client:
        history = client.get(
            f"/api/discoveries/{run_id}/tracks/quality_discount"
            "/stages/track_gate"
        )
        assert history.status_code == 200
        assert history.json()["current"]["status"] == "running"


def test_common_failure_after_rollover_does_not_persist_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.discovery_models import DiscoveryCommonStage
    from app.models import JobLock
    from jobs import discovery as discovery_job
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "common-rollover-failure.db")
    factory = create_session_factory(engine)
    now = [SNAPSHOT_AT]
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_common_stage(session, run)
        run_id, stage_id = run.id, stage.id
        session.commit()

        def rollover_then_fail(*_args):
            now[0] += timedelta(days=7)
            raise RuntimeError("late common failure")

        monkeypatch.setattr(
            discovery_job,
            "classify_common",
            rollover_then_fail,
        )
        outcome = drain_run_queue(
            session,
            run_id,
            now_provider=lambda: now[0],
        )
        assert outcome.acquired
        persisted = session.get(DiscoveryCommonStage, stage_id)
        assert (
            persisted.status,
            persisted.error,
            persisted.failure_kind,
        ) == ("running", None, None)
        assert session.get(JobLock, 1).job_id is None
    engine.dispose()


def test_track_failure_after_rollover_does_not_persist_failure(
    tmp_path,
) -> None:
    from app.discovery_models import DiscoveryStage
    from app.models import JobLock
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / "track-rollover-failure.db")
    factory = create_session_factory(engine)
    now = [SNAPSHOT_AT]
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = _add_track_stage(
            session,
            run,
            track="quality_discount",
            created_at=SNAPSHOT_AT,
        )
        run_id, stage_id = run.id, stage.id
        session.commit()

        def rollover_then_fail(_context):
            now[0] += timedelta(days=7)
            raise RuntimeError("late track failure")

        with pytest.raises(RuntimeError, match="late track failure"):
            drain_run_queue(
                session,
                run_id,
                track_executor=rollover_then_fail,
                now_provider=lambda: now[0],
            )
        persisted = session.get(DiscoveryStage, stage_id)
        assert (
            persisted.status,
            persisted.error,
            persisted.failure_kind,
        ) == ("running", None, None)
        assert session.get(JobLock, 1).job_id is None
    engine.dispose()


@pytest.mark.parametrize("kind", ["common", "track"])
def test_prior_week_queued_stage_is_not_claimed(
    tmp_path, kind: str
) -> None:
    from app.discovery_models import DiscoveryCommonStage, DiscoveryStage
    from app.models import JobLock
    from jobs.discovery import drain_run_queue

    engine = init_db(tmp_path / f"prior-week-{kind}.db")
    factory = create_session_factory(engine)
    now = SNAPSHOT_AT + timedelta(days=7)
    with factory() as session:
        run = _freeze_run(session, [company("600000.SH")])
        stage = (
            _add_common_stage(session, run)
            if kind == "common"
            else _add_track_stage(
                session,
                run,
                track="quality_discount",
                created_at=SNAPSHOT_AT,
            )
        )
        run_id, stage_id = run.id, stage.id
        session.commit()

        outcome = drain_run_queue(
            session,
            run_id,
            track_executor=_passing_track_outcome,
            now_provider=lambda: now,
        )
        assert outcome.executed == 0
        model = (
            DiscoveryCommonStage
            if kind == "common"
            else DiscoveryStage
        )
        assert session.get(model, stage_id).status == "queued"
        assert session.get(JobLock, 1).job_id is None
    engine.dispose()
