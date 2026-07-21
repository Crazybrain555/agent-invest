from __future__ import annotations

import copy
from dataclasses import replace
import unittest
from typing import Any

from disclosure_anchor.adapters.unit_builder.builder import (
    UnitDraft,
    build_unit_drafts_s1_s7,
)
from disclosure_anchor.application.services.document_unit_audit import (
    AuditDocumentMetadata,
    AuditUnitView,
    audit_document,
)


def _element(
    order: int,
    *,
    kind: str,
    text: str | None = None,
    raw_kind: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "document_id": "doc_audit",
        "ir_id": f"ir_{order:04d}",
        "source_item_index": order,
        "order_index": order,
        "page_idx": 0,
        "page_no": 1,
        "kind": kind,
        "raw_kind": raw_kind or kind,
        **extra,
    }
    if text is not None:
        value["text"] = text
    return value


def _ir(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "contract_version": "normalized_ir.v2",
        "created_at": "2026-07-16T00:00:00Z",
        "document_id": "doc_audit",
        "source_pdf": "raw/audit.pdf",
        "title": "审计样本",
        "parser": {
            "name": "MinerU",
            "package_version": "3.4.0",
            "backend": "pipeline",
            "method": "auto",
            "language": "ch",
            "formula": False,
            "table": True,
        },
        "parser_artifacts": {
            "artifact_root_relpath": "parser/audit",
            "content_list_relpath": "parser/audit/content.json",
        },
        "parsed_pages": {
            "start_page_no": 1,
            "end_page_no": 1,
            "full_pdf": True,
        },
        "elements": elements,
    }


def _views(units: list[UnitDraft]) -> list[AuditUnitView]:
    return [
        AuditUnitView(
            order_index=index,
            payload_kind=unit.payload_kind,
            payload=unit.payload,
            title=unit.title,
            heading_path=unit.heading_path,
            structural_path=unit.structural_path,
            semantic_key=unit.semantic_key,
            semantic_keys=unit.semantic_keys,
            quality_status=unit.quality_status,
            applicability=unit.applicability,
            artifact_locator=unit.artifact_locator,
        )
        for index, unit in enumerate(units, start=1)
    ]


def _codes(report: Any) -> set[str]:
    return {item.code for item in report.findings}


class DocumentUnitAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = AuditDocumentMetadata(
            document_id="doc_audit",
            title="审计样本",
            filing_type="other",
        )

    def test_blank_table_annotations_are_proven_empty_source(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="table",
                    raw_kind="table",
                    table={"headers": [], "rows": []},
                    table_html="",
                    table_caption=[],
                    table_footnote=["", "  "],
                )
            ]
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[],
            metadata=self.metadata,
        )

        self.assertEqual(report.metrics["coverage"]["proven_empty"], 1)
        self.assertNotIn(
            "source_atom_uncovered", {item.code for item in report.findings}
        )

    def test_builder_output_conserves_heading_and_text_by_source_identity(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="heading",
                    raw_kind="text",
                    text="一、经营情况",
                    heading_level=1,
                ),
                _element(1, kind="text", text="营业收入同比增长20%。"),
            ]
        )
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )

        report = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=self.metadata,
        )

        self.assertTrue(report.ok, report.findings)
        self.assertEqual(report.metrics["coverage"]["payload"], 1)
        self.assertEqual(report.metrics["coverage"]["structure"], 1)
        self.assertEqual(report.metrics["typed_payload_projections"], 1)
        self.assertEqual(report.metrics["source_text_chars"], 0)
        self.assertEqual(
            report.metrics["source_text_chars"],
            report.metrics["output_text_chars"],
        )

    def test_repeated_heading_text_cannot_cover_an_unreferenced_occurrence(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="heading",
                    raw_kind="text",
                    text="风险提示",
                    heading_level=1,
                ),
                _element(
                    1,
                    kind="heading",
                    raw_kind="text",
                    text="风险提示",
                    heading_level=1,
                ),
            ]
        )
        unit = AuditUnitView(
            order_index=1,
            payload_kind="text",
            payload={"text": "风险提示"},
            title="风险提示",
            heading_path=["风险提示"],
            structural_path=["风险提示"],
            semantic_key="document_content",
            semantic_keys=["document_content"],
            quality_status="ok",
            applicability=None,
            artifact_locator={
                "ir_id": "ir_0000",
                "source_item_index": 0,
                "order_index": 0,
                "page_no": 1,
            },
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[unit],
            metadata=self.metadata,
        )

        self.assertFalse(report.ok)
        self.assertIn("source_atom_uncovered", {item.code for item in report.findings})

    def test_exact_registered_metadata_can_be_covered_outside_payload(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="text",
                    raw_kind="header",
                    text="证券代码：300012",
                ),
                _element(1, kind="text", text="公司经营保持稳定。"),
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="华测检测：审计样本",
            security_code="300012",
            security_name="华测检测",
        )

        report = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=AuditDocumentMetadata(
                document_id="doc_audit",
                title="华测检测：审计样本",
                filing_type="other",
                security_code="300012",
                security_name="华测检测",
            ),
            source_dispositions=stats.source_dispositions,
        )

        self.assertTrue(report.ok, report.findings)
        self.assertEqual(report.metrics["coverage"]["external_metadata"], 1)

    def test_typed_projection_detects_payload_loss(self) -> None:
        normalized = _ir([_element(0, kind="text", text="完整来源事实。")])
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )
        broken = replace(units[0], payload={"text": "来源事实。"})

        report = audit_document(
            normalized_ir=normalized,
            units=_views([broken]),
            metadata=self.metadata,
        )

        self.assertIn("payload_projection_mismatch", _codes(report))

    def test_legacy_text_component_still_detects_payload_loss(self) -> None:
        normalized = _ir([_element(0, kind="text", text="完整来源事实。")])
        unit = AuditUnitView(
            order_index=1,
            payload_kind="text",
            payload={"text": "来源事实。"},
            title="审计样本",
            heading_path=["审计样本"],
            structural_path=[],
            semantic_key="document_content",
            semantic_keys=["document_content"],
            quality_status="ok",
            applicability=None,
            artifact_locator={
                "ir_id": "ir_0000",
                "source_item_index": 0,
                "order_index": 0,
                "page_no": 1,
            },
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[unit],
            metadata=self.metadata,
        )

        self.assertIn("text_component_mismatch", _codes(report))

    def test_headerless_table_caption_is_a_typed_source_anchor(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="table",
                    table={"headers": ["项目"], "rows": [["研发"]]},
                    table_caption=["募集资金使用表"],
                    table_footnote=[],
                )
            ]
        )
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title=None,
        )

        report = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=replace(self.metadata, title=None),
        )

        self.assertTrue(report.ok, report.findings)
        self.assertEqual(units[0].heading_path, ["募集资金使用表"])

    def test_untyped_headerless_anchor_is_rejected(self) -> None:
        normalized = _ir([_element(0, kind="text", text="来源事实。")])
        unit = AuditUnitView(
            order_index=1,
            payload_kind="text",
            payload={"text": "来源事实。"},
            title="任意锚点",
            heading_path=["任意锚点"],
            structural_path=[],
            semantic_key="document_content",
            semantic_keys=["document_content"],
            quality_status="ok",
            applicability=None,
            artifact_locator={
                "ir_id": "ir_0000",
                "source_item_index": 0,
                "order_index": 0,
                "page_no": 1,
            },
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[unit],
            metadata=self.metadata,
        )

        self.assertIn("public_heading_path_mismatch", _codes(report))

    def test_table_spans_are_checked_as_source_structure(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="table",
                    table={
                        "headers": ["项目", "金额"],
                        "rows": [["收入", "100"]],
                        "merged_cells": [
                            {"row": 0, "col": 0, "rowspan": 1, "colspan": 2}
                        ],
                    },
                    table_caption=[],
                    table_footnote=[],
                )
            ]
        )
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )
        valid = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=self.metadata,
        )
        self.assertTrue(valid.ok, valid.findings)

        broken = replace(
            units[0],
            payload={**units[0].payload, "merged_cells": []},
        )
        invalid = audit_document(
            normalized_ir=normalized,
            units=_views([broken]),
            metadata=self.metadata,
        )
        self.assertIn(
            "table_structure_mismatch",
            {item.code for item in invalid.findings},
        )

    def test_bad_navigation_locator_does_not_override_typed_ownership(self) -> None:
        normalized = _ir([_element(0, kind="text", text="唯一事实。")])
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )
        locator = dict(units[0].artifact_locator or {})
        locator["ir_id"] = "does_not_exist"
        broken = replace(units[0], artifact_locator=locator)

        report = audit_document(
            normalized_ir=normalized,
            units=_views([broken]),
            metadata=self.metadata,
        )

        self.assertIn("locator_identity_unresolved", _codes(report))
        self.assertNotIn("source_atom_uncovered", _codes(report))

    def test_bad_typed_source_cannot_be_rescued_by_navigation_locator(self) -> None:
        normalized = _ir([_element(0, kind="text", text="唯一事实。")])
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )
        locator = copy.deepcopy(units[0].artifact_locator or {})
        locator["source_projection"]["payload"]["sources"][0]["source"][
            "ir_id"
        ] = "does_not_exist"
        broken = replace(units[0], artifact_locator=locator)

        report = audit_document(
            normalized_ir=normalized,
            units=_views([broken]),
            metadata=self.metadata,
        )

        self.assertIn("source_ref_identity_invalid", _codes(report))
        self.assertIn("source_atom_uncovered", _codes(report))

    def test_public_heading_path_cannot_hide_behind_private_structure(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="heading",
                    text="一、经营情况",
                    heading_level=1,
                ),
                _element(1, kind="text", text="经营保持稳定。"),
            ]
        )
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )
        broken = replace(units[0], heading_path=[])

        report = audit_document(
            normalized_ir=normalized,
            units=_views([broken]),
            metadata=self.metadata,
        )

        self.assertIn("public_heading_path_mismatch", _codes(report))
        self.assertIn("heading_source_path_mismatch", _codes(report))

    def test_unit_source_order_cannot_move_backwards(self) -> None:
        normalized = _ir(
            [
                _element(0, kind="text", text="第一段。"),
                _element(1, kind="text", text="第二段。"),
            ]
        )
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title=None,
        )
        self.assertEqual(len(units), 1)
        first = replace(
            units[0],
            payload={"text": "第一段。"},
            artifact_locator={
                "ir_id": "ir_0000",
                "source_item_index": 0,
                "order_index": 0,
                "page_no": 1,
            },
        )
        second = replace(
            units[0],
            payload={"text": "第二段。"},
            artifact_locator={
                "ir_id": "ir_0001",
                "source_item_index": 1,
                "order_index": 1,
                "page_no": 1,
            },
        )
        views = _views([second, first])

        report = audit_document(
            normalized_ir=normalized,
            units=views,
            metadata=self.metadata,
        )

        self.assertIn("unit_source_order_invalid", _codes(report))

    def test_duplicate_equal_table_occurrences_are_not_value_deduplicated(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="table",
                    table={"headers": ["项目"], "rows": [["收入"]]},
                    table_caption=[],
                    table_footnote=[],
                )
            ]
        )
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )

        report = audit_document(
            normalized_ir=normalized,
            units=_views([units[0], units[0]]),
            metadata=self.metadata,
        )

        self.assertIn("table_payload_count_invalid", _codes(report))

    def test_applicability_requires_one_target_with_the_proven_value(self) -> None:
        normalized = _ir(
            [_element(0, kind="text", text="√适用 □不适用\n经营保持稳定。")]
        )
        units, stats = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )
        self.assertEqual(units[0].applicability, "applicable")
        broken = replace(units[0], applicability="not_applicable")

        report = audit_document(
            normalized_ir=normalized,
            units=_views([broken]),
            metadata=self.metadata,
            source_dispositions=stats.source_dispositions,
        )

        self.assertIn("applicability_value_mismatch", _codes(report))

    def test_fully_externalized_source_cannot_still_be_published(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="page_furniture",
                    raw_kind="page_number",
                    text="1",
                )
            ]
        )
        unit = AuditUnitView(
            order_index=1,
            payload_kind="text",
            payload={"text": "1"},
            title="1",
            heading_path=["1"],
            structural_path=["1"],
            semantic_key="document_content",
            semantic_keys=["document_content"],
            quality_status="needs_review",
            applicability=None,
            artifact_locator={
                "ir_id": "ir_0000",
                "source_item_index": 0,
                "order_index": 0,
                "page_no": 1,
            },
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[unit],
            metadata=self.metadata,
            source_dispositions=[
                {
                    "ir_id": "ir_0000",
                    "source_item_index": 0,
                    "order_index": 0,
                    "role": "external_metadata",
                    "reason": "exact_page_number",
                }
            ],
        )

        self.assertIn("external_source_emitted", _codes(report))

    def test_captioned_cover_image_as_structure_tolerates_description_text(
        self,
    ) -> None:
        # A cover image can carry both a caption (real document text, usable
        # as structure) and a parser-generated scene description in ``text``.
        # The description travels as payload; only the caption must be
        # represented by the structure that claims the image.
        digest = "c" * 64
        normalized = _ir(
            [
                _element(
                    0,
                    kind="image",
                    raw_kind="image",
                    image_path=f"source/{digest}.png",
                    text="Abstract digital circuit board pattern with nodes",
                    image_caption=["2025年度报告"],
                ),
                _element(1, kind="text", raw_kind="text", text="正文内容。"),
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            normalized,
            filing_type="annual_report",
            document_title="风华高科：2025年年度报告",
            image_bytes_resolver=lambda _path: b"cover-bytes",
        )

        report = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=self.metadata,
            source_dispositions=stats.source_dispositions,
            image_hashes={"ir_0000": "sha256:" + "1" * 64},
        )

        self.assertNotIn("structure_text_mismatch", _codes(report))

    def test_leading_marker_with_conflicting_followers_claims_nothing(
        self,
    ) -> None:
        # Symmetry with the label-then-marker gate: a block OPENING with a
        # marker whose later sub-items answer differently describes no single
        # applicability either; the first answer must not speak for the block.
        block = "\n".join(
            [
                "□适用√不适用",
                "(3) 通过融资租赁租入的固定资产情况",
                "√适用 □不适用",
            ]
        )
        normalized = _ir(
            [
                _element(
                    0,
                    kind="heading",
                    raw_kind="text",
                    text="固定资产",
                    heading_level=1,
                ),
                _element(1, kind="text", raw_kind="text", text=block),
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            normalized, filing_type="semiannual_report", document_title="审计样本"
        )

        text_units = [u for u in units if u.payload_kind == "text"]
        self.assertEqual([u.applicability for u in text_units], [None])
        # The refused marker stays visible in the text, not silently dropped.
        self.assertIn("□适用√不适用", str(text_units[0].payload.get("text", "")))
        report = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=self.metadata,
            source_dispositions=stats.source_dispositions,
        )
        self.assertNotIn("source_disposition_proof_invalid", _codes(report))
        self.assertNotIn("applicability_target_count_invalid", _codes(report))

    def test_conflicting_sub_item_markers_claim_no_block_applicability(
        self,
    ) -> None:
        # One block, four sub-items, and their answers disagree — no single
        # applicability describes the block, so none may be claimed.
        block = "\n".join(
            [
                "(2) 暂时闲置的固定资产情况",
                "□适用√不适用",
                "(3) 通过融资租赁租入的固定资产情况",
                "□适用√不适用",
                "(4) 未办妥产权证书的固定资产情况",
                "√适用 □不适用",
            ]
        )
        normalized = _ir(
            [
                _element(
                    0,
                    kind="heading",
                    raw_kind="text",
                    text="固定资产",
                    heading_level=1,
                ),
                _element(1, kind="text", raw_kind="text", text=block),
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            normalized, filing_type="semiannual_report", document_title="审计样本"
        )

        self.assertEqual(
            [unit.applicability for unit in units if unit.payload_kind == "text"],
            [None],
        )
        report = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=self.metadata,
            source_dispositions=stats.source_dispositions,
        )
        self.assertNotIn("source_disposition_proof_invalid", _codes(report))

    def test_marker_labelled_as_heading_is_not_image_context(self) -> None:
        # A backend may label a bare 适用/不适用 line as a heading. It answers
        # the section above it; letting a following image adopt it as context
        # made two units claim one marker.
        normalized = _ir(
            [
                _element(
                    0,
                    kind="heading",
                    raw_kind="text",
                    text="(4). 研发人员构成发生重大变化的原因",
                    heading_level=1,
                    page_no=1,
                ),
                _element(
                    1,
                    kind="heading",
                    raw_kind="text",
                    text="□适用√不适用",
                    heading_level=1,
                    page_no=1,
                ),
                _element(
                    2,
                    kind="image",
                    raw_kind="image",
                    image_path=f"source/{'b' * 64}.png",
                    page_no=1,
                ),
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            normalized,
            filing_type="annual_report",
            document_title="审计样本",
            image_bytes_resolver=lambda _path: b"image-bytes",
        )

        image_units = [u for u in units if "image_ref" in u.payload]
        self.assertEqual([u.payload.get("context") for u in image_units], [""])
        report = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=self.metadata,
            source_dispositions=stats.source_dispositions,
            image_hashes={"ir_0002": "sha256:" + "0" * 64},
        )
        self.assertNotIn("applicability_target_count_invalid", _codes(report))

    def test_absorbed_duplicate_furniture_keeps_a_disposition_each(
        self,
    ) -> None:
        # Repeated 证券代码/证券简称 headers collapse onto one carrier in S1,
        # and the registered-header dedup then drops that carrier. Without a
        # disposition per absorbed occurrence the later pages' source atoms
        # end up represented by nothing at all.
        normalized = _ir(
            [
                _element(
                    0,
                    kind="heading",
                    raw_kind="text",
                    text="投资者关系活动记录表",
                    heading_level=1,
                    page_no=1,
                ),
                _element(
                    1, kind="text", raw_kind="text", text="正文内容。", page_no=1
                ),
                _element(
                    2,
                    kind="page_furniture",
                    raw_kind="header",
                    text="证券代码：688525",
                    page_no=1,
                ),
                _element(
                    3, kind="text", raw_kind="text", text="第二页正文。", page_no=2
                ),
                _element(
                    4,
                    kind="page_furniture",
                    raw_kind="header",
                    text="证券代码：688525",
                    page_no=2,
                ),
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            normalized,
            filing_type="investor_relations",
            document_title="审计样本",
            security_code="688525",
        )

        report = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=replace(self.metadata, security_code="688525"),
            source_dispositions=stats.source_dispositions,
        )

        self.assertNotIn("source_atom_uncovered", _codes(report))
        # Both occurrences are accounted for, not just the surviving identity.
        dropped = [
            d
            for d in stats.source_dispositions
            if d.get("reason") == "registered_security_header"
        ]
        self.assertEqual({d["ir_id"] for d in dropped}, {"ir_0002", "ir_0004"})

    def test_case_flapped_furniture_dedup_is_valid_exact_duplication(
        self,
    ) -> None:
        # OCR case-flaps the same repeated footer ("[QR Code]"/"[QR CODE]").
        # The builder dedups them under comparison_text equivalence; the
        # audit must validate that claim under the same equivalence.
        normalized = _ir(
            [
                _element(
                    0,
                    kind="heading",
                    raw_kind="text",
                    text="重要提示",
                    heading_level=1,
                ),
                _element(1, kind="text", raw_kind="text", text="正文内容。"),
                _element(
                    2,
                    kind="page_furniture",
                    raw_kind="footer",
                    text="[QR Code]",
                    page_no=1,
                ),
                _element(
                    3,
                    kind="page_furniture",
                    raw_kind="footer",
                    text="[QR CODE]",
                    page_no=2,
                ),
            ]
        )
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )

        report = audit_document(
            normalized_ir=normalized,
            units=_views(units),
            metadata=self.metadata,
        )

        self.assertNotIn("exact_dedup_content_mismatch", _codes(report))

    def test_duplicate_equal_image_occurrences_are_counted_separately(self) -> None:
        digest = "a" * 64
        normalized = _ir(
            [
                _element(
                    0,
                    kind="image",
                    image_path=f"source/{digest}.png",
                    text="图一",
                )
            ]
        )
        unit = AuditUnitView(
            order_index=1,
            payload_kind="text",
            payload={
                "image_ref": f"images/{digest}.png",
                "caption": "图一",
                "context": "",
            },
            title="图一",
            heading_path=["图一"],
            structural_path=["图一"],
            semantic_key="document_content",
            semantic_keys=["document_content"],
            quality_status="needs_review",
            applicability=None,
            artifact_locator={
                "ir_id": "ir_0000",
                "source_item_index": 0,
                "order_index": 0,
                "page_no": 1,
            },
        )

        report = audit_document(
            normalized_ir=normalized,
            units=[unit, replace(unit, order_index=2)],
            metadata=self.metadata,
            image_hashes={"ir_0000": f"sha256:{digest}"},
        )

        self.assertIn("image_payload_count_invalid", _codes(report))

    def test_semantic_key_invalid_states_are_flagged(self) -> None:
        normalized = _ir(
            [
                _element(
                    0,
                    kind="heading",
                    raw_kind="text",
                    text="一、经营情况",
                    heading_level=1,
                ),
                _element(1, kind="text", text="营业收入同比增长20%。"),
            ]
        )
        units, _ = build_unit_drafts_s1_s7(
            normalized,
            filing_type="other",
            document_title="审计样本",
        )
        forged_states = [
            ("scalar_none_with_nonempty_array", None, ["dividend"]),
            ("empty_array_with_valid_scalar", "dividend", []),
            ("array_contains_invalid_token", "dividend", ["dividend", "Bad-Key!"]),
            ("array_duplicate", "dividend", ["dividend", "dividend"]),
            ("scalar_not_member", "dividend", ["cash_flow"]),
        ]
        for label, semantic_key, semantic_keys in forged_states:
            with self.subTest(state=label):
                broken = replace(
                    units[0],
                    semantic_key=semantic_key,
                    semantic_keys=semantic_keys,
                )
                report = audit_document(
                    normalized_ir=normalized,
                    units=_views([broken]),
                    metadata=self.metadata,
                )
                self.assertIn("semantic_key_invalid", _codes(report))

    def test_image_payload_field_and_hash_negatives_are_flagged(self) -> None:
        digest = "a" * 64
        mismatch = "b" * 64
        base_payload = {
            "image_ref": f"images/{digest}.png",
            "caption": "",
            "context": "",
            "visual_kind": "image",
        }
        cases = [
            (
                "image_caption_dropped",
                _element(
                    0,
                    kind="image",
                    raw_kind="image",
                    image_path=f"source/{digest}.png",
                    image_caption=["关键图注"],
                ),
                base_payload,
                {"ir_0000": f"sha256:{digest}"},
            ),
            (
                "visual_kind_mismatch",
                _element(
                    0,
                    kind="image",
                    raw_kind="chart",
                    image_path=f"source/{digest}.png",
                ),
                base_payload,
                {"ir_0000": f"sha256:{digest}"},
            ),
            (
                "visual_subtype_mismatch",
                _element(
                    0,
                    kind="image",
                    raw_kind="image",
                    image_path=f"source/{digest}.png",
                    visual_subtype="bar",
                ),
                base_payload,
                {"ir_0000": f"sha256:{digest}"},
            ),
            (
                "image_hash_mismatch",
                _element(
                    0,
                    kind="image",
                    raw_kind="image",
                    image_path=f"source/{digest}.png",
                ),
                {**base_payload, "image_ref": f"images/{mismatch}.png"},
                {"ir_0000": f"sha256:{digest}"},
            ),
            (
                "image_hash_unavailable",
                _element(
                    0,
                    kind="image",
                    raw_kind="image",
                    image_path="source/plain_diagram.png",
                ),
                base_payload,
                {},
            ),
        ]
        for code, element, payload, image_hashes in cases:
            with self.subTest(code=code):
                unit = AuditUnitView(
                    order_index=1,
                    payload_kind="text",
                    payload=payload,
                    title="图注",
                    heading_path=["图注"],
                    structural_path=["图注"],
                    semantic_key="document_content",
                    semantic_keys=["document_content"],
                    quality_status="needs_review",
                    applicability=None,
                    artifact_locator={
                        "ir_id": "ir_0000",
                        "source_item_index": 0,
                        "order_index": 0,
                        "page_no": 1,
                    },
                )
                report = audit_document(
                    normalized_ir=_ir([element]),
                    units=[unit],
                    metadata=self.metadata,
                    image_hashes=image_hashes,
                )
                self.assertIn(code, _codes(report))


if __name__ == "__main__":
    unittest.main()
