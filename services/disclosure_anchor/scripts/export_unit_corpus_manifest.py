#!/usr/bin/env python3
"""Export one deterministic active NormalizedIR run per document."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


MANIFEST_SCHEMA_VERSION = "document-unit-corpus-manifest.v1"


def _safe_data_path(data_root: Path, relpath: str) -> Path:
    relative = Path(relpath)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"unsafe normalized_ir_relpath: {relpath!r}")
    root = (data_root / "data").resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"normalized_ir_relpath escapes data root: {relpath!r}") from exc
    return candidate


def _file_hash(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise RuntimeError(f"cannot hash active NormalizedIR {path}: {exc}") from exc


def export_manifest(*, database_url: str, data_root: Path) -> list[dict[str, Any]]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            rows = connection.execute(
                text(
                    """
                    SELECT d.document_id,
                           d.provider,
                           d.provider_document_id,
                           r.processing_run_id,
                           s.security_code,
                           NULLIF(d.provider_metadata ->> 'security_name', '')
                               AS security_name,
                           c.legal_name AS company_name,
                           d.title,
                           public_doc.filing_type,
                           r.normalized_ir_relpath
                      FROM disclosure_core.document AS d
                      JOIN disclosure_core.processing_run AS r
                        ON r.document_id = d.document_id
                       AND r.is_active
                      JOIN disclosure_public.documents_v1 AS public_doc
                        ON public_doc.document_id = d.document_id
                      LEFT JOIN disclosure_core.security AS s
                        ON s.security_id = d.security_id
                      LEFT JOIN disclosure_core.company AS c
                        ON c.company_id = d.company_id
                     WHERE r.normalized_ir_relpath IS NOT NULL
                     ORDER BY d.provider, d.provider_document_id, d.document_id
                    """
                )
            ).mappings().all()
            connection.rollback()
    finally:
        engine.dispose()

    manifest: list[dict[str, Any]] = []
    document_ids: set[str] = set()
    for row in rows:
        required = (
            "document_id",
            "provider",
            "provider_document_id",
            "processing_run_id",
            "security_code",
            "filing_type",
            "normalized_ir_relpath",
        )
        missing = [key for key in required if not isinstance(row[key], str) or not row[key]]
        if missing:
            raise RuntimeError(
                f"active row {row['document_id']!r} lacks: {', '.join(missing)}"
            )
        document_id = str(row["document_id"])
        if document_id in document_ids:
            raise RuntimeError(f"more than one active run for document {document_id}")
        document_ids.add(document_id)
        relpath = str(row["normalized_ir_relpath"])
        manifest.append(
            {
                "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
                "document_id": document_id,
                "provider": str(row["provider"]),
                "provider_document_id": str(row["provider_document_id"]),
                "processing_run_id": str(row["processing_run_id"]),
                "security_code": str(row["security_code"]),
                "security_name": row["security_name"],
                "company_name": row["company_name"],
                "title": row["title"],
                "filing_type": str(row["filing_type"]),
                "normalized_ir_relpath": relpath,
                "normalized_ir_sha256": _file_hash(
                    _safe_data_path(data_root, relpath)
                ),
            }
        )
    if not manifest:
        raise RuntimeError("no active NormalizedIR rows found")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="defaults to DATABASE_URL",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=(
            Path(os.environ["DISCLOSURE_DATA_ROOT"])
            if os.environ.get("DISCLOSURE_DATA_ROOT")
            else None
        ),
        help="service runtime root containing data/",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    if args.data_root is None:
        parser.error("--data-root or DISCLOSURE_DATA_ROOT is required")
    manifest = export_manifest(
        database_url=str(args.database_url),
        data_root=args.data_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in manifest
        ),
        encoding="utf-8",
    )
    companies = Counter(str(row.get("company_name") or "unknown") for row in manifest)
    filing_types = Counter(str(row["filing_type"]) for row in manifest)
    print(
        f"wrote {len(manifest)} documents, {len(companies)} companies, "
        f"{len(filing_types)} filing types to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
