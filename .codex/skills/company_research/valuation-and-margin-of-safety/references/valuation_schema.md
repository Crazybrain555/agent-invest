# Valuation Schema (Phase 1)

## Outputs
- current/valuation/valuation.yaml
- current/valuation/valuation_model.csv
- current/valuation/value_state.yaml
- current/valuation/investment_memo.md

## valuation.yaml (Phase 1)
Fields are designed for traceability and reproducibility.

```yaml
as_of: YYYY-MM-DD
policy_version: v0.1-phase1
inputs:
  policy_path: ".../valuation_policy_phase1.yaml"
  policy_hash: "..."
  market_snapshot_hash: "..."
  core_metrics_hash: "..."
  economic_statements_hash: "..."
methods:
  model_type: hybrid
  method_weights:
    epv: 0.6
    dcf: 0.4
  methods_used: ["epv", "dcf"]
assumptions:
  epv:
    multiple:
      bear: 10
      base: 14
      bull: 18
    owner_earnings_adjustment:
      bear: -0.2
      base: 0.0
      bull: 0.2
  dcf:
    years: 5
    growth:
      bear: 0.0
      base: 0.03
      bull: 0.06
    discount_rate:
      bear: 0.12
      base: 0.105
      bull: 0.095
    terminal_multiple:
      bear: 12
      base: 15
      bull: 18
results:
  owner_earnings_base: 123000000
  owner_earnings_scenarios:
    bear: 98400000
    base: 123000000
    bull: 147600000
  epv_value:
    bear: 984000000
    base: 1722000000
    bull: 2656800000
  dcf_value:
    bear: 890000000
    base: 1550000000
    bull: 2400000000
  intrinsic_value_per_share:
    bear: 10.2
    base: 20.5
    bull: 31.8
  margin_of_safety:
    bear: -0.10
    base: 0.15
    bull: 0.45
notes:
  margin_of_safety_formula: "(IV - price) / price"
```

## valuation_model.csv (Phase 1)
Columns:
- scenario
- owner_earnings
- owner_earnings_adjustment
- epv_multiple
- epv_value
- dcf_growth
- dcf_discount_rate
- dcf_terminal_multiple
- dcf_value
- combined_value
- intrinsic_value_per_share
- margin_of_safety
- weight_epv
- weight_dcf

## value_state.yaml (Phase 1)
Minimal fields required by the Phase 1 pipeline. Quality components are null and confidence is low.

```yaml
ticker: ABC
as_of: YYYY-MM-DD
market:
  price: 12.34
  shares_outstanding: 100000000
  market_cap: 1234000000
  enterprise_value: 1500000000
profit:
  base_period: "TTM"
  owner_earnings: 180000000
  owner_earnings_per_share: 1.80
  nopat: 210000000
  invested_capital: 1200000000
  roic: 0.175
  fcf: 160000000
  maintenance_capex_estimate: 60000000
quality:
  coefficient_base: 0.5
  implied_multiple_base: 14.0
  discount_rate_base: 0.105
  components:
    financial_quality: null
    moat: null
    governance_capital_allocation: null
    balance_sheet_resilience: null
  confidence: 0.3
valuation:
  intrinsic_value_per_share:
    bear: 10.0
    base: 20.0
    bull: 30.0
  margin_of_safety_base: 0.62
links:
  memo: "current/valuation/investment_memo.md"
  valuation_yaml: "current/valuation/valuation.yaml"
```
