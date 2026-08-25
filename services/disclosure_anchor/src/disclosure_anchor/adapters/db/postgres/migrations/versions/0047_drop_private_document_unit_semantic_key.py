"""Drop the private scalar Unit route after a lossless preflight.

Revision ID: 0047_drop_unit_semantic_key
Revises: 0046_revoke_private_acl
Create Date: 2026-08-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA


revision: str = "0047_drop_unit_semantic_key"
down_revision: Union[str, None] = "0046_revoke_private_acl"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "document_unit"


def upgrade() -> None:
    connection = op.get_bind()
    mismatches = connection.execute(
        sa.text(
            f"""
            SELECT count(*)
              FROM {CORE_SCHEMA}.{_TABLE}
             WHERE NOT (
                 (semantic_key IS NULL AND semantic_keys IS NULL)
                 OR (
                     semantic_key IS NOT NULL
                     AND jsonb_typeof(semantic_keys) = 'array'
                     AND jsonb_array_length(semantic_keys) > 0
                     AND semantic_keys->>0 = semantic_key
                 )
             )
            """
        )
    ).scalar_one()
    if mismatches:
        raise RuntimeError(
            "0047 refuses to drop document_unit.semantic_key while plural state differs"
        )
    op.drop_constraint(
        "ck_document_unit_semantic_key_set",
        _TABLE,
        schema=CORE_SCHEMA,
        type_="check",
    )
    op.drop_index(
        "ix_document_unit_semantic_key",
        table_name=_TABLE,
        schema=CORE_SCHEMA,
    )
    op.drop_column(_TABLE, "semantic_key", schema=CORE_SCHEMA)
    op.create_check_constraint(
        "ck_document_unit_semantic_keys",
        _TABLE,
        "semantic_keys IS NULL OR ("
        "jsonb_typeof(semantic_keys) = 'array' AND "
        "jsonb_array_length(semantic_keys) BETWEEN 1 AND 8)",
        schema=CORE_SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_document_unit_semantic_keys",
        _TABLE,
        schema=CORE_SCHEMA,
        type_="check",
    )
    op.add_column(
        _TABLE,
        sa.Column("semantic_key", sa.String(length=128), nullable=True),
        schema=CORE_SCHEMA,
    )
    op.execute(
        f"UPDATE {CORE_SCHEMA}.{_TABLE} "
        "SET semantic_key = semantic_keys->>0 "
        "WHERE semantic_keys IS NOT NULL"
    )
    op.create_check_constraint(
        "ck_document_unit_semantic_key_set",
        _TABLE,
        "(semantic_key IS NULL AND semantic_keys IS NULL) OR ("
        "semantic_key IS NOT NULL AND "
        "jsonb_typeof(semantic_keys) = 'array' AND "
        "jsonb_array_length(semantic_keys) > 0 AND "
        "semantic_keys->>0 = semantic_key)",
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "ix_document_unit_semantic_key",
        _TABLE,
        ["semantic_key"],
        schema=CORE_SCHEMA,
    )
