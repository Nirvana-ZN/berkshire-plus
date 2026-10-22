"""Index-fund screen over a fixed universe of representative funds.

The universe (``screen/index_fund_universe.json``, rebuilt quarterly by
``scripts/build_index_fund_universe.py``) keeps one fund per tracked index and
drops indices whose daily returns move almost exactly like a larger one.
Refresh then asks: is the index cheap, and is its fund still sound.

The index gate is a valuation spread, not an expected return: book earnings
yield (100 / PE) must clear the treasury yield by YIELD_SPREAD_MIN points. For
cyclical / financial indices the method is PB, and the same spread is checked
on normalized ROE / PB. The ~10-year percentile is shown beside the verdict and
orders rows within a verdict; it is not a cross-index value score.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from discovery.metrics import percentile_rank
from market.funds import parse_cn_date, percent, to_decimal

YIELD_SPREAD_MIN = Decimal("3")
HISTORY_MIN_POINTS = 260  # weekly points, ~5 years
ROE_RECENT_POINTS = 156  # weekly points, ~3 years
LOW_YIELD_NOTE = Decimal("2")  # PE > 50: low or loss-hit earnings base
PROXY_MAX_DAYS = 91  # 13 weeks, matches scripts/check_pe_proxy.py
TREASURY_MAX_DAYS = 30
# scripts/check_pe_proxy.py: 13 weeks after the anchor the proxy is off by
# >14% one time in ten, so a proxy this close to the gate can't decide it.
PROXY_BAND = Decimal("0.15")
TRACKING_ERROR_MAX = Decimal("3")
FUND_SCALE_MIN = Decimal("2")  # 亿元
AGE_MIN_DAYS = 365
# ponytail: one day's turnover, not a 20-day average; a quiet day can drop a
# liquid ETF. Upgrade by pulling the ETF kline if it bites.
ETF_TURNOVER_MIN = Decimal("1000")  # 万元
ETF_PREMIUM_MAX = Decimal("1")  # %
UNIVERSE_LIMIT = 300
SIMILAR_CORRELATION = 0.95  # daily returns over ~1 year
MIN_OVERLAP_DAYS = 120
EQUITY_TYPES = frozenset({"指数型-股票", "指数型-海外股票"})
# ponytail: name keywords, not constituent industry weights; a cyclical basket
# without these words still gets PE. Upgrade needs index constituent data.
# Balance-sheet financials and resource cyclicals use PB + normalized ROE.
PB_METHOD = re.compile(r"银行|证券|券商|保险|金融(?!科技)|地产|煤炭|钢铁|有色|金属|黄金|化工|石油|油气|稀土|航运|矿|资源")
# Cycle-distorted PE, but book value is also a weak anchor (R&D, tech iteration,
# overcapacity, biological assets): show history percentile, withhold verdict.
REVIEW_METHOD = re.compile(r"军工|国防|新能源车|新能源汽车|光伏|锂|电池|养殖|畜牧|游戏|动漫|传媒|影视")
HIGH_PERCENTILE_REVIEW = Decimal("92")
HIGH_PERCENTILE_NOTE = Decimal("75")
OVERSEAS_TREASURY_YIELDS = (
    (re.compile(r"恒生|港股|香港|中概|中国互联|中国教育|沪港深"), Decimal("3.10"), "10年期港债 3.10%"),
    (
        re.compile(r"纳斯达克|纳指|标准普尔|标普500|标普100|标普美国|标普生物|道琼斯|MSCI USA|S&P"),
        Decimal("4.10"),
        "10年期美债 4.10%",
    ),
    (re.compile(r"法兰克福|德国|DAX"), Decimal("2.25"), "10年期德债 2.25%"),
    (re.compile(r"日经|东证"), Decimal("1.05"), "10年期日债 1.05%"),
)

RULES = {
    "yield_spread_min": YIELD_SPREAD_MIN,
    "history_min_points": HISTORY_MIN_POINTS,
    "tracking_error_max": TRACKING_ERROR_MAX,
    "fund_scale_min": FUND_SCALE_MIN,
    "age_min_days": AGE_MIN_DAYS,
    "etf_turnover_min": ETF_TURNOVER_MIN,
    "etf_premium_max": ETF_PREMIUM_MAX,
}
_STATUS_ORDER = {"pass": 0, "drop": 1, "review": 2, "insufficient": 3}


def _pct(value: object) -> Decimal | None:
    number = to_decimal(value)
    return None if number is None else number * 100


def valuation_method(name: str, pb_flag: bool = False) -> str:
    """Owned by the project, not the vendor: 'pe', 'pb', or 'review'."""
    if REVIEW_METHOD.search(name):
        return "review"
    return "pb" if pb_flag or PB_METHOD.search(name) else "pe"


def proxy_band(age_days: int) -> Decimal:
    """Empirical P90 proxy error band by anchor age (4w: 7%, 8w: 10%, 13w: 15%)."""
    if age_days <= 28:
        return Decimal("0.07")
    return Decimal("0.10") if age_days <= 56 else PROXY_BAND


def benchmark_treasury(
    name: str, category: str | None, default_treasury: Decimal
) -> tuple[Decimal | None, str | None]:
    """Risk-free 10Y yield for the index's market; None if overseas market is unconfigured."""
    if category != "海外":
        return default_treasury, None
    for pattern, rate, label in OVERSEAS_TREASURY_YIELDS:
        if pattern.search(name):
            return rate, label
    return None, None


def valuation_tier(candidate: dict) -> int:
    source = (candidate.get("valuation") or {}).get("source")
    return 0 if source == "danjuan" else 1 if source == "csindex" else 2


def normalized_roe(history: list[Decimal]) -> Decimal | None:
    """Lower of the whole-history median and the last-3-year mean (fractions),
    so a boom-era median can't hide a collapse (国证地产: median 10%, now 1.6%)."""
    if len(history) < HISTORY_MIN_POINTS:
        return None
    recent = history[-ROE_RECENT_POINTS:]
    return min(statistics.median(history), sum(recent) / len(recent))


def judge_index(
    item: dict,
    metric: str,
    history: list[Decimal],
    treasury: Decimal,
    roe_history: list[Decimal] | None = None,
) -> dict:
    current = to_decimal(item.get(metric))
    pe = to_decimal(item.get("pe"))
    pb = to_decimal(item.get("pb"))
    earnings_yield = Decimal(100) / pe if pe is not None and pe > 0 else None
    row = {
        "metric": metric,
        "current": current,
        "pe": pe,
        "pb": pb,
        "roe": _pct(item.get("roe")),
        "normalized_roe": None,
        "min_roe": None,
        "roe_headroom": None,
        "dividend_yield": _pct(item.get("yeild")),
        "earnings_yield": earnings_yield,
        "gate_yield": None,
        "spread": None,
        "percentile": None,
        "history_years": round(len(history) / 52, 1),
    }
    label = metric.upper()
    if current is None or current <= 0:
        return {**row, "status": "insufficient", "reason": f"当前 {label} 缺失或为负"}
    if sum(1 for value in history if value > 0) >= HISTORY_MIN_POINTS:
        row["percentile"] = percentile_rank(current, history)
    if metric == "pb":
        roe = normalized_roe(roe_history or [])
        if roe is None:
            return {**row, "status": "review", "reason": "周期或金融指数，缺 5 年 ROE 历史，绝对估值待复核"}
        min_roe = current * (treasury + YIELD_SPREAD_MIN)
        row["normalized_roe"] = roe * 100
        row["min_roe"] = min_roe
        row["roe_headroom"] = (1 - min_roe / (roe * 100)) * 100 if roe > 0 else None
        row["gate_yield"] = roe * 100 / current
    else:
        row["gate_yield"] = earnings_yield
    row["spread"] = None if row["gate_yield"] is None else row["gate_yield"] - treasury
    if row["spread"] is None or row["spread"] < YIELD_SPREAD_MIN:
        basis = "正常化 ROE ÷ PB" if metric == "pb" else "账面盈利收益率"
        return {**row, "status": "drop", "reason": f"{basis}比国债高不到 {YIELD_SPREAD_MIN} 个百分点"}
    if row["percentile"] is not None and row["percentile"] > HIGH_PERCENTILE_REVIEW:
        return {
            **row,
            "status": "review",
            "reason": f"历史分位 {row['percentile']:.1f}% 处于近10年极高位（>{HIGH_PERCENTILE_REVIEW}%），防周期见顶或估值均值回归",
        }
    return {**row, "status": "pass", "reason": None}


def static_product(kind: str, code: str, fund_code: str, name: str, profile: dict) -> dict:
    """Slow-moving fund facts from its overview page, frozen into the universe."""
    management = percent(profile.get("管理费率"))
    custody = percent(profile.get("托管费率"))
    service = percent(profile.get("销售服务费率")) or Decimal(0)
    established = parse_cn_date(profile.get("成立日期/规模"))
    return {
        "kind": kind,
        "code": code,
        "fund_code": fund_code,
        "name": name,
        "fee": None if management is None or custody is None else management + custody + service,
        "established": None if established is None else established.isoformat(),
        "scale": percent(profile.get("净资产规模")),  # 亿元, last quarterly report
    }


def age_days(product: dict, today: date) -> int | None:
    established = product.get("established")
    return None if not established else (today - date.fromisoformat(str(established))).days


def pick_representative(products: list[dict], today: date) -> dict | None:
    """Cheapest viable fund for one index; larger scale breaks fee ties."""
    viable = [
        p
        for p in products
        if p.get("fee") is not None
        and p.get("scale") is not None
        and p["scale"] >= FUND_SCALE_MIN
        and (age_days(p, today) or 0) >= AGE_MIN_DAYS
        and not (p["kind"] == "场外" and p["name"].endswith("Y"))
        and not (p["kind"] == "场内" and (p.get("turnover") or 0) < ETF_TURNOVER_MIN)
    ]
    return min(viable, key=lambda p: (p["fee"], -p["scale"]), default=None)


def correlation(a: dict[str, float], b: dict[str, float]) -> float | None:
    days = sorted(a.keys() & b.keys())
    if len(days) < MIN_OVERLAP_DAYS:
        return None
    try:
        return statistics.correlation([a[d] for d in days], [b[d] for d in days])
    except statistics.StatisticsError:
        return None


def select_distinct(
    candidates: list[dict],
    returns_of: Callable[[dict], dict[str, float]],
    *,
    limit: int = UNIVERSE_LIMIT,
    threshold: float = SIMILAR_CORRELATION,
) -> tuple[list[dict], list[dict]]:
    """Greedy over candidates (largest first): skip one that tracks an accepted
    index above ``threshold``. Returns (accepted, skipped-with-reason)."""
    accepted: list[dict] = []
    series: list[dict[str, float]] = []
    skipped: list[dict] = []
    for candidate in candidates:
        if len(accepted) >= limit:
            break
        returns = returns_of(candidate)
        if len(returns) < MIN_OVERLAP_DAYS:
            skipped.append({**candidate, "skip": "收益序列不足"})
            continue
        twin_idx, twin_corr = max(
            ((idx, correlation(returns, other)) for idx, other in enumerate(series)),
            key=lambda pair: -2 if pair[1] is None else pair[1],
            default=(-1, None),
        )
        if twin_corr is not None:
            kept = accepted[twin_idx]
            pair_threshold = (
                0.985
                if valuation_tier(candidate) == 0 and valuation_tier(kept) == 0
                else threshold
            )
            if twin_corr > pair_threshold:
                if valuation_tier(candidate) < valuation_tier(kept):
                    accepted[twin_idx] = candidate
                    series[twin_idx] = returns
                    skipped.append({**kept, "skip": f"与{candidate['name']}相关 {twin_corr:.3f}"})
                else:
                    skipped.append({**candidate, "skip": f"与{kept['name']}相关 {twin_corr:.3f}"})
                continue
        accepted.append(candidate)
        series.append(returns)
    return accepted, skipped


def live_product(fund: dict, etf_row: dict | None, tracking_error: Decimal | None, today: date) -> dict:
    product = {
        **fund,
        "fee": to_decimal(fund.get("fee")),
        "scale": to_decimal(fund.get("scale")),
        "tracking_error": tracking_error,
        "age_days": age_days(fund, today),
        "turnover": None,
        "premium": None,
    }
    if fund["kind"] == "场内" and etf_row is not None:
        price, iopv = etf_row.get("price"), etf_row.get("iopv")
        cap, turnover = etf_row.get("market_cap"), etf_row.get("turnover")
        product["scale"] = None if cap is None else cap / 100_000_000
        product["turnover"] = None if turnover is None else turnover / 10000
        if price is not None and iopv is not None and price > 0 and iopv > 0:
            product["premium"] = (price / iopv - 1) * 100
    return product


def judge_product(product: dict) -> tuple[str, str | None]:
    age = product.get("age_days")
    te = product.get("tracking_error")
    scale = product.get("scale")
    if product["kind"] == "场外" and product["name"].endswith("Y"):
        return "drop", "Y 类份额仅限个人养老金账户"
    if product.get("fee") is None:
        return "insufficient", "缺费率"
    if age is None:
        return "insufficient", "缺成立日期"
    if age < AGE_MIN_DAYS:
        return "drop", "成立不满 1 年"
    if te is None:
        return "insufficient", "缺跟踪误差"
    if te > TRACKING_ERROR_MAX:
        return "drop", f"跟踪误差 > {TRACKING_ERROR_MAX}%"
    if scale is None:
        return "insufficient", "缺规模"
    if scale < FUND_SCALE_MIN:
        return "drop", f"基金规模 < {FUND_SCALE_MIN} 亿"
    if product["kind"] == "场内":
        turnover, premium = product.get("turnover"), product.get("premium")
        if turnover is None or premium is None:
            return "insufficient", "缺成交额或参考净值"
        if turnover < ETF_TURNOVER_MIN:
            return "drop", f"当日成交额 < {ETF_TURNOVER_MIN} 万"
        if premium > ETF_PREMIUM_MAX:
            return "drop", f"溢价率 > {ETF_PREMIUM_MAX}%"
    return "pass", None


def build_payload(collected: dict, *, treasury: Decimal, today: date) -> dict:
    treasury_date = collected.get("treasury_date")
    treasury_stale = (
        not treasury_date
        or (today - date.fromisoformat(str(treasury_date))).days > TREASURY_MAX_DAYS
    )
    rows = []
    for entry in collected["items"]:
        index = entry["index"]
        source = (index.get("valuation") or {}).get("source")
        rate, rate_label = benchmark_treasury(
            index["index_name"], index.get("category"), treasury
        )
        effective_treasury = rate if rate is not None else treasury
        if entry.get("error") or entry.get("metric") is None:
            judged = {
                **judge_index({}, "pe", [], effective_treasury),
                "reason": entry.get("error") or "无估值来源",
            }
        else:
            judged = judge_index(
                entry["values"],
                entry["metric"],
                entry["history"],
                effective_treasury,
                entry.get("roe_history"),
            )
            review = entry.get("review")
            if not review and judged["status"] in ("pass", "drop"):
                if index.get("category") == "海外" and rate is None:
                    review = "海外指数暂未接入当地国债利率，不参与中国国债利差判定"
                elif index.get("category") != "海外" and treasury_stale:
                    review = f"国债收益率已超过 {TREASURY_MAX_DAYS} 天未更新，暂停利差判定"
            if review and judged["status"] != "insufficient":
                # The percentile stays visible; only the verdict is withheld.
                judged = {**judged, "status": "review", "reason": review}
        fund = entry["product"]
        status, reason = judge_product(fund)
        ey = judged["earnings_yield"]
        notes = []
        if rate_label is not None and judged["current"] is not None:
            notes.append(f"基准利率：{rate_label}")
        if judged["current"] is not None and judged["percentile"] is None:
            notes.append(f"{judged['metric'].upper()} 历史不足 5 年（{judged['history_years']} 年），无分位")
        elif (
            judged["status"] == "pass"
            and judged["percentile"] is not None
            and judged["percentile"] >= HIGH_PERCENTILE_NOTE
        ):
            notes.append(f"历史分位偏高（{judged['percentile']:.1f}%），利差虽达标但安全边际收敛")
        if judged["metric"] == "pe" and ey is not None and ey < LOW_YIELD_NOTE:
            notes.append("PE 很高，可能受盈利基数低或亏损成分影响，解释力有限")
        rows.append(
            {
                "name": index["name"],
                "index_name": index["index_name"],
                "category": index.get("category"),
                "valuation_source": source,
                "pe_from": entry.get("pe_from"),
                "note": "；".join(notes) or None,
                **judged,
                "fund": {**fund, "status": status, "reason": reason},
            }
        )
    rows.sort(
        key=lambda r: (
            _STATUS_ORDER[r["status"]],
            r["percentile"] if r["percentile"] is not None else Decimal(101),
            -(r["gate_yield"] or 0),
        )
    )
    judged_count = sum(r["status"] in ("pass", "drop") for r in rows)
    return {
        "treasury_yield": treasury,
        "treasury_date": collected.get("treasury_date"),
        "coverage": {"judged": judged_count, "total": len(rows)},
        "rules": RULES,
        "universe_generated_at": collected.get("generated_at"),
        "tracking_failures": collected.get("tracking_failures", 0),
        "indices": rows,
    }
