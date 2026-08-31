from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from math import floor

from market.types import Constituent, FinancialRow

A_SHARE_SUFFIXES = (".SH", ".SZ", ".BJ")
ROLE_HIGH_CERTAINTY = "high_certainty"
ROLE_HIGH_ELASTICITY = "high_elasticity"
ROLE_MID_ELASTICITY = "mid_elasticity"
VERDICT_KEEP = "keep"
VERDICT_DROP = "drop"
VERDICT_INSUFFICIENT = "insufficient"
REASON_NOT_KEPT = "未进入本轮保留名单"
ELASTIC_INDUSTRIES = ("有色", "煤炭", "航运", "证券")
THIN_MODELS = frozenset({"高周转薄利", "会员", "平台佣金"})
KEEP_LIMIT = 10
FINALIST_LIMIT = 3

ROE_FLOOR = Decimal("8")
INTEREST_COVER_MIN = Decimal("2")
GROSS_MARGIN_FLOOR = Decimal("15")
OCF_NI_FLOOR = Decimal("0.7")
NET_MARGIN_FLOOR = Decimal("5")
SHARE_GROWTH_MAX = Decimal("0.20")
EXEMPT_GROSS_MARGIN = Decimal("30")
HIGH_CERT_ROE = Decimal("15")
EXEMPT_C_ROE = Decimal("20")
EXEMPT_C_OCF_NI = Decimal("1.0")
PEG_ELASTIC = Decimal("1.5")


@dataclass
class ScreenRow:
    code: str
    name: str
    verdict: str
    reason: str | None
    role: str | None = None
    finalist: bool = False
    mcap: Decimal | None = None
    industry: str = ""
    pe: Decimal | None = None
    peg: Decimal | None = None
    roe_mean: Decimal | None = None
    ocf_ni: Decimal | None = None
    metrics: dict[str, object] = field(default_factory=dict)


@dataclass
class FunnelResult:
    keep: list[ScreenRow]
    drop: list[ScreenRow]
    insufficient: list[ScreenRow]
    finalists: list[ScreenRow]


def _is_a_share(code: str) -> bool:
    text = (code or "").strip().upper()
    if len(text) != 9 or text[6] != ".":
        return False
    return text[:6].isdigit() and text.endswith(A_SHARE_SUFFIXES)


def _dec(value: Decimal | None) -> Decimal | None:
    if value is None or not value.is_finite():
        return None
    return value


def _mean(values: Iterable[Decimal]) -> Decimal | None:
    numbers = [item for item in values if item.is_finite()]
    if not numbers:
        return None
    return sum(numbers, Decimal("0")) / Decimal(len(numbers))


def _annual_for(code: str, rows: Iterable[FinancialRow]) -> list[FinancialRow]:
    annual = [
        row
        for row in rows
        if row.code == code and row.period_type == "annual" and str(row.period).isdigit()
    ]
    annual.sort(key=lambda row: int(row.period), reverse=True)
    return annual


def _values(rows: Sequence[FinancialRow], attr: str, n: int) -> list[Decimal]:
    found: list[Decimal] = []
    for row in rows:
        number = _dec(getattr(row, attr))
        if number is not None:
            found.append(number)
        if len(found) >= n:
            break
    return found


def _bank_or_insurance(industry: str) -> bool:
    if "非银行" in industry:
        return "保险" in industry
    return "银行" in industry or "保险" in industry


def _elastic_industry(industry: str) -> bool:
    return any(token in industry for token in ELASTIC_INDUSTRIES)


def _listed_under_10(listed_year: int | None, as_of_year: int) -> bool:
    if listed_year is None or listed_year > as_of_year:
        return False
    return as_of_year - listed_year < 10


def _last_two_ocf_positive(rows: Sequence[FinancialRow]) -> bool:
    recent = []
    for row in rows[:2]:
        ocf = _dec(row.ocf)
        if ocf is None:
            return False
        recent.append(ocf)
    return len(recent) == 2 and all(item > 0 for item in recent)


def _net_margin_ok_or_rising(rows: Sequence[FinancialRow]) -> bool:
    margins = _values(rows, "net_margin", 2)
    if len(margins) >= 2:
        if all(item >= NET_MARGIN_FLOOR for item in margins[:2]):
            return True
        return margins[0] > margins[1]
    if len(margins) == 1:
        return margins[0] >= NET_MARGIN_FLOOR
    return False


def _fcf_sum(rows: Sequence[FinancialRow], n: int) -> Decimal | None:
    total = Decimal("0")
    counted = 0
    for row in rows:
        ocf = _dec(row.ocf)
        capex = _dec(row.capex)
        if ocf is None or capex is None:
            continue
        total += ocf - capex
        counted += 1
        if counted >= n:
            break
    if counted == 0:
        return None
    return total


def _interest_cover(rows: Sequence[FinancialRow]) -> Decimal | None:
    for row in rows:
        ebit = _dec(row.ebit)
        interest = _dec(row.interest)
        if ebit is None or interest is None or interest == 0:
            continue
        return ebit / interest
    return None


def _share_growth(rows: Sequence[FinancialRow], n: int) -> tuple[Decimal | None, bool | None]:
    known: list[tuple[FinancialRow, Decimal]] = []
    for row in rows:
        shares = _dec(row.shares)
        if shares is None or shares <= 0:
            continue
        known.append((row, shares))
        if len(known) >= n:
            break
    if len(known) < 2:
        return None, None
    latest_row, latest = known[0]
    _oldest_row, oldest = known[-1]
    growth = (latest / oldest) - Decimal("1")
    return growth, latest_row.share_change_ma


def _ocf_ni_mean(rows: Sequence[FinancialRow], n: int) -> Decimal | None:
    ratios: list[Decimal] = []
    for row in rows:
        ocf = _dec(row.ocf)
        ni = _dec(row.net_income)
        if ocf is None or ni is None or ni == 0:
            continue
        ratios.append(ocf / ni)
        if len(ratios) >= n:
            break
    return _mean(ratios)


def _classify(
    snap: Constituent,
    annual: list[FinancialRow],
    listed_year: int | None,
    *,
    as_of_year: int,
) -> ScreenRow:
    roe_all = _values(annual, "roe", 99)
    ni_all = _values(annual, "net_income", 99)
    roe_mean = _mean(roe_all)
    ocf_ni = _ocf_ni_mean(annual, 5)
    base = ScreenRow(
        code=snap.code,
        name=snap.name,
        verdict=VERDICT_KEEP,
        reason=None,
        mcap=_dec(snap.mcap),
        industry=snap.industry or "",
        pe=_dec(snap.pe),
        peg=_dec(snap.peg),
        roe_mean=roe_mean,
        ocf_ni=ocf_ni,
    )
    if not _is_a_share(snap.code):
        base.verdict = VERDICT_INSUFFICIENT
        base.reason = "缺少可解析A股代码"
        return base
    if base.mcap is None:
        base.verdict = VERDICT_INSUFFICIENT
        base.reason = "缺少市值"
        return base
    if not roe_all:
        base.verdict = VERDICT_INSUFFICIENT
        base.reason = "缺少至少1年ROE"
        return base
    if not ni_all:
        base.verdict = VERDICT_INSUFFICIENT
        base.reason = "缺少至少1年净利润"
        return base

    roe10 = _mean(_values(annual, "roe", 10))
    gm5 = _mean(_values(annual, "gross_margin", 5))
    nm10 = _mean(_values(annual, "net_margin", 10))
    fcf5 = _fcf_sum(annual, 5)
    cover = _interest_cover(annual)
    share_growth, share_ma = _share_growth(annual, 5)
    exempt_c = (
        roe_mean is not None
        and roe_mean > EXEMPT_C_ROE
        and ocf_ni is not None
        and ocf_ni > EXEMPT_C_OCF_NI
        and (snap.business_model or "") in THIN_MODELS
    )
    exempt_a = (
        _listed_under_10(listed_year, as_of_year)
        and gm5 is not None
        and gm5 > EXEMPT_GROSS_MARGIN
        and _last_two_ocf_positive(annual)
    )
    exempt_b = (
        gm5 is not None
        and gm5 > EXEMPT_GROSS_MARGIN
        and _net_margin_ok_or_rising(annual)
    )

    if roe10 is not None and roe10 < ROE_FLOOR and not exempt_a:
        base.verdict = VERDICT_DROP
        base.reason = "10年平均ROE低于8%"
        return base
    if fcf5 is not None and fcf5 < 0:
        base.verdict = VERDICT_DROP
        base.reason = "5年累计自由现金流为负"
        return base
    if not _bank_or_insurance(base.industry):
        if cover is not None and cover < INTEREST_COVER_MIN:
            base.verdict = VERDICT_DROP
            base.reason = "利息覆盖低于2"
            return base
    if gm5 is not None and gm5 < GROSS_MARGIN_FLOOR and not exempt_c:
        base.verdict = VERDICT_DROP
        base.reason = "近5年平均毛利率低于15%"
        return base
    if ocf_ni is not None and ocf_ni < OCF_NI_FLOOR:
        base.verdict = VERDICT_DROP
        base.reason = "5年均值经营现金流/净利润低于0.7"
        return base
    if nm10 is not None and nm10 < NET_MARGIN_FLOOR and not exempt_b and not exempt_c:
        base.verdict = VERDICT_DROP
        base.reason = "近10年平均净利率低于5%"
        return base
    if share_growth is not None and share_growth > SHARE_GROWTH_MAX:
        if share_ma is False:
            base.verdict = VERDICT_DROP
            base.reason = "5年总股本膨胀超过20%"
            return base

    gaps: list[str] = []
    if fcf5 is None:
        gaps.append("5年累计自由现金流")
    if not _bank_or_insurance(base.industry) and cover is None:
        gaps.append("利息覆盖")
    if gm5 is None:
        gaps.append("近5年平均毛利率")
    if ocf_ni is None:
        gaps.append("经营现金流/净利润")
    if nm10 is None:
        gaps.append("近10年平均净利率")
    if share_growth is None:
        gaps.append("5年总股本")
    elif share_growth > SHARE_GROWTH_MAX and share_ma is not False:
        gaps.append("5年总股本膨胀是否并购无法识别")

    base.metrics = {
        "roe_mean": str(roe_mean) if roe_mean is not None else None,
        "ocf_ni": str(ocf_ni) if ocf_ni is not None else None,
        "gaps": gaps,
    }
    return base


def _mcap_ranks(keep: Sequence[ScreenRow]) -> tuple[set[str], set[str]]:
    ordered = sorted(
        keep,
        key=lambda row: (
            row.mcap if row.mcap is not None else Decimal("-Infinity"),
            row.name,
        ),
        reverse=True,
    )
    n = len(ordered)
    width = floor(n / 3)
    top = {row.code for row in ordered[:width]}
    bottom = {row.code for row in ordered[n - width :]} if width else set()
    return top, bottom


def _assign_roles(keep: list[ScreenRow]) -> None:
    n = len(keep)
    use_quantile = n >= 3
    top, bottom = _mcap_ranks(keep) if use_quantile else (set(), set())
    for row in keep:
        high_cert = row.roe_mean is not None and row.roe_mean >= HIGH_CERT_ROE
        if use_quantile:
            high_cert = high_cert and row.code in top
        if high_cert:
            row.role = ROLE_HIGH_CERTAINTY
            continue
        high_elas = False
        if use_quantile and row.code in bottom:
            high_elas = True
        if row.peg is not None and row.peg >= PEG_ELASTIC:
            high_elas = True
        if _elastic_industry(row.industry):
            high_elas = True
        row.role = ROLE_HIGH_ELASTICITY if high_elas else ROLE_MID_ELASTICITY


def _full_order_key(row: ScreenRow, held: set[str]) -> tuple:
    mcap = row.mcap if row.mcap is not None else Decimal("-Infinity")
    roe = row.roe_mean if row.roe_mean is not None else Decimal("-Infinity")
    ocf_ni = row.ocf_ni if row.ocf_ni is not None else Decimal("-Infinity")
    return (-mcap, -roe, -ocf_ni, 1 if row.industry in held else 0, row.name)


def _pick_finalists(keep: list[ScreenRow], held: set[str] | None = None) -> None:
    if not keep:
        return
    held_set = held or set()
    ordered = list(keep)
    if len(keep) < 3:
        names: set[str] = set()
        picked = 0
        for row in ordered:
            if row.name in names:
                continue
            row.finalist = True
            names.add(row.name)
            picked += 1
            if picked >= FINALIST_LIMIT:
                break
        return

    selected: list[ScreenRow] = []
    selected_names: set[str] = set()
    for bucket in (ROLE_HIGH_CERTAINTY, ROLE_HIGH_ELASTICITY, ROLE_MID_ELASTICITY):
        if len(selected) >= FINALIST_LIMIT:
            break
        bucket_rows = [
            row
            for row in ordered
            if row.role == bucket and row.name not in selected_names
        ]
        chosen = bucket_rows[0] if bucket_rows else None
        if chosen is None:
            remaining = [
                row
                for row in sorted(
                    keep,
                    key=lambda row: (
                        row.mcap if row.mcap is not None else Decimal("-Infinity"),
                        row.name,
                    ),
                    reverse=True,
                )
                if row.name not in selected_names
            ]
            chosen = remaining[0] if remaining else None
        if chosen is not None:
            chosen.finalist = True
            selected.append(chosen)
            selected_names.add(chosen.name)


def run_funnel(
    snapshots: Sequence[Constituent],
    financials: Iterable[FinancialRow],
    listed_years: dict[str, int] | None = None,
    held_industries: Iterable[str] | None = None,
    *,
    as_of_year: int | None = None,
) -> FunnelResult:
    year = as_of_year if as_of_year is not None else date.today().year
    held = {item for item in (held_industries or []) if item}
    listed = listed_years or {}
    by_code: dict[str, list[FinancialRow]] = {}
    for row in financials:
        by_code.setdefault(row.code, []).append(row)

    classified: list[ScreenRow] = []
    seen_codes: set[str] = set()
    for snap in snapshots:
        if snap.code in seen_codes:
            continue
        seen_codes.add(snap.code)
        classified.append(
            _classify(
                snap,
                _annual_for(snap.code, by_code.get(snap.code, [])),
                listed.get(snap.code),
                as_of_year=year,
            )
        )

    candidates = [row for row in classified if row.verdict == VERDICT_KEEP]
    insufficient = [row for row in classified if row.verdict == VERDICT_INSUFFICIENT]
    dropped = [row for row in classified if row.verdict == VERDICT_DROP]

    candidates.sort(key=lambda row: _full_order_key(row, held))
    keep = candidates[:KEEP_LIMIT]
    for row in candidates[KEEP_LIMIT:]:
        row.verdict = VERDICT_DROP
        row.reason = REASON_NOT_KEPT
        dropped.append(row)

    _assign_roles(keep)
    _pick_finalists(keep, held)

    return FunnelResult(
        keep=keep,
        drop=dropped,
        insufficient=insufficient,
        finalists=[row for row in keep if row.finalist],
    )
