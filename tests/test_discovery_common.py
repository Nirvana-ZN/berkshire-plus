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
    from app.discovery_models import DiscoveryStage

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
        common = _add_common_stage(session, run, created_at=SNAPSHOT_AT)
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
        real_classifier = job.classify_common

        def recording_classifier(item, dates):
            order.append(("common", common.id))
            return real_classifier(item, dates)

        def track_executor(worker, stage_id):
            from app.discovery_models import DiscoveryStage

            order.append(("track", stage_id))
            stage = worker.get(DiscoveryStage, stage_id)
            assert stage is not None
            stage.status = "waiting_confirmation"
            stage.updated_at = datetime.now(timezone.utc)

        monkeypatch.setattr(job, "classify_common", recording_classifier)
        outcome = job.drain_run_queue(
            session, run_id, track_executor=track_executor
        )
        assert outcome.acquired
        assert outcome.executed == 3
        assert order == [
            ("track", early.id),
            ("common", common.id),
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
    return result


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

        def execute(worker, selected_id):
            selected = worker.get(DiscoveryStage, selected_id)
            assert selected is not None
            _add_track_result(worker, selected)
            selected.status = "waiting_confirmation"
            selected.updated_at = datetime.now(timezone.utc)

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


def test_drain_rolls_back_flushed_track_writes_then_marks_failed(
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

        def execute(worker, selected_id):
            selected = worker.get(DiscoveryStage, selected_id)
            assert selected is not None
            _add_track_result(worker, selected)
            selected.status = "waiting_confirmation"
            worker.flush()
            raise RuntimeError("track programmer bug")

        with pytest.raises(RuntimeError, match="track programmer bug"):
            drain_run_queue(session, run_id, track_executor=execute)

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
    from sqlalchemy.exc import IntegrityError

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

        def execute(worker, selected_id):
            selected = worker.get(DiscoveryStage, selected_id)
            assert selected is not None
            _add_track_result(worker, selected)
            _add_track_result(worker, selected)
            selected.status = "waiting_confirmation"
            worker.flush()

        with pytest.raises(IntegrityError):
            drain_run_queue(session, run_id, track_executor=execute)

    with SessionLocal() as verify:
        stage = verify.get(DiscoveryStage, stage_id)
        assert (stage.status, stage.failure_kind) == ("failed", "permanent")
        assert verify.scalars(select(DiscoveryResult)).all() == []
        assert verify.get(JobLock, 1).job_id is None
    engine.dispose()


def test_drain_rejects_track_callback_commit(tmp_path) -> None:
    from app.discovery_models import DiscoveryResult, DiscoveryStage
    from app.models import JobLock
    from jobs.discovery import TrackExecutorContractError, drain_run_queue

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

        def execute(worker, selected_id):
            selected = worker.get(DiscoveryStage, selected_id)
            assert selected is not None
            _add_track_result(worker, selected)
            selected.status = "waiting_confirmation"
            worker.commit()

        with pytest.raises(TrackExecutorContractError):
            drain_run_queue(session, run_id, track_executor=execute)

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
    from jobs.discovery import QueueStalledError, drain_run_queue

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
        with pytest.raises(QueueStalledError):
            drain_run_queue(
                session, run_id, track_executor=lambda _session, _id: None
            )
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
