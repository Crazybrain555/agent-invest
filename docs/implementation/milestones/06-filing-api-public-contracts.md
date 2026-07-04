---
id: disclosure_anchor_milestone_06_filing-api-public-contracts
project: disclosure_anchor
title: Filing API 与 public 契约
status: ready-for-implementation
created_at: 2026-06-26
updated_at: 2026-07-04
depends_on: milestone 05
delivers_to: milestone 07 / L2 / MCP 包装
---

# Milestone 06: Filing API 与 public 契约

把 `disclosure_public.*_v1` 的读契约以 HTTP API 形式暴露（协议 §3.11 的 L1 对外契约六条），
并冻结 JSON schema。API 是视图的薄投影：**不引入视图之外的新语义**，读侧语义与
`disclosure_public.*_v1` 逐列一致。

## 1. 前置依赖

- 05 完成（active run、unit 数据、事件流）；0007/0008 视图列全集
  （15 scope keys + asset_id + asset_kind/observed_at/source_tier/trace_level/raw_file_hash +
  builder_rules_version）；
- `document.status` 枚举（04R-D4）——`L1_PROCESSING_REQUIRED` 的判定数据面。

## 2. Endpoint 清单与语义

只读（reader 角色连接，经 public 视图，不回读私有表）：

```text
GET /v1/health                     存活 + migration head + 外置盘挂载状态
GET /v1/documents                  过滤：company_ref / security_code / filing_type /
                                   report_period / announcement_date_from,to / status；游标分页
GET /v1/documents/{document_id}    单档；被取代时正常返回但携带 superseded_by_document_id
GET /v1/documents/{id}/runs        该 document 全部 processing_run（历史可见）
GET /v1/documents/{id}/units       默认 active run 的 units；?processing_run_id= 读历史快照
GET /v1/units/{asset_id}           单 unit 全量（payload 完整返回，不截断）
GET /v1/units/{asset_id}/source-ref source_refs_v1 单行投影
GET /v1/filings/latest             过滤同 /v1/documents；每 (company_ref, filing_type,
                                   report_period) 取 announcement_date 最新且未被取代的一份
GET /v1/changes?after_seq=&limit=  change_events_v1 按 seq 升序；默认 limit 100，上限 1000
```

写侧（admin，本地运维用，POST；对应 use case 直通）：

```text
POST /v1/admin/documents/register-local-pdf     RegisterLocalPdfCommand 字段
POST /v1/admin/documents/{id}/parse             触发 ParseDocument
POST /v1/admin/documents/{id}/build-units       触发 BuildUnits（05）
POST /v1/admin/runs/{processing_run_id}/publish 触发 PublishRun（05）
```

## 3. 契约细则

1. **DTO = 视图列**：`document_unit.v1.json` 等 schema 从 0007 后的视图列生成，逐列同名同义；
   不得缺列、不得改名。unit 级 DTO 额外携带序列化层派生字段
   `asset_uri = "asset://disclosure_anchor/v1/document_unit/{asset_id}"`（不入库不进视图）。
2. **游标分页**：列表接口统一 keyset 分页——响应携带 `next_cursor`（base64(JSON) 不透明游标，
   内容为排序键值），请求带 `cursor=`；排序键：documents 按 (announcement_date DESC,
   document_id DESC)，units 按 (order_index ASC)，changes 直接用 `after_seq`。
   禁止 offset 分页。scope-key 过滤 + 游标分页是契约义务（协议 §3.11 第 3 条）。
3. **错误模型（service-purpose §12.3）**，响应体统一
   `{"error_code": ..., "message": ..., "detail": {...}}`：

```text
NOT_FOUND                  → 404
GONE_SUPERSEDED            → 410，detail.superseded_by = superseded_by_document_id
                             （仅当请求方显式 ?reject_superseded=true 时触发；默认正常返回旧档）
L1_PROCESSING_REQUIRED     → 409，document.status ∈ {registered, parse_failed} 或无 active run
                             时请求 units；detail.status 携带当前状态
CONTRACT_VERSION_MISMATCH  → 400，请求头 X-Contract-Version 显式指定且不受支持时
```

   错误响应不含内部堆栈、绝对路径、私有 schema 信息。
4. **change feed 消费协定**（协议 §2.8）：`seq` 单调、可断点续读；at-least-once + 消费端幂等；
   同一 subject 内保序；事件携带 `event_kind` / `change_kind` / `subject_ref` / `source` /
   `contract_version`；下游失效只由 materialized 触发。
5. **OpenAPI 与 schema 冻结**：生成并提交 `contracts/filing_api.openapi.yaml`；
   `contracts/public_models/` 下冻结 `document.v1.json / document_unit.v1.json /
   processing_run.v1.json / source_ref.v1.json / change_event.v1.json`；contract test
   逐列钉死视图 ↔ schema ↔ DTO 三方一致。
6. **实现形态**：FastAPI router 按资源拆分（documents / units / filings / changes / admin)；
   读侧连接使用 reader 角色 DSN（settings 新增 `DISCLOSURE_READER_DATABASE_URL`，缺省回落
   DATABASE_URL 并 doctor WARN）；进程级单 engine；响应模型 Pydantic，不返回 ORM 对象。
7. **MCP 映射预留**（协议 §3.11 第 6 条，不在本期实现）：查询→tool、取回→resource（按
   asset_uri）、变更→notifications；本期只保证 URI 与游标契约稳定，包装时零改造。

## 4. 检查点

- 全部 endpoint 可用；`GET /v1/changes?after_seq=0` 全量可拉、断点续读正确。
- `?processing_run_id=` 可读历史 run 快照；默认只读 active run。
- 四个错误码各有 contract test；GONE_SUPERSEDED 携带 superseded_by。
- unit 响应携带 asset_uri 且视图/表无此列。
- API 不返回绝对路径 / 私有状态 / 内部异常堆栈；reader 角色连接无法写。
- 分页游标在数据插入期间稳定（keyset 无重复/遗漏）。

## 5. 测试要求

contract：schema↔视图↔DTO 三方列集一致；错误码四例；asset_uri 派生。
集成（DB-gated）：分页游标翻页完整性；filings/latest 的取代/最新语义；changes 幂等重读；
admin POST 全链（register→parse→build→publish→units 可读）；权限（reader 只读）。

## 6. Definition of Done

- 三个本地样本经 admin API 全链处理后，units/changes/source-ref 均可经 HTTP 读取；
- OpenAPI 与五个 JSON schema 提交并有 contract test 守护；
- acceptance-matrix A22/A23 及错误码行置 pass。

## 7. 明确不做

- 不做鉴权/多租户（本地单用户服务）；不做 MCP 包装（预留映射）；不做全文检索/向量检索；
- 不做写侧批量接口（07 的 sync 走内部 use case，不走 HTTP）。

## 8. 常见失败与处理

- 视图列与 schema 漂移：contract test 先红——先改视图迁移或 schema，再改代码。
- 大 payload 响应慢：payload 完整返回是契约，不截断；必要时上游加 `fields=` 白名单参数（可选）。
- L1_PROCESSING_REQUIRED 误报：判定只依赖 document.status + active run 存在性，不看文件系统。
