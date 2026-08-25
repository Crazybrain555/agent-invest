"""Outline nodes aggregate the full route arrays, not the internal lead key.

Revision ID: 0042_outline_full_route_keys
Revises: 0041_drop_public_semantic_key
Create Date: 2026-08-20

``document_outline_v1`` previously aggregated the private scalar lead key, so
a multi-route Unit surfaced only its first topic in the outline and the node's
recall surface was silently narrower than ``semantic_keys``.  Aggregate the
distinct elements of the total ``semantic_keys`` arrays instead; column names,
order, SQL types (elements cast back to varchar(128), matching the private
column), and the ``document_outline.v1`` contract label all stay unchanged.
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)

revision: str = "0042_outline_full_route_keys"
down_revision: Union[str, None] = "0041_drop_public_semantic_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW_NAME = "document_outline_v1"


def upgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW_NAME}")
    op.execute(_outline_view_sql(aggregate_full_arrays=True))
    _grant()


def downgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW_NAME}")
    op.execute(_outline_view_sql(aggregate_full_arrays=False))
    _grant()


def _grant() -> None:
    for role in READ_ONLY_PUBLIC_ROLES:
        op.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW_NAME} TO {role}")


def _outline_view_sql(*, aggregate_full_arrays: bool) -> str:
    semantic_keys = (
        "keys.semantic_keys"
        if aggregate_full_arrays
        else (
            "array_agg(DISTINCT u.semantic_key) "
            "FILTER (WHERE u.semantic_key IS NOT NULL) AS semantic_keys"
        )
    )
    if not aggregate_full_arrays:
        return f"""
        CREATE VIEW {PUBLIC_SCHEMA}.{_VIEW_NAME} AS
        SELECT u.document_id,
               u.heading_path AS path,
               jsonb_array_length(u.heading_path) AS depth,
               count(*) AS unit_count,
               count(*) FILTER (WHERE u.payload_kind = 'table') AS table_count,
               count(*) FILTER (WHERE u.payload_kind = 'image') AS image_count,
               {semantic_keys},
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
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.{_VIEW_NAME} AS
    WITH base AS (
        SELECT u.document_id,
               u.heading_path AS path,
               jsonb_array_length(u.heading_path) AS depth,
               count(*) AS unit_count,
               count(*) FILTER (WHERE u.payload_kind = 'table') AS table_count,
               count(*) FILTER (WHERE u.payload_kind = 'image') AS image_count,
               min(u.page_no) AS page_from,
               max(u.page_no) AS page_to,
               min(u.order_index) AS first_order_index
          FROM {CORE_SCHEMA}.document_unit u
          JOIN {CORE_SCHEMA}.processing_run pr
            ON pr.processing_run_id = u.processing_run_id
         WHERE pr.is_active
         GROUP BY u.document_id, u.heading_path
    ),
    node_keys AS (
        SELECT u.document_id,
               u.heading_path AS path,
               array_agg(DISTINCT (route.key)::varchar(128)
                         ORDER BY (route.key)::varchar(128)) AS semantic_keys
          FROM {CORE_SCHEMA}.document_unit u
          JOIN {CORE_SCHEMA}.processing_run pr
            ON pr.processing_run_id = u.processing_run_id
         CROSS JOIN LATERAL jsonb_array_elements_text(u.semantic_keys)
                    AS route(key)
         WHERE pr.is_active
           AND u.semantic_keys IS NOT NULL
         GROUP BY u.document_id, u.heading_path
    )
    SELECT base.document_id,
           base.path,
           base.depth,
           base.unit_count,
           base.table_count,
           base.image_count,
           node_keys.semantic_keys,
           base.page_from,
           base.page_to,
           base.first_order_index,
           'document_outline.v1'::text AS contract_version
      FROM base
      LEFT JOIN node_keys
        ON node_keys.document_id = base.document_id
       AND node_keys.path = base.path
    """
