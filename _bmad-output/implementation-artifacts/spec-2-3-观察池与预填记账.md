---
title: '观察池与预填记账'
type: 'feature'
created: '2026-08-29'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-2-2-终选-checklist-与决策备忘录.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 评估停在纸面上，看中了没有观察池；若从评估页直接改仓，未手记就会动持仓。

**Approach:** 通过或灰色地带经评估入口加入观察池，工作台列待办。评估或观察池只预填看仓记账表单，提交仍走已有手记。本故事不做审视、四大师。

## Boundaries & Constraints

**Always:**
- 入池须绑定已完成评估，`verdict` 为 `pass` 或 `gray`。`fail`、无结论、未完成 → 拒绝。同一用户 `name` 唯一。加入、删除、预填都不产生成交。
- 观察池由 `app` 读写，不经 `ledger` 写 API，也不是评估完成的副作用。`research` 不 import `ledger`。
- 评估页不得直接改持仓或目标。预填只带名称等到看仓表单；点「记账」才 `POST /api/trades`。无一键下单或一键改目标。
- 工作台可见锁状态、最近任务、观察池待办。错误信封 `error`/`code`/`detail`。中文 UI。

**Ask First:**
- 评估完成后自动入池；不绑评估 id 的手工加池。

**Never:**
- 审视、四大师、读 Desk。评估页写成交/现金/目标。`screen/` 引入 HTTP。改手记卖超或现金公式。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 通过入池 | 已完成评估 `pass`；POST 带该评估 id | 一行观察池；成交数不变；工作台可见名称 | 无 |
| 灰色入池 | 已完成评估 `gray` | 同上 | 无 |
| 不通过 | `fail` 或经该入口 POST | 不写入；持仓不变 | 400 信封 |
| 重名 | 同名已在池中 | 仍一行；不成交 | 409 |
| 预填不记账 | 跳到看仓并带名称 | 表单有名称；提交前成交数不变 | 无 |
| 预填后手记 | 预填后再提交 | 走现有 `POST /api/trades` | 卖超仍 400 |

</frozen-after-approval>

## Code Map

沿用 `_artifact_payload`（`app/main.py:277`）、`POST /api/trades`（`:414`）→ `record_trade`（`ledger/trades.py:33`）、`LOCAL_USER_ID`（`ledger/positions.py:11`）、`_error_json`（`:56`）、health 锁（`:397`）、`GET /api/jobs`（`:992`）。无 `watchlist` 表。看仓表单 `PositionsView.vue:72` / 提交 `:207`，无 query 预填。`HomeView.vue` 仅双入口。`ResearchView.vue` 只读。`App.vue:20` 免责声明。

- `app/models.py` `app/main.py` -- `watchlist`（`user_id`+`name` 唯一）；`GET/POST /api/watchlist`、`DELETE /api/watchlist/{id}`；入池校验评估产物；不调 `record_trade`
- `web/src/views/ResearchView.vue` -- `pass`/`gray` 可入池；预填链到 `/positions`；无直接改仓
- `web/src/views/PositionsView.vue` -- 读 query 预填；提交仍 `POST /api/trades`
- `web/src/views/HomeView.vue` -- 锁、最近任务、观察池待办
- `tests/test_watchlist.py` -- 矩阵；回归成交行数

只读勿改：Desk、`screen/`、`research` 禁 import ledger、漏斗锁、密钥不进表、绑定。

## Tasks & Acceptance

**Execution:**
- [x] `app/models.py` `app/main.py` -- 观察池表与 CRUD，入池闸门 -- 不经 ledger；不成交
- [x] `ResearchView.vue` `PositionsView.vue` `HomeView.vue` -- 入池、预填、工作台待办 -- 无直接改仓
- [x] `tests/test_watchlist.py` -- 矩阵 -- Fake 评估；断言成交行数

**Acceptance Criteria:**
- Given 结论为不通过，when 经评估入口加入观察池，then 被拒绝且持仓不变
- Given 预填记账表单，when 尚未点记账，then 成交账不变
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Spec Change Log

## Design Notes

- 入池体含 `research_id`；名称与代码从该评估拷贝，不信客户端自报结论。
- 预填 `/positions?name=`（可选 `price`）。删除观察池不成交。最近任务读现有 `GET /api/jobs`。

## Verification

**Commands:**
- `pytest tests/test_watchlist.py tests/test_trades.py tests/test_research.py tests/test_health.py` -- expected: 通过；含 pass/gray 入池、fail 400、重名 409、预填不成交
- 检查 `research/` 无 `import ledger` -- expected: 无

**Manual checks (if no CLI):**
- pass/gray 能入池，fail 不能；工作台可见待办；预填未提交则持仓不变。

## Suggested Review Order

**入池闸门**

- 只认已完成评估的 pass/gray，不信客户端自报结论
  [`main.py:328`](../../app/main.py#L328)

- 名称与代码从评估拷贝，重名 409
  [`main.py:1194`](../../app/main.py#L1194)

- 同一用户名称唯一
  [`models.py:165`](../../app/models.py#L165)

**预填而不记账**

- 评估页 pass/gray 可入池；预填只跳看仓
  [`ResearchView.vue:57`](../../web/src/views/ResearchView.vue#L57)

- query 写入记账名称，提交仍走手记
  [`PositionsView.vue:364`](../../web/src/views/PositionsView.vue#L364)

- 工作台待办可预填、可删，不成交
  [`HomeView.vue:146`](../../web/src/views/HomeView.vue#L146)

**测试**

- 矩阵覆盖入池、拒绝、重名、预填不成交
  [`test_watchlist.py:110`](../../tests/test_watchlist.py#L110)

