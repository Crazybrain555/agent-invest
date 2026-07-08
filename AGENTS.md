# agent-invest Monorepo Operating Contract

This repository is the monorepo for the 投研预测引擎 (L1–L6 investment-research prediction engine).
The governing architecture document is protocol v0.7 (see "Authority documents" below). The repo currently
contains the L1 disclosure path service; further services join as siblings under `services/`.

Keep this file thin: cross-service norms and pointers only. Everything service-specific lives in that
service's own `AGENTS.md`.

## 1. Layout and authority chain

```text
services/disclosure_anchor/   L1 披露 PDF 路径服务（live；也是新服务的复用蓝图）
services/asset_intake/        L1 dataset_snapshot + tool_result 轻登记服务（M-C 立项中）
packages/envelope_kernel/     跨服务共享代码：data_asset 信封核（字段模型、kind 矩阵、asset:// URI、契约 schema）
docs/reference/               引擎级协议文档（v0.7 现行 + v0.6 沿革）
docs/archive/pre-restart/     Quant_agent 时代冻结存档（协议 v0.4/v0.5、旧 harness 文档）——历史证据，非现行政策
（规划中）services/upload_service/   L1 人工上传服务（独立服务，最后立项）
```

Rules:

1. **Nearest `AGENTS.md` wins.** Each service subtree is authoritative for its own norms, validation
   commands, and directory maps. This root file governs only cross-service concerns.
2. `docs/archive/pre-restart/` is frozen history. Never treat it as current policy, plan, or protocol.
3. Per-service `docs/agent/` is gitignored machine-local working memory and does not travel with clones.

## 2. Authority documents

- Engine architecture: `docs/reference/投研预测引擎顶层框架协议_v0.8.md`
  (engine-wide protocol, homed at root `docs/reference/` alongside prior versions).
- L1 disclosure service contract: `services/disclosure_anchor/AGENTS.md` and its `docs/` tree.

## 3. Cross-service hard rules

0. 调研先行（用户裁决 2026-07-08）：在方案优化的讨论与执行中（不限于设计/配置/契约，含实现细节与运维流程），先主动调研 2-4 个同形态的优秀开源项目或网上成熟实现（deepwiki / github / web），独立对比找差距、有可借鉴处纳入方案——自查发现要先于用户指出。

1. **One PostgreSQL cluster** (AgentSSD `pg18-main`); services separate by schema + role, never by
   per-layer databases; cross-service reads go through versioned `public` views only (protocol §1.8, §3.11).
2. **Blueprint reuse**: new services copy the disclosure_anchor手法 (envelope + kind, stable keys列化,
   `*_v1` public views, outbox change feed, processing_run/action_log, role least-privilege) instead of
   inventing new shapes (protocol §3.10).
3. Runtime data, raw files, model caches, and PG data live on AgentSSD
   (`/Volumes/AgentSSD/agent_system/{services/<svc>,shared,postgres}`), never in the repo.
4. No credentials in tracked files. Machine-specific MCP/harness config stays gitignored.
5. Do not commit, push, or rewrite git history unless the user explicitly asks.

## 4. Validation

Root `make agent-check` / `make test` delegate to each service's own gates (currently only
disclosure_anchor). Always run a service's gate from its own directory semantics (the root Makefile does
`$(MAKE) -C`); record blockers per that service's rules.

## 5. Adding a new service (checklist)

1. Confirm the service slot and scope against protocol v0.7 and the current plan with the user.
2. `services/<name>/` with its own `AGENTS.md` (+ `CLAUDE.md` symlink), `Makefile` with `agent-check`,
   `pyproject.toml`, `.gitignore` (copy disclosure_anchor's), `docs/agent/` (gitignored, machine-local).
3. Wire it into the root Makefile delegation list and the Layout table above.
4. DB: own schemas + roles in the shared cluster; expose `*_v1` public views; no cross-service private-table reads.
