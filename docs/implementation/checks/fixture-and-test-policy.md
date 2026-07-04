---
id: disclosure_anchor_fixture_and_test_policy
project: disclosure_anchor
title: 样本与测试策略
status: final-for-implementation
created_at: 2026-06-26
---

# 样本与测试策略

## 1. 样本类型

最小样本集：

```text
annual_report.pdf        年报，验证 heading + long text + table
ir_activity.pdf          投关记录，验证完整 Q&A
short_announcement.pdf   短公告，验证少量事项型 text/table
```

样本用于 fixture 和人工复核，不用于覆盖全市场质量。

## 1.1 真实场景优先原则

测试应尽可能模拟真实使用场景。对会处理披露文件、文件系统归档、解析产物、数据库发布、API
查询或 worker 状态流转的里程碑，完成验证不能只依赖 synthetic/minimal payload；只要本地存在代表性样本，
就必须至少补一条真实样本 smoke/integration/fixture 验证。

执行规则：

- 优先使用本地真实披露样本，例如 `tmp/sample_filings/` 里的 CNINFO PDF，或
  `tests/fixtures/phase00/` 中由真实 PDF 生成的 golden 输出。
- 真实样本验证应尽量保留真实文件大小、文件名、证券代码、provider_document_id、日期、hash、目录结构和
  DB/runtime 路径行为；不得为了测试方便改成只覆盖 toy path 或 toy metadata。
- synthetic 测试仍然需要保留，用于纯函数/unit 逻辑、边界条件、失败注入、权限错误、不可读文件、hash
  mismatch、无 DB 环境 skip 等难以稳定复现的分支。
- 当某个 acceptance item 面向真实业务输入时，synthetic 测试只能证明局部机制，不能单独作为完成证明。
- 如果本地没有合适真实样本，或真实样本验证需要外部网络/凭据/长耗时资源，必须在 `Status.md` 或
  `Plan.md` 的 validation 记录中写明例外原因、替代验证和后续补齐条件。
- 真实样本 smoke/integration 测试应使用临时 data/runtime root、随机 provider_document_id 或可清理测试
  database 记录，避免污染持久样本库和生产式 raw archive。

## 2. Fixture 输出

每个样本至少保存：

```text
mineru_raw 或简化 parser output
normalized_ir.v2.json（04R-R5 起；R5 前为 .v1。再生成协议 = 04R §6.4，
  脚本 scripts/regen_phase00_fixtures.py，document_id 保持 phase00_<key> 不变）
document_units.v1.jsonl（unit builder 到 05 才存在，04R 阶段禁止再生成）
manual_review.md
```

## 3. 测试分层

```text
unit
  ids
  hashing
  PathBuilder
  retention rules
  quality checks
  source_ref builder

integration
  PostgreSQL repositories
  UnitOfWork rollback
  RawDocumentStore
  MinerU adapter with fixture
  document_unit builder

smoke（显式运行，不进 make test 默认发现）
  tests/integration/smoke_real_mineru.py：真 MinerU CLI + 真样本 PDF 的
  register→parse 全链（`make test-mineru-smoke`；三重门控：DB 可达 +
  DISCLOSURE_MINERU_BIN 指向可执行 CLI + 本地样本 PDF 存在，缺一 skip。
  文件名不带 test_ 前缀是刻意的——保持 §6.1 双模式绿判据（live-DB 不带
  skipped 的 OK）不被未开启的冒烟 skip 污染）

contract
  API DTO
  public view columns
  source_ref schema
  change_event schema

e2e
  register local PDF
  parse
  build units
  publish active run
  query units
  get source_ref
```

## 4. Golden sample 规则

如果 parser 升级导致 fixture 变化，必须区分：

```text
raw_file_hash 是否变化
unit content_hash 是否变化
structure_hash 是否变化
quality_status 是否变化
```

只有 unit content_hash 变化，才视为 L2 需要重处理。

## 5. 人工复核报告格式

```text
sample: annual_report.pdf
parser_version:
backend:
result:
  text_units_ok: yes/no
  table_units_ok: yes/no
  qa_units_ok: n/a
issues:
  - ...
action:
  pass / needs_rule_adjustment / parser_unusable
```

## 6. 测试落地（目录与命令）

第 3 节是目标分层，下面是当前仓库的实际落地与入口命令。

```text
tests/unit/          已实现  settings / ids / value_objects / PathBuilder / raw store / MinerU adapter / artifact store / doctor / health / app startup
tests/contract/      已实现  phase00 golden fixture 结构 + NormalizedIR v1 契约（document_unit 字段、content_hash、order_index、IR↔unit 一致性）
tests/sample_corpus/ 已实现  tmp/sample_filings manifest 完整性 + 真实 ID/hash 走 PathBuilder
tests/integration/   已实现  Phase 02 PG schema/权限/views + Phase 03 register_local_pdf / raw-hash doctor + Phase 04 ParseDocument + 0005/0006 public view scope 与术语契约
```

入口命令（Makefile）：

```bash
make test              # 跑全部分层（当前 82 个；无 DB 环境时 26 个 integration skip）
make test-unit         # 仅 tests/unit
make test-contract     # 仅 tests/contract
make test-data         # 仅 tests/sample_corpus
make test-integration  # 仅 tests/integration（需要本地 PG + migration 到 head）
```

约定：

- `make test` 是**唯一权威入口**；VSCode Testing 已配置发现根为 `./tests`（`.vscode/settings.json`），三层都会出现在 Test Explorer。
- 依赖外部资源的测试（`tests/sample_corpus` 需要 `tmp/sample_filings`，`tests/integration` 需要本地 PG/runtime）在资源缺失时必须 **skip**，保证无外部依赖的环境仍全绿。
- `tmp/sample_filings` 与大型 parser artifact 是 git-ignored 的本地 fixture，不入库；只有 `tests/fixtures/phase00/` 的小型 golden 输出入库。

## 7. Phase 02 独立 testing 验证记录

2026-06-29 独立复测覆盖：

```bash
make migrate                         # 连续运行，幂等
.venv/bin/python -m alembic current  # 0002_harden_ops_permissions (当时 head；现已推进至 0006)
make test-integration                # 16 tests, OK
make test                            # DB env: 54 tests, OK
env -u DISCLOSURE_MIGRATION_DATABASE_URL -u DATABASE_URL -u DISCLOSURE_ADMIN_DATABASE_URL make test
                                     # no DB env: 54 tests, OK (skipped=16)
```

SQL 点检结果：

```text
tables=10
views=5
alembic=0002_harden_ops_permissions
leak_cols=0
active_idx=1
app_alembic_priv_count=0
missing_fk_indexes=none
```

Fresh DB 验证：创建随机临时库、按 bootstrap 前置 schema/grant 建库、从零 `alembic upgrade head` 到
`0002_harden_ops_permissions`，确认 5 个 public views、`disclosure_app` 无 `alembic_version` 权限、
FK helper indexes 无缺口，然后删除临时库。

## 8. Phase 03 独立 testing 验证记录

2026-06-29 独立复测覆盖：

```bash
.venv/bin/python -m compileall -q src tests
make migrate
make test-integration                # DB env: 22 tests, OK
make test                            # DB env: 64 tests, OK
env -u DISCLOSURE_MIGRATION_DATABASE_URL -u DATABASE_URL -u DISCLOSURE_ADMIN_DATABASE_URL make test
                                     # no DB env: 64 tests, OK (skipped=22)
make doctor                          # DB env + AgentSSD runtime paths, raw hash check pass
git diff --check
```

Phase 03 新增覆盖：

- raw archive 路径合同和 `FileStorePathBuilder` 唯一路径入口。
- `RawDocumentStore` atomic tmp/fsync/hard-link 发布、verify、重复写复用、非法输入 quarantine。
- `register_local_pdf` 写入 raw file、`source_access`、`document`、`outbox_event`。
- 重复导入同 provider_document_id + hash 复用已有 document。
- 缺失 / 非 PDF 输入进入 `runtime/quarantine` 且不写 document。
- 既有 security/company metadata 冲突在 raw 写入前 fail fast。
- registered raw file 被手工改动后，doctor raw-hash 检查失败。
- 真实 CNINFO PDF smoke test：用 `tmp/sample_filings/002484_江海股份/...1225087169.pdf`
  （约 5.48MB 年报）执行 `register_local_pdf`，验证 raw archive 落盘、重复登记复用、doctor raw-hash
  PASS；测试结束后清理临时 DB 记录和临时 data root。

备注：`make doctor` 当前仍主要覆盖 AgentSSD/runtime/model-cache 基线；Phase 02 的 DB 证明来自
Alembic、SQL 点检和 integration tests，PG doctor 输出项后续单独补齐。

## 9. Phase 04 独立 testing 验证记录

2026-06-29 独立复测覆盖：

```bash
.venv/bin/python -m compileall -q src tests
make test-unit                    # 38 tests, OK
make test-contract                # 6 tests, OK
make test                         # no DB env: 73 tests, OK (skipped=24)
make migrate                      # head=0003_parser_run_metadata（当时；现已推进至 0006）
make test-integration             # DB env: 24 tests, OK
make test                         # DB env: 73 tests, OK
make doctor                       # DB env + AgentSSD runtime paths, pass
git diff --check
```

真实场景验证：

- 使用 `tmp/sample_filings/002484_江海股份/...1225376481.pdf` 真实短公告 PDF（约 102KB）。
- 通过 Phase 03 `register_local_pdf` 登记 raw PDF，再通过 Phase 04 `ParseDocument` 调用实际 MinerU CLI。
- 生成 parser artifacts、`normalized_ir.v1.json` 和 `processing_run.status=succeeded`；NormalizedIR 元素数
  31。
- 测试使用临时 data/runtime root 和临时 DB 记录，结束后清理。

Phase 04 新增覆盖：

- `DocumentParserPort` / `ParserOptions` / `ParserResult` parser-neutral port。
- `MinerUProcess` CLI wrapper、`MinerUArtifactReader`、`MinerUToNormalizedIRMapper`、`MinerUDocumentParser`。
- Phase 04 parser artifact 与 NormalizedIR relpath 合同。
- `ParseDocument` success/failure run 状态，失败时不影响既有 active run。
- `0003_parser_run_metadata` migration 与 public view 不暴露 relpath 的 SQL 点检。
