# Berkshire Plus

[English](README.en.md)

本机 A 股看仓与选股工作台。浏览器打开即可，无登录，只监听本机，不构成投资建议。

克隆下来的是空账本：没有持仓、没有密钥、没有已构建的页面。按下面的顺序做完，就能在浏览器里使用。

## 准备

安装这两样：

- [uv](https://docs.astral.sh/uv/getting-started/installation/)。它会按项目要求准备 Python 3.12–3.14，不用单独装 Python。
- Node.js **22.18 及以上的 22.x**，或 **24.12 及以上**。Node 20 和 23 不符合前端要求。

命令在 PowerShell 和 Git Bash 里相同。下面都在仓库根目录执行。

## 安装并启动

```powershell
git clone https://github.com/Nirvana-ZN/berkshire-plus.git
cd berkshire-plus
cd web
npm install
npm run build
cd ..
uv run berkshire-plus
```

浏览器打开 http://127.0.0.1:8765 。这个地址只在运行命令的那台电脑上能打开。

结束时在该终端按 Ctrl+C。

页面若显示「前端尚未构建」，说明跳过了 `npm run build`。回到 `web` 目录建好再启动。`web/dist` 不进版本库，每台机器都要构建一次。

同一时间只能跑一个后台任务。页眉会显示正在运行的是选股漏斗、个股评估、组合审视、每周发现还是全市场抓取。

## 第一次使用

空库可以直接用，不必准备 YAML。

1. **看仓** http://127.0.0.1:8765/positions  
   打开「记账（买 / 卖）」，填写日期、方向、名称、股数和价格，点「记账」。名称可检索，也可直接填写。已有持仓时，名称旁的「记买入」「记卖出」会把表单预填好。

2. **设置** http://127.0.0.1:8765/settings  
   漏斗筛名单不需要密钥。评估、一键审视需要 OpenAI 兼容密钥，见下一节。

3. **选股** http://127.0.0.1:8765/screen  
   选择行业、指数或主题，点「开始筛选」。勾选「评估终选」，或使用「个股直接评估」，才会调用模型。

4. **每周发现** http://127.0.0.1:8765/discover  
   抓取完成本身不授予 discovery-v8 执行权限。需保留已审核来源，完成 schema4 冻结抓取，并按下方的数据就绪与发布流程操作。抓取从东方财富拉取市场数据，耗时很长，期间不要关掉运行工作台的终端。

5. **指数基金** http://127.0.0.1:8765/index-funds  
   点「刷新数据」。每个指数给出一只代表基金的低估值筛选结果。

行情在使用时拉取，不随仓库提供。

## 模型密钥

工作台启动后打开 http://127.0.0.1:8765/settings ，在「大模型接口配置」里填写后点「保存设置」：

- **API 密钥**：评估和审视必填。写入 `.env` 的 `OPENAI_API_KEY`。
- **接口地址**：兼容接口的根地址，末尾不要带 `/chat/completions`。留空则使用 `https://api.openai.com/v1`。智谱 Coding Plan 用 `https://open.bigmodel.cn/api/coding/paas/v4`，按量计费用 `https://open.bigmodel.cn/api/paas/v4`。
- **模型**：可选。留空则使用 `gpt-4o-mini`。

保存后密钥和接口地址立即生效，不用重启。页面只显示「已保存」，输入框留空再保存不会清掉已有密钥。

网页搜索是可选项。要在评估前检索中报、分红和股权结构，在同一页填写 Tavily 密钥并勾选「网页搜索」。没有这把密钥时，勾选也不会发起搜索。财务数字仍以已抓取的年报为准。

密钥写在项目根目录 `.env`，不进数据库、不进 git。也可以手工创建这个文件，改完后需要重新启动工作台：

```
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
TAVILY_API_KEY=tvly-...
```

## 迁入已有 YAML 账本（可选）

只有在第一次启动、库里还没有任何成交时才会迁入。四个文件必须放在同一目录：`trades.yaml`、`cash.yaml`、`rules.yaml`、`symbols.yaml`。

PowerShell：

```powershell
$env:PORTFOLIO_YAML_DIR = "D:\path\to\your-yaml"
uv run berkshire-plus
```

Git Bash：

```bash
export PORTFOLIO_YAML_DIR="/d/path/to/your-yaml"
uv run berkshire-plus
```

迁入只发生一次。改了 YAML 想重新迁入，先停掉工作台，删除 `data/app.db`，再带同一个环境变量启动。删除数据库会同时清掉已经记在里面的成交、发现结果和设置里的模型名称。

没有这四个文件就跳过本节，在看仓页记账。

## 数据文件

| 路径 | 用途 |
|------|------|
| `data/app.db` | SQLite 账本与产物。首次启动自动创建 |
| `.env` | 模型密钥与接口地址 |
| `reports/` | 评估与审视 Markdown |
| `web/dist` | 前端构建结果 |

这些都不进版本库。换一台电脑要重新构建前端，并重新记账或迁入 YAML。不要把另一台机器上数 GB 的数据库备份拷进仓库。

## 已有库升级

当前可执行构建是 `discovery-v8`，v7退休后仅保留历史读取。已有批次、尺子、来源与跟踪配置保持原字节；新启动不清空旧财务缓存，不替旧来源补可信标签。数据库 schema 8 的连接能力守卫会拒绝旧程序写入；未知 schema 仅尝试兼容读取，写接口返回 `schema_read_only`。

v8使用分支内归一化排名、经济稀释、扣非TTM/PEG、可用现金和可追溯的总投入资本回报率。应收、合同资产与受限资金信号保留来源和缺失状态，作为影子信号；三轨通过代表研究资格。研究等级、价格带和建议后的共同经济风险暴露由确定性代码约束。

**生产尚未就绪。** 已保留的[来源核验记录](docs/discovery-calibration/selection-evidence-source-audit.md)和原始归档是局部抽查，不是全市场或历史时点完整性证明。三个v8校准产物仍是无观测、计数为空的合约假设。软件验收、数据源就绪、生产启用和期限结果成熟分别判断；本次实现没有对生产库执行发布。

准备独立的新schema4填充，并按[发布与来源准备流程](docs/stock-selection-strategies.md#9-selection-v8发布与历史边界)保留经复核的完整证券状态清单、披露日历、原文和财务来源。只读检查：

```powershell
uv run python -m scripts.release_selection_v8 --db <数据库绝对路径> --fill-id <新填充ID> --dry-run
```

发布必须通过共同字段95%、每个适用新指标/来源90%及每个非空行业的相同最低覆盖；候选数量不作门槛。明确执行 `--apply --reason <理由> --backup-dir <备份目录绝对路径>` 才建立经完整性、外键、全部实际表计数和SHA256校验的SQLite一致备份，并在一个事务追加三轨尺子与发布回执。默认报告不写库、不创建备份目录。回退使用 `--rollback-read-only --reason <理由> --backup-dir <绝对路径>` 追加只读回执，保留历史，不启用旧构建重算。

纸面跟踪包含分红、停牌、无法成交、成本和基准来源缺口；未到期收益保持空值，不外推年化。支持1992-01-01起的市场、公告与交易日窗口，早期上市日期仍可作为元数据；更早的历史时区窗口明确不支持。合成测试的成熟结果不代表真实策略已有成熟回报。
