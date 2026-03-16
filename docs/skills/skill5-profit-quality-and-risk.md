# Skill 5: `profit-quality-and-risk`

> 基于财报，发现风险，预测未来利润：质量系数与情景下界

**状态：规划中**

---

## 职责边界

- 基于 economic_statements/core_metrics 做：
  - 利润质量（现金支撑、应计质量、操纵风险）
  - 财务风险（杠杆、流动性、表外压力）
  - 对未来 3-5 年经济利润的**风险拆解**
- 参考框架：Sloan、Piotroski、Beneish、Dechow、Financial Shenanigans

## Hard 依赖

- `current/analysis_data/economic/economic_statements.parquet`
- `current/analysis_data/economic/core_metrics.parquet`
- `events/sec/events_index.parquet`
- `raw/sec/`（用于引用审计意见、会计政策、风险因素）

## 输出

- `current/analytics/diagnostics/profit_quality.yaml`
- `current/analytics/diagnostics/profit_risk_forecast.yaml`
- questions/evidence

## blocked 条件

- economic/core 缺失 → `blocked`
