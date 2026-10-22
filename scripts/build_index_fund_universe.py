"""Rebuild screen/index_fund_universe.json.

One representative fund per tracked equity index (cheapest viable, ETF or OTC
A-class; feeder and enhanced funds skipped), indices ranked by total scale of
every fund tracking them, then near-duplicates dropped by daily-return
correlation until UNIVERSE_LIMIT indices remain.

Run quarterly (~30-40 min): uv run python -m scripts.build_index_fund_universe
csindex bans the IP for hours when hit too often; matching stops at the first
403 and keeps progress. Resume later without rescanning funds:
    uv run python -m scripts.build_index_fund_universe --valuation-only
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

from discovery.constants import china_market_date
from jobs.index_funds import UNIVERSE_PATH, _json_default, load_universe
from market.funds import CsindexBlocked, FundClient
from screen.index_funds import (
    EQUITY_TYPES,
    MIN_OVERLAP_DAYS,
    SIMILAR_CORRELATION,
    UNIVERSE_LIMIT,
    pick_representative,
    select_distinct,
    static_product,
)

DANJUAN_CATEGORY = {"1": "宽基", "2": "策略", "3": "行业"}
CSINDEX_CATEGORY = {"规模": "宽基", "行业": "行业", "主题": "主题", "策略": "策略", "风格": "风格"}
NAME_PROBES = 3
HISTORY_START = date(2016, 1, 1)
_SHARE_CLASS = re.compile(r"[B-Z]$")
_BASIS = re.compile(r"([（(](价格|收益|全收益|人民币|港元|港币|美元)[)）]|(人民币|港元|港币|美元))?(指数)?$")
DANJUAN_ALIASES = {
    "沪深300价值指数": "SH000919",
    "中证医药100指数": "SH000978",
    "上证50AH优选指数": "SH000170",
    "上证50AH优选人民币指数": "SH000170",
    "中证锐联基本面50指数": "SH000925",
    "标普中国A股红利机会指数": "CSPSADRP",
    "标普中国A股大盘价值指数": "SPACEVCP",
    "标普中国A股质量价值指数": "SPCQVCP",
    "细分食品": "SZ399396",
    "国证食品饮料行业指数": "SZ399396",
    "国证房地产行业指数": "SZ399393",
    "国证新能源汽车指数": "SZ399417",
    "恒生指数": "HKHSI",
    "恒生中国企业指数": "HKHSCEI",
    "恒生香港35指数": "HSFML25",
    "恒生港股通新经济指数": "HKHSSCNE",
    "标普香港上市中国中小盘精选指数": "SPHCMSHP",
    "纳斯达克100指数": "NDX",
    "标准普尔500指数": "SP500",
    "法兰克福DAX指数": "GDAXI",
}
CSINDEX_ALIASES = {
    "中证香港证券投资主题港元指数": ("930709", "香港证券", "主题"),
    "中证港股通非银行金融主题人民币指数": ("931024", "港股通非银", "主题"),
    "中证港股通高股息投资港元指数": ("930914", "港股通高股息", "策略"),
    "中证港股通高股息精选港元指数": ("930839", "港股通高息精选", "策略"),
    "中证沪港深创新药产业人民币指数": ("931409", "SHS创新药", "主题"),
    "中证港股通内地金融港元指数": ("H11146", "港股通内地金融", "行业"),
    "中证港股通消费主题港元指数": ("931454", "港股通消费", "主题"),
    "中证沪港深云计算产业人民币指数": ("931470", "SHS云计算", "主题"),
    "中证香港银行投资人民币指数": ("930792", "HK银行", "主题"),
    "中证银行AH价格优选人民币指数": ("931039", "银行AH", "策略"),
    "中证全球中国教育主题人民币指数": ("931456", "中国教育", "主题"),
    "有色金属": ("000819", "有色金属", "行业"),
    "细分化工": ("000813", "细分化工", "主题"),
    "消费80": ("000069", "消费80", "主题"),
    "上证资源": ("000068", "上证资源", "主题"),
    "中证360互联网+大数据100指数": ("930734", "360互联+", "策略"),
    "大数据100指数": ("930734", "360互联+", "策略"),
    "上证科创板新能源主题指数": ("000692", "科创新能源", "主题"),
}


def log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def scan(client, today):
    """跟踪标的 -> static products, fund code -> 跟踪标的, overseas targets."""
    etfs = client.etf_list()
    etf_rows = {e["fund_code"]: e for e in etfs}
    otc = [
        ("场外", code, code, name)
        for code, name, kind in client.fund_codes()
        if kind in EQUITY_TYPES
        and code not in etf_rows
        and "增强" not in name
        and "联接" not in name
        and not _SHARE_CLASS.search(name)
    ]
    todo = [("场内", e["code"], e["fund_code"], e["name"]) for e in etfs if "增强" not in e["name"]] + otc
    groups: dict[str, list[dict]] = defaultdict(list)
    code_target: dict[str, str] = {}
    overseas: set[str] = set()
    for n, (kind, code, fund_code, name) in enumerate(todo, 1):
        if n % 100 == 0:
            log(f"扫描基金 {n}/{len(todo)}")
        try:
            profile = client.fund_profile(fund_code) or {}
        except Exception as exc:
            log(f"跳过 {fund_code}：{exc}")
            continue
        target = profile.get("跟踪标的", "")
        if profile.get("基金类型") not in EQUITY_TYPES or target in {"", "--"} or "无跟踪" in target:
            continue
        product = static_product(kind, code, fund_code, name, profile)
        if kind == "场内":
            cap, turnover = etf_rows[fund_code]["market_cap"], etf_rows[fund_code]["turnover"]
            product["scale"] = None if cap is None else cap / 100_000_000
            product["turnover"] = None if turnover is None else turnover / 10000
        groups[target].append(product)
        code_target[fund_code] = target
        if profile["基金类型"] == "指数型-海外股票":
            overseas.add(target)
    return groups, code_target, overseas


def danjuan_by_target(client, code_target: dict[str, str]) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    items = client.index_valuations()
    by_code = {item["index_code"]: item for item in items}
    for target, dj_code in DANJUAN_ALIASES.items():
        if dj_code in by_code:
            mapping[target] = by_code[dj_code]
    for item in items:
        funds = client.passive_funds(item["index_code"])
        targets = {code_target[f["fund_code"]] for f in funds if f["fund_code"] in code_target}
        if not targets:
            feeders_first = sorted(funds, key=lambda f: "ETF联接" not in str(f.get("fund_name")))
            for fund in feeders_first[:NAME_PROBES]:
                target = (client.fund_profile(fund["fund_code"]) or {}).get("跟踪标的")
                if target:
                    targets.add(target)
        for target in targets:
            mapping.setdefault(target, item)
        raw = item["index_code"].removeprefix("CSI").removeprefix("SH").removeprefix("SZ")
        mapping.setdefault(f"__code__:{raw}", item)
    return mapping


def attach_valuation(client, candidate: dict, danjuan: dict, overseas: set[str], today) -> None:
    """Danjuan first, else csindex (weekly PE history frozen into the universe).
    Raises CsindexBlocked so the caller stops before the ban lengthens."""
    target = candidate["index_name"]
    item = danjuan.get(target)
    category = DANJUAN_CATEGORY.get(str(item.get("ttype")), "其他") if item else "其他"
    valuation = {"source": "danjuan", "code": item["index_code"]} if item else None
    known = candidate.get("valuation") or {}
    alias = CSINDEX_ALIASES.get(target)
    if item is None and (known.get("source") == "csindex" or alias):
        code = known.get("code") or alias[0]
        dj_item = danjuan.get(f"__code__:{code}")
        if dj_item:
            valuation = {"source": "danjuan", "code": dj_item["index_code"]}
            candidate["name"] = dj_item["name"]
            category = DANJUAN_CATEGORY.get(str(dj_item.get("ttype")), "其他")
        else:
            pe = client.csindex_pe(code, HISTORY_START, today)
            valuation = (
                {"source": "csindex", "code": code, **pe}
                if pe["history"] and pe["pe_date"]
                else None
            )
            if alias and candidate.get("name") == target:
                candidate["name"] = alias[1]
            category = candidate.get("category") if candidate.get("category") not in (None, "其他") else (alias[2] if alias else "其他")
    elif item is None:
        query = f"{_BASIS.sub('', target)}指数" if "指数" in target else target
        hit = client.csindex_search(query)
        if hit:
            code = hit["indexCode"]
            dj_item = danjuan.get(f"__code__:{code}")
            if dj_item:
                valuation = {"source": "danjuan", "code": dj_item["index_code"]}
                candidate["name"] = dj_item["name"]
                category = DANJUAN_CATEGORY.get(str(dj_item.get("ttype")), "其他")
            else:
                pe = client.csindex_pe(code, HISTORY_START, today)
                clean_target = _BASIS.sub("", target)
                matched = clean_target in {
                    _BASIS.sub("", pe["name"]),
                    _BASIS.sub("", str(hit.get("indexName") or "")),
                }
                if pe["history"] and pe["pe_date"] and matched:
                    valuation = {"source": "csindex", "code": code, **pe}
                    candidate["name"] = hit.get("indexName") or target
                    category = CSINDEX_CATEGORY.get(hit.get("indexClassify"), "其他")
    if valuation and valuation["source"] == "csindex":
        valuation.pop("name", None)
    candidate["valuation"] = valuation
    candidate["category"] = "海外" if target in overseas else category
    candidate["valuation_checked"] = True


def match_valuations(client, items: list[dict], danjuan: dict, overseas: set[str], today) -> int:
    def pending(item: dict) -> bool:
        valuation = item.get("valuation") or {}
        if valuation.get("source") == "danjuan":
            return False
        return not item.get("valuation_checked") or (
            valuation.get("source") == "csindex"
            and ("pe_date" not in valuation or not valuation.get("history"))
        )

    todo = [item for item in items if pending(item)]
    for n, item in enumerate(todo, 1):
        if n % 20 == 0:
            log(f"匹配估值 {n}/{len(todo)}")
        try:
            attach_valuation(client, item, danjuan, overseas, today)
        except CsindexBlocked:
            left = len(todo) - n + 1
            log(f"中证限流，剩 {left} 个指数未匹配；隔几小时后运行 --valuation-only 续跑")
            return left
        except Exception as exc:
            log(f"估值匹配失败 {item['index_name']}：{exc}")
    return 0


def write(universe: dict) -> None:
    UNIVERSE_PATH.write_text(
        json.dumps(universe, ensure_ascii=False, indent=1, default=_json_default),
        encoding="utf-8",
    )


def valuation_only() -> None:
    universe = load_universe()
    overseas = {i["index_name"] for i in universe["items"] if i.get("category") == "海外"}
    left = match_valuations(FundClient(), universe["items"], {}, overseas, china_market_date())
    universe["counts"]["unmatched_pending"] = left
    write(universe)
    log(f"写入 {UNIVERSE_PATH}，待续 {left} 个")


def main() -> None:
    client = FundClient()
    today = china_market_date()
    groups, code_target, overseas = scan(client, today)
    candidates = []
    for target, products in groups.items():
        fund = pick_representative(products, today)
        if fund is not None:
            candidates.append(
                {
                    "index_name": target,
                    "name": target,
                    "aggregate_scale": sum(p["scale"] or 0 for p in products),
                    "fund": fund,
                }
            )
    candidates.sort(key=lambda c: -c["aggregate_scale"])
    log(f"跟踪指数 {len(groups)} 个，有合格代表基金的 {len(candidates)} 个")
    danjuan = danjuan_by_target(client, code_target)
    for candidate in candidates:
        target = candidate["index_name"]
        if target in danjuan:
            candidate["name"] = danjuan[target]["name"]
            candidate["valuation"] = {"source": "danjuan", "code": danjuan[target]["index_code"]}
        elif target in CSINDEX_ALIASES or any(k in target for k in ("中证", "上证", "沪深")):
            candidate["valuation"] = {"source": "csindex"}

    def returns_of(candidate: dict) -> dict[str, float]:
        try:
            return client.daily_returns(candidate["fund"])
        except Exception as exc:
            log(f"收益序列失败 {candidate['fund']['code']}：{exc}")
            return {}

    accepted, skipped = select_distinct(candidates, returns_of)
    for candidate in accepted:
        candidate["category"] = "海外" if candidate["index_name"] in overseas else "其他"
    left = match_valuations(client, accepted, danjuan, overseas, today)
    universe = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rules": {
            "limit": UNIVERSE_LIMIT,
            "similar_correlation": SIMILAR_CORRELATION,
            "min_overlap_days": MIN_OVERLAP_DAYS,
        },
        "counts": {
            "tracked_indices": len(groups),
            "with_representative": len(candidates),
            "selected": len(accepted),
            "skipped": len(skipped),
            "unmatched_pending": left,
        },
        "items": accepted,
        "skipped": [
            {k: c[k] for k in ("name", "index_name", "aggregate_scale", "skip")} for c in skipped
        ],
    }
    write(universe)
    log(f"写入 {UNIVERSE_PATH}：{len(accepted)} 个指数，估值待续 {left} 个")


if __name__ == "__main__":
    valuation_only() if "--valuation-only" in sys.argv else main()
