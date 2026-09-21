from __future__ import annotations

from discovery.competition import CompetitionCandidate, rank_competition


ORDER = (
    ("valuation_percentile", "asc", "nulls_last"),
    ("roe_stability", "asc", "nulls_last"),
    ("cash_conversion", "desc", "nulls_last"),
    ("code", "asc", "nulls_last"),
)


def candidate(
    code: str,
    *,
    industry: str | None = "制造业",
    valuation: str | None = "20",
    stability: str | None = "0.1",
    cash: str | None = "1.2",
) -> CompetitionCandidate:
    return CompetitionCandidate(
        code=code,
        industry=industry,
        metrics={
            "valuation_percentile": valuation,
            "roe_stability": stability,
            "cash_conversion": cash,
            "code": code,
        },
    )


def test_ruler_order_is_lexicographic_with_stable_code_tiebreak() -> None:
    rows = rank_competition(
        (
            candidate("000004.SZ", cash="1.1"),
            candidate("000003.SZ", cash="1.3"),
            candidate("000002.SZ", valuation="10"),
            candidate("000001.SZ", cash="1.3"),
        ),
        ORDER,
        industry_cap=20,
    )

    assert [(row.code, row.rank) for row in rows] == [
        ("000002.SZ", 1),
        ("000001.SZ", 2),
        ("000003.SZ", 3),
        ("000004.SZ", 4),
    ]


def test_unknown_industry_is_insufficient_before_competition() -> None:
    rows = rank_competition(
        (
            candidate("000001.SZ", industry=None, valuation="1"),
            candidate("000002.SZ", industry="未知", valuation="2"),
            candidate("000003.SZ", valuation="3"),
        ),
        ORDER,
    )

    assert [(row.code, row.verdict, row.reason, row.rank) for row in rows] == [
        ("000003.SZ", "pass", None, 1),
        ("000001.SZ", "insufficient", "industry_unknown", None),
        ("000002.SZ", "insufficient", "industry_unknown", None),
    ]


def test_industry_and_total_caps_keep_exclusion_rank_and_reason() -> None:
    candidates = tuple(
        candidate(
            f"{index:06d}.SZ",
            industry=f"行业{index // 3}",
            valuation=str(index),
        )
        for index in range(18)
    )
    rows = rank_competition(candidates, ORDER)
    by_code = {row.code: row for row in rows}

    assert sum(row.verdict == "pass" for row in rows) == 12
    assert (
        by_code["000002.SZ"].verdict,
        by_code["000002.SZ"].reason,
        by_code["000002.SZ"].rank,
    ) == ("drop", "industry_cap_exceeded", 3)

    uncapped = tuple(
        candidate(
            f"{index:06d}.SZ",
            industry=f"行业{index}",
            valuation=str(index),
        )
        for index in range(18)
    )
    rows = rank_competition(uncapped, ORDER)
    assert sum(row.verdict == "pass" for row in rows) == 15
    assert [(row.reason, row.rank) for row in rows[-3:]] == [
        ("total_cap_exceeded", 16),
        ("total_cap_exceeded", 17),
        ("total_cap_exceeded", 18),
    ]
