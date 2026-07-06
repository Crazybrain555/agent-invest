"""mixed payload_kind and is_active_run on public views

Revision ID: 0011_mixed_units_and_active_flag
Revises: 0010_document_unit_applicability
Create Date: 2026-07-06
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

# revision identifiers, used by Alembic.
revision: str = "0011_mixed_units_and_active_flag"
down_revision: Union[str, None] = "0010_document_unit_applicability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Round3 P0#1: business-semantic units (meeting proposals, short filings)
    # carry ordered text/table parts in one payload — payload_kind 'mixed'.
    op.drop_constraint(
        "ck_document_unit_payload_kind", "document_unit", schema=CORE_SCHEMA, type_="check"
    )
    op.create_check_constraint(
        "ck_document_unit_payload_kind",
        "document_unit",
        "payload_kind in ('text','table','qa','mixed')",
        schema=CORE_SCHEMA,
    )
    # Round3 P1#7: DB-direct consumers (L2) must be able to filter active-run
    # rows without knowing the processing_run join; the contract model already
    # promises is_active_run.
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql(with_active_flag=True))
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.source_refs_v1")
    op.execute(_source_refs_view_sql(with_active_flag=True))
    _grant_view()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql(with_active_flag=False))
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.source_refs_v1")
    op.execute(_source_refs_view_sql(with_active_flag=False))
    _grant_view()
    op.drop_constraint(
        "ck_document_unit_payload_kind", "document_unit", schema=CORE_SCHEMA, type_="check"
    )
    op.create_check_constraint(
        "ck_document_unit_payload_kind",
        "document_unit",
        "payload_kind in ('text','table','qa')",
        schema=CORE_SCHEMA,
    )


def _grant_view() -> None:
    for view in ("document_units_v1", "source_refs_v1"):
        op.execute(
            f"GRANT SELECT ON {PUBLIC_SCHEMA}.{view} TO "
            f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
        )


def _active_join() -> str:
    return (
        f"\n    JOIN {CORE_SCHEMA}.processing_run r "
        "ON r.processing_run_id = u.processing_run_id"
    )


def _source_refs_view_sql(*, with_active_flag: bool) -> str:
    extra = "\n        COALESCE(r.is_active, false) AS is_active_run," if with_active_flag else ""
    join = _active_join() if with_active_flag else ""
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
        u.processing_run_id,{extra}
        u.payload_kind,
        u.heading_path,
        u.title,
        u.content_hash AS unit_content_hash,
        u.quality_status,
        u.applicability,
        u.page_no,
        u.artifact_locator
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id{join}
    """


def _document_units_view_sql(*, with_active_flag: bool) -> str:
    extra = "\n        COALESCE(r.is_active, false) AS is_active_run," if with_active_flag else ""
    join = _active_join() if with_active_flag else ""
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
    SELECT
        u.asset_id,
        u.document_id,
        u.processing_run_id,{extra}
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
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id{join}
    """
