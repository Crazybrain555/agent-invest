---
id: disclosure_anchor_doctor_checklist
project: disclosure_anchor
title: doctor 检查清单
status: final-for-implementation
created_at: 2026-06-26
---

# doctor 检查清单

`doctor` 是本服务最小运行自检，不是备份系统，也不是监控平台。

## 1. 环境检查

必须检查：

```text
/Volumes/AgentSSD 已挂载
/Volumes/AgentSSD/agent_system/MOUNT_SENTINEL_DO_NOT_CREATE_ON_INTERNAL 存在
DISCLOSURE_DATA_ROOT 指向 /Volumes/AgentSSD/agent_system/services/disclosure_anchor
DISCLOSURE_SHARED_ROOT 指向 /Volumes/AgentSSD/agent_system/shared
DISCLOSURE_RUNTIME_ROOT 指向 /Volumes/AgentSSD/agent_system/services/disclosure_anchor/runtime
```

失败策略：fail closed。

## 2. PostgreSQL 检查

```text
PG localhost TCP（127.0.0.1:55432）或 AgentSSD socket 可连接
当前 database 是 disclosure_anchor
migration version 最新
disclosure_core / disclosure_public / disclosure_ops 存在
当前 app role 权限正确
```

## 3. 文件系统检查

```text
raw_documents 可读写
parser_artifacts 可读写
derived/normalized_ir 可读写
document_unit_snapshots 可读写
runtime/inbox 可读写
runtime/quarantine 可读写
runtime/failed 可读写
runtime/tmp 可读写
runtime/locks 可读写
```

## 4. 模型缓存检查

```text
MINERU_MODEL_CACHE 指向外置盘
HF_HOME 指向外置盘
MODELSCOPE_CACHE 指向外置盘
```

## 5. 数据一致性抽样检查

抽样参数：默认 `--sample 20`（按 raw_file_relpath 排序取前 N ∪ 最新 N），`--full` 全量
（04R-R6.1）。至少支持：

```text
抽样 document.raw_file_relpath 是否存在
抽样 document.raw_file_hash 是否与文件 bytes 一致
按 run 结局区分（04R-R6.3 / E11）：
  succeeded run → normalized_ir_relpath 存在且 artifact_hash 匹配
  failed run    → 只要求结构化 error 存在（合法 JSON），不报 artifact 缺失
  unit_build_status='succeeded' → document_units_relpath 存在且快照哈希与 DB 聚合一致
抽样 document_unit.artifact_locator 是否可回指
每个 document 最多一个 current active run
outbox seq 单调递增（空洞 → WARN）
stale running run（超龄阈值经 queries.py helper 施加）→ WARN
孤儿 raw/artifact 文件 → 报告不报错（05-S8 的 FS-先行 orphan 是合法状态）
```

## 6. 输出格式与分级

FAIL/WARN 封闭分级表与退出码以 **milestone 08 §3.6 为唯一权威**
（FAIL→退出码 1，仅 WARN→0）。建议输出：

```text
[PASS] mount sentinel
[PASS] pg connection
[FAIL] raw hash mismatch: document_id=...
[WARN] stale running run: processing_run_id=..., age=...
```

doctor 失败时不得自动修复数据。自动修复必须单独命令，并需要显式确认。
