"""document_unit applicability column and view upgrade

Revision ID: 0010_document_unit_applicability
Revises: 0009_ops_sync_queue_views
Create Date: 2026-07-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)

# revision identifiers, used by Alembic.
revision: str = "0010_document_unit_applicability"
down_revision: Union[str, None] = "0009_ops_sync_queue_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # User decision (2026-07-06): the section applicability declaration is a
    # first-class filter column, not a payload key — payload stays raw text.
    with op.batch_alter_table("document_unit", schema=CORE_SCHEMA) as batch:
        batch.add_column(sa.Column("applicability", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("page_no", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_document_unit_applicability",
            "applicability IN ('applicable','not_applicable')",
        )
    op.execute(
        f"CREATE INDEX ix_document_unit_applicability "
        f"ON {CORE_SCHEMA}.document_unit (applicability) "
        f"WHERE applicability IS NOT NULL"
    )
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql_0010())
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.source_refs_v1")
    op.execute(_source_refs_view_sql(with_applicability=True))
    _grant_view()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql_0007())
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.source_refs_v1")
    op.execute(_source_refs_view_sql(with_applicability=False))
    _grant_view()
    op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.ix_document_unit_applicability")
    with op.batch_alter_table("document_unit", schema=CORE_SCHEMA) as batch:
        batch.drop_constraint("ck_document_unit_applicability", type_="check")
        batch.drop_column("applicability")
        batch.drop_column("page_no")


def _grant_view() -> None:
    for view in ("document_units_v1", "source_refs_v1"):
        op.execute(
            f"GRANT SELECT ON {PUBLIC_SCHEMA}.{view} TO "
            f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
        )


def _source_refs_view_sql(*, with_applicability: bool) -> str:
    extra = "\n        u.applicability,\n        u.page_no," if with_applicability else ""
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
        u.quality_status,{extra}
        u.artifact_locator
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    """


def _view_body(extra_column: str) -> str:
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
        u.quality_status,{extra_column}
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
    """


def _document_units_view_sql_0010() -> str:
    return _view_body("\n        u.applicability,\n        u.page_no,")


def _document_units_view_sql_0007() -> str:
    return _view_body("")
