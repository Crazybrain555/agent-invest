"""Re-derive filing_type + disclosure_topics from raw_category (idempotent).

Registration classifies a document ONCE; re-observation never re-derives, so
when filing_type_map.json / topic_map.json bump a version, existing rows keep
stale values. This is the compensating tool: rerun it after every vocabulary
bump — it re-derives both classifications for every document that carries an
F006V raw_category and reports each change. Docs without raw_category
(web/local channel) are untouched by design.
"""

from __future__ import annotations

import json
import os

from sqlalchemy import create_engine, text

from disclosure_anchor.adapters.sources.cninfo.mapper import (
    map_filing_type,
    topics_for_category,
)


def main() -> int:
    engine = create_engine(os.environ["DATABASE_URL"])
    filing_changed = 0
    topics_changed = 0
    with engine.begin() as conn:
        names = {
            row.category_code: row.category_name
            for row in conn.execute(
                text(
                    "SELECT category_code, category_name"
                    " FROM disclosure_core.provider_category"
                )
            )
        }
        rows = conn.execute(
            text(
                """
                SELECT document_id, title, filing_type, disclosure_topics,
                       provider_metadata->>'raw_category' AS raw_category
                  FROM disclosure_core.document
                 WHERE provider_metadata->>'raw_category' IS NOT NULL
                """
            )
        ).mappings().all()
        for row in rows:
            new_filing = map_filing_type(
                row["raw_category"], category_names_by_code=names
            )
            new_topics = topics_for_category(row["raw_category"])
            if new_filing != row["filing_type"]:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.document SET filing_type=:v"
                        " WHERE document_id=:id"
                    ),
                    {"v": new_filing, "id": row["document_id"]},
                )
                filing_changed += 1
                print(
                    f"filing_type {row['filing_type']} -> {new_filing} | "
                    f"{row['title'][:40]}"
                )
            if new_topics != row["disclosure_topics"]:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.document"
                        " SET disclosure_topics = CAST(:v AS jsonb)"
                        " WHERE document_id=:id"
                    ),
                    {"v": json.dumps(new_topics), "id": row["document_id"]},
                )
                topics_changed += 1
    print(
        f"reclassified: filing_type {filing_changed}, disclosure_topics "
        f"{topics_changed} (of {len(rows)} docs with raw_category)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
