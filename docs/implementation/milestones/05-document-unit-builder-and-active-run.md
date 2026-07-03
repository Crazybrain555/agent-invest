---
id: disclosure_anchor_milestone_05_document-unit-builder-and-active-run
project: disclosure_anchor
title: document_unit builder 与 active run
status: ready-for-implementation
created_at: 2026-06-26
---

# Milestone 05: document_unit builder 与 active run

## 1. 目标

从 NormalizedIR 生成 L2-ready document_unit，完成 A 类确定性清洗、质量标记、unit snapshot、active run 发布和 change_event。

## 2. 范围

范围内：

- heading tree builder。
- text/table/qa unit builder。
- A 类清洗规则。
- retention rules。
- quality checks。
- document_unit 写库。
- document_unit_snapshots 写文件。
- publish active run。
- change detector / outbox。


## 3. 实施细则

1. 读取 normalized_ir。
2. 构建 heading_path。
3. 生成三类 unit：

```text
text
table
qa
```

4. 执行 A 类确定性清洗。
5. 计算：

```text
content_hash
structure_hash
content_hash_aggregate
```

6. 写入 document_unit。
7. 写入 document_units.v1.jsonl snapshot。
8. quality_status：

```text
ok
needs_review
unusable
```

9. publish run：

```text
同一 document 的 current_processing_run_id 切换到新 run
旧 run 保留
```

10. 产生 outbox_event（下列为 `event_kind` 取值）：

```text
processing_run_created
processing_run_published
document_unit_created
document_unit_changed
quality_status_changed
```

每条事件同时写 `change_kind`（service-purpose §12.2）：凡引起 public read model 可见内容变化
（发布、unit 内容 / 质量状态变化）为 `materialized`；仅巡检 / 来源观察且无可消费变化为 `observed`。
下游失效只由 `materialized` 触发。


## 4. 检查点

- 年报样本可取经营分析 text unit。
- 年报样本可取完整 table unit。
- 投关样本可取完整 qa unit。
- `payload` 存快照本身，不只是 locator。
- 重跑后旧 run 不删除。
- 发布失败时旧 active run 不变。
- content_hash 不变时不产生不必要的 unit_changed。


## 5. Definition of Done

- 样本 document 可从 raw → run → units → active run。
- outbox_event 可查询。
- unit builder tests 通过。


## 6. 明确不做

- 不抽取 claim。
- 不做 table_cell。
- 不做 page/bbox 核心索引。
- 不做 LLM 语义价值判断。


## 7. 交付给下一阶段

- document_unit 表数据。
- document_unit_snapshots。
- active processing_run。
- change_event。


## 8. 常见失败与处理

- A 类清洗误删实质内容：立即降级规则，倾向保留。
- 表格跨页合并失败：标记 needs_review，不阻塞 text/qa。
- Q&A 边界不稳：保存为 text 或 needs_review，不自由拆 claim。
