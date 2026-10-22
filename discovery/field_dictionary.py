from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class FieldSpec:
    report: str | tuple[str, ...] | None
    field: str | tuple[str, ...] | None
    unit: str
    consolidated_basis: str
    period_type: str
    derivation_formula: str | None
    financial_sector_substitute: str | None
    missing_policy: str
    source_as_of: str
    trusted: bool = True


_CNY = dict(
    unit="CNY",
    consolidated_basis="consolidated",
    period_type="cumulative",
    source_as_of="NOTICE_DATE/UPDATE_DATE",
)
_BALANCE = {**_CNY, "period_type": "period_end"}
_MAIN = dict(
    consolidated_basis="consolidated",
    period_type="period_end",
    source_as_of="NOTICE_DATE/UPDATE_DATE",
)

FIELD_DICTIONARY: Mapping[str, FieldSpec] = MappingProxyType(
    {
        "revenue": FieldSpec(
            "RPT_DMSK_FN_INCOME",
            "TOTAL_OPERATE_INCOME",
            derivation_formula=None,
            financial_sector_substitute="TOTALOPERATEREVE",
            missing_policy="None when the reported consolidated line is absent.",
            **_CNY,
        ),
        "net_income": FieldSpec(
            "RPT_DMSK_FN_INCOME",
            "PARENT_NETPROFIT",
            derivation_formula=None,
            financial_sector_substitute="PARENTNETPROFIT",
            missing_policy="None when parent-attributable profit is absent.",
            **_CNY,
        ),
        "deducted_net_income": FieldSpec(
            "RPT_DMSK_FN_INCOME",
            "DEDUCT_PARENT_NETPROFIT",
            derivation_formula=None,
            financial_sector_substitute="KCFJCXSYJLR",
            missing_policy="None; never substitute reported net income.",
            **_CNY,
        ),
        "pretax_income": FieldSpec(
            "RPT_DMSK_FN_INCOME",
            "TOTAL_PROFIT",
            derivation_formula=None,
            financial_sector_substitute="TOTAL_PROFIT",
            missing_policy="None when total profit is absent.",
            **_CNY,
        ),
        "income_tax": FieldSpec(
            "RPT_DMSK_FN_INCOME",
            "INCOME_TAX",
            derivation_formula=None,
            financial_sector_substitute="INCOME_TAX",
            missing_policy="None when income-tax expense is absent.",
            **_CNY,
        ),
        "total_assets": FieldSpec(
            "RPT_DMSK_FN_BALANCE",
            "TOTAL_ASSETS",
            derivation_formula=None,
            financial_sector_substitute="TOTAL_ASSETS_PK",
            missing_policy="None when the period-end consolidated line is absent.",
            **_BALANCE,
        ),
        "net_assets": FieldSpec(
            "RPT_DMSK_FN_BALANCE",
            "TOTAL_EQUITY",
            derivation_formula=None,
            financial_sector_substitute="TOTAL_EQUITY_PK",
            missing_policy="None when total equity is absent.",
            **_BALANCE,
        ),
        "cash": FieldSpec(
            "RPT_DMSK_FN_BALANCE",
            "MONETARYFUNDS",
            derivation_formula=None,
            financial_sector_substitute="MONETARYFUNDS",
            missing_policy=(
                "None when absent; bank monetary funds remain visible but are not "
                "used for ROIC without verified interest debt."
            ),
            **_BALANCE,
        ),
        "short_loan": FieldSpec(
            "RPT_F10_FINANCE_GBALANCE",
            "SHORT_LOAN",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the detailed balance report is unavailable; zero when "
                "the period row is present and the line is null."
            ),
            **_BALANCE,
        ),
        "long_loan": FieldSpec(
            "RPT_F10_FINANCE_GBALANCE",
            "LONG_LOAN",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the detailed balance report is unavailable; zero when "
                "the period row is present and the line is null."
            ),
            **_BALANCE,
        ),
        "bond_payable": FieldSpec(
            "RPT_F10_FINANCE_GBALANCE",
            "BOND_PAYABLE",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the detailed balance report is unavailable; zero when "
                "the period row is present and the line is null."
            ),
            **_BALANCE,
        ),
        "noncurrent_liab_1year": FieldSpec(
            "RPT_F10_FINANCE_GBALANCE",
            "NONCURRENT_LIAB_1YEAR",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the detailed balance report is unavailable; zero when "
                "the period row is present and the line is null. Retained "
                "separately because it can include "
                "non-interest-bearing items."
            ),
            **_BALANCE,
        ),
        "interest_debt": FieldSpec(
            "RPT_F10_FINANCE_GBALANCE",
            (
                "SHORT_LOAN",
                "LONG_LOAN",
                "BOND_PAYABLE",
                "NONCURRENT_LIAB_1YEAR",
            ),
            derivation_formula=(
                "sum_known(short_loan, long_loan, bond_payable, "
                "noncurrent_liab_1year)"
            ),
            financial_sector_substitute=None,
            missing_policy=(
                "None only when every component is absent. The due-within-one-year "
                "component can overstate interest-bearing debt."
            ),
            **_BALANCE,
        ),
        "ocf": FieldSpec(
            "RPT_DMSK_FN_CASHFLOW",
            "NETCASH_OPERATE",
            derivation_formula=None,
            financial_sector_substitute="NETCASH_OPERATE_PK",
            missing_policy="None when operating net cash flow is absent.",
            **_CNY,
        ),
        "capex": FieldSpec(
            "RPT_DMSK_FN_CASHFLOW",
            "CONSTRUCT_LONG_ASSET",
            derivation_formula=None,
            financial_sector_substitute="CONSTRUCT_LONG_ASSET",
            missing_policy=(
                "None when cash paid to construct/acquire long-term assets is absent."
            ),
            **_CNY,
        ),
        "roe": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            "ROEJQ",
            unit="percent",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the exact weighted diluted ROE field is absent; "
                "ROE and WEIGHTAVGROE are not aliases."
            ),
            **_MAIN,
        ),
        "shares": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            "TOTAL_SHARE",
            unit="shares",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None unless the exact field is finite and positive; no total-share "
                "alias is accepted."
            ),
            **_MAIN,
        ),
        "gross_margin": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            "XSMLL",
            unit="percent",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the exact reported gross-margin percentage is absent; "
                "not applicable to banks."
            ),
            **_MAIN,
        ),
        "net_margin": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            "XSJLL",
            unit="percent",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the exact reported net-margin percentage is absent."
            ),
            **_MAIN,
        ),
        "interest_coverage": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            "INTSTCOVRATE",
            unit="rate",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the direct reported interest-coverage rate is absent; "
                "not reconstructed from EBIT and interest expense and not "
                "applicable to banks."
            ),
            **_MAIN,
        ),
        "ebit": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            ("PER_EBIT", "TOTAL_SHARE"),
            unit="CNY",
            derivation_formula="PER_EBIT * TOTAL_SHARE",
            financial_sector_substitute=None,
            missing_policy=(
                "Discovery value is None: vendor EBIT/share has not been reproduced "
                "from the disclosed annual-report statements."
            ),
            trusted=False,
            **_MAIN,
        ),
        "normalized_effective_tax_rate": FieldSpec(
            "RPT_DMSK_FN_INCOME",
            ("INCOME_TAX", "TOTAL_PROFIT"),
            unit="ratio",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "Discovery value is None; verified tax and pretax inputs remain "
                "available without a look-ahead normalized rate."
            ),
            trusted=False,
            **_MAIN,
        ),
        "invested_capital": FieldSpec(
            None,
            None,
            unit="CNY",
            derivation_formula=(
                "average(net_assets + interest_debt - explicit excess_cash)"
            ),
            financial_sector_substitute=None,
            missing_policy=(
                "Always None until an explicit excess-cash source is verified; "
                "MONETARYFUNDS is not excess cash."
            ),
            trusted=False,
            **_MAIN,
        ),
        "roic": FieldSpec(
            None,
            None,
            unit="percent",
            derivation_formula=(
                "NOPAT / average(net_assets + interest_debt - explicit excess_cash)"
            ),
            financial_sector_substitute=None,
            missing_policy=(
                "Always None until EBIT and explicit excess cash are verified; "
                "financial-industry rows are explicitly ineligible. Never use vendor "
                "ROIC or ROE as a proxy."
            ),
            trusted=False,
            **_MAIN,
        ),
        "capital_adequacy": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            "NEWCAPITALADER",
            unit="percent",
            derivation_formula=None,
            financial_sector_substitute="NEWCAPITALADER",
            missing_policy="None outside financial reporters or when absent.",
            **_MAIN,
        ),
        "core_tier1_capital_adequacy": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            "HXYJBCZL",
            unit="percent",
            derivation_formula=None,
            financial_sector_substitute="HXYJBCZL",
            missing_policy="None outside banks or when absent.",
            **_MAIN,
        ),
        "npl": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            "NONPERLOAN",
            unit="percent",
            derivation_formula=None,
            financial_sector_substitute="NONPERLOAN",
            missing_policy="None outside banks or when absent.",
            **_MAIN,
        ),
        "nonperforming_loan_balance": FieldSpec(
            "RPT_F10_FINANCE_MAINFINADATA",
            "NON_PERFORMING_LOAN",
            unit="CNY",
            derivation_formula=None,
            financial_sector_substitute="NON_PERFORMING_LOAN",
            missing_policy="None outside banks or when absent.",
            **_MAIN,
        ),
        "impairment_loss": FieldSpec(
            "RPT_F10_FINANCE_GINCOME",
            (
                "ASSET_IMPAIRMENT_LOSS",
                "CREDIT_IMPAIRMENT_LOSS",
                "ASSET_IMPAIRMENT_INCOME",
                "CREDIT_IMPAIRMENT_INCOME",
            ),
            derivation_formula=(
                "sum direct *_LOSS, otherwise negate each corresponding *_INCOME"
            ),
            financial_sector_substitute="CREDIT_IMPAIRMENT_LOSS",
            missing_policy="None only when every exact impairment line is absent.",
            **_CNY,
        ),
        "government_grants": FieldSpec(
            None,
            None,
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "Always None: OTHER_INCOME includes items beyond government grants "
                "and is not a reliable substitute."
            ),
            trusted=False,
            **_CNY,
        ),
        "other_income": FieldSpec(
            "RPT_F10_FINANCE_GINCOME",
            "OTHER_INCOME",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the exact other-income line is absent. This line is a "
                "conservative non-core-income proxy and must not be labeled as "
                "government grants."
            ),
            **_CNY,
        ),
        "asset_disposal_gain": FieldSpec(
            "RPT_F10_FINANCE_GINCOME",
            "ASSET_DISPOSAL_INCOME",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy="None when the exact disposal-income line is absent.",
            **_CNY,
        ),
        "goodwill": FieldSpec(
            "RPT_F10_FINANCE_GBALANCE",
            "GOODWILL",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the detailed balance report is unavailable; zero when "
                "the period row is present and the line is null."
            ),
            **_BALANCE,
        ),
        "acquisition_cash_paid": FieldSpec(
            "RPT_F10_FINANCE_GCASHFLOW",
            "OBTAIN_SUBSIDIARY_OTHER",
            derivation_formula=None,
            financial_sector_substitute=None,
            missing_policy=(
                "None when the detailed cash-flow report is unavailable; zero when "
                "the period row is present and the line is null. The line is a "
                "net amount and can be negative."
            ),
            **_CNY,
        ),
    }
)


# Selection evidence fields do not expand the legacy mutable Financial DTO.
# Keep this registry separate: incomplete source audit blocks automated trust.
SELECTION_FIELD_DICTIONARY: Mapping[str, FieldSpec] = MappingProxyType(
    {
        "accounts_receivable": FieldSpec(
            'RPT_DMSK_FN_BALANCE', 'ACCOUNTS_RECE', unit="CNY",
            consolidated_basis="consolidated", period_type="period_end",
            derivation_formula=None, financial_sector_substitute=None,
            missing_policy="None for absent/null/ambiguous values; never infer zero. "
                           "Manual review required; zero/missing/full-market audit incomplete.",
            source_as_of="NOTICE_DATE (publication); UPDATE_DATE (source revision only)",
            trusted=False,
        ),
        "contract_assets": FieldSpec(
            'RPT_F10_FINANCE_GBALANCE', 'CONTRACT_ASSET', unit="CNY",
            consolidated_basis="consolidated", period_type="period_end",
            derivation_formula=None, financial_sector_substitute=None,
            missing_policy="None for absent/null/ambiguous values; never infer zero. "
                           "Manual review required; zero/missing/full-market audit incomplete.",
            source_as_of="NOTICE_DATE (publication); UPDATE_DATE (source revision only)",
            trusted=False,
        ),
        "inventory": FieldSpec(
            'RPT_DMSK_FN_BALANCE', 'INVENTORY', unit="CNY",
            consolidated_basis="consolidated", period_type="period_end",
            derivation_formula=None, financial_sector_substitute=None,
            missing_policy="None for absent/null/ambiguous values; never infer zero. "
                           "Manual review required; zero/missing/full-market audit incomplete.",
            source_as_of="NOTICE_DATE (publication); UPDATE_DATE (source revision only)",
            trusted=False,
        ),
        "total_equity_including_minority": FieldSpec(
            'RPT_DMSK_FN_BALANCE', 'TOTAL_EQUITY', unit="CNY",
            consolidated_basis="consolidated", period_type="period_end",
            derivation_formula=None, financial_sector_substitute=None,
            missing_policy="None for absent/null/ambiguous values; never infer zero. "
                           "Manual review required; zero/missing/full-market audit incomplete.",
            source_as_of="NOTICE_DATE (publication); UPDATE_DATE (source revision only)",
            trusted=False,
        ),
        "interest_expense": FieldSpec(
            'RPT_F10_FINANCE_GINCOME', 'FE_INTEREST_EXPENSE', unit="CNY",
            consolidated_basis="consolidated", period_type="cumulative",
            derivation_formula=None, financial_sector_substitute=None,
            missing_policy="None for absent/null/ambiguous values; never infer zero. "
                           "Manual review required; zero/missing/full-market audit incomplete.",
            source_as_of="NOTICE_DATE (publication); UPDATE_DATE (source revision only)",
            trusted=False,
        ),
        "restricted_cash": FieldSpec(
            None, None, unit="CNY",
            consolidated_basis="consolidated", period_type="period_end",
            derivation_formula=None, financial_sector_substitute=None,
            missing_policy="None for absent/null/ambiguous values; never infer zero. "
                           "Manual review required; zero/missing/full-market audit incomplete.",
            source_as_of="NOTICE_DATE (publication); UPDATE_DATE (source revision only)",
            trusted=False,
        ),
        "related_party_funds": FieldSpec(
            None, None, unit="CNY",
            consolidated_basis="consolidated", period_type="period_end",
            derivation_formula=None, financial_sector_substitute=None,
            missing_policy="None for absent/null/ambiguous values; never infer zero. "
                           "Manual review required; zero/missing/full-market audit incomplete.",
            source_as_of="NOTICE_DATE (publication); UPDATE_DATE (source revision only)",
            trusted=False,
        ),
    }
)
