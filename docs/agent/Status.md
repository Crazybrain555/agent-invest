# Status.md — Quanti Durable Current State

Last updated: 2026-04-24 Asia/Shanghai
Updated by: Codex

Keep this file short. It is the first file to read when continuing durable work. Long history belongs in `docs/agent/Documentation.md`.

## Current task identity

Task: Stabilize Codex-native harness and harden current Quanti skill runner.

Task relationship rule:
If the user says "continue", "next", "resume", or points at the current milestone, continue this task. If the user introduces a materially new goal, initialize a new durable task by updating `Prompt.md`, `Plan.md`, and this file after recording any useful old-task handoff in `Documentation.md`.

## Current state

Current milestone: M2 — Harden `collect-company-facts`

Status: `in_progress`

One-sentence summary:
M2 is now in progress: the current runner baseline dependencies are installed locally, `--demo` has been confirmed to keep the real `company.yaml.cik` dependency, and the second-level `run_id` collision exposed by real AAPL validation has been fixed and revalidated.

Next action:
Run the required M2 review gate if the user explicitly wants the read-only reviewer invoked in this session; otherwise keep M2 open with the current validation evidence and continue only with user direction.

## Recently completed

- Replaced the old active router with the v4 three-mode router:
  1. Quick standalone task.
  2. Durable workflow.
  3. Harness or agent-policy maintenance.
- Moved continuation/recovery into durable-file reading instead of treating it as a top-level mode.
- Added task-relationship handling for new durable tasks, current-task revisions, and current-task execution.
- Added explicit mandatory triggers in `AGENTS.md` for durable planning, implementation strategy, verification, independent review, and credential hygiene.
- Expanded `Plan.md` so `Progress`, `Active working checklist`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` are all first-class durable state.
- Strengthened the independent review gate through `docs/agent/code_review.md` and `.codex/agents/quanti_reviewer.toml`.
- Preserved the existing repo-reality fixes: `README.md` and `CLAUDE.md` already point to `docs/agent/*`, and `CONTINUITY.md` remains retired from the active workflow.

## Current blockers

- No current environment blocker remains for local import/help validation after installing `PyYAML`, `pandas`, and `pyarrow`.
- M2 is still blocked from closure until the required review evidence is recorded for the runtime/setup-doc changes made on 2026-04-24.

## Files changed in current task

- `.agents/skills/company_research/collect-company-facts/SKILL.md`
- `.agents/skills/company_research/collect-company-facts/scripts/run.py`
- `AGENTS.md`
- `docs/agent/Prompt.md`
- `docs/agent/Plan.md`
- `docs/agent/Status.md`
- `docs/agent/Implement.md`
- `docs/agent/Documentation.md`
- `docs/agent/code_review.md`
- `.codex/agents/quanti_reviewer.toml`
- `company_research_runtime/runlog.py`
- `requirements.txt`
- `README.md`

## Latest validation

| Command | Result | Notes |
| --- | --- | --- |
| `python - <<'PY' ... tomllib ... PY` for `.codex/config.toml` and `.codex/agents/quanti_reviewer.toml` | pass | Both TOML files parse successfully. |
| Targeted harness heading checks in `AGENTS.md`, `Plan.md`, `Implement.md`, `code_review.md`, and `Status.md` | pass | Per-file heading assertions passed after narrowing the checks to avoid self-match and false-pass behavior. |
| `legacy_router_pattern='5''-mode|five primary ''modes|Mode ''4|Mode ''5|Resume durable ''work|Plan or replan durable ''work|Execute an approved durable ''milestone'; rg -n "$legacy_router_pattern" AGENTS.md docs/agent/Prompt.md docs/agent/Plan.md docs/agent/Implement.md docs/agent/Documentation.md` | pass | No old router terminology remains active in current guidance. |
| `python -m pip install PyYAML pandas pyarrow` | pass | Installed the current runner baseline dependencies into `/opt/homebrew/Caskroom/miniconda/base/bin/python`; `numpy` came in transitively via `pandas`. |
| `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` | pass | Runner imports now resolve locally and `--help` renders successfully. |
| `python -m compileall company_research_runtime .agents/skills/company_research/collect-company-facts/scripts/run.py` | pass | Runtime helpers and the active runner compile after dependency setup. |
| `COMPANY_RESEARCH_ROOT=/Users/yuye/mcp/work/company_research python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL --demo` | pass | Demo path now validates against a real listed-company input root and returns `ok`; with no filings payload it writes `current/filings_index.yaml` and `current/filings_index.parquet`. |
| `COMPANY_RESEARCH_ROOT=/Users/yuye/mcp/work/company_research python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL --filings-path /tmp/quanti_aapl_recent_filings.json` | pass_with_finding | Real SEC recent-filings payload for AAPL produced `events_index.parquet`, but running this in parallel with `--demo` exposed a second-level `run_id` collision (`20260423_123007`). |
| Parallel post-fix rerun of `AAPL --demo` and `AAPL --filings-path /tmp/quanti_aapl_recent_filings.json` | pass | Same-second runs now produce distinct `run_id` values (`20260423_123307_757233_8c6731` and `20260423_123307_757234_dc4dd3`), so `runs/{run_id}` no longer collides in this validation path. |
| Independent review on this optimization round | pass | Initial medium findings on durable-state sync and validation specificity were fixed, and the final confirmation review reported no material findings. |

## Review state

Latest independent review: pass for the 2026-04-23 mandatory-trigger and living-plan optimization round via the read-only `quanti_reviewer` subagent. M2 runner changes made on 2026-04-24 still need their own review evidence before milestone closure.

Accepted findings:
The two earlier medium findings on durable-state sync and validation specificity were fixed in the harness round. No M2 review findings have been accepted yet because the M2 review gate has not run in this session.

## Continuation instructions

To continue this task:

1. Read root `AGENTS.md`.
2. Read this file.
3. Read the active milestone, `Progress`, and active working checklist in `docs/agent/Plan.md`.
4. Read `Surprises & Discoveries` and `Decision Log` in `docs/agent/Plan.md`.
5. Read `docs/agent/Implement.md`.
6. Continue with the next action above.

To start a new durable task:

1. Record any useful old-task handoff in `docs/agent/Documentation.md`.
2. Replace current-task sections of `Prompt.md`, `Plan.md`, and this file.
3. Draft the new plan before editing runtime code unless the user explicitly asks to plan and implement together.
