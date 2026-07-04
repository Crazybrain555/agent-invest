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
- **D4 document.status 生命周期枚举定死**：`registered → parsed | parse_failed →（05）published`。
  `parse_document._finish_run` 同事务更新 document.status；加 CHECK 约束。08 的 worker
  队列扫描直接建立在该枚举上。
- **D5 主体解析顺序（协议 §6.5.1 规则 1/4 落地）**：`(exchange, security_code)` 命中 →
  沿 security 取 company；否则 USCC 强键查找（新增 `get_by_credit_code`）；都未命中才新建。
  `legal_name` 只作一致性校验：命中主体后名称不一致 → 抛 `RegistrationMetadataError`
  （弱键不得自动合并）；既有公司缺 USCC 而命令带 USCC → 同事务回填；USCC 冲突（同码不同司
  或同司不同码）→ 抛错，不静默。
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
  归一为 parser 中立枚举 `text / heading / table / image / page_furniture / unknown`
  （`page_furniture` = 页眉/页脚/页码等版面件），原始类型保留在 `raw_kind`。
  没有消费者，直接升 `normalized_ir.v2`，golden fixtures 全量再生成——05 的噪声抑制与
  heading builder 只依赖中立分类，不依赖任何 MinerU 命名。
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
1. ops.outbox_event 加列：
   change_kind varchar(16) NOT NULL DEFAULT 'materialized'
     CHECK (change_kind IN ('observed','materialized'))
   回填：UPDATE ... SET change_kind = payload->>'change_kind'
         WHERE payload->>'change_kind' IN ('observed','materialized')
2. core.processing_run 加列：parser_method varchar(16)、parser_language varchar(16)
3. core.document 加 status CHECK：
   status IN ('registered','parsed','parse_failed','published')
   （先核对存量值；现库只有 'registered'）
   core.document_unit 加 quality_status CHECK：IN ('ok','needs_review','unusable')
4. 索引：
   ix_document_company_period_type   ON document (company_id, report_period, filing_type)
   ix_document_announcement_date     ON document (announcement_date)
   ix_document_unit_document_kind    ON document_unit (document_id, payload_kind)
   ix_document_unit_content_hash     ON document_unit (content_hash)
   ix_document_unit_heading_path     ON document_unit USING gin (heading_path jsonb_path_ops)
5. document_units_v1 重建（drop + create，追加列、不删不改名）：
   + 'document_unit'::text AS asset_kind
   + u.created_at          AS observed_at
   + CASE WHEN d.filing_type IN ('investor_relations','performance_briefing')
          THEN 'tier_0b' ELSE 'tier_0a' END AS source_tier
   + 'G0'::text            AS trace_level
   + d.raw_file_hash
6. change_events_v1 重建：投影 e.change_kind（删 LIKE CASE），追加
   'disclosure_anchor'::text AS source、'change_event.v1'::text AS contract_version、
   COALESCE(e.asset_id, e.processing_run_id, e.document_id) AS subject_ref（协议 §2.8 事件形状）
7. documents_v1 重建：追加 'document.v1'::text AS contract_version、company_ref、
   security_ref、source_ref、supersedes_document_id、correction_of_document_id、
   superseded_by_document_id（自联派生：谁的 supersedes 指向我）
8. processing_runs_v1 重建：追加 parser_method、parser_language
downgrade：逆序还原至 0006 形状。授权依赖 0001 default privileges（0006 已验证），
权限测试需覆盖重建的全部视图。
```

模型/mapper/实体同步加 `change_kind`、`parser_method`、`parser_language`；models.py 的
document.status 与 outbox.change_kind 加同样 CHECK。

### R2 — 主体解析服务化 + 注册核心重构（D5/D7/D10；评审项 A1/A6/A7/A10/A11/B18/B19）

1. 新建 `application/services/subject_resolver.py`：实现 D5 解析顺序，
   `CompanyRepository` 增加 `get_by_credit_code(uscc)` 与 `update(company)`；
   USCC 回填与冲突抛错；uscc / `uq_security_code_exchange` unique 冲突均映射为领域错误。
   `register_local_pdf` 改为调用该服务；07 直接复用。
2. 按 D10 把注册核心（去重查询、supersedes 链、source_access、document 写入、outbox 事件）
   抽为 `register_document` 内部函数/服务，`RegisterLocalPdf` 收敛为"归档 + 调核心"的薄入口；
   为 07 的 `RegisterProviderDocument` 预留同一核心。
3. 并发冲突恢复：主体/证券 unique 冲突后新 UoW 重查重试一次（与 document 冲突路径同型）。
4. 隔离（quarantine）路径写 DB 痕迹：新开小 UoW 写
   `SourceAccess(status='failed', error=reason, query_params={provider_document_id, filename})`，
   `source_access_id` 进 `RegisterLocalPdfResult`。
5. 重复导入复用 document 时：写一条 `status='ok'` 的 source_access + `document_observed`
   （observed）事件（D6），本次获取可查询。
6. 命令边界校验：`domain/value_objects` 加 `ReportPeriod.parse`
   （regex `^\d{4}(A|Q[1-4])$`，协议 §2.5 label 形态）与 filing_type 词表校验（D7），
   register 入口 fail fast。
7. quarantine 的 `reason` 收敛为枚举（防任意字符串使隔离本身抛 PathSafety 错）。

### R3 — outbox 事件惯例与 parse 事件（D3/D6；A3/A4/B12）

1. 新建 `domain/entities/outbox_events.py` 事件工厂；register/parse 全部改用工厂构造。
2. mapper 修复：`outbox_event_to_model` 映射 `occurred_at`（现被静默丢弃）与 `change_kind`。
3. `parse_document._prepare_run` 事务内写 `processing_run_created`（observed）；
   `_finish_run` 失败分支写 `processing_run_failed`（observed，payload 含结构化错误）。

### R4 — parse 健壮性（B6/B7/B8/B9/B10/A12/A16/A17；D4）

1. settings 增加 `DISCLOSURE_PARSE_TIMEOUT_SECONDS`（默认 1800）与
   `DISCLOSURE_MINERU_BIN`（MinerU 可执行路径，缺省走 PATH，07/08 批量 wiring 必需）；`ParseDocument` 在
   options 未显式给值时套用；MinerU 超时 → `TimeoutExpired` → failed run，
   `error_code='parse_timeout'`，retryable=true。
2. parser 版本探测 fail closed：adapter 构造/首用探测一次并缓存；探测失败 → ParserError →
   parse 失败（不再吞成 'unknown' 继续）；删除 IR 顶层 `warnings` 旁路。
3. `_finish_run` 记录 `parser_method` / `parser_language`（R1 列）；失败 run 也要落
   parser 身份（`_prepare_run` 时即从 adapter 元数据落 parser_name/version，失败可归因）。
4. retryable 映射收紧：显式捕 `ParserError`/`OSError`/`TimeoutExpired` 分类映射；
   未知异常持久化 failed run（retryable=false）后 re-raise（不伪装成可重试）。
5. 两个 except 块去重：`except Exception` 归一为构造 `_ParseRunFailure` 走同一失败出口。
6. `parser_result.normalized_ir` 禁止就地改写：ParseDocument 先算 artifact relpaths
   再传入 mapper 一次成型（顺带消灭 `artifact_relpath_map` 双份死代码）。
7. `parsed_pages.full_pdf` 如实：ParserOptions 的 start/end 传入 mapper，
   `full_pdf = start_page is None and end_page is None`。
8. D4 落地：`_finish_run` 同事务更新 `document.status`（succeeded→'parsed'，
   failed→'parse_failed'）。

### R5 — mapper / NormalizedIR v2（D9；B2/B3/B4/B21/B20）

IR 契约直接升 `normalized_ir.v2`（无消费者，schema 文件同步，golden fixtures 全量再生成）：

1. **parser 中立元素分类（D9）**：`kind ∈ {text, heading, table, image, page_furniture,
   unknown}`，原始 MinerU type 保留在 `raw_kind`；MinerU 映射：`text→text`、
   `header/page_number/footer→page_furniture`（页眉页脚页码在 IR 层就分类出来，05 的噪声
   抑制只看中立 kind）、`table→table`、`image→image`、`text 且判定为标题→heading`。
2. **表结构化**：mapper 内做 rowspan/colspan 感知的 HTML→grid 解析，table 元素增加
   `table: {headers: [...], rows: [[...]], merged_cells?: [...]}`；`table_html` 保留作
   fallback；解析失败置元素级 `table_parse_failed: true`（05 据此打 needs_review）。
   "单位"识别是业务规则，留给 05，不进 mapper。
3. **heading 信号归一**：MinerU `text_level` / `type=header` 归一为元素级
   `heading_level: int|null`（kind=heading 时尽量给出层级）；用当前 MinerU 版本对年报样本
   实测 text_level 覆盖率并记录在 PR 描述（05 heading builder 的验收前提）。
4. **Q&A 载体打通**：依赖 2 的结构化表，投关记录里嵌在 table cell 内的问答文本可被 05
   的 qa 规则处理（切分规则本身在 05，mapper 只保证 cell 文本可取）。
5. 移除生产 IR 里的 `sample_key` / `sample_role` 顶层字段与 ir_id 的 sample 前缀
   （fixture 专用概念，改由 fixture 生成脚本注入）；golden fixtures 同步再生成。
6. 死代码清理：`mapper_to_ir.artifact_relpath_map` 删除；`parser.py` 不再传空
   `parser_artifacts={}`。

### R6 — doctor 与运行时（B11/B16/B17/A8/A18）

1. startup preflight 与深检分离：`create_app` 只做快检（env/路径/DB ping/migration head）；
   全量 raw 重哈希移到 CLI doctor 且默认抽样（`--full` 才全量）。
2. API 服务态把 DB 缺配作 FAIL（现在 database_url=None 直接跳过 DB 检查）；CLI doctor 降 WARN。
3. doctor 补 doctor-checklist §2/§5：PG 连通、migration head、三 schema、角色权限、
   normalized_ir_relpath 存在、每 document 最多一个 active run、outbox seq 单调、
   stale running run（status='running' 且 started_at 超阈值）告警、孤儿 raw 文件报告；
   CheckResult 增加 WARN 态。
4. 生产 wiring 收敛为单 engine：`unit_of_work_from_settings` 每调用新建 engine 的形态废弃，
   app/worker 统一"进程级单 engine + `lambda: SqlAlchemyUnitOfWork(engine=engine)`"。

### R7 — 测试补齐（A9/B23）

单测（fake UoW，不依赖 DB）：

```text
register：supersedes 链（同 pid 不同 hash）/ 竞态恢复 / expected_raw_file_hash 不匹配隔离 /
         USCC 落库+回填+冲突 / legal_name 不一致拒绝 / ReportPeriod 与 filing_type 校验 /
         quarantine 落 source_access / 复用路径 observed 事件
parse：  raw_missing 与 raw_hash_mismatch 分支 / 缺 metadata 拒绝 / 二次 parse 独立 run /
         超时→failed(retryable) / 版本探测失败→失败 / 未知异常 re-raise /
         document.status 变迁 / 事件含 change_kind 与 occurred_at
mapper： kind 中立分类（header/page_number→page_furniture）/ rowspan/colspan 表结构化 /
         Q&A-in-table cell 可取 / heading_level 归一 / full_pdf 随切页取值 /
         raw_kind 保留（ir_activity fixture 已有素材）
```

集成/契约测（DB-gated）：

```text
document_units_v1 列集合契约测试钉死：15 scope keys + asset_id + 0007 新增 5 列
source_tier 映射契约测试（investor_relations→tier_0b 等）
change_kind 落列后：写侧必填、回填正确、名含 observed 的 materialized 事件反例
documents_v1 superseded_by 派生正确
权限测试覆盖 0007 重建的全部视图
```

### R8 — 文档同步

service-purpose：§5 登记 filing_type 词表与 source_tier 映射（D2/D7）；§12.1 补 0007 新增列；
§5.1 document.status 枚举（D4）。fixture 再生成后 contract test 同步。本文件各决策落地后
在 acceptance-matrix 勾 A33–A36（见 §4）。

## 3. 检查点

- 0007 在真库与临时库均通过 upgrade/downgrade 往返；权限完好。
- register：USCC 强键命中不再靠 legal_name 合并；隔离与复用路径在 DB 可查。
- parse：超时、探测失败、未知异常三类失败行为符合 R4；document.status 随 run 变迁。
- mapper：ir_activity 样本的 Q&A 表格结构化可取；annual_report 样本表格 headers/rows 完整。
- 全套测试（含新增）no-DB 与 live-DB 双绿；`git diff --check` 干净。

## 4. Definition of Done

- R1–R8 全部完成并通过 §3 检查点；acceptance-matrix 新增行：
  A33 信封最小核视图列 + source_tier 映射（R1）、A34 主体强键解析与回填（R2）、
  A35 change_kind 落列与事件惯例（R1/R3）、A36 parse 超时/归因/status 生命周期（R4）。
- 05 的前置依赖（结构化表、heading_level、publish 所需列与索引、事件惯例）全部就绪。

## 5. 明确不做

- 不实现 document_unit builder、publish、`GET /v1/changes`（05）；不做 Filing API（06）；
  不做 CNINFO 同步（07）；不做 worker（08）。
- 不改 0001–0006 迁移；不 rename `created_at` 列；不给 document/unit 加 tier/trace 存储列。
- 不引入新顶层对象；不做 LLM 相关逻辑。
