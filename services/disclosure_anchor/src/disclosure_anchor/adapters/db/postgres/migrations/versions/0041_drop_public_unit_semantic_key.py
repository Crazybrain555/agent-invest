"""Drop the redundant scalar ``semantic_key`` from the public Unit view.

Revision ID: 0041_drop_public_semantic_key
Revises: 0040_unit_route_arrays_nonnull
Create Date: 2026-08-20

The scalar was only ever ``semantic_keys[0]``.  Keeping it in the public
contract invites consumers to filter on the lead key alone and silently lose
multi-route recall.  Private persistence keeps its ``semantic_key`` column,
hashing, and receipts unchanged; only the public v1 surface narrows to the
two total route arrays (39 columns).
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)


revision: str = "0041_drop_public_semantic_key"
down_revision: Union[str, None] = "0040_unit_route_arrays_nonnull"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW = "document_units_v1"


def upgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW}")
    op.execute(_document_units_view_sql(include_scalar_semantic_key=False))
    _grant_view()


def downgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW}")
    op.execute(_document_units_view_sql(include_scalar_semantic_key=True))
    _grant_view()


def _grant_view() -> None:
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW} TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def _document_units_view_sql(*, include_scalar_semantic_key: bool) -> str:
    scalar_semantic_key = (
        "u.semantic_key,\n        " if include_scalar_semantic_key else ""
    )
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.{_VIEW} AS
    SELECT
        u.asset_id,
        u.document_id,
        u.processing_run_id,
        COALESCE(r.is_active, false) AS is_active_run,
        u.provider_document_id,
        u.payload_kind,
        u.heading_path,
        (SELECT string_agg(seg.value, ' > ' ORDER BY seg.ordinality)
           FROM jsonb_array_elements_text(u.heading_path)
                WITH ORDINALITY AS seg(value, ordinality)
        ) AS heading_path_text,
        u.title,
        u.order_index,
        {scalar_semantic_key}COALESCE(u.semantic_keys, '[]'::jsonb) AS semantic_keys,
        COALESCE(u.section_keys, '[]'::jsonb) AS section_keys,
        u.payload,
        u.content_hash,
        u.structure_hash,
        u.quality_status,
        u.applicability,
        u.page_no,
        u.artifact_locator,
        u.created_at,
        'document_unit.v1'::text AS contract_version,
        d.company_id AS company_ref,
        d.security_id AS security_ref,
        s.security_code,
        s.exchange,
        COALESCE(d.class_filing_type, 'other') AS filing_type,
        d.class_disclosure_topics AS disclosure_topics,
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref,
        'document_unit'::text AS asset_kind,
        u.created_at AS observed_at,
        CASE
            WHEN COALESCE(d.class_filing_type, 'other')
                 IN ('investor_relations','performance_briefing')
                THEN 'tier_0b'
            ELSE 'tier_0a'
        END AS source_tier,
        'G0'::text AS trace_level,
        d.raw_file_hash,
        u.query_projection_hash,
        CASE
            WHEN u.payload_kind = 'text'
                 AND u.payload = '{{"text": ""}}'::jsonb
                 AND u.title IS NOT NULL
                THEN 'heading_only'
            WHEN u.payload_kind = 'text'
                 AND u.payload = '{{"text": ""}}'::jsonb
                THEN 'empty'
            ELSE 'content'
        END AS body_status
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r
      ON r.processing_run_id = u.processing_run_id
    """
