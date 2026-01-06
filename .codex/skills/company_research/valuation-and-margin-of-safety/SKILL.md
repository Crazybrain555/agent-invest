---
name: valuation-and-margin-of-safety
description: "Estimate intrinsic value and margin of safety from market_snapshot and recast economic metrics. Use when producing valuation range, value_state.yaml, or investment memo for a ticker (Phase 1 v0.1)."
---

# valuation-and-margin-of-safety

## Overview
Compute EPV/multiple and simplified DCF scenarios from owner earnings (Phase 1). Output valuation range, value_state.yaml, and an investment memo.

## Inputs
- ticker (required)
- model_type (optional: epv|dcf|hybrid, default hybrid)
- policy_path (optional; default scripts/valuation_policy_phase1.yaml)
- as_of (optional, default today)
- force_refresh (optional)

## Hard dependencies (Phase 1)
- current/market_snapshot.yaml
- current/economic/core_metrics.parquet
- current/economic/economic_statements.parquet

## Outputs
- current/valuation/valuation.yaml
- current/valuation/valuation_model.csv
- current/valuation/value_state.yaml
- current/valuation/investment_memo.md
- runs/<run_id>/outputs/*

## Phase 1 defaults
- Use EPV via owner earnings * multiple with scenario adjustments.
- Use simplified DCF with default growth/discount and terminal multiple (optional; hybrid by default).
- Leave quality components null and set low confidence.

## Blocked conditions
- market_snapshot missing price or shares (and cannot derive shares from market_cap).
- core_metrics missing owner_earnings values.
- required economic artifacts missing or empty.

## MCP tools
- fs (read/write outputs under company_research)

## Scripts
- scripts/run.py orchestrates the workflow and writes outputs.
- scripts/model.py contains valuation math and model CSV construction.
- scripts/render_memo.py renders the memo from assets template.
- scripts/valuation_policy_phase1.yaml stores default assumptions.

## References
- references/valuation_schema.md for output schema and field definitions.
