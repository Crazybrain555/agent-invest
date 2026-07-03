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
GET /v1/filings/latest
GET /v1/changes
```

检查项：

```text
默认返回 active run
显式 processing_run_id 可查历史 run
分页参数存在
错误响应不泄露内部堆栈
响应不含绝对路径
```

## 3. Public view 检查

必须存在：

```text
disclosure_public.documents_v1
disclosure_public.document_units_v1
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
```
