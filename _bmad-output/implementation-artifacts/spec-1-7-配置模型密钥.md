---
title: '配置模型密钥'
type: 'feature'
created: '2026-08-29'
status: 'done'
baseline_commit: 'e0345722cfcee3effdee67feaf6df424e6461c50'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 设置页只能看迁入状态，不能写入评估密钥与模型；选股/审视以后仍要靠 Cursor 配环境。

**Approach:** 设置页保存 API Key 到本机 `.env`（不进业务表、不进 git），模型名与是否网页搜索进 `settings` 表。保存后热加载当前进程并提示。本故事不调用 LLM，无密钥 400 留给 Epic 2。

## Boundaries & Constraints

**Always:**
- 密钥只写项目根 `.env` 的 `OPENAI_API_KEY`。禁止写入 `settings`、其它业务表、响应 JSON 明文。GET `/api/settings` 只用 `has_api_key` 布尔，不回传密钥。
- 模型名 `llm_model`、网页搜索 `web_search`（`true`/`false`）进 `settings` 表。迁入键 `yaml_migrated` / `portfolio_yaml_dir` 不改语义。
- `.env` 与 `data/app.db` 已在 `.gitignore`；测试不得把密钥写进仓库内真实 `.env`。
- 保存成功：把密钥热加载进当前进程 `os.environ`，响应/界面提示「已热加载」（可兼提重启）。不绑 `0.0.0.0`。OpenAI 兼容，不引入官方 `openai` SDK。
- 缺密钥字段：不改 `.env`。密钥为空或空白 → HTTP 400 信封。测试不打真模型。

**Ask First:**
- 把密钥写入 SQLite；保存时用真 LLM 校验密钥。

**Never:**
- 调用 LLM、跑漏斗/评估/审视、实现无密钥 400。
- 读 Desk。把密钥打进日志或 `GET` 体。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 保存密钥+模型 | POST key、模型名、搜索开关 | `.env` 含 `OPENAI_API_KEY`；`settings` 有模型与搜索；`has_api_key=true`；提示已热加载；`os.environ` 已更新 | 业务表无密钥明文 |
| 空密钥 | 密钥字段为空或空白 | HTTP 400；`.env` 与表不变 | 信封 `error`/`code`/`detail` |
| GET 不泄密 | 已保存密钥 | JSON 无密钥字符串；仅 `has_api_key` | 无 |
| 只改模型 | POST 无 key 字段 | `.env` 不动；模型写入 `settings` | 无 |
| gitignore | 查仓库忽略规则 | `.env` 与 `data/app.db` 被忽略 | 无 |

</frozen-after-approval>

## Code Map

沿用：`GET /api/settings`（`app/main.py:573-586`，仅 `yaml_dir`/`migrated`）、`Setting`（`app/models.py:80-84`）、`SettingsView.vue`（只读迁入）、`.gitignore:1-2`。无 `.env` 读写、无密钥表单。`create_app` 无 `env_path`。

- `app/envfile.py` 或 `app/settings.py` -- 读写项目 `.env` 单键、热加载 `os.environ`；测试可注入路径
- `app/main.py` -- 扩展 GET；`POST /api/settings`；`create_app(env_path=)`
- `web/src/views/SettingsView.vue` -- 密钥（不回显明文）、模型名、搜索开关、保存与热加载提示；保留迁入状态
- `tests/test_settings.py` -- 矩阵：写 tmp `.env`、表无密钥、GET 不泄密、空密钥 400、gitignore；回归 health/migrate
- `.gitignore` -- 确认 `.env`、`data/app.db`（已有则只测不改）

只读勿改：Desk、`record_trade`、目标/规则灯、migrate 四件套、`__main__.py` 绑定、health `lock`。

## Tasks & Acceptance

**Execution:**
- [x] `app/envfile.py` `app/main.py` -- `.env` 与 POST/GET -- 密钥不进表、可热加载
- [x] `web/src/views/SettingsView.vue` -- 表单与提示 -- 不回显明文密钥
- [x] `tests/test_settings.py` -- 矩阵 -- 写临时文件，不打真模型

**Acceptance Criteria:**
- Given 保存成功，when 查 `settings` 表与 GET 体，then 无密钥明文
- Given 查看 `.gitignore`，when 对照，then 含 `.env` 与 `data/app.db`
- Given 实现过程，when 对照来源，then 未阅读或复制 Desk

## Spec Change Log

## Design Notes

- 不新增 `python-dotenv` / `openai` 依赖；自行读写 `KEY=value` 行。
- 密钥输入 `type=password`；已配置只显示「已保存」，不填占位星号当密钥。
- `web_search` 默认 `false`。模型名可空（表示尚未选）。
- POST 体：`api_key`（可选）、`model`、`web_search`。

## Verification

**Commands:**
- `pytest tests/test_settings.py tests/test_health.py tests/test_migrate.py tests/test_rules.py tests/test_targets.py tests/test_quotes.py tests/test_trades.py` -- expected: 通过；含不泄密、400、gitignore
- 检查 `settings` 表与 GET JSON 无 `OPENAI_API_KEY` 值 -- expected: 无

**Manual checks (if no CLI):**
- 设置页能保存密钥并看到已热加载；刷新后仍是已保存，看不到原文

## Suggested Review Order

**密钥隔离**

- GET 只用 `has_api_key`，密钥不进 JSON
  [`main.py:177`](../../app/main.py#L177)

- 缺 `api_key` 字段不改 `.env`；空或空白返回 400 信封
  [`main.py:644`](../../app/main.py#L644)

- 先提交模型与搜索开关，再写 `.env` 并热加载
  [`main.py:681`](../../app/main.py#L681)

- 单键读写项目 `.env`，保留其它行
  [`envfile.py:39`](../../app/envfile.py#L39)

- 启动时把已有密钥热加载进当前进程
  [`main.py:207`](../../app/main.py#L207)

**设置表**

- 模型名与网页搜索进 `settings`，不改迁入键语义
  [`main.py:674`](../../app/main.py#L674)

**设置页**

- 密码框；留空则不提交 `api_key`
  [`SettingsView.vue:50`](../../web/src/views/SettingsView.vue#L50)

- 加载完成前不渲染表单，避免空值覆盖
  [`SettingsView.vue:88`](../../web/src/views/SettingsView.vue#L88)

- 成功提示用接口 `message`（含「已热加载」）；已配置只显示「已保存」
  [`SettingsView.vue:65`](../../web/src/views/SettingsView.vue#L65)

**测试**

- 写临时 `.env`、表与 GET 无密钥、空密钥 400、gitignore、不碰仓库真实 `.env`
  [`test_settings.py:57`](../../tests/test_settings.py#L57)
