"""Proof-bound document-unit builder invariants.

These tests deliberately avoid a second heading language.  Source carriers
remain parser-neutral; only a closed, PDF-bound ``structure_proof`` may define
section ownership.
"""

from __future__ import annotations

import hashlib
import unittest
from typing import Any
from unittest.mock import patch

from disclosure_anchor.application.services.unit_builder import retrieval_routing
from disclosure_anchor.application.services.unit_builder.builder import (
    ResolvedImageArtifact,
    SourceEvidenceClosureError,
    UnitDraft,
    build_unit_drafts_s1_s7,
)
from disclosure_anchor.application.contracts.document_structure import (
    DOCUMENT_STRUCTURE_ALGORITHM,
    DOCUMENT_STRUCTURE_VERSION,
    DocumentStructureContractError,
    carrier_set_sha256,
)
from disclosure_anchor.application.contracts.source_evidence import (
    SourceEvidenceProof,
    SourcePageProof,
    SourceProofIdentity,
)
from disclosure_anchor.application.services.document_unit_audit import (
    AuditDocumentMetadata,
    DocumentAuditReport,
)
from disclosure_anchor.application.services.unit_preparation import (
    prepare_and_audit_units,
)
from tests.unit.test_document_unit_audit import _ir as write_valid_ir


_SOURCE_PDF_SHA256 = "sha256:" + "a" * 64


def _element(
    index: int,
    *,
    kind: str = "text",
    raw_kind: str | None = None,
    text: str | None = None,
    page_no: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "document_id": "doc_builder",
        "ir_id": f"ir_{index:04d}",
        "source_item_index": index,
        "source_item_sha256": "sha256:"
        + hashlib.sha256(f"carrier:{index}".encode()).hexdigest(),
        "order_index": index,
        "page_idx": page_no - 1,
        "page_no": page_no,
        "bbox": [0.0, float(index * 10), 100.0, float(index * 10 + 8)],
        "kind": kind,
        "raw_kind": raw_kind or kind,
        **extra,
    }
    if text is not None:
        element["text"] = text
    return element


def _heading(
    node_id: int,
    source_index: int,
    *,
    text: str,
    section_end: int,
    parent_node_id: int | None = None,
    level: int = 1,
    propagates: bool = True,
    text_span: tuple[int, int] | None = None,
) -> dict[str, Any]:
    span = text_span or (0, len(text))
    return {
        "node_id": node_id,
        "parent_node_id": parent_node_id,
        "heading_level": level,
        "propagates": propagates,
        "evidence_kinds": ["mineru_v2_title"],
        "section_span": [source_index, section_end],
        "source_refs": [
            {
                "source_item_index": source_index,
                "field": "text",
                "text_span": list(span),
            }
        ],
    }


def _proof(
    elements: list[dict[str, Any]],
    *,
    headings: list[dict[str, Any]] | None = None,
    page_frames: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    heading_values = list(headings or [])
    frame_values = list(page_frames or [])
    return {
        "contract_version": DOCUMENT_STRUCTURE_VERSION,
        "algorithm_version": DOCUMENT_STRUCTURE_ALGORITHM,
        "source_pdf_sha256": _SOURCE_PDF_SHA256,
        "source_pdf_page_count": max(
            (int(element["page_no"]) for element in elements),
            default=1,
        ),
        "carrier_set_sha256": carrier_set_sha256(elements),
        "native": {
            "status": "untagged",
            "artifact_role": "pdf_structure",
        },
        "headings": heading_values,
        "page_frames": frame_values,
        "conflicts": [],
        "coverage": {
            "heading_nodes": len(heading_values),
            "page_frame_groups": len(frame_values),
        },
    }


def _build(
    elements: list[dict[str, Any]],
    *,
    headings: list[dict[str, Any]] | None = None,
    page_frames: list[dict[str, Any]] | None = None,
    filing_type: str = "annual_report",
    document_title: str | None = None,
) -> tuple[list[UnitDraft], Any]:
    normalized_ir: dict[str, Any] = {
        "contract_version": "normalized_ir.v4",
        "source_pdf_sha256": _SOURCE_PDF_SHA256,
        "elements": elements,
        "structure_proof": _proof(
            elements,
            headings=headings,
            page_frames=page_frames,
        ),
    }
    if document_title is not None:
        normalized_ir["title"] = document_title
    return build_unit_drafts_s1_s7(
        normalized_ir,
        filing_type=filing_type,
        image_artifact_resolver=lambda role, path: ResolvedImageArtifact(
            content=(content := f"fixture:{path}".encode()),
            artifact_role=role,
            sha256="sha256:" + hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type="image/png",
        ),
    )


def _replay_and_audit(
    elements: list[dict[str, Any]],
    *,
    headings: list[dict[str, Any]],
    page_count: int,
) -> tuple[list[UnitDraft], DocumentAuditReport]:
    """Replay one case through publication assembly and the independent audit.

    ``_build`` states only the proof facts a boundary case is about, while the
    audit additionally requires the closed v4 envelope publication validates.
    Cases that must stay publishable reuse the envelope the audit cases already
    maintain instead of restating it.
    """

    normalized_ir = write_valid_ir(elements, headings=headings)
    normalized_ir["document_id"] = str(elements[0]["document_id"])
    normalized_ir["source_pdf_page_count"] = page_count
    normalized_ir["structure_proof"]["source_pdf_page_count"] = page_count
    normalized_ir["parsed_pages"]["end_page_no"] = page_count
    artifacts = normalized_ir["parser_artifacts"]["files"]
    drafts, _stats, report = prepare_and_audit_units(
        normalized_ir=normalized_ir,
        filing_type="annual_report",
        metadata=AuditDocumentMetadata(
            document_id=str(normalized_ir["document_id"]),
            title=str(normalized_ir["title"]),
            filing_type="annual_report",
        ),
        image_artifact_resolver=None,
        image_hash_provider=dict,
        source_proof=SourceEvidenceProof(
            identity=SourceProofIdentity(
                source_evidence_sha256=str(artifacts["source_evidence"]["sha256"]),
                source_pdf_sha256=str(normalized_ir["source_pdf_sha256"]),
                page_count=page_count,
            ),
            pages=tuple(
                SourcePageProof(page_idx=page_idx, events=())
                for page_idx in range(page_count)
            ),
            retrieval_runs=(),
            visual_bindings=(),
            verified_visuals=(),
        ),
    )
    return drafts, report


def _sample_share_change() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    elements = [
        _element(0, text="第七节 股份变动及股东情况", text_level=1),
        _element(1, text="一、股份变动情况", text_level=2),
        _element(2, text="1、股份变动情况", text_level=3),
        _element(
            3,
            kind="table",
            raw_kind="table",
            table_caption=["单位：股"],
            table_footnote=[],
            table_html=(
                "<table><tr><td>股份总数</td><td>843,978,741</td></tr></table>"
            ),
            table={
                "headers": ["项目", "本次变动后"],
                "rows": [["股份总数", "843,978,741"]],
                "merged_cells": [],
            },
        ),
        _element(4, text="股份变动的原因"),
    ]
    headings = [
        _heading(
            1,
            0,
            text=str(elements[0]["text"]),
            section_end=4,
            level=1,
        ),
        _heading(
            2,
            1,
            text=str(elements[1]["text"]),
            section_end=4,
            parent_node_id=1,
            level=2,
        ),
        _heading(
            3,
            2,
            text=str(elements[2]["text"]),
            section_end=4,
            parent_node_id=2,
            level=3,
        ),
    ]
    return elements, headings


def _all_visible_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_all_visible_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(_all_visible_text(item) for item in value)
    return ""


def _source_indices(value: object) -> set[int]:
    found: set[int] = set()
    if isinstance(value, dict):
        source_index = value.get("source_item_index")
        if isinstance(source_index, int) and not isinstance(source_index, bool):
            found.add(source_index)
        for child in value.values():
            found.update(_source_indices(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_source_indices(child))
    return found


def _evidence_shape(unit: UnitDraft) -> tuple[object, ...]:
    return (
        unit.payload_kind,
        unit.payload,
        unit.source_order,
        unit.heading_path,
        unit.section_path,
        unit.title,
        unit.quality_status,
        unit.applicability,
        unit.artifact_locator,
        unit.detached_from_section,
    )


class BuilderBoundaryTests(unittest.TestCase):
    def test_structure_proof_is_required_and_bound_to_carriers(self) -> None:
        elements = [_element(0, text="来源事实")]
        with self.assertRaises(DocumentStructureContractError):
            build_unit_drafts_s1_s7(
                {
                    "contract_version": "normalized_ir.v4",
                    "source_pdf_sha256": _SOURCE_PDF_SHA256,
                    "elements": elements,
                },
                filing_type="other",
            )

        proof = _proof(elements)
        proof["carrier_set_sha256"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(
            DocumentStructureContractError,
            "carrier set hash differs",
        ):
            build_unit_drafts_s1_s7(
                {
                    "contract_version": "normalized_ir.v4",
                    "source_pdf_sha256": _SOURCE_PDF_SHA256,
                    "elements": elements,
                    "structure_proof": proof,
                },
                filing_type="other",
            )

    def test_routing_taxonomy_cannot_change_evidence_or_boundaries(self) -> None:
        elements, headings = _sample_share_change()
        baseline, _ = _build(elements, headings=headings)
        with (
            patch.object(
                retrieval_routing,
                "semantic_keys",
                return_value=["taxonomy_probe"],
            ),
            patch.object(
                retrieval_routing,
                "note_keys",
                return_value=["note_probe"],
            ),
        ):
            rerouted, _ = _build(elements, headings=headings)

        self.assertEqual(
            [_evidence_shape(unit) for unit in rerouted],
            [_evidence_shape(unit) for unit in baseline],
        )
        self.assertNotEqual(
            [unit.semantic_keys for unit in rerouted],
            [unit.semantic_keys for unit in baseline],
        )

    def test_future_carrier_fails_closed_instead_of_being_dropped(self) -> None:
        elements = [
            _element(
                0,
                kind="audio",
                raw_kind="future_audio",
                text="不得静默丢弃的未来载荷",
            )
        ]
        with self.assertRaisesRegex(
            SourceEvidenceClosureError,
            "unsupported NormalizedIR carrier kind",
        ):
            _build(elements)


class StructureProofProjectionTests(unittest.TestCase):
    def test_empty_visual_stays_in_mixed_section_with_sibling_evidence(self) -> None:
        elements = [
            _element(0, text="第一节 经营情况", text_level=1),
            _element(1, text="主营业务收入增长。"),
            _element(
                2,
                kind="image",
                raw_kind="image",
                image_path="images/" + "d" * 64 + ".png",
                image_caption=[],
                image_footnote=[],
            ),
            _element(
                3,
                kind="table",
                raw_kind="table",
                table_caption=["单位：万元"],
                table_footnote=[],
                table_html="<table><tr><td>收入</td><td>100</td></tr></table>",
                table={
                    "headers": [],
                    "rows": [["收入", "100"]],
                    "merged_cells": [],
                },
            ),
        ]
        headings = [
            _heading(
                1,
                0,
                text="第一节 经营情况",
                section_end=3,
                level=1,
            )
        ]

        units, _ = _build(elements, headings=headings)

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload_kind, "mixed")
        self.assertEqual(units[0].title, "第一节 经营情况")
        self.assertEqual(units[0].heading_path, ["第一节 经营情况"])
        self.assertEqual(units[0].quality_status, "needs_review")
        self.assertEqual(
            [part["kind"] for part in units[0].payload["parts"]],
            ["text", "image", "table"],
        )
        image_part = units[0].payload["parts"][1]
        self.assertEqual(image_part["caption"], "")
        self.assertEqual(image_part["visual_kind"], "image")
        self.assertEqual(image_part["quality_status"], "needs_review")

    def test_deepest_proven_heading_owns_table_and_caption_is_not_title(self) -> None:
        elements, headings = _sample_share_change()
        units, _ = _build(elements, headings=headings)

        self.assertEqual(len(units), 1)
        section = units[0]
        self.assertEqual(section.payload_kind, "mixed")
        self.assertEqual(section.title, "1、股份变动情况")
        self.assertEqual(
            section.heading_path,
            [
                "第七节 股份变动及股东情况",
                "一、股份变动情况",
                "1、股份变动情况",
            ],
        )
        table_part = next(
            part for part in section.payload["parts"] if part["kind"] == "table"
        )
        self.assertEqual(table_part["caption"], ["单位：股"])
        self.assertNotEqual(section.title, "单位：股")
        self.assertEqual(
            _source_indices(
                {
                    "payload": section.payload,
                    "locator": section.artifact_locator,
                }
            ),
            set(range(5)),
        )

    def test_cross_page_tables_share_proved_context_without_rewriting_cells(
        self,
    ) -> None:
        elements = [
            _element(0, text="第七节 股份变动及股东情况", text_level=1),
            _element(1, text="一、股份变动情况", text_level=2),
            _element(2, text="1、股份变动情况", text_level=3),
            _element(
                3,
                kind="table",
                raw_kind="table",
                page_no=1,
                image_path="images/page_24_table.png",
                table_caption=["单位：股"],
                table_footnote=[],
                table_html="<table><tr><td>4、其</td><td></td></tr></table>",
                table={
                    "headers": [],
                    "rows": [["4、其", ""]],
                    "merged_cells": [],
                },
            ),
            _element(
                4,
                kind="table",
                raw_kind="table",
                page_no=2,
                image_path="images/page_25_table.png",
                table_caption=[],
                table_footnote=[],
                table_html="<table><tr><td>他</td><td></td></tr></table>",
                table={
                    "headers": [],
                    "rows": [["他", ""]],
                    "merged_cells": [],
                },
            ),
            _element(5, page_no=2, text="股份变动的原因"),
        ]
        headings = [
            _heading(1, 0, text=str(elements[0]["text"]), section_end=5, level=1),
            _heading(
                2,
                1,
                text=str(elements[1]["text"]),
                section_end=5,
                parent_node_id=1,
                level=2,
            ),
            _heading(
                3,
                2,
                text=str(elements[2]["text"]),
                section_end=5,
                parent_node_id=2,
                level=3,
            ),
        ]

        units, _ = _build(elements, headings=headings)

        self.assertEqual(len(units), 1)
        section = units[0]
        self.assertEqual(section.title, "1、股份变动情况")
        self.assertEqual(
            section.heading_path,
            [
                "第七节 股份变动及股东情况",
                "一、股份变动情况",
                "1、股份变动情况",
            ],
        )
        parts = section.payload["parts"]
        self.assertEqual([part["kind"] for part in parts], ["table", "table", "text"])
        self.assertEqual(parts[0]["rows"], [["4、其", ""]])
        self.assertEqual(parts[1]["rows"], [["他", ""]])
        self.assertEqual(parts[0]["caption"], ["单位：股"])
        self.assertNotEqual(section.title, "单位：股")
        self.assertNotIn("4、其他", _all_visible_text(section.payload))
        self.assertEqual(
            [
                part["artifact_locator"]["evidence_artifacts"][0]["artifact_role"]
                for part in parts[:2]
            ],
            ["evidence_image_000003", "evidence_image_000004"],
        )

    def test_heading_like_text_never_opens_sections_without_proof(self) -> None:
        elements, _ = _sample_share_change()
        units, _ = _build(elements)

        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertIsNone(table.title)
        self.assertEqual(table.heading_path, [])
        visible = _all_visible_text([unit.payload for unit in units])
        for expected in (
            "第七节 股份变动及股东情况",
            "一、股份变动情况",
            "1、股份变动情况",
            "单位：股",
            "股份变动的原因",
        ):
            self.assertIn(expected, visible)

    def test_equal_heading_text_occurrences_keep_distinct_identity(self) -> None:
        elements = [
            _element(0, text="风险提示"),
            _element(1, text="甲事实"),
            _element(2, text="风险提示"),
            _element(3, text="乙事实"),
        ]
        headings = [
            _heading(1, 0, text="风险提示", section_end=1),
            _heading(2, 2, text="风险提示", section_end=3),
        ]
        units, _ = _build(elements, headings=headings)

        self.assertEqual([unit.title for unit in units], ["风险提示", "风险提示"])
        self.assertEqual([unit.section_path for unit in units], [[1], [2]])
        self.assertEqual(
            [unit.payload["text"] for unit in units],
            ["甲事实", "乙事实"],
        )

    def test_heading_only_and_partial_span_evidence_remain_searchable(self) -> None:
        heading_only = [_element(0, text="仅有标题")]
        units, stats = _build(
            heading_only,
            headings=[_heading(1, 0, text="仅有标题", section_end=0)],
        )
        self.assertEqual(units[0].payload["text"], "仅有标题")
        self.assertEqual(stats.heading_only_carriers_preserved, 1)

        partial = [_element(0, text="第一节\n同一载荷中的正文")]
        units, _ = _build(
            partial,
            headings=[
                _heading(
                    1,
                    0,
                    text=str(partial[0]["text"]),
                    section_end=0,
                    text_span=(0, 3),
                )
            ],
        )
        self.assertEqual(units[0].title, "第一节")
        self.assertIn("同一载荷中的正文", units[0].payload["text"])

    def test_anchor_only_provider_title_inherits_coarser_section(self) -> None:
        elements = [
            _element(0, text="原生章节"),
            _element(1, text="模型标题候选"),
            _element(2, text="关键事实"),
        ]
        units, _ = _build(
            elements,
            headings=[
                _heading(1, 0, text="原生章节", section_end=2),
                _heading(
                    2,
                    1,
                    text="模型标题候选",
                    section_end=1,
                    propagates=False,
                ),
            ],
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].heading_path, ["原生章节"])
        self.assertEqual(units[0].title, "原生章节")
        self.assertEqual(
            units[0].payload["text"],
            "模型标题候选\n关键事实",
        )

    def test_unheaded_prelude_remains_searchable_without_invented_title(self) -> None:
        elements = [
            _element(0, text="公告封面原始说明"),
            _element(1, text="第一节 正文"),
            _element(2, text="业务事实"),
        ]
        units, _ = _build(
            elements,
            headings=[_heading(1, 1, text="第一节 正文", section_end=2)],
        )

        self.assertEqual(units[0].payload["text"], "公告封面原始说明")
        self.assertIsNone(units[0].title)
        self.assertEqual(units[1].title, "第一节 正文")

    def test_registered_title_cannot_change_unheaded_boundaries(self) -> None:
        elements = [
            _element(0, text="正文说明"),
            _element(
                1,
                kind="table",
                raw_kind="table",
                table_caption=["单位：股"],
                table_footnote=[],
                table_html="<table><tr><td>股份总数</td></tr></table>",
                table={
                    "headers": [],
                    "rows": [["股份总数"]],
                    "merged_cells": [],
                },
            ),
            _element(2, text="表后解释"),
        ]

        untitled, _ = _build(elements)
        titled, _ = _build(
            elements,
            document_title="股份变动公告",
        )

        def boundary_signature(unit: UnitDraft) -> tuple[object, ...]:
            return (
                unit.payload_kind,
                unit.source_order,
                unit.heading_path,
                unit.section_path,
                unit.payload,
            )

        self.assertEqual(len(untitled), 3)
        self.assertEqual(
            [boundary_signature(unit) for unit in titled],
            [boundary_signature(unit) for unit in untitled],
        )
        self.assertEqual(
            [unit.payload_kind for unit in titled],
            ["text", "table", "text"],
        )
        self.assertTrue(all(unit.title == "股份变动公告" for unit in titled))
        self.assertTrue(all(unit.title is None for unit in untitled))
        self.assertEqual(
            titled[1].payload["caption"],
            ["单位：股"],
        )

    def test_cross_page_qa_follows_proved_occurrences_not_lexical_markers(
        self,
    ) -> None:
        elements = [
            _element(0, text="互动问答", page_no=1),
            _element(1, text="问题一：请说明本期变化。", page_no=1),
            _element(
                2,
                kind="table",
                raw_kind="table",
                page_no=1,
                table_caption=["单位：万元"],
                table_footnote=[],
                table_html="<table><tr><td>本期</td><td>100</td></tr></table>",
                table={
                    "headers": [],
                    "rows": [["本期", "100"]],
                    "merged_cells": [],
                },
            ),
            _element(3, text="回复：变化来自主营业务增长。", page_no=2),
            _element(4, text="声明：上述数据以审计结果为准。", page_no=2),
            _element(5, text="问题二", page_no=2),
            _element(6, text="请说明下一事项。", page_no=2),
        ]
        headings = [
            _heading(1, 0, text="互动问答", section_end=6),
            _heading(
                2,
                5,
                text="问题二",
                section_end=6,
                parent_node_id=1,
                level=2,
            ),
        ]

        units, _ = _build(elements, headings=headings)

        self.assertEqual([unit.section_path for unit in units], [[1], [1, 2]])
        self.assertEqual([unit.title for unit in units], ["互动问答", "问题二"])
        self.assertEqual(units[0].payload_kind, "mixed")
        self.assertEqual(
            [part["kind"] for part in units[0].payload["parts"]],
            ["text", "table", "text"],
        )
        self.assertIn(
            "回复：变化来自主营业务增长。", _all_visible_text(units[0].payload)
        )
        self.assertIn(
            "声明：上述数据以审计结果为准。", _all_visible_text(units[0].payload)
        )
        self.assertEqual(
            _source_indices(
                {
                    "payload": units[0].payload,
                    "locator": units[0].artifact_locator,
                }
            ),
            set(range(5)),
        )
        self.assertEqual(units[1].payload["text"], "请说明下一事项。")


class ConservationTests(unittest.TestCase):
    def test_text_coalescing_is_ordered_and_locator_complete(self) -> None:
        elements = [
            _element(0, text="章节"),
            _element(1, text="甲"),
            _element(2, text="乙"),
            _element(3, text="丙"),
        ]
        units, _ = _build(
            elements,
            headings=[_heading(1, 0, text="章节", section_end=3)],
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload["text"], "甲\n乙\n丙")
        self.assertEqual(
            _source_indices(units[0].artifact_locator),
            {0, 1, 2, 3},
        )

    def test_proven_page_frame_deduplicates_only_its_members(self) -> None:
        elements = [
            _element(
                0,
                kind="page_furniture",
                raw_kind="header",
                text="重复页眉",
                page_no=1,
            ),
            _element(1, text="第一页事实", page_no=1),
            _element(
                2,
                kind="page_furniture",
                raw_kind="header",
                text="重复页眉",
                page_no=2,
            ),
            _element(3, text="第二页事实", page_no=2),
        ]
        frames = [
            {
                "group_id": f"frame_{number}",
                "role": "running_furniture",
                "proof_kind": "native_artifact",
                "member_source_item_indices": [source],
                "representative_source_item_index": source,
            }
            for number, source in enumerate((0, 2), start=1)
        ]
        proved, stats = _build(elements, page_frames=frames)
        visible = _all_visible_text([unit.payload for unit in proved])
        self.assertEqual(visible.count("重复页眉"), 0)
        self.assertEqual(stats.dropped_by_kind["proven_page_frame_externalized"], 2)
        self.assertEqual(
            [
                (item["source_item_index"], item["role"], item["reason"])
                for item in stats.source_dispositions
            ],
            [
                (0, "external_metadata", "proven_running_furniture"),
                (2, "external_metadata", "proven_running_furniture"),
            ],
        )

        unproved, stats = _build(elements)
        self.assertEqual(
            _all_visible_text([unit.payload for unit in unproved]).count("重复页眉"),
            2,
        )
        self.assertEqual(stats.dropped_by_kind["proven_page_frame_externalized"], 0)
        self.assertTrue(all(unit.heading_path == [] for unit in unproved))

    def test_unproved_page_furniture_is_neutral_inside_one_section(self) -> None:
        elements = [
            _element(0, text="章节", text_level=1, page_no=1),
            _element(1, text="前段事实", page_no=1),
            _element(
                2,
                kind="page_furniture",
                raw_kind="header",
                text="未证明页框",
                page_no=2,
            ),
            _element(3, text="后段事实", page_no=2),
        ]
        units, _ = _build(
            elements,
            headings=[_heading(1, 0, text="章节", section_end=4)],
        )

        self.assertEqual(len(units), 1)
        unit = units[0]
        self.assertEqual(unit.heading_path, ["章节"])
        self.assertEqual(unit.section_path, [1])
        self.assertEqual(
            [part.get("text") for part in unit.payload["parts"]],
            ["前段事实", "未证明页框", "后段事实"],
        )
        self.assertEqual(
            [part.get("role") for part in unit.payload["parts"]],
            [None, None, None],
        )
        self.assertNotIn("heading_path", unit.payload["parts"][1])
        self.assertEqual(
            _source_indices(
                {
                    "payload": unit.payload,
                    "locator": unit.artifact_locator,
                }
            ),
            {0, 1, 2, 3},
        )

    def test_neutral_furniture_does_not_merge_distinct_sections(self) -> None:
        elements = [
            _element(0, text="甲节", page_no=1),
            _element(1, text="甲事实", page_no=1),
            _element(
                2,
                kind="page_furniture",
                raw_kind="header",
                text="未证明页框",
                page_no=2,
            ),
            _element(3, text="乙节", page_no=2),
            _element(4, text="乙事实", page_no=2),
        ]
        units, _ = _build(
            elements,
            headings=[
                _heading(1, 0, text="甲节", section_end=2),
                _heading(2, 3, text="乙节", section_end=4),
            ],
        )

        self.assertEqual([unit.heading_path for unit in units], [["甲节"], ["乙节"]])
        self.assertEqual(
            [part.get("role") for part in units[0].payload["parts"]],
            [None, None],
        )
        self.assertEqual(units[1].payload["text"], "乙事实")

    def test_proved_empty_section_never_binds_its_page_furniture(self) -> None:
        elements = [
            _element(0, text="27、生物资产", text_level=1, page_no=1),
            _element(
                1,
                kind="page_furniture",
                raw_kind="header",
                text="某某股份有限公司 2024 年年度报告",
                page_no=2,
            ),
            _element(
                2,
                kind="page_furniture",
                raw_kind="page_number",
                text="第 128 页",
                page_no=2,
            ),
            _element(3, text="28、油气资产", text_level=1, page_no=2),
            _element(4, text="本期无油气资产。", page_no=2),
        ]
        headings = [
            _heading(1, 0, text="27、生物资产", section_end=2),
            _heading(2, 3, text="28、油气资产", section_end=4),
        ]

        units, report = _replay_and_audit(
            elements,
            headings=headings,
            page_count=2,
        )

        self.assertEqual(
            [(unit.payload_kind, unit.heading_path) for unit in units],
            [
                ("text", ["27、生物资产"]),
                ("text", []),
                ("text", []),
                ("text", ["28、油气资产"]),
            ],
        )
        self.assertEqual(units[0].payload, {"text": "27、生物资产"})
        self.assertEqual(units[0].section_path, [1])
        self.assertEqual(
            [unit.payload["text"] for unit in units[1:3]],
            ["某某股份有限公司 2024 年年度报告", "第 128 页"],
        )
        self.assertEqual([unit.section_path for unit in units[1:3]], [[], []])
        self.assertTrue(all(unit.detached_from_section for unit in units[1:3]))
        self.assertEqual(units[3].payload, {"text": "本期无油气资产。"})
        self.assertTrue(report.ok, report.findings)

    def test_cross_page_furniture_joins_the_section_its_content_proves(
        self,
    ) -> None:
        elements = [
            _element(0, text="七、合并财务报表项目注释", text_level=1, page_no=1),
            _element(1, text="27、生物资产", text_level=2, page_no=1),
            _element(
                2,
                kind="page_furniture",
                raw_kind="header",
                text="某某股份有限公司 2024 年年度报告",
                page_no=2,
            ),
            _element(3, text="本期无生物资产。", page_no=2),
        ]
        headings = [
            _heading(1, 0, text="七、合并财务报表项目注释", section_end=3),
            _heading(
                2,
                1,
                text="27、生物资产",
                section_end=1,
                parent_node_id=1,
                level=2,
            ),
        ]

        units, report = _replay_and_audit(
            elements,
            headings=headings,
            page_count=2,
        )

        self.assertEqual(len(units), 1)
        unit = units[0]
        self.assertEqual(unit.payload_kind, "mixed")
        self.assertEqual(unit.payload["semantic_type"], "section")
        self.assertEqual(unit.heading_path, ["七、合并财务报表项目注释"])
        self.assertEqual(unit.section_path, [1])
        self.assertEqual(
            [part.get("text") for part in unit.payload["parts"]],
            [
                "27、生物资产",
                "某某股份有限公司 2024 年年度报告",
                "本期无生物资产。",
            ],
        )
        self.assertEqual(
            [part.get("heading_path") for part in unit.payload["parts"]],
            [
                ["七、合并财务报表项目注释", "27、生物资产"],
                None,
                ["七、合并财务报表项目注释"],
            ],
        )
        self.assertTrue(report.ok, report.findings)

    def test_empty_child_headings_are_flat_parts_of_parent_occurrence(self) -> None:
        elements = [
            _element(0, text="父节"),
            _element(1, text="父节事实"),
            _element(2, text="空子节甲"),
            _element(3, text="空子节乙"),
        ]
        units, stats = _build(
            elements,
            headings=[
                _heading(1, 0, text="父节", section_end=3),
                _heading(
                    2,
                    2,
                    text="空子节甲",
                    section_end=2,
                    parent_node_id=1,
                    level=2,
                ),
                _heading(
                    3,
                    3,
                    text="空子节乙",
                    section_end=3,
                    parent_node_id=1,
                    level=2,
                ),
            ],
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].heading_path, ["父节"])
        parts = units[0].payload["parts"]
        self.assertTrue(all(part["kind"] != "mixed" for part in parts))
        self.assertEqual(
            [part["heading_path"] for part in parts[1:]],
            [["父节", "空子节甲"], ["父节", "空子节乙"]],
        )
        self.assertTrue(
            all("role" not in part and "title" not in part for part in parts)
        )
        self.assertEqual(stats.heading_outline_units_generated, 1)

    def test_unsupported_unknown_carrier_fails_closed(self) -> None:
        elements = [
            _element(0, kind="unknown", raw_kind="mystery", text="未知但可读事实")
        ]
        with self.assertRaisesRegex(
            SourceEvidenceClosureError,
            "unsupported NormalizedIR carrier kind",
        ):
            _build(elements)

    def test_typed_carriers_preserve_their_structured_payload(self) -> None:
        image_name = "images/" + "b" * 64 + ".png"
        elements = [
            _element(
                0,
                kind="text",
                raw_kind="list",
                text="第一项\n第二项",
                list_items=["第一项", "第二项"],
                list_subtype="ordered",
            ),
            _element(
                1,
                kind="text",
                raw_kind="code",
                text="算法\nreturn 1\n注释",
                code_body="return 1",
                code_caption=["算法"],
                code_footnote=["注释"],
                code_subtype="algorithm",
            ),
            _element(
                2,
                kind="equation",
                raw_kind="equation",
                text="x=1",
                text_format="latex",
            ),
            _element(
                3,
                kind="image",
                raw_kind="image",
                image_path=image_name,
                image_caption=["图一"],
                image_footnote=["图注"],
            ),
            _element(
                4,
                kind="image",
                raw_kind="chart",
                image_path="images/" + "c" * 64 + ".png",
                text="收入 10",
                image_caption=["收入图"],
                image_footnote=[],
                visual_subtype="bar",
            ),
        ]
        units, _ = _build(elements)
        by_order = {unit.source_order: unit for unit in units}

        self.assertEqual(by_order[0].payload["list_items"], ["第一项", "第二项"])
        self.assertEqual(by_order[1].payload["code_body"], "return 1")
        self.assertEqual(by_order[2].payload["text_format"], "latex")
        expected_image_digest = hashlib.sha256(
            f"fixture:{image_name}".encode()
        ).hexdigest()
        self.assertEqual(
            by_order[3].payload["image_ref"],
            f"images/{expected_image_digest}.png",
        )
        self.assertEqual(by_order[3].payload["notes"], ["图注"])
        self.assertEqual(by_order[4].payload["visual_kind"], "chart")
        self.assertEqual(by_order[4].payload["visual_subtype"], "bar")

    def test_image_bytes_without_recognized_text_remain_reviewable(self) -> None:
        elements = [
            _element(
                0,
                kind="image",
                raw_kind="image",
                image_path="images/" + "d" * 64 + ".png",
                image_caption=[],
                image_footnote=[],
            )
        ]

        units, _ = _build(elements)

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload["caption"], "")
        self.assertEqual(units[0].payload["visual_kind"], "image")
        self.assertTrue(units[0].payload["image_ref"].startswith("images/"))
        self.assertEqual(units[0].quality_status, "needs_review")


class TablePayloadTests(unittest.TestCase):
    def test_visual_only_table_remains_a_reviewable_evidence_unit(self) -> None:
        elements = [
            _element(
                0,
                kind="table",
                raw_kind="table",
                image_path="images/table.png",
                table_caption=[],
                table_footnote=[],
                table_html="",
                table={"headers": [], "rows": []},
            )
        ]

        units, stats = _build(elements)

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload_kind, "table")
        self.assertEqual(units[0].payload["rows"], [])
        self.assertEqual(units[0].quality_status, "needs_review")
        self.assertEqual(stats.dropped_by_kind["table_empty"], 0)
        artifacts = units[0].artifact_locator["evidence_artifacts"]
        self.assertEqual(artifacts[0]["artifact_role"], "evidence_image_000000")

    def test_table_media_occurrences_publish_separate_content_addressed_edges(
        self,
    ) -> None:
        elements = [
            _element(
                0,
                kind="table",
                raw_kind="table",
                image_path="images/table.png",
                table_caption=[],
                table_footnote=[],
                table_html=(
                    '<table><tr><td>值<img src="images/shared.png"/>'
                    '<img src="images/shared.png"/></td></tr></table>'
                ),
                table={
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
                            "occurrence_index": occurrence,
                            "cell_media_index": occurrence,
                            "row": 0,
                            "col": 0,
                            "rowspan": 1,
                            "colspan": 1,
                            "image_path": "images/shared.png",
                            "artifact_role": (
                                f"evidence_table_media_000000_{occurrence:06d}"
                            ),
                        }
                        for occurrence in range(2)
                    ],
                },
            )
        ]

        drafts, _ = _build(elements)
        self.assertEqual(len(drafts), 1)
        media = drafts[0].payload["embedded_media"]
        self.assertEqual([item["occurrence_index"] for item in media], [0, 1])
        self.assertEqual(media[0]["image_ref"], media[1]["image_ref"])
        locator = drafts[0].artifact_locator
        assert locator is not None
        self.assertEqual(
            [item["artifact_role"] for item in locator["evidence_artifacts"]],
            [
                "evidence_image_000000",
                "evidence_table_media_000000_000000",
                "evidence_table_media_000000_000001",
            ],
        )

    def test_unheaded_table_never_uses_caption_as_title(self) -> None:
        elements = [
            _element(
                0,
                kind="table",
                raw_kind="table",
                table_caption=["单位：万元"],
                table_footnote=["注：未经审计"],
                table_html="<table><tr><td>收入</td><td>10</td></tr></table>",
                table={
                    "headers": [],
                    "rows": [["收入", "10"]],
                    "merged_cells": [{"row": 0, "col": 0, "rowspan": 1, "colspan": 2}],
                },
            )
        ]
        units, _ = _build(elements)
        table = units[0]

        self.assertIsNone(table.title)
        self.assertEqual(table.heading_path, [])
        self.assertEqual(table.payload["caption"], ["单位：万元"])
        self.assertEqual(table.payload["notes"], ["注：未经审计"])
        self.assertEqual(table.payload["rows"], [["收入", "10"]])
        self.assertEqual(
            table.payload["merged_cells"],
            [{"row": 0, "col": 0, "rowspan": 1, "colspan": 2}],
        )

    def test_unreconciled_table_html_fails_closed(self) -> None:
        for table_html in (
            "<table><tr><td>仍可检索</td></tr></table>",
            "<table></table>",
        ):
            elements = [
                _element(
                    0,
                    kind="table",
                    raw_kind="table",
                    table_caption=[],
                    table_footnote=[],
                    table_html=table_html,
                    table={"headers": [], "rows": [], "merged_cells": []},
                )
            ]
            with (
                self.subTest(table_html=table_html),
                self.assertRaisesRegex(
                    SourceEvidenceClosureError,
                    "no reconciled logical grid",
                ),
            ):
                _build(elements)


if __name__ == "__main__":
    unittest.main()
