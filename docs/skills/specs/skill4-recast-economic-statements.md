# Skill 4: `recast-economic-statements`

> 三表重铸与核心指标（经济报表层）：Owner Earnings / ROIC

**状态：待开发** — 规格已定义，代码尚未实现。

---

## 职责边界

- 从 xbrl_atlas 出发，重铸：
  - operating vs financing 拆分
  - NOPAT、Invested Capital、ROIC
  - FCF、Owner Earnings（含 maintenance capex 估计）
- 输出"经济三表 + 指标宽表"
- 记录重铸规则与映射（recast_policy），用于可追溯与可迭代

## 输入参数

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `policy_version` | string | - | "default" | 重铸策略标识 |
| `force_refresh` | bool | - | false | 强制刷新 |

## Hard 依赖

- `current/analysis_data/xbrl_atlas/nodes.parquet`
- `current/analysis_data/xbrl_atlas/edges.parquet`
- `current/analysis_data/xbrl_atlas/facts.parquet`
- `current/analysis_data/xbrl_atlas/periods.yaml`

## 输出

- `current/analysis_data/economic/recast_policy.yaml`
- `current/analysis_data/economic/economic_statements.parquet`
- `current/analysis_data/economic/core_metrics.parquet`
- evidence

## 核心指标输出

| 指标 | 说明 |
|------|------|
| `revenue` | 主营业收入 |
| `nopat` | 税后经营利润 |
| `invested_capital` | 投入资本 |
| `roic` | 投入资本回报率 |
| `cfo` | 经营现金流 |
| `capex` | 资本支出 |
| `maintenance_capex` | 维护性资本支出（估计） |
| `fcf` | 自由现金流 |
| `owner_earnings` | 股东盈余 |

## 内部步骤

1. 加载 xbrl_atlas (facts + periods)
2. 对每个 period，用 LABEL_MATCHERS 匹配 GAAP 标签到经济概念
3. 计算 core_metrics（见下方公式）
4. 写 recast_policy.yaml 记录映射决策
5. 写 economic_statements.parquet + core_metrics.parquet

## 关键公式

```python
LABEL_MATCHERS = {
    "revenue": ["total revenue", "revenues", "net revenue", "net sales"],
    "operating_income": ["operating income", "income from operations"],
    "cfo": ["net cash provided by operating activities", "cash flows from operating activities"],
    "capex": ["capital expenditure", "purchases of property", "payments for property"],
    "depreciation": ["depreciation and amortization", "depreciation"],
    "total_debt": ["total debt", "long-term debt", "total borrowings"],
    "total_equity": ["stockholders equity", "total equity"],
    "cash": ["cash and cash equivalents", "cash"],
    "tax_expense": ["income tax expense", "provision for income taxes"],
    "pretax_income": ["income before income taxes", "pretax income"],
}

# 有效税率：clamp 到 [0.15, 0.35]，默认 0.25
eff_tax = min(max(tax / pretax, 0.15), 0.35) if valid else 0.25

# NOPAT = operating_income * (1 - eff_tax)
# Invested Capital = debt + equity - cash (minimum 1)
# Maintenance Capex = max(depreciation * 0.8, capex * 0.5)
# FCF = CFO - capex
# Owner Earnings = CFO - maintenance_capex
# ROIC = NOPAT / Invested Capital
```

## 查漏补缺规则

- policy_version 与输入未变 + atlas 未变 + 输出存在 → `skipped`
- policy_version 或 atlas 更新 → 重跑

## blocked 条件

- atlas 缺失/不完整到无法产出最小 economic_statements → `blocked`

## partial 条件

- CFO 或 capex 未找到 → `partial`，使用 fallback 估计

## Definition of Done

- `core_metrics.parquet` 至少一行有 `owner_earnings`
- `recast_policy.yaml` 显示映射决策

---

## 参考实现

完整 run.py 参考见 `docs/archive/Phase_1_implementation_guide.md` §五。
