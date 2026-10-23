from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from research.llm import LLMClient
from research.thesis import dynamic_sizing, is_overvalued
from ledger.exposure import exposure_report, proposal_priority, read_exposure_context
from discovery.constants import CHINA_MARKET_TIMEZONE

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
    exposure_context: dict[str, Any] | None = None


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
    positive_deltas: dict[str, Decimal] = {}
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
        priority = _priority(evaluation)
        restriction = None
        proposed_weight = None
        if priority == 'exit_review':
            action, restriction = 'sell', 'exit_review'
            reason = '论文不通过或角色为不买，优先退出审视。' + reason
        elif action == 'add':
            if priority != 'eligible' or short_exam:
                action, restriction = 'hold', 'freeze_add'
                reason = '信息等级、经营状态或论文时效不支持新增。' + reason
            elif payload.exposure_context is None:
                action, restriction = 'hold', 'exposure_unavailable'
                reason = '未冻结共同风险输入，新增建议不可验证。' + reason
            else:
                current, _labels, _policy = read_exposure_context(payload.exposure_context)
                code = row.get('code') or f'name:{name}'
                sizing = (evaluation or {}).get('dynamic_sizing') or dynamic_sizing(
                    (evaluation or {}).get('structured'), current_price=row.get('price'),
                    price_usable=True, actual_weight=row.get('exposure_weight')) or {}
                target = _as_decimal(row.get('exposure_target'))
                cap = _as_decimal(sizing.get('suggested_max'))
                target = min(target, cap) if target is not None and cap is not None else cap
                if (sizing.get('action') not in {'add_to_cap', 'normal_config'} or target is None
                        or code not in current or target <= current[code]):
                    action, restriction = 'hold', 'no_valid_addition_weight'
                    reason = '价格带或角色上限不支持可复算的新增权重。' + reason
                else:
                    proposed_weight = str(target)
                    positive_deltas[code] = max(positive_deltas.get(code, Decimal('0')), target - current[code])
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
                'priority': priority, 'restriction': restriction, 'proposed_weight': proposed_weight,
            }
        )

    common_exposure = exposure_report(payload.exposure_context, positive_deltas) if payload.exposure_context else None
    if common_exposure and not common_exposure['allowed']:
        for position in positions:
            if position['action'] == 'add':
                position.update(action='hold', restriction='shared_exposure_limit', proposed_weight=None)
                position['reason'] = '建议新增后的共同风险超限或权重缺失，停止新增。' + position['reason']
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
    for item in actions:
        item['validation'] = 'unvalidated_note'
        item['cash_or_target_link'] = '未量化研究备注，不构成新增许可。' + item['cash_or_target_link']

    structured = {
        "positions": positions,
        "portfolio_actions": actions,
        "has_stale": has_stale,
        'common_exposure': common_exposure,
        'opportunity_cost': _opportunity_cost(payload),
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


def _as_decimal(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _priority(evaluation):
    data = (evaluation or {}).get('structured')
    data = data if isinstance(data, dict) else {}
    memo = data.get('memo') if isinstance(data.get('memo'), dict) else {}
    operating = data.get('operating_result') if isinstance(data.get('operating_result'), dict) else {}
    state = operating.get('state', 'unknown')
    if (evaluation or {}).get('financial_drift'):
        state = 'deteriorating'
    return proposal_priority('fail' if data.get('verdict') == 'fail' else 'skip' if memo.get('role') == 'skip' else
        (evaluation or {}).get('verdict') or data.get('verdict'), state, data.get('data_grade', 'C'))


def _opportunity_cost(payload):
    comparison = (payload.exposure_context or {}).get('comparison') or {'status': 'missing', 'reason': 'no_same_day_comparison'}
    rows = []
    if comparison['status'] == 'available':
        for row in _snapshot_positions(payload.snapshot):
            data = (payload.evaluations.get(row['name']) or {}).get('structured') or {}
            rows.append({'name': row['name'], **_comparison_price(row, payload),
                         'valuation': data.get('valuation_scenarios'), 'data_grade': data.get('data_grade', 'C'),
                         'fees': None,
                         'common_risks': (payload.exposure_context or {}).get('labels', {}).get(row.get('code')) or ['unknown']})
    return {**comparison, 'holdings': rows, 'return_comparison': 'assumptions_only_no_reproducible_return'}


def _aware_observation(value):
    try:
        observed = datetime.fromisoformat(value)
        if observed.utcoffset() is not None:
            return observed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    return None


def _comparison_price(row, payload):
    observed = _aware_observation(row.get('exposure_price_observed_at'))
    cutoff = _aware_observation((payload.exposure_context or {}).get('cutoff_at'))
    day = observed.astimezone(CHINA_MARKET_TIMEZONE).date().isoformat() if observed else None
    number = _as_decimal(row.get('exposure_price'))
    stale = bool(row.get('stale'))
    reason = None
    if stale:
        reason = 'stale_quote'
    elif number is None or number <= 0:
        reason = 'unusable_quote'
    elif observed is None:
        reason = 'quote_date_missing'
    elif cutoff is None:
        reason = 'quote_cutoff_missing'
    elif observed > cutoff:
        reason = 'quote_time_after_cutoff'
    elif day != payload.as_of_date:
        reason = 'quote_date_mismatch'
    return {'comparison_date': payload.as_of_date, 'as_of_date': day,
            'price_observed_at': observed.isoformat() if observed else None,
            'price_source': row.get('exposure_price_source'), 'price_stale': stale,
            'price_status': 'unavailable' if reason else 'available', 'price_reason': reason,
            'current_price': None if reason else str(number)}


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
    exposure = structured.get('common_exposure')
    if exposure:
        lines.extend(['## 共同经济风险', '', '每个标签计入完整持仓权重，多标签合计可超过100%。',
                      '已配置的上限约束新增建议。' if exposure['configured'] else '未设置共同风险上限；仅展示分布，不判断超限。',
                      f"冻结版本：{exposure['version_id']}；输入指纹：{exposure['fingerprint']}"])
        if not exposure['complete']:
            lines.append('部分持仓缺少可用权重，以下仅为已知部分，暂停新增建议。')
        for key, value in exposure['current_totals'].items():
            lines.append(f"- {exposure['label_names'][key]}：{Decimal(value) * 100}%")
        if exposure['breaches']:
            lines.append('建议新增后的超限项：' + '、'.join(exposure['label_names'][key] for key in exposure['breaches']))
        lines.append('')
    comparison = structured.get('opportunity_cost') or {}
    lines.extend(['## 同日期宽基ETF机会成本', ''])
    if comparison.get('status') != 'available':
        lines.append(f"对照不可用：{comparison.get('reason') or 'missing'}。缺少可核对的同日期证据。")
    else:
        data = comparison['data']
        lines.extend([f"日期：{data['as_of_date']}；ETF：{data['code']}；宽基依据：{data['broad_market_basis']}",
                      f"费用率：{data['fee_rate']}；估值口径：{data['valuation_basis']}；倍数：{data['valuation_multiple']}",
                      f"声明盈利假设：{data['earnings_assumption']}；研究置信度：{data['confidence']}",
                      f"共同风险：{'、'.join(data['common_risks'])}；证据：{'、'.join(data['evidence_ids'])}"])
        for item in comparison['holdings']:
            price = (f"同日可用报价 {item['current_price']}" if item['price_status'] == 'available'
                     else f"持仓报价不可用（{item['price_reason']}）")
            lines.append(f"- {item['name']}：{price}；报价记录日期 {item['as_of_date'] or '未知'}（记录时间，不代表交易所事件时间）；"
                         f"费用假设未提供；信息等级 {item['data_grade']}；共同风险 {item['common_risks']}；"
                         f"冻结估值事实与声明假设 {json.dumps(item['valuation'], ensure_ascii=False) if item['valuation'] else '缺少可复算估值'}")
    lines.extend(['仅作事实与声明假设对照；没有可复算的收益模型，不显示预期超额收益数字。', '', '## 组合建议', ''])
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
    sizing = (evaluation or {}).get("dynamic_sizing") if isinstance(evaluation, dict) else None
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
            "dynamic_sizing": sizing,
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
    if row.get("no_target"):
        sizing_label = str(sizing.get("label") or "").strip() if isinstance(sizing, dict) else ""
        target_note = (
            f"无目标，不参与偏差告警。动态仓位评估：{sizing_label}。"
            if sizing_label
            else "无目标，不参与偏差告警。"
        )
    else:
        target_note = "对照目标仓位。"
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
    cash_target_desc = (
        "无目标（机动资金，不设保留底线）"
        if cash.get("no_target") or cash.get("target") is None
        else str(cash.get("target"))
    )
    return (
        "请给出最多 3 条组合建议，必须落到本轮持仓、现金与目标仓位。"
        "只输出 JSON，键为 portfolio_actions，每项含 action、names、cash_or_target_link。"
        f"当前持仓：{held}。现金：{snapshot.get('cash_balance')}，"
        f"现金占比 {cash.get('weight')}，现金目标 {cash_target_desc}。"
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
