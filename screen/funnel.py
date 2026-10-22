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
    roe10: Decimal | None = None
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
    if not (text[:6].isdigit() and text.endswith(A_SHARE_SUFFIXES)):
        return False
    if (text.endswith(".SH") and text.startswith("900")) or (
        text.endswith(".SZ") and text.startswith("200")
    ):
        return False
    return True


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


def _interims_for(code: str, rows: Iterable[FinancialRow]) -> dict[str, FinancialRow]:
    out: dict[str, FinancialRow] = {}
    for row in rows:
        if row.code == code and row.period_type != "annual":
            p = str(row.period or "").strip().upper()
            if len(p) >= 6 and p[:4].isdigit():
                out[p] = row
    return out


def _interim_deterioration_reason(
    interims: dict[str, FinancialRow], *, finance: bool
) -> str | None:
    if not interims:
        return None
    for period in sorted(interims.keys(), reverse=True):
        prev_period = f"{int(period[:4]) - 1}{period[4:]}"
        if prev_period not in interims:
            continue
        cur = interims[period]
        prev = interims[prev_period]
        cur_dni, prev_dni = _dec(cur.deducted_net_income), _dec(prev.deducted_net_income)
        if cur_dni is not None and prev_dni is not None and prev_dni > 0:
            if (cur_dni - prev_dni) / prev_dni <= Decimal("-0.20"):
                return f"最新期（{period}）扣非净利润同比下滑超20%"
        cur_ni, prev_ni = _dec(cur.net_income), _dec(prev.net_income)
        if cur_ni is not None and prev_ni is not None and prev_ni > 0:
            if (cur_ni - prev_ni) / prev_ni <= Decimal("-0.20"):
                return f"最新期（{period}）净利润同比下滑超20%"
        if not finance:
            cur_ocf, prev_ocf = _dec(cur.ocf), _dec(prev.ocf)
            if cur_ocf is not None and cur_ocf < 0 and prev_ocf is not None and prev_ocf > 0:
                return f"最新期（{period}）经营现金流由正转负"
        return None
    return None


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


def _thin_model(snap: Constituent) -> bool:
    return (snap.business_model or "") in THIN_MODELS


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
    interims: dict[str, FinancialRow] | None = None,
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
    base.roe10 = roe10
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
        and _thin_model(snap)
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
    finance = _bank_or_insurance(base.industry)
    if fcf5 is not None and fcf5 < 0:
        base.verdict = VERDICT_DROP
        base.reason = "5年累计自由现金流为负"
        return base
    if not finance:
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
    if not finance and len(annual) >= 3:
        latest_ann = annual[0]
        l_ni = _dec(latest_ann.net_income)
        l_ocf = _dec(latest_ann.ocf)
        if l_ni is not None and l_ni > 0 and l_ocf is not None and (l_ocf / l_ni) < Decimal("0.40"):
            base.verdict = VERDICT_DROP
            base.reason = f"最新年报（{latest_ann.period}）经营现金流/净利润低于0.4（近期现金流骤降）"
            return base
        f0_ocf, f0_cap = _dec(annual[0].ocf), _dec(annual[0].capex)
        f1_ocf, f1_cap = _dec(annual[1].ocf), _dec(annual[1].capex)
        if (
            f0_ocf is not None
            and f0_cap is not None
            and f1_ocf is not None
            and f1_cap is not None
            and (f0_ocf - f0_cap) < 0
            and (f1_ocf - f1_cap) < 0
        ):
            base.verdict = VERDICT_DROP
            base.reason = "近2年自由现金流连续为负"
            return base
    if nm10 is not None and nm10 < NET_MARGIN_FLOOR and not exempt_b and not exempt_c:
        base.verdict = VERDICT_DROP
        base.reason = "近10年平均净利率低于5%"
        return base
    if share_growth is not None and share_growth > SHARE_GROWTH_MAX and share_ma is False:
        base.verdict = VERDICT_DROP
        base.reason = "5年总股本膨胀超过20%"
        return base
    if annual:
        latest_ann = annual[0]
        ann_ni = _dec(latest_ann.net_income)
        ann_dni = _dec(latest_ann.deducted_net_income)
        if ann_ni is not None and ann_ni > 0 and ann_dni is not None:
            if ann_dni <= 0:
                base.verdict = VERDICT_DROP
                base.reason = f"最新年报（{latest_ann.period}）扣非净利润为负（靠非经常性损益扭亏）"
                return base
            if ann_dni / ann_ni < Decimal("0.65") and len(annual) >= 2:
                prev_dni = _dec(annual[1].deducted_net_income)
                if prev_dni is not None and prev_dni > 0 and (ann_dni - prev_dni) / prev_dni <= Decimal("-0.15"):
                    base.verdict = VERDICT_DROP
                    base.reason = f"最新年报（{latest_ann.period}）扣非占归母不足65%且扣非同比下滑超15%"
                    return base
    interim_drop = _interim_deterioration_reason(interims or {}, finance=finance)
    if interim_drop is not None:
        base.verdict = VERDICT_DROP
        base.reason = interim_drop
        return base

    gaps: list[str] = []
    if fcf5 is None:
        gaps.append("5年累计自由现金流")
    if not finance and cover is None:
        gaps.append("利息覆盖")
    if gm5 is None:
        gaps.append("近5年平均毛利率")
    if ocf_ni is None:
        gaps.append("经营现金流/净利润")
    if nm10 is None:
        gaps.append("近10年平均净利率")
    if share_growth is None:
        gaps.append("5年总股本")
    elif share_growth > SHARE_GROWTH_MAX and share_ma is not True:
        gaps.append("无法识别股本膨胀是否并购")

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


def _quality_value_score(row: ScreenRow) -> Decimal:
    if row.roe_mean is None or row.pe is None or row.pe <= 0:
        return Decimal("-Infinity")
    eff_roe = min(row.roe_mean, Decimal("30"))
    ocf_factor = (
        min(max(row.ocf_ni, Decimal("0.7")), Decimal("1.3"))
        if row.ocf_ni is not None
        else Decimal("1")
    )
    return (eff_roe * ocf_factor / row.pe).quantize(Decimal("0.0001"))


def _full_order_key(row: ScreenRow, held: set[str]) -> tuple:
    qv = _quality_value_score(row)
    mcap = row.mcap if row.mcap is not None else Decimal("-Infinity")
    roe = row.roe_mean if row.roe_mean is not None else Decimal("-Infinity")
    ocf_ni = row.ocf_ni if row.ocf_ni is not None else Decimal("-Infinity")
    return (-qv, -mcap, -roe, -ocf_ni, 1 if row.industry in held else 0, row.name)


def _pick_finalists(
    keep: list[ScreenRow],
    held: set[str] | None = None,
    limit: int = FINALIST_LIMIT,
) -> None:
    if not keep or limit <= 0:
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
            if picked >= limit:
                break
        return

    selected: list[ScreenRow] = []
    selected_names: set[str] = set()
    for bucket in (ROLE_HIGH_CERTAINTY, ROLE_HIGH_ELASTICITY, ROLE_MID_ELASTICITY):
        if len(selected) >= limit:
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


def _classify_all(
    snapshots: Sequence[Constituent],
    financials: Iterable[FinancialRow],
    listed_years: dict[str, int] | None = None,
    *,
    as_of_year: int | None = None,
) -> list[ScreenRow]:
    year = as_of_year if as_of_year is not None else date.today().year
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
        code_rows = by_code.get(snap.code, [])
        classified.append(
            _classify(
                snap,
                _annual_for(snap.code, code_rows),
                listed.get(snap.code),
                as_of_year=year,
                interims=_interims_for(snap.code, code_rows),
            )
        )
    return classified


def screen_keep(
    snapshots: Sequence[Constituent],
    financials: Iterable[FinancialRow],
    listed_years: dict[str, int] | None = None,
    *,
    as_of_year: int | None = None,
) -> list[ScreenRow]:
    return [
        row
        for row in _classify_all(
            snapshots, financials, listed_years, as_of_year=as_of_year
        )
        if row.verdict == VERDICT_KEEP
    ]


def run_funnel(
    snapshots: Sequence[Constituent],
    financials: Iterable[FinancialRow],
    listed_years: dict[str, int] | None = None,
    held_industries: Iterable[str] | None = None,
    *,
    as_of_year: int | None = None,
    finalist_limit: int | None = None,
) -> FunnelResult:
    held = {item for item in (held_industries or []) if item}
    classified = _classify_all(
        snapshots, financials, listed_years, as_of_year=as_of_year
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
    cap = FINALIST_LIMIT if finalist_limit is None else min(FINALIST_LIMIT, max(0, finalist_limit))
    _pick_finalists(keep, held, cap)

    return FunnelResult(
        keep=keep,
        drop=dropped,
        insufficient=insufficient,
        finalists=[row for row in keep if row.finalist],
    )
