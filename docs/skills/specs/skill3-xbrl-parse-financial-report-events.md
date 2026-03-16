# Skill 3: `xbrl-parse-financial-report-events`

> per-event XBRL 解析 + 全局 Statement Atlas：利润事实底座

**状态：待开发** — 规格已定义，代码尚未实现。

---

## 职责边界（严格）

- 只处理 `events/sec/events_index.parquet` 中 `category=financial_report` 的事件
- 对每个财报事件：
  - 从 raw_refs 定位 raw/xbrl 文件集合
  - 深解析 XBRL/iXBRL（instance + linkbases），构建 per-event atlas
  - 落盘到该事件对象目录：`events/sec/events/{event_id}/structured_data/xbrl_atlas/*`
- 同时维护全局合并 atlas：`current/analysis_data/xbrl_atlas/*`

## MCP Tools

- `fs` — read/write files
- (fallback) `sec_edgar_mcp.get_financials` — get financial statements
- (fallback) `sec_edgar_mcp.get_xbrl_concepts` / `discover_xbrl_concepts`

## 输入参数

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `ticker` | string | Y | - | 股票代码 |
| `as_of` | date | - | 当天 | 数据截止日 |
| `lookback_years` | int | - | 10 | 回溯年数 |
| `force_refresh` | bool | - | false | 强制刷新 |

## Hard 依赖

- `events/sec/events_index.parquet`（含 category=financial_report 事件）
- 对于目标财报事件：其 `event.yaml` 与 `raw_refs` 指向的 raw/xbrl 必须存在
- `company.yaml`（用于 fiscal_period 推断/校验）

## 输出

### Per-event
- `events/sec/events/{event_id}/structured_data/xbrl_atlas/periods.yaml`
- `events/sec/events/{event_id}/structured_data/xbrl_atlas/facts.parquet`
- `events/sec/events/{event_id}/structured_data/xbrl_atlas/nodes.parquet`
- `events/sec/events/{event_id}/structured_data/xbrl_atlas/edges.parquet`
- `events/sec/events/{event_id}/structured_data/xbrl_atlas/paths.parquet`

### Global (merged)
- `current/analysis_data/xbrl_atlas/periods.yaml`
- `current/analysis_data/xbrl_atlas/facts.parquet`
- `current/analysis_data/xbrl_atlas/nodes.parquet`
- `current/analysis_data/xbrl_atlas/edges.parquet`
- `current/analysis_data/xbrl_atlas/paths.parquet`

### Gaps
- `current/gaps/missing_data.yaml`（for events with missing/unparseable XBRL）

## 内部步骤

1. 读取 events_index，筛选 `category=financial_report` 且窗口内的事件
2. 对每个事件（增量模式：跳过已解析且 raw 未变化的）：
   - 从 event.yaml 的 raw_refs 定位 raw/xbrl 文件集
   - 识别 instance（iXBRL 常见 `*_htm.xml`；传统 `{stem}.xml`）
   - 解析 instance facts：concept + contextRef + unitRef + decimals + value
   - 解析 schema/linkbases：
     - `*_pre.xml`（presentation）→ 报表树（nodes/edges + role_uri）
     - `*_cal.xml`（calculation）→ 加总关系
     - `*_def.xml`（definition）→ 维度/成员
     - `*_lab.xml`（label）→ 标签
   - 产出 per-event atlas：facts/nodes/edges/paths/periods
3. 合并全局 atlas（append + 去重 fact_id）
4. 更新 event.yaml 的 `parse_status.xbrl_parsed`

## 增量策略

- 以事件的 `lineage.raw_manifest_sha256` + `xbrl.instance_filename sha256` 作为 cache key
- 未变化 → per-event 跳过
- 新事件/变化事件 → 只解析增量
- 全局 atlas 用"append + 去重（fact_id）"合并；并更新 periods.yaml

## Fallback 策略

- 当本地 XBRL 缺失或解析失败时，可用 SEC "已抽取"XBRL / `sec_edgar_mcp.get_financials` 做 bootstrap
- 必须在 result/manifest 中记录降级原因

## blocked 条件

- events_index 缺失 → blocked
- 目标窗口内财报事件全部无可解析 XBRL → blocked

## partial 条件

- 部分事件 XBRL 缺失/解析失败，但至少一个事件成功 → partial
- Linkbases 不完整（缺 calculation/definition）→ partial
- Fallback 触发 → partial

## Definition of Done

- Per-event：至少一个财报事件有 facts.parquet 且有数据
- Global：`current/analysis_data/xbrl_atlas/facts.parquet` 存在且有行
- `periods.yaml` 映射 period_end → event_id → accession

---

## 参考实现

完整 run.py 参考见 `docs/archive/Phase_1_implementation_guide.md` §四。

关键模式：
- DataFrame schema：facts (14 columns), nodes (7), edges (4), paths (7)
- Per-event 循环 + global atlas 合并
- `map_statement_type()` 映射 API type → IS/BS/CF
