---
title: '一键审视得到纸面决定'
type: 'feature'
created: '2026-08-30'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 看完持仓表仍写不下决定；聊天式分析无法再打开，也不能对着这本账说话。

**Approach:** 持仓页一键跑一轮纸面审视：每票动作加 0–3 条组合建议。有最近 Checklist 必须引用，否则短体检。占已有全局锁。不记账、不改目标、无一键应用。

## Boundaries & Constraints

**Always:**
- 单票 `action` 仅为 `hold`/`add`/`cut`/`sell`/`unknown`。必须答两问。对照规则灯与目标；无目标则标明。有最近完成评估必须引用；否则短体检并标明「短体检，非六关」。不得无动作的两面讨好。
- 缺现价、过期价、或该票模型失败 → 该票只能 `unknown`；整轮仍 `done`。含过期价须可见。禁止编造行情或财务数字。
- `positions[]` 覆盖当前持仓，键 `name` `action` `reason` `actual_weight` `target_weight` `rule_light` `short_exam`。`portfolio_actions[]` 最多 3，键 `action` `names` `cash_or_target_link`。新票只许最近漏斗 `pass`/`gray`；无漏斗则新票候选为空。
- `review` 不 import `ledger`；`jobs` 注入只读快照（含 stale、规则灯、无目标）。只写 `portfolio_reviews` 与 `reports/` Markdown。不写成交/现金/目标/规则。
- 占全局 `job_lock`；第二任务 409 含当前类型。无密钥 → 400 指向设置。现价刷新不另占锁，但本轮须先刷新（失败则沿用并标过期）。`sync=true` 仅测试。关闭后再打开仍在；持仓页可见最近摘要。

**Ask First:**
- 四大师并入默认审视；一键应用建议改仓或改目标。

**Never:**
- 读 Desk。`review` import `ledger`。一键下单/改仓/改目标。收益排行。「今日推荐」。改手记公式、去劣、观察池闸门。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 成功审视 | 有持仓、有密钥、Fake LLM | 每票有动作；组合 0–3；可再 GET；成交不变 | 无 |
| 有 Checklist | 该名有 done 评估 | 引用该评估；`short_exam=false` | 无 |
| 无 Checklist | 该名无完成评估 | 短体检；`short_exam=true`；文案含「短体检，非六关」 | 无 |
| 过期价 | 刷新失败留下 stale | 该票 `unknown`；整轮 `done`；标明含过期价 | 无 |
| 该票模型失败 | Fake 对该名抛错 | 该票 `unknown`；其余完成；整轮 `done` | 无 |
| 无密钥 | 无 key | HTTP 400 指向设置；无审视行 | 信封 |
| 锁占用 | 已有漏斗或评估 | POST 409；第一任务不被覆盖 | 含任务类型 |
| 组合新票 | LLM 写入不通过名称 | 不得当用现金买入候选 | 规范化剔除 |

</frozen-after-approval>

## Code Map

沿用 `BookSnapshot`/`stale`/`rule_light`（`ledger/quotes.py:58`）；`apply_refresh` 不占锁（`app/main.py:512`）；`try_acquire`（`jobs/lock.py:55`）；研究入队（`app/main.py:1083`）；`FakeLLM`（`research/llm.py:23`）。持仓页刷新旁放按钮（`PositionsView.vue:431`）。无 `review/`、无 `portfolio_reviews`。评估用瘦 `book_to_dict` 缺 stale，审视勿复用。

- `review/` -- 校验 JSON 与 Markdown；不 import ledger
- `jobs/` -- 持锁：先刷新，再注入完整快照、最近评估、漏斗 pass/gray 名称
- `app/models.py` `app/main.py` `pyproject.toml` -- 表；`POST/GET /api/reviews` 与 `GET /api/reviews/latest`；锁类型 `review`
- `PositionsView.vue` `HomeView.vue` -- 一键、摘要、锁文案；无一键应用
- `tests/test_review.py` -- 矩阵

只读勿改：Desk、`screen/`、`record_trade`、观察池闸门、密钥不进表、绑定。

## Tasks & Acceptance

**Execution:**
- [x] `review/` `jobs/` -- 校验与持锁注入 -- 过期/失败票 unknown；不 import ledger
- [x] `app/models.py` `app/main.py` `pyproject.toml` -- 表与 API -- 400/409；可再打开
- [x] `PositionsView.vue` `HomeView.vue` -- 一键与摘要 -- 无一键应用
- [x] `tests/test_review.py` -- 矩阵 -- Fake LLM + Fake quotes

**Acceptance Criteria:**
- Given 无密钥，when 请求审视，then 400 指向设置且无假装结论
- Given 过期价或该票模型失败，when 审视完成，then 该票 unknown 且整轮 done
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Spec Change Log

## Design Notes

- 锁 `job_type` 为 `review`。Jobs 注入含 stale/rule_light/no_target 的快照。
- 最近 Checklist：同名最新 done 评估。新票候选：最近漏斗下评估 pass/gray 名称。
- 先刷新再投影。stale 或无价强制 unknown；非法新票从组合建议剔除。

## Verification

**Commands:**
- `pytest tests/test_review.py tests/test_watchlist.py tests/test_research.py tests/test_health.py` -- expected: 通过；含入库、400、409、过期 unknown、短体检、不成交
- 检查 `review/` 无 `import ledger` -- expected: 无

**Manual checks (if no CLI):**
- 持仓页能一键审视并看到摘要；过期价为无法判断；无「一键应用」。

## Suggested Review Order

**纸面决定与闸门**

- 过期或无价强制 unknown，整轮仍完成
  [`run.py:63`](../../review/run.py#L63)

- 组合建议只许漏斗 pass/gray 新票
  [`run.py:139`](../../review/run.py#L139)

- 审视包不碰账本
  [`__init__.py:1`](../../review/__init__.py#L1)

**持锁注入**

- 锁类型是 review，与漏斗/评估共用一行
  [`lock.py:11`](../../jobs/lock.py#L11)

- 先刷新再投影完整快照，含过期价与规则灯
  [`review.py:52`](../../jobs/review.py#L52)

- 无合法 verdict 的评估不当最近 Checklist
  [`review.py:82`](../../jobs/review.py#L82)

- 失败票不拖垮整轮；锁在 finally 释放
  [`review.py:174`](../../jobs/review.py#L174)

**API 与表**

- 无密钥 400；坏 JSON 400；占锁 409
  [`main.py:1356`](../../app/main.py#L1356)

- 最近摘要只认已完成审视
  [`main.py:1426`](../../app/main.py#L1426)

- 纸面结果落 portfolio_reviews，不成交
  [`models.py:177`](../../app/models.py#L177)

**界面**

- 持仓页一键审视，无一键应用
  [`PositionsView.vue:587`](../../web/src/views/PositionsView.vue#L587)

- 工作台锁文案含审视，只读最近摘要
  [`HomeView.vue:36`](../../web/src/views/HomeView.vue#L36)

**测试**

- 矩阵覆盖入库、无钥、占锁、过期、缺价、短体检
  [`test_review.py:176`](../../tests/test_review.py#L176)
