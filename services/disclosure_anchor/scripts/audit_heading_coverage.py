"""IR heading ↔ unit title-domain reconciliation (swallowed-heading audit).

Round14 found that pure-title intermediate note headings (17、存货) were
evicted from the heading stack before any child could inherit them — the
string existed in the IR yet appeared NOWHERE in the built units. This audit
institutionalizes that diagnosis as a corpus-wide invariant:

    every kind='heading' element in an active run's NormalizedIR must appear
    somewhere in that document's units — as a title, a heading_path segment,
    a mixed-part local_heading, or a payload text line — unless a versioned
    rule deliberately strips it (declarations, noise, closing formulas,
    skip-sections).

Any residue is a candidate slicing bug of the "标题被吞" class. Deterministic
slice time is untouched; this is an offline reviewer loop like
audit_boilerplate_candidates.py.
"""

from __future__ import annotations

import json
import os
import unicodedata

from sqlalchemy import create_engine, text

from disclosure_anchor.adapters.unit_builder import rules

DATA_ROOT_ENV = "DISCLOSURE_DATA_ROOT"


def _norm(line: str) -> str:
    # Whitespace-insensitive: IR and stored text differ in internal spacing.
    return "".join(unicodedata.normalize("NFKC", line).split())


def _deliberately_stripped(line: str) -> bool:
    return (
        rules.is_unit_declaration_line(line)
        or rules.is_declaration_line(line)
        or rules.is_standalone_noise(line)
        or rules.is_closing_formula_line(line)
        or bool(rules.BOILERPLATE_GUARANTEE_RE.match(line))
        or rules.strip_header_kv_line(line) is not None
        or _norm(line) in {_norm(t) for t in rules.SKIP_SECTION_TITLES}
    )


def _unit_haystack(payload_kind: str, title: str | None, heading_path: list, payload: dict) -> set[str]:
    lines: set[str] = set()
    if title:
        lines.add(_norm(title))
    for segment in heading_path or []:
        lines.add(_norm(str(segment)))

    def _add_text_block(block: str) -> None:
        for raw in str(block).splitlines():
            norm = _norm(raw)
            if norm:
                lines.add(norm)

    def _add_payload(kind: str, body: dict) -> None:
        if body.get("text"):
            _add_text_block(body["text"])
        if body.get("caption"):
            _add_text_block(body["caption"])
        if body.get("question"):
            _add_text_block(body["question"])
        if body.get("answer"):
            _add_text_block(body["answer"])

    if payload_kind == "mixed":
        for part in payload.get("parts", []):
            # Parts carry their headings as local_heading (str or list) and/or
            # a per-part heading_path (short-doc collapse route).
            for field in ("local_heading", "heading_path"):
                value = part.get(field)
                if isinstance(value, str):
                    lines.add(_norm(value))
                elif isinstance(value, list):
                    for segment in value:
                        lines.add(_norm(str(segment)))
            _add_payload(part.get("kind", "text"), part)
    else:
        _add_payload(payload_kind, payload)
    return lines


def _section_has_content(elements: list, heading_index: int) -> bool:
    """Any substantive element between this heading and the next heading?

    A heading whose entire section is empty (blank table, stripped
    declarations, nothing at all) has no unit to carry it — that is the
    documented EMPTY-SECTION class (信息性), not a slicing bug.
    """

    for element in elements[heading_index + 1 :]:
        kind = element.get("kind")
        if kind == "page_furniture":
            continue
        if kind == "heading":
            return False
        if kind == "table":
            table = element.get("table") or {}
            cells = [*(table.get("headers") or [])]
            for row in table.get("rows") or []:
                cells.extend(row)
            if any(str(cell).strip() for cell in cells):
                return True
            if str(element.get("table_html") or "").strip():
                return True
            continue
        text = (element.get("text") or "").strip()
        if text and not _deliberately_stripped(text):
            return True
    return False


def main() -> int:
    engine = create_engine(os.environ["DATABASE_URL"])
    data_root = os.environ[DATA_ROOT_ENV]
    total_missing = 0
    total_empty = 0
    with engine.connect() as conn:
        runs = conn.execute(
            text(
                """
                SELECT r.document_id, d.title AS doc_title, r.processing_run_id,
                       r.normalized_ir_relpath
                  FROM disclosure_core.processing_run r
                  JOIN disclosure_core.document d ON d.document_id = r.document_id
                 WHERE r.is_active AND r.normalized_ir_relpath IS NOT NULL
                 ORDER BY r.document_id
                """
            )
        ).mappings().all()
        print(f"# heading coverage audit — {len(runs)} active runs with IR")
        for run in runs:
            # Relpaths resolve under <data_root>/data (storage path_builder contract).
            ir_path = os.path.join(data_root, "data", run["normalized_ir_relpath"])
            if not os.path.exists(ir_path):
                print(f"SKIP (IR missing): {run['document_id']} {run['doc_title']}")
                continue
            with open(ir_path, encoding="utf-8") as fh:
                ir = json.load(fh)
            elements = ir.get("elements", [])
            headings = [
                (index, (element.get("text") or "").strip())
                for index, element in enumerate(elements)
                if element.get("kind") == "heading" and (element.get("text") or "").strip()
            ]
            units = conn.execute(
                text(
                    """
                    SELECT payload_kind, title, heading_path, payload
                      FROM disclosure_core.document_unit
                     WHERE processing_run_id = :run_id
                    """
                ),
                {"run_id": run["processing_run_id"]},
            ).mappings().all()
            haystack: set[str] = set()
            for unit in units:
                haystack |= _unit_haystack(
                    unit["payload_kind"], unit["title"], unit["heading_path"], unit["payload"]
                )
            missing = [
                (index, heading)
                for index, heading in headings
                if _norm(heading) not in haystack and not _deliberately_stripped(heading)
            ]
            first_pattern_heading = next(
                (
                    index
                    for index, element in enumerate(elements)
                    if element.get("kind") == "heading"
                    and any(
                        pattern.match((element.get("text") or "").strip())
                        for _, pattern in rules.HEADING_PATTERNS
                    )
                ),
                0,
            )
            swallowed = [
                (index, heading)
                for index, heading in missing
                # Cover/title-page headings precede the first numbered heading
                # and are evicted by it; document.title already carries them.
                if index >= first_pattern_heading
                and _section_has_content(elements, index)
            ]
            empty_sections = len(missing) - len(swallowed)
            if empty_sections:
                total_empty += empty_sections
            if swallowed:
                total_missing += len(swallowed)
                print(f"\n{run['document_id']} {run['doc_title']}")
                for _, heading in swallowed:
                    print(f"  SWALLOWED: {heading}")
    print(f"\n(empty sections, accepted class: {total_empty})")
    print(
        f"{'OK — no swallowed headings' if total_missing == 0 else f'{total_missing} swallowed headings'}"
    )
    return 0 if total_missing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
