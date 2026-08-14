# disclosure_anchor implementation map

本目录记录当前实现的设计、检查、操作和仍有价值的工程历史。Agent 运行规则只由
仓库根工作流、服务合同和最近的源目录合同维护；`003-agent-execution-rules.md` 是短期兼容
指针，不是第三套工作流。已由
`provider_document.v1` / `provider_unit.v6` 取代的 NormalizedIR writer、旧证明图、
旧 unit-builder、phase00 fixture 与 corpus reset/reparse 工具不再保留在工作树；需要
考古时直接查看 Git 历史。

## 当前入口

- `design/mineru-medium-greenfield.md`：MinerU 3.4.4 Hybrid-medium 唯一 writer、
  provider-native artifact、薄 outline、table owner/stub 与 Unit 投影决策。
- `checks/mineru-medium-greenfield-visual-review.md`：冻结真实样本的视觉验收。
- `checks/contract-checklist.md`：当前持久化、公开契约与历史 v4 只读边界。
- `checks/fixture-and-test-policy.md`：真实样本、合成测试和 provider opt-in gate。
- `runbooks/production-operations.md`：当前 worker、doctor、GC 与迁移操作。
- `design/worker-dynamic-scheduling.md`：resident worker 机制。
- `design/retrieval-and-semantic-keys.md`：检索投影当前契约及其历史演变。
- `design/classification-facets-and-derived-views.md` 与 `milestones/07-cninfo-sync.md`：分类/CNInfo
  稳定机制；具体语料审查证据保存在 `reviews/`，不进入 AGENTS.md。

## 目录

```text
docs/implementation/
  003-agent-execution-rules.md  # deprecated pointer only
  design/
    mineru-medium-greenfield.md
    document-outline-and-toc.md
    retrieval-and-semantic-keys.md
    retrieval-scale-hardening.md
    classification-facets-and-derived-views.md
    watchlist-operations.md
    worker-dynamic-scheduling.md
  milestones/
    02-postgres-and-migrations.md
    03-filestore-and-raw-archive.md
    06-filing-api-public-contracts.md
    06R-retrieval-search-projection.md
    07-cninfo-sync.md
    08-worker-loop-and-ops.md
    09-production-readiness.md
  checks/
    acceptance-matrix.md
    contract-checklist.md
    doctor-checklist.md
    fixture-and-test-policy.md
    independent-review-guide.md
    mineru-medium-greenfield-visual-review.md
  reviews/                  # dated evidence/history; not active agent policy
  runbooks/
    production-operations.md
```

应用过的数据库迁移仍是不可改写的历史；`normalized_ir.v4` 仅保留一个很小的历史
evidence resolver，以维持既有 public asset/run 解引用。它不是新 Build、Rebuild、
Publish 或 search writer 的输入。
