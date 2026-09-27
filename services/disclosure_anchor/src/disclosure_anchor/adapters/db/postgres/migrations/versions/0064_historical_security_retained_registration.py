"""Historical security bindings and retained-archive registration receipts.

Revision ID: 0064_retained_registration
Revises: 0063_parse_requeue_decision
Create Date: 2026-09-26

A download attempt can fail after its PDF was archived, e.g. when the index
candidate carries a historical exchange code the ledger does not know. Such
failures are retryable=false and stay permanently out of the download queue.
This revision adds the one relation needed to release exactly one of them:

1. ``source_access.recovery_of_source_access_id`` (nullable, RESTRICT FK to
   ``source_access``). Only a successful ``local:register_retained_pdf.v1``
   access may carry it (CHECK), and each failed access is resolved at most
   once (partial UNIQUE). Every existing row keeps NULL; no stored value is
   rewritten or reinterpreted.
2. Binding decisions are ``local:historical_security_binding.v1`` accesses:
   CHECK closes their shape and a partial UNIQUE on ``result_hash`` makes a
   repeated import of the same decision return the recorded row.
3. ``disclosure_ops.download_failure_resolution_v1`` exposes, per failed
   CNINFO download attempt, whether it is non-retryable and which successful
   retained registration (if any) resolved it. A resolution counts only while
   its Document still carries the same provider, pid, raw hash, company and
   security. The download queue, dead-letter count, legacy pending path and
   doctor all read this one definition.
4. ``pending_download_v1`` is re-created with identical columns; its terminal
   exclusion now ignores exactly the failures resolved above.
   ``failed_download_count`` still counts every failed attempt (history is not
   reset), so a later failure — retryable or not — blocks again.

0023 keeps its bytes; downgrade restores its view definition verbatim.
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    OPS_SCHEMA,
)

# revision identifiers, used by Alembic.
revision: str = "0064_retained_registration"
down_revision: Union[str, None] = "0063_parse_requeue_decision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RESOLUTION_VIEW = "download_failure_resolution_v1"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.source_access "
        "ADD COLUMN recovery_of_source_access_id varchar(64) NULL"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.source_access "
        "ADD CONSTRAINT fk_source_access_recovery_of "
        "FOREIGN KEY (recovery_of_source_access_id) "
        f"REFERENCES {CORE_SCHEMA}.source_access (source_access_id) "
        "ON DELETE RESTRICT"
    )
    # Every term is two-valued once a link is present (provider_interface is
    # nullable; status is NOT NULL): a NULL interface fails the CHECK instead
    # of passing it as UNKNOWN.
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.source_access "
        "ADD CONSTRAINT ck_source_access_recovery_receipt CHECK ("
        "recovery_of_source_access_id IS NULL OR ("
        "provider_interface IS NOT DISTINCT FROM 'local:register_retained_pdf.v1' "
        "AND status = 'ok' AND result_hash IS NOT NULL "
        "AND company_id IS NOT NULL AND security_id IS NOT NULL "
        "AND recovery_of_source_access_id <> source_access_id))"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_source_access_successful_recovery "
        f"ON {CORE_SCHEMA}.source_access (recovery_of_source_access_id) "
        "WHERE recovery_of_source_access_id IS NOT NULL"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.source_access "
        "ADD CONSTRAINT ck_source_access_historical_binding CHECK ("
        "provider_interface IS DISTINCT FROM "
        "'local:historical_security_binding.v1' OR ("
        "status = 'ok' AND result_hash IS NOT NULL "
        "AND company_id IS NOT NULL AND security_id IS NOT NULL "
        "AND recovery_of_source_access_id IS NULL))"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_source_access_historical_binding_decision "
        f"ON {CORE_SCHEMA}.source_access (result_hash) "
        "WHERE provider_interface = 'local:historical_security_binding.v1'"
    )
    op.execute(_resolution_view_sql())
    op.execute(f"GRANT SELECT ON {OPS_SCHEMA}.{_RESOLUTION_VIEW} TO {APP_ROLE}")
    # Same columns in the same order: OR REPLACE keeps the existing grants.
    op.execute(_pending_download_view_sql())


def downgrade() -> None:
    # Restore the 0023 body first so nothing depends on the facts view.
    op.execute(_pending_download_view_0023_sql())
    op.execute(f"DROP VIEW IF EXISTS {OPS_SCHEMA}.{_RESOLUTION_VIEW}")
    op.execute(
        f"DROP INDEX IF EXISTS {CORE_SCHEMA}.uq_source_access_historical_binding_decision"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.source_access "
        "DROP CONSTRAINT IF EXISTS ck_source_access_historical_binding"
    )
    op.execute(
        f"DROP INDEX IF EXISTS {CORE_SCHEMA}.uq_source_access_successful_recovery"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.source_access "
        "DROP CONSTRAINT IF EXISTS ck_source_access_recovery_receipt"
    )
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.source_access "
        "DROP CONSTRAINT IF EXISTS fk_source_access_recovery_of"
    )
    # Dropping the column discards recovery links; binding rows and receipts
    # themselves stay as ordinary source_access history.
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.source_access "
        "DROP COLUMN IF EXISTS recovery_of_source_access_id"
    )


def _resolution_view_sql() -> str:
    # Facts only. A resolution is the unique successful receipt naming the
    # failed attempt whose Document still matches it; nothing else clears a
    # non-retryable failure.
    return f"""
    CREATE VIEW {OPS_SCHEMA}.{_RESOLUTION_VIEW} AS
    SELECT f.source_access_id,
           f.query_params->>'provider_document_id' AS provider_document_id,
           f.accessed_at,
           (f.error IS NOT NULL
            AND (f.error)::jsonb->>'retryable' = 'false') AS nonretryable,
           rec.source_access_id AS resolved_by_source_access_id
      FROM {CORE_SCHEMA}.source_access f
      LEFT JOIN {CORE_SCHEMA}.source_access rec
        ON rec.recovery_of_source_access_id = f.source_access_id
       AND rec.provider = f.provider
       AND rec.provider_interface = 'local:register_retained_pdf.v1'
       AND rec.status = 'ok'
       AND rec.query_params->>'provider_document_id'
           = f.query_params->>'provider_document_id'
       AND EXISTS (SELECT 1 FROM {CORE_SCHEMA}.document d
                    WHERE d.provider = rec.provider
                      AND d.provider_document_id
                          = rec.query_params->>'provider_document_id'
                      AND d.raw_file_hash = rec.result_hash
                      AND d.company_id = rec.company_id
                      AND d.security_id = rec.security_id)
     WHERE f.provider = 'cninfo'
       AND f.provider_interface = 'cninfo:download_pdf'
       AND f.status = 'failed'
    """


def _pending_download_view_sql() -> str:
    # 0023 body; only the terminal exclusion reads the resolution facts.
    return f"""
    CREATE OR REPLACE VIEW {OPS_SCHEMA}.pending_download_v1 AS
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
       AND NOT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.{_RESOLUTION_VIEW} nf
             WHERE nf.provider_document_id = cand.provider_document_id
               AND nf.nonretryable
               AND nf.resolved_by_source_access_id IS NULL)
    """


def _pending_download_view_0023_sql() -> str:
    # The 0023 view body verbatim; OR REPLACE keeps grants and dependents.
    return f"""
    CREATE OR REPLACE VIEW {OPS_SCHEMA}.pending_download_v1 AS
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
