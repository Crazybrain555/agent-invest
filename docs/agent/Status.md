# Status.md

Last updated: 2026-04-20 Asia/Shanghai
Updated by: Codex

## Current state

Current milestone: M2 — Harden `collect-company-facts`
Status: `ready_to_start`

One-sentence summary: M1 is complete: the human-facing docs now match the trimmed repo reality, `CONTINUITY.md` is retired from the active workflow, and the next step is to harden the current `collect-company-facts` runner.

Next action: Start M2 by inspecting `.agents/skills/company_research/collect-company-facts/SKILL.md`, `.agents/skills/company_research/collect-company-facts/scripts/run.py`, and `company_research_runtime/` for blocked/demo/output-contract mismatches before any code edits.

## Recently completed

- Completed M1 — Repo reality reconciliation.
- Updated `README.md` so it no longer claims `company-foundation/` exists in the worktree and no longer treats `CONTINUITY.md` as the active resume file.
- Rewrote `CLAUDE.md` to use `docs/agent/Status.md` / `Plan.md` / `Implement.md` as the active durable workflow references.
- Confirmed `docs/skills/README.md` and `docs/skills/MASTER_PLAN.md` already matched current implementation-vs-target distinctions and did not require edits.
- Kept the current `CONTINUITY.md` deletion state untouched and documented the repo decision to retire it from the active workflow.

## Current blockers

- Local runtime validation for M2 is currently limited because `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` fails with `ModuleNotFoundError: No module named 'yaml'`.
- M2 still needs an explicit decision on whether `--demo` should require `company.yaml.cik` or support a true dependency-light demo path.

## Files changed in current task

- `README.md`
- `CLAUDE.md`
- `docs/MCP_SETUP_GUIDE.md`
- `docs/agent/Plan.md`
- `docs/agent/Prompt.md`
- `docs/agent/Status.md`
- `docs/agent/Documentation.md`
- `docs/skills/references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md`

## Latest validation

| Command | Result | Notes |
| --- | --- | --- |
| `find . -maxdepth 4 -type f \( -name '*.md' -o -name '*.py' \) \| sort` | pass | Confirmed the trimmed repo shape used for M1 doc reconciliation. |
| `rg -n "docs/MASTER_PLAN\|extract-xbrl-timeseries\|company-foundation/scripts/run.py\|valuation-and-margin-of-safety/scripts/run.py\|CONTINUITY" README.md AGENTS.md CLAUDE.md docs \|\| true` | pass | Active entry docs were cleaned; remaining matches are deliberate retirement notes, historical aliases in references, or archive/history files. |
| `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` | fail | Local environment is missing `yaml` (`ModuleNotFoundError`), so M2 runtime validation is currently blocked on dependency setup. |

## Important decisions since last update

- Treat `docs/agent/Status.md` as the active durable resume file across agents.
- Retire `CONTINUITY.md` from the active workflow instead of restoring it implicitly.
- Keep `CLAUDE.md` as Claude-specific guidance only; implementation truth lives in the filesystem, `AGENTS.md`, `docs/agent/*`, and `docs/skills/*`.
- Treat `docs/agent/Status.md` as the short session-resume file.
- Treat `docs/agent/Documentation.md` as longer audit/operator memory.

## Resume instructions

To resume:

1. Read root `AGENTS.md`.
2. Read this file.
3. Read the active milestone in `docs/agent/Plan.md`.
4. Read `docs/agent/Implement.md`.
5. Continue with the next action above.
