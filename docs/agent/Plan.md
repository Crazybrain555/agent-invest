# Plan.md — Quanti Durable Execution Plan

This file is the source of truth for current long-horizon work. Keep it updated whenever scope, milestone order, acceptance criteria, validation, or blockers change.

## Task

Short name: Codex-native harness foundation for Quanti

Related specification:

- `docs/agent/Prompt.md`

## How to use this plan

- For a quick answer or small bounded edit, follow `AGENTS.md` Mode 1 and do not use this full plan unless the task expands.
- For a new durable task, stale plan, changed scope, or ambiguous work, follow Mode 2 and update this file before implementation.
- For approved implementation work, follow Mode 3 and execute only the active milestone.
- For a new session or interrupted work, follow Mode 4 and start from `docs/agent/Status.md`.
- For changes to Codex policy, this file, or other harness docs, follow Mode 5.
- Keep detailed step-by-step execution in `docs/agent/Implement.md`; keep `AGENTS.md` focused on routing and stable repository rules.

## Current milestone

Milestone: M1 — Repo reality reconciliation

Status: `ready_to_start`

Next action: Run a targeted doc-reality pass across `README.md`, `docs/skills/README.md`, `docs/skills/MASTER_PLAN.md`, `CLAUDE.md`, and the new durable workflow docs so Codex and human-facing docs agree on what is actually implemented today.

## Milestone overview

| ID | Milestone | Scope | Acceptance criteria | Validation | Status |
| --- | --- | --- | --- | --- | --- |
| M0 | Harness foundation | `.codex/config.toml`, `AGENTS.md`, `docs/agent/*` | Codex has concise repo policy, 5-mode router, durable state, runbook, and validation protocol | TOML parse; path/routing checks | completed |
| M1 | Repo reality reconciliation | `README.md`, `docs/skills/README.md`, `docs/skills/MASTER_PLAN.md`, maybe `CLAUDE.md` | Human docs and agent docs agree on current implemented assets | Manual doc review; path checks | ready_to_start |
| M2 | Harden `collect-company-facts` | skill definition + runner + runtime helpers | Runner behavior matches documented contract; blocked/demo behavior is explicit | `compileall`, `--help`, focused run when artifacts exist | not_started |
| M3 | Decide next first-class skill | specs + plan only, likely `company-foundation` or Skill 2 migration | A self-contained implementation plan exists before code edits | Plan review | not_started |
| M4 | Implement next runner in small slice | selected skill path + runtime helpers | New runner writes meta/result/needs and one useful artifact | compile/help/focused run | not_started |
| M5 | Add lightweight validation harness | scripts or docs, not external wrapper yet | Repeated checks are copy-pasteable and reliable | Run validation commands | not_started |

## Milestones

### M0 — Install Codex-native durable workflow foundation

Scope:

- `.codex/config.toml`
- `AGENTS.md`
- `docs/agent/Prompt.md`
- `docs/agent/Plan.md`
- `docs/agent/Status.md`
- `docs/agent/Implement.md`
- `docs/agent/Documentation.md`

Acceptance criteria:

- `AGENTS.md` clearly states this is a skills-only company research repo.
- `AGENTS.md` states that only `collect-company-facts` is currently implemented in-repo.
- `AGENTS.md` uses the 5-mode task router:
  1. Quick task.
  2. Plan or replan durable work.
  3. Execute an approved durable milestone.
  4. Resume durable work.
  5. Harness or agent-policy maintenance.
- `AGENTS.md` stays concise and defers detailed execution loops to `docs/agent/Implement.md`.
- `docs/agent/Status.md` can orient a fresh session in under one minute.
- `.codex/config.toml` contains session-level Codex behavior without trying to replace Codex base instructions.
- No API key is hard-coded in the Codex config.

Validation commands:

```text
python - <<'PY'
import pathlib
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
p = pathlib.Path('.codex/config.toml')
tomllib.loads(p.read_text(encoding='utf-8'))
print('config.toml parses')
PY

rg -n 'Mode 1|Mode 2|Mode 3|Mode 4|Mode 5' AGENTS.md
find docs/agent -maxdepth 1 -type f | sort
```

Expected evidence:

- Files exist.
- Config parses as TOML.
- `AGENTS.md` contains exactly the five primary modes above.
- User can run `/status` in Codex app/CLI and verify project root/config are what they expect.

Status notes:

- Applied in the live repository on 2026-04-20.
- Simplified task routing from many modes to five primary modes.
- Local MCP paths were preserved.
- `model_reasoning_effort` and `plan_mode_reasoning_effort` were raised to `xhigh`.
- `model_verbosity` was raised to `high`, which is the highest valid value for that key.

### M1 — Repo reality reconciliation

Scope:

- `README.md`
- `docs/skills/README.md`
- `docs/skills/MASTER_PLAN.md`
- optional: `CLAUDE.md`
- optional: retire or repurpose `CONTINUITY.md`

Acceptance criteria:

- All docs agree on actual implemented runner paths.
- Docs do not point to nonexistent `docs/MASTER_PLAN.md`; canonical path is `docs/skills/MASTER_PLAN.md`.
- Docs distinguish target 9-skill architecture from current implementation.
- `CONTINUITY.md` is either retired, replaced by `docs/agent/Status.md`, or explicitly marked Claude/session-specific.

Validation commands:

```text
find . -maxdepth 4 -type f \( -name '*.md' -o -name '*.py' \) | sort
rg -n "docs/MASTER_PLAN|extract-xbrl-timeseries|company-foundation/scripts/run.py|valuation-and-margin-of-safety/scripts/run.py|CONTINUITY" README.md AGENTS.md CLAUDE.md docs || true
```

Expected evidence:

- A short list of mismatches and proposed doc edits before editing.

Status notes:

- Not started.

### M2 — Harden `collect-company-facts`

Scope:

- `.agents/skills/company_research/collect-company-facts/SKILL.md`
- `.agents/skills/company_research/collect-company-facts/scripts/run.py`
- `company_research_runtime/`
- relevant docs under `docs/skills/`

Acceptance criteria:

- `SKILL.md` matches actual runner behavior.
- The runner clearly documents that `company.yaml.cik` is a hard dependency, including for demo mode unless changed intentionally.
- Missing dependencies produce `blocked` and `needs.yaml`.
- Outputs are written under `COMPANY_RESEARCH_ROOT`, not the repo.
- Validation commands are documented.

Validation commands:

```text
python -m compileall company_research_runtime .agents/skills/company_research/collect-company-facts/scripts/run.py
python .agents/skills/company_research/collect-company-facts/scripts/run.py --help
```

Optional functional validation when `COMPANY_RESEARCH_ROOT/company/AAPL/company.yaml` exists with `cik`:

```text
python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL --demo
```

Status notes:

- Not started.

### M3 — Decide next first-class skill

Scope:

- `docs/skills/README.md`
- `docs/skills/specs/skill1-company-foundation.md`
- `docs/skills/specs/skill2-sec-ingest-and-materialize-events.md`
- `docs/skills/specs/skill3-xbrl-parse-financial-report-events.md`
- `docs/agent/Plan.md`

Acceptance criteria:

- The next implementation target is chosen explicitly.
- The plan states why that target unlocks downstream work.
- The plan identifies required inputs, outputs, status codes, and validation.
- No code is written before the plan is accepted or the user asks Codex to proceed.

Validation commands:

```text
rg -n "Hard 依赖|输出|blocked 条件|Definition of Done|参考实现" docs/skills/specs/skill1-company-foundation.md docs/skills/specs/skill2-sec-ingest-and-materialize-events.md docs/skills/specs/skill3-xbrl-parse-financial-report-events.md
```

Status notes:

- Not started.

### M4 — Implement next runner in small slice

Scope:

- TBD after M3.

Acceptance criteria:

- New code uses `company_research_runtime` helpers where appropriate.
- Runner has `--help`, `--as-of`, `--demo` or a documented reason not to support demo.
- Runner writes `meta.yaml`, `result.yaml`, and `needs.yaml` when blocked.
- Runner supports a minimal deterministic validation path.

Validation commands:

```text
python -m compileall company_research_runtime .agents/skills/company_research/<selected-skill>/scripts/run.py
python .agents/skills/company_research/<selected-skill>/scripts/run.py --help
```

Status notes:

- Not started.

### M5 — Add lightweight validation harness

Scope:

- A simple script or documented command group, only after repeated checks are known.
- Avoid a large external wrapper in this phase.

Acceptance criteria:

- Common validation can be run with one documented command.
- The command fails loudly on broken imports or runner CLI regressions.
- It does not require production credentials for lightweight checks.

Validation commands:

```text
# TBD after M2/M4 patterns are stable.
```

Status notes:

- Not started.

## Architecture notes

The durable workflow follows the native Codex long-horizon pattern:

- `Prompt.md` freezes goals and non-goals.
- `Plan.md` breaks work into milestones with acceptance criteria and validation.
- `Implement.md` defines how Codex should execute the plan.
- `Documentation.md` records audit history, decisions, run instructions, and known issues.
- `Status.md` is an extra short resume file for this repository so new sessions can orient quickly.

This is deliberately not an external orchestrator. It is a file-based harness for Codex app/CLI behavior.

## Risk register

| Risk | Why it matters | Mitigation | Status |
| --- | --- | --- | --- |
| Specs imply runners that do not exist | Codex may hallucinate commands or implementation status | Trust `docs/skills/README.md` and filesystem truth | active |
| Long docs overwhelm context | Codex may read too much and drift | Start from `Status.md`, then active milestone only | active |
| Machine-specific MCP config breaks elsewhere | `.codex/config.toml` uses absolute local paths | Keep config machine-local; use env vars for secrets | active |
| Demo path requires `company.yaml` | A nominal demo can block if CIK is missing | Document current behavior or change intentionally in M2 | active |
| `CONTINUITY.md` conflicts with new durable state | Multiple state files confuse future sessions | Decide in M1 whether to retire or repurpose it | active |

## Decision log

| Date | Decision | Rationale | Consequences |
| --- | --- | --- | --- |
| 2026-04-19 | Use `docs/agent/` for Codex durable state | Keeps long-horizon state separate from Claude-specific files and chat | Codex startup protocol must read `Status.md` |
| 2026-04-19 | Keep first phase to config + AGENTS + durable docs | Avoid premature hooks/skills/subagents before baseline behavior is observed | Hooks/skills can be added later if repeated pain appears |
| 2026-04-19 | Use `.agents/skills` as repo-local skill path | Current repo already uses this path and it matches Codex skills direction | No new `.codex/skills` added in this phase |
| 2026-04-19 | Do not hard-code Context7 API key in config | Prevent secret leakage in shared artifacts | Requires `CONTEXT7_API_KEY` env var if Context7 is used |

## Validation checklist

- [ ] `.codex/config.toml` parses as TOML.
- [ ] `AGENTS.md` is concise enough to fit project-doc budget.
- [ ] `docs/agent/Status.md` is short and current.
- [ ] Current runnable commands are accurate.
- [ ] Missing dependencies are documented rather than hidden.
