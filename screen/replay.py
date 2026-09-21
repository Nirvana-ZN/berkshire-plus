from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Financial, ScreenResult, ScreenRun, Setting
from market.types import Constituent, FinancialRow
from screen.funnel import FunnelResult, ScreenRow, run_funnel
from screen.ruler import GATE_ACK_KEY, current_hash, latest

CAUSE_RULER = "ruler"
CAUSE_DATA = "data"
CAUSE_NOISE = "noise"
CAUSE_GAP = "gap"
GATE_LIMIT = 2

_FIN_FIELDS = (
    "roe",
    "net_income",
    "ocf",
    "capex",
    "ebit",
    "interest",
    "interest_coverage",
    "gross_margin",
    "net_margin",
    "shares",
    "bps",
    "npl",
    "eps",
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
)


def _num(value: Decimal | None) -> int | float | None:
    if value is None or not value.is_finite():
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def snapshot_constituents(raw: object) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        items = raw.get("constituents")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def dump_snapshot(
    snapshots: list[Constituent],
    rows: list[FinancialRow],
    years: dict[str, int],
    held: set[str],
) -> dict[str, object]:
    return {
        "ruler_digest": current_hash(),
        "as_of_year": date.today().year,
        "constituents": [
            {
                "code": item.code,
                "name": item.name,
                "mcap": _num(item.mcap),
                "pe": _num(item.pe),
                "industry": item.industry,
                "peg": _num(item.peg),
            }
            for item in snapshots
        ],
        "financials": [_fin_json(row) for row in rows],
        "listed_years": {code: int(year) for code, year in years.items()},
        "held": sorted(held),
    }


def _fin_json(row: FinancialRow) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": row.code,
        "period": row.period,
        "period_type": row.period_type,
        "share_change_ma": row.share_change_ma,
        "source_as_of": row.source_as_of,
    }
    for key in _FIN_FIELDS:
        payload[key] = _num(getattr(row, key))
    return payload


def parse_snapshot(raw: str | None) -> dict[str, object] | None:
    try:
        payload = json.loads(raw or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    constituents = snapshot_constituents(payload)
    financials = payload.get("financials")
    if not constituents or not isinstance(financials, list):
        return None
    return payload


def _to_constituent(item: dict) -> Constituent:
    return Constituent(
        code=str(item.get("code") or ""),
        name=str(item.get("name") or ""),
        mcap=_dec(item.get("mcap")),
        pe=_dec(item.get("pe")),
        industry=str(item.get("industry") or ""),
        peg=_dec(item.get("peg")),
    )


def _to_financial(item: dict) -> FinancialRow:
    return FinancialRow(
        code=str(item.get("code") or ""),
        period=str(item.get("period") or ""),
        period_type=str(item.get("period_type") or "annual"),
        roe=_dec(item.get("roe")),
        net_income=_dec(item.get("net_income")),
        ocf=_dec(item.get("ocf")),
        capex=_dec(item.get("capex")),
        ebit=_dec(item.get("ebit")),
        interest=_dec(item.get("interest")),
        interest_coverage=_dec(item.get("interest_coverage")),
        gross_margin=_dec(item.get("gross_margin")),
        net_margin=_dec(item.get("net_margin")),
        shares=_dec(item.get("shares")),
        share_change_ma=item.get("share_change_ma") if isinstance(item.get("share_change_ma"), bool) else None,
        bps=_dec(item.get("bps")),
        npl=_dec(item.get("npl")),
        eps=_dec(item.get("eps")),
        revenue=_dec(item.get("revenue")),
        deducted_net_income=_dec(item.get("deducted_net_income")),
        total_assets=_dec(item.get("total_assets")),
        net_assets=_dec(item.get("net_assets")),
        cash=_dec(item.get("cash")),
        short_loan=_dec(item.get("short_loan")),
        long_loan=_dec(item.get("long_loan")),
        bond_payable=_dec(item.get("bond_payable")),
        noncurrent_liab_1year=_dec(item.get("noncurrent_liab_1year")),
        interest_debt=_dec(item.get("interest_debt")),
        pretax_income=_dec(item.get("pretax_income")),
        income_tax=_dec(item.get("income_tax")),
        normalized_effective_tax_rate=_dec(
            item.get("normalized_effective_tax_rate")
        ),
        invested_capital=_dec(item.get("invested_capital")),
        roic=_dec(item.get("roic")),
        capital_adequacy=_dec(item.get("capital_adequacy")),
        core_tier1_capital_adequacy=_dec(
            item.get("core_tier1_capital_adequacy")
        ),
        nonperforming_loan_balance=_dec(item.get("nonperforming_loan_balance")),
        impairment_loss=_dec(item.get("impairment_loss")),
        government_grants=_dec(item.get("government_grants")),
        other_income=_dec(item.get("other_income")),
        asset_disposal_gain=_dec(item.get("asset_disposal_gain")),
        goodwill=_dec(item.get("goodwill")),
        acquisition_cash_paid=_dec(item.get("acquisition_cash_paid")),
        source_as_of=(
            str(item["source_as_of"]) if item.get("source_as_of") else None
        ),
    )


def _cached_financials(session: Session, codes: list[str]) -> list[FinancialRow]:
    if not codes:
        return []
    rows = session.scalars(select(Financial).where(Financial.code.in_(codes))).all()
    return [
        FinancialRow(
            code=row.code,
            period=row.period,
            period_type=row.period_type,
            roe=row.roe,
            net_income=row.net_income,
            ocf=row.ocf,
            capex=row.capex,
            ebit=row.ebit,
            interest=row.interest,
            interest_coverage=row.interest_coverage,
            gross_margin=row.gross_margin,
            net_margin=row.net_margin,
            shares=row.shares,
            share_change_ma=row.share_change_ma,
            bps=row.bps,
            npl=row.npl,
            eps=row.eps,
            revenue=row.revenue,
            deducted_net_income=row.deducted_net_income,
            total_assets=row.total_assets,
            net_assets=row.net_assets,
            cash=row.cash,
            short_loan=row.short_loan,
            long_loan=row.long_loan,
            bond_payable=row.bond_payable,
            noncurrent_liab_1year=row.noncurrent_liab_1year,
            interest_debt=row.interest_debt,
            pretax_income=row.pretax_income,
            income_tax=row.income_tax,
            normalized_effective_tax_rate=row.normalized_effective_tax_rate,
            invested_capital=row.invested_capital,
            roic=row.roic,
            capital_adequacy=row.capital_adequacy,
            core_tier1_capital_adequacy=row.core_tier1_capital_adequacy,
            nonperforming_loan_balance=row.nonperforming_loan_balance,
            impairment_loss=row.impairment_loss,
            government_grants=row.government_grants,
            other_income=row.other_income,
            asset_disposal_gain=row.asset_disposal_gain,
            goodwill=row.goodwill,
            acquisition_cash_paid=row.acquisition_cash_paid,
            source_as_of=row.source_as_of,
        )
        for row in rows
    ]


def _fin_fp(rows: list[FinancialRow], code: str) -> str:
    items = []
    for row in sorted(
        (item for item in rows if item.code == code),
        key=lambda item: (item.period, item.period_type),
    ):
        items.append(
            [
                row.period,
                row.period_type,
                str(row.roe),
                str(row.net_income),
                str(row.ocf),
                str(row.capex),
                str(row.shares),
            ]
        )
    return json.dumps(items, ensure_ascii=False)


def _state_result(row: ScreenResult) -> tuple[str, bool]:
    try:
        metrics = json.loads(row.metrics_json or "{}")
    except json.JSONDecodeError:
        metrics = {}
    return (row.verdict, bool(metrics.get("finalist")) if isinstance(metrics, dict) else False)


def _state_screen(row: ScreenRow) -> tuple[str, bool]:
    return (row.verdict, bool(row.finalist))


def _index_funnel(result: FunnelResult) -> dict[str, ScreenRow]:
    return {row.name: row for row in (*result.keep, *result.drop, *result.insufficient)}


def last_reconstructable(session: Session) -> tuple[ScreenRun, dict[str, object]] | None:
    runs = session.scalars(
        select(ScreenRun).where(ScreenRun.status == "done").order_by(ScreenRun.id.desc())
    ).all()
    for run in runs:
        payload = parse_snapshot(run.snapshot_json)
        if payload is not None:
            return run, payload
    return None


def last_finalist_count(session: Session) -> int | None:
    found = last_reconstructable(session)
    if found is None:
        return None
    run, _payload = found
    rows = session.scalars(select(ScreenResult).where(ScreenResult.screen_run_id == run.id)).all()
    return sum(1 for row in rows if _state_result(row)[1])


def _acked(session: Session) -> bool:
    row = latest(session)
    if row is None:
        return False
    setting = session.get(Setting, GATE_ACK_KEY)
    return setting is not None and setting.value == str(row.id)


def replay_report(session: Session) -> dict[str, object]:
    found = last_reconstructable(session)
    empty = {"screen_id": None, "rows": [], "ruler_flips": 0, "gate": False}
    if found is None:
        return empty
    run, payload = found
    constituents = [_to_constituent(item) for item in snapshot_constituents(payload)]
    frozen_fins = [
        _to_financial(item) for item in payload.get("financials") or [] if isinstance(item, dict)
    ]
    years: dict[str, int] = {}
    raw_years = payload.get("listed_years") or {}
    if isinstance(raw_years, dict):
        for code, year in raw_years.items():
            try:
                years[str(code)] = int(year)
            except (TypeError, ValueError):
                continue
    held = {str(item) for item in (payload.get("held") or []) if item}
    as_of = payload.get("as_of_year")
    year = int(as_of) if isinstance(as_of, int) else date.today().year
    digest = str(payload.get("ruler_digest") or "")
    digest_changed = bool(digest) and digest != current_hash()
    frozen_names = {item.name for item in constituents if item.name}
    orig_rows = session.scalars(select(ScreenResult).where(ScreenResult.screen_run_id == run.id)).all()
    orig = {row.name: row for row in orig_rows}
    compare = run_funnel(
        constituents,
        frozen_fins if digest_changed else _cached_financials(session, [item.code for item in constituents]),
        years,
        held,
        as_of_year=year,
    )
    fresh = _index_funnel(compare)
    cache_fins = _cached_financials(session, [item.code for item in constituents])
    rows = []
    for name in sorted(set(orig) | set(fresh)):
        old = orig.get(name)
        new = fresh.get(name)
        old_state = _state_result(old) if old is not None else None
        new_state = _state_screen(new) if new is not None else None
        if old_state == new_state:
            continue
        code = (old.code if old is not None else "") or (new.code if new is not None else "")
        if name not in frozen_names:
            cause = CAUSE_GAP
        elif digest_changed:
            cause = CAUSE_RULER
        elif _fin_fp(frozen_fins, code) != _fin_fp(cache_fins, code):
            cause = CAUSE_DATA
        else:
            cause = CAUSE_NOISE
        rows.append(
            {
                "name": name,
                "code": code,
                "was": None if old_state is None else old_state[0],
                "now": None if new_state is None else new_state[0],
                "was_finalist": None if old_state is None else old_state[1],
                "now_finalist": None if new_state is None else new_state[1],
                "cause": cause,
            }
        )
    ruler_flips = sum(1 for row in rows if row["cause"] == CAUSE_RULER)
    return {
        "screen_id": run.id,
        "rows": rows,
        "ruler_flips": ruler_flips,
        "gate": ruler_flips > GATE_LIMIT and not _acked(session),
    }


def gate_is_open(session: Session) -> bool:
    return bool(replay_report(session)["gate"])


def apply_credibility_gate(structured: dict) -> dict:
    if structured.get("verdict") == "pass":
        structured["verdict"] = "gray"
    bands = structured.get("price_bands")
    if isinstance(bands, dict) and bands.get("mode") != "watch":
        structured["price_bands"] = {**bands, "mode": "watch"}
    return structured
