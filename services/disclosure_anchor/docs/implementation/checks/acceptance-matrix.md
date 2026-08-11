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
| A04 | 样本 PDF 可生成 hash-bound `provider_document.v1`，并通过 PDF 页数与 bundle 全量重读 admission | provider | frozen source-identity replay | pass |
| A05 | admitted ProviderDocument 可确定性生成 provider-native coarse Units 与显式 search bindings | provider | unit + frozen replay | pass |
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
| A16 | 年报经营分析 text unit 可查询 | 05/06 | fixture/API test | pass |
| A17 | 年报完整 table unit 可查询 | 05/06 | fixture/API test | pass |
| A18 | 投关完整 qa unit 可查询 | 05/06 | fixture/API test | pass |
| A19 | document_unit.payload 保存快照本身 | 05 | DB check | pass |
| A20 | active run 发布不删除历史 run | 05 | integration test | pass |
| A21 | unit content_hash / quality_status 变化产生 materialized change_event；内容不变的重跑只记 observed，不触发下游失效 | 05/06 | integration test | pass |
| A22 | `GET /v1/filings/latest` 可用 | 06 | API test | pass |
| A23 | `GET /v1/units/{id}/source-ref` 可用 | 06 | API test | pass |
| A24 | `GET /v1/changes` 可增量读取 | 06 | API test | pass |
| A25 | CNINFO 指定 10 家公司可同步公告索引 | 07 | integration/manual | pass (2026-07-06 真实 API：10/10 家、464 份公告，字段零空值) |
| A26 | CNINFO PDF 下载进入 raw archive | 07 | integration/manual | pass (2026-07-06：464 份 PDF 不可变归档，跨通道 hash 幂等吸收) |
| A27 | 查空写 source_access | 07 | DB check | pass (2026-07-06：600519 checkpoint 续跑真实空窗口 result='empty' 落库) |
| A28 | CNINFO 凭据只从环境变量进入 settings，且不写入 repo、DB、artifact 或日志 | 01/07 | settings test + review | pass |
| A29 | `make worker-once` 可从 pending 跑到 active run | 08 | end-to-end | pass (2026-07-06 真实语料 2 docs parse→build→publish 27s；scratch 库全链集成含对账断言) |
| A30 | worker 崩溃不破坏 raw archive | 08 | failure test | pass (2026-07-06 kill -9 advisory 锁全释放；stale 回收 retryable=true；raw 归档只读校验不受影响) |
| A31 | 外置盘未挂载时服务 fail closed | 01/08 | doctor/startup test | pass |
| A32 | document_units_v1 保留 15 个 unit 级 scope keys + asset_id；契约名收敛为 asset_id/payload_kind/event_kind | 02/06 | integration test | pass |
| A33 | 0007：信封最小核视图列（asset_kind/observed_at/source_tier/trace_level/raw_file_hash）+ source_tier 按 filing_type 映射 | migrations/current contract | contract test | pass |
| A34 | 主体解析走 identifier ledger 强键顺序（security→强键→新建），legal_name 只校验不合并；冲突置 contested 并拒绝 | current register contract | unit test | pass |
| A35 | change_kind 落列且写侧必填；事件工厂统一 event_kind/change_kind/occurred_at | current event contract | integration test | pass |
| A36 | parse 超时/探测失败 fail-closed/未知异常 re-raise；document.status 生命周期随 run 变迁 | provider writer | unit+integration test | pass |
| A37 | 新 writer 仅接受 MinerU 3.4.4 Hybrid-medium；历史 NormalizedIR v4 只允许 evidence read，不得 Build/Rebuild/Publish | provider | import firewall + contract test | pass |
| A38 | retrieval projection 派生层：published unit 的 search projection（heading_path_text/display_subtitle/search_text/controlled_keywords/extractive_keywords/retrieval_rules_version，字段族=05-U7）全部可由已持久化数据确定性再生 | 06R | integration test | pending |
| A39 | 检索 smoke：自然语言关键词（应收账款账龄 / 关税影响 / 退市风险）可召回已知样本 unit | 06R | API test | pending |
| A40 | search projection 不污染证据：summary/keywords 不进 content_hash；payload 不变时投影重建不产生 materialized 事件、不触发 L3 失效 | 06R | integration test | pending |
| A41 | Filing API 错误码（NOT_FOUND/GONE_SUPERSEDED/L1_PROCESSING_REQUIRED/CONTRACT_VERSION_MISMATCH/VALIDATION_ERROR）contract test 全绿 | 06 | contract test | pass |

状态枚举：`pending / pass / fail / blocked / intentionally-deferred`。

注：A38–A40 的对应阶段 "06R"（检索投影派生层）是规划中的里程碑，规格文档尚未编写
（`docs/implementation/milestones/` 下暂无 06R 文件）；三行在其立项前保持 pending。
