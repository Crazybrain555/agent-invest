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
- persist_inputs (optional, default false; when true, store input payloads under runs/{run_id}/inputs)

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
- sec_edgar_mcp.get_recent_filings (annual form period_of_report → fiscal_year_end)
- sec_edgar_mcp.get_xbrl_concepts (optional shares outstanding fallback)
- trading_mcp.get_fundamental_stock_metrics
- alpaca.get_stock_latest_trade / alpaca.get_stock_snapshot (price)
- alpaca.get_asset (exchange fallback)
- yfinance.get_stock_info (fallback for shares/market cap/EV)
- fs (write under /home/help/mcp/work/company_research)

## Workflow
1. Initialize directories and run id (see `scripts/run.py`).
2. Skip identity when company.yaml has a valid cik and force_refresh is false.
3. Skip market snapshot when as_of matches and price + shares_outstanding exist.
4. Resolve identity via SEC tools; use fallback only if needed.
5. Fetch market snapshot with a multi-source chain; keep `market_cap` from a primary source and cross-check vs `price*shares_outstanding` (switch only on large divergence); fill `enterprise_value` in USD when currency is aligned (or FX-converted).
6. Fill `company.yaml.exchange` via Alpaca asset exchange (preferred) with Yahoo fallback; infer `company.yaml.fiscal_year_end` from SEC annual filing `period_of_report`.
7. Write run outputs, promote to current on ok/partial, update artifacts_state, append evidence.

## Market snapshot sourcing (recommended)
Priority order (use whichever data you can fetch; earlier sources fill missing fields first):
1. **Alpaca**: `get_stock_latest_trade` or `get_stock_snapshot` for `price` (low-frequency price data).
2. **trading_mcp**: `get_fundamental_stock_metrics` for `marketCap` / `sharesOutstanding` when available.
3. **SEC**: `get_xbrl_concepts` for `CommonStockSharesOutstanding` or `EntityCommonStockSharesOutstanding`
   (use form type 10-K / 20-F as appropriate).
4. **Yahoo**: `get_stock_info` as fallback for `sharesOutstanding`, `floatShares`, `marketCap`, `enterpriseValue`.

Notes:
- Alpaca does not provide shares outstanding; use trading_mcp/SEC/Yahoo for share counts.
- `market_cap` is taken from the first available `marketCap` (e.g., Yahoo), and cross-checked against
  `price*shares_outstanding`. Only switch to derived when the relative gap is large.
- For ADRs (e.g., BABA), Yahoo's `enterpriseValue` can be in financial currency (CNY) while `marketCap` is USD.
  Use an FX payload to convert, or treat `enterprise_value` as optional unless you confirm currency alignment.
- `market_snapshot.yaml` is USD-denominated (`currency: USD`). Values in other currencies should be converted before writing.

## company.yaml fields (recommended)
- `exchange`: primary listing exchange (prefer `alpaca.get_asset`, fallback to `yfinance.get_stock_info` exchange fields).
- `fiscal_year_end`: derived from the most recent annual SEC filing `period_of_report` (10-K / 20-F).

### FX payload (optional, for ADRs)
If your market payload includes `enterpriseValue` with `financialCurrency != currency`, you can pass an FX payload:
```json
{"fx_from":"USD","fx_to":"CNY","fx_rate":6.9831,"source":"yfinance.get_stock_info"}
```
The runner will invert the rate as needed (e.g., CNY→USD) to normalize `enterprise_value`.

## References
- `references/schemas.md` for company.yaml and market_snapshot.yaml schemas.

## Script
- `scripts/run.py` implements the workflow and accepts optional JSON inputs for tool results.
- Prefer `--identity-json/--market-json` to avoid writing temporary files. Inputs are not persisted by default.
- `--market-json` / `--market-path` can be repeated; earlier payloads take priority when filling fields.
- If you must retain raw inputs for reproducibility, use `--persist-inputs` (keep payloads small).
