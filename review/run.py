from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from research.llm import LLMClient
from research.thesis import is_overvalued

ACTIONS = frozenset({"hold", "add", "cut", "sell", "unknown"})
ACTION_ALIASES = {
    "hold": "hold",
    "持有": "hold",
    "add": "add",
    "加仓": "add",
    "cut": "cut",
    "减仓": "cut",
    "sell": "sell",
    "卖出": "sell",
    "unknown": "unknown",
    "无法判断": "unknown",
}
ACTION_ZH = {
    "hold": "持有",
    "add": "加仓",
    "cut": "减仓",
    "sell": "卖出",
    "unknown": "无法判断",
}
RULE_LIGHT_ZH = {"hit": "触及", "clear": "未触及", "none": "无规则", "unknown": "无现价"}
SHORT_EXAM_MARK = "短体检，非六关"
NO_THESIS_MARK = "无论文"
STALE_THESIS_MARK = "论文过期"
CURRENT_THESIS_MARK = "现行论文"
STALE_MARK = "含过期价"
THESIS_Q = "买入论文还在吗"
PRICE_Q = "今天此价格还会买吗"
MAX_PORTFOLIO = 3
PORTFOLIO_NAME = "__PORTFOLIO__"
REBALANCE_ACTIONS = frozenset(
    {
        "rebalance",
        "rebalance_note",
        "再平衡",
        "再平衡提示",
    }
)


@dataclass(frozen=True)
class ReviewInput:
    snapshot: dict[str, Any]
    evaluations: dict[str, dict[str, Any]]
    funnel_names: tuple[str, ...]
    as_of_date: str
    review_id: int
    reports_dir: Path


@dataclass(frozen=True)
class ReviewOutput:
    structured: dict[str, Any]
    markdown: str
    report_path: str
    has_stale: bool


class ReviewError(ValueError):
    pass


def run_review(llm: LLMClient, payload: ReviewInput) -> ReviewOutput:
    rows = _snapshot_positions(payload.snapshot)
    positions: list[dict[str, Any]] = []
    has_stale = False
    for row in rows:
        stale = bool(row.get("stale"))
        missing_price = _price_unusable(row)
        if stale or missing_price:
            has_stale = True
        name = str(row.get("name") or "").strip()
        evaluation = payload.evaluations.get(name)
        short_exam = evaluation is None or bool(evaluation.get("stale"))
        if stale or missing_price:
            action = "unknown"
            reason = _forced_unknown_reason(
                row, evaluation, short_exam=short_exam, stale=stale or missing_price
            )
        else:
            try:
                raw = llm.complete(
                    _position_prompt(row, evaluation, short_exam=short_exam),
                    name=name,
                )
                parsed = _parse_model_json(raw)
                action, reason = _normalize_position(
                    parsed, row, evaluation, short_exam=short_exam
                )
            except Exception:
                action = "unknown"
                reason = _forced_unknown_reason(
                    row, evaluation, short_exam=short_exam, model_failed=True
                )
        positions.append(
            {
                "name": name,
                "action": action,
                "reason": reason,
                "actual_weight": row.get("weight"),
                "target_weight": row.get("target"),
                "rule_light": _rule_light(row),
                "short_exam": short_exam,
                "thesis_id": evaluation.get("id") if evaluation else None,
            }
        )

    held = {item["name"] for item in positions if item["name"]}
    allowed_new = {str(item).strip() for item in payload.funnel_names if str(item).strip()}
    try:
        raw_portfolio = llm.complete(
            _portfolio_prompt(payload.snapshot, allowed_new, positions),
            name=PORTFOLIO_NAME,
        )
        parsed_portfolio = _parse_model_json(raw_portfolio)
        actions = normalize_portfolio_actions(parsed_portfolio, held, allowed_new)
    except Exception:
        actions = []
    actions = _maybe_add_rebalance(actions, payload.snapshot)

    structured = {
        "positions": positions,
        "portfolio_actions": actions,
        "has_stale": has_stale,
    }
    markdown = render_markdown(
        structured,
        as_of_date=payload.as_of_date,
        snapshot=payload.snapshot,
    )
    path = _write_report(payload, markdown)
    return ReviewOutput(
        structured=structured,
        markdown=markdown,
        report_path=str(path),
        has_stale=has_stale,
    )


def normalize_portfolio_actions(
    raw: object,
    held: set[str],
    allowed_new: set[str],
) -> list[dict[str, Any]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("portfolio_actions", raw.get("actions"))
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        if action in REBALANCE_ACTIONS or "再平衡" in action:
            continue
        link = str(item.get("cash_or_target_link") or "").strip()
        names_raw = item.get("names")
        if isinstance(names_raw, str):
            names_raw = [part.strip() for part in re.split(r"[,，、]+", names_raw)]
        if not isinstance(names_raw, list):
            continue
        names: list[str] = []
        for value in names_raw:
            name = str(value).strip()
            if not name:
                continue
            if name in held or name in allowed_new:
                if name not in names:
                    names.append(name)
        if not action or not link or not names:
            continue
        out.append(
            {
                "action": action,
                "names": names,
                "cash_or_target_link": link,
            }
        )
        if len(out) >= MAX_PORTFOLIO:
            break
    return out


def render_markdown(
    structured: dict[str, Any],
    *,
    as_of_date: str,
    snapshot: dict[str, Any],
) -> str:
    positions = structured.get("positions") if isinstance(structured.get("positions"), list) else []
    actions = (
        structured.get("portfolio_actions")
        if isinstance(structured.get("portfolio_actions"), list)
        else []
    )
    has_stale = bool(structured.get("has_stale"))
    cash = snapshot.get("cash") if isinstance(snapshot.get("cash"), dict) else {}
    lines = [
        f"# 组合审视 {as_of_date}",
        "",
        f"本轮{'含过期价。' if has_stale else '现价已刷新或沿用缓存。'}",
        "",
        f"现金：{snapshot.get('cash_balance')}　占比：{cash.get('weight')}　"
        f"目标：{'无目标' if cash.get('no_target') else cash.get('target')}",
        "",
        "## 单票",
        "",
    ]
    for row in positions:
        if not isinstance(row, dict):
            continue
        action = ACTION_ZH.get(str(row.get("action")), str(row.get("action") or ""))
        light = RULE_LIGHT_ZH.get(str(row.get("rule_light")), str(row.get("rule_light") or ""))
        target = "无目标" if row.get("target_weight") is None else row.get("target_weight")
        exam = _exam_label(row)
        lines.extend(
            [
                f"### {row.get('name')} — {action}",
                "",
                exam,
                f"实盘占比：{row.get('actual_weight')}　目标：{target}　规则灯：{light}",
                str(row.get("reason") or ""),
                "",
            ]
        )
    lines.extend(["## 组合建议", ""])
    if not actions:
        lines.append("本轮无组合建议。")
    else:
        for index, item in enumerate(actions, start=1):
            if not isinstance(item, dict):
                continue
            names = item.get("names") if isinstance(item.get("names"), list) else []
            lines.append(
                f"{index}. {item.get('action')}（{'、'.join(str(n) for n in names)}）"
                f"：{item.get('cash_or_target_link')}"
            )
    lines.append("")
    return "\n".join(lines)


def _snapshot_positions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows = snapshot.get("positions") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("name") or "").strip()]


def _price_unusable(row: dict[str, Any]) -> bool:
    price = row.get("price")
    if price is None:
        return True
    try:
        number = Decimal(str(price))
    except (InvalidOperation, ValueError, TypeError):
        return True
    return (not number.is_finite()) or number <= 0


def _exam_label(row: dict[str, Any]) -> str:
    thesis_id = row.get("thesis_id")
    if not row.get("short_exam"):
        if thesis_id is not None:
            return f"引用现行论文 {thesis_id}"
        return "引用现行论文"
    if thesis_id is not None:
        return f"{SHORT_EXAM_MARK}；{STALE_THESIS_MARK}"
    return f"{SHORT_EXAM_MARK}；{NO_THESIS_MARK}"


def _ensure_thesis_phrase(
    text: str,
    evaluation: dict[str, Any] | None,
    *,
    short_exam: bool,
) -> str:
    if short_exam:
        mark = STALE_THESIS_MARK if evaluation is not None else NO_THESIS_MARK
        if mark not in text:
            return f"{text}{mark}。"
        return text
    if evaluation is None:
        if NO_THESIS_MARK not in text:
            return f"{text}{NO_THESIS_MARK}。"
        return text
    eval_id = evaluation.get("id")
    verdict = evaluation.get("verdict") or "无结论"
    cite = f"{CURRENT_THESIS_MARK} {eval_id}"
    if cite not in text:
        return f"{text}引用{cite}（{verdict}）。"
    return text


def _rule_light(row: dict[str, Any]) -> str:
    light = str(row.get("rule_light") or "none").strip()
    if light not in {"hit", "clear", "none", "unknown"}:
        return "none"
    return light


def _coerce_action(value: object) -> str:
    text = str(value or "").strip()
    return ACTION_ALIASES.get(text, "")


def _normalize_position(
    raw: object,
    row: dict[str, Any],
    evaluation: dict[str, Any] | None,
    *,
    short_exam: bool,
) -> tuple[str, str]:
    if not isinstance(raw, dict):
        raise ReviewError("单票结果不是对象")
    action = _coerce_action(raw.get("action"))
    reason = str(raw.get("reason") or "").strip()
    if action not in ACTIONS:
        action = "unknown"
    if not reason:
        reason = "模型未给出理由。"
    unknown = action == "unknown"
    reason = _ensure_required_phrases(
        reason,
        row,
        evaluation,
        short_exam=short_exam,
        unknown=unknown,
        stale=False,
    )
    structured = evaluation.get("structured") if isinstance(evaluation, dict) else None
    usable = not _price_unusable(row) and not bool(row.get("stale"))
    if action in {"hold", "add"} and is_overvalued(
        structured, current_price=row.get("price"), price_usable=usable
    ):
        action = "cut"
        if "高估" not in reason:
            reason = f"现价高估，结论改为不通过。{reason}"
    return action, reason


def _forced_unknown_reason(
    row: dict[str, Any],
    evaluation: dict[str, Any] | None,
    *,
    short_exam: bool,
    stale: bool = False,
    model_failed: bool = False,
) -> str:
    parts: list[str] = []
    if stale:
        parts.append("缺现价或现价过期，无法判断。本轮含过期价。")
    if model_failed:
        parts.append("该票模型失败，无法判断。")
    reason = "".join(parts) or "无法判断。"
    return _ensure_required_phrases(
        reason,
        row,
        evaluation,
        short_exam=short_exam,
        unknown=True,
        stale=stale,
    )


def _ensure_required_phrases(
    reason: str,
    row: dict[str, Any],
    evaluation: dict[str, Any] | None,
    *,
    short_exam: bool,
    unknown: bool,
    stale: bool,
) -> str:
    text = reason.strip()
    if short_exam and SHORT_EXAM_MARK not in text:
        text = f"{SHORT_EXAM_MARK}。{text}"
    if stale and STALE_MARK not in text:
        text = f"{text}{STALE_MARK}。"
    if row.get("no_target") and "无目标" not in text:
        text = f"{text}无目标。"
    light = _rule_light(row)
    light_zh = RULE_LIGHT_ZH[light]
    if "规则灯" not in text:
        text = f"{text}规则灯：{light_zh}。"
    if THESIS_Q not in text:
        suffix = "无法判断。" if unknown else "见上。"
        text = f"{text}{THESIS_Q}：{suffix}"
    if PRICE_Q not in text:
        suffix = "无法判断。" if unknown else "见上。"
        text = f"{text}{PRICE_Q}：{suffix}"
    text = _ensure_thesis_phrase(text, evaluation, short_exam=short_exam)
    return text


def _position_prompt(
    row: dict[str, Any],
    evaluation: dict[str, Any] | None,
    *,
    short_exam: bool,
) -> str:
    facts = json.dumps(
        {
            "name": row.get("name"),
            "shares": row.get("shares"),
            "cost": row.get("cost"),
            "price": row.get("price"),
            "stale": row.get("stale"),
            "weight": row.get("weight"),
            "target": row.get("target"),
            "no_target": row.get("no_target"),
            "rule_light": _rule_light(row),
            "deviation": row.get("deviation"),
        },
        ensure_ascii=False,
    )
    body = _thesis_prompt_body(evaluation)
    if short_exam:
        if evaluation is not None:
            exam = (
                f"现行论文已过期。必须做短体检，文案含「{SHORT_EXAM_MARK}」和「{STALE_THESIS_MARK}」。"
                f"过期论文快照：{body}。"
            )
        else:
            exam = f"无现行论文。必须做短体检，文案含「{SHORT_EXAM_MARK}」和「{NO_THESIS_MARK}」。"
    else:
        exam = (
            f"必须引用现行论文 {evaluation.get('id')}，"
            f"结论 {evaluation.get('verdict')}。"
            f"备忘：{json.dumps(evaluation.get('memo'), ensure_ascii=False)}。"
            f"论文正文：{body}。"
        )
    target_note = "无目标，不参与偏差告警。" if row.get("no_target") else "对照目标仓位。"
    return (
        f"请对持仓 {row.get('name')} 给出纸面单票动作。"
        f"只输出 JSON，键只有 action 与 reason。"
        f"action 只能是 hold/add/cut/sell/unknown。"
        f"必须回答「{THESIS_Q}」与「{PRICE_Q}」。"
        f"对照规则灯与目标。{target_note}"
        f"{exam}"
        f"不得两面讨好而不给动作。禁止编造行情或财务数字。"
        f"持仓事实：{facts}。"
    )


def _thesis_prompt_body(evaluation: dict[str, Any] | None) -> str:
    if evaluation is None:
        return ""
    structured = evaluation.get("structured")
    if not isinstance(structured, dict):
        structured = evaluation
    return json.dumps(
        {
            "mirror_test": structured.get("mirror_test"),
            "price_bands": structured.get("price_bands"),
            "as_of": structured.get("as_of"),
        },
        ensure_ascii=False,
    )


def _portfolio_prompt(
    snapshot: dict[str, Any],
    allowed_new: set[str],
    tickets: list[dict[str, Any]],
) -> str:
    held = [row.get("name") for row in _snapshot_positions(snapshot)]
    cash = snapshot.get("cash") if isinstance(snapshot.get("cash"), dict) else {}
    rebalance = snapshot.get("rebalance") if isinstance(snapshot.get("rebalance"), list) else []
    candidates = "、".join(sorted(allowed_new)) if allowed_new else "无"
    ticket_lines = [
        {
            "name": row.get("name"),
            "action": row.get("action"),
            "weight": row.get("actual_weight"),
            "target": row.get("target_weight"),
            "short_exam": row.get("short_exam"),
        }
        for row in tickets
    ]
    return (
        "请给出最多 3 条组合建议，必须落到本轮持仓、现金与目标仓位。"
        "只输出 JSON，键为 portfolio_actions，每项含 action、names、cash_or_target_link。"
        f"当前持仓：{held}。现金：{snapshot.get('cash_balance')}，"
        f"现金占比 {cash.get('weight')}，现金目标 {cash.get('target')}。"
        f"本轮单票动作：{json.dumps(ticket_lines, ensure_ascii=False)}。"
        f"再平衡提示名单：{rebalance}。"
        f"新票候选（最近漏斗 pass/gray）：{candidates}。"
        "涉及新票时只能用上述候选；不通过或名单外名称不得当作用现金买入候选。"
        "禁止空泛评论、收益排行或今日推荐。建议停在纸面，不下单、不改目标。"
    )


def _maybe_add_rebalance(
    actions: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = snapshot.get("rebalance") if isinstance(snapshot, dict) else None
    if not isinstance(raw, list) or not raw:
        return actions
    dumped = json.dumps(actions, ensure_ascii=False)
    if "再平衡" in dumped:
        return actions
    if len(actions) >= MAX_PORTFOLIO:
        return actions
    names: list[str] = []
    for item in raw:
        name = str(item).strip()
        if not name:
            continue
        names.append("现金" if name == "__CASH__" else name)
    if not names:
        return actions
    added = list(actions)
    added.append(
        {
            "action": "再平衡提示",
            "names": names,
            "cash_or_target_link": (
                f"实盘与目标绝对偏差大于 3 个百分点（{'、'.join(names)}），仅提示，不下单。"
            ),
        }
    )
    return added


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
        raise ReviewError("模型输出不是 JSON")


def _write_report(payload: ReviewInput, markdown: str) -> Path:
    payload.reports_dir.mkdir(parents=True, exist_ok=True)
    path = payload.reports_dir / f"{payload.review_id}-portfolio-review.md"
    path.write_text(markdown, encoding="utf-8")
    return path
