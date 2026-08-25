from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import cast
import unittest

from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document import (
    ProviderArtifact,
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
    provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
    SourceQualityFinding,
    SourceTextReconciliation,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderSearchDestination,
    ProviderUnitLocator,
    ProviderUnitSourceQualityFinding,
    ProviderUnitSourceTextReconciliation,
    provider_unit_locator_from_payload,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.contracts.provider_table_projection import (
    ProviderTablePartRef,
    UnboundProviderTablePart,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
    replay_provider_unit_search_binding,
)
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
)


_SOURCE_SHA = "sha256:" + "a" * 64
_OWNER = "run_01K0000000000000000000000"
_DOCUMENT = "doc_01K00000000000000000000000"


class ProviderUnitBuilderTests(unittest.TestCase):
    def test_untyped_prompt_selector_stays_in_parent_locator_v8(self) -> None:
        template = (
            "报告期内公司经营情况的重大变化，以及报告期内发生的对公司"
            "经营情况有重大影响和预计未来会有重大影响的事项"
        )
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "二、分红、回购与增持"),),
                        annotation="title",
                        level=2,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "公司完成股份回购。"),),
                        annotation="paragraph",
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, template),),
                        annotation="paragraph",
                    ),
                    _block(
                        3,
                        0,
                        "text",
                        (ProviderPayload("text", None, "□适用 √不适用"),),
                        annotation="paragraph",
                    ),
                    _block(
                        4,
                        0,
                        "text",
                        (ProviderPayload("text", None, "三、核心竞争力分析"),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        units = build_provider_units(_admitted(document)).units

        self.assertEqual(len(units), 2)
        parent_unit = units[0]
        self.assertEqual(parent_unit.title, "二、分红、回购与增持")
        self.assertIsNone(parent_unit.applicability)
        self.assertIn(template, json.dumps(parent_unit.payload, ensure_ascii=False))
        self.assertEqual(
            parent_unit.locator.contract_version,
            "provider_unit_locator.v8",
        )
        with self.assertRaisesRegex(ValueError, "heading placement"):
            replace(
                parent_unit.locator,
                heading_chain=(
                    *parent_unit.locator.heading_chain[:-1],
                    replace(
                        parent_unit.locator.heading_chain[-1],
                        placement_source="statutory_template",
                    ),
                ),
            )

    def test_unheaded_sentence_selector_does_not_claim_applicability(
        self,
    ) -> None:
        template = (
            "公司应当根据重要性原则，说明报告期内公司经营情况的重大变化，"
            "以及报告期内发生的对公司经营情况有重大影响和预计未来会有重大影响的事项"
        )
        for selector in (
            "□适用 √不适用",
            "□适用 √不适用 □适用 √不适用",
            "□适用 √不适用 □",
            "□适用 √适用",
        ):
            with self.subTest(selector=selector):
                document = _document(
                    pages=(
                        (
                            _block(
                                0,
                                0,
                                "text",
                                (ProviderPayload("text", None, template + selector),),
                                annotation="paragraph",
                            ),
                        ),
                    ),
                    segments=(),
                )
                unit = build_provider_units(_admitted(document)).units[0]
                self.assertIsNone(unit.applicability)

    def test_delayed_table_notice_keeps_one_source_conserving_owner_unit(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "项目开发情况和开发计划"),),
                        annotation="title",
                        level=2,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "单位：平方米"),),
                        annotation="paragraph",
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "特别风险提示"),),
                        annotation="title",
                        level=2,
                    ),
                    _block(
                        3,
                        0,
                        "text",
                        (ProviderPayload("text", None, "上述项目开发计划可能调整。"),),
                        annotation="paragraph",
                    ),
                    _block(
                        4,
                        0,
                        "table",
                        (
                            ProviderPayload(
                                "table_body",
                                None,
                                "<table><tr><td>项目名称</td>"
                                "<td>开发计划</td></tr><tr><td>A</td>"
                                "<td>2026</td></tr></table>",
                            ),
                        ),
                        annotation="table",
                    ),
                ),
            ),
            segments=(_segment(0, 0, "retained"),),
        )

        result = build_provider_units(_admitted(document))

        self.assertEqual(len(result.units), 1)
        unit = result.units[0]
        self.assertEqual(unit.title, "项目开发情况和开发计划")
        self.assertEqual(unit.heading_path, ("项目开发情况和开发计划",))
        self.assertEqual(unit.payload_kind, "mixed")
        self.assertEqual(
            tuple(
                source_index
                for part in unit.locator.parts
                for source_index in part.block_source_indices
            ),
            (1, 2, 3, 4),
        )
        self.assertEqual(unit.locator.heading_chain[-1].source_index, 0)

    def test_source_native_numeric_repair_is_payload_and_locator_provenance(self) -> None:
        provider_text = "年 月，本集团净利差 。"
        source_text = "2026年1-3月，本集团净利差1.77%。"
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, provider_text),),
                        annotation="paragraph",
                    ),
                ),
            ),
            segments=(),
        )
        block = document.blocks[0]
        admitted = replace(
            _admitted(document),
            source_text_reconciliations=(
                SourceTextReconciliation(
                    source_index=0,
                    payload_ordinal=0,
                    raw_block_sha256=block.raw_item_sha256,
                    provider_text_sha256=_sha_text(provider_text),
                    source_text_sha256=_sha_text(source_text),
                    source_text=source_text,
                ),
            ),
        )

        unit = build_provider_units(admitted).units[0]

        self.assertEqual(unit.payload, {"text": source_text})
        self.assertEqual(len(unit.locator.source_text_reconciliations), 1)
        self.assertEqual(
            provider_unit_locator_from_payload(
                provider_unit_locator_to_payload(unit.locator)
            ),
            unit.locator,
        )
        for binding in unit.locator.search_targets:
            self.assertEqual(
                replay_provider_unit_search_binding(admitted, unit, binding),
                (source_text,),
            )

    def test_source_table_quality_finding_marks_unit_and_locator_needs_review(
        self,
    ) -> None:
        document = _representative_document()
        table_block = document.blocks[3]
        table_payload = next(
            payload for payload in table_block.payloads if payload.field == "table_body"
        )
        admitted = replace(
            _admitted(document),
            source_quality_findings=(
                SourceQualityFinding(
                    source_index=3,
                    payload_ordinal=0,
                    raw_block_sha256=table_block.raw_item_sha256,
                    provider_text_sha256=_sha_text(table_payload.text),
                    source_text_sha256=_sha_text("source-native-table-text"),
                    reason="numeric_token_mismatch",
                ),
            ),
        )

        unit = build_provider_units(admitted).units[1]

        self.assertEqual(unit.quality_status, "needs_review")
        self.assertEqual(
            [item.reason for item in unit.locator.source_quality_findings],
            ["numeric_token_mismatch"],
        )
        self.assertEqual(
            provider_unit_locator_from_payload(
                provider_unit_locator_to_payload(unit.locator)
            ),
            unit.locator,
        )

    def test_source_text_quality_finding_marks_unit_without_rewriting_payload(
        self,
    ) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "附注 五 年度"),),
                        annotation="paragraph",
                    ),
                ),
            ),
            segments=(),
        )
        block = document.blocks[0]
        admitted = replace(
            _admitted(document),
            source_quality_findings=(
                SourceQualityFinding(
                    source_index=0,
                    payload_ordinal=0,
                    raw_block_sha256=block.raw_item_sha256,
                    provider_text_sha256=_sha_text("附注 五 年度"),
                    source_text_sha256=_sha_text("附注(五)42 2025年度"),
                    reason="native_text_omission",
                    source_kind="source_pdf_native_text_quality.v1",
                ),
            ),
        )

        unit = build_provider_units(admitted).units[0]

        self.assertEqual(unit.payload, {"text": "附注 五 年度"})
        self.assertEqual(unit.quality_status, "needs_review")
        self.assertEqual(
            provider_unit_locator_from_payload(
                provider_unit_locator_to_payload(unit.locator)
            ),
            unit.locator,
        )

    def test_ancestor_source_repair_is_bound_to_descendant_heading_chain(
        self,
    ) -> None:
        provider_text = "章 总则"
        source_text = "第一章 总则"
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, provider_text),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "一、范围"),),
                        annotation="title",
                        level=2,
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "正文"),),
                        annotation="paragraph",
                    ),
                ),
            ),
            segments=(),
        )
        block = document.blocks[0]
        admitted = replace(
            _admitted(document),
            source_text_reconciliations=(
                SourceTextReconciliation(
                    source_index=0,
                    payload_ordinal=0,
                    raw_block_sha256=block.raw_item_sha256,
                    provider_text_sha256=_sha_text(provider_text),
                    source_text_sha256=_sha_text(source_text),
                    source_text=source_text,
                ),
            ),
        )

        parent, descendant = build_provider_units(admitted).units

        self.assertEqual(descendant.heading_path, (source_text, "一、范围"))
        self.assertEqual(len(parent.locator.source_text_reconciliations), 1)
        self.assertEqual(len(descendant.locator.source_text_reconciliations), 1)
        self.assertEqual(
            descendant.locator.source_text_reconciliations,
            parent.locator.source_text_reconciliations,
        )

    def test_heading_table_visual_and_demoted_content_are_conserved_once(
        self,
    ) -> None:
        admitted = _admitted(_representative_document())

        result = build_provider_units(admitted)

        self.assertEqual(len(result.units), 2)
        preamble, section = result.units
        self.assertEqual(preamble.payload_kind, "text")
        self.assertEqual(preamble.payload, {"text": ""})
        self.assertEqual(
            preamble.locator.evidence_only_block_source_indices,
            (0,),
        )

        self.assertEqual(section.title, "第一章 标题")
        self.assertEqual(section.heading_path, ("第一章 标题",))
        self.assertEqual(section.payload_kind, "mixed")
        parts = section.payload["parts"]
        self.assertIsInstance(parts, list)
        assert isinstance(parts, list)
        self.assertTrue(all("provider_type" not in part for part in parts))
        self.assertEqual(
            [part.kind for part in section.locator.parts],
            ["text", "table", "visual", "text"],
        )
        self.assertNotIn("semantic_type", section.payload)
        self.assertTrue(all("kind" not in part for part in parts))
        self.assertNotIn("第一章 标题", json.dumps(section.payload, ensure_ascii=False))
        self.assertIn("□适用", json.dumps(section.payload, ensure_ascii=False))
        self.assertIsNone(section.applicability)

        table_ref = section.locator.parts[1]
        self.assertEqual(table_ref.block_source_indices, (3, 5))
        self.assertEqual(table_ref.physical_table_segment_indices, (0, 1))
        self.assertEqual(table_ref.logical_table_index, 0)
        visual = parts[2]
        self.assertEqual(
            visual["content_artifacts"],
            [
                {
                    "media_type": "image/jpeg",
                    "sha256": "sha256:" + "f" * 64,
                    "size_bytes": 321,
                }
            ],
        )

        title_binding = section.locator.search_targets[0]
        self.assertEqual(title_binding.destination.kind, "unit_title")
        self.assertEqual(
            replay_provider_unit_search_binding(admitted, section, title_binding),
            ("第一章 标题",),
        )
        for binding in section.locator.search_targets:
            replay_provider_unit_search_binding(admitted, section, binding)
        self.assertEqual(
            [binding.source.source_index for binding in section.locator.search_targets],
            [1, 2, 3, 3, 7],
        )

        locator_payload = provider_unit_locator_to_payload(section.locator)
        self.assertEqual(
            provider_unit_locator_from_payload(locator_payload),
            section.locator,
        )
        self.assertEqual(
            [item.sha256 for item in section.locator.evidence_artifacts],
            ["sha256:" + "f" * 64],
        )
        encoded = json.dumps(locator_payload, ensure_ascii=False)
        self.assertNotIn("relative_path", encoded)
        self.assertNotIn("raw_item_json", encoded)

    def test_cover_identity_and_logo_join_first_titled_unit_without_loss(
        self,
    ) -> None:
        artifact = ProviderArtifact(
            role="cover_logo",
            relative_path="e_images/logo.jpg",
            sha256="sha256:" + "f" * 64,
            size_bytes=8505,
            media_type="image/jpeg",
        )
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "A股简称：示例公司"),),
                        annotation="paragraph",
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "A 股代码：600001"),),
                        annotation="paragraph",
                    ),
                    _block(
                        2,
                        0,
                        "image",
                        (ProviderPayload("content", None, ""),),
                        annotation="image",
                        artifact_roles=(artifact.role,),
                    ),
                    _block(
                        3,
                        0,
                        "text",
                        (ProviderPayload("text", None, "2025年度报告"),),
                        annotation="title",
                        level=1,
                    ),
                ),
            ),
            segments=(),
            extra_artifacts=(artifact,),
        )

        result = build_provider_units(_admitted(document))

        self.assertEqual(len(result.units), 1)
        unit = result.units[0]
        self.assertEqual(unit.title, "2025年度报告")
        self.assertEqual(unit.heading_path, ("2025年度报告",))
        self.assertEqual(unit.payload_kind, "mixed")
        self.assertEqual(
            [part.kind for part in unit.locator.parts],
            ["text", "text", "visual"],
        )
        self.assertEqual(
            [binding.source.source_index for binding in unit.locator.search_targets],
            [0, 1, 3],
        )
        self.assertEqual(
            [item.sha256 for item in unit.locator.evidence_artifacts],
            [artifact.sha256],
        )
        self.assertIn(artifact.sha256, json.dumps(unit.payload, ensure_ascii=False))

    def test_applicability_is_unit_local_explicit_and_hash_bound(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "第一节"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (
                            ProviderPayload(
                                "text",
                                None,
                                "☑适用 ☐不适用；✓适用 □不适用",
                            ),
                        ),
                        annotation="paragraph",
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "第二节"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        3,
                        0,
                        "text",
                        (ProviderPayload("text", None, "资金来源：不适用"),),
                        annotation="paragraph",
                    ),
                    _block(
                        4,
                        0,
                        "text",
                        (ProviderPayload("text", None, "第三节"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        5,
                        0,
                        "text",
                        (ProviderPayload("text", None, "□适用 √不适用"),),
                        annotation="paragraph",
                    ),
                ),
            ),
            segments=(),
        )

        units = build_provider_units(_admitted(document)).units

        self.assertEqual(
            [unit.applicability for unit in units],
            ["applicable", None, "not_applicable"],
        )
        not_applicable = units[2]
        without_applicability = compute_unit_hashes(
            payload_kind=not_applicable.payload_kind,
            payload=dict(not_applicable.payload),
            title=not_applicable.title,
            heading_path=list(not_applicable.heading_path),
            semantic_key=not_applicable.semantic_key,
            semantic_keys=None,
            section_keys=None,
            applicability=None,
            quality_status=not_applicable.quality_status,
            order_index=not_applicable.unit_index + 1,
        )
        self.assertEqual(not_applicable.content_hash, without_applicability.content_hash)
        self.assertEqual(
            not_applicable.structure_hash,
            without_applicability.structure_hash,
        )
        self.assertNotEqual(
            not_applicable.query_projection_hash,
            without_applicability.query_projection_hash,
        )
        same_source_different_projection = replace(
            not_applicable,
            applicability=None,
            query_projection_hash=without_applicability.query_projection_hash,
        )
        self.assertEqual(
            same_source_different_projection.locator,
            not_applicable.locator,
        )

    def test_applicability_conflicts_and_invalid_pairs_remain_null(self) -> None:
        cases = (
            "□适用 □不适用",
            "√适用 √不适用",
            "√适用 □不适用；□适用 √不适用",
            "本事项不适用于公司",
            "√适用",
            "□不适用",
        )
        for source_text in cases:
            with self.subTest(source_text=source_text):
                document = _document(
                    pages=(
                        (
                            _block(
                                0,
                                0,
                                "text",
                                (ProviderPayload("text", None, source_text),),
                                annotation="paragraph",
                            ),
                        ),
                    ),
                    segments=(),
                )

                draft = build_provider_units(_admitted(document)).units[0]

                self.assertIsNone(draft.applicability)

    def test_applicability_reads_visible_table_text(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "table",
                        (
                            ProviderPayload(
                                "table_body",
                                None,
                                (
                                    "<table><tr><td>&#61522;<span>适用</span></td>"
                                    "<td>&nbsp;□<br>不适用</td></tr></table>"
                                ),
                            ),
                        ),
                        annotation="table",
                    ),
                ),
            ),
            segments=(_segment(0, 0, "retained"),),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.applicability, "applicable")

    def test_later_child_selector_does_not_hide_substantive_parent_content(
        self,
    ) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "一、研发投入"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "研发投入金额4.4亿元。"),),
                        annotation="paragraph",
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "（1）重大变化原因"),),
                        annotation="paragraph",
                    ),
                    _block(
                        3,
                        0,
                        "text",
                        (ProviderPayload("text", None, "□适用√不适用"),),
                        annotation="paragraph",
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertIsNone(draft.applicability)

    def test_leading_parent_selector_is_not_overridden_by_later_child_selector(
        self,
    ) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "一、担保情况"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "√适用□不适用"),),
                        annotation="paragraph",
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "报告期担保余额100万元。"),),
                        annotation="paragraph",
                    ),
                    _block(
                        3,
                        0,
                        "text",
                        (ProviderPayload("text", None, "□适用√不适用"),),
                        annotation="paragraph",
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.applicability, "applicable")

    def test_applicability_does_not_cross_a_leading_visual_part(self) -> None:
        artifact = ProviderArtifact(
            role="child_visual",
            relative_path="e_images/child.jpg",
            sha256="sha256:" + "1" * 64,
            size_bytes=10,
            media_type="image/jpeg",
        )
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "一、母项"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "image",
                        (ProviderPayload("content", None, ""),),
                        annotation="image",
                        artifact_roles=(artifact.role,),
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "□适用√不适用"),),
                        annotation="paragraph",
                    ),
                ),
            ),
            segments=(),
            extra_artifacts=(artifact,),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertIsNone(draft.applicability)

    def test_nonleading_prompt_or_fact_selector_does_not_claim_applicability(
        self,
    ) -> None:
        def build(*body: str):  # type: ignore[no-untyped-def]
            blocks = [
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, "其他重要事项"),),
                    annotation="title",
                    level=1,
                )
            ]
            blocks.extend(
                _block(
                    index,
                    0,
                    "text",
                    (ProviderPayload("text", None, text),),
                    annotation="paragraph",
                )
                for index, text in enumerate(body, start=1)
            )
            return build_provider_units(
                _admitted(_document(pages=(tuple(blocks),), segments=()))
            ).units[0]

        for first_part in (
            "请说明报告期内对公司经营情况有重大影响的事项",
            "公司本期完成一项重大收购。",
            "1、已披露且后续无变化的事项",
            "无",
        ):
            with self.subTest(first_part=first_part):
                self.assertIsNone(
                    build(first_part, "□适用 √不适用").applicability
                )

        for body in (
            ("请说明报告期内对公司经营情况有重大影响的事项□适用√不适用",),
            (
                "请说明报告期内对公司经营情况有重大影响的事项",
                "公司本期完成一项收购。",
                "□适用√不适用",
            ),
            (
                "请说明报告期内对公司经营情况有重大影响的事项",
                "√适用√不适用",
            ),
        ):
            with self.subTest(body=body):
                self.assertIsNone(build(*body).applicability)

    def test_visual_digest_changes_content_hash_without_a_search_target(self) -> None:
        first = build_provider_units(_admitted(_visual_only_document("f"))).units[0]
        second = build_provider_units(_admitted(_visual_only_document("e"))).units[0]

        self.assertEqual(first.payload_kind, "mixed")
        self.assertEqual(first.locator.search_targets, ())
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.query_projection_hash, second.query_projection_hash)

    def test_table_without_body_keeps_crop_digest_in_content_hash(
        self,
    ) -> None:
        first = build_provider_units(_admitted(_table_visual_only_document("f"))).units[
            0
        ]
        second = build_provider_units(
            _admitted(_table_visual_only_document("e"))
        ).units[0]

        self.assertEqual(first.payload_kind, "table")
        self.assertNotIn("provider_type", first.payload)
        self.assertEqual(len(first.locator.search_targets), 1)
        self.assertIn("content_artifacts", first.payload)
        self.assertEqual(len(first.locator.evidence_artifacts), 1)
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.query_projection_hash, second.query_projection_hash)

    def test_table_with_body_does_not_treat_crop_as_semantic_content(
        self,
    ) -> None:
        first = build_provider_units(
            _admitted(_table_with_body_and_crop_document("f"))
        ).units[0]
        second = build_provider_units(
            _admitted(_table_with_body_and_crop_document("e"))
        ).units[0]

        self.assertEqual(first.payload_kind, "table")
        self.assertNotIn("provider_type", first.payload)
        self.assertNotIn("content_artifacts", first.payload)
        self.assertEqual(len(first.locator.evidence_artifacts), 1)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.query_projection_hash, second.query_projection_hash)

    def test_unit_locator_rejects_segment_only_unbound_table_parts(self) -> None:
        with self.assertRaisesRegex(ValueError, "segment-only"):
            ProviderUnitLocator(
                provider_document_sha256="sha256:" + "a" * 64,
                unit_index=0,
                heading_chain=(),
                parts=(),
                evidence_only_block_source_indices=(),
                unbound_table_parts=(
                    UnboundProviderTablePart(
                        part=ProviderTablePartRef(
                            block_source_index=None,
                            physical_segment_index=0,
                        ),
                        reason="page_table_count_mismatch",
                    ),
                ),
                evidence_artifacts=(),
                search_targets=(),
            )

    def test_unit_locator_decoder_rejects_unknown_and_malformed_fields(self) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        payload = provider_unit_locator_to_payload(locator)
        payload["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "locator fields"):
            provider_unit_locator_from_payload(payload)

        payload = provider_unit_locator_to_payload(locator)
        parts = cast(list[dict[str, object]], payload["parts"])
        parts[0]["part_index"] = True
        with self.assertRaisesRegex(ValueError, "part index"):
            provider_unit_locator_from_payload(payload)

        payload = provider_unit_locator_to_payload(locator)
        headings = cast(list[dict[str, object]], payload["heading_chain"])
        headings[0].pop("payload_ordinal")
        with self.assertRaisesRegex(ValueError, "heading fields"):
            provider_unit_locator_from_payload(payload)

    def test_locator_decoder_keeps_legacy_v1_read_compatibility(self) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        payload = provider_unit_locator_to_payload(locator)
        payload["contract_version"] = "provider_unit_locator.v1"
        payload.pop("source_text_reconciliations")
        payload.pop("source_quality_findings")
        for heading in cast(list[dict[str, object]], payload["heading_chain"]):
            heading.pop("payload_ordinal")
            heading.pop("continuation_fragments")

        decoded = provider_unit_locator_from_payload(payload)

        self.assertEqual(decoded.contract_version, "provider_unit_locator.v1")
        self.assertFalse(decoded.source_text_reconciliations)
        self.assertEqual(
            provider_unit_locator_from_payload(
                provider_unit_locator_to_payload(decoded)
            ),
            decoded,
        )

    def test_locator_decoder_keeps_v2_source_repair_read_compatibility(self) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        payload = provider_unit_locator_to_payload(locator)
        payload["contract_version"] = "provider_unit_locator.v2"
        payload.pop("source_quality_findings")
        for heading in cast(list[dict[str, object]], payload["heading_chain"]):
            heading.pop("payload_ordinal")
            heading.pop("continuation_fragments")

        decoded = provider_unit_locator_from_payload(payload)

        self.assertEqual(decoded.contract_version, "provider_unit_locator.v2")
        self.assertTrue(decoded.source_text_reconciliations == ())
        self.assertTrue(all(item.payload_ordinal == 0 for item in decoded.heading_chain))

    def test_locator_decoder_keeps_v3_payload_binding_read_compatibility(self) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        payload = provider_unit_locator_to_payload(locator)
        payload["contract_version"] = "provider_unit_locator.v3"
        payload.pop("source_quality_findings")
        for heading in cast(list[dict[str, object]], payload["heading_chain"]):
            heading.pop("continuation_fragments")

        decoded = provider_unit_locator_from_payload(payload)

        self.assertEqual(decoded.contract_version, "provider_unit_locator.v3")
        self.assertTrue(
            all(not item.continuation_fragments for item in decoded.heading_chain)
        )

    def test_locator_decoder_keeps_v4_native_quality_read_compatibility(self) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        payload = provider_unit_locator_to_payload(locator)
        payload["contract_version"] = "provider_unit_locator.v4"

        decoded = provider_unit_locator_from_payload(payload)

        self.assertEqual(decoded.contract_version, "provider_unit_locator.v4")
        self.assertEqual(
            provider_unit_locator_from_payload(
                provider_unit_locator_to_payload(decoded)
            ),
            decoded,
        )

    def test_locator_versions_close_heading_placement_vocabulary(self) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        heading = locator.heading_chain[-1]
        accepted = {
            "provider_unit_locator.v1": "numbering",
            "provider_unit_locator.v2": "numbering",
            "provider_unit_locator.v3": "table_container",
            "provider_unit_locator.v4": "table_container",
            "provider_unit_locator.v5": "table_container",
            "provider_unit_locator.v6": "table_container",
            "provider_unit_locator.v7": "statutory_template",
            "provider_unit_locator.v8": "table_container",
        }
        for version, placement_source in accepted.items():
            with self.subTest(version=version, accepted=placement_source):
                candidate = replace(
                    locator,
                    contract_version=version,
                    heading_chain=(
                        *locator.heading_chain[:-1],
                        replace(heading, placement_source=placement_source),
                    ),
                    source_text_reconciliations=(),
                    source_quality_findings=(),
                )
                self.assertEqual(
                    provider_unit_locator_from_payload(
                        provider_unit_locator_to_payload(candidate)
                    ),
                    candidate,
                )

        v3_table_label = replace(
            locator,
            contract_version="provider_unit_locator.v3",
            heading_chain=(
                *locator.heading_chain[:-1],
                replace(heading, placement_source="table_label"),
            ),
            source_text_reconciliations=(),
            source_quality_findings=(),
        )
        self.assertEqual(
            provider_unit_locator_from_payload(
                provider_unit_locator_to_payload(v3_table_label)
            ),
            v3_table_label,
        )

        for version, placement_source in (
            ("provider_unit_locator.v2", "table_label"),
            ("provider_unit_locator.v6", "statutory_template"),
            ("provider_unit_locator.v8", "statutory_template"),
        ):
            with self.subTest(version=version, rejected=placement_source):
                with self.assertRaisesRegex(ValueError, "heading placement"):
                    replace(
                        locator,
                        contract_version=version,
                        heading_chain=(
                            *locator.heading_chain[:-1],
                            replace(heading, placement_source=placement_source),
                        ),
                        source_text_reconciliations=(),
                        source_quality_findings=(),
                    )

        with self.assertRaisesRegex(ValueError, "heading reference"):
            replace(heading, placement_source="future_magic")  # type: ignore[arg-type]

        payload = provider_unit_locator_to_payload(locator)
        raw_headings = cast(list[dict[str, object]], payload["heading_chain"])
        raw_headings[-1]["placement_source"] = "future_magic"
        with self.assertRaisesRegex(ValueError, "heading reference"):
            provider_unit_locator_from_payload(payload)

    def test_locator_versions_reject_later_native_evidence_vocabulary(self) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        digest = "sha256:" + "a" * 64
        identifier_v1 = ProviderUnitSourceTextReconciliation(
            source_index=0,
            payload_ordinal=0,
            raw_block_sha256=digest,
            provider_text_sha256=digest,
            source_text_sha256=digest,
            source_kind="source_pdf_native_identifier.v1",
        )
        for version in ("provider_unit_locator.v2", "provider_unit_locator.v3"):
            with self.subTest(version=version), self.assertRaisesRegex(
                ValueError, "reconciliation kind"
            ):
                replace(
                    locator,
                    contract_version=version,
                    source_text_reconciliations=(identifier_v1,),
                    source_quality_findings=(),
                )
        self.assertEqual(
            replace(
                locator,
                contract_version="provider_unit_locator.v4",
                source_text_reconciliations=(identifier_v1,),
            ).source_text_reconciliations,
            (identifier_v1,),
        )
        with self.assertRaisesRegex(ValueError, "reconciliation kind"):
            replace(
                locator,
                contract_version="provider_unit_locator.v4",
                source_text_reconciliations=(
                    ProviderUnitSourceTextReconciliation(
                        source_index=0,
                        payload_ordinal=0,
                        raw_block_sha256=digest,
                        provider_text_sha256=digest,
                        source_text_sha256=digest,
                        source_kind="source_pdf_native_identifier.v2",
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "quality kind"):
            replace(
                locator,
                contract_version="provider_unit_locator.v4",
                source_quality_findings=(
                    ProviderUnitSourceQualityFinding(
                        source_index=0,
                        payload_ordinal=0,
                        raw_block_sha256=digest,
                        provider_text_sha256=digest,
                        source_text_sha256=digest,
                        reason="native_text_omission",
                        source_kind="source_pdf_native_text_quality.v1",
                    ),
                ),
            )
        for source_kind, reason in (
            (
                "source_pdf_native_identifier_quality.v1",
                "identifier_confusable_mismatch",
            ),
            ("source_pdf_native_text_quality.v2", "cjk_bracket_omission"),
        ):
            finding = ProviderUnitSourceQualityFinding(
                source_index=99,
                payload_ordinal=0,
                raw_block_sha256=digest,
                provider_text_sha256=digest,
                source_text_sha256=digest,
                reason=reason,
                source_kind=source_kind,
            )
            with self.subTest(source_kind=source_kind), self.assertRaisesRegex(
                ValueError,
                "quality kind",
            ):
                replace(
                    locator,
                    contract_version="provider_unit_locator.v5",
                    source_text_reconciliations=(),
                    source_quality_findings=(finding,),
                )
            accepted = replace(
                locator,
                contract_version="provider_unit_locator.v6",
                source_text_reconciliations=(),
                source_quality_findings=(finding,),
            )
            self.assertEqual(accepted.source_quality_findings, (finding,))

    def test_locator_versions_reject_later_title_fragment_search_vocabulary(
        self,
    ) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        binding = locator.search_targets[0]
        fragment_binding = replace(
            binding,
            destination=ProviderSearchDestination(
                kind="unit_title_fragment",
                item_index=0,
            ),
        )

        for version in (
            "provider_unit_locator.v1",
            "provider_unit_locator.v2",
            "provider_unit_locator.v3",
        ):
            with self.subTest(version=version), self.assertRaisesRegex(
                ValueError,
                "title fragment search",
            ):
                replace(
                    locator,
                    contract_version=version,
                    search_targets=(fragment_binding,),
                    source_text_reconciliations=(),
                    source_quality_findings=(),
                )

        accepted = replace(
            locator,
            contract_version="provider_unit_locator.v4",
            search_targets=(fragment_binding,),
            source_text_reconciliations=(),
            source_quality_findings=(),
        )
        self.assertEqual(
            accepted.search_targets[0].destination.kind,
            "unit_title_fragment",
        )

        payload = provider_unit_locator_to_payload(locator)
        payload["contract_version"] = "provider_unit_locator.v3"
        payload.pop("source_quality_findings")
        for heading in cast(list[dict[str, object]], payload["heading_chain"]):
            heading.pop("continuation_fragments")
        raw_search = cast(list[dict[str, object]], payload["search_targets"])
        raw_destination = cast(dict[str, object], raw_search[0]["destination"])
        raw_destination.update(
            {
                "field": None,
                "item_index": 0,
                "kind": "unit_title_fragment",
                "part_index": None,
            }
        )
        with self.assertRaisesRegex(ValueError, "title fragment search"):
            provider_unit_locator_from_payload(payload)

    def test_locator_rejects_overlapping_repair_and_quality_evidence(self) -> None:
        locator = (
            build_provider_units(_admitted(_representative_document())).units[1].locator
        )
        digest = "sha256:" + "a" * 64
        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            replace(
                locator,
                source_text_reconciliations=(
                    ProviderUnitSourceTextReconciliation(
                        source_index=0,
                        payload_ordinal=0,
                        raw_block_sha256=digest,
                        provider_text_sha256=digest,
                        source_text_sha256=digest,
                        source_kind="source_pdf_native_numeric.v1",
                    ),
                ),
                source_quality_findings=(
                    ProviderUnitSourceQualityFinding(
                        source_index=0,
                        payload_ordinal=0,
                        raw_block_sha256=digest,
                        provider_text_sha256=digest,
                        source_text_sha256=digest,
                        reason="numeric_token_mismatch",
                        source_kind="source_pdf_native_table_quality.v1",
                    ),
                ),
            )

    def test_heading_only_unit_keeps_structure_without_body_duplication(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "唯一标题"),),
                        annotation="title",
                        level=1,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.title, "唯一标题")
        self.assertEqual(draft.payload_kind, "text")
        self.assertEqual(
            draft.payload,
            {"text": ""},
        )
        self.assertEqual(len(draft.locator.search_targets), 1)
        self.assertEqual(
            draft.locator.search_targets[0].destination.kind,
            "unit_title",
        )

    def test_wrapped_heading_uses_v4_fragments_and_replays_each_source(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "（七）担保情况及对债"),),
                        annotation="title",
                        level=2,
                        bbox=ProviderBBox(100, 100, 900, 119),
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "券投资者权益的影响"),),
                        annotation="paragraph",
                        bbox=ProviderBBox(150, 127, 350, 145),
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "√适用□不适用"),),
                        annotation="paragraph",
                    ),
                ),
            ),
            segments=(),
        )
        admitted = _admitted(document)

        draft = build_provider_units(admitted).units[0]

        self.assertEqual(draft.title, "（七）担保情况及对债券投资者权益的影响")
        self.assertEqual(draft.locator.contract_version, "provider_unit_locator.v8")
        self.assertEqual(
            [
                fragment.source_index
                for fragment in draft.locator.heading_chain[-1].continuation_fragments
            ],
            [1],
        )
        self.assertEqual(
            [binding.destination.kind for binding in draft.locator.search_targets[:2]],
            ["unit_title_fragment", "unit_title_fragment"],
        )
        self.assertNotIn("券投资者权益的影响", json.dumps(draft.payload, ensure_ascii=False))
        self.assertEqual(
            provider_unit_locator_from_payload(
                provider_unit_locator_to_payload(draft.locator)
            ),
            draft.locator,
        )
        for binding in draft.locator.search_targets:
            replay_provider_unit_search_binding(admitted, draft, binding)

    def test_bare_applicability_title_is_conserved_once_as_body(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "20、 投资性房地产"),),
                        annotation="title",
                        level=2,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "不适用"),),
                        annotation="title",
                        level=3,
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "21、 固定资产"),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        first, second = build_provider_units(_admitted(document)).units

        self.assertEqual(first.title, "20、 投资性房地产")
        self.assertEqual(first.heading_path, ("20、 投资性房地产",))
        self.assertEqual(first.payload, {"text": "不适用"})
        self.assertEqual(
            tuple(
                source_index
                for part in first.locator.parts
                for source_index in part.block_source_indices
            ),
            (1,),
        )
        self.assertEqual(
            [
                binding.source.source_index
                for binding in first.locator.search_targets
            ],
            [0, 1],
        )
        self.assertIsNone(first.applicability)
        self.assertEqual(second.title, "21、 固定资产")
        self.assertEqual(second.payload, {"text": ""})

    def test_numbered_table_caption_opens_a_section_without_duplicating_text(
        self,
    ) -> None:
        caption = (
            "四、纳入环境信息依法披露企业名单的上市公司及其主要"
            "子公司的环境信息情况√适用 □不适用"
        )
        table_body = "<table><td>企业数量</td><td>9</td></table>"
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "第四节 公司治理、环境和社会"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "table",
                        (
                            ProviderPayload("table_body", None, table_body),
                            ProviderPayload("table_caption", 0, caption),
                        ),
                        annotation="table",
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (
                            ProviderPayload(
                                "text",
                                None,
                                "(一) 在报告期内为减少污染物排放所采取的措施",
                            ),
                        ),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(_segment(0, 0, "retained"),),
        )

        chapter, table, child = build_provider_units(_admitted(document)).units

        self.assertEqual(chapter.title, "第四节 公司治理、环境和社会")
        self.assertEqual(table.title, caption)
        self.assertEqual(table.heading_path, (chapter.title, caption))
        self.assertEqual(table.payload_kind, "table")
        self.assertEqual(table.payload, {"table_body": table_body})
        self.assertEqual(table.locator.heading_chain[-1].source_index, 1)
        self.assertEqual(table.locator.heading_chain[-1].payload_ordinal, 1)
        self.assertEqual(
            [
                (binding.source.payload_ordinal, binding.destination.kind)
                for binding in table.locator.search_targets
            ],
            [(0, "unit_payload"), (1, "unit_title")],
        )
        self.assertNotIn(caption, json.dumps(table.payload, ensure_ascii=False))
        self.assertEqual(child.heading_path, (chapter.title, caption, child.title))

    def test_empty_text_carrier_is_evidence_only_but_visual_content_survives(
        self,
    ) -> None:
        artifact = ProviderArtifact(
            role="image_0001",
            relative_path="e_images/figure.jpg",
            sha256="sha256:" + "f" * 64,
            size_bytes=321,
            media_type="image/jpeg",
        )
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, ""),),
                        annotation="paragraph",
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "正文"),),
                        annotation="paragraph",
                    ),
                    _block(
                        2,
                        0,
                        "image",
                        (ProviderPayload("content", None, ""),),
                        annotation="image",
                        artifact_roles=(artifact.role,),
                    ),
                ),
            ),
            segments=(),
            extra_artifacts=(artifact,),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.payload_kind, "mixed")
        parts = cast(list[dict[str, object]], draft.payload["parts"])
        self.assertTrue(all("provider_type" not in part for part in parts))
        self.assertEqual(
            [part.kind for part in draft.locator.parts],
            ["text", "visual"],
        )
        self.assertTrue(all("kind" not in part for part in parts))
        self.assertEqual(draft.locator.evidence_only_block_source_indices, (0,))
        self.assertEqual(
            [binding.source.source_index for binding in draft.locator.search_targets],
            [1],
        )

    def test_unique_typed_header_is_content_not_semantic_furniture(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "header",
                        (ProviderPayload("text", None, "证券代码：000001"),),
                        annotation="page_header",
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.payload_kind, "mixed")
        parts = cast(list[dict[str, object]], draft.payload["parts"])
        self.assertNotIn("provider_type", parts[0])
        self.assertEqual(parts[0]["text"], "证券代码：000001")
        self.assertEqual(document.blocks[0].provider_type, "header")
        self.assertEqual(draft.locator.parts[0].kind, "text")
        self.assertEqual(draft.locator.evidence_only_block_source_indices, ())
        self.assertEqual(len(draft.locator.search_targets), 1)

    def test_repeated_header_bilingual_variant_is_evidence_only(self) -> None:
        base = "3S 三生国健药业(上海)股份有限公司"
        bilingual = base + "Sunshine Guojian Pharmaceutical(Shanghai)Co.,Ltd."
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "header",
                        (ProviderPayload("text", None, base),),
                        annotation="page_header",
                        bbox=ProviderBBox(159, 52, 608, 84),
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "一、经营情况"),),
                        annotation="title",
                        level=1,
                    ),
                ),
                (
                    _block(
                        2,
                        1,
                        "header",
                        (ProviderPayload("text", None, bilingual),),
                        annotation="page_header",
                        bbox=ProviderBBox(159, 52, 606, 83),
                    ),
                    _block(
                        3,
                        1,
                        "text",
                        (ProviderPayload("text", None, "本期经营稳定。"),),
                        annotation="paragraph",
                    ),
                ),
                (
                    _block(
                        4,
                        2,
                        "header",
                        (ProviderPayload("text", None, base),),
                        annotation="page_header",
                        bbox=ProviderBBox(159, 52, 608, 84),
                    ),
                    _block(
                        5,
                        2,
                        "text",
                        (ProviderPayload("text", None, "后续说明。"),),
                        annotation="paragraph",
                    ),
                ),
            ),
            segments=(),
        )

        result = build_provider_units(_admitted(document))

        evidence_only = {
            source_index
            for unit in result.units
            for source_index in unit.locator.evidence_only_block_source_indices
        }
        self.assertEqual(evidence_only, {0, 2, 4})
        self.assertNotIn(
            "Sunshine Guojian",
            json.dumps([unit.payload for unit in result.units], ensure_ascii=False),
        )
        self.assertTrue(
            all(
                binding.source.source_index not in {0, 2, 4}
                for unit in result.units
                for binding in unit.locator.search_targets
            )
        )

    def test_unbound_block_is_published_and_segment_only_parts_are_not_guessed(
        self,
    ) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "table",
                        (
                            ProviderPayload(
                                "table_body", None, "<table><td>A</td></table>"
                            ),
                        ),
                        annotation="table",
                    ),
                ),
            ),
            segments=(
                _segment(0, 0, "retained"),
                _segment(0, 1, "retained"),
            ),
        )

        result = build_provider_units(_admitted(document))
        draft = result.units[0]

        self.assertEqual(draft.payload_kind, "table")
        self.assertEqual(draft.quality_status, "needs_review")
        self.assertEqual(len(draft.locator.unbound_table_parts), 1)
        self.assertEqual(
            [
                part.part.physical_segment_index
                for part in result.unassigned_table_parts
            ],
            [0, 1],
        )
        self.assertEqual(draft.locator.parts[0].physical_table_segment_indices, ())

    def test_improbable_ascii_glyph_map_marks_unit_for_review_without_rewriting(
        self,
    ) -> None:
        damaged = r"""!"#\$%&'()\* ,-./0123456%&'()\* 789:9;<=>?,@ABCDEFGHI"""
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, damaged),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.quality_status, "needs_review")
        self.assertEqual(draft.title, damaged)
        self.assertEqual(draft.heading_path, (damaged,))

    def test_ordinary_english_and_code_titles_do_not_trigger_glyph_review(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "EUSA Pharma / BGB-11417"),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.quality_status, "ok")

    def test_markup_only_non_cjk_title_is_reviewed_without_rewriting(self) -> None:
        damaged = "<sup>®</sup> BTK"
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, damaged),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.quality_status, "needs_review")
        self.assertEqual(draft.title, damaged)
        self.assertEqual(draft.heading_path, (damaged,))

    def test_markup_title_with_visible_chinese_is_not_flagged(self) -> None:
        intact = "百悦泽<sup>®</sup>"
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, intact),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )

        draft = build_provider_units(_admitted(document)).units[0]

        self.assertEqual(draft.quality_status, "ok")
        self.assertEqual(draft.title, intact)

    def test_payload_tampering_breaks_exact_search_replay(self) -> None:
        admitted = _admitted(_representative_document())
        draft = build_provider_units(admitted).units[1]
        payload = dict(draft.payload)
        parts = [dict(part) for part in cast(list[dict[str, object]], payload["parts"])]
        parts[0]["text"] = "伪造正文"
        payload["parts"] = parts
        tampered = replace(draft, payload=payload)
        body_binding = next(
            binding
            for binding in draft.locator.search_targets
            if binding.source.source_index == 2
        )

        with self.assertRaisesRegex(ValueError, "differs from its source"):
            replay_provider_unit_search_binding(admitted, tampered, body_binding)

    def test_search_replay_rejects_wrong_document_and_cross_part_binding(self) -> None:
        admitted = _admitted(_identical_text_parts_document())
        draft = build_provider_units(admitted).units[0]
        first, second = draft.locator.search_targets

        wrong_document = replace(
            draft,
            locator=replace(
                draft.locator,
                provider_document_sha256="sha256:" + "b" * 64,
            ),
        )
        with self.assertRaisesRegex(ValueError, "different document"):
            replay_provider_unit_search_binding(admitted, wrong_document, first)

        forged_binding = replace(first, destination=second.destination)
        forged = replace(
            draft,
            locator=replace(
                draft.locator,
                search_targets=(forged_binding, second),
            ),
        )
        with self.assertRaisesRegex(ValueError, "not owned by its mixed part"):
            replay_provider_unit_search_binding(admitted, forged, forged_binding)

    def test_search_replay_rejects_equal_text_from_a_different_field(self) -> None:
        admitted = _admitted(_table_with_equal_caption_and_footnote())
        draft = build_provider_units(admitted).units[0]
        caption, footnote = draft.locator.search_targets
        forged_binding = replace(caption, destination=footnote.destination)
        forged = replace(
            draft,
            locator=replace(
                draft.locator,
                search_targets=(forged_binding, footnote),
            ),
        )

        with self.assertRaisesRegex(ValueError, "differs from its source field"):
            replay_provider_unit_search_binding(admitted, forged, forged_binding)


def _representative_document() -> ProviderDocument:
    image_role = "image_0001"
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "header",
                    (ProviderPayload("text", None, "页眉"),),
                    annotation="page_header",
                ),
                _block(
                    1,
                    0,
                    "text",
                    (ProviderPayload("text", None, "第一章 标题"),),
                    annotation="title",
                    level=2,
                ),
                _block(
                    2,
                    0,
                    "text",
                    (ProviderPayload("text", None, "正文"),),
                    annotation="paragraph",
                ),
                _block(
                    3,
                    0,
                    "table",
                    (
                        ProviderPayload(
                            "table_body", None, "<table><td>甲</td></table>"
                        ),
                        ProviderPayload("table_caption", 0, "表一"),
                    ),
                    annotation="table",
                ),
            ),
            (
                _block(
                    4,
                    1,
                    "header",
                    (ProviderPayload("text", None, "页眉"),),
                    annotation="page_header",
                ),
                _block(5, 1, "table", (), annotation="table"),
                _block(
                    6,
                    1,
                    "image",
                    (ProviderPayload("content", None, ""),),
                    annotation="image",
                    artifact_roles=(image_role,),
                ),
                _block(
                    7,
                    1,
                    "text",
                    (ProviderPayload("text", None, "□适用 \uf052不适用"),),
                    annotation="title",
                    level=2,
                ),
            ),
        ),
        segments=(
            _segment(0, 0, "retained"),
            _segment(1, 0, "deleted"),
        ),
        extra_artifacts=(
            ProviderArtifact(
                role=image_role,
                relative_path="e_images/figure.jpg",
                sha256="sha256:" + "f" * 64,
                size_bytes=321,
                media_type="image/jpeg",
            ),
        ),
    )


def _visual_only_document(digest: str) -> ProviderDocument:
    artifact = ProviderArtifact(
        role="image_0001",
        relative_path="e_images/figure.jpg",
        sha256="sha256:" + digest * 64,
        size_bytes=321,
        media_type="image/jpeg",
    )
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "image",
                    (ProviderPayload("content", None, ""),),
                    annotation="image",
                    artifact_roles=(artifact.role,),
                ),
            ),
        ),
        segments=(),
        extra_artifacts=(artifact,),
    )


def _table_visual_only_document(digest: str) -> ProviderDocument:
    artifact = ProviderArtifact(
        role="table_crop_0001",
        relative_path="e_images/table.jpg",
        sha256="sha256:" + digest * 64,
        size_bytes=321,
        media_type="image/jpeg",
    )
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "table",
                    (
                        ProviderPayload("table_body", None, ""),
                        ProviderPayload("table_caption", 0, "只有表注"),
                    ),
                    annotation="table",
                ),
            ),
        ),
        segments=(
            _segment(
                0,
                0,
                "retained",
                crop_artifact_role=artifact.role,
            ),
        ),
        extra_artifacts=(artifact,),
    )


def _table_with_body_and_crop_document(digest: str) -> ProviderDocument:
    artifact = ProviderArtifact(
        role="table_crop_0001",
        relative_path="e_images/table.jpg",
        sha256="sha256:" + digest * 64,
        size_bytes=321,
        media_type="image/jpeg",
    )
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "table",
                    (
                        ProviderPayload(
                            "table_body",
                            None,
                            "<table><td>正文</td></table>",
                        ),
                    ),
                    annotation="table",
                ),
            ),
        ),
        segments=(
            _segment(
                0,
                0,
                "retained",
                crop_artifact_role=artifact.role,
            ),
        ),
        extra_artifacts=(artifact,),
    )


def _identical_text_parts_document() -> ProviderDocument:
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, "相同正文"),),
                    annotation="paragraph",
                ),
                _block(
                    1,
                    0,
                    "text",
                    (ProviderPayload("text", None, "相同正文"),),
                    annotation="paragraph",
                ),
            ),
        ),
        segments=(),
    )


def _table_with_equal_caption_and_footnote() -> ProviderDocument:
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "table",
                    (
                        ProviderPayload("table_caption", 0, "相同文字"),
                        ProviderPayload("table_footnote", 0, "相同文字"),
                    ),
                    annotation="table",
                ),
            ),
        ),
        segments=(_segment(0, 0, "retained"),),
    )


def _document(
    *,
    pages: tuple[tuple[ProviderBlock, ...], ...],
    segments: tuple[ProviderPhysicalTableSegment, ...],
    extra_artifacts: tuple[ProviderArtifact, ...] = (),
) -> ProviderDocument:
    provider_pages = tuple(
        ProviderPage(
            page_index=page_index,
            page_size=(600.0, 800.0),
            blocks=tuple(
                replace(block, order_in_page=order)
                for order, block in enumerate(blocks)
            ),
        )
        for page_index, blocks in enumerate(pages)
    )
    artifacts = tuple(
        sorted(
            (*_required_artifacts(), *extra_artifacts),
            key=lambda item: item.relative_path,
        )
    )
    return ProviderDocument(
        source_pdf_sha256=_SOURCE_SHA,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=provider_pages,
        physical_table_segments=segments,
        artifacts=artifacts,
        bundle_sha256=provider_artifact_bundle_sha256(artifacts),
    )


def _block(
    source_index: int,
    page_index: int,
    provider_type: str,
    payloads: tuple[ProviderPayload, ...],
    *,
    annotation: str | None,
    level: int | None = None,
    artifact_roles: tuple[str, ...] = (),
    bbox: ProviderBBox | None = None,
) -> ProviderBlock:
    raw = json.dumps(
        {
            "page_idx": page_index,
            "source_index": source_index,
            "type": provider_type,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProviderBlock(
        source_index=source_index,
        page_index=page_index,
        order_in_page=0,
        provider_type=provider_type,
        typed_annotation=annotation,
        provider_level=level,
        bbox=bbox,
        payloads=payloads,
        referenced_artifact_roles=artifact_roles,
        raw_item_json=raw,
        raw_item_sha256=_sha_text(raw),
    )


def _segment(
    page_index: int,
    order_in_page: int,
    status: str,
    *,
    crop_artifact_role: str | None = None,
) -> ProviderPhysicalTableSegment:
    raw = json.dumps(
        {"index": order_in_page, "page": page_index, "type": "table"},
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProviderPhysicalTableSegment(
        page_index=page_index,
        order_in_page=order_in_page,
        provider_index=order_in_page,
        bbox=None,
        page_local_html=f"<table><td>{page_index}:{order_in_page}</td></table>",
        crop_artifact_role=crop_artifact_role,
        logical_stream_status=status,  # type: ignore[arg-type]
        raw_segment_json=raw,
        raw_segment_sha256=_sha_text(raw),
    )


def _required_artifacts() -> tuple[ProviderArtifact, ...]:
    return tuple(
        ProviderArtifact(
            role=role,
            relative_path=relative_path,
            sha256="sha256:" + digest * 64,
            size_bytes=128,
            media_type="application/json",
        )
        for role, relative_path, digest in (
            ("content_list", "a_content_list.json", "b"),
            ("content_list_v2", "b_content_list_v2.json", "c"),
            ("middle_json", "c_middle.json", "d"),
            ("model_json", "d_model.json", "e"),
        )
    )


def _admitted(document: ProviderDocument) -> AdmittedProviderDocument:
    envelope = ProviderDocumentEnvelope.build(
        document_id=_DOCUMENT,
        artifact_owner_processing_run_id=_OWNER,
        provider="cninfo",
        provider_document_id="1225087169",
        source_pdf_relpath=(
            f"raw_documents/cninfo/000001/2026/1225087169/sha256_{'a' * 64}.pdf"
        ),
        source_pdf_page_count=len(document.pages),
        parser_artifact_root_relpath=(
            "parser_artifacts/cninfo/000001/1225087169/"
            f"{_OWNER}/sha256_{'a' * 64}/hybrid_auto"
        ),
        parser_target_identity=_target(),
        provider_document=document,
    )
    record = provider_document_envelope_to_bytes(envelope)
    return AdmittedProviderDocument(
        provider_document_relpath=Path(
            "derived/provider_documents/cninfo/000001/1225087169/"
            f"{_OWNER}/provider_document.v1.json"
        ),
        provider_document_sha256=_sha_bytes(record),
        envelope=envelope,
    )


def _target() -> ParserTargetIdentity:
    return ParserTargetIdentity(
        backend="hybrid-http-client",
        effort="medium",
        formula=True,
        full_pdf=True,
        image_analysis=False,
        language="ch",
        method="auto",
        name="MinerU",
        package_version="3.4.4",
        runtime_bundle_identity_sha256="sha256:" + "c" * 64,
        table=True,
    )


def _sha_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    unittest.main()
