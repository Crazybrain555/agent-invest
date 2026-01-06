# CLAUDE.md - Claude Code Agent Guide for AIQuantLab

**Last Updated:** 2025-12-29
**Project Type:** Quantitative Finance / ML Trading System
**Status:** Active Development

This document is optimized for Claude Code and other coding agents. For human-readable docs, see README.md.
Agent-critical context: build/run commands, repo invariants, operational gotchas, and "how to make changes safely".

---

## 0) Continuity Ledger (compaction-safe)

Maintain a single Continuity Ledger for this workspace in `CONTINUITY.md` (repo root).
The ledger is the canonical session briefing designed to survive context compaction; do not rely on
earlier chat text unless it's reflected in the ledger.

### How it works
- At the start of every assistant turn: read `CONTINUITY.md`, update it to reflect the latest
  goal/constraints/decisions/state, then proceed with the work.
- Update `CONTINUITY.md` again whenever any of these change: goal, constraints/assumptions,
  key decisions, progress state (Done/Now/Next), or important tool outcomes.
- Keep it short and stable: facts only, no transcripts. Prefer bullets. Mark uncertainty as
  `UNCONFIRMED` (never guess).

### CONTINUITY.md format (keep headings)
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

## 1) Agent Operating Mode

### Bias to action
- Default expectation: deliver working code + verification steps, not just a plan.
- If details are missing, make reasonable assumptions and implement a safe default.
- Do not produce mid-rollout status chatter; keep intermediate messages minimal.

### Explore efficiently
- Search before writing: reuse existing helpers/patterns.
- Prefer fast search (`rg`) and targeted reads (avoid reading huge files end-to-end).
- Batch file reads when possible; avoid one-file-at-a-time thrashing.

### Safe editing rules
- Never run destructive commands (`git reset --hard`, mass deletes, DB drops) unless explicitly asked.
- Do not "fix" by broad try/except or silent fallbacks; surface failures with actionable logs.
- Preserve existing behavior by default; gate behavior changes behind config/flags when possible.
- Keep UTF-8 explicit for file/log I/O (`encoding="utf-8"`).

### Presenting results
- Be concise. Reference paths as inline code (e.g. `src/scheduler/Dfzq_gru_scheduler.py`).
- When you changed code: explain what + why + how to verify.

---

## 2) Repo Layout (actual structure)

```
AIQuantLab/
├── src/                           # Core source code
│   ├── tasks/                     # Atomic reusable tasks (inherit BaseTask)
│   ├── scheduler/                 # Orchestration / sequencing / time-based logic
│   ├── data_service/              # Data loading, preprocessing, pipelines
│   │   ├── data_loading/          # LocalTestDBDataProvider, etc.
│   │   ├── preprocessing/         # Normalization, standardization
│   │   ├── pipelines/             # Dataset_builder, factor_utils, space_signals
│   │   └── data_saving/           # DB write utilities
│   ├── models/                    # ML models (GRU, Transformer, TSVIT)
│   ├── strategies/                # Trading strategy implementations
│   ├── backtesting/               # Backtest engine components
│   ├── tools/                     # Utility tools (e.g., corr/)
│   ├── utils/                     # Common utilities
│   └── train/                     # Training scripts
├── task_piplines/                 # Runnable CLI entry scripts (note: spelling)
│   ├── train_data_update/         # Daily automation scripts
│   │   ├── run_nas_data_pipeline.py
│   │   ├── run_daily_data_pipeline.py
│   │   └── factors_share_iq_pipline.py
│   └── singnals_output/           # Strategy signal exports
│       └── factors_share_xtj_pipline.py
├── backtest/                      # Backtest framework
│   ├── backtester/
│   ├── engine/
│   ├── metrics/
│   └── portfolio/
├── configs/                       # Central YAML config
│   ├── db/                        # table_config.yaml, local_db_configs.yaml
│   ├── field_mappings/            # Semantic field mappings
│   ├── backtest/
│   └── dataset/
├── Daily_pipline/                 # Legacy daily pipelines (reference)
├── production_pipline/            # Production factor export scripts (legacy)
├── tests/                         # Unit and integration tests
├── notebooks/                     # Jupyter notebooks for research
├── master_scheduler.py            # Daily automation scheduler
├── backtest_model.py              # Model backtest entry
├── run_tsvit.py                   # TSVIT training entry
├── AGENTS.md                      # Full agent guide (comprehensive)
├── CONTINUITY.md                  # Session continuity ledger
└── CLAUDE.md                      # This file
```

---

## 3) Environment Setup

### 3.1 Encoding defaults (required)
Always run commands in UTF-8 terminals.
- WSL/Linux:
  ```bash
  export PYTHONIOENCODING=UTF-8
  export PYTHONUTF8=1
  ```
- PowerShell:
  ```powershell
  chcp 65001 | Out-Null; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8
  ```

When reading/writing text files: always set `encoding="utf-8"` (or `utf-8-sig` for Excel-friendly CSVs).

### 3.2 WSL workflow (preferred)
```bash
cd /mnt/f/AIQuantLab
source .venv_wsl/bin/activate
# Use venv python explicitly:
./.venv_wsl/bin/python <script>.py
```

### 3.3 Windows workflow
```powershell
. .venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe <script>.py
```

### 3.4 Key packages (pinned versions)
- `numpy 2.2.4`, `pandas 2.3.0`, `pyarrow 20.0.0`, `duckdb 1.3.2`
- `tqdm 4.67.1`, `PyYAML 6.0.2`, `SQLAlchemy 2.0.39`
- `numba 0.61.2` + `llvmlite 0.44.0`, `matplotlib 3.10.1`
- `h5py 3.14.0`, `pymssql 2.3.2`, `psycopg 3.2.9`

---

## 4) Core Workflows & Commands

### 4.1 Master scheduler (daily automation)
```bash
# WSL:
.venv_wsl/bin/python master_scheduler.py

# Windows:
start_master_scheduler.bat
```

Runs daily ~00:30:
1. `task_piplines/train_data_update/run_nas_data_pipeline.py --latest`
2. `task_piplines/train_data_update/run_daily_data_pipeline.py --step all`
3. `task_piplines/train_data_update/factors_share_iq_pipline.py ...`

### 4.2 Daily data pipeline
```bash
python task_piplines/train_data_update/run_daily_data_pipeline.py --step all
# Steps: all/normalize/standardize/label/forbid
```

Step -> task mapping:
- normalize: `src/tasks/market_price_norm_data_initialization.py:MarketPriceNormDataTask`
- standardize: `src/tasks/standardization_parameter_generation.py:StandardParamsGenerator`
- label: `src/tasks/label_generation_task.py:LabelGenerationTask`
- forbid: `src/tasks/forbid_pool_generation_task.py:ForbidPoolGenerationTask`

### 4.3 NAS forbid ingestion
```bash
# Latest only:
python task_piplines/train_data_update/run_nas_data_pipeline.py --latest

# Init/backfill:
python task_piplines/train_data_update/run_nas_data_pipeline.py --init

# Date range:
python task_piplines/train_data_update/run_nas_data_pipeline.py --range START END
```

### 4.4 Factor export (IQ)
```bash
python task_piplines/train_data_update/factors_share_iq_pipline.py \
  --start_date YYYYMMDD --end_date YYYYMMDD \
  --model_path <dir> --factor_name <name>
```

### 4.5 Correlation tool
```bash
python -m src.tools.corr.cli [command] [options]
# See CORR_TOOL_CORE_API_PLAN.md for API details
```

---

## 5) Code Conventions

### 5.1 Python standards
- Follow PEP 8 style guide
- Use type hints for function signatures
- Maximum line length: 100 characters
- Use docstrings (Google or NumPy style)

### 5.2 Naming conventions
- Classes: `PascalCase` (e.g., `MeanReversionStrategy`)
- Functions/Methods: `snake_case` (e.g., `calculate_sharpe_ratio`)
- Constants: `UPPER_SNAKE_CASE` (e.g., `TRADING_DAYS_PER_YEAR`)
- Private methods: prefix with `_` (e.g., `_validate_data`)

### 5.3 Imports organization
```python
# Standard library
import os
from datetime import datetime

# Third-party
import numpy as np
import pandas as pd

# Local application
from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.tasks.base import BaseTask
```

### 5.4 DataFrame conventions
- Use pandas for time series data
- Always use datetime index for price data
- Column names: lowercase with underscores (`close_price`, `volume`)
- Standard OHLCV: `open`, `high`, `low`, `close`, `volume`
- Always set `encoding="utf-8"` when reading/writing

---

## 6) Configuration System

### 6.1 Config loading
- `ConfigLoader` reads under `configs/` and caches results.
- If config changes do not apply: restart process or call `ConfigLoader.clear_cache()`

### 6.2 Adding a new table (checklist)
1. Create/ensure DB table exists (schema + indexes)
2. Register in `configs/db/local_db_configs.yaml`
3. Add metadata in `configs/db/table_config.yaml` if needed
4. Add field mapping in `configs/field_mappings/<domain>.yaml`
5. Ensure imported by `configs/field_mapping.yaml`
6. Smoke test with small date range

---

## 7) Data & DB Writing Rules

- Prefer idempotent writes:
  - use `TestDBManager.save_dataframe(mode='update', pk_fields=[...])`
  - ensure DB has PK/unique index that matches `pk_fields`
- Incremental jobs use overlap windows (`overlap_days` like 3/20/60) for revisions/backfills
- Never "overwrite everything" unless explicitly a full rebuild

---

## 8) Quantitative Finance Considerations

### Data integrity
- **Survivorship Bias**: Avoid using only currently-listed stocks
- **Corporate Actions**: Adjust for splits, dividends, mergers
- **Point-in-Time Data**: Ensure no future information leaks into the past
- **Look-Ahead Bias**: Never use future data in historical signals

### Backtesting standards
- Use realistic transaction costs
- Include slippage models
- Account for market impact (for large orders)
- Use proper train/test splits

### Risk metrics to track
- Returns: Total return, annualized return, rolling returns
- Risk: Standard deviation, beta, maximum drawdown
- Risk-Adjusted: Sharpe ratio, Sortino ratio, Calmar ratio
- Trade Stats: Win rate, profit factor, average trade duration

---

## 9) Testing & Validation

Before declaring a change "done":
```bash
# Syntax check:
python -m compileall src

# If tests exist:
pytest -q

# For pipeline changes:
# - Run smoke path (latest / narrow range) first
# - Verify outputs and DB tables: row counts, date coverage, no duplicates
```

---

## 10) Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Unknown table: ai_is.xxx` | Not registered | Add to `configs/db/local_db_configs.yaml` |
| WSL cannot access `\\space\...` | UNC not supported | Mount share to `/mnt/...`, set `NAS_BASE_PATH` |
| Config changed but not effective | ConfigLoader caching | Restart process or `ConfigLoader.clear_cache()` |
| `ModuleNotFoundError` | Wrong venv/PYTHONPATH | Check venv active, set `PYTHONPATH` to repo root |
| Encoding artifacts (`a??`) | Non-UTF8 terminal | Set UTF-8 env vars (see section 3.1) |
| Slow WSL I/O | Repo on Windows FS | Move to `~/code/...` in WSL filesystem |

---

## 11) Git Workflow

### Branch naming
- Feature: `feature/<name>`
- Research: `research/<hypothesis>`
- Hotfix: `hotfix/<issue>`
- Claude: `claude/<topic>-<session-id>`

### Commit message convention
```
<type>(<scope>): <subject>

Types: feat, fix, docs, style, refactor, test, chore
Example: feat(corr): add one-to-many correlation compute
```

---

## 12) Reference Documents

- `AGENTS.md` - Full agent guide (comprehensive version)
- `CONTINUITY.md` - Session continuity ledger
- `CORR_TOOL_CORE_API_PLAN.md` - Correlation tool API design
- `README.md` - Human-readable project overview
