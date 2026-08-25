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
GET /v1/semantic-routes
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
  见 06 §3.8）/
  semantic_keys_any / semantic_keys_all /
  section_keys_any / section_keys_all /
  quality_status 等）
0010 起 document_units_v1 追加 applicability / page_no 列（applicability：
  'applicable'|'not_applicable'|NULL，节适用性声明的一等筛选列，payload 保持纯原文，
  仅由当前叶标题自身，或第一个实质/visual part 前 declaration-only leading part 中受控、成对且一致的
  勾选声明确定；不跨 Unit 继承，实质/视觉 carrier 后的 child-local selector、普通文字、双选、
  双空或冲突均为 NULL；部分索引 ix_document_unit_applicability；
  page_no：artifact_locator 首页码提升列）。
0007 起 document_units_v1 追加 6 列：asset_kind / observed_at / source_tier /
  trace_level / raw_file_hash / query_projection_hash
  （0039 当前唯一 v1 为 **40 列**，以 Unit 自有 `body_status` 取代 Document-only
  `content_categories`。semantic_keys=直接主题，section_keys=规范化章节位置）
0007 起 change_events_v1 追加 change_kind（真实列）/ subject_kind / subject_ref /
  source / contract_version
0007 起 documents_v1 追加 contract_version / company_ref / security_ref / source_ref /
  supersedes 链 / superseded_by_document_id / provider_metadata
company_ref / security_ref / security_code / exchange 当前均为 provider 获取与登记范围
  （source-scope identity），不是 PDF 正文 issuer 的 canonical 断言。v1 尚无 run-bound
  content-identity assessment；因此 documents/Units 的这些列以及 /v1/filings/latest 都不得
  单独作为 issuer-safe filter、same-subject join 或预测事实归因。母公司代发子公司附件必须在
  未来的 assessment/eligibility 公共契约中 fail closed；不得用名称匹配或改写 provider identity。
0011 起 document_unit.payload_kind 增加 'mixed'（round3 P0#1 业务语义块：
  payload.parts 承载同一 source-proved 结构区间内的有序浅内容；精确 provider type 留在
  ProviderDocument，粗 kind/source owner 留在 locator，不在 payload 重复；document/section 由 title/headpath/locator 推导；
  监管 taxonomy 不参与切分）
0034 恢复 semantic_keys 存储/GIN/API 集合过滤；nullable scalar semantic_key 是 primary，
  数组是完整有序 route set。Provider writer 的独立 route 阶段只接受 source-bound 闭集候选，
  仅 `body_status=content` 的答案载体可落 direct key；其唯一精确标题可确定性落键，歧义候选由 Luna 只选 candidate ID 或弃权；不得用键改变 source
  payload/boundary，也不得恢复旧自由词面/公司专例规则堆
0036 新增 section_keys 存储/GIN/API 集合过滤；只从可靠 heading_path 的显式结构容器
  做精确、可重建的结构归一（定期报告 context-container；事件公告命中 filing_type/
  authoritative disclosure_topics scope 的窄
  section-container），不调用模型，不占 semantic route cap，不改变 Unit 边界
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
disclosure_public.unit_search_row_atoms_v1
```

`unit_search_projection_v1`（0025+0028，06R 派生检索投影层）：原 11 列及顺序保持不变；
`unit_body_search_windows_v1` 只承载 PostgreSQL 无法无损表示的 body token 连续窗。两者全部
可由已持久化 unit
确定性再生，不进 content/query_projection 哈希、重建不产生 outbox 事件；non-evidence 派生面，
与 documents/units 事实视图区别对待。

`unit_search_atoms_v1`（0030）列顺序固定为 `asset_id / atom_index / atom_text /
retrieval_rules_version / built_at`；每行来自 explicit search target 的一个非空叶子，禁止跨
target/part 连接。`atom_text` 是 NFKC+casefold 候选投影，不是证据。

`unit_search_row_atoms_v1`（0044）列顺序固定为 `asset_id / row_atom_index /
table_target_id / source_row_index / row_text / retrieval_rules_version / built_at /
row_search_tsv`。它只为同一 explicit table target 中机械闭合的三列问答表生成：可选单行总标题，
随后精确表头 `序号 / 提问内容 / 回复内容`，数据行必须无 span、序号从 1 连续且问答均非空；任一
歧义整表不生成 row atom。`row_text`/`row_search_tsv` 只提供“问题词 AND 回答词位于同一源行”的
候选定位；`source_row_index` 是原 HTML table 中从 0 开始的 `<tr>` 序号。命中仍引用 parent Unit
与 source row，不是新 Unit 或证据。每个 row vector 写入前复用
PostgreSQL 物理安全探针；超限行安全省略，parent word/leaf/window 通道继续作为召回兜底。
private parent 保存 safe-row count + manifest hash；delta 同时核对实际 child 数量与 manifest identity，
只有 builder 同事务完成后才置 private manifest-ready；缺失、额外、旧 child 或迁移重建后的 unready
parent 会重建完整 owning run，strict-abstain/安全省略且 ready 的零 child 可保持 quiet。

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
Provider writer 使用版本化受控词表 + filing scope + Unit-local candidate gate；仅有内容 Unit 的唯一精确标题可确定性落键，
歧义候选由闭集 Luna 裁决，Build receipt 冻结、Publish 只重放；证据不足仍为 NULL，不伪造 document_content
singleton 数组不重复改变 query_projection_hash；只有真实 secondary route 扩展 query hash，避免无信息的全量哈希翻转
```

2026-08-13（0036 直接主题与章节位置分权）：

```text
semantic_key(s) 只保存 Unit 自身直接主题；不再把父章节混进模型候选或 receipt
section_keys 保存已接受 heading_path 的精确结构容器链：定期报告 context-container 与事件公告
命中 filing_type/authoritative disclosure_topics scope 的窄 section-container；完整根到叶、无相似/包含匹配
只有 semantic_keys 进入全文 key-token 检索；section_keys 独立进入 query_projection_hash，并仅由
any/all 结构过滤参与 L2/L3 联合召回，避免把父章节词复制到每个正文 Unit
content_categories 仍仅是 Document provider facet，经 Unit public view 继承；不能填充任一 Unit route
```

2026-08-14（0037 Unit 与 Document facet 分权）:

```text
document_units_v1 / DocumentUnitV1 删除 content_categories；Unit 公共列由 40 收敛为 39
documents_v1 / document_categories_v1、Document materialized facet、CNInfo F006V 原始事实与 documents API/filter 保留
L2 如需 provider facet 粗筛，先筛 Document 再按 document_id 获取 Units；Unit 主题召回只使用 semantic_keys、section_keys 与 lexical search
```

2026-08-15（0038 Unit 公共读契约版本化修复）:

```text
0037 删除 public v1 字段属于 breaking change；不改写已应用迁移，追加 0038 修复版本边界
document_units_v1 恢复末列 content_categories 仅作 deprecated compatibility join，contract_version=document_unit.v1
document_units_v2 暴露无 content_categories、带 body_status 的 40 列当前读面，contract_version=document_unit.v2
v2 consumer 如需 provider facet，先读 documents_v1/document_categories_v1 再按 document_id 取 Units
Filing API 当前仍只有 v1；X-Contract-Version:v2 在完整 v2 HTTP 契约落地前继续 fail closed
0038 downgrade 只删除 v2，并保留已恢复的 40 列 v1；绝不以回滚名义重新暴露 0037 的已知破坏形状，继续向更早 revision 回退仍由各自 migration 处理
```

2026-08-19（0039 Unit 公共读契约单版本收敛）:

```text
服务仍处开发期，用户裁决只保留一个 Unit 公共契约；追加迁移而不改写 0038 历史
删除 disclosure_public.document_units_v2；不保留 alias、双 serializer 或第二组 API 路由
唯一 document_units_v1 使用原 v2 的干净 40 列结构：末列 body_status，无 content_categories
contract_version=document_unit.v1；DB 直读、Filing API、导出 schema 与 L2 验收脚本统一消费 v1
provider content_categories 继续只在 documents_v1/document_categories_v1，不从 Unit 内容伪造
```

2026-08-20（0040 Unit 路由集合总化）:

```text
私有 document_unit 继续以 NULL 表示没有直接/结构 route，不制造任何占位 key
唯一 document_units_v1 以 COALESCE 将 semantic_keys / section_keys 的缺失投影成 JSONB []
nullable scalar semantic_key 保持 NULL；public Pydantic/OpenAPI 两个 plural 字段改为 required array
列名、顺序、数量、contract_version 与 query_projection_hash 语义均不变
```

2026-08-20（0041 移除公共 scalar semantic_key，用户决策）:

```text
scalar 恒等于 semantic_keys[0]，公共面保留只会诱导 lead-key 单键过滤而漏召回；删列后 v1 为 39 列
私有 document_unit.semantic_key 列、索引、unit_hashing 与 receipts 完全不变；仅公共读面收窄
units API 同步移除 semantic_key 查询参数；单键召回 = 单元素 semantic_keys_any；any/all 集合过滤不变
downgrade 恢复 0040 的 40 列形状；outline 的 lead-key 低估随后由 0042 裁决修复（见下一条）
```

2026-08-22（0045–0047 Unit build 终态、ACL 与私有 scalar 收口）:

```text
0045：processing_run 增加 v2 receipt relpath/version、semantic_adjudication_status、
      degraded/failover counts 与闭合 summary；将历史 JSON literal null 清为 SQL NULL；
      新增 disclosure_ops.unit_build_terminal_v1，并仅授 disclosure_app SELECT
0046：撤销 disclosure_reader 对 disclosure_core.provider_category 的遗留 SELECT；
      分类消费仍走 document_categories_v1，不开放私有字典表
0047：升级前逐行验证 semantic_key 与 semantic_keys[0] 无差异；验证通过后删除私有 scalar、
      scalar 索引与成对 CHECK，只保留 semantic_keys JSONB（SQL NULL 或 1..8 个元素）
公开 document_units_v1、Filing API v1 与 change feed v1 的列/行为不变；旧 snapshot/hash 的
lead 兼容值从 semantic_keys[0] 派生，不再是 DB 列
```

2026-08-22（0048 Unit build 修复代际收口）:

```text
pending_build_v1 与 unit_build_terminal_v1 以 (started_at, processing_run_id) 为稳定顺序，
排除已存在后续 status=succeeded + unit_build_status=succeeded 代际的旧 not_started/failed run；
若成功代际之后又发生新失败，只隐藏更早失败，最新失败仍进入 queue/terminal/health/doctor/dead-letter。
不删除历史 processing_run，不改 public v1/change feed，不破坏 artifact_owner lineage。
```

2026-08-22（0049 非 superuser migration-head 健康检查）:

```text
disclosure_app 只获得 disclosure_ops.alembic_version 的 SELECT，以便 health/doctor 比对当前 head；
UPDATE/INSERT/DELETE/DDL 仍拒绝；0024 已明确授予 disclosure_reader 与 future_l2_reader 同一窄读
权限，0049 不扩大它们的既有权限。
```

2026-08-22（receipt v2 / 0047 NULL 修复与 clean v1 冻结）:

```text
semantic_route_receipt.v2 的历史 group 由 receipt 中相同 group_hash 的连续成员反推；fresh
input_hash 重算必须等于该 group_hash，组内 attempt/result lineage 必须逐成员完全相同，覆盖与顺序
必须闭合。Replay 不再用当前 semantic batch size 重分历史 v2；v1 只读兼容路径不变。
0047 已在开发库应用且不可改写。Alembic online env 在任何仍有私有 semantic_key 列的迁移调用中，
先以 NULL-safe CASE 比较 scalar 与 plural 首项；scalar-only/plural SQL NULL 会在 0047 前 fail closed。
0050_verify_unit_routes 只断言删除后仍可证明的事实：scalar 列不存在，semantic_keys/section_keys 为
SQL NULL 或非空、去重、英文规范 key 数组；它不声称能从已删除列证明过去不存在 scalar-only 行。
当前开发库的 lossless 证据来自已记录的 512 行 source/receipt/live replay；若日后 replay 不一致，
只能走正常 rebuild/publish 代际修复，禁止手工 UPDATE 猜 route。
0039/0041 的 clean document_unit.v1 是用户在无生产、无真实消费者阶段明确批准的一次性收口；
现由 literal model-field/required/enum、byte-exact exported schema、SQL view 列顺序和 API filter golden
共同冻结。自本记录起任何 breaking 变化必须新建 v2，并按顶层协议 §2.7 并行保留 v1 弃用期。
```

2026-08-20（0042 outline 全键聚合 + taxonomy r55 + 检索 rp v4）:

```text
document_outline_v1 聚合 semantic_keys 数组的去重元素（此前只聚合内部 lead key，低估节点召回面）
元素强转 varchar(128) 保持公共列类型不变；列名/顺序/document_outline.v1 契约号不变；downgrade 恢复 lead-key 形状
taxonomy r55（financial r28/events r41）：新增 controlling_shareholder_profile 与 share_pledge
（质押别名锚定 股份/股权/累计质押股份，资产抵押质押类标题不落键）；内控/环境/资金占用 checklist 补模板 heading 别名
router v89：标题尾部勾选式"√适用/□不适用"标记为源噪音剥离；裸 适用/不适用 文本保留
检索规则 rp v4：key_tokens = direct keys + 各键中文规范标签的分词 token；section 键仍为纯过滤通道不进全文
```

2026-08-20（0043 visual-only mixed Unit 的公共 body_status）:

```text
mixed payload 若 parts 中除 content_artifacts 外没有任何非空文本/表格/列表/标题/脚注/代码/公式，
仍保留原始视觉证据与 Unit identity，但 document_units_v1 不得把它标为可回答 content：
有 title 时 body_status=heading_only，无 title 时 body_status=empty。只改公共派生判定，不删 source row，
不改 payload/hash/lineage；普通 mixed content 仍为 body_status=content。downgrade 恢复 0041 判定。
```

2026-08-20（0044 严格 Q&A 同行检索投影 + 检索 rp v5）:

```text
不拆 public Unit、不改变 Unit identity/hash；新增可完全再生的 unit_search_row_atoms_v1
仅对 source-bound 三列问答表做 strict whole-table admission；单格 Q&A、畸形/跨格/span/断号全部弃权
同行 AND 只在一个 row_search_tsv 内求交；跨行词不能拼成命中，引用仍回 parent Unit/source row
row tsv 写入前逐行走 PostgreSQL exact safety probe；不安全行省略，parent/leaf/lossless windows 不受影响
视图固定 8 列；disclosure_app/disclosure_reader/future_l2_reader 仅 SELECT，不得 DML
任何 tokenizer、HTML admission 或 row projection 变化均升 retrieval_rules_version 并重建
```

2026-08-14（semantic route 公共目录）：

```text
GET /v1/semantic-routes 返回 semantic_routes_catalog.v1；直接从当前 taxonomy 投影 key/description/
labels/scopes/usable_as_section_key，taxonomy_version 随资源版本变化
usable_as_section_key 对当前所有公开 route 为 true：router 可把任意 scope-valid exact heading 发为
section_key；container 标志只控制继承/直接路由，不是 eligibility。document_content fallback 不作为
真实 route 暴露；L2/L3 不复制 L1 私有 JSON
```

2026-08-14（provider Unit v5 / locator v2 数字保真）：

```text
ProviderDocument 仍逐字保存 MinerU 3.4.4 Hybrid-medium 输出；不引入双 parser 或第二搜索流
admission 可在同一 raw PDF hash/page count、同一 MinerU text bbox 与 raw block 下读取 native text
只有 native 相对 MinerU 仅新增完整数字核心（可连同或保留 `%/‰`），且除数字 token 位置相邻 ASCII 横向空格/Tab
可随 token 缺失或保留占位外，其余字符（包括宽窄字符、标点、空白）与已有数字原序逐字相等时才投影
唯一 reader 规范化为非首尾孤立 PDFium `CRLF`，并仅在同一多行观察移除一个矩形末尾空格；CRLF 周围
空白、多个末尾空格、NUL、裸 `CR/LF`、空白行均拒绝；数字替换/重排、表格、
非数字差异、旋转/页面形状不闭合、高度重叠 bbox 和歧义不修
provider_unit_locator.v2 记录 source_index/payload ordinal/raw block hash/provider+source text hash/source kind；
provider_unit_locator.v3 进一步让 heading_chain 明确绑定 payload ordinal，以支持 Provider 表格 block 中唯一的强编号 caption 标题 occurrence；
provider_unit_locator.v4 保留 v3 能力并新增 source-bound `continuation_fragments`、
`source_pdf_native_identifier.v1` 和 `source_pdf_native_table_quality.v1`。identifier 只允许完整数字 atom、
数字相邻 ASCII 空格与至多一个 source-proved 开引号差异；其他空白逐字相等。table quality finding
只把空尾、畸形数字分组或 numeric token 变异标为 `needs_review`，不得重写 table HTML 或合成 cell
provider_unit_locator.v5 保留 v4 全部能力；`source_pdf_native_identifier.v2` 只允许
同一 text bbox 内恰好一处 `=` 与至少一个完整数字 atom 同时漏失，且 provider 不得已有 `=`；
只可在实际删除的 `=`/数字 atom 位点消费 provider/native 的 ASCII 空格或 Tab 占位；
`source_pdf_native_text_quality.v1/native_text_omission` 只作 finding，不改 payload。上一页表尾的 exact
`page_footnote` 可作 physical continuation boundary，但下一页表前脚注仍阻断，且脚注不进入 semantic furniture
每个 locator 覆盖本 Unit blocks 与 heading chain 的 repair 依赖；Publish 从不可变 PDF 重放校正；
reader pin `pypdfium2==5.13.0`。provider_unit_locator.v6 只新增严格两列 USCC 的
单一 `O/0` checksum 冲突与 CJK `〔〕` 单括号漏失两种 finding-only vocabulary，均不改 payload；
历史 locator v1-v5 继续按原 vocabulary 读取：v2/v3 只接受 numeric.v1，v4 才接受
identifier.v1/table-quality，v5 才接受 identifier.v2/text-quality，且不得声明 v6 evidence；v1-v3
也不得声明 v4 才引入的 `unit_title_fragment` search destination。当前 writer
`provider_unit_locator.v8` 保留 v6 的 source-bound vocabulary，但不凭普通 paragraph
整句词面创造 heading placement。历史 v7 只读，仍可解码其 `statutory_template` placement；v1-v6
不得声明该 v7 vocabulary，v8 writer 也不得重新发出。若 ProviderDocument 没有明确标题 occurrence，
prompt 与 selector 留在既有 Unit；只有完整 headed Unit 在同页按 source 顺序恰含一个 prompt part 和
一个 closed selector part也不足以证明 Unit-level ownership，仍保持 NULL，直到 Provider 提供明确
prompt role。
```
