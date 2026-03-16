# Skill 6: `growth-driver-explorer`

> 成长性进一步探索：未来利润路径

**状态：规划中** — 仅有初步规格，代码尚未实现。

---

## 职责边界

- 把增长拆成"能解释"的驱动：量/价/结构/地区/新产品/会计口径/并购 vs 内生
- 输出：再投资率、ROIIC、生命周期阶段

## Hard 依赖

- `current/analytics/diagnostics/profit_quality.yaml`
- `current/analytics/diagnostics/profit_risk_forecast.yaml`
- `events/sec/events_index.parquet`
- `raw/sec/`

## 输出

- `current/analytics/diagnostics/growth_drivers.yaml`
- questions/evidence

## blocked 条件

- 缺任一 hard 产物 → `blocked`
