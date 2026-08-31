# Berkshire Plus 设计规格

- 状态：草稿，待用户确认后进入实现计划
- 日期：2026-08-27
- 产品：本机 Web 工作台，覆盖 A 股选股评估与持仓管理

## 1. 背景与目标

`berkshire-plus` 从空仓库实现。方法论来自本规格，以及 AI Berkshire 的研究技能（去劣、Checklist、组合审视的问题清单）。用户本机已有一份 YAML 成交账，仅作为**数据迁入源**。

### 实现禁区（硬约束）

**完全不要参考、阅读、复制或「对齐」任何 Desk / `desk/` 应用代码**（含其测试、前端、迁入脚本、行情封装、API）。实现者以本规格 + 本仓库测试为准。行为对错不拿另一套应用验。

第一版成功标准：用户能在本机浏览器里完成两条闭环，且不再依赖 Cursor 跑选股或组合审视。

1. 给出行业 / 指数 / 主题 → 得到少数几家公司的 Checklist + 一页决策备忘录。
2. 记录真实成交与目标仓位 → 得到单票 + 组合两层优化建议。
3. YAML 成交账迁入后，持仓股数/成本与 `trades.yaml` 聚合结果一致，现金与 `cash.yaml` 余额一致。

## 2. 非目标（v1 明确不做）

- 自动下单、券商同步、导入交割单以外的券商 API
- 港股、美股、多币种
- 云端部署、用户注册、登录、多租户隔离落地（只预留 `user_id`）
- 全 A 股定时扫描
- 用四大师完整深度报告作为漏斗默认评估
- 移动端、暗色主题专项、行情 K 线大盘

四大师研究可在 v1 末尾作为**手动加深**入口，不阻塞主路径。产物为四角色 + Team Lead 的 Markdown，`structured_json` 可空；不要求六关结构化字段。

## 3. 形态与技术选型

本机单进程 Web 应用：浏览器打开 `http://127.0.0.1:8765`。

| 层 | 选择 | 原因 |
|---|---|---|
| 后端 | Python 3.12+ / FastAPI / uvicorn | 财务计算、YAML 迁入、与现有工具链一致 |
| 数据库 | SQLite + 单一连接约定（写操作串行） | 单用户本机足够；文件可备份 |
| 前端 | Vue 3 + Vite，构建产物由 FastAPI 托管 | 产品级界面；开发时 Vite 代理 API |
| 行情 | 腾讯行情（现价）；东方财富公开接口（成分、财务） | 公开接口，无付费终端 |
| LLM | OpenAI 兼容 HTTP API（Key 在 `.env`） | 不绑定 Cursor；可用国内兼容网关 |
| 网页检索 | 可选 Tavily 或同类；未配置则评估仍可仅用抓取财务 | 缺搜索不得编造新闻 |

密钥只写 `.env`，不进数据库、不进 git。设置页可写入 Key 到 `.env` 并提示重启或热加载环境变量。

所有业务表带 `user_id`。v1 启动时确保存在 `users.id = 1` 的本地用户，所有读写都带该 id。不加登录。

## 4. 组件与目录

```
berkshire-plus/
  app/                 # FastAPI：路由、依赖注入
  ledger/              # 成交、现金、持仓派生、目标仓位、规则灯
  market/              # 代码解析、现价、成分股、财务拉取与缓存
  screen/              # 去劣、漏斗
  research/            # Checklist + 备忘录（及可选四大师）
  review/              # 组合审视
  jobs/                # 全局任务锁、后台线程
  migrate/             # YAML → SQLite
  web/                 # Vue 源码
  tests/
  data/app.db          # gitignore
  reports/             # Markdown 双写，gitignore 或只忽略生成物
```

组件边界：

- `ledger` 不调用 LLM，不拉行情；只消费已写入的 quotes。
- `market` 不写成交；失败时抛明确错误，由 API/jobs 转换成任务失败。
- `screen` 只做规则过滤与排序；不生成投资建议文案。
- `research` 只评估给定公司列表；不改持仓。
- `review` 读持仓 + 最近评估/漏斗结果，产出建议；不记账。

## 5. 数据模型

成交账是持仓的唯一真相源。不提供「直接改股数占比」的永久入口（一次性迁入除外）。

### 5.1 表

- `users`：id, name。v1 一行。
- `instruments`：code (PK), name, industry, sector, listed_year
- `trades`：id, user_id, date, direction (买/卖), name, shares, price, note
- `cash`：user_id 唯一一行, balance, currency='CNY', updated_at
- `quotes`：user_id + name, code, price, source, updated_at, stale
- `rules`：id, user_id, name, type, price, note（例：加仓上限价）
- `position_targets`：user_id + name（现金用保留名 `__CASH__`）, target_weight（0–1）
- `screen_runs`：id, user_id, kind, scope_type (industry|index|theme), scope_text, params_json, status, error, report_path, timestamps
- `screen_results`：run_id, code, name, verdict (keep|drop|insufficient), metrics_json, reason
- `research_jobs`：id, user_id, screen_run_id（可空，漏斗自动评估时关联）, kind (checklist_memo|four_masters), symbols_json, status, model, error, report_path, timestamps
- `research_artifacts`：job_id, role, markdown, structured_json, verdict
- `portfolio_reviews`：id, user_id, status, markdown, structured_json, report_path, error, timestamps
- `watchlist`：id, user_id, name, code, note, added_at；同一用户 name 唯一
- `settings`：user_id + key, value（model, web_search）
- `job_lock`：全局一行，kind, job_id, started_at
- `financials`：code + period + period_type 缓存东财拉取结果

### 5.2 不变量

- 持仓 = 对 `trades` 按名称聚合：买入增加股数与成本总额，卖出按移动平均成本减少；股数为 0 则剔除。
- 卖出股数 > 持仓 → 拒绝整笔，不改 `trades` 也不改 `cash`。
- 现金与成交同步、按成交额记账：
  - 买入：`cash.balance -= shares * price`；余额允许为负。
  - 卖出：`cash.balance += shares * price`。
  - 买入后余额 < 0 → **整笔仍写入** `trades` 与 `cash`，API 返回警告字段（例如「现金不足」）。不拦截、不截断股数。
- 迁入时现金以 `cash.yaml` 的余额为准，**不**用历史成交反推。迁入之后的每一笔买卖才按上式改余额。
- `position_targets` 含现金后合计必须等于 1.0（容差 1e-6）。保存时校验失败则拒绝。
- 未设置目标的已持仓：审视时标「无目标」，不参与偏差告警，但出现在单票体检里。
- 实盘占比 vs 目标偏差绝对值 > 3 个百分点 → 再平衡提示。

### 5.3 迁入

首次启动若库中无成交：读取 `PORTFOLIO_YAML_DIR` 下四个文件（路径可配，仓库内测试用 `tests/fixtures/portfolio/`）。

文件约定（迁入只认此结构，不读任何外部应用）：

```yaml
# trades.yaml
成交:
  - 日期: '2026-07-27'
    方向: 买          # 买 | 卖
    名称: 招商银行
    股数: 800
    价格: 35.25

# cash.yaml
余额: 100000.0

# rules.yaml
规则:
  - 名称: 招商银行
    类型: 加仓上限价
    价格: 32
    说明: 可选

# symbols.yaml
映射:
  招商银行: "600036.SH"
```

- `成交` 逐笔写入 `trades`；`余额` 写入 `cash.balance`（不按历史成交反推现金）。
- `映射` 写入 `instruments.code`；名称以成交里的「名称」为准。
- `规则` 写入 `rules`。
- 目标仓位：YAML 无独立目标文件时，用迁入后的**实盘占比**作为初始目标（含现金）。用户之后可改。
- 迁入只发生一次（meta 标记）。不把 SQLite 再写回 YAML。

## 6. 主流程 A：选股 → 评估

输入：范围类型 + 文本（行业如「银行」，指数如「沪深300」，主题如「创新药」）。

1. **解析成分**  
   东财公开接口取成分 + 行情快照（代码、名称、市值、PE、涨跌等）。解析失败 → 任务失败，不编名单。指数与主题必须解析到明确成分列表；失败不得降级为全市场扫描。

2. **去劣（quality）**  
   七条硬指标与三条豁免见附录 A（阈值写死在本规格，不依赖仓库外 skill）。每条结果为 pass / fail / insufficient。  
   - 任一条 **fail 且无豁免** → `drop`，必须写淘汰理由。  
   - 无红线 fail，且命中附录 A 的「整家 insufficient」规则 → `insufficient`（第三态，写入 `screen_results.verdict`）。  
   - 其余 → 进入漏斗候选（尚未 keep）。  
   `insufficient` 既不是通过也不是否决：出现在选股页「数据不足」表，**不得**进入 keep 池（最多 10 家），也**不得**进入终选 3 家。

3. **漏斗收窄到最多 10 家 keep**  
   只从去劣未 drop、未 insufficient 的集合里选。全序（前者优先）：市值降序 → 十年（或可得年份）ROE 均值降序 → OCF/NI 降序 → 已持仓同一 `industry` 的靠后。取排序后最多 10 家为 keep；不足 10 则全部 keep。其余候选标 `drop`，理由为「未进入本轮 keep 池」。

4. **终选 3 家（规则打标，不先跑 LLM）**  
   市值前/后 1/3：`floor(n/3)`，n=keep 数量；n<3 时不做分位，只按下面桶规则能打则打，否则全进中弹性再按序取。  
   每家 keep **只进一个角色桶**，按以下顺序判定（先命中先生效）：  
   1. **高确定性**：可得年份 ROE 均值 ≥ 15% **且** 市值位于 keep 池从大到小前 `floor(n/3)` 名。  
   2. **高弹性**：尚未入桶，且（市值位于 keep 池最后 `floor(n/3)` 名，**或** 快照含 PEG 且 PEG ≥ 1.5，**或** 东财行业名包含：有色、煤炭、航运、证券）。快照无 PEG 则忽略 PEG 支路。  
   3. **中弹性**：其余尚未入桶的 keep。  
   角色写入该行 `metrics_json.role` 与 `metrics_json.finalist`（是否进入终选 3 家）。  
   取桶顺序：高确定性 → 高弹性 → 中弹性。每桶取该桶内、按第 3 步排序最前、且**尚未入选**的 1 家。某桶为空或该桶公司都已入选，则从尚未入选的 keep 中按市值从大到小补 1 家。直到选出 3 个不同名字或 keep 用尽。keep 不足 3 家则有多少评多少，不凑数。  
   终选只从 keep 来。

5. **评估（默认自动，最多 3 家）**  
   对终选公司调用 LLM。整个「漏斗 + 可选评估」占用**一把**全局任务锁。先写 `screen_runs`；若 `evaluate=true`，再为每家创建 `research_jobs`，`params_json` / 任务表用 `screen_run_id` 关联（`research_jobs` 增加可空列 `screen_run_id`）。前端轮询 `screen_runs.id`；评估子任务在 `GET /api/screens/{id}` 中带出关联的 research job 列表。用户可设 `evaluate=false` 只筛名单。

6. **观察与买入**  
   通过或灰色地带可一键进观察池。买入必须手记成交，不从评估页直接改持仓（可预填记账表单）。

### 6.1 评估产物（结构化，必须可查询）

`structured_json` 至少包含：

- `verdict`: 通过 | 不通过 | 灰色地带
- `gates`: 六关每关 pass/fail/gray + 一句理由（能力圈、好生意、护城河、管理层、安全边际、决策纪律）
- `mirror_test`: 五句话；不完整则 verdict 不得为「通过」
- `price_bands`: 激进 / 稳健 / 保守 的价格区间或「观望」
- `memo`: 生意本质、主要风险、建议仓位角色（核心 / 卫星 / 不买）
- `data_grade`: A/B/C 信息丰富度
- `as_of`: 研究日期与所用股价

Markdown 全文同时写入 `research_artifacts.markdown` 和 `reports/`。

LLM 必须优先使用本次拉取的财务与行情；禁止编造数字。数据不足标灰色地带。未配置网页搜索时不得把「无搜索」写成「无财务」。

## 7. 主流程 B：持仓 → 优化

用户在持仓页点「审视」，创建 `portfolio_reviews` 任务。

1. 刷新持仓相关现价（失败则沿用缓存并标 stale）。
2. 派生每只标的：股数、成本、现价、市值、实盘占比、目标占比、偏差、规则灯、浮盈亏。
3. **单票体检**（每只持仓）：  
   - 若有最近 Checklist/备忘录则引用，否则基于财务快照 + LLM 做短体检（不是完整六关）。  
   - 必须回答：买入论文还在吗；今天此价格还会买吗。  
   - 动作：持有 / 加仓 / 减仓 / 卖出 + 理由。对照规则灯与目标仓位。
4. **组合层**：集中度、现金过多/过少、实盘 vs 目标、最近一次漏斗 keep 名单是否适合用现金或换仓买入（只考虑 Checklist 为通过或灰色地带的标的）。
5. 输出：每票一条动作 + **不超过三条**组合级具体建议。不自动下单。

`structured_json` 含 `positions[]`（name, action, reason, actual_weight, target_weight）与 `portfolio_actions[]`（最多 3 条）。

缺行情或 LLM 失败的标的：action = 无法判断，不得编造。

## 8. 数据流

```
YAML（一次性） → migrate → SQLite
用户记账 / 改目标 → ledger → SQLite
刷新行情 → market.quotes → SQLite
选股 → market.universe → screen → research → SQLite + reports/
审视 → ledger 快照 + quotes + 最近 research → review → SQLite + reports/
Vue 只通过 REST 读写，不直连 SQLite
```

REST 以资源为主，至少包括：

- `GET /api/health`（含任务锁）
- `GET/POST /api/overview` 相关：总览、记账、改目标、规则、刷新行情、手工价
- `POST /api/screens/funnel`（scope_type, scope_text, evaluate: bool）
- `POST /api/research`（checklist_memo | four_masters）
- `POST /api/reviews`
- `GET /api/jobs`、`GET /api/jobs/{id}`
- watchlist CRUD
- settings

重任务：`sync` 默认为 false（立即返回 job id，前端轮询）。测试可 sync=true。全局 `job_lock`：第二任务 409。

## 9. 界面

中文。页面：工作台、持仓、选股、评估、设置。

- 工作台：锁状态、最近任务、观察池待办。
- 持仓：表（名称、股数、成本、现价、市值、实盘%、目标%、偏差、规则灯、浮盈亏）、现金、记账表单、目标编辑、一键审视、最近审视摘要。
- 选股：范围类型 + 文本、进度；三张表 keep / drop / insufficient（含理由）；终选 3 家评估入口/结果。禁止把原始 snapshot JSON 当作主交互。
- 评估：渲染 Markdown + 结构化结论条（verdict、价格带、镜子测试）。
- 设置：模型、网页搜索开关、API Key、YAML 迁入路径（只读显示是否已迁入）。

v1 不做路由级权限。不做图表库。

## 10. 失败处理

| 情况 | 行为 |
|---|---|
| 任务锁占用 | 409，告知当前 kind |
| 行情失败 | 保留旧价，stale=true；审视降置信 |
| 成分/财务接口失败 | 任务 failed，error 原文可展示 |
| 无 LLM Key | 评估/审视 400，指向设置页 |
| LLM 超时/空响应 | 该标的「无法判断」 |
| 卖超 | 400，整笔不写 |
| 目标合计 ≠ 1 | 400 |
| 代码无法解析 | 400，提示填写如 `601166.SH` |

## 11. 测试

不依赖真实行情和真实 LLM。

必须覆盖：

- 账本：买、卖、均价、卖超、现金警告、目标合计、偏差 3%
- 去劣 / 漏斗：固定 fixtures，断言 `drop` / `insufficient` / `keep` 集合、淘汰理由，以及终选 3 家不重复
- 迁入：用仓库内最小 YAML 夹具，断言成交笔数、现金、初始目标合计 1.0
- API：记账、锁冲突 409、无 Key 时评估 400
- research/review：Fake LLM 返回固定 JSON/Markdown，断言结构化字段入库

可选：对 `market` 解析函数做纯函数测试；网络客户端 mock。

## 12. 对照与风险

实现与验收只对照**本规格**。方法论可参考 AI Berkshire 技能文档中的问题清单（七条去劣、六关 Checklist、组合审视要问的问题），阈值与流程以本规格附录和正文为准。禁止打开或对照任何 Desk 实现。

风险：东财/腾讯接口变更会导致选股失败——产品应失败可见，而不是静默空仓。LLM 质量随模型变化——结构化字段 + 镜子测试用于约束输出，不保证投资正确。本产品不构成投资建议，界面页脚需免责声明。

实现时按阶段拆计划，不要当成一个不可分的里程碑：① 账本 + YAML 迁入 + 目标仓位；② 行情/财务 + 去劣 + 漏斗；③ Checklist 备忘录 + 组合审视；④ Vue 页面把闭环接上。

## 附录 A：去劣指标（v1 写死）

排除条件（命中且无豁免 → drop）：

| # | 指标 | 排除 |
|---|---|---|
| 1 | 10 年平均 ROE | < 8% |
| 2 | 5 年累计自由现金流 | 为负 |
| 3 | 利息覆盖 EBIT/利息 | < 2；银行/保险跳过本条 |
| 4 | 近 5 年平均毛利率 | < 15% |
| 5 | 5 年均值 经营现金流/净利润 | < 0.7 |
| 6 | 近 10 年平均净利率 | < 5% |
| 7 | 5 年总股本膨胀（非并购） | > 20% |

豁免：

- A（仅第 1 条）：上市不足 10 年，且近 5 年（或可得年份）平均毛利率 > 30%，且最近 2 年经营现金流为正。
- B（仅第 6 条）：近 5 年（或可得年份）平均毛利率 > 30%，且近 2 年净利率已 ≥ 5% 或明确上升。
- C（第 4、6 条）：可得年份 ROE 均值 > 20%，且 OCF/NI > 1.0，且商业模式为高周转薄利/会员/平台佣金（`business_type` 或行业标注；无法判断则不给本豁免）。

**窗口不足时怎么算（唯一规则）**：需要 N 年时，用「上市以来可得、至多 N 年」的样本计算均值或累计，**不因年数不够就把该条标 insufficient**。该条为 insufficient 仅当该指标样本年为 0（完全没有该字段）。这样上市 3–6 年的公司仍可触发豁免 A（第 1 条用可得年份均值，若 < 8% 为 fail，再看豁免）。

FCF = 经营现金流 − 资本开支。第 7 条无法识别「是否并购」时：本条标 insufficient（不因此 drop）。

成分快照字段至少包括：code, name, mcap, pe, industry, 以及可选 peg。

**整家 `insufficient`（唯一规则）**：缺以下**任一项** → 该公司 verdict=`insufficient`，不进漏斗候选：可解析的 A 股代码、市值、至少 1 年 ROE、至少 1 年净利润。

单条指标缺数据（样本年 = 0）：只把该条标为 insufficient。单条 insufficient **不会**单独把整家打成第三态（除非同时命中上一句的整家规则）。无红线 fail、又不是整家 insufficient → 进入漏斗候选。
