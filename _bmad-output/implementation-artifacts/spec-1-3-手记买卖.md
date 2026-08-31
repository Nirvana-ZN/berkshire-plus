---
title: '手记买卖'
type: 'feature'
created: '2026-08-28'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 迁入后账只能看，不能记真实买卖；没有入口把成交写成唯一真相，作者仍可能想直接改股数。

**Approach:** 在持仓页手记一笔成交（日期、方向、名称、股数、价格、可选说明）。只经 `ledger` 写 `trades` 并改现金；持仓立刻按现有聚合刷新。卖超整笔拒绝；现金不足仍写入并警告。

## Boundaries & Constraints

**Always:**
- 写成交/现金只走 `ledger`，禁止可写 positions、禁止前端直接改股数。
- API 方向 `buy`/`sell`；界面文案买/卖。日期 ISO `YYYY-MM-DD`。股数、价格用 Decimal。
- 买入：该名称股数与成本总额增加，现金减少「股数×价格」。卖出：按移动平均减成本，现金增加同等金额。v1 无佣金印花税。
- 卖出股数 > 当前该名称持仓：HTTP 400，信封 `error`/`code`/`detail`；成交与现金不变。
- 买入后现金为负：整笔 2xx 写入，JSON 带可感知 `warning`（如「现金不足」），不截断股数。
- 卖出后股数为 0：该名称从持仓表消失。
- 复用 `load_positions`；不刷现价、不改目标、不改规则。
- 不读 Desk。沿用 1.1/1.2 绑定、health、迁入。

**Ask First:**
- 本故事加佣金/印花税或允许直接改股数。

**Never:**
- 迁入路径兼做日常记账；`migrate` 直接 `session.add(Trade)` 作为手记入口。
- 本故事实现现价、目标、规则灯、密钥、漏斗。
- 对照 Desk 或 `docs/superpowers/` 验收。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 合法买入 | 空仓或已有仓；买 名称/股数/价格 | 成交入库；股数与成本增加；现金 − 股数×价格；`GET /api/positions` 立刻一致 | 缺字段/非正股数/负价格/非法日期 → 400 信封 |
| 合法卖出 | 持仓 N≥卖出股数 | 现金 + 股数×价格；均价成本减少 | N/A |
| 卖超 | 持仓 N，卖出 > N | 400；trades 与 cash 不变 | `code` 固定可测（如 `SELL_EXCEEDS`） |
| 现金不足仍买 | 买入后余额将为负 | 2xx 写入；`warning` 可读；股数不截断 | 不得用 400 拦截 |
| 卖光 | 卖出后该名称股数 0 | 持仓表不再出现该名称 | N/A |

</frozen-after-approval>

## Code Map

沿用 1.2：`load_positions`（`ledger/positions.py:21`）是唯一聚合；`GET /api/positions`（`app/main.py:86`）；`Trade`/`Cash`（`app/models.py:17`）。持仓页现为只读表（`PositionsView.vue:16`）。迁入仍走 `migrate_yaml_portfolio`，不要把它当手记。

- `ledger/trades.py` -- 新增 `record_trade`：校验、卖超拒绝、写 Trade、改 Cash（无行则当 0）、同一事务；返回持仓快照 + 可选 warning
- `app/main.py` -- `POST /api/trades`；body：`date` `direction` `name` `shares` `price` 可选 `note`；卖超 400；成功 2xx + 当前 positions/cash，现金不足时加 `warning`
- `web/src/views/PositionsView.vue` -- 手记表单（买/卖）；提交后刷新表；无改股数字段
- `tests/test_trades.py` -- 买、卖、均价、卖超不变账、现金不足 warning、卖光消失；不打行情。迁入与 health 原测试须仍过

只读勿改：`_bmad/`、`.agents/`、Desk、`__main__.py` 绑定、health 形状。

## Tasks & Acceptance

**Execution:**
- [x] `ledger/trades.py` -- `record_trade` 写账与现金 -- 日常成交唯一写入口
- [x] `app/main.py` -- `POST /api/trades` -- 卖超 400、现金不足 2xx+warning
- [x] `web/src/views/PositionsView.vue` -- 买/卖表单并刷新持仓 -- 立刻看见账变
- [x] `tests/test_trades.py` -- 覆盖 I/O 矩阵 -- 不依赖真行情

**Acceptance Criteria:**
- Given 持仓页，when 查看，then 没有直接改股数的输入
- Given 界面提交，when 选择方向，then 用户看到买/卖，请求体为 `buy`/`sell`
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Design Notes

- 卖超在写入前用 `load_positions`（或同一套 lots）判断当前股数，拒绝则不 `add`。
- 现金行不存在时按 0 处理再写入（空仓首次买入）。
- 成功响应可含与 `GET /api/positions` 同形的 `positions`/`cash`，便于前端少打一轮。
- 均价断言：买两笔不同价再部分卖，成本总额按移动平均。

## Verification

**Commands:**
- `pytest tests/test_trades.py tests/test_migrate.py tests/test_health.py` -- expected: 通过；含买/卖/均价/卖超/现金警告
- 检查 `PositionsView.vue` -- expected: 买/卖表单，无「股数」可编辑持仓列

**Manual checks (if no CLI):**
- 持仓页记一笔买、一笔卖：表与现金立刻变；卖超看到拒绝原因；现金不足仍成交并有警告

## Suggested Review Order

**账本写入**

- 日常成交唯一入口：校验后写 Trade、改 Cash、卖超先拒绝
  [`trades.py:33`](../../ledger/trades.py#L33)

- 卖超 detail 含持有与卖出股数；非有限数字拒绝
  [`trades.py:56`](../../ledger/trades.py#L56)

**API**

- `POST /api/trades`：卖超 400 `SELL_EXCEEDS`；现金不足 2xx + `warning`
  [`main.py:117`](../../app/main.py#L117)

**持仓页**

- 买/卖表单提交 `buy`/`sell`；持仓表股数只读
  [`PositionsView.vue:62`](../../web/src/views/PositionsView.vue#L62)

- 成功记账刷新表并不清掉迁入失败横幅
  [`PositionsView.vue:35`](../../web/src/views/PositionsView.vue#L35)

**测试**

- 买/卖/均价/卖超/现金警告/空仓卖/字符串股数
  [`test_trades.py:63`](../../tests/test_trades.py#L63)
