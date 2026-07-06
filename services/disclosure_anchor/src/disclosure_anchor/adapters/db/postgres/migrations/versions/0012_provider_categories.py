"""cninfo provider category dimension and document categories view

Revision ID: 0012_provider_categories
Revises: 0011_mixed_units_and_active_flag
Create Date: 2026-07-06

Round3 P1#6: document.filing_type is a coarse internal bucket only; the
provider-native classification (p_info3005, 2135 codes) becomes a real
dimension. F006V segments are orthogonal facets (发布机构/市场/公告类型), so
the view exposes ordinal without inventing an is_primary semantic.
"""

import json
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)

# revision identifiers, used by Alembic.
revision: str = "0012_provider_categories"
down_revision: Union[str, None] = "0011_mixed_units_and_active_flag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SERVICE_ROOT = Path(__file__).resolve().parents[7]
_CATEGORY_SNAPSHOT = (
    _SERVICE_ROOT / "docs" / "architecture" / "cninfo-announcement-categories.json"
)


def upgrade() -> None:
    table = op.create_table(
        "provider_category",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("category_code", sa.String(length=32), nullable=False),
        sa.Column("parent_category_code", sa.String(length=32), nullable=True),
        sa.Column("category_name", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("provider", "category_code"),
        schema=CORE_SCHEMA,
    )
    op.bulk_insert(table, _cninfo_category_rows())
    op.execute(_document_categories_view_sql())
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.document_categories_v1 TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )
    op.execute(
        f"GRANT SELECT ON {CORE_SCHEMA}.provider_category TO {APP_ROLE}, {READER_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_categories_v1")
    op.drop_table("provider_category", schema=CORE_SCHEMA)


def _cninfo_category_rows() -> list[dict[str, object]]:
    snapshot = json.loads(_CATEGORY_SNAPSHOT.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for record in snapshot["categories"]:
        rows.append(
            {
                "provider": "cninfo",
                "category_code": str(record["sortcode"]),
                "parent_category_code": record.get("parentcode") or None,
                "category_name": str(record["sortname"]),
                "valid_from": record.get("start") or None,
                "valid_to": record.get("end") or None,
                "raw_payload": record,
            }
        )
    return rows


def _document_categories_view_sql() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.document_categories_v1 AS
    SELECT
        d.document_id,
        d.provider,
        d.provider_document_id,
        seg.category_code,
        seg.ordinal::int AS ordinal,
        pc.category_name,
        pc.parent_category_code,
        'document_category.v1'::text AS contract_version
    FROM {CORE_SCHEMA}.document d
    CROSS JOIN LATERAL unnest(
        string_to_array(coalesce(d.provider_metadata->>'raw_category', ''), '||')
    ) WITH ORDINALITY AS seg(category_code, ordinal)
    LEFT JOIN {CORE_SCHEMA}.provider_category pc
        ON pc.provider = d.provider AND pc.category_code = seg.category_code
    WHERE seg.category_code <> ''
    """
