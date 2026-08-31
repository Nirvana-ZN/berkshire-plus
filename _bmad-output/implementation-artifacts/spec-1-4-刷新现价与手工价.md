---
title: '刷新现价与手工价'
type: 'feature'
created: '2026-08-28'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 持仓有股数和成本，但没有现价，市值与浮盈亏算不出来；行情失败时也不能把过期价装成新价。

**Approach:** 同步刷新持仓现价（不占重任务锁）。`market` 只负责解析与 HTTP，不写 `instruments`。`ledger` 只读写已落库的 `quotes`（`code` 复制自 `instruments`）。失败保留旧价并标过期；无代码不写假价。用户可补代码或手工价。

## Boundaries & Constraints

**Always:**
- 出站用 `httpx2==2.12.0`。测试注入 Fake market，不打真行情。
- 刷新同步；`GET /api/health` 的 `lock` 仍为未占用，本故事不建 `job_lock`。
- `ledger` 不 import HTTP。`market` 不写 `instruments`：返回代码后由 `app` 经 `ledger` 落表。
- `quotes` 主键语义：`user_id`+`name`；`code` 来自 `instruments`。手工价 `source` 与自动行情可区分（如 `manual` / `auto`）。
- 成功刷新：该标的不再 `stale`；持仓表算市值、实盘占比、浮盈亏。失败：保留旧价、`stale=true`，不删持仓。
- 代码须为 `.SH` / `.SZ` / `.BJ`。无法解析：不写假价；单独刷新该名称 → HTTP 400 信封。
- 无现价：市值/浮盈亏/占比为空，不得用 0 假装有价。占比分母 = 有价持仓市值之和 + 现金。
- 不读 Desk。不改 `record_trade` 语义、迁入、绑定。

**Ask First:**
- 本故事引入第二套名称↔代码表，或刷新走后台锁。

**Never:**
- `ledger` 打行情 HTTP；`market` 写 `instruments`。
- 本故事实现审视、目标仓位、规则灯、漏斗。
- 过期价上给出持有/加仓等动作。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 刷新成功 | 名称已有 `.SH/.SZ/.BJ` 代码；Fake 返回价 | 写入 quotes；`stale=false`；市值=股数×现价；浮盈亏=市值−成本；占比按分母 | 单票 Fake 失败见下行 |
| 行情失败 | 该票已有旧价；Fake/HTTP 失败 | 旧价保留；`stale=true`；持仓仍在 | 表上过期可识别；不 500 空仓 |
| 无代码整表刷新 | 有持仓缺 instruments | 有代码的照常；无代码的不写假价，计入 `errors`；总请求 2xx | 错误可读 |
| 无代码单票 | 对该名称刷新或补价 | HTTP 400，`error`/`code`/`detail` | 不写 quotes |
| 补代码 | POST 名称+代码 | `ledger` 写 `instruments`；随后可刷新 | 非法后缀 400 |
| 手工价 | 名称+价格 | quotes `source=manual`，`stale=false`；与 auto 可区分 | 非正价格 400 |

</frozen-after-approval>

## Code Map

沿用：`GET /api/positions`（`app/main.py` `_positions_payload`）、`load_positions`、`Instrument`（`app/models.py`）、`PositionsView.vue` 表。无 `market/`、无 `Quote`。`httpx2` 尚未进运行时依赖。

- `app/models.py` -- 增加 `Quote`（user_id, name, code, price, source, updated_at, stale）
- `market/quotes.py` -- 取现价接口 + Fake；只返回价/失败，不写库、不写 instruments
- `ledger/quotes.py` -- 写 quotes（code 从 instruments 复制）、补代码、手工价；`load_positions` 或新投影拼现价/市值/占比/浮盈亏/stale
- `app/main.py` -- `POST /api/quotes/refresh`（可选 name）；`POST /api/instruments`；`POST /api/quotes/manual`；扩展 GET positions
- `web/src/views/PositionsView.vue` -- 现价/市值/占比/浮盈亏/过期标记；刷新按钮；补代码与手工价表单
- `pyproject.toml` -- 运行时 `httpx2==2.12.0`；hatch 含 `market`
- `tests/test_quotes.py` -- Fake：成功、失败标 stale、无代码 400/errors、手工价 source、ledger 无 HTTP

只读勿改：Desk、`record_trade` 公式、migrate 四件套、`__main__.py` 绑定、health 形状。

## Tasks & Acceptance

**Execution:**
- [x] `app/models.py` -- `quotes` 表 -- 现价可落库
- [x] `market/quotes.py` `ledger/quotes.py` -- Fake/真实取价与落库边界 -- ledger 不 HTTP，market 不写 instruments
- [x] `app/main.py` -- refresh / 补代码 / 手工价 + 扩展 positions -- 同步、无锁
- [x] `web/src/views/PositionsView.vue` -- 报价列与操作 -- 过期可识别，非 JSON 主交互
- [x] `tests/test_quotes.py` `pyproject.toml` -- Fake 覆盖矩阵 -- 不打真行情

**Acceptance Criteria:**
- Given 持仓表有过期价，when 查看，then 过期可识别且持仓行仍在
- Given 手工价与自动价，when 查看，then 来源可区分
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Design Notes

- 依赖注入：`create_app(..., quote_client=Fake)`；默认生产客户端用 httpx2 调腾讯行情，测试不走网络。
- 整表刷新不要因一票无代码整单 400；单票无代码才 400。
- 浮盈亏与占比只在有现价时计算。目标仓位列留给 1.5，本故事可空。
- `stale` 必须持久，供 Epic 3 读成无法判断。

## Verification

**Commands:**
- `pytest tests/test_quotes.py tests/test_trades.py tests/test_migrate.py tests/test_health.py` -- expected: 通过；含成功刷新、stale、无代码、手工价
- 检查 `ledger/` 无 httpx/httpx2 import -- expected: 无

**Manual checks (if no CLI):**
- 持仓页刷新：有代码的出市值；失败标过期；可补代码与手工价

## Suggested Review Order

**注入与边界**

- 测试注入 Fake；生产才用腾讯客户端
  [`main.py:105`](../../app/main.py#L105)

- market 只 HTTP/解析，把腾讯符号映回 A 股代码
  [`quotes.py:93`](../../market/quotes.py#L93)

- ledger 写 quotes：有限正价才落库，否则标过期或记错误
  [`quotes.py:222`](../../ledger/quotes.py#L222)

**落库形状**

- quotes 主键 `user_id`+`name`，带来源与 stale
  [`models.py:60`](../../app/models.py#L60)

- 手工价 `source=manual`，与自动刷新可区分
  [`quotes.py:162`](../../ledger/quotes.py#L162)

- 补代码只写 instruments，随后才能刷新
  [`quotes.py:121`](../../ledger/quotes.py#L121)

**同步刷新 API**

- 整表 2xx+errors；空 name 不当成整表；单票无代码 400
  [`main.py:205`](../../app/main.py#L205)

- 持仓投影：无价则市值/占比/浮盈亏为空，分母不含无价票
  [`quotes.py:54`](../../ledger/quotes.py#L54)

- 记账响应复用同一投影，刷新后记账不丢现价
  [`main.py:196`](../../app/main.py#L196)

**看仓**

- 过期标在现价旁；来源显示自动/手工
  [`PositionsView.vue:321`](../../web/src/views/PositionsView.vue#L321)

- 刷新按钮走整表；补代码与手工价另表单
  [`PositionsView.vue:275`](../../web/src/views/PositionsView.vue#L275)

**测试与依赖**

- Fake 覆盖成功、stale 清除、无代码、QUOTE_FAILED、手工价
  [`test_quotes.py:78`](../../tests/test_quotes.py#L78)

- 出站钉 `httpx2`；hatch 含 `market`
  [`pyproject.toml:12`](../../pyproject.toml#L12)
