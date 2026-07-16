# Agent task-state and runtime workflow

This is the shared, tool-neutral state protocol referenced by root `AGENTS.md`. Read it in full
before the first repository mutation when a durable-task trigger applies, an existing HANDOFF/parked record is
being resumed, parallel worktrees are involved, or shared runtime state may be mutated.

## 1. Durable records and the write gate

- A gitignored `docs/agent/HANDOFF.md` is mandatory when work crosses sessions; changes architecture, a public
  contract, migration/data boundaries, or high-risk operations; has material unknowns; is explicitly requested
  as durable; resumes an existing durable task; or pauses for a user decision. Create it before the first mutation
  when known up front, or before yielding when a bounded task first meets a trigger.
- Per worktree, HANDOFF holds the one gate-holding task; `docs/agent/parked/<task-key>.md` holds parked tasks.
  Any HANDOFF whose `State:` is not exactly `closed` or `completed` holds the worktree write gate. Only its named
  writer may mutate that worktree; every other session is read-only there or uses a separate worktree.
- The writer changes only by closing, parking, or explicit user handoff. A crashed/absent writer is never reclaimed
  from recency. The user's resolution of that task's pending decision authorizes the resuming session to claim it;
  unrelated requests do not. Closing is invalid while the task still has uncommitted edits in the worktree.
- Root/cross-repo work uses root HANDOFF; component work uses the nearest component HANDOFF. A worktree never has
  two gate-holding handoffs or two writers.

## 2. Session start, claim, and resume

- Before the first mutation, read root and affected-component HANDOFF and parked records. In a spawned worktree,
  also inspect the primary checkout's root and affected-component parked guards. Announce each gate task/state/
  writer and parked key/state once.
- Claim only if no unclosed HANDOFF exists anywhere in the worktree (`scripts/agent_worktree.sh list`) and the tree
  is clean or every dirty path belongs to the task being resumed. Claims are check-then-verify: re-read HANDOFF or
  the changed RUNTIME row after writing it; if writer/owner differs, back off and announce the conflict.
- HANDOFF, parked records, and runtime claims are checkout-local and are not copied by Git or `.worktreeinclude`.
  Resume in that checkout or perform an explicit handoff. Transfer or close all user gates, premise guards, and
  runtime obligations before archiving/deleting a task or worktree.

## 3. Parking and closing

- Parking is one step: write `docs/agent/parked/<task-key>.md` and delete HANDOFF. It requires no uncommitted task
  edits (commit them on an authorized `task/` branch or have none) and released runtime claims. Only a
  `monitoring` parked task may retain claims. Otherwise the task remains in HANDOFF and keeps the gate.
- A parked record never gates the worktree, but its named pending decision and guarded premises are immutable in
  every worktree until the user resolves them. Resume by moving it into a free HANDOFF and deleting the parked
  record in the same step. If the user's answer completes it, report the result and delete the parked record.

## 4. Parallel worktrees and Git

- Reviewers/helpers are read-only. Parallel mutating tasks use `scripts/agent_worktree.sh spawn <task-key>`, which
  creates `../agent-invest-worktrees/<task-key>` on `task/<task-key>`, copies `.worktreeinclude`, and surfaces the
  primary checkout's parked guards.
- Spawning authorizes local commits only on that task branch. Push, merge into `main`, branch deletion, and history
  rewrite still require explicit user authorization; the primary writer or user performs the merge.
- `reap` requires a clean worktree, no unclosed handoff, no unresolved/untransferred parked record, and no retained
  runtime claim.

## 5. Shared runtime claims

- Worktrees do not isolate PostgreSQL writes, AgentSSD mutation, or worker/launchd control. Each has one owner in
  the primary checkout's gitignored `docs/agent/RUNTIME.md` (the first entry of `git worktree list`). Read it before
  a task may touch those resources; claim before mutation and release on close or park.
- RUNTIME claim/release edits are coordination state and exempt from the primary worktree write gate. Never claim
  over a non-default owner: surface it, including a parked monitoring owner, and obtain user confirmation. The
  resident launchd KeepAlive job is the worker's steady-state owner when no task claims it.

## 6. Record schemas and history

- HANDOFF is at most 80 lines and contains only: task key/title, scope, state, authority, user intent/acceptance,
  authorization boundary, research/decision evidence, next action, blockers, worktree/branch/base, changed paths,
  latest current-session validation/review, runtime claims, writer, and updated time.
- A parked record is at most 40 lines and contains only: key/title, state, pending decision/re-entry condition,
  guarded premises, retained runtime claims, next action, branch/base, brief origin note, and updated time.
- Long-lived facts belong in tracked contracts/docs. Never put secrets, chat transcripts, duplicated repository
  facts, or volatile ops snapshots in task records.
- Legacy `Prompt/Plan/Status/Documentation/Implement/code_review`, `archive/`, and `notes/` are read-only history,
  never current authority. Do not update/recreate them, or delete ignored legacy state without explicit approval.
