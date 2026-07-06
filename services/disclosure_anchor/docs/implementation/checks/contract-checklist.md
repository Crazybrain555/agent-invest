---
id: disclosure_anchor_contract_checklist
project: disclosure_anchor
title: API / public view / source_ref 契约检查清单
status: final-for-implementation
created_at: 2026-06-26
---

# API / public view / source_ref 契约检查清单

## 1. 对外契约对象

只允许对外稳定发布：

```text
document
document_unit
document_category
processing_run
source_ref
change_event
```

不得对外承诺：

```text
MinerU raw JSON
SQLAlchemy model
disclosure_core 表结构
绝对文件路径
page / bbox / parser block / table cell
```

## 2. API 检查

必测 endpoint：

```text
GET /v1/health
GET /v1/documents
GET /v1/documents/{document_id}
GET /v1/documents/{document_id}/runs
GET /v1/documents/{document_id}/units
GET /v1/units/{asset_id}
GET /v1/units/{asset_id}/source-ref
GET /v1/units/{asset_id}/context
GET /v1/filings/latest
GET /v1/changes
```

检查项（语义细则以 milestone 06 为唯一权威，本清单只列覆盖面）：

```text
默认返回 active run
显式 processing_run_id 可查历史 run；单 asset_id 解引用永远可用并携带 is_active_run
分页参数存在（limit 默认 100 / 上限 1000；keyset 游标语义见 06 §3.2）
错误响应不泄露内部堆栈
响应不含绝对路径
错误码枚举：L1_PROCESSING_REQUIRED / NOT_FOUND / CONTRACT_VERSION_MISMATCH /
           GONE_SUPERSEDED / VALIDATION_ERROR（触发条件见 06 §3.3）
unit 级 DTO 派生字段全集 = {asset_uri}（仅 API 序列化层派生，不入库、不进 *_v1 视图，
  DERIVED 白名单排除）；is_active_run 自 0011 起是 document_units_v1 / source_refs_v1
  的真实视图列（round3 P1#7：DB 直读方可直接过滤 active run）
scope keys 过滤参数可用（filing_type / payload_kind / heading_prefix（数组前缀语义，
  见 06 §3.8）/ semantic_key / quality_status 等）
0010 起 document_units_v1 追加 applicability / page_no 列（applicability：
  'applicable'|'not_applicable'|NULL，节适用性声明的一等筛选列，payload 保持纯原文，
  部分索引 ix_document_unit_applicability；page_no：artifact_locator 首页码提升列）。
0007 起 document_units_v1 追加 6 列：asset_kind / observed_at / source_tier /
  trace_level / raw_file_hash / query_projection_hash
  （列全集：04R-R7 的 32 列 + 0010 applicability/page_no + 0011 is_active_run = 35 列）
0007 起 change_events_v1 追加 change_kind（真实列）/ subject_kind / subject_ref /
  source / contract_version
0007 起 documents_v1 追加 contract_version / company_ref / security_ref / source_ref /
  supersedes 链 / superseded_by_document_id / provider_metadata
0011 起 document_unit.payload_kind 增加 'mixed'（round3 P0#1 业务语义块：
  semantic_type = meeting_proposal / document / section，payload.parts 承载有序部件）
0012 起新增 document_categories_v1（provider 原生分类：F006V 段 × provider_category
  字典（p_info3005 快照 seed）；facet 语义只给 ordinal 不造 is_primary；filing_type
  仍为内部粗桶，规则包 2026-07-r3 起 调研活动→investor_relations）
```

## 3. Public view 检查

必须存在：

```text
disclosure_public.documents_v1
disclosure_public.document_units_v1
disclosure_public.document_categories_v1
disclosure_public.processing_runs_v1
disclosure_public.source_refs_v1
disclosure_public.change_events_v1
```

检查项：

```text
只读角色可 select
只读角色不可 insert/update/delete
不暴露 private state columns
不暴露 MinerU raw JSON
不暴露绝对路径
字段含义与 API DTO 一致
```

## 4. source_ref 检查

source_ref 必须包含：

```text
service
contract_version
source_access_id
document_id
provider
provider_document_id
raw_file_hash
processing_run_id
is_active_run
asset_id
payload_kind
heading_path
title
unit_content_hash
quality_status
artifact_locator
```

L2 引用 source_ref 后，应能回到：

```text
原始 PDF hash
处理 run
unit payload snapshot
artifact locator
```

## 5. Change feed 检查

`GET /v1/changes?after_seq=...` 必须满足：

```text
seq 单调递增
limit 生效
无重复事件
可从 0 全量拉取
可从 last_seq 增量拉取
事件 payload 不含 private details
事件携带 event_kind（与 outbox 列同名）
事件携带 change_kind，取值仅 observed / materialized（历史事件默认 materialized）
at-least-once 投递 + 消费端幂等（重复投递不产生重复消费效果；“无重复事件”指 feed 内 seq 不重复）
同一 subject（document / asset）内事件保序
下游失效只由 change_kind=materialized 触发
```
