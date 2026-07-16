# disclosure_anchor Agent Operating Contract

This service is the L1 disclosure/PDF path of the 投研预测引擎. It inherits the monorepo root `AGENTS.md`;
this file adds service-specific rules only. Agents apply `AGENTS.md` policy from the repository root toward
the working directory, so nearer files narrow local behavior rather than repeat parent rules.

## 1. Authority and local task state

Use separate authorities for separate questions:

1. **Normative semantics:** engine protocol
   `../../docs/reference/投研预测引擎顶层框架协议_v0.8.md`, then service contract
   `docs/architecture/service-purpose.md` and the matching architecture/contract checklist.
2. **What exists now:** current files, runnable commands, schemas, and observed behavior. A mismatch with a
   normative authority is drift to fix or explicitly revise, not an implicit contract override.
3. **Durable task state:** follow `../../docs/agent-workflow.md`; service work uses `docs/agent/HANDOFF.md`,
   cross-repo work uses `../../docs/agent/HANDOFF.md`, and never both as gate holders.
4. **Setup/config:** `docs/MCP_SETUP_GUIDE.md`, `.codex/config.toml`, `.mcp.json`, and environment templates,
   each only for its own surface.

HANDOFF/parked state is worktree-local and gitignored. Durable policy and product facts belong in tracked files,
never only in task state; legacy task files remain read-only history.

## 2. Service hard boundaries

1. **Layer scope:** this service registers, acquires, parses, units, publishes, and exposes disclosure assets.
   Do not implement L2 claim/evidence/forecast semantics here.
2. **Database boundary:** write only `disclosure_core` / `disclosure_ops`; consumers read versioned
   `disclosure_public.*_v1` views, Filing API, change feed, or source references. Never create a private-table
   dependency across services.
3. **Storage boundary:** runtime files live under
   `/Volumes/AgentSSD/agent_system/services/disclosure_anchor/`; shared/runtime/PG data never belongs in Git.
   Paths come from settings/path builders, not hard-coded literals in runtime code.
4. **Provenance:** preserve source access, hashes, immutable raw artifacts, document/run lineage, and stable
   public identifiers. Do not publish unverifiable synthetic defaults.
5. **Contracts:** public-view columns, exported contracts, error shapes, CLI/API commands, and migration
   semantics change together with their tests/specs. Applied migrations are append-only; do not edit history.
6. **Tests:** use `unittest`, not pytest. DB-touching tests clean their own rows and must not mutate sibling
   service schemas. Credentialed/provider tests are explicit opt-in or skip with a concrete reason.
7. **Failure visibility:** unexpected parser, DB, migration, artifact, command, or policy failures fail loudly.
   Catch only specific expected errors when the code can recover, quarantine, persist structured failure, or
   re-raise with useful context.
8. **Secrets:** real credentials enter only via environment variables or private user-level config; tracked
   files, fixtures, and examples carry placeholders only.

Nearest source/test `AGENTS.md` files define directory maps and additional local rules. Keep those files short
and update them only when topology or a real hard boundary changes.

## 3. Repository map and operational facts

```text
src/disclosure_anchor/domain/          pure domain entities, enums, errors, outbox factories
src/disclosure_anchor/application/     ports, services, use cases, worker orchestration
src/disclosure_anchor/adapters/        DB, storage, parser, and provider implementations
src/disclosure_anchor/api/             public/admin API composition and schemas
contracts/                             generated public artifacts; do not hand-edit
config/                                tracked watchlist/policy/rule inputs
tests/                                 unit, contract, sample-corpus, integration, opt-in smoke
docs/architecture/                     service semantic and data-contract authorities
docs/implementation/                   roadmap, milestones, checks, and operational designs
scripts/                               deterministic maintenance/audit/launchd helpers
```

The worker is installed as a resident user launchd job by `scripts/install_launchd.sh` (`KeepAlive` + adaptive
`worker loop`). Code/config/env or loaded-rule changes require an explicit job restart and doctor check; there is
no periodic process restart that reloads them. Treat live scheduler/GPU/backlog values as operational evidence
that must be re-verified for an ops task, not as permanent prompt facts.

## 4. Validation and independent review

- Default deterministic gate: `make agent-check` (ruff, strict mypy, no-DB unittest, `git diff --check`).
- Run `make test` and required migration round trips for DB/migration behavior when local credentials and the
  shared test cluster are in scope; otherwise record the exact blocker.
- Before changing parser, unit-builder, publication, or retrieval behavior, use read-only DBHub/SQL against
  `disclosure_public.*_v1` when available: inspect the actual view schema, then compare active published units and
  cross-filing/issuer distributions with a source-identity replay of the current worktree. Published rows may have
  been built by older code, while old tests/fixtures may encode an earlier AI agent's assumption; neither is the
  semantic oracle by itself. Resolve the delta against the normative contract, raw artifact, and NormalizedIR,
  then add only the confirmed general invariant and its adjacent negative case to tests.
- The root pre-design research gate applies to every parser, unit-builder, publication, and retrieval behavior
  change, even when the problem looks simple, familiar, or covered by local tests. After inspecting the failing
  artifact and representative corpus, check the official MinerU contract and 1–2 relevant document systems such
  as Docling or Unstructured before designing the behavior change; difficulty changes only the depth. Announce
  the research question/tools and the
  adopted or rejected invariant. Only a design-neutral mechanical edit may skip the check, with the reason stated.
  Validate the result with source-identity replay.
  Every fix must name the failure family, invariant, and fail-closed boundary, then pass positive and negative
  examples across representative filing types and issuers. Never branch on document IDs, grow a phrase list from
  isolated samples, or copy another project's internals; a bounded provider/form schema is valid only when the
  source format supplies that contract.
- Changes involving disclosure inputs, parser outputs, archive storage, DB publication, APIs, or worker state
  use representative local samples when available. Synthetic-only validation needs a recorded exception; see
  `docs/implementation/checks/fixture-and-test-policy.md`.
- For policy/config changes, parse every changed TOML/JSON file with project-venv Python, validate any
  gate-holding `HANDOFF.md` and `docs/agent/parked/` records (fields, line budgets, single gate-holder per
  worktree) and documented commands, measure the applicable instruction-file chain against each tool's
  documented size limits, and run `git diff --check`.

Material runtime, public-contract, setup-command, agent-policy, or durable-workflow changes require the root §4
independent read-only review. Give the reviewer the request, acceptance criteria, policy, diff, affected files,
and validation evidence; findings remain candidates and only evidence-backed material items are fixed.

Completion means acceptance is met, relevant checks passed or exact blockers are recorded, and required
material review findings are resolved or explicitly deferred by the user. Update only the gate-holding
HANDOFF or the task's own parked record when one exists; otherwise report the result directly.
