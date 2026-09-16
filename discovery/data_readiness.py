from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import Financial, ValuationHistory
from market.cheap import CheapRow, _cached
from screen.funnel import _bank_or_insurance

QUALITY_DISCOUNT = "quality_discount"
DEEP_VALUE = "deep_value"
GARP = "garp"
TRACKS = (QUALITY_DISCOUNT, DEEP_VALUE, GARP)
MIN_VALUATION_MONTHS = 60


@dataclass(frozen=True)
class TrackReadiness:
    track: str
    ready: bool
    eligible: int
    complete: int
    coverage: Decimal
    missing_fields: tuple[str, ...]
    reason: str


def _persisted_universe(session: Session) -> tuple[list[CheapRow], float | None]:
    cached = _cached(session, 0, max_age=float("inf"))
    return ([], None) if cached is None else cached


def _base_eligible(row: CheapRow) -> bool:
    return (
        row.exchange in {"SH", "SZ"}
        and row.is_st is False
        and row.is_delisting is False
    )


def _listed_at_least_five_years(value: str | None, as_of: date) -> bool | None:
    try:
        listed = date.fromisoformat(value or "")
    except ValueError:
        return None
    years = as_of.year - listed.year
    return years > 5 or (
        years == 5 and (as_of.month, as_of.day) >= (listed.month, listed.day)
    )


def _latest_financials(
    session: Session, codes: tuple[str, ...]
) -> dict[str, dict[str, Decimal | str | None]]:
    if not codes:
        return {}
    latest = (
        select(
            Financial.code.label("code"),
            func.max(Financial.period).label("period"),
        )
        .where(
            Financial.period_type == "annual",
            Financial.code.in_(codes),
        )
        .group_by(Financial.code)
        .subquery()
    )
    rows = session.execute(
        select(
            Financial.code,
            Financial.capex,
            Financial.revenue,
            Financial.roic,
            Financial.capital_adequacy,
            Financial.npl,
            Financial.source_as_of,
        ).join(
            latest,
            and_(
                Financial.code == latest.c.code,
                Financial.period == latest.c.period,
                Financial.period_type == "annual",
            ),
        )
    )
    return {
        row.code: {
            "capex": row.capex,
            "revenue": row.revenue,
            "roic": row.roic,
            "capital_adequacy": row.capital_adequacy,
            "npl": row.npl,
            "source_as_of": row.source_as_of,
        }
        for row in rows
    }


def _valuation_counts(
    session: Session, codes: tuple[str, ...]
) -> dict[str, int]:
    if not codes:
        return {}
    valid = case(
        (
            and_(
                ValuationHistory.pe.is_not(None),
                ValuationHistory.pe > 0,
                ValuationHistory.pb.is_not(None),
                ValuationHistory.pb > 0,
            ),
            1,
        ),
        else_=0,
    )
    rows = session.execute(
        select(
            ValuationHistory.code,
            ValuationHistory.ratio_method,
            func.sum(valid).label("valid_months"),
        )
        .where(ValuationHistory.code.in_(codes))
        .group_by(
            ValuationHistory.code,
            ValuationHistory.ratio_method,
        )
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.code] = max(counts.get(row.code, 0), int(row.valid_months or 0))
    return counts


def _thresholds(
    approved_thresholds: Mapping[str, Decimal] | None,
) -> dict[str, Decimal]:
    thresholds: dict[str, Decimal] = {}
    for track, raw in (approved_thresholds or {}).items():
        if track not in TRACKS:
            raise ValueError(f"unknown discovery track: {track}")
        value = Decimal(raw)
        if not value.is_finite() or value < 0 or value > 1:
            raise ValueError(f"{track} threshold must be between 0 and 1")
        thresholds[track] = value
    return thresholds


def _track(
    track: str,
    *,
    eligible: int,
    complete: int,
    missing: Counter[str],
    thresholds: Mapping[str, Decimal],
) -> TrackReadiness:
    coverage = (
        Decimal(complete) / Decimal(eligible) if eligible else Decimal("0")
    )
    threshold = thresholds.get(track)
    if threshold is None:
        ready = False
        reason = "threshold_not_activated"
    else:
        ready = coverage >= threshold
        reason = "ready" if ready else "data_not_ready"
    return TrackReadiness(
        track=track,
        ready=ready,
        eligible=eligible,
        complete=complete,
        coverage=coverage,
        missing_fields=tuple(sorted(missing)),
        reason=reason,
    )


def _iso_utc_timestamp(value: float | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    stamp = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return stamp.astimezone(timezone.utc).isoformat()


def discovery_data_readiness(
    session: Session,
    *,
    as_of: date,
    approved_thresholds: Mapping[str, Decimal] | None = None,
) -> dict[str, object]:
    """Measure persisted track inputs without activating a discovery ruler."""
    universe, universe_fetched_at = _persisted_universe(session)
    candidates = [row for row in universe if _base_eligible(row)]
    codes = tuple(row.code for row in candidates)
    financials = _latest_financials(session, codes)
    valuation_counts = _valuation_counts(session, codes)
    thresholds = _thresholds(approved_thresholds)

    eligible = Counter[str]()
    complete = Counter[str]()
    missing = {track: Counter[str]() for track in TRACKS}
    insufficient = {track: Counter[str]() for track in TRACKS}

    for company in candidates:
        listing_age = _listed_at_least_five_years(company.listing_date, as_of)
        if listing_age is False:
            insufficient[QUALITY_DISCOUNT]["recent_listing"] += 1
        else:
            eligible[QUALITY_DISCOUNT] += 1
            quality_missing = []
            if listing_age is None:
                quality_missing.append("listing_date")
            if valuation_counts.get(company.code, 0) < MIN_VALUATION_MONTHS:
                quality_missing.append("valuation_history")
            if quality_missing:
                missing[QUALITY_DISCOUNT].update(quality_missing)
            else:
                complete[QUALITY_DISCOUNT] += 1

        eligible[DEEP_VALUE] += 1
        financial = financials.get(company.code, {})
        deep_missing: list[str] = []
        if not company.industry or company.industry == "-":
            deep_missing.append("industry")
        elif _bank_or_insurance(company.industry):
            deep_missing.extend(
                field
                for field in ("capital_adequacy", "npl")
                if financial.get(field) is None
            )
        elif financial.get("capex") is None:
            deep_missing.append("capex")
        if deep_missing:
            missing[DEEP_VALUE].update(deep_missing)
        else:
            complete[DEEP_VALUE] += 1

        eligible[GARP] += 1
        garp_missing = [
            field
            for field in ("revenue", "roic")
            if financial.get(field) is None
        ]
        if garp_missing:
            missing[GARP].update(garp_missing)
        else:
            complete[GARP] += 1

    tracks = {
        track: _track(
            track,
            eligible=eligible[track],
            complete=complete[track],
            missing=missing[track],
            thresholds=thresholds,
        )
        for track in TRACKS
    }
    if any(track.reason == "data_not_ready" for track in tracks.values()):
        status = "data_not_ready"
    elif len(thresholds) == len(TRACKS):
        status = "ready"
    else:
        status = "threshold_not_activated"

    if codes:
        valuation_as_of, valuation_fetched_at = session.execute(
            select(
                func.max(ValuationHistory.trade_date),
                func.max(ValuationHistory.fetched_at),
            ).where(ValuationHistory.code.in_(codes))
        ).one()
    else:
        valuation_as_of = valuation_fetched_at = None
    financial_as_of = max(
        (
            str(row["source_as_of"])
            for row in financials.values()
            if row["source_as_of"]
        ),
        default=None,
    )
    security_as_of = max(
        (row.industry_as_of for row in candidates if row.industry_as_of),
        default=None,
    )
    return {
        "status": status,
        "threshold_activated": len(thresholds) == len(TRACKS),
        "approved_thresholds": thresholds,
        "tracks": tracks,
        "missing_counts": {
            track: dict(sorted(missing[track].items())) for track in TRACKS
        },
        "company_insufficient": {
            track: dict(sorted(insufficient[track].items())) for track in TRACKS
        },
        "source_as_of": {
            "universe": _iso_utc_timestamp(universe_fetched_at),
            "security": security_as_of,
            "financial": financial_as_of,
            "valuation": valuation_as_of,
            "valuation_fetched_at": _iso_utc_timestamp(valuation_fetched_at),
        },
    }
