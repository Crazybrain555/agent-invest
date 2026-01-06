# Mapping Heuristics (v0.1)

Use these heuristics when mapping Statement Atlas facts into economic statements.

## Matching priority
1. Concept match (exact, case-insensitive) when concept is present and stable.
2. Label match (substring match on normalized label).

## Label normalization
- Lowercase the label.
- Replace non-alphanumeric characters with spaces.
- Collapse multiple spaces.

## Statement scoping
- Constrain matches to the expected statement_type (IS, BS, CF).
- Do not mix statement types unless explicitly allowed in policy.

## Value selection
- When multiple matches exist, prefer the row with the largest absolute value.
- Keep the original sign for CFO; take absolute value for capex.

## Fallbacks
- If no match is found, leave the line item null and record fallback_used.
- For invested_capital, fallback to (total_debt + total_equity - cash) when total_assets is missing.

## Traceability
- Record chosen_labels and chosen_concepts per target in recast_policy.
- Capture rationale and whether fallbacks were used.
