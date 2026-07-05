"""intake_public read views and supersede-lookup index

Revision ID: 0002_public_views
Revises: 0001_initial
Create Date: 2026-07-06
"""

from typing import Sequence, Union

from alembic import op

from asset_intake.db.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    OPS_SCHEMA,
    PUBLIC_SCHEMA,
    READER_ROLE,
)

revision: str = "0002_public_views"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PUBLIC_VIEWS = ("data_assets_v1", "source_accesses_v1", "change_events_v1")

# Public read contract (protocol §3.6/§3.11): full scope keys on every asset row;
# producer-internal columns (dedup_key) and raw error text stay private.
VIEW_SQL: list[str] = [
    f"""
    CREATE VIEW {PUBLIC_SCHEMA}.data_assets_v1 AS
    SELECT
        a.asset_id,
        'asset://asset_intake/v1/' || a.asset_kind || '/' || a.asset_id AS asset_uri,
        a.asset_kind,
        a.payload_kind,
        a.contract_version,
        a.content_hash,
        a.subject_candidates,
        a.title,
        a.semantic_key,
        a.material_type,
        a.event_time,
        a.published_at,
        a.report_period,
        a.observed_at,
        a.source_access_id AS source_ref,
        a.provider,
        a.adapter,
        a.tool,
        a.source_tier,
        a.trace_level,
        a.locator,
        a.raw_asset_ref,
        a.processing_run_id AS producer_action_ref,
        a.sensitivity,
        a.payload,
        a.quality_status,
        a.is_active,
        a.superseded_by
    FROM {CORE_SCHEMA}.data_asset a
    """,
    f"""
    CREATE VIEW {PUBLIC_SCHEMA}.source_accesses_v1 AS
    SELECT
        s.access_id,
        s.provider,
        s.adapter,
        s.adapter_version,
        s.dataset_key,
        s.tool,
        s.query_params,
        s.query_params_hash,
        s.provider_as_of,
        s.observed_at,
        s.result_status,
        s.result_count,
        s.processing_run_id AS producer_action_ref
    FROM {CORE_SCHEMA}.source_access s
    """,
    f"""
    CREATE VIEW {PUBLIC_SCHEMA}.change_events_v1 AS
    SELECT
        e.seq AS change_seq,
        e.event_id,
        'asset_intake' AS source,
        e.event_kind,
        e.subject_ref,
        e.asset_id,
        e.processing_run_id,
        e.payload,
        e.occurred_at
    FROM {OPS_SCHEMA}.outbox_event e
    """,
]

INDEX_SQL = (
    f"CREATE INDEX ix_data_asset_provider_semantic_active "
    f"ON {CORE_SCHEMA}.data_asset (provider, semantic_key) WHERE is_active"
)

GRANT_SQL = [
    f"GRANT SELECT ON ALL TABLES IN SCHEMA {PUBLIC_SCHEMA} TO {APP_ROLE}",
    f"GRANT SELECT ON ALL TABLES IN SCHEMA {PUBLIC_SCHEMA} TO {READER_ROLE}, {FUTURE_L2_READER_ROLE}",
]


def upgrade() -> None:
    for statement in VIEW_SQL:
        op.execute(statement)
    op.execute(INDEX_SQL)
    for statement in GRANT_SQL:
        op.execute(statement)


def downgrade() -> None:
    for view in PUBLIC_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{view}")
    op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.ix_data_asset_provider_semantic_active")
