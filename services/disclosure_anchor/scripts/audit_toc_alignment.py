"""Audit in-document TOC pages against the actual heading tree.

Audit-only (design: docs/implementation/design/document-outline-and-toc.md):
a TOC entry with no counterpart anywhere in the heading tree points at a
heading the parser lost; nothing is repaired here. Findings land in a JSONL
for per-family triage.

Usage:
  .venv/bin/python scripts/audit_toc_alignment.py --out <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from disclosure_anchor.adapters.unit_builder.toc_outline import (  # noqa: E402
    normalize_section_title,
    parse_toc_titles,
    strip_section_enumerator,
)


def normalize_heading(text: str) -> str:
    """Match key: enumerator-stripped so 目录's 第X章 form meets the
    prefix-less body opener."""

    return normalize_section_title(strip_section_enumerator(text))


def match_titles_to_tree(
    titles: list[str], segments: set[str]
) -> tuple[list[str], list[str]]:
    """Split TOC titles into (matched, missing) against heading segments.

    Exact normalized equality first; then a one-sided prefix match (>=4
    chars) tolerating truncation on either side without letting short
    fragments match everything.
    """

    matched: list[str] = []
    missing: list[str] = []
    for title in titles:
        key = normalize_heading(title)
        if not key:
            continue
        if key in segments:
            matched.append(title)
            continue
        hit = any(
            (len(key) >= 4 and segment.startswith(key))
            or (len(segment) >= 4 and key.startswith(segment))
            for segment in segments
        )
        (matched if hit else missing).append(title)
    return matched, missing


def main() -> int:
    parser = argparse.ArgumentParser(prog="audit_toc_alignment")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    from sqlalchemy import text

    from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
    from disclosure_anchor.cli.worker import _database_url
    from disclosure_anchor.settings import load_settings

    engine = create_db_engine(_database_url(load_settings()))
    try:
        with engine.connect() as conn:
            toc_rows = conn.execute(
                text(
                    """
                    SELECT u.document_id, d.filing_type, d.title,
                           string_agg(u.payload->>'text', E'\n'
                                      ORDER BY u.order_index) AS toc_text
                      FROM disclosure_public.document_units_v1 u
                      JOIN disclosure_public.documents_v1 d
                        ON d.document_id = u.document_id
                     WHERE u.is_active_run
                       AND u.semantic_key = 'table_of_contents'
                       AND u.payload_kind = 'text'
                     GROUP BY u.document_id, d.filing_type, d.title
                    """
                )
            ).all()
            segment_rows = conn.execute(
                text(
                    """
                    SELECT document_id, path
                      FROM disclosure_public.document_outline_v1
                    """
                )
            ).all()
    finally:
        engine.dispose()

    segments_by_doc: dict[str, set[str]] = {}
    for document_id, path in segment_rows:
        parts = json.loads(path) if isinstance(path, str) else path
        bucket = segments_by_doc.setdefault(document_id, set())
        for part in parts:
            key = normalize_heading(str(part))
            if key:
                bucket.add(key)

    args.out.mkdir(parents=True, exist_ok=True)
    findings_path = args.out / "findings.jsonl"
    stats: Counter = Counter()
    by_type: Counter = Counter()
    with findings_path.open("w", encoding="utf-8") as handle:
        for document_id, filing_type, title, toc_text in toc_rows:
            stats["documents_with_toc"] += 1
            titles, unparsed = parse_toc_titles(toc_text or "")
            if not titles:
                stats["toc_unparsable"] += 1
                handle.write(
                    json.dumps(
                        {
                            "code": "toc_unparsable",
                            "document_id": document_id,
                            "filing_type": filing_type,
                            "title": title,
                            "unparsed_lines": unparsed,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                continue
            matched, missing = match_titles_to_tree(
                titles, segments_by_doc.get(document_id, set())
            )
            stats["toc_titles"] += len(titles)
            stats["toc_titles_matched"] += len(matched)
            if missing:
                stats["documents_with_missing_sections"] += 1
                stats["toc_sections_missing_in_tree"] += len(missing)
                by_type[filing_type] += len(missing)
                handle.write(
                    json.dumps(
                        {
                            "code": "toc_section_missing_in_tree",
                            "document_id": document_id,
                            "filing_type": filing_type,
                            "title": title,
                            "missing": missing,
                            "matched_count": len(matched),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    summary = {
        "stats": dict(stats),
        "missing_by_filing_type": dict(by_type.most_common()),
    }
    (args.out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"findings -> {findings_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
