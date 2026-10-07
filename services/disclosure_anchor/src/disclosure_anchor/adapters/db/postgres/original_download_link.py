"""Own-schema read of a document's matching recorded official index URL."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor.application.ports.original_download_link import (
    OriginalDownloadLinkRecord,
)


_READ = text("""
    SELECT p.document_id, p.provider, p.provider_document_id,
           p.raw_file_hash, p.source_ref, recorded.url, recorded.accessed_at
      FROM disclosure_public.documents_v1 p
      JOIN disclosure_core.document d ON d.document_id = p.document_id
      LEFT JOIN LATERAL (
          SELECT candidate->>'download_url' AS url, sa.accessed_at
            FROM disclosure_core.source_access sa
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(sa.result_snapshot->'candidates') = 'array'
                     THEN sa.result_snapshot->'candidates' ELSE '[]'::jsonb END
            ) AS candidate
           WHERE sa.company_id = d.company_id
             AND sa.provider = p.provider
             AND sa.provider_interface IN ('cninfo:p_info3015', 'cninfo:hisAnnouncement')
             AND sa.status = 'ok'
             AND candidate->>'provider_document_id' = p.provider_document_id
             AND candidate->>'security_code' = p.security_code
             AND candidate->>'exchange' = p.exchange
             AND candidate->>'announcement_date' = p.announcement_date::text
             AND jsonb_typeof(candidate->'download_url') = 'string'
             AND candidate->>'download_url' <> ''
           ORDER BY sa.accessed_at DESC, sa.source_access_id DESC,
                    candidate->>'download_url'
           LIMIT 1
      ) AS recorded ON true
     WHERE p.document_id = :document_id
""")


class PostgresOriginalDownloadLinkReader:
    """Use only in the authenticated agent API, never the public reader app."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read(self, document_id: str) -> OriginalDownloadLinkRecord | None:
        with self._engine.connect() as connection, connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '8s'"))
            row = connection.execute(_READ, {"document_id": document_id}).mappings().one_or_none()
            if row is None:
                return None
            return OriginalDownloadLinkRecord(
                document_id=row["document_id"], provider=row["provider"],
                provider_document_id=row["provider_document_id"],
                raw_file_hash=row["raw_file_hash"], source_ref=row["source_ref"],
                recorded_url=row["url"], recorded_at=row["accessed_at"],
            )
