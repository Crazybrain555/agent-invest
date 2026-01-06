# Statement Atlas Schema (v0.1)

Keep the schema stable so v0.2 can replace the tree builder without breaking downstream steps.

## periods.yaml

```
periods:
  - period_end: "YYYY-MM-DD"
    fiscal_period: "FY" | "Q1" | "Q2" | "Q3" | "Q4" | null
    accession: "0000000000-00-000000" | null
```

## facts.parquet (minimum columns)

| column | description |
| --- | --- |
| period_end | Period end date (string ISO). |
| fiscal_period | FY/Q1/Q2/Q3/Q4 when available. |
| statement_type | IS/BS/CF/CI/Equity/OTHER. |
| role_uri | XBRL role URI when available, else null. |
| concept | XBRL tag or synthetic:{slug(label)}. |
| label | Line item label. |
| value | Numeric value when available. |
| unit | Unit (USD if missing). |
| decimals | XBRL decimals/precision when available. |
| accession | Filing accession. |
| context_id | XBRL context id when available. |
| fact_id | Stable unique id per fact. |
| dimensions | JSON string of axis->member when available. |

## nodes.parquet

| column | description |
| --- | --- |
| node_id | Unique node id. |
| statement_type | IS/BS/CF/CI/Equity/OTHER. |
| role_uri | XBRL role URI or null. |
| concept | Concept for this node (root uses synthetic). |
| label | Display label. |
| depth | 0 for root, 1 for line items. |
| order | Order within statement. |

## edges.parquet

| column | description |
| --- | --- |
| parent_node_id | Root node id. |
| child_node_id | Line item node id. |
| arcrole | "presentation" in v0.1. |
| weight | 1.0 in v0.1. |

## paths.parquet

| column | description |
| --- | --- |
| node_id | Line item node id. |
| period_end | Period end date (string ISO). |
| statement_type | IS/BS/CF/CI/Equity/OTHER. |
| path_str | "{statement_type}/{label}". |
| value | Value for the fact. |
| accession | Filing accession. |
