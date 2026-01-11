Goal (incl. success criteria):
- Remove specified root directories and related files from `LLM_skills_strategy` to keep only large-model content.
- Success = deletions complete on LLM branch; keep LLM-related files (`README.md`, `AGENTS.md`, `.codex/`, `.vscode/`, `.claude/`, `CLAUDE.md`, `CONTINUITY.md`).

Constraints/Assumptions:
- approval_policy=never; sandbox_mode=danger-full-access; network_access=enabled.
- Follow AGENTS.md continuity rules; update `CONTINUITY.md` at start and on changes.

Key decisions:
- Delete root dirs on LLM branch: backtest, company_research_runtime, configs, Daily_pipline, docs, examples, others, production_pipline, scripts, src, task_piplines, tests, tools, Train, Train_others.
- Delete related root scripts: `master_scheduler.py`, `activate_env.ps1`, `backtest_model.py`, `initiate_pip_pv_dataset.py`, `main.py`, `profile_tsvit_dataloader.py`, `run_daily_space_signals.bat`, `run_space_data_pipeline.py`, `run_tsvit.py`, `space_command.txt`, `space_pipeline_scheduler.py`, `start_master_scheduler.bat`.
- Keep LLM-related docs/config dirs: `README.md`, `AGENTS.md`, `.codex/`, `.vscode/`, `.claude/`, `CLAUDE.md`, `CONTINUITY.md`.

State:
- Current branch: `LLM_skills_strategy`.
- Deletions applied; pending commit.

Done:
- Removed listed directories and root scripts from LLM branch.

Now:
- Stage and commit deletions.

Next:
- Verify remaining root files align with LLM-only scope; decide on `requirements.txt` if needed.

Open questions (UNCONFIRMED if needed):
- Keep or remove `requirements.txt` and remaining root markdowns (e.g., `stock_skills_*`, `Phase 1 ...`) for LLM-only repo?

Working set (files/ids/commands):
- `CONTINUITY.md`
- `git status -sb`
- `git add -A`
- `git commit -m "chore: remove small-model pipelines from LLM branch"`
