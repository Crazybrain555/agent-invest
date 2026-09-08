# Agent coordination and shared-runtime workflow

This is a project convention, not a Codex requirement or a backup system. Use native task history/resume
for routine continuation and native worktree/Handoff for task and Git transfer. Read this file when changing
coordination policy, handling an existing HANDOFF/parked record, transferring work, or touching shared runtime.

## When a project record is needed

- Keep an existing active record until its obligations are completed or explicitly transferred. Create a
  gitignored docs/agent/HANDOFF.md only for cross-tool ownership/acceptance handoff, shared-runtime obligations,
  a consequential decision or high-risk external action whose state must survive a pause, or an explicit user
  request. A complex design can use an existing plan/receipt; do not create another state system for it.
- Ordinary follow-ups, intact-context resume, small clarifications and routine uncommitted edits do not create
  HANDOFF by themselves. Material contract/migration work still needs its governing decisions and validation,
  but those can live in the applicable tracked design or existing receipt rather than a duplicate control file.
- Use one gate-holding HANDOFF per worktree: root for cross-repository work, nearest component for leaf work.
  Existing docs/agent/parked/<task-key>.md records retain their pending-decision and premise protections.

## Ownership and recovery

- At task entry/before the first write, inspect existing root and affected-component HANDOFF/parked records;
  in a worktree also inspect the primary checkout's corresponding parked guards. Do not create missing records
  just to make this check pass. Report material ownership conflicts, not an unchanged gate on every turn.
- A HANDOFF whose State is not closed or completed holds the worktree write gate. Only its named writer may
  mutate that checkout, except the RUNTIME coordination writes below or an explicit user-authorized scoped
  exception. A scoped exception does not transfer the writer or authorize unrelated files/resources.
- Identify the writer by host, native task/session ID and worktree path. Neither a model name, /root label,
  matching timestamp nor absence of activity proves ownership. Resolve legacy identity through task evidence
  or explicit user handoff; never reclaim from age alone.
- Before claiming, establish that no active gate conflicts and the tree is clean or every dirty path belongs
  to the resumed task. Re-read a changed ownership record and stop conflicting writes if it differs. This is
  a coordination convention, not an atomic lock; do not use it to justify concurrent shared-resource writers.
- Recover when history is missing, unclear or conflicts with current state. Re-read relevant instructions and
  HANDOFF, inspect HEAD/status/diff and only the external facts needed by the next action. With intact context,
  continue normally. User instructions determine scope; receipts preserve decisions; observed state establishes
  what actually happened. A summary alone is not proof that an action remains pending.
- Completed work stays closed unless its recorded invalidation condition holds, fresh evidence contradicts it,
  or the user requests a repeat. A conflict blocks the dependent side effect, not independently authorized work.

## What HANDOFF contains

- Keep a readable current summary: task/goal, state and phase, writer/worktree/branch, authorization and protected
  paths, consequential decisions, concise completed receipts with evidence links, next actions, blockers,
  current validation limits, retained runtime claims, delivery/release condition and updated time.
- Update on material changes to ownership, authorization, decisions, external-action outcomes, validation or
  the actual next step. Do not append a transcript or rewrite it for every tool call, wait or normal follow-up.
- Link existing plans and receipts instead of copying historical experiments, manifests or old reviews. Mark
  historical results with their source identity and invalidation boundary; they are not current gates.
  Preserve unique evidence when compacting a record, but do not create a new file per historical item.
- No hard line/byte quota: readability is the goal, not long compressed lines or a new stopping condition.
  Keep secrets and raw/source datasets out. Long-lived rules belong in tracked contracts, not task history.
- Completed and pending sections must be disjoint. Parked records need only their decision/re-entry condition,
  guarded premises, writer/origin, next action, retained claims and evidence references.
- Legacy Prompt/Plan/Status/Documentation/Implement/code_review, archive/ and notes/ remain read-only history,
  not active policy. Do not recreate them or delete ignored historical evidence without authorization.

## Delivery, parking and transfer

- Deliver completed authorized work and applicable checks even if no Git action was requested. Report a required
  verification gap explicitly; optional cleanup/tests are not delivery gates. Do not ask to commit just to close
  a record. If HANDOFF applies and delivered edits remain uncommitted, use State: delivered and retain ownership.
- Closing/completing or parking requires no uncommitted task edits and released/transferred runtime obligations.
  Only an explicitly monitoring parked task may retain claims. Otherwise leave the gate in place; do not invent
  commit or cleanup authority. Park by recording the task under parked/ and removing HANDOFF in the same
  authorized operation. Pending parked decisions/premises remain protected across worktrees until resolved.
- Other mutating tasks use isolated worktrees; reviewers are read-only. Prefer native worktree/Handoff with its
  own invocation/approval rules. The existing scripts/agent_worktree.sh spawn <task-key> is a compatibility path
  only when native facilities cannot cover the requested operation; its task/<task-key> branch is script-specific.
- Git isolation does not transfer project ownership or external resource obligations. Inspect destination
  ownership and primary parked guards, and explicitly transfer any actual obligations. Do not copy HANDOFF,
  parked or RUNTIME ownership with .worktreeinclude. Use retained/permanent worktrees for outstanding ignored
  records; neither a native snapshot nor automatic cleanup proves those obligations were transferred.
- Commit, push, merge, branch deletion, history rewrite and destructive cleanup retain their authorization
  requirements. Before archiving/deleting/reaping a worktree, verify it is clean, has no active gate, unresolved
  or untransferred parked decision, or retained runtime claim.

## Shared runtime

- PostgreSQL, AgentSSD, GPU/MinerU queues and worker/service control are shared across worktrees. Their one
  owner is recorded in the primary checkout's gitignored docs/agent/RUNTIME.md. Read it before operations on
  these resources; claim before authorized mutation or workload submission, release on close/park.
- Non-mutating status/log/read-only SQL checks do not acquire ownership or authorize writes. Respect existing
  access restrictions and distinguish a read-only query from a probe that submits work or alters runtime.
- RUNTIME claim/release updates are coordination state exempt from the primary write gate, not permission to
  operate the resource. Never replace a non-default owner without explicit user transfer; re-read the row after
  changing it. Worker steady-state ownership remains its resident launchd KeepAlive job when no task claims it.
- Keep the existing coarse claims unless actual isolation and reliable coordination are established. Distinct
  keys/ports/schemas are not proof: parent/child paths and aliases can overlap, APIs can share one GPU/queue,
  and PG operations can cross schemas or affect the database. Do not introduce a new lock service for this cleanup.

## External review

- Advice is not an exact-current implementation verdict. For a user-requested external repository/code verdict,
  follow the applicable scope and packet rules in
  services/disclosure_anchor/docs/implementation/checks/independent-review-guide.md.
- Keep the sent target/dependencies, evidence, packet and question frozen; only independent authorized work that
  cannot affect them may proceed elsewhere. Reproduce findings before implementing. Record the final verdict,
  exact source/packet identity, accepted/rejected findings and invalidation boundary in the existing receipt;
  HANDOFF links it rather than duplicating it. Only already-authorized cleanup follows; retention does not block delivery.

## Basis

- [AGENTS discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md) and
  [native worktrees/Handoff](https://learn.chatgpt.com/docs/environments/git-worktrees).
- [Cookbook agents and plans](https://developers.openai.com/cookbook/examples/codex/iterating-development-workflows-with-codex#agents-and-plans)
  treats planning files as optional conventions, not required Codex machinery.
- [GPT-6 instruction guidance](https://developers.openai.com/api/docs/guides/latest-model#gpt-6-astra-instruction-following)
  motivates explicit priorities and removal of conflicting guidance, not removal of safety boundaries.

Checked 2026-09-08. Native history/Git transfer does not establish ownership of shared runtime.
