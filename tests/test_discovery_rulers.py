from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

from app.db import create_session_factory, init_db
from app.models import RulerVersion, Setting
from discovery.builds import BUILD_VERSION
HISTORICAL_BUILD_VERSION = 'discovery-v7'
from discovery.rulers import (
    DISCOVERY_PAYLOAD_SCOPE,
    TrackRuler,
    activate,
    canonical_payload,
    latest,
    payload_digest,
    readiness,
    validate_ruler_version,
)
from discovery.snapshot import (
    canonical_json,
    freeze_discovery_ruler_payloads,
    resolve_discovery_rulers,
)
from discovery.tracks import (
    DEEP_VALUE_SCOPE,
    GARP_SCOPE,
    QUALITY_DISCOUNT_SCOPE,
)
from screen.ruler import GATE_ACK_KEY, current_hash


def _payload(track: str = "quality_discount", *, build_version=BUILD_VERSION) -> dict[str, object]:
    if build_version == "discovery-v8":
        from tests.test_selection_v8 import candidate
        return json.loads(canonical_payload(candidate(track)))
    if track == "quality_discount":
        from tests.test_discovery_tracks_quality import quality_payload

        return quality_payload()
    if track == "deep_value":
        from tests.test_discovery_tracks_deep_value import deep_value_payload

        return deep_value_payload()
    return {
        "scope": DISCOVERY_PAYLOAD_SCOPE,
        "track": track,
        "fields": ["roe", "code"],
        "formulas": {"roe": "mean(annual.roe)"},
        "thresholds": {"roe_floor": "12"},
        "min_observations": {"roe": 10},
        "missing_policy": {
            "roe": "insufficient",
            "code": "insufficient",
        },
        "sector_branches": {
            "financial": {"fields": ["roe"]},
            "non_financial": {"fields": ["roe"]},
        },
        "order": [
            ["roe", "desc", "nulls_last"],
            ["code", "asc", "nulls_last"],
        ],
        "industry_version": "citic-v1",
        "algorithm_version": build_version,
    }


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_legacy_ruler_scope_migrates_idempotently_and_keeps_default(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    legacy = create_engine(f"sqlite:///{db_path.as_posix()}")
    with legacy.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, "
                "name VARCHAR NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE ruler_versions ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "digest VARCHAR NOT NULL, reason VARCHAR NOT NULL, "
                "payload_json TEXT NOT NULL, activated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(text("INSERT INTO users VALUES (1, '本机用户')"))
        conn.execute(
            text(
                "INSERT INTO ruler_versions VALUES "
                "(1, 1, 'legacy', '旧尺子', '{}', CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO ruler_versions VALUES "
                "(2, 1, 'legacy', '旧尺子', '{}', CURRENT_TIMESTAMP)"
            )
        )
    legacy.dispose()

    init_db(db_path).dispose()
    engine = init_db(db_path)
    with engine.begin() as conn:
        assert conn.execute(
            text(
                "SELECT id, scope FROM ruler_versions "
                "WHERE digest = 'legacy' ORDER BY id"
            )
        ).all() == [(1, "funnel"), (2, "funnel")]
        partial_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'ux_ruler_versions_user_scope_digest'"
            )
        ).scalar_one()
        assert "WHERE id > 2" in partial_sql
        conn.execute(
            text(
                "INSERT INTO ruler_versions "
                "(user_id, digest, reason, payload_json, activated_at) "
                "VALUES (1, 'raw', '原始插入', '{}', CURRENT_TIMESTAMP)"
            )
        )
        assert conn.execute(
            text("SELECT scope FROM ruler_versions WHERE digest = 'raw'")
        ).scalar_one() == "funnel"
        indexes = {
            row[1]: bool(row[2])
            for row in conn.execute(text("PRAGMA index_list(ruler_versions)"))
        }
        assert indexes["ix_ruler_versions_user_scope_id"] is False
        assert indexes["ux_ruler_versions_user_scope_digest"] is True
        assert conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'ux_ruler_versions_user_scope_digest'"
            )
        ).scalar_one() == partial_sql
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ruler_versions "
                    "(user_id, digest, reason, payload_json, activated_at) "
                    "VALUES (1, 'raw', '重复', '{}', CURRENT_TIMESTAMP)"
                )
            )
    engine.dispose()
    engine = init_db(db_path)
    with engine.begin() as conn:
        assert conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'ux_ruler_versions_user_scope_digest'"
            )
        ).scalar_one() == partial_sql


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_migration_preserves_referenced_duplicate_ruler_ids(
    tmp_path: Path,
) -> None:
    from app import discovery_models as _discovery_models  # noqa: F401
    from app.db import Base, create_db_engine
    from app.discovery_models import (
        DiscoveryCommonStage,
        DiscoveryEvidence,
        DiscoveryInputSnapshot,
        DiscoveryRun,
        DiscoveryStage,
    )
    from app.models import User
    from discovery.snapshot import load_historical_replay

    db_path = tmp_path / "referenced-duplicates.db"
    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    payload = _payload(build_version=HISTORICAL_BUILD_VERSION)
    payload_json = canonical_payload(payload)
    digest = payload_digest(payload)
    frozen_json = canonical_json(
        {
            "quality_discount": {
                "id": 41,
                "payload": payload,
                "sha256": digest,
            },
            "deep_value": None,
            "garp": None,
        }
    )
    now = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    with factory() as session:
        session.add(User(id=1, name="本机用户"))
        session.flush()
        session.add_all(
            [
                RulerVersion(
                    id=ruler_id,
                    user_id=1,
                    scope=QUALITY_DISCOUNT_SCOPE,
                    digest=digest,
                    reason="legacy duplicate",
                    payload_json=payload_json,
                    activated_at=now,
                )
                for ruler_id in (41, 42)
            ]
        )
        session.flush()
        run = DiscoveryRun(
            id=7,
            user_id=1,
            week_key="2026-W38",
            current=False,
            status="completed",
            created_at=now,
            updated_at=now,
            snapshot_at=now,
            data_version="data-v1",
            financial_version="financial-v1",
            valuation_version="valuation-v1",
            quality_discount_ruler_version_id=41,
            deep_value_ruler_version_id=None,
            garp_ruler_version_id=None,
            algorithm_version=HISTORICAL_BUILD_VERSION,
            trading_dates_json='{"SH":["2026-09-17"]}',
            ruler_payloads_json=frozen_json,
            input_fingerprint="run-fingerprint",
            error=None,
            replay_of_run_id=None,
        )
        session.add(run)
        session.flush()
        session.add(
            DiscoveryInputSnapshot(
                run_id=7,
                code="600000.SH",
                name="浦发银行",
                industry="银行",
                security_status="{}",
                fields_json="{}",
                sources_json="{}",
                as_of_json="{}",
                data_version="data-v1",
                financial_version="financial-v1",
                valuation_version="valuation-v1",
                ruler_versions_json=(
                    '{"deep_value":null,"garp":null,'
                    '"quality_discount":41}'
                ),
                algorithm_version=HISTORICAL_BUILD_VERSION,
                input_fingerprint="row-fingerprint",
                snapshot_at=now,
            )
        )
        session.flush()
        common = DiscoveryCommonStage(
            run_id=7,
            attempt=1,
            status="confirmed",
            total_count=1,
            pass_count=1,
            drop_count=0,
            insufficient_count=0,
            error=None,
            confirmed_at=now,
            input_fingerprint="run-fingerprint",
            algorithm_version=HISTORICAL_BUILD_VERSION,
            retry_of_stage_id=None,
            failure_kind=None,
            superseded_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(common)
        session.flush()
        gate = DiscoveryStage(
            run_id=7,
            track="quality_discount",
            stage="track_gate",
            attempt=1,
            status="confirmed",
            total_count=1,
            pass_count=1,
            drop_count=0,
            insufficient_count=0,
            error=None,
            confirmed_at=now,
            input_fingerprint="run-fingerprint",
            ruler_version_id=41,
            algorithm_version=HISTORICAL_BUILD_VERSION,
            common_stage_id=common.id,
            predecessor_stage_id=None,
            predecessor_stage=None,
            retry_of_stage_id=None,
            reopen_of_stage_id=None,
            failure_kind=None,
            superseded_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(gate)
        session.flush()
        competition = DiscoveryStage(
            run_id=7,
            track="quality_discount",
            stage="competition",
            attempt=1,
            status="confirmed",
            total_count=1,
            pass_count=1,
            drop_count=0,
            insufficient_count=0,
            error=None,
            confirmed_at=now,
            input_fingerprint="run-fingerprint",
            ruler_version_id=41,
            algorithm_version=HISTORICAL_BUILD_VERSION,
            common_stage_id=None,
            predecessor_stage_id=gate.id,
            predecessor_stage="track_gate",
            retry_of_stage_id=None,
            reopen_of_stage_id=None,
            failure_kind=None,
            superseded_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(competition)
        session.flush()
        session.add(
            DiscoveryEvidence(
                run_id=7,
                track="quality_discount",
                competition_attempt=1,
                competition_stage_id=competition.id,
                stage="competition",
                code="600000.SH",
                ruler_version_id=41,
                rule_version="legacy-rule",
                kind="evidence",
                rule_id="legacy",
                input_fields_json='["roe"]',
                display_text="legacy evidence",
                as_of="2026-09-17",
                data_completeness="complete",
                created_at=now,
            )
        )
        session.commit()

    with engine.begin() as conn:
        before_rows = conn.execute(
            text(
                "SELECT id, user_id, scope, digest, reason, payload_json, "
                "activated_at FROM ruler_versions ORDER BY id"
            )
        ).all()
    engine.dispose()

    init_db(db_path).dispose()
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    with engine.begin() as conn:
        assert conn.execute(
            text(
                "SELECT id, user_id, scope, digest, reason, payload_json, "
                "activated_at FROM ruler_versions WHERE id IN (41, 42) "
                "ORDER BY id"
            )
        ).all() == before_rows
        index_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'ux_ruler_versions_user_scope_digest'"
            )
        ).scalar_one()
        assert "WHERE id > 42" in index_sql

    with factory() as session:
        row = activate(session, payload, "must reuse newest legacy")
        assert row.id == 42
        session.commit()
        assert session.scalars(
            select(RulerVersion).where(
                RulerVersion.scope == QUALITY_DISCOUNT_SCOPE,
                RulerVersion.digest == digest,
            )
        ).all()[0].id == 41
        assert session.scalar(
            select(func.count())
            .select_from(RulerVersion)
            .where(
                RulerVersion.scope == QUALITY_DISCOUNT_SCOPE,
                RulerVersion.digest == digest,
            )
        ) == 2
        assert resolve_discovery_rulers(session, algorithm_version=HISTORICAL_BUILD_VERSION)["quality_discount"] == 42
        frozen = json.loads(
            freeze_discovery_ruler_payloads(
                session,
                {
                    "quality_discount": 41,
                    "deep_value": None,
                    "garp": None,
                },
                HISTORICAL_BUILD_VERSION,
            )
        )
        assert frozen["quality_discount"]["id"] == 41
        replay = load_historical_replay(session, 7)
        assert replay.track_stages[0]["ruler_version_id"] == 41
        assert replay.evidence[0]["ruler_version_id"] == 41
        run = session.get(DiscoveryRun, 7)
        snapshot = session.scalars(
            select(DiscoveryInputSnapshot).where(
                DiscoveryInputSnapshot.run_id == 7
            )
        ).one()
        assert run is not None
        assert run.ruler_payloads_json == frozen_json
        assert run.input_fingerprint == "run-fingerprint"
        assert snapshot.ruler_versions_json.endswith(
            '"quality_discount":41}'
        )
        assert snapshot.input_fingerprint == "row-fingerprint"


def test_fresh_init_does_not_activate_discovery_rulers(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "app.db")
    factory = create_session_factory(engine)
    with engine.begin() as conn:
        index_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'ux_ruler_versions_user_scope_digest'"
            )
        ).scalar_one()
        assert "WHERE id > 0" in index_sql
    with factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(RulerVersion)
            .where(RulerVersion.scope.like("discovery:%"))
        ) == 0
        assert readiness(session, "quality_discount")["status"] == "ruler_not_ready"


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_quality_semantics_guard_activation_resolution_and_freeze(
    tmp_path: Path,
) -> None:
    from tests.test_discovery_tracks_quality import quality_payload

    engine = init_db(tmp_path / "semantic-quality.db")
    factory = create_session_factory(engine)
    invalid = quality_payload(build_version=HISTORICAL_BUILD_VERSION)
    invalid["thresholds"].pop("roe_mean_min")
    payload_json = canonical_payload(invalid)
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    with factory() as session:
        with pytest.raises(ValueError, match="quality"):
            activate(session, invalid, "must never persist")
        session.rollback()
        assert session.scalar(
            select(func.count())
            .select_from(RulerVersion)
            .where(RulerVersion.scope == QUALITY_DISCOUNT_SCOPE)
        ) == 0

        raw = RulerVersion(
            user_id=1,
            scope=QUALITY_DISCOUNT_SCOPE,
            digest=digest,
            reason="raw semantic corruption",
            payload_json=payload_json,
            activated_at=datetime.now(timezone.utc),
        )
        session.add(raw)
        session.flush()
        raw_id = raw.id
        with pytest.raises(ValueError, match="integrity"):
            validate_ruler_version(raw, expected_track="quality_discount")
        assert latest(session, "quality_discount") is None
        assert readiness(session, "quality_discount")["status"] == "ruler_not_ready"
        assert resolve_discovery_rulers(session)["quality_discount"] is None
        with pytest.raises(ValueError, match="incompatible"):
            freeze_discovery_ruler_payloads(
                session,
                {
                    "quality_discount": raw_id,
                    "deep_value": None,
                    "garp": None,
                },
                HISTORICAL_BUILD_VERSION,
            )


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_legacy_funnel_row_with_spoofed_payload_is_never_discovery(
    tmp_path: Path,
) -> None:
    engine = init_db(tmp_path / "app.db")
    factory = create_session_factory(engine)
    with factory() as session:
        spoof = RulerVersion(
            user_id=1,
            scope="funnel",
            digest="spoof",
            reason="legacy payload spoof",
            payload_json=canonical_json(
                {
                    "scope": "weekly-discovery",
                    "track": "quality_discount",
                    "algorithm_version": HISTORICAL_BUILD_VERSION,
                }
            ),
            activated_at=datetime.now(timezone.utc),
        )
        session.add(spoof)
        session.flush()
        assert resolve_discovery_rulers(session)["quality_discount"] is None
        with pytest.raises(ValueError, match="incompatible"):
            freeze_discovery_ruler_payloads(
                session,
                {
                    "quality_discount": spoof.id,
                    "deep_value": None,
                    "garp": None,
                },
                HISTORICAL_BUILD_VERSION,
            )


@pytest.mark.parametrize(
    "case",
    [
        "minimal",
        "wrong_digest",
        "noncanonical",
        "wrong_scope",
        "wrong_track",
        "wrong_build",
        "missing:fields",
        "missing:formulas",
        "missing:thresholds",
        "missing:min_observations",
        "missing:missing_policy",
        "missing:sector_branches",
        "missing:order",
        "missing:industry_version",
        "missing:algorithm_version",
    ],
)
@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_strict_row_parser_blocks_invalid_resolver_and_freeze(
    tmp_path: Path,
    case: str,
) -> None:
    engine = init_db(tmp_path / f"{case.replace(':', '-')}.db")
    factory = create_session_factory(engine)
    payload = _payload(build_version=HISTORICAL_BUILD_VERSION)
    scope = QUALITY_DISCOUNT_SCOPE
    if case == "minimal":
        payload = {
            "scope": DISCOVERY_PAYLOAD_SCOPE,
            "track": "quality_discount",
            "algorithm_version": HISTORICAL_BUILD_VERSION,
        }
    elif case == "wrong_scope":
        scope = "funnel"
    elif case == "wrong_track":
        payload["track"] = "deep_value"
    elif case == "wrong_build":
        payload["algorithm_version"] = "retired-build"
    elif case.startswith("missing:"):
        payload.pop(case.split(":", 1)[1])

    valid_text = canonical_payload(_payload(build_version=HISTORICAL_BUILD_VERSION))
    payload_json = canonical_json(payload)
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if case == "wrong_digest":
        digest = "0" * 64
    elif case == "noncanonical":
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(valid_text.encode("utf-8")).hexdigest()

    with factory() as session:
        row = RulerVersion(
            user_id=1,
            scope=scope,
            digest=digest,
            reason=case,
            payload_json=payload_json,
            activated_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        with pytest.raises((TypeError, ValueError)):
            validate_ruler_version(
                row,
                expected_track="quality_discount",
                expected_algorithm_version=HISTORICAL_BUILD_VERSION,
            )
        assert resolve_discovery_rulers(session) == {
            "quality_discount": None,
            "deep_value": None,
            "garp": None,
        }
        with pytest.raises(ValueError):
            freeze_discovery_ruler_payloads(
                session,
                {
                    "quality_discount": row.id,
                    "deep_value": None,
                    "garp": None,
                },
                HISTORICAL_BUILD_VERSION,
            )


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_scoped_activation_is_independent_and_funnel_is_unchanged(
    tmp_path: Path,
) -> None:
    engine = init_db(tmp_path / "app.db")
    factory = create_session_factory(engine)
    with factory() as session:
        funnel = session.scalars(
            select(RulerVersion).where(RulerVersion.scope == "funnel")
        ).one()
        ack = session.get(Setting, GATE_ACK_KEY)
        assert ack is not None and ack.value == str(funnel.id)

        quality = activate(session, _payload(build_version=HISTORICAL_BUILD_VERSION), "calibrated quality")
        same = activate(session, _payload(build_version=HISTORICAL_BUILD_VERSION), "duplicate request")
        deep = activate(
            session,
            _payload("deep_value", build_version=HISTORICAL_BUILD_VERSION),
            "calibrated deep value",
        )
        session.commit()

        assert quality.id == same.id
        assert latest(session, "quality_discount").id == quality.id
        assert latest(session, "deep_value").id == deep.id
        assert latest(session, "garp") is None
        assert quality.scope == QUALITY_DISCOUNT_SCOPE
        assert deep.scope == DEEP_VALUE_SCOPE
        assert GARP_SCOPE == "discovery:garp"
        assert session.get(type(ack), GATE_ACK_KEY).value == str(funnel.id)
        current_funnel = session.scalars(
            select(RulerVersion)
            .where(
                RulerVersion.user_id == 1,
                RulerVersion.scope == "funnel",
            )
            .order_by(RulerVersion.id.desc())
        ).first()
        assert current_funnel is not None
        assert current_funnel.digest == current_hash()


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_discovery_activation_is_atomic_across_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "activation-race.db"
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    barrier = threading.Barrier(2)

    def activate_once(reason: str) -> int:
        with factory() as session:
            barrier.wait()
            row = activate(session, _payload(build_version=HISTORICAL_BUILD_VERSION), reason)
            session.commit()
            return row.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(activate_once, ("race one", "race two")))

    assert ids[0] == ids[1]
    with factory() as session:
        rows = session.scalars(
            select(RulerVersion).where(
                RulerVersion.scope == QUALITY_DISCOUNT_SCOPE
            )
        ).all()
        assert [row.id for row in rows] == [ids[0]]


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_ruler_ledger_rows_are_immutable(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "immutable.db")
    factory = create_session_factory(engine)
    with factory() as session:
        row = activate(session, _payload(build_version=HISTORICAL_BUILD_VERSION), "immutable")
        session.commit()
        row_id = row.id

    columns = {
        "id": str(row_id + 1000),
        "user_id": "2",
        "scope": "'discovery:deep_value'",
        "digest": "'changed'",
        "payload_json": "'{}'",
        "reason": "'changed'",
        "activated_at": "'2030-01-01 00:00:00'",
    }
    for column, value in columns.items():
        with pytest.raises(IntegrityError, match="immutable"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"UPDATE ruler_versions SET {column} = {value} "
                        "WHERE id = :id"
                    ),
                    {"id": row_id},
                )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ruler_versions SET payload_json = payload_json "
                "WHERE id = :id"
            ),
            {"id": row_id},
        )

    with factory() as session:
        row = session.get(RulerVersion, row_id)
        assert row is not None
        row.digest = "orm-corruption"
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_ruler_ledger_rejects_all_deletes_and_keeps_run_semantics(
    tmp_path: Path,
) -> None:
    from app.discovery_models import DiscoveryRun
    from app.models import User

    db_path = tmp_path / "append-only.db"
    engine = init_db(db_path)
    factory = create_session_factory(engine)
    now = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    with factory() as session:
        quality = activate(session, _payload(build_version=HISTORICAL_BUILD_VERSION), "referenced")
        deep = activate(session, _payload("deep_value", build_version=HISTORICAL_BUILD_VERSION), "unreferenced")
        funnel = session.scalars(
            select(RulerVersion).where(RulerVersion.scope == "funnel")
        ).one()
        session.add(
            DiscoveryRun(
                id=77,
                user_id=1,
                week_key="2026-W39",
                current=False,
                status="completed",
                created_at=now,
                updated_at=now,
                snapshot_at=now,
                data_version="data-v1",
                financial_version="financial-v1",
                valuation_version="valuation-v1",
                quality_discount_ruler_version_id=quality.id,
                deep_value_ruler_version_id=None,
                garp_ruler_version_id=None,
                algorithm_version=HISTORICAL_BUILD_VERSION,
                trading_dates_json=None,
                ruler_payloads_json=None,
                input_fingerprint=None,
                error=None,
                replay_of_run_id=None,
            )
        )
        session.commit()
        ruler_ids = (funnel.id, quality.id, deep.id)
        quality_id = quality.id

    engine.dispose()
    init_db(db_path).dispose()
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    for ruler_id in ruler_ids:
        with pytest.raises(IntegrityError, match="immutable"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE ruler_versions SET id = id + 10000 "
                        "WHERE id = :id"
                    ),
                    {"id": ruler_id},
                )
        with pytest.raises(IntegrityError, match="append-only"):
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM ruler_versions WHERE id = :id"),
                    {"id": ruler_id},
                )

        with factory() as session:
            row = session.get(RulerVersion, ruler_id)
            assert row is not None
            row.id += 20_000
            with pytest.raises(IntegrityError, match="immutable"):
                session.commit()
            session.rollback()

        with factory() as session:
            row = session.get(RulerVersion, ruler_id)
            assert row is not None
            session.delete(row)
            with pytest.raises(IntegrityError, match="append-only"):
                session.commit()
            session.rollback()

    with factory() as session:
        run = session.get(DiscoveryRun, 77)
        assert run is not None
        session.delete(run)
        session.commit()
        assert session.get(RulerVersion, quality_id) is not None

    with factory() as session:
        user = session.get(User, 1)
        assert user is not None
        session.delete(user)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_activation_rejects_corrupt_unique_conflict(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "corrupt-conflict.db")
    factory = create_session_factory(engine)
    digest = payload_digest(_payload(build_version=HISTORICAL_BUILD_VERSION))
    with factory() as session:
        session.add(
            RulerVersion(
                user_id=1,
                scope=QUALITY_DISCOUNT_SCOPE,
                digest=digest,
                reason="forged",
                payload_json="{}",
                activated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    with factory() as session:
        with pytest.raises(ValueError, match="integrity"):
            activate(session, _payload(build_version=HISTORICAL_BUILD_VERSION), "must not return forged")


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_track_ruler_is_deeply_immutable_and_canonical() -> None:
    ruler = TrackRuler.from_payload(_payload(build_version=HISTORICAL_BUILD_VERSION))
    assert ruler.scope == QUALITY_DISCOUNT_SCOPE
    assert ruler.track == "quality_discount"
    assert ruler.thresholds["roe_mean_min"] == "12"
    assert ruler.order[-1] == ("code", "asc", "nulls_last")
    text = canonical_payload(ruler)
    assert text == canonical_payload(deepcopy(_payload(build_version=HISTORICAL_BUILD_VERSION)))
    assert payload_digest(ruler) == payload_digest(deepcopy(_payload(build_version=HISTORICAL_BUILD_VERSION)))
    assert payload_digest(ruler) == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()
    with pytest.raises(TypeError):
        ruler.thresholds["roe_mean_min"] = "10"  # type: ignore[index]
    with pytest.raises(TypeError):
        ruler.sector_branches["bank"]["gate_order"] = ()  # type: ignore[index]


@pytest.mark.parametrize(
    ("category", "mutate"),
    [
        *[
            (
                f"missing {key}",
                lambda value, key=key: value.pop(key),
            )
            for key in (
                "fields",
                "formulas",
                "thresholds",
                "min_observations",
                "missing_policy",
                "sector_branches",
                "order",
                "industry_version",
                "algorithm_version",
            )
        ],
        ("extra key", lambda value: value.__setitem__("extra", True)),
        ("empty field", lambda value: value.__setitem__("fields", [""])),
        (
            "duplicate field",
            lambda value: value.__setitem__("fields", ["roe", "roe"]),
        ),
        (
            "missing formula",
            lambda value: value.__setitem__("formulas", {}),
        ),
        (
            "noncanonical threshold",
            lambda value: value["thresholds"].__setitem__("roe_floor", "12.0"),
        ),
        (
            "nonfinite threshold",
            lambda value: value["thresholds"].__setitem__("roe_floor", "NaN"),
        ),
        (
            "bool minimum",
            lambda value: value["min_observations"].__setitem__("roe", True),
        ),
        (
            "incomplete missing policy",
            lambda value: value["missing_policy"].pop("code"),
        ),
        (
            "malformed branch",
            lambda value: value["sector_branches"].__setitem__("financial", []),
        ),
        (
            "malformed order",
            lambda value: value.__setitem__("order", [["roe", "asc"]]),
        ),
        (
            "bad direction",
            lambda value: value["order"].__setitem__(
                0, ["roe", "sideways", "nulls_last"]
            ),
        ),
        (
            "bad null ordering",
            lambda value: value["order"].__setitem__(
                0, ["roe", "asc", "nulls_middle"]
            ),
        ),
        (
            "empty industry version",
            lambda value: value.__setitem__("industry_version", ""),
        ),
        (
            "unavailable build",
            lambda value: value.__setitem__("algorithm_version", "retired"),
        ),
    ],
)
@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_malformed_payload_matrix(category: str, mutate) -> None:
    value = _payload(build_version=HISTORICAL_BUILD_VERSION)
    mutate(value)
    with pytest.raises((TypeError, ValueError), match="."):
        TrackRuler.from_payload(value)


@pytest.mark.usefixtures("legacy_v7_algorithm")
def test_payload_rejects_nonfinite_json_aliases_and_cycles() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        TrackRuler.from_json(
            canonical_payload(_payload(build_version=HISTORICAL_BUILD_VERSION)).replace('"12"', "NaN", 1)
        )

    alias: list[str] = ["roe"]
    value = _payload(build_version=HISTORICAL_BUILD_VERSION)
    value["sector_branches"] = {
        "financial": {"fields": alias},
        "non_financial": {"fields": alias},
    }
    with pytest.raises(TypeError, match="alias"):
        TrackRuler.from_payload(value)

    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    value = _payload(build_version=HISTORICAL_BUILD_VERSION)
    value["sector_branches"] = {"financial": cycle}
    with pytest.raises(TypeError, match="cycle"):
        TrackRuler.from_payload(value)
