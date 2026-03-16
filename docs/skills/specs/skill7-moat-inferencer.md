# Skill 7: `moat-inferencer`

> 护城河推断器 → 质量系数：质量系数主体

**状态：规划中** — 仅有初步规格，代码尚未实现。

---

## 职责边界

- 用可追溯证据识别护城河来源：Greenwald、Porter、Morningstar、Mauboussin
- 产出 **quality_coefficient**：把证据映射成估值参数

## Hard 依赖

- `current/analytics/diagnostics/growth_drivers.yaml`
- `current/analytics/diagnostics/profit_quality.yaml`
- `events/sec/events_index.parquet`
- `raw/sec/`

## 输出

- `current/analytics/diagnostics/moat.yaml`
- `current/analytics/diagnostics/quality_coefficient.yaml`
- evidence/questions

## blocked 条件

- 任一 hard 缺失 → `blocked`
