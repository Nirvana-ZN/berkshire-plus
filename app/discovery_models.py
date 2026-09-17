from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import UTCDateTime


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    week_key: Mapped[str] = mapped_column(String, nullable=False)
    current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    snapshot_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    data_version: Mapped[str] = mapped_column(String, nullable=False)
    financial_version: Mapped[str] = mapped_column(String, nullable=False)
    valuation_version: Mapped[str] = mapped_column(String, nullable=False)
    quality_discount_ruler_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("ruler_versions.id"), nullable=True
    )
    deep_value_ruler_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("ruler_versions.id"), nullable=True
    )
    garp_ruler_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("ruler_versions.id"), nullable=True
    )
    algorithm_version: Mapped[str] = mapped_column(String, nullable=False)
    trading_dates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_of_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="SET NULL"), nullable=True
    )


class DiscoveryInputSnapshot(Base):
    __tablename__ = "discovery_input_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "code", name="uq_discovery_input_snapshot_run_code"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    industry: Mapped[str | None] = mapped_column(String, nullable=True)
    security_status: Mapped[str] = mapped_column(String, nullable=False)
    fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    sources_json: Mapped[str] = mapped_column(Text, nullable=False)
    as_of_json: Mapped[str] = mapped_column(Text, nullable=False)
    data_version: Mapped[str] = mapped_column(String, nullable=False)
    financial_version: Mapped[str] = mapped_column(String, nullable=False)
    valuation_version: Mapped[str] = mapped_column(String, nullable=False)
    ruler_versions_json: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryCommonStage(Base):
    __tablename__ = "discovery_common_stages"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "attempt", name="uq_discovery_common_stage_attempt"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drop_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    input_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String, nullable=False)
    failure_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryCommonResult(Base):
    __tablename__ = "discovery_common_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "attempt"],
            ["discovery_common_stages.run_id", "discovery_common_stages.attempt"],
            name="fk_discovery_common_result_stage",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "run_id",
            "attempt",
            "code",
            name="uq_discovery_common_result_attempt_code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryStage(Base):
    __tablename__ = "discovery_stages"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "track",
            "stage",
            "attempt",
            name="uq_discovery_stage_attempt",
        ),
        UniqueConstraint(
            "id",
            "run_id",
            "track",
            "stage",
            "attempt",
            name="uq_discovery_stage_artifact_parent",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False
    )
    track: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drop_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    input_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    ruler_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("ruler_versions.id"), nullable=True
    )
    algorithm_version: Mapped[str] = mapped_column(String, nullable=False)
    failure_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryResult(Base):
    __tablename__ = "discovery_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "track", "stage", "attempt"],
            [
                "discovery_stages.run_id",
                "discovery_stages.track",
                "discovery_stages.stage",
                "discovery_stages.attempt",
            ],
            name="fk_discovery_result_stage",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "run_id",
            "track",
            "stage",
            "attempt",
            "code",
            name="uq_discovery_result_attempt_code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    track: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryOverride(Base):
    __tablename__ = "discovery_overrides"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "track", "stage", "attempt"],
            [
                "discovery_stages.run_id",
                "discovery_stages.track",
                "discovery_stages.stage",
                "discovery_stages.attempt",
            ],
            name="fk_discovery_override_stage",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "run_id",
            "track",
            "stage",
            "attempt",
            "code",
            name="uq_discovery_override_attempt_code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    track: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryDecision(Base):
    __tablename__ = "discovery_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "competition_stage_id",
                "run_id",
                "track",
                "stage",
                "competition_attempt",
            ],
            [
                "discovery_stages.id",
                "discovery_stages.run_id",
                "discovery_stages.track",
                "discovery_stages.stage",
                "discovery_stages.attempt",
            ],
            name="fk_discovery_decision_competition_stage",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "stage = 'competition'",
            name="ck_discovery_decision_competition_stage",
        ),
        UniqueConstraint(
            "run_id",
            "track",
            "competition_attempt",
            "code",
            name="uq_discovery_decision_attempt_code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    track: Mapped[str] = mapped_column(String, nullable=False)
    competition_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    competition_stage_id: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="competition",
        server_default="competition",
    )
    code: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    keep_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DiscoveryEvidence(Base):
    __tablename__ = "discovery_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "competition_stage_id",
                "run_id",
                "track",
                "stage",
                "competition_attempt",
            ],
            [
                "discovery_stages.id",
                "discovery_stages.run_id",
                "discovery_stages.track",
                "discovery_stages.stage",
                "discovery_stages.attempt",
            ],
            name="fk_discovery_evidence_competition_stage",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "stage = 'competition'",
            name="ck_discovery_evidence_competition_stage",
        ),
        UniqueConstraint(
            "run_id",
            "track",
            "competition_attempt",
            "code",
            "kind",
            "rule_id",
            name="uq_discovery_evidence_rule",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    track: Mapped[str] = mapped_column(String, nullable=False)
    competition_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    competition_stage_id: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="competition",
        server_default="competition",
    )
    code: Mapped[str] = mapped_column(String, nullable=False)
    ruler_version_id: Mapped[int] = mapped_column(
        ForeignKey("ruler_versions.id"), nullable=False
    )
    rule_version: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    rule_id: Mapped[str] = mapped_column(String, nullable=False)
    input_fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    display_text: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[str] = mapped_column(String, nullable=False)
    data_completeness: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
