---
name: company-foundation
description: "Initialize a ticker research folder and write company.yaml plus market_snapshot.yaml; use when starting coverage or refreshing shares price EV, or when prompts say 初始化某个 ticker 的研究目录 / 需要 company.yaml / 需要 market snapshot / 更新 shares 和 EV."
---

# company-foundation

## Overview
Initialize the company research tree, resolve identity, and capture a market snapshot for valuation denominators.

## Inputs
- ticker (required)
- as_of (optional, default today)
- force_refresh (optional, default false)

## Outputs
- company/{TICKER}/company.yaml
- company/{TICKER}/current/market_snapshot.yaml
- company/{TICKER}/current/artifacts_state.yaml
- company/{TICKER}/runs/{run_id}/meta.yaml
- company/{TICKER}/runs/{run_id}/result.yaml
- append company/{TICKER}/current/evidence.jsonl

## MCP tools
- sec_edgar_mcp.get_cik_by_ticker
- sec_edgar_mcp.get_company_info
- trading_mcp.get_fundamental_stock_metrics
- fs (write under /home/help/mcp/work/company_research)

## Workflow
1. Initialize directories and run id (see `scripts/run.py`).
2. Skip identity when company.yaml has a valid cik and force_refresh is false.
3. Skip market snapshot when as_of matches and price + shares_outstanding exist.
4. Resolve identity via SEC tools; use fallback only if needed.
5. Fetch market snapshot via trading_mcp; compute market_cap, EV, net_debt when possible.
6. Write run outputs, promote to current on ok/partial, update artifacts_state, append evidence.

## References
- `references/schemas.md` for company.yaml and market_snapshot.yaml schemas.

## Script
- `scripts/run.py` implements the workflow and accepts optional JSON inputs for tool results.
