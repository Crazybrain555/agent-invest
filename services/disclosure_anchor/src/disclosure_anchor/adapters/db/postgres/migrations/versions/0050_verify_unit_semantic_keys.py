"""Verify plural-only Unit route storage after the immutable 0047 transition.

Revision ID: 0050_verify_unit_routes
Revises: 0049_app_head_read
Create Date: 2026-08-22

0047 is already applied to the development database and remains append-only.
Fresh online upgrades are protected by the Alembic environment's NULL-safe
preflight before 0047 runs.  This forward revision asserts only facts that can
still be proven after the scalar column has been removed; it never pretends to
reconstruct an already-dropped scalar value.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA


revision: str = "0050_verify_unit_routes"
down_revision: Union[str, None] = "0049_app_head_read"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "document_unit"
_ROUTE_COLUMNS = {"semantic_keys": 8, "section_keys": None}


def upgrade() -> None:
    connection = op.get_bind()
    scalar_columns = connection.execute(
        sa.text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table "
            "AND column_name = 'semantic_key'"
        ),
        {"schema": CORE_SCHEMA, "table": _TABLE},
    ).scalar_one()
    if scalar_columns:
        raise RuntimeError(
            "0050 requires plural-only document_unit storage; semantic_key remains"
        )
    for column, max_items in _ROUTE_COLUMNS.items():
        invalid = connection.execute(
            sa.text(_invalid_route_array_sql(column=column, max_items=max_items))
        ).scalar_one()
        if invalid:
            raise RuntimeError(
                f"0050 found {invalid} invalid document_unit.{column} rows"
            )


def downgrade() -> None:
    # Assertion-only revision: crossing back changes only the Alembic head.
    pass


def _invalid_route_array_sql(*, column: str, max_items: int | None) -> str:
    if column not in _ROUTE_COLUMNS or _ROUTE_COLUMNS[column] != max_items:
        raise ValueError("unsupported Unit route column")
    size_check = f"jsonb_array_length({column}) < 1"
    if max_items is not None:
        size_check += f" OR jsonb_array_length({column}) > {max_items}"
    return f"""
        SELECT count(*)
          FROM {CORE_SCHEMA}.{_TABLE}
         WHERE CASE
             WHEN {column} IS NULL THEN false
             WHEN jsonb_typeof({column}) IS DISTINCT FROM 'array' THEN true
             ELSE
                 {size_check}
                 OR EXISTS (
                     SELECT 1
                       FROM jsonb_array_elements({column}) AS item(value)
                      WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                         OR btrim(item.value #>> '{{}}') = ''
                         OR (item.value #>> '{{}}') !~ '^[a-z][a-z0-9_]{{0,127}}$'
                 )
                 OR (
                     SELECT count(*)
                       FROM jsonb_array_elements_text({column}) AS item(value)
                 ) <> (
                     SELECT count(DISTINCT item.value)
                       FROM jsonb_array_elements_text({column}) AS item(value)
                 )
         END
    """
