# Status.md — Quanti Durable Current State

Last updated: 2026-04-21 Asia/Shanghai
Updated by: Codex

Keep this file short. It is the first file to read when continuing durable work. Long history belongs in `docs/agent/Documentation.md`.

## Current task identity

Task: Stabilize Codex-native harness and harden current Quanti skill runner.

Task relationship rule:
If the user says "continue", "next", "resume", or points at the current milestone, continue this task. If the user introduces a materially new goal, initialize a new durable task by updating `Prompt.md`, `Plan.md`, and this file after recording any useful old-task handoff in `Documentation.md`.

## Current state

Current milestone: M2 — Harden `collect-company-facts`

Status: `ready_to_start`

One-sentence summary:
The harness v4 Merge Keep State migration is complete: the three-mode router, task-relationship handling, and explicit review gate are in place, M1 remains complete, and the next product milestone is still M2 runner hardening.

Next action:
Begin M2 by inspecting `collect-company-facts/SKILL.md`, `scripts/run.py`, and relevant `company_research_runtime/` helpers.

## Recently completed

- Replaced the old active router with the v4 three-mode router:
  1. Quick standalone task.
  2. Durable workflow.
  3. Harness or agent-policy maintenance.
- Moved continuation/recovery into durable-file reading instead of treating it as a top-level mode.
- Added task-relationship handling for new durable tasks, current-task revisions, and current-task execution.
- Added an independent review gate through `docs/agent/code_review.md` and `.codex/agents/quanti_reviewer.toml`.
- Preserved the existing repo-reality fixes: `README.md` and `CLAUDE.md` already point to `docs/agent/*`, and `CONTINUITY.md` remains retired from the active workflow.

## Current blockers

- `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` still fails locally with `ModuleNotFoundError: No module named 'yaml'`.
- M2 still needs an explicit decision on whether `--demo` should require `company.yaml.cik` or support a dependency-light demonstration path.

## Files changed in current harness task

- `AGENTS.md`
- `docs/agent/Prompt.md`
- `docs/agent/Plan.md`
- `docs/agent/Status.md`
- `docs/agent/Implement.md`
- `docs/agent/Documentation.md`
- `docs/agent/code_review.md`
- `.codex/agents/quanti_reviewer.toml`
- `docs/MCP_SETUP_GUIDE.md`

## Latest validation

| Command | Result | Notes |
| --- | --- | --- |
| `python - <<'PY' ... tomllib ... PY` for `.codex/agents/quanti_reviewer.toml` | pass | Reviewer agent config parses as TOML. |
| `rg -n 'Mode 1\|Mode 2\|Mode 3\|Branch A\|Branch B\|Branch C\|task relationship\|independent review gate' AGENTS.md docs/agent/Implement.md docs/agent/Plan.md docs/agent/code_review.md docs/agent/Status.md` | pass | Active router and review-gate structure are present across the durable files. |
| `legacy_router_pattern='5''-mode|five primary ''modes|Mode ''4|Mode ''5|Resume durable ''work|Plan or replan durable ''work|Execute an approved durable ''milestone'; rg -n "$legacy_router_pattern" AGENTS.md docs/agent/Prompt.md docs/agent/Plan.md docs/agent/Implement.md docs/agent/Documentation.md` | pass | No old router terminology remains active in current guidance. |
| `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` | fail | Local environment is missing `yaml` (`ModuleNotFoundError`), so M2 runtime validation is currently blocked on dependency setup. |
| Independent review via read-only `quanti_reviewer` | pass | Verdict: `pass`; confidence: `high`; no material findings for the harness migration. |

## Review state

Latest independent review: pass for the v4 Merge Keep State migration via the read-only `quanti_reviewer` subagent.

Accepted findings:
None. The reviewer reported no material issues.

## Continuation instructions

To continue this task:

1. Read root `AGENTS.md`.
2. Read this file.
3. Read the active milestone, `Progress`, and active working checklist in `docs/agent/Plan.md`.
4. Read `docs/agent/Implement.md`.
5. Continue with the next action above.

To start a new durable task:

1. Record any useful old-task handoff in `docs/agent/Documentation.md`.
2. Replace current-task sections of `Prompt.md`, `Plan.md`, and this file.
3. Draft the new plan before editing runtime code unless the user explicitly asks to plan and implement together.
