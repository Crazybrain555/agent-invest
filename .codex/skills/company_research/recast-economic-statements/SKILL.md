---
name: recast-economic-statements
description: "Transform GAAP statements into economic statements (NOPAT, ROIC, FCF, Owner Earnings) from Statement Atlas facts. Use when mapping xbrl_atlas facts into economic outputs or when current/economic outputs are missing or need refresh."
---

# recast-economic-statements

## Overview
Recast Statement Atlas facts into a minimal economic statement layer (v0.1 Phase 1). Use label-based mapping with concept priority and persist the mapping decisions in recast_policy for traceability.

## Inputs
- ticker (required)
- policy_version (optional, default v0.1)
- policy_path (optional; default scripts/recast_policy_default.yaml)
- force_refresh (optional)

## Hard dependencies
- current/xbrl_atlas/facts.parquet
- current/xbrl_atlas/periods.yaml
- current/xbrl_atlas/nodes.parquet
- current/xbrl_atlas/edges.parquet

## Outputs
- current/economic/recast_policy.yaml
- current/economic/economic_statements.parquet
- current/economic/core_metrics.parquet
- runs/<run_id>/outputs/*

## v0.1 strategy (Phase 1 minimal)
1. Load facts + periods.
2. Map facts to targets using label-based matching with concept priority.
3. Compute core metrics: owner_earnings, maintenance_capex, fcf, plus simplified NOPAT/ROIC.
4. Write recast_policy.yaml with chosen labels/concepts, rationale, and fallback_used.

## Core metrics (Phase 1)
- owner_earnings = CFO - maintenance_capex + normalized_wc (normalized_wc defaults to 0)
- maintenance_capex = depr_floor (floor_ratio) vs capex_floor_ratio
- fcf = CFO - capex
- nopat = operating_income * (1 - tax_rate)
- invested_capital = total_assets - cash - non_interest_bearing_current_liabilities
  - fallback: total_debt + total_equity - cash

## Blocked conditions
- Atlas artifacts missing or facts/periods empty.

## Partial conditions
- CFO or capex missing in the latest period.

## Scripts
- scripts/run.py orchestrates the workflow and writes outputs.
- scripts/recast.py contains mapping + metric logic.
- scripts/recast_policy_default.yaml stores default selectors and formulas.

## References
- references/mapping_heuristics.md for label/concept matching rules.
