---
id: disclosure_anchor_milestone_04R_phase000_004_rework
project: disclosure_anchor
title: Phase000–004 返工（契约与行为收敛）
status: ready-for-implementation
created_at: 2026-07-04
depends_on: milestones 00–04（已完成）
delivers_to: milestone 05
---

# Milestone 04R: Phase000–004 返工

本文件是 Phase000–004 已实现代码的**可执行返工规格**，来自 2026-07-04 对代码逐行对照
canonical 契约（`service-purpose.md` v1.2 + 顶层协议 v0.7）的深度评审。执行顺序在 04 之后、05 之前。
实施 agent 按 R1→R8 顺序执行；每个工作包独立可验收、独立可提交。

**执行姿态（用户指示，2026-07-04）**：本返工**不受"最小直接改动"纪律约束**——项目处于初始阶段、
无外部消费者，按最优结构彻底优化，不为兼容旧实现保留过渡形态。唯一例外是 §1 清单：那些是已验证
正确的高风险实现（归档不可变性、事务性 outbox、路径安全等），"彻底"不等于重写已被证明正确的东西；
可以扩展它们，不要推倒它们。

## 0. 本返工锁定的契约决策（先读）

以下决策在本返工中定案，05–08 的规格都建立在其上，不得在实施时另行发明：

- **D1 信封最小核走视图派生，不加存储列**：`document_units_v1` 以投影补齐协议 §3.2 最小核——
  `asset_kind` = `'document_unit'` 常量；`observed_at` = `u.created_at` 别名（不 rename 列）；
  `trace_level` = `'G0'` 常量；`raw_file_hash` 来自 join 的 document。与已有 `contract_version`
  常量列、`asset_uri` 不落库同一规范。
- **D2 source_tier 不是常量，按 filing_type 派生**：投关记录 / 业绩说明会是协议 §2.9 的
  **Tier 0B**，财报 / 公告 / 业绩预告 / 问询函回复是 Tier 0A。视图 CASE 派生 +
  contract test 钉死映射。映射规则（唯一权威，写入 service-purpose §5）：
  `filing_type ∈ {investor_relations, performance_briefing} → 'tier_0b'，其余 → 'tier_0a'`。
- **D3 change_kind 落列**：`ops.outbox_event` 加 `change_kind` 列（NOT NULL DEFAULT
  'materialized' + CHECK），写侧显式必填；`change_events_v1` 直接投影，删除
  `LIKE '%observed%'` 启发式 CASE。
- **D4 document.status = public availability state（与 run/build 状态解耦）**：枚举
  `registered / parsed / parse_failed / published`（CHECK 约束；archived 预留不入枚举）。
  语义：status 只回答"public 契约下这份文档现在可消费吗"，**不**代表最近一次 run 的结果——
  已 published 的文档重解析失败**不得降级**（旧 active run 继续可用）：
  `parse success：无 current run → parsed；有 → 保持 published`；
  `parse failed：无 current run → parse_failed；有 → 保持 published，只记 failed run + observed 事件`；
  `publish success → published`。builder 自身的状态放 processing_run（新增
  unit_build_status not_started/running/succeeded/failed + unit_build_error jsonb +
  unit_build_attempt_count + unit_built_at），parse run 的 status 仍只表示解析生命周期。
  08 的队列同时依据 document.status 与 run.unit_build_status（见 08 §1）。
  latest run 不落列，由 run 表按 started_at 派生。
- **D5 主体解析 + typed identifier ledger（协议 §6.5.1 规则 1/4 落地）**：新建
  `core.company_identifier` 账本表（identifier_id / company_id / scheme / raw_value /
  normalized_value / jurisdiction / source_access_id / status active|retired|contested /
  valid_from / valid_to / observed_at / created_at；强键 partial unique：
  `(scheme, normalized_value) WHERE scheme IN ('uscc','lei','sec_cik','hk_cr') AND status='active'`）。
  scheme 含 provider 侧 `cninfo_org_id`（仅 provider 命名空间内稳定，不等同法律身份）。
  解析顺序：`(exchange, security_code)` 命中 → 沿 security 取 company；否则强键
  （uscc/lei/…）查 ledger；都未命中才新建（company + 相应 identifier 行）。命中后交叉校验：
  另一强键或 `legal_name` 不一致 → 该 identifier 置 `contested` 并抛
  `RegistrationMetadataError`，**绝不自动合并**；既有公司缺某强键而命令携带 → 同事务补 ledger 行。
  红筹/VIE 边界写入规格：开曼/香港上市主体 ≠ 境内 USCC 运营实体，identifier 不得跨主体挂接。
  `company.unified_social_credit_code` 列保留并与 ledger 同步写（迁移时从该列回填 ledger）。
- **D6 outbox 事件构造惯例**：domain 层提供事件工厂（`domain/entities/outbox_events.py`），
  统一填 `event_kind`（snake_case）、`change_kind`、`occurred_at`（use case 时钟，不依赖
  server default）。register 的 `document_registered` = materialized；重复登记复用已有
  document = `document_observed`（observed）；parse 的 `processing_run_created` /
  `processing_run_failed` = observed（run 未发布，不构成 public 可消费变化）；
  `processing_run_published` 与 unit 级事件留给 05。
- **D7 filing_type 初始词表登记**：`annual_report / semiannual_report / quarterly_report /
  performance_forecast / performance_flash / investor_relations / performance_briefing /
  inquiry_reply / other`。domain 枚举校验 + 写入 service-purpose §5；新增值走契约升版，
  禁止自由字符串（D2 的 CASE 依赖它）。
- **D8 run_kind 语义预留**：Phase05 在**同一个 parse run** 内继续 build units 并 publish；
  `run_kind='rebuild_units'`（从既有 IR 重建 unit 而不重新解析）作为保留值写入契约，
  05 不实现。
- **D9 NormalizedIR 升 v2：parser 中立元素分类**：IR 的 `kind` 不再透传 MinerU 原始 type，
  归一为 parser 中立枚举 `text / heading / table / image / equation / page_furniture / unknown`
  （`page_furniture` = 页眉/页脚/页码等版面件；`equation` 为 MinerU 实际会产出的类型），
  原始类型保留在 `raw_kind`。**任何 kind 都不得静默消失**：builder 不生成 unit 的元素必须
  进入 build 统计（丢弃计数按 kind 分桶），image 类见 05-S1 的保留规则。评审建议的
  list/chart/code/footnote 枚举值不采纳（当前 MinerU 实测：list 以 text 形态出现、chart 即
  image、footnote 已在 table_footnote 通道），但该理由不写死为"parser 永不产出"：mapper 对
  任何未显式映射的 raw type 一律落 `kind='unknown'` + 原样 `raw_kind`，禁止丢弃；`unknown`
  的 builder 处置见 05-S1（有可读文本 → 并入 text 流 + needs_review，否则计数丢弃）。
  kind 集合以本条为唯一权威，D9 / R5 / 05 前置依赖 / IR schema 四处必须逐字一致。
  没有消费者，直接升 `normalized_ir.v2`，golden fixtures 全量再生成。
- **D10 注册核心与主体解析服务化（为 07 复用而设计）**：主体解析（D5 顺序）抽成独立的
  `SubjectResolver`（application service，输入证券/公司标识候选，输出已解析或新建的
  company+security，含回填与冲突语义）；文档注册抽成 `register_document` 核心
  （去重、supersedes、source_access、outbox），`register_local_pdf` 与 07 的
  provider 下载路径都是它上面的薄入口。现在就按这个结构重构，不做临时补丁。

## 1. 明确不要动的部分（评审已验证正确）

返工时不得重写以下已验证正确的实现（churn 即回归风险）：

1. 事务性 outbox：register 单 UoW 事务内写 company/security/source_access/document/outbox；
2. 去重键 `(provider, provider_document_id, raw_file_hash)` partial unique index + 竞态恢复骨架；
3. RawDocumentStore 不可变归档（tmp 写入 + fsync + hardlink 防覆盖 + 写后重哈希 + `%PDF-` 魔数）；
4. ArtifactStore 原子写、path_builder 注入/逃逸防护；
5. UoW 默认回滚语义；parse 前 raw hash 校验、失败 run 不扰动 active run；
6. `uq_processing_run_one_active_per_document` 与 `uq_document_unit_run_order` 索引；
7. 结构化错误 JSON（stage/error_code/retryable）；公共面无路径泄漏；
8. 三 schema + 四角色权限模型；迁移 0001–0006 冻结不改；
9. `document_units_v1` 的 15 个 §12.1 scope keys；asset_uri 不落库；
10. MinerU 代理环境剥离（Phase00 验证过的刻意行为）。

## 2. 工作包（按序执行）

### R1 — 迁移 0007_envelope_and_feed_hardening（D1/D2/D3 + 索引 + documents_v1 补列）

新建 `0007_envelope_and_feed_hardening.py`（0001–0006 不动），内容：

```text
1. ops.outbox_event 加列（E1/E2：最终不留 DEFAULT，写侧必须显式传）：
   ADD COLUMN change_kind varchar(16)
   回填：SET change_kind = CASE WHEN payload->>'change_kind' IN ('observed','materialized')
         THEN payload->>'change_kind' ELSE 'materialized' END
   然后 SET NOT NULL + CHECK (change_kind IN ('observed','materialized'))，不加 DEFAULT
   ADD COLUMN subject_kind varchar(32)、subject_ref varchar(64)（事件工厂显式填；
   历史行回填 CASE：asset_id 非空→('document_unit', asset_id)，processing_run_id 非空→
   ('processing_run', ...)，否则 ('document', document_id)）；回填后两列同样 SET NOT NULL，
   subject_kind 加 CHECK ck_outbox_event_subject_kind:
   IN ('document','processing_run','document_unit','source_access')，subject_ref 不加 CHECK
2. core.processing_run 加列：parser_method varchar(16)、parser_language varchar(16)、
   unit_build_status varchar(16) NOT NULL DEFAULT 'not_started'
     CHECK IN ('not_started','running','succeeded','failed')、
   unit_build_error jsonb NULL、unit_build_attempt_count int NOT NULL DEFAULT 0、
   unit_built_at timestamptz NULL（D4/B1：builder 状态与 parse status 解耦）；
   另将 error 列 Text→jsonb：ALTER COLUMN error TYPE jsonb
   USING CASE WHEN error IS NULL OR error='' THEN NULL ELSE error::jsonb END
   （存量失败 run 均为结构化 JSON 字符串；无效 JSON 使迁移大声失败即符合预期；
   models.py 同步 JSONB——第 10 项队列视图与 08 依赖 error->>'retryable'）
3. core.document 加 status CHECK：IN ('registered','parsed','parse_failed','published')
   （先核对存量值；现库只有 'registered'）；
   加 provider_metadata jsonb NOT NULL DEFAULT '{}'（E10：只放稳定、小体积、无敏感信息的
   provider 元数据如 orgId/原始分类；完整 index response 留在 source_access.result_snapshot）；
   core.document_unit 加 quality_status CHECK：IN ('ok','needs_review','unusable')、
   加 query_projection_hash varchar(128) NULL（05-U3 三哈希分层）
4. core.company_identifier 新表（D5 ledger），DDL 定死：
   identifier_id varchar(64) PK、company_id varchar(64) NOT NULL FK→core.company、
   scheme varchar(32) NOT NULL、raw_value text NOT NULL、normalized_value varchar(128) NOT NULL、
   jurisdiction varchar(8) NULL、source_access_id varchar(64) NULL FK→core.source_access、
   status varchar(16) NOT NULL DEFAULT 'active'
     CHECK ck_company_identifier_status IN ('active','retired','contested')、
   valid_from date NULL、valid_to date NULL、observed_at timestamptz NOT NULL、
   created_at timestamptz NOT NULL DEFAULT now()；
   索引：uq_company_identifier_strong_key ON (scheme, normalized_value)
     WHERE scheme IN ('uscc','lei','sec_cik','hk_cr') AND status='active'；
   ix_company_identifier_company ON (company_id)。
   uscc 回填在迁移内用 Python 执行（identifier_id 无法在纯 SQL 造 ULID）：
   SELECT company_id, unified_social_credit_code, created_at FROM core.company
   WHERE unified_social_credit_code IS NOT NULL，逐行 INSERT
   (identifier_id=ids.new_company_identifier_id()（ids.py 新增，前缀 'ci'）,
    scheme='uscc', raw_value=原值, normalized_value=upper(trim(原值)),
    jurisdiction='CN', status='active', observed_at=company.created_at)。
   接口同步：ports/repositories.py 新增 CompanyIdentifierRepository
   (add / get / get_by_scheme_value(scheme, normalized_value) / update)，
   UnitOfWork 属性名 company_identifiers
5. 索引：
   ix_document_company_period_type   ON document (company_id, report_period, filing_type)
   ix_document_announcement_date     ON document (announcement_date)
   ix_document_unit_run_order        ON document_unit (document_id, processing_run_id,
                                                       order_index, asset_id)  ← 06 热路径
   ix_document_unit_content_hash     ON document_unit (content_hash)
   ix_document_unit_heading_path     ON document_unit USING gin (heading_path jsonb_path_ops)
   （GIN 保留：heading_path 过滤是 §3.11 契约义务；v1 API 的 heading 过滤语义定为
    **数组前缀匹配**，jsonb_path_ops 的 containment 只作候选过滤、命中后精确校验前缀
    ——见 06 §3.8；若后续证明 v1 只按序读取可在 0009 撤）
6. document_units_v1 重建（drop + create，追加列、不删不改名）：
   + 'document_unit'::text AS asset_kind
   + u.created_at          AS observed_at
   + CASE WHEN d.filing_type IN ('investor_relations','performance_briefing')
          THEN 'tier_0b' ELSE 'tier_0a' END AS source_tier
     （E3 的 provider guard 在写侧落实：register/07 只接受官方披露 provider 白名单，
      故视图不引入协议外的 tier_unknown 值）
   + 'G0'::text            AS trace_level
   + d.raw_file_hash
   + u.query_projection_hash
7. change_events_v1 重建：投影 e.change_kind / e.subject_kind / e.subject_ref（真实列，
   不用 COALESCE 猜），追加 'disclosure_anchor'::text AS source、
   'change_event.v1'::text AS contract_version
8. documents_v1 重建：追加 'document.v1'::text AS contract_version、company_ref、
   security_ref、source_ref、supersedes_document_id、correction_of_document_id、
   superseded_by_document_id（自联派生，实现定死防行数翻倍：
   LEFT JOIN LATERAL (SELECT x.document_id FROM core.document x
   WHERE x.supersedes_document_id = d.document_id
   ORDER BY x.created_at DESC, x.document_id DESC LIMIT 1) sb ON true，
   投影 sb.document_id）、provider_metadata
9. processing_runs_v1 重建：追加 parser_method、parser_language、unit_build_status、
   unit_build_attempt_count、unit_built_at
10. ops 内部队列视图（S4，非 public 契约，worker/doctor/人工共用同一套 SQL）。
    原则：**视图只暴露事实列，不内嵌阈值**（重试上限/超时阈值是 settings，DDL 读不到；
    由调用方查询时加 WHERE 绑定参数）。五个视图 SQL 定死：

    pending_parse_v1:
      SELECT d.document_id, d.status,
        (SELECT count(*) FROM core.processing_run r
          WHERE r.document_id=d.document_id AND r.status='failed'
            AND r.run_kind='parse') AS failed_parse_count,
        (SELECT (r.error->>'retryable')::boolean FROM core.processing_run r
          WHERE r.document_id=d.document_id AND r.status='failed'
          ORDER BY r.started_at DESC, r.processing_run_id DESC LIMIT 1)
          AS last_failed_retryable
      FROM core.document d
      WHERE d.status IN ('registered','parse_failed')
        AND NOT EXISTS (SELECT 1 FROM core.processing_run r
          WHERE r.document_id=d.document_id AND r.status='running')
    pending_build_v1:
      SELECT r.processing_run_id, r.document_id, r.unit_build_status,
             r.unit_build_attempt_count
      FROM core.processing_run r
      WHERE r.status='succeeded' AND r.unit_build_status IN ('not_started','failed')
    pending_publish_v1:
      SELECT r.processing_run_id, r.document_id
      FROM core.processing_run r
      WHERE r.status='succeeded' AND r.unit_build_status='succeeded'
        AND r.is_active=false
        AND r.started_at > COALESCE((SELECT a.started_at FROM core.processing_run a
          WHERE a.document_id=r.document_id AND a.is_active), '-infinity')
    retryable_failed_run_v1:
      SELECT r.processing_run_id, r.document_id, r.run_kind,
             r.error->>'error_code' AS error_code, r.finished_at
      FROM core.processing_run r
      WHERE r.status='failed' AND (r.error->>'retryable')::boolean
    stale_running_run_v1:
      SELECT r.processing_run_id, r.document_id, r.started_at
      FROM core.processing_run r WHERE r.status='running'
      （started_at 超龄判定由调用方按 DISCLOSURE_STALE_RUN_THRESHOLD_SECONDS 过滤）

    另两个队列视图 ops.sync_due_v1 / ops.pending_download_v1 依赖 07 才引入的
    candidates/checkpoint 语义，**随 08 的 0009_ops_sync_queue_views 迁移交付**
    （07 用 repository 内联查询，不建 ops 视图），不在 0007。
11. 存量 provider 归一（R2.8 前置）：
    UPDATE core.document SET provider='cninfo' WHERE provider='local';
    UPDATE core.source_access SET provider='cninfo' WHERE provider='local'
    （存量本地样本均为 CNINFO 披露 PDF 的手工下载件；只回填不删除）
CHECK 约束命名表（迁移与 models.py 的 CheckConstraint 用同名，downgrade 按名 DROP）：
  ck_document_status / ck_document_unit_quality_status /
  ck_processing_run_unit_build_status / ck_outbox_event_change_kind /
  ck_outbox_event_subject_kind / ck_company_identifier_status
downgrade：逆序还原至 0006 形状（error 列还原 Text，jsonb::text 回写）。授权依赖 0001
default privileges（0006 已验证），权限测试需覆盖重建的全部视图；ops 队列视图只授 app 角色。
```

模型/mapper/实体同步加全部新列（change_kind/subject_kind/subject_ref、unit_build_*、
provider_metadata、query_projection_hash、CompanyIdentifier 实体+仓储）；models.py 加同样 CHECK。
`report_period` 语义修订（B8）：DB 列本就 nullable，保持；必填性按 filing_type 在命令校验层
执行（见 R2.6）。

### R2 — 主体解析服务化 + 注册核心重构（D5/D7/D10；评审项 A1/A6/A7/A10/A11/B18/B19）

1. 新建 `application/services/subject_resolver.py`：实现 D5 解析顺序，公开签名定死
   `SubjectResolver.resolve(uow, candidate: SubjectCandidate) -> ResolvedSubject`
   （SubjectCandidate 含 security_code / exchange / board / legal_name / credit_code）；
   `CompanyRepository` 增加 `get_by_credit_code(uscc)` 与 `update(company)`；
   USCC 回填与冲突抛错；uscc / `uq_security_code_exchange` unique 冲突均映射为
   `domain/errors.py` 新增 `SubjectIdentityConflictError(RegistrationMetadataError)`。
   `register_local_pdf` 改为调用该服务；07 直接复用。
2. 按 D10 把注册核心（去重查询、supersedes 链、source_access、document 写入、outbox 事件）
   抽为 `application/services/register_document.py`，入口签名定死
   `register_document(uow, *, subject: ResolvedSubject, doc_meta: DocumentRegistration,
   raw: RawDocumentWriteResult) -> RegisterDocumentOutcome`；
   `RegisterLocalPdf` 收敛为"归档 + 调核心"的薄入口；
   为 07 的 `RegisterProviderDocument` 预留同一核心。
3. 并发冲突恢复：主体/证券 unique 冲突后新 UoW 重查重试一次（与 document 冲突路径同型）。
4. 隔离（quarantine）路径写 DB 痕迹：新开小 UoW 写
   `SourceAccess(status='failed', error=reason, query_params={provider_document_id, filename})`，
   `source_access_id` 进 `RegisterLocalPdfResult`。
5. 重复导入复用 document 时：写一条 `status='ok'` 的 source_access + `document_observed`
   （observed）事件（D6），本次获取可查询。
6. 命令边界校验：`domain/value_objects` 加 `ReportPeriod.parse`
   （regex `^\d{4}(A|Q[1-4])$`，协议 §2.5 label 形态）与 filing_type 词表校验（D7），
   register 入口 fail fast。**`report_period` 改为 `ReportPeriod | None`（B8）**，
   必填性按 filing_type：annual/semiannual/quarterly_report 必填；performance_forecast/
   performance_flash/performance_briefing 建议填、缺失记 warning 不阻断；其余（investor_relations/
   inquiry_reply/other 等临时公告）可空。public view 的 `report_period` 保留且允许 null。
   **半年报 label 定死为 `YYYYQ2`**（协议 §2.5 词表无 H 形态；A股披露节奏中 Q2 槽位即半年报、
   Q3 槽位即三季报——label 指报告周期槽位，覆盖区间真值属 Time Registry/L2，L1 不表达；
   regex 不扩展，该约定 R8 写入 service-purpose §5）。"建议填缺失记 warning"落点 =
   `logging.getLogger(__name__).warning`，不进 Result 也不进 DB。
7. quarantine 的 `reason` 收敛为枚举（防任意字符串使隔离本身抛 PathSafety 错）。封闭集定死：
   `domain/value_objects/common.py` 新增
   `QuarantineReason = Literal['invalid_raw_document','expected_hash_mismatch','io_error']`；
   `RawDocumentStorePort.quarantine_raw_document` 的 reason 参数改用该类型，
   现有调用点保持 'invalid_raw_document'。
8. **provider 白名单（E3 写侧落地）**：`provider` 必须属于官方披露源词表（首版仅 `cninfo`；
   07 接入新源时升词表），`RegisterLocalPdfCommand.provider` 删除 `'local'` 默认值——本地导入
   是**获取通道**（已由 `source_access.provider_interface='local:register_pdf'` 表达），不是
   披露源；非白名单 provider fail fast。理由：D2 按 filing_type 派生 source_tier 的前提是
   文档确系官方披露，错 tier 比缺 tier 更危险（tier 是 L2 检索/排序因子）。
   实施细节定死：(a) dataclass 字段序——provider 移到 provider_document_id 之后、board 之前
   且无默认值（直接删默认值会触发 non-default-follows-default TypeError）；
   (b) 存量数据由 R1 第 11 项的 UPDATE 归一为 'cninfo'；
   (c) 受影响测试清单：tests/integration/test_register_local_pdf.py 的 `_command()` 增加
   provider='cninfo'，该文件与 tests/integration/test_parse_document.py 共 6 处清理 SQL
   `WHERE provider = 'local'` 改 'cninfo'（tests/unit/test_raw_document_store.py 的
   provider='local' 是路径段用法，不改）。

### R3 — outbox 事件惯例与 parse 事件（D3/D6；A3/A4/B12）

1. 新建 `domain/entities/outbox_events.py` 事件工厂；register/parse 全部改用工厂构造。
   工厂必填 `subject_kind + subject_ref`（E2：document / processing_run / document_unit /
   source_access 四值，精确指事件主体，不做 COALESCE 推断）。
2. mapper 修复：`outbox_event_to_model` 映射 `occurred_at`（现被静默丢弃）、`change_kind`、
   `subject_kind`、`subject_ref`。
3. `parse_document._prepare_run` 事务内写 `processing_run_created`（observed）；
   `_finish_run` 失败分支写 `processing_run_failed`（observed，payload 含结构化错误）。

### R4 — parse 健壮性（B6/B7/B8/B9/B10/A12/A16/A17；D4）

1. settings 增加 `DISCLOSURE_PARSE_TIMEOUT_SECONDS`（默认 1800）与
   `DISCLOSURE_MINERU_BIN`（MinerU 可执行路径，缺省走 PATH，07/08 批量 wiring 必需）。
   注入通道定死（use case 不直接读 settings）：`ParseDocument.__init__` 增加
   `default_timeout_seconds: int = 1800`，execute 内 options.timeout_seconds 为 None 时
   `dataclasses.replace(command.options, timeout_seconds=self._default_timeout_seconds)`，
   由构造方（08 wiring / 测试）从 settings 传入；MinerU 超时 → `TimeoutExpired` → failed run，
   `error_code='parse_timeout'`，retryable=true。
2. parser 版本探测 fail closed：adapter 构造/首用探测一次并缓存；探测失败 → ParserError →
   parse 失败（不再吞成 'unknown' 继续）；删除 IR 顶层 `warnings` 旁路。
3. `_finish_run` 记录 `parser_method` / `parser_language`（R1 列）；失败 run 也要落
   parser 身份（`_prepare_run` 时即从 adapter 元数据落 parser_name/version，失败可归因）。
   元数据通道定死：ports/parser.py 新增
   `@dataclass ParserIdentity(name, version, backend, method, language)` 与
   `DocumentParserPort.identity() -> ParserIdentity`（MinerU adapter 构造/首用探测并缓存，
   探测失败抛 ParserVersionProbeError）；`ParserResult` 增加 `parser_language: str` 字段。
4. parser 异常分型（E4）：adapter 抛 typed 层级
   `ParserTimeoutError / ParserInvocationError / ParserVersionProbeError /
   ParserOutputContractError / ParserUnknownError`；use case 只按类型映射
   stage/error_code/retryable（timeout/invocation → retryable=true，
   version_probe/output_contract → false）；未知异常持久化 failed run
   （retryable=false）后 re-raise——worker（08）在循环层 catch 保证单个坏 PDF
   不打死 loop。
5. 两个 except 块去重：`except Exception` 归一为构造 `_ParseRunFailure` 走同一失败出口。
6. `parser_result.normalized_ir` 禁止就地改写：ParseDocument 先算 artifact relpaths
   再传入 mapper 一次成型（顺带消灭 `artifact_relpath_map` 双份死代码）。
7. `parsed_pages.full_pdf` 如实：ParserOptions 的 start/end 传入 mapper，
   `full_pdf = start_page is None and end_page is None`。
8. D4 落地（B1 解耦语义）：`_finish_run` 同事务更新 `document.status`，但**只在
   `current_processing_run_id IS NULL` 时**（succeeded→'parsed'，failed→'parse_failed'）；
   已 published 的文档保持 published，失败只留 failed run + observed 事件，读侧不降级。
9. `ids.py` docstring 修正（S3）：ULID 是"毫秒级时间有序"，同毫秒内非严格单调——
   不得声称 strict monotonic，排序一律用显式键（created_at+id / order_index+asset_id / seq）。

### R5 — mapper / NormalizedIR v2（D9；B2/B3/B4/B21/B20）

IR 契约直接升 `normalized_ir.v2`（无消费者，schema 文件同步，golden fixtures 全量再生成，
执行按 §6.4 协议）。v2 schema 落盘定死：新建 `contracts/normalized_ir/normalized_ir.v2.json`
（contract_version const 'normalized_ir.v2'），删除 v1 文件；elements required =
['ir_id','kind','raw_kind','order_index','source_item_index']，kind enum 逐字取 D9 七值；
heading_level 可选 {"type":["integer","null"]}；table 元素可选对象
{headers: string[], rows: string[][], merged_cells?: [{row,col,rowspan,colspan}]}，
table_parse_failed 可选 boolean；fixture 文件名改 normalized_ir.v2.json，
tests/contract 两个测试文件的 SCHEMA/FIXTURE 路径与断言键同步改（清单见 §6.4）。

1. **parser 中立元素分类（D9）**：`kind ∈ {text, heading, table, image, equation,
   page_furniture, unknown}`（与 D9 逐字一致），原始 MinerU type 保留在 `raw_kind`；
   MinerU 映射：`text→text`、`header/page_number/footer→page_furniture`（页眉页脚页码在
   IR 层就分类出来，05 的噪声抑制只看中立 kind）、`table→table`、`image→image`、
   `equation→equation`、**其余未映射类型→`unknown`（raw_kind 原样保留，禁止丢弃）**。
   heading 判定谓词定死：`kind='heading'` 当且仅当 raw type=='text' 且 item 含
   'text_level' 且 int(text_level)>=1，此时 `heading_level=int(text_level)`；
   其余 text 元素 kind='text'、heading_level=null（R7 单测按此断言：
   {'type':'text','text_level':1}→heading/1；{'type':'text'}→text/None）。
2. **表结构化**：mapper 内做 rowspan/colspan 感知的 HTML→grid 解析，table 元素增加
   `table: {headers: [...], rows: [[...]], merged_cells?: [...]}`；`table_html` 保留作
   fallback；解析失败置元素级 `table_parse_failed: true`（05 据此打 needs_review）。
   "单位"识别是业务规则，留给 05，不进 mapper。
3. **heading 信号归一**：MinerU `text_level` / `type=header` 归一为元素级
   `heading_level: int|null`（kind=heading 时尽量给出层级）。text_level 覆盖率实测的
   着陆点定死：`scripts/regen_phase00_fixtures.py` 再生成时逐 sample_key 统计并打印三个数
   （kind==heading 元素数 / heading_level 非空数 / 元素总数），人工粘贴进
   `tests/fixtures/phase00/phase00-parser-validation.md` 新增小节
   `## IR v2 heading_level coverage`；覆盖率低不是 R5 验收门槛
   （heading 归一以合成输入单测验收），但 05 的 S2 辅助信号权重要据此校准。
4. **Q&A 载体打通**：依赖 2 的结构化表，投关记录里嵌在 table cell 内的问答文本可被 05
   的 qa 规则处理（切分规则本身在 05，mapper 只保证 cell 文本可取）。
5. 移除生产 IR 里的 `sample_key` / `sample_role` 顶层字段与 ir_id 的 sample 前缀
   （fixture 专用概念，改由 fixture 生成脚本注入）；golden fixtures 同步再生成。
6. 死代码清理：`mapper_to_ir.artifact_relpath_map` 删除；`parser.py` 不再传空
   `parser_artifacts={}`。

### R6 — doctor 与运行时（B11/B16/B17/A8/A18）

1. startup preflight 与深检分离：`create_app` 只做快检（env/路径/DB ping/migration head）；
   全量 raw 重哈希移到 CLI doctor 且默认抽样。参数定死：cli/doctor.py 增加 `--full`（全量）
   与 `--sample N`（默认 20）；抽样 = 按 raw_file_relpath 排序取前 N ∪ 最新 N（确定性，可复跑对比）。
2. API 服务态把 DB 缺配作 FAIL（现在 database_url=None 直接跳过 DB 检查）；CLI doctor 降 WARN。
3. doctor 补 doctor-checklist §2/§5，并按 run 结局区分要求（E11）：
   succeeded run → normalized_ir_relpath 必须存在且 artifact_hash 匹配；
   failed run → 只要求结构化 error 存在，不报 artifact 缺失；
   unit_build_status='succeeded' → document_units_relpath 存在且快照哈希与 DB 聚合一致。
   另补：PG 连通、migration head、三 schema、角色权限、每 document 最多一个 active run、
   outbox seq 单调、stale running run 告警、孤儿 raw/artifact 文件报告（B9 的 orphan 是
   合法状态，报告不报错）；CheckResult 增加 WARN 态。
4. 生产 wiring 收敛为单 engine：`unit_of_work_from_settings` 每调用新建 engine 的形态废弃，
   app/worker 统一"进程级单 engine + `lambda: SqlAlchemyUnitOfWork(engine=engine)`"。

### R7 — 测试补齐（A9/B23）

测试基建与文件布局定死（本仓库是 unittest，非 pytest，`make test` 用 unittest discover）：
新建 `tests/unit/_fakes.py`（内存版 Company/Security/SourceAccess/Document/ProcessingRun/
Outbox/CompanyIdentifier 各仓储 + FakeUnitOfWork，记录 commit/rollback）；用例文件：
`tests/unit/test_subject_resolver.py`（ledger/回填/contested/legal_name）、
`tests/unit/test_register_document.py`（supersedes/竞态/隔离/复用事件/ReportPeriod 必填性）、
`tests/unit/test_parse_document.py`（parse 组全部）、
`tests/unit/test_mapper_to_ir.py`（mapper 组全部）；全部 unittest.TestCase 风格。

单测（fake UoW，不依赖 DB）：

```text
register：supersedes 链（同 pid 不同 hash）/ 竞态恢复 / expected_raw_file_hash 不匹配隔离 /
         identifier ledger 落行+回填+contested 冲突 / legal_name 不一致拒绝 /
         ReportPeriod 按 filing_type 必填性（非 period 公告 report_period=null 可注册）/
         quarantine 落 source_access / 复用路径 observed 事件
parse：  raw_missing 与 raw_hash_mismatch 分支 / 缺 metadata 拒绝 / 二次 parse 独立 run /
         typed exception → error_code/retryable 映射 / 版本探测失败→失败 / 未知异常 re-raise /
         document.status 变迁含 **published 文档重解析失败不降级** /
         事件含 change_kind、occurred_at、subject_kind+subject_ref
mapper： kind 中立分类（header/page_number→page_furniture）/ rowspan/colspan 表结构化 /
         Q&A-in-table cell 可取 / heading_level 归一 / full_pdf 随切页取值 /
         raw_kind 保留（ir_activity fixture 已有素材）
```

集成/契约测（DB-gated）：

```text
document_units_v1 列集合契约测试钉死为 32 列全集（26 既有 + 0007 新增 6）：
{asset_id, document_id, processing_run_id, provider_document_id, payload_kind,
 heading_path, title, order_index, semantic_key, payload, content_hash, structure_hash,
 quality_status, artifact_locator, created_at, contract_version, company_ref, security_ref,
 security_code, exchange, filing_type, report_period, announcement_date,
 producer_action_ref, source_ref, parent_ref,
 asset_kind, observed_at, source_tier, trace_level, raw_file_hash, query_projection_hash}
source_tier 映射契约测试（investor_relations→tier_0b 等）
change_kind 落列后：写侧必填、回填正确、名含 observed 的 materialized 事件反例
documents_v1 superseded_by 派生正确
权限测试覆盖 0007 重建的全部视图
```

### R8 — 文档同步

service-purpose：§5 登记 filing_type 词表、source_tier 映射（D2/D7）与半年报 `YYYYQ2`
label 约定（R2.6）；§12.1 补 0007 新增 6 列（含 query_projection_hash）；
§5.1 document.status 枚举（D4）。fixture 再生成后 contract test 同步。本文件各决策落地后
在 acceptance-matrix 把 A33–A37 置 pass（见 §4，不新增行）。

## 3. 检查点（判据只认命令输出，执行方式见 §6）

1. 0007 迁移往返：按 §6.2 的完整命令序列执行，全部绿判据达成（含种子行回填断言）；
   "权限完好" = `PYTHONPATH=src .venv/bin/python -m unittest
   tests.integration.test_permissions -v` 全部 ok（扩到 0007 全部重建视图）。
2. register：`PYTHONPATH=src .venv/bin/python -m unittest tests.unit.test_subject_resolver
   tests.unit.test_register_document -v` 全 ok；"隔离与复用路径在 DB 可查"落成
   tests/integration/test_register_local_pdf.py 两个新测试方法（不是人工 psql）：
   (a) quarantine 后按 Result.source_access_id 查 disclosure_core.source_access，
       status=='failed' 且 error 非空；
   (b) 重复导入后 disclosure_ops.outbox_event 新增一行 event_kind=='document_observed'
       且 change_kind=='observed'。
3. parse：`PYTHONPATH=src .venv/bin/python -m unittest tests.unit.test_parse_document -v`
   全 ok，其中超时、探测失败 fail-closed、未知异常 re-raise、published 不降级
   各至少一个独立 test method。
4. mapper：断言放 tests/contract（`make test-contract` 承载）：
   (a) ir_activity v2 fixture 中 order_index 最小的 kind=='table' 元素 table.rows 非空，
       且全部 cell 文本拼接含全角 '？'（该表即嵌 Q&A 的活动记录表）；
   (b) annual_report_excerpt v2 fixture 的唯一 table 元素 headers 与 rows 均非空，
       否则必须带 table_parse_failed==true（不允许既无 grid 又无失败标记）；
       annual_report 本地全量 fixture（gitignored）存在时套用同断言 +
       "未标 table_parse_failed 的 table 元素占比 ≥95% 且 headers 与各 row 列数一致"，缺失 skip。
5. 全套测试双绿：按 §6.1 的 no-DB 与 live-DB 两种模式命令与绿判据执行；`git diff --check` 干净。

## 4. Definition of Done

- R1–R8 全部完成并通过 §3 检查点；acceptance-matrix
  （docs/implementation/checks/acceptance-matrix.md）中**已预登记**的 A33–A37 行随对应
  工作包完成把状态 pending→pass（A33↔R1、A34↔R2、A35↔R1/R3、A36↔R4、A37↔R5/D9），
  **不得新增重复行**。核验：`grep -c "| A3[3-7] " docs/implementation/checks/acceptance-matrix.md`
  输出 5 且五行状态列均为 pass。
- 05 的前置依赖（结构化表、heading_level、publish 所需列与索引、事件惯例）全部就绪。

## 5. 明确不做

- 不实现 document_unit builder、publish、`GET /v1/changes`（05）；不做 Filing API（06）；
  不做 CNINFO 同步（07）；不做 worker（08）。
- 不改 0001–0006 迁移；不 rename `created_at` 列；不给 document/unit 加 tier/trace 存储列。
- 不引入新顶层对象；不做 LLM 相关逻辑。
- 不给 core 加 summary/keywords/search_text/embedding 列或 search_projection_hash——
  retrieval/search projection 是 06R 的**派生发现层**（边界定义见 05-U7），过早落 core 是迁移债。

## 6. 执行与核验协议（检查点的唯一执行方式；05–08 复用 §6.1/§6.3）

### 6.1 执行循环

本仓库测试框架是 **unittest**（.venv 无 pytest，禁止引入 pytest 调用），入口是 make 目标；
测试分层与政策见 docs/implementation/checks/fixture-and-test-policy.md。
每个工作包 Rx 完成后，依次执行且全部通过才允许 commit：

```bash
cd /Users/zhang/dev/agent-invest/services/disclosure_anchor
# 1) no-DB 模式（保证无外部依赖环境仍绿）
env -u DATABASE_URL -u DISCLOSURE_MIGRATION_DATABASE_URL make test
# 绿判据：末行 OK (skipped=N)。返工前基线：Ran 82 tests / OK (skipped=26)（已实测）；
# 每新增一个 DB-gated 测试 N+1；出现 FAILED/ERROR 即红。
# 2) live-DB 模式（DSN 见 §6.3；socket 免密码）
export DISCLOSURE_MIGRATION_DATABASE_URL='postgresql+psycopg://disclosure_anchor@/disclosure_anchor?host=/Volumes/AgentSSD/agent_system/postgres/sockets&port=55432'
make test
# 绿判据：末行为不带 skipped 的 OK（基线：Ran 82 tests / OK，已实测）。
# 3) git diff --check   # 输出必须为空
```

追加规则：R1 完成后加跑 §6.2；R5 完成后加跑 §6.4 + `make test-contract`；
R7 期间可用 `make test-unit` / `make test-contract` / `make test-integration` 单层快循环，
但提交门禁只认上面 1)–3)。live-DB 连接失败先 `make pg-status`，
集群未运行则 `make pg-start`（PGDATA/socket/端口已是 Makefile 默认值）。

### 6.2 迁移往返核验（§3 检查点 1）

0007 版本号约定（与 0001–0006 一致，文件名即 revision）：
revision = "0007_envelope_and_feed_hardening"，
down_revision = "0006_v07_terminology_convergence"，路径
`src/disclosure_anchor/adapters/db/postgres/migrations/versions/0007_envelope_and_feed_hardening.py`。

环境准备（六个根路径 env 缺一不可；缺失时 alembic 报误导性的
"RuntimeError: No migration database URL"——真实原因是 fail-closed settings 加载失败，
不要被报错文本带偏。settings 的 env_file=None，任何 .env 都不会自动加载，必须显式 export）：

```bash
cd /Users/zhang/dev/agent-invest/services/disclosure_anchor
export DISCLOSURE_DATA_ROOT=/Volumes/AgentSSD/agent_system/services/disclosure_anchor
export DISCLOSURE_SHARED_ROOT=/Volumes/AgentSSD/agent_system/shared
export DISCLOSURE_RUNTIME_ROOT=/Volumes/AgentSSD/agent_system/services/disclosure_anchor/runtime
export MINERU_MODEL_CACHE=/Volumes/AgentSSD/agent_system/shared/model_cache/mineru
export HF_HOME=/Volumes/AgentSSD/agent_system/shared/model_cache/huggingface
export MODELSCOPE_CACHE=/Volumes/AgentSSD/agent_system/shared/model_cache/modelscope
PSQL=/opt/homebrew/opt/postgresql@18/bin/psql
SOCK=/Volumes/AgentSSD/agent_system/postgres/sockets
```

A. 临时库往返（先于真库执行）：

```bash
$PSQL -h $SOCK -p 55432 -U disclosure_anchor -d postgres -c "DROP DATABASE IF EXISTS disclosure_anchor_migtest;"
$PSQL -h $SOCK -p 55432 -U disclosure_anchor -d postgres -c "CREATE DATABASE disclosure_anchor_migtest OWNER disclosure_owner;"
export DISCLOSURE_ADMIN_DATABASE_URL='postgresql+psycopg://disclosure_anchor@/postgres?host=/Volumes/AgentSSD/agent_system/postgres/sockets&port=55432'
export DISCLOSURE_MIGRATION_DATABASE_URL='postgresql+psycopg://disclosure_anchor@/disclosure_anchor_migtest?host=/Volumes/AgentSSD/agent_system/postgres/sockets&port=55432'
make db-create        # 在临时库建 3 schema + 基础授权（角色已存在则幂等）
PYTHONPATH=src .venv/bin/python -m alembic upgrade 0006_v07_terminology_convergence
# 种子两行历史形态 outbox 行（0006 无 change_kind 列），核验 0007 回填 CASE：
$PSQL -h $SOCK -p 55432 -U disclosure_anchor -d disclosure_anchor_migtest -c \
  "INSERT INTO disclosure_ops.outbox_event (outbox_event_id, event_kind, occurred_at, payload)
   VALUES ('oe_migtest_1','document_observed', now(), '{\"change_kind\":\"observed\"}'),
          ('oe_migtest_2','document_registered', now(), '{}');"
PYTHONPATH=src .venv/bin/python -m alembic upgrade head
$PSQL -h $SOCK -p 55432 -U disclosure_anchor -d disclosure_anchor_migtest -c \
  "SELECT outbox_event_id, change_kind FROM disclosure_ops.outbox_event ORDER BY outbox_event_id;"
# 绿判据：oe_migtest_1→observed，oe_migtest_2→materialized
PYTHONPATH=src .venv/bin/python -m alembic downgrade 0006_v07_terminology_convergence
PYTHONPATH=src .venv/bin/python -m alembic upgrade head
PYTHONPATH=src .venv/bin/python -m alembic current
# 绿判据：0007_envelope_and_feed_hardening (head)
$PSQL -h $SOCK -p 55432 -U disclosure_anchor -d postgres -c "DROP DATABASE disclosure_anchor_migtest;"
```

（若 0006 的 outbox_event 实际列集与上式不符，以 `\d disclosure_ops.outbox_event` 实查为准
调整 INSERT 列——断言目标不变：有 change_kind 线索的行回填 observed，无线索回填 materialized。）

B. 真库 upgrade：把 DISCLOSURE_MIGRATION_DATABASE_URL 的库名换回 disclosure_anchor 后
`make migrate`，再 `alembic current` 断言 0007（真库当前 head 为 0006，已实测）。

C. 权限完好判据：`PYTHONPATH=src .venv/bin/python -m unittest tests.integration.test_permissions -v`
全部 ok（用真库 DSN）。

### 6.3 DB-gated 测试开启

门控实现是 `tests/integration/_support.py` 的 `engine_or_skip()`，只认
`DISCLOSURE_MIGRATION_DATABASE_URL` 或 `DATABASE_URL` 之一；设一个即可
（integration 测试自建临时 Settings，与六个根路径 env 无关）。
禁止用 .env.example 的 TCP DSN（密码是占位符）；本机 socket DSN 免密码：

```bash
export DISCLOSURE_MIGRATION_DATABASE_URL='postgresql+psycopg://disclosure_anchor@/disclosure_anchor?host=/Volumes/AgentSSD/agent_system/postgres/sockets&port=55432'
# 确认 skip 清零（返工前基线 26 个 skip；此命令输出必须为 0）：
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/integration -t . -p 'test_*.py' -v 2>&1 | grep -c "skipped"
```

### 6.4 golden fixture 再生成协议（R5 执行时引用）

顺序固定：(1) 先写 schema——新建 contracts/normalized_ir/normalized_ir.v2.json、删除 v1、
tests/contract/test_normalized_ir_contract.py 的 SCHEMA_PATH 改名；(2) 再改 mapper
（R5.1–R5.4、R5.6）；(3) 跑再生成脚本；(4) `make test-contract` 收口。

再生成脚本**需新建**：`scripts/regen_phase00_fixtures.py`（scripts/ 目录一并新建）。行为规格：
对 4 个 sample_key（annual_report_excerpt / ir_activity / short_announcement / annual_report），
读 `tests/fixtures/phase00/<key>/parser_artifacts_ref.txt` 中 "Content list: " 行的路径
（**特例 annual_report_excerpt**：其 ref 是占位符、盘上无独立产物——输入 = annual_report
的 content_list 过滤 `page_idx <= 6`（即第 1–7 页；窗口按盘上产物实测选定：恰含 1 个
table 元素（第 7 页释义表，40 元素），从而同时满足 §3 检查点 4(b) 的"唯一 table 元素"断言；
第 1–2 页无 table，旧 v1 fixture 的表来自手工裁剪产物，不可复现），
parsed_pages 写 {start:1, end:7, full_pdf:false}；再生成时把该 ref 文件更新为指向
annual_report 产物 + 页范围说明），
经 MinerUArtifactReader.read_content_list + MinerUToNormalizedIRMapper.map_content_list
生成 v2 IR；sample_key=<key>、sample_role 与 document_id 由**脚本注入**（不再由 mapper 注入），
document_id 必须保持既有值 `"phase00_"+<key>`（document_units.v1.jsonl 的 document_id
交叉校验依赖它；**本阶段禁止再生成 units fixture**——unit builder 到 05 才存在）；
parser_info 用 ref 产物的实际环境值（MinerU 3.4.0 / pipeline / auto / ch）；
输出写 `tests/fixtures/phase00/<key>/normalized_ir.v2.json` 并删除同目录 v1 文件。
前置检查：content_list 路径存在；缺失即触发 §7 第 2 条停下来问人，**不得改跑真 MinerU**。

同步更新引用 v1 文件名的位置（均已核实存在）：
tests/contract/test_normalized_ir_contract.py 的 "normalized_ir.v1.json" 字面量（约 16/85/93/102 行）；
tests/contract/test_phase00_fixtures.py 的文件名与 NORMALIZED_IR_REQUIRED_KEYS
（fixture 仍含脚本注入的 sample_key，键集合不因 R5.5 缩减）；
.gitignore 中 tests/fixtures/phase00/annual_report/normalized_ir.v1.json 一行改 .v2
（annual_report 全量 fixture 是 gitignored 本地文件，再生成后仍不入库）。
收口：`make test-contract` OK，且 annual_report 本地 fixture 存在时不 skip。

## 7. 停下来问人（封闭清单，逐条对号；触发即停）

1. §6.1/§6.3 的 socket DSN 连接失败，且 `make pg-status` + `make pg-start` 之后仍失败；
2. /Volumes/AgentSSD 未挂载，或 parser_artifacts_ref.txt 指向的 content_list 文件缺失
   （fixture 再生成无输入）；
3. 任一 §3 检查点在命令无误、重试一次后仍不能复现规格声明的结果
   （例如 0007 downgrade 报错、test_permissions 红）；
4. 完成某 R 项被证明必须修改 §1「明确不要动」清单（含 0001–0006 迁移文件）；
5. 规格引用的列名/函数/路径与代码现实不一致，且本文件与 05–08 均未给出裁决依据；
6. 需要真实 CNINFO 凭据、外网访问或重跑真 MinerU 才能继续（本返工不应出现，出现即理解走偏）。

触发任一条：停止该工作包，报告触发编号 + 已执行的完整命令 + 原样输出；
禁止自行绕过、降级绿判据或把检查点改成"部分通过"。
