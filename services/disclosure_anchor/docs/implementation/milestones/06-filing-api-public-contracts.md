---
id: disclosure_anchor_milestone_06_filing-api-public-contracts
project: disclosure_anchor
title: Filing API 与 public 契约
status: complete
created_at: 2026-06-26
updated_at: 2026-07-07
depends_on: milestone 05
delivers_to: milestone 07 / L2 / MCP 包装
---

# Milestone 06: Filing API 与 public 契约

> 2026-08-14：本文件保留里程碑完成时的历史契约。0034/0036 恢复 Unit 的
> `semantic_keys`、`semantic_keys_any/all` 与 `section_keys`；0037 又从 Unit 读面删除
> 继承的 `content_categories`，使全部 provider facets 保持 Document-only。当前列集以
> contract-checklist 为准。

把 `disclosure_public.*_v1` 的读契约以 HTTP API 形式暴露（协议 §3.11 的 L1 对外契约六条），
并冻结 JSON schema。API 是视图的薄投影：**不引入视图之外的新语义**，读侧语义与
`disclosure_public.*_v1` 逐列一致。

## 1. 前置依赖

- 05 完成（active run、unit 数据、事件流）；里程碑完成时 `document_units_v1` 为 41 列（0037 当前为 39 列；
  完整列集以 contract-checklist §2 为准；32 列仅是 0007/0008 的历史基线），包含 active run、
  applicability/page_no、semantic_keys、heading_path_text 与三维分类投影；
  processing_runs_v1：builder_rules_version（0008，是 run 列不是 unit 列）；
- `document.status` 枚举（04R-D4）——`L1_PROCESSING_REQUIRED` 的判定数据面。

## 2. Endpoint 清单与语义

只读（reader 角色连接，经 public 视图，不回读私有表）：

```text
GET /v1/health                     HealthResponse 扩展定死：+ migration_head: str|null
                                   （SELECT version_num FROM disclosure_ops.alembic_version）、
                                   + data_root_mounted: bool（settings sentinel 存在）；
                                   status ∈ {"ok","degraded"}——DB 查询失败或 sentinel 缺失
                                   → "degraded"（HTTP 仍 200，migration_head=null）
GET /v1/documents                  过滤：company_ref / security_code / filing_type /
                                   report_period / announcement_date_from,to / status；游标分页。
                                   默认返回全部文档**含已被取代者**（行携带
                                   superseded_by_document_id 由消费方过滤；不加
                                   include_superseded 参数；"排除已取代"语义只由
                                   /v1/filings/latest 提供——实施方案 §14.4 草案的
                                   "默认排除"不采用，以本条为准）
GET /v1/documents/{document_id}    单档；被取代时正常返回但携带 superseded_by_document_id
GET /v1/documents/{id}/runs        该 document 全部 processing_run（历史可见）；
                                   不分页，ORDER BY started_at DESC, processing_run_id DESC
GET /v1/documents/{id}/units       默认 active run 的 units；?processing_run_id= 读历史快照
GET /v1/units/{asset_id}           单 unit 全量（payload 完整返回，不截断）。unit 不可变、
                                   旧引用永远可解析（协议 §2.6）：历史 run 的 unit 照常返回，
                                   响应携带 is_active_run: bool——不做"默认只返回 active"
                                   （E12 评审建议被否：会打断 L2 已持有 asset_id 的解引用）
GET /v1/units/{asset_id}/source-ref source_refs_v1 单行投影
GET /v1/units/{asset_id}/context   运行时上下文包装（实施方案 §9.6/§14.3）。响应结构定死：
                                   {"asset_id","asset_uri","is_active_run",
                                    "document": <document.v1 DTO 全量>,
                                    "heading_path","title","payload"
                                    [,"excerpt","start","end","excerpt_hash"]}
                                   excerpt 算法定死：源串 = canonical_json(payload)（复用
                                   05-U2 的 json.dumps 参数）；start/end 为 Unicode 码点偏移，
                                   start=0、end=min(max_chars, len(源串))，excerpt=源串[start:end]；
                                   excerpt_hash = "sha256:"+sha256(excerpt.encode()).hexdigest()；
                                   源串短于 max_chars 仍返回四字段（end=len）；未带 max_chars
                                   不返回这四字段。纯运行时派生，不持久化、不构成证据身份——
                                   L2 用摘录须自存快照
GET /v1/filings/latest             语义用一条 SQL 钉死：
                                   SELECT DISTINCT ON (company_ref, filing_type, report_period) *
                                   FROM disclosure_public.documents_v1
                                   WHERE superseded_by_document_id IS NULL [AND 过滤条件]
                                   ORDER BY company_ref, filing_type, report_period,
                                            announcement_date DESC NULLS LAST, document_id DESC
                                   （report_period 为 NULL 视为同一组，参与分组不排除；
                                   外层结果按 (announcement_date DESC NULLS LAST,
                                   document_id DESC) 排序，游标与 /v1/documents 相同）
GET /v1/changes?after_seq=&limit=  change_events_v1 按 seq 升序。响应同样返回 next_cursor
                                   （JSON {"seq": <末行 seq>}，空结果 null）；cursor 与
                                   after_seq 同时携带时 cursor 优先；after_seq 语义 =
                                   seq > after_seq（严格大于）
```

写侧（admin，本地运维用，POST，全部同步执行、成功 200；请求/响应体定死）：

```text
POST /v1/admin/documents/register-local-pdf
  请求 = RegisterLocalPdfCommand 字段（file_path 为服务本机绝对路径字符串）
  响应 = RegisterLocalPdfResult 字段，但 quarantined_path 只返回 basename（不泄绝对路径）
POST /v1/admin/documents/{id}/parse
  请求 = ParserOptions 字段全集（method/backend/language/formula/table/start_page/
         end_page/timeout_seconds，全部可选）
  响应 = ParseDocumentResult 字段（relpath 均为相对路径，可返回）
POST /v1/admin/documents/{id}/build-units
  请求 = {}；响应 = {"processing_run_id","unit_build_status","unit_count"}
POST /v1/admin/runs/{processing_run_id}/publish
  请求 = {"allow_empty": false [, "reason": "<str>"]}
  响应 = {"document_id","processing_run_id","is_active": true}
```

## 3. 契约细则

1. **DTO = 视图列**：`document_unit.v1.json` 等 schema 从 0007 后的视图列生成，逐列同名同义；
   不得缺列、不得改名。unit 级 DTO 派生字段**全集** = {asset_uri, is_active_run}
   （asset_uri = "asset://disclosure_anchor/v1/document_unit/{asset_id}"；均不入库不进视图，
   document_unit.v1.json 收录二者；列表与单查共用同一 DTO，列表行同样携带 is_active_run）。
2. **游标分页（live keyset，语义显式声明）**：列表接口统一 keyset 分页——响应携带
   `next_cursor`（base64(JSON) 不透明游标），请求带 `cursor=`；稳定排序键：
   documents 按 (announcement_date DESC NULLS LAST, document_id DESC)，
   units 按 (order_index ASC, asset_id ASC)，changes 按 (seq ASC)。
   游标 JSON 内容定死：documents={"announcement_date":"YYYY-MM-DD"|null,"document_id":"<id>"}；
   units={"order_index":<int>,"asset_id":"<id>"}；changes={"seq":<int>}；最后一页
   next_cursor=null。documents 续页谓词定死（第一排序键可空，朴素行比较会在 NULL 上漏行）：
   cursor.announcement_date 非 null → WHERE announcement_date IS NULL OR
   announcement_date < :ad OR (announcement_date = :ad AND document_id < :id)；
   为 null → WHERE announcement_date IS NULL AND document_id < :id。
   units 续页谓词：WHERE (order_index, asset_id) > (:oi, :aid)（行序比较）。
   limit 统一：全部列表端点 limit 参数默认 100、上限 1000，超上限 → 422。
   承诺：同一查询同一方向下**无重复、不漏翻页开始前已存在的行**；
   **不承诺**翻页过程中看见新插入的数据——增量由 change feed 补齐，不引入 snapshot_seq。
   禁止 offset 分页。scope-key 过滤 + 游标分页是契约义务（协议 §3.11 第 3 条）。
3. **错误模型（service-purpose §12.3）**，响应体统一
   `{"error_code": ..., "message": ..., "detail": {...}}`：

```text
NOT_FOUND                  → 404
GONE_SUPERSEDED            → 410，detail.superseded_by = superseded_by_document_id。
                             ?reject_superseded=true 仅 GET /v1/documents/{document_id} 与
                             GET /v1/documents/{id}/units 两端点支持（其余端点不声明该参数）；
                             默认正常返回旧档；contract test 以 documents/{id} 为准
L1_PROCESSING_REQUIRED     → 409，仅当 document **无 active run**（status ∈ {registered,
                             parsed, parse_failed} 且 current_processing_run_id IS NULL）
                             且请求**未携带** ?processing_run_id= 时；detail.status 携带
                             当前状态。显式携带 processing_run_id 时按该 run 直查
                             （run 不存在或不属于该 document → 404 NOT_FOUND）。
                             有 active run 但最新 run 失败 → 200 正常返回旧 active 数据 +
                             **响应体顶层字段** "warning": "LATEST_PROCESSING_FAILED"|null
                             （不用响应头）；触发条件定死 = 有 active run，且按 started_at
                             最大的 run 不是 active run 且 (status='failed' OR
                             unit_build_status='failed')（B1：published 不因重解析失败降级）
CONTRACT_VERSION_MISMATCH  → 400，请求头 X-Contract-Version 携带且值 ∉ {"v1"}（受支持集合
                             固定 {"v1"}；未携带 = 默认通过）；detail =
                             {"requested": <请求值>, "supported": ["v1"]}
VALIDATION_ERROR           → 422，参数/游标校验失败（坏 base64/JSON 游标、limit 超上限、
                             非法过滤值）；沿用统一 envelope，detail 携带字段级错误列表
                             （§12.3 是"至少包含"，允许补充非业务码）
```

   错误响应不含内部堆栈、绝对路径、私有 schema 信息。
4. **change feed 消费协定**（协议 §2.8）：`seq` 单调、可断点续读；at-least-once + 消费端幂等；
   同一 subject 内保序；事件携带 `event_kind` / `change_kind` / `subject_ref` / `source` /
   `contract_version`；下游失效只由 materialized 触发。
5. **OpenAPI 与 schema 冻结（从代码导出，不手写）**：新建
   `src/disclosure_anchor/cli/export_contracts.py`，命令
   `PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.export_contracts`——
   (a) create_app().openapi() 经 yaml.safe_dump(sort_keys=True, allow_unicode=True) 写
   `contracts/filing_api.openapi.yaml`；(b) 七个 Pydantic 响应模型 .model_json_schema()
   经 json.dumps(indent=2, ensure_ascii=False, sort_keys=True) 写
   `contracts/public_models/{document,document_unit,processing_run,source_ref,change_event,document_category,tracked_company}.v1.json`。
   三方一致断言拆两层（contract 测试是 no-DB 的，读不到视图列）：
   (a) no-DB：tests/contract/test_filing_api_contracts.py 重新导出并断言与已提交文件
       逐字节一致，且对七个模型断言 set(schema["properties"]) == set(Model.model_fields)−DERIVED；
   (b) DB-gated：tests/integration/test_filing_api_views_contract.py 对每个视图断言
       information_schema.columns（table_schema='disclosure_public'）的列名集合 ==
       set(schema["properties"]) − DERIVED；
   DERIVED = {"asset_uri"}（仅 document_unit.v1；`is_active_run` 已是公开响应模型字段）。
6. **实现形态**：FastAPI router 按资源拆分（documents / units / filings / changes / admin)；
   engine 拓扑定死：读侧 router（documents/units/filings/changes/health）用
   `DISCLOSURE_READER_DATABASE_URL` engine（缺省回落 DATABASE_URL 并 doctor WARN），
   admin router 用 DATABASE_URL（app 角色）engine——"进程级单 engine" = 每个 DSN 一个
   进程级单例 engine（本期共 2 个；reader 角色写库会权限报错，不得混用）；
   响应模型 Pydantic，不返回 ORM 对象。
7. **MCP 映射预留**（协议 §3.11 第 6 条，不在本期实现）：查询→tool、取回→resource（按
   asset_uri）、变更→notifications；本期只保证 URI 与游标契约稳定，包装时零改造。
8. **unit 过滤与 heading 语义**：`/documents/{id}/units` 支持 `payload_kind` / `semantic_key` /
   `semantic_keys_any` / `semantic_keys_all` / `quality_status` / `heading_prefix` 过滤；旧的
   `semantic_key` 保持 v1 的兼容召回语义，同时命中 scalar 或 `semantic_keys` 数组，避免只有
   secondary key 的 mixed unit 漏召回；`semantic_keys_any/all` 提供显式集合语义。any/all 使用
   逗号分隔的受控键列表（原始项最多 50 个，随后稳定去重）；
   scalar/list 中每个 key 都必须是
   1–128 字符的小写 ASCII snake_case（字母开头），控制字符等非法值在 SQL 前返回 422。
   `heading_prefix` 语义 = heading_path **数组前缀
   匹配**（实现：GIN jsonb_path_ops containment 作候选过滤，命中后精确校验前缀，不把
   containment 当长期方案）。全文/向量检索不在本期：`GET /v1/search/units`
   （PostgreSQL FTS/pg_trgm + retrieval projection 派生层，边界见 05-U7）保留给 06R
   （06R 为规划中的检索投影里程碑，规格文档尚未编写），
   本期 DTO 与游标契约不得阻断其后续加入。

## 4. 检查点

- 全部 endpoint 可用；`GET /v1/changes?after_seq=0` 全量可拉、断点续读正确。
- `?processing_run_id=` 可读历史 run 快照；默认只读 active run。
- 四个错误码各有 contract test；GONE_SUPERSEDED 携带 superseded_by。
- unit 响应携带 asset_uri 且视图/表无此列。
- API 不泄漏内部信息的断言口径定死：对样本文档在全部端点（含 admin 与错误响应）的响应
  json.dumps 后断言不含四个子串：str(settings.data_root)、str(settings.shared_root)、
  "/Users/"、"Traceback"；reader 角色连接无法写。
- 分页游标语义符合 live keyset 承诺（无重复、不漏存量；新插入不承诺，测试按此断言）。
- 已 published 文档最新 run 失败：units 返回 200 + LATEST_PROCESSING_FAILED warning。
- 历史 run 的 unit 按 asset_id 可取回且 is_active_run=false。
- `/context`：excerpt 偏移与 excerpt_hash 一致、max_chars 截取正确；heading_prefix 过滤
  为精确数组前缀语义（非 containment 误命中）。

## 5. 测试要求

contract：schema↔视图↔DTO 三方列集一致（§3.5 两层断言）；错误码五例（含 VALIDATION_ERROR
坏游标用例）；asset_uri 派生。
集成（DB-gated）：分页游标翻页完整性（含 announcement_date=null 行的 NULLS LAST 续页）；
filings/latest 的取代/最新/同日 tie-break 语义；changes 幂等重读；
admin POST 全链（register→parse→build→publish→units 可读）；权限（reader 只读）。
全链测试的输入与门控由 machine-local 环境显式提供：`DISCLOSURE_MINERU_BIN` 与
`DISCLOSURE_TEST_<LABEL>_PDF`。任一路径不存在时 `unittest.SkipTest`，不再从仓库内的
历史 phase00 path reference 猜测本机文件。
register 参数用 05 §5 三样本表的对应值（short_announcement：security_code=002484、
exchange=szse、filing_type=other、report_period 省略、provider=cninfo、
provider_document_id=文件名去后缀）。

## 6. Definition of Done

- 每包提交门禁 = `make agent-check`（lint+严格 mypy+no-DB test+diff check，零违例基线）
  + live-DB `make test`（04R §6.1 2026-07-05 修订）；
- 三个本地样本经 admin API 全链处理后，units/changes/source-ref 均可经 HTTP 读取；
- OpenAPI 与七个 JSON schema 提交并有 contract test 守护；
- acceptance-matrix A16/A17/A18/A22/A23/A24/A41 置 pass
  （A41 = 错误码 contract test 行，已预登记；不得新增重复行）。

## 7. 明确不做

- 不做鉴权/多租户（本地单用户服务）；不做 MCP 包装（预留映射）；
- 不做全文/向量检索——`GET /v1/search/units` 与 retrieval projection 留给 06R（05-U7），
  向量（pgvector）与 LLM summary 在 06R 之后按需评估，不引入独立向量库；
- 不做写侧批量接口（07 的 sync 走内部 use case，不走 HTTP）。

## 8. 常见失败与处理

- 视图列与 schema 漂移：contract test 先红——先改视图迁移或 schema，再改代码。
- 大 payload 响应慢：payload 完整返回是契约，不截断；必要时上游加 `fields=` 白名单参数（可选）。
- L1_PROCESSING_REQUIRED 误报：判定只依赖 document.status + active run 存在性，不看文件系统。

## 9. 实施后修订（2026-07-07 记录）

- **DERIVED 集收缩为 `{asset_uri}`**（0011 迁移定案，phase008 round3 P1#7）：`is_active_run`
  自 0011 起是 `document_units_v1` / `source_refs_v1` 的**真实视图列**，不再是 API 派生字段。
  §3.1 的 "派生字段全集 = {asset_uri, is_active_run}、均不入库不进视图" 与 §3.5 的
  `DERIVED = {"asset_uri","is_active_run"}` 以本条为准修正：unit 级 DTO 派生字段全集 =
  `{asset_uri}`；`document_unit.v1.json` 的 properties 同时收录 asset_uri（派生）与
  is_active_run（视图列）；三方一致断言按 `DERIVED = {"asset_uri"}` 执行
  （tests/integration/test_filing_api_views_contract.py 与 contract-checklist §2 已同步）。
- §1 的 32/36 列与 0014–0016 的 41 列均为历史迁移口径；0037 后当前
  `document_units_v1` 为 **39 列**，`is_active_run` 是真实视图列，列全集以
  contract-checklist §2 为准。
- **2026-07-27 evidence bytes 契约补充**：unit locator 绑定的视觉 evidence 通过
  `GET /v1/units/{asset_id}/evidence/{sha256}` 以内容 digest 读取；请求不得携带 role/path，
  服务必须复核 run 的 NormalizedIR hash、v4 manifest 与 bytes 的 size/hash/media type。
  `document_unit` 的 DERIVED 集相应为 `{asset_uri,evidence_refs}`，`source_ref` 的 DERIVED 集为
  `{evidence_refs}`；新增公开错误码 `EVIDENCE_INTEGRITY_ERROR` 专用于已发布证据缺失或漂移。
