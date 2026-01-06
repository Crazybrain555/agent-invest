# AIQuantLab Agent Guide (AGENTS.md)

This document is optimized for coding agents (Codex/Cursor/Devin/etc.) and also readable for humans.
README/Docs are for humans; this file captures the extra, agent-critical context: build/run commands,
repo invariants, operational gotchas, and "how we do changes safely".

---

## 0) Continuity Ledger (compaction-safe)

Maintain a single Continuity Ledger for this workspace in `CONTINUITY.md` (repo root).
The ledger is the canonical session briefing designed to survive context compaction; do not rely on
earlier chat text unless it's reflected in the ledger.
If MCP `fs` tool access is restricted to `/home/help/mcp/work` and cannot reach the repo root,
maintain a temporary mirror at `/home/help/mcp/work/CONTINUITY.md` for tool access and sync it back
to the repo-root `CONTINUITY.md` whenever shell access is available (repo root remains canonical).

### How it works
- At the start of every assistant turn: read `CONTINUITY.md`, update it to reflect the latest
  goal/constraints/decisions/state, then proceed with the work.
- Update `CONTINUITY.md` again whenever any of these change: goal, constraints/assumptions,
  key decisions, progress state (Done/Now/Next), or important tool outcomes.
- Keep it short and stable: facts only, no transcripts. Prefer bullets. Mark uncertainty as
  `UNCONFIRMED` (never guess).
- If you notice missing recall or a compaction/summary event: refresh/rebuild the ledger from visible
  context, mark gaps `UNCONFIRMED`, ask up to 1-3 targeted questions, then continue.

### update_plan vs the Ledger
- `update_plan` (or similar TODO tool) is for short-term execution scaffolding (3-7 steps).
- `CONTINUITY.md` is for long-running continuity across compaction (the "what/why/current state"),
  not a step-by-step task list.
- Keep them consistent: when the plan/state changes, update the ledger at the intent/progress level.

### Suggested CONTINUITY.md format (keep headings)
- Goal (incl. success criteria):
- Constraints/Assumptions:
- Key decisions:
- State:
- Done:
- Now:
- Next:
- Open questions (UNCONFIRMED if needed):
- Working set (files/ids/commands):

---

## 1) Agent Operating Mode (Codex-style, repo-specific)

### Bias to action
- Default expectation: deliver working code + verification steps, not just a plan.
- If details are missing, make reasonable assumptions and implement a safe default.
- Do not produce mid-rollout status chatter; keep intermediate messages minimal.

### Explore efficiently
- Search before writing: reuse existing helpers/patterns.
- Prefer fast search (`rg`) and targeted reads (avoid reading huge files end-to-end).
- Batch file reads when possible; avoid one-file-at-a-time thrashing.

### Tool output truncation (for harness implementers + agents)
If a tool response may be huge (logs, big diffs, data dumps), keep outputs small and "in-distribution":
- Target <= ~10k tokens/tool response (approx `num_bytes/4`).
- If truncating: use half budget for the beginning + half for the end, and truncate the middle with:
  `...3 tokens truncated...`
- Prefer producing smaller outputs upstream:
  - narrow `rg` patterns, add `--max-count`, print specific ranges, or summarize.

### Safe editing rules
- Never run destructive commands (`git reset --hard`, mass deletes, DB drops) unless explicitly asked.
- Do not "fix" by broad try/except or silent fallbacks; surface failures with actionable logs.
- Preserve existing behavior by default; gate behavior changes behind config/flags when possible.
- Keep UTF-8 explicit for file/log I/O (`encoding="utf-8"`).

### Presenting results
- Be concise. Reference paths as inline code (e.g. `src/scheduler/Dfzq_gru_scheduler.py`).
- When you changed code: explain what + why + how to verify.

---

## 2) AGENTS.md Discovery (FYI)

Codex builds an instruction chain every run:
- Global: `~/.codex/AGENTS.override.md` wins; else `~/.codex/AGENTS.md` (only the first non-empty file).
- Project: from repo root down to current working directory. In each directory:
  `AGENTS.override.md`, then `AGENTS.md`, then any configured fallback names
  (`project_doc_fallback_filenames`).
- Merge order: root -> leaf; deeper files override earlier guidance.
- Combined size cap: `project_doc_max_bytes` (default 32 KiB). Split into nested AGENTS files or raise
  the limit if needed.

Useful verification commands:
- `codex --ask-for-approval never "Summarize the current instructions."`
- `codex --cd <subdir> --ask-for-approval never "List the instruction sources you loaded."`

---

## 3) Contributor Quickstart Guide

### 3.1 Encoding defaults (required)
Always run commands in UTF-8 terminals.
- WSL/Linux:
  - `export PYTHONIOENCODING=UTF-8`
  - `export PYTHONUTF8=1`
- PowerShell:
  - `chcp 65001 | Out-Null; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.UTF8Encoding]::new()`
- CMD:
  - `chcp 65001 >NUL`

When reading/writing text files: always set `encoding="utf-8"` (or `utf-8-sig` for Excel-friendly CSVs).

### 3.2 WSL workflow (preferred for Linux-like ops)
1. `cd /mnt/f/AIQuantLab` (or move the repo into WSL filesystem `~/code/...` for faster I/O).
2. Activate venv: `source .venv_wsl/bin/activate`
3. Use venv python explicitly when scripting: `./.venv_wsl/bin/python <script>.py` (prefer `./` to avoid PATH issues)

### 3.3 Windows workflow
- PowerShell:
  - `. .venv\\Scripts\\Activate.ps1`
  - Use venv python explicitly when scripting: `.\\.venv\\Scripts\\python.exe <script>.py`
- CMD:
  - `chcp 65001 >NUL && .\\.venv\\Scripts\\activate.bat`

### 3.4 Package parity (Windows vs WSL)
Keep consistent versions across `.venv` (Windows) and `.venv_wsl` (WSL).
Key packages pinned in production runs (update if you change requirements):
- `numpy 2.2.4`, `pandas 2.3.0`, `pyarrow 20.0.0`, `duckdb 1.3.2`, `tqdm 4.67.1`, `PyYAML 6.0.2`,
  `requests 2.32.3`, `SQLAlchemy 2.0.39`, `numba 0.61.2` + `llvmlite 0.44.0`, `matplotlib 3.10.1`,
  `h5py 3.14.0`, `pymssql 2.3.2`, `psycopg 3.2.9`, `psycopg2-binary 2.9.10`.
Notes:
- PyTorch nightly builds on Windows may not be installed in WSL; only add if CUDA toolchain is configured.

### 3.5 VS Code / Cursor tasks
Common tasks live in `.vscode/tasks.json` (WSL). Run via Terminal -> Run Task...

### 3.6 Proxy note (WSL)
If WSL prints localhost proxy warnings in NAT mode:
- disable proxy envs in `~/.bashrc`, or
- switch WSL to mirrored networking on Windows: `wsl --set-default-networking-mode mirrored`,
  then restart: `wsl --shutdown`

---

## 4) Repo Layout (where things live)

Core layers:
- `src/tasks/`:
  atomic reusable tasks (prefer inheriting `BaseTask` for new tasks).
- `src/scheduler/`:
  orchestration / sequencing / time-based logic (daily pipeline scheduler).
- `task_piplines/`:
  runnable CLI entry scripts. Note the spelling is `task_piplines` (keep it consistent).

Important pipelines / modules:
- `task_piplines/train_data_update/`:
  daily automation scripts:
  - `run_nas_data_pipeline.py`
  - `run_daily_data_pipeline.py`
  - `factors_share_iq_pipline.py`
- `task_piplines/singnals_output/`:
  - `factors_share_xtj_pipline.py`
- `Daily_pipline/`:
  legacy daily pipelines (keep for reference).
- `production_pipline/`:
  production factor export scripts (legacy entry points).
- `src/data_service/pipelines/Dataset_builder/`:
  PV dataset builder + indices + maintenance utilities.
- `src/data_service/pipelines/factor_utils/`:
  `FactorGenerator` inference/export and related config/db fetch helpers.
- `configs/`:
  central YAML config:
  - DB table metadata + code rules: `configs/db/table_config.yaml`
  - local/test DB field/type mappings: `configs/db/local_db_configs.yaml`
  - field mappings (semantic names -> raw columns/tables): `configs/field_mappings/*`
  - master import file: `configs/field_mapping.yaml`
- Root scripts:
  - `master_scheduler.py` (+ `start_master_scheduler.bat`)
  - `run_tsvit.py`, `profile_tsvit_dataloader.py`
  - `run_space_data_pipeline.py`, `space_pipeline_scheduler.py`
  - `backtest_model.py`, `initiate_pip_pv_dataset.py`
- Design docs:
  - `STK_POOL_PIPELINE_DEV.md` (stock pool pipeline design)

---

## 5) Core Workflows & Canonical Commands

### 5.1 Master scheduler (daily automation)
The scheduler runs the data-update chain daily around 00:30 local time and keeps the process resident:
- Run (WSL): `.venv_wsl/bin/python master_scheduler.py`
- Run (Windows): `start_master_scheduler.bat`

What it runs (in order):
1) `task_piplines/train_data_update/run_nas_data_pipeline.py --latest`
2) `task_piplines/train_data_update/run_daily_data_pipeline.py --step all`
3) `task_piplines/train_data_update/factors_share_iq_pipline.py ...`

Operational details:
- Uses `sys.executable` so the current venv Python is used.
- Injects `PYTHONPATH=<repo_root>` so subdir scripts can still `import src/...` and `import configs/...`.
- Forces UTF-8 with `PYTHONIOENCODING=UTF-8` and `PYTHONUTF8=1`.

### 5.2 Daily training data processing
Entry script:
- `task_piplines/train_data_update/run_daily_data_pipeline.py`

Modes:
- default: run once then exit
- `--schedule`: resident schedule mode (script monkey-patches schedule runtime)
- `--step`: select step: `all/normalize/standardize/label/forbid`

Orchestration:
- `src/scheduler/Dfzq_gru_scheduler.py:DfzqGruScheduler` (internally uses `DataPipelineManager`)

Step -> task mapping (core truth):
- normalize / factor_engineering:
  - `src/tasks/market_price_norm_data_initialization.py:MarketPriceNormDataTask`
  - window config: `src/data_service/preprocessing/methods/norm_config.py:Z_WINDOW_MAP_FACTOR_ENGINEERING`
  - output: long table like `ai_is.inter_train_factors_mkt_processed_v3`
- standardize parameters:
  - `src/tasks/standardization_parameter_generation.py:StandardParamsGenerator`
- label generation:
  - `src/tasks/label_generation_task.py:LabelGenerationTask`
  - output: training label table (default `ai_is.training_label_v1`, plus variants)
- forbid pool generation:
  - `src/tasks/forbid_pool_generation_task.py:ForbidPoolGenerationTask`
  - output: `ai_is.forbid_pool_comprehensive` (configured in the pipeline script)

Engineering note:
- `run_daily_data_pipeline.py` currently contains hard-coded dict configs (table names, dates, perf knobs).
  This is OK for fast iteration but hurts maintainability; prefer migrating to YAML under `configs/`.

### 5.3 NAS forbid ingestion
Entry script:
- `task_piplines/train_data_update/run_nas_data_pipeline.py`

Typical runs:
- latest only: `python task_piplines/train_data_update/run_nas_data_pipeline.py --latest`
- init/backfill: `python task_piplines/train_data_update/run_nas_data_pipeline.py --init`
- date/range: `python task_piplines/train_data_update/run_nas_data_pipeline.py --date YYYYMMDD`
            or `python task_piplines/train_data_update/run_nas_data_pipeline.py --range START END`
- resident schedule: `python task_piplines/train_data_update/run_nas_data_pipeline.py --schedule`

WSL note: UNC paths like `\\\\space\\forbid` are not accessible from WSL.
Mount the share and either:
- set `NAS_BASE_PATH`/`SPACE_BASE_PATH` for code paths that use `src/utils/nas_connection.py`
  or `src/utils/space_connection.py`, and
- update `NAS_FORBID_PATH` in `task_piplines/train_data_update/run_nas_data_pipeline.py`
  (or pass a mounted path into `NASForbidDataTask`) if needed.

### 5.4 Factor export to Space/NAS (IQ share)
Entry script:
- `task_piplines/train_data_update/factors_share_iq_pipline.py`
- legacy/production: `production_pipline/factors_share_iq_pipline.py`

Purpose:
- Given `model_path` + date range, generate model predictions (no backtest) and export per trading day.
- Resume behavior: detect last exported date from existing files and continue from next day unless disabled.

Core engine:
- `src/data_service/pipelines/factor_utils/factor_generator.py:FactorGenerator`
- experiment/schema resolution: `src/data_service/pipelines/factor_utils/config_utils.py:resolve_experiment_and_schema`
- DB feature fill (wide lag fetch): `src/data_service/pipelines/factor_utils/db_fetcher.py:fetch_wide_lag`
- DB provider: `src/data_service/data_loading/local_testdb_data.py:LocalTestDBDataProvider`
- table/field typing: `configs/db/local_db_configs.yaml`
- base config: `configs/backtest/model_backtest_config.py:ModelBacktestConfig`

CLI conventions (common flags in this repo):
- `--start_date YYYYMMDD`, `--end_date YYYYMMDD`
- `--model_path <dir>`, `--factor_name <name>`
- `--dataset_path <dir>` (optional: auto-resolve from experiment config)
- `--output_root <UNC or mounted path>`
- `--no_overwrite`, `--no_resume` (safety + reproducibility)

### 5.5 Strategy rebalance file export (XTJ)
Entry script:
- `task_piplines/singnals_output/factors_share_xtj_pipline.py`

Purpose:
- Generate predictions and export rebalance files on a fixed cycle (default 10 trading days).
- Output path defaults to a NAS UNC; in WSL you likely need a mounted path.

Common options:
- `--start_date`, `--end_date`
- `--data_source model|factor_files`
- `--factor_files_path <dir>`
- `--model_path <dir>`
- `--strgyid <id>`
- `--rebalance_freq` (default 10)
- `--weight_method equal|rank`, `--top_n` (0 or omitted -> all)
- `--output_root`, `--no_overwrite`, `--no_resume`

---

## 6) WSL: Mounting Space/NAS shares (required for pipelines that use UNC)

Linux/WSL cannot directly access Windows UNC paths like `\\\\space\\forbid`.
Mount the share as a Linux path and set env vars so code uses the mounted location.

### 6.1 One-time setup (first time)
```bash
sudo apt-get update && sudo apt-get install -y cifs-utils
sudo mkdir -p /mnt/space/forbid

# Credentials file (see configs/nas_disk/nas_config.yaml for account details)
sudo tee /etc/cifs-space-cred >/dev/null <<'EOF'
domain=space
username=YOUR_USERNAME
password=YOUR_PASSWORD
EOF
sudo chmod 600 /etc/cifs-space-cred
```

### 6.2 Mount + run (needed after WSL/Windows reboot)
```bash
sudo umount /mnt/space/forbid 2>/dev/null || true
sudo mount -t cifs //space/forbid /mnt/space/forbid \
  -o credentials=/etc/cifs-space-cred,vers=3.0,sec=ntlmssp,iocharset=utf8,uid=$(id -u),gid=$(id -g)

export NAS_BASE_PATH=/mnt/space/forbid
export SPACE_BASE_PATH=/mnt/space/forbid   # if your code distinguishes Space vs NAS

python master_scheduler.py
```

Notes:
- Put `export NAS_BASE_PATH=...` into `~/.bashrc` if you want it in every new terminal.
- If mount errors occur, try `vers=2.1` and check `dmesg | tail`.

---

## 7) Configuration System (YAML-driven, cache-aware)

### 7.1 How configs are loaded
- `ConfigLoader` reads under `configs/` and caches results.
- If config changes do not apply:
  - restart the process, or call `ConfigLoader.clear_cache()` / `db_config.reload_configs()` if available.

### 7.2 Adding a new table / data source (required checklist)
If you add a new feature/label/stats/forbid table and expect it to be used by Dataset Builder or FactorGenerator:

1. Create/ensure DB table exists (schema + indexes).
2. Register the table typing/mapping:
   - `configs/db/local_db_configs.yaml` (required for LocalTestDBDataProvider / DB fetch)
3. Add/adjust table metadata + code rules if needed:
   - `configs/db/table_config.yaml`
4. Add semantic field mapping if you want config-driven data loading:
   - `configs/field_mappings/<domain>.yaml`
   - ensure it is imported by `configs/field_mapping.yaml`
5. Smoke test with a small date range and verify row counts.

---

## 8) Data & DB Writing Rules (avoid silent corruption)

- Prefer idempotent writes:
  - use `TestDBManager.save_dataframe(mode='update', pk_fields=[...])`
  - ensure DB has PK/unique index that matches `pk_fields`
- Incremental jobs should use overlap windows (`overlap_days` like 3/20/60) to handle revisions/backfills.
- Never "overwrite everything" unless the task is explicitly a full rebuild.

---

## 9) Testing & Validation (minimum bar)

Before declaring a change "done":
- Syntax check: `python -m compileall src`
- If tests exist: `pytest -q`
- For pipeline changes:
  - run a smoke path (latest / narrow range) before full init/backfill
  - verify outputs and DB tables: row counts, date coverage, no duplicates

---

## 10) Troubleshooting Playbook

- `Unknown table: ai_is.xxx`
  - cause: not registered in `configs/db/local_db_configs.yaml`
  - fix: register it; possibly sync code format rules in `configs/db/table_config.yaml`

- WSL cannot access `\\\\space\\...`
  - mount share to `/mnt/...` and set `NAS_BASE_PATH` / `SPACE_BASE_PATH` (see section 6)

- Config changed but not effective
  - cause: `ConfigLoader` caching
  - fix: restart process or `ConfigLoader.clear_cache()` / `db_config.reload_configs()`

- `ModuleNotFoundError`
  - confirm correct venv python is running
  - confirm `PYTHONPATH` includes repo root (scheduler does this for subprocesses)

- `cx_Oracle` missing
  - install Oracle Instant Client, set `LD_LIBRARY_PATH`/`PATH`, then `pip install cx_Oracle==8.3.0`

- Encoding artifacts (progress bars like `a??`)
  - enforce UTF-8 env vars / terminal; consider `--progress-bar off` for pip

- Slow WSL I/O
  - move repo under WSL filesystem (`~/code/...`) and access via `\\wsl$` from Windows

---

## 11) Use the Codex Task Queue as a Lightweight Backlog

When tangential fixes appear mid-task:
- capture them as queued tasks instead of expanding scope immediately
- keep each queued task independently shippable
- return to the main goal unless explicitly asked to broaden scope
