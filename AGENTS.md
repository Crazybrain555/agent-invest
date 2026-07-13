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

Codex loads applicable `AGENTS.md` files from the repository root toward the working directory. Nearer files
add or narrow subtree rules and take precedence only when the same subject conflicts; they do not erase
unrelated parent rules. `AGENTS.md` is Codex project guidance; Claude-specific workflow lives in `CLAUDE.md`.
`docs/archive/pre-restart/` remains frozen even if it contains old instruction files.

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

## 3. Work selection, research, and handoff

- Default to bounded, in-scope work. Read-only requests authorize inspection/reporting; change/fix requests
  authorize requested local edits and non-destructive validation. Ask before destructive, external, costly,
  credential/permission, commit/push, or materially scope-expanding actions.
- A gitignored `docs/agent/HANDOFF.md` is mandatory when work is expected to cross sessions; changes
  architecture, a public contract, migration/data boundary, or high-risk operations; has material unknowns;
  is explicitly requested as durable; resumes an existing durable task; or pauses awaiting a user decision.
  Create it before the first task mutation, or before yielding when a bounded task first meets a trigger.
- In every new session, before the first repository mutation, inspect the root and nearest component
  `docs/agent/HANDOFF.md` when present and announce its task/state/writer once. Any unresolved handoff—
  `active`, `monitoring`, `waiting_user`, `paused`, or `blocked`—keeps its gate and writer ownership; stay
  read-only until it is explicitly handed off, closed/completed, or isolated in a different worktree. Never
  infer ownership from recency, and replace/delete a handoff only after an explicit closed/completed transition.
- Cross-repo work uses root `docs/agent/HANDOFF.md`; component work uses the nearest component handoff.
  One worktree has at most one active task and one owning writer. Different modifying tasks use different
  worktrees. Reviewers and helper agents are read-only. Shared PostgreSQL, AgentSSD, worker, and launchd state
  still has one runtime owner.
- A handoff is local to its checkout/worktree and is not copied through Git or `.worktreeinclude`. A receiving
  session must resume in that checkout or perform an explicit handoff. Before archiving/deleting a task or
  worktree, close it or transfer every unresolved user gate and external-state obligation.
- Keep `HANDOFF.md` under 80 lines and record only: task key/title, scope, state, authority, user intent and
  acceptance, authorization boundary, next action, blockers, worktree/branch/base, changed paths, latest
  current-session validation/review, runtime owner, writer, and updated time. Long-lived facts belong in
  tracked contracts/docs; secrets, chat transcripts, duplicated repo facts, and volatile ops snapshots do not.
  This handoff protocol is mirrored in the "Durable handoff" section of root `CLAUDE.md`; edit both together.
- Legacy `Prompt.md`, `Plan.md`, `Status.md`, `Documentation.md`, `Implement.md`, `code_review.md`, `archive/`,
  and `notes/` are migration/history evidence only: read on demand; never update, rotate, recreate, or use as
  current authority. Do not delete ignored legacy state without the user's explicit approval.
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
   `agent-check`, `pyproject.toml`, `.gitignore`, and gitignored `docs/agent/HANDOFF.md` only when a task meets
   the §3 triggers. Tool-specific adapters are maintained separately from this shared policy.
3. Add the component to the root Makefile delegation list and update the layout table above.
4. Give DB-backed services their own schemas and roles in `invest_engine`; expose versioned public views and
   forbid private cross-service reads.
5. Validate from root and from the component's own directory semantics; record environment-specific blockers.
