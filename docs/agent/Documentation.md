# Documentation.md — Quanti Agent Audit Log and Operator Notes

This file is the long-form memory for durable Codex work in this repository. Keep `Status.md` short; put longer decisions, validation history, review history, and operator notes here.

## Current feature / task overview

The current task is to stabilize a Codex-native harness for a skills-only company research workspace, then use that harness to harden the currently implemented `collect-company-facts` runner and future company research skills.

The harness is deliberately file-based and native to Codex app/CLI usage:

- `AGENTS.md` defines stable repo policy, explicit mandatory triggers, the three-mode router, and task-relationship handling.
- `Prompt.md` freezes goals, non-goals, constraints, deliverables, and done criteria.
- `Plan.md` tracks milestones, progress, active working checklist/todos, surprises/discoveries, decisions, outcomes/retrospective, risk, and validation.
- `Status.md` is the short current-task and continuation vector for new sessions.
- `Implement.md` is the detailed runbook.
- `code_review.md` defines independent review behavior.
- `.codex/agents/quanti_reviewer.toml` defines the read-only reviewer subagent.

The current active product milestone remains `M2 — Harden collect-company-facts`. The 2026-04-21 harness migration changed workflow semantics and review policy, but it did not advance or replace `M2`.

As of 2026-04-24, M2 is actively in progress. The local runner baseline dependencies are installed, the user has confirmed that `--demo` should still rely on real listed-company inputs, and the runtime slice now includes a fix for the second-level `run_id` collision uncovered by real AAPL validation.

## Operating model

The preferred durable workflow is:

```text
Classify task relationship -> apply mandatory triggers -> plan/replan if needed -> execute active milestone -> update living plan sections -> validate -> independent review -> fix accepted findings -> update Status/Documentation -> continue or stop
```

The task relationship can be:

```text
new durable task | revision of current durable task | continuation/execution of current durable task
```

Interruption recovery is not a separate project mode. It is an expected property of durable files:

```text
Read Status.md -> read active Plan.md milestone/progress/checklist/discoveries/decisions -> read Implement.md -> continue safely
```

If a new session cannot continue from these files, the harness failed to externalize enough state. Update the durable files before proceeding.

## Borrowed repo-harness pattern

This harness borrows the useful repository-maintenance pattern from mature Codex-maintained projects: keep `AGENTS.md` focused on explicit mandatory triggers, keep plans as living self-contained execution documents, and keep hooks/automation narrow until the manual workflow is reliable.

For Quanti, that means:

- `AGENTS.md` says when durable planning, implementation strategy, verification, review, and credential hygiene are mandatory.
- `Plan.md` maintains `Progress`, `Active working checklist`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`.
- `docs/agent/code_review.md` filters independent review down to material, evidence-backed findings.
- Future skills/hooks should be narrow and only added after repeated manual workflow proves stable.

## How to run current checks

TOML parse for reviewer config:

```bash
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
```

Harness routing check:

```bash
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
```

Old router should not remain active in current guidance:

```bash
legacy_router_pattern='5''-mode|five primary ''modes|Mode ''4|Mode ''5|Resume durable ''work|Plan or replan durable ''work|Execute an approved durable ''milestone'
rg -n "$legacy_router_pattern" AGENTS.md docs/agent/Prompt.md docs/agent/Plan.md docs/agent/Implement.md docs/agent/Documentation.md
```

Default lightweight runner validation after Python/runtime changes:

```bash
python -m compileall company_research_runtime .agents/skills/company_research/collect-company-facts/scripts/run.py
python .agents/skills/company_research/collect-company-facts/scripts/run.py --help
```

Functional runner validation only when `COMPANY_RESEARCH_ROOT/company/AAPL/company.yaml` exists with `cik`:

```bash
python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL --demo
```

## Review protocol notes

`/review` is a user/interface slash workflow. It should not be written as something the agent silently performs on the user's behalf.

For native automated review inside the durable workflow, use:

```text
Spawn the read-only quanti_reviewer subagent to review the current milestone diff using docs/agent/code_review.md. The reviewer must not edit files. Return only material findings.
```

If the user manually runs `/review`, the main agent can consume that report as the independent review evidence.

Reviewer findings are not accepted truth. The main agent should accept only material, evidence-backed findings and should record only accepted findings/fixes in durable state.

## Decisions

### 2026-04-20 — Use `docs/agent/` as durable project memory

Decision:
Use markdown files under `docs/agent/` for long-horizon state instead of relying on chat history.

Rationale:
Codex sessions can be interrupted, resumed, or compacted. File-based state makes work recoverable.

Consequences:
New durable tasks should update `Prompt.md`, `Plan.md`, and `Status.md` before implementation.

### 2026-04-20 — Retire `CONTINUITY.md` from active workflow

Decision:
Use `docs/agent/Status.md` as the active continuation state.

Rationale:
`Status.md` is shorter, clearer, and agent-agnostic.

Consequences:
README/Claude docs should not direct active work to `CONTINUITY.md` unless it is explicitly restored for a separate purpose.

### 2026-04-20 — Earlier router revision is now superseded

Decision:
An earlier harness revision used more top-level routing categories and separated continuation handling more explicitly.

Rationale:
That revision improved the repo over the original broad router, but it still made routing heavier than necessary.

Consequences:
It is no longer the active workflow model. Current guidance is defined by the three-mode router plus task-relationship handling adopted on 2026-04-21.

### 2026-04-21 — Treat independent review as a completion gate, not a top-level mode

Decision:
Review belongs inside durable execution and harness/policy completion protocols, not as a separate mode.

Rationale:
Review is a gate on durable work completion. Making it a top-level router mode increases cognitive load and ambiguity.

Consequences:
Complex milestones should validate and review before being marked complete.

### 2026-04-21 — Collapse the active router to three top-level modes

Decision:
Use three top-level modes: quick standalone task, durable workflow, and harness/policy maintenance.

Rationale:
Too many modes make the agent spend context and reasoning on routing.

Consequences:
`AGENTS.md` is simpler; `Implement.md` carries the detailed protocols.

### 2026-04-21 — Treat continuation as durable-state recovery, not a workflow phase

Decision:
Remove continuation as a separate durable phase. First classify whether the user is starting a new durable task, revising the current durable task, or continuing/executing the current durable task. A new session or compaction should recover from `Status.md`, `Plan.md`, and `Implement.md`.

Rationale:
Users naturally alternate between continuing, interrupting, revising, and starting new work. Treating continuation as a mode makes routing brittle. Durable files should make continuation natural.

Consequences:
`AGENTS.md` now uses task-relationship routing inside Mode 2. `Status.md` records current task identity and continuation instructions. `Plan.md` owns milestones and active todos.

### 2026-04-21 — Add dynamic working checklist/todos to `Plan.md`

Decision:
Keep milestones narrative and stable, but track small changing todos in an active working checklist.

Rationale:
Milestones explain the story and acceptance proof; granular todos evolve during implementation and validation.

Consequences:
Codex should update the active working checklist as steps complete, split, or become obsolete.

### 2026-04-21 — Apply harness v4 using Merge Keep State

Decision:
Adopt the v4 router/review structure without rolling back current repo state.

Rationale:
`Prompt.md`, `Plan.md`, `Status.md`, and `Documentation.md` are live state, not static scaffolding. Literal overwrite would have regressed the active milestone, blockers, and M1 completion state.

Consequences:
`M1` remains complete, `M2` remains current, known local blockers remain visible, and `CONTINUITY.md` stays retired from active workflow.

### 2026-04-23 — Adopt explicit mandatory triggers

Decision:
Add concrete mandatory triggers for durable planning, implementation strategy, verification, independent review, and credential hygiene.

Rationale:
Concrete path and behavior triggers are easier for Codex to follow than broad principles like "be careful". They also make review and completion gates more objective.

Consequences:
Future durable milestones should ask which trigger applies before editing, and `Status.md` / `Documentation.md` should record validation and review evidence.

### 2026-04-23 — Keep plan sections living and explicit

Decision:
Maintain `Progress`, `Active working checklist`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` in `Plan.md`.

Rationale:
Milestones describe the big work story; progress, checklist, discoveries, decisions, and outcomes preserve execution reality across context resets.

Consequences:
`Status.md` remains short, while `Plan.md` carries the detailed execution state needed for safe continuation.

## Validation history

| Date | Area | Command / Check | Result | Notes |
| --- | --- | --- | --- | --- |
| 2026-04-20 | M1 docs | `rg` checks for stale runner/path claims | pass with follow-ups | Major stale claims were fixed. |
| 2026-04-20 | Runtime env | `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` | fail | Missing `yaml` package. |
| 2026-04-21 | Harness v4 migration | Merge Keep State structural file application | pass | `AGENTS.md`, `Implement.md`, `code_review.md`, and `quanti_reviewer.toml` updated to v4 structure. |
| 2026-04-21 | Harness v4 migration | Live-state file merge | pass | `Prompt.md`, `Plan.md`, `Status.md`, and `Documentation.md` were moved to the new router semantics without regressing current repo state. |
| 2026-04-21 | Harness v4 migration | Companion setup-doc sync | pass | `docs/MCP_SETUP_GUIDE.md` wording was narrowed so target MCP mapping is not mistaken for currently implemented runners. |
| 2026-04-21 | Harness v4 migration | `python - <<'PY' ... tomllib ... PY` for `.codex/agents/quanti_reviewer.toml` | pass | Reviewer config parses. |
| 2026-04-21 | Harness v4 migration | Router/review structure `rg` check | pass | Active router and review-gate terms are present where expected. |
| 2026-04-21 | Harness v4 migration | Legacy-router `rg` check | pass | No old router terminology remains active in current guidance files. |
| 2026-04-21 | Harness v4 migration | Independent review via read-only `quanti_reviewer` | pass | Verdict `pass`, confidence `high`, no material findings. |
| 2026-04-23 | Harness optimization | Mandatory-trigger and living-plan file merge | applied | `AGENTS.md`, `Plan.md`, `Implement.md`, `code_review.md`, and related durable docs were updated without changing product/runtime code. |
| 2026-04-23 | Harness optimization | Targeted TOML and heading checks | pass | TOML parse passed; targeted per-file heading checks passed; legacy router terms remained absent. |
| 2026-04-23 | Harness optimization | Independent review via read-only `quanti_reviewer` | pass_with_findings | Accepted findings were fixed in the same round: durable-state sync and per-file validation specificity. |
| 2026-04-23 | Harness optimization | Final confirmation review via read-only `quanti_reviewer` | pass | No material findings remained after the accepted fixes were applied. |
| 2026-04-24 | M2 runtime/setup | `python -m pip install PyYAML pandas pyarrow` | pass | Installed the current runner baseline dependencies into the local conda-base Python; `numpy` came in transitively via `pandas`. |
| 2026-04-24 | M2 runtime/setup | `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` | pass | Help now renders locally and clarifies that `--demo` still requires `company.yaml.cik`. |
| 2026-04-24 | M2 runtime/setup | `python -m compileall company_research_runtime .agents/skills/company_research/collect-company-facts/scripts/run.py` | pass | Active runtime helper and runner compile after the M2 edits. |
| 2026-04-24 | M2 runtime/setup | `COMPANY_RESEARCH_ROOT=/Users/yuye/mcp/work/company_research python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL --demo` | pass | Real listed-company input root works in demo mode; current filings index and parquet outputs are written under the external artifact root. |
| 2026-04-24 | M2 runtime/setup | `COMPANY_RESEARCH_ROOT=/Users/yuye/mcp/work/company_research python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL --filings-path /tmp/quanti_aapl_recent_filings.json` | pass_with_finding | Real SEC recent-filings payload for AAPL exercised the non-demo path and exposed a second-level `run_id` collision before the runtime fix. |
| 2026-04-24 | M2 runtime/setup | Parallel rerun of `AAPL --demo` and `AAPL --filings-path /tmp/quanti_aapl_recent_filings.json` after the `default_run_id()` fix | pass | Same-second validations now emit distinct `run_id` values, confirming the per-run audit directory no longer collides in this path. |

## Known issues

| Issue | Impact | Workaround / next step |
| --- | --- | --- |
| M2 independent review evidence is still missing | Runtime/setup-doc changes from 2026-04-24 should not be treated as milestone-complete without review evidence | Run `/review` manually or explicitly authorize the read-only `quanti_reviewer` subagent in a later turn. |
| Reviewer can over-report | Noise can pollute workflow | Use `code_review.md` materiality gate; record only accepted findings. |

## Follow-ups

- [x] Run static checks for the v4 Merge Keep State migration.
- [x] Run independent review gate on the harness migration and stop after reporting findings.
- [x] Run static checks for the mandatory-trigger and living-plan optimization.
- [x] Run final confirmation review for the mandatory-trigger and living-plan optimization after accepted fixes.
- [ ] Run the M2 independent review gate for the 2026-04-24 runtime/setup slice and then decide whether M2 can close.
