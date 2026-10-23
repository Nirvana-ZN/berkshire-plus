"""Branch-local research queue ranks; these are not absolute quality scores.

The integration boundary must supply ``metrics.ranking_branch`` explicitly;
legacy ``branch`` and ``quality_branch`` are deliberately not consulted.
Rank values must already be finite Decimal (or None). ``branch_orders`` maps
that branch to (metric, asc/desc) pairs; omission means not applicable.
``risk_policy`` maps operating_states and verified_risk_types to risk tiers.
Missing/unmapped risk evidence is unknown; an explicit empty verified list is
known to contain no verified risks. Final ordering is risk tier, score, code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import (
    Context, Decimal, DivisionByZero, InvalidOperation, Overflow,
    ROUND_HALF_EVEN, localcontext,
)

from discovery.competition import (
    CompetitionCandidate,
    CompetitionVerdict,
    _apply_caps,
    _known_industry,
    _validated_candidates,
)


_TIERS = {"clear": 0, "watch": 1, "unknown": 2, "deteriorating": 3}
_OPERATING_STATES = {
    "improving": "clear", "stable": "clear", "unknown": "unknown",
    "deteriorating": "deteriorating",
}
_PRECISION = 34


def _context(precision: int = _PRECISION) -> Context:
    # Construct a context, rather than inheriting caller rounding or traps.
    return Context(
        prec=precision, rounding=ROUND_HALF_EVEN,
        Emax=999999, Emin=-999999, flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def _dimension_ranks(
    values: Mapping[str, Decimal | None], direction: str,
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    if direction not in {"asc", "desc"}:
        raise ValueError("invalid rank direction")
    for value in values.values():
        if value is not None and not isinstance(value, Decimal):
            raise TypeError("rank values must be Decimal or None")
        if value is not None and not value.is_finite():
            raise ValueError("rank values must be finite")
    if not values or len(set(values.values())) == 1:
        return {}, {}
    groups: dict[Decimal | None, list[str]] = {}
    for code, value in values.items():
        groups.setdefault(value, []).append(code)
    keys = sorted((v for v in groups if v is not None), reverse=direction == "desc")
    if None in groups:
        keys.append(None)
    ranks, scores = {}, {}
    start, n = 1, len(values)
    with localcontext(_context()):
        for value in keys:
            codes = groups[value]
            midrank = Decimal(2 * start + len(codes) - 1) / 2
            score = (midrank - 1) / (n - 1)
            ranks.update(dict.fromkeys(codes, midrank))
            scores.update(dict.fromkeys(codes, score))
            start += len(codes)
    return ranks, scores


def normalized_ranks(
    values: Mapping[str, Decimal | None], direction: str,
) -> dict[str, Decimal]:
    """Return normalized midranks (lower is better), omitting constant columns.

    Missing observations share the worst occupied ranks in either direction.
    Validation precedes the constant/singleton shortcut. Arithmetic uses a
    bounded 34-digit local Decimal context, independent of caller settings.
    """
    return _dimension_ranks(values, direction)[1]


def _validated_contract(contract: Mapping[str, object]):
    orders = contract.get("branch_orders")
    policy = contract.get("risk_policy")
    if not isinstance(orders, Mapping) or not orders:
        raise ValueError("branch_orders must be a nonempty mapping")
    normalized = {}
    for branch, order in orders.items():
        if not isinstance(branch, str) or not branch:
            raise ValueError("branch names must be nonblank strings")
        if isinstance(order, (str, bytes)) or not isinstance(order, Sequence):
            raise ValueError("branch order must contain metric/direction pairs")
        dimensions = []
        for item in order:
            if (isinstance(item, (str, bytes)) or not isinstance(item, Sequence)
                    or len(item) != 2 or not isinstance(item[0], str)
                    or not item[0] or item[1] not in {"asc", "desc"}):
                raise ValueError("branch order must contain metric/direction pairs")
            if item[0] == "code":
                raise ValueError("code is only a final tie break")
            dimensions.append(tuple(item))
        if len({d[0] for d in dimensions}) != len(dimensions):
            raise ValueError("branch dimensions must be unique")
        normalized[branch] = tuple(dimensions)
    if not isinstance(policy, Mapping):
        raise ValueError("risk_policy must be a mapping")
    states, risks = policy.get("operating_states"), policy.get("verified_risk_types")
    if not isinstance(states, Mapping) or not isinstance(risks, Mapping):
        raise ValueError("risk_policy needs operating_states and verified_risk_types mappings")
    if any(states.get(state) != tier for state, tier in _OPERATING_STATES.items()):
        raise ValueError("operating state policy must preserve binding semantics")
    for mapping in (states, risks):
        if any(not isinstance(k, str) or not k or v not in _TIERS for k, v in mapping.items()):
            raise ValueError("risk policy entries must map names to risk tiers")
    return normalized, states, risks


def _risk_audit(metrics, states, risks):
    state = metrics.get("operating_state")
    if state is not None and not isinstance(state, str):
        raise TypeError("operating_state must be a string or None")
    missing = [] if state in states else ["operating_state"]
    tiers = [states.get(state, "unknown")]
    raw_types = metrics.get("verified_risk_types")
    if raw_types is None:
        types = []
        missing.append("verified_risk_types")
        tiers.append("unknown")
    else:
        if (isinstance(raw_types, (str, bytes)) or not isinstance(raw_types, Sequence)
                or any(not isinstance(item, str) or not item for item in raw_types)):
            raise TypeError("verified_risk_types must be a sequence of nonblank strings")
        types = sorted(set(raw_types))
        for risk in types:
            tiers.append(risks.get(risk, "unknown"))
            if risk not in risks:
                missing.append(f"verified_risk_types:{risk}")
    return {
        "operating_state": state, "verified_risk_types": types,
        "risk_state": max(tiers, key=_TIERS.__getitem__),
        "risk_evidence_missing": missing,
    }


def _calculate(candidates, contract):
    industry_cap, total_cap = contract.get("industry_cap"), contract.get("total_cap")
    rows = _validated_candidates(candidates, industry_cap, total_cap)
    orders, states, risks = _validated_contract(contract)
    audit, branches, scores = {}, {}, {}
    for row in rows:
        branch = row.metrics.get("ranking_branch")
        if not isinstance(branch, str) or branch not in orders:
            raise ValueError("ranking_branch must explicitly name a configured branch")
        audit[row.code] = {
            "ranking_branch": branch,
            "applicable_dimensions": [field for field, _ in orders[branch]],
            "effective_dimensions": [], "dimensions": {},
            "rank_score": "0.5", "ranking_uninformative": True,
            **_risk_audit(row.metrics, states, risks),
        }
        if _known_industry(row.industry):
            branches.setdefault(branch, []).append(row)
    for branch, peers in branches.items():
        effective = []
        for field, direction in orders[branch]:
            values = {row.code: row.metrics.get(field) for row in peers}
            midranks, percentiles = _dimension_ranks(values, direction)
            if percentiles:
                effective.append(field)
            if percentiles:
                status = "ranked"
            elif all(value is None for value in values.values()):
                status = "all_missing"
            else:
                status = "constant"
            for row in peers:
                value = values[row.code]
                reasons = row.metrics.get("ranking_missing_reasons", {})
                if not isinstance(reasons, Mapping):
                    raise TypeError("ranking_missing_reasons must be a mapping")
                reason = reasons.get(field, "missing") if value is None else None
                if reason is not None and not isinstance(reason, str):
                    raise TypeError("ranking missing reasons must be strings")
                audit[row.code]["dimensions"][field] = {
                    "direction": direction, "value": str(value) if value is not None else None,
                    "missing_reason": reason, "status": status,
                    "midrank": str(midranks[row.code]) if midranks else None,
                    "rank_pct": str(percentiles[row.code]) if percentiles else None,
                    "contribution": "0",
                }
        for row in peers:
            item = audit[row.code]
            item["effective_dimensions"] = list(effective)
            item["ranking_uninformative"] = not effective
            with localcontext(_context()):
                contributions = []
                # Midranks are exact half-integers. Divide once at the end so
                # repeated fractional dimensions cannot disturb an economic tie.
                denominator = (len(peers) - 1) * len(effective)
                numerator = Decimal(0)
                for field in effective:
                    dimension = item["dimensions"][field]
                    rank_offset = Decimal(dimension["midrank"]) - 1
                    numerator += rank_offset
                    contribution = rank_offset / denominator
                    dimension["contribution"] = str(contribution)
                    contributions.append(contribution)
                scores[row.code] = numerator / denominator if effective else Decimal("0.5")
            # Align all operands' decimal places before summing/subtracting;
            # contribution exponents can differ even with the same precision.
            # Allow carry digits for every term, preserving the exact identity.
            if effective:
                operands = (scores[row.code], *contributions[:-1])
                lowest = min(value.as_tuple().exponent for value in operands)
                highest = max(value.adjusted() for value in operands)
                precision = max(
                    _PRECISION, highest - lowest + len(str(len(operands))) + 2,
                )
                with localcontext(_context(precision)):
                    last = item["dimensions"][effective[-1]]
                    last["contribution"] = str(
                        scores[row.code] - sum(contributions[:-1], Decimal(0))
                    )
            item["rank_score"] = str(scores[row.code])
    eligible = sorted(
        (row for row in rows if _known_industry(row.industry)),
        key=lambda row: (_TIERS[audit[row.code]["risk_state"]], scores[row.code], row.code),
    )
    verdicts = _apply_caps(eligible, rows, industry_cap, total_cap)
    for verdict in verdicts:
        audit[verdict.code].update({
            "verdict": verdict.verdict, "reason": verdict.reason, "rank": verdict.rank,
            "cap_exclusion_reason": verdict.reason if verdict.verdict == "drop" else None,
        })
    return verdicts, {code: audit[code] for code in sorted(audit)}


def rank_candidates(
    candidates: Sequence[CompetitionCandidate], contract: Mapping[str, object],
) -> tuple[CompetitionVerdict, ...]:
    """Rank within explicit branches, merge by risk/score/code, and apply caps."""
    return _calculate(candidates, contract)[0]


def ranking_audit(
    candidates: Sequence[CompetitionCandidate], contract: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """Return JSON-ready contributions and verdicts from the same calculation.

    All Decimal values (including occupied midranks) are decimal strings;
    unknown-industry candidates do not enter peer populations. Contributions
    are equally weighted; the final effective dimension absorbs the Decimal
    rounding residual so their exact sum equals rank_score. Scores are computed
    independently from exact midranks, so that residual never affects sorting.
    """
    return _calculate(candidates, contract)[1]
