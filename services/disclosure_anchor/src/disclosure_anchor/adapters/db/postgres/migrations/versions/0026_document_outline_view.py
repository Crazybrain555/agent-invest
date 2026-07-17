"""Document outline view: per-document heading-tree skeleton for L2.

Derived read surface only (design: docs/implementation/design/
document-outline-and-toc.md): one row per (document, heading_path) node with
per-node unit counts, payload-kind counts, distinct scalar keys, and page
span, aggregated from active-run units. No new tables, no stored data, no
events — the view regenerates with every publish.
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)

# revision identifiers, used by Alembic.
revision: str = "0026_document_outline_view"
down_revision: Union[str, None] = "0025_retrieval_search_projection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW_NAME = "document_outline_v1"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE VIEW {PUBLIC_SCHEMA}.{_VIEW_NAME} AS
        SELECT u.document_id,
               u.heading_path AS path,
               jsonb_array_length(u.heading_path) AS depth,
               count(*) AS unit_count,
               count(*) FILTER (WHERE u.payload_kind = 'table') AS table_count,
               count(*) FILTER (WHERE u.payload_kind = 'image') AS image_count,
               array_agg(DISTINCT u.semantic_key)
                   FILTER (WHERE u.semantic_key IS NOT NULL) AS semantic_keys,
               min(u.page_no) AS page_from,
               max(u.page_no) AS page_to,
               min(u.order_index) AS first_order_index,
               'document_outline.v1'::text AS contract_version
          FROM {CORE_SCHEMA}.document_unit u
          JOIN {CORE_SCHEMA}.processing_run pr
            ON pr.processing_run_id = u.processing_run_id
         WHERE pr.is_active
         GROUP BY u.document_id, u.heading_path
        """
    )
    for role in READ_ONLY_PUBLIC_ROLES:
        op.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW_NAME} TO {role}")


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{_VIEW_NAME}")
