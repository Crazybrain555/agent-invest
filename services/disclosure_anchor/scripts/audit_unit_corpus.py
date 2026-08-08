#!/usr/bin/env python3
"""Replay a deterministic NormalizedIR manifest through the current builder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable, cast

from jsonschema import Draft202012Validator, FormatChecker

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    mineru_provider_item_sha256,
)
from disclosure_anchor.adapters.parsers.mineru.existing_artifact_pipeline import (
    build_current_ir_from_mineru_artifacts,
)
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    source_visual_artifact_descriptors,
    validate_mapped_element_bindings,
    validate_source_evidence_ledger,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence_validator import (
    MinerUSourceEvidenceValidator,
    source_evidence_proof_from_validated_ledger,
)
from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NativeStructureIndex,
    validate_pdf_structure_artifact,
)
from disclosure_anchor.adapters.parsers.pdf_visual_evidence import (
    VisualPageEvidence,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    normalized_ir_schema_filename,
    read_normalized_ir_version,
    validate_current_normalized_ir_for_write,
    validate_normalized_ir_contract,
    validate_normalized_ir_path_version,
)
from disclosure_anchor.application.contracts.source_evidence import (
    SourceEvidenceProof,
)
from disclosure_anchor.application.ports.source_evidence import (
    VerifiedParserArtifact,
)
from disclosure_anchor.application.services.document_unit_audit import (
    AuditDocumentMetadata,
)
from disclosure_anchor.application.services.unit_builder import rules
from disclosure_anchor.application.services.unit_builder.builder import (
    ResolvedImageArtifact,
)
from disclosure_anchor.application.services.unit_preparation import (
    prepare_and_audit_units,
)
from disclosure_anchor.application.use_cases.parse_document import (
    build_parser_artifact_manifest,
)

AUDIT_SCHEMA_VERSION = "document-unit-corpus-audit.v3"
REPO_ROOT = Path(__file__).resolve().parents[1]
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class LegacyEvidenceClosureError(ValueError):
    """A frozen IR predates the evidence contract required for replay."""

    reason_code = "legacy_ir_evidence_closure_missing"


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


def _exception_failure_family(exc: BaseException) -> str:
    """Return the deepest typed reason in the explicit exception chain."""

    family: str | None = None
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        reason_code = getattr(current, "reason_code", None)
        if (
            isinstance(reason_code, str)
            and _REASON_CODE_RE.fullmatch(reason_code) is not None
        ):
            if reason_code != "parser_output_contract_error" or family is None:
                family = reason_code
        current = (
            current.__cause__ if current.__cause__ is not None else current.__context__
        )
    return family or "audit_execution_error"


def _audit_one(argument: tuple[ManifestEntry, str, bool]) -> dict[str, Any]:
    entry, data_root_text, source_replay = argument
    data_root = Path(data_root_text).resolve()
    base = data_root / "data"
    path = _under_root(base, Path(entry.normalized_ir_relpath))
    observations: dict[str, Any] = {}
    try:
        raw = path.read_bytes()
        actual_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        if actual_hash != entry.normalized_ir_sha256:
            raise ValueError(
                f"IR hash mismatch: {actual_hash} != {entry.normalized_ir_sha256}"
            )
        frozen_ir = json.loads(raw)
        if not isinstance(frozen_ir, dict):
            raise ValueError("NormalizedIR root must be an object")
        try:
            frozen_version = read_normalized_ir_version(frozen_ir)
            validate_normalized_ir_path_version(path, version=frozen_version)
        except NormalizedIRVersionError as exc:
            raise ValueError(f"invalid NormalizedIR version: {exc}") from exc
        source_replay_evidence: dict[str, Any] | None = None
        source_evidence: Mapping[str, Any]
        source_proof: SourceEvidenceProof
        native_structure: NativeStructureIndex
        if source_replay:
            validate_normalized_ir_contract(frozen_ir)
            (
                normalized_ir,
                source_replay_evidence,
                source_evidence,
                source_proof,
                native_structure,
            ) = _replay_source_ir(frozen_ir, data_root=data_root)
            # Source-identity replay reproduces a frozen generation whose
            # parser payload is asserted byte-equal to the stored artifact;
            # target currency applies to new production writes, not here.
            ir_version = validate_current_normalized_ir_for_write(
                normalized_ir,
                write_authority="frozen_generation",
            )
        else:
            normalized_ir = frozen_ir
            ir_version = validate_normalized_ir_contract(normalized_ir)
            persisted = _load_persisted_source_bundle(
                normalized_ir,
                data_root=data_root,
            )
            source_proof = persisted.proof
            source_evidence = persisted.ledger
            native_structure = persisted.native_structure_index
        observations = _source_observations(
            normalized_ir,
            source_evidence=source_evidence,
            native_structure=native_structure,
        )
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
        _drafts, stats, report = prepare_and_audit_units(
            normalized_ir=normalized_ir,
            filing_type=entry.filing_type,
            metadata=AuditDocumentMetadata(
                document_id=entry.document_id,
                title=entry.title,
                filing_type=entry.filing_type,
                security_code=entry.security_code,
                security_name=entry.security_name,
            ),
            source_proof=source_proof,
            image_artifact_resolver=image_resolver,
            image_hash_provider=lambda: image_hashes,
        )
        return {
            **entry.as_dict(),
            "ok": report.ok,
            "failure_family": None,
            "metrics": report.metrics,
            "build_stats": stats.as_dict(),
            "source_replay": source_replay_evidence,
            "source_observations": observations,
            "findings": [item.as_dict() for item in report.findings],
        }
    except Exception as exc:
        failure_family = _exception_failure_family(exc)
        return {
            **entry.as_dict(),
            "ok": False,
            "failure_family": failure_family,
            "metrics": {},
            "build_stats": {},
            "source_observations": observations,
            "findings": [
                {
                    "code": "audit_execution_error",
                    "failure_family": failure_family,
                    "severity": "error",
                    "message": f"{exc.__class__.__name__}: {exc}",
                    "source_ref": None,
                    "unit_order": None,
                }
            ],
        }


def _replay_source_ir(
    frozen_ir: dict[str, Any],
    *,
    data_root: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    Mapping[str, Any],
    SourceEvidenceProof,
    NativeStructureIndex,
]:
    """Re-run the production reconciliation/mapper from immutable raw output."""

    parser_artifacts = frozen_ir.get("parser_artifacts")
    if not isinstance(parser_artifacts, dict):
        raise ValueError("source replay requires parser_artifacts")
    frozen_version = read_normalized_ir_version(frozen_ir)
    content_descriptor = _artifact_descriptor(
        parser_artifacts,
        version=frozen_version,
        role="content_list",
        required=True,
    )
    assert content_descriptor is not None
    content_relpath, content_sha256, content_size = content_descriptor
    content_path = _under_root(
        data_root / "data",
        _safe_relpath(content_relpath),
    )
    content_artifact = MinerUArtifactReader().read_content_artifact(content_path)
    _verify_artifact_descriptor(
        role="content_list",
        raw=content_artifact.raw,
        expected_sha256=content_sha256,
        expected_size=content_size,
    )
    content_list = content_artifact.items
    source_pdf_relpath = frozen_ir.get("source_pdf")
    if not isinstance(source_pdf_relpath, str) or not source_pdf_relpath:
        raise ValueError("source replay requires source_pdf")
    source_pdf_path = _under_root(
        data_root / "data",
        _safe_relpath(source_pdf_relpath),
    )
    source_pdf_raw = source_pdf_path.read_bytes()
    source_pdf_sha256 = "sha256:" + hashlib.sha256(source_pdf_raw).hexdigest()
    encoded_hash = re.search(
        r"(?:^|/)sha256_([0-9a-f]{64})\.pdf$",
        source_pdf_relpath,
    )
    if encoded_hash is not None and (
        source_pdf_sha256 != "sha256:" + encoded_hash.group(1)
    ):
        raise ValueError("source PDF hash differs from its registered path")
    frozen_source_hash = frozen_ir.get("source_pdf_sha256")
    if frozen_source_hash is not None and frozen_source_hash != source_pdf_sha256:
        raise ValueError("source PDF hash differs from frozen IR")
    frozen_elements = frozen_ir.get("elements")
    if not isinstance(frozen_elements, list) or len(frozen_elements) != len(
        content_list
    ):
        raise ValueError("frozen IR does not preserve content-list item cardinality")
    frozen_hashes_verified = 0
    for index, (source_item, frozen_element) in enumerate(
        zip(content_list, frozen_elements, strict=True)
    ):
        if (
            not isinstance(frozen_element, dict)
            or frozen_element.get("source_item_index") != index
            or frozen_element.get("order_index") != index
        ):
            raise ValueError(f"frozen IR item identity mismatch at index {index}")
        frozen_hash = frozen_element.get("source_item_sha256")
        if frozen_hash is None:
            continue
        if frozen_hash != mineru_provider_item_sha256(source_item):
            raise ValueError(f"frozen IR source item hash mismatch at index {index}")
        frozen_hashes_verified += 1

    model_path: Path | None = None
    model_descriptor = _artifact_descriptor(
        parser_artifacts,
        version=frozen_version,
        role="model",
        required=False,
    )
    model_relpath: str | None = None
    if model_descriptor is not None:
        model_relpath, model_sha256, model_size = model_descriptor
        model_path = _under_root(
            data_root / "data",
            _safe_relpath(model_relpath),
        )
        if not model_path.is_file():
            raise FileNotFoundError(f"model artifact not found: {model_relpath}")
        _verify_artifact_descriptor(
            role="model",
            raw=model_path.read_bytes(),
            expected_sha256=model_sha256,
            expected_size=model_size,
        )
    visual_descriptor = _artifact_descriptor(
        parser_artifacts,
        version=frozen_version,
        role="visual_semantics",
        required=True,
    )
    if visual_descriptor is None:
        raise FileNotFoundError(
            "visual_semantics artifact descriptor is required but missing"
        )
    visual_relpath, visual_sha256, visual_size = visual_descriptor
    visual_semantics_path = _under_root(
        data_root / "data",
        _safe_relpath(visual_relpath),
    )
    visual_semantics_bytes = visual_semantics_path.read_bytes()
    _verify_artifact_descriptor(
        role="visual_semantics",
        raw=visual_semantics_bytes,
        expected_sha256=visual_sha256,
        expected_size=visual_size,
    )

    located = MinerUArtifactReader().locate(content_path.parent)
    located_content = located.paths["content_list"]
    if located_content is None or located_content.resolve() != content_path.resolve():
        raise ValueError("source replay located a different content_list artifact")
    if model_path is not None:
        located_model = located.paths["model"]
        if located_model is None or located_model.resolve() != model_path.resolve():
            raise ValueError("source replay located a different model artifact")

    parser = frozen_ir.get("parser")
    if not isinstance(parser, dict):
        raise ValueError("source replay requires parser identity")
    parser_info = MinerUParserInfo.from_payload(parser)
    parsed_pages = frozen_ir.get("parsed_pages")
    if not isinstance(parsed_pages, dict):
        raise ValueError("source replay requires parsed_pages")
    full_pdf = parsed_pages.get("full_pdf") is True
    start_page_no = parsed_pages.get("start_page_no")
    end_page_no = parsed_pages.get("end_page_no")
    start_page = (
        None if full_pdf or not isinstance(start_page_no, int) else start_page_no - 1
    )
    end_page = None if full_pdf or not isinstance(end_page_no, int) else end_page_no - 1
    content_list_v2_path = located.paths.get("content_list_v2")
    if content_list_v2_path is None:
        raise ValueError("source replay requires MinerU content_list_v2 artifact")
    content_list_v2 = MinerUArtifactReader().read_content_list_v2(content_list_v2_path)
    middle_path = located.paths.get("middle")
    if middle_path is None:
        raise ValueError("source replay requires MinerU middle artifact")
    artifact_root_relpath = parser_artifacts.get("artifact_root_relpath")
    if not isinstance(artifact_root_relpath, str) or not artifact_root_relpath:
        raise ValueError("source replay requires artifact_root_relpath")
    artifact_root = _under_root(
        data_root / "data",
        _safe_relpath(artifact_root_relpath),
    )
    if located.root.resolve() != artifact_root.resolve():
        raise ValueError("source replay artifact root differs from manifest")
    with TemporaryDirectory(prefix="disclosure-source-evidence-") as temp_dir:
        build = build_current_ir_from_mineru_artifacts(
            raw_pdf_path=source_pdf_path,
            source_pdf_sha256=source_pdf_sha256,
            content_artifact=content_artifact,
            content_list_v2=content_list_v2,
            middle_path=middle_path,
            model_path=model_path,
            parser_info=parser_info,
            document_metadata={
                "document_id": frozen_ir.get("document_id"),
                "source_pdf": frozen_ir.get("source_pdf"),
                "title": frozen_ir.get("title"),
            },
            visual_output_dir=Path(temp_dir) / "visual_evidence",
            start_page=start_page,
            end_page=end_page,
            visual_semantic_artifact=visual_semantics_bytes,
        )
        replay_parser_artifacts = build_parser_artifact_manifest(
            artifact_root=artifact_root,
            artifact_root_relpath=Path(artifact_root_relpath),
            artifact_paths={
                **located.paths,
                **build.evidence_image_paths,
                "visual_semantics": visual_semantics_path,
            },
        )
        verified_visual_hashes = _verify_visual_evidence_bytes(build.visual_evidence)
        source_visual_hashes = {
            role: verified_visual_hashes[role]
            for role in source_visual_artifact_descriptors(build.source_evidence)
        }
        replay_parser_artifacts = _bind_transient_source_artifacts(
            replay_parser_artifacts,
            native_structure=build.native_structure,
            source_evidence=build.source_evidence,
            visual_evidence=build.visual_evidence,
        )
    normalized_ir = build.normalized_ir
    if normalized_ir.get("parser") != parser:
        raise ValueError("source replay changed the frozen parser identity")
    normalized_ir["parser_artifacts"] = replay_parser_artifacts
    source_evidence = build.source_evidence
    page_count = int(build.native_structure["source_pdf_page_count"])
    validate_source_evidence_ledger(
        source_evidence,
        expected_source_pdf_sha256=source_pdf_sha256,
        expected_source_pdf_page_count=page_count,
        expected_mineru_artifact_sha256=build.content_list_sha256,
        mineru_content_list_bytes=content_artifact.raw,
        canonical_content_list=build.canonical_content_list,
        expected_mineru_typed_artifact_sha256=content_list_v2.sha256,
        native_structure=build.native_structure,
        mineru_middle_artifact=build.middle_artifact,
        parser_artifacts=replay_parser_artifacts,
    )
    validate_mapped_element_bindings(
        source_evidence,
        elements=normalized_ir["elements"],
    )
    replay_files = cast(
        Mapping[str, Mapping[str, Any]],
        replay_parser_artifacts["files"],
    )
    source_evidence_descriptor = replay_files["source_evidence"]
    source_proof = source_evidence_proof_from_validated_ledger(
        ledger=source_evidence,
        source_evidence_sha256=cast(
            str,
            source_evidence_descriptor["sha256"],
        ),
        visual_hashes=source_visual_hashes,
        visual_semantics=build.visual_semantics,
    )
    validate_current_normalized_ir_for_write(
        normalized_ir,
        write_authority="frozen_generation",
    )
    raw_struct_tree_citations = _validate_raw_struct_tree_citations(
        normalized_ir,
        build.native_structure,
    )
    elements = normalized_ir.get("elements")
    if (
        not isinstance(elements, list)
        or len(elements) != len(content_list)
        or [
            (element.get("source_item_index"), element.get("order_index"))
            for element in elements
            if isinstance(element, dict)
        ]
        != [(index, index) for index in range(len(content_list))]
    ):
        raise ValueError("source replay did not preserve item identity and order")
    for index, (source_item, element) in enumerate(
        zip(content_list, elements, strict=True)
    ):
        if not isinstance(element, dict) or element.get(
            "source_item_sha256"
        ) != mineru_provider_item_sha256(source_item):
            raise ValueError(f"source replay item hash mismatch at index {index}")
    return (
        normalized_ir,
        {
            "content_list_relpath": content_relpath,
            "content_list_sha256": build.content_list_sha256,
            "source_item_count": len(content_list),
            "reconciled_item_count": len(build.table_reconciliation.content_list),
            "frozen_source_item_hashes_verified": frozen_hashes_verified,
            "model_relpath": model_relpath,
            "model_sha256": build.table_reconciliation.stats.model_hash,
            "source_pdf_sha256": source_pdf_sha256,
            "source_pdf_page_count": page_count,
            "persistent_publication_artifact_closure_validated": False,
            "transient_source_evidence_rebuilt": True,
            "transient_visual_bytes_validated": True,
            "native_text_pages": source_evidence["coverage"]["native_text_pages"],
            "visual_pages": source_evidence["coverage"]["visual_pages"],
            "visual_regions": source_evidence["coverage"]["visual_regions"],
            "native_geometry_issues": source_evidence["coverage"][
                "native_geometry_issues"
            ],
            "raw_struct_tree_citations_validated": raw_struct_tree_citations,
        },
        source_evidence,
        source_proof,
        build.native_structure_index,
    )


_NON_SECTION_NATIVE_ROLES = frozenset({"TOC", "TOCI", "TABLE", "TH", "TD"})
_OracleNode = Mapping[str, Any]
_OracleBBox = tuple[float, float, float, float]
_OracleResolved = Mapping[int, tuple[_OracleNode, _OracleNode]]


def _validate_raw_struct_tree_citations(
    normalized_ir: Mapping[str, Any],
    native_structure: Mapping[str, Any],
) -> int:
    """Independently close emitted StructTree citations against raw PDF evidence."""

    proof = cast(Mapping[str, Any], normalized_ir["structure_proof"])
    headings = cast(list[_OracleNode], proof["headings"])
    elements = {
        int(item["source_item_index"]): item
        for item in cast(list[_OracleNode], normalized_ir["elements"])
    }
    nodes: dict[tuple[str, int], list[_OracleNode]] = defaultdict(list)
    for node in cast(list[_OracleNode], native_structure["nodes"]):
        identity = cast(str, node["segment_id"]), cast(int, node["node_id"])
        nodes[identity].append(node)
    marked: dict[tuple[int, int], list[_OracleNode]] = defaultdict(list)
    for item in cast(list[_OracleNode], native_structure["marked_content"]):
        key = cast(int, item["page_idx"]), cast(int, item["mcid"])
        marked[key].append(item)
    resolved: dict[int, tuple[_OracleNode, _OracleNode]] = {}
    native_claims: set[tuple[str, int]] = set()
    object_claims: set[tuple[int, int, int]] = set()
    for heading in headings:
        if "struct_tree" not in heading["evidence_kinds"]:
            continue
        identity = _cited_native_identity(heading)
        matches = nodes.get(identity, [])
        if len(matches) != 1 or identity in native_claims:
            raise ValueError("StructTree citation identity is missing or ambiguous")
        node = matches[0]
        if heading.get("native_role") != node.get("standard_role"):
            raise ValueError("StructTree citation role differs from raw node")
        _close_native_heading_objects(heading, node, elements, marked, object_claims)
        proof_node_id = cast(int, heading["node_id"])
        if proof_node_id in resolved:
            raise ValueError("StructTree proof node identity is invalid")
        native_claims.add(identity)
        resolved[proof_node_id] = (heading, node)
    for heading, node in resolved.values():
        _validate_raw_native_parent(heading, node, resolved=resolved)
    return len(resolved)


def _cited_native_identity(heading: _OracleNode) -> tuple[str, int]:
    return cast(str, heading["native_segment_id"]), cast(int, heading["native_node_id"])


def _close_native_heading_objects(
    heading: _OracleNode,
    node: _OracleNode,
    elements: Mapping[int, _OracleNode],
    marked: Mapping[tuple[int, int], list[_OracleNode]],
    object_claims: set[tuple[int, int, int]],
) -> None:
    sources = _oracle_heading_sources(heading, elements=elements)
    objects: list[_OracleNode] = []
    raw_refs = node.get("mcid_refs")
    if not isinstance(raw_refs, list) or not raw_refs:
        raise ValueError("StructTree citation has no raw MCID evidence")
    for raw_ref in cast(list[_OracleNode], raw_refs):
        page, mcid = cast(int, raw_ref["page_idx"]), cast(int, raw_ref["mcid"])
        matches = marked.get((page, mcid), [])
        if not matches:
            raise ValueError("StructTree citation MCID has no marked-content object")
        objects.extend(matches)
    assignments: dict[int, list[tuple[int, str]]] = defaultdict(list)
    local_claims: set[tuple[int, int, int]] = set()
    for obj in objects:
        identity = (
            cast(int, obj["page_idx"]),
            cast(int, obj["mcid"]),
            cast(int, obj["object_order"]),
        )
        bbox, text = _oracle_bbox(obj.get("bbox")), _oracle_text(obj.get("text"))
        # A StructTree MCID can also wrap spacing or non-text paint objects.
        # They are not reader-visible heading text, so do not turn their
        # absence into a false citation failure.  The exact concatenation
        # check below still requires every substantive heading glyph to close
        # against one source span.
        if not text:
            continue
        if bbox is None or identity in local_claims | object_claims:
            raise ValueError("invalid or reused StructTree marked-content object")
        candidates = [
            index
            for index, (source_page, source_bbox, source_text) in enumerate(sources)
            if identity[0] == source_page
            and _oracle_contains(source_bbox, bbox)
            and text in source_text
        ]
        if len(candidates) != 1:
            raise ValueError(
                "StructTree object-to-source binding is missing or ambiguous"
            )
        assignments[candidates[0]].append((identity[2], text))
        local_claims.add(identity)
    for index, (_, _, expected) in enumerate(sources):
        actual = "".join(text for _, text in sorted(assignments[index]))
        if actual != expected:
            raise ValueError("StructTree marked content does not close source text")
    object_claims.update(local_claims)


def _oracle_heading_sources(
    heading: _OracleNode,
    *,
    elements: Mapping[int, _OracleNode],
) -> list[tuple[int, _OracleBBox, str]]:
    output: list[tuple[int, _OracleBBox, str]] = []
    for ref in cast(list[_OracleNode], heading["source_refs"]):
        element = elements[cast(int, ref["source_item_index"])]
        start, end = cast(list[int], ref["text_span"])
        bbox = _oracle_bbox(element["bbox"])
        text = _oracle_text(cast(str, element["text"])[start:end])
        if bbox is None or not text:
            raise ValueError("StructTree source reference cannot be resolved")
        output.append((cast(int, element["page_idx"]), bbox, text))
    if not output or len({page for page, _, _ in output}) != 1:
        raise ValueError("StructTree heading sources must close on one page")
    return output


def _validate_raw_native_parent(
    heading: _OracleNode,
    node: _OracleNode,
    *,
    resolved: _OracleResolved,
) -> None:
    roles = node.get("ancestor_roles")
    ancestors = node.get("ancestor_node_ids")
    if (
        not isinstance(roles, list)
        or not all(isinstance(role, str) for role in roles)
        or not isinstance(ancestors, list)
        or not all(isinstance(item, int) for item in ancestors)
    ):
        raise ValueError("StructTree citation lacks raw ancestry")
    if heading.get("propagates") is True and {
        role.upper() for role in roles
    }.intersection(_NON_SECTION_NATIVE_ROLES):
        raise ValueError("propagating StructTree heading is inside a non-section role")
    parent_id = heading.get("parent_node_id")
    if parent_id is None:
        return
    parent = resolved.get(parent_id)
    child_segment, _ = _cited_native_identity(heading)
    if (
        parent is None
        or _cited_native_identity(parent[0])[0] != child_segment
        or _cited_native_identity(parent[0])[1] not in ancestors
    ):
        raise ValueError(
            "StructTree heading parent crosses or contradicts raw ancestry"
        )


def _oracle_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(unicodedata.normalize("NFKC", value).split())


def _oracle_bbox(value: object) -> _OracleBBox | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    bbox = cast(
        _OracleBBox,
        tuple(float(number) for number in value),
    )
    return bbox if bbox[0] < bbox[2] and bbox[1] < bbox[3] else None


def _oracle_contains(
    outer: _OracleBBox,
    inner: _OracleBBox,
) -> bool:
    return (
        outer[0] - 8 <= inner[0]
        and outer[1] - 8 <= inner[1]
        and inner[2] <= outer[2] + 8
        and inner[3] <= outer[3] + 8
    )


def _verify_visual_evidence_bytes(
    visual_evidence: tuple[VisualPageEvidence, ...],
) -> dict[str, str]:
    verified: dict[str, str] = {}
    for visual in visual_evidence:
        raw = visual.artifact_path.read_bytes()
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if (
            len(raw) != visual.size_bytes
            or actual != visual.sha256
            or _image_media_type(raw) != visual.media_type
        ):
            raise ValueError(
                f"transient source visual bytes differ: {visual.artifact_role}"
            )
        verified[visual.artifact_role] = actual
    return verified


@dataclass(frozen=True)
class _PersistedSourceBundle:
    proof: SourceEvidenceProof
    ledger: Mapping[str, Any]
    native_structure_index: NativeStructureIndex


def _load_persisted_source_bundle(
    normalized_ir: dict[str, Any],
    *,
    data_root: Path,
) -> _PersistedSourceBundle:
    version = read_normalized_ir_version(normalized_ir)
    if version != "normalized_ir.v4":
        raise LegacyEvidenceClosureError(
            "persisted source audit requires normalized_ir.v4 evidence closure; "
            f"got {version}"
        )
    parser_artifacts = normalized_ir.get("parser_artifacts")
    if not isinstance(parser_artifacts, dict):
        raise ValueError("normalized_ir.v4 requires parser_artifacts")
    artifact_payloads: dict[str, bytes] = {}

    def read_role(role: str) -> VerifiedParserArtifact:
        descriptor = _artifact_descriptor(
            parser_artifacts,
            version=version,
            role=role,
            required=True,
        )
        assert descriptor is not None
        relpath, expected_sha256, expected_size = descriptor
        raw = _under_root(
            data_root / "data",
            _safe_relpath(relpath),
        ).read_bytes()
        _verify_artifact_descriptor(
            role=role,
            raw=raw,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
        artifact_payloads[role] = raw
        return VerifiedParserArtifact(
            payload=raw,
            sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        )

    bundle = MinerUSourceEvidenceValidator().validate(
        normalized_ir,
        load_artifact=read_role,
    )
    for visual in bundle.proof.verified_visuals:
        if _image_media_type(artifact_payloads[visual.artifact_role]) != "image/png":
            raise ValueError(
                f"persisted source visual media type differs: {visual.artifact_role}"
            )
    raw_source_evidence = artifact_payloads["source_evidence"]
    raw_native_structure = artifact_payloads["pdf_structure"]
    try:
        decoded: object = json.loads(raw_source_evidence)
        native_decoded: object = json.loads(raw_native_structure)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("persisted source artifact is invalid JSON") from exc
    if not isinstance(decoded, Mapping) or not isinstance(native_decoded, Mapping):
        raise ValueError("persisted source artifacts must be JSON objects")
    return _PersistedSourceBundle(
        proof=bundle.proof,
        ledger=decoded,
        native_structure_index=validate_pdf_structure_artifact(
            native_decoded,
            expected_source_pdf_sha256=cast(
                str,
                normalized_ir["source_pdf_sha256"],
            ),
            expected_page_count=cast(
                int,
                normalized_ir["source_pdf_page_count"],
            ),
        ),
    )


def _load_persisted_source_evidence(
    normalized_ir: dict[str, Any],
    *,
    data_root: Path,
) -> tuple[SourceEvidenceProof, Mapping[str, Any]]:
    """Compatibility projection used by existing build-path parity tests."""

    bundle = _load_persisted_source_bundle(normalized_ir, data_root=data_root)
    return bundle.proof, bundle.ledger


def _bind_transient_source_artifacts(
    parser_artifacts: Mapping[str, Any],
    *,
    native_structure: Mapping[str, Any],
    source_evidence: Mapping[str, Any],
    visual_evidence: tuple[VisualPageEvidence, ...],
) -> dict[str, Any]:
    """Bind replayed bytes without claiming they were written to production."""

    root = parser_artifacts.get("artifact_root_relpath")
    raw_files = parser_artifacts.get("files")
    if not isinstance(root, str) or not root or not isinstance(raw_files, Mapping):
        raise ValueError("source replay parser artifact manifest is invalid")
    files = {
        str(role): dict(descriptor)
        for role, descriptor in raw_files.items()
        if isinstance(role, str) and isinstance(descriptor, Mapping)
    }

    def bind_json(role: str, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        files[role] = {
            "availability": "present",
            "relpath": f"{root}/{role}.json",
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }

    bind_json("pdf_structure", native_structure)
    bind_json("source_evidence", source_evidence)
    for visual in visual_evidence:
        files[visual.artifact_role] = {
            "availability": "present",
            "relpath": f"{root}/{visual.artifact_role}.png",
            "sha256": visual.sha256,
            "size_bytes": visual.size_bytes,
        }
    return {"artifact_root_relpath": root, "files": files}


def _artifact_descriptor(
    parser_artifacts: dict[str, Any],
    *,
    version: str,
    role: str,
    required: bool,
) -> tuple[str, str | None, int | None] | None:
    if version != "normalized_ir.v4":
        relpath = parser_artifacts.get(f"{role}_relpath")
        if relpath is None and not required:
            return None
        if not isinstance(relpath, str) or not relpath:
            raise ValueError(f"source replay requires {role}_relpath")
        return relpath, None, None

    files = parser_artifacts.get("files")
    if not isinstance(files, dict):
        raise ValueError("normalized_ir.v4 parser_artifacts requires files")
    descriptor = files.get(role)
    if not isinstance(descriptor, dict):
        if required:
            raise ValueError(f"source replay requires parser artifact role {role}")
        return None
    availability = descriptor.get("availability")
    if availability == "not_emitted" and not required:
        return None
    if availability != "present":
        raise ValueError(f"source replay requires present parser artifact {role}")
    relpath = descriptor.get("relpath")
    sha256 = descriptor.get("sha256")
    size_bytes = descriptor.get("size_bytes")
    if (
        not isinstance(relpath, str)
        or not relpath
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise ValueError(f"source replay parser artifact {role} descriptor is invalid")
    return relpath, sha256, size_bytes


def _verify_artifact_descriptor(
    *,
    role: str,
    raw: bytes,
    expected_sha256: str | None,
    expected_size: int | None,
) -> None:
    if expected_size is not None and len(raw) != expected_size:
        raise ValueError(
            f"source replay parser artifact {role} size mismatch: "
            f"{len(raw)} != {expected_size}"
        )
    if expected_sha256 is not None:
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if actual != expected_sha256:
            raise ValueError(
                f"source replay parser artifact {role} hash mismatch: "
                f"{actual} != {expected_sha256}"
            )


def _image_bindings(
    normalized_ir: dict[str, Any],
    *,
    data_root: Path,
) -> tuple[dict[str, str], Any]:
    parser_artifacts = normalized_ir.get("parser_artifacts")
    artifact_root: Path | None = None
    artifact_files: Mapping[str, Any] = {}
    if isinstance(parser_artifacts, dict):
        value = parser_artifacts.get("artifact_root_relpath")
        if isinstance(value, str) and value:
            artifact_root = _under_root(data_root / "data", _safe_relpath(value))
        files = parser_artifacts.get("files")
        if isinstance(files, Mapping):
            artifact_files = files

    image_elements = [
        element
        for element in normalized_ir.get("elements") or []
        if isinstance(element, dict)
        and (
            element.get("kind") in {"image", "equation"}
            or element.get("kind") == "table"
        )
    ]
    if artifact_root is None:
        if any(
            str(element.get("image_path") or "").strip() for element in image_elements
        ):
            raise ValueError("image source exists without parser artifact_root_relpath")
        return {}, None

    cache: dict[str, bytes] = {}

    def read_image(image_path: str) -> bytes:
        if image_path in cache:
            return cache[image_path]
        relative = _safe_relpath(image_path)
        candidate = _under_root(artifact_root, relative)
        if candidate.is_file():
            cache[image_path] = candidate.read_bytes()
            return cache[image_path]
        raise FileNotFoundError(f"image artifact not found: {image_path}")

    def resolve(artifact_role: str, image_path: str) -> ResolvedImageArtifact:
        content = read_image(image_path)
        role = artifact_role
        sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
        descriptor = artifact_files.get(role)
        if not isinstance(descriptor, Mapping) or (
            descriptor.get("availability") != "present"
            or descriptor.get("sha256") != sha256
            or descriptor.get("size_bytes") != len(content)
        ):
            raise ValueError(f"image manifest descriptor differs: {role}")
        return ResolvedImageArtifact(
            content=content,
            artifact_role=role,
            sha256=sha256,
            size_bytes=len(content),
            media_type=_image_media_type(content),
        )

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
        source_item_index = element.get("source_item_index")
        if not isinstance(source_item_index, int):
            raise ValueError(f"image source index is invalid: {ref}")
        role = f"evidence_image_{source_item_index:06d}"
        hashes[role] = resolve(role, image_path).sha256
        hashes[ref] = hashes[role]
        table = element.get("table")
        raw_media = table.get("embedded_media") if isinstance(table, Mapping) else None
        if not isinstance(raw_media, list):
            continue
        for media in raw_media:
            if not isinstance(media, Mapping):
                continue
            media_role = media.get("artifact_role")
            media_path = media.get("image_path")
            if not isinstance(media_role, str) or not isinstance(media_path, str):
                raise ValueError(f"table media source is invalid: {ref}")
            hashes[media_role] = resolve(media_role, media_path).sha256
    return hashes, resolve


def _source_observations(
    normalized_ir: Mapping[str, Any],
    *,
    source_evidence: Mapping[str, Any] | None,
    native_structure: NativeStructureIndex | None,
) -> dict[str, Any]:
    proof = normalized_ir.get("structure_proof")
    raw_conflicts = proof.get("conflicts") if isinstance(proof, Mapping) else None
    conflict_codes = (
        Counter(
            str(item.get("relation") or "invalid")
            for item in raw_conflicts
            if isinstance(item, Mapping)
        )
        if isinstance(raw_conflicts, list)
        else Counter()
    )
    proof_coverage = (
        {
            str(key): int(value)
            for key, value in raw_coverage.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if isinstance(proof, Mapping)
        and isinstance((raw_coverage := proof.get("coverage")), Mapping)
        else {}
    )

    raw_pages = (
        source_evidence.get("pages") if isinstance(source_evidence, Mapping) else None
    )
    fallback_reasons: Counter[str] = Counter()
    fallback_pages_by_reason: Counter[str] = Counter()
    fallback_page_reason_sets: Counter[str] = Counter()
    fallback_pages = 0
    if isinstance(raw_pages, list):
        for page in raw_pages:
            if not isinstance(page, Mapping):
                continue
            fallback_pages += int(page.get("fallback_required") is True)
            reasons = page.get("fallback_reasons")
            if isinstance(reasons, Mapping):
                valid_reasons = {
                    str(key): int(value)
                    for key, value in reasons.items()
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                }
                fallback_reasons.update(valid_reasons)
                fallback_pages_by_reason.update(valid_reasons.keys())
                if valid_reasons:
                    fallback_page_reason_sets["+".join(sorted(valid_reasons))] += 1
    raw_source_coverage = (
        source_evidence.get("coverage")
        if isinstance(source_evidence, Mapping)
        else None
    )
    source_coverage = (
        {
            str(key): int(value)
            for key, value in raw_source_coverage.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if isinstance(raw_source_coverage, Mapping)
        else {}
    )
    raw_retrieval_runs = (
        source_evidence.get("retrieval_runs")
        if isinstance(source_evidence, Mapping)
        else None
    )
    retrieval_boundary_basis: Counter[str] = Counter()
    if isinstance(raw_retrieval_runs, list):
        for run in raw_retrieval_runs:
            if not isinstance(run, Mapping):
                continue
            basis = run.get("boundary_basis")
            retrieval_boundary_basis[
                basis if isinstance(basis, str) and basis else "unspecified"
            ] += 1
    return {
        "structure_conflicts": dict(sorted(conflict_codes.items())),
        "structure_coverage": dict(sorted(proof_coverage.items())),
        "fallback_pages": fallback_pages,
        "fallback_reasons": dict(sorted(fallback_reasons.items())),
        "fallback_pages_by_reason": dict(sorted(fallback_pages_by_reason.items())),
        "fallback_page_reason_sets": dict(sorted(fallback_page_reason_sets.items())),
        "source_evidence_coverage": dict(sorted(source_coverage.items())),
        "retrieval_boundary_basis": dict(sorted(retrieval_boundary_basis.items())),
        "native_pdf_structure": _native_structure_observations(native_structure),
    }


def _native_structure_observations(
    native_structure: NativeStructureIndex | None,
) -> dict[str, Any]:
    if native_structure is None:
        return {
            "status": "missing",
            "pdfium_tagged": None,
            "roles": {},
            "diagnostics": {},
            "unresolved_reasons": {},
            "object_issues": {},
        }
    diagnostics = native_structure.diagnostics
    roles = Counter(node.standard_role for node in native_structure.nodes)
    diagnostic_counts = {
        "marked_content_objects": diagnostics.marked_content_objects,
        "object_issues": len(diagnostics.object_issues),
        "parent_conflicts": diagnostics.parent_conflicts,
        "referenced_mcid_refs": diagnostics.referenced_mcid_refs,
        "resolved_mcid_refs": diagnostics.resolved_mcid_refs,
        "root_reachable_nodes": diagnostics.root_reachable_nodes,
        "unresolved": len(diagnostics.unresolved_reasons),
        "unresolved_mcid_refs": len(diagnostics.unresolved_mcid_refs),
        "visible_mcid_anchors": diagnostics.visible_mcid_anchors,
    }
    unresolved_reasons = Counter(diagnostics.unresolved_reasons)
    object_issues = Counter(issue.reason for issue in diagnostics.object_issues)
    return {
        "status": native_structure.native_status,
        "pdfium_tagged": native_structure.pdfium_tagged,
        "roles": dict(sorted(roles.items())),
        "diagnostics": dict(sorted(diagnostic_counts.items())),
        "unresolved_reasons": dict(sorted(unresolved_reasons.items())),
        "object_issues": dict(sorted(object_issues.items())),
    }


def _image_media_type(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("unsupported image media type")


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
    source_replay: bool,
) -> dict[str, Any]:
    finding_codes: Counter[str] = Counter()
    failure_families: Counter[str] = Counter()
    unit_kinds: Counter[str] = Counter()
    structure_conflicts: Counter[str] = Counter()
    structure_coverage: Counter[str] = Counter()
    fallback_reasons: Counter[str] = Counter()
    fallback_pages_by_reason: Counter[str] = Counter()
    fallback_page_reason_sets: Counter[str] = Counter()
    source_evidence_coverage: Counter[str] = Counter()
    retrieval_boundary_basis: Counter[str] = Counter()
    native_statuses: Counter[str] = Counter()
    native_pdfium_tagged: Counter[str] = Counter()
    native_roles: Counter[str] = Counter()
    native_diagnostics: Counter[str] = Counter()
    native_unresolved_reasons: Counter[str] = Counter()
    native_object_issues: Counter[str] = Counter()
    structure_conflict_documents = 0
    native_object_issue_documents = 0
    fallback_documents = 0
    fallback_pages = 0
    breakdown: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"documents": 0, "failed_documents": 0, "units": 0}
    )
    total_units = 0
    total_errors = 0
    for result in results:
        failure_family = result.get("failure_family")
        if isinstance(failure_family, str) and failure_family:
            failure_families[failure_family] += 1
        metrics = result.get("metrics") or {}
        unit_count = int(metrics.get("unit_count") or 0)
        total_units += unit_count
        unit_kinds.update(metrics.get("unit_kinds") or {})
        observations = result.get("source_observations") or {}
        document_conflicts = observations.get("structure_conflicts") or {}
        structure_conflicts.update(document_conflicts)
        structure_conflict_documents += int(bool(document_conflicts))
        structure_coverage.update(observations.get("structure_coverage") or {})
        document_fallback_pages = int(observations.get("fallback_pages") or 0)
        fallback_pages += document_fallback_pages
        fallback_documents += int(document_fallback_pages > 0)
        fallback_reasons.update(observations.get("fallback_reasons") or {})
        fallback_pages_by_reason.update(
            observations.get("fallback_pages_by_reason") or {}
        )
        fallback_page_reason_sets.update(
            observations.get("fallback_page_reason_sets") or {}
        )
        source_evidence_coverage.update(
            observations.get("source_evidence_coverage") or {}
        )
        retrieval_boundary_basis.update(
            observations.get("retrieval_boundary_basis") or {}
        )
        native = observations.get("native_pdf_structure")
        if isinstance(native, Mapping):
            native_statuses[str(native.get("status") or "missing")] += 1
            tagged = native.get("pdfium_tagged")
            native_pdfium_tagged[
                "true" if tagged is True else "false" if tagged is False else "unknown"
            ] += 1
            native_roles.update(native.get("roles") or {})
            native_diagnostics.update(native.get("diagnostics") or {})
            native_unresolved_reasons.update(native.get("unresolved_reasons") or {})
            document_object_issues = native.get("object_issues") or {}
            native_object_issues.update(document_object_issues)
            native_object_issue_documents += int(bool(document_object_issues))
        else:
            native_statuses["unavailable"] += 1
            native_pdfium_tagged["unknown"] += 1
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
        "source_replay": source_replay,
        "rules_version": rules.RULES_VERSION,
        "documents": len(results),
        "ok_documents": sum(bool(result.get("ok")) for result in results),
        "failed_documents": sum(not result.get("ok") for result in results),
        "error_count": total_errors,
        "unit_count": total_units,
        "unit_kinds": dict(sorted(unit_kinds.items())),
        "finding_codes": dict(sorted(finding_codes.items())),
        "failure_families": dict(sorted(failure_families.items())),
        "source_observations": {
            "documents_with_structure_conflicts": (structure_conflict_documents),
            "structure_conflicts": dict(sorted(structure_conflicts.items())),
            "structure_coverage": dict(sorted(structure_coverage.items())),
            "documents_with_fallback_pages": fallback_documents,
            "fallback_pages": fallback_pages,
            "fallback_reasons": dict(sorted(fallback_reasons.items())),
            "fallback_pages_by_reason": dict(sorted(fallback_pages_by_reason.items())),
            "fallback_page_reason_sets": dict(
                sorted(fallback_page_reason_sets.items())
            ),
            "source_evidence_coverage": dict(sorted(source_evidence_coverage.items())),
            "retrieval_boundary_basis": dict(sorted(retrieval_boundary_basis.items())),
            "native_pdf_structure": {
                "statuses": dict(sorted(native_statuses.items())),
                "pdfium_tagged": dict(sorted(native_pdfium_tagged.items())),
                "roles": dict(sorted(native_roles.items())),
                "diagnostics": dict(sorted(native_diagnostics.items())),
                "unresolved_reasons": dict(sorted(native_unresolved_reasons.items())),
                "documents_with_object_issues": native_object_issue_documents,
                "object_issues": dict(sorted(native_object_issues.items())),
            },
        },
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
    parser.add_argument(
        "--source-replay",
        action="store_true",
        help="re-run reconciliation and mapping from content_list/model artifacts",
    )
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

    arguments = [
        (entry, str(args.data_root.resolve()), args.source_replay) for entry in entries
    ]
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
    summary = _summary(
        results,
        manifest_hash=manifest_hash,
        source_replay=args.source_replay,
    )
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
