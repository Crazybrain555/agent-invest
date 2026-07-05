"""v0.7 terminology convergence: rename local column names to protocol names

document_unit.document_unit_id -> asset_id, document_unit.unit_kind ->
payload_kind, outbox_event.event_type -> event_kind, outbox_event.
document_unit_id -> asset_id. The three affected public views are recreated
without the transitional alias columns that 0005 introduced. ID values and row
data are unchanged.

Revision ID: 0006_v07_terminology_convergence
Revises: 0005_public_view_scope_contracts
Create Date: 2026-07-03
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    OPS_SCHEMA,
    PUBLIC_SCHEMA,
)

# revision identifiers, used by Alembic.
revision: str = "0006_v07_terminology_convergence"
down_revision: Union[str, None] = "0005_public_view_scope_contracts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RENAMED_VIEWS = ("document_units_v1", "source_refs_v1", "change_events_v1")


def upgrade() -> None:
    for view in RENAMED_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{view}")

    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.document_unit "
        "RENAME COLUMN document_unit_id TO asset_id"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.document_unit "
        "RENAME COLUMN unit_kind TO payload_kind"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.document_unit "
        "RENAME CONSTRAINT ck_document_unit_kind TO ck_document_unit_payload_kind"
    )
    op.execute(
        f"ALTER TABLE {OPS_SCHEMA}.outbox_event "
        "RENAME COLUMN event_type TO event_kind"
    )
    op.execute(
        f"ALTER TABLE {OPS_SCHEMA}.outbox_event "
        "RENAME COLUMN document_unit_id TO asset_id"
    )

    op.execute(_document_units_view_sql())
    op.execute(_source_refs_view_sql())
    op.execute(_change_events_view_sql())


def downgrade() -> None:
    for view in RENAMED_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{view}")

    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.document_unit "
        "RENAME COLUMN asset_id TO document_unit_id"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.document_unit "
        "RENAME COLUMN payload_kind TO unit_kind"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.document_unit "
        "RENAME CONSTRAINT ck_document_unit_payload_kind TO ck_document_unit_kind"
    )
    op.execute(
        f"ALTER TABLE {OPS_SCHEMA}.outbox_event "
        "RENAME COLUMN event_kind TO event_type"
    )
    op.execute(
        f"ALTER TABLE {OPS_SCHEMA}.outbox_event "
        "RENAME COLUMN asset_id TO document_unit_id"
    )

    op.execute(_document_units_view_sql_0005())
    op.execute(_source_refs_view_sql_0004())
    op.execute(_change_events_view_sql_0005())


def _document_units_view_sql() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
    SELECT
        u.asset_id,
        u.document_id,
        u.processing_run_id,
        u.provider_document_id,
        u.payload_kind,
        u.heading_path,
        u.title,
        u.order_index,
        u.semantic_key,
        u.payload,
        u.content_hash,
        u.structure_hash,
        u.quality_status,
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
        u.document_id AS parent_ref
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    """


def _source_refs_view_sql() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.source_refs_v1 AS
    SELECT
        'disclosure_anchor'::text AS service,
        'source_ref.v1'::text AS contract_version,
        u.asset_id,
        d.source_access_id,
        u.document_id,
        d.provider,
        d.provider_document_id,
        d.raw_file_hash,
        u.processing_run_id,
        u.payload_kind,
        u.heading_path,
        u.title,
        u.content_hash AS unit_content_hash,
        u.quality_status,
        u.artifact_locator
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    """


def _change_events_view_sql() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.change_events_v1 AS
    SELECT
        e.seq,
        e.event_id,
        e.event_kind,
        e.document_id,
        e.processing_run_id,
        e.asset_id,
        e.payload,
        e.occurred_at,
        CASE
            WHEN e.payload ->> 'change_kind' IN ('observed', 'materialized')
                THEN e.payload ->> 'change_kind'
            WHEN e.event_kind LIKE '%observed%' THEN 'observed'
            ELSE 'materialized'
        END AS change_kind,
        e.created_at
    FROM {OPS_SCHEMA}.outbox_event e
    """


def _document_units_view_sql_0005() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
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
        u.created_at,
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
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    """


def _source_refs_view_sql_0004() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.source_refs_v1 AS
    SELECT
        'disclosure_anchor'::text AS service,
        'source_ref.v1'::text AS contract_version,
        u.document_unit_id,
        d.source_access_id,
        u.document_id,
        d.provider,
        d.provider_document_id,
        d.raw_file_hash,
        u.processing_run_id,
        u.unit_kind,
        u.heading_path,
        u.title,
        u.content_hash AS unit_content_hash,
        u.quality_status,
        u.artifact_locator
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    """


def _change_events_view_sql_0005() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.change_events_v1 AS
    SELECT
        e.seq,
        e.event_id,
        e.event_type,
        e.document_id,
        e.processing_run_id,
        e.document_unit_id,
        e.payload,
        e.occurred_at,
        e.event_type AS event_kind,
        CASE
            WHEN e.payload ->> 'change_kind' IN ('observed', 'materialized')
                THEN e.payload ->> 'change_kind'
            WHEN e.event_type LIKE '%observed%' THEN 'observed'
            ELSE 'materialized'
        END AS change_kind,
        e.created_at
    FROM {OPS_SCHEMA}.outbox_event e
    """
