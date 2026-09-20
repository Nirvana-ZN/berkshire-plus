"""Dependency-free industry branch classification for frozen labels."""

from __future__ import annotations

from typing import Literal, TypeAlias

IndustryBranch: TypeAlias = Literal[
    "bank",
    "insurance",
    "other_financial",
    "nonfinancial",
    "unknown",
]

INDUSTRY_BRANCHES: tuple[IndustryBranch, ...] = (
    "bank",
    "insurance",
    "other_financial",
    "nonfinancial",
    "unknown",
)
_OTHER_FINANCIAL_TOKENS = ("证券", "多元金融", "信托", "期货", "金融")


def classify_industry_branch(industry: str | None) -> IndustryBranch:
    """Classify one frozen industry label with the established token order."""

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
