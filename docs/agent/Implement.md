# Implement.md — Quanti Codex Execution Runbook

This file defines how Codex should execute durable-workflow tasks in this repository. It is a runbook, not a task plan. The task plan lives in `docs/agent/Plan.md`; task routing lives in `AGENTS.md`.

## Non-negotiable execution rules

- Treat `AGENTS.md` as the task-mode router and repository operating contract.
- Treat `docs/agent/Plan.md` as the source of truth for milestone order and acceptance criteria.
- Treat `docs/agent/Status.md` as the source of truth for current progress and resume state.
- Treat `docs/skills/README.md` as the source of truth for what is implemented today.
- Treat `docs/skills/MASTER_PLAN.md` and `docs/skills/specs/` as target architecture/specifications.
- Do not implement before the active milestone and validation criteria are clear.
- Do not expand scope silently. If scope changes, use Mode 2 from `AGENTS.md` to update `Plan.md` and `Status.md` first.
- Keep diffs scoped and reviewable.
- If validation fails, repair or explicitly block the current milestone before moving on.
- If blocked by missing environment, credentials, packages, or artifacts, record the blocker and exact next command in `Status.md`.

## Mode-specific runbook

### Mode 1 — Quick task

Use this for simple answers, read-only inspection, small bounded edits, or docs-only changes.

1. Identify the smallest file set needed.
2. Answer or edit directly.
3. Run a lightweight validation only if a change was made and a relevant command exists.
4. Do not update `Plan.md` or `Status.md` unless the user asks or the task becomes durable.
5. If scope expands, stop and switch to Mode 2.

### Mode 2 — Plan or replan durable work

Use this for new durable tasks, stale plans, scope changes, ambiguous tasks, or risky/multi-file work.

1. Read `docs/agent/Status.md`.
2. Read `docs/agent/Prompt.md` if task goals or standing constraints matter.
3. Inspect only enough repo context to avoid writing a false plan.
4. Update `Prompt.md` only when goals, non-goals, constraints, deliverables, or done criteria changed.
5. Update `Plan.md` with milestones, scope, acceptance criteria, validation commands, risks, and current milestone.
6. Update `Status.md` with the current milestone, next action, blockers, and whether implementation is waiting for user approval.
7. Do not edit product/runtime code unless the user explicitly asked to plan and implement in the same turn.

### Mode 3 — Execute an approved durable milestone

Use this when an active plan exists and the user asks to continue or implement.

1. Read `Status.md`.
2. Read the active milestone in `Plan.md`.
3. Confirm acceptance criteria and validation commands.
4. Identify the minimal relevant files.
5. Inspect code/docs with targeted search before opening broad files.
6. Make the smallest coherent implementation or documentation change.
7. Run milestone validation.
8. If validation fails, repair the current milestone before moving on. If blocked, document the blocker and exact next command.
9. Update `Status.md`.
10. Update `Documentation.md` if the run produced durable knowledge.
11. Stop after the active milestone unless the user explicitly requested multiple milestones.

### Mode 4 — Resume durable work

Use this after interruption, compaction, a new session, or when the user says to resume.

1. Read `Status.md` first.
2. Read the active milestone in `Plan.md`.
3. Read this runbook.
4. State current milestone, next action, blockers, and latest validation.
5. If the next action is safe and unambiguous, continue with Mode 3.
6. If the plan is stale or blocked, switch to Mode 2 and update the plan/status instead of guessing.

### Mode 5 — Harness or agent-policy maintenance

Use this for `.codex/config.toml`, `AGENTS.md`, `docs/agent/*`, future hooks, future skills, or other agent-facing policy/workflow files.

1. Keep changes focused on agent behavior.
2. Do not edit runtime/product code unless explicitly requested.
3. Preserve the five task modes unless the user explicitly wants to redesign routing.
4. Keep `AGENTS.md` concise; move detailed execution loops to this runbook instead of duplicating them.
5. Validate `.codex/config.toml` after config changes.
6. Validate that routing names and durable-doc paths agree across `AGENTS.md`, `Plan.md`, `Status.md`, and this file.
7. Runtime compile/help checks are only required if runtime code also changed.
8. Update `Status.md` and `Documentation.md` with the policy change and evidence.

## Code inspection order

Use this default order unless the active milestone says otherwise:

1. `docs/agent/Status.md`
2. active section of `docs/agent/Plan.md`
3. `docs/skills/README.md`
4. relevant `docs/skills/specs/<skill>.md`
5. relevant `.agents/skills/company_research/<skill>/SKILL.md`
6. relevant `.agents/skills/company_research/<skill>/scripts/run.py`
7. relevant helpers in `company_research_runtime/`
8. only then, related artifact files under `COMPANY_RESEARCH_ROOT`

## Skill implementation protocol

When implementing or changing a skill runner:

1. Re-read the relevant spec and current `SKILL.md`.
2. Write down hard dependencies and expected outputs before editing.
3. Use `company_research_runtime` helpers for paths, atomic writes, evidence ledgers, artifact state, and run logs where practical.
4. Ensure missing hard dependencies return `blocked` and write `needs.yaml`.
5. Ensure every run writes `meta.yaml` and `result.yaml`.
6. Promote only validated outputs to `current/`.
7. Add or update documentation for the exact commands and expected artifacts.

## Bug-fix protocol

When fixing a bug:

1. Reproduce or identify the failing behavior.
2. Add or update a focused validation path when feasible.
3. Make the smallest fix.
4. Run focused validation.
5. Run broader validation if the change touches shared runtime helpers.
6. Record evidence in `Status.md` and `Documentation.md`.

## Validation protocol

Default lightweight checks after Python code changes:

```text
python -m compileall company_research_runtime .agents/skills/company_research/collect-company-facts/scripts/run.py
python .agents/skills/company_research/collect-company-facts/scripts/run.py --help
```

Functional checks require local artifacts and sometimes MCP credentials. If unavailable, do not fake success; record the blocker.

Optional focused check when `COMPANY_RESEARCH_ROOT/company/AAPL/company.yaml` exists with a valid `cik`:

```text
python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL --demo
```

## Documentation update protocol

Update `Status.md` whenever any of these changes:

- active milestone,
- next action,
- blocker,
- validation result,
- files changed,
- important decision.

Update `Documentation.md` when the change creates durable knowledge:

- how to run something,
- why a design decision was made,
- known issue or limitation,
- validation history worth preserving,
- artifact schema or path convention.

Keep `Status.md` short. Move long history to `Documentation.md`.

## Completion protocol

Before reporting a durable task as complete:

- Check every relevant milestone in `Plan.md`.
- Run required validation commands, or document exactly why they could not run.
- Update `Status.md`.
- Update `Documentation.md`.
- Final response must include:
  - changed files,
  - verification evidence,
  - blockers or residual risks,
  - next recommended action.
