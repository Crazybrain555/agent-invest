# agent-invest Monorepo Operating Contract

This repository is the monorepo for the 投研预测引擎 (L1–L6). Keep this file limited to cross-service rules;
each service/package owns its local commands, maps, and hard boundaries in a nearer `AGENTS.md`.

## 1. Authority and layout

Use the authority that governs the question:

- **Task intent and authorization:** the current user request and acceptance criteria set task scope and
  permitted actions; they revise product semantics only when the user explicitly authorizes that revision.
- **Product semantics:** the engine protocol `docs/reference/投研预测引擎顶层框架协议_v0.8.md`; L1 planning
  `docs/reference/l1_planning/L1来源资产层整体规划_v0.5.md`, subordinate to v0.8; then the nearest component's
  architecture/contract docs.
- **Agent operating policy:** the applicable `AGENTS.md` chain plus the shared workflow docs it references
  (`docs/agent-workflow.md`, `docs/agent-research-workflow.md`).
- **External mechanisms:** version-matched official specifications, documentation, release notes, and source
  for the library, provider, protocol, or source format actually deployed.
- **Descriptive truth:** actual files, schemas, commands, and observed results describe what currently exists.
  A mismatch with a normative authority is implementation/doc drift to reconcile; it never silently overrides
  v0.8 or a component's normative contract.
- **Comparative design evidence:** maintainer records, issues/PRs, and mature implementations inform design but
  never override product semantics. Discovery tools provide maps and hypotheses, not authority; verify material
  behavior claims in the exact official artifact or source at a pinned ref and map them to the deployed version.

```text
services/disclosure_anchor/   L1 disclosure/PDF path (live; blueprint for service mechanics)
services/asset_intake/        L1 dataset_snapshot + tool_result registration service (implemented;
                              real provider adapters remain follow-up work)
packages/envelope_kernel/     shared data_asset envelope model, kind matrix, asset:// URI, schemas
docs/reference/               current v0.8 engine protocol, v0.6 history, and L1 planning authority
docs/archive/pre-restart/     frozen Quant_agent-era evidence; never current policy or an execution cwd
(planned) services/upload_service/   independent L1 human-upload service
```

Instruction files layer from the repository root toward the working directory; nearer files add or narrow rules
for their subtree, so keep the chain non-contradictory. `AGENTS.md` is the shared tool-neutral contract. Root
`CLAUDE.md` and Codex configuration are thin tool adapters and do not duplicate product rules.
`docs/archive/pre-restart/` is frozen even when it contains old instruction files.

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
   the user explicitly asks. Creating a task branch or worktree does not grant that authorization. Never run
   destructive cleanup without explicit approval.
7. **Service ownership:** migrations write only the owning component's schemas/roles. Shared-package changes and
   public-contract changes update all affected consumers, exports, tests, and docs together.

## 3. Work selection, research, and task state

- Default to bounded, in-scope work. Read-only requests authorize inspection/reporting; change/fix requests
  authorize requested local edits and non-destructive validation. Ask before destructive, costly,
  credential/permission, commit/push, external-write/message, or materially scope-expanding actions. Preserve
  unrelated user changes: never revert or overwrite working-tree edits that are not the current task's.
- Durable task state is required when work crosses sessions; touches architecture, a public contract,
  migration/data boundaries, or high-risk operations; has material unknowns; is explicitly requested as durable;
  resumes an existing durable task; or pauses for a user decision. Before the first mutation, fully read and follow
  `docs/agent-workflow.md` when a trigger applies, HANDOFF/parked state is in scope, or shared runtime may change.
- At session start, inspect root and affected-service HANDOFF/parked records before mutation and announce each
  active gate once. An unclosed HANDOFF holds the worktree write gate; only its named writer may mutate. Read and
  claim the primary checkout's `docs/agent/RUNTIME.md` before shared PG, AgentSSD, or worker mutations.
- If task history looks incomplete, stale, resumed, or compacted, reconcile the current user request, applicable
  HANDOFF, Git/worktree truth, and any in-scope external state before another mutation or side effect. Conversation
  summaries are hints, not execution state; never repeat an action recorded as completed solely because it reappears
  as pending in chat context. Follow the recovery receipt in `docs/agent-workflow.md` §2.
- Legacy `Prompt/Plan/Status/Documentation/Implement/code_review`, `archive/`, and `notes/` are read-only history,
  never current authority. Do not recreate, update, or delete ignored legacy state without explicit approval.
- **Pre-design evidence gate:** before a behavior-affecting decision or edit, inspect the governing contract,
  current implementation, and a representative real case when one is available and applicable. Follow
  `docs/agent-research-workflow.md` for when external research is required, source authority, depth, the
  before-edit record, and stop conditions; nearer
  component rules may deliberately be stricter. Adopt general invariants and positive/negative tests, not
  project-specific patches or copied internals. External evidence never silently revises a repository contract.
- For DB-backed parsing, projection, publication, or retrieval work, calibrate against the real versioned public
  view/read model with read-only SQL when available (DBHub is suitable): inspect the live schema before naming
  columns, then sample active rows and distributions across representative types/owners. Historical tests and
  fixtures are evidence, not product authority. Keep currently published output distinct from an offline replay
  of dirty-tree code, explain any delta from source identity, and only then encode the confirmed invariant as a
  positive/negative regression test. Never mutate shared runtime data merely to make an inspection convenient.
- Keep scope surgical, expose unexpected failures, validate real boundaries, and close behavior changes with
  tests or an exact blocker plus matching contract/docs updates.

## 4. Validation and review

- `make agent-check` delegates to the components listed by the root Makefile; `make test` delegates their test
  targets. Run component-specific live-DB, migration, fixture, or smoke gates when the changed behavior needs
  them and the environment is in scope.
- Policy/doc changes require `git diff --check`, current path/command verification, and TOML/JSON parsing when
  those formats change. Back "done" claims only with checks actually run in the current session; record exact
  blockers rather than weakening a gate.
- Before completing material runtime, public-contract, setup/validation-command, agent-policy, or durable-
  workflow changes, use an independent read-only reviewer that did not implement the diff. Treat findings as
  candidates and fix only evidence-backed material items. Routine progress updates and trivial factual edits do
  not independently trigger review.

## 5. Adding a component

1. Confirm scope against protocol v0.8, the L1 v0.5 plan when relevant, and the user's current priorities.
2. Create `services/<name>/` or `packages/<name>/` with a short local `AGENTS.md`, `Makefile` containing
   `agent-check`, `pyproject.toml`, `.gitignore`, and gitignored `docs/agent/HANDOFF.md` only when a task meets
   the §3 triggers. Tool-specific adapters are maintained separately from this shared policy.
3. Add the component to the root Makefile delegation list and update the layout table above.
4. Give DB-backed services their own schemas and roles in `invest_engine`; expose versioned public views and
   forbid private cross-service reads.
5. Validate from root and from the component's own directory semantics; record environment-specific blockers.
