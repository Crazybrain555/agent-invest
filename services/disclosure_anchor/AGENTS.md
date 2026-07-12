# disclosure_anchor Agent Operating Contract

This service is the L1 disclosure/PDF path of the 投研预测引擎. It inherits the monorepo root `AGENTS.md`;
this file adds service-specific rules only. Applicable `AGENTS.md` files are loaded from the repository root
toward the working directory, so nearer files should narrow local behavior rather than repeat parent rules.

## 1. Authority and task state

Use separate authorities for separate questions:

1. **Normative semantics:** engine protocol
   `../../docs/reference/投研预测引擎顶层框架协议_v0.8.md`, then service contract
   `docs/architecture/service-purpose.md` and the matching architecture/contract checklist.
2. **What exists now:** current files, runnable commands, schemas, and observed behavior. A mismatch with a
   normative authority is drift to fix or explicitly revise, not an implicit contract override.
3. **Active task state:** when the task qualifies, `docs/agent/Status.md`, then `Plan.md`, then `Prompt.md`.
4. **Setup/config:** `docs/MCP_SETUP_GUIDE.md`, `.codex/config.toml`, `.mcp.json`, and environment templates,
   each only for its own surface.

`docs/agent/` is gitignored machine-local state. Archives and notes are evidence read on demand, never current
policy. Rules that must survive a clone belong in tracked `AGENTS.md` or product documentation.

Each task has one durable-state owner. A cross-repo task uses root `../../docs/agent/`; a service product task
uses this directory. Parallel tasks may coexist, but never mix their milestones or checklists in one task file.

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

Nearest source/test `AGENTS.md` files define directory maps and additional local rules. Keep those files short
and update them only when topology or a real hard boundary changes.

## 3. Work selection and authorization

Default to a bounded task:

- **Answer, inspect, diagnose, review, or plan:** read relevant material and report; do not implement unless the
  request also asks for a change.
- **Change, build, or fix:** make requested in-scope local edits and run relevant non-destructive checks without
  seeking extra confirmation.
- Ask before destructive actions, external writes, purchases, credential/permission changes, commits/pushes,
  or a material scope expansion.

Use a durable task only when at least one applies:

- the work is likely to cross sessions;
- it changes architecture, a public contract, a migration/data boundary, or a high-risk ops workflow;
- material requirements or feasibility are unknown;
- the user explicitly requests a durable plan; or
- the user asks to continue an active durable task.

Multiple files, several steps, or ordinary documentation/state updates do not by themselves make work durable.
An unrelated bounded request must not overwrite unfinished durable state. If the user supersedes a durable task,
archive its handoff before replacing the live files.

Agent-policy/config changes add a **policy validation overlay** to the chosen task; they are not a separate
self-triggering mode. Updating ordinary `Prompt.md`/`Plan.md`/`Status.md` progress is state maintenance, not a
policy change.

## 4. Research, planning, and progress

- Before choosing a materially new architecture, cross-service contract, dependency, storage/queue/scheduling
  mechanism, or agent workflow, compare 2–4 relevant implementations. Prefer the vendor's official docs for
  vendor/model behavior; use OSS comparisons for design trade-offs.
- Do not force this survey for read-only fact finding, execution of an approved design, a localized bug fix,
  configuration value changes, or state/doc synchronization.
- Before changing agent policy/workflow semantics, setup or dependency guidance, user-facing validation
  commands, public contracts, or artifact locations, record the boundary and acceptance check in the active
  plan when the task is durable. A plan does not authorize work beyond the user's request.
- When the user says continue/resume, execute the next safe in-scope checklist item instead of returning only a
  status summary. After two no-progress attempts on one item, record the failed direction and change a
  structural assumption, milestone slice, evidence source, or validation path before retrying.

## 5. Durable files

Use these roles only for a qualifying durable task:

- `Prompt.md`: user intent, scope, boundaries, and done criteria.
- `Plan.md`: active milestone, acceptance, checklist, discoveries, decisions, and outcome.
- `Status.md`: current task, next action, blockers, latest validation, and latest independent review.
- `Documentation.md`: compact decision/index/follow-up notes; long evidence goes in `notes/`.
- `Implement.md` and `code_review.md`: optional local execution aids; they cannot add mandatory policy absent
  from this tracked file.

Keep live content under: Status 120 lines, Plan 300, Documentation 200, and this `AGENTS.md` 220. At milestone
closure or before replacing a task, snapshot affected live files to
`docs/agent/archive/<File>-<YYYY-MM-DD>[-N].md`, never overwriting an archive, then rewrite rather than append.

Allowed `docs/agent/` top-level entries are `Prompt.md`, `Plan.md`, `Status.md`, `Implement.md`,
`Documentation.md`, `code_review.md`, `archive/`, and `notes/`.

## 6. Repository map and operational facts

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

The worker is installed as a user launchd job by `scripts/install_launchd.sh` (at load and every two hours).
Treat live scheduler/GPU/backlog values as operational evidence that must be re-verified for an ops task, not as
permanent prompt facts.

## 7. Validation and independent review

- Default deterministic gate: `make agent-check` (ruff, strict mypy, no-DB unittest, `git diff --check`).
- Run `make test` and required migration round trips for DB/migration behavior when local credentials and the
  shared test cluster are in scope; otherwise record the exact blocker.
- Changes involving disclosure inputs, parser outputs, archive storage, DB publication, APIs, or worker state
  use representative local samples when available. Synthetic-only validation needs a recorded exception; see
  `docs/implementation/checks/fixture-and-test-policy.md`.
- For policy/config changes, parse every changed TOML/JSON file with project-venv Python, verify active durable
  pointers/line budgets and documented commands, measure the applicable AGENTS chain against the configured
  project-doc limit, and run `git diff --check`.

Before completing a material change to runtime behavior, public contracts, setup/validation commands, agent
policy, or durable-workflow semantics, obtain an independent read-only review from an agent/user path that did
not implement the change. Give it the request, acceptance criteria, applicable policy, current diff, affected
files, and validation evidence; it must not edit or update state. Findings are candidates—fix only material,
evidence-backed items. Ordinary state progress and trivial factual/typographic edits do not trigger review.

Completion means acceptance is met, the active checklist/state is current, relevant checks passed or exact
blockers are recorded, and required material review findings are resolved or explicitly deferred by the user.
