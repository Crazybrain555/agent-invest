"""ops sync/download queue views for the worker loop

Revision ID: 0009_ops_sync_queue_views
Revises: 0008_unit_builder_provenance
Create Date: 2026-07-06
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    OPS_SCHEMA,
)

# revision identifiers, used by Alembic.
revision: str = "0009_ops_sync_queue_views"
down_revision: Union[str, None] = "0008_unit_builder_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VIEWS = ("sync_due_v1", "pending_download_v1")


def upgrade() -> None:
    op.execute(_sync_due_view_sql())
    op.execute(_pending_download_view_sql())
    for view in VIEWS:
        op.execute(f"GRANT SELECT ON {OPS_SCHEMA}.{view} TO {APP_ROLE}")


def downgrade() -> None:
    for view in VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {OPS_SCHEMA}.{view}")


def _sync_due_view_sql() -> str:
    # window_end NULL (no checkpoint yet) means due; the scope_key format is
    # pinned to the 07 writer: "<company_id>:p_info3015".
    return f"""
    CREATE VIEW {OPS_SCHEMA}.sync_due_v1 AS
    SELECT tc.tracked_company_id, tc.company_id, tc.security_id,
           sc.cursor->>'window_end' AS window_end
      FROM {CORE_SCHEMA}.tracked_company tc
      LEFT JOIN {CORE_SCHEMA}.source_checkpoint sc
        ON sc.provider='cninfo' AND sc.scope_key = tc.company_id || '\\:p_info3015'
      WHERE tc.status='active'
    """


def _pending_download_view_sql() -> str:
    # Candidates = latest snapshot row per provider_document_id across all
    # index accesses (both the WebAPI and the web fallback channel); facts
    # only — the >=N retry cut-off is applied by the queries.py helper.
    return f"""
    CREATE VIEW {OPS_SCHEMA}.pending_download_v1 AS
    WITH candidate AS (
        SELECT DISTINCT ON (c->>'provider_document_id')
               c->>'provider_document_id' AS provider_document_id,
               c->>'download_url' AS download_url,
               c->>'title' AS title,
               c->>'announcement_date' AS announcement_date,
               sa.source_access_id,
               sa.company_id,
               sa.accessed_at,
               c AS candidate
          FROM {CORE_SCHEMA}.source_access sa,
               jsonb_array_elements(sa.result_snapshot->'candidates') AS c
         WHERE sa.provider='cninfo'
           AND sa.provider_interface IN ('cninfo:p_info3015', 'cninfo:hisAnnouncement')
           AND sa.status='ok'
         ORDER BY c->>'provider_document_id', sa.accessed_at DESC
    )
    SELECT cand.provider_document_id,
           cand.download_url,
           cand.title,
           cand.announcement_date,
           cand.source_access_id,
           cand.company_id,
           cand.candidate,
           (SELECT count(*) FROM {CORE_SCHEMA}.source_access f
             WHERE f.provider='cninfo'
               AND f.provider_interface='cninfo:download_pdf'
               AND f.status='failed'
               AND f.query_params->>'provider_document_id' = cand.provider_document_id
           ) AS failed_download_count
      FROM candidate cand
     WHERE NOT EXISTS (SELECT 1 FROM {CORE_SCHEMA}.document d
             WHERE d.provider='cninfo'
               AND d.provider_document_id = cand.provider_document_id)
       AND NOT EXISTS (SELECT 1 FROM {CORE_SCHEMA}.source_access nf
             WHERE nf.provider='cninfo'
               AND nf.provider_interface='cninfo:download_pdf'
               AND nf.status='failed'
               AND nf.query_params->>'provider_document_id' = cand.provider_document_id
               AND nf.error IS NOT NULL
               AND (nf.error)::jsonb->>'retryable' = 'false')
    """
