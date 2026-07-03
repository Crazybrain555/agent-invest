---
id: disclosure_anchor_milestone_04_mineru-adapter-and-normalized-ir
project: disclosure_anchor
title: MinerU adapter 与 NormalizedIR
status: complete
created_at: 2026-06-26
implemented_at: 2026-06-29
verified_at: 2026-06-29
---

# Milestone 04: MinerU adapter 与 NormalizedIR

> 实施状态（2026-06-29）：已实现并完成本地验证。新增 parser-neutral
> `DocumentParserPort`/`ParserResult`/`ParserOptions`，`MinerUProcess` CLI wrapper，
> `MinerUArtifactReader`，`MinerUToNormalizedIRMapper`，`MinerUDocumentParser`，以及
> `ParseDocument` use case。`processing_run` 通过 `0003_parser_run_metadata` 增加
> `parser_backend` / `input_raw_file_hash` / `parser_artifact_relpath`，同时保持 public view 不暴露
> relpath。真实短公告 PDF 已通过实际 MinerU smoke：登记 raw PDF、调用 MinerU、生成 parser artifacts 和
> `normalized_ir.v1.json`，31 个元素，run 状态 `succeeded`。A14-A15 pass。

## 1. 目标

把已登记 document 的 raw PDF 解析为 parser_artifacts，并将 MinerU 输出映射成 parser-neutral `NormalizedIR v1`。

## 2. 范围

范围内：

- MinerU process wrapper。
- parser artifact layout。
- MinerU output reader。
- Mapper to NormalizedIR。
- normalized_ir artifact 写入。
- parse processing_run 状态。


## 3. 实施细则

1. 实现 parser port：

```text
DocumentParserPort.parse(input_pdf, output_dir, options) -> ParserResult
```

2. 实现 MinerU adapter：

```text
mineru_process.py
artifact_reader.py
mapper_to_ir.py
```

3. 输出目录：

```text
data/parser_artifacts/<provider>/<security>/<provider_doc_id>/run_<run_id>/
```

4. NormalizedIR 输出：

```text
data/derived/normalized_ir/<provider>/<security>/<provider_doc_id>/run_<run_id>/normalized_ir.v1.json
```

5. processing_run 写入 parser_name、parser_version、backend、input_raw_file_hash、artifact relpath、normalized_ir relpath、status。
6. adapter 不让 domain 读取 MinerU raw JSON。


## 4. 检查点

- 给定 document_id，可生成 processing_run。
- parser_artifacts 目录存在。
- normalized_ir.v1.json 存在。
- NormalizedIR schema contract test 通过。
- 解析失败时 processing_run.status = failed，旧 active run 不受影响。
- 模型缓存没有落到内置盘默认 cache。


## 5. Definition of Done

- MinerU adapter 可解析样本 PDF。
- NormalizedIR 可被后续 unit builder 读取。
- parser 失败可定位。


## 6. 明确不做

- 不生成最终 document_unit。
- 不发布 active run。
- 不做复杂 parser fallback。
- 不实现常驻 HTTP MinerU service。


## 7. 交付给下一阶段

- ParserResult。
- parser_artifacts。
- normalized_ir artifact。
- processing_run parse 状态。


## 8. 常见失败与处理

- MinerU 输出格式变化：只改 mapper，不改 domain。
- 单份 PDF 解析超时：记录 failed，不阻塞其他任务。
- IR 过大：记录大小，暂不优化存储。

## 9. 独立 testing 验证

验证环境：

```text
PostgreSQL 18 / AgentSSD pg18-main
socket: /Volumes/AgentSSD/agent_system/postgres/sockets
port: 55432
MinerU: /Volumes/AgentSSD/agent_system/services/disclosure_anchor/runtime/venvs/mineru-phase00/bin/mineru
```

命令验证：

```text
.venv/bin/python -m compileall -q src tests        pass
make test-unit                                    38 tests, OK
make test-contract                                6 tests, OK
make test                                         no DB env: 73 tests, OK (skipped=24)
make migrate                                      pass, head=0003_parser_run_metadata（当时 head；现已推进至 0006）
make test-integration                             24 tests, OK
make test                                         DB env: 73 tests, OK
make doctor                                       pass
git diff --check                                  pass
```

SQL 点检：

```text
alembic=0003_parser_run_metadata（当时 head；现已推进至 0006）
processing_run Phase 04 cols=3
public relpath cols=0
processing_runs_v1 parser metadata cols=2
```

真实样本 smoke：

- 输入真实 PDF：`tmp/sample_filings/002484_江海股份/...1225376481.pdf`，约 102KB。
- 使用临时 data/runtime root 和临时 DB 记录，执行 `register_local_pdf` + `ParseDocument` + 实际 MinerU CLI。
- 结果：`processing_run.status=succeeded`，生成 parser artifact relpath、`normalized_ir.v1.json`，元素数 31，
  `kinds=text`。
- 测试结束后清理临时 DB 记录和临时 data root。

覆盖：

- A14：domain/application 不直接读取 MinerU raw JSON；application 只依赖 parser port，MinerU raw reader/mapper
  位于 adapter 层。
- A15：parser failure 会写入新的 failed parse run，既有 active run 保持 `succeeded/is_active=true`。
