from __future__ import annotations

from calendar import monthrange
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import Financial, ValuationHistory
from discovery.field_dictionary import FIELD_DICTIONARY
from market.cheap import CheapRow, _cached

QUALITY_DISCOUNT = "quality_discount"
DEEP_VALUE = "deep_value"
GARP = "garp"
TRACKS = (QUALITY_DISCOUNT, DEEP_VALUE, GARP)
MIN_VALUATION_MONTHS = 60

_FINANCIAL_FIELDS = (
    "capex",
    "revenue",
    "roic",
    "capital_adequacy",
    "npl",
)
_ALLOWED_MINIMUMS = {
    QUALITY_DISCOUNT: frozenset({"pe", "pb"}),
    DEEP_VALUE: frozenset({"capex", "capital_adequacy", "npl"}),
    GARP: frozenset({"revenue", "roic"}),
}
_MEASUREMENT_MINIMUMS = {
    QUALITY_DISCOUNT: {"pe": MIN_VALUATION_MONTHS, "pb": MIN_VALUATION_MONTHS},
    DEEP_VALUE: {"capex": 1, "capital_adequacy": 1, "npl": 1},
    GARP: {"revenue": 1, "roic": 1},
}
_VALUATION_POLICIES = frozenset({"both", "either", "pe", "pb"})
_OTHER_FINANCIAL_TOKENS = ("证券", "多元金融", "信托", "期货", "金融")
_FINANCIAL_DEEP_FIELDS = tuple(
    field
    for field in ("capital_adequacy",)
    if FIELD_DICTIONARY[field].trusted
    and FIELD_DICTIONARY[field].financial_sector_substitute
)
_BANK_DEEP_FIELDS = _FINANCIAL_DEEP_FIELDS + tuple(
    field
    for field in ("npl",)
    if FIELD_DICTIONARY[field].trusted
    and FIELD_DICTIONARY[field].financial_sector_substitute
)


@dataclass(frozen=True)
class TrackReadiness:
    track: str
    ready: bool
    eligible: int
    complete: int
    coverage: Decimal
    missing_fields: tuple[str, ...]
    reason: str
    activated: bool
    threshold: Decimal | None


@dataclass(frozen=True)
class ValuationObservationCounts:
    pe: int
    pb: int


@dataclass(frozen=True)
class CompanyObservationCounts:
    branch: str
    financial: Mapping[str, int]
    aligned: Mapping[str, int]
    valuation: Mapping[str, ValuationObservationCounts]


@dataclass(frozen=True)
class ReadinessReport:
    status: str
    evaluated_as_of: str
    tracks: Mapping[str, TrackReadiness]
    approved_thresholds: Mapping[str, Decimal]
    minimum_observations: Mapping[str, Mapping[str, int]]
    valuation_policies: Mapping[str, str]
    missing_counts: Mapping[str, Mapping[str, int]]
    company_insufficient: Mapping[str, Mapping[str, int]]
    observations: Mapping[str, CompanyObservationCounts]
    source_as_of: Mapping[str, str | None]


def _thresholds(
    approved_thresholds: Mapping[str, Decimal] | None,
) -> dict[str, Decimal]:
    thresholds: dict[str, Decimal] = {}
    for track, raw in (approved_thresholds or {}).items():
        if track not in TRACKS:
            raise ValueError(f"unknown discovery track: {track}")
        if not isinstance(raw, Decimal):
            raise ValueError(f"{track} threshold must be a Decimal")
        value = raw
        if not value.is_finite() or value < 0 or value > 1:
            raise ValueError(f"{track} threshold must be between 0 and 1")
        thresholds[track] = value
    return thresholds


def _minimums(
    minimum_observations: Mapping[str, Mapping[str, int]] | None,
) -> dict[str, dict[str, int]]:
    configured: dict[str, dict[str, int]] = {}
    for track, fields in (minimum_observations or {}).items():
        if track not in TRACKS:
            raise ValueError(f"unknown discovery track: {track}")
        if not isinstance(fields, Mapping):
            raise ValueError(f"{track} observation requirements must be a mapping")
        parsed: dict[str, int] = {}
        for field, minimum in fields.items():
            if field not in _ALLOWED_MINIMUMS[track]:
                raise ValueError(f"unknown observation field: {track}.{field}")
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
                raise ValueError(
                    f"{track}.{field} minimum observations must be a positive integer"
                )
            parsed[field] = minimum
        configured[track] = parsed
    return configured


def _policies(
    valuation_policies: Mapping[str, str] | None,
) -> dict[str, str]:
    policies: dict[str, str] = {}
    for track, policy in (valuation_policies or {}).items():
        if track != QUALITY_DISCOUNT:
            raise ValueError(
                f"valuation policy only supports {QUALITY_DISCOUNT}: {track}"
            )
        if policy not in _VALUATION_POLICIES:
            raise ValueError(f"unknown valuation policy: {policy}")
        policies[track] = policy
    return policies


def _persisted_universe(
    session: Session, as_of: date
) -> tuple[list[CheapRow], float | None]:
    cached = _cached(session, 0, max_age=float("inf"))
    if cached is None:
        return [], None
    rows, fetched_at = cached
    if datetime.fromtimestamp(fetched_at, timezone.utc).date() > as_of:
        return [], None
    return rows, fetched_at


def _base_eligible(row: CheapRow) -> bool:
    return (
        row.exchange in {"SH", "SZ"}
        and row.is_st is False
        and row.is_delisting is False
    )


def _industry_branch(industry: str | None) -> str:
    value = (industry or "").strip()
    if not value or value == "-":
        return "unknown"
    if "保险" in value:
        return "insurance"
    if "银行" in value and "非银行" not in value:
        return "bank"
    if any(token in value for token in _OTHER_FINANCIAL_TOKENS):
        return "other_financial"
    return "nonfinancial"


def _listing_age(value: str | None, as_of: date) -> str:
    try:
        listed = date.fromisoformat(value or "")
    except ValueError:
        return "invalid"
    if listed > as_of:
        return "invalid"
    years = as_of.year - listed.year
    old_enough = years > 5 or (
        years == 5 and (as_of.month, as_of.day) >= (listed.month, listed.day)
    )
    return "eligible" if old_enough else "recent"


def _period_end(value: str) -> date | None:
    text = (value or "").strip()
    try:
        if len(text) == 4 and text.isdigit():
            return date(int(text), 12, 31)
        if len(text) == 7:
            year, month = (int(part) for part in text.split("-"))
            return date(year, month, monthrange(year, month)[1])
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None


def _source_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _financial_history(
    session: Session,
    codes: tuple[str, ...],
    as_of: date,
) -> tuple[
    dict[str, dict[str, dict[str, Decimal | None]]],
    str | None,
]:
    histories: dict[str, dict[str, dict[str, Decimal | None]]] = {
        code: {} for code in codes
    }
    if not codes:
        return histories, None
    source_dates: list[str] = []
    rows = session.execute(
        select(
            Financial.code,
            Financial.period,
            Financial.capex,
            Financial.revenue,
            Financial.roic,
            Financial.capital_adequacy,
            Financial.npl,
            Financial.source_as_of,
        ).where(
            Financial.period_type == "annual",
            Financial.code.in_(codes),
        )
    )
    for row in rows:
        period_end = _period_end(row.period)
        source_date = _source_date(row.source_as_of)
        if (
            period_end is None
            or source_date is None
            or period_end > as_of
            or source_date > as_of
        ):
            continue
        histories[row.code][row.period] = {
            field: getattr(row, field) for field in _FINANCIAL_FIELDS
        }
        source_dates.append(row.source_as_of)
    return histories, max(source_dates, default=None)


def _five_year_start(as_of: date) -> date:
    day = min(as_of.day, monthrange(as_of.year - 5, as_of.month)[1])
    return date(as_of.year - 5, as_of.month, day)


def _valuation_history(
    session: Session,
    codes: tuple[str, ...],
    as_of: date,
) -> tuple[
    dict[str, dict[str, ValuationObservationCounts]],
    str | None,
    datetime | None,
]:
    histories: dict[str, dict[str, ValuationObservationCounts]] = {
        code: {} for code in codes
    }
    if not codes:
        return histories, None, None
    valid_pe = case(
        (
            and_(
                ValuationHistory.pe.is_not(None),
                ValuationHistory.pe > 0,
            ),
            1,
        ),
        else_=0,
    )
    valid_pb = case(
        (
            and_(
                ValuationHistory.pb.is_not(None),
                ValuationHistory.pb > 0,
            ),
            1,
        ),
        else_=0,
    )
    end = datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=timezone.utc)
    rows = session.execute(
        select(
            ValuationHistory.code,
            ValuationHistory.ratio_method,
            func.sum(valid_pe).label("pe_count"),
            func.sum(valid_pb).label("pb_count"),
            func.max(ValuationHistory.trade_date).label("trade_date"),
            func.max(ValuationHistory.fetched_at).label("fetched_at"),
        )
        .where(
            ValuationHistory.code.in_(codes),
            ValuationHistory.trade_date >= _five_year_start(as_of).isoformat(),
            ValuationHistory.trade_date <= as_of.isoformat(),
            ValuationHistory.fetched_at < end,
        )
        .group_by(
            ValuationHistory.code,
            ValuationHistory.ratio_method,
        )
    )
    trade_dates: list[str] = []
    fetched_dates: list[datetime] = []
    for row in rows:
        histories[row.code][row.ratio_method] = ValuationObservationCounts(
            pe=int(row.pe_count or 0),
            pb=int(row.pb_count or 0),
        )
        if row.trade_date:
            trade_dates.append(row.trade_date)
        if row.fetched_at:
            fetched_dates.append(row.fetched_at)
    return (
        histories,
        max(trade_dates, default=None),
        max(fetched_dates, default=None),
    )


def _financial_counts(
    periods: Mapping[str, Mapping[str, Decimal | None]],
) -> dict[str, int]:
    return {
        field: sum(row[field] is not None for row in periods.values())
        for field in _FINANCIAL_FIELDS
    }


def _aligned_count(
    periods: Mapping[str, Mapping[str, Decimal | None]],
    fields: tuple[str, ...],
) -> int:
    return sum(
        all(row[field] is not None for field in fields)
        for row in periods.values()
    )


def _financial_complete(
    counts: Mapping[str, int],
    aligned: int,
    fields: tuple[str, ...],
    minimums: Mapping[str, int],
) -> tuple[bool, tuple[str, ...]]:
    missing = tuple(
        field for field in fields if counts[field] < minimums[field]
    )
    if missing:
        return False, missing
    if aligned < min(minimums[field] for field in fields):
        return False, ("period_alignment",)
    return True, ()


def _valuation_complete(
    methods: Mapping[str, ValuationObservationCounts],
    minimums: Mapping[str, int],
    policy: str,
) -> tuple[bool, tuple[str, ...]]:
    pe_min = minimums.get("pe", 0)
    pb_min = minimums.get("pb", 0)

    def satisfied(counts: ValuationObservationCounts) -> bool:
        if policy == "pe":
            return counts.pe >= pe_min
        if policy == "pb":
            return counts.pb >= pb_min
        if policy == "either":
            return counts.pe >= pe_min or counts.pb >= pb_min
        return counts.pe >= pe_min and counts.pb >= pb_min

    if any(satisfied(counts) for counts in methods.values()):
        return True, ()
    max_pe = max((counts.pe for counts in methods.values()), default=0)
    max_pb = max((counts.pb for counts in methods.values()), default=0)
    missing: list[str] = []
    if policy in {"both", "pe"} and max_pe < pe_min:
        missing.append("valuation_pe")
    if policy in {"both", "pb"} and max_pb < pb_min:
        missing.append("valuation_pb")
    if policy == "either" and max_pe < pe_min and max_pb < pb_min:
        missing.append("valuation_pe_or_pb")
    if not missing:
        missing.append("valuation_method_alignment")
    return False, tuple(missing)


def _effective_minimums(
    configured: Mapping[str, Mapping[str, int]],
    track: str,
) -> dict[str, int]:
    return dict(configured.get(track, _MEASUREMENT_MINIMUMS[track]))


def _track_activated(
    track: str,
    thresholds: Mapping[str, Decimal],
    configured: Mapping[str, Mapping[str, int]],
    policies: Mapping[str, str],
) -> bool:
    if track not in thresholds or track not in configured:
        return False
    if track == QUALITY_DISCOUNT:
        policy = policies.get(track)
        if policy is None:
            return False
        required = (
            {"pe", "pb"}
            if policy == "both"
            else {policy}
            if policy in {"pe", "pb"}
            else {"pe", "pb"}
        )
        return required <= configured[track].keys()
    return _ALLOWED_MINIMUMS[track] <= configured[track].keys()


def _track(
    track: str,
    *,
    eligible: int,
    complete: int,
    missing: Counter[str],
    threshold: Decimal | None,
    activated: bool,
) -> TrackReadiness:
    coverage = (
        Decimal(complete) / Decimal(eligible) if eligible else Decimal("0")
    )
    ready = bool(activated and threshold is not None and coverage >= threshold)
    reason = (
        "not_activated"
        if not activated
        else "ready"
        if ready
        else "data_not_ready"
    )
    return TrackReadiness(
        track=track,
        ready=ready,
        eligible=eligible,
        complete=complete,
        coverage=coverage,
        missing_fields=tuple(sorted(missing)),
        reason=reason,
        activated=activated,
        threshold=threshold,
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
    minimum_observations: Mapping[str, Mapping[str, int]] | None = None,
    valuation_policies: Mapping[str, str] | None = None,
) -> ReadinessReport:
    """Measure only information that was persisted and knowable by ``as_of``."""
    thresholds = _thresholds(approved_thresholds)
    configured_minimums = _minimums(minimum_observations)
    policies = _policies(valuation_policies)
    universe, universe_fetched_at = _persisted_universe(session, as_of)
    candidates = [row for row in universe if _base_eligible(row)]
    codes = tuple(row.code for row in candidates)
    financials, financial_as_of = _financial_history(session, codes, as_of)
    valuations, valuation_as_of, valuation_fetched_at = _valuation_history(
        session, codes, as_of
    )

    eligible = Counter[str]()
    complete = Counter[str]()
    missing = {track: Counter[str]() for track in TRACKS}
    insufficient = {track: Counter[str]() for track in TRACKS}
    observations: dict[str, CompanyObservationCounts] = {}

    for company in candidates:
        branch = _industry_branch(company.industry)
        financial_counts = _financial_counts(financials[company.code])
        aligned: dict[str, int] = {}

        listing_age = _listing_age(company.listing_date, as_of)
        if listing_age == "recent":
            insufficient[QUALITY_DISCOUNT]["recent_listing"] += 1
        else:
            eligible[QUALITY_DISCOUNT] += 1
            quality_missing: tuple[str, ...]
            if listing_age == "invalid":
                quality_missing = ("listing_date",)
                insufficient[QUALITY_DISCOUNT]["invalid_listing_date"] += 1
            else:
                quality_minimums = _effective_minimums(
                    configured_minimums, QUALITY_DISCOUNT
                )
                quality_policy = policies.get(QUALITY_DISCOUNT, "both")
                _, quality_missing = _valuation_complete(
                    valuations[company.code],
                    quality_minimums,
                    quality_policy,
                )
            if quality_missing:
                missing[QUALITY_DISCOUNT].update(quality_missing)
            else:
                complete[QUALITY_DISCOUNT] += 1

        deep_minimums = _effective_minimums(configured_minimums, DEEP_VALUE)
        eligible[DEEP_VALUE] += 1
        if branch == "unknown":
            deep_missing = ("industry",)
            aligned[DEEP_VALUE] = 0
        elif branch == "other_financial":
            deep_missing = ("financial_branch_source",)
            aligned[DEEP_VALUE] = 0
            insufficient[DEEP_VALUE]["unsupported_financial_branch"] += 1
        else:
            deep_fields = (
                _BANK_DEEP_FIELDS
                if branch == "bank"
                else _FINANCIAL_DEEP_FIELDS
                if branch == "insurance"
                else ("capex",)
            )
            aligned[DEEP_VALUE] = _aligned_count(
                financials[company.code], deep_fields
            )
            _, deep_missing = _financial_complete(
                financial_counts,
                aligned[DEEP_VALUE],
                deep_fields,
                deep_minimums,
            )
        if deep_missing:
            missing[DEEP_VALUE].update(deep_missing)
        else:
            complete[DEEP_VALUE] += 1

        if branch in {"bank", "insurance", "other_financial"}:
            insufficient[GARP]["financial_industry"] += 1
        else:
            eligible[GARP] += 1
            if branch == "unknown":
                garp_missing = ("industry",)
                aligned[GARP] = 0
            else:
                garp_fields = ("revenue", "roic")
                garp_minimums = _effective_minimums(configured_minimums, GARP)
                aligned[GARP] = _aligned_count(
                    financials[company.code], garp_fields
                )
                _, garp_missing = _financial_complete(
                    financial_counts,
                    aligned[GARP],
                    garp_fields,
                    garp_minimums,
                )
            if garp_missing:
                missing[GARP].update(garp_missing)
            else:
                complete[GARP] += 1

        observations[company.code] = CompanyObservationCounts(
            branch=branch,
            financial=financial_counts,
            aligned=aligned,
            valuation=valuations[company.code],
        )

    tracks = {
        track: _track(
            track,
            eligible=eligible[track],
            complete=complete[track],
            missing=missing[track],
            threshold=thresholds.get(track),
            activated=_track_activated(
                track,
                thresholds,
                configured_minimums,
                policies,
            ),
        )
        for track in TRACKS
    }
    activated = [track for track in tracks.values() if track.activated]
    if any(track.reason == "data_not_ready" for track in activated):
        status = "data_not_ready"
    elif len(activated) == len(TRACKS):
        status = "ready"
    elif activated:
        status = "partially_activated"
    else:
        status = "not_activated"

    security_as_of = max(
        (
            row.industry_as_of
            for row in candidates
            if row.industry_as_of
            and _source_date(row.industry_as_of)
            and _source_date(row.industry_as_of) <= as_of
        ),
        default=None,
    )
    return ReadinessReport(
        status=status,
        evaluated_as_of=as_of.isoformat(),
        tracks=tracks,
        approved_thresholds=thresholds,
        minimum_observations=configured_minimums,
        valuation_policies=policies,
        missing_counts={
            track: dict(sorted(missing[track].items())) for track in TRACKS
        },
        company_insufficient={
            track: dict(sorted(insufficient[track].items())) for track in TRACKS
        },
        observations=observations,
        source_as_of={
            "universe": _iso_utc_timestamp(universe_fetched_at),
            "security": security_as_of,
            "financial": financial_as_of,
            "valuation": valuation_as_of,
            "valuation_fetched_at": _iso_utc_timestamp(valuation_fetched_at),
        },
    )
