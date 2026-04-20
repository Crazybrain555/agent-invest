# Quanti Agent Operating Contract

This repository is a **skills-only company research and valuation workspace**. It is not the full AIQuantLab production codebase and it is not a generic Python app. Optimize all agent work for evidence traceability, small reviewable changes, and recoverable long-horizon execution.

Users may prompt naturally with requirements and goals. Do not require a rigid prompt template. Route the request into one of the task modes below and use `docs/agent/` as durable memory when the work is long-running.

Keep this file concise. Put detailed execution loops in `docs/agent/Implement.md`, milestone state in `docs/agent/Plan.md`, and short resume state in `docs/agent/Status.md`.

## 1. Source-of-truth hierarchy

When files disagree, prefer current filesystem truth and runnable commands over prose. Use this order:

1. Actual files, commands, and code behavior in this checkout.
2. `docs/agent/Status.md` for the current durable-task state.
3. `docs/agent/Plan.md` for the active milestone and validation criteria.
4. `docs/skills/README.md` for actual skill implementation status.
5. `docs/skills/MASTER_PLAN.md` and `docs/skills/specs/` for target architecture and per-skill contracts.
6. `README.md` for human-facing overview.
7. `CLAUDE.md` for Claude Code-specific behavior only; do not treat it as Codex policy unless the user asks.

If a stable rule changes, update the checked-in docs instead of relying on chat memory.

## 2. Repository reality

Primary working areas:

- `.agents/skills/company_research/`: repo-local company research skills.
- `.agents/skills/company_research/collect-company-facts/`: currently the only in-repo skill runner that is actually present.
- `company_research_runtime/`: shared runtime utilities for atomic writes, paths, evidence ledgers, artifact state, hashing, and run logs. Do not delete or bypass these helpers casually.
- `docs/skills/MASTER_PLAN.md`: target architecture for the 9-skill company research chain.
- `docs/skills/README.md`: implementation-status index. Trust this when deciding what exists today.
- `docs/skills/specs/`: per-skill specifications. Many specs are design documents, not implemented runners.
- `docs/agent/`: durable Codex project memory and execution state.
- `.codex/config.toml`: project-scoped Codex/MCP configuration. It is machine-specific and may be ignored by git.

Do not assume `src/`, schedulers, web apps, notebooks, databases, or a full production pipeline exists unless you find those files.

## 3. Task-mode router

Choose one primary mode for each request. If a request spans modes, use the safer earlier mode: plan before executing, resume before guessing, and update durable docs before making risky changes.

### Mode 1 — Quick task

Use for simple answers, read-only inspection, small bounded edits, or docs-only changes that are not risky and do not need multi-step verification.

Protocol:

1. Inspect only the minimum files needed.
2. Make the smallest coherent change, or report findings directly.
3. Run only the relevant lightweight check for the change. Docs-only or harness-only edits do not require runtime validation unless runtime code also changed.
4. If the task expands into multi-file, architectural, risky, ambiguous, or long-running work, switch to Mode 2 before continuing.

### Mode 2 — Plan or replan durable work

Use when the task is new, has no approved plan, changes scope, invalidates the current plan, is ambiguous, is architectural, touches multiple files, is risky, may span more than one session, or needs repeated verification.

Protocol:

1. Read `docs/agent/Status.md` and `docs/agent/Prompt.md` if present.
2. Inspect only enough repository context to draft a correct plan.
3. Create or update:
   - `docs/agent/Prompt.md` for goals, non-goals, constraints, deliverables, and done criteria.
   - `docs/agent/Plan.md` for milestones, scope, acceptance criteria, validation commands, risks, and current milestone.
   - `docs/agent/Status.md` for current state and next action.
4. Do not edit product/runtime code in this mode unless the user explicitly asked to plan and implement in the same turn.
5. Stop with a concise plan summary and ask for approval only when the next step would be a material implementation change or an unresolved product decision.

### Mode 3 — Execute an approved durable milestone

Use when `Plan.md` already contains an active milestone and the user asks to continue, implement, run the next step, fix the active item, or execute the approved plan.

Protocol:

1. Read `docs/agent/Status.md`.
2. Read the active milestone in `docs/agent/Plan.md`.
3. Read `docs/agent/Implement.md`.
4. Make only the smallest change needed for that milestone.
5. Run the milestone validation. If validation fails, repair the current milestone or mark it blocked before moving on.
6. Update `docs/agent/Status.md` before responding.
7. Update `docs/agent/Documentation.md` when decisions, commands, behavior, validation history, or known issues changed.
8. Do not silently move to the next milestone unless the user explicitly asked for multi-milestone execution.

### Mode 4 — Resume durable work

Use after interruption, compaction, a new session, a stalled run, or when the user says “resume”, “continue from status”, “pick this back up”, or similar.

Protocol:

1. Read `docs/agent/Status.md` first.
2. Read the active milestone in `docs/agent/Plan.md`.
3. Read `docs/agent/Implement.md`.
4. Do not rely on previous chat context unless it is reflected in repository files.
5. Briefly state the current milestone, next action, blockers, and latest validation.
6. If the next action is safe and unambiguous, continue using Mode 3. If not, update `Status.md` with the blocker or switch to Mode 2 to replan.

### Mode 5 — Harness or agent-policy maintenance

Use when changing `.codex/config.toml`, `AGENTS.md`, `docs/agent/*`, future hooks, future skills, or other agent-facing policy/workflow files.

Protocol:

1. Keep changes focused on agent behavior; do not modify product/runtime code unless the user explicitly asks.
2. Preserve the 5-mode router unless there is a clear reason to change it.
3. After config edits, validate `.codex/config.toml` with a TOML parser.
4. Validate that `AGENTS.md`, `Plan.md`, `Status.md`, and `Implement.md` agree on workflow names and paths.
5. Runtime compile/help checks are required only if runtime code also changed.
6. Update `docs/agent/Status.md` and `docs/agent/Documentation.md` with the policy change and validation evidence.

## 4. Context discipline

- Use `rg`, path-limited searches, and small file slices before opening large files.
- Prefer `docs/skills/README.md` before scanning every spec.
- Prefer the active milestone in `Plan.md` before reading unrelated project history.
- Do not read large artifact directories under `/home/help/mcp/work/company_research/` unless the task explicitly requires artifact inspection.
- When researching a ticker run, read `result.yaml`, `needs.yaml`, `artifacts_state.yaml`, and current indexes before raw evidence files.
- Do not expand scope silently. If the task grows, switch to Mode 2 and update `Plan.md` and `Status.md` before continuing.

## 5. Company research artifact invariants

Production research outputs do **not** belong in the repository. They belong under:

```text
${COMPANY_RESEARCH_ROOT:-/home/help/mcp/work/company_research}/company/{TICKER}/
```

Core invariants:

- `raw/` is append-only evidence. Do not delete or rewrite raw evidence unless the user explicitly asks and understands the consequence.
- `events/` is the future database-like event layer.
- `current/` is the query layer for latest promoted state.
- `runs/{run_id}/` is the immutable per-run audit record.
- A blocked skill writes `runs/{run_id}/needs.yaml`; it must not fabricate downstream data.
- Skills should write run outputs first, then promote successful or partial outputs atomically to `current/`.

## 6. Skill-chain reality and contracts

Target architecture is the 9-skill chain described in `docs/skills/MASTER_PLAN.md` and `docs/skills/README.md`, but the current checkout only has one implemented runner:

```bash
python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL
```

The current runner still requires a valid `company/{TICKER}/company.yaml` with `cik` under `COMPANY_RESEARCH_ROOT`; if that hard dependency is missing, a `blocked` result and `needs.yaml` are expected.

Skill contract rules:

- A skill is parameter + required input artifacts -> required output artifacts + `meta.yaml`/`result.yaml`/optional `needs.yaml`.
- Skills must not secretly call other skills. Missing hard dependencies become `blocked` with `needs.yaml`.
- Prefer deterministic transforms, explicit warnings, and structured missing-data records over silent fallback.
- Avoid broad `try/except` that hides data-quality problems.
- Keep evidence traceable through `evidence.jsonl`, artifact state, local paths, hashes, or source metadata.

## 7. Validation commands

Default lightweight validation after changing Python runtime or skill runner code:

```bash
python -m compileall company_research_runtime .agents/skills/company_research/collect-company-facts/scripts/run.py
python .agents/skills/company_research/collect-company-facts/scripts/run.py --help
```

Focused functional validation, only when `COMPANY_RESEARCH_ROOT` contains a valid `company/{TICKER}/company.yaml` with `cik`:

```bash
python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL --demo
```

If a validation command cannot run because of missing environment, credentials, packages, or company artifacts, record the exact reason and the exact command in `docs/agent/Status.md`.

## 8. Safe editing rules

- Never run destructive commands such as `git reset --hard`, mass deletes, filesystem wipes, or raw-artifact cleanup unless the user explicitly asks.
- Do not commit, push, or rewrite git history unless the user explicitly asks.
- Do not modify `.mcp.json`, secrets, local credentials, or machine-specific MCP paths unless the task is specifically about MCP configuration.
- Do not hard-code API keys. Use environment variables or Codex `env_http_headers` for HTTP MCP headers.
- Keep UTF-8 explicit for file/log I/O.
- Preserve existing behavior by default; gate behavior changes behind parameters or documented decisions when feasible.

## 9. Done means

For durable-workflow tasks, do not claim completion until:

- the active milestone acceptance criteria in `docs/agent/Plan.md` are satisfied or explicitly blocked,
- relevant validation commands have passed, or failures are documented with exact commands and reasons,
- `docs/agent/Status.md` reflects current state, latest validation, and next action,
- `docs/agent/Documentation.md` records important decisions, commands, or known issues,
- and the final response summarizes changed files, verification evidence, and remaining risks.
