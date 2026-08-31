---
title: '设定目标仓位'
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

**Problem:** 看仓已有实盘占比，但没有「该在的位置」；迁入后也不会留下一套可改的初始目标，对照无从谈起。

**Approach:** 启动在迁入后同步刷新现价（允许 stale），若 `position_targets` 为空则由 `ledger` 按当时实盘占比（含现金 `__CASH__`）写入初始目标。用户可整表保存目标；合计须为 1。无目标行仍出现并标「无目标」。偏差超过 3 个百分点给出再平衡提示，不下单。

## Boundaries & Constraints

**Always:**
- 只 `ledger` 写 `position_targets`。`migrate` 不写该表。改目标不改成交与现金。
- 现金保留名 `__CASH__`。保存时含现金合计 `|sum-1|≤1e-6`，否则 HTTP 400 信封，账不变。各目标（含现金）落在 `[0, 1]`。
- 启动编排由 `app`：需要则迁入 → 同步刷新现价（不占 `job_lock`，Fake 可注入）→ 仅当目标表为空时按当时占比（有价市值+现金为分母，允许 stale）写入。已有目标不覆盖。新持仓未写入目标则「无目标」。
- 无现价的名称不写初始目标（标「无目标」）。现金为负导致现金权重不在 `[0,1]` 时不写初始目标，不编造。
- 有目标且实盘占比可得、绝对偏差 `> 0.03` 时给出再平衡提示；无目标与无占比的不计入告警。提示不下单。
- 沿用现有持仓投影扩展目标/无目标/偏差，禁止第二套市值口径。测试不打真行情、不调 LLM。

**Ask First:**
- 用第二套表或文件存目标；启动刷新走后台锁；保存时改成交来「平衡」仓位。

**Never:**
- `migrate` 写 `position_targets`；改 `record_trade` 公式。
- 本故事实现规则灯、审视、漏斗、下单。
- 读 Desk。把再平衡提示做成下单或改账按钮。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 启动初始目标 | 已迁入、Fake 有价、目标表空 | 持仓权重+`__CASH__` 写入；合计 1±1e-6；`migrate` 未写该表 | 刷新失败仍可用 stale 占比 |
| 已有目标 | 目标表非空再启动 | 不覆盖用户目标 | 仍可刷新现价 |
| 合计不为 1 | POST 目标 `|sum-1|>1e-6` 或越出 `[0,1]` | HTTP 400；成交笔数与现金不变 | 信封 `error`/`code`/`detail` |
| 合计为 1 | 合法 map 含 `__CASH__` | 2xx；持仓表立刻可见新目标 | 不改成交 |
| 无目标行 | 某持仓无 target 行 | 行仍在，标「无目标」；偏差告警不含它 | 无占比同：不算 3pp |
| 偏差 > 3pp | 有目标且 `|weight-target|>0.03` | 看仓可见再平衡提示，无下单 | 等于 0.03 不提示 |

</frozen-after-approval>

## Code Map

沿用：`QuotedPosition` / `project_positions`（`ledger/quotes.py:29-118`，分母=有价市值+现金）、`_positions_payload`（`app/main.py:57-82`）、lifespan 仅迁入（`app/main.py:111-132`）、`PositionsView.vue` 表与 `applyBook`。无 `PositionTarget`、无 `__CASH__`、无启动刷新。1.4 过期价与 Fake 客户端保持。

- `app/models.py` -- `PositionTarget`（user_id+name，现金 `__CASH__`，target_weight）
- `ledger/targets.py` -- 空表则按投影权重种子；整表保存校验合计与区间；不 import HTTP
- `ledger/quotes.py` -- 投影附加 target / 无目标 / 偏差；现金权重与目标；市值口径不变
- `app/main.py` -- lifespan：迁入后同步 refresh 再 `seed`；`POST /api/targets`；扩展 GET/记账/刷新响应
- `web/src/views/PositionsView.vue` -- 目标、偏差、「无目标」、再平衡文案、保存表单；无下单
- `tests/test_targets.py` -- 种子合计、不覆盖、400 账不变、无目标、3pp、容差 1e-6；Fake；回归 quotes/trades/migrate/health

只读勿改：Desk、`record_trade`、migrate 四件套与「不写 position_targets」、`__main__.py` 绑定、health `lock`、`market` HTTP 内部。

## Tasks & Acceptance

**Execution:**
- [x] `app/models.py` -- `position_targets` 表 -- 目标可落库
- [x] `ledger/targets.py` -- 种子与保存 -- migrate 不写；合计/区间拒绝
- [x] `ledger/quotes.py` `app/main.py` -- 投影+启动刷新+API -- 空表才种子，允许 stale
- [x] `web/src/views/PositionsView.vue` -- 目标列与保存 -- 无目标可识别，提示不下单
- [x] `tests/test_targets.py` -- 矩阵 -- 不打真行情/真模型

**Acceptance Criteria:**
- Given 再平衡提示出现，when 查看看仓，then 没有下单或改账按钮
- Given 保存目标成功，when 查成交与现金，then 与保存前一致
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Spec Change Log

## Design Notes

- 种子条件：`position_targets` 为空，不是「每次启动重算」。无价名称不进种子。
- 启动刷新与 1.4 同一 `quote_client`；失败走既有 stale，不 500 空仓。
- 本故事扩展现有投影即可；`BookSnapshot` 命名与规则灯留给 1.6，禁止平行市值。
- `POST /api/targets` 整表替换。未列入的持仓变为无目标。

## Verification

**Commands:**
- `pytest tests/test_targets.py tests/test_quotes.py tests/test_trades.py tests/test_migrate.py tests/test_health.py` -- expected: 通过；含种子合计、400 不改账、无目标、3pp
- 检查 `migrate/` 无 `position_targets` 写入；`ledger/targets.py` 无 httpx -- expected: 无

**Manual checks (if no CLI):**
- 持仓页能看到目标/无目标/偏差；合计不对无法保存；再平衡只有提示

## Suggested Review Order

**启动编排**

- 迁入后同步刷价，空表才按实盘占比种子
  [`main.py:163`](../../app/main.py#L163)

- 无价名称不进种子；现金权重须在 [0,1]
  [`targets.py:24`](../../ledger/targets.py#L24)

**落库与校验**

- `user_id`+`name` 存目标，现金名 `__CASH__`
  [`models.py:72`](../../app/models.py#L72)

- 整表替换；合计 1±1e-6，权重 [0,1]，不改成交
  [`targets.py:68`](../../ledger/targets.py#L68)

**投影**

- 同一市值分母上挂目标、无目标、偏差与现金权重
  [`quotes.py:31`](../../ledger/quotes.py#L31)

- 有目标且 `|Δ|>0.03` 才进再平衡名单
  [`quotes.py:171`](../../ledger/quotes.py#L171)

- 持仓/记账/刷新共用扩展后的 payload
  [`main.py:60`](../../app/main.py#L60)

- `POST /api/targets` 拒绝则账不变
  [`main.py:447`](../../app/main.py#L447)

**看仓**

- 再平衡只提示、不下单；表内目标只读
  [`PositionsView.vue:363`](../../web/src/views/PositionsView.vue#L363)

- 无目标行可识别；现金同样展示目标
  [`PositionsView.vue:406`](../../web/src/views/PositionsView.vue#L406)

**测试**

- 种子合计、不覆盖、400 不改账、无目标、3pp、容差
  [`test_targets.py:88`](../../tests/test_targets.py#L88)
