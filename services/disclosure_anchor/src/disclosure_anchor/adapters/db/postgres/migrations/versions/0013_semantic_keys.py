"""semantic_keys recall column on document_unit

Revision ID: 0013_semantic_keys
Revises: 0012_provider_categories
Create Date: 2026-07-06
"""

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
revision: str = "0013_semantic_keys"
down_revision: Union[str, None] = "0012_provider_categories"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Codex round4 P1#1: semantic grouping must not swallow recall — a mixed
    # section can carry several topic keys (revenue_breakdown + inventory_...),
    # so the single-value semantic_key is complemented by the full key set.
    # GIN (jsonb_ops) supports `semantic_keys ? 'revenue_breakdown'`.
    op.add_column(
        "document_unit",
        sa.Column("semantic_keys", JSONB, nullable=True),
        schema=CORE_SCHEMA,
    )
    op.execute(
        f"CREATE INDEX ix_document_unit_semantic_keys ON {CORE_SCHEMA}.document_unit "
        "USING gin (semantic_keys) WHERE semantic_keys IS NOT NULL"
    )
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql(with_semantic_keys=True))
    _grant_view()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql(with_semantic_keys=False))
    _grant_view()
    op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.ix_document_unit_semantic_keys")
    op.drop_column("document_unit", "semantic_keys", schema=CORE_SCHEMA)


def _grant_view() -> None:
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.document_units_v1 TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def _document_units_view_sql(*, with_semantic_keys: bool) -> str:
    extra = "\n        u.semantic_keys," if with_semantic_keys else ""
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
    SELECT
        u.asset_id,
        u.document_id,
        u.processing_run_id,
        COALESCE(r.is_active, false) AS is_active_run,
        u.provider_document_id,
        u.payload_kind,
        u.heading_path,
        u.title,
        u.order_index,
        u.semantic_key,{extra}
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
        d.filing_type,
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref,
        'document_unit'::text AS asset_kind,
        u.created_at AS observed_at,
        CASE
            WHEN d.filing_type IN ('investor_relations','performance_briefing')
                THEN 'tier_0b'
            ELSE 'tier_0a'
        END AS source_tier,
        'G0'::text AS trace_level,
        d.raw_file_hash,
        u.query_projection_hash
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r ON r.processing_run_id = u.processing_run_id
    """
