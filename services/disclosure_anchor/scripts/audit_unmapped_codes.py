"""Unmapped-content-code audit (classification quality loop, design §4).

A content-facet F006V code seen in the corpus that matches NO class rule is
a vocabulary gap candidate: it silently lands in filing_type='other' with no
topic. Surface each with its cninfo name and document count for human
promotion into class_map.json (bump version + `make load-rules`). Same
pattern as the boilerplate / swallowed-heading discovery loops.
"""

from __future__ import annotations

import os
from collections import Counter

from sqlalchemy import create_engine, text

from disclosure_anchor.adapters.sources.cninfo.mapper import (
    load_class_map,
    load_facet_map,
    split_category_segments,
)

# cninfo's own misc buckets: mapping them would fabricate semantics. Docs
# carrying ONLY these stay filing_type='other' honestly (title-keyword
# fallback at registration still applies).
ACCEPTED_MISC_CODES = {"012399", "352399"}


def main() -> int:
    class_prefixes = [
        str(prefix)
        for spec in load_class_map()["classes"].values()
        for prefix in spec["prefixes"]
    ]
    facet_prefixes = [
        str(prefix)
        for rule in load_facet_map()["rules"]
        for prefix in rule["prefixes"]
    ]
    engine = create_engine(os.environ["DATABASE_URL"])
    doc_counts: Counter[str] = Counter()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT provider_metadata->>'raw_category' AS raw_category
                  FROM disclosure_core.document
                 WHERE provider_metadata->>'raw_category' IS NOT NULL
                """
            )
        ).all()
        for row in rows:
            for code in set(split_category_segments(row.raw_category)):
                doc_counts[code] += 1
        names = {
            str(r.category_code): str(r.category_name)
            for r in conn.execute(
                text(
                    "SELECT category_code, category_name"
                    " FROM disclosure_core.provider_category"
                )
            )
        }

    unmapped = [
        (code, count)
        for code, count in doc_counts.most_common()
        if code not in ACCEPTED_MISC_CODES
        and not any(code.startswith(p) for p in facet_prefixes)
        and not any(code.startswith(p) for p in class_prefixes)
    ]
    print(f"# unmapped content codes ({len(rows)} coded docs scanned)")
    for code, count in unmapped:
        print(f"{count:4d} docs | {code} | {names.get(code, '??')}")
    if not unmapped:
        print("(none — class_map covers every content code in the corpus beyond the accepted misc buckets)")
    return 0 if not unmapped else 1


if __name__ == "__main__":
    raise SystemExit(main())
