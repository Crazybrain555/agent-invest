"""prod-readiness hardening: status CHECK, download-queue indexes, view rebuild

Revision ID: 0023_prod_readiness_hardening
Revises: 0022_prescale_hardening
Create Date: 2026-07-14

Three changes from the 2026-07-14 pre-production review (round23):

1. ``tracked_company.status`` gains a CHECK constraint — a misspelled status
   silently dropped the company out of every queue (the write path validates
   too; this is the last line of defense for direct SQL).
2. Expression/partial indexes for the download queue's correlated scans:
   ``failed_download_count`` and the terminal-failure exclusion both correlate
   ``source_access`` rows through ``query_params->>'provider_document_id'``
   with no index support, and the candidate CTE scans the cninfo index
   snapshots by (provider, interface, status) — cost grew with sync history.
3. ``pending_download_v1`` rebuild, facts only:
   - Snapshot preference: latest CODED snapshot (non-empty raw_category)
     wins over a newer uncoded web snapshot, so registration keeps F006V
     provenance instead of permanently erasing it. Known trade-off: while
     the API channel is down, an older coded snapshot masks the newer web
     snapshot's file-size hint, delaying signature_differs re-fetch until
     the API channel recovers — accepted and self-healing (round23).
   - New fact columns ``already_registered`` and ``signature_differs``
     (candidate file_signature_hint vs the registered document's stored
     file_signature). The worker's re-fetch predicate for same-TEXTID file
     replacement lives in queries.py on top of these facts; only the
     self-limiting signature trigger is used by the resident loop (a
     supersede refreshes the stored signature and the flag clears).
   The threshold predicates stay in queries.py per the 08 §1 contract.
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    OPS_SCHEMA,
)

# revision identifiers, used by Alembic.
revision: str = "0023_prod_readiness_hardening"
down_revision: Union[str, None] = "0022_prescale_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.tracked_company "
        "ADD CONSTRAINT ck_tracked_company_status "
        "CHECK (status IN ('active','paused'))"
    )
    op.execute(
        f"CREATE INDEX ix_source_access_download_pid ON {CORE_SCHEMA}.source_access "
        "((query_params->>'provider_document_id')) "
        "WHERE provider = 'cninfo' "
        "AND provider_interface = 'cninfo:download_pdf'"
    )
    op.execute(
        f"CREATE INDEX ix_source_access_index_snapshots ON {CORE_SCHEMA}.source_access "
        "(provider_interface, accessed_at) "
        "WHERE provider = 'cninfo' "
        "AND provider_interface IN ('cninfo:p_info3015', 'cninfo:hisAnnouncement') "
        "AND status = 'ok'"
    )
    op.execute(f"DROP VIEW IF EXISTS {OPS_SCHEMA}.pending_download_v1")
    op.execute(_pending_download_view_sql())
    op.execute(
        f"GRANT SELECT ON {OPS_SCHEMA}.pending_download_v1 TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {OPS_SCHEMA}.pending_download_v1")
    op.execute(_pending_download_view_0009_sql())
    op.execute(
        f"GRANT SELECT ON {OPS_SCHEMA}.pending_download_v1 TO {APP_ROLE}"
    )
    op.execute(
        f"DROP INDEX IF EXISTS {CORE_SCHEMA}.ix_source_access_index_snapshots"
    )
    op.execute(
        f"DROP INDEX IF EXISTS {CORE_SCHEMA}.ix_source_access_download_pid"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.tracked_company "
        "DROP CONSTRAINT IF EXISTS ck_tracked_company_status"
    )


def _pending_download_view_sql() -> str:
    # Facts only; retry cut-offs and re-fetch eligibility live in queries.py.
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
         ORDER BY c->>'provider_document_id',
                  -- Coded snapshots first: a newer code-less web snapshot
                  -- must not erase F006V classification provenance at
                  -- registration time (round23).
                  (CASE WHEN COALESCE(c->>'raw_category', '') <> '' THEN 0 ELSE 1 END),
                  sa.accessed_at DESC
    )
    SELECT cand.provider_document_id,
           cand.download_url,
           cand.title,
           cand.announcement_date,
           cand.source_access_id,
           cand.company_id,
           cand.candidate,
           (d.document_id IS NOT NULL) AS already_registered,
           (
             d.document_id IS NOT NULL
             AND (cand.candidate->'file_signature_hint'->>'file_size') IS NOT NULL
             AND (d.provider_metadata->'file_signature'->>'file_size') IS NOT NULL
             AND (cand.candidate->'file_signature_hint'->>'file_size')
                 IS DISTINCT FROM
                 (d.provider_metadata->'file_signature'->>'file_size')
           ) AS signature_differs,
           (SELECT count(*) FROM {CORE_SCHEMA}.source_access f
             WHERE f.provider='cninfo'
               AND f.provider_interface='cninfo:download_pdf'
               AND f.status='failed'
               AND f.query_params->>'provider_document_id' = cand.provider_document_id
           ) AS failed_download_count
      FROM candidate cand
      LEFT JOIN LATERAL (
            SELECT d.document_id, d.provider_metadata
              FROM {CORE_SCHEMA}.document d
             WHERE d.provider='cninfo'
               AND d.provider_document_id = cand.provider_document_id
             ORDER BY d.created_at DESC, d.document_id DESC
             LIMIT 1
           ) d ON TRUE
     WHERE (d.document_id IS NULL
            OR (
                (cand.candidate->'file_signature_hint'->>'file_size') IS NOT NULL
                AND (d.provider_metadata->'file_signature'->>'file_size') IS NOT NULL
                AND (cand.candidate->'file_signature_hint'->>'file_size')
                    IS DISTINCT FROM
                    (d.provider_metadata->'file_signature'->>'file_size')
            ))
       AND NOT EXISTS (SELECT 1 FROM {CORE_SCHEMA}.source_access nf
             WHERE nf.provider='cninfo'
               AND nf.provider_interface='cninfo:download_pdf'
               AND nf.status='failed'
               AND nf.query_params->>'provider_document_id' = cand.provider_document_id
               AND nf.error IS NOT NULL
               AND (nf.error)::jsonb->>'retryable' = 'false')
    """


def _pending_download_view_0009_sql() -> str:
    # Byte-faithful restore of the 0009 view for downgrade round-trips.
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
