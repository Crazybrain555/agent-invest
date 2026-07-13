# disclosure_anchor implementation pack v1.0

本包用于指导 `disclosure_anchor` 的第一轮工程实施。

核心原则：

1. `disclosure_anchor` 是本地单机、独立、自包含的 L1 披露文件服务。
2. 本服务内部是模块化单体，不拆微服务，不引入 Redis / Celery / Kafka / 独立工作流平台。
3. 已锁定：native PostgreSQL + 外置 PGDATA；native macOS MinerU batch worker；单 PG cluster、多 database。
4. 本服务只拥有披露文件侧 L1 数据域，不拥有 L2-L6 的 claim、证据账本、假设账本、预测快照、调度脊柱。
5. 后续 agent 必须按 milestone 逐步实施，每完成一阶段先通过检查点，再进入下一阶段。

## 文件结构

```text
docs/implementation/
  000-design-review-note.md
  001-disclosure-anchor-framework.md
  002-implementation-roadmap.md
  003-agent-execution-rules.md
  disclosure_anchor_phase000_004_final_merged_review.md   # phase000–004 审查意见（历史记录）
  design/
    classification-facets-and-derived-views.md
    retrieval-and-semantic-keys.md
    watchlist-operations.md
    worker-dynamic-scheduling.md
  milestones/
    00-local-environment-and-parser-validation.md
    01-code-skeleton-and-config.md
    02-postgres-and-migrations.md
    03-filestore-and-raw-archive.md
    04-mineru-adapter-and-normalized-ir.md
    04R-phase000-004-rework.md
    05-document-unit-builder-and-active-run.md
    06-filing-api-public-contracts.md
    07-cninfo-sync.md
    08-worker-loop-and-ops.md
    09-production-readiness.md
  checks/
    acceptance-matrix.md
    doctor-checklist.md
    contract-checklist.md
    fixture-and-test-policy.md
  reviews/
    phase008-db-comprehensive-audit-round3.md
    process-classes-review-2026-07-12.md
  runbooks/
    phase00-environment-and-parser-validation.md
    com.agentinvest.disclosure-anchor.worker-once.plist
```

里程碑 00–08（含 04R）均已实施完成（frontmatter status 与 `checks/acceptance-matrix.md` 为准）；
09 为生产就绪持续整改，状态为 in-progress；06R（检索投影派生层）为规划中里程碑，规格文档
尚未编写。

`milestones/` 讲每阶段"做什么/为什么/验收口径"；`runbooks/` 讲"具体敲什么命令、产物落哪、每步怎么自检"，照着跑即可。

推荐阅读顺序：

1. `000-design-review-note.md`
2. `001-disclosure-anchor-framework.md`
3. `002-implementation-roadmap.md`
4. `003-agent-execution-rules.md`
5. 当前要实施的 milestone 文件
6. `checks/` 下对应检查清单
