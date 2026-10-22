"""Generic deterministic competition over persisted track metrics."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key


UNKNOWN_INDUSTRIES = frozenset({"", "unknown", "未知", "未知行业"})

_TRAP_FLAGS = frozenset(
    {
        "thin_equity_high_pb",
        "goodwill_bloat",
        "recent_ocf_collapse",
        "consecutive_negative_fcf",
        "negative_5y_fcf",
        "annual_deducted_drop",
        "roe_step_down_3y",
    }
)


def _risk_sort_tier(metrics: Mapping[str, object]) -> tuple[int, int, float, int]:
    flags = tuple(str(f) for f in (metrics.get("risk_flags") or ()) if f)
    interim_flags = [f for f in flags if f.startswith("interim_")]
    trap_flags = [f for f in flags if f in _TRAP_FLAGS]
    yoy_raw = metrics.get("_interim_yoy")
    try:
        yoy = float(yoy_raw) if yoy_raw is not None else 0.0
    except (TypeError, ValueError):
        yoy = 0.0
    if not interim_flags:
        return (0, len(trap_flags), 0.0, len(flags))
    if interim_flags == ["interim_consecutive_decline"]:
        return (1, len(trap_flags), -yoy, len(flags))
    return (2, len(trap_flags), -yoy, len(flags))


@dataclass(frozen=True)
class CompetitionCandidate:
    code: str
    industry: str | None
    metrics: Mapping[str, object]


@dataclass(frozen=True)
class CompetitionVerdict:
    code: str
    verdict: str
    reason: str | None
    rank: int | None


def _value(value: object) -> object:
    if type(value) in {str, int}:
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return value
        return parsed if parsed.is_finite() else value
    return value


def _compare(left: object, right: object) -> int:
    left, right = _value(left), _value(right)
    if type(left) is not type(right):
        left, right = str(left), str(right)
    try:
        return (left > right) - (left < right)  # type: ignore[operator]
    except TypeError:
        left_text, right_text = str(left), str(right)
        return (left_text > right_text) - (left_text < right_text)


def _candidate_compare(
    order: tuple[tuple[str, str, str], ...],
    left: CompetitionCandidate,
    right: CompetitionCandidate,
    *,
    cap_roe: bool = False,
) -> int:
    for field, direction, null_order in order:
        left_value = left.code if field == "code" else left.metrics.get(field)
        right_value = right.code if field == "code" else right.metrics.get(field)
        if cap_roe and field == "roe_mean":
            lv, rv = _value(left_value), _value(right_value)
            if isinstance(lv, Decimal):
                left_value = min(lv, Decimal("30"))
            if isinstance(rv, Decimal):
                right_value = min(rv, Decimal("30"))
        if left_value is None or right_value is None:
            if left_value is right_value:
                continue
            return (
                -1
                if (left_value is None) == (null_order == "nulls_first")
                else 1
            )
        result = _compare(left_value, right_value)
        if result:
            return result if direction == "asc" else -result
    return _compare(left.code, right.code)


def _validated_order(
    order: Sequence[Sequence[str]],
) -> tuple[tuple[str, str, str], ...]:
    if isinstance(order, (str, bytes)):
        raise TypeError("competition order must be a sequence")
    result = []
    for item in order:
        if (
            isinstance(item, (str, bytes))
            or not isinstance(item, Sequence)
            or len(item) != 3
            or item[1] not in {"asc", "desc"}
            or item[2] not in {"nulls_first", "nulls_last"}
        ):
            raise ValueError("competition order entries must be valid triples")
        result.append((item[0], item[1], item[2]))
    if not result or result[-1] != ("code", "asc", "nulls_last"):
        raise ValueError("competition order must end with stable code ordering")
    if len({item[0] for item in result}) != len(result):
        raise ValueError("competition order fields must be unique")
    return tuple(result)


def _known_industry(industry: str | None) -> bool:
    return (
        type(industry) is str
        and industry.strip() == industry
        and industry.casefold() not in UNKNOWN_INDUSTRIES
    )


def rank_competition(
    candidates: Sequence[CompetitionCandidate],
    order: Sequence[Sequence[str]],
    *,
    industry_cap: int = 2,
    total_cap: int = 15,
) -> tuple[CompetitionVerdict, ...]:
    """Rank entrants from the ruler order, then apply deterministic caps."""

    if type(industry_cap) is not int or industry_cap < 1:
        raise ValueError("industry_cap must be a positive integer")
    if type(total_cap) is not int or total_cap < 1:
        raise ValueError("total_cap must be a positive integer")
    rows = tuple(candidates)
    if any(type(row) is not CompetitionCandidate for row in rows):
        raise TypeError("competition candidates must be immutable DTOs")
    if any(not isinstance(row.metrics, Mapping) for row in rows):
        raise TypeError("competition candidate metrics must be mappings")
    codes = [row.code for row in rows]
    if any(type(code) is not str or not code for code in codes):
        raise ValueError("competition candidate codes must be nonblank")
    if len(codes) != len(set(codes)):
        raise ValueError("competition candidate codes must be unique")

    ruler_order = _validated_order(order)
    eligible = sorted(
        (row for row in rows if _known_industry(row.industry)),
        key=cmp_to_key(lambda left, right: _candidate_compare(
            ruler_order, left, right
        )),
    )
    industries: Counter[str] = Counter()
    selected = 0
    verdicts = []
    for rank, row in enumerate(eligible, 1):
        assert row.industry is not None
        industries[row.industry] += 1
        if industries[row.industry] > industry_cap:
            verdict, reason = "drop", "industry_cap_exceeded"
        elif selected >= total_cap:
            verdict, reason = "drop", "total_cap_exceeded"
        else:
            verdict, reason = "pass", None
            selected += 1
        verdicts.append(CompetitionVerdict(row.code, verdict, reason, rank))
    verdicts.extend(
        CompetitionVerdict(row.code, "insufficient", "industry_unknown", None)
        for row in sorted(rows, key=lambda item: item.code)
        if not _known_industry(row.industry)
    )
    return tuple(verdicts)


def rank_borda_competition(
    candidates: Sequence[CompetitionCandidate],
    order: Sequence[Sequence[str]],
    *,
    industry_cap: int = 2,
    total_cap: int = 15,
) -> tuple[CompetitionVerdict, ...]:
    """Rank entrants by equal-weight Borda rank sum across ruler dimensions, breaking ties lexicographically."""

    if type(industry_cap) is not int or industry_cap < 1:
        raise ValueError("industry_cap must be a positive integer")
    if type(total_cap) is not int or total_cap < 1:
        raise ValueError("total_cap must be a positive integer")
    rows = tuple(candidates)
    if any(type(row) is not CompetitionCandidate for row in rows):
        raise TypeError("competition candidates must be immutable DTOs")
    if any(not isinstance(row.metrics, Mapping) for row in rows):
        raise TypeError("competition candidate metrics must be mappings")
    codes = [row.code for row in rows]
    if any(type(code) is not str or not code for code in codes):
        raise ValueError("competition candidate codes must be nonblank")
    if len(codes) != len(set(codes)):
        raise ValueError("competition candidate codes must be unique")

    ruler_order = _validated_order(order)
    dims = tuple(item for item in ruler_order if item[0] != "code")
    if len(dims) <= 1:
        return rank_competition(
            rows, order, industry_cap=industry_cap, total_cap=total_cap
        )
    known = [row for row in rows if _known_industry(row.industry)]
    lex_sorted = sorted(
        known,
        key=cmp_to_key(
            lambda left, right: _candidate_compare(
                ruler_order, left, right, cap_roe=True
            )
        ),
    )
    lex_rank = {row.code: idx for idx, row in enumerate(lex_sorted, 1)}
    dim_ranks: dict[str, dict[str, int]] = {}
    for dim in dims:
        single_order = (dim, ("code", "asc", "nulls_last"))
        sorted_dim = sorted(
            known,
            key=cmp_to_key(
                lambda left, right, so=single_order: _candidate_compare(
                    so, left, right, cap_roe=True
                )
            ),
        )
        dim_ranks[dim[0]] = {row.code: idx for idx, row in enumerate(sorted_dim, 1)}
    eligible = sorted(
        known,
        key=lambda row: (
            *_risk_sort_tier(row.metrics),
            sum(dim_ranks[d[0]][row.code] for d in dims),
            lex_rank[row.code],
            row.code,
        ),
    )
    industries: Counter[str] = Counter()
    selected = 0
    verdicts = []
    for rank, row in enumerate(eligible, 1):
        assert row.industry is not None
        industries[row.industry] += 1
        if industries[row.industry] > industry_cap:
            verdict, reason = "drop", "industry_cap_exceeded"
        elif selected >= total_cap:
            verdict, reason = "drop", "total_cap_exceeded"
        else:
            verdict, reason = "pass", None
            selected += 1
        verdicts.append(CompetitionVerdict(row.code, verdict, reason, rank))
    verdicts.extend(
        CompetitionVerdict(row.code, "insufficient", "industry_unknown", None)
        for row in sorted(rows, key=lambda item: item.code)
        if not _known_industry(row.industry)
    )
    return tuple(verdicts)

