---
id: disclosure_anchor_milestone_05_document-unit-builder-and-active-run
project: disclosure_anchor
title: document_unit builder 与 active run
status: ready-for-implementation
created_at: 2026-06-26
updated_at: 2026-07-04
depends_on: milestone 04R（必须先完成）
delivers_to: milestone 06
---

# Milestone 05: document_unit builder 与 active run

从 NormalizedIR v2 生成 L2-ready `document_unit`，完成载体规范化（carrier normalization，
顶层协议 §3.5）、质量标记、unit snapshot、active run 原子发布和 change_event。
本文件是实施级规格：算法、事务边界、哈希定义、事件语义全部定死，实施 agent 不另做设计决策。

## 1. 前置依赖（04R 交付，开工前核对）

- NormalizedIR **v2**：parser 中立 `kind ∈ {text, heading, table, image, page_furniture, unknown}`、
  `heading_level`、结构化 `table: {headers, rows, merged_cells?}`（04R-R5）；
- `ops.outbox_event.change_kind` 列 + 事件工厂 `domain/entities/outbox_events.py`（04R-R1/R3）；
- `document.status` 枚举 `registered/parsed/parse_failed/published`（04R-D4）；
- 0007 索引（`document_unit(content_hash)` 等）与视图信封列；
- 既有不变量：`uq_processing_run_one_active_per_document`、`uq_document_unit_run_order`、
  `document_units_relpath` 全链、`write_jsonl_atomic`。

## 2. 本 milestone 锁定的契约决策

- **U1 同 run 构建（04R-D8）**：builder 作用于一个 `status='succeeded'` 的 parse run，
  unit 挂在同一 `processing_run_id` 下；`run_kind='rebuild_units'` 保留不实现。
- **U2 content_hash 规范化定义（unit 级）**：
  `content_hash = "sha256:" + sha256(canonical_json({payload_kind, title, payload}))`，
  canonical_json = `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))`。
  **不含** heading_path / order_index / semantic_key / artifact_locator——顺序与边界变化
  不得触发下游失效（协议 §2.8：内容不变属 observed）。实现为 domain service
  `domain/services/unit_hashing.py`，golden fixture 有跨进程稳定性测试。
- **U3 结构与聚合哈希（run 级）**：
  `unit.structure_hash = "sha256:" + sha256(canonical_json({payload_kind, heading_path, order_index}))`；
  `run.content_hash_aggregate = "sha256:" + sha256("\n".join(sorted(所有 unit content_hash)))`
  （排序 → 与顺序无关，只反映内容集合）；
  `run.structure_hash = "sha256:" + sha256("\n".join(按 order_index 排列的 unit structure_hash))`。
  判"内容没变"只看 `content_hash_aggregate`，**绝不用 normalized_ir 的 artifact_hash**
  （IR 顶层含 created_at，每次运行必变）。
- **U4 asset_id 生成**：`du_` + ULID（`ids.new_asset_id()`），系统生成、不编码业务含义；
  跨 run 不承诺同 ID（身份由 content_hash 表达）。
- **U5 事件模型（发布事务内产出，全部走事件工厂）**：

```text
processing_run_published   每次发布 1 条。
                           materialized ⇔ 新 run 的 content_hash_aggregate ≠ 上一 active run 的
                           （或此前无 active run）；相等 → observed（协议 §2.8）。
document_unit_created      新 run 中 content_hash 不存在于上一 active run 的 unit → 每个 1 条，
                           materialized，payload 含 {asset_id, content_hash, payload_kind, heading_path}。
document_unit_removed      上一 active run 中 content_hash 不存在于新 run 的 unit → 每个 1 条，
                           materialized，payload 含 {content_hash, payload_kind}。
quality_status_changed     content_hash 相同但 quality_status 不同 → 每个 1 条，materialized。
```

  不做模糊的 unit"修改"配对：一次内容修改 = removed + created 两条事件，语义无歧义。
  首次发布：全部 unit 走 document_unit_created。
- **U6 builder 规则版本可归因（协议 §2.6 rule_bundle_ref）**：新迁移
  `0008_unit_builder_provenance`：`processing_run` 加 `builder_rules_version varchar(32)`，
  `processing_runs_v1` 跟进。builder 规则表（噪声/保留/切分/semantic_key）集中在
  `adapters/unit_builder/rules.py`，模块级 `RULES_VERSION = "ub-2026.07-1"`，规则变更必须升版。

## 3. Builder 流水线（`application/use_cases/build_units.py`）

输入：`document_id`（找其最新 succeeded parse run）或显式 `processing_run_id`。
前置校验：run 存在、`status='succeeded'`、`normalized_ir_relpath` 可读、IR `contract_version`
为 `normalized_ir.v2`（v1 → 报 `IR_CONTRACT_TOO_OLD`，指引重新 parse）；该 run 已有 unit → 拒绝
（unit 不可变，重建走新 run）。

按序七个阶段（每阶段独立纯函数，输入输出可单测）：

**S1 噪声抑制**：丢弃 `kind='page_furniture'` 与 `kind='image'`（无正文语义的装饰图）元素；
文本级稳定噪声（水印字符串、控制字符、纯分隔线）清理。被抑制的内容不生成 unit——原文与
parser artifact 仍在，可重处理（协议 §3.5 安全红线）。

**S2 heading tree**：对 `kind='heading'` 与文本形态像标题的 `kind='text'` 元素建层级：

```text
主信号（正则，按优先级给层级）：
  L1: ^第[一二三四五六七八九十百]+[节章]        （第八节 财务报告）
  L2: ^[一二三四五六七八九十]+、               （一、主营业务分析）
  L3: ^（[一二三四五六七八九十]+）              （（一）收入构成）
  L4: ^\d+([.、．]|\s)                         （1. / 1、）
辅助信号：IR heading_level（有值时可提升置信，不单独定级）
排除规则：以？/?结尾、或匹配 qa 编号模式（见 S4）的行绝不是标题——
         防 Phase00 的"问句累积成 heading_path"回归（有专门回归测试）
深度上限 4；heading_path = 祖先标题原文列表（保留编号前缀，与 golden fixtures 一致）
```

**S3 text 切分（service-purpose §8.1 优先级）**：同一最深标题下的连续 text 元素合并为一个
`text` unit；显式编号条目多且长时按条目拆分；长而无内部结构的小节保持单 unit（§8.2，
不做字符数/token 切分）。`title` = 最近标题文本。

**S4 qa builder（投关记录 / 业绩说明会 filing_type 触发）**：

```text
问题模式：^\s*(\d+[、.．]?|问[:：]|Q\d*[:：])?\s*.{2,}[？?]\s*$
回答模式：^\s*(答|A)[:：] 起始，或问题行之后到下一问题行之间的全部文本
来源：text 元素序列 + 结构化表格 cell 内的多行文本（ir_activity 样本的问答嵌在
      "投资者关系活动主要内容介绍" cell 里——按行拆后套同一规则）
产出：每个 Q&A 对一个 qa unit，payload = {question, answer, raw_text}
      （与 golden fixtures 契约一致）；边界不稳的对保存为 text + needs_review，不硬拆
```

**S5 table builder**：一个 IR table 元素 → 一个 `table` unit。payload：

```text
{
  "caption":  table_caption 列表原样,
  "unit":     从 caption / 表头 / 前一文本元素按规则识别（"单位：元/万元/千元"），识别不到为 null,
  "headers":  IR 结构化 headers,
  "rows":     IR 结构化 rows,
  "notes":    table_footnote 列表原样
}
title = caption 首项或最近标题。IR 带 table_parse_failed → payload 落 {caption, raw_html, notes}
并 quality_status='needs_review'。跨页表合并：相邻 table 元素间无非噪声元素、且列数相同、
且后表无独立 caption → 合并 rows；合并失败标 needs_review，不阻塞其他 unit（不确定即不合并）。
```

**S6 保留/跳过（service-purpose §9 + 红线）**：规则表驱动（rules.py），默认跳过仅限
§9.2 的封面/目录/签章/空表/纯模板类；**「重要提示」「风险提示」标题的板块必须生成 unit**，
不得按标签跳过（协议 §3.5，有专门测试）；拿不准 → 保留。跳过项记入 build 统计（S8 报告），
不写 DB。

**S7 semantic_key + quality_status**：semantic_key 由规则表按
`filing_type + heading/caption 正则` 给出（首版词表：receivable_aging / inventory_breakdown /
goodwill_impairment / revenue_breakdown / guarantee / related_party / shareholder_structure /
tariff_exposure；未命中 = null，禁止自由发明）。quality_status：结构完整 → ok；
表解析失败/QA 边界不稳/跨页合并失败 → needs_review；payload 空或乱码占比超阈值 → unusable。

**S8 落库与快照（单 UoW 事务）**：`DocumentUnitRepository.add_many`（新增仓储方法，含
`list_by_processing_run`、`list_by_document_active`）；order_index 全文档递增；写
`document_units.v1.jsonl` 快照（`write_jsonl_atomic` → `document_units_snapshot_relpath`，
行格式与 golden fixtures 同键）；回写 run 的 `document_units_relpath` /
`content_hash_aggregate` / `structure_hash` / `builder_rules_version`。

## 4. 发布事务（`application/use_cases/publish_run.py`，04R-B13 的原子操作）

单 UoW 事务，顺序固定：

```text
1. SELECT document FOR UPDATE（新增 DocumentRepository.get_for_update）
2. 读上一 active run 及其 unit content_hash/quality_status 集合（U5 diff 的基准）
3. 旧 active run：is_active=false（先 flush——partial unique index 要求先清后置）
4. 新 run：is_active=true
5. document.current_processing_run_id = 新 run；document.status = 'published'
6. 按 U5 计算 diff，经事件工厂写全部 outbox 事件（processing_run_published + unit 级）
7. commit。任何一步失败 → 整体回滚，旧 active run 不变（既有检查点保持）
```

前置校验：run 属于该 document、`status='succeeded'`、已有 unit（空 run 拒绝发布，除非显式
`allow_empty=true` 且记 needs_review 原因）。重复发布同一 run → 幂等返回，不重复发事件。

## 5. CLI 与 Make 入口

```bash
python -m disclosure_anchor.cli.pipeline build-units --document-id <id>   # S1–S8
python -m disclosure_anchor.cli.pipeline publish --processing-run-id <id> # §4
python -m disclosure_anchor.cli.pipeline process --document-id <id>       # parse→build→publish 串行
make build-units DOC=<id> / make publish RUN=<id> / make process DOC=<id>
```

process 是 08 worker 的单文档执行单元；build 统计（生成/跳过/降级数量）打印且入 run 级日志。

## 6. 检查点

- 年报样本（002484 2025A）：可取经营分析 `text` unit；应收账款账龄 `table` unit 的
  headers/rows/unit 完整，semantic_key='receivable_aging'。
- 投关样本（000333）：≥30 个 qa unit，question/answer 绑定正确（对照 golden fixtures）。
- `payload` 存快照本身；heading_path 无问句污染（回归测试）。
- 重跑 build+publish 且内容未变：`processing_run_published` 为 observed，0 条 unit 级事件。
- 修改一个 unit 内容后重跑：恰好 1 removed + 1 created，其余不动。
- 发布中途失败（注入）：旧 active run 与 current_processing_run_id 不变。
- 「重要提示」板块存在于 unit 中（红线测试）。
- 0008 迁移真库 + 临时库往返通过；`document_units_v1` 行可按 15 scope keys 检索。

## 7. 测试要求

单测：S1–S7 每阶段纯函数测试（含 heading 排除规则、qa 边界、跨页合并、U2/U3 哈希稳定性、
semantic_key 规则表、红线保留）；publish diff 三态（首发/无变化/增删改）；事件 change_kind。
集成（DB-gated）：build→publish 全链、幂等重发布、发布失败回滚、事件可从 change_events_v1
读回且 subject_ref/change_kind 正确。契约：快照 jsonl 与 golden fixtures 键集一致。

## 8. Definition of Done

- 三个本地样本 raw → parse → build → publish 全链跑通，§6 检查点全过；
- `make test` no-DB 与 live-DB 双绿；acceptance-matrix A19/A20/A21 置 pass。

## 9. 明确不做

- 不抽取 claim；不做 table_cell / page-bbox 核心索引；不做 LLM 语义价值判断；
- 不实现 `rebuild_units`；不做 Filing API（06）；不做批量调度（08）。

## 10. 交付给下一阶段

document_unit 表数据与快照、active run、outbox 事件流（06 的 `GET /v1/changes` 直接消费）、
build/publish/process CLI（08 worker 的执行单元）。

## 11. 常见失败与处理

- 载体规范化误删实质内容：降级规则倾向保留；原文与 parser artifact 可重处理（红线）。
- 表格跨页合并失败：needs_review，不阻塞 text/qa。
- Q&A 边界不稳：保存为 text 或 needs_review，不自由拆。
- 发布竞态：FOR UPDATE + one-active-run 索引兜底；IntegrityError 翻译为领域错误后重查。
