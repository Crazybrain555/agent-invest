# CLAUDE.md — Claude Code guide (monorepo root)

<!-- Maintainer note (stripped before injection): this ROOT file deliberately has no @AGENTS.md
     import (user decision) — root AGENTS.md §3 and this file each carry their own tool's workflow.
     Component/package/nested CLAUDE.md files are symlinks to their sibling AGENTS.md (tool-neutral
     boundaries, lazily auto-loaded). Do not add an import or symlink at the root. -->

## Defaults and authority

- Product semantics follow the engine protocol, component contracts, and actual repository evidence.
- Read-only requests (questions, diagnosis, review) authorize inspection and reporting only;
  change/fix requests authorize in-scope local edits and non-destructive validation.
- Before modifying the repository, read the root `AGENTS.md` for the cross-service invariants and
  layout; deeper `AGENTS.md` files load automatically through their CLAUDE.md symlinks as you work
  in those directories. Codex-specific mechanisms in them (instruction loading, sessions, tool
  config) do not apply to Claude.
- Preserve unrelated user changes. Without an explicit user request: no commit/push (except local
  commits on a spawned `task/<task-key>` branch, per Durable handoff below), no publishing, no
  credential or permission changes, no external writes, no destructive operations. Credentials
  never go into tracked files, Auto Memory, or handoffs.
- Back "done" claims only with checks actually run in the current session; report exact blockers
  instead of weakening a gate.

## Durable handoff

A gitignored durable record — `docs/agent/HANDOFF.md`, or a parked record once parked — is
mandatory when any of these applies: work crosses sessions; touches architecture, a public
contract, a migration/data boundary, or high-risk operations; has material unknowns; is explicitly
requested as durable; resumes an existing task; or awaits a user decision. Create it before the
first repository mutation when the trigger is known up front, or before yielding when a bounded
task first meets a trigger.

- Records and locks are separate: per worktree, `HANDOFF.md` holds the single gate-holding task and
  `docs/agent/parked/<task-key>.md` holds any number of parked tasks. The worktree write gate is
  held exactly while an unclosed `HANDOFF.md` exists anywhere in the worktree; a session that does
  not own that gate makes no working-tree mutation there, however small — it works in a separate
  worktree instead. Closed means `State:` is exactly `closed` or `completed`; a missing, malformed,
  or novel state counts as unclosed and holds the gate.
- A gate-holding handoff keeps a named writer; the writer changes only by parking, closing, or an
  explicit user handoff. Closing requires the worktree to carry none of the task's uncommitted
  edits — the gate follows protected state. The user's resolution of the task's pending decision
  authorizes the resuming session to claim the writer slot; unrelated requests never do, and a
  crashed or absent session's unclosed task is reclaimed only on explicit user instruction, never
  inferred from recency.
- Parking is one step: write `docs/agent/parked/<task-key>.md` and delete `HANDOFF.md`; the gate
  releases with that deletion. Parking (`waiting_user`, `blocked`, `paused`, `monitoring`)
  requires: no uncommitted edits of the task's own (committed to its `task/` branch, or none) and
  runtime claims released — only a `monitoring` record may retain listed claims. A task that cannot
  meet these (e.g. `waiting_user` over an uncommitted diff) stays in `HANDOFF.md` and keeps the gate.
- A parked record never gates the worktree; it names its pending decision and premises. No session
  in any worktree edits those premises or acts on the pending decision until the user resolves it.
  Resuming moves the record back into a free `HANDOFF.md` slot (absent or closed) and deletes the
  parked file in the same step; a task the user resolves without further work is closed by deleting
  its parked record after reporting the outcome.
- In every new session, before the first repository mutation, check this worktree's root and
  nearest component `HANDOFF.md` and `parked/` — and, in a spawned worktree, the primary checkout's
  root and affected-component `parked/` — and state the gate task/state/writer and each parked
  key/state once. Claim the writer slot only when no unclosed `HANDOFF.md` exists anywhere in the
  worktree (`scripts/agent_worktree.sh list` shows them all) and the tree is clean or every dirty
  path belongs to the record being resumed. Claims are check-then-verify: after writing
  `HANDOFF.md` or a `RUNTIME.md` row, re-read it before the first mutation; if the writer/owner
  differs, back off and re-announce.
- Cross-repo tasks use the root handoff; component tasks use the nearest component handoff. One
  worktree has at most one gate-holding task and one owning writer (root or component, not both);
  reviewers and helpers are read-only. Parallel mutating tasks use separate worktrees via
  `scripts/agent_worktree.sh spawn <task-key>`, which also surfaces the primary checkout's parked
  guards. Work there is delivered as local commits on `task/<task-key>` — spawning authorizes
  commits on that branch only; push, merging into `main`, and branch deletion still need an
  explicit user request, and the merge is performed by the primary writer or the user. `reap`
  removes only a clean worktree whose handoffs are closed or absent, parked records resolved or
  transferred, and runtime claims released.
- Worktrees do not isolate shared runtime state: shared PG writes, AgentSSD mutation, and
  worker/launchd control each have exactly one owner, recorded in the gitignored
  `docs/agent/RUNTIME.md` of the primary checkout (the first entry of `git worktree list`). Read it
  before any task that may touch these resources; claim before mutating; release on close or park.
  Claim/release edits to `RUNTIME.md` are coordination state, exempt from the primary worktree's
  write gate. Never claim over a non-default owner — surface the current owner, including any
  parked task retaining the claim, and get the user's confirmation first. The resident launchd
  KeepAlive job owns the worker's steady state when no task claims it.
- A handoff, parked record, or runtime claim lives only in its checkout/worktree and is never
  copied via Git or `.worktreeinclude`; a receiving session must resume in that checkout or perform
  an explicit handoff. Before archiving or deleting a task or worktree, close it or explicitly
  transfer every unresolved user gate, premise guard, and external runtime obligation.
- A handoff records only: task key/title, scope, state, authority, user intent/acceptance,
  authorization boundary, next action, blockers, worktree/branch/base, changed paths, latest
  current-session validation/review, runtime claims, writer, updated time — 80 lines max. A parked
  record: key/title, state, pending decision and re-entry condition, guarded premises, retained
  runtime claims, next action, branch/base, brief origin note, updated time — 40 lines max.
- Legacy `Prompt/Plan/Status/Documentation/Implement/code_review`, `archive/`, and `notes/` are
  read-only history: never update or recreate them; physically deleting these ignored files needs
  explicit user approval.

This section and root `AGENTS.md` §3 are two renderings of one protocol; edit them together.
Auto Memory keeps only user preferences and collaboration habits that outlast tasks; specs belong in
tracked docs, and volatile ops facts are not stored.
