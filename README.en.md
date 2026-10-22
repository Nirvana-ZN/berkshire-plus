# Berkshire Plus

[中文](README.md)

A local workbench for A-share portfolios and stock selection. Open it in a browser. There is no login, it listens only on this machine, and it is not investment advice.

A fresh clone is an empty ledger: no holdings, no API keys, and no built frontend. Follow the steps below and it is ready to use.

## Requirements

Install both of these:

- [uv](https://docs.astral.sh/uv/getting-started/installation/). It downloads Python 3.12–3.14 for this project, so a separate Python install is unnecessary.
- Node.js **22.18 or newer on the 22.x line**, or **24.12 or newer**. Node 20 and 23 do not meet the frontend requirement.

The commands are the same in PowerShell and Git Bash. Run them from the repository root.

## Install and start

```powershell
git clone https://github.com/Nirvana-ZN/berkshire-plus.git
cd berkshire-plus
cd web
npm install
npm run build
cd ..
uv run berkshire-plus
```

Open http://127.0.0.1:8765 . That address works only on the computer running the command.

Press Ctrl+C in that terminal to stop.

If the page says the frontend has not been built, `npm run build` was skipped. Build it in the `web` directory, then start again. `web/dist` is not in git, so every machine builds it once.

Only one background job runs at a time. The header shows whether the running job is the funnel, a single-name evaluation, a portfolio review, weekly discovery, or a full-market fetch.

## First use

An empty database is usable. A YAML ledger is optional.

1. **Positions** at http://127.0.0.1:8765/positions  
   Open "记账（买 / 卖）", fill in date, side, name, shares, and price, then click "记账". The name field searches, and a name can also be typed directly. On an existing position, "记买入" and "记卖出" prefill the form.

2. **Settings** at http://127.0.0.1:8765/settings  
   Screening a list does not need a key. Evaluation and one-click review need an OpenAI-compatible key. See the next section.

3. **Screen** at http://127.0.0.1:8765/screen  
   Choose an industry, index, or theme, then click "开始筛选". The model is called only when "评估终选" is checked, or when "个股直接评估" is used.

4. **Weekly discovery** at http://127.0.0.1:8765/discover  
   Click "更新全市场数据" first. "开始本周发现" stays disabled until that fetch finishes. The fetch pulls a full-market snapshot from East Money and takes a long time. Leave the workbench terminal open.

5. **Index funds** at http://127.0.0.1:8765/index-funds  
   Click "刷新数据". Each index gets one representative fund and an undervaluation screen.

Quotes are fetched while you use the app. They are not stored in the repository.

## API keys

With the workbench running, open http://127.0.0.1:8765/settings . Fill in "大模型接口配置" and click "保存设置":

- **API key**: required for evaluation and review. Stored as `OPENAI_API_KEY` in `.env`.
- **Base URL**: the root of an OpenAI-compatible API. Do not append `/chat/completions`. Empty means `https://api.openai.com/v1`. Zhipu Coding Plan uses `https://open.bigmodel.cn/api/coding/paas/v4`. Zhipu pay-as-you-go uses `https://open.bigmodel.cn/api/paas/v4`.
- **Model**: optional. Empty means `gpt-4o-mini`.

The key and base URL take effect immediately. A restart is not required. The page shows only that the value was saved. Leaving an input blank and saving again does not clear the stored key.

Web search is optional. To look up interim reports, dividends, and ownership before an evaluation, enter a Tavily key on the same page and check "网页搜索". With no Tavily key, the checkbox does not send a search. Reported financial figures still come from fetched filings.

Keys live in `.env` at the repository root. They are not written to the database and they are not committed. You can also create the file by hand. Restart the workbench after a manual edit:

```
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
TAVILY_API_KEY=tvly-...
```

## Import an existing YAML ledger (optional)

Import runs only on the first start, and only when the database has no trades yet. These four files must sit in one directory: `trades.yaml`, `cash.yaml`, `rules.yaml`, and `symbols.yaml`.

PowerShell:

```powershell
$env:PORTFOLIO_YAML_DIR = "D:\path\to\your-yaml"
uv run berkshire-plus
```

Git Bash:

```bash
export PORTFOLIO_YAML_DIR="/d/path/to/your-yaml"
uv run berkshire-plus
```

Import happens once. To import edited YAML again, stop the workbench, delete `data/app.db`, and start with the same variable. Deleting the database also removes trades, discovery results, and the model name saved in settings.

Skip this section if you do not have the four files, and record trades on the positions page.

## Data files

| Path | Purpose |
|------|---------|
| `data/app.db` | SQLite ledger and generated results. Created on first start |
| `.env` | Model key and base URL |
| `reports/` | Evaluation and review Markdown |
| `web/dist` | Built frontend |

None of these are committed. Another computer needs its own frontend build, and a new ledger or a YAML import. Do not copy multi-gigabyte database backups from another machine into the repository.

## Upgrading an existing database

A fresh clone skips this section. It applies only to a full-market snapshot that was already marked `ready` before an upgrade. That snapshot is kept as-is. Fields are not backfilled and values are not rewritten. Do not keep using that fill after the upgrade. Stop the workbench, mark the old fill unavailable, then click "更新全市场数据" again on the weekly discovery page:

```sql
UPDATE discovery_data_fills
SET snapshot_status = 'unavailable',
    status = 'snapshot_unavailable',
    last_error = 'pre-fix snapshot; start a new fill'
WHERE id = <旧填充 ID> AND snapshot_status = 'ready';
```

The old snapshot stays read-only. Do not delete or edit it row by row.
