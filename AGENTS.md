# agent-invest Monorepo Operating Contract

This repository is the monorepo for the 投研预测引擎 (L1–L6). Keep this file limited to cross-service rules;
each service/package owns its local commands, maps, and hard boundaries in a nearer `AGENTS.md`.

## 1. Authority and layout

Semantic authorities:

- Engine protocol: `docs/reference/投研预测引擎顶层框架协议_v0.8.md`.
- L1 planning: `docs/reference/l1_planning/L1来源资产层整体规划_v0.5.md`, subordinate to v0.8.
- Component contracts: the nearest tracked `AGENTS.md` plus that component's architecture/contract docs.
- Descriptive truth: actual files, schemas, commands, and observed results determine what currently exists.
  A mismatch is implementation/doc drift to reconcile; it does not silently override v0.8 or a component's
  normative contract.

```text
services/disclosure_anchor/   L1 disclosure/PDF path (live; blueprint for service mechanics)
services/asset_intake/        L1 dataset_snapshot + tool_result registration service (implemented;
                              real provider adapters remain follow-up work)
packages/envelope_kernel/     shared data_asset envelope model, kind matrix, asset:// URI, schemas
docs/reference/               current v0.8 engine protocol, v0.6 history, and L1 planning authority
docs/archive/pre-restart/     frozen Quant_agent-era evidence; never current policy or an execution cwd
(planned) services/upload_service/   independent L1 human-upload service
```

Codex loads applicable project instructions from the repository root toward the working directory. Nearer
files add or narrow subtree rules and take precedence only when the same subject conflicts; they do not erase
unrelated parent rules. `docs/archive/pre-restart/` remains frozen even if it contains old instruction files.

Root and per-service `docs/agent/` directories are gitignored machine-local task state. A cross-repo task has
one state owner at root; a service-scoped task uses that service's state. Do not maintain competing active plans.

## 2. Cross-service invariants

1. **One PostgreSQL cluster and database:** AgentSSD `pg18-main`, database `invest_engine`; components isolate
   with schemas and least-privilege roles, not per-layer databases. Cross-service reads use versioned public
   views or explicit APIs/change feeds, never another service's private tables.
2. **Shared envelope:** services reuse `packages/envelope_kernel` for the `data_asset` envelope, kind matrix,
   `asset://` URI, and exported schema. Breaking changes require a versioned contract.
3. **Blueprint, not cloning:** reuse disclosure_anchor's proven mechanics—stable keys, public `*_v1` views,
   outbox/change feed, processing runs/action logs, and role boundaries—while keeping each service contract thin.
4. **External runtime state:** PG data, raw files, caches, models, and generated research artifacts live under
   `/Volumes/AgentSSD/agent_system/`, never in Git.
5. **Secrets:** real credentials live in environment variables or private user-level config. Tracked files and
   examples contain placeholders only; replace exposed credentials and tell the user to rotate them.
6. **Git/external actions:** do not commit, push, rewrite history, publish, or make other external writes unless
   the user explicitly asks. Never run destructive cleanup without explicit approval.
7. **Service ownership:** migrations write only the owning component's schemas/roles. Shared-package changes and
   public-contract changes update all affected consumers, exports, tests, and docs together.

## 3. Planning and research

- Default to bounded, in-scope work. Read-only requests authorize inspection/reporting; change/fix requests
  authorize requested local edits and non-destructive validation. Ask before destructive, external, costly, or
  materially scope-expanding actions.
- Use durable task files for cross-session work, architecture/public contracts/migrations, high-risk operations,
  material unknowns, an explicit user request, or continuation of an active durable task. File count and routine
  state/doc maintenance are not sufficient triggers.
- Before selecting a materially new architecture, cross-service contract, dependency, provider framework, or
  ops mechanism, compare 2–4 relevant implementations. Prefer official vendor docs for vendor/model behavior.
  Approved-plan execution, localized fixes, factual corrections, and state synchronization are exempt.
- Keep scope surgical, expose unexpected failures, validate real boundaries, and close behavior changes with
  tests or an exact blocker plus matching contract/docs updates.

## 4. Validation and review

- `make agent-check` delegates to the components listed by the root Makefile; `make test` delegates their test
  targets. Run component-specific live-DB, migration, fixture, or smoke gates when the changed behavior needs
  them and the environment is in scope.
- Policy/doc changes require `git diff --check`, current path/command verification, and TOML/JSON parsing when
  those formats change. Record exact blockers rather than weakening a gate.
- Before completing material runtime, public-contract, setup/validation-command, agent-policy, or durable-
  workflow changes, use an independent read-only reviewer that did not implement the diff. Treat findings as
  candidates and fix only evidence-backed material items. Routine progress updates and trivial factual edits do
  not independently trigger review.

## 5. Adding a component

1. Confirm scope against protocol v0.8, the L1 v0.5 plan when relevant, and the user's current priorities.
2. Create `services/<name>/` or `packages/<name>/` with a short local `AGENTS.md`, `Makefile` containing
   `agent-check`, `pyproject.toml`, `.gitignore`, and gitignored `docs/agent/` only when durable local state is
   actually needed. Tool-specific adapters are maintained separately from this shared policy.
3. Add the component to the root Makefile delegation list and update the layout table above.
4. Give DB-backed services their own schemas and roles in `invest_engine`; expose versioned public views and
   forbid private cross-service reads.
5. Validate from root and from the component's own directory semantics; record environment-specific blockers.
