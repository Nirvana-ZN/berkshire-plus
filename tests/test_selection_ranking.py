from __future__ import annotations

from decimal import Decimal as D, localcontext
from itertools import permutations

import pytest

from discovery.competition import CompetitionCandidate
from discovery.ranking import normalized_ranks, rank_candidates, ranking_audit


def contract(orders=None, **caps):
    return {
        "branch_orders": orders or {"nonfinancial": (("x", "asc"), ("y", "asc"))},
        "risk_policy": {
            "operating_states": {
                "improving": "clear", "stable": "clear", "watch": "watch",
                "unknown": "unknown", "deteriorating": "deteriorating",
            },
            "verified_risk_types": {"cash_warning": "watch", "decline": "deteriorating"},
        },
        "industry_cap": 2, "total_cap": 15, **caps,
    }


def candidate(code, *, branch="nonfinancial", industry=None, state="stable", risks=(), **values):
    return CompetitionCandidate(code, industry or f"industry-{code}", {
        "ranking_branch": branch, "operating_state": state,
        "verified_risk_types": risks, **values,
    })


def test_equal_values_share_midrank():
    assert normalized_ranks({"a": D("1"), "b": D("1"), "c": D("3")}, "asc") == {
        "a": D("0.25"), "b": D("0.25"), "c": D("1"),
    }


@pytest.mark.parametrize("values", [{}, {"a": None, "b": None}, {"a": D(2)}, {"a": D(2), "b": D(2)}])
def test_uninformative_dimension_has_no_contribution(values):
    assert normalized_ranks(values, "asc") == {}


@pytest.mark.parametrize("values", [
    {"a": D("Infinity")}, {"a": D("NaN"), "b": D("NaN")},
    {"a": D("-Infinity"), "b": D("-Infinity")},
])
def test_nonfinite_values_are_rejected_before_constant_shortcut(values):
    with pytest.raises(ValueError, match="finite"):
        normalized_ranks(values, "asc")


@pytest.mark.parametrize("value", [1, "1", 1.0, True])
def test_numeric_types_must_be_normalized_to_decimal(value):
    with pytest.raises(TypeError, match="Decimal"):
        normalized_ranks({"a": value}, "asc")


def test_invalid_direction_is_rejected_even_without_values():
    with pytest.raises(ValueError, match="direction"):
        normalized_ranks({}, "ascending")


def test_partial_missing_values_share_worst_midrank_in_both_directions():
    values = {"a": D(1), "b": None, "c": D(3), "d": None}
    assert normalized_ranks(values, "asc") == {
        "a": D(0), "c": D("0.3333333333333333333333333333333333"),
        "b": D("0.8333333333333333333333333333333333"), "d": D("0.8333333333333333333333333333333333"),
    }
    result = normalized_ranks(values, "desc")
    assert result["c"] == 0 and result["a"] < result["b"] == result["d"]


def test_three_stock_reproduction_empty_pb_does_not_change_scores_or_order():
    rows = (candidate("001", x=D(3), y=D(3)), candidate("002", x=D(2), y=D(1)), candidate("003", x=D(1), y=D(2)))
    with_pb = contract({"nonfinancial": (("x", "asc"), ("y", "asc"), ("pb", "asc"))})
    for order in permutations(rows):
        assert [v.code for v in rank_candidates(order, contract())] == ["002", "003", "001"]
        assert rank_candidates(order, contract()) == rank_candidates(order, with_pb)
        audit = ranking_audit(order, with_pb)
        assert {c: D(a["rank_score"]) for c, a in audit.items()} == {"001": D(1), "002": D("0.25"), "003": D("0.25")}
        assert audit["002"]["dimensions"]["pb"]["status"] == "all_missing"


def test_code_only_breaks_final_ties_and_cannot_change_economic_scores():
    original = (candidate("a", x=D(1), y=D(1)), candidate("z", x=D(2), y=D(2)))
    renamed = (candidate("z", x=D(1), y=D(1)), candidate("a", x=D(2), y=D(2)))
    assert [v.code for v in rank_candidates(original, contract())] == ["a", "z"]
    assert [v.code for v in rank_candidates(renamed, contract())] == ["z", "a"]
    assert ranking_audit(original, contract())["a"]["rank_score"] == ranking_audit(renamed, contract())["z"]["rank_score"] == "0"


def test_branch_applicability_and_missing_reasons_are_explicit():
    config = contract({"nonfinancial": (("cash", "desc"),), "bank": (("pb", "asc"),), "cyclical": (("pe", "asc"),)})
    rows = (
        candidate("n1", cash=D(2)), candidate("n2", cash=None, ranking_missing_reasons={"cash": "untrusted_source"}),
        candidate("b1", branch="bank", pb=D(1)), candidate("b2", branch="bank", pb=D(2)),
        candidate("c1", branch="cyclical", pe=D(8)),
    )
    audit = ranking_audit(rows, config)
    assert audit["b1"]["applicable_dimensions"] == ["pb"]
    assert "cash" not in audit["b1"]["dimensions"]
    assert audit["n2"]["dimensions"]["cash"]["missing_reason"] == "untrusted_source"
    assert audit["n2"]["rank_score"] == "1"
    assert audit["c1"]["rank_score"] == "0.5"
    assert audit["c1"]["ranking_uninformative"] is True


def test_audit_has_midrank_and_equal_weight_contributions_summing_to_score():
    rows = (candidate("a", x=D(1), y=D(2)), candidate("b", x=D(1), y=D(1)), candidate("c", x=D(3), y=D(3)))
    audit = ranking_audit(rows, contract())
    assert audit["a"]["dimensions"]["x"]["midrank"] == "1.5"
    assert audit["a"]["dimensions"]["x"]["rank_pct"] == "0.25"
    assert audit["a"]["dimensions"]["x"]["contribution"] == "0.125"
    assert audit["a"]["rank_score"] == "0.375"
    for item in audit.values():
        assert sum(D(d["contribution"]) for d in item["dimensions"].values()) == D(item["rank_score"])


def test_risk_tier_precedes_score_and_repeated_risk_types_do_not_add_penalties():
    rows = (
        candidate("clear", state="improving", x=D(99)),
        candidate("watch", risks=("cash_warning",), x=D(9)),
        candidate("unknown", state="unknown", x=D(2)),
        candidate("bad", state="deteriorating", x=D(1)),
    )
    assert [v.code for v in rank_candidates(rows, contract())] == ["clear", "watch", "unknown", "bad"]
    repeated = (*rows[:1], candidate("watch", risks=("cash_warning",) * 3, x=D(9)), *rows[2:])
    assert ranking_audit(rows, contract()) == ranking_audit(repeated, contract())
    assert ranking_audit(rows, contract())["watch"]["risk_state"] == "watch"


def test_missing_or_unrecognized_risk_evidence_is_unknown():
    rows = (
        CompetitionCandidate("missing", "industry-a", {"ranking_branch": "nonfinancial", "x": D(1)}),
        candidate("unmapped", risks=("new_risk",), x=D(1)),
        candidate("clear", x=D(1)),
    )
    audit = ranking_audit(rows, contract())
    assert audit["missing"]["risk_state"] == audit["unmapped"]["risk_state"] == "unknown"
    assert [v.code for v in rank_candidates(rows, contract())] == ["clear", "missing", "unmapped"]


def test_branch_is_required_and_never_inferred_from_legacy_metrics():
    row = CompetitionCandidate("a", "industry", {"quality_branch": "bank", "branch": "financial"})
    with pytest.raises(ValueError, match="ranking_branch"):
        rank_candidates((row,), contract())
    with pytest.raises(ValueError, match="ranking_branch"):
        rank_candidates((candidate("a", branch="other"),), contract())


def test_code_cannot_be_a_ranking_dimension():
    with pytest.raises(ValueError, match="code"):
        rank_candidates((candidate("a"),), contract({"nonfinancial": (("code", "asc"),)}))


def test_ranks_and_scores_are_independent_of_ambient_decimal_context():
    rows = tuple(candidate(str(i), x=D(i), y=D(4-i)) for i in range(4))
    baseline = ranking_audit(rows, contract())
    with localcontext() as context:
        context.prec = 2
        context.rounding = "ROUND_DOWN"
        assert ranking_audit(rows, contract()) == baseline


@pytest.mark.parametrize("state", ["improving", "stable", "unknown", "deteriorating"])
def test_risk_policy_cannot_change_binding_operating_state_semantics(state):
    config = contract()
    config["risk_policy"]["operating_states"][state] = "watch"
    with pytest.raises(ValueError, match="operating"):
        rank_candidates((candidate("a"),), config)


@pytest.mark.parametrize("caps", [{"industry_cap": 0}, {"total_cap": True}])
def test_invalid_caps_are_rejected(caps):
    with pytest.raises(ValueError, match="cap"):
        rank_candidates((candidate("a"),), contract(**caps))


def test_duplicate_codes_are_rejected_before_branch_grouping():
    with pytest.raises(ValueError, match="unique"):
        rank_candidates((candidate("a"), candidate("a", branch="bank")), contract())


def test_equal_cross_branch_scores_do_not_depend_on_dimension_count_or_rounding():
    config = contract({"nonfinancial": (("x", "asc"), ("y", "asc"), ("z", "asc")), "bank": (("x", "asc"), ("y", "asc"))})
    rows = (
        candidate("n-best", x=D(0), y=D(0), z=D(0)), candidate("z-worst", x=D(1), y=D(1), z=D(1)),
        candidate("b-best", branch="bank", x=D(0), y=D(0)), candidate("a-worst", branch="bank", x=D(1), y=D(1)),
    )
    audit = ranking_audit(rows, config)
    assert D(audit["z-worst"]["rank_score"]) == D(audit["a-worst"]["rank_score"]) == 1
    assert [v.code for v in rank_candidates(rows, config)] == ["b-best", "n-best", "a-worst", "z-worst"]
    with localcontext() as context:
        context.prec = 128
        for item in audit.values():
            assert sum(D(d["contribution"]) for d in item["dimensions"].values()) == D(item["rank_score"])


def test_cross_branch_recurring_rank_ties_use_code_only_at_the_end():
    config = contract({"nonfinancial": (("x", "asc"), ("y", "asc"), ("z", "asc")), "bank": (("x", "asc"), ("y", "asc"))})
    rows = tuple(
        candidate(f"{prefix}{i}", branch=branch, x=D(i), y=D(i), z=D(i))
        for branch, prefix in (("nonfinancial", "z"), ("bank", "a"))
        for i in range(4)
    )
    audit = ranking_audit(rows, config)
    assert D(audit["a1"]["rank_score"]) == D(audit["z1"]["rank_score"]) == D("0.3333333333333333333333333333333333")
    assert D(audit["a2"]["rank_score"]) == D(audit["z2"]["rank_score"]) == D("0.6666666666666666666666666666666667")
    assert [v.code for v in rank_candidates(rows, config)] == ["a0", "z0", "a1", "z1", "a2", "z2", "a3", "z3"]
