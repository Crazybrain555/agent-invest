"""Small source-bound NormalizedIR v4 fixtures shared by unit tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    mineru_provider_item_sha256,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    ExtractorIdentity,
    reconcile_source_evidence,
)
from disclosure_anchor.adapters.parsers.mineru.structure_proof import (
    build_mineru_structure_proof,
)
from disclosure_anchor.adapters.parsers.mineru.text_projection import (
    build_mineru_text_projections,
)
from tests.unit._native_support import test_carrier_source_support
from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextAtom,
    NativeTextGeometryIssue,
    NativeTextLayoutRef,
    NativeTextPage,
)
from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NATIVE_PDF_STRUCTURE_VERSION,
    validate_pdf_structure_artifact,
)
from disclosure_anchor.adapters.parsers.pdf_visual_evidence import (
    PNG_OPTIONS,
    RENDERER_IDENTITY,
    RENDER_OPTIONS,
    VisualPageEvidence,
)
from disclosure_anchor.application.contracts.parse_receipt import (
    build_parse_receipt,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    validate_current_normalized_ir_for_write,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.contracts.visual_semantics import (
    MINERU_VL_UTILS_PACKAGE_VERSION,
    VisualSemanticClosure,
    VisualSemanticDisposition,
    parser_target_sha256,
    text_sha256,
    visual_semantic_bytes,
    visual_semantic_diagnostics,
)


SOURCE_PDF_SHA256 = "sha256:" + "a" * 64
TEST_RUNTIME_BUNDLE_SHA256 = "sha256:" + "b" * 64


def write_text_ir_bundle(
    root: Path,
    ir_relpath: Path,
    *,
    texts: tuple[str, ...] = ("重要提示", "公司存在退市风险，请投资者注意。"),
    heading_levels: dict[int, int] | None = None,
    native_only_texts: tuple[str, ...] = (),
    native_page_texts: tuple[str, ...] | None = None,
    full_pdf: bool = True,
    page_visual: bool = False,
    image: bool = False,
    image_provider_text: str | None = "模型生成的来源图片描述",
    document_id: str = "doc_1",
    source_pdf: str = "raw.pdf",
    document_title: str = "公告",
    parser_target: ParserTargetIdentity | None = None,
) -> dict[str, Any]:
    """Write exact content/source-evidence artifacts and a matching v4 IR."""

    heading_levels = dict(heading_levels or {0: 1})
    native_positions = (
        {
            text: position
            for position, text in enumerate(native_page_texts)
            if native_page_texts.count(text) == 1
        }
        if native_page_texts is not None
        else {}
    )
    content_list = [
        {
            "type": "text",
            "text": text,
            "page_idx": 0,
            "bbox": [
                0,
                100 + native_positions.get(text, index) * 200,
                1000,
                200 + native_positions.get(text, index) * 200,
            ],
        }
        for index, text in enumerate(texts)
    ]
    for index, level in heading_levels.items():
        content_list[index]["text_level"] = level
    if image:
        image_item: dict[str, Any] = {
                "type": "image",
                "page_idx": 0,
                "bbox": [0, 600, 1000, 900],
                "img_path": "images/source.png",
                "image_caption": [],
                "image_footnote": [],
            }
        if image_provider_text is not None:
            image_item["text"] = image_provider_text
        content_list.append(image_item)
    content_bytes = _json_bytes(content_list)
    content_list_v2 = [
        [
            *[
                {
                    "type": ("title" if index in heading_levels else "paragraph"),
                    "bbox": item["bbox"],
                    "content": (
                        {
                            "level": heading_levels[index],
                            "title_content": [
                                {"type": "text", "content": item["text"]}
                            ],
                        }
                        if index in heading_levels
                        else {
                            "paragraph_content": [
                                {"type": "text", "content": item["text"]}
                            ]
                        }
                    ),
                }
                for index, item in enumerate(content_list[: len(texts)])
            ],
            *(
                [
                    {
                        "type": "image",
                        "bbox": content_list[-1]["bbox"],
                        "content": {
                            "image_caption": [],
                            "image_footnote": [],
                        },
                    }
                ]
                if image
                else []
            ),
        ]
    ]
    content_list_v2_bytes = _json_bytes(content_list_v2)
    text_projections = build_mineru_text_projections(
        content_list,
        content_list_v2,
        serializer_backend="pipeline",
        page_offset=0,
        expected_page_count=1,
    )
    native_page = _native_page(
        native_page_texts or (*texts, *native_only_texts),
        geometry_issue=page_visual,
        heading_texts=frozenset(
            texts[index] for index in heading_levels if index < len(texts)
        ),
    )
    native_structure = _untagged_native_structure()
    proof = build_mineru_structure_proof(
        native=validate_pdf_structure_artifact(
            native_structure,
            expected_source_pdf_sha256=SOURCE_PDF_SHA256,
            expected_page_count=1,
        ),
        content_list=content_list,
        content_list_v2=content_list_v2,
        text_projections=text_projections,
        source_pdf_sha256=SOURCE_PDF_SHA256,
        source_pages=(native_page,),
        carrier_source_support=test_carrier_source_support(
            content_list,
            source_pages=(native_page,),
        ),
    )
    target = parser_target or MinerUParserInfo(
        name="MinerU",
        package_version="3.4.0",
        backend="pipeline",
        method="auto",
        language="ch",
        formula=True,
        table=True,
        runtime_bundle_identity_sha256=TEST_RUNTIME_BUNDLE_SHA256,
        full_pdf=full_pdf,
        start_page=None if full_pdf else 0,
        end_page=None if full_pdf else 0,
    )
    normalized_ir = MinerUToNormalizedIRMapper().map_content_list(
        content_list=content_list,
        parser_info=target,
        document_metadata={
            "document_id": document_id,
            "source_pdf": source_pdf,
            "title": document_title,
        },
        structure_proof=proof,
        source_pdf_sha256=SOURCE_PDF_SHA256,
        source_pdf_page_count=1,
        start_page=target.start_page,
        end_page=target.end_page,
    )

    content_sha256 = _sha256(content_bytes)
    middle_bytes = _json_bytes(
        {
            "_backend": "pipeline",
            "_version_name": "3.4.0",
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [native_page.width, native_page.height],
                    "preproc_blocks": [],
                    "para_blocks": [],
                }
            ],
        }
    )
    middle_artifact = MinerUArtifactReader().read_middle_bytes(
        middle_bytes,
        expected_version="3.4.0",
        expected_backend="pipeline",
        expected_page_count=1,
    )
    visual_payload = b"\x89PNG\r\n\x1a\nsource-page-fixture"
    visual = VisualPageEvidence(
        page_idx=0,
        artifact_role="source_page_visual_000001",
        artifact_path=Path("/unused/source_page_visual_000001.png"),
        sha256=_sha256(visual_payload),
        size_bytes=len(visual_payload),
        pixel_width=100,
        pixel_height=100,
        media_type="image/png",
        renderer=RENDERER_IDENTITY,
        render_options=RENDER_OPTIONS,
        png_options=PNG_OPTIONS,
    )
    artifact_root = root / "parser/a"
    artifact_root.mkdir(parents=True, exist_ok=True)
    occurrence_payload = b"\x89PNG\r\n\x1a\nsource-occurrence-fixture"
    occurrence = VisualPageEvidence(
        page_idx=0,
        artifact_role=f"source_visual_occurrence_{len(content_list) - 1:06d}",
        artifact_path=artifact_root
        / f"source_visual_occurrence_{len(content_list) - 1:06d}.png",
        sha256=_sha256(occurrence_payload),
        size_bytes=len(occurrence_payload),
        pixel_width=100,
        pixel_height=100,
        media_type="image/png",
        renderer=RENDERER_IDENTITY,
        render_options=RENDER_OPTIONS,
        png_options=PNG_OPTIONS,
        bbox=(0.0, 600.0, 1000.0, 900.0),
    )
    generated_image_path = artifact_root / "images/source.png"
    generated_image_payload = b"\x89PNG\r\n\x1a\nsource-image-fixture"
    if image:
        generated_image_path.parent.mkdir(parents=True, exist_ok=True)
        generated_image_path.write_bytes(generated_image_payload)
    ledger = reconcile_source_evidence(
        source_pdf_sha256=SOURCE_PDF_SHA256,
        source_pdf_page_count=1,
        source_extractor=ExtractorIdentity("fixture-native", "1"),
        source_pages=(native_page,),
        native_structure=native_structure,
        mineru_content_list_bytes=content_bytes,
        expected_mineru_artifact_sha256=content_sha256,
        canonical_content_list=text_projections.canonical_items,
        expected_mineru_typed_artifact_sha256=_sha256(content_list_v2_bytes),
        mineru_extractor=ExtractorIdentity("MinerU", "3.4.0"),
        middle_artifact=middle_artifact,
        visual_pages=(visual,) if page_visual else (),
        visual_occurrence_artifacts=(occurrence,) if image else (),
        generated_annotation_artifacts=(
            {f"evidence_image_{len(content_list) - 1:06d}": (generated_image_path)}
            if image
            else {}
        ),
    )
    source_evidence_bytes = _json_bytes(ledger)
    dispositions = (
        (
            VisualSemanticDisposition(
                occurrence_id=f"source:{len(content_list) - 1:06d}",
                occurrence_kind="image",
                source_item_index=len(content_list) - 1,
                source_item_sha256=mineru_provider_item_sha256(
                    content_list[-1]
                ),
                page_idx=0,
                bbox=(0.0, 600.0, 1000.0, 900.0),
                table_media=None,
                artifact_role=occurrence.artifact_role,
                artifact_sha256=occurrence.sha256,
                semantic_text=image_provider_text,
                semantic_text_sha256=(
                    text_sha256(image_provider_text)
                    if image_provider_text is not None
                    else None
                ),
                semantic_origin=(
                    "provider_visual_text"
                    if image_provider_text is not None
                    else None
                ),
                status=(
                    "semantic_text"
                    if image_provider_text is not None
                    else "unresolved"
                ),
            ),
        )
        if image
        else ()
    )
    visual_semantics = VisualSemanticClosure(
        source_pdf_sha256=SOURCE_PDF_SHA256,
        source_pdf_page_count=1,
        source_evidence_sha256=_sha256(source_evidence_bytes),
        content_list_sha256=content_sha256,
        content_list_v2_sha256=_sha256(content_list_v2_bytes),
        middle_sha256=middle_artifact.sha256,
        model_sha256=_sha256(b"[]"),
        parser_target_sha256=parser_target_sha256(target.to_payload()),
        runtime_bundle_identity_sha256=TEST_RUNTIME_BUNDLE_SHA256,
        mineru_package_version="3.4.0",
        mineru_vl_utils_version=MINERU_VL_UTILS_PACKAGE_VERSION,
        enrichment_backend="http-client",
        enrichment_image_analysis=True,
        server_url_sha256=_sha256(b"http://fixture"),
        formula_enabled=True,
        dispositions=dispositions,
    )
    visual_semantics_payload = visual_semantic_bytes(visual_semantics)
    files = {
        "content_list": (
            artifact_root / "content.json",
            content_bytes,
        ),
        "content_list_v2": (
            artifact_root / "content_list_v2.json",
            content_list_v2_bytes,
        ),
        "model": (
            artifact_root / "model.json",
            b"[]",
        ),
        "middle": (
            artifact_root / "middle.json",
            middle_bytes,
        ),
        "pdf_structure": (
            artifact_root / "pdf_structure.json",
            _json_bytes(native_structure),
        ),
        "source_evidence": (
            artifact_root / "source_evidence.json",
            source_evidence_bytes,
        ),
        "visual_semantics": (
            artifact_root / "visual_semantics.json",
            visual_semantics_payload,
        ),
        "parse_receipt": (
            artifact_root / "parse_receipt.json",
            _json_bytes(
                build_parse_receipt(
                    source_pdf_sha256=SOURCE_PDF_SHA256,
                    parser_target_payload=normalized_ir["parser"],
                    server_url="http://fixture",
                    http_request_concurrency=None,
                    timeout_seconds=None,
                )
            ),
        ),
    }
    if page_visual:
        files[visual.artifact_role] = (
            artifact_root / f"{visual.artifact_role}.png",
            visual_payload,
        )
    if image:
        files[occurrence.artifact_role] = (
            occurrence.artifact_path,
            occurrence_payload,
        )
        files[f"evidence_image_{len(content_list) - 1:06d}"] = (
            generated_image_path,
            generated_image_payload,
        )
        files[f"evidence_image_{len(content_list) - 1:06d}"][0].parent.mkdir(
            parents=True,
            exist_ok=True,
        )
    for path, payload in files.values():
        path.write_bytes(payload)
    normalized_ir["parser_artifacts"] = {
        "artifact_root_relpath": "parser/a",
        "files": {
            role: {
                "availability": "present",
                "relpath": str(path.relative_to(root)),
                "sha256": _sha256(payload),
                "size_bytes": len(payload),
            }
            for role, (path, payload) in files.items()
        },
    }
    normalized_ir["parser_diagnostics"] = {
        "table_reconciliation": {
            "algorithm_version": "mineru-page-local-table-closure.v7",
                "comparison_contract": (
                    "reader-visible-table-projection.v1"
                ),
                "projection_root": "sha256:" + "c" * 64,
            "model_hash": _sha256(files["model"][1]),
            "content_tables": 0,
            "model_tables": 0,
            "matched_tables": 0,
            "page_local_closed": True,
        },
        "visual_semantics": visual_semantic_diagnostics(visual_semantics),
    }
    validate_current_normalized_ir_for_write(normalized_ir)
    ir_path = root / ir_relpath
    ir_path.parent.mkdir(parents=True, exist_ok=True)
    ir_path.write_bytes(_json_bytes(normalized_ir))
    return normalized_ir


def artifact_paths_from_ir(
    root: Path,
    normalized_ir: dict[str, Any],
) -> dict[str, Path | None]:
    files = normalized_ir["parser_artifacts"]["files"]
    return {
        str(role): (
            root / str(descriptor["relpath"])
            if descriptor["availability"] == "present"
            else None
        )
        for role, descriptor in files.items()
    }


def _native_page(
    texts: tuple[str, ...],
    *,
    geometry_issue: bool = False,
    heading_texts: frozenset[str] = frozenset(),
) -> NativeTextPage:
    parts: list[str] = []
    atoms: list[NativeTextAtom] = []
    offset = 0
    for order, text in enumerate(texts):
        if parts:
            parts.append("\n")
            offset += 1
        start = offset
        parts.append(text)
        offset += len(text)
        y0 = 10.0 + order * 20.0
        # A current-lane heading needs real native display evidence: the
        # heading line is centered and taller than the body's modal line
        # height, exactly like a printed display title.
        bbox = (
            (30.0, y0, 70.0, y0 + 14.0)
            if text in heading_texts
            else (0.0, y0, 100.0, y0 + 10.0)
        )
        atoms.append(
            NativeTextAtom(
                page_idx=0,
                order=order,
                bbox=bbox,
                char_span=(start, offset),
                text=text,
                layout=NativeTextLayoutRef(0, order, 0, 0),
            )
        )
    return NativeTextPage(
        page_idx=0,
        width=100.0,
        height=max(100.0, 20.0 * len(texts) + 10.0),
        text="".join(parts),
        atoms=tuple(atoms),
        geometry_issues=(
            (
                NativeTextGeometryIssue(
                    page_idx=0,
                    word_order=len(atoms),
                    text="不可定位字符",
                    raw_bbox=(0.0, 0.0, 0.0, 0.0),
                    reason="bbox_non_positive_extent",
                ),
            )
            if geometry_issue
            else ()
        ),
    )


def _untagged_native_structure() -> dict[str, Any]:
    return {
        "contract_version": NATIVE_PDF_STRUCTURE_VERSION,
        "source_pdf_sha256": SOURCE_PDF_SHA256,
        "source_pdf_page_count": 1,
        "native_status": "untagged",
        "pdfium_tagged": False,
        "role_map": {},
        "segments": [],
        "nodes": [],
        "marked_content": [],
        "bookmarks": [],
        "diagnostics": {
            "parent_conflicts": 0,
            "unresolved": [],
            "root_reachable_nodes": 0,
            "visible_mcid_anchors": 0,
            "marked_content_objects": 0,
            "referenced_mcid_refs": 0,
            "resolved_mcid_refs": 0,
            "unresolved_mcid_refs": [],
            "object_issues": [],
        },
    }


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
