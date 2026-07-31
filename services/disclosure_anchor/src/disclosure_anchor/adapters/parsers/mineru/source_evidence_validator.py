"""MinerU implementation of the application source-evidence boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, cast

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    resolved_table_html,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    SourceEvidenceContractError,
    source_visual_artifact_descriptors,
    validate_mapped_element_bindings,
    validate_source_evidence_ledger,
)
from disclosure_anchor.adapters.parsers.mineru.text_projection import (
    build_mineru_text_projections,
    mineru_serializer_backend,
)
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    TableHtmlStructureError,
    parse_table_html_structure,
)
from disclosure_anchor.adapters.parsers.mineru.visual_semantic_closure import (
    resolve_visual_semantic_closure,
)
from disclosure_anchor.application.ports.source_evidence import (
    ParserArtifactLoader,
    SourceEvidenceValidationError,
    ValidatedSourceEvidenceBundle,
)
from disclosure_anchor.application.contracts.source_evidence import (
    GeometryIssueEvent,
    LayoutPath,
    MappedSourceEvent,
    NativeTextEvent,
    RetrievalRunProof,
    SourceEvidenceProof,
    SourceEvidenceProofError,
    SourcePageEvent,
    SourcePageProof,
    SourceProofIdentity,
    VerifiedVisualArtifact,
    VisualArtifactProof,
    VisualBindingProof,
    VisualPageFallback,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)
from disclosure_anchor.application.contracts.visual_semantics import (
    VisualSemanticContractError,
    VisualSemanticClosure,
    ensure_no_unresolved_visuals,
    validate_visual_semantic_ir,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


class MinerUSourceEvidenceValidator:
    """Validate MinerU-specific artifacts and expose only the closed ledger."""

    def validate(
        self,
        normalized_ir: Mapping[str, Any],
        *,
        load_artifact: ParserArtifactLoader,
    ) -> ValidatedSourceEvidenceBundle:
        try:
            return _validate(normalized_ir, load_artifact=load_artifact)
        except SourceEvidenceContractError as exc:
            raise SourceEvidenceValidationError(
                exc.reason_code,
                str(exc),
            ) from exc


def _validate(
    normalized_ir: Mapping[str, Any],
    *,
    load_artifact: ParserArtifactLoader,
) -> ValidatedSourceEvidenceBundle:
    content_list = load_artifact("content_list")
    content_list_v2 = load_artifact("content_list_v2")
    middle = load_artifact("middle")
    model = load_artifact("model")
    pdf_structure = load_artifact("pdf_structure")
    source_evidence = load_artifact("source_evidence")
    visual_semantics_artifact = load_artifact("visual_semantics")
    try:
        decoded: object = json.loads(source_evidence.payload)
        native_decoded: object = json.loads(pdf_structure.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceEvidenceContractError(
            "source_evidence_json_invalid",
            f"source evidence or native structure artifact is invalid JSON: {exc}",
        ) from exc
    if not isinstance(native_decoded, Mapping):
        raise SourceEvidenceContractError(
            "native_structure_invalid",
            "native PDF structure artifact must be an object",
        )

    source_pdf_sha256 = normalized_ir["source_pdf_sha256"]
    source_pdf_page_count = normalized_ir["source_pdf_page_count"]
    elements = normalized_ir["elements"]
    parser = normalized_ir["parser"]
    assert isinstance(source_pdf_sha256, str)
    assert isinstance(source_pdf_page_count, int)
    assert isinstance(elements, list)
    assert isinstance(parser, Mapping)
    parsed_pages = normalized_ir.get("parsed_pages")
    if (
        not isinstance(parsed_pages, Mapping)
        or parsed_pages.get("full_pdf") is not True
        or parsed_pages.get("start_page_no") != 1
        or parsed_pages.get("end_page_no") != source_pdf_page_count
    ):
        raise SourceEvidenceContractError(
            "mineru_text_projection_range_invalid",
            "full-PDF artifacts must cover every source page",
        )
    package_version = parser.get("package_version")
    backend = parser.get("backend")
    if not isinstance(package_version, str) or not isinstance(backend, str):
        raise SourceEvidenceContractError(
            "middle_artifact_identity_invalid",
            "normalized IR parser identity is invalid",
        )

    artifact_reader = MinerUArtifactReader()
    try:
        canonical_content_list = artifact_reader.read_content_list_bytes(
            content_list.payload
        )
        typed_content_list = artifact_reader.read_content_list_v2_bytes(
            content_list_v2.payload
        )
        if len(typed_content_list) != source_pdf_page_count:
            raise ParserOutputContractError(
                "MinerU content_list_v2 page count differs from the source PDF"
            )
        text_projections = build_mineru_text_projections(
            canonical_content_list,
            typed_content_list,
            serializer_backend=mineru_serializer_backend(backend),
            page_offset=0,
            expected_page_count=source_pdf_page_count,
        )
    except ParserOutputContractError as exc:
        raise SourceEvidenceContractError(
            "mineru_text_projection_invalid",
            str(exc),
        ) from exc
    try:
        middle_artifact = artifact_reader.read_middle_bytes(
            middle.payload,
            expected_version=package_version,
            expected_backend=backend.split("-", 1)[0],
            expected_page_count=source_pdf_page_count,
        )
    except ParserOutputContractError as exc:
        raise SourceEvidenceContractError(
            "middle_artifact_invalid",
            str(exc),
        ) from exc

    ledger = validate_source_evidence_ledger(
        decoded,
        expected_source_pdf_sha256=source_pdf_sha256,
        expected_source_pdf_page_count=source_pdf_page_count,
        expected_mineru_artifact_sha256=content_list.sha256,
        mineru_content_list_bytes=content_list.payload,
        canonical_content_list=text_projections.canonical_items,
        expected_mineru_typed_artifact_sha256=content_list_v2.sha256,
        native_structure=native_decoded,
        mineru_middle_artifact=middle_artifact,
        parser_artifacts=cast(
            Mapping[str, Any],
            normalized_ir["parser_artifacts"],
        ),
    )
    validate_mapped_element_bindings(
        ledger,
        elements=cast(list[Mapping[str, Any]], elements),
    )

    table_structures = {}
    try:
        for source_index, item in enumerate(canonical_content_list):
            if item.get("type") != "table":
                continue
            html = resolved_table_html(item)
            if isinstance(html, str) and html.strip():
                table_structures[source_index] = parse_table_html_structure(
                    html
                )
        parser_target = ParserTargetIdentity.from_payload(parser)

        def resolve_visual_artifact(role: str) -> tuple[None, str]:
            return None, load_artifact(role).sha256

        visual_semantics = resolve_visual_semantic_closure(
            identity_content_list=canonical_content_list,
            canonical_content_list=text_projections.canonical_items,
            table_structures=table_structures,
            artifact_resolver=resolve_visual_artifact,
            source_evidence=ledger,
            source_pdf_sha256=source_pdf_sha256,
            source_pdf_page_count=source_pdf_page_count,
            source_evidence_sha256=source_evidence.sha256,
            content_list_sha256=content_list.sha256,
            content_list_v2_sha256=content_list_v2.sha256,
            middle_sha256=middle.sha256,
            model_sha256=model.sha256,
            parser_target=parser_target,
            server_url=None,
            persisted_artifact=visual_semantics_artifact.payload,
        )
        validate_visual_semantic_ir(
            normalized_ir,
            visual_semantics,
            artifact_sha256=visual_semantics_artifact.sha256,
        )
        ensure_no_unresolved_visuals(visual_semantics)
    except (
        ParserOutputContractError,
        ParserTargetIdentityError,
        TableHtmlStructureError,
        VisualSemanticContractError,
    ) as exc:
        raise SourceEvidenceContractError(
            getattr(exc, "reason_code", "visual_semantics_invalid"),
            f"visual semantic closure is invalid: {exc}",
        ) from exc

    visual_hashes: dict[str, str] = {}
    for role in source_visual_artifact_descriptors(ledger):
        visual_hashes[role] = load_artifact(role).sha256
    for record in ledger["carrier_support"]:
        assert isinstance(record, Mapping)
        support = record.get("support")
        if not isinstance(support, Mapping):
            continue
        artifact = support.get("artifact")
        if not isinstance(artifact, Mapping):
            continue
        artifact_role = artifact.get("artifact_role")
        assert isinstance(artifact_role, str)
        if (
            support.get("kind") == "generated_annotation"
            and artifact_role not in visual_hashes
        ):
            load_artifact(artifact_role)
    try:
        proof = source_evidence_proof_from_validated_ledger(
            ledger=ledger,
            source_evidence_sha256=source_evidence.sha256,
            visual_hashes=visual_hashes,
            visual_semantics=visual_semantics,
        )
    except (KeyError, TypeError, ValueError, AssertionError) as exc:
        if isinstance(exc, SourceEvidenceProofError):
            message = str(exc)
        else:
            message = f"{exc.__class__.__name__}: {exc}"
        raise SourceEvidenceContractError(
            "source_evidence_proof_invalid",
            f"validated MinerU ledger cannot form a typed source proof: {message}",
        ) from exc
    return ValidatedSourceEvidenceBundle(proof=proof)


def source_evidence_proof_from_validated_ledger(
    *,
    ledger: Mapping[str, Any],
    source_evidence_sha256: str,
    visual_hashes: Mapping[str, str],
    visual_semantics: VisualSemanticClosure | None = None,
) -> SourceEvidenceProof:
    """Normalize an already validated MinerU ledger without reinterpreting it."""

    source_pdf = cast(Mapping[str, Any], ledger["source_pdf"])
    raw_pages = cast(list[Mapping[str, Any]], ledger["pages"])
    raw_atoms = cast(list[Mapping[str, Any]], ledger["atoms"])
    raw_support = cast(list[Mapping[str, Any]], ledger["carrier_support"])
    raw_occurrences = cast(
        list[Mapping[str, Any]],
        ledger["visual_occurrences"],
    )

    visual_bindings: list[VisualBindingProof] = []
    visually_bound_pages: set[int] = set()
    seen_guards: set[tuple[int, int, str]] = set()
    page_visuals = {
        cast(int, page["page_idx"]): page.get("visual_artifact") for page in raw_pages
    }
    for record in raw_support:
        support = cast(Mapping[str, Any], record["support"])
        if support["kind"] != "visual_bound":
            continue
        selector = cast(Mapping[str, Any], record["selector"])
        artifact = cast(Mapping[str, Any], support["artifact"])
        page_idx = cast(int, record["page_idx"])
        guard_key = (
            cast(int, selector["source_item_index"]),
            page_idx,
            cast(str, artifact["artifact_role"]),
        )
        if guard_key in seen_guards:
            # One guard crop backs every unmatched field of its carrier
            # (e.g. table_caption and table_html); the binding is a
            # carrier-level fact and must stay single in the proof.
            continue
        seen_guards.add(guard_key)
        visual_bindings.append(
            VisualBindingProof(
                source_item_index=cast(int, selector["source_item_index"]),
                page_idx=page_idx,
                kind="carrier_guard",
                artifact=_visual_artifact(artifact),
            )
        )
        if artifact == page_visuals.get(page_idx):
            visually_bound_pages.add(page_idx)
    for record in raw_occurrences:
        visual_bindings.append(
            VisualBindingProof(
                source_item_index=cast(int, record["source_item_index"]),
                page_idx=cast(int, record["page_idx"]),
                kind="occurrence_crop",
                artifact=_visual_artifact(cast(Mapping[str, Any], record["artifact"])),
            )
        )

    events_by_page: dict[int, list[SourcePageEvent]] = {
        page_idx: [] for page_idx in range(cast(int, source_pdf["page_count"]))
    }
    for atom_index, atom in enumerate(raw_atoms):
        source = cast(Mapping[str, Any], atom["source"])
        disposition = cast(Mapping[str, Any], atom["disposition"])
        page_idx = cast(int, source["page_idx"])
        if disposition["kind"] == "mineru_carrier":
            carrier = cast(Mapping[str, Any], disposition["carrier"])
            selector = cast(Mapping[str, Any], carrier["selector"])
            event: SourcePageEvent = MappedSourceEvent(
                atom_index=atom_index,
                word_order=cast(int, source["order"]),
                source_item_index=cast(int, selector["source_item_index"]),
                order_state=cast(
                    Literal["monotonic", "conflict"],
                    disposition["source_order"],
                ),
                selector_field=cast(str, selector["field"]),
                selector_index=cast("int | None", selector.get("index")),
                selector_char_span=_span(cast(list[int], selector["char_span"])),
                selector_value_sha256=cast(str, selector["value_sha256"]),
                carrier_order=cast(int, carrier["order"]),
                carrier_bbox=_bbox(cast(list[int | float], carrier["bbox"])),
                atom_bbox=_bbox(cast(list[int | float], source["bbox"])),
            )
        else:
            event = NativeTextEvent(
                atom_index=atom_index,
                word_order=cast(int, source["order"]),
                text=cast(str, source["text"]),
                text_sha256=cast(str, source["text_sha256"]),
                bbox=_bbox(cast(list[int | float], source["bbox"])),
                char_span=_span(cast(list[int], source["char_span"])),
                layout_path=_layout_path(cast(list[int], source["layout_path"])),
            )
        events_by_page[page_idx].append(event)

    pages: list[SourcePageProof] = []
    for raw_page in raw_pages:
        page_idx = cast(int, raw_page["page_idx"])
        visual = raw_page.get("visual_artifact")
        for issue in cast(
            list[Mapping[str, Any]],
            raw_page["geometry_issues"],
        ):
            assert isinstance(visual, Mapping)
            raw_bbox = issue.get("raw_bbox")
            events_by_page[page_idx].append(
                GeometryIssueEvent(
                    word_order=cast(int, issue["word_order"]),
                    text=cast(str, issue["text"]),
                    text_sha256=cast(str, issue["text_sha256"]),
                    raw_bbox=(
                        _bbox(cast(list[int | float], raw_bbox))
                        if isinstance(raw_bbox, list)
                        else None
                    ),
                    reason=cast(str, issue["reason"]),
                    visual_artifact=_visual_artifact(visual),
                )
            )
        events = tuple(
            sorted(
                events_by_page[page_idx],
                key=lambda event: event.word_order,
            )
        )
        semantic = (
            next(
                (
                    item
                    for item in visual_semantics.dispositions
                    if item.occurrence_kind == "visual_page"
                    and item.page_idx == page_idx
                ),
                None,
            )
            if visual_semantics is not None
            else None
        )
        visual_only = (
            VisualPageFallback(
                visual_artifact=_visual_artifact(cast(Mapping[str, Any], visual)),
                semantic_text=(
                    semantic.semantic_text if semantic is not None else None
                ),
                semantic_text_sha256=(
                    semantic.semantic_text_sha256 if semantic is not None else None
                ),
            )
            if (
                raw_page["modality"] == "visual_page"
                and not raw_page["geometry_issues"]
                and raw_page["fallback_required"] is True
                and page_idx not in visually_bound_pages
            )
            else None
        )
        pages.append(
            SourcePageProof(
                page_idx=page_idx,
                events=events,
                visual_only=visual_only,
            )
        )

    retrieval_runs = tuple(
        RetrievalRunProof(
            page_idx=cast(int, record["page_idx"]),
            run_index=cast(int, record["run_index"]),
            atom_indices=tuple(cast(list[int], record["atom_indices"])),
            text_sha256=cast(str, record["text_sha256"]),
            boundary_basis=cast(
                Literal[
                    "native_complete_cell",
                    "native_table_guard",
                    "provider_table_guard",
                    "source_layout",
                ],
                record["boundary_basis"],
            ),
        )
        for record in cast(
            list[Mapping[str, Any]],
            ledger["retrieval_runs"],
        )
    )
    return SourceEvidenceProof(
        identity=SourceProofIdentity(
            source_evidence_sha256=source_evidence_sha256,
            source_pdf_sha256=cast(str, source_pdf["sha256"]),
            page_count=cast(int, source_pdf["page_count"]),
        ),
        pages=tuple(pages),
        retrieval_runs=retrieval_runs,
        visual_bindings=tuple(visual_bindings),
        verified_visuals=tuple(
            VerifiedVisualArtifact(artifact_role=role, sha256=sha256)
            for role, sha256 in sorted(visual_hashes.items())
        ),
    )


def _visual_artifact(value: Mapping[str, Any]) -> VisualArtifactProof:
    return VisualArtifactProof(
        artifact_role=cast(str, value["artifact_role"]),
        sha256=cast(str, value["sha256"]),
        size_bytes=cast(int, value["size_bytes"]),
        pixel_width=cast(int, value["pixel_width"]),
        pixel_height=cast(int, value["pixel_height"]),
        media_type=cast(Literal["image/png"], value["media_type"]),
    )


def _bbox(value: list[int | float]) -> tuple[float, float, float, float]:
    return (
        float(value[0]),
        float(value[1]),
        float(value[2]),
        float(value[3]),
    )


def _span(value: list[int]) -> tuple[int, int]:
    start, end = value
    return start, end


def _layout_path(value: list[int]) -> LayoutPath:
    flow, block, line, word = value
    return flow, block, line, word
