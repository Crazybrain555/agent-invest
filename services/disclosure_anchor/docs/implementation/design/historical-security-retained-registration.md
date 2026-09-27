---
id: disclosure_anchor_historical_security_retained_registration
project: disclosure_anchor
title: 历史证券绑定与保留原件登记
status: current
created_at: 2026-09-26
authority: 语义上位为 architecture/service-purpose.md §10.1/§10.2；本文件是 0064、历史证券解析与 source_recovery CLI 的实现契约
---

# 历史证券绑定与保留原件登记

## 1. 问题与边界

同一上市法人换证券代码后（首例：中航成飞 300114→302132，官方公告 2025-028，新代码 2025-02-17
启用、法人主体存续），当前公司 302132 的索引会返回仍标 300114 的历史候选。下载归档成功后，
注册找不到 300114 Security，写入 `registration_metadata_error / retryable=false`，候选因此永久出队；
原件已归档但没有 Document。

本契约只补三件事：有证据的具名历史证券绑定、从已归档点注册的保留原件登记、精确到单条失败的
解决关系。它**不是**全市场历史代码/主体平台，不把 orgId 或名称升格为强键，不建立别名闭包，
不处理合并/分拆/代码复用，不新增队列、作业表或重试守护，不改解析/MinerU/容量。

失败批次（首批 42 条、E4 启动后新增的失败等）是证据 cohort，不是产品常量：每一批都要新鲜
preview 与具名计划，已批准的计划与日期范围不会自动扩展到新失败。

## 2. 身份职责

| 信息 | 允许承担 | 禁止承担 |
|---|---|---|
| company_id | 服务内同一法律实体 | 全局主体 / PDF 正文主体 |
| 有来源、active 的 USCC | 锚定当前公司 | 合成一份不存在的旧代码 profile |
| 官方换码公告 | 证明本次换码与生效日 | 证明所有重组同主体 |
| 具名绑定（binding） | 把证据与 company/新旧 Security/批准范围连起来 | 无来源 override、隐式合并、放行任意 orgId |
| provider orgId | 带来源的一致性否证 | 强键、`.first()` 唯一性、传递闭包 |
| index SourceAccess 查询范围 | 说明候选怎样获得（302132 查询） | 反向改写候选代码 300114 |

Document 的 company/security 仍是获取/登记范围：历史候选登记到同 company 的历史 Security（300114），
index 访问仍指向 302132 查询；两者经新来源记录中的 index ID 与 binding 相连，不做 canonical issuer 推断。
被收购子公司不是该 company 的别名。

## 3. 历史绑定 `historical-security-binding.v1`

闭合决定文档（`application/contracts/historical_security_registration.py`）：`provider=cninfo`、
`event_kind=same_legal_entity_security_code_change`（唯一取值；同码仅改简称、继受但不存续等不走此模式）、
`target_company_id`、`exchange`（SSE/SZSE/BSE）、`current_security_id/current_code`、`old_code`（≠current）、
`security_code_effective_date`、新旧简称、`expected_uscc{identifier_id,value,profile_source_access_id}`、
`official_evidence{url,sha256,byte_count,announcement_id,pages,local_evidence_ref}`、
`approved_index_interfaces`（p_info3015/hisAnnouncement 的有序子集）、`approved_query`（必须是目标公司
当前证券）、`approved_announcement_range{from_inclusive,to_exclusive}`（`to_exclusive ≤` 生效日；下界是
人工批准的获取范围，不是上市日期）、可空 `query_org_observation{value,provenance=profile_context,
source_access_id}`、`decided_by/decided_at/reason`、`decision_basis[]`。

- 解码拒绝未知/重复键、非有限数、NUL 与孤立代理、非规范 code/exchange、绝对或含 `..` 的证据引用。
  身份 = 规范 JSON（紧凑、键排序、UTF-8）的 sha256；hash 证明完整性，不等于签名或授权。
- 存储为一行 SourceAccess：`provider=cninfo`、`provider_interface=local:historical_security_binding.v1`、
  `dataset_key=historical_security_binding.v1`、`status=ok`、`company_id`=目标公司、`security_id`=历史 Security、
  `result_hash`=决定 hash、`result_snapshot`=决定本身。它是本地确认的导入，不伪造 `cninfo:p_stock2100` 调用。
- 事实（provider、事件、公司、交易所、新旧 Security/代码、生效日、USCC 锚点三元）在同一历史 Security
  的全部绑定间必须一致；批准范围与 org observation 是单个决定的属性。追加只能是事实一致的新决定，
  从不覆盖、撤销或"最后一条生效"；事实冲突则整条历史路径拒用，直到复审。
- 导入（`HistoricalSecurityBinding`）：preview 只读；execute 先以 `FOR NO KEY UPDATE` 锁当前 Security，
  重验锚点与已有绑定，同一事务内新建 `status='historical'` 的 Security（缺失时）并追加绑定行。
  相同决定 hash 再次执行返回原行（部分唯一索引兜底并发）；`decided_by` 必须与执行人一致；证据文件
  hash/字节须与决定一致；旧代码已属其他公司或非 historical 状态的 Security 一律拒绝。
- 绑定从不修改 company、USCC identifier、当前 Security、tracked 行或 checkpoint。误绑定的修正需先停相关
  入口、保留证据、另行审查受影响 Document；本契约不提供"撤销后自动找上一个"。

## 4. 获取主体解析

`application/services/source_security_resolution.resolve_acquisition_subject` 是下载与恢复共用入口。

- 当前（非 historical）证券：探测规则、SubjectCandidate 构造与 `SubjectResolver.resolve` 调用逐字不变。
- 历史证券：须同时满足——
  1. 调用方给出承载该候选的 index SourceAccess（cninfo、index 接口、status=ok、有 result_hash 与
     company/security）；其中该 pid 的候选唯一，且与待登记候选规范 hash 相同；
  2. 该历史 Security 的全部绑定可解码、hash 未漂移、事实一致；
  3. 至少一条绑定批准该 index 接口、查询 owner（index 的 company/security）与公告日期；
     恢复计划可指定绑定，普通下载取 ID 最小的适用绑定；
  4. 所选绑定的锚点现读仍成立：公司 USCC=锚定值；该 identifier active、来源为锚定 profile 访问；
     该值无 contested、无他司持有；profile 访问为 cninfo p_stock2100 ok 且观测到该 USCC 与当前代码；
     当前 Security 未漂移；tracked 行仍指向当前 Security；org observation（若有）对应本公司 active 行；
  5. orgId 只作否证：snapshot `identity_context.query_profile_org_id`（旧 snapshot 取候选的 legacy
     `provider_org_id`，只当查询上下文）或候选自带 `candidate_provider_org_id` 与绑定 observation 不等、
     或该 org 属于他司 → 拒绝；相等不构成任何证明。
- 任一条件不满足 → `RegistrationMetadataError` 子类（`HistoricalSecurityBindingRequiredError` /
  `HistoricalSecurityProvenanceError`）；在下载中照常记为 `registration_metadata_error / retryable=false`，
  原件保留，不自动重试。
- 通用 `SubjectResolver.resolve` 与本地登记（`RegisterLocalPdf`，归档前预检）遇 historical Security 直接拒绝，
  不修改任何 identifier 状态；不存在 `allow_history=True` 之类的绕行开关。
- 返回的 `HistoricalAcquisitionProvenance` 只能由解析器签发（模块私有令牌），调用方无法自称"已验证"。

## 5. 候选与失败记录

- 索引 snapshot（有候选时）新增 `identity_context{version=1, query_profile_org_id,
  query_profile_source_access_id, candidate_org_source=announcement_ref}`；每个候选新增
  `candidate_provider_org_id`（provider ref 自身 orgId，缺失即 null）。候选旧字段 `provider_org_id`
  保持原义（查询 profile 的 org 投影），旧 snapshot 不改写、不补写。空 snapshot 形态不变。
- `DownloadDocumentCommand.index_source_access_id`：worker（pending 行 `source_access_id`）、CLI sync、
  legacy 仓储（`PendingDownloadCandidate` 类型信封）均传入；缺失时当前证券照常登记，历史证券拒绝。
- 新失败记录：`error` 增 `failure_phase`（candidate/download/archive/registration，`stage` 仍为 download）；
  `query_params` 增 `index_source_access_id`（已知时）；`result_snapshot` 增 `candidate_sha256` 与
  `archive`：注册失败为 `{archive_completed:true, raw_file_relpath, raw_file_hash, byte_count, raw_created}`，
  其余为 `{archive_completed:false}`。`result_hash` 仍为 null（失败不是成功证据）。已有失败记录不补写。

## 6. 保留原件登记

`application/use_cases/recover_archived_registration.py`；只读原件、无 HTTP/下载器/解析器/模型。

- **请求**（`retained-registration-request.v1`）：精确的 `failed_source_access_id`、承载其候选的
  `index_source_access_id`、期望 raw hash 与字节数；无查询、无 glob、无"latest"。
- **preview**（只读，逐项；任何一项拒绝即不产生计划，输出全部拒绝原因）：
  失败须为 cninfo 下载、`registration_metadata_error`（闭集）、retryable=false、阶段为 registration
  （旧记录无阶段字段视为 registration）；新记录记下的 index ID 须与请求一致、`archive_completed=false`
  拒绝；index 访问不得晚于失败；候选经解析器完成第 4 节全部验证且适用指定绑定；同 pid 不得有请求外的
  未解决不可重试失败，失败总数不得达到 `CNINFO_MAX_RETRIES`（恢复不重置预算）；原件目录
  `raw_documents/cninfo/<候选代码>/<公告年>/<pid>/` 必须恰有一个规则文件 `sha256_<hex>.pdf`（非链接、
  PDF 魔数、内容与名称一致），与请求期望及新记录的归档事实一致；同 pid 已有不同 raw 版本拒绝；
  已有同 hash Document 必须同主体同路径；已有成功回执必须是同一义务（计划标 `already_resolved`）。
- **原件读取**：打开前 `lstat` 须为普通文件；以 `O_NOFOLLOW|O_NONBLOCK` 打开，`fstat` 复核普通文件且
  身份等于打开前所见，再恢复阻塞读取。FIFO/设备在打开前被拒；检查后被换成特殊文件也只会使描述符复核
  失败，不会阻塞在 open。读取前后身份、字节数、PDF 魔数与内容地址核对不变。
- **计划**（`retained-registration-plan.v1`，规范 JSON）：逐项绑定失败投影 hash（原列投影，排除后加列）、
  index ID/hash、候选 hash、公告日期、原候选代码、查询范围、目标主体、raw 路径/hash/字节、关联依据
  （`failure_record_archive_binding` 或 `post_failure_archive_inventory`——后者是事后清点，不冒充失败时的
  HTTP 响应 hash）、Document 状态；另绑定 binding ID/hash、请求 hash、`max_items` 与 recovery 代码摘要。
  raw 路径是闭合的六段归档形态：pid 段与归档路径构造器同一规则（拒绝空、`.`/`..`/点开头、`/`、`\`、
  控制/格式/未分配字符），其余段形态不变，文件名等于 raw hash。
- **execute**：计划只能按其精确字节与 sha 执行，且 recovery 代码摘要必须一致；逐项：
  shared corpus 准入 → 只读重验原件 → 单独 UoW 内 `FOR NO KEY UPDATE` 锁失败行 → 先查成功回执
  （存在则核对为同一义务并返回 `already_completed`，不新增访问/事件）→ 重验失败投影、index、候选、
  绑定、阻断与 Document 状态 → 共享注册核心 → 同一事务提交 Document（或 observation）、回执与 outbox。
  重验同时核对计划的全部谱系字段（失败码/原因、index 接口、原候选代码/交易所/公告日、关联依据与新记录
  的归档事实）；计划 raw 路径必须等于由重验后的候选代码、公告年、pid 与 raw hash 推出的本条归档路径，
  否则 `RAW_ARCHIVE_MISMATCH`——执行只登记本文档自己的归档文件，不登记计划所指的任意路径。
- **回执与同一义务**：preview、execute（回执优先与提交结果不明读回）与 reconcile 用同一判定，与队列事实
  视图的解决关系一致：失败行仍是该 pid 的 cninfo `download_pdf` 失败；回执的 `recovery_of_source_access_id`
  正是该失败行、`provider` 与失败行相同、为成功的 `local:register_retained_pdf.v1` 且同 pid；Document 按
  回执的 provider/pid/raw hash/company/security 存在。再核对计划义务（raw hash 与目标主体）。跨 provider、
  链接错位或错 pid 的回执一律 `RECEIPT_CONFLICT`（失败行不符为 `FAILURE_RECORD_CHANGED`），不会被当作已完成。
  Document 唯一冲突至多一次受控重读；提交结果未知时在同一行锁下读回回执再判定；首个拒绝即停止，
  已提交前缀保留，未执行项标 `not_executed`，重跑跳过已完成项。结果文件只是证据，数据库回执为准。
- **reconcile**：只读，逐项报告 resolved/unresolved/conflict 与失败历史是否未变；conflict 表示回执与
  Document 或队列事实视图不一致。
- **共享核心守卫**（`register_document`）：普通调用逐字不变（多来源 observation 语义不变）。恢复时
  raw 必须 `created=False` 且与 provenance 一致；既有同 hash Document 必须同 company/security/路径；
  无同 hash Document 时同 pid 不得已有任何版本（旧归档不按插入时间 supersede 新版本）；这些检查先于任何
  写入。回执 `provider_interface=local:register_retained_pdf.v1`、`dataset_key=retained_archive_registration.v1`、
  `result_hash`=raw hash、`recovery_of_source_access_id`=失败 ID；lineage 位于 `query_params.index_source_access_id`、
  `result_snapshot.acquisition_provenance` 与 `result_snapshot.retained_registration`，保留键不可被覆盖。
  普通历史证券下载成功时同样记录 `acquisition_provenance`，但没有回执列。

## 7. 持久化（0064，append-only）

- `source_access.recovery_of_source_access_id varchar(64) NULL`，`FK → source_access ON DELETE RESTRICT`；
  `ck_source_access_recovery_receipt`：非空时必须是 `local:register_retained_pdf.v1`、ok、result_hash/
  company/security 非空且不自指；`uq_source_access_successful_recovery`（非空部分唯一）。`provider_interface`
  可空，故接口条件写作 `IS NOT DISTINCT FROM`：链接非空时每一项都是二值，NULL 接口使 CHECK 失败而非以
  UNKNOWN 放行（`status` 为 NOT NULL）。
- `ck_source_access_historical_binding` 与 `uq_source_access_historical_binding_decision`（绑定行
  `result_hash` 部分唯一）。
- `disclosure_ops.download_failure_resolution_v1`：每条 cninfo 失败下载的 `nonretryable` 与
  `resolved_by_source_access_id`（仅当唯一成功回执的 provider/pid/raw hash/company/security 与现存
  Document 一致）。`pending_download_v1` 以 `CREATE OR REPLACE` 重建，列不变，终态排除改为"存在未解决的
  不可重试失败"；`failed_download_count` 仍计全部失败，`max_retries` 不变；0023 字节不变，downgrade
  逐字恢复 0023 视图。
- 部署顺序：先 `make migrate`（旧代码可在 0064 上运行：新列可空、视图列不变），再部署读取新列的代码。
  加列取短暂 ACCESS EXCLUSIVE；CHECK/FK/唯一索引各扫描一次 source_access——在维护窗口执行。

## 8. 诊断与一致谓词

同一事实视图供四处读取：`pending_download_v1`、`queries.download_dead_letter_count`（health
`download_dead_letters`）、legacy `SourceAccessRepository._terminal_download_failure`，以及 doctor
`download dead letters`（`queries.download_failure_resolution_summary`：不可重试失败总数、已由保留原件登记
解决数、未解决数与死信候选数；有死信为 WARN）。历史失败从不删除或改写；之后新的不可重试失败即使同 pid
仍重新阻断；失败重放或孤立回执不能解除阻断。

## 9. 运维

命令、窗口与核对见 `runbooks/production-operations.md` §5.5。执行前由 root 核对最终代码/发布身份、
0064 已应用、维护状态与备份；执行纯登记时使用维护停止窗口，否则常驻 worker 会按既有范围继续解析新
Document（CLI 本身不调用模型，不等于系统总调用为 0）。

## 10. 已知限制与延期

- 历史 Security 占用 `(code, exchange)` 唯一键；真实跨主体代码复用需另立契约，本入口拒绝。
- 预算耗尽、`subject_identity_conflict` 等其他失败类不在本入口；扩集合需新契约。
- orgId 否证导致普通下载拒绝时，经保留原件登记 + 显式绑定处理，不放宽否证。
- 无绑定撤销；B2（>1000 页结果封套）与历史 NOTMET 不受影响。

## 11. 仍需在真实 PostgreSQL 上验证（独立作者 / root scratch）

0064 apply/downgrade 往返与旧行字节不变；链接非空而 `provider_interface` 为 NULL（其余合法）的行被
`ck_source_access_recovery_receipt` 拒绝；绑定 Security+访问同事务提交/回滚与并发导入；回执、Document、
outbox 同事务；两进程并发执行同一计划、提交成功但回执丢失后的重跑、前缀成功+中途失败+重跑；普通注册
并发抢先与同 pid 不同 hash；视图 / legacy / doctor / health 计数一致；未来新失败仍阻断；跨 provider
或错 pid 的链接回执在视图、execute 与 reconcile 中一致地不算解决；`FOR NO KEY UPDATE` 在 app 角色下可用；
读回在旧后端未结束时的等待行为。

## 12. 依据

Pro B1 审查与实施细则（root 已复算 6 反例、15 个 hash）与 root 裁决；官方 2025-028 PDF
sha256 `dd68049c48df826848f361fd9e7b23dd20b6805144a2e5bc36e54db638611488`（370156 字节）。
PostgreSQL 18 官方文档：SELECT 锁子句需 UPDATE 权限、行锁冲突表与事务结束释放、唯一索引 NULLS DISTINCT
默认与部分唯一索引、`CREATE OR REPLACE VIEW` 保留权限且须保持既有列、可空无默认值加列不重写表。
机制对照（GLEIF 事件/生效日、OpenFIGI 可变代码、NiFi 保留内容重放、事务性 outbox）只作设计参照。
