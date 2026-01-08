---
name: extract-xbrl-timeseries
description: "Extract SEC financial statements into a Statement Atlas (facts.parquet, nodes, edges, paths, periods). Use when building XBRL timeseries for recast or when current/xbrl_atlas outputs are missing or need refresh."
---

# extract-xbrl-timeseries

## Overview
Build a minimal Statement Atlas (v0.1) to unblock recast-economic-statements. Use sec_edgar_mcp.get_financials payloads to create facts and a shallow tree (root -> line items). Keep output schema stable so v0.2 linkbase parsing can drop in later.

## Inputs
- ticker (required)
- as_of (optional, default today)
- lookback_years (optional, default 10)
- force_refresh (optional, default false)
- financials payloads from sec_edgar_mcp.get_financials (preferred)
- persist_inputs (optional, default false; when true, store input payloads under runs/{run_id}/inputs)

## Hard dependencies
- current/filings_index.yaml preferred (for accession -> period_end mapping)
- If filings_index is missing, require financials payloads or mark blocked

## Outputs
- current/xbrl_atlas/periods.yaml
- current/xbrl_atlas/nodes.parquet
- current/xbrl_atlas/edges.parquet
- current/xbrl_atlas/facts.parquet
- current/xbrl_atlas/paths.parquet
- runs/{run_id}/meta.yaml
- runs/{run_id}/result.yaml
- runs/{run_id}/needs.yaml (only when blocked)

## MCP tools
- sec_edgar_mcp.get_financials
- sec_edgar_mcp.discover_xbrl_concepts (optional for v0.2)
- sec_edgar_mcp.get_xbrl_concepts (optional for v0.2)
- fs

## Workflow (v0.1 shallow)
1. Load current/filings_index.yaml for accession -> period_end fallback.
2. Call sec_edgar_mcp.get_financials(identifier=ticker, statement_type="all").
3. Build facts.parquet with minimal fields (see references/atlas_schema.md).
4. Build shallow nodes/edges: one root per statement_type, children per line item.
5. Build paths.parquet as "{statement_type}/{label}" per fact.
6. Build periods.yaml mapping period_end to accession.
7. Update artifacts_state.yaml and evidence/questions when partial.

## Incremental rules
- If atlas files exist and no new payloads, skip unless force_refresh is set.
- Always refresh if force_refresh is true.

## Blocked conditions
- filings_index.yaml missing and no financials payloads available.
- financials payloads empty for all statements.

## Partial conditions
- Some statement types missing data.
- Some facts missing period_end or accession (log warnings + questions).

## Scripts
- scripts/run.py implements the workflow and accepts JSON payloads for tool results.
- Prefer inline JSON flags to avoid temporary files; inputs are not persisted by default.
- If you must keep raw inputs for reproducibility, use --persist-inputs (keep payloads small).
- scripts/build_atlas_minimal.py builds the v0.1 atlas from payloads.
- scripts/build_atlas_full.py is a placeholder for v0.2 linkbase parsing.

## References
- references/atlas_schema.md documents the output schema.
