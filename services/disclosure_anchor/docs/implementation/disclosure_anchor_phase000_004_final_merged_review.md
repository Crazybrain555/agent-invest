# disclosure_anchor phase000–phase004 最终合并审查意见

审查对象：`disclosure_anchor(2).zip` 及另一份 `disclosure_anchor_phase004_code_review.md` 建议  
结论性质：在前两版审查意见基础上，只吸收确有工程价值的增量，不做不必要大改。

> 【历史记录注（2026-07）】本文为 Phase004 审查时点的意见记录，文中"当前 / 现状"均指 0004 migration
> 之前的代码。A1、A2、A3、A4、A6、A7、A8 已随 migrations 0004–0005 与 Phase004.5 hardening 落地；
> 0006 已完成术语收敛（asset_id / payload_kind / event_kind）。A5、A9 与 PR-5（Phase005
> document_unit builder 与 active-run 发布事务）仍未开始。

---

## 1. 总结论

**phase000–phase004 不需要推翻，也不建议重构主架构。**

继续保留：

- 本地单机独立服务；
- PostgreSQL + 文件系统；
- 模块化单体；
- domain / application / adapters / api / cli 分层；
- ports/adapters 隔离外部 parser 和后续 CNINFO provider；
- raw PDF immutable archive；
- parser artifact + NormalizedIR；
- private schema + public view；
- Phase005 再做 document_unit builder 与 active run 发布。

另一份 AI agent 的建议里，有一些点是有价值的，尤其集中在 **幂等、raw hash 复核、active run 发布事务、JSON Schema 真验证、source_ref 契约补字段**。这些应吸收到 Phase004.5 / Phase005 gate 里。

但另一份建议也有部分内容偏“过度具体化”或“后续阶段事项”，不建议全部照搬。最终应保持小修、硬化、可验收，不把 phase005 拖成大重构。

---

## 2. 本次确认新增吸收的建议

### A1. 给 `document` 增加数据库级幂等约束

**采纳，优先级 P1。**

当前代码只有普通索引：

```text
ix_document_provider_ref(provider, provider_document_id)
ix_document_raw_hash(raw_file_hash)
```

`register_local_pdf` 是先查后插。单机手动跑暂时够用，但后续 CNINFO 同步、worker-loop 或两个 agent 同时跑时，仍可能发生并发重复 insert。

建议新增 migration：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_provider_doc_hash
ON disclosure_core.document (provider, provider_document_id, raw_file_hash)
WHERE provider IS NOT NULL
  AND provider_document_id IS NOT NULL
  AND raw_file_hash IS NOT NULL;
```

实现要求：

1. migration 前先检查是否已有重复数据；如有，先人工或脚本合并。
2. repository/use case insert 时捕获 `IntegrityError`。
3. 捕获后按同 key 再查一次，返回既有 document。
4. 增加 integration test：同一 `provider + provider_document_id + raw_file_hash` 重复注册，最终只有一条 document。

不建议把唯一键设成 `provider + provider_document_id`，因为后续同一 provider_document_id 如果文件 hash 变化，应允许作为新版本、修订版或异常分支保留。

---

### A2. Parse 前重新校验 raw PDF hash

**采纳，优先级 P1。**

当前 `RawDocumentStore` 已有 `verify_raw_document()`，doctor 也能做抽样校验，但 `ParseDocument._prepare_run()` 没有在真正 parser 前重新校验 raw 文件。也就是说，如果 raw 文件被手工覆盖、磁盘损坏、路径错配，parser 仍可能解析错误文件。

建议：

1. 给 `ParseDocument` 注入一个 `raw_store` 或 `raw_verifier` port。
2. 在调用 parser 之前执行：

```text
verify_raw_document(
  relpath=document.raw_file_relpath,
  expected_hash=document.raw_file_hash,
)
```

3. 如果校验失败：

```text
processing_run.status = failed
error_code = raw_hash_mismatch / raw_missing
不要调用 parser
不要写 normalized_ir
不要改 current active run
```

4. 增加测试：注册 PDF 后手工篡改 raw 文件，再 parse，必须 failed，且错误明确是 hash mismatch。

这是 L1 披露锚服务的底线。parser 失败只是可重试，raw 错配会污染证据链。

---

### A3. NormalizedIR / document_units snapshot 继续使用 ArtifactStore 原子写；parser artifact 可加完成标记

**部分采纳。**

我原建议已经包含：`normalized_ir` 不能 `path.write_bytes(raw)`，需要 `tmp + fsync + os.replace + fsync(parent_dir)`。

另一份建议补充了 parser artifact `_MANIFEST.json`，这个方向合理，但不要把它升级成重型 artifact registry。建议合并成轻量规则：

1. 新增 `ArtifactStore`，支持：

```text
write_json_atomic(relpath, payload) -> sha256
write_jsonl_atomic(relpath, rows) -> sha256
write_text_atomic(relpath, text) -> sha256
```

2. Phase004 用它写 `normalized_ir.v1.json`。
3. Phase005 用它写 `document_units.v1.jsonl`。
4. parser artifact 目录可以新增轻量 `_MANIFEST.json`：

```json
{
  "status": "complete",
  "parser_name": "MinerU",
  "parser_version": "...",
  "content_list_relpath": "...",
  "markdown_relpath": "...",
  "created_at": "..."
}
```

但 Phase005 builder 的可信入口仍应是：

```text
processing_run.status = succeeded
artifact_hash 对得上
normalized_ir JSON 可解析且 schema valid
```

不要让 builder 直接扫 parser artifact 目录来决定处理状态。

---

### A4. `source_refs_v1` 补 `service` / `contract_version`

**采纳，优先级 P1。**

当前 public view 里 `source_refs_v1` 已经有核心字段，但缺少明确的服务名和契约版本。建议在下一版 migration 中补：

```sql
CREATE OR REPLACE VIEW disclosure_public.source_refs_v1 AS
SELECT
    'disclosure_anchor'::text AS service,
    'source_ref.v1'::text AS contract_version,
    ...
```

同时增加 contract test：

```text
source_refs_v1 必须包含 service / contract_version
source_ref API DTO 字段与 public view 字段一致
artifact_locator 不得包含绝对路径
raw_file_relpath / parser_artifact_relpath / normalized_ir_relpath 不得进入 public source_ref
```

这点对 L2/L3 很重要。source_ref 是后续证据引用的“名片”，最好从一开始就版本化。

---

### A5. Phase005 的 active run 发布必须是事务边界

**采纳为 Phase005 gate。**

当前 `processing_run` 已有 partial unique index，保证同一 document 最多一个 `is_active=true`。但 `document.current_processing_run_id` 只是普通字符串，不是强 FK。注释里也说明是为了避免和 `processing_run.document_id` 形成循环。

建议不要马上强上 composite FK。第一阶段先做应用层强约束 + doctor 检查即可。

Phase005 发布 active run 应封装成单独 use case，例如：

```text
PublishProcessingRun
```

事务顺序：

```text
1. SELECT document FOR UPDATE
2. 确认 new_run.document_id == document.document_id
3. 确认 new_run.status == succeeded
4. 旧 active run: is_active = false
5. 新 run: is_active = true
6. document.current_processing_run_id = new_run_id
7. 写 outbox_event: processing_run_published
8. commit
```

验收：

```text
同一 document 永远最多一个 active run
document.current_processing_run_id 必须指向这个 active run
publish 失败不能留下半发布状态
重复 publish 同一 run 应幂等
```

composite FK 可以作为后续增强，不作为 Phase005 开始前的硬要求。

---

### A6. Contract test 要真正跑 JSON Schema validation

**采纳，优先级 P1。**

当前 `contracts/normalized_ir/normalized_ir.v1.json` 存在，但 contract test 主要检查 required keys，没有真正用 JSON Schema validator 验完整 fixture 和 mapper 输出。

建议增加轻量依赖：

```toml
[project.optional-dependencies]
dev = ["jsonschema>=4", "ruff>=0.6"]
```

或者分成：

```toml
test = ["jsonschema>=4"]
dev = ["ruff>=0.6"]
```

测试覆盖：

```text
contracts/normalized_ir/normalized_ir.v1.json 自身可解析
phase00 golden normalized_ir 全量通过 schema
MinerUToNormalizedIRMapper synthetic 输出通过 schema
ParseDocument 写出的 normalized_ir 通过 schema
```

同时 schema 要逐步收紧：

```text
created_at: date-time
parser_artifacts 至少包含 artifact_root_relpath / content_list_relpath
parser.backend / parser.method 使用枚举或半枚举
所有 artifact_locator / parser_artifacts 路径必须是 relative path
禁止 /Volumes、/Users、C:\ 等机器绝对路径
```

这和我之前的 NormalizedIR hardening 建议一致，但这次补上了具体测试方式。

---

### A7. `MinerUDocumentParser` 的 version probe 不应让成功 parse 变 failed

**采纳，优先级 P2。**

当前代码在 MinerU CLI parse 成功后，还会调用：

```python
parser_version = self._parser_version or self._process.version()
```

如果 `mineru -v` 临时失败，理论上会让一次已经成功生成 artifact 的 parse 被标记 failed。批量解析时也会多一次 CLI 调用。

建议：

1. `MinerUDocumentParser` 内做 lazy cache：

```python
self._version_cache: str | None = None
```

2. version probe 失败时，不覆盖 parse 成功结果；记录：

```text
parser_version = "unknown"
warning = "version_probe_failed"
```

3. 如果 `_MANIFEST.json` 实现了，可把 warning 写进去。

这不是架构问题，但属于低成本稳定性修补。

---

### A8. `RawDocumentStore` 的 hard-link 要检查同 filesystem，或改成 final-dir tmp

**采纳为 P2，不挡 Phase005 主线。**

当前 raw 写入流程是：

```text
runtime/tmp 写临时文件
os.link(tmp_path, final_path)
```

如果 `DISCLOSURE_RUNTIME_ROOT` 和 `DISCLOSURE_DATA_ROOT` 不在同一 filesystem，`os.link` 会失败。现在 `.env.example` 默认把 runtime 放 data root 下，一般没问题，但 settings 没有强约束。

建议两种实现二选一：

**最小修：** doctor 检查 `runtime/tmp` 与 `data/raw_documents` 的 `st_dev` 是否一致，不一致 fail closed。

**更稳实现：** raw tmp 文件直接写在 `final_path.parent` 下，然后同目录 `os.replace` / hard-link 发布。

考虑当前项目阶段，先做 doctor 检查即可；等后续 storage 抽象稳定后，再考虑把 raw tmp 改到 final parent。

---

### A9. Phase005 builder 不能做 IR item 一对一落库

**采纳为 Phase005 设计原则，但不强制照搬目录名。**

Phase005 是决定 L2 是否可用的关键阶段。不要把 MinerU content_list 或 NormalizedIR elements 简单一条一条落成 `document_unit`。应做一层稳定的文档单元构建。

推荐处理流水线：

```text
NormalizedIR
→ schema validation
→ 过滤页码/header/footer/空文本等确定性噪音
→ heading tree builder
→ table wrapper
→ QA boundary detector
→ text/table/qa unit builder
→ retention rules
→ quality checks
→ content_hash / structure_hash / aggregate hash
→ write document_unit rows
→ write document_units.v1.jsonl snapshot
→ publish active run
→ outbox events
```

`content_hash` 不应包含这些易变字段：

```text
created_at
processing_run_id
parser_artifact_relpath
page_idx
bbox
source_item_index
```

`content_hash` 建议基于：

```text
payload_kind
heading_path
title
canonical payload text/table/qa 内容
```

`quality_status` 建议不要放进 content_hash，而是单独触发：

```text
quality_status_changed
```

change_event 建议至少区分（下列均为 `event_kind` 层面的分类，不与 `change_kind` 竞争；
它们全部映射为 `change_kind=materialized`，`observed`/`materialized` 二分见 service-purpose §12.2）：

```text
content_changed        # L2 必须重处理
structure_changed      # L2 可能需要重排引用
quality_status_changed # L2 需要看是否降级/恢复
```

表格 payload 第一版不要假装全结构化。如果 MinerU 稳定产出的是 `table_html`，就先接受下面这种
**过渡形态**（目标契约仍是 service-purpose §6.3 的 unit/headers/rows 结构表，此形态应标记
`needs_review` 类质量状态，不是长期表契约）：

```json
{
  "table_html": "...",
  "caption": [],
  "footnote": [],
  "raw_text": "...",
  "structure_status": "html_only"
}
```

不要为了“看起来结构化”强行拆 table_cell。这个结论和我们之前“不建 table_cell 核心表”的判断一致。

---

## 3. 已覆盖或不需要重复加入的建议

另一份建议中有些内容是对的，但我们之前已经覆盖，或者只需要作为原则保留，不需要新增任务。

| 建议 | 判断 |
|---|---|
| 不引入 Redis / Celery / Kafka / Airflow / 向量库 / ES | 已覆盖，继续坚持 |
| MinerU 不泄露到 domain/application | 已覆盖，继续坚持 |
| document_unit 不是普通 RAG chunk | 已覆盖，继续坚持 |
| 不把标准三表从 PDF 重建一套 | 已覆盖，继续坚持 |
| zip 不包含 `.env/.venv/.git/tmp/__pycache__` | 已覆盖，仍是 P0 |
| README / AGENTS / CLAUDE 更新 | 已覆盖；README 也可一起修 |
| ruff | 可以加，但不作为阻塞项；jsonschema 比 ruff 更优先 |
| Phase006 query service / source_ref builder | 方向对，但放 Phase006，不挤进 Phase004.5 |
| Phase007 CNINFO source_access 记录查空、凭据不进日志 | 方向对，但放 Phase007 gate |

---

## 4. 不建议立即采纳或需要降级的建议

### R1. 不要现在强上 composite FK

`document.current_processing_run_id` 没有 FK 是问题，但当前注释也说明了是为了避免循环依赖。马上加 composite FK 会增加 migration 和 ORM 复杂度。

建议顺序：

1. Phase005 先用 publish use case + row lock + doctor 检查保证一致性。
2. 如果后续 active run 发布逻辑稳定，再考虑 composite FK。

### R2. parser artifact `_MANIFEST.json` 不作为 Phase005 builder 的唯一可信入口

manifest 有用，但不能代替 DB run status 和 artifact hash。builder 读取必须以 `processing_run.status=succeeded`、`normalized_ir hash valid`、`schema valid` 为准。

### R3. 不照搬具体目录结构

另一份建议列了很多模块路径，例如 `domain/services/heading_tree.py`、`retention_rules.py` 等。方向合理，但不建议让开发人员机械照搬。可以按职责拆，但保持当前项目命名风格和最少文件数。

### R4. CNINFO stream 写入接口暂不提前做

`put_raw_document(input_file: Path)` 对 Phase004/005 足够。Phase007 下载 PDF 时，可以先下载到 runtime tmp，再调用现有 raw store。`put_raw_document_stream` 可后置。

---

## 5. 最终建议开发顺序

### PR-0：交付包与凭据清理

保持前版建议：

- 删除/不再打包 `.env/.venv/.git/tmp/__pycache__/.DS_Store/__MACOSX`；
- 轮换已进入 zip 的 CNINFO credential；
- 用 `git archive` 生成源码包；
- Makefile 不要因为存在坏 `.venv/bin/python` 就失败；
- `.env.template` 只保留 disclosure_anchor 需要的变量。

### PR-1：clean checkout fixture + contract 真验证

合并前版和本次新增：

- 新增可提交的小型 `annual_report_excerpt` fixture；
- full annual report 大 artifact 改为 local optional；
- contract test 不依赖 ignored artifact；
- 引入 `jsonschema`，对 NormalizedIR fixture 和 mapper 输出做真 schema validation；
- schema 禁止绝对路径进入 parser_artifacts / artifact_locator。

### PR-2：存储与 parse 硬化

- 新增 `ArtifactStore`；
- `normalized_ir.v1.json` 原子写；
- Phase005 `document_units.v1.jsonl` 也走原子写；
- `ParseDocument` parser 前校验 raw hash；
- `ProcessingRun.error` 逐步结构化，至少包含 `stage/error_code/retryable/message`；
- MinerU version probe 缓存且非阻断。

### PR-3：DB 幂等与 public contract migration

- 新增 `uq_document_provider_doc_hash` partial unique index；
- register use case 捕获 `IntegrityError` 后幂等返回；
- `source_refs_v1` 增加 `service` / `contract_version`；
- 增加对应 integration / contract tests。

### PR-4：路径布局与 raw archive 运行时检查

- 统一 `document_units_snapshot_relpath()` 到 provider/security/provider_doc_id/run 布局；
- doctor 检查 runtime tmp 与 raw final root 是否同 filesystem；
- 后续可考虑 raw tmp 改到 final parent。

### PR-5：Phase005 document_unit builder

- NormalizedIR schema validation；
- heading tree；
- deterministic noise filter；
- text/table/qa unit builder；
- stable content_hash / structure_hash；
- 不把 bbox/page_idx/artifact path 放入 content_hash；
- table 第一版允许 html_only；
- publish active run 事务；
- outbox event 分类为 content/structure/quality。

### PR-6：文档与轻量静态检查

- 更新 README、AGENTS.md、CLAUDE.md；
- 清理 placeholder docstring；
- 增加 `make lint`，但不要让 lint 阻塞 Phase005 主功能；
- 保留 mypy/pyright 后置。

---

## 6. 最终判断

另一份建议虽然整体表达不如前版克制，但里面有几个具体工程点值得吸收：

1. document 数据库级幂等；
2. parse 前 raw hash 校验；
3. source_ref 补 service / contract_version；
4. active run 发布事务；
5. JSON Schema 真验证；
6. MinerU version probe 非阻断；
7. hard-link 同 filesystem 检查；
8. Phase005 content_hash / change_event / table payload 规则。

这些点建议加入最终执行清单。

但不建议扩大成“大修”。当前最优路线仍然是：**phase004.5 做小型 hardening，然后进入 phase005 document_unit builder。**
