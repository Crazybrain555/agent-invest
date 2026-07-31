from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any

from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextAtom,
    NativeTextGeometryIssue,
    NativeTextLayoutRef,
    NativeTextPage,
)
from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NATIVE_PDF_STRUCTURE_VERSION,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    comparison_text,
    ExtractorIdentity,
    SourceEvidenceContractError,
    carrier_source_support_index,
    iter_mineru_text_carriers,
    reconcile_source_evidence,
    required_carrier_visual_regions,
    resolve_ir_text_selector,
    source_visual_artifact_descriptors,
    validate_mapped_element_bindings,
    validate_source_evidence_ledger,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence_validator import (
    source_evidence_proof_from_validated_ledger,
)
from disclosure_anchor.adapters.parsers.mineru.structure_proof import (
    build_mineru_structure_proof,
)
from disclosure_anchor.application.contracts.source_evidence import (
    MappedSourceEvent,
    NativeTextEvent,
    SourceEvidenceProof,
)
from disclosure_anchor.adapters.parsers.mineru.text_projection import (
    build_mineru_text_projections,
)
from disclosure_anchor.adapters.parsers.pdf_visual_evidence import (
    PNG_OPTIONS,
    RENDERER_IDENTITY,
    RENDER_OPTIONS,
    VisualPageEvidence,
)
from disclosure_anchor.domain.errors import ParserOutputContractError

from tests.unit._native_index import native_index

TYPED_ARTIFACT_SHA256 = "sha256:" + "c" * 64
SOURCE_EVIDENCE_SHA256 = "sha256:" + "b" * 64


def _artifact(items: list[dict[str, object]]) -> tuple[bytes, str]:
    payload = json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()
    return payload, "sha256:" + hashlib.sha256(payload).hexdigest()


def _typed_args(
    items: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "canonical_content_list": items,
        "expected_mineru_typed_artifact_sha256": TYPED_ARTIFACT_SHA256,
        "native_structure": _native_structure(),
    }


def _native_structure(
    *,
    page_count: int = 1,
    cells: tuple[tuple[int, str, tuple[float, float, float, float]], ...] = (),
) -> dict[str, object]:
    marked = [
        {
            "page_idx": page_idx,
            "mcid": index,
            "mcid_marks": [{"mark_order": 0, "mcid": index}],
            "object_order": index,
            "object_type": "text",
            "object_depth": 0,
            "stream_scope": "page_content",
            "text": text,
            "bbox": list(bbox),
        }
        for index, (page_idx, text, bbox) in enumerate(cells)
    ]
    nodes: list[dict[str, object]] = []
    segments: list[dict[str, object]] = []
    if cells:
        refs = [
            {"page_idx": page_idx, "mcid": index}
            for index, (page_idx, _, _) in enumerate(cells)
        ]
        nodes.append(
            {
                "node_id": 1,
                "object_id": 10,
                "raw_role": "Table",
                "standard_role": "Table",
                "segment_id": "native_1",
                "ancestor_roles": [],
                "ancestor_node_ids": [],
                "forward_parent_object_id": 1,
                "declared_parent_object_id": 1,
                "parent_consistent": True,
                "mcid_refs": refs,
            }
        )
        for index, ref in enumerate(refs, start=2):
            nodes.append(
                {
                    "node_id": index,
                    "object_id": 10 + index,
                    "raw_role": "TD",
                    "standard_role": "TD",
                    "segment_id": "native_1",
                    "ancestor_roles": ["Table"],
                    "ancestor_node_ids": [1],
                    "forward_parent_object_id": 10,
                    "declared_parent_object_id": 10,
                    "parent_consistent": True,
                    "mcid_refs": [ref],
                }
            )
        segment_pages = sorted({page_idx for page_idx, _, _ in cells})
        segments.append(
            {
                "segment_id": "native_1",
                "top_object_id": 10,
                "node_id_span": [1, len(nodes)],
                "page_indices": segment_pages,
                "pages_contiguous": True,
            }
        )
    referenced_refs = {
        (int(ref["page_idx"]), int(ref["mcid"]))
        for node in nodes
        for ref in node["mcid_refs"]
    }
    resolved_refs = {(int(item["page_idx"]), int(item["mcid"])) for item in marked}
    return {
        "contract_version": NATIVE_PDF_STRUCTURE_VERSION,
        "source_pdf_sha256": "sha256:" + "a" * 64,
        "source_pdf_page_count": page_count,
        "native_status": "usable" if cells else "untagged",
        "pdfium_tagged": bool(cells),
        "role_map": {},
        "segments": segments,
        "nodes": nodes,
        "marked_content": marked,
        "bookmarks": [],
        "diagnostics": {
            "parent_conflicts": 0,
            "unresolved": [],
            "root_reachable_nodes": len(nodes),
            "visible_mcid_anchors": len(marked),
            "marked_content_objects": len(marked),
            "referenced_mcid_refs": len(referenced_refs),
            "resolved_mcid_refs": len(referenced_refs & resolved_refs),
            "unresolved_mcid_refs": [
                {"page_idx": page_idx, "mcid": mcid}
                for page_idx, mcid in sorted(referenced_refs - resolved_refs)
            ],
            "object_issues": [],
        },
    }


def _page(*atoms: tuple[str, tuple[float, float, float, float]]) -> NativeTextPage:
    offset = 0
    parts: list[str] = []
    native_atoms: list[NativeTextAtom] = []
    for order, (text, bbox) in enumerate(atoms):
        if parts:
            parts.append(" ")
            offset += 1
        start = offset
        parts.append(text)
        offset += len(text)
        native_atoms.append(
            NativeTextAtom(
                page_idx=0,
                order=order,
                bbox=bbox,
                char_span=(start, offset),
                text=text,
                layout=NativeTextLayoutRef(0, order, 0, 0),
            )
        )
    return NativeTextPage(0, 100.0, 200.0, "".join(parts), tuple(native_atoms))


def _page_at(
    page_idx: int,
    *atoms: tuple[str, tuple[float, float, float, float]],
) -> NativeTextPage:
    page = _page(*atoms)
    return replace(
        page,
        page_idx=page_idx,
        atoms=tuple(replace(atom, page_idx=page_idx) for atom in page.atoms),
    )


def _touching_page() -> NativeTextPage:
    return NativeTextPage(
        page_idx=0,
        width=100.0,
        height=200.0,
        text="股 份变动",
        atoms=(
            NativeTextAtom(
                page_idx=0,
                order=0,
                bbox=(10.0, 10.0, 20.0, 20.0),
                char_span=(0, 1),
                text="股",
                layout=NativeTextLayoutRef(0, 0, 0, 0),
            ),
            NativeTextAtom(
                page_idx=0,
                order=1,
                bbox=(20.0, 10.0, 50.0, 20.0),
                char_span=(2, 5),
                text="份变动",
                layout=NativeTextLayoutRef(0, 0, 0, 1),
            ),
        ),
    )


def _reconcile(
    items: list[dict[str, object]],
    page: NativeTextPage,
    *,
    visual_pages: tuple[VisualPageEvidence, ...] = (),
    visual_regions: tuple[VisualPageEvidence, ...] = (),
    visual_occurrence_artifacts: tuple[VisualPageEvidence, ...] | None = None,
    generated_annotation_artifacts: dict[str, Path] | None = None,
    native_structure: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    payload, digest = _artifact(items)
    if visual_occurrence_artifacts is None:
        visual_occurrence_artifacts = tuple(
            _occurrence(
                source_item_index,
                tuple(float(value) for value in item["bbox"]),
                page_idx=int(item["page_idx"]),
            )
            for source_item_index, item in enumerate(items)
            if item.get("type") in {"image", "chart"}
            and isinstance(item.get("bbox"), list)
            and isinstance(item.get("page_idx"), int)
        )
    return reconcile_source_evidence(
        source_pdf_sha256="sha256:" + "a" * 64,
        source_pdf_page_count=1,
        source_extractor=ExtractorIdentity("pdftotext", "25.06.0"),
        source_pages=(page,),
        native_structure=native_structure or _native_structure(),
        mineru_content_list_bytes=payload,
        expected_mineru_artifact_sha256=digest,
        canonical_content_list=items,
        expected_mineru_typed_artifact_sha256=TYPED_ARTIFACT_SHA256,
        mineru_extractor=ExtractorIdentity("mineru", "3.4.0"),
        visual_pages=visual_pages,
        visual_regions=visual_regions,
        visual_occurrence_artifacts=visual_occurrence_artifacts,
        generated_annotation_artifacts=generated_annotation_artifacts,
    )


def _validate(
    ledger: object,
    items: list[dict[str, object]],
    *,
    native_structure: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    payload, digest = _artifact(items)
    return validate_source_evidence_ledger(
        ledger,
        expected_source_pdf_sha256="sha256:" + "a" * 64,
        expected_source_pdf_page_count=1,
        expected_mineru_artifact_sha256=digest,
        mineru_content_list_bytes=payload,
        canonical_content_list=items,
        expected_mineru_typed_artifact_sha256=TYPED_ARTIFACT_SHA256,
        native_structure=native_structure or _native_structure(),
    )


def _visual(page_idx: int = 0) -> VisualPageEvidence:
    return VisualPageEvidence(
        page_idx=page_idx,
        artifact_role=f"source_page_visual_{page_idx + 1:06d}",
        artifact_path=Path(f"/unused/source_page_visual_{page_idx + 1:06d}.png"),
        sha256="sha256:" + f"{page_idx + 1:064x}",
        size_bytes=123,
        pixel_width=100,
        pixel_height=200,
        media_type="image/png",
        renderer=RENDERER_IDENTITY,
        render_options=RENDER_OPTIONS,
        png_options=PNG_OPTIONS,
    )


def _region(
    bbox: tuple[float, float, float, float],
    *,
    page_idx: int = 0,
    component_idx: int = 0,
) -> VisualPageEvidence:
    return replace(
        _visual(page_idx),
        artifact_role=(
            f"source_bbox_visual_{page_idx + 1:06d}_{component_idx + 1:06d}"
        ),
        bbox=bbox,
    )


def _occurrence(
    source_item_index: int,
    bbox: tuple[float, float, float, float],
    *,
    page_idx: int = 0,
) -> VisualPageEvidence:
    return replace(
        _visual(page_idx),
        artifact_role=f"source_visual_occurrence_{source_item_index:06d}",
        sha256="sha256:" + f"{source_item_index + 100:064x}",
        bbox=bbox,
    )


class SourceEvidenceTests(unittest.TestCase):
    def test_retrieval_run_joins_touching_fallback_occurrences(self) -> None:
        ledger = _reconcile([], _touching_page())

        self.assertEqual(
            [record["atom_indices"] for record in ledger["retrieval_runs"]],
            [[0, 1]],
        )
        self.assertEqual(
            ledger["retrieval_runs"][0]["text_sha256"],
            "sha256:" + hashlib.sha256("股份变动".encode()).hexdigest(),
        )
        self.assertEqual(
            ledger["retrieval_runs"][0]["boundary_basis"],
            "source_layout",
        )
        self.assertEqual(ledger["coverage"]["retrieval_run_atoms"], 2)

        mutated = deepcopy(ledger)
        mutated["retrieval_runs"][0]["atom_indices"].reverse()
        with self.assertRaises(SourceEvidenceContractError) as raised:
            _validate(mutated, [])
        self.assertEqual(
            raised.exception.reason_code,
            "retrieval_run_closure_invalid",
        )

    def test_table_fallback_occurrences_remain_singleton_without_cell_geometry(
        self,
    ) -> None:
        ledger = _reconcile(
            [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 500],
                    "table_body": "<table><tr><td>其他</td></tr></table>",
                }
            ],
            _touching_page(),
            visual_regions=(_region((0.0, 0.0, 1000.0, 500.0)),),
        )

        self.assertEqual(
            [
                (
                    record["atom_indices"],
                    record["join_algorithm"],
                    record["boundary_basis"],
                )
                for record in ledger["retrieval_runs"]
            ],
            [
                (
                    [0],
                    "table-cell-unproved-singleton.v1",
                    "provider_table_guard",
                ),
                (
                    [1],
                    "table-cell-unproved-singleton.v1",
                    "provider_table_guard",
                ),
            ],
        )

    def test_table_fallback_joins_only_inside_one_proved_pdf_cell(self) -> None:
        native_structure = _native_structure(
            cells=((0, "股份变动", (100.0, 50.0, 500.0, 100.0)),)
        )
        ledger = _reconcile(
            [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 500],
                    "table_body": "<table><tr><td>其他</td></tr></table>",
                }
            ],
            _touching_page(),
            visual_regions=(_region((0.0, 0.0, 1000.0, 500.0)),),
            native_structure=native_structure,
        )

        self.assertEqual(
            [
                (
                    record["atom_indices"],
                    record["join_algorithm"],
                    record["boundary_basis"],
                )
                for record in ledger["retrieval_runs"]
            ],
            [
                (
                    [0, 1],
                    "pdf-struct-tree-td-run.v1",
                    "native_complete_cell",
                )
            ],
        )
        _validate(
            ledger,
            [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 500],
                    "table_body": "<table><tr><td>其他</td></tr></table>",
                }
            ],
            native_structure=native_structure,
        )

    def test_adjacent_pdf_cells_never_form_one_retrieval_run(self) -> None:
        native_structure = _native_structure(
            cells=(
                (0, "股", (100.0, 50.0, 200.0, 100.0)),
                (0, "份变动", (200.0, 50.0, 500.0, 100.0)),
            )
        )
        ledger = _reconcile(
            [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 500],
                    "table_body": "<table><tr><td>其他</td></tr></table>",
                }
            ],
            _touching_page(),
            visual_regions=(_region((0.0, 0.0, 1000.0, 500.0)),),
            native_structure=native_structure,
        )

        self.assertEqual(
            [
                (
                    record["atom_indices"],
                    record["join_algorithm"],
                    record["boundary_basis"],
                )
                for record in ledger["retrieval_runs"]
            ],
            [
                ([0], "pdf-struct-tree-td-run.v1", "native_complete_cell"),
                ([1], "pdf-struct-tree-td-run.v1", "native_complete_cell"),
            ],
        )

    def test_native_cells_bound_runs_even_when_mineru_misses_the_table(self) -> None:
        native_structure = _native_structure(
            cells=(
                (0, "股", (100.0, 50.0, 200.0, 100.0)),
                (0, "份变动", (200.0, 50.0, 500.0, 100.0)),
            )
        )
        ledger = _reconcile(
            [],
            _touching_page(),
            native_structure=native_structure,
        )

        self.assertEqual(
            [
                (
                    record["atom_indices"],
                    record["join_algorithm"],
                    record["boundary_basis"],
                )
                for record in ledger["retrieval_runs"]
            ],
            [
                ([0], "pdf-struct-tree-td-run.v1", "native_complete_cell"),
                ([1], "pdf-struct-tree-td-run.v1", "native_complete_cell"),
            ],
        )

    def test_partial_native_cell_still_guards_against_cross_cell_join(self) -> None:
        native_structure = _native_structure(
            cells=((0, "股份变动", (100.0, 50.0, 500.0, 100.0)),)
        )
        for node in native_structure["nodes"]:
            node["mcid_refs"].append({"page_idx": 0, "mcid": 9})
        native_structure["native_status"] = "partial"
        native_structure["diagnostics"].update(
            {
                "referenced_mcid_refs": 2,
                "resolved_mcid_refs": 1,
                "unresolved_mcid_refs": [{"page_idx": 0, "mcid": 9}],
            }
        )

        ledger = _reconcile(
            [],
            _touching_page(),
            native_structure=native_structure,
        )
        self.assertEqual(
            [
                (
                    record["atom_indices"],
                    record["join_algorithm"],
                    record["boundary_basis"],
                )
                for record in ledger["retrieval_runs"]
            ],
            [
                ([0], "table-cell-unproved-singleton.v1", "native_table_guard"),
                ([1], "table-cell-unproved-singleton.v1", "native_table_guard"),
            ],
        )

    def test_carrier_reverse_support_is_native_only_when_fully_covered(self) -> None:
        item: dict[str, object] = {
            "type": "text",
            "page_idx": 0,
            "bbox": [0, 0, 1000, 500],
            "text": "甲 乙",
        }
        native = _page(
            ("甲", (1, 1, 10, 10)),
            ("乙", (11, 1, 30, 10)),
        )
        partial = _page(("甲", (1, 1, 10, 10)))
        zero = _page(("unrelated", (1, 1, 30, 10)))

        self.assertEqual(
            required_carrier_visual_regions(
                [item],
                source_pages=(native,),
                source_pdf_page_count=1,
            ),
            (),
        )
        for page in (partial, zero):
            with self.subTest(native_text=page.text):
                requests = required_carrier_visual_regions(
                    [item],
                    source_pages=(page,),
                    source_pdf_page_count=1,
                )
                self.assertEqual(
                    requests[0].bbox,
                    (0.0, 0.0, 1000.0, 500.0),
                )
                ledger = _reconcile(
                    [item],
                    page,
                    visual_regions=(_region((0.0, 0.0, 1000.0, 500.0)),),
                )
                self.assertEqual(
                    ledger["carrier_support"][0]["support"]["kind"],
                    "visual_bound",
                )

        native_ledger = _reconcile([item], native)
        self.assertEqual(
            native_ledger["carrier_support"][0]["support"],
            {"kind": "native_exact", "source_atom_orders": [0, 1]},
        )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            required_carrier_visual_regions(
                [{**item, "bbox": None}],
                source_pages=(native,),
                source_pdf_page_count=1,
            )
        self.assertEqual(raised.exception.reason_code, "mineru_carrier_unbound")

    def test_visual_support_index_and_manifest_are_hash_closed(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 500],
                "text": "ABC",
            }
        ]
        region = _region((0.0, 0.0, 1000.0, 500.0))
        ledger = _reconcile(
            items,
            _page(("A", (1, 1, 10, 10))),
            visual_regions=(region,),
        )
        self.assertEqual(len(ledger["carrier_support"]), 1)
        record = ledger["carrier_support"][0]
        self.assertEqual(record["bbox"], [0.0, 0.0, 1000.0, 500.0])
        self.assertEqual(
            resolve_ir_text_selector(
                [{"source_item_index": 0, "text": "ABC"}],
                record["selector"],
            ),
            "ABC",
        )
        self.assertEqual(
            record["support"],
            {
                "kind": "visual_bound",
                "component_bbox": [0.0, 0.0, 1000.0, 500.0],
                "artifact": {
                    "artifact_role": region.artifact_role,
                    "sha256": region.sha256,
                    "size_bytes": region.size_bytes,
                    "pixel_width": region.pixel_width,
                    "pixel_height": region.pixel_height,
                    "media_type": region.media_type,
                },
            },
        )
        payload, digest = _artifact(items)
        manifest = {
            "files": {
                region.artifact_role: {
                    "availability": "present",
                    "relpath": f"parser/run/{region.artifact_role}.png",
                    "sha256": region.sha256,
                    "size_bytes": region.size_bytes,
                }
            }
        }

        validated = validate_source_evidence_ledger(
            ledger,
            expected_source_pdf_sha256="sha256:" + "a" * 64,
            expected_source_pdf_page_count=1,
            expected_mineru_artifact_sha256=digest,
            mineru_content_list_bytes=payload,
            **_typed_args(items),
            parser_artifacts=manifest,
        )
        validate_mapped_element_bindings(
            validated,
            elements=[{"source_item_index": 0, "page_idx": 0, "text": "ABC"}],
        )
        index = carrier_source_support_index(validated)
        support = index[(0, "text", None)]
        self.assertEqual(
            (support.kind, support.artifact_role, support.artifact_sha256),
            ("visual_bound", region.artifact_role, region.sha256),
        )
        with self.assertRaises(TypeError):
            index[(1, "text", None)] = support  # type: ignore[index]
        manifest["files"][region.artifact_role]["sha256"] = "sha256:" + "f" * 64
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(
                ledger,
                expected_source_pdf_sha256="sha256:" + "a" * 64,
                expected_source_pdf_page_count=1,
                expected_mineru_artifact_sha256=digest,
                mineru_content_list_bytes=payload,
                **_typed_args(items),
                parser_artifacts=manifest,
            )
        self.assertEqual(
            raised.exception.reason_code,
            "visual_manifest_identity_mismatch",
        )

    def test_generated_image_description_is_annotation_not_source_text(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "image",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 500],
                "text": "模型生成描述",
                "img_path": "images/0.png",
            }
        ]
        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "0.png"
            image_path.write_bytes(b"provider-image-bytes")
            ledger = _reconcile(
                items,
                _page(("模型生成描述", (1, 1, 30, 10))),
                generated_annotation_artifacts={"evidence_image_000000": image_path},
            )

            self.assertEqual(
                ledger["atoms"][0]["disposition"],
                {
                    "kind": "source_native_fallback",
                    "reason": "mineru_text_missing",
                },
            )
            annotation = ledger["carrier_support"][0]
            self.assertEqual(
                annotation["support"]["kind"],
                "generated_annotation",
            )
            self.assertEqual(
                annotation["support"]["artifact"]["artifact_role"],
                "evidence_image_000000",
            )
            payload, digest = _artifact(items)
            self.assertEqual(
                carrier_source_support_index(ledger),
                {},
            )

        with self.assertRaises(SourceEvidenceContractError) as raised:
            _reconcile(items, _page(("模型生成描述", (1, 1, 30, 10))))
        self.assertEqual(
            raised.exception.reason_code,
            "generated_annotation_unbound",
        )

    def test_generated_image_annotation_cannot_become_a_heading(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "image",
                "page_idx": 0,
                "bbox": [100, 100, 300, 130],
                "text": "模型生成标题",
                "img_path": "images/0.png",
            }
        ]
        typed = [
            [
                {
                    "type": "title",
                    "bbox": [100, 100, 300, 130],
                    "content": {
                        "level": 1,
                        "title_content": [
                            {
                                "type": "text",
                                "content": "模型生成标题",
                            }
                        ],
                    },
                }
            ]
        ]
        with self.assertRaises(ParserOutputContractError):
            projections = build_mineru_text_projections(
                items,
                typed,
                serializer_backend="vlm",
                page_offset=0,
                expected_page_count=1,
            )
            build_mineru_structure_proof(
                native=native_index(page_count=1),
                content_list=items,
                content_list_v2=typed,
                text_projections=projections,
                source_pdf_sha256="sha256:" + "a" * 64,
            )

    def test_table_caption_selector_round_trips_from_mapped_ir(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "table",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 300],
                "table_caption": ["2.1 公司基本情况"],
                "table_body": "<table><tr><td>注册地</td></tr></table>",
                "table_footnote": [],
            }
        ]
        ledger = _reconcile(
            items,
            _page(
                ("2.1 公司基本情况", (10, 10, 80, 22)),
                ("注册地", (10, 30, 40, 42)),
            ),
        )

        atoms = ledger["atoms"]
        assert isinstance(atoms, list)
        pages = ledger["pages"]
        assert isinstance(pages, list)
        self.assertFalse(pages[0]["fallback_required"])
        self.assertEqual(pages[0]["fallback_reasons"], {})
        self.assertEqual(
            pages[0]["text_sha256"],
            "sha256:" + hashlib.sha256(pages[0]["text"].encode()).hexdigest(),
        )
        caption = atoms[0]["disposition"]
        self.assertEqual(caption["kind"], "mineru_carrier")
        selector = caption["carrier"]["selector"]
        self.assertEqual(
            {
                "source_item_index": selector["source_item_index"],
                "field": selector["field"],
                "index": selector["index"],
            },
            {"source_item_index": 0, "field": "table_caption", "index": 0},
        )
        carriers = iter_mineru_text_carriers(items)
        self.assertEqual(carriers[0].source_value, "2.1 公司基本情况")
        self.assertEqual(carriers[0].comparison_value, "2.1公司基本情况")
        mapped_ir = [
            {
                "source_item_index": 0,
                "table_caption": ["2.1 公司基本情况"],
                "table_html": "<table><tr><td>注册地</td></tr></table>",
                "table_footnote": [],
            }
        ]
        self.assertEqual(
            resolve_ir_text_selector(mapped_ir, selector),
            "2.1公司基本情况",
        )
        body_selector = atoms[1]["disposition"]["carrier"]["selector"]
        self.assertEqual(body_selector["field"], "table_html")
        self.assertEqual(
            resolve_ir_text_selector(mapped_ir, body_selector),
            "注册地",
        )

    def test_repeated_atoms_use_distinct_spans_exactly_once(self) -> None:
        ledger = _reconcile(
            [
                {
                    "type": "text",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 500],
                    "text": "甲 甲",
                }
            ],
            _page(
                ("甲", (10, 10, 20, 20)),
                ("甲", (30, 10, 40, 20)),
            ),
        )

        atoms = ledger["atoms"]
        assert isinstance(atoms, list)
        selectors = [atom["disposition"]["carrier"]["selector"] for atom in atoms]
        self.assertEqual(
            [selector["char_span"] for selector in selectors],
            [[0, 1], [1, 2]],
        )
        self.assertEqual(
            [atom["disposition"]["kind"] for atom in atoms],
            ["mineru_carrier", "mineru_carrier"],
        )

    def test_exact_alternate_reading_order_is_diagnostic_not_fallback(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [500, 0, 1000, 500],
                "text": "右栏",
            },
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 500, 500],
                "text": "左栏",
            },
        ]

        ledger = _reconcile(
            items,
            _page(
                ("左栏", (10, 10, 40, 20)),
                ("右栏", (60, 10, 90, 20)),
            ),
        )

        self.assertEqual(
            [atom["disposition"]["source_order"] for atom in ledger["atoms"]],
            ["monotonic", "conflict"],
        )
        self.assertEqual(ledger["pages"][0]["source_order_conflicts"], 1)
        self.assertFalse(ledger["pages"][0]["fallback_required"])
        self.assertEqual(ledger["pages"][0]["fallback_reasons"], {})
        self.assertEqual(ledger["coverage"]["source_order_conflicts"], 1)
        self.assertEqual(ledger["coverage"]["source_native_fallbacks"], 0)

    def test_real_coverage_gap_still_falls_back_with_order_conflict(self) -> None:
        ledger = _reconcile(
            [
                {
                    "type": "text",
                    "page_idx": 0,
                    "bbox": [500, 0, 1000, 500],
                    "text": "右栏",
                },
                {
                    "type": "text",
                    "page_idx": 0,
                    "bbox": [0, 0, 500, 500],
                    "text": "左栏",
                },
            ],
            _page(
                ("左栏", (10, 10, 40, 20)),
                ("右栏", (60, 10, 90, 20)),
                ("缺失", (10, 40, 40, 50)),
            ),
        )

        self.assertEqual(ledger["pages"][0]["source_order_conflicts"], 1)
        self.assertTrue(ledger["pages"][0]["fallback_required"])
        self.assertEqual(
            ledger["pages"][0]["fallback_reasons"],
            {"mineru_text_missing": 1},
        )
        self.assertEqual(ledger["coverage"]["source_native_fallbacks"], 1)

    def test_overlapping_duplicate_carriers_do_not_fake_unique_location(
        self,
    ) -> None:
        ledger = _reconcile(
            [
                {
                    "type": "text",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 500],
                    "text": "重复事实",
                },
                {
                    "type": "text",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 500],
                    "text": "重复事实",
                },
            ],
            _page(("重复事实", (10, 10, 80, 20))),
            visual_regions=(_region((0.0, 0.0, 1000.0, 500.0)),),
        )

        self.assertEqual(
            ledger["atoms"][0]["disposition"],
            {
                "kind": "source_native_fallback",
                "reason": "mineru_locator_unproved",
            },
        )
        self.assertEqual(
            ledger["pages"][0]["fallback_reasons"],
            {"mineru_locator_unproved": 1},
        )

    def test_occurrence_cannot_cross_table_cells_but_can_cross_inline_markup(
        self,
    ) -> None:
        across_cells = _reconcile(
            [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 500],
                    "table_body": "<table><tr><td>1</td><td>2</td></tr></table>",
                }
            ],
            _page(("12", (10, 10, 40, 20))),
            visual_regions=(_region((0.0, 0.0, 1000.0, 500.0)),),
        )
        within_cell = _reconcile(
            [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 500],
                    "table_body": (
                        "<table><tr><td>1 <strong>2</strong></td></tr></table>"
                    ),
                }
            ],
            _page(("12", (10, 10, 40, 20))),
        )

        self.assertEqual(
            across_cells["atoms"][0]["disposition"]["kind"],
            "source_native_fallback",
        )
        self.assertEqual(
            within_cell["atoms"][0]["disposition"]["kind"],
            "mineru_carrier",
        )

    def test_missing_or_unlocated_text_is_preserved_as_native_fallback(self) -> None:
        ledger = _reconcile(
            [
                {
                    "type": "text",
                    "page_idx": 0,
                    "bbox": [0, 800, 1000, 900],
                    "text": "存在但位置不符",
                }
            ],
            _page(
                ("存在但位置不符", (10, 10, 80, 20)),
                ("MinerU缺失", (10, 30, 80, 40)),
            ),
            visual_regions=(_region((0.0, 800.0, 1000.0, 900.0)),),
        )

        atoms = ledger["atoms"]
        assert isinstance(atoms, list)
        self.assertEqual(
            [atom["disposition"]["kind"] for atom in atoms],
            ["source_native_fallback", "source_native_fallback"],
        )
        self.assertEqual(
            [atom["disposition"]["reason"] for atom in atoms],
            ["mineru_locator_unproved", "mineru_text_missing"],
        )
        self.assertEqual(
            [atom["source"]["text"] for atom in atoms],
            ["存在但位置不符", "MinerU缺失"],
        )
        self.assertEqual(
            ledger["pages"][0]["fallback_reasons"],
            {"mineru_locator_unproved": 1, "mineru_text_missing": 1},
        )
        self.assertTrue(ledger["pages"][0]["fallback_required"])

    def test_hash_schema_and_mapped_value_drift_fail_closed(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 100],
                "text": "A",
            }
        ]
        payload, digest = _artifact(items)
        common = {
            "source_pdf_sha256": "sha256:" + "a" * 64,
            "source_pdf_page_count": 1,
            "source_extractor": ExtractorIdentity("pdftotext", "25.06.0"),
            "source_pages": (_page(("A", (1, 1, 5, 5))),),
            "mineru_content_list_bytes": payload,
            **_typed_args(items),
            "mineru_extractor": ExtractorIdentity("mineru", "3.4.0"),
        }
        with self.assertRaisesRegex(
            SourceEvidenceContractError, "artifact hash differs"
        ):
            reconcile_source_evidence(
                **common,
                expected_mineru_artifact_sha256="sha256:" + "b" * 64,
            )
        unknown, unknown_hash = _artifact([{"type": "future_type"}])
        with self.assertRaisesRegex(SourceEvidenceContractError, "unsupported type"):
            reconcile_source_evidence(
                **{
                    **common,
                    "mineru_content_list_bytes": unknown,
                    "expected_mineru_artifact_sha256": unknown_hash,
                    "canonical_content_list": [{"type": "future_type"}],
                }
            )

        ledger = reconcile_source_evidence(
            **common,
            expected_mineru_artifact_sha256=digest,
        )
        atoms = ledger["atoms"]
        assert isinstance(atoms, list)
        selector = atoms[0]["disposition"]["carrier"]["selector"]
        with self.assertRaisesRegex(SourceEvidenceContractError, "field drifted"):
            resolve_ir_text_selector(
                [{"source_item_index": 0, "text": "B"}],
                selector,
            )

    def test_visual_page_requires_a_searchable_carrier(self) -> None:
        empty_page = NativeTextPage(0, 100.0, 200.0, "", ())
        with self.assertRaisesRegex(
            SourceEvidenceContractError, "visual evidence pages differ"
        ) as raised:
            _reconcile([], empty_page)
        self.assertEqual(
            raised.exception.reason_code,
            "visual_artifact_closure_invalid",
        )

        with self.assertRaises(SourceEvidenceContractError) as raised:
            _reconcile([], empty_page, visual_pages=(_visual(),))
        self.assertEqual(
            raised.exception.reason_code,
            "visual_page_search_carrier_missing",
        )

        items: list[dict[str, object]] = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 500],
                "text": "OCR正文",
            }
        ]
        ledger = _reconcile(
            items,
            empty_page,
            visual_pages=(_visual(),),
        )

        page = ledger["pages"][0]
        self.assertEqual(page["modality"], "visual_page")
        self.assertEqual(page["text"], "")
        self.assertEqual(page["atom_count"], 0)
        self.assertEqual(
            page["fallback_reasons"],
            {"source_native_text_absent": 1},
        )
        self.assertEqual(
            page["visual_artifact"]["artifact_role"],
            "source_page_visual_000001",
        )
        self.assertEqual(ledger["coverage"]["visual_pages"], 1)
        self.assertEqual(
            ledger["carrier_support"][0]["support"]["artifact"],
            page["visual_artifact"],
        )
        self.assertIsNotNone(ledger["visual_renderer"])
        content_payload, content_hash = _artifact(items)
        visual = page["visual_artifact"]
        manifest = {
            "files": {
                visual["artifact_role"]: {
                    "availability": "present",
                    "relpath": "parser/run/source_page_visual_000001.png",
                    "sha256": visual["sha256"],
                    "size_bytes": visual["size_bytes"],
                }
            }
        }
        validate_source_evidence_ledger(
            ledger,
            expected_source_pdf_sha256="sha256:" + "a" * 64,
            expected_source_pdf_page_count=1,
            expected_mineru_artifact_sha256=content_hash,
            mineru_content_list_bytes=content_payload,
            **_typed_args(items),
            parser_artifacts=manifest,
        )
        manifest["files"][visual["artifact_role"]]["sha256"] = "sha256:" + "f" * 64
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(
                ledger,
                expected_source_pdf_sha256="sha256:" + "a" * 64,
                expected_source_pdf_page_count=1,
                expected_mineru_artifact_sha256=content_hash,
                mineru_content_list_bytes=content_payload,
                **_typed_args(items),
                parser_artifacts=manifest,
            )
        self.assertEqual(
            raised.exception.reason_code,
            "visual_manifest_identity_mismatch",
        )

    def test_invalid_native_geometry_requires_a_closed_visual_guard(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 1000],
                "text": "visible",
            }
        ]
        page = replace(
            _page(("visible", (1, 1, 5, 5))),
            geometry_issues=(
                NativeTextGeometryIssue(
                    page_idx=0,
                    word_order=1,
                    text="hidden",
                    raw_bbox=(6.0, 7.0, 7.0, 7.0),
                    reason="bbox_non_positive_extent",
                ),
            ),
        )
        with self.assertRaises(SourceEvidenceContractError) as raised:
            _reconcile(items, page)
        self.assertEqual(
            raised.exception.reason_code,
            "visual_artifact_closure_invalid",
        )

        ledger = _reconcile(items, page, visual_pages=(_visual(),))
        record = ledger["pages"][0]
        self.assertEqual(record["modality"], "native_text_with_visual_guard")
        self.assertEqual(
            record["fallback_reasons"],
            {"source_native_geometry_invalid": 1},
        )
        self.assertEqual(record["geometry_issues"][0]["word_order"], 1)
        self.assertEqual(ledger["coverage"]["native_geometry_issues"], 1)
        self.assertEqual(ledger["coverage"]["visual_pages"], 1)

        record["geometry_issues"][0]["text_sha256"] = "sha256:" + "f" * 64
        content_payload, content_hash = _artifact(items)
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(
                ledger,
                expected_source_pdf_sha256="sha256:" + "a" * 64,
                expected_source_pdf_page_count=1,
                expected_mineru_artifact_sha256=content_hash,
                mineru_content_list_bytes=content_payload,
                **_typed_args(items),
            )
        self.assertEqual(
            raised.exception.reason_code,
            "source_geometry_issue_invalid",
        )

    def test_geometry_only_page_is_not_mislabeled_as_native_text_absence(self) -> None:
        page = NativeTextPage(
            0,
            100.0,
            200.0,
            "",
            (),
            (
                NativeTextGeometryIssue(
                    page_idx=0,
                    word_order=0,
                    text="unlocated",
                    raw_bbox=(1.0, 2.0, 2.0, 2.0),
                    reason="bbox_non_positive_extent",
                ),
            ),
        )

        ledger = _reconcile([], page, visual_pages=(_visual(),))

        record = ledger["pages"][0]
        self.assertEqual(record["modality"], "visual_page")
        self.assertEqual(
            record["fallback_reasons"],
            {"source_native_geometry_invalid": 1},
        )

    def test_visual_artifact_cannot_replace_or_duplicate_native_text_page(
        self,
    ) -> None:
        with self.assertRaises(SourceEvidenceContractError) as raised:
            _reconcile(
                [{"type": "text", "page_idx": 0, "text": "A"}],
                _page(("A", (1, 1, 5, 5))),
                visual_pages=(_visual(),),
            )
        self.assertEqual(
            raised.exception.reason_code,
            "visual_artifact_closure_invalid",
        )

    def test_ledger_validation_rejects_page_and_ir_drift(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "text",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 100],
                "text": "A",
            }
        ]
        payload, content_hash = _artifact(items)
        ledger = _reconcile(items, _page(("A", (1, 1, 5, 5))))
        validated = validate_source_evidence_ledger(
            ledger,
            expected_source_pdf_sha256="sha256:" + "a" * 64,
            expected_source_pdf_page_count=1,
            expected_mineru_artifact_sha256=content_hash,
            mineru_content_list_bytes=payload,
            **_typed_args(items),
        )
        validate_mapped_element_bindings(
            validated,
            elements=[{"source_item_index": 0, "page_idx": 0, "text": "A"}],
        )

        page = ledger["pages"][0]
        page["text"] = "B"
        with self.assertRaisesRegex(
            SourceEvidenceContractError,
            "page text/geometry/count is invalid",
        ):
            validate_source_evidence_ledger(
                ledger,
                expected_source_pdf_sha256="sha256:" + "a" * 64,
                expected_source_pdf_page_count=1,
                expected_mineru_artifact_sha256=content_hash,
                mineru_content_list_bytes=payload,
                **_typed_args(items),
            )
        page["text"] = "A"
        page["text_sha256"] = "sha256:" + hashlib.sha256(b"A").hexdigest()
        with self.assertRaisesRegex(
            SourceEvidenceContractError,
            "mapped IR field drifted",
        ):
            validate_mapped_element_bindings(
                validated,
                elements=[{"source_item_index": 0, "page_idx": 0, "text": "B"}],
            )
        with self.assertRaisesRegex(
            SourceEvidenceContractError,
            "source atom page differs from mapped IR carrier",
        ):
            validate_mapped_element_bindings(
                validated,
                elements=[{"source_item_index": 0, "page_idx": 1, "text": "A"}],
            )

    def test_ledger_rejects_root_atoms_outside_page_major_order(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "text",
                "page_idx": page_idx,
                "bbox": [0, 0, 1000, 100],
                "text": text,
            }
            for page_idx, text in enumerate(("A", "B"))
        ]
        payload, digest = _artifact(items)
        ledger = reconcile_source_evidence(
            source_pdf_sha256="sha256:" + "a" * 64,
            source_pdf_page_count=2,
            source_extractor=ExtractorIdentity("pdftotext", "25.06.0"),
            source_pages=(
                _page_at(0, ("A", (1, 1, 5, 5))),
                _page_at(1, ("B", (1, 1, 5, 5))),
            ),
            native_structure=_native_structure(page_count=2),
            mineru_content_list_bytes=payload,
            expected_mineru_artifact_sha256=digest,
            canonical_content_list=items,
            expected_mineru_typed_artifact_sha256=TYPED_ARTIFACT_SHA256,
            mineru_extractor=ExtractorIdentity("mineru", "3.4.0"),
        )
        atoms = ledger["atoms"]
        assert isinstance(atoms, list)
        atoms.reverse()

        with self.assertRaisesRegex(
            SourceEvidenceContractError,
            "strict page-major source order",
        ):
            validate_source_evidence_ledger(
                ledger,
                expected_source_pdf_sha256="sha256:" + "a" * 64,
                expected_source_pdf_page_count=2,
                expected_mineru_artifact_sha256=digest,
                mineru_content_list_bytes=payload,
                canonical_content_list=items,
                expected_mineru_typed_artifact_sha256=(TYPED_ARTIFACT_SHA256),
                native_structure=_native_structure(page_count=2),
            )

    def test_carrier_schema_uses_canonical_ir_fields(self) -> None:
        carriers = iter_mineru_text_carriers(
            [
                {
                    "type": "chart",
                    "page_idx": 0,
                    "bbox": [0, 0, 100, 100],
                    "content": "series",
                    "chart_caption": ["caption"],
                    "chart_footnote": ["note"],
                },
                {
                    "type": "list",
                    "page_idx": 0,
                    "bbox": [0, 100, 100, 200],
                    "list_items": ["one", "two"],
                },
            ]
        )

        self.assertEqual(
            [(item.field, item.index) for item in carriers],
            [
                ("image_caption", 0),
                ("image_footnote", 0),
                ("list_items", 0),
                ("list_items", 1),
            ],
        )

    def test_chart_recognition_is_always_bound_to_its_occurrence_crop(self) -> None:
        item: dict[str, object] = {
            "type": "chart",
            "page_idx": 0,
            "bbox": [100, 100, 500, 500],
            "content": "收入 10",
            "img_path": "images/chart.png",
        }

        ledger = _reconcile(
            [item],
            _page(("收入 10", (10, 10, 50, 20))),
        )

        self.assertEqual(
            ledger["atoms"][0]["disposition"],
            {
                "kind": "source_native_fallback",
                "reason": "mineru_text_missing",
            },
        )
        (support,) = ledger["carrier_support"]
        self.assertEqual(support["selector"]["field"], "text")
        self.assertEqual(support["support"]["kind"], "visual_bound")
        self.assertEqual(
            support["support"]["artifact"]["artifact_role"],
            "source_visual_occurrence_000000",
        )
        self.assertEqual(
            ledger["visual_occurrences"][0]["source_item_index"],
            0,
        )

    def test_visual_occurrence_crop_closure_rejects_missing_or_misbound_crops(
        self,
    ) -> None:
        item: dict[str, object] = {
            "type": "chart",
            "page_idx": 0,
            "bbox": [100, 100, 500, 500],
            "content": "收入 10",
            "img_path": "images/chart.png",
        }
        native = _page(("unrelated", (10, 10, 50, 20)))
        invalid_artifacts = (
            (),
            (
                replace(
                    _occurrence(0, (100, 100, 500, 500)),
                    artifact_role="source_visual_occurrence_000001",
                ),
            ),
            (
                replace(
                    _occurrence(0, (100, 100, 500, 500)),
                    page_idx=1,
                ),
            ),
            (_occurrence(0, (100, 100, 600, 500)),),
        )

        for artifacts in invalid_artifacts:
            with self.subTest(artifacts=artifacts):
                with self.assertRaises(SourceEvidenceContractError):
                    _reconcile(
                        [item],
                        native,
                        visual_occurrence_artifacts=artifacts,
                    )

    def test_duplicate_visual_bytes_remain_distinct_occurrences(self) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "image",
                "page_idx": 0,
                "bbox": [0, 0, 400, 400],
                "img_path": "images/shared.png",
            },
            {
                "type": "chart",
                "page_idx": 0,
                "bbox": [500, 500, 900, 900],
                "img_path": "images/shared.png",
            },
        ]
        shared_sha = "sha256:" + "e" * 64
        occurrences = (
            replace(
                _occurrence(0, (0, 0, 400, 400)),
                sha256=shared_sha,
            ),
            replace(
                _occurrence(1, (500, 500, 900, 900)),
                sha256=shared_sha,
            ),
        )

        ledger = _reconcile(
            items,
            _page(("native", (10, 10, 50, 20))),
            visual_occurrence_artifacts=occurrences,
        )

        self.assertEqual(
            [
                record["artifact"]["artifact_role"]
                for record in ledger["visual_occurrences"]
            ],
            [
                "source_visual_occurrence_000000",
                "source_visual_occurrence_000001",
            ],
        )
        self.assertEqual(
            {record["artifact"]["sha256"] for record in ledger["visual_occurrences"]},
            {shared_sha},
        )

    def test_visual_occurrence_identity_and_manifest_are_hash_closed(self) -> None:
        item: dict[str, object] = {
            "type": "chart",
            "page_idx": 0,
            "bbox": [100, 100, 500, 500],
            "content": "收入 10",
            "img_path": "images/chart.png",
        }
        occurrence = _occurrence(0, (100, 100, 500, 500))
        ledger = _reconcile(
            [item],
            _page(("unrelated", (10, 10, 50, 20))),
            visual_occurrence_artifacts=(occurrence,),
        )
        payload, digest = _artifact([item])
        manifest = {
            "files": {
                occurrence.artifact_role: {
                    "availability": "present",
                    "sha256": occurrence.sha256,
                    "size_bytes": occurrence.size_bytes,
                }
            }
        }
        validate_source_evidence_ledger(
            ledger,
            expected_source_pdf_sha256="sha256:" + "a" * 64,
            expected_source_pdf_page_count=1,
            expected_mineru_artifact_sha256=digest,
            mineru_content_list_bytes=payload,
            **_typed_args([item]),
            parser_artifacts=manifest,
        )

        tampered = deepcopy(ledger)
        tampered["visual_occurrences"][0]["source_item_sha256"] = "sha256:" + "f" * 64
        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(
                tampered,
                expected_source_pdf_sha256="sha256:" + "a" * 64,
                expected_source_pdf_page_count=1,
                expected_mineru_artifact_sha256=digest,
                mineru_content_list_bytes=payload,
                **_typed_args([item]),
            )
        self.assertEqual(
            raised.exception.reason_code,
            "visual_occurrence_identity_mismatch",
        )

        with self.assertRaises(SourceEvidenceContractError) as raised:
            validate_source_evidence_ledger(
                ledger,
                expected_source_pdf_sha256="sha256:" + "a" * 64,
                expected_source_pdf_page_count=1,
                expected_mineru_artifact_sha256=digest,
                mineru_content_list_bytes=payload,
                **_typed_args([item]),
                parser_artifacts={"files": {}},
            )
        self.assertEqual(
            raised.exception.reason_code,
            "visual_manifest_closure_invalid",
        )


def _table_item(table_body: str) -> dict[str, object]:
    return {
        "type": "table",
        "page_idx": 0,
        "bbox": [0, 0, 1000, 500],
        "table_body": table_body,
    }


def _dispositions(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    atoms = ledger["atoms"]
    assert isinstance(atoms, list)
    return [atom["disposition"] for atom in atoms]


def _carrier_identity(disposition: Mapping[str, Any]) -> tuple[Any, ...]:
    carrier = disposition["carrier"]
    selector = carrier["selector"]
    return (
        carrier["page_idx"],
        tuple(carrier["bbox"]),
        carrier["order"],
        selector["source_item_index"],
        selector["field"],
        selector.get("index"),
        selector["value_sha256"],
    )


class WrappedCellTokenTests(unittest.TestCase):
    """One cell value wrapped across source words stays inside that cell.

    A narrow column breaks a single visual token across word boundaries, so
    every fragment fails the clean-edge occurrence test on its own.  These
    cases pin the joined re-match: it recovers exactly the fragments a cell
    proves, never invents a value the carrier does not contain, and never
    outranks a fragment that already proves its own cell.
    """

    _ROW = (
        "<table><tr><td>营业收入</td><td>2,473,393,488.20</td>"
        "<td>2,170,257,670.61</td></tr></table>"
    )

    def test_cell_value_wrapped_across_words_binds_one_carrier_span(self) -> None:
        # Poppler emits "2,473,393,488.2" and "0" as two words because the
        # cell wraps; neither fragment has a clean edge inside the carrier.
        ledger = _reconcile(
            [_table_item(self._ROW)],
            _page(
                ("营业收入", (10, 10, 30, 20)),
                ("2,473,393,488.2", (40, 10, 60, 20)),
                ("0", (40, 22, 60, 32)),
                ("2,170,257,670.61", (70, 10, 90, 20)),
            ),
        )

        dispositions = _dispositions(ledger)
        self.assertEqual(
            [item["kind"] for item in dispositions],
            ["mineru_carrier"] * 4,
        )
        head, tail = dispositions[1], dispositions[2]
        self.assertEqual(_carrier_identity(head), _carrier_identity(tail))
        self.assertEqual(head["carrier"]["selector"]["char_span"], [4, 19])
        self.assertEqual(tail["carrier"]["selector"]["char_span"], [19, 20])
        self.assertEqual(
            head["carrier"]["selector"]["char_span"][1],
            tail["carrier"]["selector"]["char_span"][0],
        )
        self.assertEqual(
            [item["source_order"] for item in dispositions],
            ["monotonic"] * 4,
        )
        self.assertEqual(ledger["coverage"]["source_native_fallbacks"], 0)
        self.assertEqual(ledger["pages"][0]["fallback_reasons"], {})

    def test_joined_fragments_absent_from_the_cell_stay_native_fallback(
        self,
    ) -> None:
        # The trailing digit does not complete the cell value, so the join
        # has no occurrence either: recovery must stay a native fallback
        # instead of binding the nearest number.
        ledger = _reconcile(
            [_table_item(self._ROW)],
            _page(
                ("营业收入", (10, 10, 30, 20)),
                ("2,473,393,488.2", (40, 10, 60, 20)),
                ("1", (40, 22, 60, 32)),
                ("2,170,257,670.61", (70, 10, 90, 20)),
            ),
            visual_regions=(_region((0.0, 0.0, 1000.0, 500.0)),),
        )

        dispositions = _dispositions(ledger)
        self.assertEqual(
            [item["kind"] for item in dispositions],
            [
                "mineru_carrier",
                "source_native_fallback",
                "source_native_fallback",
                "mineru_carrier",
            ],
        )
        self.assertEqual(
            [dispositions[1]["reason"], dispositions[2]["reason"]],
            ["mineru_text_missing", "mineru_text_missing"],
        )
        self.assertEqual(
            ledger["pages"][0]["fallback_reasons"],
            {"mineru_text_missing": 2},
        )

    def test_atoms_proving_their_own_cell_outrank_a_longer_join(self) -> None:
        # Both words prove one cell each, while their concatenation is the
        # value of a third cell.  The per-atom proof must win, or the two
        # words would be attributed to a cell they never occupy.
        ledger = _reconcile(
            [
                _table_item(
                    "<table><tr><td>2,473,393,488.2</td><td>0</td>"
                    "<td>2,473,393,488.20</td></tr></table>"
                )
            ],
            _page(
                ("2,473,393,488.2", (10, 10, 40, 20)),
                ("0", (50, 10, 60, 20)),
            ),
            visual_regions=(_region((0.0, 0.0, 1000.0, 500.0)),),
        )

        dispositions = _dispositions(ledger)
        self.assertEqual(
            [item["kind"] for item in dispositions],
            ["mineru_carrier", "mineru_carrier"],
        )
        self.assertEqual(
            [item["carrier"]["selector"]["char_span"] for item in dispositions],
            [[0, 15], [15, 16]],
        )


class CheckboxGlyphEquivalenceTests(unittest.TestCase):
    def test_pua_checked_box_matches_the_provider_ballot_box(self) -> None:
        self.assertEqual(
            comparison_text("\uf052特定对象调研"),
            comparison_text("\u2611特定对象调研"),
        )

    def test_unknown_private_use_glyphs_stay_distinct(self) -> None:
        self.assertNotEqual(
            comparison_text("\uf053特定对象调研"),
            comparison_text("\u2611特定对象调研"),
        )

    def test_native_ballot_box_matches_the_provider_white_square(self) -> None:
        # cninfo investor-relations forms: ToUnicode yields U+2610 (ballot
        # box) for the empty checkbox the provider table renders as U+25A1.
        self.assertEqual(
            comparison_text("\u2610分析师会议"),
            comparison_text("\u25a1分析师会议"),
        )

    def test_checked_and_empty_boxes_stay_distinct(self) -> None:
        self.assertNotEqual(
            comparison_text("\u2610业绩说明会"),
            comparison_text("\u2611业绩说明会"),
        )


class SourceProofLocatorTests(unittest.TestCase):
    def test_proof_events_carry_every_proved_ledger_locator(self) -> None:
        items, ledger = _locator_case()
        validated = _validate(ledger, items)

        proof = source_evidence_proof_from_validated_ledger(
            ledger=validated,
            source_evidence_sha256=SOURCE_EVIDENCE_SHA256,
            visual_hashes={},
        )

        atoms = validated["atoms"]
        events = [event for page in proof.pages for event in page.events]
        self.assertEqual(len(events), len(atoms))
        mapped_selector_indices: list[int | None] = []
        native_layout_paths: list[tuple[int, ...]] = []
        for event in events:
            atom = atoms[event.atom_index]
            source = atom["source"]
            disposition = atom["disposition"]
            if isinstance(event, MappedSourceEvent):
                carrier = disposition["carrier"]
                selector = carrier["selector"]
                mapped_selector_indices.append(event.selector_index)
                self.assertEqual(
                    (
                        event.selector_field,
                        event.selector_index,
                        list(event.selector_char_span),
                        event.selector_value_sha256,
                        event.carrier_order,
                        list(event.carrier_bbox),
                        list(event.atom_bbox),
                    ),
                    (
                        selector["field"],
                        selector.get("index"),
                        selector["char_span"],
                        selector["value_sha256"],
                        carrier["order"],
                        [float(value) for value in carrier["bbox"]],
                        [float(value) for value in source["bbox"]],
                    ),
                )
                continue
            assert isinstance(event, NativeTextEvent)
            native_layout_paths.append(tuple(event.layout_path))
            self.assertEqual(list(event.layout_path), source["layout_path"])
        self.assertEqual(sorted(mapped_selector_indices, key=str), [0, None])
        self.assertEqual(native_layout_paths, [(0, 2, 0, 0)])

    def test_absent_selector_digest_cannot_default_into_a_proof(self) -> None:
        items, ledger = _locator_case()
        holed = deepcopy(dict(_validate(ledger, items)))
        mapped = next(
            atom
            for atom in holed["atoms"]
            if atom["disposition"]["kind"] == "mineru_carrier"
        )
        del mapped["disposition"]["carrier"]["selector"]["value_sha256"]

        with self.assertRaises(KeyError):
            source_evidence_proof_from_validated_ledger(
                ledger=holed,
                source_evidence_sha256=SOURCE_EVIDENCE_SHA256,
                visual_hashes={},
            )


class SourceProofGuardBindingTests(unittest.TestCase):
    def test_one_guard_crop_binds_a_multi_field_carrier_exactly_once(self) -> None:
        items, ledger = _guarded_table_case()
        self.assertEqual(
            [
                (
                    record["selector"]["source_item_index"],
                    record["selector"]["field"],
                    record["support"]["kind"],
                    record["support"]["artifact"]["artifact_role"],
                )
                for record in ledger["carrier_support"]
            ],
            [
                (0, "table_caption", "visual_bound", GUARD_ROLE),
                (0, "table_html", "visual_bound", GUARD_ROLE),
            ],
        )

        proof = _guard_proof(items, ledger)

        self.assertEqual(
            [
                (
                    binding.source_item_index,
                    binding.page_idx,
                    binding.kind,
                    binding.artifact.artifact_role,
                )
                for binding in proof.visual_bindings
            ],
            [(0, 0, "carrier_guard", GUARD_ROLE)],
        )

    def test_distinct_carriers_keep_their_own_guard_binding(self) -> None:
        items, ledger = _guarded_table_case(second_table=True)
        self.assertEqual(
            [
                (
                    record["selector"]["source_item_index"],
                    record["support"]["artifact"]["artifact_role"],
                )
                for record in ledger["carrier_support"]
            ],
            [(0, GUARD_ROLE), (0, GUARD_ROLE), (1, GUARD_ROLE), (1, GUARD_ROLE)],
        )

        proof = _guard_proof(items, ledger)

        self.assertEqual(
            [
                (
                    binding.source_item_index,
                    binding.page_idx,
                    binding.kind,
                    binding.artifact.artifact_role,
                )
                for binding in proof.visual_bindings
            ],
            [
                (0, 0, "carrier_guard", GUARD_ROLE),
                (1, 0, "carrier_guard", GUARD_ROLE),
            ],
        )


def _locator_case() -> tuple[list[dict[str, object]], dict[str, object]]:
    items: list[dict[str, object]] = [
        {
            "type": "table",
            "page_idx": 0,
            "bbox": [0, 0, 1000, 300],
            "table_caption": ["2.1 公司基本情况"],
            "table_body": "<table><tr><td>注册地</td></tr></table>",
            "table_footnote": [],
        }
    ]
    return items, _reconcile(
        items,
        _page(
            ("2.1 公司基本情况", (10, 10, 80, 22)),
            ("注册地", (10, 30, 40, 42)),
            ("未认领", (10, 50, 40, 62)),
        ),
    )


GUARD_ROLE = "source_bbox_visual_000001_000001"


def _guarded_table_case(
    *,
    second_table: bool = False,
) -> tuple[list[dict[str, object]], dict[str, Any]]:
    """Tables whose caption and HTML both miss the native text layer."""

    items: list[dict[str, object]] = [
        {
            "type": "table",
            "page_idx": 0,
            "bbox": [0, 700, 1000, 800],
            "table_caption": ["合并资产负债表"],
            "table_body": "<table><tr><td>货币资金</td></tr></table>",
            "table_footnote": [],
        }
    ]
    region = _region((0.0, 700.0, 1000.0, 800.0))
    if second_table:
        items.append(
            {
                "type": "table",
                "page_idx": 0,
                "bbox": [0, 780, 1000, 900],
                "table_caption": ["合并利润表"],
                "table_body": "<table><tr><td>营业收入</td></tr></table>",
                "table_footnote": [],
            }
        )
        region = _region((0.0, 700.0, 1000.0, 900.0))
    ledger: dict[str, Any] = _reconcile(
        items,
        _page(("原生正文", (10, 10, 80, 20))),
        visual_regions=(region,),
    )
    return items, ledger


def _guard_proof(
    items: list[dict[str, object]],
    ledger: Mapping[str, Any],
) -> SourceEvidenceProof:
    validated = _validate(ledger, items)
    return source_evidence_proof_from_validated_ledger(
        ledger=validated,
        source_evidence_sha256=SOURCE_EVIDENCE_SHA256,
        visual_hashes={
            role: str(descriptor["sha256"])
            for role, descriptor in source_visual_artifact_descriptors(
                validated
            ).items()
        },
    )


if __name__ == "__main__":
    unittest.main()
