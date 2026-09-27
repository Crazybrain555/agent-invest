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

`worker operational control`（不需要 DB）：`runtime/control/worker-circuit-stop.json` 存在、无效或
control 存储/runtime 根不可信（含受监督根缺挂载 sentinel 或不在同一设备），或受监督 label 的 launchd
读回未知 → FAIL（报 SHA、cause、record_origin、native_disable 或读回 detail）；label 被原生 disable 或
loaded job 以 78 退出但没有记录 → WARN（原生状态本身没有原因，不编造语义故障；启动门仍拒绝）；否则
PASS。只有 macOS 上的受监督 runtime 根（`DISCLOSURE_WORKER_SUPERVISED_RUNTIME_ROOT`）才读 launchd，
其它根报 `no native supervisor applies`；生产 label 与非生产根（或生产根与其它 label）配对为
`label_root_mismatch`，不调用 launchctl，按读回未知 FAIL。合同见 `../design/worker-operational-stop.md`。

`worker execution qualification` / `worker legacy obligations`（仅在配置本地执行升级 U01 时出现；exact 部署不
增加任何行）：前者用 resident worker 同一 checker 验证 U01，PASS 报 `compatible_parent (inherited)`、父资格原日期、
R0→R1、W0→W1 与提案 SHA；v2 提案改报 `contract=worker-local-execution-upgrade.v2`、`anchor_qualified_at`/
`anchor_runtime`（Q0）与 recovery origin→target 的 release/runtime/writer，任何不一致 FAIL；后者（需要 DB）在一个 READ ONLY
快照中执行 worker 启动时同一范围复核，附 `final_states=`（按终态计数，合法 `local_failed` 与 `acked` 同为终态），
成员仍未终结 → WARN（新 H0 被保持），全部终结 → PASS；成员未终结时出现的任何清单外 head、非成员 legacy head、
身份/历史漂移或 staged non-current prepared 行 → FAIL。合同见 `../design/local-execution-upgrade.md`。

## 2. PostgreSQL 检查

```text
PG localhost TCP（127.0.0.1:55432）或 AgentSSD socket 可连接
当前 database 是 invest_engine（monorepo 共库，本服务只拥有 disclosure_* schema）
migration version 最新
disclosure_core / disclosure_public / disclosure_ops 存在
当前 app role 权限正确
```

## 3. 文件系统检查

```text
raw_documents 可读写
parser_artifacts 可读写
derived/normalized_ir 可读（历史 v4 evidence/GC；新 writer 不再写）
derived/provider_documents 可读写
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

抽样参数：默认 `--sample 20`（按 raw_file_relpath 排序取前 N ∪ 最新 N），`--full` 全量。
至少支持：

```text
抽样 document.raw_file_relpath 是否存在
抽样 document.raw_file_hash 是否与文件 bytes 一致
按 run 结局区分：
  succeeded parse/rebuild run → normalized_ir_relpath 与 provider_document_relpath 恰一存在，
                                所选 primary artifact 与 artifact_hash 匹配
  failed run    → 只要求结构化 error 存在（合法 JSON），不报 artifact 缺失
  unit_build_status='succeeded' → document_units_relpath 存在且快照哈希与 DB 聚合一致
  receipt v2 → relpath/version/hash/逐行闭合 contract、Unit count 与 semantic summary 一致
  unit_build_status='failed' → unit_build_error 是 SQL object，且进入 retry/dead-letter 可见面
  active semantic_adjudication_status='degraded_unavailable' → WARN，不得以 parse success 掩盖
抽样 document_unit.artifact_locator 是否可回指
每个 document 最多一个 current active run
outbox seq 单调递增（空洞 → WARN）
stale running run（超龄阈值经 queries.py helper 施加）→ WARN
Unit build retrying / dead letters → WARN / FAIL；修复入口为显式 rebuild-units
download dead letters（0064，ops.download_failure_resolution_v1）→ 有死信候选 WARN；同行列出不可重试失败
  总数 / 已由保留原件登记解决数 / 未解决数，失败历史保留；修复入口为 runbook §5.5 的具名 source-recovery
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

doctor 还必须确认 `DATABASE_URL` 当前用户是非 superuser `disclosure_app`；migration owner DSN
不得作为 worker/pipeline 回退。doctor 失败时不得自动修复数据。自动修复必须单独命令，并需要显式确认。
