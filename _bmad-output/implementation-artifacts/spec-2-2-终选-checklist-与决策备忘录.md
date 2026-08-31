---
title: '终选 Checklist 与决策备忘录'
type: 'feature'
created: '2026-08-29'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-2-1-按范围跑漏斗得到三态名单.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 漏斗已能打出终选，但没有可再打开的强制结论；研究仍要靠聊天，也无法对照当前仓说话。

**Approach:** 默认对终选最多三家跑 Checklist + 决策备忘录（也可 `evaluate=true`）；漏斗与评估占同一把锁。无密钥时评估 400 并指向设置。可只要漏斗。本故事不做观察池、审视、四大师。

## Boundaries & Constraints

**Always:**
- 结论仅为 `pass`/`fail`/`gray`（UI 通过/不通过/灰色地带）。六关：能力圈、好生意、护城河、管理层、安全边际、决策纪律；每关通过/失败/灰色 + 一句理由。镜子测试须五句，不完整则不得为 `pass`。价格带：激进/稳健/保守或观望。备忘含生意本质、主要风险、仓位角色（核心/卫星/不买）。`portfolio_fit` 必须对照注入的 `BookSnapshot` 回答三问：该不该进当前仓、用什么钱买、是否叠逻辑。
- `structured_json` 必含：`verdict` `gates` `mirror_test` `price_bands` `memo` `data_grade` `as_of` `portfolio_fit`。Markdown 双写 `reports/`。关闭后再打开仍在。
- 财务或行情不足 → `gray`，不编造数字。未开网页搜索仍可用已抓取财务完成，不得把「无搜索」写成「无财务」。
- `research` 不 import `ledger`；不写成交/现金/目标。由 `jobs` 注入只读仓快照。LLM 为 OpenAI 兼容 HTTP，不引入官方 SDK。测试 Fake LLM，不打真模型。
- 漏斗为父任务，`research_jobs.screen_run_id` 指向该轮；界面可见最多三个评估子任务。漏斗+默认评估占同一把 `job_lock`，评估结束前不释放。某子任务失败不推翻已完成漏斗名单。只筛（`evaluate=false`）不因无密钥失败。需要模型的评估无密钥 → HTTP 400 指向设置，不生成假装完整结论。`sync=true` 仅测试。

**Ask First:**
- 把四大师设为默认评估；从评估页直接改持仓。

**Never:**
- 观察池、预填记账、审视、四大师入口、一键下单/改目标。
- 读 Desk。`research` import `ledger`。编造财务报表数字。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 默认评估成功 | Fake LLM + 已有密钥；`evaluate` 开或默认开 | 最多三家产物；结构化字段入库；`reports/` 有 MD；可再 GET | 漏斗名单仍在 |
| 无密钥评估 | `evaluate=true` 且无 key | HTTP 400 指向设置；无假装结论 | 信封 `error`/`code`/`detail` |
| 只筛 | `evaluate=false` 无 key | 漏斗 `done`；无评估行 | 成功 |
| 数据不足灰色 | Fake 缺财务或现价 | `verdict=gray`；无编造数字 | 无 |
| 无搜索 | `web_search=false` | 仍完成；文案不是「无财务」 | 无 |
| 子任务失败 | 一家 Fake LLM 失败 | 该子任务 `failed`；漏斗 keep 不变 | 不推翻名单 |
| 镜子不完整 | Fake 镜子不足五句 | 结论不得为 `pass` | 无 |

</frozen-after-approval>

## Code Map

沿用 2.1：funnel POST 忽略 `evaluate`（`app/main.py:792`）；`execute_funnel` 结束即 `release`（`jobs/funnel.py:152`）；screen payload `jobs: []`（`app/main.py:236`）；`BookSnapshot`（`ledger/quotes.py:58`）；`has_api_key`（`app/main.py:185`）；无 `research/`、无 `/research/:id`。

- `research/` -- 结构化校验、Markdown、Fake LLM；不 import ledger
- `jobs/` -- `evaluate` 为真时持锁跑最多三子任务；注入仓快照；失败不删名单
- `app/models.py` `app/main.py` `pyproject.toml` -- 评估表与 API；GET screen 带 jobs；无密钥 400；`llm_client`；hatch 纳入 `research`
- `ScreenView.vue` `ResearchView.vue` `router` -- 评估开关、子任务、结论条；`/research/:id`
- `tests/test_research.py` -- 矩阵；回归只筛无密钥
- `.gitignore` -- `reports/`

只读勿改：Desk、成交、去劣、`screen/` 无 HTTP、密钥不进表、绑定。观察池留给 2.3。

## Tasks & Acceptance

**Execution:**
- [x] `research/` `jobs/` -- Fake LLM、校验 structured、持锁评估 -- 不 import ledger；失败不推翻名单
- [x] `app/models.py` `app/main.py` `pyproject.toml` -- 表与 API -- 无密钥 400；GET 含子任务
- [x] `ScreenView.vue` `ResearchView.vue` `router` -- 开关、子任务、结论条 -- 非 JSON 主交互
- [x] `tests/test_research.py` -- 矩阵 -- Fake；回归只筛

**Acceptance Criteria:**
- Given 无密钥且只要漏斗，when 提交 `evaluate=false`，then 名单成功且无评估行
- Given 无密钥且要评估，when 提交，then 400 并指向设置
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Spec Change Log

## Design Notes

- `evaluate` 缺省 true；选股页能关掉。只筛测试须显式 `evaluate: false`。
- 锁类型仍为 `funnel`。`POST /api/research` 单独评估也占这把锁。

## Verification

**Commands:**
- `pytest tests/test_research.py tests/test_screen.py tests/test_settings.py tests/test_health.py` -- expected: 通过；含结构化入库、400、只筛、灰色、子任务失败不推翻名单、镜子不足不得 pass
- 检查 `research/` 无 `import ledger`；`screen/` 无 httpx -- expected: 无

**Manual checks (if no CLI):**
- 打开评估页能看到结论条与 Markdown；关掉再开仍在；选股页能看到子任务状态

## Suggested Review Order

**漏斗与评估同一把锁**

- 默认评估；无密钥直接 400，不假装出结论
  [`main.py:931`](../../app/main.py#L931)

- 漏斗先标 done，再持锁跑最多三家终选
  [`funnel.py:144`](../../jobs/funnel.py#L144)

- 评估异常不把已完成名单改成 failed
  [`funnel.py:154`](../../jobs/funnel.py#L154)

- 子任务从 jobs 注入只读仓快照，research 不碰账本
  [`research.py:61`](../../jobs/research.py#L61)

- 重启把残留评估标失败并清锁
  [`lock.py:33`](../../jobs/lock.py#L33)

**Checklist 与灰色地带**

- 财务或现价不足时不调用模型，直接 gray
  [`evaluate.py:78`](../../research/evaluate.py#L78)

- ROE 与净利须在同一行才算有财务
  [`evaluate.py:55`](../../research/evaluate.py#L55)

- 镜子不足五句或任一关失败则不得 pass
  [`evaluate.py:110`](../../research/evaluate.py#L110)

- 无搜索文案写已抓取财务，不写成无财务
  [`evaluate.py:142`](../../research/evaluate.py#L142)

- LLM 走 OpenAI 兼容 HTTP，测试注入 FakeLLM
  [`llm.py:61`](../../research/llm.py#L61)

**API 与界面**

- GET 选股结果带子任务，最多三家
  [`main.py:253`](../../app/main.py#L253)

- 产物扁平返回，关掉再开仍在
  [`main.py:277`](../../app/main.py#L277)

- 任务列表把评估标成 research，锁类型仍是 funnel
  [`main.py:1004`](../../app/main.py#L1004)

- 单独评估也占同一把锁
  [`main.py:1039`](../../app/main.py#L1039)

- 选股页默认勾评估终选，可进子任务
  [`ScreenView.vue:186`](../../web/src/views/ScreenView.vue#L186)

- 评估页结论条与 Markdown，404 停止轮询
  [`ResearchView.vue:140`](../../web/src/views/ResearchView.vue#L140)

**测试**

- 矩阵覆盖默认评估、无密钥、只筛、灰色、子任务失败
  [`test_research.py:110`](../../tests/test_research.py#L110)

- 漏斗 done 后锁仍占用到评估结束
  [`test_research.py:368`](../../tests/test_research.py#L368)

