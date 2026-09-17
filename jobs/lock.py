from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    DiscoveryDataCheckpoint,
    DiscoveryDataFill,
    JobLock,
    PortfolioReview,
    ResearchJob,
    ScreenRun,
)

JOB_TYPE_FUNNEL = "funnel"
JOB_TYPE_RESEARCH = "research"
JOB_TYPE_REVIEW = "review"
JOB_TYPE_DISCOVERY_DATA = "discovery_data"
JOB_TYPE_DISCOVERY = "discovery"
LOCK_ROW_ID = 1
ACTIVE_STATUSES = ("queued", "running")


def ensure_lock_row(session: Session) -> JobLock:
    row = session.get(JobLock, LOCK_ROW_ID)
    if row is None:
        row = JobLock(id=LOCK_ROW_ID, job_id=None, job_type=None, held_at=None)
        session.add(row)
        session.flush()
    return row


def clear_stale_lock(session: Session) -> None:
    # Local import registers discovery tables without creating an app-model cycle.
    from app.discovery_models import DiscoveryCommonStage, DiscoveryStage

    ensure_lock_row(session)
    leftover = session.scalars(
        select(ScreenRun).where(ScreenRun.status.in_(ACTIVE_STATUSES))
    ).all()
    for run in leftover:
        run.status = "failed"
        run.error = "进程重启，任务中断"
        run.updated_at = datetime.now(timezone.utc)
    leftover_research = session.scalars(
        select(ResearchJob).where(ResearchJob.status.in_(ACTIVE_STATUSES))
    ).all()
    for job in leftover_research:
        job.status = "failed"
        job.error = "进程重启，任务中断"
        job.updated_at = datetime.now(timezone.utc)
    leftover_reviews = session.scalars(
        select(PortfolioReview).where(PortfolioReview.status.in_(ACTIVE_STATUSES))
    ).all()
    for review in leftover_reviews:
        review.status = "failed"
        review.error = "进程重启，任务中断"
        review.updated_at = datetime.now(timezone.utc)
    leftover_discovery = session.scalars(
        select(DiscoveryDataFill).where(
            DiscoveryDataFill.status.in_(ACTIVE_STATUSES)
        )
    ).all()
    interrupted_at = datetime.now(timezone.utc)
    for fill in leftover_discovery:
        fill.status = "paused"
        fill.last_error = "进程重启，任务已暂停"
        fill.updated_at = interrupted_at
    interrupted_checkpoints = session.scalars(
        select(DiscoveryDataCheckpoint).where(
            DiscoveryDataCheckpoint.status == "running"
        )
    ).all()
    for checkpoint in interrupted_checkpoints:
        checkpoint.status = "pending"
        checkpoint.updated_at = interrupted_at
        fill = session.get(DiscoveryDataFill, checkpoint.fill_id)
        if fill is not None:
            fill.status = "paused"
            fill.updated_at = interrupted_at
            if fill.last_error is None:
                fill.last_error = "进程重启，任务已暂停"
    interrupted_stages = [
        *session.scalars(
            select(DiscoveryCommonStage).where(
                DiscoveryCommonStage.status == "running",
                DiscoveryCommonStage.superseded_at.is_(None),
            )
        ).all(),
        *session.scalars(
            select(DiscoveryStage).where(
                DiscoveryStage.status == "running",
                DiscoveryStage.superseded_at.is_(None),
            )
        ).all(),
    ]
    for stage in interrupted_stages:
        stage.status = "failed"
        stage.failure_kind = "transient"
        if stage.error is None:
            stage.error = "进程重启，任务中断"
        stage.updated_at = interrupted_at
    row = session.get(JobLock, LOCK_ROW_ID)
    if row is not None:
        row.job_id = None
        row.job_type = None
        row.held_at = None
    session.commit()


def lock_status(session: Session) -> dict[str, object] | None:
    row = session.get(JobLock, LOCK_ROW_ID)
    if row is None or row.job_id is None:
        return None
    return {"type": row.job_type, "job_id": row.job_id}


def try_acquire(session: Session, job_id: int, job_type: str) -> bool:
    ensure_lock_row(session)
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(JobLock)
        .where(JobLock.id == LOCK_ROW_ID, JobLock.job_id.is_(None))
        .values(job_id=job_id, job_type=job_type, held_at=now)
    )
    return int(result.rowcount or 0) == 1


def release(session: Session, job_id: int, job_type: str) -> None:
    session.execute(
        update(JobLock)
        .where(
            JobLock.id == LOCK_ROW_ID,
            JobLock.job_id == job_id,
            JobLock.job_type == job_type,
        )
        .values(job_id=None, job_type=None, held_at=None)
    )
