from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from research.llm import LLMClient
from research.search import fetch_hits
from research.thesis import expensive_line

GATE_NAMES = ("能力圈", "好生意", "护城河", "管理层", "安全边际", "决策纪律")
REQUIRED_KEYS = (
    "verdict",
    "gates",
    "mirror_test",
    "price_bands",
    "memo",
    "data_grade",
    "as_of",
    "portfolio_fit",
)
VERDICTS = frozenset({"pass", "fail", "gray"})
GATE_STATUSES = frozenset({"pass", "fail", "gray"})
BAND_MODES = frozenset({"aggressive", "steady", "conservative", "watch"})
MEMO_ROLES = frozenset({"core", "satellite", "skip"})
GRADES = frozenset({"A", "B", "C"})
MIRROR_MIN = 5
CYCLE_TOKENS = ("有色", "煤炭", "航运", "证券", "锂", "钢", "铝", "化工", "铜", "锌", "镍", "煤")


@dataclass(frozen=True)
class EvalInput:
    name: str
    code: str
    financials: tuple[dict[str, Any], ...]
    price: Decimal | None
    book: dict[str, Any]
    web_search: bool
    as_of_date: str
    job_id: int
    reports_dir: Path
    valuation: dict[str, Any] | None = None
    industry: str = ""
    search_hits: tuple[dict[str, Any], ...] = ()
    notices: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class EvalOutput:
    structured: dict[str, Any]
    markdown: str
    report_path: str


class EvalError(ValueError):
    pass


def has_min_financials(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> bool:
    for row in rows:
        roe = row.get("roe")
        ni = row.get("net_income")
        if roe is None or ni is None:
            continue
        try:
            if Decimal(str(roe)).is_finite() and Decimal(str(ni)).is_finite():
                return True
        except Exception:
            continue
    return False


def has_valuation(raw: dict[str, Any] | None) -> bool:
    if not raw:
        return False
    return any(raw.get(key) is not None for key in ("pb", "pe", "bps", "npl_pct", "eps"))


def data_insufficient(payload: EvalInput) -> bool:
    price_ok = (
        payload.price is not None
        and payload.price.is_finite()
        and payload.price > 0
    )
    if not price_ok:
        return True
    return not has_min_financials(payload.financials)


def run_checklist(llm: LLMClient, payload: EvalInput) -> EvalOutput:
    if payload.web_search and not payload.search_hits:
        payload = replace(
            payload,
            search_hits=tuple(fetch_hits(payload.name, payload.code, payload.as_of_date)),
        )
    insufficient = data_insufficient(payload)
    if insufficient:
        structured = _gray_from_facts(payload)
    else:
        raw_text = llm.complete(_build_prompt(payload), name=payload.name)
        parsed = _parse_model_json(raw_text)
        structured = normalize_structured(parsed, payload)
    structured["valuation"] = _enrich_valuation(payload)
    if payload.web_search:
        structured["search_hits"] = [dict(row) for row in payload.search_hits]
    if payload.notices:
        structured["notices"] = [dict(row) for row in payload.notices]
    markdown = render_markdown(payload.name, structured, web_search=payload.web_search)
    path = _write_report(payload, markdown)
    return EvalOutput(structured=structured, markdown=markdown, report_path=str(path))


def normalize_structured(raw: object, payload: EvalInput) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvalError("评估结果不是对象")
    raw = _canonicalize_eval_dict(raw)
    missing = [key for key in REQUIRED_KEYS if key not in raw]
    if missing:
        raise EvalError(f"缺少字段: {', '.join(missing)}")
    verdict = _coerce_status(str(raw.get("verdict") or "").strip())
    if verdict not in VERDICTS:
        raise EvalError("结论必须是 pass / fail / gray")
    gates = _normalize_gates(raw.get("gates"))
    mirror = _normalize_mirror(raw.get("mirror_test"))
    bands = _normalize_bands(raw.get("price_bands"))
    memo = _normalize_memo(raw.get("memo"))
    grade = str(raw.get("data_grade") or "").strip().upper()
    if grade not in GRADES:
        grade = "C"
    if not payload.web_search and grade == "A":
        grade = "B"
    fit = _normalize_fit(raw.get("portfolio_fit"), payload.book)
    if any(gate.get("status") == "fail" for gate in gates):
        verdict = "fail"
    elif verdict == "pass" and (
        _mirror_count(mirror) < MIRROR_MIN
        or any(gate.get("status") == "gray" for gate in gates)
    ):
        verdict = "gray"
    if verdict == "pass" and payload.price is not None and payload.price > 0:
        ceiling = expensive_line(bands, payload.price)
        if ceiling is not None and payload.price > ceiling:
            verdict = "fail"
    return {
        "verdict": verdict,
        "gates": gates,
        "mirror_test": mirror,
        "price_bands": bands,
        "memo": memo,
        "data_grade": grade,
        "as_of": {"date": payload.as_of_date, "price": _json_optional(payload.price)},
        "portfolio_fit": fit,
    }


def render_markdown(name: str, structured: dict[str, Any], *, web_search: bool) -> str:
    verdict = _verdict_zh(str(structured.get("verdict")))
    gates = structured.get("gates") if isinstance(structured.get("gates"), list) else []
    gate_lines = []
    for item in gates:
        if not isinstance(item, dict):
            continue
        gate_lines.append(
            f"- {item.get('name')}: {_gate_zh(str(item.get('status')))} — {item.get('reason')}"
        )
    mirror = structured.get("mirror_test") if isinstance(structured.get("mirror_test"), list) else []
    mirror_lines = [f"{index}. {line}" for index, line in enumerate(mirror, start=1)]
    bands = structured.get("price_bands") if isinstance(structured.get("price_bands"), dict) else {}
    memo = structured.get("memo") if isinstance(structured.get("memo"), dict) else {}
    fit = structured.get("portfolio_fit") if isinstance(structured.get("portfolio_fit"), dict) else {}
    as_of = structured.get("as_of") if isinstance(structured.get("as_of"), dict) else {}
    hits = structured.get("search_hits") if isinstance(structured.get("search_hits"), list) else []
    if not web_search:
        search_note = "未开启网页搜索。评估基于已抓取财务，并非缺少财务。"
    elif hits:
        search_note = "已开启网页搜索。检索摘要已交给模型，财务数字仍以已抓取财务为准。"
    else:
        search_note = "已开启网页搜索但未取到结果。评估基于已抓取财务，并非缺少财务。"
    lines = [
        f"# {name} 决策备忘录",
        "",
        "## 结论",
        "",
        verdict,
        "",
        "## 六关",
        "",
        *gate_lines,
        "",
        "## 镜子测试",
        "",
        *mirror_lines,
        "",
        "## 价格带",
        "",
        f"策略：{_band_zh(str(bands.get('mode')))}",
        f"激进：{bands.get('aggressive') or '—'}",
        f"稳健：{bands.get('steady') or '—'}",
        f"保守：{bands.get('conservative') or '—'}",
        "",
        "## 备忘",
        "",
        f"生意本质：{memo.get('essence') or ''}",
        f"主要风险：{memo.get('risks') or ''}",
        f"仓位角色：{_role_zh(str(memo.get('role')))}",
        "",
        "## 对照当前仓",
        "",
        f"该不该进当前仓：{fit.get('should_enter') or ''}",
        f"用什么钱买：{fit.get('funding') or ''}",
        f"是否叠逻辑：{fit.get('overlap') or ''}",
        "",
        f"信息丰富度：{structured.get('data_grade')}",
        f"研究日期：{as_of.get('date')}",
        f"所用股价：{as_of.get('price') if as_of.get('price') is not None else '无现价'}",
        _valuation_md(structured.get("valuation")),
        search_note,
        _notices_md(structured.get("notices")),
        "",
    ]
    return "\n".join(lines)


def _gray_from_facts(payload: EvalInput) -> dict[str, Any]:
    reason = "财务或行情不足，不编造数字。"
    return {
        "verdict": "gray",
        "gates": [{"name": name, "status": "gray", "reason": reason} for name in GATE_NAMES],
        "mirror_test": [
            "财务或现价不足，第一句无法完成。",
            "财务或现价不足，第二句无法完成。",
            "财务或现价不足，第三句无法完成。",
            "财务或现价不足，第四句无法完成。",
            "财务或现价不足，第五句无法完成。",
        ],
        "price_bands": {
            "mode": "watch",
            "aggressive": "",
            "steady": "",
            "conservative": "",
            "overvalued": "",
        },
        "memo": {
            "essence": "财务或行情不足，不编造生意数字。",
            "risks": "缺少可核验财务或现价。",
            "role": "skip",
        },
        "data_grade": "C",
        "as_of": {"date": payload.as_of_date, "price": _json_optional(payload.price)},
        "portfolio_fit": _fit_from_book(payload.book),
        "valuation": _enrich_valuation(payload),
    }


def _enrich_valuation(payload: EvalInput) -> dict[str, Any]:
    raw = dict(payload.valuation or {})
    for row in payload.financials:
        if not isinstance(row, dict):
            continue
        for key in ("bps", "npl_pct"):
            if raw.get(key) is None and row.get(key) is not None:
                raw[key] = row[key]
        if raw.get("period") is None and row.get("period") is not None:
            raw["period"] = row.get("period")
    annual_eps, annual_period = _latest_annual_eps(payload.financials)
    if annual_eps is not None:
        raw["eps"] = _json_optional(annual_eps)
        if annual_period:
            raw["eps_period"] = annual_period
    price = payload.price
    if price is not None:
        raw.setdefault("price", _json_optional(price))
    bps = _as_decimal(raw.get("bps"))
    eps = _as_decimal(raw.get("eps"))
    if raw.get("pb") is None and price is not None and bps is not None and bps > 0:
        raw["pb"] = _json_optional(price / bps)
    if price is not None and eps is not None and eps > 0:
        raw["pe"] = _json_optional(price / eps)
        basis = raw.get("eps_period") or ""
        raw["pe_basis"] = f"静态（现价/{basis}年报EPS）" if basis else "静态（现价/年报EPS）"
    else:
        raw.pop("pe", None)
        raw.pop("pe_basis", None)
    raw.pop("pe_mid", None)
    raw.pop("pe_mid_basis", None)
    raw.pop("eps_mid", None)
    if _is_cyclical(payload.name, payload.industry):
        mid_eps, mid_years = _median_annual_eps(payload.financials)
        if price is not None and mid_eps is not None and mid_eps > 0:
            raw["eps_mid"] = _json_optional(mid_eps)
            raw["pe_mid"] = _json_optional(price / mid_eps)
            raw["pe_mid_basis"] = f"中周期对照（现价/{mid_years}年年报EPS中位数）"
    return raw


def _latest_annual_eps(rows: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> tuple[Decimal | None, str]:
    best_eps: Decimal | None = None
    best_period = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("period_type") or "annual")
        if kind and kind != "annual":
            continue
        eps = _as_decimal(row.get("eps"))
        if eps is None:
            continue
        period = str(row.get("period") or "")
        if best_eps is None or period > best_period:
            best_eps = eps
            best_period = period
    return best_eps, best_period


def _median_annual_eps(
    rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> tuple[Decimal | None, int]:
    by_period: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("period_type") or "annual")
        if kind and kind != "annual":
            continue
        eps = _as_decimal(row.get("eps"))
        if eps is None:
            continue
        period = str(row.get("period") or "")
        if not period:
            continue
        by_period[period] = eps
    values = list(by_period.values())
    if len(values) < 2:
        return None, len(values)
    # ponytail: all stored annual EPS, not a 5–8y cycle window; upgrade if history spans multiple regimes
    return Decimal(str(statistics.median(values))), len(values)


def _is_cyclical(name: str, industry: str = "") -> bool:
    haystack = f"{name}{industry}"
    return any(token in haystack for token in CYCLE_TOKENS)


_VALUATION_ZH = {
    "price": "现价",
    "bps": "每股净资产",
    "pb": "市净率",
    "pe": "静态市盈率",
    "pe_basis": "市盈率口径",
    "eps_mid": "中周期每股收益",
    "pe_mid": "中周期对照市盈率",
    "pe_mid_basis": "中周期市盈率口径",
    "npl_pct": "不良率",
    "eps": "每股收益",
    "eps_period": "每股收益报告期",
    "period": "报告期",
}


def _valuation_zh(raw: dict[str, Any]) -> dict[str, Any]:
    return {_VALUATION_ZH.get(key, key): value for key, value in raw.items()}


def _valuation_md(raw: object) -> str:
    if not isinstance(raw, dict) or not raw:
        return "估值：无"
    parts = [f"{_VALUATION_ZH.get(key, key)} {raw[key]}" for key in _VALUATION_ZH if key in raw]
    extra = [f"{key} {value}" for key, value in raw.items() if key not in _VALUATION_ZH]
    return "估值：" + "，".join(parts + extra)


def _as_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return number


def _notices_md(raw: object) -> str:
    if not isinstance(raw, list) or not raw:
        return "未拉取到巨潮公告标题。分红与股权以已抓取财务为准，不编造公告。"
    lines = ["巨潮公告（仅标题，无正文）："]
    for row in raw:
        if not isinstance(row, dict):
            continue
        date = row.get("date") or ""
        title = row.get("title") or ""
        if title:
            lines.append(f"- {date} {title}".strip())
    return "\n".join(lines) if len(lines) > 1 else "未拉取到巨潮公告标题。分红与股权以已抓取财务为准，不编造公告。"


def _notices_prompt_block(payload: EvalInput) -> str:
    if not payload.notices:
        return "巨潮公告：未取到标题（这不代表没有财务）。"
    blob = json.dumps([dict(row) for row in payload.notices], ensure_ascii=False)
    return (
        f"巨潮公告标题：{blob}。"
        "公告只有标题没有正文；可补分红、不分配、股权结构线索，禁止把标题写成已抓取财务数字，禁止编造未出现的派息金额。"
    )


def _search_prompt_block(payload: EvalInput) -> str:
    if not payload.web_search:
        return "网页搜索：未开启（这不代表没有财务）。"
    hits = [dict(row) for row in payload.search_hits]
    if not hits:
        return "网页搜索：已开启但未取到结果（这不代表没有财务）。"
    blob = json.dumps(hits, ensure_ascii=False)
    return (
        f"网页搜索：已开启。检索摘要：{blob}。"
        "检索可补充中报趋势、分红、股权结构；禁止用摘要改写已抓取财务或估值快照里的数字。"
    )


def _cycle_prompt_lock(snap: dict[str, Any]) -> str:
    if snap.get("pe_mid") is None:
        return ""
    return (
        "周期股可讨论周期位置、资本开支与库存，但市盈率结论与价格带必须仍用静态市盈率。"
        "禁止用中周期对照市盈率替代静态市盈率，禁止用景气高峰年份EPS压低市盈率，"
        "禁止把中周期对照市盈率写入保守带上沿。"
    )


def _build_prompt(payload: EvalInput) -> str:
    book = json.dumps(payload.book, ensure_ascii=False)
    financials = json.dumps(list(payload.financials), ensure_ascii=False)
    snap = _enrich_valuation(payload)
    valuation = json.dumps(_valuation_zh(snap), ensure_ascii=False)
    search = _search_prompt_block(payload)
    price = _json_optional(payload.price)
    return (
        f"请对 {payload.name}（{payload.code}）做六关 Checklist 与决策备忘录。"
        f"只输出 JSON，键必须是英文: {', '.join(REQUIRED_KEYS)}。"
        f"verdict 只能是 pass/fail/gray。"
        f"gates 必须是数组（不能是对象），六项且每项含 name/status/reason，"
        f"name 覆盖：{'、'.join(GATE_NAMES)}，status 为 pass/fail/gray。"
        f"mirror_test 必须是五句。"
        f"给出价格带：激进 / 稳健 / 保守，或观望。"
        f"price_bands.mode 为 aggressive/steady/conservative/watch。"
        f"mode 不是 watch 时，aggressive、steady、conservative 各写一个现价数字，不要空着，不要写 PE/PB。"
        f"watch 时这三项留空。"
        f"memo 为对象，含 essence、risks、role；role 只能是 core/satellite/skip。"
        f"portfolio_fit 必须对照当前仓回答 should_enter、funding、overlap。"
        f"研究日期 {payload.as_of_date}，现价 {price}。"
        f"{search}{_notices_prompt_block(payload)}已抓取财务：{financials}。估值快照：{valuation}。当前仓：{book}。"
        f"必须用估值快照与已抓取财务核验，禁止编造数字。"
        f"银行优先用市净率、每股净资产、不良率；缺毛利率、EBIT、分红率不得把安全边际或总结论标为 gray。"
        f"市盈率一律用估值快照里的静态市盈率（现价/最新年报每股收益）。"
        f"禁止改用动态、TTM、滚动市盈率，禁止用中报或季报EPS重算市盈率，禁止把两种口径写进同一份结论。"
        f"中报与季报只用于看趋势（收入、利润、ROE是否在掉），不得改写年报每股收益。"
        f"{_cycle_prompt_lock(snap)}"
        f"任一关失败则 verdict 必须是 fail，不得改成 gray 或 pass。"
        f"中报趋势恶化或说不清还是不是好生意时，好生意或护城河可以为 gray，总结论也可以是 gray。"
        f"没有安全边际则安全边际关为 fail。"
        f"亏损或每股收益≤0则不写市盈率，不得编造。禁止编造快照与财务中未出现的数字。"
        f"财务或行情不足、算不清价格时 verdict 才是 gray。"
    )


_KEY_ALIASES = {
    "verdict": ("verdict", "结论"),
    "gates": ("gates", "六关", "checklist"),
    "mirror_test": ("mirror_test", "镜子测试", "镜子"),
    "price_bands": ("price_bands", "价格带"),
    "memo": ("memo", "备忘", "决策备忘录"),
    "data_grade": ("data_grade", "信息丰富度"),
    "as_of": ("as_of", "研究日期"),
    "portfolio_fit": ("portfolio_fit", "对照当前仓", "仓位匹配"),
}
_MEMO_ALIASES = {
    "essence": ("essence", "生意本质", "生意", "本质", "business", "nature", "thesis", "summary"),
    "risks": ("risks", "主要风险", "风险", "risk", "main_risk", "key_risks"),
    "role": ("role", "仓位角色", "仓位", "角色", "position", "position_role", "suggested_role"),
}
_FIT_ALIASES = {
    "should_enter": (
        "should_enter",
        "shouldEnter",
        "enter",
        "in_book",
        "该不该进当前仓",
        "该不该进",
        "该不该买",
        "是否进入",
        "是否进仓",
        "是否买入",
    ),
    "funding": (
        "funding",
        "funding_source",
        "how_to_fund",
        "cash",
        "用什么钱买",
        "用什么钱",
        "资金来源",
        "资金",
    ),
    "overlap": (
        "overlap",
        "overlap_logic",
        "是否叠逻辑",
        "叠逻辑",
        "是否重叠",
        "会不会叠逻辑",
        "逻辑重叠",
        "重叠",
    ),
}
_BAND_ALIASES = {
    "mode": ("mode", "策略"),
    "aggressive": ("aggressive", "激进", "便宜买"),
    "steady": ("steady", "稳健", "合理买"),
    "conservative": ("conservative", "保守", "停止加仓"),
    "overvalued": ("overvalued", "明显高估", "高估"),
}


def _first_key(raw: dict[str, Any], names: tuple[str, ...]) -> object:
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _canonicalize_eval_dict(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(raw)
    for key, names in _KEY_ALIASES.items():
        if key not in out or out.get(key) in (None, ""):
            value = _first_key(raw, names)
            if value is not None:
                out[key] = value
    memo = out.get("memo")
    if isinstance(memo, dict):
        mapped = dict(memo)
        for key, names in _MEMO_ALIASES.items():
            if key not in mapped or not mapped.get(key):
                value = _first_key(memo, names)
                if value is not None:
                    mapped[key] = value
        if "role" in mapped:
            mapped["role"] = _coerce_role(str(mapped["role"]))
        out["memo"] = mapped
    fit = out.get("portfolio_fit")
    if isinstance(fit, dict):
        mapped = dict(fit)
        for key, names in _FIT_ALIASES.items():
            if key not in mapped or not mapped.get(key):
                value = _first_key(fit, names)
                if value is not None:
                    mapped[key] = value
        out["portfolio_fit"] = mapped
    bands = out.get("price_bands")
    if isinstance(bands, dict):
        mapped = dict(bands)
        for key, names in _BAND_ALIASES.items():
            if key not in mapped or not mapped.get(key):
                value = _first_key(bands, names)
                if value is not None:
                    mapped[key] = value
        if "mode" in mapped:
            mapped["mode"] = _coerce_band(str(mapped["mode"]))
        out["price_bands"] = mapped
    return out


def _coerce_status(value: str) -> str:
    text = value.strip().lower()
    mapping = {
        "pass": "pass",
        "通过": "pass",
        "fail": "fail",
        "失败": "fail",
        "不通过": "fail",
        "未通过": "fail",
        "gray": "gray",
        "灰色": "gray",
        "灰色地带": "gray",
    }
    exact = mapping.get(text, mapping.get(value.strip(), ""))
    if exact in VERDICTS or exact in GATE_STATUSES:
        return exact
    if "未通过" in text or "不通过" in text or "失败" in text:
        return "fail"
    if "灰色" in text or "gray" in text:
        return "gray"
    if "通过" in text or "pass" in text:
        return "pass"
    return value.strip()


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_as_text(item) for item in value]
        return "；".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "summary", "value", "reason", "desc"):
            text = _as_text(value.get(key))
            if text:
                return text
        parts = [_as_text(item) for item in value.values()]
        return "；".join(part for part in parts if part)
    return str(value).strip()


def _coerce_role(value: str) -> str:
    text = value.strip().lower()
    mapping = {
        "core": "core",
        "核心": "core",
        "satellite": "satellite",
        "卫星": "satellite",
        "skip": "skip",
        "不买": "skip",
        "watch": "skip",
        "观望": "skip",
    }
    exact = mapping.get(text, mapping.get(value.strip(), ""))
    if exact in MEMO_ROLES:
        return exact
    if "核心" in text or "core" in text:
        return "core"
    if "卫星" in text or "satellite" in text:
        return "satellite"
    if any(
        token in text
        for token in ("不买", "跳过", "观望", "skip", "watch", "不持有", "不建议", "观察")
    ):
        return "skip"
    if any(token in text for token in ("持有", "标配", "增持", "配置")):
        return "satellite"
    if any(token in text for token in ("减持", "清仓")):
        return "skip"
    return value.strip()


def _coerce_band(value: str) -> str:
    text = value.strip().lower()
    mapping = {
        "aggressive": "aggressive",
        "激进": "aggressive",
        "便宜买": "aggressive",
        "steady": "steady",
        "稳健": "steady",
        "合理买": "steady",
        "conservative": "conservative",
        "保守": "conservative",
        "停止加仓": "conservative",
        "watch": "watch",
        "观望": "watch",
    }
    exact = mapping.get(text, mapping.get(value.strip(), ""))
    if exact in BAND_MODES:
        return exact
    if "观望" in text or "watch" in text:
        return "watch"
    if "便宜买" in text or "激进" in text or "aggressive" in text:
        return "aggressive"
    if "停止加仓" in text or "保守" in text or "conservative" in text:
        return "conservative"
    if "合理买" in text or "稳健" in text or "steady" in text:
        return "steady"
    return value.strip()


def _parse_model_json(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise EvalError("模型输出不是 JSON")


def _official_gate_name(name: str) -> str:
    if name in GATE_NAMES:
        return name
    for official in GATE_NAMES:
        if official in name:
            return official
    return name


def _gates_as_list(raw: object) -> list[object]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EvalError("gates 必须是数组") from exc
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        items: list[object] = []
        for key, value in raw.items():
            if isinstance(value, dict):
                row = dict(value)
                if not str(row.get("name") or "").strip():
                    row["name"] = key
                items.append(row)
            elif isinstance(value, str) and value.strip():
                status = _coerce_status(value)
                if status not in GATE_STATUSES:
                    status = "gray"
                items.append({"name": key, "status": status, "reason": value.strip()})
        return items
    raise EvalError("gates 必须是数组")


def _normalize_gates(raw: object) -> list[dict[str, str]]:
    by_name: dict[str, dict[str, str]] = {}
    for item in _gates_as_list(raw):
        if not isinstance(item, dict):
            continue
        name = _official_gate_name(str(item.get("name") or "").strip())
        status = _coerce_status(str(item.get("status") or "").strip())
        reason = str(item.get("reason") or item.get("理由") or "").strip()
        if name in GATE_NAMES and status in GATE_STATUSES and reason:
            by_name[name] = {"name": name, "status": status, "reason": reason}
    if len(by_name) != len(GATE_NAMES):
        raise EvalError("六关不完整")
    return [by_name[name] for name in GATE_NAMES]


def _normalize_mirror(raw: object) -> list[str]:
    if isinstance(raw, str):
        parts = [part.strip() for part in re.split(r"[\n。]+", raw) if part.strip()]
        return parts
    if not isinstance(raw, list):
        raise EvalError("mirror_test 必须是五句")
    return [str(item).strip() for item in raw if str(item).strip()]


def _mirror_count(lines: list[str]) -> int:
    return len([line for line in lines if line.strip()])


def _normalize_bands(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise EvalError("price_bands 必须是对象")
    mode = str(raw.get("mode") or "").strip()
    if mode not in BAND_MODES:
        raise EvalError("价格带必须是激进/稳健/保守或观望")
    return {
        "mode": mode,
        "aggressive": str(raw.get("aggressive") or "").strip(),
        "steady": str(raw.get("steady") or "").strip(),
        "conservative": str(raw.get("conservative") or "").strip(),
        "overvalued": str(raw.get("overvalued") or "").strip(),
    }


def _normalize_memo(raw: object) -> dict[str, str]:
    if isinstance(raw, str) and raw.strip():
        raw = {"essence": raw.strip(), "risks": raw.strip(), "role": raw.strip()}
    if not isinstance(raw, dict):
        raise EvalError("memo 必须是对象")
    essence = _as_text(raw.get("essence"))
    risks = _as_text(raw.get("risks"))
    role = _coerce_role(_as_text(raw.get("role")))
    used = {name for names in _MEMO_ALIASES.values() for name in names if name in raw}
    leftovers = [_as_text(value) for key, value in raw.items() if key not in used]
    leftovers = [text for text in leftovers if text]
    if not essence and leftovers:
        essence = leftovers.pop(0)
    if not risks and leftovers:
        risks = leftovers.pop(0)
    if role not in MEMO_ROLES and leftovers:
        role = _coerce_role(leftovers.pop(0))
    if not essence or not risks or role not in MEMO_ROLES:
        raise EvalError("备忘须含生意本质、主要风险、仓位角色")
    return {"essence": essence, "risks": risks, "role": role}


def _fit_answer(value: object) -> str:
    if isinstance(value, bool):
        return "是。" if value else "否。"
    return _as_text(value)


def _normalize_fit(raw: object, book: dict[str, Any]) -> dict[str, str]:
    if isinstance(raw, str) and raw.strip():
        raw = {"should_enter": raw, "funding": raw, "overlap": raw}
    if isinstance(raw, list):
        texts = [text for text in (_fit_answer(item) for item in raw) if text]
        if len(texts) >= 3:
            raw = {"should_enter": texts[0], "funding": texts[1], "overlap": texts[2]}
    if not isinstance(raw, dict):
        raise EvalError("portfolio_fit 必须对照当前仓回答三问")
    mapped = {key: _fit_answer(_first_key(raw, names)) for key, names in _FIT_ALIASES.items()}
    used = {name for names in _FIT_ALIASES.values() for name in names if name in raw}
    leftovers = [_fit_answer(value) for key, value in raw.items() if key not in used]
    leftovers = [text for text in leftovers if text]
    for key in ("should_enter", "funding", "overlap"):
        if not mapped[key] and leftovers:
            mapped[key] = leftovers.pop(0)
    if not mapped["should_enter"] or not mapped["funding"] or not mapped["overlap"]:
        raise EvalError("portfolio_fit 三问不完整")
    return mapped


def _fit_from_book(book: dict[str, Any]) -> dict[str, str]:
    positions = book.get("positions") if isinstance(book, dict) else None
    names: list[str] = []
    if isinstance(positions, list):
        for row in positions:
            if isinstance(row, dict) and row.get("name"):
                names.append(str(row["name"]))
    cash = book.get("cash_balance") if isinstance(book, dict) else None
    held = "、".join(names) if names else "空仓"
    return {
        "should_enter": f"对照当前仓（{held}）与现金 {cash}，数据不足时不判断该不该进来。",
        "funding": f"当前现金 {cash}。数据不足时不指定买法。",
        "overlap": f"需核对该标的是否与已有持仓（{held}）叠同一逻辑。",
    }


def _write_report(payload: EvalInput, markdown: str) -> Path:
    payload.reports_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", payload.name).strip("._") or "research"
    path = payload.reports_dir / f"{payload.job_id}-{safe}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def _json_optional(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    number = Decimal(value)
    if not number.is_finite():
        return None
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _verdict_zh(value: str) -> str:
    return {"pass": "通过", "fail": "不通过", "gray": "灰色地带"}.get(value, value)


def _gate_zh(value: str) -> str:
    return {"pass": "通过", "fail": "失败", "gray": "灰色"}.get(value, value)


def _band_zh(value: str) -> str:
    return {
        "aggressive": "激进",
        "steady": "稳健",
        "conservative": "保守",
        "watch": "观望",
    }.get(value, value)


def _role_zh(value: str) -> str:
    return {"core": "核心", "satellite": "卫星", "skip": "不买"}.get(value, value)
