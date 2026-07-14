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
   the user explicitly asks. Local commits on a spawned `task/<task-key>` branch are the one standing
   exception (§3). Never run destructive cleanup without explicit approval.
7. **Service ownership:** migrations write only the owning component's schemas/roles. Shared-package changes and
   public-contract changes update all affected consumers, exports, tests, and docs together.

## 3. Work selection, research, and handoff

- Default to bounded, in-scope work. Read-only requests authorize inspection/reporting; change/fix requests
  authorize requested local edits and non-destructive validation. Ask before destructive, external, costly,
  credential/permission, commit/push, or materially scope-expanding actions.
- A gitignored durable record — `docs/agent/HANDOFF.md`, or a parked record once parked — is mandatory when
  work is expected to cross sessions; changes architecture, a public contract, migration/data boundary, or
  high-risk operations; has material unknowns; is explicitly requested as durable; resumes an existing
  durable task; or pauses awaiting a user decision. Create it before the first task mutation, or before
  yielding when a bounded task first meets a trigger.
- Task records and locks are separate things. Per worktree, `docs/agent/HANDOFF.md` holds the single
  gate-holding task and `docs/agent/parked/<task-key>.md` holds any number of parked tasks. The worktree
  write gate is held exactly while an unclosed `HANDOFF.md` exists anywhere in the worktree; a session that
  does not own that gate makes no working-tree mutation there, however small — it works in a separate
  worktree instead. Closed means `State:` is exactly `closed` or `completed`; a missing, malformed, or novel
  state counts as unclosed and holds the gate.
- A gate-holding handoff keeps a named writer; the writer changes only by parking, closing, or an explicit
  user handoff. Closing requires the worktree to carry none of the task's uncommitted edits — closing over
  live dirt is invalid; the gate follows protected state. The user's resolution of a gate-holding task's
  pending decision authorizes the resuming session to claim its writer slot; unrelated requests never do,
  and a crashed or absent session's unclosed task is reclaimed only on explicit user instruction, never
  inferred from recency.
- Parking is one step: write `docs/agent/parked/<task-key>.md` and delete `HANDOFF.md`; the gate releases
  with that deletion. Parking (`waiting_user`, `blocked`, `paused`, `monitoring`) requires: no uncommitted
  working-tree edits of the task's own (committed to its `task/` branch, or none) and runtime claims
  released in `RUNTIME.md` — only a `monitoring` record may retain listed claims. A task that cannot meet
  these — e.g. `waiting_user` over an uncommitted diff — stays in `HANDOFF.md` and keeps the gate.
- A parked record never gates the worktree; it names its pending decision and premises (the paths and
  subjects the decision rests on). No session in any worktree edits those premises or acts on the pending
  decision until the user resolves it. Resuming moves the record back into a free `HANDOFF.md` slot (absent
  or closed) and deletes the parked file in the same step; a task the user resolves without further work is
  closed by deleting its parked record after reporting the outcome.
- In every new session, before the first repository mutation, read this worktree's root and nearest
  component `docs/agent/HANDOFF.md` and `docs/agent/parked/` — and, when working in a spawned worktree, the
  primary checkout's root and affected-component `parked/` — and announce the gate task/state/writer and
  each parked key/state once. Claim the writer slot only when no unclosed `HANDOFF.md` exists anywhere in
  the worktree (`scripts/agent_worktree.sh list` shows them all) and the tree is clean or every dirty path
  belongs to the record being resumed. Claims are check-then-verify: after writing `HANDOFF.md` or a
  `RUNTIME.md` row, re-read it before the first mutation; if the writer/owner differs, back off and
  re-announce.
- Cross-repo work uses root `docs/agent/HANDOFF.md`; component work uses the nearest component handoff.
  One worktree has at most one gate-holding task and one owning writer (root or component level, not both).
  Parallel mutating tasks use separate worktrees — `scripts/agent_worktree.sh spawn <task-key>` creates
  `../agent-invest-worktrees/<task-key>` on branch `task/<task-key>`, copies `.worktreeinclude` files, and
  surfaces the primary checkout's parked guards; `reap` removes only a clean worktree whose handoffs are
  closed or absent, parked records resolved or transferred, and runtime claims released. Reviewers and
  helper agents are read-only.
- Work in a spawned worktree is delivered as local commits on its `task/<task-key>` branch — spawning
  authorizes commits on that branch only (the §2.6 exception); push, merging into `main`, and branch
  deletion still need an explicit user request, and the merge is performed by the primary checkout's
  writer or the user.
- Worktrees do not isolate shared runtime state. Shared PostgreSQL writes, AgentSSD mutation, and
  worker/launchd control each have exactly one owner, recorded in the gitignored `docs/agent/RUNTIME.md`
  of the primary checkout (the first entry of `git worktree list`). Read it before any task that may touch
  these resources; claim before mutating; release on close or park. Claim/release edits to `RUNTIME.md` are
  coordination state, exempt from the primary worktree's write gate. Never claim over a non-default owner:
  surface the current owner — including any parked task retaining the claim — and get the user's
  confirmation first. The resident launchd KeepAlive job is the worker's steady-state owner when no task
  claims it.
- A handoff, parked record, or runtime claim is local to its checkout/worktree and is not copied through Git
  or `.worktreeinclude`. A receiving session must resume in that checkout or perform an explicit handoff.
  Before archiving/deleting a task or worktree, close it or transfer every unresolved user gate, premise
  guard, and external-state obligation.
- Keep `HANDOFF.md` under 80 lines and record only: task key/title, scope, state, authority, user intent and
  acceptance, authorization boundary, next action, blockers, worktree/branch/base, changed paths, latest
  current-session validation/review, runtime claims, writer, and updated time. Keep each parked record under
  40 lines: key/title, state, pending decision and re-entry condition, guarded premises, retained runtime
  claims, next action, branch/base, brief origin note, and updated time. Long-lived facts belong in tracked
  contracts/docs; secrets, chat transcripts, duplicated repo facts, and volatile ops snapshots do not.
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
