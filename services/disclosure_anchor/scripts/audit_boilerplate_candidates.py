"""Offline boilerplate-variant discovery (C4/eDiscovery frequency technique).

Deterministic slice time never guesses; this audit finds CANDIDATE new
declaration/boilerplate variants for human promotion into the versioned
pattern family (rules.UNIT_DECLARATION_RES etc. → RULES_VERSION bump).

Heuristic: a normalized short line appearing in >= MIN_DOCS distinct
documents across >= MIN_COMPANIES companies, not already matched by the
current families, is probably format boilerplate rather than content.
"""

from __future__ import annotations

from collections import defaultdict
import os
import unicodedata

from sqlalchemy import create_engine, text

from disclosure_anchor.adapters.unit_builder import rules

MAX_LINE_CHARS = 40
MIN_DOCS = 3
MIN_COMPANIES = 2


def _normalize(line: str) -> str:
    return unicodedata.normalize("NFKC", line).strip()


def _already_handled(line: str) -> bool:
    return (
        rules.is_unit_declaration_line(line)
        or rules.is_declaration_line(line)
        or rules.is_standalone_noise(line)
        or bool(rules.BOILERPLATE_GUARANTEE_RE.match(line))
        or rules.strip_header_kv_line(line) is not None
    )


def main() -> int:
    engine = create_engine(os.environ["DATABASE_URL"])
    docs_by_line: dict[str, set[str]] = defaultdict(set)
    companies_by_line: dict[str, set[str]] = defaultdict(set)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT u.document_id, d.company_id, u.payload
                  FROM disclosure_core.document_unit u
                  JOIN disclosure_core.processing_run r
                    ON r.processing_run_id = u.processing_run_id
                  JOIN disclosure_core.document d
                    ON d.document_id = u.document_id
                 WHERE r.is_active
                """
            )
        ).mappings()
        for row in rows:
            payload = row["payload"]
            texts = []
            if isinstance(payload, dict):
                if "text" in payload:
                    texts.append(str(payload["text"]))
                for part in payload.get("parts", []):
                    if isinstance(part, dict) and part.get("text"):
                        texts.append(str(part["text"]))
            for block in texts:
                for line in block.splitlines():
                    norm = _normalize(line)
                    if not norm or len(norm) > MAX_LINE_CHARS:
                        continue
                    docs_by_line[norm].add(row["document_id"])
                    companies_by_line[norm].add(row["company_id"] or "?")

    candidates = [
        (line, len(docs), len(companies_by_line[line]))
        for line, docs in docs_by_line.items()
        if len(docs) >= MIN_DOCS
        and len(companies_by_line[line]) >= MIN_COMPANIES
        and not _already_handled(line)
    ]
    candidates.sort(key=lambda item: (-item[1], item[0]))
    print(f"# boilerplate candidates (docs>={MIN_DOCS}, companies>={MIN_COMPANIES})")
    for line, doc_count, company_count in candidates:
        print(f"{doc_count:4d} docs / {company_count} companies | {line}")
    if not candidates:
        print("(none — current pattern families cover the corpus)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
