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
- Preserve unrelated user changes. Without an explicit user request: no commit/push, no publishing,
  no credential or permission changes, no external writes, no destructive operations. Credentials
  never go into tracked files, Auto Memory, or handoffs.
- Back "done" claims only with checks actually run in the current session; report exact blockers
  instead of weakening a gate.

## Durable handoff

A gitignored `docs/agent/HANDOFF.md` is mandatory when any of these applies: work crosses sessions;
touches architecture, a public contract, a migration/data boundary, or high-risk operations; has
material unknowns; is explicitly requested as durable; resumes an existing task; or awaits a user
decision. Create it before the first repository mutation when the trigger is known up front, or
before yielding when a bounded task first meets a trigger.

- Cross-repo tasks use the root handoff; component tasks use the nearest component handoff.
- One worktree has one active task and one owning writer; separate write tasks use separate
  worktrees; reviewers and helpers are read-only. Shared PG, AgentSSD, worker, and launchd runtime
  state likewise has exactly one owner.
- In every new session, before the first repository mutation, check the root and nearest component
  `HANDOFF.md` and state its task/state/writer once.
- Any unclosed handoff (`active`, `monitoring`, `waiting_user`, `paused`, `blocked`) keeps its gate
  and writer: stay read-only until an explicit handoff, closed/completed, or a separate worktree;
  never infer ownership from recency.
- A handoff lives only in its checkout/worktree and is never copied via Git or `.worktreeinclude`.
  Before archiving or deleting a task or worktree, close it or explicitly transfer every unresolved
  user gate and external runtime obligation.
- A handoff records only: task key/title, scope, state, authority, user intent/acceptance,
  authorization boundary, next action, blockers, worktree/branch/base, changed paths, latest
  current-session validation/review, runtime owner, writer, updated time — 80 lines max.
- Legacy `Prompt/Plan/Status/Documentation/Implement/code_review`, `archive/`, and `notes/` are
  read-only history: never update or recreate them; physically deleting these ignored files needs
  explicit user approval.

This section and root `AGENTS.md` §3 are two renderings of one protocol; edit them together.
Auto Memory keeps only user preferences and collaboration habits that outlast tasks; specs belong in
tracked docs, and volatile ops facts are not stored.
