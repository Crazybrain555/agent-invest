"""public view scope contracts

Revision ID: 0005_public_view_scope_contracts
Revises: 0004_review_hardening_contracts
Create Date: 2026-07-02
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    OPS_SCHEMA,
    PUBLIC_SCHEMA,
)

# revision identifiers, used by Alembic.
revision: str = "0005_public_view_scope_contracts"
down_revision: Union[str, None] = "0004_review_hardening_contracts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(_document_units_view_sql(include_v07_columns=True))
    op.execute(_change_events_view_sql(include_v07_columns=True))


def downgrade() -> None:
    op.execute(_document_units_view_sql(include_v07_columns=False))
    op.execute(_change_events_view_sql(include_v07_columns=False))


def _document_units_view_sql(*, include_v07_columns: bool) -> str:
    v07_columns = ""
    joins = ""
    if include_v07_columns:
        v07_columns = """
        ,
        'document_unit.v1'::text AS contract_version,
        d.company_id AS company_ref,
        d.security_id AS security_ref,
        s.security_code,
        s.exchange,
        d.filing_type,
        d.report_period,
        d.announcement_date,
        u.unit_kind AS payload_kind,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref
        """
        joins = f"""
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
        """

    return f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
    SELECT
        u.document_unit_id,
        u.document_id,
        u.processing_run_id,
        u.provider_document_id,
        u.unit_kind,
        u.heading_path,
        u.title,
        u.order_index,
        u.semantic_key,
        u.payload,
        u.content_hash,
        u.structure_hash,
        u.quality_status,
        u.artifact_locator,
        u.created_at
        {v07_columns}
    FROM {CORE_SCHEMA}.document_unit u
    {joins}
    """


def _change_events_view_sql(*, include_v07_columns: bool) -> str:
    v07_columns = ""
    if include_v07_columns:
        v07_columns = """
        ,
        e.event_type AS event_kind,
        CASE
            WHEN e.payload ->> 'change_kind' IN ('observed', 'materialized')
                THEN e.payload ->> 'change_kind'
            WHEN e.event_type LIKE '%observed%' THEN 'observed'
            ELSE 'materialized'
        END AS change_kind,
        e.created_at
        """

    return f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.change_events_v1 AS
    SELECT
        e.seq,
        e.event_id,
        e.event_type,
        e.document_id,
        e.processing_run_id,
        e.document_unit_id,
        e.payload,
        e.occurred_at
        {v07_columns}
    FROM {OPS_SCHEMA}.outbox_event e
    """
