---
name: collect-company-facts
description: "Collect SEC filings for a ticker and write filings_index.yaml (plus raw/sec snapshots) under current/. Use when building or refreshing the SEC evidence pool."
version: v0.2
---

# collect-company-facts

## Overview
Build a stable, incremental, and traceable **SEC evidence pool** by collecting SEC filings and writing:
- `current/filings_index.yaml` (and optionally `current/filings_index.parquet`)
- `raw/sec/{accession}/...` snapshots for replay/traceability

News and papers are **out of scope for Phase 1** (planned to be handled by a standalone evidence DB/service via MCP).

## Inputs
- ticker (required)
- as_of (optional, default today)
- lookback_years (optional, default 10)
- force_refresh (optional, default false)
- persist_inputs (optional, default false; when true, store input payloads under runs/{run_id}/inputs)

## Hard dependencies
- company/{TICKER}/company.yaml with a valid cik
- `--demo` does not bypass `company.yaml.cik`; it only injects a minimal built-in filing when no filings payload is provided.

## Outputs
- current/filings_index.yaml
- current/filings_index.parquet (optional; written when pandas is available)
- raw/sec/{accession}/...
- current/events_index.parquet (optional; SEC event candidates pointers)
- runs/{run_id}/meta.yaml
- runs/{run_id}/result.yaml
- runs/{run_id}/needs.yaml (only when blocked)

## MCP tools
- sec_edgar_mcp.get_recent_filings
- sec_edgar_mcp.get_filing_content
- sec_edgar_mcp.get_filing_sections
- fs

## Workflow
1. Load company.yaml, extract cik and company_name.
2. SEC filings:
   - Query forms: 10-K, 10-Q, 8-K, DEF14A (optionally 20-F, 6-K).
   - Merge with existing filings_index; only new accessions are downloaded.
   - Store per-accession raw data under raw/sec/{accession}/ (Phase 1 can store sections or content only).
   - Write current/filings_index.yaml with as_of, form, filed_at, period_end, accession, has_xbrl, local_dir.
3. (Optional) Build `current/events_index.parquet` as a **SEC-only** event candidates pointer table for Phase 2.
4. Update artifacts_state.yaml and append evidence records.

## Incremental rules
- SEC filings: do not re-download accessions already in filings_index.

## Blocked conditions
- company.yaml missing or cik empty -> blocked, needs.yaml points to company-foundation.
- SEC filings list unavailable and no existing filings_index -> blocked.

## Script
- scripts/run.py implements the workflow and accepts optional JSON inputs for tool results.
- `--demo` keeps the real ticker/company dependency and is best treated as a lightweight data path, not a dependency-free mode.
- Prefer inline JSON flags to avoid temporary files; inputs are not persisted by default.
- If you must keep raw inputs for reproducibility, use --persist-inputs (keep payloads small).
