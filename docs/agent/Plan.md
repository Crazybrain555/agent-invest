# Plan.md — Quanti Durable Execution Plan

This file is the source of truth for current long-horizon work. Keep it updated whenever scope, milestone order, acceptance criteria, validation, blockers, progress, working todos, discoveries, decisions, or outcomes change.

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

Status: `in_progress`

Next action: Finalize the current M2 hardening slice by documenting the demo/dependency contract, preserving unique run directories across close-start runs, and recording the new real-input validation evidence.

Note: The 2026-04-21 harness v4 migration updated workflow semantics and review policy, but it did not change the active product milestone. `M2` remains current.

## Progress

Progress and milestones are distinct. Milestones describe the big story and acceptance proof; this section tracks granular execution.

- [x] 2026-04-20 — M0 — Install Codex-native durable workflow foundation.
- [x] 2026-04-20 — M1 — Reconcile current repo docs with implementation reality.
- [x] 2026-04-23 — Tighten the harness with explicit mandatory triggers, living-plan sections, and acceptance-oriented review guidance.
- [ ] M2 — Harden `collect-company-facts` runner behavior and contract.
- [x] 2026-04-24 — Began M2 inspection, installed the current runner baseline dependencies, and collected real AAPL validation inputs outside the repo.
- [x] 2026-04-24 — Fixed second-level `run_id` collisions and re-ran parallel AAPL validation with distinct run directories.
- [ ] M3 — Choose the next first-class skill and produce an approved implementation plan.
- [ ] M4 — Implement the next runner in a small verified slice.
- [ ] M5 — Add a lightweight validation harness only after repeated checks are stable.

## Active working checklist

For M2, update this checklist as implementation proceeds. Split, add, or mark obsolete todos as new evidence appears.

- [x] Confirm which mandatory triggers apply to M2: implementation strategy, verification, and independent review.
- [x] Inspect `collect-company-facts/SKILL.md` and `scripts/run.py` for actual CLI, dependency, blocked, and demo behavior.
- [x] Inspect `company_research_runtime/` helpers used by the runner.
- [x] Decide whether `--demo` should still require `company.yaml.cik` or support a dependency-light demonstration path.
- [x] Decide whether the local `yaml`/PyYAML failure should be addressed through setup docs, dependency declaration, or both.
- [x] Align `SKILL.md`, setup docs, and runner behavior.
- [x] Run compile/help validation or record exact environment blockers.
- [x] Re-run focused validation after fixing second-level `run_id` collisions in `company_research_runtime.default_run_id`.
- [ ] Run independent review gate before marking M2 complete.
- [x] Update `Status.md` and `Documentation.md` with validation and review outcome.

## Surprises & Discoveries

Record unexpected facts with short evidence. Keep this section factual, not speculative.

- 2026-04-20 — `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` can fail before argument parsing if `yaml` / PyYAML is not installed. Evidence: local `ModuleNotFoundError: No module named 'yaml'`.
- 2026-04-20 — The current checkout contains only one implemented in-repo runner even though the target architecture describes nine skills. Evidence: `docs/skills/README.md` and `.agents/skills/company_research/` paths.
- 2026-04-24 — `--demo` still enforces `company.yaml.cik`; it only injects a built-in demo filings list when no filings payload is passed. Evidence: `scripts/run.py` loads `company.yaml` and returns `blocked` before the `demo and not raw_filings` branch.
- 2026-04-24 — Parallel validations that start within the same second can collide on `runs/{run_id}` because `default_run_id()` was second-granular. Evidence: concurrent `AAPL --demo` and `AAPL --filings-path /tmp/quanti_aapl_recent_filings.json` runs both produced `run_id=20260423_123007`.
- 2026-04-24 — After adding microsecond precision plus a short suffix to `default_run_id()`, the same parallel AAPL validations produced distinct `run_id` values (`20260423_123307_757233_8c6731` and `20260423_123307_757234_dc4dd3`). Evidence: post-fix validation output.

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
- `AGENTS.md` contains explicit mandatory triggers for planning, implementation strategy, verification, review, and credential hygiene.
- `Plan.md` contains living sections for `Progress`, `Active working checklist`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`.
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

rg -n '^## 2\\. Policies and mandatory triggers$' AGENTS.md
rg -n '^### Step 1 — Decide task relationship$' AGENTS.md
rg -n '^### Mode 1 — Quick standalone task$' AGENTS.md
rg -n '^### Mode 2 — Durable workflow$' AGENTS.md
rg -n '^#### Branch A — New durable task$' AGENTS.md
rg -n '^#### Branch B — Revise current durable task$' AGENTS.md
rg -n '^#### Branch C — Execute current durable task$' AGENTS.md
rg -n '^### Mode 3 — Harness or agent-policy maintenance$' AGENTS.md
rg -n '^## Progress$' docs/agent/Plan.md
rg -n '^## Active working checklist$' docs/agent/Plan.md
rg -n '^## Surprises & Discoveries$' docs/agent/Plan.md
rg -n '^## Decision log$' docs/agent/Plan.md
rg -n '^## Outcomes & Retrospective$' docs/agent/Plan.md
rg -n '^## Mandatory trigger protocol$' docs/agent/Implement.md
rg -n '^## Living-plan sections$' docs/agent/Implement.md
rg -n '^## Acceptance-oriented review$' docs/agent/code_review.md
rg -n '^Current milestone:' docs/agent/Status.md
rg -n '^Latest independent review:' docs/agent/Status.md
find docs/agent -maxdepth 1 -type f | sort
```

Status notes:

- Initial durable workflow foundation landed on 2026-04-20.
- Workflow semantics were revised on 2026-04-21 to use three top-level modes plus task-relationship routing inside Mode 2.
- Continuation/recovery now comes from durable files, not a separate router mode.
- The independent review gate is now part of the harness foundation.
- 2026-04-23 tightened the harness with explicit mandatory triggers and fully explicit living-plan sections.

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

- In progress.
- `requirements.txt` now needs to carry the current runner baseline dependencies so fresh-checkout help validation is reproducible.
- `--demo` is now intentionally treated as a lightweight data path that still requires a real `company.yaml.cik`.
- The second-level `run_id` collision has been fixed locally and validated; M2 still needs the required review gate before the milestone can close.

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
| 2026-04-23 | Add explicit mandatory triggers | Concrete triggers are easier for Codex to follow than broad principles | `AGENTS.md` now has planning, strategy, verification, review, and credential-hygiene triggers |
| 2026-04-23 | Keep living-plan sections explicit | Milestones and progress are distinct; discoveries/decisions/outcomes must survive context resets | `Plan.md` now makes those sections first-class durable state |
| 2026-04-24 | Keep `--demo` dependent on `company.yaml.cik` | The user wants validation to use real listed-company inputs, and the existing runner already treats `company.yaml.cik` as a hard dependency | Docs/help should clarify that demo only substitutes filings payloads, not company identity |
| 2026-04-24 | Formalize the current runner baseline dependencies in `requirements.txt` | Fresh-checkout `--help` should not fail on missing `yaml`, and parquet outputs depend on the common data stack | Minimal baseline is now `PyYAML`, `pandas`, and `pyarrow` |
| 2026-04-24 | Make `run_id` unique beyond second-level precision | Real parallel validations showed that second-granular IDs can overwrite the immutable per-run audit directory | `default_run_id()` now needs timestamp precision plus a uniqueness suffix |

## Outcomes & Retrospective

Update this after a milestone or task closes.

- M0 outcome: Codex-native durable state exists and can be resumed from checked-in docs.
- M1 outcome: Docs distinguish the actual implemented runner from the target 9-skill architecture.
- Harness outcome so far: The workflow now uses three top-level modes, task-relationship routing, explicit mandatory triggers, living-plan sections, and a read-only independent review gate.
- Remaining gap: M2 still needs the required review gate before closure.

## Risk register

| Risk | Why it matters | Mitigation | Status |
| --- | --- | --- | --- |
| Reviewer over-reports low-value issues | Noise can waste time and pollute durable docs | Use `code_review.md` materiality gate; record accepted findings only | active |
| Current runner baseline dependencies are not formalized | Fresh-checkout help validation can fail before behavior can be tested | Update `requirements.txt` and setup docs, then validate in a clean-ish local environment | mitigated |
| `run_id` collisions can overwrite per-run audit state | Concurrent or near-concurrent runs violate the immutability expectation of `runs/{run_id}` | Change `default_run_id()` and rerun parallel validation | mitigated |
| Setup docs drift from implemented runner reality | Users may assume target skills are runnable | Keep M1 doc reconciliation complete and recheck setup docs when commands change | active |
