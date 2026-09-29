---
id: disclosure_anchor_contract_checklist
project: disclosure_anchor
title: API / public view / source_ref 契约检查清单
status: final-for-implementation
created_at: 2026-06-26
---

# API / public view / source_ref 契约检查清单

## Private resident lifecycle backends

- Explicit receipt/seal v3 separates actual API profile/epoch from Mac observer process/clock; original
  v1/v2 schema bytes and frame-v2 remain unchanged. No old-evidence upgrade or phase-clock comparability.
  Canonical private replay binds native source/QPC/UTC and actual GPU/API/parent-cgroup projections;
  source-less hashes and self-reported READY alone never qualify activation or combined CPU.
- Append-only 0062 permits exact receipt v2/v3 supplement versions; projection reconciliation includes
  version in conflict identity. Existing rows and 0054 are unchanged; v3 rows block a lossy downgrade.
  Validation includes unchanged exports, separate-epoch spawn/replay, backend identity negative cases,
  and managed scratch v3 round-trip/unknown-version rejection/mixed-version conflict.

- Default-off Linux supervisor v1 owns one fixed-source sampler child; normal close/EOF/exact wait4
  exit0 precede exit CPU. Failure emits no successful closed receipt. A separate +2s native-hang fuse
  does not extend sampling or the KPI denominator; external owner still proves exact container absence.
- Default-off Windows Job accounting v1 uses creation-time JOB_LIST membership and actual active0
  totals including dead descendants. Forced termination/nonzero exit disqualify normal closure.
  Supervisor CPU is pre-attestation, not full-run; repeated process cumulative CPU is never double-counted.
- Prepared DLL/source/recipe bindings are verified before measured runtime byte-array loading;
  runtime cannot compile. Backend smoke and these receipts do not alone qualify combined CPU or host-hour.
- Prepared v2 pins three C# sources and System.Net.Http; strict recursive JSON/raw slices and bounded LF
  coalescing/flush retain actual wire bytes. A poisoned stdio/HTTP instance cannot resume or replace pending I/O.
  HTTP uses one frozen loopback port, bounded headers/body/deadline and verified cancellation quiescence.
  Queue health/PID and exact engine/model metric identities fail closed; no summed aliases or rounded counts.
  Explicit PS5.1 wire/loopback tests remain mechanism gates, not deployment or combined CPU qualification.
- Private session v1 pins config/bootstrap/executable/prepared bytes; READY binds actual process/clock/
  backend observations, while owner-supplied host/boot/runtime/profile claims still require independent checks.
  Final artifact names appear only after flushed pending bytes and a same-directory no-replace move.
  Session-qualified after/close, first-request start, absolute slots, latest-only exact retry and finite
  lease/hard lifetime prevent old-session revival, silent sequence rebasing, and catch-up bursts.
  Closed-v2 carries source first/last/closing QPC+UTC/count/sequence/skips for independent mapping.
  Close artifacts need normal Job exit and exact Docker-ID absence; response loss alone is not failure
  of already completed cleanup, and file presence alone is not proof of exit or combined CPU safety.
  Detailed boundaries and official mechanism references: `../design/synchronized-capacity-telemetry.md`.

## Private MinerU outgoing HTTP telemetry

- Patched serving process exposes read-only `GET /agent/telemetry/http-requests/v1` with
  `contract_version=mineru.api-http-request-snapshot.v1`, nonnegative `active_requests` /
  `pending_requests`, and its real namespace `process_id`. Original closed `/health` is unchanged.
- Patched serving process computes `GET /health` from the registry durable view: the closed key set and
  values equal the locked projection of the last durable state; no registry lock, no executor lane, no
  `observe()` busy 503 on this route; 503 shapes unchanged (unhealthy manager, `registry_persistence_unavailable`
  for degraded/uncertain persistence, capacity-observation `RuntimeError` text) except the mid-observation
  "task manager became unhealthy during health observation" body, unreachable now that the route no longer
  awaits between the health gate and the projection. Task routes keep `observe()` busy semantics.
  Executor stage entry follows its durable commit; stage exit precedes the next durable transition.
  A parse permit awaiting the processing commit is still reserved, but not counted as executing.
  Capacity inequalities and actual semaphore limits are unchanged. Pressure identity uses canonical
  mount-instance fields and the qualified local mount flags, excluding host-global super-options and
  propagation metadata; actual source replacement remains fatal and the new release rebinds the digest.
  Pressure kernel reads use the `observe` IO lane. `MINERU_LOOP_TRACE` JSON lines
  (lag/gc/summary/probe_failed) are diagnostics only, gated by `MINERU_PHASE_TRACE`.
- Counters surround only the existing final async POST semaphore; pending/acquire/exception/cancel
  conserve across loop threads. Transport retries remain one logical active call; no inbound-task or
  socket-count substitution. No task-manager initialization or side effect; `Cache-Control: no-store`.
- Exact upstream preimages, repeated-patch rejection, actual patched POST error/waiter-cancel/retry,
  multi-loop snapshots and real FastAPI read-only route are deterministic gates. Deployment still needs
  current-source image/epoch attestation; source tests alone do not qualify resident telemetry.

## 私有部署证明：输出静止性（2026-09-06）

- `mineru-windows-runtime-observation.v5` 的 output_root 保留物理文件数/字节数，并要求
  `mineru-output-quiescence.v1`；不改公共 Unit/Filing/API 或 registry-v2 持久化语义。
- 固定 bind-mount 根身份与稳定原始登记 hash；只允许唯一登记控制树及零资源 consumed 历史，
  保留 tombstone/提交水位，不删除状态或把文件数伪造为零。
- 安装前旧 runtime 可为空根；新 runtime 必须有登记。拒绝未知目录/文件、链接、并发替换、
  非 canonical/重复字段/非有限数值、根身份漂移、未清理记录；API 前后 idle 加实际 writer 排空。
- 检查器复用协议的只读 decoder，不创建 registry、不执行启动修复；源码 hash、collector 版本、
  installer、attester 与正负例同步。旧 observation v4 拒绝；新 Windows/held-out gate 仍需现场证明。
- serving `/health` 新增闭合 `task_protocol_runtime` v1，以已初始化的真实 registry/executor
  对象报告容量；collector 不在新进程构造或读取服务 globals。wire 的 v2 标记及容量子证明完整
  验证后才投影原有 13 字段健康 receipt；拒绝缺失/未知字段/错误类型/漂移，不改变旧 receipt 形状。

## 1. 对外契约对象

已发布 V4 bundle 的通用 source admission 必须在同一个完整 pinned tree 中验证闭合 canonical
materialization manifest、provider envelope 及全部 payload hash/bytes；只对经过验证的 parser
payload 重建语义，并要求完整 `ProviderDocument` 相等。请求路径绑定 envelope 的 canonical
published 路径；manifest 的历史 scratch output/spool 不要求仍存在，也不冒充 promotion/readiness
证明。document/run/source/pages/parser target 必须交叉匹配；外层仍核对 processing run 登记的
权威 envelope hash/身份。控制文件缺失、伪装 parser payload、额外文件、链接、篡改或读取途中
换根均失败关闭，不降级 legacy。无管理文件的 legacy bundle 仍保留所有普通 sidecar。
不删除/重写已发布控制文件、不改公共 Unit/Filing/API。回归包含发布/ACK 后真实 PG 通用 admission、
完整重建 equality，以及控制文件/identity/projection/race 正负例；真实来源只读重放另行记录。

私有 `staged-v4-commissioning.v1` CLI 只允许显式 1..8 唯一 document IDs 和有限截止时间，
一轮真实 coordinator；默认 resident 组合仍使用 None/全普通队列，worker once 仍拒绝 staged。
allowlist 在 SQL keyset/LIMIT 前筛选并在 IO/H0/源拒绝前复核；prepared/recovery 不筛选，
范围外未闭合 owner 阻止启动/继续。新 active+succeeded run 和同 run acked 才是新发布证明，
QUIESCENT/零 credit 单独不等于 PASS。scope/组合/CLI/V4 wire source 纳入 writer identity。
验证包含 SQL 合并 cursor/allowlist、注入范围外 candidate、恢复 owner 拒绝，以及 managed scratch
真实 runtime builder→PG→全七阶段→发布→清理/ACK；HTTP/语义 fake 不能冒充真实 GPU/PDF 质量。

私有部署 smoke v6 / heldout validation v2 以闭合 `mineru-diagnostic-disposal.v1`
证明独立诊断 task 的 source/runtime/attempt/fence/key、terminal ZIP hash/bytes/owner、
完整 source/provider 页数、bundle、local resource absence、consumed ACK 和同 key 404。
bootstrap 保持 DB/queue-free；这不是生产 `finish_committed`，不得构造或替代其 durable witness。
意图在提交前持久化；validated evidence 与 local cleanup/处置意图先于 ACK。响应不确定时
保留原 key/episode 并停止，不新 key 重提或清理未知资源。原始输入保留；成功只清理本次诊断副本。
新-only audit records 使用 0700 目录/0600 文件。v5 smoke 不升级冒充 v6，writer hash 覆盖
新 adapter、共享 wire/ZIP/reader、gate/receipt builders 与所用 ports；版本/消费者/负例同时更新。
有界提交响应在解析前留存 exact bytes/hash/status。显式 `reconcile_submitted=True` 仅允许
尚未记录 accepted、且原 intent/完整 snapshot/resource inode/source/runtime 均相等的诊断恢复；
只 GET 原 key，404 或漂移不得重提。后续阶段失败仍保留证据，不由此入口推断处置权限。
MinerU 3.4.4 POST builder 的可选 `message` 只接受 `Task submitted successfully` 精确值；
GET 无此字段同样有效，所有身份字段仍必需，未知字段继续拒绝；V3/V4/诊断同步。

私有 staged 请求摘要 `mineru-staged-request.v2` 固定上传名为
`sha256_<64hex>.pdf`，与源身份 reader/官方 writer 产物 stem 一致。这个版本不是远端
task-protocol.v2 的变更，也不新增公共 API/Unit/Filing 字段。bare-hash v1 请求摘要不能
静默重算或升级；旧 attempt/证据保持原字节并 fail closed，只有全新 attempt 使用新摘要。
V3 prepared port、V4 ingress/resolver/command 与纯 wire codec 必须同时验证精确命名；
禁止下载后重命名官方产物或放宽 reader 到任意 stem。已有数据库若存在历史请求，先按
原证据排空/关闭后再切换；不改写已应用迁移。生产部署仍需 scratch/完整/独立及真实门。

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
  page_no：Unit 本身首个 source block 的页码，非完整 locator 祖先证据的最小页）。
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

私有 accepted-result recovery 不改变 source_ref/public view：历史 H0/spec/runtime/profile 不重写，
当前执行代码另记在 `staged-v4-accepted-result-recovery.v1` receipt。验证 versioned private grant +
独立 GO review + 新旧 exact writer/runtime + 相同远端 epoch；拒绝普通/prepared 新准入和 task POST。
派生恢复 wrapper 必须绑定历史 runtime/writer 与原 heldout epoch；普通部署 wrapper、来源/epoch
不匹配和多余字段均拒绝。先核验远端实际代码无漂移；该 wrapper 不构成新部署资格。
原 attempt/run 发布、cleanup/ACK、零信用均闭合才能 RECOVERY_PASS，且 deployment_qualification=false。
下载只接受 identity Content-Encoding 与原 ZIP 精确长度/hash；压缩、缺长、错长都在读流前拒绝。
对应 loader/CLI/全量 scope/transport 负例和 scratch 原 H0 重启恢复测试必须随实现同步。
V4 publication 保留完整 sorted/unique `page_numbers`（含祖先标题/continuation/reconciliation）；
`page_no` 保留 Unit 本身首个 source block 的页码，只要求属于完整页集，不要求等于祖先最早页。
正页码、source 页数上界、未知 block 拒绝和 routed-draft hash 闭合不放宽；既有 public 列语义不变。

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

`processing_run_published` 的非幂等提交必须在同一事务携带 content-free source identity、source
page count 与 commit instant；历史缺失只能以 hash-validated supplemental outbox event 补齐，不能
从 Unit 数量、页码覆盖或内容猜值。
两种事件均通过现有开放 `change_event.v1` feed 原样暴露；字段是 additive、content-free，consumer
必须按 `event_kind` 选择自己理解的事件，未知种类不得推断业务变化。0052 的两个 partial index 只
约束 worker progress 的 host-hour base lookup 与 late supplement join，不改变 feed 顺序或内容。
事件携带 change_kind，取值仅 observed / materialized（历史事件默认 materialized）
at-least-once 投递 + 消费端幂等（重复投递不产生重复消费效果；“无重复事件”指 feed 内 seq 不重复）
同一 subject（document / asset）内事件保序
下游失效只由 change_kind=materialized 触发
```

## 6. 契约变更记录（append-only）

2026-09-06（0060/0061 私有 V4 execution spec 生命周期）——public view/API/change feed 不变：

`remote_parse_v4_execution_spec` 保存每个 resourceful H0 至多 512 KiB exact canonical control bytes；
attempt/fence/preparation 的 deferred closure、不可变约束与 H0 UoW 同步。源 PDF 不进入该表。
最终态/非 current 历史均保留；0060 添加历史 NOT VALID FK，显式全历史回填后由 0061 验证。
strict reload 重新核对 hash/length/identity，worker 无文件系统 spec fallback。旧文件退役需实际
writer drain、validated FK 和 exact copied/orphan 证明；不改写已应用迁移或既有 cleanup receipt。
本地 ambiguity 原地保留，所有权异常开启 circuit 并保留信用；历史残留启动门与 ACK 前实际
absence 检查见 [V4 resource lifetime](../design/v4-resource-lifetime.md)。这不是上线或吞吐 GO。

2026-09-01（0057 私有 remote parse v4 authority）——不改变 public view/API/change feed：

```text
disclosure_ops.remote_parse_attempt 以 checkpoint_contract_version=4 复用既有 shared head、document-current
唯一性与 document advisory-lock root；row_version 等于当前 immutable V4 checkpoint lifecycle_version，
claim/reclaim/renew 只改变 claim generation/owner/lease，不产生第二套 head 或 lifecycle token。
remote_parse_v4_evidence/checkpoint/secret 与 atomic_publication_winner_v4 均为私有 append-only authority；
checkpoint 通过命名 state/evidence CHECK 与 deferred predecessor trigger 同时限制每个状态的 evidence frontier、
每条 transition 可新增的 evidence、outcome marker、cleanup/ACK 顺序和 immutable evidence 保留。
checkpoint 私有 source byte/page 投影随 predecessor 冻结；命名 credit-shape CHECK 与 cleanup predecessor
等值 trigger 共同禁止非终态凭空增加、提前释放或伪造 held credit，final state 必须归零。
V4 provider secret 仅保存 AEAD envelope/revision；final secret purge 必须携带 exact attempt/fence/final
checkpoint version+hash/current revision，函数只授予 disclosure_app，PUBLIC/reader/L2 无执行或表权限。
processing_run semantic receipt locator 保留 absent、V1 hash-only，以及带 relpath+contract version+hash 的
V2/V3 三种合法形状；任何 partial locator、非 canonical hash 或 document/Unit 不闭合继续 fail closed。
0057 只建立私有 durable authority；typed evidence bytes 的 strict reload、repository/UOW CAS、transaction-P
whole-PDF publication 和 coordinator activation 属后续默认关闭里程碑，不能从本 migration 的存在推断上线。
```

2026-09-04（M5b 私有 V4 recovery backlog reader）——不改变 migration/public view/API/change feed：

```text
RemoteParseV4Repository.list_recoverable_heads 只读 current contract-v4 head；legacy current head 不属于
本版本作用域，静默忽略、不报错。所有过滤都在 LIMIT 前完成，cursor predicate 与 ORDER BY 共同使用 COLLATE "C"，
返回严格递增且不重复的 attempt identity keyset page；整页租约只使用同一 materialized database clock，
remaining seconds 仅为可过期 hint，claim_recovery 仍须重新读取并以 durable CAS 取得执行权；
扫描不取 row/document lock、不写入、不 commit，也不加载 checkpoint/evidence/secret 完整 aggregate；
generation-0 current prepared head 与 live/expired owned head 均可表达，完整启动扫描依赖 worker singleton；
运行中 supersession 激活的新 generation-0 head 由后续 admit_new 纳入同一 PostgreSQL admission，不增加
第二持久队列或重复 backlog reader。
```

2026-09-04（M5b 私有 V4 coordinator persistence slice）——不改变 migration/public view/API/change feed：

```text
DurableStagedCoordinatorPersistenceV4 只闭合 list/claim/renew/reload/admit 的 PostgreSQL authority，
七个 remote/local/publish/cleanup/ACK stage 尚未实现，因此本切片保持 default-off 且不可接入 worker；
CoordinatorWork 不足以自行构造 fence/checkpoint witness，renew/reload 必须 fresh load 完整 V4 authority，
禁止以内存 cache 代替数据库真相；读取和写 claim 分属短 UoW，避免 share→update lock upgrade。
own lease 用覆盖完整 UoW 的 monotonic bracket 保守投影；foreign live claim 只进入 credit ledger，等待
数据库租约上界后重试，不授予本进程执行权。claim/renew 的 commit 响应丢失只允许 exact reload 闭合
或一次相同幂等写重试，任一 owner/generation/state/version/checkpoint/lease 漂移均 fail closed。
运行时 generation-0 superseder 由同表 list_unclaimed_prepared_heads 读取；current/V4/prepared/version0/
generation0/no-owner/no-lease 全部在 LIMIT 前过滤，hint fresh load 后才 CAS claim。当前放不下的 head
保持 backlog/blocked-dimension 可见，同时允许后续可装入 head 继续准入；recovery page 配置限 1..1000。
批内已有 claim 成功、后续 candidate 再失败时，以 AdmissionInterrupted 携带全部 durable claim；coordinator
先按 recovery 记账再开路，不允许已占有 work 暂时隐身。并发 claimant 可使 generation 从 hint 跨越多个
代际；只要求本次 owner 的 generation 相对 fresh-load hint 严格前进，不固定 generation=1 或恰好 +1。
```

2026-09-02（0058 私有 V4 supersession staging authority）——不改变 public view/API/change feed：

```text
追加 remote_parse_v4_supersession_link 与非 current prepared H0 的严格暂存形状，不改写 0057；
source supersession receipt、source current/final head、target H0 checkpoint 形成一对一不可变闭包；
resourceful supersession 在 source cleanup/ACK 期间只暂存 target，source final superseded 后才原子转移 currentness；
resource-free supersession 在一个事务中直接建立 final source、current target 与同一 link；
0058 仍只建立私有 durable authority，不代表 repository/UOW、coordinator 或 live migration 已启用。
```

2026-09-02（M3 私有 V4 repository/UOW exact-CAS）——不改变 public view/API/change feed：

```text
strict load 必须逐行重建完整 checkpoint/evidence/reservation/winner/secret/link authority；持久化漂移
映射为 typed authority violation，连接、deadlock、serialization 等基础设施 DBAPI 错误不得伪装成数据损坏；
create/replay 在既有 DOC_NS + stable_document_hash(document_id) 事务锁下执行，generation 连续且
同 document 只有一个 current；legacy current 以 typed projection 原样返回并阻止新的 V4 current；
claim/reclaim/renew 在锁行后读取数据库时钟，same-owner response-loss 重试不增 generation，ABA 由
claim_generation + exact head witness 封闭；successor append 只接受 exact CAS，final secret purge、winner、
staged superseder 与 currentness transfer 均和 head 更新同事务，repository 不自行 commit；
同一 outer UOW 若组合 row-locking claim/renew/reload/rewrap 与 successor append，调用方必须先取得独占
DOC_NS 文档事务锁；禁止先持有 head row lock 再等待 DOC_NS，M4 transaction-P 也必须遵循 DOC_NS→head；
UOW 绑定同一 Session，只有显式 commit 才持久化，未 commit、异常和 outer rollback 均保持全有或全无；
V3 create 的共享文档锁/typed cross-version race 闭合是 M4 transaction-P 的显式前置，不能由 M3 推断完成。
```

2026-08-30（0054 私有 publish evidence ledger）——不改变 public view/API/change feed：

```text
disclosure_ops.durable_publish_base 与 PublishRun 在同一事务写入 run/document/source/page 和
precommit 下界；first durable source 只按不可变 ledger_seq 的完整历史推导，不信 caller 标志。
disclosure_ops.durable_publish_supplement append-only 保存 verified observer receipt/seal 锚点和
postcommit durable-observed 上界；冲突永久保留并令 replay incomplete。上下界跨 UTC 小时或 host/profile
coverage span 时两个受影响小时都 incomplete，绝不猜测 commit instant。0054 前历史 outbox 不自动升级。
disclosure_ops.progress_relay_head 保存 strict canonical resume bytes/hash/length 和 predecessor CAS 链；
relay id 绑定 run UUID + process epoch，任意回滚、重复 source、非排序或累计页数不闭合都 fail closed。
```

2026-08-30（0053 私有 staged parse checkpoint）——不改变 public view/API/change feed：

```text
disclosure_ops.remote_parse_attempt 保存 processing-run 外部 attempt、fence/version CAS、canonical terminal
receipt bytes/hash/length 与 result owner；每 document 最多一个 current non-final attempt，且 generation 不复用。
disclosure_ops.remote_parse_resume_secret 单独保存 opaque submission/terminal/ACK token exact bytes identity；
reader/L2 无 schema/table 权限，token 不进公开契约、outbox、日志或 repr。
submit identity 只由专用 CAS 设置；receipt-bearing local failure/supersede 保留不可变 remote terminal evidence。
同一 terminal receipt 重放幂等；不同 receipt/投影、旧 fence/version 或非法 state transition fail closed。
```

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
也不得声明 v4 才引入的 `unit_title_fragment` search destination。locator v8 保留 v6 的
source-bound vocabulary，但不凭普通 paragraph 整句词面创造 heading placement；历史 v7 只读，仍可
解码其 `statutory_template` placement。当前 writer `provider_unit_locator.v9` 只新增 finding-only 的
完整 token omission、截断后仍至少两位的单数字末位截断与 cell-scoped 畸形数字分组证据，不改 payload。v1-v6 不得声明 v7
vocabulary，v8/v9 writer 不得重新发出，v8 也不得声明 v9 quality kind。若 ProviderDocument 没有明确标题 occurrence，
prompt 与 selector 留在既有 Unit；只有完整 headed Unit 在同页按 source 顺序恰含一个 prompt part 和
一个 closed selector part也不足以证明 Unit-level ownership，仍保持 NULL，直到 Provider 提供明确
prompt role。
```

2026-09-22（router v102 / prompt v33 锁定溢出降级 + 日期不作数值）:

```text
SemanticRouteEvidenceKind 新增 source_locked_overflow_demoted；MAX_DEMOTED_SEMANTIC_CANDIDATES = 32
MAX_SEMANTIC_DECISION_ROUTES_PER_UNIT（模型决策成员上限，非路由上限）= 32；MAX_SEMANTIC_ROUTES / MAX_SEMANTIC_CANDIDATES 仍为 8
（公共 semantic_keys 上限、DB CHECK、domain VO 未变）
SemanticRouteUnitInput 只在全部候选带降级标记且无 locked 时允许 9..32 个候选；降级 Unit 单独成组裁决
路由器：locked 9..32 个 → 降级为软候选交模型，规范排序后保留前 8 个 direct route；>32 仍 SemanticRouteLockedCandidateOverflowError
定期报告量化主题锁定：标签后的日历期间（年/年度/月/月份/1-3月/1至3月）跳过且不作数值；期间后仍须真实数值或方向结果
prompt v33 增加降级候选裁决说明；cache/receipt identity 随版本变化，已发布 receipt 只读不变
跟进：semantic-retrieval-query-gold.v4.json 仍 pin router v101，下次检索质量评审前须在 v102 上重新评定
设计 docs/implementation/design/retrieval-and-semantic-keys.md；测试 tests/unit/test_semantic_router.py
```

2026-09-22（0063 契约类 parse 失败的显式放行决定）——public view/API/change feed 不变:

```text
disclosure_ops.parse_requeue_decision 是私有 append-only ops 表：decision_id 为 prq_ ULID，
document_id/processing_run_id 均为 RESTRICT FK，processing_run_id UNIQUE（一次失败至多一条决定），
failure_retry_budget_class CHECK 为闭集合 IN (provider_artifact_contract, provider_protocol,
provider_runaway, provider_terminal, semantic_route_contract)——与 coordinator 实际写入的契约类一致，
扩集合需要新的 revision；failure_error_code/fixed_by/reason/decided_by 非空 CHECK，decided_at 由
数据库 now() 赋值。只授予 disclosure_app SELECT/INSERT（无 UPDATE/DELETE/TRUNCATE），
PUBLIC/reader/future_l2_reader 全部 REVOKE：append-only 由权限保证，不依赖 CLI 自觉。
pending_parse（及共享该 helper 的 V4 ordinary candidate source）的两道准入门同时读这张表：契约类
排除按 failed_run.processing_run_id 逐 run 例外；last_failed_retryable 闩锁按 0032 视图口径的
「最新失败 provider-only parse run」例外。决定只放行它指名的那一次失败；出现新的无决定失败时两道门
重新关闭。失败 run 永不改写（逐字节不变），item/charged 重试预算计数不变。
CLI `python -m disclosure_anchor.cli.parse_requeue`（make parse-requeue）写入并打印 JSON receipt，
--dry-run 跑完 guardrail 不写、不分配 decision_id。receipt 分列三件事：decision_recorded、
currently_eligible（写入后经 pending_parse 本身判定，不另写谓词）、remaining_blockers（最新失败 run 与
其 retryable/released、未放行契约类失败数、item/charged 计数与上限、document 状态、是否有 running run），
并显式声明登记决定本身不带来准入：只有正常 worker 扫描或已授权 campaign 才会真正重排。
guardrail 全部 fail closed：run 属于该 document、provider-only parse run、status=failed、
error.retryable 为 boolean、retry_budget_class 在上述闭集合内（自动类与未知类各有专门 error_code）、
不存在不早于该失败的成功 provider run（parse 与 rebuild_units 同算，按 (started_at, processing_run_id)
排序，未知 started_at 不算更早）、该 run 尚无决定、fixed_by/reason/decided_by 非空。
doctor 两行均为 WARN 级（与既有 parse dead letters 一致）：无决定的契约类失败只看队列会考虑的文档
（document.status ∈ registered/parse_failed；之后的成功 parse 不算隐式放行；最多列 20 个 document id + 总数）；
决定态按「决定之后最新一次 provider parse run 的结果」判：succeeded→resolved、failed→released_refailed、
无→released_pending（无 started_at 的 run 不计），pending 是否真的重新排队直接问 pending_parse；未 resolved
（pending 或 refailed）且决定超过 24h 的 WARN；诊断 SQL 失败报 FAIL，不静默为 0。
guardrail 里「不存在不早于该失败的成功 provider run」对每一条成功 provider run 逐条判定，started_at 未知的
成功 run 一律视为不可证明更早 → 拒绝。
```

2026-09-22（router v103：降级组按 Unit 顺序连续；日历 token 封闭）——public view/API/change feed 不变:

```text
semantic_router.v102 → v103（prompt 仍为 v33，taxonomy 仍为 r64）。
1. `_semantic_adjudication_groups` 按 Unit 顺序分组，降级溢出 Unit 是单例边界：v102 把普通 Unit 先全部分批、降级 Unit 排最后，
   当降级 Unit 夹在同一批次的两个普通 Unit 之间时，receipt 按 Unit 顺序存储会让 `_derive_v2_receipt_group_hashes` 判
   "group membership is not contiguous"、Publish 回放拒绝；现在不会。无降级 Unit 的文档分组与 v102 相同。
2. `_is_standardized_quantitative_topic` 的期间语法改为封闭 token：年(度)/月(份)/日/季度/纯数字日期与区间（分隔符含 −、到），
   整体可能所有式跳过，数值排除复用同一 token；标签后允许冒号；方向分支复用主语语法（总额、括号缩写）。修复：2024-03-31、
   2024/03/31、2024-03、2024年1季度、2023-2024年度、1−3月、1到3月 曾被当作数值锁定；2024年3月31日为…、：2024年度为…、
   总额/括号缩写+期间+方向 曾漏锁；2024.3 / 1,234.56 仍是金额。
验证：tests/unit/test_semantic_router.py（降级 Unit 夹在普通 Unit 之间的 v2 回放用例、期间用例表）；36 篇已发布文档 / 10,747 Unit
回放（a-locked-overflow-replay/v103-*）0 变化；外部复审 ChatGPT Pro 对 4548ecaa 的 P1/P2 由本次修复。
```

2026-09-22（router v104 因果片段不锁定 + taxonomy r65 变动情况及原因章节容器）——public view/API/change feed 不变:

```text
semantic_router.v103 → v104；semantic-taxonomy-2026-08-r64 → r65（semantic-financial r34 → r35，199 条 financial 路由；prompt 仍 v33）。
1. `_is_standardized_quantitative_topic`：命中落在尾随原因片段（引导词 主要系/主要是(由于|因为)?/主要由于/主要因(为)?/主要原因(是|为|系)/
   系由于/系因/原因(是|为|系) 在任意子句边界后，是由于/是因为/系(?!统|列|数|指) 仅在 ，,:;； 之后；片段到 。；;！？ 或下一条 （n） 为止）
   时不算锁定见证；裸 由于/因为 只覆盖到第一个逗号。方向词表补 同比上升/同比增加/同比减少/同比降低；标签后允许 科目 填充词。
   动机：002997 unit 9 修完日期后剩下的 6 个锁全来自 "主要系利息收入增加" 类从句，与提示词第 6 条相反；回放 36 篇 / 10,747 Unit：
   12 个 Unit / 7 篇变化（去掉原因从句锁、补回主句锁），其中 5 个 Unit 因主句全部锁住而进入 9–20 锁定的降级路径（首次由真实语料触发），
   0 个 >32 溢出。已发布 Unit 的键需 rebuild 才变。
2. 新增 financial context container `financial_data_change_explanation`（names/aliases 为季报/半年报该章节的标题写法）：标题下的
   Unit 进 section_keys；不参与正文/相似度候选；与其他 context container 一致，自身标题正好是该标签的有正文 Unit 把它作为直接 route；语料现状：季报该标题下 55 个 Unit 中 44 个无直接键，此前 section_keys 只有上级
   company_profile_metrics。
验证：tests/unit/test_semantic_router.py（因果片段用例表、章节键投影用例、taxonomy 计数 345）；回放存档 a-locked-overflow-replay/；
Codex 只读复审。跟进：semantic-retrieval-query-gold.v4.json 仍 pin r64 / v101，下次检索质量评审前须重新评定。
```

2026-09-23（R28 已接受 PDF 的 typed 暂时终态失败 → 既有 infrastructure 预算自动有限重排）——migration/public view/API 不变:

```text
Windows（新 API 镜像）：生成的 mineru_vl_utils http_client 由最终请求所有者 _ProcessAsyncRequestLimiter 在异常实例上标记
最终 VLM chat 请求的结果（get_response_data 非 200 → 原类 ServerError、原 message 加状态码标记；最终 POST 的 httpx
TransportError → 精确类名标记），类型、message、传播不变。agent_task_protocol_v2.task_failure_cause 只按该标记给出闭合
mineru-task-failure-cause.v1 {schema, task_id, retry_class, code, http_status, transport_error}；registry.fail 在同一次
_persist 写入 state=failed、原 error（mineru-task-failure.v1 不变）与 failure_cause；ACK 消费时清除，quiescent 根不得含有；
v3 记录仅在存在时编码该键（其余记录字节不变，registry schema 仍 v3，旧 reader 对未 ACK 的 typed failed 记录 fail closed）。
build_status_payload 在 protocol_state 旁投影 failure_cause（仅 status=failed）。
Mac：protocol_v2_wire 在 TASK_PAYLOAD_FIELDS_V2 增加可选闭合字段 failure_cause，仅允许在 failed 上出现，校验 schema、task_id、
code 形状，并用 remote_provider_v4.provider_failure_retry_class_v4 复算 retry_class，任何漂移/未知版本或 code 为协议违约（不进
可重试分支）；TaskProtocolV2Observation 不变。RemoteProviderFailedV4 新增可选 failure_cause（RemoteProviderFailureCauseV4，
在 wire 解析处与 remote_task_identity 及同一状态响应字节的 sha256/字节数绑定，wire JSON 仍为 6 个字段）。只按最终 POST 的最终
观察结果分类，不宣称内层 httpx-retries 已耗尽。后端：outcome 任务身份须等于 accepted；缺失 → 与原来逐字节相同的 provider_terminal_failure/
provider_terminal/retryable=false；transient（429/502/503/504、ConnectError/ConnectTimeout/ReadTimeout/WriteTimeout/PoolTimeout）
→ provider_terminal_transient_failure/infrastructure/retryable=true；其余 typed → provider_terminal_failure/provider_terminal，
message = "<descriptor>; response <sha256>; provider error: <原文>"（≤4096）。FailureReceiptV4、TerminalReceiptV4、
processing_run.error 键集合、outbox 事件形状、change_event 契约不变，只是 V4 首次写入既有值 retry_budget_class=infrastructure
与新 error_code 值。失败仍经 receipt → cleanup → ACK → 终态 failed run；之后才由普通 pending_parse 以新 attempt/fence/key
重排，上限为既有 item<max_retries、item+infrastructure<5×max_retries（默认 15），跨重启计数，失败 run 不改写。
部署：先 Mac worker 再 Windows API 镜像（patcher 与 task protocol 源码哈希、镜像身份变化，需要走原发布/资格链）。
```

2026-09-24（F5 worker 公共故障持久停止与显式放行）——public view/change feed/migration/公共数据契约不变：

```text
新增私有 ops 合同（运行根 control 文件，不入 DB、不入 Git）：worker-circuit-stop.v1（scope worker-operational-control，
record_origin automatic_fault|operator_reconstructed；闭合 cause = kind/reason_code/origin/exception_class/
exception_fingerprint + 派发时 attempt/lane/state/lifecycle_version/claim_generation/claim_owner + 至多 16 个 provider
attempt 的身份/结果/原因/cache key；worker pid/started_at；profile/runtime 指纹；native_disable 状态/细节/目标），
worker-circuit-release.v1（released_stop_sha256/decided_by/reason/fixed_by/decided_at），归档按原字节
worker-circuit-stop.<sha256hex>.json。目录 0700、文件 0600；记录身份是原始字节 SHA-256，不自哈希；不含异常原文、
提示词、模型输出、Unit 文本、环境或凭据。
CLI：worker status 的 stdout 形状不变，存在公共停止/无效/不可信 control 时先在 stderr 报 STOPPED 并退出 3；新增
worker status --control-only [--format json]（worker-operational-control.v1，不连 DB/MinerU/模型，RUNNABLE→0 否则 3），
worker release-circuit --expect-sha256 --decided-by --reason --fixed-by [--dry-run]（0/3 拒绝/75 忙），
worker record-circuit-stop --from-disabled --evidence --evidence-sha256 --decided-by --reason [--dry-run]。
worker loop|once 在任何 DB/MinerU 之前检查停止，拒绝时 78；loop 拿不到单例锁由 0 改为 75（once 仍 0）；公共故障 78；
纯操作员 TERM/INT 无故障 0；watchdog 70 不变。pipeline build-units|publish|process|rebuild-units 与
scripts/generate_current_source_replay.py 在停止时以 78 拒绝；admin API build/publish 返回既有 503 SERVICE_UNAVAILABLE
（消息只含状态与记录 SHA，不含路径），其余 admin 行为不变。
Coordinator：CoordinatorResult 追加可选 stop_cause/termination_kind（位置构造兼容）；StageLeaseLost 追加 provenance
（默认 unspecified，位置消息不变）；StageLeaseGuard.revoke 可带 provenance。执行器先按闭合 reason 分类：failed_closed
在任何 guard checkpoint 前以原类型抛出；cancelled/availability 仍先检查 guard。
launchd：worker plist RunAtLoad=true，KeepAlive={SuccessfulExit=true}，ThrottleInterval=30，ExitTimeOut=90，
EnvironmentVariables.DISCLOSURE_WORKER_LAUNCHD_LABEL；无 PathState/WatchPaths/QueueDirectories/StartInterval。
install_launchd.sh 在停止/无效/不可信时 78 拒绝，disabled label 需 --confirm-operator-disabled 才 enable（预检只把
OPERATOR_DISABLED 交给该确认）；make worker-restart 需 control RUNNABLE。doctor 新增 worker operational control 行。
启动门（所有上述入口共用 require_worker_start_permitted）：latch → 活动记录 → 仅当无记录且 macOS 上
DISCLOSURE_RUNTIME_ROOT == 新设置 DISCLOSURE_WORKER_SUPERVISED_RUNTIME_ROOT（默认生产 runtime 根）时读回
label（DISCLOSURE_WORKER_LAUNCHD_LABEL 或生产 label）三态：已知 disabled→OPERATOR_DISABLED、未知→CONTROL_UNAVAILABLE，
均拒绝；已知 enabled 且非“loaded/未运行/上次 78”才 RUNNABLE（否则 SUPERVISOR_ONLY_STOP 拒绝）。未 loaded 只认 print
退出 113 + stderr `Could not find service "<label>"`；last exit 按前导整数解析（`78: EX_CONFIG`）。其它根与非 macOS
不调用 launchctl。runtime 根须 lstat 非 symlink、属 worker uid、组/他人不可写；受监督根另须与挂载 sentinel 同设备。
worker-operational-control.v1 JSON 追加 supervision（supervised|not_macos|unsupervised_runtime_root）；native_supervisor
字段为 service_target/available/disabled/disabled_detail/loaded/running/pid/last_exit_code/print_detail/closure
（closed|running|unknown，null=未知）。release 要求可证明收尾：launchd 未知/运行中、进程表不可读或已知 owned 进程存活→75，
保持停止；dry-run 追加 supervision/native_closure/process_closure。record-circuit-stop 仅限 macOS 受监督根。
原生 disable 与启动门共用同一绑定 worker_supervision：仅 macOS 受监督根、对其绑定 label，launchd 或操作员启动相同；
不再比较 XPC_SERVICE_NAME（原生验收 18/18 个 launchd 子进程为 "0"）。生产 label 只与生产 runtime 根互相绑定，其它配对
为 supervision=label_root_mismatch：不调用 launchctl、原生状态未知（CONTROL_UNAVAILABLE 拒绝，release 收尾未知），停止
记录 native_disable=failed:label_root_mismatch；无监督根记录 failed:unsupervised_runtime_root|failed:not_macos（取代
not_supervised_launchd_job/supervisor_target_unconfigured）。RuntimeWorkerStopControl.for_settings 的 environ= 改为
supervision=，删除 supervised_launchd_label。
Coordinator 重试 episode：新增 StageWaiting 子类 StageProviderWaiting（仅 V4 backend 在权威 status pending/processing
且 runaway 检查之后抛出），清零本 attempt 的 retry 次数与首失败时间；常量、退避、runaway 与全局软熔断不变。retry 耗尽
错误行追加 (attempts=n/max, elapsed=s/window s, last=<已识别固定字面量|unrecognized sha256:…>, causes=<类型名链>
[, http_status=nnn])，不含异常原文。worker loop 公共停止时 stderr 输出一行 `[staged-v4] <协调器错误>`（恢复 F5 前的
可见性）；terminal 进度行 `[staged-v4]` 追加 UTC ISO 秒级时间戳（jsonl 形状不变）。
语义子进程边界（codex_cli._run_process，Codex/Claude 共用）：清理失败不再以 PermissionError 覆盖原取消/故障（原先被
适配器当作 executable_unavailable 而降级发布）；EPERM 只在本调用已回收 leader 且 signal-0 探测为 ESRCH 时视为进程组
已结束。无法证明时仍抛原异常并附 note、stderr 一行 `[semantic-process] …`、stage note process_ended 追加
closure=unproven，仍存活的 leader 保持登记；关停 sweep 不再抛出，只报告未能发信号且仍在运行的子进程。
设计 docs/implementation/design/worker-operational-stop.md、worker-dynamic-scheduling.md；运维 runbook §1.1f。独立回归
测试与 scratch/原生 launchd 验收由独立作者与 root 另行执行，本条不宣称其已通过。
```

2026-09-25（本地执行升级 U01 与只读部署预检）——public view/change feed/migration/公共数据契约与 API 不变：

```text
新增私有 ops 合同（运行者私有文件，不入 DB、不入 Git；全部 closed JSON，未知/缺失字段拒绝，SHA-256 为文件原字节）：
worker-execution-release.v1（E1：source_revision、writer_code_sha256=W1〔沿用 42 成员算法〕、worker_python_version、
worker_package_set_sha256、files[path,sha256,bytes] 排序唯一；范围固定为 src/disclosure_anchor/**、scripts/*、scripts/launchd/**，
排除 __pycache__/*.pyc/.DS_Store，symlink/非普通文件拒绝）；worker-legacy-scope-inventory.v1（全部 current V4 head 的
attempt/document/run/generation/fence/H0/spec/source/target/request/runtime/key/submission_epoch/P/WP/观测状态·版本·checkpoint/
accepted sha，无能力明文；存在 non-current prepared V4 行时拒绝捕获）；worker-local-execution-upgrade.v1
（transition_kind=local_operational_compatible；parent_qualification R0/W0/Q0 三文件 SHA/P0 文件+SHA/WP0/A0 文件+SHA/
qualified_at_utc=canary 原 passed_at_utc/service_epoch；current_execution E1 文件+SHA/revision/W1/M1 文件+SHA/R1/P1/WP1/
容量/A1；compatibility_basis 三个审计引用；legacy_scope 文件+SHA+成员数）；worker-local-execution-upgrade-review.v1
（verdict=GO、proposal_sha256=提案文件 SHA、reviewer/decision 引用）；worker-deployment-preflight.v1（只读报告）；
worker-execution-boot-receipt.v1（每次 resident 启动一份，$DISCLOSURE_RUNTIME_ROOT/reports/execution-boot/<owner>.json，
create-only 0600，绑定 owner/pid/启动时间/U01/审阅/E1/W0·W1/R0·R1/P0·P1/WP0·WP1/容量/A1/父资格日期/清单/观测范围，只作证据）。
E1 范围以已加载包的位置为根：__pycache__ 之外的字节码（SourcelessFileLoader 可无源导入）、scripts/ 下未知子目录、
symlink/非普通文件一律拒绝而非跳过；第三方依赖以解释器分发包名+版本集合绑定，不按字节。
Settings：DISCLOSURE_WORKER_EXECUTION_UPGRADE_FILE/_SHA256、DISCLOSURE_WORKER_EXECUTION_UPGRADE_REVIEW_FILE/_SHA256，
四值同设或同空；缺省时 exact 路径（profile 精确相等、writer drift 拒绝、stream guard runtime）逐字节不变。
Gate：verify_mineru_deployment_gate/MinerUDeploymentChecker 追加 accept_execution_upgrade=False；配置 U01 时只有
resident worker loop、worker deployment-preflight、doctor 接受，其余入口（once/pipeline/admin/commission/campaign/recover）拒绝；
VerifiedMinerUDeployment 追加 qualification_origin（exact|compatible_parent）与 execution；compatible_parent 时
runtime_identity=R1、canary_passed_at_utc=Q0 原值，年龄/缓存/held-out/容量/策略按真实时钟重验。read_owner_only_evidence
与 ParentQualificationExpectation 导出；_load_evidence 行为与错误文案不变。
边界：ProductionV4StageInputResolver(legacy_execution=)、inspect_frozen_identity（无 claim 的同一 H0 闭包，只读）；
RemoteSubmissionCommandV4.legacy_authorization（默认 None，compare=False，不持久化）；MinerUHttpRemoteV4(legacy_execution=)
在 before_send 核对授权并以 R1 调用 stream guard，原请求字节/key 不改写；StagedV4NewWorkAdmitter(legacy_obligations=)
在普通扫描前抛 LegacyObligationsOpen（NewWorkAdmissionUnavailable）直到清单成员全部终结；build_staged_worker_v4_runtime
(verified_execution=) 仅 unscoped resident 且须等于组合出的 P1/WP1/R1/容量/A1；RemoteParseV4Repository 追加只读
observe() 与 count_staged_prepared_heads()。POST 授权在边界处与清单成员重新比对 fence/H0/spec/source/request/key/P0；
成员进度要求捕获前缀 history[0..v] 连续且 previous_checkpoint_sha256 逐级相连；清单外的未决 head：任一清单成员
仍未终结时一律拒绝（过早的新工作，即使绑定 E1）；全部终结后只有精确绑定 E1 的 WP1/P1/R1 才允许，重启无需移除 U01。
CLI：worker deployment-preflight [--format json|terminal] [--prepared-key-ttl-seconds N]（0 就绪/78 未就绪/64 参数；
control 停止/无效/不可读时在 checker、DB 与原生探测之前即拒绝；legacy-sync 模式只跑 resident checker）；
python -m disclosure_anchor.cli.execution_upgrade release-manifest|derive|legacy-scope|propose（0/64/65/70，stdout 一个
JSON，只新建 0600 文件）。install_launchd.sh 接受 --prepared-key-ttl-seconds N，在 F5 停止预检之后、任何 plist/enable/
bootstrap 之前运行预检，未就绪 78 且零 launchd 变更（沙箱中无 MinerU 证据时安装器因此在 bootstrap 前拒绝）。
F5：新增 reason_code execution_upgrade_scope_failed（kind startup_fatal、origin startup_recovery），在单例下、依赖/
报告/恢复/维护之前的 E1 与清单复核失败时记录；DB 传输层错误非零退出不记停止。doctor 仅在配置 U01 时追加
worker execution qualification 与 worker legacy obligations 两行。未实现：过期未投递 prepared 的重投分支（待 root 的
实际 TTL 证据）。设计 docs/implementation/design/local-execution-upgrade.md；运维 runbook §1.1g。独立回归测试与
scratch/原生验收由独立作者与 root 另行执行，本条不宣称其已通过。
```

2026-09-25（provider_unit.v24 / locator v10 U+0000 标记、发布文本可表示性兜底、U01 v2 三角色升级）——migration/public view 列/API/change feed 形状不变：

```text
内容策略 provider_text_nul_substitution.v1：PostgreSQL TEXT/JSONB 不能存 U+0000，provider 丢失的原字符不可证明，
因此 admission 有效视图在 native 校正之后、Unit 哈希之前，把每个未被 native 校正替换的 payload 中的每个 U+0000
一对一换成 U+FFFD；其余字符（字面 `\u0000`、TAB/LF/CR/SOH/DEL、U+FFFD/U+FFFE/U+FFFF、扩展汉字、emoji）逐字保留，
ProviderDocument/MinerU artifact/PDF/已封存请求不改写。ProviderTextSubstitution（source_index、payload_ordinal、
raw_block_sha256、provider_text_sha256、substituted_text_sha256、substituted_text、occurrence_count、policy）挂在
ProviderSourceSemantics / AdmittedProviderDocument.text_substitutions；校验要求有序唯一、与 reconciliation 身份不相交、
hash/次数/逐字替换闭合，且覆盖每个未被校正的含 U+0000 payload（缺、多、改一律拒绝）；可与 native finding 同身份并存。
native reconciliation/finding 的推导、作用域与 v1-v9 不变量不变；校正文本仍含 U+0000 时由下述 gate 拒绝。
document_units_v1.artifact_locator：依赖标记的 Unit 为 provider_unit_locator.v10 = v9 全部字段 + 非空 text_substitutions
（每条 7 个字段：source_index、payload_ordinal、raw_block_sha256、provider_text_sha256、substituted_text_sha256、
occurrence_count、policy，不含正文）；其余 Unit 仍为逐字节不变的 v9。v10 必须非空、v1-v9 不得含该键；挂载规则与
reconciliation 相同（本 Unit blocks ∪ heading chain ∪ continuation fragments），builder 验证每条恰被依赖它的 Unit 覆盖。
quality_status：被标记文本出现在 title、heading_path（含祖先标题，其后代同样）或 part owner block payload 时为
needs_review；evidence-only（page frame 等）只留记录不标记；逻辑表 continuation 不能携带非空 payload，因此含 U+0000 的
下一页表块不会被隐藏为 continuation。私有质量 occurrence 新增 kind=text_substitution（reason_id
text_substitution:provider_text_nul_substitution.v1，携带上述 7 字段）。builder_rules_version = provider_unit.v24；
已封存 V4 preparation 只重开不重建，按其请求中的原版本（如 v23）发布。m6.source-semantic-record.v1 字节不变（标记由
记录中的 provider document 纯推导，严格 decoder 重算到 DecodedSourceSemanticRecord.text_substitutions）；
m6.source-semantic-build.v1 接受当前 v9/v10 locator、固定当前 builder 版本（v24 之前的 build 记录需重生）；source
comparison 在 finding_binding 下单列 locator_text_substitutions；M6 verifier 的 source_admission 追加
text_substitution_count。
发布前兜底 publication_text_representability.v1（纯函数，build_or_reopen 之后、readiness/事务 P 之前、前后各一次
stage guard checkpoint）：只读已闭合 V4 请求的 title、heading_path、canonical_payload_json（一次解码，含键）中的实际
U+0000/孤立代理 → PublicationTextUnrepresentableError；control 字段（semantic_keys、section_keys、artifact_locator、
processing-run projection 持久化字段）出现同类字符 → 既有 WholeDocumentPublicationV4Error（worker 停止）。不改写/
规范化/重算任何字节；诊断为有界 ASCII：`publication_text_representability.v1: request=<sha> findings=N occurrences=M
shown=K; unit=U field=title|heading_path|payload [path=/…] [in=key] codepoint=U+XXXX count=C`，路径段数组为十进制下标、
对象键一律为排序序号 `#n`（不含键文本或 hash），最多 12 条、≤4096 字符。COMMIT lane 只把该 typed 错误映射到既有
FailureReceiptV4：error_code publication_text_unrepresentable、error_class PublicationTextUnrepresentableError、
outcome local_failure、error_stage commit、retryable=false、retry_budget_class provider_artifact_contract（0063 的
parse-requeue 闭集合内），message = safe_summary；随后既有 cleanup/ACK → local_failed。未知 DataError/hash/IO/
integrity/fence 仍按原路径停止。v24 构建的 NUL 文档不会触发它；可达角色是 v24 之前已封存的请求与校正文本残留 U+0000。
U01 v2（worker-local-execution-upgrade.v2，v1 编码/解码/校验/文案不变）：qualification_anchor（v1 parent 字段集，
Q0 原 qualified_at/期限按真实时钟复验）、recovery_origin（release_manifest_file/sha256、source_revision、
writer_code_sha256、runtime_bundle_file/sha256、runtime_identity_sha256、process_profile_file/sha256、
worker_profile_sha256、capacity_config_sha256、stream_activation_file/sha256，全部由归档文件按自身 pin 重算、不与当前
树比较）、current_execution（target）、compatibility_basis、legacy_scope。构造要求 target release ≠ origin release、
容量相同。两条直接关系：Q0→target 与 origin→target 均为同一计算（manifest 只允许 writer 不同，writer 也可不变），
P/WP/A 只移动其 runtime/profile 引用；清单成员绑定 origin 的 R/P/WP（v1 仍绑定 parent），anchor 永不进入成员证明。
新 H0 屏障仍按清单成员 attempt_id：全部成员终结（含合法 local_failed）前拒绝，与 target R 是否等于 origin R 无关。
decode_execution_upgrade 按 contract_version 精确分派；VerifiedQualifiedExecution 追加 upgrade_contract_version、
qualification_anchor、recovery_origin、member_* 身份，v2 summary 用 anchor_*/origin_*/无前缀 target 键（v1 summary
仅追加 upgrade_contract_version）。worker-execution-boot-receipt.v2 写入 v2 summary 全部身份与 scope；preflight 终端
v2 分三行列出 qualification anchor/recovery origin/target；LegacyScopeObservation 追加 closed_state_counts（按终态
计数，boot receipt scope、worker 日志 final_states=、doctor 行同步）。CLI：propose --contract-version v1|v2（v2 需
--anchor-process-profile/--anchor-activation/--origin-release-manifest/--origin-runtime-bundle/--origin-process-profile/
--origin-activation，混用 v1/v2 角色参数为用法错误 64）；derive --contract-version v1|v2（v2 用 --anchor-*，并允许
target 保持 Q0 writer；v1 仍要求 writer 变化；输出追加 contract_version，v2 键名为 anchor_*/target_*）。
设计 docs/implementation/design/local-execution-upgrade.md、provider-source-semantics.md，架构 service-purpose §7.2，
运维 runbook。独立测试、scratch PostgreSQL 与生产恢复由独立作者与 root 执行，本条不宣称其已通过。
```

2026-09-25（staged-v4 普通准入有限扫描轮次与容量等待）——migration/public view/API/change feed/公共数据契约不变：

```text
私有端口 V4OrdinaryParseCandidateSourcePort 追加 latest_document_id()（当前合格集最大 document_id，空集为 None）与
list_candidates(through_document_id=None)（闭区间上界）；queries.pending_parse 追加可选 through_document_id（含）与
descending=False，二者与 after_document_id、document_ids、scope/active-company/重试谓词同在 LIMIT 前；其他调用方
（ingress 事务内资格复核、源拒绝、doctor、legacy worker、parse_admission_diagnosis）用默认参数，SQL 不变。
PostgresV4OrdinaryParseCandidateSource.latest_document_id 以同一 max_retries/scope_classes/allowlist 调用
pending_parse(limit=1, descending=True)；campaign 全 carry-in（空 allowlist）不发 SQL。
StagedV4NewWorkAdmitter 以本轮上界作为唯一“进行中”标记：无上界时，在 legacy gate 与 admission guard 之后、首页之前
取 latest_document_id 冻结上界（None 即本轮完成、不发页查询；非空字符串以外 fail closed "pass bound is invalid"）。
每页 fail closed：条数 <= 页大小、全部 > 游标（原文案 "cursor did not advance within its bound"）、全部 <= 上界
（"page exceeded its frozen pass bound"）。只有 _outcome(incomplete=False)（上界内读完、ready 候选为本轮最后一个、
或合格集为空）同时清空游标与上界；容量等待、observation 等待/放弃、NewWorkAdmissionUnavailable（readiness/legacy
obligations）与 prepared claims 未完成均保留本轮上界与游标。
容量等待：页内 observation_request 抛 V4InitialIngressCapacityBlocked、ineligible_dimensions 为空且候选
archived_raw_byte_count 非空时，记录 blocked 维度后返回 scan_incomplete=True，游标停在该候选之前；下一次调用按原
游标与上界重列并重检。ready 观察结果的 build() 抛同类临时阻塞时（不论归档大小原本是否已知）保留 _ready_observation
与游标，返回 scan_incomplete=True，下一次调用只重跑 build()。ineligible_dimensions 非空（含观察后页数派生维度）、
ready 源为 V4SourcePdfOverLimit、或观察前大小未知时仍记录后越过。每轮 blocked/ineligible 汇总改为“无上界”时
重置：容量等待记录阻塞而游标不前进，旧的“游标为空”条件会在首候选等待期间每次调用清空本轮汇总。构造器要求
ordinary_candidates 同时提供 list_candidates 与 latest_document_id。不新增字段、构造参数、时钟、超时、信用缓存、
持久状态、队列或 migration，重启即新一轮；AdmissionOutcome 形状与 coordinator 不变，等待期间每个调度 tick 至多
重列一页。prepared claims 优先（放不下的 prepared head 仍越过）、legacy barrier、admission guard、
commissioning/campaign scope、事务内 H0 资格/身份复核、容量向量、profile、parser 与 NUL 策略不变。
保证：持续到达的更高 ID 不再使一轮没有终点，游标之下变为合格的行最迟下一轮被重新检查；已知大小、profile 装得下的
候选临时放不下时不被越过，其后 ID 不先于它准入。不保证：固定等待时长或持续满载（进展以在途工作继续完成、释放信用
为条件，等待期间利用率可能下降）；观察前大小未知、按整个字节预算计费观察的 PDF 的有限时间准入。
设计 docs/implementation/design/worker-dynamic-scheduling.md §10；里程碑 08 §3。独立测试（含既有 fake 候选源的
端口适配）与 scratch PostgreSQL 由 root 另行编写和执行，本条不宣称其已通过。
```

2026-09-26（0064 历史证券绑定与保留原件登记）——public view/API 列与 change feed 事件词表不变:

```text
disclosure_core.source_access 追加可空 recovery_of_source_access_id（FK→source_access RESTRICT），CHECK 限定非空时
provider_interface IS NOT DISTINCT FROM local:register_retained_pdf.v1（可空接口为 NULL 时 CHECK 失败，不以 UNKNOWN 放行）、
status=ok、result_hash/company/security 非空且不自指，部分 UNIQUE
一条失败至多一个成功回执；绑定行 local:historical_security_binding.v1 以 CHECK 封闭形态、result_hash 部分 UNIQUE。
既有行全部为 NULL，不回填、不改写；0023 原字节不变，downgrade 逐字恢复 0023 pending_download_v1。
新私有 ops 视图 download_failure_resolution_v1（仅 app SELECT）：失败下载的 nonretryable 与 resolved_by（唯一成功回执且
Document provider/pid/raw hash/company/security 一致）。pending_download_v1 以 CREATE OR REPLACE 重建、列不变，终态排除改为
「存在未解决的不可重试失败」；failed_download_count 与 CNINFO_MAX_RETRIES 语义不变。queries.download_dead_letter_count、
legacy SourceAccessRepository._terminal_download_failure 与 doctor `download dead letters` 读同一视图；health
queues.download_dead_letters 字段不变，已被回执解决的失败不再计入。
security.status 新值 historical：只能由具名绑定创建；SubjectResolver 与 RegisterLocalPdf 遇之拒绝
（HistoricalSecurityBindingRequiredError，不改 identifier 状态）；下载经 resolve_acquisition_subject 的证据链使用。
closed contracts（application/contracts/historical_security_registration.py）：historical-security-binding.v1、
historical-security-binding-plan.v1、retained-registration-request.v1、retained-registration-plan.v1；回执快照段
acquisition_provenance（historical-acquisition-provenance.v1）与 retained_registration（retained-archive-registration.v1）。
计划 raw_file_relpath 为闭合六段归档路径（pid 段与归档路径构造器同一规则，拒绝空、./../点开头、/、\、控制/格式/
未分配字符；文件名=raw hash）；execute 另要求其等于由重验候选代码/公告年/pid/hash 推出的本条归档路径，并核对计划全部谱系
字段。回执“同一义务”在 preview/execute/读回/reconcile 用同一判定，与 download_failure_resolution_v1 的解决关系一致
（失败行、recovery_of、同 provider、成功保留原件接口、同 pid、Document 一致）。原件读取打开前拒绝非普通文件，O_NONBLOCK
打开后 fstat 复核类型与身份，FIFO 不阻塞。
可恢复失败闭集 {registration_metadata_error}；扩集合需新契约。
索引 snapshot 追加 identity_context（query_profile_org_id/query_profile_source_access_id，仅有候选时）与候选
candidate_provider_org_id；候选旧 provider_org_id 仍为查询 profile 投影；空 snapshot 形态与 result_hash 规则不变。
下载失败记录追加 error.failure_phase、query_params.index_source_access_id（已知时）、result_snapshot.candidate_sha256 与
archive{archive_completed[, raw_file_relpath, raw_file_hash, byte_count, raw_created]}；result_hash 仍为 null。
DownloadDocumentCommand 追加 index_source_access_id；SourceAccessRepository.list_pending_download_candidates 返回
PendingDownloadCandidate（映射视图 + index_source_access_id）。register_document 的普通调用输出不变。
CLI python -m disclosure_anchor.cli.source_recovery（make source-recovery ARGS=...）：binding-preview/binding-execute/
replay-preview/replay-execute/reconcile；计划为规范 JSON，按 --expect-sha256 与 recovery 代码摘要执行，输出不覆盖，
0=完成、1=拒绝/停止。设计 docs/implementation/design/historical-security-retained-registration.md；运维 runbook §5.5。
application/worker/queries.py 在 MinerU writer 指纹内，发布须走既有兼容/资格预检。独立测试与 scratch PostgreSQL 由
root 另派作者编写执行，本条不宣称其已通过。
```

2026-09-26（router v105 / taxonomy r66 / prompt v34：合并及公司报表两个报表键均为 direct route；路由校验先于缓存写入并在命中时复验）——public view/API/change feed、receipt 版本与 migration 不变:

```text
semantic_router.v104 → v105；semantic-taxonomy-2026-08-r65 → r66（semantic-financial r35 → r36，仍 199 条 financial 路由；event r49 不变）；
semantic_route_adjudication.v33 → v34。
1. taxonomy：10 个“合并和公司/银行{资产负债表,利润表,现金流量表,所有者权益变动表,股东权益变动表}”标签从 composite_context_labels
   原序移到 composite_direct_labels，别名不变；另两个 context composite（公司治理、环境和社会；公司简介和主要财务指标）不动。
   有正文 Unit 的自身标题精确命中（及/和、编号、（续）归一化后）时两个报表键都以 source_heading_exact 锁定，确定性 receipt 为
   (合并, 母公司)，不调模型；heading-only Unit 仍只得 section_keys；_section_keys 对两类 composite 读法相同，所有 Unit 的
   section_keys 不变。静态测试：同一规范化标签对应 ≥2 个 exclusive 键时必须是键集相同的 direct composite（当前恰为这 10 个）。
2. router：_validate_decision 由“多个 exclusive 容器不能并存”改为“exclusive 容器不能与非容器 route 并存”，每个容器仍须有
   source_heading_exact；_canonicalize_decision 选中任一容器时保留全部被选容器（候选顺序）并去掉行项目。Midea 2025Q3 第 10 页
   合并及公司现金流量表（Unit 15）的真实双选答案不再被拒；合并及公司资产负债表 + 正文“资产负债表（续）”不再只落母公司。
3. executor：SemanticAdjudicationExecutorPort.adjudicate 增加可选 validate（SemanticDecisionValidator），router 每次传入组校验
   （Unit 覆盖、规范化、逐 Unit 规则）。新答案在写缓存前校验：失败为该次调用的 failed_closed attempt（provider_call_ended
   记 failed_closed），不写缓存、不切备用；决策未恰好覆盖所请求 Unit（新类型 SemanticDecisionCoverageError）保持原
   invalid_contract 类，其余为 invalid_decision。缓存命中先校验：失败为同形 attempt（该条目 cache_key），无 provider 调用、
   不记 cache_hit、不隔离也不改写条目，stage note 为 cache_hit_invalid。校验器的其他异常原样抛出；不传 validate 的调用方
   行为不变。m6 只读 verifier 的拒绝型 executor 只补签名。
4. prompt：第 5 条删去“exclusive 候选必须是唯一 route”，改为选中容器时全部 route 须为容器，仅当 Unit 标题同时点名多个载体
   时可多选；第 2 条补上组合标题这一锁定来源。prompt_sha256 与缓存身份随之变化。
兼容：r65/v104 receipt 在当前 router 下按既有规则报 router/taxonomy stale（与 r64 → r65 相同）；V4 在 commit 时新路由，已封存
请求按原字节回放；r65 缓存目录（含被拒条目 fb001911…）不再被读取，也不删除。已发布 Unit 原样保留：设计复审 2026-09-26 的只读
公共视图普查为 55 个合并及公司报表 Unit（49 个有正文：46 个 []、3 个 ["balance_sheet_parent"]），重标需逐文档
rebuild-units，不在本次范围。
验证：tests/unit/test_semantic_router.py（composite 列表与静态不变量；10 个标签 × {原样, 编号+（续）} 的确定性双键、
section_keys 与回放；单一报表标题负例；合成 taxonomy 的模型路径多容器规范化与容器+行项目篡改 receipt）、
tests/unit/test_semantic_adjudication.py（切备用后被拒、缓存命中被拒、Unit 覆盖错误、校验器异常）、tests/unit/test_semantic_codex_cli.py
（prompt 第 5 条）；独立测试 test_semantic_enrichment_policy_independent.py 与 test_semantic_adjudication_validation_independent.py。
```

2026-09-27（大文档结果存储 capacity v2 / 阶段 grant / 可续传 spool / 新合格运行时升级；候选，未部署）——public view、Filing API、change feed 与 migration 不变:

```text
新增/升版（旧版本字节、解码与校验不变，不重解释旧 B/credit 记录）：
- mineru.capacity-config.v2（v1 计算字段 + 嵌套 mineru.result-storage-policy.v1，取代 B/L）；mineru.process-profile.v3
  （B/L 为 null，绑定 result_storage_policy_sha256）；原生 mineru-task-runtime.v4 / mineru-task-registry.v4 /
  mineru.capacity-observation.v2（result_storage 账本）/ mineru.task-storage.v1 / mineru.task-storage-status.v1 /
  mineru.result-inventory.v1；Mac staged-resource-credit-policy.v3（v2 字节与 SHA 不变）、remote-terminal-receipt.v5、
  remote-parse-materialization-intent.v5、stage-resource-grant.v1（可选 execution_upgrade_sha256/execution_spec_sha256，
  缺省时不进 canonical 字节）、mineru-v4-spool-owner.v2、mineru.stream-activation.v2 / mineru.stream-policy.v2、
  worker-qualified-runtime-upgrade.v1、worker-legacy-key-lookup.v1、worker-execution-boot-receipt.v3。
原生 API（仅 capacity v2 进程；v1 进程生成代码行为不变）：
- POST /tasks：读体前要求唯一 Content-Length（411 content_length_required），超过 source_pdf_bytes_limit+64 KiB 为
  413 source_upload_too_large，容量不足为闭合 429 {"detail":{"code":"storage_capacity_wait","reason":...}}，均不读体；
  multipart spool 与上传副本一次计费，spool 位于输出卷；只接受单一上传（400 storage_managed_single_upload）。
- GET /tasks/{id}：status 增加闭合 storage 信封；hold 任务保持 processing 且可见，不失败、不 ACK。
- GET /tasks/{id}/result：强 ETag = 结果 SHA-256；Range/If-Range 由 Starlette FileResponse 处理（206/200/416）。
- 未提供 storage-release 路由（放弃不是续作）；超出硬包络的 hold 本阶段不承诺自动恢复。hold 原因闭合集合新增
  tree_integrity（路径/根/叶身份拒绝，与容量耗尽 hard_envelope_exceeded 区分；解析器吞掉的拒绝同样 hold，不封存）。
Mac：存储绑定读方要求 storage 信封（旧读方仍拒绝）；closed 429 storage_capacity_wait 经原 key 查询 404 后为健康
StageWaiting，普通 429/未知 POST 仍走原失败语义；grant/传输/解码 hold 为 StageCapacityBlocked（可见，不失败）；
Mac 工作卷总量须等于策略值，实时余量不足以覆盖下限+在途承诺时为同一 attempt 的等待；v5 LOCAL 在解码后、改写 staging 前
与提升前检查 stage 截止；release binding 拒绝超过 Mac 配额 D 的临时盘/输出上限。
CLI：python -m disclosure_anchor.cli.execution_upgrade 新增 legacy-key-lookups（只读 GET）与 propose-qualified；
execution_upgrade 其余子命令与 U01 v1/v2 合同不变。
策略校验：C 必须容纳硬结果的物理 completion 计费（physical_charge(hard) = 分配单元取整 + 每文件开销 ≤ C），而不只
是逻辑 hard ≤ C；原生账本按物理计费对 P + C 预留 completion，源可恰好填满 P。
合格升级 POST 边界：VerifiedQualifiedExecution.require_submission(command, proof, *, now_unix) 对 qualified 升级以批准的
key 寿命与传输墙钟在 POST 发出前复核（now − submission_epoch_unix ≥ TTL 即拒绝，缺时钟即拒绝）；查询命中任务的对账
不经此检查；U01 v1/v2 无批准寿命，行为不变。拒绝时 head 保持 reconciling、协调器可见停止，不失败、不 POST、不换 key；
过期 key 的结案见下文受管入口。
容量 hold 与 F5：Mac 对原生 storage_blocked、spool_* 完整性 hold 与任何账本都装不下的 grant 以 coordinator_circuit
首因公共停止（reason native_storage_hold / transfer_integrity_hold / stage_grant_unsatisfiable），decode/传输预算/发布包络
hold 保持单 attempt，只要其它路径还能推进就不停；无在途、无自唤醒路径（重试/等待计时器、外部租约、准入观察、stream
延后候选、仍能带来可运行工作的准入：未读扫描位置、会重探的 readiness 延后、安全 stream 暂停）而只剩 hold 或被挡住的
排队时一次停止（capacity_holds_exhausted 点名阻塞 hold，否则 resource_credit_grant_unavailable 点名首个受阻队首）；
AdmissionOutcome.held_for_credit（原地等信用）与 deferred_on_obligations（未结 legacy obligations）都不算唤醒；操作员
排空只剩 hold 时以 operator_drain 结束，不新写公共停止；不失败、不 cleanup、不 ACK。LOCAL_PREPARE 连同 LOCAL 完成输出
额度一起准入，materializing head 的完成承诺由 durable 有效预留减已持有额度重建；StageCapacityBlocked 可带类型化
CapacityHoldDetail，CoordinatorResult.diagnostics 只含闭合 token/ID/整数/sha256。
RemoteProviderWaitingV4 的 hold 须恰为闭合原生 hold 原因（NATIVE_STORAGE_HOLD_REASONS，线缆解码器复用）。
原生 hold 操作员决定：POST /agent/storage-holds/{task_id}（仅操作员；执行与 Mac worker 不调用）preview →
mineru.storage-hold-preview.v1（含 preview_sha256），execute(expected_preview_sha256, decided_by, reason, fixed_by)
→ mineru.storage-hold-decision-receipt.v1；task failure cause 新增闭合码 storage_hold_terminated（附 hold_reason、
decision_sha256，retry_class permanent），原生校验与 Mac 解码器同步；Mac 失败为 provider_storage_hold_terminated /
provider_terminal。操作员门（路由与 worker 共用隧道 origin）：先要求 Authorization: Bearer，比对容器内登记的
mineru.storage-hold-operator.v1（/run/agent-invest-operator/storage-hold-operator.json，只存凭据 sha256，0700/0600，
非 bind mount，enroll_storage_hold_operator / revoke_storage_hold_operator 经 docker exec），常数时间比较摘要；
未登记或登记不安全 403 storage_hold_operator_disabled / storage_hold_operator_misconfigured，缺失/畸形/错误凭据 401
storage_hold_operator_unauthorized（WWW-Authenticate: Bearer），均在读请求体与任何 registry 读取之前；之后
Content-Length > 16384 → 413，请求体分块读取、超过 16 KiB 即 413。Mac CLI：python -m
disclosure_anchor.cli.storage_hold operator-verifier|preview|execute，preview/execute 必须 --operator-token-file
（本人所有 0600、一行 43–128 个 URL 安全字符，不来自 settings/环境变量，错误信息不含凭据），operator-verifier
只打印 credential_sha256、不连 DB/网络（receipt disclosure.storage-hold-preview.v1 /
disclosure.storage-hold-decision.v1，不覆盖、同决定重放同字节）。
过期 prepared 受管结案：worker-expired-prepared-closure-plan.v1（固定清单、origin、实际 TTL、成员 H0/key/过期时刻、
仍有效成员）；CLI python -m disclosure_anchor.cli.expired_prepared_closure preview|execute（receipt
worker-expired-prepared-closure-receipt.v1）；失败 original_key_expired / original_key_lifetime / error_stage
managed_closure，经既有 pre_submission_failure → cleanup_pending → pre_submission_failed 与最终失败提交。execute 逐
成员各自持久 CAS 提交，无批量事务：后面成员被拒时前面成员已结案，同一计划与决定重跑安全续做。结案从不排队；
放行是既有显式 parse-requeue 决定（0065 起接受 original_key_lifetime，见下条）。public view、Filing API 与 change
feed 不变。
设计：docs/implementation/design/mineru-result-storage.md、local-execution-upgrade.md（Qualified result runtime upgrade）、
mineru-stream-admission.md（v2 算法）。未执行：Windows PowerShell 安装/采集/健康检查、原生实机 Range、scratch
PostgreSQL、真实 PDF/GPU；独立测试与最终门由 root 执行，本条不宣称已通过部署资格。
```

2026-09-27（0065 显式 parse-requeue 接受受管过期 prepared 结案类）——public view/API/change feed 不变:

```text
只增 revision 0065_requeue_key_lifetime（down_revision 0064_retained_registration）在一条 ALTER TABLE 内替换
disclosure_ops.parse_requeue_decision 的 ck_parse_requeue_decision_class：0063 的五个契约类原样保留，只加
original_key_lifetime（受管过期 prepared 结案写入的重试类，错误码 original_key_expired）。ORM CheckConstraint 与
RELEASABLE_PARSE_RETRY_BUDGET_CLASSES 同步为同一六类闭集合；未知/畸形类仍被合同与 DB CHECK 同时拒绝，自动类
（item/infrastructure/neutral）仍不需要也不接受决定。不加表、不加列、不改授权、不改任何行或视图；队列与 doctor
共用的 PARSE_UNRELEASED_CONTRACT_FAILURE_SQL 本来就排除所有非自动类直到有指名该 run 的决定，因此不变——结案 run
在决定之前留在队列外，决定只放行它指名的那一次失败，结案本身从不自动放行。downgrade 在已有 original_key_lifetime
决定时报错（不删除、不改写），否则恢复 0063 的逐字 CHECK。0063/0064 不改。测试：tests/unit/test_migration_state.py
（0063 逐字恢复、ORM/合同一致、升降级语句与降级守卫）、tests/unit/test_parse_requeue.py（该类 dry-run/记录/失败 run
不变）、tests/integration/test_ops_queue_views.py（CHECK 接受该类且以 ck_parse_requeue_decision_class 拒绝未知类）。
未执行：scratch PostgreSQL 迁移与集成测试、生产 make migrate / 结案 / 重排（root 授权）。
```

2026-09-27（大文档资源生命周期更正：重活许可、工作配额 D、私有发布包络、原生 hold 决定原文；候选，未部署）——public
view、Filing API、change feed 与 migration 不变:

```text
协调器：CoordinatorLimits 新增 heavy_work_permits（正整数，默认 1）、work_disk_bytes（None 或正整数）、
work_disk_margin_bytes（非负）；capacity v2 组合为 mac_work_disk_limit_bytes 与 mac_work_file_margin_bytes(policy)
= (max_members + 8) × 4096。work_disk_footprint(credits, margin) = snapshot + max(temp_disk, compressed + output)
+ margin（无占用为 0）；持久 credit 与在途 transition hold 的占用之和只在增长时受 D 检查（缺口 work_disk_bytes）；
COMMIT/CLEANUP/ACK 不增长、从不被 D 阻塞。work_disk_local_reserve_bytes（非负且小于 D；capacity v2 组合为
mac_document_disk_upper_bound(policy, hard, single)，即一份最大 grant）：准入只提供 D − reserve − 在用 的余量，
先为每个可准入文档扣除 margin（documents ≤ 余量 // (margin + 1)，snapshot_bytes = 余量 − documents × margin），
准入后校验以 D − reserve 为界；D 受阻的队首只让后面尚无 LOCAL 占用的新 grant 等待，且仅当队首在 LOCAL 工作排空后
装得下（队首增长 ≤ D − 仅源快照占用），否则装得下的 grant 越过队首以排空已恢复的超额快照。重活许可：COMMIT 派发时取得；
LOCAL 不持许可派发，到解码点抛 StageHeavyWorkRequired（StageWaiting 子类，健康等待）后带许可重派；许可随 Future
完成释放（成功/错误/等待/取消），CLEANUP/ACK 从不需要，被挡的 lane 在 credit_blocked_by_lane 记 heavy_work。
执行守卫 StagedExecutionGuard.heavy_work_permitted: bool | None（不参与比较，None = 独立调用方）；端口
MaterializationHeavyWorkRequiredV4 与 require_heavy_work_permit(stage_guard)（仅守卫明确为 False 时抛出）。后端：
run_local 映射为 StageHeavyWorkRequired；commit 在守卫为 False 时直接拒绝；PublicationEnvelopeExceededError →
StageCapacityBlocked(dimensions=("publication_envelope",))；commit 中 MaterializationCapacityWaitV4 → StageWaiting。
物化器：granted LOCAL 在每个成员与其记录都已持久、记录退役前要求许可（staging 仍精确续做）；重放已提升输出与
reopen_materialized_v4 前要求许可；cleanup 转移与 promote_or_replay 不再解码，由 receipt 的输出清单（逐文件
SHA-256、总字节、文件数）在 rename 前后证明；LOCAL 提升后以封存 staging 的文件清单证明，不二次解码；卷绑定拒绝
f_frsize > 4096；新增 publication_write_space(attempt_id, byte_count)（实时余量承诺，publication_capacity_waiting）。
就绪：新端口 PublicationWriteSpacePort；适配器可选 write_space，第一次写入前先编码准备与清单，再只为尚不存在的
文件承诺；同一准备复用刚编码的 artifact 字节；verify_ready 以请求 sha 比较。
私有包络（8 MiB 请求 / 24 MiB 准备 / 8 MiB 清单，数值不变）：PublicationEnvelopeExceededError（byte_count、limit，
消息不变）与 PublicationArtifactEnvelopeExceededError（同时是 AtomicPublicationArtifactReadinessError）在编码超限时
抛出，均早于任何就绪写入与事务 P；读回超包络仍是完整性拒绝。winner 8 MiB DB CHECK 不变，未单独类型化（由请求包络
支配）。
策略校验（mineru_capacity_config，原生随镜像分发）：新增 MAC_ALLOCATION_UNIT_BYTES = 4096、mac_work_file_margin_bytes；
拒绝 maximal grant + source_pdf_bytes_limit + margin > D（原有 maximal grant ≤ D 检查保留）。
原生 hold 决定（agent_task_protocol_v2，随镜像分发）：storage_hold_terminated 原因新增闭合 decision 对象
mineru.storage-hold-decision.v1（schema、preview_sha256、decided_by、reason、fixed_by），decision_sha256 为其规范 JSON
的 sha256，原生校验与 Mac 解码都重算；收据含 decision，重放从持久原因返回。Mac：StorageHoldDecisionV1、
RemoteProviderFailureCauseV4.decision（hold 码必需且摘要一致，其它码必须为 None），decode_task_failure_cause_v2
严格解码。路由在把分块并入缓冲前检查 len(raw) + len(chunk) > 16384 → 413。Mac CLI 新增 storage_hold recover
--attempt-id --out（普通 status 路由，无凭据，只重建收据）；execute 应答丢失时从 status 读回，只有逐字段相同的持久
决定才完成。已论证无需新锁：hold 任务重启后以 processing 水合，唯一调度点只取 pending。
测试：tests/unit/test_staged_coordinator_resource_lifetime.py（新：许可互斥与释放、两任务真实调度器缩放文件见证、
恢复归属只计一次、准入预留与逐文档余量、预留下等待 grant 必然完成（无预留对照停滞）、超额恢复快照先排空、
footprint 规则、publication_envelope hold），tests/unit/test_staged_v4_capacity.py（存储绑定组合 D/margin/reserve），以及 test_mineru_materialize_grant_v5.py、
test_mineru_http_staged_v4.py、test_staged_coordinator_backend_v4.py、test_atomic_publication_artifact_readiness_adapter_v4.py、
test_atomic_document_publication_v4.py、test_mineru_result_storage.py、test_mineru_result_storage_generated.py、
test_mineru_result_grant_mac.py、test_storage_hold_cli.py 的新增/更新用例。
未执行：原生镜像重建/attest/资格、scratch PostgreSQL、真实 PDF/GPU、真实内存（W 不是 OS 内存上限，许可只串行化
重活，不度量 RSS）；独立测试与最终门由 root 执行，本条不宣称已通过部署资格。
```


2026-09-27（R2 采集流式交接与空间错误分类；候选，未部署）——public view、Filing API 和 migration 不变：

- `DisclosureSourcePort.download_pdf_to(ref, sink)` 返回 `CompletedPdfTransfer`；API/web 请求 identity 编码并逐块
  写入 owned staging，同一逻辑期限包含重试与 EOF。非 identity 响应在正文前拒绝，实际字节数验证完成性。
- `RawDocumentStore.from_settings` 在 worker、pipeline、本地登记 admin 三个写入口使用同一
  `DISCLOSURE_ACQUISITION_FREE_FLOOR_BYTES`（未设为卷容量 10%）。归档完整大小预检与逐块余量检查在实际 copy 内；
  已存在同 hash 原件复用不额外预留副本。检查不是跨进程预留，外部写入量不在保证范围内。
- 输入不合法、归档身份冲突、目标存储 IO/容量不足分开：目标 ENOSPC/EIO 不能转成 `InvalidRawDocumentError`；
  下载保持有限可重试失败，本地登记记录失败访问再抛出。同文件登记、no-replace、hash、supersedes 语义保持。
- 完整 staging 经文件及目录 fsync 后封存；仅在归档或完整已验证 quarantine 接管后删除。
  quarantine manifest/result 明确 `transfer_complete` / `payload_complete`、`input_missing`、大小/hash；失败访问
  新增 `transfer`、`capacity`、`quarantine_complete` 和 `retained_*` 身份字段，保留唯一完整输入。
- 相关验证见 `test_acquisition_streaming_independent`、下载/归档/本地登记/admin 普通测试，以及 managed-scratch
  `test_cninfo_download`、`test_register_local_pdf`、`test_cninfo_sync`。离线测试不替代实际 provider 编码兼容性或断电证明。

2026-09-29（发布私有记录单一包络政策 PublicationEnvelopePolicyV1、整计划写前测量与类型化容量诊断；候选，未部署）——public
view、Filing API、change feed、migration 与 winner 8 MiB DB CHECK 不变:

```text
application/contracts/publication_envelope_policy.py 是发布链私有记录唯一的源码固定包络：
PublicationEnvelopePolicyV1（frozen；identity = sha256(canonical JSON)；无 settings/env/fallback）按记录类
声明预算：request（规范请求、准备记录中的嵌入请求文本、上游证据/处理投影/上下文等组件）、previous_active_inventory
（前一活动清单，request 预算）、unit/unit_row（单个 pre-ID Unit 记录、routed-draft 与最终 Unit 行/lineage 行
hash 输入，unit 预算 ≤ request）、preparation、readiness/unit_bindings（清单与 final/lineage 聚合，readiness
预算）、winner（winner 记录与 outbox 行/聚合/durable base，≤ 已应用 0057 CHECK 8 MiB）、snapshot
（document_units.v1.jsonl）、semantic（semantic_route_receipts.v3.jsonl）。请求/就绪/winner 三个模块的编码器、
解码器、存储读回上限与嵌入请求读回全部取自该值；原 8/24/8 MiB 私有常量与就绪合同内的嵌入请求 8 MiB 字面量
删除，models/0057 的 DB CHECK 仍为独立 DB 事实（测试断言三者一致）。scripts/gc_orphan_artifacts.py 读取
preparation 所有者的读前上限同样取自 preparation 预算（原 24 MiB 字面量删除）；超出预算、第二硬链接、符号链接、
他处路径、读取期间变化与无效内容仍使 GC 失败关闭。发布值（root 决定）：request 64 MiB、
unit 64 MiB（单个 Unit 可接近整个请求）、preparation 160 MiB、readiness 8 MiB、winner 8 MiB、snapshot 128 MiB、
semantic 64 MiB；identity sha256:04262751b9a70bdb1d4d10bc6c8f47c3557a990f0fca09944e988604de152a0b。
不设页数或 Unit 数准入上限，原资源配额与 heavy1 不变。支持域是各记录预算与 winner 保守准入同时满足：
请求在 64 MiB 内不代表其它记录必然装得下（例如 Unit 多时 winner 上界先拒绝）；观测到的密度不构成请求与
winner 之间的普遍关系。
规范 JSON 选项、字段、合同版本、hash 与小记录字节均不变（56abdb93 同输入逐字节 sha 一致，已 pin）。
写前整计划测量 application/services/publication_envelope_plan_v4.py：就绪适配器在第一次写入前编码 preparation
与 readiness 各一次，按写入顺序测 request/preparation/snapshot/semantic/readiness 精确字节，并以与事务 P 相同
的投影函数（固定宽度 ID、最宽 BIGINT outbox 序号、最宽 UTC 时间）计算 winner 保守上界；第一条超出即拒绝，
早于任何 preparation、promotion、资源文件、readiness 写入与 P；P 内仍精确校验 winner。
类型化事实：PublicationEnvelopeExceededError(fact)；PublicationCapacityFactV1 = record_kind（闭合词表）、
bound（exact|lower_bound|upper_bound）、byte_count、limit、policy_identity；错误消息与类层次不变
（PublicationArtifactEnvelopeExceededError 仍兼为就绪错误；winner 编码超限现为同一事实类型且仍是 ValueError）。
exact/lower_bound 表示记录本身超限；upper_bound 是尚未写出的 winner 的保守投影，其拒绝不证明事务 P 实际
winner 超过 8 MiB；投影途中 winner 组件超限同样按 upper_bound 报告。读回超限仍是完整性拒绝，不改判为容量 hold。
诊断接口（与调度器合并为一条）：COMMIT 把事实映射为 StageCapacityBlocked(dimensions=("publication_envelope",),
detail=CapacityHoldDetail(record_kind, byte_count, limit, bound, policy_sha256))；CapacityHoldDetail.bound 取闭合值
CAPACITY_HOLD_BYTE_BOUNDS（exact|lower_bound|upper_bound，取代原 lower_bound 布尔），payload/观测标量键为 bound；
调度器把它放进 CapacityHoldEvent 与 NoProgressSummary（CoordinatorResult.diagnostics 最多 64 条；summary 每 lane
至多一个队首、每维度至多一条 pressure、hold 至多 8 条）。删除 PublicationCapacityBlocked 子类、后端
publication_envelope_hold note、safe_line 与 PUBLICATION_CAPACITY_HOLD_LINE。CLI：_end_staged_resident 先锁存首因
（无首因时 unclassified_circuit），再只打印重试耗尽行与精确类型 CapacityHoldEvent/NoProgressSummary 的
"[staged-v4] diagnostic " + 排序 ASCII JSON；其它类型或无法编码的记录丢弃并只计数，errors 其它条目不打印；
畸形结果或日志流失败不阻止首因锁存、原生停用与公共停止（exit 78）。
表示副本：Preparation 构造只解码嵌入请求一次；快照逐 Unit 行编码并在首个超限前缀处以 lower_bound 停止；
准备/就绪配对校验对每条记录只编码一次；写前计划的 preparation 字节写入后即释放；diff 只为变化对解码投影。
回退边界：写出超过 56abdb93 读取上限（请求 8 MiB、准备 24 MiB、就绪 8 MiB）的记录后，旧 reader 读不回，
不能盲回滚到 56abdb93。
测试：tests/unit/test_publication_envelope_policy.py（发布值 pin/事实闭合且可映射为 hold detail/读写对称/整计划
±1 与 winner upper_bound 保守拒绝/winner 上界精确/>8 MiB 多 Unit、>8 MiB 单 Unit（CJK）与转义密集单 Unit 全链/
小记录字节 pin）、test_atomic_publication_artifact_readiness_adapter_v4.py（winner 上界写前拒绝零写入、测量期间
阶段失效零写入、测量 note）、test_staged_coordinator_backend_v4.py（三种 bound 的事实映射为 detail）、
test_staged_completion_credit.py（detail/summary 闭合与上限）、test_worker_cli.py（真实协调器 + V4 后端 commit
映射到仅 hold 的公共停止与诊断输出；未锁存 circuit 先锁存后记录、伪造/无法编码诊断丢弃、日志流失败）、
test_gc_orphan_artifacts.py（>24 MiB 有效 preparation 保有其 bundle；越过预算、第二硬链接、符号链接、他处路径、
读取期间变化、非规范内容与孤立就绪均阻断 GC）；fixture tests/unit/_publication_family_fixture.py。
root 已用 managed scratch PostgreSQL 验证事务 P、读回、响应丢失重放与跨重启恢复；两个需要旧实机 Q0 环境的
可选用例未执行，实际部署仍须通过当前 Q0 与全部旧职责的预检。真实材料只有 3 份保留诊断重建的纯解码/配对/整计划
测量；纯离线 RSS 测量不等于整机最坏情况资格。不宣称 84 项职责全部装得下或 8 MiB DB CHECK 对其足够：它们以原
身份在普通守卫下恢复，按实际字节准入。独立复审与最终门由 root 执行，本条不宣称已通过部署资格。
```
