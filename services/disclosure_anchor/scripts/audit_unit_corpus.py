#!/usr/bin/env python3
"""Replay a deterministic NormalizedIR manifest through the current builder."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker

from disclosure_anchor.adapters.unit_builder import rules
from disclosure_anchor.adapters.unit_builder.builder import (
    UnitDraft,
    build_unit_drafts_s1_s7,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    normalized_ir_schema_filename,
    validate_normalized_ir_contract,
    validate_normalized_ir_path_version,
)
from disclosure_anchor.application.services.document_unit_audit import (
    AuditDocumentMetadata,
    AuditUnitView,
    audit_document,
)


AUDIT_SCHEMA_VERSION = "document-unit-corpus-audit.v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ManifestEntry:
    document_id: str
    provider: str
    provider_document_id: str
    processing_run_id: str
    security_code: str
    security_name: str | None
    company_name: str | None
    title: str | None
    filing_type: str
    normalized_ir_relpath: str
    normalized_ir_sha256: str

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, line_no: int) -> ManifestEntry:
        required = {
            "document_id",
            "provider",
            "provider_document_id",
            "processing_run_id",
            "security_code",
            "filing_type",
            "normalized_ir_relpath",
            "normalized_ir_sha256",
        }
        missing = sorted(required - value.keys())
        if missing:
            raise ValueError(f"manifest line {line_no} missing: {', '.join(missing)}")

        def required_text(key: str) -> str:
            item = value.get(key)
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"manifest line {line_no}: {key} must be text")
            return item.strip()

        def optional_text(key: str) -> str | None:
            item = value.get(key)
            if item is None:
                return None
            if not isinstance(item, str):
                raise ValueError(f"manifest line {line_no}: {key} must be text/null")
            return item.strip() or None

        digest = required_text("normalized_ir_sha256").lower()
        if _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(
                f"manifest line {line_no}: normalized_ir_sha256 is invalid"
            )
        relpath = _safe_relpath(required_text("normalized_ir_relpath"))
        return cls(
            document_id=required_text("document_id"),
            provider=required_text("provider"),
            provider_document_id=required_text("provider_document_id"),
            processing_run_id=required_text("processing_run_id"),
            security_code=required_text("security_code"),
            security_name=optional_text("security_name"),
            company_name=optional_text("company_name"),
            title=optional_text("title"),
            filing_type=required_text("filing_type"),
            normalized_ir_relpath=str(relpath),
            normalized_ir_sha256=digest,
        )

    def identity(self) -> tuple[str, str, str]:
        return self.provider, self.provider_document_id, self.document_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "provider": self.provider,
            "provider_document_id": self.provider_document_id,
            "processing_run_id": self.processing_run_id,
            "security_code": self.security_code,
            "security_name": self.security_name,
            "company_name": self.company_name,
            "title": self.title,
            "filing_type": self.filing_type,
            "normalized_ir_relpath": self.normalized_ir_relpath,
            "normalized_ir_sha256": self.normalized_ir_sha256,
        }


def load_manifest(path: Path) -> tuple[list[ManifestEntry], str]:
    raw = path.read_bytes()
    manifest_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    entries: list[ManifestEntry] = []
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"manifest line {line_no} must be an object")
        entries.append(ManifestEntry.from_dict(value, line_no=line_no))
    if not entries:
        raise ValueError("manifest is empty")
    identities = [entry.identity() for entry in entries]
    if len(identities) != len(set(identities)):
        raise ValueError("manifest contains duplicate document identities")
    document_ids = [entry.document_id for entry in entries]
    if len(document_ids) != len(set(document_ids)):
        raise ValueError("manifest selects more than one run for a document")
    return sorted(entries, key=ManifestEntry.identity), manifest_hash


@lru_cache(maxsize=2)
def _ir_validator(version: str) -> Draft202012Validator:
    schema_path = (
        REPO_ROOT
        / "contracts"
        / "normalized_ir"
        / normalized_ir_schema_filename(version)
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _audit_one(argument: tuple[ManifestEntry, str]) -> dict[str, Any]:
    entry, data_root_text = argument
    data_root = Path(data_root_text).resolve()
    base = data_root / "data"
    path = _under_root(base, Path(entry.normalized_ir_relpath))
    try:
        raw = path.read_bytes()
        actual_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        if actual_hash != entry.normalized_ir_sha256:
            raise ValueError(
                f"IR hash mismatch: {actual_hash} != {entry.normalized_ir_sha256}"
            )
        normalized_ir = json.loads(raw)
        if not isinstance(normalized_ir, dict):
            raise ValueError("NormalizedIR root must be an object")
        try:
            ir_version = validate_normalized_ir_contract(normalized_ir)
            validate_normalized_ir_path_version(path, version=ir_version)
        except NormalizedIRVersionError as exc:
            raise ValueError(f"invalid NormalizedIR version: {exc}") from exc
        schema_errors = sorted(
            _ir_validator(ir_version).iter_errors(normalized_ir),
            key=lambda item: list(item.absolute_path),
        )
        if schema_errors:
            first = schema_errors[0]
            location = "/".join(str(item) for item in first.absolute_path) or "$"
            raise ValueError(
                f"NormalizedIR schema failed at {location}: {first.message}"
            )

        image_hashes, image_resolver = _image_bindings(
            normalized_ir,
            data_root=data_root,
        )
        drafts, stats = build_unit_drafts_s1_s7(
            normalized_ir,
            filing_type=entry.filing_type,
            document_title=entry.title,
            security_code=entry.security_code,
            security_name=entry.security_name,
            image_bytes_resolver=image_resolver,
        )
        report = audit_document(
            normalized_ir=normalized_ir,
            units=_unit_views(drafts),
            metadata=AuditDocumentMetadata(
                document_id=entry.document_id,
                title=entry.title,
                filing_type=entry.filing_type,
                security_code=entry.security_code,
                security_name=entry.security_name,
            ),
            source_dispositions=stats.source_dispositions,
            image_hashes=image_hashes,
        )
        return {
            **entry.as_dict(),
            "ok": report.ok,
            "metrics": report.metrics,
            "build_stats": stats.as_dict(),
            "findings": [item.as_dict() for item in report.findings],
        }
    except Exception as exc:
        return {
            **entry.as_dict(),
            "ok": False,
            "metrics": {},
            "build_stats": {},
            "findings": [
                {
                    "code": "audit_execution_error",
                    "severity": "error",
                    "message": f"{exc.__class__.__name__}: {exc}",
                    "source_ref": None,
                    "unit_order": None,
                }
            ],
        }


def _image_bindings(
    normalized_ir: dict[str, Any],
    *,
    data_root: Path,
) -> tuple[dict[str, str], Any]:
    parser_artifacts = normalized_ir.get("parser_artifacts")
    artifact_root: Path | None = None
    if isinstance(parser_artifacts, dict):
        value = parser_artifacts.get("artifact_root_relpath")
        if isinstance(value, str) and value:
            artifact_root = _under_root(data_root / "data", _safe_relpath(value))

    image_elements = [
        element
        for element in normalized_ir.get("elements") or []
        if isinstance(element, dict) and element.get("kind") == "image"
    ]
    if artifact_root is None:
        if any(str(element.get("image_path") or "").strip() for element in image_elements):
            raise ValueError("image source exists without parser artifact_root_relpath")
        return {}, None

    cache: dict[str, bytes] = {}

    def resolve(image_path: str) -> bytes:
        if image_path in cache:
            return cache[image_path]
        relative = _safe_relpath(image_path)
        candidate = _under_root(artifact_root, relative)
        if candidate.is_file():
            cache[image_path] = candidate.read_bytes()
            return cache[image_path]
        raise FileNotFoundError(f"image artifact not found: {image_path}")

    hashes: dict[str, str] = {}
    for element in image_elements:
        ref = element.get("ir_id")
        image_path = element.get("image_path")
        if (
            not isinstance(ref, str)
            or not isinstance(image_path, str)
            or not image_path
        ):
            continue
        hashes[ref] = "sha256:" + hashlib.sha256(resolve(image_path)).hexdigest()
    return hashes, resolve


def _unit_views(drafts: list[UnitDraft]) -> list[AuditUnitView]:
    return [
        AuditUnitView(
            order_index=index,
            payload_kind=draft.payload_kind,
            payload=draft.payload,
            title=draft.title,
            heading_path=draft.heading_path,
            structural_path=draft.structural_path,
            semantic_key=draft.semantic_key,
            semantic_keys=draft.semantic_keys,
            quality_status=draft.quality_status,
            applicability=draft.applicability,
            artifact_locator=draft.artifact_locator,
        )
        for index, draft in enumerate(drafts, start=1)
    ]


def _safe_relpath(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe relative path: {value!r}")
    return path


def _under_root(root: Path, relative: Path) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes data root: {relative}") from exc
    return candidate


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _summary(
    results: list[dict[str, Any]],
    *,
    manifest_hash: str,
) -> dict[str, Any]:
    finding_codes: Counter[str] = Counter()
    unit_kinds: Counter[str] = Counter()
    breakdown: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"documents": 0, "failed_documents": 0, "units": 0}
    )
    total_units = 0
    total_errors = 0
    for result in results:
        metrics = result.get("metrics") or {}
        unit_count = int(metrics.get("unit_count") or 0)
        total_units += unit_count
        unit_kinds.update(metrics.get("unit_kinds") or {})
        for finding in result.get("findings") or []:
            finding_codes[str(finding.get("code") or "unknown")] += 1
            total_errors += int(finding.get("severity") == "error")
        key = (
            str(result.get("company_name") or "unknown"),
            str(result.get("filing_type") or "unknown"),
        )
        row = breakdown[key]
        row["documents"] += 1
        row["failed_documents"] += int(not result.get("ok"))
        row["units"] += unit_count
    breakdown_rows = [
        {
            "company_name": company,
            "filing_type": filing_type,
            **values,
        }
        for (company, filing_type), values in sorted(breakdown.items())
    ]
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "manifest_sha256": manifest_hash,
        "rules_version": rules.RULES_VERSION,
        "documents": len(results),
        "ok_documents": sum(bool(result.get("ok")) for result in results),
        "failed_documents": sum(not result.get("ok") for result in results),
        "error_count": total_errors,
        "unit_count": total_units,
        "unit_kinds": dict(sorted(unit_kinds.items())),
        "finding_codes": dict(sorted(finding_codes.items())),
        "breakdown_company_filing": breakdown_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=(
            Path(os.environ["DISCLOSURE_DATA_ROOT"])
            if os.environ.get("DISCLOSURE_DATA_ROOT")
            else None
        ),
        help="service runtime root containing data/ (or DISCLOSURE_DATA_ROOT)",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.data_root is None:
        parser.error("--data-root or DISCLOSURE_DATA_ROOT is required")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    entries, manifest_hash = load_manifest(args.manifest)
    if args.limit is not None:
        if args.limit < 1:
            parser.error("--limit must be >= 1")
        entries = entries[: args.limit]

    arguments = [(entry, str(args.data_root.resolve())) for entry in entries]
    if args.workers == 1:
        results = [_audit_one(argument) for argument in arguments]
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            results = list(executor.map(_audit_one, arguments, chunksize=8))
    results.sort(
        key=lambda item: (
            str(item["provider"]),
            str(item["provider_document_id"]),
            str(item["document_id"]),
        )
    )
    summary = _summary(results, manifest_hash=manifest_hash)
    args.out.mkdir(parents=True, exist_ok=True)
    _write_json(args.out / "summary.json", summary)
    _write_jsonl(args.out / "per_document.jsonl", results)
    findings = [
        {
            "document_id": result["document_id"],
            "provider": result["provider"],
            "provider_document_id": result["provider_document_id"],
            "company_name": result.get("company_name"),
            "filing_type": result["filing_type"],
            **finding,
        }
        for result in results
        for finding in result.get("findings") or []
    ]
    _write_jsonl(args.out / "findings.jsonl", findings)
    print(
        f"audited={summary['documents']} units={summary['unit_count']} "
        f"failed={summary['failed_documents']} errors={summary['error_count']}"
    )
    print(f"wrote {args.out}")
    return 0 if summary["error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
