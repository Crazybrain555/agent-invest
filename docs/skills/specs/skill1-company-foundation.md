# Skill 1: `company-foundation`

> 身份 + 市场口径：估值分母/每股化基座

---

## 职责边界

- 初始化目录（如不存在）
- 解析 ticker → CIK/公司名/交易所等身份信息
- 拉取 market snapshot（price、shares outstanding、float、EV 等）
- **查漏补缺**：已有且新 → 跳过

## MCP Tools

- `sec_edgar_mcp.get_cik_by_ticker` — resolve CIK from ticker
- `sec_edgar_mcp.get_company_info` — get company details
- `sec_edgar_mcp.get_recent_filings` — infer fiscal year end from annual filing period_of_report
- `alpaca.get_stock_latest_trade` / `alpaca.get_stock_snapshot` — price (USD)
- `alpaca.get_asset` — exchange fallback
- `trading_mcp.get_fundamental_stock_metrics` — (optional) shares / marketCap / EV
- `yfinance.get_stock_info` — fallback shares / marketCap / EV
- `fs` — write files

## 输入参数

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `force_refresh` | bool | - | false | 强制刷新 |

## Hard 依赖

无（链条起点）

## 输出

- `company/{TICKER}/company.yaml`
- `company/{TICKER}/current/analysis_data/market_snapshot.yaml`
- `company/{TICKER}/current/gaps/artifacts_state.yaml`（更新）
- `runs/{run_id}/meta.yaml`, `result.yaml`

## 内部步骤

1. 确保目录树存在（raw/events/current/runs + current 子目录）
2. 身份解析：SEC CIK、公司名、FY end、货币等
3. 市场口径：
   - `alpaca` 优先提供 `price`
   - shares / market cap / EV：优先 trading_mcp/SEC，其次 Yahoo 兜底
   - `market_cap` 默认用来源值，并用 `price * shares_outstanding` 交叉验证
   - `enterprise_value` 以 USD 输出
4. 写 evidence（身份来源、市场数据来源）

## 查漏补缺规则

- identity：若 `company.yaml` 已有 cik 且未 `force_refresh` → `skipped`
- market_snapshot：若 `as_of` 相同且文件存在且字段齐全 → `skipped`

## blocked 条件

- SEC 返回无 CIK 且 fallback 失败 → `blocked`

## partial 条件

- 市场数据不完整（price 缺失）→ `partial`

## Definition of Done

- `company.yaml` 存在且 `cik` 字段有值
- `market_snapshot.yaml` 存在且有 `price` 和 `shares_outstanding`
- `result.yaml` 显示 status=ok 或 partial

---

## 参考实现

完整 run.py 参考见 `docs/archive/Phase_1_implementation_guide.md` §二。

关键模式：
- 先写 `runs/{run_id}/outputs/`，再原子替换到 `current/`
- skip 检测：identity 和 market 分开判断
- evidence 集成：CIK 解析成功后追加 evidence
- artifacts_state 更新：每个产物独立追踪状态
