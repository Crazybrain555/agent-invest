# disclosure_anchor 架构与实施规格复审合并版 v2

> 版本：2026-07-04  
> 范围：Phase00–04 已实现代码、migration 0001–0006、04R 返工规格、05–08 后续规格、`service-purpose.md` 与顶层协议 v0.7 对齐性。  
> 测试口径修正：我在上传 zip 内运行现有测试，结果为 `48 passed, 34 skipped`。按你的说明，`skipped` 主要来自未随 zip 上传的大型样本/外部依赖样本，不视为测试失败；因此报告中所有判断均不应被理解为“测试没通过”。本报告仍是架构与实施规格审核，不是 PostgreSQL / MinerU / CNINFO 端到端验收。

---

## 0. 总体结论

04R 的主方向是正确的，不需要推翻三 schema、public view、transactional outbox、content-addressed raw archive、`document_unit`、NormalizedIR、Filing API 这些核心设计。

另一份 AI 建议大体可信，且补充了几个我上一版没有充分展开的工程边界：`report_period` 可空、builder 状态持久化、live/snapshot pagination 语义、QA 中文规则、heading ruleset 版本化、跨页表格重复表头、事件 `subject_kind + subject_ref`。这些建议应采纳。

但最终版不应只停留在那份建议的 5 个阻断项。为了让 L2 后续真正可增量消费、可撤销、可重放，建议把下面 9 项作为进入实现前的阻断清单：

```text
B1  document.status 与 run/build/worker 状态解耦
B2  document_unit diff 改为 multiset + old/new asset_id
B3  content_hash / structure_hash / query_projection_hash 分层
B4  NormalizedIR taxonomy 扩展，image/chart/list/footnote 不静默丢弃
B5  typed identifier ledger，禁止 legal_name 自动 merge
B6  pending_download candidates 持久化，checkpoint 不越界
B7  provider_document_id precheck 加 file_signature / overlap re-download
B8  report_period nullable by filing_type
B9  FS artifact 与 DB transaction 非原子边界写清
```

修完这些后，04R → 05 → 06 → 07 → 08 可以继续推进。

---

## 1. 与另一份 AI 建议的差异对照

### 1.1 一致并应采纳的点

| 锚点 | 一致判断 |
|---|---|
| `04R §0 D4` / `05 §U1` / `08 §1` | `document.status` 不能同时代表 public availability、parse run 状态、worker queue 状态。重解析失败不得让已有 active run 从 public API 消失。 |
| `05 §U2/U3/U5` | `content_hash` diff 不能按 set 做，必须支持重复 hash / multiset；title 不应轻易进 content hash。 |
| `07 §2/§3` / `08 §1` | `pending_download` 不能只在内存里；index candidates 必须持久化，checkpoint 只能在候选持久化后推进。 |
| `RegisterLocalPdfCommand` / `04R §R2.6` | `report_period` 不能对所有公告强制必填；临时公告、问询函、重大合同、回购、担保等可以没有标准 period。 |
| `05 §S8` | 文件系统 artifact 与 PostgreSQL transaction 不能声明为一个真正原子事务；只能定义写入顺序、orphan 策略和 doctor 检查。 |
| `08 §2` | advisory lock 必须写清 session / transaction 生命周期，避免连接池泄漏；文档级锁不要使用不透明的 `hashtext(document_id)` 作为长期规格。 |
| `06 §3` | API 分页要区分 live keyset 与 snapshot pagination；没有 snapshot/version 就不能承诺“插入期间无遗漏”。 |
| `05 §S4/S5/S6` | QA、heading、table、红线保留规则需要更细的中文公告 golden tests。 |
| `04R §D5/R2` | 主体身份不能靠 legal_name 自动 merge；需要 typed identifier ledger。 |
| `07 §3.2` | provider 原始 category / orgId / metadata 必须有落点。 |
| `05 §S1` | image 不能无条件丢弃；带 caption/OCR/text anchor 的图片、图表、股权结构图、组织结构图必须可见或 needs_review。 |

### 1.2 另一份建议补充、我本次采纳的点

1. `unit_build_status` 必须持久化。builder 失败不能只靠 `document.status='parsed'` 无限重试。建议在 `processing_run` 上加：`unit_build_status`、`unit_build_error`、`unit_build_attempt_count`、`unit_built_at`。
2. `report_period` nullable 是阻断项。否则 CNINFO 接入大量临时公告时会被错误建模。
3. 分页语义要明确 live vs snapshot。我上一版只写了稳定排序键；这次增加“不引入 snapshot_seq 时，不承诺翻页过程中看到新插入数据”。
4. QA 识别规则要覆盖中文投关/问询函格式，例如 `问题1：`、`问：`、`Q1：`、`投资者提问：`、`答：`、`公司回复：`。
5. heading ruleset 应按 market/language 版本化，例如 `cn_a_v1`、`hk_zh_en_v1`、`us_10k_v1`。
6. 跨页表格合并要去重复表头，保留 page span / merge_reason，避免把重复 header 当成数据行。
7. `subject_ref` 不能用 `COALESCE(asset_id, processing_run_id, document_id)` 隐式决定，应有 `subject_kind + subject_ref` 或按 `event_kind` CASE。
8. worker 要 catch use-case exception，一个坏 PDF 不能打死整个 loop。

### 1.3 我上一版更强、仍建议保留的点

1. `STALE_BUT_AVAILABLE` / `LATEST_PROCESSING_FAILED` 读侧语义：已有 active run 时，最新重解析失败不能返回 409；应返回旧 active 数据并附 warning。
2. `query_projection_hash`：`semantic_key`、`quality_status`、`title`、`heading_path` 不是 content，但它们是 public query projection。规则升级导致 projection 变化时，必须能触发 event。
3. removed / projection_changed 事件必须携带 old/new asset_id。L2 持有的是旧 `asset_id`，只给 `content_hash` 不足以撤销。
4. NormalizedIR taxonomy 要比 04R-D9 当前枚举更宽：`list / chart / equation / code / footnote` 不能全部塞进 `unknown` 或被 builder 丢弃。
5. CNINFO precheck 不能只看 provider_document_id：同 ID、同 URL 也可能发生文件替换；需要 provider file signature 或 overlap re-download + raw hash 复核。
6. `ops.outbox_event.change_kind` 不应长期保留 DEFAULT `'materialized'`：迁移 backfill 可用 default，但最终应 drop default，写侧必须显式传。
7. parser exception hierarchy：timeout / invocation / version probe / output contract / unknown 要 typed，不能都包成普通 ParserError。
8. doctor 要区分 succeeded run 和 failed run 的 artifact 要求：failed run 没有 normalized_ir 不应报一致性错误。
9. `source_tier` 应是 `f(provider/source_channel, filing_type)`：不能单纯 `other -> tier_0a`，否则手工上传/网页/非官方投关来源会被误标硬披露。
10. ULID docstring 不要声称 strict monotonic-sortable：当前 ID 生成若是 timestamp + random，同毫秒内不是严格单调。

---

# 2. 阻断项：先改规格，再进入实现

## B1. `document.status` 必须从 run/build/worker 状态中解耦

**锚点：** `04R §0 D4`、`04R R4`、`05 §U1`、`06 §3`、`08 §1`  
**严重度：阻断**

当前规格把 `document.status` 定义为：

```text
registered → parsed | parse_failed → published
```

并要求 parse `_finish_run` 同事务更新 `document.status`。这对首次解析成立，但对重解析不成立。

问题场景：

```text
1. document A 已发布，current_processing_run_id = run_1
2. parser 升级后生成 run_2
3. run_2 解析失败
4. 如果 document.status 被改成 parse_failed，Filing API 可能返回 L1_PROCESSING_REQUIRED
5. 但 run_1 的 active units 仍然可用，不应从 public 契约里消失
```

建议把 `document.status` 定义为 **public availability state**：

```text
registered   = 无 usable active run，且尚未成功 parse
parsed       = 无 usable active run，已有成功 parse run，等待首次 build/publish
parse_failed = 无 usable active run，最新 parse 失败
published    = 存在 active published run
archived     = 后续如需人工停用再引入
```

建议在 `processing_run` 增加 builder 状态：

```text
processing_run.unit_build_status:
  not_started | running | succeeded | failed

processing_run.unit_build_error jsonb null
processing_run.unit_build_attempt_count int not null default 0
processing_run.unit_built_at timestamptz null
```

建议在 `document` 增加或明确：

```text
document.current_processing_run_id = 当前 active/published run
document.latest_processing_run_id  = 最新尝试 run，可成功也可失败，可落列或由 run 表计算
```

状态转换规则：

```text
parse success:
  if document.current_processing_run_id is null:
      document.status = parsed
  else:
      document.status 保持 published，不降级

parse failed:
  if document.current_processing_run_id is null:
      document.status = parse_failed
  else:
      document.status 保持 published，只记录 failed run + observed event

build success:
  processing_run.unit_build_status = succeeded

build failed:
  processing_run.unit_build_status = failed
  processing_run.unit_build_error = structured error

publish success:
  document.status = published
  document.current_processing_run_id = new_run
  old active run is_active=false, new run is_active=true
```

API 同步：

```text
请求 units 且无 active run / 无 public units → 409 L1_PROCESSING_REQUIRED
有 active run，但 latest run failed → 200 OK + warning LATEST_PROCESSING_FAILED
```

Worker queue 同步：

```text
pending_parse:
  document.status in ('registered','parse_failed')
  and no running run
  and retry policy allows

pending_build:
  processing_run.status='succeeded'
  and processing_run.unit_build_status in ('not_started','failed')
  and retry policy allows

pending_publish:
  processing_run.status='succeeded'
  and processing_run.unit_build_status='succeeded'
  and processing_run.is_active=false
```

---

## B2. `document_unit` diff 必须从 set diff 改为 multiset / instance diff

**锚点：** `05 §U2/U3/U5`  
**严重度：阻断**

当前 U5 逻辑是：

```text
content_hash 不在旧 active 中 → created
content_hash 不在新 active 中 → removed
same content_hash but quality_status changed → quality_status_changed
```

问题：公告里会有重复内容。set diff 看不出 multiplicity。

建议以 multiset 为第一层：

```python
old_counter = Counter((payload_kind, content_hash))
new_counter = Counter((payload_kind, content_hash))

created_count = max(0, new_counter[key] - old_counter[key])
removed_count = max(0, old_counter[key] - new_counter[key])
```

以 stable pairing 为第二层：

```text
key = payload_kind + content_hash
对同 key 的 old units 按 order_index, asset_id 排序
对同 key 的 new units 按 order_index, asset_id 排序
逐个配对
未配对 old → removed
未配对 new → created
配对但 query_projection_hash 不同 → projection_changed
配对但 quality_status 不同 → quality_status_changed 或 projection_changed 子类
```

`document_unit_removed` 必须能让 L2 撤销旧引用：

```json
{
  "old_asset_id": "du_old_...",
  "old_processing_run_id": "run_old_...",
  "content_hash": "sha256:...",
  "payload_kind": "text",
  "old_order_index": 42,
  "old_heading_path": ["第三节", "风险因素"]
}
```

`document_unit_created`：

```json
{
  "new_asset_id": "du_new_...",
  "new_processing_run_id": "run_new_...",
  "content_hash": "sha256:...",
  "payload_kind": "table",
  "new_order_index": 43,
  "new_heading_path": ["第三节", "风险因素"]
}
```

`document_unit_projection_changed`：

```json
{
  "old_asset_id": "du_old_...",
  "new_asset_id": "du_new_...",
  "content_hash": "sha256:...",
  "old_query_projection_hash": "sha256:...",
  "new_query_projection_hash": "sha256:...",
  "changed_fields": ["semantic_key", "quality_status"]
}
```

---

## B3. `content_hash` / `structure_hash` / `query_projection_hash` 边界必须定死

**锚点：** `05 §U2/U3/U5`、`service-purpose §13.2`  
**严重度：阻断**

当前 05 定义：

```text
content_hash = sha256(canonical_json({payload_kind, title, payload}))
```

建议改成：

```text
content_hash = sha256(canonical_json({
  payload_kind,
  payload
}))

structure_hash = sha256(canonical_json({
  heading_path,
  order_index,
  artifact_locator.section_or_page_span
}))

query_projection_hash = sha256(canonical_json({
  payload_kind,
  title,
  heading_path,
  semantic_key,
  quality_status
}))
```

事件语义：

```text
content_hash changed / multiset count changed
  → document_unit_created / document_unit_removed，materialized

query_projection_hash changed but content_hash same
  → document_unit_projection_changed，默认 materialized

structure_hash changed only
  → document_unit_structure_changed 或 processing_run_restructured
  → 默认 observed，除非 API 明确承诺结构查询稳定性
```

---

## B4. NormalizedIR v2 原则正确，但 taxonomy 太窄；image/chart/footnote/list 不能静默丢弃

**锚点：** `04R §0 D9`、`04R R5`、`05 §S1`  
**严重度：阻断**

D9 的原则正确：

```text
parser-neutral kind + raw_kind
```

但当前枚举太窄：

```text
text / heading / table / image / page_furniture / unknown
```

建议 IR v2 canonical kind：

```text
text
heading
list
table
image
chart
equation
code
footnote
page_furniture
unknown
```

Builder 丢弃规则：

```text
page_furniture:
  header/footer/page_number 且无红线关键词 → 可丢弃

image/chart:
  无 caption、无 OCR text、无 nearby heading、无明显面积/图表信号 → 可丢弃或只留 artifact
  有 caption/OCR/nearby heading/chart signal → 生成 document_unit，quality_status='needs_review'

footnote:
  与 table 相邻或含单位/追溯调整/会计政策/风险提示 → 必须保留

list:
  作为 list payload 或 text payload 保留，不得 unknown 后静默丢弃

equation/code:
  v1 可以 quality_status='needs_review'，但不可无痕消失
```

---

## B5. 主体身份必须落 typed identifier ledger；禁止 legal_name 自动合并

**锚点：** `04R §0 D5`、`04R R2`、`service-purpose §6.5.1`、`RegisterLocalPdf` 当前代码  
**严重度：阻断**

现有代码有：

```python
company = uow.companies.get_by_legal_name(command.company_legal_name)
```

这属于弱键自动 merge。建议 0007/0008 加最小表：

```text
core.company_identifier
- identifier_id
- company_id
- scheme              -- uscc / lei / sec_cik / hk_cr / isin / exchange_ticker / cninfo_org_id / provider_org_id / other
- raw_value
- normalized_value
- jurisdiction
- source_access_id
- confidence
- status              -- active / retired / contested
- valid_from
- valid_to
- observed_at
- created_at
```

强唯一建议：

```sql
UNIQUE (scheme, normalized_value)
WHERE scheme IN ('uscc','lei','sec_cik','hk_cr')
  AND status = 'active'
```

SubjectResolver 顺序：

```text
1. 强键命中：lei / uscc / sec_cik / hk_cr
   - 唯一命中 → 自动关联
   - 冲突 → quarantine / contested，不自动 merge

2. 官方 security listing 命中：exchange + ticker + valid date
   - 通过 security.issuer_company_id 传导
   - 若没有 issuer relation，只能作为候选

3. provider org id：如 cninfo_org_id
   - 仅在 provider namespace 内稳定
   - 不能等同 legal identity，除非本地 snapshot 明确映射

4. name + jurisdiction
   - 只出候选，不自动 merge
```

红筹/VIE 边界必须写进规格：

```text
Cayman listed issuer ≠ PRC USCC operating entity
HK listed issuer ≠ 境内运营子公司
ADR CIK issuer ≠ 所有并表经营实体
```

---

## B6. CNINFO `pending_download` 必须可恢复；checkpoint 不能越过未持久化候选

**锚点：** `07 §2/§3.5`、`08 §1`  
**严重度：阻断**

当前 07/08 组合存在 crash 丢公告风险：

```text
1. index sync 找到 100 个公告
2. 候选进入内存 pending_download
3. checkpoint 前进
4. 进程在下载前 crash
5. 重启后如果 overlap window 覆盖不到，就永久漏公告
```

推荐方案：复用 `source_access.result_snapshot`：

```text
SyncDisclosureIndex:
  - 每次 index response 写 source_access
  - result_snapshot.candidates[] 保存标准化候选
  - pending_download 从 result_snapshot 中“尚未注册 document”的 candidates 派生
  - checkpoint 只能推进到 source_access 持久化成功后的 window upper bound
```

candidate 示例：

```json
{
  "provider": "cninfo",
  "provider_document_id": "...",
  "provider_org_id": "...",
  "security_code": "000001",
  "exchange": "SZSE",
  "title": "...",
  "announcement_date": "2026-06-30",
  "download_url": "...",
  "original_category": "...",
  "file_signature_hint": {
    "file_size": null,
    "etag": null,
    "last_modified": null,
    "index_updated_at": "..."
  }
}
```

---

## B7. CNINFO precheck 不能只看 `provider_document_id`

**锚点：** `07 §3`  
**严重度：阻断**

当前思路是：

```text
(provider, provider_document_id) 已存在，且 index 没有 new-version signal → skip
```

风险：公告系统可能同 ID、同 URL 或近似 URL 替换 PDF。你的服务已经承认：

```text
same provider_document_id + different raw_file_hash = supersedes / new version
```

但如果下载前就 skip，就无法发现不同 raw hash。

建议 skip 条件：

```text
skip only if:
  provider_document_id already exists
  AND provider_file_signature unchanged
  AND candidate is outside overlap verification window
  AND no correction/replacement signal
```

`provider_file_signature` 可包括：

```text
download_url
file_size
etag
last_modified
index_updated_at
raw_file_hash after re-download
```

如果 provider 不提供可靠 signature，则最近 N 天 overlap window 内必须重新下载并 hash。

---

## B8. `report_period` 必须 nullable，并按 filing_type 校验

**锚点：** `RegisterLocalPdfCommand`、`04R R2.6`、`service-purpose §5.1`  
**严重度：阻断**

当前 `RegisterLocalPdfCommand.report_period: str` 必填，04R-R2.6 又要求正则：

```text
^\d{4}(A|Q[1-4])$
```

这只适用于财报/季报等 period filing，不适用于大量临时公告。

建议类型：

```python
report_period: ReportPeriod | None
```

可以增加：

```python
event_date: date | None
```

校验规则：

```text
annual_report / semiannual_report / quarterly_report:
  report_period required

earnings_preannouncement / performance_briefing:
  report_period recommended；缺失时 warning，不阻断

inquiry_reply / investor_relations / other / material_contract / guarantee / litigation / repurchase:
  report_period optional
```

public view 的 15 个 scope keys 保留 `report_period`，但必须允许 null。

---

## B9. 文件系统 artifact 与 DB transaction 必须写清非原子边界

**锚点：** `05 §S8`、`05 §4`  
**严重度：阻断**

PostgreSQL transaction 可以 rollback，文件系统不能随 DB rollback。`write_jsonl_atomic` 只能保证单文件 rename 原子，不能保证 DB+FS 分布式原子性。

推荐顺序：

```text
1. build units in memory
2. write snapshot to temp path
3. fsync temp file / directory if feasible
4. atomic rename to final artifact path
5. verify artifact_hash / size
6. begin DB transaction
7. insert document_unit rows
8. update processing_run.document_units_relpath / content_hash_aggregate / structure_hash / build status
9. update document.current_processing_run_id / document.status on publish
10. insert outbox events
11. commit
```

失败策略：

```text
FS write/rename failed:
  不写 document_unit；run.unit_build_status=failed；error_code=ARTIFACT_WRITE_FAILED

DB transaction failed after FS success:
  DB rollback；artifact 可能成为 orphan；允许存在；doctor deep-check 可报告/清理

DB commit success but artifact missing:
  严重一致性错误；doctor fail
```

---

# 3. 应改项

## E1. `ops.outbox_event.change_kind` 不应长期 DEFAULT `materialized`

迁移可以临时 default 方便 backfill，但最终应去掉 default。否则新事件忘记显式传 `change_kind` 时，会被误认为 materialized。

建议：

```sql
ALTER TABLE ops.outbox_event ADD COLUMN change_kind text;
UPDATE ops.outbox_event
SET change_kind = CASE
  WHEN payload->>'change_kind' IN ('observed','materialized') THEN payload->>'change_kind'
  ELSE 'materialized'
END;
ALTER TABLE ops.outbox_event ALTER COLUMN change_kind SET NOT NULL;
ALTER TABLE ops.outbox_event ADD CONSTRAINT ck_outbox_change_kind
  CHECK (change_kind IN ('observed','materialized'));
-- 不保留 DEFAULT
```

---

## E2. outbox event 要显式 `subject_kind + subject_ref`

不要用：

```sql
COALESCE(e.asset_id, e.processing_run_id, e.document_id) AS subject_ref
```

建议事件工厂生成：

```text
subject_kind: document | processing_run | document_unit | source_access
subject_ref: exact id
```

如果不愿新增列，public view 至少用 `event_kind` CASE。

---

## E3. `source_tier` 要加 provider/channel guard

当前：

```text
investor_relations / performance_briefing → tier_0b
all others → tier_0a
```

建议改为：

```text
provider in official_disclosure_providers
AND source_channel='official_disclosure'
AND filing_type not in ('investor_relations','performance_briefing')
  → tier_0a

provider in official_disclosure_providers
AND filing_type in ('investor_relations','performance_briefing')
  → tier_0b

manual_upload / unknown_web / broker_forwarded / non-official page
  → tier_unknown 或 pending_tier，不自动 tier_0a
```

---

## E4. Parser adapter 要有 typed exception hierarchy

建议：

```python
ParserTimeoutError
ParserInvocationError
ParserVersionProbeError
ParserOutputContractError
ParserUnknownError
```

use case 只基于 typed exception 写：

```text
run.status
error_code
retryable
stage
```

未知异常策略：先 `_finish_run(status='failed')`，再由 worker catch，loop 继续。

---

## E5. Filing API pagination 要定义稳定排序与 live/snapshot 语义

稳定排序：

```text
documents_v1:
  ORDER BY announcement_date DESC NULLS LAST, document_id DESC

/document/{id}/units:
  WHERE processing_run_id = active_run
  ORDER BY order_index ASC, asset_id ASC

change_events_v1:
  ORDER BY seq ASC, event_id ASC
```

v1 建议采用 live keyset，承诺：

```text
同一 query + cursor 方向下不重复
不承诺翻页过程中看见 cursor 前新插入的数据
新增/变更通过 change feed 补齐
```

---

## E6. Advisory lock 要改 transaction-level 或 dedicated connection；文档级 lock 用稳定 hash

worker singleton 二选一：

```text
方案 A：dedicated DB connection
方案 B：每轮 pg_try_advisory_xact_lock，transaction 结束自动释放
```

本地单机初期建议方案 B。

文档级 lock 不建议写：

```sql
pg_try_advisory_xact_lock(hashtext(document_id))
```

建议：

```text
pg_try_advisory_xact_lock(namespace_int, stable_hash_int)
```

---

## E7. QA 识别规则太窄，要覆盖中文公告格式

建议规则：

```text
question_start:
  ^\s*(问题|问|Q|Q\d+|投资者提问|提问)\s*\d*[：:]

answer_start:
  ^\s*(答|回复|公司回复|A|A\d+)\s*[：:]
```

表格 cell 内也要应用同样规则。

---

## E8. heading tree 要利用 `text_level`，并按 market/language 版本化

建议：

```text
heading_rules_version = cn_a_v1 | hk_zh_en_v1 | us_10k_v1
```

`cn_a_v1` 覆盖：

```text
第X章 / 第X节
一、二、三、
（一）（二）（三）
1. / 1、 / 1.1 / 1.1.1
①②③
重要提示 / 释义 / 目录 / 附录
```

MinerU `text_level` 有值时作为强信号，但不单独决定。

---

## E9. table builder 要保留 caption、footnote、merged cell、multi-row header，并处理跨页重复表头

验收规则：

```text
caption 不得丢
单位说明、脚注、注释不得作为 page_furniture 丢弃
merged cell 保留 row_span / col_span
多行表头保留 header_rows
空单元格、"-"、"—"、"不适用" 区分
跨页合并时保留 page_span / artifact_locator
下一页重复表头应识别并删除，不得当成数据行
merge_reason = continued_table
```

---

## E10. `provider_metadata` 与 `source_access.result_snapshot` 分工要写清

建议 `core.document` 加：

```text
provider_metadata jsonb not null default '{}'
```

只放稳定、体积小、无敏感信息的 provider 元数据。完整 index response / candidate list 放：

```text
source_access.result_snapshot
```

---

## E11. Doctor 检查要区分成功 run 与失败 run

建议：

```text
processing_run.status in ('succeeded','published'):
  normalized_ir_relpath must exist
  artifact_hash must match

processing_run.status='failed':
  normalized_ir_relpath optional
  error_code/error_message/retryable must exist

processing_run.unit_build_status='succeeded':
  document_units_relpath must exist
  snapshot hash must match DB aggregate
```

---

## E12. `/v1/units/{asset_id}` 默认只返回 active run unit

建议：

```text
GET /v1/units/{asset_id}
  默认只返回 active processing_run 下的 unit

GET /v1/units/{asset_id}?include_inactive=true
  可返回历史 run unit，并在 response 中显式 inactive=true
```

---

# 4. 建议项

## S1. `semantic_key` 规则要版本化，并守住 L1 边界

建议字段：

```text
semantic_key_rule_version
semantic_key_rule_id
```

允许披露结构/载体级 key，例如：

```text
important_notice
risk_notice
financial_statement_table
audit_opinion
management_discussion
profit_forecast_summary
receivable_aging
inventory_breakdown
```

禁止 L1 出现投资语义 key：

```text
bullish_signal
capacity_expansion_positive
earnings_quality_warning
```

---

## S2. `heading_path` GIN `jsonb_path_ops` 索引先不要定死为长期策略

先定义查询语义，再定索引：

```text
exact path?
prefix path?
contains heading text?
按 heading_path 排序?
```

如果 v1 只是 `/documents/{id}/units` 顺序读取，则先用：

```text
(document_id, processing_run_id, order_index)
```

`heading_path` GIN 可以后置。

---

## S3. ULID 文档不要声称严格单调

如果当前 ULID 是 timestamp + random，同一毫秒内不是 strict monotonic。不要把 ID 当排序依据。

排序必须显式：

```text
created_at + id
order_index + asset_id
seq + event_id
```

---

## S4. 不引入 Celery/Redis/Kafka，但建议把派生队列固化为 ops views

当前“不引入重队列”正确。建议增加：

```text
ops.pending_download_v1
ops.pending_parse_v1
ops.pending_build_v1
ops.pending_publish_v1
ops.retryable_failed_run_v1
ops.stale_running_run_v1
```

worker、doctor、人工排查共用同一套 SQL 判断，减少状态机漂移。

---

## S5. Golden tests 要覆盖红线保留与不过度保留

重点加：

```text
published doc reparse failed 不影响 active run
same content_hash duplicate diff
non-period announcement report_period=null
crash after index before download
same provider_document_id with different raw_file_hash
artifact orphan doctor
advisory lock under connection pool
重要提示含退市风险必须保留
页眉重复“重要提示”不得每页生成高质量 unit
表格脚注含“追溯调整”必须保留
跨页表格重复表头去重
Q&A in table cell
```

---

# 5. 建议不要动的设计

| 锚点 | 判断 |
|---|---|
| `04R §0 D1` | 信封最小核通过 public view 派生，不加存储列。正确。 |
| `asset_uri` 决策 | 只在 API/MCP 序列化层派生，不落库、不进 public view。正确。 |
| `0001–0006 冻结，0007+ 新增迁移` | 正确。保持 migration discipline。 |
| raw archive content-addressed | 正确。raw PDF 是 L1 最高价值资产之一。 |
| transactional outbox | 正确。只需修 `change_kind` 默认值和事件 payload，不要换 Kafka。 |
| 不新增 `chunk/table_cell/page_bbox/event_unit` 顶层对象 | 正确。table cell/bbox/parser block 应留在 payload/artifact，不应固化为 public identity。 |
| `processing_run` 作为 L1 action log specialization | 正确。内部叫 processing_run，public 通过 producer_action_ref 抽象即可。 |
| Filing API 是 public view 的薄封装 | 正确。API 不应绕过 public view 回读 core 私表。 |
| 不引入 Celery/Redis/Airflow | 正确。本地单机阶段 PostgreSQL 状态表 + outbox + advisory/row lock 足够。 |
| `SubjectResolver` + `register_document` core 抽象 | 正确。CNINFO、本地上传、未来交易所接入都应复用。 |

---

# 6. 0007/0008 迁移建议草案

## 6.1 `ops.outbox_event`

```text
change_kind text not null check in ('observed','materialized')
subject_kind text null
subject_ref text null
producer_action_ref text null 或 processing_run_id/source_access_id 保持可投影
```

迁移完成后 drop `change_kind` default。

## 6.2 `core.document`

```text
status check in ('registered','parsed','parse_failed','published','archived')
current_processing_run_id nullable
latest_processing_run_id nullable
provider_metadata jsonb not null default '{}'
report_period nullable
```

## 6.3 `core.processing_run`

```text
parser_method text null
parser_language text null
unit_build_status text not null default 'not_started'
unit_build_error jsonb null
unit_build_attempt_count int not null default 0
unit_built_at timestamptz null
query_projection_hash_aggregate text null
```

`status` 仍表示 parse run 生命周期：

```text
running / succeeded / failed
```

不要把 build/publish 混进 `processing_run.status`。

## 6.4 `core.document_unit`

```text
content_hash text not null
structure_hash text null
query_projection_hash text null
quality_status check in ('ok','needs_review','unusable')
heading_path jsonb not null default '[]'
```

索引优先：

```text
(document_id, processing_run_id, order_index, asset_id)
(content_hash)
```

## 6.5 `core.company_identifier`

```text
identifier_id text primary key
company_id text not null references core.company(company_id)
scheme text not null
raw_value text not null
normalized_value text not null
jurisdiction text null
source_access_id text null
confidence numeric null
status text not null default 'active'
valid_from date null
valid_to date null
observed_at timestamptz not null
created_at timestamptz not null
```

强唯一 partial index：

```sql
CREATE UNIQUE INDEX ux_company_identifier_strong_active
ON core.company_identifier (scheme, normalized_value)
WHERE scheme IN ('uscc','lei','sec_cik','hk_cr')
  AND status = 'active';
```

---

# 7. 修改顺序

## 第一步：改 04R

```text
D4: document.status 改为 public availability state
D5/R2: typed identifier ledger；legal_name 只出候选
D9/R5: NormalizedIR taxonomy 扩展；image/chart/list/footnote 不得静默丢弃
R1: change_kind drop default；subject_kind/subject_ref；provider_metadata；build status 字段
R4: parser typed exception；重解析不降级 published
R6: doctor 区分 success/failed run artifacts
R7: 增加重解析失败、非 period 公告、duplicate content_hash 测试
```

## 第二步：改 05

```text
U2/U3: content_hash / structure_hash / query_projection_hash 定义
U5: multiset diff + old/new asset_id payload
S1: image/chart/list/footnote 保留策略
S2: heading ruleset version + text_level
S4: QA 中文规则
S5: table footnote/merged cell/repeated header
S8: FS/DB 非原子事务与 orphan 策略
```

## 第三步：改 06

```text
L1_PROCESSING_REQUIRED 只在无 active run 时返回
有 active run + latest failed → 200 + warning
pagination 定义 stable order/cursor
明确 live keyset，不承诺插入期间无遗漏
/v1/units/{asset_id} 默认 active only
```

## 第四步：改 07

```text
source_access.result_snapshot 保存 candidates
checkpoint 只在 candidate 持久化后推进
provider_file_signature / overlap re-download 规则
provider_metadata 落 document，完整 response 落 source_access
report_period nullable by filing_type
```

## 第五步：改 08

```text
pending_download 从 persisted candidates 派生
pending_build/publish 从 processing_run.unit_build_status 派生
advisory lock 生命周期改 transaction-level 或 dedicated connection
document lock 改 stable hash
worker catch use-case exception 后继续 loop
ops pending views
```

---

# 8. 最终判断

请把测试口径与架构建议分开看：上传包内可运行测试是通过的；`skipped` 不等于失败，也不削弱现有 Phase00–04 的已完成度。

我的建议针对的是 04R/05–08 接下来实现前应修正的契约边界，尤其是持续运行、重解析、增量消费、主体身份、非 period 公告和文件/DB 一致性问题。这些现在修成本低，等 L2 消费后再修会变成兼容性债。
