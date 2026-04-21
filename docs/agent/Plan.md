# Plan.md — Quanti Durable Execution Plan

This file is the source of truth for current long-horizon work. Keep it updated whenever scope, milestone order, acceptance criteria, validation, blockers, progress, or working todos change.

## Task

Short name: Stabilize Codex harness and current in-repo runner for Quanti

Related specification:

- `docs/agent/Prompt.md`

## How to use this plan

- For quick standalone answers or small bounded edits, follow `AGENTS.md` Mode 1 and do not use this full plan unless the task expands.
- For durable work, follow `AGENTS.md` Mode 2. Before acting, decide whether the user is:
  - starting a new durable task,
  - revising the current durable task,
  - or continuing/executing the current durable task.
- A new session, interruption, or compaction is not a separate task type. Recover by reading `Status.md`, this plan, and `Implement.md`; if recovery is impossible from those files, update them before continuing.
- For harness or agent-policy changes, follow `AGENTS.md` Mode 3.
- Keep detailed step-by-step execution in `docs/agent/Implement.md`; keep `AGENTS.md` focused on routing and stable repository rules.

## Current milestone

Milestone: M2 — Harden `collect-company-facts`

Status: `ready_to_start`

Next action: Inspect `.agents/skills/company_research/collect-company-facts/` and `company_research_runtime/` so the current runner behavior, blocked conditions, demo expectations, and validation expectations match the documented contract.

Note: The 2026-04-21 harness v4 migration updated workflow semantics and review policy, but it did not change the active product milestone. `M2` remains current.

## Progress

Progress and milestones are distinct. Milestones describe the big story and acceptance proof; this section tracks granular execution.

- [x] M0 — Install Codex-native durable workflow foundation.
- [x] M1 — Reconcile current repo docs with implementation reality.
- [ ] M2 — Harden `collect-company-facts` runner behavior and contract.
- [ ] M3 — Choose the next first-class skill and produce an approved implementation plan.
- [ ] M4 — Implement the next runner in a small verified slice.
- [ ] M5 — Add a lightweight validation harness only after repeated checks are stable.

## Active working checklist

For M2, update this checklist as implementation proceeds. Split, add, or mark obsolete todos as new evidence appears.

- [ ] Inspect `collect-company-facts/SKILL.md` and `scripts/run.py` for actual CLI, dependency, blocked, and demo behavior.
- [ ] Inspect `company_research_runtime/` helpers used by the runner.
- [ ] Decide whether `--demo` should still require `company.yaml.cik` or support a dependency-light demonstration path.
- [ ] Decide whether the local `yaml`/PyYAML failure should be addressed through setup docs, dependency declaration, or both.
- [ ] Align `SKILL.md`, setup docs, and runner behavior.
- [ ] Run compile/help validation or record exact environment blockers.
- [ ] Run independent review gate before marking M2 complete.
- [ ] Update `Status.md` and `Documentation.md` with validation and review outcome.

## Milestone overview

| ID | Milestone | Scope | Acceptance criteria | Validation | Status |
| --- | --- | --- | --- | --- | --- |
| M0 | Harness foundation | `.codex/config.toml`, `AGENTS.md`, `docs/agent/*` | Codex has concise repo policy, task-relationship router, durable state, runbook, review gate, and validation protocol | TOML parse; path/routing checks | completed |
| M1 | Repo reality reconciliation | `README.md`, `docs/skills/README.md`, `docs/skills/MASTER_PLAN.md`, `CLAUDE.md`, setup docs as needed | Human docs and agent docs agree on current implemented assets | Manual doc review; path checks; independent review when docs are user-facing | completed |
| M2 | Harden `collect-company-facts` | skill definition + runner + runtime helpers | Runner behavior matches documented contract; blocked/demo behavior is explicit | `compileall`, `--help`, focused run when artifacts exist; independent review | ready_to_start |
| M3 | Decide next first-class skill | specs + plan only, likely `company-foundation` or Skill 2 migration | A self-contained implementation plan exists before code edits | Plan review | not_started |
| M4 | Implement next runner in small slice | selected skill path + runtime helpers | New runner writes `meta/result/needs` and one useful artifact | compile/help/focused run; independent review | not_started |
| M5 | Add lightweight validation harness | scripts or docs, not external wrapper yet | Repeated checks are copy-pasteable and reliable | Run validation commands; independent review if harness files change | not_started |

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
- `docs/agent/code_review.md`
- `.codex/agents/quanti_reviewer.toml`

Acceptance criteria:

- `AGENTS.md` clearly states this is a skills-only company research repo.
- `AGENTS.md` states that only `collect-company-facts` is currently implemented in-repo.
- `AGENTS.md` uses the three primary modes:
  1. Quick standalone task.
  2. Durable workflow, with new-task, current-task revision, and current-task execution branches.
  3. Harness or agent-policy maintenance.
- `AGENTS.md` stays concise and defers detailed execution loops to `docs/agent/Implement.md`.
- `docs/agent/Status.md` can orient a fresh session quickly.
- `docs/agent/code_review.md` defines an independent review gate.
- `.codex/agents/quanti_reviewer.toml` defines a read-only reviewer subagent.
- No API key is hard-coded in Codex config.

Validation commands:

```text
python - <<'PY'
import pathlib
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
for p in ['.codex/config.toml', '.codex/agents/quanti_reviewer.toml']:
    tomllib.loads(pathlib.Path(p).read_text(encoding='utf-8'))
    print(f'{p} parses')
PY

rg -n 'Mode 1|Mode 2|Mode 3|Branch A|Branch B|Branch C|independent review gate|task relationship' AGENTS.md docs/agent/Implement.md docs/agent/code_review.md docs/agent/Plan.md docs/agent/Status.md
find docs/agent -maxdepth 1 -type f | sort
```

Status notes:

- Initial durable workflow foundation landed on 2026-04-20.
- Workflow semantics were revised on 2026-04-21 to use three top-level modes plus task-relationship routing inside Mode 2.
- Continuation/recovery now comes from durable files, not a separate router mode.
- The independent review gate is now part of the harness foundation.

### M1 — Repo reality reconciliation

Scope:

- `README.md`
- `docs/skills/README.md`
- `docs/skills/MASTER_PLAN.md`
- `CLAUDE.md`
- user-facing setup docs such as `docs/MCP_SETUP_GUIDE.md`
- optional: retire or repurpose `CONTINUITY.md`

Acceptance criteria:

- All docs agree on actual implemented runner paths.
- Docs do not point to nonexistent `docs/MASTER_PLAN.md`; canonical path is `docs/skills/MASTER_PLAN.md`.
- Docs distinguish target 9-skill architecture from current implementation.
- Setup docs do not imply that unimplemented runners can be executed today.
- `CONTINUITY.md` is either retired, replaced by `docs/agent/Status.md`, or explicitly marked Claude/session-specific.

Validation commands:

```text
find . -maxdepth 4 -type f \( -name '*.md' -o -name '*.py' \) | sort
rg -n "docs/MASTER_PLAN|extract-xbrl-timeseries|company-foundation/scripts/run.py|valuation-and-margin-of-safety/scripts/run.py|CONTINUITY" README.md AGENTS.md CLAUDE.md docs || true
```

Expected evidence:

- A short list of mismatches and proposed doc edits before editing.
- Independent review gate runs before M1 is treated as fully closed, because M1 changed user-facing docs and agent-policy docs.

Status notes:

- Completed on 2026-04-20 and kept completed during the 2026-04-21 harness migration.
- `README.md` reflects that only `collect-company-facts` exists in-repo today.
- `CLAUDE.md` points active continuation at `docs/agent/Status.md`, `Plan.md`, and `Implement.md`.
- `CONTINUITY.md` remains retired from the active workflow and was not restored implicitly.
- `docs/MCP_SETUP_GUIDE.md` wording was tightened so target skill-chain MCP mapping is not mistaken for current runner availability.

### M2 — Harden `collect-company-facts`

Scope:

- `.agents/skills/company_research/collect-company-facts/SKILL.md`
- `.agents/skills/company_research/collect-company-facts/scripts/run.py`
- `company_research_runtime/`
- relevant docs under `docs/skills/`

Acceptance criteria:

- `SKILL.md` matches actual runner behavior.
- The runner clearly documents whether `company.yaml.cik` is a hard dependency, including for demo mode unless changed intentionally.
- Missing dependencies produce `blocked` and `needs.yaml`.
- Outputs are written under `COMPANY_RESEARCH_ROOT`, not the repo.
- Validation commands are documented.
- Independent review gate reports no unresolved material high/medium findings.

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
- Current known local blocker: `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` fails with `ModuleNotFoundError: No module named 'yaml'`.
- Current open product decision: whether `--demo` should remain dependent on `company.yaml.cik` or become dependency-light.

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
- The plan identifies required inputs, outputs, status codes, validation, and review gate expectations.
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
- Independent review gate reports no unresolved material high/medium findings.

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
- Independent review gate confirms it is scoped and not a hidden external orchestrator.

Validation commands:

```text
# TBD after M2/M4 patterns are stable.
```

Status notes:

- Not started.

## Decision log

| Date | Decision | Rationale | Consequences |
| --- | --- | --- | --- |
| 2026-04-20 | Use durable docs under `docs/agent/` instead of chat memory | Long-horizon Codex sessions need recoverable state | New tasks should update Prompt/Plan/Status before implementation |
| 2026-04-20 | Retire `CONTINUITY.md` from active workflow | `docs/agent/Status.md` is shorter and works across agents | README/CLAUDE should point to `docs/agent/Status.md` |
| 2026-04-21 | Treat independent review as a milestone completion gate | Review is valuable but should not become another top-level mode | Durable execution must validate, review, then update durable state |
| 2026-04-21 | Collapse the active router to three top-level modes | Fewer routing branches reduce model confusion | Durable work now uses task-relationship routing inside Mode 2 |
| 2026-04-21 | Treat continuation as durable-state recovery, not a separate workflow phase | Users naturally continue, interrupt, revise, and restart work | `Status.md`, `Plan.md`, and `Implement.md` must be sufficient to resume safely |

## Risk register

| Risk | Why it matters | Mitigation | Status |
| --- | --- | --- | --- |
| Reviewer over-reports low-value issues | Noise can waste time and pollute durable docs | Use `code_review.md` materiality gate; record accepted findings only | active |
| `yaml` dependency missing locally | Runner help/validation fails before behavior can be tested | Fix setup docs or dependency file before deeper runner validation | active |
| `--demo` contract is unclear | Demo runs may block unexpectedly or mislead users | Decide and document/fix in M2 | active |
| Setup docs drift from implemented runner reality | Users may assume target skills are runnable | Keep M1 doc reconciliation complete and recheck setup docs when commands change | active |
