---
title: '一次性迁入 YAML 并看到持仓'
type: 'feature'
created: '2026-08-28'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 工作台能打开，但库里没有成交，持仓页仍是空壳；作者还得对着 YAML 看仓。

**Approach:** 库中无成交时，从已配置 YAML 目录一次性迁入成交、现金、规则与名称↔代码；持仓由成交按名称聚合；设置页显示路径与是否已迁入。本故事不刷现价、不写初始目标、不手记买卖。

## Boundaries & Constraints

**Always:**
- 夹具仅 `tests/fixtures/portfolio/`；中文键见 addendum 四件套。路径来自 `PORTFOLIO_YAML_DIR`。
- `migrate` 只写成交、现金、规则、`instruments`；不写 `position_targets`、不写行情。
- 现金取 YAML `余额`，不从历史成交回放。库中已有成交则不再迁入。
- 持仓只读派生：按名称聚合股数与移动平均成本；股数 0 不出现。持久化方向 `buy`/`sell`（YAML「买」「卖」在迁入时映射）。
- `instruments` 是名称↔代码唯一权威；`market` 本故事不出现。
- 设置页可见迁入路径与「已迁入」/「未迁入」。持仓表至少：名称、股数、成本、现金。现价可空。
- 表按需 `create_all`：`trades` `cash` `rules` `instruments` `settings`。沿用 1.1 的 `users`、NullPool、`busy_timeout`、`127.0.0.1:8765`。
- 迁入失败须可见，不得标已迁入、不得留下半截成交。不读 Desk。

**Ask First:**
- 改 YAML 键名或四件套文件名。
- 本故事刷行情或写初始目标仓位。

**Never:**
- 读/抄 Desk；二次把库导回 YAML；可写 positions 表；用手记 API 完成本故事。
- 本故事实现记账、现价、目标、规则灯 UI、密钥、漏斗、锁。
- 对照 `docs/superpowers/` 验收。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 首次迁入 | 空成交；`PORTFOLIO_YAML_DIR` 指向夹具 | 成交/现金/规则/`instruments` 入库；现金=文件余额；持仓与成交聚合一致；设置「已迁入」 | 夹具须含买卖与「余额≠回放」 |
| 只迁一次 | 已迁入后再启动 | 成交笔数与现金不变 | 不得再 insert 同一批 |
| 已有成交 | 库中已有成交，即使 YAML 仍在 | 不迁入，账不变 | N/A |
| 未配置目录 | 空成交且未设 `PORTFOLIO_YAML_DIR` | 不迁入；持仓空；设置「未迁入」；进程仍可用 | 不得 500 伪装已迁入 |
| 坏 YAML / 半截失败 | 目录存在但解析或写入失败 | 不标已迁入；成交表仍空（或回滚） | 错误可读（信封 `error`/`code`/`detail` 或启动日志+API 状态） |

</frozen-after-approval>

## Code Map

沿用 1.1：`create_app` lifespan 在 `init_db`（`app/db.py:56` 种子 `users.id=1`）之后、yield 之前挂钩迁入。勿改 `__main__.py` 绑定与 `/api/health` 形状。Vue 无组件库。权威：`epic-1-context.md` AD-3/AD-9/AD-10。

- `app/models.py` -- 增加 `Trade` `Cash` `Rule` `Instrument` `Setting`；`trades.direction` 为 `buy`/`sell`
- `app/db.py` -- `create_all` 覆盖新表；保持 NullPool
- `ledger/positions.py` -- 只读聚合：名称、股数、均价成本总额；现金一行 `CNY`
- `migrate/yaml_portfolio.py` -- 读四件套；空成交才跑；事务提交后写 settings 已迁入
- `app/main.py` -- lifespan 调 migrate；`GET /api/positions`（派生表+现金）；`GET /api/settings`（路径+已迁入布尔）
- `web/src/views/PositionsView.vue` -- 拉 `/api/positions` 渲染表；无现价列也可
- `web/src/views/SettingsView.vue` -- 显示路径与已迁入/未迁入
- `pyproject.toml` -- 增加 `PyYAML==6.0.2`
- `tests/fixtures/portfolio/` -- `trades.yaml` `cash.yaml` `rules.yaml` `symbols.yaml`；余额故意对不上回放
- `tests/test_migrate.py` -- 笔数、现金=余额、聚合、只迁一次、已有成交跳过、未配置不迁、失败不标已迁入

只读勿改：`_bmad/`、`.agents/`、Desk、1.1 health/bind 断言除非被破坏。

## Tasks & Acceptance

**Execution:**
- [x] `app/models.py` `app/db.py` -- 迁入所需表 -- 账本与映射可落库
- [x] `migrate/yaml_portfolio.py` `ledger/positions.py` -- 一次性迁入 + 只读聚合 -- 现金不回放、持仓跟成交走
- [x] `app/main.py` -- 启动挂钩与 `GET /api/positions` `GET /api/settings` -- 工作台能读到账
- [x] `web/src/views/PositionsView.vue` `web/src/views/SettingsView.vue` -- 持仓表与迁入状态 -- 不再空壳
- [x] `tests/fixtures/portfolio/` `tests/test_migrate.py` `pyproject.toml` -- 夹具与覆盖矩阵 -- 不打真行情

**Acceptance Criteria:**
- Given 持仓页已加载迁入结果，when 查看，then 至少见名称、股数、成本与现金，不是原始 YAML/JSON 当主交互
- Given 设置页，when 迁入完成后查看，then 可见配置路径与「已迁入」
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Design Notes

- 聚合：买入加股数与成本总额；卖出按移动平均减少成本；方向 token 入库为英文。
- 启动：`init_db` → 若无成交则 `migrate`（目录来自环境变量）。测试给 `create_app` 注入 yaml 目录，避免写开发者本机路径。
- 无独立目标文件：初始目标留给 1.5（需现价）。规则入库但不做规则灯 UI。
- 失败回滚：同一事务写 trades/cash/rules/instruments，成功后再标 settings。

## Verification

**Commands:**
- `pytest tests/test_migrate.py tests/test_health.py` -- expected: 通过；含笔数、现金=余额、只迁一次
- 检查 `web/src/views/PositionsView.vue` -- expected: 表格列含名称/股数/成本，请求 `/api/positions`

**Manual checks (if no CLI):**
- 配置夹具目录后启动：持仓页有派生行与现金；设置页「已迁入」；再启动笔数不变

## Suggested Review Order

**启动挂钩**

- 种子用户之后才迁入；失败回滚并记下可读错误
  [`main.py:57`](../../app/main.py#L57)

**迁入事务**

- 空成交且未迁入才跑；四件套与已迁入标记同一 commit
  [`yaml_portfolio.py:38`](../../migrate/yaml_portfolio.py#L38)

- 现金取文件余额，方向映射为 `buy`/`sell`
  [`yaml_portfolio.py:121`](../../migrate/yaml_portfolio.py#L121)

**只读账本**

- 按名称聚合股数与移动平均成本总额
  [`positions.py:21`](../../ledger/positions.py#L21)

- 成交/现金/规则/映射/设置表，无写 positions
  [`models.py:17`](../../app/models.py#L17)

**API 与页面**

- 派生持仓 + 现金；迁入失败信封给看仓
  [`main.py:86`](../../app/main.py#L86)

- 路径优先读已存 settings，重启仍能看见已迁入
  [`main.py:110`](../../app/main.py#L110)

- 表格绑定 `data.positions`；空仓「暂无持仓」；列名成本总额
  [`PositionsView.vue:16`](../../web/src/views/PositionsView.vue#L16)

- 设置页绑定路径与 `data.migrated`
  [`SettingsView.vue:12`](../../web/src/views/SettingsView.vue#L12)

**夹具与测试**

- 余额故意不等于成交回放
  [`cash.yaml:1`](../../tests/fixtures/portfolio/cash.yaml#L1)

- 笔数、只迁一次、坏 YAML 回滚、无目录不迁
  [`test_migrate.py:48`](../../tests/test_migrate.py#L48)
