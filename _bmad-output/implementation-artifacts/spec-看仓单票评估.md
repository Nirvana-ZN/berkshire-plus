---
title: '看仓单票评估'
type: 'feature'
created: '2026-08-31'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/spec-2-2-终选-checklist-与决策备忘录.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 看仓已有持仓，但只能经选股终选跑六关；账上的票没有单票 Checklist 入口，审视只能短体检。

**Approach:** 看仓每只持仓可对该票发起已有 `POST /api/research`：有代码则排队评估并打开评估页；无代码则走补代码。不改审视深度，不批量评估整仓。

## Boundaries & Constraints

**Always:**
- 复用 `POST /api/research`（`name`+`code`）。锁类型仍为 `funnel`；冲突 409，detail 含当前任务类型。无密钥 400 且指向设置，不生成假装结论。
- 有代码：提交成功后进入 `/research/:id`。无代码：打开补代码，不发评估请求。现金行无评估入口。
- 表 `.book` 内不得出现 `<button` 或 `<a `（沿用现测）。评估用与「无代码」同类的可点文案。
- 保留：`一键审视`、`短体检，非六关`、`补代码`、`保存目标`、`form.shares` 不在表内、无一键应用。`research/` 不 import `ledger`。

**Ask First:**
- 一次评估多只持仓；评估完成后自动开审视；四大师。

**Never:**
- 审视三档深度（已记入 deferred-work）。改漏斗 `evaluate=false`。一键应用/下单。读 Desk。在 `.book` 表内加 button/锚点。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 有代码有密钥 | 点该票评估 | POST 得 id；跳转 `/research/:id` | 无 |
| 无代码 | 点该票评估 | 打开补代码 fold；无 research 行 | 无 POST |
| 无密钥 | 有代码，点评估 | 停留看仓；文案指向设置 | 400 |
| 锁占用 | 已有漏斗/评估/审视 | 停留看仓；可见当前任务类型 | 409 |
| 现金 | 现金行 | 无评估入口 | 无 |

</frozen-after-approval>

## Code Map

沿用独立评估：`POST /api/research`（`app/main.py:1333-1406`，缺 name/code → `INVALID_RESEARCH`；无密钥 `_missing_key_error:208`；`try_acquire(..., JOB_TYPE_FUNNEL)`；200 `{id,status,type}`；默认线程 `_run_research_job`）。`GET /api/research/{id}`（`:1408`）+ `ResearchView.vue` 800ms 轮询。锁 `jobs/lock.py:63`；409 `_conflict_locked:471`。代码来自持仓 `row.code`；无映射则空（`ledger/quotes.py:126-131`，`prefillCode:709`）。评估页已有，勿重写产物结构。

- `web/src/views/PositionsView.vue` -- 持仓行「评估」；`startResearch`；无代码走 `prefillCode`；成功 `useRouter` 去 `/research/:id`；400/409 写入现有错误位。表内禁用 button/a。工具栏一键审视不动。
- `tests/test_research.py` -- 源码断言：`评估`、`/api/research`、跳转 `/research/`；表内无 `<button`/`<a `；现金行无评估。复用无密钥 400 与锁 409。
- `web/` dist -- `npm run build-only`；uvicorn 托管 `web/dist`（`app/main.py:67`）

只读勿改：`research/` 求值与 Fake LLM、漏斗 `evaluate=false`、`review/`、Desk、`record_trade`、观察池闸门、密钥不进表。

## Tasks & Acceptance

**Execution:**
- [x] `web/src/views/PositionsView.vue` -- 单票评估入口与跳转 -- 无代码不 POST；表内无 button/a
- [x] `tests/test_research.py` -- 源码矩阵 -- 不放宽规则/目标/审视的表内禁令
- [x] `web/` -- `npm run build-only` -- 本机 uvicorn 能点到评估页

**Acceptance Criteria:**
- Given 持仓有代码，when 点评估且密钥可用、锁空闲，then 创建 research 任务并打开该评估页
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk
- Given 现有源码测，when 跑 `test_rules`/`test_targets`/`test_review` 的 PositionsView 断言，then 仍通过

## Spec Change Log

## Design Notes

`.book` 表被 `test_targets`/`test_rules` 锁死：表内不得 `<button`、`<a `。评估做成与「无代码」一样的 `span` + `@click.stop`，不要 RouterLink。

## Verification

**Commands:**
- `Remove-Item Env:PORTFOLIO_YAML_DIR -ErrorAction SilentlyContinue; uv run pytest tests/test_research.py tests/test_rules.py tests/test_targets.py tests/test_review.py tests/test_quotes.py tests/test_trades.py -q` -- expected: 全绿
- `npm run build-only`（在 `web/`）-- expected: dist 更新

## Suggested Review Order

**入口与无代码短路**

- 看仓点「评估」：空白代码只开补代码，有代码才 POST
  [`PositionsView.vue:307`](../../web/src/views/PositionsView.vue#L307)

- 表内可点文案，不是 button/锚点，现金行没有入口
  [`PositionsView.vue:854`](../../web/src/views/PositionsView.vue#L854)

**失败留在看仓**

- 400/409 把接口 detail 写到页顶，成功才跳评估页
  [`PositionsView.vue:325`](../../web/src/views/PositionsView.vue#L325)

**源码矩阵**

- 锁表内禁令、现金无评估、POST 字段与跳转
  [`test_research.py:378`](../../tests/test_research.py#L378)

