"""Audit F006V coverage at the candidate boundary and downloaded corpus.

The candidate layer is primary: auditing only downloaded ``document`` rows has
survivor bias because unknown codes are often excluded by the processing gate.
Append-only, overlapping index snapshots are deduplicated by provider document
id before counting.  The downloaded layer remains a secondary cross-check for
manual/local registrations.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from disclosure_anchor.adapters.sources.cninfo.classification_coverage import (
    unmapped_code_counts,
)
from disclosure_anchor.adapters.sources.cninfo.mapper import (
    load_class_map,
    load_facet_map,
    split_category_segments,
)
from disclosure_anchor.application.worker.queries import candidate_code_counts


def _document_code_counts(conn: Connection) -> Counter[str]:
    counts: Counter[str] = Counter()
    rows = conn.execute(
        text(
            "SELECT provider_metadata->>'raw_category' AS raw_category "
            "FROM disclosure_core.document "
            "WHERE NULLIF(btrim(provider_metadata->>'raw_category'), '') IS NOT NULL"
        )
    ).all()
    for row in rows:
        for code in set(split_category_segments(str(row.raw_category))):
            counts[code] += 1
    return counts


def _print_gaps(
    label: str,
    *,
    scanned_count: int,
    gaps: Mapping[str, int],
    names: Mapping[str, str],
) -> None:
    print(f"# {label} ({scanned_count} F006V segment occurrences scanned)")
    if not gaps:
        print("(none beyond accepted generic misc buckets)")
        return
    for code, count in sorted(gaps.items(), key=lambda item: (-item[1], item[0])):
        print(f"{count:5d} announcements | {code} | {names.get(code, '??')}")


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
    with engine.connect() as conn:
        candidate_counts = candidate_code_counts(conn)
        document_counts = _document_code_counts(conn)
        names = {
            str(row.category_code): str(row.category_name)
            for row in conn.execute(
                text(
                    "SELECT category_code, category_name "
                    "FROM disclosure_core.provider_category "
                    "WHERE provider = 'cninfo'"
                )
            )
        }

    candidate_gaps = unmapped_code_counts(
        candidate_counts,
        class_prefixes=class_prefixes,
        facet_prefixes=facet_prefixes,
    )
    document_gaps = unmapped_code_counts(
        document_counts,
        class_prefixes=class_prefixes,
        facet_prefixes=facet_prefixes,
    )
    _print_gaps(
        "candidate-layer unmapped content codes",
        scanned_count=sum(candidate_counts.values()),
        gaps=candidate_gaps,
        names=names,
    )
    print()
    _print_gaps(
        "downloaded-document unmapped content codes (secondary check)",
        scanned_count=sum(document_counts.values()),
        gaps=document_gaps,
        names=names,
    )
    return 0 if not candidate_gaps and not document_gaps else 1


if __name__ == "__main__":
    raise SystemExit(main())
