from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from inspect import signature
from math import ceil
from pathlib import Path
from time import sleep
from typing import Protocol

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.models import (
    DiscoveryDataCheckpoint,
    DiscoveryDataFill,
    DiscoveryFillTradingDate,
    DiscoveryUniverseSecurity,
    Financial,
    FinancialFetchCompleteness,
    FinancialFieldProvenance,
    ValuationHistory,
    ValuationSeries,
)
from discovery.company_payload import (
    PAYLOAD_SCHEMA_RESTART_GUIDANCE,
    SCHEMA_VERSION as COMPANY_PAYLOAD_SCHEMA_VERSION,
    company_payload_digest,
    parse_company_payload,
    serialize_company_payload,
    verify_company_payload_digest,
)
from discovery.constants import CHINA_MARKET_TIMEZONE, china_market_date
from discovery.field_dictionary import FIELD_DICTIONARY
from discovery.evidence_store import (append_evidence, append_coverage, load_evidence_as_of,
                                      load_coverage, load_coverage_references)
from discovery.fundamentals import coverage_reference_evidence
from jobs.lock import (
    FILL_RESTART_PAUSE,
    JOB_TYPE_DISCOVERY_DATA,
    lock_status,
    release,
    try_acquire,
)
from market.types import (
    DiscoveryCompanyData,
    DiscoveryCompanyError,
    DiscoveryPermanentCompanyError,
    DiscoveryRequestBudgetExhausted,
    DiscoveryRequestRuntimeExhausted,
    DiscoveryRequestStopped,
    FinancialRow,
    SecurityState,
    ValuationPoint,
    validate_security_state,
)

# Universe and company fetches are charged exactly at each HTTP request
# boundary. Task 1/2 probes justify the conservative 0.5 req/s, 1,000/day,
# and 15-minute run cap without a full-market live calibration.
FILL_CHUNK = 20


@dataclass(frozen=True)
class RequestPolicy:
    # A full-market fill is ~55k requests (~10.5 per company); 1/s finished
    # two fills without East Money throttling.
    requests_per_second: float = 1.0
    max_attempts: int = 3
    daily_budget: int = 120_000
    max_runtime_seconds: float = 15 * 60
    retry_delay_seconds: float = 5 * 60

    def __post_init__(self) -> None:
        if (
            self.requests_per_second <= 0
            or self.max_attempts <= 0
            or self.daily_budget <= 0
            or self.max_runtime_seconds <= 0
            or self.retry_delay_seconds < 0
        ):
            raise ValueError("request policy values must be positive")


DEFAULT_POLICY = RequestPolicy()
SNAPSHOT_UNAVAILABLE_STATUS = "snapshot_unavailable"
SNAPSHOT_UNAVAILABLE_GUIDANCE = (
    "snapshot unavailable or mismatched; start a new fill"
)


class FillClock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        sleep(seconds)


_SECURITY_SNAPSHOT_FIELDS = (
    "fill_id",
    "code",
    "name",
    "industry",
    "exchange",
    "listing_date",
    "is_st",
    "is_delisting",
    "trading_status",
    "is_suspended",
    "last_trade_date",
    "industry_version",
    "source_as_of",
    "provenance",
    "market_status_code",
    "snapshot_at",
)


@dataclass(frozen=True)
class FrozenSecuritySnapshot:
    fill_id: int
    code: str
    state: SecurityState
    identity: tuple[object, ...]


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
        fill = self.session.get(DiscoveryDataFill, self.fill_id)
        if fill is None:
            raise RuntimeError(f"discovery fill {self.fill_id} missing")
        run_started = fill.run_started_at or _aware_utc(self.clock.now())
        self._deadline = run_started + timedelta(
            seconds=self.policy.max_runtime_seconds
        )
        interval = timedelta(seconds=1 / self.policy.requests_per_second)
        if fill.last_request_at is not None:
            self._next_request_at = _aware_utc(fill.last_request_at) + interval
        elif fill.request_count > 0:
            self._next_request_at = _aware_utc(self.clock.now()) + interval
        else:
            self._next_request_at = None

    def __call__(self) -> None:
        now = _aware_utc(self.clock.now())
        if now >= self._deadline or (
            self._next_request_at is not None
            and self._next_request_at >= self._deadline
        ):
            self._stop_for_runtime(now)
        _wait_until(self.clock, self._next_request_at)
        now = _aware_utc(self.clock.now())
        if now >= self._deadline:
            self._stop_for_runtime(now)
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
        fill.last_request_at = now
        fill.updated_at = now
        self.session.commit()
        self.attempts += 1
        self._next_request_at = now + timedelta(
            seconds=1 / self.policy.requests_per_second
        )

    def _stop_for_runtime(self, now: datetime) -> None:
        fill = self.session.get(DiscoveryDataFill, self.fill_id)
        if fill is not None:
            fill.status = "paused"
            fill.last_error = "maximum run time reached"
            fill.updated_at = now
            self.session.commit()
        raise DiscoveryRequestRuntimeExhausted("maximum run time reached")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _policy_day(now: datetime) -> str:
    return (
        _aware_utc(now)
        .astimezone(CHINA_MARKET_TIMEZONE)
        .date()
        .isoformat()
    )


def _latest_fill(session: Session) -> DiscoveryDataFill | None:
    return session.scalars(
        select(DiscoveryDataFill).order_by(DiscoveryDataFill.id.desc())
    ).first()


def _snapshot_parts(
    session: Session,
    fill_id: int,
) -> tuple[set[str], set[str], bool, bool]:
    checkpoint_rows = session.scalars(
        select(DiscoveryDataCheckpoint).where(
            DiscoveryDataCheckpoint.fill_id == fill_id
        )
    ).all()
    checkpoints = {row.code for row in checkpoint_rows}
    def valid_payload(row: DiscoveryDataCheckpoint) -> bool:
        if row.status != "done":
            return True
        if not verify_company_payload_digest(
            row.payload_json,
            row.payload_digest,
        ):
            return False
        try:
            payload = parse_company_payload(
                row.payload_json,
                expected_code=row.code,
            )
            if payload['schema_version'] != COMPANY_PAYLOAD_SCHEMA_VERSION:
                return False
        except (TypeError, ValueError):
            return False
        return True

    payloads_complete = all(valid_payload(row) for row in checkpoint_rows)
    snapshots = set(
        session.scalars(
            select(DiscoveryUniverseSecurity.code).where(
                DiscoveryUniverseSecurity.fill_id == fill_id
            )
        )
    )
    has_dates = (
        session.scalars(
            select(DiscoveryFillTradingDate.trade_date)
            .where(DiscoveryFillTradingDate.fill_id == fill_id)
            .limit(1)
        ).first()
        is not None
    )
    return checkpoints, snapshots, has_dates, payloads_complete


def _mark_snapshot_unavailable(
    fill: DiscoveryDataFill,
    now: datetime,
    *,
    guidance: str = SNAPSHOT_UNAVAILABLE_GUIDANCE,
) -> None:
    fill.status = SNAPSHOT_UNAVAILABLE_STATUS
    fill.snapshot_status = "unavailable"
    fill.last_error = guidance
    fill.updated_at = _aware_utc(now)


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
    if active is not None:
        if active.payload_schema_version != COMPANY_PAYLOAD_SCHEMA_VERSION:
            _mark_snapshot_unavailable(
                active,
                stamp,
                guidance=PAYLOAD_SCHEMA_RESTART_GUIDANCE,
            )
            if current_lock == {
                "type": JOB_TYPE_DISCOVERY_DATA,
                "job_id": active.id,
            }:
                release(session, active.id, JOB_TYPE_DISCOVERY_DATA)
            session.commit()
            current_lock = lock_status(session)
            active = None
    if active is not None:
        checkpoints, snapshots, has_dates, payloads_complete = _snapshot_parts(
            session, active.id
        )
        invalid_snapshot = (
            bool(checkpoints)
            and (
                active.snapshot_status != "ready"
                or checkpoints != snapshots
                or not has_dates
                or not payloads_complete
            )
        ) or (
            not checkpoints
            and active.status == "paused"
            and active.snapshot_status is None
        )
        if invalid_snapshot:
            _mark_snapshot_unavailable(active, stamp)
            if current_lock == {
                "type": JOB_TYPE_DISCOVERY_DATA,
                "job_id": active.id,
            }:
                release(session, active.id, JOB_TYPE_DISCOVERY_DATA)
            session.commit()
            current_lock = lock_status(session)
            active = None
    if current_lock is not None and current_lock.get("type") == JOB_TYPE_DISCOVERY_DATA:
        locked_fill = session.get(DiscoveryDataFill, current_lock.get("job_id"))
        if locked_fill is not None:
            return locked_fill, False
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
            snapshot_status=None,
            payload_schema_version=COMPANY_PAYLOAD_SCHEMA_VERSION,
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


def fill_resume_delay(
    session: Session, fill_id: int, now: datetime
) -> float | None:
    """Seconds until a paused fill can progress unattended; None keeps it paused."""
    fill = session.get(DiscoveryDataFill, fill_id)
    if fill is None or fill.status != "paused":
        return None
    stamp = _aware_utc(now)
    if (
        fill.budget_day == _policy_day(stamp)
        and fill.daily_requests >= fill.daily_budget
    ):
        return None
    if fill.last_error in ("maximum run time reached", FILL_RESTART_PAUSE):
        return 0.0
    counts = _checkpoint_counts(session, fill.id)
    if counts["pending"] or counts["running"]:
        # Stopped with work left for another reason (universe, crash): a person
        # should see the error before it retries.
        return None
    retry_at = session.scalar(
        select(func.min(DiscoveryDataCheckpoint.next_retry_at)).where(
            DiscoveryDataCheckpoint.fill_id == fill.id,
            DiscoveryDataCheckpoint.status == "retryable",
        )
    )
    if retry_at is None:
        return None
    return max((_aware_utc(retry_at) - stamp).total_seconds(), 0.0)


def _reset_budget_day(fill: DiscoveryDataFill, now: datetime) -> None:
    day = _policy_day(now)
    if fill.budget_day != day:
        fill.budget_day = day
        fill.daily_requests = 0


# Intraday the exchange calendar already lists today, but daily valuations only
# appear after the close (~15:15 Beijing on 2026-09-22); a fill started then
# would pin early companies to a price that does not exist yet.
# ponytail: fixed cutoff with margin, not a probe of the publication itself.
_VALUATION_PUBLISHED_AT = time(16, 0)


def _completed_session_date(now: datetime) -> date:
    local = _aware_utc(now).astimezone(CHINA_MARKET_TIMEZONE)
    if local.time() < _VALUATION_PUBLISHED_AT:
        return local.date() - timedelta(days=1)
    return local.date()


def _initialize_checkpoints(
    session: Session,
    market: object,
    fill: DiscoveryDataFill,
    request_gate: DiscoveryRequestGate,
) -> bool:
    if fill.payload_schema_version != COMPANY_PAYLOAD_SCHEMA_VERSION:
        _mark_snapshot_unavailable(
            fill,
            request_gate.clock.now(),
            guidance=PAYLOAD_SCHEMA_RESTART_GUIDANCE,
        )
        session.commit()
        return False
    checkpoints, snapshots, has_dates, payloads_complete = _snapshot_parts(
        session, fill.id
    )
    if checkpoints:
        if (
            fill.snapshot_status != "ready"
            or checkpoints != snapshots
            or not has_dates
            or not payloads_complete
        ):
            _mark_snapshot_unavailable(fill, request_gate.clock.now())
            session.commit()
            return False
        return True
    if snapshots or has_dates or fill.snapshot_status is not None:
        _mark_snapshot_unavailable(fill, request_gate.clock.now())
        session.commit()
        return False
    try:
        fetch = getattr(market, "fetch_universe")
        as_of = _completed_session_date(request_gate.clock.now())
        rows = list(
            fetch(before_request=request_gate, as_of=as_of)
            if _supports_keyword(fetch, "as_of")
            else fetch(before_request=request_gate)
        )
        if hasattr(market, "_last_universe_trading_dates"):
            trading_dates = list(market._last_universe_trading_dates)
        else:
            fetch_dates = getattr(market, "fetch_trading_dates")
            end = as_of.isoformat()
            if "before_request" in signature(fetch_dates).parameters:
                trading_dates = list(
                    fetch_dates(end, limit=6, before_request=request_gate)
                )
            else:
                request_gate()
                trading_dates = list(fetch_dates(end, limit=6))
    except DiscoveryRequestStopped:
        return False
    except Exception as exc:
        fill.status = "paused"
        fill.last_error = f"universe: {exc}"
        fill.updated_at = _aware_utc(request_gate.clock.now())
        session.commit()
        return False
    universe = {
        code: row
        for row in rows
        if (
            code := str(getattr(row, "code", "")).strip().upper()
        ).endswith((".SH", ".SZ"))
    }
    codes = sorted(universe)
    stamp = _aware_utc(request_gate.clock.now())
    session.add_all(
        [
            DiscoveryUniverseSecurity(
                fill_id=fill.id,
                code=code,
                name=str(getattr(universe[code], "name", "")),
                industry=str(getattr(universe[code], "industry", "")),
                exchange=getattr(universe[code], "exchange", None),
                listing_date=getattr(universe[code], "listing_date", None),
                is_st=getattr(universe[code], "is_st", None),
                is_delisting=getattr(universe[code], "is_delisting", None),
                trading_status=getattr(universe[code], "trading_status", None),
                is_suspended=(
                    True
                    if getattr(universe[code], "trading_status", None)
                    == "suspended"
                    else False
                    if getattr(universe[code], "trading_status", None)
                    == "trading"
                    else None
                ),
                last_trade_date=getattr(
                    universe[code], "last_trade_date", None
                ),
                industry_version=getattr(
                    universe[code], "industry_version", None
                ),
                source_as_of=getattr(universe[code], "industry_as_of", None),
                provenance=getattr(
                    universe[code], "security_source", None
                ),
                market_status_code=getattr(
                    universe[code], "market_status_code", None
                ),
                snapshot_at=stamp,
            )
            for code in codes
        ]
    )
    session.add_all(
        [
            DiscoveryFillTradingDate(fill_id=fill.id, trade_date=trade_date)
            for trade_date in sorted(set(trading_dates))
        ]
    )
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
    # Universe rows and trading dates must exist before the parent seal flips.
    session.flush()
    fill.snapshot_status = "ready"
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
        row.payload_json = None
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


def _security_snapshot_identity(
    row: DiscoveryUniverseSecurity,
) -> tuple[object, ...]:
    return tuple(getattr(row, field) for field in _SECURITY_SNAPSHOT_FIELDS)


def _load_frozen_security(
    session: Session,
    fill: DiscoveryDataFill,
    checkpoint: DiscoveryDataCheckpoint,
) -> FrozenSecuritySnapshot:
    row = session.get(
        DiscoveryUniverseSecurity, (fill.id, checkpoint.code)
    )
    if row is None:
        raise DiscoveryPermanentCompanyError(
            checkpoint.code,
            "security_snapshot_incomplete",
            "fill security snapshot is missing",
        )
    required = (
        row.name,
        row.industry,
        row.exchange,
        row.listing_date,
        row.trading_status,
        row.last_trade_date,
        row.industry_version,
        row.source_as_of,
        row.provenance,
    )
    if (
        any(type(value) is not str or not value for value in required)
        or type(row.is_st) is not bool
        or type(row.is_delisting) is not bool
        or type(row.is_suspended) is not bool
    ):
        raise DiscoveryPermanentCompanyError(
            checkpoint.code,
            "security_snapshot_incomplete",
            "fill security snapshot lacks required trusted fields",
        )
    try:
        if row.fill_id != fill.id or row.code != checkpoint.code:
            raise ValueError("snapshot belongs to another fill or code")
        if row.is_suspended != (row.trading_status == "suspended"):
            raise ValueError("snapshot suspension fields disagree")
        snapshot_at = _aware_utc(row.snapshot_at)
        cutoff = china_market_date(fill.started_at)
        if china_market_date(snapshot_at) > cutoff:
            raise ValueError("snapshot date exceeds fill cutoff")
        state = SecurityState(
            code=row.code,
            name=row.name,
            exchange=row.exchange,  # type: ignore[arg-type]
            listing_date=row.listing_date,  # type: ignore[arg-type]
            is_st=row.is_st,  # type: ignore[arg-type]
            is_delisting=row.is_delisting,  # type: ignore[arg-type]
            trading_status=row.trading_status,  # type: ignore[arg-type]
            last_trade_date=row.last_trade_date,  # type: ignore[arg-type]
            industry=row.industry,
            industry_version=row.industry_version,  # type: ignore[arg-type]
            industry_as_of=row.source_as_of,  # type: ignore[arg-type]
            source=row.provenance,  # type: ignore[arg-type]
            market_status_code=row.market_status_code,
        )
        validate_security_state(state, checkpoint.code, cutoff=cutoff)
    except (TypeError, ValueError) as exc:
        raise DiscoveryPermanentCompanyError(
            checkpoint.code,
            "security_snapshot_invalid",
            str(exc),
        ) from exc
    return FrozenSecuritySnapshot(
        fill_id=fill.id,
        code=checkpoint.code,
        state=state,
        identity=_security_snapshot_identity(row),
    )


def _revalidate_frozen_security(
    session: Session,
    snapshot: FrozenSecuritySnapshot,
) -> None:
    columns = tuple(
        getattr(DiscoveryUniverseSecurity, field)
        for field in _SECURITY_SNAPSHOT_FIELDS
    )
    current = session.execute(
        select(*columns).where(
            DiscoveryUniverseSecurity.fill_id == snapshot.fill_id,
            DiscoveryUniverseSecurity.code == snapshot.code,
        )
    ).one_or_none()
    if current is None or tuple(current) != snapshot.identity:
        raise DiscoveryPermanentCompanyError(
            snapshot.code,
            "security_snapshot_mutated",
            "fill security snapshot changed during company fetch",
        )


def _supports_keyword(fetch: object, name: str) -> bool:
    try:
        parameters = signature(fetch).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name
        or parameter.kind == parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _supports_security_state(fetch: object) -> bool:
    return _supports_keyword(fetch, "security_state")


def _persist_company(
    session: Session,
    checkpoint: DiscoveryDataCheckpoint,
    result: DiscoveryCompanyData,
    now: datetime,
) -> None:
    fill = session.get(DiscoveryDataFill, checkpoint.fill_id)
    if (
        fill is None
        or fill.payload_schema_version != COMPANY_PAYLOAD_SCHEMA_VERSION
    ):
        raise ValueError(PAYLOAD_SCHEMA_RESTART_GUIDANCE)
    if result.code != checkpoint.code or any(
        row.code != checkpoint.code
        for row in (*result.financial_rows, *result.valuation_points)
    ):
        raise ValueError(f"{checkpoint.code} result code mismatch")
    for record in result.evidence_records:
        append_evidence(session, record)
    for window in result.coverage:
        append_coverage(session, window, captured_at=now)
    selected = load_evidence_as_of(session, result.code, now)
    windows = load_coverage(session, result.code, cutoff=now)
    selected_ids = {row.evidence_id for row in selected}
    references = load_coverage_references(session, result.code, now, windows)
    result = replace(result, evidence_records=selected + tuple(coverage_reference_evidence(row)
        for row in references if row.evidence_id not in selected_ids), coverage=windows)
    payload_json = serialize_company_payload(result, captured_at=now)
    payload_digest = company_payload_digest(payload_json)
    persist_discovery_financials(session, result.financial_rows)
    persist_valuation_history(session, result.valuation_points, fetched_at=now)
    checkpoint.status = "done"
    checkpoint.next_retry_at = None
    checkpoint.valuation_points = len(result.valuation_points)
    checkpoint.error = None
    checkpoint.payload_json = payload_json
    checkpoint.payload_digest = payload_digest
    checkpoint.updated_at = now


def execute_discovery_data_fill(
    session: Session,
    market: object,
    fill_id: int,
    *,
    policy: RequestPolicy = DEFAULT_POLICY,
    clock: FillClock | None = None,
    selection_fundamentals: bool = False,
    selection_source_dir: Path | None = None,
    operating_policy: Mapping[str, object] | None = None,
) -> None:
    timer = clock or SystemClock()
    run_started = _aware_utc(timer.now())
    try:
        fill = session.get(DiscoveryDataFill, fill_id)
        if fill is None:
            return
        if fill.payload_schema_version != COMPANY_PAYLOAD_SCHEMA_VERSION:
            _mark_snapshot_unavailable(
                fill,
                run_started,
                guidance=PAYLOAD_SCHEMA_RESTART_GUIDANCE,
            )
            session.commit()
            return
        if fill.status == "paused" and fill.snapshot_status is None:
            checkpoints, _snapshots, _has_dates, _payloads_complete = _snapshot_parts(
                session, fill.id
            )
            if not checkpoints:
                _mark_snapshot_unavailable(fill, run_started)
                session.commit()
                return
        fill.status = "running"
        fill.daily_budget = policy.daily_budget
        fill.run_started_at = run_started
        fill.updated_at = run_started
        session.commit()
        request_gate = DiscoveryRequestGate(
            session,
            fill.id,
            policy=policy,
            clock=timer,
        )
        if not _initialize_checkpoints(
            session, market, fill, request_gate
        ):
            return

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
                attempt_number = checkpoint.attempts + 1
                failure: Exception | None = None
                frozen_security: FrozenSecuritySnapshot | None = None
                try:
                    frozen_security = _load_frozen_security(
                        session, fill, checkpoint
                    )
                    fetch = market.fetch_discovery_data
                    supports_security_state = _supports_security_state(fetch)
                    selection_kwargs = {}
                    if selection_fundamentals:
                        if not _supports_keyword(fetch, 'selection_fundamentals'):
                            raise ValueError('market adapter does not support schema4 evidence fetching')
                        selection_kwargs['selection_fundamentals'] = True
                        selection_kwargs['selection_source_dir'] = selection_source_dir
                    if supports_security_state:
                        result = fetch(
                            checkpoint.code,
                            before_request=request_gate,
                            security_state=frozen_security.state,
                            **selection_kwargs,
                        )
                    else:
                        result = fetch(
                            checkpoint.code,
                            before_request=request_gate,
                            **selection_kwargs,
                        )
                except DiscoveryRequestStopped:
                    stop = True
                    break
                except Exception as exc:
                    checkpoint.attempts = attempt_number
                    failure = exc
                else:
                    checkpoint.attempts = attempt_number
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
                    elif frozen_security is not None and (
                        result.security_state != frozen_security.state
                        or result.source_as_of.security
                        != frozen_security.state.industry_as_of
                    ):
                        failure = DiscoveryPermanentCompanyError(
                            checkpoint.code,
                            "security_snapshot_mismatch",
                            "result security differs from fill snapshot",
                            request_count=actual,
                        )
                    try:
                        if failure is None:
                            if operating_policy is not None:
                                result = replace(result, operating_policy=operating_policy)
                            if frozen_security is not None:
                                _revalidate_frozen_security(
                                    session, frozen_security
                                )
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
                        checkpoint.attempts = attempt_number
                        failure = exc
                if failure is not None:
                    checkpoint.error = str(failure)
                    checkpoint.updated_at = _aware_utc(timer.now())
                    fill.last_error = str(failure)
                    if (
                        not isinstance(
                            failure, DiscoveryPermanentCompanyError
                        )
                        and checkpoint.attempts < policy.max_attempts
                    ):
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
            if fill.status in ("done", "failed"):
                unresolved = session.scalars(
                    select(DiscoveryDataCheckpoint)
                    .where(
                        DiscoveryDataCheckpoint.fill_id == fill.id,
                        DiscoveryDataCheckpoint.status != "done",
                        DiscoveryDataCheckpoint.error.is_not(None),
                    )
                    .order_by(
                        DiscoveryDataCheckpoint.updated_at.desc(),
                        DiscoveryDataCheckpoint.code,
                    )
                ).first()
                fill.last_error = None if unresolved is None else unresolved.error
        release(session, fill_id, JOB_TYPE_DISCOVERY_DATA)
        session.commit()


def _eta_seconds(
    session: Session,
    fill: DiscoveryDataFill,
    remaining: int,
) -> int | None:
    if fill.run_started_at is None:
        return None
    completed = session.scalars(
        select(DiscoveryDataCheckpoint)
        .where(
            DiscoveryDataCheckpoint.fill_id == fill.id,
            DiscoveryDataCheckpoint.status == "done",
            DiscoveryDataCheckpoint.updated_at >= fill.run_started_at,
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
            "snapshot_status": None,
            "restart_required": False,
            "guidance": None,
            **base,
        }
    if fill.payload_schema_version != COMPANY_PAYLOAD_SCHEMA_VERSION:
        pending = max(fill.total - fill.completed - fill.failed, 0)
        return {
            "id": fill.id,
            "type": JOB_TYPE_DISCOVERY_DATA,
            "status": SNAPSHOT_UNAVAILABLE_STATUS,
            "total": fill.total,
            "completed": fill.completed,
            "failed": fill.failed,
            "pending": pending,
            "retryable": 0,
            "running": 0,
            "request_count": fill.request_count,
            "daily_requests": fill.daily_requests,
            "budget_day": fill.budget_day,
            "last_error": PAYLOAD_SCHEMA_RESTART_GUIDANCE,
            "snapshot_status": "unavailable",
            "restart_required": True,
            "guidance": PAYLOAD_SCHEMA_RESTART_GUIDANCE,
            **base,
            "daily_budget": fill.daily_budget,
        }
    counts = _checkpoint_counts(session, fill.id)
    remaining = counts["pending"] + counts["retryable"] + counts["running"]
    restart_required = (
        fill.status == SNAPSHOT_UNAVAILABLE_STATUS
        or (fill.total > 0 and fill.snapshot_status != "ready")
    )
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
        "snapshot_status": fill.snapshot_status,
        "restart_required": restart_required,
        "guidance": (
            SNAPSHOT_UNAVAILABLE_GUIDANCE
            if restart_required
            else None
        ),
        **base,
        "daily_budget": fill.daily_budget,
        "eta_seconds": _eta_seconds(session, fill, remaining),
    }

_DISCOVERY_SHARED_FINANCIAL_FIELDS = (
    "roe",
    "net_income",
    "ocf",
    "capex",
    "interest_coverage",
    "gross_margin",
    "net_margin",
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
    "other_income",
    "asset_disposal_gain",
    "goodwill",
    "acquisition_cash_paid",
    "source_as_of",
)
_DISCOVERY_FINANCIAL_FIELDS = (
    _DISCOVERY_SHARED_FINANCIAL_FIELDS + _DISCOVERY_ONLY_FINANCIAL_FIELDS
)
_DISCOVERY_FINANCIAL_SOURCE = "discovery"


class ValuationMethodMismatchError(ValueError):
    pass


def persist_discovery_financials(
    session: Session,
    rows: Iterable[FinancialRow],
) -> None:
    codes: set[str] = set()
    for row in rows:
        untrusted = tuple(
            field
            for field, spec in FIELD_DICTIONARY.items()
            if not spec.trusted and getattr(row, field) is not None
        )
        if row.interest is not None:
            untrusted += ("interest",)
        if untrusted:
            raise ValueError(
                "discovery financial row contains untrusted fields: "
                + ", ".join(untrusted)
            )
        codes.add(row.code)
        key = {
            "code": row.code,
            "period": row.period,
            "period_type": row.period_type,
        }
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
            value = getattr(row, field)
            if field not in _DISCOVERY_SHARED_FINANCIAL_FIELDS or value is not None:
                setattr(stored, field, value)
        session.execute(
            delete(FinancialFieldProvenance).where(
                FinancialFieldProvenance.code == row.code,
                FinancialFieldProvenance.period == row.period,
                FinancialFieldProvenance.period_type == row.period_type,
                FinancialFieldProvenance.source
                == _DISCOVERY_FINANCIAL_SOURCE,
                FinancialFieldProvenance.field.in_(
                    _DISCOVERY_FINANCIAL_FIELDS
                ),
            )
        )
        provenance: list[FinancialFieldProvenance] = []
        for field in _DISCOVERY_FINANCIAL_FIELDS:
            if getattr(row, field) is None:
                continue
            spec = FIELD_DICTIONARY.get(field)
            provenance.append(
                FinancialFieldProvenance(
                    **key,
                    field=field,
                    source=_DISCOVERY_FINANCIAL_SOURCE,
                    source_report=(
                        spec.report
                        if spec is not None
                        and isinstance(spec.report, str)
                        else None
                    ),
                    source_field=(
                        spec.field
                        if spec is not None
                        and isinstance(spec.field, str)
                        else None
                    ),
                )
            )
        session.add_all(provenance)
    stamp = datetime.now(timezone.utc)
    for code in codes:
        session.merge(
            FinancialFetchCompleteness(
                code=code,
                mode="discovery",
                completed_at=stamp,
            )
        )


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
