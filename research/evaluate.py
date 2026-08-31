from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from research.llm import LLMClient

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


def data_insufficient(payload: EvalInput) -> bool:
    price_ok = (
        payload.price is not None
        and payload.price.is_finite()
        and payload.price > 0
    )
    return (not has_min_financials(payload.financials)) or (not price_ok)


def run_checklist(llm: LLMClient, payload: EvalInput) -> EvalOutput:
    insufficient = data_insufficient(payload)
    if insufficient:
        structured = _gray_from_facts(payload)
    else:
        raw_text = llm.complete(_build_prompt(payload), name=payload.name)
        parsed = _parse_model_json(raw_text)
        structured = normalize_structured(parsed, payload)
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
        grade = "B"
    if not payload.web_search and grade == "A":
        grade = "B"
    fit = _normalize_fit(raw.get("portfolio_fit"), payload.book)
    if _mirror_count(mirror) < MIRROR_MIN and verdict == "pass":
        verdict = "gray"
    if verdict == "pass" and any(gate.get("status") == "fail" for gate in gates):
        verdict = "gray"
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
    search_note = (
        "未开启网页搜索。评估基于已抓取财务，并非缺少财务。"
        if not web_search
        else "已开启网页搜索。"
    )
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
        search_note,
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
        },
        "memo": {
            "essence": "财务或行情不足，不编造生意数字。",
            "risks": "缺少可核验财务或现价。",
            "role": "skip",
        },
        "data_grade": "C",
        "as_of": {"date": payload.as_of_date, "price": _json_optional(payload.price)},
        "portfolio_fit": _fit_from_book(payload.book),
    }


def _build_prompt(payload: EvalInput) -> str:
    book = json.dumps(payload.book, ensure_ascii=False)
    financials = json.dumps(list(payload.financials), ensure_ascii=False)
    search = "已开启" if payload.web_search else "未开启（这不代表没有财务）"
    price = _json_optional(payload.price)
    return (
        f"请对 {payload.name}（{payload.code}）做六关 Checklist 与决策备忘录。"
        f"只输出 JSON，键必须是英文: {', '.join(REQUIRED_KEYS)}。"
        f"verdict 只能是 pass/fail/gray。"
        f"gates 必须是数组（不能是对象），六项且每项含 name/status/reason，"
        f"name 覆盖：{'、'.join(GATE_NAMES)}，status 为 pass/fail/gray。"
        f"mirror_test 必须是五句。"
        f"price_bands.mode 为 aggressive/steady/conservative/watch。"
        f"memo 为对象，含 essence、risks、role；role 只能是 core/satellite/skip。"
        f"portfolio_fit 必须对照当前仓回答 should_enter、funding、overlap。"
        f"研究日期 {payload.as_of_date}，现价 {price}。"
        f"网页搜索：{search}。已抓取财务：{financials}。当前仓：{book}。"
        f"禁止编造财务报表数字。财务或行情不足时 verdict 必须是 gray。"
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
    "should_enter": ("should_enter", "该不该进当前仓"),
    "funding": ("funding", "用什么钱买"),
    "overlap": ("overlap", "是否叠逻辑"),
}
_BAND_ALIASES = {
    "mode": ("mode", "策略"),
    "aggressive": ("aggressive", "激进"),
    "steady": ("steady", "稳健"),
    "conservative": ("conservative", "保守"),
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
        "gray": "gray",
        "灰色": "gray",
        "灰色地带": "gray",
    }
    return mapping.get(text, mapping.get(value.strip(), value.strip()))


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
    return value.strip()


def _coerce_band(value: str) -> str:
    text = value.strip().lower()
    mapping = {
        "aggressive": "aggressive",
        "激进": "aggressive",
        "steady": "steady",
        "稳健": "steady",
        "conservative": "conservative",
        "保守": "conservative",
        "watch": "watch",
        "观望": "watch",
    }
    return mapping.get(text, mapping.get(value.strip(), value.strip()))


def _parse_model_json(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise EvalError("模型输出不是 JSON") from exc


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
        name = str(item.get("name") or "").strip()
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
    }


def _normalize_memo(raw: object) -> dict[str, str]:
    if isinstance(raw, str) and raw.strip():
        raw = {"essence": raw.strip(), "risks": raw.strip(), "role": raw.strip()}
    if not isinstance(raw, dict):
        raise EvalError("memo 必须是对象")
    essence = _as_text(raw.get("essence"))
    risks = _as_text(raw.get("risks"))
    role = _coerce_role(_as_text(raw.get("role")))
    if not essence or not risks or role not in MEMO_ROLES:
        raise EvalError("备忘须含生意本质、主要风险、仓位角色")
    return {"essence": essence, "risks": risks, "role": role}


def _normalize_fit(raw: object, book: dict[str, Any]) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise EvalError("portfolio_fit 必须对照当前仓回答三问")
    should = str(raw.get("should_enter") or "").strip()
    funding = str(raw.get("funding") or "").strip()
    overlap = str(raw.get("overlap") or "").strip()
    if not should or not funding or not overlap:
        raise EvalError("portfolio_fit 三问不完整")
    return {"should_enter": should, "funding": funding, "overlap": overlap}


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
