from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from time import sleep
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.models import (
    DiscoveryDataCheckpoint,
    DiscoveryDataFill,
    Financial,
    ValuationHistory,
    ValuationSeries,
)
from jobs.lock import JOB_TYPE_DISCOVERY_DATA, lock_status, release, try_acquire
from market.types import (
    DiscoveryCompanyData,
    DiscoveryCompanyError,
    DiscoveryRequestBudgetExhausted,
    FinancialRow,
    ValuationPoint,
)

# The universe probe used about 71 requests (56 pages, calendar, suspensions);
# 100 keeps a crash-safe margin before discovery checkpoints exist. Discovery
# company fetches are charged exactly at each HTTP request boundary. Task 1/2
# probes justify the conservative 0.5 req/s, 1,000/day, and 15-minute run cap.
FILL_CHUNK = 20
POLICY_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class RequestPolicy:
    requests_per_second: float = 0.5
    max_attempts: int = 3
    daily_budget: int = 1000
    max_runtime_seconds: float = 15 * 60
    retry_delay_seconds: float = 5 * 60
    universe_request_reservation: int = 100

    def __post_init__(self) -> None:
        if (
            self.requests_per_second <= 0
            or self.max_attempts <= 0
            or self.daily_budget <= 0
            or self.max_runtime_seconds <= 0
            or self.retry_delay_seconds < 0
            or self.universe_request_reservation <= 0
        ):
            raise ValueError("request policy values must be positive")


DEFAULT_POLICY = RequestPolicy()


class FillClock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        sleep(seconds)


class DiscoveryRequestGate:
    """Durably charge one scoped discovery HTTP attempt before transport."""

    def __init__(
        self,
        session: Session,
        fill_id: int,
        *,
        policy: RequestPolicy,
        clock: FillClock,
    ) -> None:
        self.session = session
        self.fill_id = fill_id
        self.policy = policy
        self.clock = clock
        self.attempts = 0
        self._next_request_at: datetime | None = None

    def __call__(self) -> None:
        _wait_until(self.clock, self._next_request_at)
        now = _aware_utc(self.clock.now())
        fill = self.session.get(DiscoveryDataFill, self.fill_id)
        if fill is None:
            raise RuntimeError(f"discovery fill {self.fill_id} missing")
        _reset_budget_day(fill, now)
        if fill.daily_requests >= fill.daily_budget:
            fill.status = "paused"
            fill.last_error = "daily request budget reached"
            fill.updated_at = now
            self.session.commit()
            raise DiscoveryRequestBudgetExhausted(
                "daily request budget reached"
            )
        fill.daily_requests += 1
        fill.request_count += 1
        fill.updated_at = now
        self.session.commit()
        self.attempts += 1
        self._next_request_at = now + timedelta(
            seconds=1 / self.policy.requests_per_second
        )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _policy_day(now: datetime) -> str:
    return _aware_utc(now).astimezone(POLICY_TIMEZONE).date().isoformat()


def _latest_fill(session: Session) -> DiscoveryDataFill | None:
    return session.scalars(
        select(DiscoveryDataFill).order_by(DiscoveryDataFill.id.desc())
    ).first()


def start_discovery_data_fill(
    session: Session,
    *,
    policy: RequestPolicy = DEFAULT_POLICY,
    now: datetime | None = None,
) -> tuple[DiscoveryDataFill | None, bool]:
    """Return (fill, dispatch). None means another heavy job owns the lock."""
    stamp = _aware_utc(now or datetime.now(timezone.utc))
    current_lock = lock_status(session)
    active = session.scalars(
        select(DiscoveryDataFill)
        .where(DiscoveryDataFill.status.in_(("queued", "running", "paused")))
        .order_by(DiscoveryDataFill.id.desc())
    ).first()
    if active is not None and current_lock == {
        "type": JOB_TYPE_DISCOVERY_DATA,
        "job_id": active.id,
    }:
        return active, False
    if current_lock is not None:
        return None, False
    if active is None:
        active = DiscoveryDataFill(
            status="queued",
            total=0,
            completed=0,
            failed=0,
            request_count=0,
            daily_budget=policy.daily_budget,
            budget_day=_policy_day(stamp),
            daily_requests=0,
            started_at=stamp,
            updated_at=stamp,
            last_error=None,
        )
        session.add(active)
        session.flush()
    if not try_acquire(session, active.id, JOB_TYPE_DISCOVERY_DATA):
        session.rollback()
        return None, False
    active.status = "running"
    active.updated_at = stamp
    session.commit()
    session.refresh(active)
    return active, True


def _reset_budget_day(fill: DiscoveryDataFill, now: datetime) -> None:
    day = _policy_day(now)
    if fill.budget_day != day:
        fill.budget_day = day
        fill.daily_requests = 0


def _reserve_requests(
    session: Session,
    fill: DiscoveryDataFill,
    amount: int,
    now: datetime,
) -> bool:
    _reset_budget_day(fill, now)
    if fill.daily_requests + amount > fill.daily_budget:
        fill.status = "paused"
        fill.last_error = "daily request budget reached"
        fill.updated_at = now
        session.commit()
        return False
    fill.daily_requests += amount
    fill.updated_at = now
    session.commit()
    return True


def _settle_request_reservation(
    session: Session,
    fill: DiscoveryDataFill,
    *,
    reserved: int,
    actual: int,
    now: datetime,
) -> int:
    used = max(0, int(actual))
    fill.daily_requests += used - reserved
    fill.request_count += used
    fill.updated_at = now
    session.commit()
    return used


def _market_request_count(market: object) -> int | None:
    value = getattr(market, "request_count", None)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _initialize_checkpoints(
    session: Session,
    market: object,
    fill: DiscoveryDataFill,
    policy: RequestPolicy,
    clock: FillClock,
) -> bool:
    existing = session.scalars(
        select(DiscoveryDataCheckpoint.code)
        .where(DiscoveryDataCheckpoint.fill_id == fill.id)
        .limit(1)
    ).first()
    if existing is not None:
        return True
    now = _aware_utc(clock.now())
    reserved = policy.universe_request_reservation
    if not _reserve_requests(session, fill, reserved, now):
        return False
    before = _market_request_count(market)
    try:
        fetch = getattr(market, "fetch_cheap_universe")
        rows = list(fetch())
    except Exception as exc:
        after = _market_request_count(market)
        actual = (
            max(0, after - before)
            if before is not None and after is not None
            else reserved
        )
        _settle_request_reservation(
            session,
            fill,
            reserved=reserved,
            actual=actual,
            now=_aware_utc(clock.now()),
        )
        fill.status = "paused"
        fill.last_error = f"universe: {exc}"
        fill.updated_at = _aware_utc(clock.now())
        session.commit()
        return False
    after = _market_request_count(market)
    actual = (
        max(0, after - before)
        if before is not None and after is not None
        else reserved
    )
    _settle_request_reservation(
        session,
        fill,
        reserved=reserved,
        actual=actual,
        now=_aware_utc(clock.now()),
    )
    codes = sorted(
        {
            str(getattr(row, "code", "")).strip().upper()
            for row in rows
            if str(getattr(row, "code", "")).strip().upper().endswith((".SH", ".SZ"))
        }
    )
    stamp = _aware_utc(clock.now())
    session.add_all(
        [
            DiscoveryDataCheckpoint(
                fill_id=fill.id,
                code=code,
                status="pending",
                attempts=0,
                next_retry_at=None,
                valuation_points=0,
                error=None,
                updated_at=stamp,
            )
            for code in codes
        ]
    )
    fill.total = len(codes)
    fill.updated_at = stamp
    if not codes:
        fill.status = "done"
    session.commit()
    return bool(codes)


def claim_pending_checkpoints(
    session: Session,
    fill_id: int,
    now: datetime,
    limit: int = FILL_CHUNK,
) -> list[DiscoveryDataCheckpoint]:
    stamp = _aware_utc(now)
    rows = session.scalars(
        select(DiscoveryDataCheckpoint)
        .where(
            DiscoveryDataCheckpoint.fill_id == fill_id,
            or_(
                DiscoveryDataCheckpoint.status == "pending",
                (
                    (DiscoveryDataCheckpoint.status == "retryable")
                    & (DiscoveryDataCheckpoint.next_retry_at <= stamp)
                ),
            ),
        )
        .order_by(DiscoveryDataCheckpoint.code)
        .limit(limit)
    ).all()
    for row in rows:
        row.status = "running"
        row.updated_at = stamp
    session.commit()
    return rows


def _checkpoint_counts(
    session: Session, fill_id: int
) -> dict[str, int]:
    counts = {
        "done": 0,
        "failed": 0,
        "pending": 0,
        "retryable": 0,
        "running": 0,
    }
    for status in session.scalars(
        select(DiscoveryDataCheckpoint.status).where(
            DiscoveryDataCheckpoint.fill_id == fill_id
        )
    ):
        if status in counts:
            counts[status] += 1
    return counts


def _sync_fill_counts(
    session: Session, fill: DiscoveryDataFill, now: datetime
) -> dict[str, int]:
    counts = _checkpoint_counts(session, fill.id)
    fill.completed = counts["done"]
    fill.failed = counts["failed"]
    fill.updated_at = _aware_utc(now)
    return counts


def _pause_running_checkpoints(
    session: Session,
    fill: DiscoveryDataFill,
    now: datetime,
) -> None:
    stamp = _aware_utc(now)
    session.execute(
        update(DiscoveryDataCheckpoint)
        .where(
            DiscoveryDataCheckpoint.fill_id == fill.id,
            DiscoveryDataCheckpoint.status == "running",
        )
        .values(status="pending", updated_at=stamp)
    )
    if fill.status == "running":
        fill.status = "paused"
    _sync_fill_counts(session, fill, stamp)


def _wait_until(
    clock: FillClock, target: datetime | None
) -> None:
    if target is None:
        return
    delay = (target - _aware_utc(clock.now())).total_seconds()
    if delay > 0:
        clock.sleep(delay)


def _persist_company(
    session: Session,
    checkpoint: DiscoveryDataCheckpoint,
    result: DiscoveryCompanyData,
    now: datetime,
) -> None:
    if result.code != checkpoint.code or any(
        row.code != checkpoint.code
        for row in (*result.financial_rows, *result.valuation_points)
    ):
        raise ValueError(f"{checkpoint.code} result code mismatch")
    persist_discovery_financials(session, result.financial_rows)
    persist_valuation_history(session, result.valuation_points, fetched_at=now)
    checkpoint.status = "done"
    checkpoint.next_retry_at = None
    checkpoint.valuation_points = len(result.valuation_points)
    checkpoint.error = None
    checkpoint.updated_at = now


def execute_discovery_data_fill(
    session: Session,
    market: object,
    fill_id: int,
    *,
    policy: RequestPolicy = DEFAULT_POLICY,
    clock: FillClock | None = None,
) -> None:
    timer = clock or SystemClock()
    run_started = _aware_utc(timer.now())
    try:
        fill = session.get(DiscoveryDataFill, fill_id)
        if fill is None:
            return
        fill.status = "running"
        fill.daily_budget = policy.daily_budget
        fill.updated_at = run_started
        session.commit()
        if not _initialize_checkpoints(session, market, fill, policy, timer):
            return
        request_gate = DiscoveryRequestGate(
            session,
            fill.id,
            policy=policy,
            clock=timer,
        )

        while True:
            now = _aware_utc(timer.now())
            if (now - run_started).total_seconds() >= policy.max_runtime_seconds:
                fill.status = "paused"
                fill.last_error = "maximum run time reached"
                session.commit()
                break
            claimed = claim_pending_checkpoints(session, fill.id, now)
            if not claimed:
                counts = _sync_fill_counts(session, fill, now)
                if counts["pending"] or counts["retryable"] or counts["running"]:
                    fill.status = "paused"
                else:
                    fill.status = "done"
                session.commit()
                break

            stop = False
            for checkpoint in claimed:
                now = _aware_utc(timer.now())
                if (now - run_started).total_seconds() >= policy.max_runtime_seconds:
                    fill.status = "paused"
                    fill.last_error = "maximum run time reached"
                    session.commit()
                    stop = True
                    break
                attempts_before = request_gate.attempts
                failure: Exception | None = None
                try:
                    result = market.fetch_discovery_data(
                        checkpoint.code,
                        before_request=request_gate,
                    )
                except DiscoveryRequestBudgetExhausted:
                    stop = True
                    break
                except Exception as exc:
                    checkpoint.attempts += 1
                    failure = exc
                else:
                    checkpoint.attempts += 1
                    actual = request_gate.attempts - attempts_before
                    if result.request_count != actual:
                        failure = DiscoveryCompanyError(
                            checkpoint.code,
                            "request_audit",
                            (
                                f"reported {result.request_count} requests, "
                                f"observed {actual}"
                            ),
                            request_count=actual,
                        )
                    try:
                        if failure is None:
                            _persist_company(
                                session,
                                checkpoint,
                                result,
                                _aware_utc(timer.now()),
                            )
                    except Exception as exc:
                        session.rollback()
                        fill = session.get(DiscoveryDataFill, fill_id)
                        checkpoint = session.get(
                            DiscoveryDataCheckpoint,
                            (fill_id, checkpoint.code),
                        )
                        if fill is None or checkpoint is None:
                            raise
                        failure = exc
                if failure is not None:
                    checkpoint.error = str(failure)
                    checkpoint.updated_at = _aware_utc(timer.now())
                    fill.last_error = str(failure)
                    if checkpoint.attempts < policy.max_attempts:
                        checkpoint.status = "retryable"
                        checkpoint.next_retry_at = checkpoint.updated_at + timedelta(
                            seconds=policy.retry_delay_seconds
                        )
                    else:
                        checkpoint.status = "failed"
                        checkpoint.next_retry_at = None
                _sync_fill_counts(session, fill, _aware_utc(timer.now()))
                session.commit()
            if stop:
                break
    except Exception as exc:
        session.rollback()
        fill = session.get(DiscoveryDataFill, fill_id)
        if fill is not None:
            fill.status = "paused"
            fill.last_error = str(exc)
            fill.updated_at = _aware_utc(timer.now())
            session.commit()
    finally:
        fill = session.get(DiscoveryDataFill, fill_id)
        if fill is not None:
            _pause_running_checkpoints(session, fill, _aware_utc(timer.now()))
        release(session, fill_id)
        session.commit()


def _eta_seconds(
    session: Session,
    fill_id: int,
    remaining: int,
) -> int | None:
    completed = session.scalars(
        select(DiscoveryDataCheckpoint)
        .where(
            DiscoveryDataCheckpoint.fill_id == fill_id,
            DiscoveryDataCheckpoint.status == "done",
        )
        .order_by(DiscoveryDataCheckpoint.updated_at)
    ).all()
    if len(completed) < 2 or remaining <= 0:
        return None
    elapsed = (
        _aware_utc(completed[-1].updated_at)
        - _aware_utc(completed[0].updated_at)
    ).total_seconds()
    if elapsed <= 0:
        return None
    return ceil(remaining * elapsed / (len(completed) - 1))


def discovery_data_status(
    session: Session,
    *,
    policy: RequestPolicy = DEFAULT_POLICY,
    now: datetime | None = None,
) -> dict[str, object]:
    fill = _latest_fill(session)
    base: dict[str, object] = {
        "requests_per_second": policy.requests_per_second,
        "max_attempts": policy.max_attempts,
        "daily_budget": policy.daily_budget,
        "max_runtime_seconds": policy.max_runtime_seconds,
        "eta_seconds": None,
    }
    if fill is None:
        return {
            "id": None,
            "type": JOB_TYPE_DISCOVERY_DATA,
            "status": "idle",
            "total": 0,
            "completed": 0,
            "failed": 0,
            "pending": 0,
            "retryable": 0,
            "running": 0,
            "request_count": 0,
            "daily_requests": 0,
            "budget_day": _policy_day(now or datetime.now(timezone.utc)),
            "last_error": None,
            **base,
        }
    counts = _checkpoint_counts(session, fill.id)
    remaining = counts["pending"] + counts["retryable"] + counts["running"]
    return {
        "id": fill.id,
        "type": JOB_TYPE_DISCOVERY_DATA,
        "status": fill.status,
        "total": fill.total,
        "completed": counts["done"],
        "failed": counts["failed"],
        "pending": counts["pending"],
        "retryable": counts["retryable"],
        "running": counts["running"],
        "request_count": fill.request_count,
        "daily_requests": fill.daily_requests,
        "budget_day": fill.budget_day,
        "last_error": fill.last_error,
        **base,
        "daily_budget": fill.daily_budget,
        "eta_seconds": _eta_seconds(session, fill.id, remaining),
    }

_DISCOVERY_SHARED_FINANCIAL_FIELDS = (
    "net_income",
    "ocf",
    "capex",
    "shares",
    "npl",
)
_DISCOVERY_ONLY_FINANCIAL_FIELDS = (
    "revenue",
    "deducted_net_income",
    "total_assets",
    "net_assets",
    "cash",
    "short_loan",
    "long_loan",
    "bond_payable",
    "noncurrent_liab_1year",
    "interest_debt",
    "pretax_income",
    "income_tax",
    "normalized_effective_tax_rate",
    "invested_capital",
    "roic",
    "capital_adequacy",
    "core_tier1_capital_adequacy",
    "nonperforming_loan_balance",
    "impairment_loss",
    "government_grants",
    "asset_disposal_gain",
    "goodwill",
    "acquisition_cash_paid",
    "source_as_of",
)
_DISCOVERY_FINANCIAL_FIELDS = (
    _DISCOVERY_SHARED_FINANCIAL_FIELDS + _DISCOVERY_ONLY_FINANCIAL_FIELDS
)


class ValuationMethodMismatchError(ValueError):
    pass


def persist_discovery_financials(
    session: Session,
    rows: Iterable[FinancialRow],
) -> None:
    for row in rows:
        stored = session.get(
            Financial,
            (row.code, row.period, row.period_type),
        )
        if stored is None:
            stored = Financial(
                code=row.code,
                period=row.period,
                period_type=row.period_type,
            )
            session.add(stored)
        for field in _DISCOVERY_FINANCIAL_FIELDS:
            setattr(stored, field, getattr(row, field))


def persist_valuation_history(
    session: Session,
    rows: Iterable[ValuationPoint],
    *,
    fetched_at: datetime,
) -> None:
    points: dict[tuple[str, str], ValuationPoint] = {}
    methods: dict[str, str] = {}
    for row in rows:
        prior_method = methods.setdefault(row.code, row.ratio_method)
        if prior_method != row.ratio_method:
            raise ValuationMethodMismatchError(
                f"{row.code} ratio_method mismatch: "
                f"{prior_method} != {row.ratio_method}"
            )
        key = (row.code, row.month)
        points[key] = row

    for code in sorted(methods):
        ratio_method = methods[code]
        session.execute(
            insert(ValuationSeries)
            .values(code=code, ratio_method=ratio_method)
            .on_conflict_do_nothing(index_elements=[ValuationSeries.code])
        )
        claimed = session.get(ValuationSeries, code)
        if claimed is None or claimed.ratio_method != ratio_method:
            existing_method = None if claimed is None else claimed.ratio_method
            raise ValuationMethodMismatchError(
                f"{code} ratio_method mismatch: "
                f"{existing_method} != {ratio_method}"
            )

    for key in sorted(points):
        row = points[key]
        session.merge(
            ValuationHistory(
                code=row.code,
                month=row.month,
                trade_date=row.trade_date,
                pe=row.pe,
                pb=row.pb,
                price=row.price,
                source=row.source,
                ratio_method=row.ratio_method,
                fetched_at=fetched_at,
            )
        )
