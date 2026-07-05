"""SQL template whitelist validation (framework v1.2 F7).

Templates live in dataset YAML provider mappings; the physical table is always
the ``{{table}}`` placeholder (resolved from the provider catalog's active
table at runtime, never hard-coded). Rendering/execution arrives with the real
provider adapter; validation runs at registry load time and in tests.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from asset_intake.providers.registry import SafetySpec, SqlTemplate

TABLE_PLACEHOLDER = "{{table}}"
_BIND_PARAM_RE = re.compile(r"(?<!:):([a-z_][a-z0-9_]*)", re.IGNORECASE)
_SELECT_STAR_RE = re.compile(r"select\s+\*", re.IGNORECASE)


def validate_template(template: "SqlTemplate", safety: "SafetySpec") -> list[str]:
    """Return a list of problems (empty = valid)."""

    issues: list[str] = []
    statement = template.statement

    if TABLE_PLACEHOLDER not in statement:
        issues.append(f"statement must use {TABLE_PLACEHOLDER} instead of a hard-coded table name")

    if safety.single_statement_only and ";" in statement:
        issues.append("statement must be a single statement (';' found)")

    if safety.forbid_select_star and _SELECT_STAR_RE.search(statement):
        issues.append("SELECT * is forbidden")

    upper = statement.upper()
    for token in safety.deny_tokens:
        if token == ";":
            continue  # handled above
        if re.search(rf"\b{re.escape(token.upper())}\b", upper):
            issues.append(f"deny token '{token}' present")

    bound = set(_BIND_PARAM_RE.findall(statement))
    declared = set(template.params)
    undeclared = bound - declared
    if undeclared:
        issues.append(f"bound params not declared: {sorted(undeclared)}")
    unused = declared - bound
    if unused:
        issues.append(f"declared params unused: {sorted(unused)}")

    for predicate in template.required_predicates:
        if predicate.upper() not in upper:
            issues.append(f"required predicate '{predicate}' absent from statement")

    if template.max_rows > safety.default_max_rows:
        issues.append(
            f"max_rows {template.max_rows} exceeds provider default_max_rows {safety.default_max_rows}"
        )

    return issues
