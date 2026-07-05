"""review hardening contracts

Revision ID: 0004_review_hardening_contracts
Revises: 0003_parser_run_metadata
Create Date: 2026-06-30
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA, PUBLIC_SCHEMA

# revision identifiers, used by Alembic.
revision: str = "0004_review_hardening_contracts"
down_revision: Union[str, None] = "0003_parser_run_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(_duplicate_document_guard_sql())
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_document_provider_doc_hash
        ON {CORE_SCHEMA}.document (provider, provider_document_id, raw_file_hash)
        WHERE provider IS NOT NULL
          AND provider_document_id IS NOT NULL
          AND raw_file_hash IS NOT NULL
        """
    )
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.source_refs_v1")
    op.execute(_source_refs_view_sql())


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.source_refs_v1")
    op.execute(_source_refs_view_sql(include_contract_columns=False))
    op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.uq_document_provider_doc_hash")


def _duplicate_document_guard_sql() -> str:
    return f"""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM {CORE_SCHEMA}.document
            WHERE provider IS NOT NULL
              AND provider_document_id IS NOT NULL
              AND raw_file_hash IS NOT NULL
            GROUP BY provider, provider_document_id, raw_file_hash
            HAVING count(*) > 1
        ) THEN
            RAISE EXCEPTION
                'duplicate document provider/provider_document_id/raw_file_hash rows exist';
        END IF;
    END
    $$;
    """


def _source_refs_view_sql(*, include_contract_columns: bool = True) -> str:
    contract_columns = ""
    if include_contract_columns:
        contract_columns = """
        'disclosure_anchor'::text AS service,
        'source_ref.v1'::text AS contract_version,
        """
    return f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.source_refs_v1 AS
    SELECT
        {contract_columns}
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
