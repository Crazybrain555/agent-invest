---
id: disclosure_anchor_milestone_06_filing-api-public-contracts
project: disclosure_anchor
title: Filing API 与 public contracts
status: ready-for-implementation
created_at: 2026-06-26
---

# Milestone 06: Filing API 与 public contracts

## 1. 目标

实现 L2 可消费的 API、public read views、source_ref、changes，并用 contract tests 锁定对外契约。

## 2. 范围

范围内：

- document query API。
- units query API。
- source_ref API。
- changes API。
- OpenAPI 文件。
- public model JSON schema。
- contract tests。


## 3. 实施细则

1. 实现 endpoint：

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
POST /v1/admin/documents/register-local-pdf
POST /v1/admin/documents/{document_id}/parse
POST /v1/admin/runs/{processing_run_id}/publish
```

2. API 默认返回 active run。
3. 支持显式 `processing_run_id` 查询历史 run。
4. 生成并提交 `contracts/filing_api.openapi.yaml`。
5. JSON schema 覆盖：

```text
document.v1.json
document_unit.v1.json
processing_run.v1.json
source_ref.v1.json
change_event.v1.json
```

6. public views 与 API DTO 字段含义保持一致。
7. units 读契约以 `document_units_v1` 的 unit 级 scope keys 为准（service-purpose §12.1 的 15 键：
   company_ref、security_ref、filing_type、report_period、announcement_date、payload_kind、
   heading_path、semantic_key、quality_status、content_hash、contract_version、
   producer_action_ref、source_ref、parent_ref、order_index），DTO 与 `document_unit.v1.json`
   不得缺列或改名。
8. changes DTO 与 `change_event.v1.json` 必须携带 `event_kind`（对外事件名）与
   `change_kind`（仅 `observed` / `materialized` 两值，未显式声明的历史事件默认
   `materialized`），语义遵循 service-purpose §12.2：下游失效只由 `materialized` 触发。


## 4. 检查点

- `GET /v1/filings/latest` 可用。
- `GET /v1/documents/{id}/units` 可用。
- `GET /v1/units/{id}/source-ref` 可用。
- `GET /v1/changes?after_seq=0` 可用，事件含 `event_kind` 与 `change_kind`（observed/materialized）。
- API 不返回绝对路径。
- API 不返回 private state / 内部异常堆栈。
- contract tests 通过。


## 5. Definition of Done

- L2 可通过 API 和 public views 消费本服务。
- source_ref 可稳定生成。
- change feed 可轮询。


## 6. 明确不做

- 不实现认证体系。
- 不开放局域网监听。
- 不实现高级搜索。
- 不引入 GraphQL。


## 7. 交付给下一阶段

- Filing API。
- OpenAPI。
- public models。
- contract tests。


## 8. 常见失败与处理

- DTO 与 public view 不一致：先修 contract，不继续。
- API 泄露 relpath 以外路径：立即修。
- 历史 run 查不到：修 query，不覆盖旧 run。
