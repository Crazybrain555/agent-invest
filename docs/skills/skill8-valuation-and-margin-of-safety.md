# Skill 8: `valuation-and-margin-of-safety`

> 估值与安全边际：输出 IV vs 市场

---

## 职责边界

- 以"经济利润 × 质量系数"组织估值：EPV / DCF / Residual Income
- 输出：bear/base/bull 估值区间、敏感性、下行保护来源

## 输入参数

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `model_type` | string | - | "hybrid" | epv / dcf / hybrid |
| `force_refresh` | bool | - | false | 强制刷新 |

## Hard 依赖

- `current/analysis_data/market_snapshot.yaml`
- `current/analysis_data/economic/core_metrics.parquet`
- `current/analysis_data/economic/economic_statements.parquet`
- `current/analytics/diagnostics/profit_risk_forecast.yaml`
- `current/analytics/diagnostics/growth_drivers.yaml`
- `current/analytics/diagnostics/quality_coefficient.yaml`

> 注：前期实现可仅依赖 market_snapshot + core_metrics，quality 相关依赖在 Skill 5-7 完成后接入。

## 输出

- `current/analytics/valuation/valuation.yaml`
- `current/analytics/valuation/valuation_model.csv`
- `current/outputs/value_state.yaml`
- `current/outputs/investment_memo.md`
- evidence

## 关键公式

```python
DEFAULT_ASSUMPTIONS = {
    "discount_rate": {"bear": 0.12, "base": 0.10, "bull": 0.085},
    "advantage_period_years": {"bear": 3, "base": 5, "bull": 8},
    "owner_earnings_growth": {"bear": 0.00, "base": 0.03, "bull": 0.06},
    "terminal_growth": 0.02,
}

# EPV = owner_earnings / discount_rate
# DCF = Stage 1 (advantage period with tapering growth) + Terminal Value
#   yr_growth = growth - (growth - terminal_growth) * (year / advantage_period)
#   terminal_value = last_cf * (1 + terminal_growth) / (discount - terminal_growth)
# Combined = 0.4 * EPV + 0.6 * DCF
# Margin of Safety = (IV_per_share - price) / IV_per_share
```

## blocked 条件

- 任一 hard 缺失 → `blocked`

## Definition of Done

- `value_state.yaml` 有 `margin_of_safety_base`
- `investment_memo.md` 可读

---

## 参考实现

完整 run.py 参考见 `docs/archive/Phase_1_implementation_guide.md` §六。

关键模式：
- EPV 作为永续年金
- DCF 两阶段：advantage period（增长递减）+ terminal value
- 方法权重：0.4 EPV + 0.6 DCF
- Investment memo 模板：verdict logic（>20% MOS = undervalued）
