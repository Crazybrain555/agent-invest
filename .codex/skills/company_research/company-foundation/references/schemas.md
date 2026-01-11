# Schemas for company-foundation outputs

## company.yaml

Required keys:
- ticker: string
- company_name: string or null
- cik: string or null
- exchange: string or null
- sic: string or null
- fiscal_year_end: string or null (MM-DD)
- currency: string (default USD)

Example:
```yaml
ticker: AAPL
company_name: Apple Inc.
cik: "0000320193"
exchange: NASDAQ
sic: "3571"
fiscal_year_end: "09-30"
currency: USD
```

## market_snapshot.yaml

Required keys:
- as_of: string (YYYY-MM-DD)
- price: number or null
- shares_outstanding: number or null
- shares_float: number or null
- market_cap: number or null
- enterprise_value: number or null
- net_debt: number or null
- source: string

Notes:
- `source` may be a single tool label (e.g., `alpaca.get_stock_snapshot`) or a mixed label
  like `mixed:alpaca.get_stock_snapshot+yfinance.get_stock_info` when multiple sources were merged.

Example:
```yaml
as_of: "2026-01-06"
price: 187.25
shares_outstanding: 15400000000
shares_float: 15300000000
market_cap: 2883650000000
enterprise_value: 2900000000000
net_debt: 16350000000
source: trading_mcp.get_fundamental_stock_metrics
```
