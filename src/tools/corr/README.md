# Corr Tool (Phase-0) README

This tool computes factor correlations across multiple long-format factor tables.
It is implemented in `src/tools/corr/` and exposed via `tools/corr_tool.py`.

Key design points:
- Factor identity is `(source_table, field_name)` only (no z_windows/lag).
- Sampling uses the Wind trading calendar and random days per year by default.
- Data loads are filtered by `field_name IN (...)` to avoid full table scans.

## Quick start

```bash
./.venv_wsl/bin/python tools/corr_tool.py --mode pair \
  --factor-a ai_is.quantitative_growth_profitability_signals::nde2p \
  --factor-b ai_is.quantitative_growth_profitability_signals::ne2e_q
```

```bash
./.venv_wsl/bin/python tools/corr_tool.py --mode one_to_many \
  --target ai_is.quantitative_growth_profitability_signals::nde2p \
  --group growth.profitability
```

```bash
./.venv_wsl/bin/python tools/corr_tool.py --mode many_to_many \
  --group growth.profitability \
  --years 2018,2020,2022
```

Outputs are written under `data/factor_correlation/<run_dir>/`. The run directory
name uses a compact focus tag (e.g. group or factor names) plus mode.

## Modes

- `pair`: compute correlation between exactly two factors.
  Use `--factor-a` and `--factor-b` (or two `--factor` values).

- `one_to_many`: compute correlation between one target factor and a list of
  candidates. Use `--target` + (`--group` or `--candidates`).

- `many_to_many`: compute a full correlation matrix for a factor set.
  Use `--group` or `--factor` (or neither to use all default tables/groups).

Mode aliases:
- `train-precheck` -> `many_to_many`
- `check-new-factor` -> `one_to_many`
- `adhoc` -> `many_to_many`

## Factor spec format

Factor specs can be written as:
- `table::field` (recommended):
  `ai_is.quantitative_growth_profitability_signals::nde2p`
- `field` only: `nde2p` (resolved from mapping; may be ambiguous)

If `--strict-name` is set and a field name exists in multiple tables,
the tool will error and list the ambiguous names.

## Parameter reference

### Core

- `--config`:
  Path to config YAML (default: `configs/tools/corr/default.yaml`).

- `--mode`:
  `pair | one_to_many | many_to_many` (see Modes).

- `--group`:
  Group selector from `configs/field_mappings/factor_mapping.yaml`.
  Example: `growth.profitability`.
  Multiple `--group` flags are allowed.

- `--factor`:
  Factor spec(s). Can be repeated or comma-separated.

- `--factor-a`, `--factor-b`:
  Pair mode explicit factor specs.

- `--target`:
  One-to-many target factor.

- `--candidates`:
  One-to-many candidate list (comma-separated factors).

- `--tables`:
  Restrict analysis to a subset of tables (comma-separated).

- `--include-table`, `--exclude-table`:
  Append to include/exclude table lists.

### Correlation compute

- `--method`:
  `cross_sectional` (default) or `time_series`.

- `--corr-type`:
  `pearson` or `spearman` (default).

- `--min-periods`:
  Minimum observations to compute a correlation (default: 30).

- `--threshold`:
  High-correlation threshold for `high_corr_pairs.parquet` (default: 0.7).

- `--strict-name`:
  If enabled, ambiguous field-only specs fail fast.

Config note:
- `use_gpu` in `configs/tools/corr/default.yaml` is not consumed by the current code,
  so setting it to `true` has no effect. YAML booleans are `true`/`false` (lowercase).

### Sampling

- `--years`:
  Fixed sampling years, comma-separated. Sets sampling mode to `fixed_years`.

- `--start-date`, `--end-date`:
  Override sampling to a date range (YYYYMMDD). Sets mode to `date_range`.

- `--random-days-per-year`:
  For `random_k_per_year`, number of days per year (default: 60).

- `--random-stocks-per-date`:
  Number of stocks sampled per date (default: 2000; set to 0/None to disable).

- `--random-seed`:
  Seed for reproducible sampling.

Config note:
- `sampling.trade_date_chunk_size` controls how many trade dates are pushed into each SQL `IN (...)` clause
  (default: 30). Smaller chunks reduce SQL length and memory spikes.

### Output & cache

- `--out-root`:
  Override output root directory.

- `--no-progress`:
  Disable tqdm progress bars (enabled by default).

- `--no-forbid-pool`:
  Disable filtering using `ai_is.forbid_pool_comprehensive`.

- `--no-cache`:
  Disable registry/edge cache writes.

## Output files (per run)

- `summary.json`:
  Effective config snapshot, selected trade dates, tables/factors used, and coverage stats.

- `corr_table.parquet`:
  Pair or one-to-many result table.

- `corr_table.xlsx`:
  Excel-friendly version with simplified factor names and a color scale.

- `corr_matrix.parquet`:
  Many-to-many correlation matrix (when factor count <= threshold).

- `corr_matrix.xlsx`:
  Excel-friendly matrix with simplified factor names and a color scale.

- `high_corr_pairs.parquet`:
  Edge list of high-correlation pairs (abs corr >= threshold).

- `recommendation.yaml`:
  Cluster-based recommendation (pick one representative per high-corr cluster).

## How the logic works (short version)

1) Resolve factor universe from:
   - `factor_mapping.yaml` groups or explicit factor specs
   - include/exclude table filters
2) Build sampling dates from Wind calendar and config sampling rules.
3) Load long-format data per table in trade-date chunks with `field_name IN (...)` and trade-date pushdown.
4) Optionally filter forbid pool and sample stocks per date.
5) Pivot to a wide factor matrix and compute correlations.
6) Write outputs and optional cache artifacts.

## Module layout (post-refactor)

`src/tools/corr/` is now split into smaller modules so each file owns one concern:

```
src/tools/corr/
  __init__.py
  cli.py                # argparse only, delegates to runner
  runner.py             # main orchestration (was cli.run)
  config.py             # config load + overrides (load-only today)
  sources.py            # factor mapping + resolution (unchanged)
  sampling.py           # calendar + date sampling (unchanged)
  compute.py            # matrix + corr stats (unchanged)
  cache.py              # registry + edge cache (unchanged)
  report.py             # parquet + Excel writers (unchanged)
  naming.py             # focus_tag + run_dir naming helpers
  loading.py            # fetch + chunk + forbid filter + stock sampling
  recommend.py          # cluster-based recommendation logic
  formats.py            # Excel-friendly name shaping and matrix/table transforms
```

Design goals:
- Keep `cli.py` thin and stable.
- Isolate data loading for performance tuning.
- Isolate recommendation logic for separate testing.
- Centralize output formatting in one place.

## Troubleshooting

- If a factor name is missing, try `table::field` to disambiguate.
- If a table is skipped, check `include_tables` and `exclude_tables`.
- If sampling returns no dates, confirm Wind calendar access.

## More scenarios

See `src/tools/corr/SCENARIOS.md` for examples using custom CSV/DataFrame inputs
and factor/table lists.
