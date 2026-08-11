"""Minimal frozen NormalizedIR v4 evidence fixture.

The historical API reader intentionally validates only identity plus the
selected parser-artifact descriptor.  This helper must not reconstruct or
import the retired v4 writer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SOURCE_PDF_SHA256 = "sha256:" + "a" * 64


def write_text_ir_bundle(
    root: Path,
    ir_relpath: Path,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": "normalized_ir.v4",
        "document_id": "doc_1",
        "source_pdf_sha256": SOURCE_PDF_SHA256,
        "elements": [
            {"raw_kind": "title"},
            {"raw_kind": "text"},
        ],
        "parser_artifacts": {
            "artifact_root_relpath": "parser/a",
            "files": {
                "unused_evidence": {
                    "availability": "present",
                    "relpath": "parser/a/unused.png",
                    "sha256": "sha256:" + "b" * 64,
                    "size_bytes": 1,
                }
            },
        },
    }
    path = root / ir_relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    (root / "parser" / "a").mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return payload
