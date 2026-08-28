---
title: '打开本机工作台'
type: 'feature'
created: '2026-08-28'
status: 'done'
baseline_commit: 'NO_VCS'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 仓库还没有可运行的工作台。作者无法在本机浏览器打开看仓/选股入口，仍要先开 Cursor 才能开始每周流程。

**Approach:** 搭起本机单进程：FastAPI 绑定 `127.0.0.1:8765`，托管（开发时由 Vite 代理）Vue 工作台；无登录；启动写入 `users.id=1`；中文双入口与免责声明落地。本故事只交付「打得开」，不迁入 YAML、不记账、不刷行情。

## Boundaries & Constraints

**Always:**
- 生产形态一个 uvicorn，只绑 `127.0.0.1:8765`；`GET /api/health` 可用。
- 启动确保 `users.id=1`；所有后续读写带该 `user_id`。
- 前端 `web/`：create-vue 3.23.0，TypeScript + Vue Router，不上 Pinia；不要 `--vue-rc`。开发时 Vite 把 `/api` 代理到 8765。
- 无登录/注册页。主路径是页面与入口，不是对话窗口。
- 文案中文。主页面（`/`、`/positions`、`/screen`、`/settings`）可见「不构成投资建议」。
- `/positions` 与 `/screen` 入口对等可达；选股页可为空壳，但必须能进。
- 栈钉死：Python `>=3.12,<3.15`，FastAPI 0.141.1，uvicorn 0.52.4，Pydantic 2.13.4，SQLAlchemy 2.0.52；SQLite `data/app.db`，v1 `create_all`，写串行。
- 验收对照本规格与架构脊柱，不对照 Desk。

**Ask First:**
- 若要把进程绑到非 loopback、加登录、或引入 Pinia / 第二个状态库。
- 若要在本故事创建成交/持仓/行情表或跑 YAML 迁入。

**Never:**
- 阅读、复制或对齐 Desk / `desk/` 代码。
- 监听 `0.0.0.0`；云部署；Electron/Tauri。
- 本故事实现迁入、记账、现价、目标仓位、规则灯、密钥写入、漏斗、评估、审视、重任务锁。
- 对话流端点作为看仓/选股主交互；「今日推荐」或收益排行。
- Alembic、官方 openai SDK、httpx（用 httpx2 留给后续故事，本故事可不发出站 HTTP）。
- 把 `docs/superpowers/` 当实现权威。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 打开工作台 | 依赖已装，uvicorn 绑 `127.0.0.1:8765` | 浏览器打开该地址见中文工作台；看仓、选股入口可见；无登录页 | 绑定失败则进程起不来，不得改绑 `0.0.0.0` |
| health | `GET /api/health` | 2xx；正文可被前端/探活使用；可含未占用锁占位，避免日后另起端点 | 非 2xx 视为未就绪 |
| 种子用户 | 空库首次启动 | `users` 表存在且 `id=1` 一行 | 重复启动不插入第二行、不改 id |
| 空壳选股 | 未跑漏斗，打开 `/screen` | 页面可达、中文、有免责；不编名单 | 不得因无数据跳去登录或 500 |
| 禁绑 | 启动参数/配置 | 进程不监听 `0.0.0.0` | 若误配为 `0.0.0.0` 视为本故事失败 |

</frozen-after-approval>

## Code Map

绿田：无应用代码。不要对照 Desk。权威：`epic-1-context.md`、脊柱 AD-1/AD-8/AD-10。本故事只建 `users` 表。

- `pyproject.toml` -- 钉 FastAPI 0.141.1 / uvicorn 0.52.4 / Pydantic 2.13.4 / SQLAlchemy 2.0.52；加 pytest
- `.gitignore` -- 忽略 `.env`、`data/app.db`、`web/node_modules`、`web/dist`、`__pycache__`
- `app/db.py` -- 单一 engine → `data/app.db`；`create_all`；`busy_timeout` 串行写；勿用 StaticPool 当文件库默认
- `app/models.py` -- 仅 `users(id, name)`
- `app/main.py` -- lifespan 建库并种子 `users.id=1`；`GET /api/health`；若存在则托管 `web/dist`；无对话端点
- `web/` -- create-vue（TS、Router、无 Pinia）；路由 `/` `/positions` `/screen` `/settings`；双入口 + 各页免责
- `web/vite.config.ts` -- `/api` → `http://127.0.0.1:8765`
- `tests/test_health.py` -- health 2xx、种子用户、再启动仍 id=1

只读勿改：`_bmad/`、`.agents/`、规划文档。`docs/superpowers/` 非验收基准。

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml` `.gitignore` -- 钉依赖与忽略规则 -- 可安装、密钥和库文件不进 git
- [x] `app/db.py` `app/models.py` `app/main.py` -- 单进程 API：health、建库、种子 `users.id=1` -- 满足探活与本机身份
- [x] `web/` -- create-vue 脚手架 + 四条路由 + 中文双入口与免责 -- 浏览器打得开，选股入口对等
- [x] `web/vite.config.ts` -- `/api` 代理到 8765 -- 开发时前端能打到 FastAPI
- [x] `tests/test_health.py` -- 覆盖 I/O 矩阵中的 health 与种子用户 -- 不依赖真行情/真模型

**Acceptance Criteria:**
- Given 任一主页面（`/` `/positions` `/screen` `/settings`），when 查看，then 可见「不构成投资建议」
- Given 前端目录，when 检查脚手架，then 位于 `web/`，TypeScript + Vue Router，无 Pinia，无 `--vue-rc`
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk 代码

## Design Notes

- 组件库本故事定为无第三方库：语义 HTML + 一份 CSS；后续 web 故事复用。
- health JSON 至少 `status: ok`；预留 `lock: null`（Epic 2 填充，不改路径）。
- 绑定靠启动参数 `127.0.0.1`，代码勿写 `0.0.0.0`。create-vue 非交互落到 `web/`；Node `^22.18` 或 `>=24.12`。有 `web/dist` 则 FastAPI 托管；开发可 Vite+代理，health 仍走 uvicorn。

## Verification

**Commands:**
- `pytest tests/test_health.py` -- expected: 通过；断言 health 2xx 与 `users.id=1`
- 检查 `web/package.json` / `web/src/router` -- expected: 有 vue-router，无 pinia 依赖
- 检查 `app/main.py` 与启动脚本 -- expected: host 为 `127.0.0.1`，无 `0.0.0.0`

**Manual checks (if no CLI):**
- 浏览器打开 `http://127.0.0.1:8765`（或 Vite 开发地址）：中文工作台、看仓/选股可点、各主页免责、无登录

## Suggested Review Order

**进程与探活**

- 唯一入口：只绑 loopback 8765，不暴露局域网
  [`__main__.py:5`](../../app/__main__.py#L5)

- 应用工厂：lifespan 建库、种子用户、关闭时 dispose
  [`main.py:21`](../../app/main.py#L21)

- 探活契约：`status: ok`，锁占位留给 Epic 2
  [`main.py:37`](../../app/main.py#L37)

**静态托管与 API 边界**

- 未知 `/api` 与缺静态文件走信封 404，不回落 SPA
  [`main.py:47`](../../app/main.py#L47)

**本机身份与库**

- 文件 SQLite 用 NullPool + busy_timeout，避免 StaticPool
  [`db.py:30`](../../app/db.py#L30)

- 空库只插入 `users.id=1`，再启动不重复
  [`db.py:47`](../../app/db.py#L47)

**中文工作台**

- 四条主路由对等，未知路径中文未找到页
  [`index.ts:8`](../../web/src/router/index.ts#L8)

- 布局页脚免责；首页链接仅精确 `/` 时高亮
  [`App.vue:10`](../../web/src/App.vue#L10)

- 看仓/选股双入口，无登录话术
  [`HomeView.vue:8`](../../web/src/views/HomeView.vue#L8)

- 开发时 `/api` 代理到本机 8765
  [`vite.config.ts:17`](../../web/vite.config.ts#L17)

**测试与工程**

- health、种子、缺 dist、默认库路径、托管真实静态文件
  [`test_health.py:19`](../../tests/test_health.py#L19)

- 钉死后端版本；测试依赖 httpx
  [`pyproject.toml:6`](../../pyproject.toml#L6)

- 忽略 `.env` 与 `data/app.db`
  [`.gitignore:1`](../../.gitignore#L1)
