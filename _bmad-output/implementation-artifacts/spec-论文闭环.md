---
title: '论文闭环'
type: 'feature'
created: '2026-08-31'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/spec-2-2-终选-checklist-与决策备忘录.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-3-1-一键审视得到纸面决定.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-看仓单票评估.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 审视引用「该名称最新评估」，评估没有身份、不会过期；旧仓短体检、新票六关，买入论文不在台面上。

**Approach:** 合格评估自动 upsert 该名称现行论文。审视只认现行论文；过期或没有则短体检并标明「论文过期」或「无论文」。过期价仍无法判断。

## Boundaries & Constraints

**Always:**
- 同一 `user_id`+`name` 最多一份现行论文。内容：`research_job_id`、`verdict`、镜子五句、价格带、`as_of` 日期与股价。
- 立论：`verdict` 为 `pass`/`gray`，镜子满五句，且五句都不是数据不足占位（含「无法完成」）。漏斗终选与持仓单票评估同一规则。
- `fail` 不立；该名称若已有现行论文则删除。占位灰色与评估任务失败不改现行论文。不回填历史评估。
- 过期（只改展示状态，不删行）：研究日距今 > 90 自然日；或 `mode != watch` 且非过期现价 < 保守带下沿（`conservative` 文本第一个有限数字，解析不出则本条不触发）；或 `mode == watch` 且有研究日股价、非过期现价相对偏离 > 15%。过期价不参与价格带/偏离判定。
- 审视：`review` 不 import `ledger`。有现行且未过期 → 必须引用「现行论文 {id}」，`short_exam=false`。无论文 → 短体检 +「无论文」。过期 → 短体检 +「论文过期」，动作仍为 hold/add/cut/sell。过期价或该票模型失败 → `unknown`（优先于论文状态）。不再把「最新评估」当论文。
- 持仓行、观察池可见 `thesis_status`：`current`/`stale`/`none`（有效/过期/无论文）。有效或过期可点开 `/research/:id`。评估页在本 job 即现行论文时标明已立或已更新。表 `.book` 内不得 `<button` / `<a `。`research/` 不 import `ledger`。不写成交/目标。

**Ask First:**
- 回填历史评估为论文；改 90 日 / 15% 阈值；过期后自动开评估；假设清单/红线/健康度。

**Never:**
- 四大师、一键应用/下单、改漏斗 `evaluate=false`、读 Desk、表内 button/锚点。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 合格评估 | pass/gray，五句真实 | upsert 现行论文；评估页「已立为论文」或「已更新论文」 | 无 |
| 不通过 | verdict=fail | 不立；删该名称现行论文 | 无 |
| 占位灰色 | 五句含「无法完成」 | 不立、不删 | 无 |
| 评估失败 | job failed | 论文不变 | 原错误可见 |
| 持仓展示 | 有现行 / 过期 / 无 | 有效可点开；过期可点开；无论文不可冒充链接 | 无 |
| 审视·有效论文 | 现行未过期 | 引用现行论文 id；非短体检 | 无 |
| 审视·过期论文 | 触达过期规则 | 短体检 +「论文过期」；有动作 | 无 |
| 审视·无论文 | 无行 | 短体检 +「无论文」 | 无 |
| 审视·过期价 | stale 或无价 | 该票 `unknown`，含过期价 | 整轮仍 done |

</frozen-after-approval>

## Code Map

- `research/thesis.py` -- `eligible` / `upsert` / `clear_on_fail` / `status`（90 日、保守下沿、观望 15%）。不 import `ledger`。
- `app/models.py` -- `theses`：`user_id`+`name` 唯一；`research_job_id`；`structured` 快照或分列镜子/价格带/`as_of`。`create_all` 即可。
- `jobs/research.py` -- 产物 `done` 后立或覆盖；`fail` 结论清论文。任务异常不改论文。
- `jobs/review.py` -- `load_current_theses` 替换「最新评估」作为引用源；注入过期状态。
- `review/run.py` -- 引用文案「现行论文」；短体检强制「无论文」或「论文过期」；过期价规则不动。
- `app/main.py` -- 持仓/观察池/评估 GET 带 `thesis_status`、`thesis_research_id`。
- `web/src/views/PositionsView.vue` -- `.book` 论文列，span 点击去评估页；现金行无。
- `web/src/views/HomeView.vue` -- 观察池论文状态。
- `web/src/views/ResearchView.vue` -- 现行论文提示。
- `tests/test_thesis.py` -- 立/覆盖/fail 清除/占位不立/过期三规则/过期价不判带。
- `tests/test_review.py` -- 引用现行论文；过期与无论文短体检；过期价仍 unknown。
- `tests/test_research.py` -- 持仓表论文列；表内无 button/a；现金无论文入口。
- `web/` dist -- `npm run build-only`。

只读勿改：漏斗规则、`evaluate=false`、记账、观察池闸门、密钥不进表、Desk。

## Tasks & Acceptance

**Execution:**
- [x] `research/thesis.py` + `app/models.py` -- 现行论文表与纯规则 -- 可单测、不碰账本
- [x] `jobs/research.py` -- done 后 upsert / fail 清除 -- 占位与任务失败不写
- [x] `jobs/review.py` + `review/run.py` -- 审视只认现行论文 -- 过期价优先 unknown
- [x] `app/main.py` -- 持仓/观察池/评估带论文字段
- [x] `web/src/views/PositionsView.vue` + `HomeView.vue` + `ResearchView.vue` -- 状态与点开
- [x] `tests/test_thesis.py` + 更新 `test_review.py` / `test_research.py` -- 矩阵与表内禁令
- [x] `web/` -- `npm run build-only`

**Acceptance Criteria:**
- Given 合格评估完成，when 再打开持仓与该评估页，then 该名称有现行论文且页上标明已立或已更新
- Given 现行论文已过 90 日或击穿价格规则，when 一键审视且现价未过期，then 该票短体检、文案含「论文过期」、动作为 hold/add/cut/sell 之一
- Given 过期价，when 一键审视，then 该票无法判断，即使论文仍有效
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Spec Change Log

## Design Notes

数据不足占位句来自 `research/evaluate.py` 的 `_gray_from_facts`（「第 N 句无法完成」）。合格判定不得把这类五句立成论文。

保守带是模型自由文本。下沿 = 文本中第一个能解析的有限数字；没有则不因价格带过期。

`.book` 表沿用 span + `@click.stop`，点有效/过期去 `/research/{thesis_research_id}`，不要 RouterLink。

## Verification

**Commands:**
- `Remove-Item Env:PORTFOLIO_YAML_DIR -ErrorAction SilentlyContinue; uv run pytest tests/test_thesis.py tests/test_review.py tests/test_research.py tests/test_rules.py tests/test_targets.py tests/test_watchlist.py -q` -- expected: 全绿
- `npm run build-only`（在 `web/`）-- expected: dist 更新

## Suggested Review Order

**立论规则**

- 合格判定：通过/灰色、五句真实镜子，占位「无法完成」不立
  [`thesis.py:29`](../../research/thesis.py#L29)

- 同一名称一份现行论文；覆盖记已更新
  [`thesis.py:44`](../../research/thesis.py#L44)

- 评估 done 后 upsert；fail 才清除；占位与任务失败不改
  [`research.py:246`](../../jobs/research.py#L246)

- 表：`user_id`+`name` 唯一，快照含评估 id 与 structured
  [`models.py:177`](../../app/models.py#L177)

**过期判定**

- 缺研究日或坏快照一律过期；90 日 / 保守下沿 / 观望 15%；过期价不参与
  [`thesis.py:85`](../../research/thesis.py#L85)

**审视只认现行论文**

- 用现行论文替换「最新评估」，并注入过期状态
  [`review.py:83`](../../jobs/review.py#L83)

- 提示词带上镜子、价格带、as_of，两问对着论文正文
  [`run.py:399`](../../review/run.py#L399)

- 引用必须是「现行论文 {id}」；过期/无论文走短体检
  [`run.py:283`](../../review/run.py#L283)

**界面**

- 持仓行 span 点有效/过期打开评估页，表内无 button
  [`PositionsView.vue:844`](../../web/src/views/PositionsView.vue#L844)

- 持仓/评估/观察池带 thesis_status 与 research_id
  [`main.py:122`](../../app/main.py#L122)

- 评估页标明已立或已更新
  [`ResearchView.vue:203`](../../web/src/views/ResearchView.vue#L203)

- 观察池链接同时要求 status 与非空 id
  [`HomeView.vue:181`](../../web/src/views/HomeView.vue#L181)

**测试**

- 立/覆盖/fail 清除/占位不立/过期三规则/HTTP stale
  [`test_thesis.py:142`](../../tests/test_thesis.py#L142)

- 引用现行论文；价格带与观望偏离过期；过期价仍 unknown
  [`test_review.py:275`](../../tests/test_review.py#L275)
