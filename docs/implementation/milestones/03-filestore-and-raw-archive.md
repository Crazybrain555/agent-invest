---
id: disclosure_anchor_milestone_03_filestore-and-raw-archive
project: disclosure_anchor
title: 文件存储与 raw archive
status: complete
created_at: 2026-06-26
implemented_at: 2026-06-29
verified_at: 2026-06-29
---

# Milestone 03: 文件存储与 raw archive

> 实施状态（2026-06-29）：已实现并完成验证。`RawDocumentStore` 以 PDF bytes 计算
> `sha256:` hash，经 runtime tmp 写入、fsync、不可覆盖 hard-link 发布到
> `data/raw_documents/{provider}/{security_code}/{year}/{provider_document_id}/sha256_<hash>.pdf`；
> `register_local_pdf` 可登记本地 PDF，写入 `source_access` / `document` / `outbox_event`，
> 重复导入同 provider_document_id + hash 复用已有 document，不重复发布 raw。非法、缺失或 hash
> mismatch 输入进入 `runtime/quarantine`，不发布 document。doctor raw-hash 检查可发现 raw 文件被手工改动。
> A11-A13 pass。

## 1. 目标

实现本地 PDF 登记和 raw archive，保证原始 PDF byte 保真、hash 可校验、路径由 FileStorePathBuilder 管理。

## 2. 范围

范围内：

- RawDocumentStore。
- atomic write。
- sha256。
- register local PDF use case。
- quarantine 机制。
- document raw metadata 写库。


## 3. 实施细则

1. 实现 `RawDocumentStore`：

```text
put_raw_document(provider, security_code, year, provider_document_id, input_file)
verify_raw_document(relpath, expected_hash)
```

2. atomic write：先写 tmp，再 fsync，再用不可覆盖 hard-link 发布；目标已存在时只允许相同 hash 复用。
3. hash 计算：以 PDF bytes 为准。
4. register local PDF use case 输入：

```text
file_path
company/security metadata
filing_type
title
announcement_date
report_period
provider/local ref
```

5. 写入：

```text
source_access
document
raw file
outbox_event(document_downloaded 或 document_registered)
```

6. 重复导入策略：

```text
同 provider_document_id + raw_file_hash：不重复写 raw
同 provider_document_id + 不同 raw_file_hash：新 document 或新版本关系
hash mismatch / 文件打不开：进入 quarantine
```


## 4. 检查点

- 本地 PDF 可登记为 document。
- raw file 存到 `data/raw_documents/.../sha256_<hash>.pdf`。
- DB 存相对路径与 hash。
- 重复导入同文件不重复写。
- hash mismatch 进入 `runtime/quarantine`。
- raw 文件被手工改动后 doctor 能发现。


## 5. Definition of Done

- `register_local_pdf` 可用。
- raw archive 不覆盖旧文件。
- document 与 raw hash 一致。
- 相关 integration tests 通过。


## 6. 明确不做

- 不调用 MinerU。
- 不生成 document_unit。
- 不接 CNINFO。
- 不暴露 raw 下载 API。


## 7. 交付给下一阶段

- RawDocumentStore。
- register_local_pdf use case。
- hash 校验。
- raw archive fixture。


## 8. 常见失败与处理

- 文件写一半失败：不得生成 published document；保留 failed 输入。
- DB 写入成功但文件写入失败：UnitOfWork 必须 rollback 或标记 failed。
- relpath 不稳定：修 PathBuilder，不迁就测试。


## 9. 独立 testing 验证

验证环境：

```text
PostgreSQL 18 / AgentSSD pg18-main
socket: /Volumes/AgentSSD/agent_system/postgres/sockets
port: 55432
database: disclosure_anchor
```

命令验证：

```text
.venv/bin/python -m compileall -q src tests  pass
make migrate                                  pass
make test-integration                         22 tests, OK
make test                                     DB env: 64 tests, OK
make test                                     no DB env: 64 tests, OK (skipped=22)
make doctor                                   pass，含 raw hash 检查
git diff --check                              pass
```

测试覆盖：

- `RawDocumentStore` 写入、verify、重复写复用、非法 PDF 不发布 raw、quarantine manifest。
- `FileStorePathBuilder` raw relpath 合同：
  `raw_documents/{provider}/{security_code}/{year}/{provider_document_id}/sha256_<hash>.pdf`。
- `register_local_pdf` 本地 PDF 登记成 document + raw hash + outbox event。
- 同 provider_document_id + 同 hash 重复导入复用已有 document。
- 非法 / 缺失 PDF 进入 `runtime/quarantine` 且不写 document。
- 既有 security 与传入 company 不一致时 fail fast，且发生在 raw archive 写入前。
- 手工改动 raw 文件后，doctor raw-hash 检查返回 FAIL。

Review：

- 初次独立 review 发现两个 material findings：不可读输入可能不进入 quarantine；既有
  security/company 可能被登记成互相矛盾的 document metadata。
- 已修复：raw-store 将输入读/hash/open 失败转为 `InvalidRawDocumentError`，quarantine 可处理缺失 /
  不可读输入；`register_local_pdf` 在 raw 写入前 preflight 既有 security 的 canonical company，冲突
  时抛 `RegistrationMetadataError`，并补回归测试。
