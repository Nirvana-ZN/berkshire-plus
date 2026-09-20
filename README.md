# Berkshire Plus

本机 A 股看仓与选股工作台。浏览器打开即可，无登录、不绑定公网。

## 运行

需要 Python 3.12–3.14，以及一次前端构建（已构建过可跳过 `npm run build`）。

```powershell
cd web
npm install
npm run build
cd ..
uv run berkshire-plus
```

浏览器打开 http://127.0.0.1:8765

## 迁入已有 YAML 账本（可选）

首次启动且库中还没有成交时，会读取环境变量 `PORTFOLIO_YAML_DIR` 下的四个文件：`trades.yaml`、`cash.yaml`、`rules.yaml`、`symbols.yaml`。迁入只发生一次。

```powershell
$env:PORTFOLIO_YAML_DIR = "D:\path\to\your-yaml"
uv run berkshire-plus
```

没有 YAML 也可以先打开工作台，在看仓页手记买卖。

## 模型密钥

漏斗可以只筛名单。评估与一键审视需要 OpenAI 兼容密钥，在设置页填写（写入项目根目录 `.env`，不进数据库、不进 git）。

## 数据文件

| 路径 | 用途 |
|------|------|
| `data/app.db` | SQLite 账本与产物 |
| `.env` | 模型密钥 |
| `reports/` | 评估与审视 Markdown |

三者均不进版本库。改 YAML 后若要重新迁入，需先停进程并删除 `data/app.db`。

## 发现数据快照升级

本版本会原样封存所有已经标记为 `ready` 的全市场证券快照；迁移不会补字段、修值或追认旧数据正确。升级前已经初始化的发现数据填充不得继续复用，需停掉工作进程，将旧填充标记为 `snapshot_unavailable` 后重新发起填充：

```sql
UPDATE discovery_data_fills
SET snapshot_status = 'unavailable',
    status = 'snapshot_unavailable',
    last_error = 'pre-fix snapshot; start a new fill'
WHERE id = <旧填充 ID> AND snapshot_status = 'ready';
```

随后调用 `POST /api/discovery-data/fill` 创建新填充。旧快照继续保持只读，不能逐行删除或修改。
