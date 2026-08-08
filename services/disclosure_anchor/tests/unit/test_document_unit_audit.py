from __future__ import annotations

from collections.abc import Iterable
import copy
from dataclasses import replace
import hashlib
import unittest
from typing import Any, Mapping

from disclosure_anchor.application.contracts.document_structure import (
    DOCUMENT_STRUCTURE_ALGORITHM,
    DOCUMENT_STRUCTURE_VERSION,
    carrier_set_sha256,
)
from disclosure_anchor.application.contracts.source_evidence import (
    MappedSourceEvent,
    NativeTextEvent,
    RetrievalRunProof,
    SourceEvidenceProof,
    SourceEvidenceProofError,
    SourcePageProof,
    SourceProofIdentity,
    VerifiedVisualArtifact,
    VisualArtifactProof,
    VisualBindingProof,
)
from disclosure_anchor.application.services.document_unit_audit import (
    AuditDocumentMetadata,
    AuditUnitView,
    DocumentAuditReport,
    _flattened_node_targets,
    _ProofHeading,
    _ProofOwnerScopeBreak,
    audit_document as _audit_document,
)


_SOURCE_PDF_SHA256 = "sha256:" + "a" * 64


def audit_document(
    *,
    normalized_ir: dict[str, Any],
    units: Iterable[AuditUnitView],
    metadata: AuditDocumentMetadata,
    source_proof: SourceEvidenceProof | None = None,
    source_dispositions: Iterable[Mapping[str, Any]] = (),
    image_hashes: Mapping[str, str] | None = None,
) -> DocumentAuditReport:
    """Keep ordinary audit tests explicit about a closed, empty source proof."""

    if source_proof is None:
        source_pdf_sha256 = normalized_ir.get("source_pdf_sha256")
        page_count = normalized_ir.get("source_pdf_page_count")
        assert isinstance(source_pdf_sha256, str)
        assert isinstance(page_count, int)
        source_proof = SourceEvidenceProof(
            identity=SourceProofIdentity(
                source_evidence_sha256="sha256:" + "b" * 64,
                source_pdf_sha256=source_pdf_sha256,
                page_count=page_count,
            ),
            pages=tuple(
                SourcePageProof(page_idx=page_idx, events=())
                for page_idx in range(page_count)
            ),
            retrieval_runs=(),
            visual_bindings=(),
            verified_visuals=(),
        )
    return _audit_document(
        normalized_ir=normalized_ir,
        units=units,
        metadata=metadata,
        source_proof=source_proof,
        source_dispositions=source_dispositions,
        image_hashes=image_hashes,
    )


def _element(
    order: int,
    *,
    text: str,
    kind: str = "text",
    raw_kind: str = "text",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "document_id": "doc_audit",
        "ir_id": f"ir_{order:04d}",
        "source_item_index": order,
        "source_item_sha256": "sha256:"
        + hashlib.sha256(f"source:{order}".encode()).hexdigest(),
        "order_index": order,
        "page_idx": 0,
        "page_no": 1,
        "kind": kind,
        "raw_kind": raw_kind,
        "text": text,
        **extra,
    }


def _heading(
    node_id: int,
    source_index: int,
    *,
    text: str,
    section_span: tuple[int, int],
    parent_node_id: int | None = None,
    heading_level: int = 1,
    propagates: bool = True,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "parent_node_id": parent_node_id,
        "heading_level": heading_level,
        "propagates": propagates,
        "evidence_kinds": ["struct_tree"],
        "native_node_id": node_id,
        "native_role": f"H{heading_level}",
        "native_segment_id": "native_1",
        "section_span": list(section_span),
        "source_refs": [
            {
                "source_item_index": source_index,
                "field": "text",
                "text_span": [0, len(text)],
            }
        ],
    }


def _ir(
    elements: list[dict[str, Any]],
    *,
    headings: list[dict[str, Any]] | None = None,
    page_frames: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    proof_headings = copy.deepcopy(headings or [])
    frames = copy.deepcopy(page_frames or [])
    table_count = sum(element.get("raw_kind") == "table" for element in elements)
    return {
        "contract_version": "normalized_ir.v4",
        "created_at": "2026-07-27T00:00:00Z",
        "document_id": "doc_audit",
        "source_pdf": "raw/audit.pdf",
        "source_pdf_sha256": _SOURCE_PDF_SHA256,
        "source_pdf_page_count": 1,
        "title": "审计样本",
        "parser": {
            "name": "MinerU",
            "package_version": "3.4.0",
            "backend": "pipeline",
            "method": "auto",
            "language": "ch",
            "formula": False,
            "table": True,
            "effort": None,
            "image_analysis": False,
            "full_pdf": True,
            "start_page": None,
            "end_page": None,
            "runtime_bundle_identity_sha256": "sha256:" + "c" * 64,
            "inline_equation_left": "$",
            "inline_equation_right": "$",
            "target_contract_version": "parser-target.v2",
            "remote_model_name": None,
            "remote_selection_mode": "not_applicable",
        },
        "parser_artifacts": {
            "artifact_root_relpath": "parser/audit",
            "files": {
                role: {
                    "availability": "present",
                    "relpath": f"parser/audit/{role}.json",
                    "sha256": "sha256:" + "b" * 64,
                    "size_bytes": 1,
                }
                for role in (
                    "content_list",
                    "content_list_v2",
                    "middle",
                    "model",
                    "pdf_structure",
                    "source_evidence",
                    "visual_semantics",
                )
            },
        },
        "parsed_pages": {
            "start_page_no": 1,
            "end_page_no": 1,
            "full_pdf": True,
        },
        "parser_diagnostics": {
            "table_reconciliation": {
                "algorithm_version": "mineru-page-local-table-closure.v6",
                "model_hash": "sha256:" + "b" * 64,
                "content_tables": table_count,
                "model_tables": table_count,
                "matched_tables": table_count,
                "page_local_closed": True,
            },
            "visual_semantics": {
                "contract_version": "visual-semantics.v1",
                "artifact_role": "visual_semantics",
                "artifact_sha256": "sha256:" + "b" * 64,
                "disposition_count": 0,
                "status_counts": {
                    "semantic_text": 0,
                    "guard_only": 0,
                    "unresolved": 0,
                },
            },
        },
        "elements": elements,
        "structure_proof": {
            "contract_version": DOCUMENT_STRUCTURE_VERSION,
            "algorithm_version": DOCUMENT_STRUCTURE_ALGORITHM,
            "source_pdf_sha256": _SOURCE_PDF_SHA256,
            "source_pdf_page_count": 1,
            "carrier_set_sha256": carrier_set_sha256(elements),
            "native": {
                "status": "usable",
                "artifact_role": "pdf_structure",
            },
            "headings": proof_headings,
            "owner_scope_breaks": [],
            "page_frames": frames,
            "conflicts": [],
            "coverage": {
                "heading_nodes": len(proof_headings),
                "page_frame_groups": len(frames),
            },
        },
    }


def _source(order: int) -> dict[str, Any]:
    return {
        "kind": "normalized_ir_element",
        "ir_id": f"ir_{order:04d}",
        "source_item_index": order,
        "order_index": order,
        "page_no": 1,
    }


def _visual_artifact(visual: Mapping[str, Any]) -> VisualArtifactProof:
    return VisualArtifactProof(
        artifact_role=visual["artifact_role"],
        sha256=visual["sha256"],
        size_bytes=visual["size_bytes"],
        pixel_width=visual["pixel_width"],
        pixel_height=visual["pixel_height"],
        media_type=visual["media_type"],
    )


def _visual_proof(
    *,
    source_item_index: int,
    page_idx: int,
    visual: Mapping[str, Any],
    kind: str = "carrier_guard",
    verified_sha256: str | None = None,
) -> SourceEvidenceProof:
    artifact = _visual_artifact(visual)
    assert kind in {"carrier_guard", "occurrence_crop"}
    binding = VisualBindingProof(
        source_item_index=source_item_index,
        page_idx=page_idx,
        kind=kind,
        artifact=artifact,
    )
    return SourceEvidenceProof(
        identity=SourceProofIdentity(
            source_evidence_sha256="sha256:" + "b" * 64,
            source_pdf_sha256=_SOURCE_PDF_SHA256,
            page_count=1,
        ),
        pages=(SourcePageProof(page_idx=0, events=()),),
        retrieval_runs=(),
        visual_bindings=(binding,),
        verified_visuals=(
            VerifiedVisualArtifact(
                artifact_role=artifact.artifact_role,
                sha256=verified_sha256 or artifact.sha256,
            ),
        ),
    )


def _selector(
    order: int,
    *,
    char_span: tuple[int, int] | None = None,
) -> dict[str, Any]:
    field: dict[str, Any] = {"kind": "text"}
    if char_span is not None:
        field["char_span"] = list(char_span)
    return {"source": _source(order), "field": field}


def _unit(
    order_index: int,
    *,
    payload_source: int,
    payload_text: str,
    headings: list[tuple[int, str]] | None = None,
    quality_status: str = "ok",
) -> AuditUnitView:
    projected_headings = headings or []
    heading_path = [text for _, text in projected_headings]
    return AuditUnitView(
        order_index=order_index,
        payload_kind="text",
        payload={"text": payload_text},
        title=heading_path[-1] if heading_path else None,
        heading_path=heading_path,
        semantic_key="document_content",
        semantic_keys=["document_content"],
        quality_status=quality_status,
        applicability=None,
        artifact_locator={
            "source_projection": {
                "version": "unit-source-projection.v4",
                "payload": {
                    "kind": "text_identity",
                    "sources": [_selector(payload_source)],
                    "target_field": "payload.text",
                    "transform": "clean_text.v1",
                },
                "heading_path": [
                    {
                        "target_index": target,
                        "kind": "source_field",
                        "selector": _selector(
                            source_index,
                            char_span=(0, len(text)),
                        ),
                        "transform": "clean_text.v1",
                    }
                    for target, (source_index, text) in enumerate(projected_headings)
                ],
                "structured": [],
                "provenance": [],
                "search_targets": ["payload.text"],
                "search_atoms": [],
                "physical_context": None,
            },
        },
    )


def _mixed_part(
    *,
    payload_source: int,
    payload_text: str,
    headings: list[tuple[int, str]],
) -> dict[str, Any]:
    atomic = _unit(
        payload_source,
        payload_source=payload_source,
        payload_text=payload_text,
        headings=headings,
    )
    return {
        "kind": "text",
        "order": payload_source,
        "text": payload_text,
        "heading_path": list(atomic.heading_path),
        "artifact_locator": copy.deepcopy(atomic.artifact_locator),
    }


def _mixed_unit(
    parts: list[dict[str, Any]],
    *,
    heading_path: list[str],
    semantic_type: str = "section",
) -> AuditUnitView:
    part_locators = [copy.deepcopy(part["artifact_locator"]) for part in parts]
    first_graph = part_locators[0]["source_projection"]
    return AuditUnitView(
        order_index=1,
        payload_kind="mixed",
        payload={
            "semantic_type": semantic_type,
            "order_status": "unresolved_physical_fallback",
            "parts": copy.deepcopy(parts),
        },
        title=heading_path[-1] if heading_path else "审计样本",
        heading_path=list(heading_path),
        semantic_key="document_content",
        semantic_keys=["document_content"],
        quality_status="ok",
        applicability=None,
        artifact_locator={
            "source_projection": {
                "version": "unit-source-projection.v4",
                "payload": {
                    "kind": "container",
                    "sources": [],
                    "target_field": "payload.parts",
                    "transform": "ordered_parts.v1",
                },
                "heading_path": copy.deepcopy(
                    first_graph["heading_path"][: len(heading_path)]
                ),
                "structured": [],
                "provenance": [],
                "search_targets": [],
                "search_atoms": [],
                "physical_context": None,
            },
        },
    )


def _codes(report: Any) -> set[str]:
    return {finding.code for finding in report.findings}


class DocumentUnitAuditTests(unittest.TestCase):
    def test_mixed_container_is_an_ordered_single_occurrence_closure(self) -> None:
        elements = [
            _element(0, text="同一章节"),
            _element(1, text="事实甲"),
            _element(2, text="事实乙"),
        ]
        headings = [
            _heading(
                1,
                0,
                text="同一章节",
                section_span=(0, 2),
            )
        ]
        parts = [
            _mixed_part(
                payload_source=1,
                payload_text="事实甲",
                headings=[(0, "同一章节")],
            ),
            _mixed_part(
                payload_source=2,
                payload_text="事实乙",
                headings=[(0, "同一章节")],
            ),
        ]
        unit = _mixed_unit(parts, heading_path=["同一章节"])
        normalized = _ir(elements, headings=headings)

        accepted = audit_document(
            normalized_ir=normalized,
            units=[unit],
            metadata=self.metadata,
        )
        self.assertTrue(accepted.ok, accepted.as_dict())

        heading_elements = [
            _element(0, text="父节"),
            _element(1, text="空子节"),
        ]
        heading_proof = [
            _heading(1, 0, text="父节", section_span=(0, 1)),
            _heading(
                2,
                1,
                text="空子节",
                section_span=(1, 1),
                parent_node_id=1,
                heading_level=2,
            ),
        ]
        heading_part = _mixed_part(
            payload_source=1,
            payload_text="空子节",
            headings=[(0, "父节"), (1, "空子节")],
        )
        heading_part["artifact_locator"]["source_projection"]["payload"]["sources"][0][
            "field"
        ]["char_span"] = [0, len("空子节")]
        heading_only = audit_document(
            normalized_ir=_ir(heading_elements, headings=heading_proof),
            units=[
                _mixed_unit(
                    [heading_part],
                    heading_path=["父节"],
                )
            ],
            metadata=self.metadata,
        )
        self.assertTrue(heading_only.ok, heading_only.as_dict())

        unproved_part_fields = copy.deepcopy(unit)
        unproved_part_fields.payload["parts"][0]["title"] = "自报标题"
        unproved_part_fields.payload["parts"][1]["role"] = "heading"
        rejected_unproved_part_fields = audit_document(
            normalized_ir=normalized,
            units=[unproved_part_fields],
            metadata=self.metadata,
        )
        self.assertIn(
            "payload_field_unproven",
            _codes(rejected_unproved_part_fields),
        )

        for label in ("outer", "part"):
            with self.subTest(locator=label):
                mirrored_locator = copy.deepcopy(unit)
                if label == "outer":
                    assert mirrored_locator.artifact_locator is not None
                    locator = mirrored_locator.artifact_locator
                else:
                    locator = mirrored_locator.payload["parts"][0]["artifact_locator"]
                locator["order_index"] = 0
                rejected_locator_mirror = audit_document(
                    normalized_ir=normalized,
                    units=[mirrored_locator],
                    metadata=self.metadata,
                )
                self.assertIn(
                    "artifact_locator_field_unproven",
                    _codes(rejected_locator_mirror),
                )

        reordered = copy.deepcopy(unit)
        reordered.payload["parts"].reverse()
        rejected_order = audit_document(
            normalized_ir=normalized,
            units=[reordered],
            metadata=self.metadata,
        )
        self.assertIn(
            "mixed_container_source_order_invalid",
            _codes(rejected_order),
        )

        invalid_transform = copy.deepcopy(unit)
        assert invalid_transform.artifact_locator is not None
        invalid_transform.artifact_locator["source_projection"]["payload"][
            "transform"
        ] = "bogus.v1"
        rejected_transform = audit_document(
            normalized_ir=normalized,
            units=[invalid_transform],
            metadata=self.metadata,
        )
        self.assertIn(
            "payload_projection_mismatch",
            _codes(rejected_transform),
        )

        open_projection = copy.deepcopy(unit)
        assert open_projection.artifact_locator is not None
        open_projection.artifact_locator["source_projection"]["payload"]["extra"] = (
            "unproved"
        )
        rejected_open_projection = audit_document(
            normalized_ir=normalized,
            units=[open_projection],
            metadata=self.metadata,
        )
        self.assertIn(
            "payload_projection_contract_invalid",
            _codes(rejected_open_projection),
        )

        forged_search_target = copy.deepcopy(unit)
        forged_search_target.payload["parts"][0]["context"] = "伪上下文"
        forged_search_target.payload["parts"][0]["artifact_locator"][
            "source_projection"
        ]["search_targets"][0] = "payload.context"
        rejected_search_target = audit_document(
            normalized_ir=normalized,
            units=[forged_search_target],
            metadata=self.metadata,
        )
        self.assertIn(
            "search_target_contract_invalid",
            _codes(rejected_search_target),
        )

        invalid_part = copy.deepcopy(unit)
        invalid_part.payload["parts"].append("not-an-object")
        rejected_part = audit_document(
            normalized_ir=normalized,
            units=[invalid_part],
            metadata=self.metadata,
        )
        self.assertIn("mixed_part_invalid", _codes(rejected_part))

        boolean_order = copy.deepcopy(unit)
        boolean_order.payload["parts"][0]["order"] = True
        rejected_boolean_order = audit_document(
            normalized_ir=normalized,
            units=[boolean_order],
            metadata=self.metadata,
        )
        self.assertIn("mixed_part_order_invalid", _codes(rejected_boolean_order))

        invalid_shapes = (
            (
                "nested mixed",
                {"kind": "mixed", "semantic_type": "section", "parts": []},
                "mixed_part_kind_invalid",
            ),
            (
                "quality enum",
                {"quality_status": "banana"},
                "mixed_part_quality_status_invalid",
            ),
            (
                "heading path scalar",
                {"heading_path": "同一章节"},
                "mixed_part_heading_path_invalid",
            ),
            (
                "heading path coercion",
                {"heading_path": [123]},
                "mixed_part_heading_path_invalid",
            ),
            (
                "applicability enum",
                {"applicability": False},
                "mixed_part_applicability_invalid",
            ),
        )
        for label, mutation, code in invalid_shapes:
            with self.subTest(label=label):
                invalid_shape = copy.deepcopy(unit)
                invalid_shape.payload["parts"][0].update(mutation)
                rejected_shape = audit_document(
                    normalized_ir=normalized,
                    units=[invalid_shape],
                    metadata=self.metadata,
                )
                self.assertIn(code, _codes(rejected_shape))

        cross_elements = [
            _element(0, text="章节甲"),
            _element(1, text="甲事实"),
            _element(2, text="章节乙"),
            _element(3, text="乙事实"),
        ]
        cross_headings = [
            _heading(1, 0, text="章节甲", section_span=(0, 1)),
            _heading(2, 2, text="章节乙", section_span=(2, 3)),
        ]
        cross_unit = _mixed_unit(
            [
                _mixed_part(
                    payload_source=1,
                    payload_text="甲事实",
                    headings=[(0, "章节甲")],
                ),
                _mixed_part(
                    payload_source=3,
                    payload_text="乙事实",
                    headings=[(2, "章节乙")],
                ),
            ],
            heading_path=["章节甲"],
        )
        rejected_scope = audit_document(
            normalized_ir=_ir(cross_elements, headings=cross_headings),
            units=[cross_unit],
            metadata=self.metadata,
        )
        self.assertIn(
            "mixed_part_structural_scope_invalid",
            _codes(rejected_scope),
        )

        forged_part_metadata = copy.deepcopy(cross_unit.payload["parts"])
        forged_part_metadata[0]["role"] = False
        forged_roles = audit_document(
            normalized_ir=_ir(cross_elements, headings=cross_headings),
            units=[
                _mixed_unit(
                    forged_part_metadata,
                    heading_path=[],
                    semantic_type="document",
                )
            ],
            metadata=self.metadata,
        )
        self.assertIn(
            "payload_field_unproven",
            _codes(forged_roles),
        )

        atomic_cross_scope = _unit(
            1,
            payload_source=1,
            payload_text="甲事实\n乙事实",
        )
        assert atomic_cross_scope.artifact_locator is not None
        atomic_cross_scope.artifact_locator["source_projection"]["payload"] = {
            "kind": "text_concat",
            "sources": [_selector(1), _selector(3)],
            "target_field": "payload.text",
            "transform": "ordered_text_concat.v1",
        }
        rejected_atomic = audit_document(
            normalized_ir=_ir(cross_elements, headings=cross_headings),
            units=[atomic_cross_scope],
            metadata=self.metadata,
        )
        self.assertIn(
            "structure_proof_section_mixed",
            _codes(rejected_atomic),
        )

        furniture_part = _mixed_part(
            payload_source=0,
            payload_text="未证明页框",
            headings=[],
        )
        furniture_only = audit_document(
            normalized_ir=_ir(
                [
                    _element(
                        0,
                        text="未证明页框",
                        kind="page_furniture",
                        raw_kind="header",
                    )
                ]
            ),
            units=[
                _mixed_unit(
                    [furniture_part],
                    heading_path=[],
                    semantic_type="document",
                )
            ],
            metadata=self.metadata,
        )
        self.assertIn(
            "mixed_container_payload_missing",
            _codes(furniture_only),
        )

    def test_table_media_occurrence_and_bytes_are_both_audited(self) -> None:
        outer_hash = "1" * 64
        media_hash = "2" * 64
        table = {
            "headers": [],
            "rows": [["值"]],
            "cells": [
                {
                    "row": 0,
                    "col": 0,
                    "rowspan": 1,
                    "colspan": 1,
                    "text": "值",
                    "is_header": False,
                }
            ],
            "embedded_media": [
                {
                    "occurrence_index": 0,
                    "cell_media_index": 0,
                    "row": 0,
                    "col": 0,
                    "rowspan": 1,
                    "colspan": 1,
                    "image_path": "images/cell.png",
                    "artifact_role": "evidence_table_media_000000_000000",
                }
            ],
        }
        normalized = _ir(
            [
                _element(
                    0,
                    text="",
                    kind="table",
                    raw_kind="table",
                    image_path="images/table.png",
                    table_html=(
                        '<table><tr><td>值<img src="images/cell.png"/>'
                        "</td></tr></table>"
                    ),
                    table=table,
                    table_caption=[],
                    table_footnote=[],
                )
            ]
        )
        files = normalized["parser_artifacts"]["files"]
        files["evidence_image_000000"] = {
            "availability": "present",
            "relpath": "parser/audit/images/table.png",
            "sha256": "sha256:" + outer_hash,
            "size_bytes": 10,
        }
        files["evidence_table_media_000000_000000"] = {
            "availability": "present",
            "relpath": "parser/audit/images/cell.png",
            "sha256": "sha256:" + media_hash,
            "size_bytes": 11,
        }
        payload = {
            "caption": [],
            "unit": None,
            "headers": [],
            "rows": [["值"]],
            "cells": table["cells"],
            "embedded_media": [
                {
                    "occurrence_index": 0,
                    "cell_media_index": 0,
                    "row": 0,
                    "col": 0,
                    "rowspan": 1,
                    "colspan": 1,
                    "image_ref": f"images/{media_hash}.png",
                }
            ],
            "merged_cells": [],
            "notes": [],
        }
        locator = {
            "evidence_artifacts": [
                {
                    "artifact_role": "evidence_image_000000",
                    "sha256": "sha256:" + outer_hash,
                    "size_bytes": 10,
                    "media_type": "image/png",
                },
                {
                    "artifact_role": "evidence_table_media_000000_000000",
                    "sha256": "sha256:" + media_hash,
                    "size_bytes": 11,
                    "media_type": "image/png",
                },
            ],
            "source_projection": {
                "version": "unit-source-projection.v4",
                "payload": {
                    "kind": "table_identity",
                    "sources": [
                        {
                            "source": _source(0),
                            "field": {"kind": "table"},
                        }
                    ],
                    "target_field": "payload",
                    "transform": "table_identity.v1",
                },
                "heading_path": [],
                "structured": [],
                "provenance": [],
                "search_targets": [
                    "payload.caption",
                    "payload.headers",
                    "payload.rows",
                    "payload.notes",
                ],
                "search_atoms": [],
                "physical_context": None,
            },
        }
        unit = AuditUnitView(
            order_index=1,
            payload_kind="table",
            payload=payload,
            title=None,
            heading_path=[],
            semantic_key="document_content",
            semantic_keys=["document_content"],
            quality_status="ok",
            applicability=None,
            artifact_locator=locator,
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[unit],
            metadata=self.metadata,
            image_hashes={
                "evidence_image_000000": outer_hash,
                "evidence_table_media_000000_000000": media_hash,
            },
        )
        self.assertTrue(report.ok, report.as_dict())

        broken = copy.deepcopy(payload)
        broken["embedded_media"] = []
        rejected = audit_document(
            normalized_ir=normalized,
            units=[replace(unit, payload=broken)],
            metadata=self.metadata,
            image_hashes={
                "evidence_image_000000": outer_hash,
                "evidence_table_media_000000_000000": media_hash,
            },
        )
        self.assertIn("table_projection_mismatch", _codes(rejected))

    def setUp(self) -> None:
        self.metadata = AuditDocumentMetadata(
            document_id="doc_audit",
            title="审计样本",
            filing_type="other",
        )

    def test_visual_source_fallback_and_persisted_bytes_close_together(self) -> None:
        visual = {
            "artifact_role": "source_page_visual_000001",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 321,
            "pixel_width": 100,
            "pixel_height": 200,
            "media_type": "image/png",
        }
        ordinary = _unit(
            1,
            payload_source=0,
            payload_text="OCR正文",
        )
        ordinary = replace(
            ordinary,
            artifact_locator={
                **(ordinary.artifact_locator or {}),
                "evidence_artifacts": [visual],
            },
        )
        report = audit_document(
            normalized_ir=_ir([_element(0, text="OCR正文")]),
            units=[ordinary],
            metadata=self.metadata,
            source_proof=_visual_proof(
                source_item_index=0,
                page_idx=0,
                visual=visual,
            ),
        )

        self.assertTrue(report.ok, [item.as_dict() for item in report.findings])

    def test_coarse_source_evidence_unit_cannot_close_a_source_gap(self) -> None:
        fallback = replace(
            _unit(1, payload_source=0, payload_text="MinerU遗漏但PDF可见"),
            artifact_locator={
                "artifact_role": "source_evidence",
                "page_idx": 0,
                "page_no": 1,
            },
        )

        report = audit_document(
            normalized_ir=_ir([_element(0, text="MinerU正文")]),
            units=[fallback],
            metadata=self.metadata,
        )

        self.assertIn("source_projection_missing", _codes(report))
        self.assertIn("output_source_closure_missing", _codes(report))

    def test_native_atom_outside_every_retrieval_run_is_reported(self) -> None:
        text = "PDF可见但未成组"
        text_sha256 = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        proof = SourceEvidenceProof(
            identity=SourceProofIdentity(
                source_evidence_sha256="sha256:" + "b" * 64,
                source_pdf_sha256=_SOURCE_PDF_SHA256,
                page_count=1,
            ),
            pages=(
                SourcePageProof(
                    page_idx=0,
                    events=(
                        NativeTextEvent(
                            atom_index=0,
                            word_order=0,
                            text=text,
                            text_sha256=text_sha256,
                            bbox=(0.0, 0.0, 100.0, 20.0),
                            char_span=(0, len(text)),
                            layout_path=(0, 0, 0, 0),
                        ),
                    ),
                ),
            ),
            retrieval_runs=(
                RetrievalRunProof(
                    page_idx=0,
                    run_index=0,
                    atom_indices=(0,),
                    text_sha256=text_sha256,
                ),
            ),
            visual_bindings=(),
            verified_visuals=(),
        )
        # The proof type closes retrieval runs over every native atom, so the
        # audit-owned guard is only reachable by forging that closed state.
        object.__setattr__(proof, "retrieval_runs", ())

        report = audit_document(
            normalized_ir=_ir([_element(0, text="MinerU正文")]),
            units=[_unit(1, payload_source=0, payload_text="MinerU正文")],
            metadata=self.metadata,
            source_proof=proof,
        )

        self.assertIn("source_evidence_projection_invalid", _codes(report))
        self.assertIn(
            "native text atom 0 belongs to no retrieval run",
            "\n".join(finding.message for finding in report.findings),
        )

    def test_legacy_coarse_owner_support_receipt_is_rejected(self) -> None:
        owner_text = "权威载体数值50"
        residual = "50"

        def digest(value: str) -> str:
            return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

        proof = SourceEvidenceProof(
            identity=SourceProofIdentity(
                source_evidence_sha256="sha256:" + "b" * 64,
                source_pdf_sha256=_SOURCE_PDF_SHA256,
                page_count=1,
            ),
            pages=(
                SourcePageProof(
                    page_idx=0,
                    events=(
                        MappedSourceEvent(
                            atom_index=0,
                            word_order=0,
                            source_item_index=0,
                            order_state="monotonic",
                            selector_field="text",
                            selector_index=None,
                            selector_char_span=(0, len(owner_text)),
                            selector_value_sha256=digest(owner_text),
                            carrier_order=0,
                            carrier_bbox=(0.0, 0.0, 100.0, 20.0),
                            atom_bbox=(0.0, 0.0, 10.0, 10.0),
                            native_layout_path=(0, 0, 0, 0),
                        ),
                        NativeTextEvent(
                            atom_index=1,
                            word_order=1,
                            text=residual,
                            text_sha256=digest(residual),
                            bbox=(10.0, 0.0, 20.0, 10.0),
                            char_span=(0, len(residual)),
                            layout_path=(0, 1, 0, 0),
                        ),
                        MappedSourceEvent(
                            atom_index=2,
                            word_order=2,
                            source_item_index=0,
                            order_state="monotonic",
                            selector_field="text",
                            selector_index=None,
                            selector_char_span=(0, len(owner_text)),
                            selector_value_sha256=digest(owner_text),
                            carrier_order=0,
                            carrier_bbox=(0.0, 0.0, 100.0, 20.0),
                            atom_bbox=(20.0, 0.0, 30.0, 10.0),
                            native_layout_path=(0, 0, 0, 2),
                        ),
                    ),
                ),
            ),
            retrieval_runs=(
                RetrievalRunProof(
                    page_idx=0,
                    run_index=0,
                    atom_indices=(1,),
                    text_sha256=digest(residual),
                ),
            ),
            visual_bindings=(),
            verified_visuals=(),
        )
        source = {
            "kind": "source_evidence_atom",
            "source_evidence_sha256": "sha256:" + "b" * 64,
            "source_pdf_sha256": _SOURCE_PDF_SHA256,
            "page_idx": 0,
            "page_no": 1,
            "atom_index": 1,
            "atom_order": 1,
            "bbox": [10.0, 0.0, 20.0, 10.0],
            "char_span": [0, len(residual)],
            "text_sha256": digest(residual),
        }
        disposition = {
            "role": "support",
            "reason": "coarse_owner_exact_text_coverage",
            "source": source,
            "owner_source_item_index": 0,
            "comparison_algorithm": "source-owner-exact-substring.v1",
        }
        normalized = _ir([_element(0, text=owner_text)])
        unit = _unit(1, payload_source=0, payload_text=owner_text)

        report = audit_document(
            normalized_ir=normalized,
            units=[unit],
            metadata=self.metadata,
            source_proof=proof,
            source_dispositions=[disposition],
        )

        self.assertFalse(report.ok)
        self.assertIn("source_disposition_identity_unresolved", _codes(report))
        self.assertIn("source_atom_uncovered", _codes(report))

    def test_visual_source_fallback_requires_verified_bytes_and_unit_binding(
        self,
    ) -> None:
        visual = {
            "artifact_role": "source_page_visual_000001",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 321,
            "pixel_width": 100,
            "pixel_height": 200,
            "media_type": "image/png",
        }
        ordinary = _unit(
            1,
            payload_source=0,
            payload_text="OCR正文",
        )
        with self.assertRaises(SourceEvidenceProofError):
            _visual_proof(
                source_item_index=0,
                page_idx=0,
                visual=visual,
                verified_sha256="sha256:" + "f" * 64,
            )

        report = audit_document(
            normalized_ir=_ir([_element(0, text="OCR正文")]),
            units=[ordinary],
            metadata=self.metadata,
            source_proof=_visual_proof(
                source_item_index=0,
                page_idx=0,
                visual=visual,
            ),
        )

        self.assertIn("source_visual_evidence_missing", _codes(report))

    def test_evidence_artifact_shape_and_ownership_fail_closed(self) -> None:
        ordinary = _unit(1, payload_source=0, payload_text="正文")
        invalid_shape = replace(
            ordinary,
            artifact_locator={
                **(ordinary.artifact_locator or {}),
                "evidence_artifacts": None,
            },
        )
        invalid_report = audit_document(
            normalized_ir=_ir([_element(0, text="正文")]),
            units=[invalid_shape],
            metadata=self.metadata,
        )
        self.assertIn("evidence_artifacts_invalid", _codes(invalid_report))

        unowned = replace(
            ordinary,
            artifact_locator={
                **(ordinary.artifact_locator or {}),
                "evidence_artifacts": [
                    {
                        "artifact_role": "unowned_visual",
                        "sha256": "sha256:" + "d" * 64,
                        "size_bytes": 10,
                        "media_type": "image/png",
                    }
                ],
            },
        )
        unowned_report = audit_document(
            normalized_ir=_ir([_element(0, text="正文")]),
            units=[unowned],
            metadata=self.metadata,
        )
        self.assertIn("evidence_artifact_unowned", _codes(unowned_report))

    def test_visual_binding_must_resolve_to_the_bound_source_page(self) -> None:
        visual = {
            "artifact_role": "source_bbox_visual_000008_000001",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 321,
            "pixel_width": 100,
            "pixel_height": 50,
            "media_type": "image/png",
        }
        report = audit_document(
            normalized_ir=_ir([_element(0, text="正文")]),
            units=[_unit(1, payload_source=0, payload_text="正文")],
            metadata=self.metadata,
            source_proof=_visual_proof(
                source_item_index=7,
                page_idx=0,
                visual=visual,
            ),
        )
        self.assertIn("source_visual_binding_invalid", _codes(report))

    def test_bbox_visual_is_bound_only_to_its_source_carrier(self) -> None:
        visual = {
            "artifact_role": "source_bbox_visual_000001_000001",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 321,
            "pixel_width": 100,
            "pixel_height": 50,
            "media_type": "image/png",
        }
        source_proof = _visual_proof(
            source_item_index=0,
            page_idx=0,
            visual=visual,
        )
        first = _unit(1, payload_source=0, payload_text="甲事实。")
        bound = replace(
            first,
            artifact_locator={
                **(first.artifact_locator or {}),
                "evidence_artifacts": [visual],
            },
        )
        positive = audit_document(
            normalized_ir=_ir([_element(0, text="甲事实。")]),
            units=[bound],
            metadata=self.metadata,
            source_proof=source_proof,
        )
        self.assertTrue(positive.ok, positive.findings)

        missing = audit_document(
            normalized_ir=_ir([_element(0, text="甲事实。")]),
            units=[first],
            metadata=self.metadata,
            source_proof=source_proof,
        )
        self.assertIn("source_visual_evidence_missing", _codes(missing))

        second = _unit(2, payload_source=1, payload_text="乙事实。")
        misbound = replace(
            second,
            artifact_locator={
                **(second.artifact_locator or {}),
                "evidence_artifacts": [visual],
            },
        )
        forged = audit_document(
            normalized_ir=_ir(
                [
                    _element(0, text="甲事实。"),
                    _element(1, text="乙事实。"),
                ]
            ),
            units=[bound, misbound],
            metadata=self.metadata,
            source_proof=source_proof,
        )
        self.assertIn("source_visual_evidence_misbound", _codes(forged))

    def test_visual_occurrence_is_hash_bound_to_outer_unit_and_exact_part(
        self,
    ) -> None:
        occurrence = {
            "artifact_role": "source_visual_occurrence_000001",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 321,
            "pixel_width": 100,
            "pixel_height": 50,
            "media_type": "image/png",
        }
        elements = [
            _element(0, text="同一章节"),
            _element(1, text="事实甲"),
            _element(2, text="事实乙"),
        ]
        headings = [_heading(1, 0, text="同一章节", section_span=(0, 2))]
        parts = [
            _mixed_part(
                payload_source=1,
                payload_text="事实甲",
                headings=[(0, "同一章节")],
            ),
            _mixed_part(
                payload_source=2,
                payload_text="事实乙",
                headings=[(0, "同一章节")],
            ),
        ]
        parts[0]["artifact_locator"]["evidence_artifacts"] = [occurrence]
        unit = _mixed_unit(parts, heading_path=["同一章节"])
        unit = replace(
            unit,
            artifact_locator={
                **(unit.artifact_locator or {}),
                "evidence_artifacts": [occurrence],
            },
        )
        kwargs = {
            "normalized_ir": _ir(elements, headings=headings),
            "metadata": self.metadata,
            "source_proof": _visual_proof(
                source_item_index=1,
                page_idx=0,
                visual=occurrence,
                kind="occurrence_crop",
            ),
        }

        accepted = audit_document(units=[unit], **kwargs)
        self.assertTrue(accepted.ok, accepted.findings)

        missing_part = copy.deepcopy(unit)
        missing_part.payload["parts"][0]["artifact_locator"]["evidence_artifacts"] = []
        rejected = audit_document(units=[missing_part], **kwargs)
        self.assertIn("source_visual_evidence_missing", _codes(rejected))

        bad_artifact = copy.deepcopy(unit)
        bad_artifact.artifact_locator["evidence_artifacts"][0]["sha256"] = (
            "sha256:" + "f" * 64
        )
        bad_hash = audit_document(units=[bad_artifact], **kwargs)
        self.assertIn("source_visual_evidence_missing", _codes(bad_hash))

    def test_explicit_dag_path_and_source_spans_are_accepted(self) -> None:
        elements = [
            _element(0, text="甲部"),
            _element(1, text="甲节"),
            _element(2, text="完整来源事实。"),
        ]
        normalized = _ir(
            elements,
            headings=[
                _heading(1, 0, text="甲部", section_span=(0, 2)),
                _heading(
                    2,
                    1,
                    text="甲节",
                    section_span=(1, 2),
                    parent_node_id=1,
                    heading_level=2,
                ),
            ],
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[
                _unit(
                    1,
                    payload_source=2,
                    payload_text="完整来源事实。",
                    headings=[(0, "甲部"), (1, "甲节")],
                )
            ],
            metadata=self.metadata,
        )

        self.assertTrue(report.ok, report.findings)
        self.assertEqual(report.metrics["coverage"]["payload"], 1)
        self.assertEqual(report.metrics["coverage"]["structure"], 2)

    def test_registered_document_title_is_valid_without_heading_path(self) -> None:
        normalized = _ir([_element(0, text="完整来源事实。")])
        unit = _unit(
            1,
            payload_source=0,
            payload_text="完整来源事实。",
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[replace(unit, title="审计样本")],
            metadata=self.metadata,
        )

        self.assertTrue(report.ok, report.findings)

    def test_anchor_only_heading_does_not_own_its_payload_path(self) -> None:
        normalized = _ir(
            [_element(0, text="仅作结构锚点")],
            headings=[
                _heading(
                    1,
                    0,
                    text="仅作结构锚点",
                    section_span=(0, 0),
                    propagates=False,
                )
            ],
        )
        unit = _unit(
            1,
            payload_source=0,
            payload_text="仅作结构锚点",
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[replace(unit, title="审计样本")],
            metadata=self.metadata,
        )

        self.assertTrue(report.ok, report.findings)

    def test_anchor_only_heading_cannot_be_projected_as_payload_path(self) -> None:
        normalized = _ir(
            [_element(0, text="仅作结构锚点")],
            headings=[
                _heading(
                    1,
                    0,
                    text="仅作结构锚点",
                    section_span=(0, 0),
                    propagates=False,
                )
            ],
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[
                _unit(
                    1,
                    payload_source=0,
                    payload_text="仅作结构锚点",
                    headings=[(0, "仅作结构锚点")],
                )
            ],
            metadata=self.metadata,
        )

        self.assertIn("structure_proof_path_mismatch", _codes(report))

    def test_anchor_only_payload_inherits_propagating_parent(self) -> None:
        normalized = _ir(
            [
                _element(0, text="已证明父节"),
                _element(1, text="仅作结构锚点"),
            ],
            headings=[
                _heading(
                    1,
                    0,
                    text="已证明父节",
                    section_span=(0, 1),
                ),
                _heading(
                    2,
                    1,
                    text="仅作结构锚点",
                    section_span=(1, 1),
                    propagates=False,
                ),
            ],
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[
                _unit(
                    1,
                    payload_source=1,
                    payload_text="仅作结构锚点",
                    headings=[(0, "已证明父节")],
                )
            ],
            metadata=self.metadata,
        )

        self.assertTrue(report.ok, report.findings)

    def test_repeated_title_cannot_swap_section_identity(self) -> None:
        elements = [
            _element(0, text="风险提示"),
            _element(1, text="甲事实。"),
            _element(2, text="风险提示"),
            _element(3, text="乙事实。"),
        ]
        normalized = _ir(
            elements,
            headings=[
                _heading(1, 0, text="风险提示", section_span=(0, 1)),
                _heading(2, 2, text="风险提示", section_span=(2, 3)),
            ],
        )
        units = [
            _unit(
                1,
                payload_source=1,
                payload_text="甲事实。",
                headings=[(0, "风险提示")],
            ),
            _unit(
                2,
                payload_source=3,
                payload_text="乙事实。",
                headings=[(2, "风险提示")],
            ),
        ]
        locator = copy.deepcopy(units[1].artifact_locator)
        assert locator is not None
        locator["source_projection"]["heading_path"] = copy.deepcopy(
            units[0].artifact_locator["source_projection"]["heading_path"]  # type: ignore[index]
        )
        forged = replace(units[1], artifact_locator=locator)

        report = audit_document(
            normalized_ir=normalized,
            units=[units[0], forged],
            metadata=self.metadata,
        )

        self.assertIn("structure_proof_source_mismatch", _codes(report))

    def test_parent_path_cannot_be_dropped_or_reparented(self) -> None:
        elements = [
            _element(0, text="第一章"),
            _element(1, text="第一节"),
            _element(2, text="事实。"),
        ]
        normalized = _ir(
            elements,
            headings=[
                _heading(1, 0, text="第一章", section_span=(0, 2)),
                _heading(
                    2,
                    1,
                    text="第一节",
                    section_span=(1, 2),
                    parent_node_id=1,
                    heading_level=2,
                ),
            ],
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[
                _unit(
                    1,
                    payload_source=2,
                    payload_text="事实。",
                    headings=[(1, "第一节")],
                )
            ],
            metadata=self.metadata,
        )

        self.assertIn("structure_proof_path_mismatch", _codes(report))

    def test_unproved_carriers_never_become_headings(self) -> None:
        cases = {
            "numbered": _element(0, text="1. 未证明编号", heading_level=1),
            "parser_level": _element(
                0,
                text="未证明 parser 标题",
                text_level=1,
            ),
            "toc": _element(0, text="第一章................1", heading_level=1),
            "table_note": _element(
                0,
                text="注：本表金额单位为元",
                heading_level=1,
                bbox=[100, 500, 400, 520],
            ),
            "page_frame": _element(
                0,
                text="公司简称 2025 年年度报告",
                kind="page_furniture",
                raw_kind="header",
            ),
        }
        for label, candidate in cases.items():
            with self.subTest(label=label):
                body = _element(1, text="正文事实。")
                normalized = _ir([candidate, body])
                report = audit_document(
                    normalized_ir=normalized,
                    units=[
                        _unit(
                            1,
                            payload_source=1,
                            payload_text="正文事实。",
                            headings=[(0, str(candidate["text"]))],
                        )
                    ],
                    metadata=self.metadata,
                )
                self.assertIn("structure_proof_path_mismatch", _codes(report))

    def test_page_frame_disposition_requires_explicit_proof_membership(self) -> None:
        frame = _element(
            0,
            text="1",
            kind="page_furniture",
            raw_kind="page_number",
        )
        disposition = {
            **_source(0),
            "role": "external_metadata",
            "reason": "proven_running_furniture",
        }
        proved = _ir(
            [frame],
            page_frames=[
                {
                    "group_id": "frame-1",
                    "member_source_item_indices": [0],
                    "proof_kind": "native_artifact",
                    "representative_source_item_index": 0,
                    "role": "running_furniture",
                }
            ],
        )

        valid = audit_document(
            normalized_ir=proved,
            units=[],
            metadata=self.metadata,
            source_dispositions=[disposition],
        )
        invalid = audit_document(
            normalized_ir=_ir([frame]),
            units=[],
            metadata=self.metadata,
            source_dispositions=[disposition],
        )
        self.assertNotIn("source_disposition_proof_invalid", _codes(valid))
        self.assertNotIn("source_atom_uncovered", _codes(valid))
        self.assertIn("source_disposition_proof_invalid", _codes(invalid))

    def test_payload_projection_preserves_exact_content(self) -> None:
        normalized = _ir([_element(0, text="ABC 事实\n下一行")])
        unit = _unit(
            1,
            payload_source=0,
            payload_text="ABC 事实\n下一行",
        )

        for label, forged_text in (
            ("case", "abc 事实\n下一行"),
            ("space", "ABC事实\n下一行"),
            ("newline", "ABC 事实 下一行"),
        ):
            with self.subTest(label=label):
                report = audit_document(
                    normalized_ir=normalized,
                    units=[replace(unit, payload={"text": forged_text})],
                    metadata=self.metadata,
                )
                self.assertIn("payload_projection_mismatch", _codes(report))

    def test_missing_projection_cannot_cover_source_content(self) -> None:
        normalized = _ir([_element(0, text="完整来源事实。")])
        unit = replace(
            _unit(
                1,
                payload_source=0,
                payload_text="完整来源事实。",
            ),
            artifact_locator=_source(0),
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[unit],
            metadata=self.metadata,
        )

        self.assertIn("source_projection_missing", _codes(report))
        self.assertIn("source_atom_uncovered", _codes(report))

    def test_heading_selector_must_match_proved_text_span(self) -> None:
        elements = [
            _element(0, text="前缀甲部"),
            _element(1, text="事实。"),
        ]
        proof_heading = _heading(
            1,
            0,
            text="前缀甲部",
            section_span=(0, 1),
        )
        proof_heading["source_refs"][0]["text_span"] = [2, 4]
        normalized = _ir(elements, headings=[proof_heading])
        unit = _unit(
            1,
            payload_source=1,
            payload_text="事实。",
            headings=[(0, "甲部")],
        )
        locator = copy.deepcopy(unit.artifact_locator)
        assert locator is not None
        locator["source_projection"]["heading_path"][0]["selector"]["field"][
            "char_span"
        ] = [0, 4]

        report = audit_document(
            normalized_ir=normalized,
            units=[replace(unit, artifact_locator=locator)],
            metadata=self.metadata,
        )

        self.assertIn("structure_proof_source_mismatch", _codes(report))

    def test_caption_cannot_be_used_as_structure_heading_source(self) -> None:
        caption = "显式结构标题"
        elements = [
            _element(
                0,
                text="",
                kind="table",
                raw_kind="table",
                table={
                    "headers": [caption],
                    "rows": [],
                    "cells": [
                        {
                            "row": 0,
                            "col": 0,
                            "rowspan": 1,
                            "colspan": 1,
                            "text": caption,
                            "is_header": True,
                        }
                    ],
                    "embedded_media": [],
                    "merged_cells": [],
                },
                table_caption=[caption],
                table_footnote=[],
                table_html="",
            ),
            _element(1, text="事实。"),
        ]
        heading = _heading(
            1,
            0,
            text=caption,
            section_span=(0, 1),
        )
        heading["source_refs"][0].update({"field": "table_caption", "index": 0})
        normalized = _ir(elements, headings=[heading])
        unit = _unit(
            1,
            payload_source=1,
            payload_text="事实。",
            headings=[(0, caption)],
        )
        locator = copy.deepcopy(unit.artifact_locator)
        assert locator is not None
        field = locator["source_projection"]["heading_path"][0]["selector"]["field"]
        field.update({"kind": "table_caption", "index": 0})

        report = audit_document(
            normalized_ir=normalized,
            units=[replace(unit, artifact_locator=locator)],
            metadata=self.metadata,
        )

        self.assertFalse(report.ok)
        self.assertIn("structure_proof_invalid", _codes(report))

    def test_source_quality_and_unit_order_audits_remain_active(self) -> None:
        elements = [
            _element(0, text="甲事实。"),
            _element(1, text="乙事实。", kind="unknown", raw_kind="unknown"),
        ]
        normalized = _ir(elements)
        units = [
            _unit(
                1,
                payload_source=1,
                payload_text="乙事实。",
                quality_status="ok",
            ),
            _unit(2, payload_source=0, payload_text="甲事实。"),
        ]

        report = audit_document(
            normalized_ir=normalized,
            units=units,
            metadata=self.metadata,
        )

        self.assertIn("quality_status_understated", _codes(report))
        self.assertIn("unit_source_order_invalid", _codes(report))


class OwnerScopeFlattenIndexTests(unittest.TestCase):
    """Audit-side flatten mapping stays a pure DAG recomputation."""

    @staticmethod
    def _heading(node_id: int, parent: int | None) -> _ProofHeading:
        return _ProofHeading(
            node_id=node_id,
            parent_node_id=parent,
            propagates=True,
            section_start=node_id,
            section_end=99,
            title=f"节{node_id}",
            source_refs=(),
        )

    @staticmethod
    def _scope_break(
        *,
        policy: str,
        flatten_root: int | None,
        target: int | None,
    ) -> _ProofOwnerScopeBreak:
        return _ProofOwnerScopeBreak(
            boundary_source_item_index=5,
            boundary_ref="ir_0005",
            boundary_field="table_caption",
            boundary_index=0,
            boundary_text_span=(0, 5),
            boundary_value_sha256="sha256:" + "b" * 64,
            page_index=0,
            eligibility_basis="numbered_caption_native_break",
            relative_rank="higher",
            current_owner_node_id=3,
            target_node_id=target,
            boundary_carrier_scope="selected_and_same_carrier",
            source_atom_orders=(9,),
            materialization_policy=policy,
            flatten_subtree_root_node_id=flatten_root,
        )

    def test_flatten_maps_the_whole_subtree_to_its_target(self) -> None:
        headings = {
            1: self._heading(1, None),
            2: self._heading(2, 1),
            3: self._heading(3, 2),
            4: self._heading(4, 3),
            5: self._heading(5, 1),
        }

        targets = _flattened_node_targets(
            (
                self._scope_break(
                    policy="flatten_intervening_subtree",
                    flatten_root=2,
                    target=1,
                ),
            ),
            headings=headings,
        )

        self.assertEqual(targets, {2: 1, 3: 1, 4: 1})

    def test_direct_target_breaks_map_nothing(self) -> None:
        headings = {
            1: self._heading(1, None),
            2: self._heading(2, 1),
            3: self._heading(3, 2),
        }

        targets = _flattened_node_targets(
            (
                self._scope_break(
                    policy="direct_target",
                    flatten_root=None,
                    target=1,
                ),
            ),
            headings=headings,
        )

        self.assertEqual(targets, {})


if __name__ == "__main__":
    unittest.main()
