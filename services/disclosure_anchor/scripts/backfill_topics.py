"""One-off: derive disclosure_topics for existing documents from raw_category.

New registrations compute topics inline (download_document); this backfills
rows registered before migration 0014. Idempotent.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text

from disclosure_anchor.adapters.sources.cninfo.mapper import topics_for_category


def main() -> int:
    engine = create_engine(os.environ["DATABASE_URL"])
    updated = 0
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT document_id, provider_metadata->>'raw_category' AS raw_category
                  FROM disclosure_core.document
                 WHERE disclosure_topics IS NULL
                """
            )
        ).mappings().all()
        for row in rows:
            topics = topics_for_category(row["raw_category"])
            if topics:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.document"
                        " SET disclosure_topics = CAST(:topics AS jsonb)"
                        " WHERE document_id = :document_id"
                    ),
                    {"topics": __import__("json").dumps(topics),
                     "document_id": row["document_id"]},
                )
                updated += 1
    print(f"backfilled disclosure_topics on {updated}/{len(rows)} documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
