---
id: disclosure_anchor_acceptance_matrix
project: disclosure_anchor
title: disclosure_anchor 验收矩阵
status: final-for-implementation
created_at: 2026-06-26
---

# disclosure_anchor 验收矩阵

| 编号 | 验收项 | 对应阶段 | 检查方式 | 状态 |
|---|---|---|---|---|
| A01 | 外置盘挂载且 sentinel 存在 | 00/01 | `make doctor` | pass |
| A02 | PostgreSQL native cluster 可启动，PGDATA 位于 AgentSSD | 00/02 | Homebrew `pg_ctl` + `pg_isready` + `psql` | pass |
| A03 | 模型缓存不落内置盘默认 cache | 00/04 | env + doctor | pass |
| A04 | 样本 PDF 可生成 normalized_ir.v1.json | 00/04 | fixture check | pass |
| A05 | 样本 PDF 可生成 document_units.v1.jsonl | 00/05 | fixture check | pass |
| A06 | 代码中无业务硬编码 `/Volumes/AgentSSD` | 01 | grep + code review | pass |
| A07 | `FileStorePathBuilder` 是唯一路径生成入口 | 01/03 | unit test + review | pass |
| A08 | DB schema 可迁移且 migration 幂等 | 02 | `make migrate` | pass |
| A09 | public views 不暴露绝对路径或 private state | 02/06 | contract test | pass |
| A10 | 只读角色不能读写 private schema | 02 | permission test | pass |
| A11 | 本地 PDF 可登记成 document + raw hash | 03 | integration test | pass |
| A12 | raw_documents 只追加不覆盖 | 03 | integration test | pass |
| A13 | raw hash 与 DB 不一致能被 doctor 发现 | 03 | doctor test | pass |
| A14 | MinerU output 不被 domain 直接读取 | 04 | code review | pass |
| A15 | parsing failed 不影响旧 active run | 04/05 | integration test | pass |
| A16 | 年报经营分析 text unit 可查询 | 05/06 | fixture/API test | pending |
| A17 | 年报完整 table unit 可查询 | 05/06 | fixture/API test | pending |
| A18 | 投关完整 qa unit 可查询 | 05/06 | fixture/API test | pending |
| A19 | document_unit.payload 保存快照本身 | 05 | DB check | pending |
| A20 | active run 发布不删除历史 run | 05 | integration test | pending |
| A21 | unit content_hash / quality_status 变化产生 materialized change_event；内容不变的重跑只记 observed，不触发下游失效 | 05/06 | integration test | pending |
| A22 | `GET /v1/filings/latest` 可用 | 06 | API test | pending |
| A23 | `GET /v1/units/{id}/source-ref` 可用 | 06 | API test | pending |
| A24 | `GET /v1/changes` 可增量读取 | 06 | API test | pending |
| A25 | CNINFO 指定 10 家公司可同步公告索引 | 07 | integration/manual | pending |
| A26 | CNINFO PDF 下载进入 raw archive | 07 | integration/manual | pending |
| A27 | 查空写 source_access | 07 | DB check | pending |
| A28 | CNINFO 凭据只从环境变量进入 settings，且不写入 repo、DB、artifact 或日志 | 01/07 | settings test + review | pass |
| A29 | `make worker-once` 可从 pending 跑到 active run | 08 | end-to-end | pending |
| A30 | worker 崩溃不破坏 raw archive | 08 | failure test | pending |
| A31 | 外置盘未挂载时服务 fail closed | 01/08 | doctor/startup test | pass |
| A32 | document_units_v1 保留 15 个 unit 级 scope keys + asset_id；契约名收敛为 asset_id/payload_kind/event_kind | 02/06 | integration test | pass |
| A33 | 0007：信封最小核视图列（asset_kind/observed_at/source_tier/trace_level/raw_file_hash）+ source_tier 按 filing_type 映射 | 04R | contract test | pending |
| A34 | 主体解析走 identifier ledger 强键顺序（security→强键→新建），legal_name 只校验不合并；冲突置 contested 并拒绝 | 04R | unit test | pending |
| A35 | change_kind 落列且写侧必填；事件工厂统一 event_kind/change_kind/occurred_at | 04R | integration test | pending |
| A36 | parse 超时/探测失败 fail-closed/未知异常 re-raise；document.status 生命周期随 run 变迁 | 04R | unit+integration test | pending |
| A37 | NormalizedIR v2：parser 中立 kind 分类、结构化表、heading_level；golden fixtures 再生成 | 04R | contract test | pending |

状态枚举：`pending / pass / fail / blocked / intentionally-deferred`。
