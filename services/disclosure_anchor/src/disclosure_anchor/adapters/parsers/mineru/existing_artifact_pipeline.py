"""Build current MinerU evidence and IR from an existing artifact bundle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUArtifactReader,
    MinerUContentArtifact,
    MinerUContentListV2Artifact,
    MinerUMiddleArtifact,
)
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    ExtractorIdentity,
    SourceEvidenceContractError,
    carrier_source_support_index,
    reconcile_source_evidence,
    required_carrier_visual_regions,
    required_visual_occurrence_regions,
    resolve_middle_table_roles,
    table_role_values_by_item,
    validate_mapped_element_bindings,
)
from disclosure_anchor.adapters.parsers.mineru.structure_proof import (
    build_mineru_structure_proof,
)
from disclosure_anchor.adapters.parsers.mineru.table_reconciler import (
    TableReconciliationResult,
    reconcile_content_list_tables,
)
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    ParsedHtmlTable,
)
from disclosure_anchor.adapters.parsers.mineru.text_projection import (
    build_mineru_text_projections,
    mineru_serializer_backend,
)
from disclosure_anchor.adapters.parsers.mineru.visual_semantic_closure import (
    VisualContentExtractor,
    resolve_visual_semantic_closure,
    semantic_dispositions_by_source,
    semantic_dispositions_by_table_media,
)
from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NativeStructureIndex,
    extract_pdf_structure,
    validate_pdf_structure_artifact,
)
from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextExtractionError,
    extract_native_pages,
    visual_guard_page_indices,
)
from disclosure_anchor.adapters.parsers.pdf_visual_evidence import (
    PdfVisualEvidenceError,
    VisualPageEvidence,
    render_pdf_visual_evidence,
)
from disclosure_anchor.application.contracts.document_structure import (
    DocumentStructureContractError,
    validate_document_structure,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    validate_reconciliation_generation,
)
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TableReconciliationContractError,
    validate_table_reconciliation_payload,
)
from disclosure_anchor.application.contracts.visual_semantics import (
    VisualSemanticClosure,
    VisualSemanticDisposition,
    bytes_sha256,
    canonical_json_bytes,
    visual_semantic_bytes,
    visual_semantic_diagnostics,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


@dataclass(frozen=True, slots=True)
class ExistingMinerUArtifactBuild:
    """The complete, source-bound result before caller-owned persistence."""

    normalized_ir: dict[str, Any]
    native_structure: dict[str, Any]
    native_structure_index: NativeStructureIndex
    source_evidence: Mapping[str, Any]
    visual_evidence: tuple[VisualPageEvidence, ...]
    table_reconciliation: TableReconciliationResult
    content_list_sha256: str
    canonical_content_list: tuple[dict[str, Any], ...]
    middle_artifact: MinerUMiddleArtifact
    evidence_image_paths: Mapping[str, Path]
    visual_semantics: VisualSemanticClosure
    visual_semantics_bytes: bytes


def build_current_ir_from_mineru_artifacts(
    *,
    raw_pdf_path: Path,
    source_pdf_sha256: str,
    content_artifact: MinerUContentArtifact,
    content_list_v2: MinerUContentListV2Artifact,
    middle_path: Path,
    model_path: Path | None,
    parser_info: MinerUParserInfo,
    document_metadata: dict[str, Any],
    visual_output_dir: Path,
    start_page: int | None = None,
    end_page: int | None = None,
    mapper: MinerUToNormalizedIRMapper | None = None,
    server_url: str | None = None,
    visual_semantic_extractor: VisualContentExtractor | None = None,
    visual_semantic_artifact: bytes | None = None,
) -> ExistingMinerUArtifactBuild:
    """Run the sole raw-PDF + MinerU-artifact to current-IR composition."""

    content_list = content_artifact.items
    content_list_bytes = content_artifact.raw
    content_list_sha256 = content_artifact.sha256
    registered_evidence_image_paths = content_artifact.evidence_image_paths
    native = extract_pdf_structure(
        raw_pdf_path,
        source_pdf_sha256=source_pdf_sha256,
    )
    page_count = native.get("source_pdf_page_count")
    if isinstance(page_count, bool) or not isinstance(page_count, int):
        raise ParserOutputContractError(
            "native PDF structure has an invalid page count"
        )
    text_projections = build_mineru_text_projections(
        content_list,
        content_list_v2.pages,
        serializer_backend=mineru_serializer_backend(parser_info.backend),
        page_offset=start_page or 0,
        expected_page_count=page_count,
    )
    canonical_content_list = list(text_projections.canonical_items)
    middle_artifact = MinerUArtifactReader().read_middle(
        middle_path,
        expected_version=parser_info.package_version,
        expected_backend=parser_info.backend.split("-", 1)[0],
        expected_page_count=page_count,
    )
    try:
        native_text = extract_native_pages(raw_pdf_path, source_pdf_sha256)
        table_roles = resolve_middle_table_roles(
            canonical_content_list,
            middle_artifact=middle_artifact,
            source_pages=native_text.pages,
        )
        guard_page_indices = visual_guard_page_indices(native_text.pages)
        guard_pages = frozenset(guard_page_indices)
        visual_evidence = render_pdf_visual_evidence(
            raw_pdf_path,
            source_pdf_sha256,
            full_pages=guard_page_indices,
            regions=tuple(
                request
                for request in required_carrier_visual_regions(
                    canonical_content_list,
                    source_pages=native_text.pages,
                    source_pdf_page_count=page_count,
                    table_role_overrides=table_roles,
                )
                if request.page_idx not in guard_pages
            ),
            occurrences=required_visual_occurrence_regions(
                content_list,
                source_pdf_page_count=page_count,
            ),
            artifact_dir=visual_output_dir,
        )
        visual_pages = visual_evidence.pages
        visual_regions = visual_evidence.regions
        visual_occurrences = visual_evidence.occurrences
        source_evidence = reconcile_source_evidence(
            source_pdf_sha256=source_pdf_sha256,
            source_pdf_page_count=page_count,
            source_extractor=ExtractorIdentity(
                "poppler-pdftotext+pdfinfo",
                (
                    f"{native_text.pdftotext_version}; "
                    f"{native_text.pdfinfo_version}"
                ),
            ),
            source_pages=native_text.pages,
            native_structure=native,
            mineru_content_list_bytes=content_list_bytes,
            expected_mineru_artifact_sha256=content_list_sha256,
            canonical_content_list=canonical_content_list,
            expected_mineru_typed_artifact_sha256=content_list_v2.sha256,
            mineru_extractor=ExtractorIdentity(
                parser_info.name,
                parser_info.package_version,
            ),
            middle_artifact=middle_artifact,
            table_role_overrides=table_roles,
            visual_pages=visual_pages,
            visual_regions=visual_regions,
            visual_occurrence_artifacts=visual_occurrences,
            generated_annotation_artifacts=registered_evidence_image_paths,
        )
        carrier_support = carrier_source_support_index(source_evidence)
    except (
        NativeTextExtractionError,
        PdfVisualEvidenceError,
        SourceEvidenceContractError,
    ) as exc:
        raise ParserOutputContractError(
            f"cannot establish source PDF evidence: {exc}"
        ) from exc

    if model_path is None:
        raise ParserOutputContractError(
            "MinerU model artifact is required for visual semantic attestation"
        )
    source_evidence_bytes = canonical_json_bytes(source_evidence)
    visual_artifact_paths = {
        **registered_evidence_image_paths,
        **{
            item.artifact_role: item.artifact_path
            for item in (*visual_pages, *visual_regions, *visual_occurrences)
        },
    }

    def resolve_visual_artifact(role: str) -> tuple[Path, str]:
        path = visual_artifact_paths.get(role)
        if path is None:
            raise KeyError(role)
        return path, _file_sha256(path, label=role)

    visual_semantics = resolve_visual_semantic_closure(
        identity_content_list=content_list,
        canonical_content_list=canonical_content_list,
        table_structures=content_artifact.table_structures,
        artifact_resolver=resolve_visual_artifact,
        source_evidence=source_evidence,
        source_pdf_sha256=source_pdf_sha256,
        source_pdf_page_count=page_count,
        source_evidence_sha256=bytes_sha256(source_evidence_bytes),
        content_list_sha256=content_list_sha256,
        content_list_v2_sha256=content_list_v2.sha256,
        middle_sha256=middle_artifact.sha256,
        model_sha256=_file_sha256(model_path, label="MinerU model"),
        parser_target=parser_info,
        server_url=server_url,
        extractor=visual_semantic_extractor,
        persisted_artifact=visual_semantic_artifact,
    )
    visual_semantics_payload = visual_semantic_bytes(visual_semantics)
    # The raw mapping stays the persisted artifact payload; every reader of its
    # facts goes through this single validated interpretation.
    native_index = validate_pdf_structure_artifact(
        native,
        expected_source_pdf_sha256=source_pdf_sha256,
        expected_page_count=page_count,
    )
    structure_proof = build_mineru_structure_proof(
        native=native_index,
        content_list=canonical_content_list,
        source_pdf_sha256=source_pdf_sha256,
        content_list_v2=content_list_v2.pages,
        text_projections=text_projections,
        start_page=start_page,
        end_page=end_page,
        carrier_source_support=carrier_support,
        table_role_overrides=table_roles,
        identity_content_list=content_list,
        source_pages=native_text.pages,
    )
    normalized_ir, reconciled = map_reconciled_mineru_content_list(
        content_list=canonical_content_list,
        identity_content_list=content_list,
        model_path=model_path,
        mapper=mapper or MinerUToNormalizedIRMapper(),
        parser_info=parser_info,
        document_metadata=document_metadata,
        structure_proof=structure_proof,
        source_pdf_sha256=source_pdf_sha256,
        source_pdf_page_count=page_count,
        registered_evidence_image_paths=registered_evidence_image_paths,
        content_table_structures=content_artifact.table_structures,
        table_role_values=table_role_values_by_item(table_roles),
        visual_semantics_by_source=semantic_dispositions_by_source(
            visual_semantics
        ),
        visual_semantics_by_table_media=(
            semantic_dispositions_by_table_media(visual_semantics)
        ),
        start_page=start_page,
        end_page=end_page,
    )
    normalized_ir["parser_diagnostics"] = {
        **normalized_ir["parser_diagnostics"],
        "visual_semantics": visual_semantic_diagnostics(visual_semantics),
    }
    try:
        validate_mapped_element_bindings(
            source_evidence,
            elements=normalized_ir["elements"],
        )
    except SourceEvidenceContractError as exc:
        raise ParserOutputContractError(
            f"invalid source evidence ledger [{exc.reason_code}]: {exc}"
        ) from exc
    return ExistingMinerUArtifactBuild(
        normalized_ir=normalized_ir,
        native_structure=native,
        native_structure_index=native_index,
        source_evidence=source_evidence,
        visual_evidence=(*visual_pages, *visual_regions, *visual_occurrences),
        table_reconciliation=reconciled,
        content_list_sha256=content_list_sha256,
        canonical_content_list=tuple(canonical_content_list),
        middle_artifact=middle_artifact,
        evidence_image_paths=registered_evidence_image_paths,
        visual_semantics=visual_semantics,
        visual_semantics_bytes=visual_semantics_payload,
    )


def map_reconciled_mineru_content_list(
    *,
    content_list: list[dict[str, Any]],
    identity_content_list: Sequence[Mapping[str, Any]] | None = None,
    model_path: Path | None,
    mapper: MinerUToNormalizedIRMapper,
    parser_info: MinerUParserInfo,
    document_metadata: dict[str, Any],
    structure_proof: Mapping[str, Any],
    source_pdf_sha256: str,
    source_pdf_page_count: int,
    registered_evidence_image_paths: Mapping[str, Path],
    content_table_structures: Mapping[int, ParsedHtmlTable] | None = None,
    table_role_values: Mapping[tuple[int, str], tuple[str, ...]] | None = None,
    visual_semantics_by_source: Mapping[
        int, VisualSemanticDisposition
    ] | None = None,
    visual_semantics_by_table_media: Mapping[
        tuple[int, int], VisualSemanticDisposition
    ] | None = None,
    parser_artifacts: Mapping[str, Any] | None = None,
    start_page: int | None = None,
    end_page: int | None = None,
) -> tuple[dict[str, Any], TableReconciliationResult]:
    """Map only after strict page-local table closure."""

    reconciled = reconcile_content_list_tables(
        content_list,
        model_path=model_path,
        registered_evidence_image_paths=registered_evidence_image_paths,
        content_table_structures=content_table_structures,
    )
    normalized_ir = mapper.map_content_list(
        content_list=reconciled.content_list,
        identity_content_list=identity_content_list,
        parser_info=parser_info,
        document_metadata=document_metadata,
        structure_proof=structure_proof,
        source_pdf_sha256=source_pdf_sha256,
        source_pdf_page_count=source_pdf_page_count,
        table_structures=content_table_structures,
        table_role_values=table_role_values,
        visual_semantics_by_source=visual_semantics_by_source,
        visual_semantics_by_table_media=visual_semantics_by_table_media,
        parser_artifacts=parser_artifacts,
        start_page=start_page,
        end_page=end_page,
    )
    existing = normalized_ir.get("parser_diagnostics")
    if existing is not None and not isinstance(existing, dict):
        raise ParserOutputContractError(
            "normalized IR parser_diagnostics must be an object"
        )
    normalized_ir["parser_diagnostics"] = {
        **(existing or {}),
        "table_reconciliation": reconciled.stats.as_dict(),
    }
    try:
        validate_document_structure(
            structure_proof,
            elements=normalized_ir["elements"],
            expected_source_pdf_sha256=source_pdf_sha256,
        )
        assessment = validate_table_reconciliation_payload(normalized_ir)
        validate_reconciliation_generation(
            version=str(normalized_ir["contract_version"]),
            algorithm_version=assessment.algorithm_version,
        )
    except (
        TableReconciliationContractError,
        DocumentStructureContractError,
        NormalizedIRVersionError,
    ) as exc:
        reason_code = getattr(exc, "reason_code", "invalid_contract")
        raise ParserOutputContractError(
            f"invalid reconciled table payload [{reason_code}]: {exc}"
        ) from exc
    return normalized_ir, reconciled


def _file_sha256(path: Path, *, label: str) -> str:
    try:
        return bytes_sha256(path.read_bytes())
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot read {label} artifact: {path}"
        ) from exc
