"""disclosure_topics second-level classification on document

Revision ID: 0014_disclosure_topics
Revises: 0013_semantic_keys
Create Date: 2026-07-07
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
revision: str = "0014_disclosure_topics"
down_revision: Union[str, None] = "0013_semantic_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Round9 user ruling: filing_type stays the coarse contract bucket; a
    # second classification level derived from F006V codes (topic_map.json)
    # makes the 2085-of-2135-categories-into-'other' space filterable and
    # drives adjustable default monitoring/parse parameters.
    op.add_column(
        "document",
        sa.Column("disclosure_topics", JSONB(none_as_null=True), nullable=True),
        schema=CORE_SCHEMA,
    )
    op.execute(
        f"CREATE INDEX ix_document_disclosure_topics ON {CORE_SCHEMA}.document "
        "USING gin (disclosure_topics) WHERE disclosure_topics IS NOT NULL"
    )
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.documents_v1")
    op.execute(_documents_view_sql(with_topics=True))
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql(with_topics=True))
    _grant_views()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql(with_topics=False))
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.documents_v1")
    op.execute(_documents_view_sql(with_topics=False))
    _grant_views()
    op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.ix_document_disclosure_topics")
    op.drop_column("document", "disclosure_topics", schema=CORE_SCHEMA)


def _grant_views() -> None:
    for view in ("documents_v1", "document_units_v1"):
        op.execute(
            f"GRANT SELECT ON {PUBLIC_SCHEMA}.{view} TO "
            f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
        )


def _documents_view_sql(*, with_topics: bool) -> str:
    extra = "\n        d.disclosure_topics," if with_topics else ""
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.documents_v1 AS
    SELECT
        d.document_id,
        d.provider,
        d.provider_document_id,
        s.security_code,
        s.exchange,
        d.filing_type,{extra}
        d.title,
        d.announcement_date,
        d.report_period,
        d.raw_file_hash,
        d.status,
        d.current_processing_run_id,
        d.created_at,
        d.updated_at,
        'document.v1'::text AS contract_version,
        d.company_id AS company_ref,
        d.security_id AS security_ref,
        d.source_access_id AS source_ref,
        d.supersedes_document_id,
        d.correction_of_document_id,
        sb.document_id AS superseded_by_document_id,
        d.provider_metadata
    FROM {CORE_SCHEMA}.document d
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    LEFT JOIN LATERAL (
        SELECT x.document_id
          FROM {CORE_SCHEMA}.document x
         WHERE x.supersedes_document_id = d.document_id
         ORDER BY x.created_at DESC, x.document_id DESC
         LIMIT 1
    ) sb ON true
    """


def _document_units_view_sql(*, with_topics: bool) -> str:
    extra = "\n        d.disclosure_topics," if with_topics else ""
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
        u.semantic_key,
        u.semantic_keys,
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
        d.filing_type,{extra}
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
