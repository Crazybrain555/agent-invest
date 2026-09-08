# agent-invest repository contract

Keep this file limited to repository-wide invariants and routing. Component implementation and operational
details belong in the nearest component docs; current task/runtime state belongs in HANDOFF/RUNTIME records.

## Scope and authority

- The current user request defines scope. Explicit authorization and its limits persist within the same task and
  authorized continuations unless the user changes them; unrelated tasks do not inherit them. Read-only work does
  not authorize writes; edits do not authorize commit, push, publication, remote mutation, service control, or
  destructive cleanup.
- Product semantics come from `docs/reference/投研预测引擎顶层框架协议_v0.8.md`, then the applicable L1
  plan and nearest component contract. Code, schemas, commands, and observations describe current state; a
  mismatch is drift to reconcile, not an implicit contract change.
- Preserve unrelated and user-owned changes. Credentials, permissions, costly operations, shared runtime mutation,
  and material scope expansion require user authorization. Check the existing authorization before asking again;
  ask again only when the proposed action exceeds its limits, such as a different protected target, broader data
  access, spending beyond the approved budget, or materially greater risk.
- For authorized edits, resolve routine implementation choices from the contracts and current evidence, complete
  the changes and applicable checks, and deliver the result. Ask only for consequential missing input or required
  authorization. Complete independent authorized work before pausing a dependent action; optional improvements
  do not become new completion gates. Optimize correctness, completeness, throughput and maintainability against
  the user's acceptance criteria; choose the justified scope of change rather than a minimum-diff default.
- Repository layout, component status, and planned services live in `README.md`.
- At session start and before the first mutation, inspect the root and affected-component HANDOFF/parked records.
  An unclosed HANDOFF protects its writer's checkout; RUNTIME coordination and explicit user-scoped exceptions
  follow docs/agent-workflow.md. Neither exception transfers general ownership.

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
5. Service runtime data, raw files, caches, models, and generated runtime artifacts live under
   `/Volumes/AgentSSD/agent_system/` and never enter Git. Canonical generated contract/schema files follow their
   package's tracked-export rules; sanitized external-review packets follow the review guide's explicit directory.
   These exceptions do not permit copying runtime datasets or secrets into Git or review packets. Raw/source
   identity, hashes, provider/parser provenance, and processing lineage remain reviewable; missing values are not invented.
6. Credentials come only from environment variables or private user configuration. Tracked files, fixtures,
   examples, logs, and review packets contain placeholders or redacted values.
7. Default tests are deterministic `unittest` without a live database. DB tests never mutate shared production
   schemas/data; components that provide a scratch runner must use it and must never fall back to production.
8. Unexpected parser, database, migration, artifact, command, or policy failures stay visible. Catch only errors
   that can be specifically recovered, quarantined, persisted with context, or re-raised.

## Conditional workflows

- Read docs/agent-workflow.md in full for coordination-policy changes, existing HANDOFF/parked obligations,
  cross-tool/worktree handoff or shared-runtime operations. It owns the project-specific protections; ordinary
  progress and continuation use native task history. Do not create a control file merely because work is complex
  or spans turns, and do not duplicate the workflow in component instructions.
- Recover from compaction/resume only when history is incomplete, unclear or conflicts with current state:
  reconcile applicable instructions/HANDOFF, Git truth and the narrow external facts needed by the next action.
  With intact context, continue; do not repeat work solely because a summary lists it as pending.
- Read `docs/agent-research-workflow.md` in full when behavior depends on an external mechanism, has material
  unresolved alternatives, or a nearer contract/user requires research. Local contracts and representative
  cases come first; external evidence cannot silently revise product semantics.
- For material policy, public-contract, runtime, or validation-command changes, use an independent read-only
  reviewer after implementation. A local read-only reviewer is sufficient unless the user requests an external
  review. Select checks for the changed boundary; policy review does not require unrelated production SQL/replay.
  The disclosure review guide and external packet format are in
  `services/disclosure_anchor/docs/implementation/checks/independent-review-guide.md`; findings are claims to
  verify, not automatic edits or authorization for external disclosure.

## Validation

- `make agent-check` is the default repository gate; use the nearest component's documented integration, live,
  migration, provider, or smoke gate only when that boundary and environment are in scope.
- For policy/document changes, also run `git diff --check`, verify referenced paths and commands, and parse any
  changed structured configuration. Report only checks actually run and exact blockers.
