---
title: '规则灯'
type: 'feature'
created: '2026-08-28'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 规则已随 YAML 入库，但看仓看不到触及与否，纪律要另翻文件；后续审视也没有规则灯可对照。

**Approach:** 列出并允许保存规则（不改成交）。持仓行用规则灯：v1 仅「加仓上限价」。有现价（含 stale）时，现价 ≥ 上限为触及，否则未触及；无该类型规则或无现价为无规则。灯进入与看仓同一套投影（`BookSnapshot`），本故事不审视。

## Boundaries & Constraints

**Always:**
- 规则字段：名称、类型、价格、可选说明。只 `ledger` 写日常规则；`migrate` 已写入的保持。规则不改成交/现金/目标。
- v1 灯只认类型「加仓上限价」（库中与 YAML 同文案）。其它类型仍可列出，不参与灯、不编造。
- 持久化 `hit` / `clear` / `none`；UI 触及 / 未触及 / 无规则。有现价且该名称有加仓上限价：`price >= 规则价` → `hit`，`<` → `clear`。无该类型或 `price` 空 → `none`。过期价仍用于灯，`stale` 另标。
- 同一名称多条加仓上限：任一条触及即为 `hit`；否则 `clear`。
- 扩展现有投影，增加 `rule_light`，并给出只读 `BookSnapshot`（含持仓灯）。禁止第二套市值口径。测试 Fake，不打真行情、不调 LLM。

**Ask First:**
- 新增第二种会改变灯语义的规则类型；用灯去改成交或下单。

**Never:**
- 改 `record_trade`、目标合计、迁入四件套语义。
- 本故事实现审视、漏斗、密钥。
- 读 Desk。编造未入库的规则。把灯做成下单按钮。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 迁入后查看 | 夹具规则 + Fake 现价（招行 40≥32，茅台 1500≥1200） | `GET /api/rules` 可见两条；两行灯 `hit` | 规则不改成交笔数 |
| 未触及 | 现价低于该名称加仓上限 | 该行 `clear` / 未触及 | 无 |
| 无规则或无价 | 无加仓上限，或无现价 | `none` / 无规则；不编造 | 不 500 |
| 保存规则 | POST 名称+类型+价格+可选说明 | 2xx 列表含新规则；成交/现金不变 | 缺字段/非正价/未知类型 400 |
| 其它类型 | 已保存非加仓上限 | 列表可见；该行灯仍按有无加仓上限计算 | 不参与灯 |

</frozen-after-approval>

## Code Map

沿用：`Rule`（`app/models.py:41-49`，列 `type`→`rule_type`）、迁入 `_parse_rules`（`migrate/yaml_portfolio.py:173`，夹具 `tests/fixtures/portfolio/rules.yaml` 类型「加仓上限价」）、`QuotedPosition` / `project_positions` / `_positions_payload`（尚无 `rule_light`）、`PositionsView.vue` 表。无规则 API、无灯、无 `BookSnapshot` 名。1.5 目标列保持。

- `ledger/rules.py` -- 列表、保存、按名称算 `hit`/`clear`/`none`；不 import HTTP
- `ledger/quotes.py` -- `QuotedPosition.rule_light`；只读 `BookSnapshot` 组装持仓+现金+灯；市值口径不变
- `app/main.py` -- `GET/POST /api/rules`；positions payload 带 `rule_light`
- `web/src/views/PositionsView.vue` -- 规则列表与保存表单；持仓列规则灯中文三态；无下单
- `tests/test_rules.py` -- 夹具灯、clear、none、保存不改账、未知类型不点灯；Fake；回归 targets/quotes/trades/migrate/health

只读勿改：Desk、`record_trade`、目标校验、migrate 规则解析键、`__main__.py` 绑定、health `lock`、`market` HTTP 内部。

## Tasks & Acceptance

**Execution:**
- [x] `ledger/rules.py` -- 读写规则与灯判定 -- 不改成交；只认加仓上限价
- [x] `ledger/quotes.py` `app/main.py` -- 投影 `rule_light` + `BookSnapshot` + API -- 与看仓同一口径
- [x] `web/src/views/PositionsView.vue` -- 列表、保存、灯列 -- 中文三态，非 JSON 主交互
- [x] `tests/test_rules.py` -- 矩阵 -- 不打真行情/真模型

**Acceptance Criteria:**
- Given 持仓行无加仓上限价，when 查看，then 显示无规则且未出现假规则
- Given 保存一条规则，when 查成交与现金，then 与保存前一致
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Spec Change Log

## Design Notes

- 灯随投影即时计算，不另落 `rule_light` 列。
- `BookSnapshot` 给 Epic 3 用；本故事只保证形状含 `rule_light`，不跑审视。
- POST 类型只接受「加仓上限价」；列表仍展示迁入的原文类型。
- 相等算触及（`>=`）。

## Verification

**Commands:**
- `pytest tests/test_rules.py tests/test_targets.py tests/test_quotes.py tests/test_trades.py tests/test_migrate.py tests/test_health.py` -- expected: 通过；含 hit/clear/none、保存不改账
- 检查 `ledger/rules.py` 无 httpx；`migrate` 规则解析未被改写成第二种口径 -- expected: 无

**Manual checks (if no CLI):**
- 持仓页能看到规则列表与灯；保存规则后账不变；灯不是下单按钮

## Suggested Review Order

**灯判定**

- 只认「加仓上限价」；现价 ≥ 上限为 hit，无价为 none
  [`rules.py:99`](../../ledger/rules.py#L99)

- 保存只追加，不改成交；POST 类型须为加仓上限价
  [`rules.py:61`](../../ledger/rules.py#L61)

**投影**

- 同一市值口径挂上 `rule_light`，供看仓与后续审视
  [`quotes.py:152`](../../ledger/quotes.py#L152)

- 只读 `BookSnapshot` 包装持仓与现金，不另算市值
  [`quotes.py:58`](../../ledger/quotes.py#L58)

**API**

- 列出迁入与已保存规则
  [`main.py:512`](../../app/main.py#L512)

- 保存后带回规则列表与持仓灯
  [`main.py:522`](../../app/main.py#L522)

**看仓**

- 持久化 hit/clear/none 显示为触及/未触及/无规则
  [`PositionsView.vue:148`](../../web/src/views/PositionsView.vue#L148)

- 表内规则灯只读，不是下单
  [`PositionsView.vue:507`](../../web/src/views/PositionsView.vue#L507)

**测试**

- 夹具 hit、低于上限 clear、无规则 none、保存改灯不改账
  [`test_rules.py:106`](../../tests/test_rules.py#L106)
