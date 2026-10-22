"""Audit the csindex PE proxy and the danjuan ROE/PB identity.

1. Checks whether danjuan's current ROE / PB equals 1 / PE across all indices.
2. Replays "anchor PE x price-index change" on danjuan domestic PE indices
   (2016-2026 weekly PE vs eastmoney price-index daily closes, 3000 trading days)
   and prints: price coverage, confusion matrix (false-pass and false-drop
   rates), breakdown by year and by arrival month, worst indices, and a
   2016-2021 vs 2022-2026 split for PROXY_BAND.

Run: uv run python -m scripts.check_pe_proxy
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from discovery.constants import CHINA_MARKET_TIMEZONE, TREASURY_YIELD
from market.funds import DANJUAN_API, FundClient
from screen.index_funds import PROXY_BAND, YIELD_SPREAD_MIN, valuation_method

HORIZONS = (4, 8, 13)  # weeks (13 weeks = 91 days = PROXY_MAX_DAYS)
DOMESTIC = ("SH", "SZ", "CSI")
THRESHOLD_PE = 100 / (float(TREASURY_YIELD) + float(YIELD_SPREAD_MIN))
SPLIT_YEAR = "2022"
TENCENT_KLINE = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={sym},day,,{end},800,"
)


def _long_closes(client: FundClient, danjuan_code: str, raw: str, today: date) -> dict[str, Decimal]:
    """Full 2016+ price-index closes: Tencent for SH/SZ, eastmoney for CSI."""
    if danjuan_code.startswith(("SH", "SZ")):
        sym = danjuan_code.lower()
        closes = {}
        try:
            for end in ("", "2023-12-31", "2020-12-31", "2017-12-31"):
                client.sleep(0.1)
                data = client._json(TENCENT_KLINE.format(sym=sym, end=end)).get("data")
                rows = data.get(sym, {}).get("day") if isinstance(data, dict) else []
                if not rows:
                    break
                for row in rows:
                    closes[row[0]] = Decimal(row[2])
        except Exception:
            closes = {}
        if len(closes) >= 1000:
            return closes
    return client.index_closes(raw, days=3000)


def passes(pe: float) -> bool:
    return pe <= THRESHOLD_PE


def near(pe: float) -> bool:
    return abs(pe / THRESHOLD_PE - 1) < float(PROXY_BAND)


def summarize(records: list[tuple[float, bool, bool]]) -> str:
    """records: (abs_rel_error, proxy_pass, real_pass)."""
    if not records:
        return "无样本"
    errs = sorted(r[0] for r in records)
    n = len(errs)
    tp = sum(p and t for _, p, t in records)
    fp = sum(p and not t for _, p, t in records)
    fn = sum(not p and t for _, p, t in records)
    tn = sum(not p and not t for _, p, t in records)
    fdr = fp / (tp + fp) if (tp + fp) else 0.0
    for_ = fn / (tn + fn) if (tn + fn) else 0.0
    return (
        f"样本 {n}，误差中位 {statistics.median(errs):.1%} / P90 {errs[int(n * 0.9)]:.1%} / 最大 {errs[-1]:.1%}；"
        f"总翻转 {(fp + fn) / n:.2%}（FP={fp} FN={fn} TP={tp} TN={tn}）；"
        f"错误通过率 {fdr:.2%}（{fp}/{tp + fp}），错误排除率 {for_:.2%}（{fn}/{tn + fn}）"
    )


def main() -> None:
    client = FundClient()
    items = client.index_valuations()

    diffs = [
        (abs((i["roe"] / i["pb"]) / (1 / i["pe"]) - 1) * 100, i["name"])
        for i in items
        if (i.get("pe") or 0) > 0 and (i.get("pb") or 0) > 0 and (i.get("roe") or 0) > 0
    ]
    worst_diff, worst_name = max(diffs)
    print(f"== 蛋卷口径核验：ROE/PB 对比 1/PE，共 {len(diffs)} 个正 PE 指数，最大相对差 {worst_diff:.3f}%（{worst_name}）")

    by_h: dict[int, list] = {k: [] for k in HORIZONS}
    coverage = []
    for item in items:
        code = item["index_code"]
        if not code.startswith(DOMESTIC) or valuation_method(item["name"], bool(item.get("pb_flag"))) != "pe":
            continue
        rows = client._json(f"{DANJUAN_API}/index_eva/pe_history/{code}?day=all")["data"]["index_eva_pe_growths"]
        weeks = [
            (datetime.fromtimestamp(r["ts"] / 1000, CHINA_MARKET_TIMEZONE).date().isoformat(), float(r["pe"]))
            for r in rows
            if r.get("pe") and r["pe"] > 0
        ]
        closes = _long_closes(
            client,
            code,
            code.removeprefix("CSI").removeprefix("SH").removeprefix("SZ"),
            datetime.now(CHINA_MARKET_TIMEZONE).date(),
        )
        days = sorted(closes)
        if len(days) < 1000:
            print(f"跳过 {item['name']}：日线 {len(days)} 条")
            continue
        coverage.append((item["name"], days[0], days[-1], len(days), weeks[0][0], weeks[-1][0], len(weeks)))

        def close_on(day: str) -> float | None:
            prior = [d for d in days if d <= day]
            return float(closes[prior[-1]]) if prior else None

        for i, (day, pe) in enumerate(weeks):
            base = close_on(day)
            for k in HORIZONS:
                if i + k >= len(weeks) or base is None:
                    continue
                later_day, real = weeks[i + k]
                later = close_on(later_day)
                if later is None:
                    continue
                proxy = pe * later / base
                by_h[k].append((item["name"], later_day, abs(proxy / real - 1), proxy, passes(proxy), passes(real)))

    print(
        f"== 行情覆盖：{len(coverage)} 个指数，日线均 {min(c[3] for c in coverage)}–{max(c[3] for c in coverage)} 条"
        f"（{min(c[1] for c in coverage)} 至 {max(c[2] for c in coverage)}），"
        f"周度 PE {min(c[4] for c in coverage)} 至 {max(c[5] for c in coverage)}"
    )
    print(f"== 门槛：100/PE ≥ {TREASURY_YIELD}% + {YIELD_SPREAD_MIN}（PE ≤ {THRESHOLD_PE:.1f}），缓冲带 ±{PROXY_BAND:.0%}")
    for k in HORIZONS:
        all_rec = [(e, p, t) for _, _, e, _, p, t in by_h[k]]
        out_rec = [(e, p, t) for _, _, e, pr, p, t in by_h[k] if not near(pr)]
        print(f"[{k:>2} 周·全部]     {summarize(all_rec)}")
        print(f"[{k:>2} 周·带外]     {summarize(out_rec)}")

    recs13 = by_h[13]
    for label, pred in (("2016-2021 样本内", lambda d: d < SPLIT_YEAR), ("2022-2026 样本外", lambda d: d >= SPLIT_YEAR)):
        sub = [(e, p, t) for _, d, e, pr, p, t in recs13 if pred(d) and not near(pr)]
        print(f"[13 周·带外·{label}] {summarize(sub)}")

    by_year: dict[str, list] = defaultdict(list)
    by_month: dict[str, list] = defaultdict(list)
    by_idx: dict[str, list] = defaultdict(list)
    for name, day, e, pr, p, t in recs13:
        if not near(pr):
            by_year[day[:4]].append((e, p, t))
            by_month[day[5:7]].append((e, p, t))
            by_idx[name].append((e, p, t))
    print("== 13 周带外·按年份（错误通过率 / 总翻转）：", "  ".join(
        f"{y}: {sum(p and not t for _, p, t in v)}/{sum(p for _, p, _ in v) or 1} ({(sum(p != t for _, p, t in v) / len(v)):.2%})"
        for y, v in sorted(by_year.items())
    ))
    print("== 13 周带外·按到达月份（总翻转）：", "  ".join(
        f"{m}月:{(sum(p != t for _, p, t in v) / len(v)):.2%}" for m, v in sorted(by_month.items())
    ))
    worst_fp = sorted(
        ((sum(p and not t for _, p, t in v), sum(p for _, p, _ in v), name) for name, v in by_idx.items()),
        reverse=True,
    )[:5]
    print("== 13 周带外·错误通过最多的指数：", ", ".join(f"{n} {fp}/{ tot }" for fp, tot, n in worst_fp if fp))


if __name__ == "__main__":
    main()
