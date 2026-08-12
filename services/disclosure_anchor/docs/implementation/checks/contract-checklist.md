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
tracked_company
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
GET /v1/units/{asset_id}/evidence/{sha256}
GET /v1/filings/latest
GET /v1/changes
GET /v1/tracked-companies
```

检查项（语义细则以 milestone 06 为唯一权威，本清单只列覆盖面）：

```text
默认返回 active run
显式 processing_run_id 可查历史 run；单 asset_id 解引用永远可用并携带 is_active_run
分页参数存在（limit 默认 100 / 上限 1000；keyset 游标语义见 06 §3.2）
错误响应不泄露内部堆栈
响应不含绝对路径
错误码枚举：L1_PROCESSING_REQUIRED / NOT_FOUND / CONTRACT_VERSION_MISMATCH /
           GONE_SUPERSEDED / VALIDATION_ERROR / EVIDENCE_INTEGRITY_ERROR
           （触发条件见 06 §3.3 与 evidence endpoint 契约）
unit 级 DTO 派生字段全集 = {asset_uri, evidence_refs}，source_ref 级派生字段全集 =
  {evidence_refs}（仅 API 序列化层派生，不入库、不进 *_v1 视图，DERIVED 白名单排除）；
  evidence_refs 只含可请求 URI、sha256、media_type、size_bytes，不含 role/path；
  is_active_run 自 0011 起是 document_units_v1 / source_refs_v1
  的真实视图列（round3 P1#7：DB 直读方可直接过滤 active run）
tracked_company DTO 派生字段全集 = {effective_lookback_days / effective_sync_seconds /
  effective_process_classes / sync_state}（级联与 due 判定在 API 层解析——全局 policy/
  间隔是配置文件与 env，视图只暴露 raw 覆盖列，NULL=继承；DERIVED 白名单排除）
0020 起 tracked_companies_v1 追加生命周期列：legal_name_status（pending/resolved，
  占位名判别）/ last_synced_at（checkpoint 时间，NULL=从未同步）/ synced_through
  （cursor window_end 覆盖日期）
scope keys 过滤参数可用（filing_type / payload_kind / heading_prefix（数组前缀语义，
  见 06 §3.8）/ semantic_key（兼容召回 primary 或 secondary route）/
  semantic_keys_any / semantic_keys_all /
  quality_status 等）
0010 起 document_units_v1 追加 applicability / page_no 列（applicability：
  'applicable'|'not_applicable'|NULL，节适用性声明的一等筛选列，payload 保持纯原文，
  部分索引 ix_document_unit_applicability；page_no：artifact_locator 首页码提升列）。
0007 起 document_units_v1 追加 6 列：asset_kind / observed_at / source_tier /
  trace_level / raw_file_hash / query_projection_hash
  （0034 当前列全集 = **39 列**；恢复 semantic_keys 与继承的 content_categories；publisher/market 只留 documents_v1/document_categories_v1）
0007 起 change_events_v1 追加 change_kind（真实列）/ subject_kind / subject_ref /
  source / contract_version
0007 起 documents_v1 追加 contract_version / company_ref / security_ref / source_ref /
  supersedes 链 / superseded_by_document_id / provider_metadata
0011 起 document_unit.payload_kind 增加 'mixed'（round3 P0#1 业务语义块：
  payload.parts 承载同一 source-proved 结构区间内的有序浅内容；精确 provider type 留在
  ProviderDocument，粗 kind/source owner 留在 locator，不在 payload 重复；document/section 由 title/headpath/locator 推导；
  监管 taxonomy 不参与切分）
0034 恢复 semantic_keys 存储/GIN/API 集合过滤；nullable scalar semantic_key 是 primary，
  数组是完整有序 route set。Provider writer 不做 L1 业务 taxonomy，当前两者都写 NULL；
  不得用键改变 source payload/boundary，也不得恢复旧词面规则堆
0015 起 document_units_v1 增加 heading_path_text（视图内派生的面包屑文本
  "第八节 财务报告 > … > 75、其他综合收益"——多级标题的可检索形态；不入库、
  不进哈希；06R 投影将对同一字段建 FTS 索引）
0014 起 document/documents_v1/document_units_v1 增加 disclosure_topics（F006V→
  topic_map.json 派生的二级分类数组，GIN 部分索引；filing_type 保持粗桶，
  round9 用户裁决"两三级分类合理"；web 兜底通道无 F006V → null，0021 起
  title_topic 标题命中会填充无码文档的 topics（无命中仍为 null））
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
disclosure_public.tracked_companies_v1
disclosure_public.unit_search_projection_v1
disclosure_public.unit_body_search_windows_v1
disclosure_public.unit_search_atoms_v1
```

`unit_search_projection_v1`（0025+0028，06R 派生检索投影层）：原 11 列及顺序保持不变；
`unit_body_search_windows_v1` 只承载 PostgreSQL 无法无损表示的 body token 连续窗。两者全部
可由已持久化 unit
确定性再生，不进 content/query_projection 哈希、重建不产生 outbox 事件；non-evidence 派生面，
与 documents/units 事实视图区别对待。

`unit_search_atoms_v1`（0030）列顺序固定为 `asset_id / atom_index / atom_text /
retrieval_rules_version / built_at`；每行来自 explicit search target 的一个非空叶子，禁止跨
target/part 连接。`atom_text` 是 NFKC+casefold 候选投影，不是证据。

`processing_runs_v1`（0031）暴露
`artifact_owner_processing_run_id` opaque provenance id，但继续禁止暴露
`normalized_ir_relpath` / `provider_document_relpath` / `parser_artifact_relpath`。parse owner=self；
rebuild owner 必须解析到同 document 的根 parse run，且 producer/owner artifact hash
一致。0032 只增加 private provider path 并将新 writer 队列限制为 provider-native；public
view 列集不变。

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
applicability
page_no
artifact_locator
evidence_refs（API 派生；URI/sha256/media_type/size_bytes）
```

L2 引用 source_ref 后，应能回到：

```text
原始 PDF hash
处理 run
unit payload snapshot
artifact locator
locator 绑定的 hash-addressed evidence bytes（仅能经 unit evidence URI 读取）
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

## 6. 契约变更记录（append-only）

2026-07-14（round23 上线加固）——公开读契约新增，随 `export_contracts` 重导：

```text
GET /v1/documents、/v1/filings/latest 新增查询参数 disclosure_topic（jsonb ? 存在判定；空白值 VALIDATION_ERROR）
GET /v1/classification 新端点：class_map 版本 + 31 处理类（含 processing_policy 处置）+ classification_rule 版本集
GET /v1/tracked-companies 新增 keyset 分页（cursor/limit，与 documents 同风格）；GET /v1/tracked-companies/{code}?exchange= 单条（404=NOT_FOUND）
GET /v1/health 响应新增 queues 对象（队列/死信/重试中文档/backfill 水位/最近事件时间；引擎不可用时为 null，不影响 status 语义）
admin 面（不进导出契约）：PUT tracked-companies 响应新增 action/cleared_overrides/status_change；entries min_length=1；
  POST {code}/sync 新增 window_start/window_end（与 window_days 互斥）；Bearer token + 回环双闸（401/403/409 运行期码）
```

2026-07-14（round24 查询面补齐，用户裁决"最小改动"）：

```text
GET /v1/documents、/v1/filings/latest：filing_type / disclosure_topic 支持逗号分隔多值（单值行为不变）；
  新增 content_category（按 jsonb 元素 code 或 name 命中，多值同上）；新增 title_contains（ILIKE 子串，
  LIKE 元字符转义，≤100 字符）
读路由（documents/filings/units/tracked/changes）未知查询参数 → 422 VALIDATION_ERROR（此前静默忽略）；
  health/admin 保持宽松
```

2026-08-12（0033 开发期 Unit schema 收敛，用户明确授权原地清理后重放）：

```text
document_units_v1 / DocumentUnitV1 删除 semantic_keys、publisher_categories、market、content_categories；
  三维分类事实继续由 documents_v1 / document_categories_v1 暴露
units API 删除 semantic_keys_any / semantic_keys_all；保留 v0.8 要求的 nullable scalar semantic_key 精确过滤
Provider writer 不再写 document_content 占位语义，semantic_key=NULL；mixed parts 不再重复 provider_type
精确 provider type 仍在 hash-bound ProviderDocument，coarse part kind/source owner 仍在 provider_unit_locator.v1
迁移前只读审计：15,690/15,690 历史 Unit 的 semantic_keys 都与单值 semantic_key 完全相同，
  0 行含 plural-only 信息；document_units_v1 无下游数据库依赖视图
本服务尚无生产/外部消费者，用户授权保留 document_unit.v1 做开发期原地删字段；若存在真实消费者则必须升 v2
不就地 NULL 旧 semantic_key（会破坏 query_projection_hash）；清理开发库后由新 writer 全量重放，
  同时替换因 mixed payload 去重而变化的 content_hash
```

2026-08-13（0034 Unit 检索路由纠偏）：

```text
0033 的 15,690 行审计只证明当时 writer 写出了 duplicate-only 数据，不证明 plural route capacity 无用
恢复 semantic_keys：首项必须等于 semantic_key；secondary keys 为 mixed Unit 提供完整 recall；GIN + any/all API 恢复
恢复 content_categories 到 document_units_v1 / DocumentUnitV1，但值仍由 Document join 继承，不复制进 Unit 表或全文 token
publisher_categories / market 保持 Document-only
当前 Provider writer 没有可信分类器，semantic_key/semantic_keys 都写 NULL；不伪造 document_content
singleton 数组不重复改变 query_projection_hash；只有真实 secondary route 扩展 query hash，避免无信息的全量哈希翻转
```
