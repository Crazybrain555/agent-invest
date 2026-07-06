# Phase008 DB Product Review（Consolidated Codex Review）

Review time: 2026-07-06, Asia/Shanghai.

Scope: 这是给 Claude 继续修 Phase008 / 切分 / DB 产品面的唯一审查文件。它整合并取代先前分散的 Round 1、Round 2、Round 3、Round 4 review。视角不是“代码能不能跑”，而是 L2 将来读 `document` / `document_unit` / public views 时，能不能得到语义正确、好查询、可审计的成品数据。

原则：不要从 payload kind、MinerU 行块、实现方便性出发切块；要从披露文件的业务语义出发。底层 text/table/image 可以作为 part 保存，但 L2 面向的 unit 必须表达一个完整的业务事实、事项、章节或证据块。

## P0 Must Fix

### 1. Unit 边界现在过于 IT 化，切得太碎会破坏业务语义

Severity: critical

用户又指出的贵州茅台董事会决议例子说明：当前 builder 太依赖 heading leaf，把一份短公告切成一串很碎的 text unit：

```text
provider_document_id=1225366262
title=贵州茅台：贵州茅台第五届董事会2026年度第一次会议决议公告

unit 1: ["贵州茅台酒股份有限公司第五届董事会 2026 年度第一次会议决议公告"]
unit 2: ["...","一、董事会会议召开情况"]
unit 3: ["...","二、董事会会议审议情况","（一）《关于选举董事长的议案》"]
unit 4: ["...","二、董事会会议审议情况","（二）《关于确定董事会专门委员会设置及组成人选的议案》"]
unit 5-9: 1.董事会战略委员会 / 2.董事会审计委员会 / ...
unit 10-13: （三）到（六）各项议案
unit 14: ["人员简历"]
```

从一般读者和业务使用的角度，这首先是一份“董事会决议公告”，可以大胆作为一个完整 unit；其下的会议召开情况、议案审议情况、人员简历是 parts 或 child blocks。L2 有 LLM 总结能力，短公告切得过碎反而丢上下文：召回到“2.董事会审计委员会”这个孤立 unit 时，它不知道这是哪次会议、哪项议案、决议结果和人员安排的整体语境。

建议增加粒度原则：

- 短公告 / 董事会决议 / 股东会决议 / 制度办法这类几页内 PDF，可以优先产一个 document-level semantic unit，payload 内保留 ordered parts。
- 对同一文档再按业务事项产可选 child units，例如 proposal、appointment、resolution、resume，但 child unit 也要尽量完整。
- 长年报才需要按章节/表格进一步切大块；即使切，也应按“管理层讨论”“财务报告附注某科目”“股东大会某议案”这种业务块，而不是按 MinerU element 或 payload kind。
- 技术层可以保留 text/table/image parts；L2 面的 unit 应该大到足以让一个人读完后回答一个业务问题。

贵州茅台股东会决议例子也非常典型：

```text
provider_document_id=1225366264
heading_path=[
  "贵州茅台酒股份有限公司2025年度股东会决议公告",
  "二、议案审议情况",
  "3.议案名称：《关于聘请2026 年度财务审计机构和内控审计机构的议案》"
]

order 10 text:
  审议结果：通过
  表决情况：

order 11 table:
  股东类型 / 同意 / 反对 / 弃权 ...

order 12 text:
  会议决定，聘请天健会计师事务所...
  4.议案名称：《关于 2025 年年度利润分配方案及 2026 年中期利润分配安排的议案》
  审议结果：通过
  表决情况：

order 13 table:
  第 4 项议案表决情况

order 14 text:
  第 4 项议案的会议决定正文
```

这里至少有两个严重问题：

1. 第 3 项议案的“审议结果 + 表决表格 + 会议决定”被拆成 text/table/text 三个碎片；L2 需要的是一个完整的议案审议 unit。
2. 第 4 项议案的标题已经出现在 order 12 文本里，但 order 12/13/14 的 `heading_path` 和 `title` 仍挂在第 3 项议案下。这不是粒度偏细，而是结构归属错误。

推荐方向：

- 不要让 `payload_kind` 决定 L2 unit 边界。可以保留底层 `part_kind=text|table`，但上层要有完整 semantic unit。
- 对公告/股东会/董事会这类事项型文档，识别事项起点，例如 `\d+.议案名称：...`、`审议结果：`、`表决情况：`、`会议决定，...`。
- 一个议案应成为一个 logical unit，内部按顺序包含 parts：

```json
{
  "payload_kind": "mixed",
  "semantic_type": "shareholder_meeting_proposal",
  "title": "3.议案名称：《关于聘请2026 年度财务审计机构和内控审计机构的议案》",
  "parts": [
    {"kind": "text", "text": "审议结果：通过\n表决情况："},
    {"kind": "table", "headers": [...], "rows": [...]},
    {"kind": "text", "text": "会议决定，聘请天健会计师事务所..."}
  ]
}
```

- 如果暂时不引入 `mixed` payload，至少增加 `logical_unit_id` / `semantic_block_id`，让 text/table 子 unit 能被稳定聚合，且 public view/API 给出聚合读面。

Acceptance:

```sql
-- 同一个议案 heading 下，不应出现下一项议案标题。
SELECT d.provider_document_id, u.order_index, u.heading_path, u.title, u.payload->>'text' AS text
FROM disclosure_core.document_unit u
JOIN disclosure_core.document d USING (document_id)
JOIN disclosure_core.processing_run r USING (processing_run_id)
WHERE r.status IN ('succeeded','published')
  AND u.payload_kind='text'
  AND coalesce(u.payload->>'text','') ~ '\n[0-9]+\.议案名称：'
  AND coalesce(u.title,'') !~ '[0-9]+\.议案名称：';
-- expected: 0

-- 短公告不应被切成一串低上下文叶子 unit。
-- 这不是最终硬阈值，而是必须人工/测试复核的 over-fragmentation audit。
SELECT d.provider_document_id,
       d.title,
       count(*) AS unit_count,
       sum(length(coalesce(u.payload->>'text', ''))) AS text_chars,
       count(*) FILTER (WHERE u.payload_kind='table') AS table_units
FROM disclosure_core.document_unit u
JOIN disclosure_core.document d USING (document_id)
JOIN disclosure_core.processing_run r USING (processing_run_id)
WHERE r.status IN ('succeeded','published')
  AND d.filing_type='other'
GROUP BY d.provider_document_id, d.title
HAVING count(*) >= 10
   AND sum(length(coalesce(u.payload->>'text', ''))) < 8000
ORDER BY unit_count DESC;
-- expected: every row has an explicit product reason, or is rebuilt as larger semantic units
```

### 2. Builder 会把真实第一/第二章误删为封面前言

Severity: critical

实证：

```text
provider_document_id=1225366261
title=贵州茅台：贵州茅台董事、高级管理人员考核和薪酬管理办法
builder_rules_version=ub-2026.07-4
document_units titles:
  第三章 绩效考核
  第四章 薪酬构成
  第五章 薪酬发放
  第六章 薪酬止付追索
  第七章 附则
```

但 normalized IR 明确有：

```text
0 heading: 贵州茅台酒股份有限公司董事、高级管理人员考核和薪酬管理办法（2026 年6 月制定）
1 text: 第一章 总则
2 text: 第一条 ...
9 text: 第二章 管理机构与职责
16 heading: 第三章 绩效考核
```

所以不是 MinerU 漏，是 unit builder 的 `_drop_cover_prelude` 误伤。原因是它只把 `kind='heading'` 的结构标题当作第一结构标题；`第一章/第二章` 被 MinerU 标成 text，于是被丢弃。

Acceptance:

```sql
WITH first_unit AS (
  SELECT d.provider_document_id,
         d.title AS document_title,
         u.title AS first_unit_title,
         row_number() OVER (PARTITION BY u.processing_run_id ORDER BY u.order_index) AS rn
  FROM disclosure_core.document_unit u
  JOIN disclosure_core.document d USING (document_id)
  JOIN disclosure_core.processing_run r USING (processing_run_id)
  WHERE r.status IN ('succeeded','published')
)
SELECT *
FROM first_unit
WHERE rn=1
  AND first_unit_title ~ '^第[三四五六七八九十]+章';
-- expected: 0 unless source IR truly starts there
```

### 3. Marker/checkbox 行不能进入 `heading_path` / `title`

Severity: high

已见过的 active/historical 形态：

```text
heading_path=["第二节 公司简介和主要财务指标","□适用 不适用"]
title="□适用 不适用"

title="是 □否"
```

`□适用 不适用`、`是 □否` 是声明，不是章节标题。它们污染结构索引后，L2 会按错误标题召回。

Acceptance:

```sql
SELECT count(*) AS marker_heading
FROM disclosure_core.document_unit u
JOIN disclosure_core.processing_run r USING (processing_run_id)
WHERE r.status IN ('succeeded','published')
  AND (
    EXISTS (
      SELECT 1
      FROM jsonb_array_elements_text(u.heading_path) hp
      WHERE hp ~ '(适\s*用|不\s*适\s*用|[□☐√☑✓])'
    )
    OR coalesce(u.title,'') ~ '(适\s*用|不\s*适\s*用|[□☐√☑✓])'
  );
-- expected: 0, unless future schema explicitly models boolean titles
```

### 4. Short announcement 不能产 `heading_path=[]` 的孤儿 unit

Severity: high

例子：

```text
provider_document_id=1225366259
title=贵州茅台：贵州茅台关于聘任董事会秘书的公告
order_index=1
heading_path=[]
title=NULL
payload.text="编号：临 2026－026"
```

如果这是元数据，应该进入 `document.provider_metadata` 并从 unit 中移除。如果保留为证据，至少挂在稳定锚点，例如 `["公告头信息"]` 或文档标题下。空 path 对 L2 检索和回放都不好。

Acceptance:

```sql
SELECT count(*) AS empty_heading
FROM disclosure_core.document_unit u
JOIN disclosure_core.processing_run r USING (processing_run_id)
WHERE r.status IN ('succeeded','published')
  AND jsonb_array_length(u.heading_path)=0;
-- expected: 0
```

## P1 Schema / DB Product Design

### 5. `applicability` 不应在 payload；字段类型也建议重新考虑

Severity: high

用户的直觉是对的：把 `applicability` 放 payload 里不适合筛选。当前迁移把它列化为：

```sql
applicability varchar(16) NULL
CHECK (applicability IN ('applicable','not_applicable'))
```

这比 JSONB 好，但如果它只表达 `适用 / 不适用 / 未声明`，nullable boolean 更贴合：

```text
declared_applicable BOOLEAN NULL
TRUE  = 明确勾选“适用”
FALSE = 明确勾选“不适用”
NULL  = 没有该声明 / 未识别 / 该 unit 不涉及该概念
```

不过不要只做一个 boolean 就结束。库里已经有大量 `□是 / 否` 类 disclosure answer，它们不是 applicability，但同属 checkbox declaration。更稳的长期结构是：

```text
document_unit_declaration
  asset_id
  declaration_kind     -- applicability | yes_no | other
  value_bool
  raw_text
  question_text
  source_order
  page_no
```

短期可接受：`declared_applicable BOOLEAN NULL` + `declaration_raw_text TEXT NULL`。

Acceptance:

```sql
SELECT count(*)
FROM disclosure_core.document_unit
WHERE payload ? 'applicability';
-- expected: 0 for rebuilt active/succeeded data
```

### 6. `document.filing_type` 过粗，不能替代巨潮原生分类

Severity: high

本地官方快照已经包含巨潮分类接口，不是 docs 少了：

- `p_info3005`: 公告分类信息，字段 `SORTCODE`, `PARENTCODE`, `SORTNAME`, `F001D`, `F002D`。
- `p_info3015`: 公告基本信息，字段 `F006V` 是信息分类编码串，需要用 `p_info3005` 解释。
- `docs/architecture/cninfo-announcement-categories.json` 当前有 2135 个分类。

当前 `document.filing_type` 只有：

```text
annual_report
semiannual_report
quarterly_report
performance_forecast
performance_flash
investor_relations
performance_briefing
inquiry_reply
other
```

live DB 快照中 54 个 document 有 51 个是 `other`，但它们其实有明确巨潮分类：

```text
012001 -> 调研活动
01010903||010112||012913 -> 审计报告
01010503||010112||011301 -> 权益分派预案及实施
01010503||010112||012330 -> 环境与社会责任报告
```

建议：

- 保留 `filing_type` 作为内部 coarse bucket，用于 report period、tier、粗筛。
- 新增 provider-native category 维表和关联表，不要把 2135 类塞进 `filing_type`：

```text
provider_category(provider, category_code, parent_category_code, category_name, valid_from, valid_to, raw_payload)
document_provider_category(document_id, provider, category_code, ordinal, is_primary)
```

- public 层暴露 `document_categories_v1`，或在 `documents_v1` 暴露 category code/name 数组。
- 修正 `012001 / 调研活动` 至少应映射到 `investor_relations`，否则投资者关系记录会和无关公告一起掉进 `other`。

### 7. Public views 暴露历史 units，L2 容易读到旧 run

Severity: high for DB-direct consumers

早前快照显示 `document_units_v1` / `source_refs_v1` 返回所有历史 rows，而不是 active-only。API 默认 active，但跨服务 DB 读 public views 时，L2 很容易读到旧 payload shape、旧 heading bug、旧 applicability。

建议至少做一个：

- 增加 `document_units_active_v1`。
- 或在 `document_units_v1` 增加 `is_active_run`，并把 L2 查询范式写入契约。
- 或明确 DB-direct consumer 必须 join `processing_runs_v1` 并过滤 active。

### 8. Outbox projection changed_fields 漏 `applicability`

Severity: high

已发现 `query_projection_hash` 包含 applicability，但 publish diff 的字段解释曾只列：

```python
PROJECTION_FIELDS = ("title", "heading_path", "semantic_key", "quality_status")
```

生产 outbox 里出现过大量 `document_unit_projection_changed` 但 `changed_fields=[]` 的事件。对 U5 审计语义来说，这是“hash 变了但不知道什么变了”。

建议：

- `PROJECTION_FIELDS` 包含所有 query projection 字段。
- 加回归：只变 applicability / declaration 字段时，outbox `changed_fields` 必须准确。

## P2 Data Quality / Acceptance Gates

### 9. `quality_status='ok'` 不能覆盖结构异常

Severity: medium-high

已见到 marker heading、empty heading、章号起点异常等行仍是 `quality_status='ok'`。如果 `quality_status` 只表示 OCR/解析质量，就需要另一个结构 QA 字段；如果它表示 L2 可用性，那这些行不能是 ok。

建议：增加 `structure_status` / `needs_review_reason`，或让 build acceptance 直接 fail。

### 10. 表格 rows 仍有空行和分组标签行混入普通数据

Severity: medium-high

早前江海年报 active 表格统计出现：

```text
blank_rows=42
repeat_first_two=60
repeat_first_three=21
```

空行应直接丢弃。重复标签/分组行要么进入 `group_label` / notes，要么显式标记为 group row，不能和普通数值行混在一起。

### 11. 财报附注 heading_path 漂移仍会破坏前缀检索

Severity: medium-high

早前江海年报 `第八节 财务报告` 中低层级标题直接挂到 `heading_path[1]`：

```text
（2） 设定受益计划变动情况
3） 按坏账计提方法分类披露
4) 以摊余成本计量的金融负债
```

这不是“审计报告内部编号重启”那个可接受限制，而是财报附注父路径丢失，会影响 L2 以 `财务报告 > 附注 > 科目` 召回。

Acceptance:

```sql
SELECT heading_path->>1 AS second_heading, count(*) units,
       min(order_index) first_idx, max(order_index) last_idx
FROM disclosure_core.document_unit u
JOIN disclosure_core.processing_run r USING (processing_run_id)
JOIN disclosure_core.document d USING (document_id)
WHERE r.status IN ('succeeded','published')
  AND heading_path->>0='第八节 财务报告'
  AND (
    heading_path->>1 ~ '^[0-9]+[)）]'
    OR heading_path->>1 ~ '^（[0-9]+）'
    OR heading_path->>1 ~ '^（[一二三四五六七八九十]+）'
  )
GROUP BY heading_path->>1
ORDER BY first_idx;
```

### 12. Company/security 测试泄漏需要彻底关掉

Severity: medium-high

多轮审查中见过 zero-doc company/security 泄漏重新出现，特别是 API/admin full-chain 测试路径。即使它不直接污染 active units，也会污染主体维表和后续 L2 聚合。

建议：

- 所有 integration cleanup 先 harvest document 的 `company_id/security_id`，再按依赖删除 `company_identifier`、`security`、`company`。
- 测试结束加 no-leak 断言。
- exchange casing 在写入边界统一或加约束。

### 13. `artifact_locator='null'::jsonb` 建议统一成 SQL NULL

Severity: low-medium

如果语义是缺失，SQL NULL 比 JSON null 更少惊喜。否则 SQL 过滤和 API 输出会出现两种空值。

## Recommended Implementation Order

1. 先修 P0：semantic unit grouping、误删第一/第二章、marker heading、empty heading。
2. 然后重建一小批真实公告，不只年报：股东会决议、治理制度、短公告、年报。
3. 再修 schema：declaration/applicability 设计、CNINFO category 维表/public view。
4. 最后收 public view active contract、outbox changed_fields、table row cleanup、company leak。

## Minimal Acceptance Pack Before Calling It DB-Clean

```sql
-- A. 不应把下一项议案标题留在上一项 heading 下。
SELECT count(*)
FROM disclosure_core.document_unit u
JOIN disclosure_core.processing_run r USING (processing_run_id)
WHERE r.status IN ('succeeded','published')
  AND u.payload_kind='text'
  AND coalesce(u.payload->>'text','') ~ '\n[0-9]+\.议案名称：'
  AND coalesce(u.title,'') !~ '[0-9]+\.议案名称：';

-- B. 不应从第三章才开始，除非 IR/source 真是如此。
WITH first_unit AS (
  SELECT u.processing_run_id, u.title,
         row_number() OVER (PARTITION BY u.processing_run_id ORDER BY u.order_index) rn
  FROM disclosure_core.document_unit u
  JOIN disclosure_core.processing_run r USING (processing_run_id)
  WHERE r.status IN ('succeeded','published')
)
SELECT count(*)
FROM first_unit
WHERE rn=1 AND title ~ '^第[三四五六七八九十]+章';

-- C. active/succeeded unit 不能有空 heading。
SELECT count(*)
FROM disclosure_core.document_unit u
JOIN disclosure_core.processing_run r USING (processing_run_id)
WHERE r.status IN ('succeeded','published')
  AND jsonb_array_length(u.heading_path)=0;

-- D. marker 不应成为 heading/title。
SELECT count(*)
FROM disclosure_core.document_unit u
JOIN disclosure_core.processing_run r USING (processing_run_id)
WHERE r.status IN ('succeeded','published')
  AND (
    EXISTS (
      SELECT 1
      FROM jsonb_array_elements_text(u.heading_path) hp
      WHERE hp ~ '(适\s*用|不\s*适\s*用|[□☐√☑✓])'
    )
    OR coalesce(u.title,'') ~ '(适\s*用|不\s*适\s*用|[□☐√☑✓])'
  );

-- E. payload 不应承载 applicability metadata。
SELECT count(*)
FROM disclosure_core.document_unit
WHERE payload ? 'applicability';
```

All five should be zero for the rebuilt acceptance corpus, or every non-zero row must have a documented product reason and a stronger schema model.

## Progress (2026-07-06, in-flight)

- P0#1 semantic grouping: builder S8 landed — meeting_proposal / document / section mixed
  units (payload_kind='mixed', ordered parts), proposal in-text anchor splitting fixes the
  股东会 misattribution; rules ub-2026.07-5. Corpus rebuild + acceptance rerun pending.
- P0#2 cover prelude: text-kind structural L1 (第一章 as text) now counts as the structural
  start — opening chapters no longer dropped.
- P0#3 markers: yes/no checkbox answers (是 □否) blocked from heading tree and table captions.
- P0#4 empty heading_path: pre-first-heading units anchored under 公告头信息.
- P1#6 categories: migration 0012 — provider_category dim (2135 p_info3005 codes seeded) +
  document_categories_v1 view (facet ordinal, no invented is_primary); filing_type bundle
  2026-07-r3 maps 调研活动→investor_relations (fallback += 012001). Verified on live DB:
  164 segments, 0 unnamed.
- P1#7 active flag: migration 0011 — document_units_v1 / source_refs_v1 expose is_active_run;
  SourceRefV1 contract updated; api/AGENTS.md derived-field note revised.
- P1#8 outbox: PROJECTION_FIELDS += applicability, regression test added.
- **Rebuild + acceptance rerun DONE (2026-07-06, sessions merged)**: full wipe → fresh sync
  (600519 W30 + 002484 W90) → acceptance corpus processed under the final rules
  (ub-2026.07-5 + heading ruleset cn_a_v2). Acceptance pack A–E **all zero**. Eyeballs:
  聘任董秘/薪酬办法/董事会决议 = one document unit each (薪酬办法 parts start at 第一章 —
  P0#2 verified); 股东会决议 = 8 units (会议概况 section + 6 meeting_proposal + 律师/表决
  section); 江海年报 687 (-4) → 205 units (99 mixed / 65 text / 41 table), 研发投入 lives
  under its true parent 四、主营业务分析.
- Heading ruleset cn_a_v2 (follow-up to P2#11 drift, same day): digit-paren levels added to
  HEADING_PATTERNS (（1）/1） = level 5, ① = level 6) — （8） no longer swallows sibling
  1、-level topics, and >depth-4 note numbering stays in unit text instead of drifting into
  heading_path[1].
- s8 refinement: proposal-path documents also section-group their non-proposal remainder
  (mixed never regroups); section-unit applicability only set when merged parts agree;
  numbered-enumeration per-line splitting removed (S3).
- Open: P1#5 declaration model, P2#9 structure QA field, P2#10 table blank/group rows,
  P2#11 residual flat-numbering drift inside 审计报告-style notes, P2#12 leak gate
  assertion, P2#13 locator JSON null.

## Round4 (Codex independent re-audit, 2026-07-06) — resolution

- Verdict was no-go with 4 P1s; verification outcome:
  - P1#1 semantic_key swallowed by grouping — CONFIRMED → fixed in ub-2026.07-6:
    part-level semantic_key + unit `semantic_keys` jsonb column (0013, GIN, view 36 cols,
    projection hash + PROJECTION_FIELDS + contracts).
  - P1#2 note heading drift — CONFIRMED, root cause found: half-width parens ((1)/(一))
    were unmapped so MinerU's flat heading_level=2 evicted the 科目 parent → cn_a_v3
    adds both paren styles at L3/L5.
  - P1#3 local_heading "stringified array" — FALSE POSITIVE: jsonb_typeof shows 'array'
    for all 614 parts; the review SQL used `->>` which renders arrays as text.
  - P1#4 collapsed unit titled 第一章 总则 — CONFIRMED → collapse now uses the registry
    document title (threaded from document.title into the builder).
  - P2#1 table blank rows — blank rows now dropped at grid merge (kept when merged_cells
    reference row indices); group-label rows remain open P2.
  - P2#13 cleared (0 JSON-null locators on regenerated data); Status.md updated.

## Bottom Line

当前最大问题不是 worker loop，而是 DB 成品的语义形状。`document_unit` 不能只是 MinerU 元素加一点 heading 的技术切片；它必须是 L2 能直接使用的披露语义证据。股东会议案这个例子说明：text/table 混合才是一个业务 unit，硬按 payload kind 切会让后续 L2 做大量脆弱拼接。先把 unit 边界从“技术块”升级成“业务语义块”，再谈 schema polish。
