# agent-invest repository contract

Keep this file limited to repository-wide invariants and routing. Component implementation and operational
details belong in the nearest component docs; current task/runtime state belongs in HANDOFF/RUNTIME records.

## Scope and authority

- The current user request defines scope and authorizes actions. Read-only work does not authorize writes;
  edits do not authorize commit, push, publication, remote mutation, service control, or destructive cleanup.
- Product semantics come from `docs/reference/投研预测引擎顶层框架协议_v0.8.md`, then the applicable L1
  plan and nearest component contract. Code, schemas, commands, and observations describe current state; a
  mismatch is drift to reconcile, not an implicit contract change.
- Preserve unrelated and user-owned changes. Ask before credentials, permissions, costly operations, shared
  runtime mutation, or a material scope expansion.
- Repository layout, component status, and planned services live in `README.md`.
- At session start and before the first mutation, inspect the root and affected-component HANDOFF/parked records.
  An unclosed HANDOFF is a write gate even when the requested edit looks small; only its named writer may mutate.

## Tool instruction loading

- **Codex:** the project instruction chain is assembled once at session start from the repository root to the
  initial working directory. A later `cd` does not rebuild it. Start with `codex --cd <target>` for leaf work;
  when one session spans sibling trees, explicitly read each nearer `AGENTS.md` before its first edit.
- **Claude Code:** ancestor `CLAUDE.md` files load at startup and descendant files load when Claude enters or
  reads that subtree. Sibling `CLAUDE.md` files are ordinary `@AGENTS.md` adapters. Use `/context` to diagnose
  loaded instructions; imports organize policy but do not reduce context use.
- Nearer instructions add local constraints; they do not cancel parent safety or authorization rules. Files
  under `docs/archive/pre-restart/` are frozen history and never active policy.

## Repository-wide hard boundaries

1. Use one PostgreSQL cluster and the `invest_engine` database. Components isolate through schemas and
   least-privilege roles, not additional databases.
2. A service writes only its owned schemas. Cross-service reads use versioned public views, explicit APIs,
   change feeds, or source references—never another service's private tables.
3. Reuse `packages/envelope_kernel` for shared `data_asset` envelopes, kind rules, `asset://` URIs, and exported
   schemas. Breaking shared or public contracts require a versioned change and synchronized consumers/tests/docs.
4. Applied migrations are append-only. Never rewrite an applied revision or silently reinterpret stored data.
5. PostgreSQL data, raw files, caches, models, and generated artifacts live under `/Volumes/AgentSSD/agent_system/`
   and never enter Git. Raw/source identity, hashes, provider/parser provenance, and processing lineage remain
   reviewable; missing values are not invented.
6. Credentials come only from environment variables or private user configuration. Tracked files, fixtures,
   examples, logs, and review packets contain placeholders or redacted values.
7. Default tests are deterministic `unittest` without a live database. DB tests never mutate shared production
   schemas/data; components that provide a scratch runner must use it and must never fall back to production.
8. Unexpected parser, database, migration, artifact, command, or policy failures stay visible. Catch only errors
   that can be specifically recovered, quarantined, persisted with context, or re-raised.

## Conditional workflows

- Before the first mutation, read `docs/agent-workflow.md` in full when work crosses sessions, changes
  architecture/public contracts/migration or data boundaries, touches shared runtime, resumes durable state,
  has material unknowns, or pauses for a decision. It owns HANDOFF, parked-task, write-gate, worktree, recovery,
  and RUNTIME-claim procedures; do not duplicate them in component instructions.
- After compaction/resume or when history is incomplete, re-read the applicable instructions and HANDOFF, then
  reconcile the current request with Git/worktree truth and the narrow external state required by the next action.
  Do not repeat an action merely because a conversation summary lists it as pending.
- Read `docs/agent-research-workflow.md` in full when behavior depends on an external mechanism, has material
  unresolved alternatives, or a nearer contract/user requires research. Local contracts and representative
  cases come first; external evidence cannot silently revise product semantics.
- For material policy, public-contract, runtime, or validation-command changes, use an independent read-only
  reviewer after implementation. The disclosure service packet format is in
  `services/disclosure_anchor/docs/implementation/checks/independent-review-guide.md`; reviewer findings are
  claims to verify, not automatic edits.

## Validation

- `make agent-check` is the default repository gate; use the nearest component's documented integration, live,
  migration, provider, or smoke gate only when that boundary and environment are in scope.
- For policy/document changes, also run `git diff --check`, verify referenced paths and commands, and parse any
  changed structured configuration. Report only checks actually run and exact blockers.
