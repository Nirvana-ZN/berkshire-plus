---
title: '按范围跑漏斗得到三态名单'
type: 'feature'
created: '2026-08-29'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/_bmad-output/specs/spec-berkshire-plus/funnel.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 选股页只是占位，无法按行业/指数/主题筛一轮；要淘汰理由还得另跑模型。

**Approach:** 提交范围后只在解析出的成分上做去劣与漏斗，选出保留/淘汰/数据不足三张表和最多三家终选（含角色打标）。本故事落地全局重任务锁；不调用 LLM、不创建 Checklist。

## Boundaries & Constraints

**Always:**
- 范围类型仅行业 / 指数 / 主题。只在解析出的成分上筛。指数或主题解析不到明确列表，或财务接口整体不可用：本轮失败，错误原文可展示；不编名单、不降级全市场、不提供手工成分表。
- 去劣、保留名单（最多十家）、终选（最多三家、名称不重复）以 `funnel.md` 为准；阈值写死。淘汰必有理由。数据不足不进保留名单、不进终选。单家缺字段或窗口不足 → 该行数据不足，任务可以 `done`。
- 选股页三张表为主交互，禁止成分快照 JSON 当主界面。持久化 `keep`/`drop`/`insufficient`，UI 保留/淘汰/数据不足。
- 漏斗是重任务：`job_lock` 全局一行；第二任务 HTTP 409，正文含当前任务类型；不覆盖、不并行写坏第一任务。现价刷新与手工价不占这把锁。默认立即返回 job id 供轮询；`sync=true` 仅测试。`GET /api/health` 含锁状态。启动清理残留锁。只筛名单不因无密钥失败。
- `screen` 不 import HTTP/LLM。出站只在 `market`，httpx2。测试 Fake market + 固定夹具，不打真行情/真模型。

**Ask First:**
- 运行时改去劣阈值；主题失败后改用手填成分表；本故事调用 LLM。

**Never:**
- 调用 LLM、创建 Checklist/评估产物、观察池、审视、四大师。
- 读 Desk。一键下单或改目标。把漏斗做成收益排行榜或「今日推荐」。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 只筛成功 | Fake 成分+财务；无密钥；`evaluate` 关或不接 | 三态名单；终选≤3 且不重复；淘汰有理由；任务 `done` | 无密钥也成功 |
| 成分失败 | 指数/主题解析不到明确列表 | 任务 `failed`；不编名单 | 错误原文可展示 |
| 财务整体失败 | 财务接口整体不可用 | 任务 `failed`；不编名单 | 错误原文可展示 |
| 单家不足 | 缺代码/市值/至少 1 年 ROE 或净利润 | 该行 `insufficient`；任务 `done` | 不进保留/终选 |
| 锁占用 | 已有重任务 | POST 409；第一任务不被覆盖 | 信封含当前任务类型 |
| 现价不占锁 | 刷新现价 | `health.lock` 未占用；仍可提交漏斗 | 无 |

</frozen-after-approval>

## Code Map

沿用：`GET /api/health` `lock: None`（`app/main.py:239`）、`create_app(..., quote_client=)`、`FakeQuoteClient`（`market/quotes.py:19`）、`ScreenView.vue` 占位、路由 `/screen`。无 `jobs/` `screen/`。规则全文：`_bmad-output/specs/spec-berkshire-plus/funnel.md`。

- `market/` -- 成分与财务客户端 + Fake（东方财富；httpx2）；失败可注入
- `screen/` -- 消费已落地快照/财务，输出三态、理由、保留十家、终选打标；不 import HTTP/LLM
- `jobs/` -- `job_lock`、调 market、落 `screen_runs`/`screen_results`、启动清残留锁
- `app/models.py` `app/main.py` `pyproject.toml` -- 表（含 `financials`、按需 `listed_year`）；`POST /api/screens/funnel`、`GET /api/screens/{id}`、`GET /api/jobs`、`GET /api/jobs/{id}`；health 真锁；注入 Fake；hatch 纳入 `screen` `jobs`
- `web/src/views/ScreenView.vue` -- 范围提交、轮询、三表与终选
- `tests/test_screen.py` -- 矩阵夹具；回归 health/quotes

只读勿改：Desk、`record_trade`、目标/规则灯、`.env` 密钥约定、`__main__.py` 绑定、腾讯现价内部。`evaluate=true` 本故事忽略，不创建评估行。

## Tasks & Acceptance

**Execution:**
- [x] `market/` `screen/` `jobs/` -- 成分/财务 Fake、去劣漏斗、锁与落库 -- 规则以 funnel.md 为准；screen 无 HTTP
- [x] `app/models.py` `app/main.py` `pyproject.toml` -- 表、API、health 锁、包发现 -- 409 与只筛不因无密钥失败
- [x] `web/src/views/ScreenView.vue` -- 提交、轮询、三表与终选 -- 中文表，非 JSON
- [x] `tests/test_screen.py` -- 矩阵 -- Fake；不打真行情/真模型

**Acceptance Criteria:**
- Given 只筛成功，when 查看选股页，then 三张表可见且淘汰有理由，终选名称不重复
- Given 已有重任务，when 再提交漏斗，then 409 且第一任务产物不被覆盖
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Spec Change Log

## Design Notes

- FCF = 经营现金流 − 资本开支。锁只认库表 `job_lock` 一行。
- POST：`scope_type`（行业/指数/主题）、`scope_text`、可选 `evaluate`（忽略）、测试可 `sync`。状态 `queued`/`running`/`done`/`failed`。

## Verification

**Commands:**
- `pytest tests/test_screen.py tests/test_quotes.py tests/test_health.py tests/test_settings.py` -- expected: 通过；含三态、终选不重复、失败原文、409、无密钥只筛、刷新后 lock 未占用
- 检查 `screen/` 无 httpx/openai 引用 -- expected: 无

**Manual checks (if no CLI):**
- 选股页提交后能看到三表；失败可见原文；刷新现价不挡下一轮漏斗

## Suggested Review Order

**入口与锁**

- 提交漏斗：占锁失败则 409，不覆盖第一任务
  [`main.py:793`](../../app/main.py#L793)

- 全局一行锁，启动时清残留任务
  [`lock.py:48`](../../jobs/lock.py#L48)

- health 返回真实占用；现价刷新不走这条路径
  [`main.py:312`](../../app/main.py#L312)

**去劣与终选**

- 规则、保留十家、角色桶与终选打标
  [`funnel.py:410`](../../screen/funnel.py#L410)

- 单家分类：整家不足 vs 淘汰理由
  [`funnel.py:197`](../../screen/funnel.py#L197)

**抓取与落库**

- Fake/实盘成分与财务；空代码不当整轮失败
  [`eastmoney.py:116`](../../market/eastmoney.py#L116)

- 调 market、写结果、失败也释放锁
  [`funnel.py:95`](../../jobs/funnel.py#L95)

**选股页**

- 三张中文表与终选；409 不打断上一轮轮询
  [`ScreenView.vue:121`](../../web/src/views/ScreenView.vue#L121)

**测试**

- 矩阵：只筛、失败原文、不足、409、刷新不占锁、保留上限
  [`test_screen.py:132`](../../tests/test_screen.py#L132)
