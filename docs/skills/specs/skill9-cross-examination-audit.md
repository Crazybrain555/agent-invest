# Skill 9: `cross-examination-audit`

> 反问和审计：提高确定性，防大错

**状态：规划中** — 仅有初步规格，代码尚未实现。

---

## 职责边界

- 对比：管理层叙事（MD&A/风险因素） vs 数字（经济三表）
- 找矛盾、反向思维审计清单
- 明确：这会如何影响估值参数

## Hard 依赖

- `current/outputs/value_state.yaml`
- `current/analytics/valuation/valuation.yaml`
- `current/analytics/diagnostics/quality_coefficient.yaml`
- `current/analytics/diagnostics/profit_quality.yaml`
- `current/analytics/diagnostics/growth_drivers.yaml`
- `events/sec/events_index.parquet`
- `raw/sec/`

## 输出

- `current/analytics/diagnostics/audit.yaml`
- `current/gaps/questions.jsonl`（追加）
- `current/analytics/evidence/evidence.jsonl`（追加）

## blocked 条件

- 任一 hard 缺失 → `blocked`
