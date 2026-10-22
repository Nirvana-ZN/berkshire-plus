"""Build-versioned quality-discount branch data contract."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from discovery.builds import BUILD_VERSION, CANDIDATE_BUILD_VERSION
from discovery.industry import IndustryBranch

QualityValuationPolicy = Literal["configured", "pb"]


@dataclass(frozen=True)
class QualityBranchDataContract:
    ruler_policy: str
    supported: bool
    valuation_policy: QualityValuationPolicy | None
    insufficient_reason: str | None


QUALITY_BRANCH_DATA_CONTRACT: Mapping[
    IndustryBranch, QualityBranchDataContract
] = MappingProxyType(
    {
        "bank": QualityBranchDataContract("bank", True, "pb", None),
        "insurance": QualityBranchDataContract(
            "unsupported",
            False,
            None,
            "industry_branch_insurance_unsupported",
        ),
        "other_financial": QualityBranchDataContract(
            "unsupported",
            False,
            None,
            "industry_branch_other_financial_unsupported",
        ),
        "nonfinancial": QualityBranchDataContract(
            "industry_map", True, "configured", None
        ),
        "unknown": QualityBranchDataContract(
            "insufficient",
            False,
            None,
            "industry_branch_unknown",
        ),
    }
)
QUALITY_DATA_CONTRACTS = MappingProxyType(
    {BUILD_VERSION: QUALITY_BRANCH_DATA_CONTRACT,
     'discovery-v7': QUALITY_BRANCH_DATA_CONTRACT,  # historical hypothesis validation only
     CANDIDATE_BUILD_VERSION: QUALITY_BRANCH_DATA_CONTRACT}
)


def quality_branch_data_contract(
    algorithm_version: str = BUILD_VERSION,
) -> Mapping[IndustryBranch, QualityBranchDataContract]:
    """Return the immutable branch contract for one executable build."""

    try:
        return QUALITY_DATA_CONTRACTS[algorithm_version]
    except KeyError as error:
        raise ValueError(
            f"quality data contract is unavailable: {algorithm_version}"
        ) from error


def quality_ruler_branch_policies(
    algorithm_version: str = BUILD_VERSION,
) -> Mapping[IndustryBranch, str]:
    """Return branch policies derived from the same build data contract."""

    return MappingProxyType(
        {
            branch: contract.ruler_policy
            for branch, contract in quality_branch_data_contract(
                algorithm_version
            ).items()
        }
    )
