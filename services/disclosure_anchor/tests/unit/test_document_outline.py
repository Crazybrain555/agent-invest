from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.document_outline import (
    HeadingHintSource,
    HeadingLevelHint,
    HeadingNegativeHint,
    HeadingNegativeReason,
)
from disclosure_anchor.application.contracts.provider_document import (
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
    provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.services.document_outline import (
    build_document_outline,
)


_SHA = "sha256:" + "a" * 64
_BUNDLE_SHA = provider_artifact_bundle_sha256(())


class DocumentOutlineTest(unittest.TestCase):
    def test_v2_paragraph_blocks_provider_level_fallback(self) -> None:
        document = _document(
            _block(0, "普通正文", annotation="paragraph", level=1),
            _block(1, "第一章 真标题", annotation="title", level=2),
            _block(2, "   ", annotation="title", level=2),
        )

        outline = build_document_outline(document)

        self.assertEqual([item.source_index for item in outline.candidates], [1])
        self.assertEqual([item.text for item in outline.headings], ["第一章 真标题"])
        self.assertEqual(outline.units[0].block_source_indices, (0,))
        self.assertEqual(outline.units[1].block_source_indices, (1, 2))

    def test_source_bound_hint_can_admit_provider_missed_title(self) -> None:
        document = _document(
            _block(0, "文档总标题", annotation="paragraph"),
            _block(1, "正文", annotation="paragraph"),
        )

        outline = build_document_outline(
            document,
            level_hints=(_level_hint(document, 0, "bookmark", 1),),
        )

        self.assertEqual(len(outline.headings), 1)
        self.assertEqual(outline.headings[0].placement_source, "bookmark")
        self.assertEqual(outline.units[0].block_source_indices, (0, 1))

    def test_style_hint_cannot_create_a_title_and_outline_hint_has_priority(
        self,
    ) -> None:
        document = _document(
            _block(0, "普通正文", annotation="paragraph"),
            _block(1, "已识别标题", annotation="title", level=4),
        )

        outline = build_document_outline(
            document,
            level_hints=(
                _level_hint(document, 0, "pdf_style", 1),
                _level_hint(document, 1, "pdf_style", 3),
                _level_hint(document, 1, "printed_toc", 2),
                _level_hint(document, 1, "bookmark", 1),
            ),
        )

        self.assertEqual([item.source_index for item in outline.candidates], [1])
        self.assertEqual(outline.headings[0].placement_source, "bookmark")
        self.assertEqual(outline.headings[0].nominal_rank, 1)

        style_only = build_document_outline(
            document,
            level_hints=(_level_hint(document, 1, "pdf_style", 3),),
        )
        self.assertEqual(style_only.headings[0].placement_source, "pdf_style")
        self.assertEqual(style_only.headings[0].nominal_rank, 3)

    def test_hard_negatives_demote_but_preserve_every_block(self) -> None:
        title_in_table = _block(
            0,
            "一、表内粗体小计",
            level=2,
            bbox=ProviderBBox(120, 120, 400, 160),
        )
        checkbox = _block(
            1,
            "□适用 \uf052不适用",
            level=2,
            bbox=ProviderBBox(100, 820, 900, 850),
        )
        continuation = _block(
            2,
            "跨页续句",
            level=2,
            bbox=ProviderBBox(100, 860, 900, 890),
        )
        math_title = _block(
            3,
            "√2 与 √3 的比较",
            level=2,
            bbox=ProviderBBox(100, 900, 900, 940),
        )
        body = _block(4, "完整正文", annotation="paragraph")
        document = _document(
            title_in_table,
            checkbox,
            continuation,
            math_title,
            body,
            segments=(
                ProviderPhysicalTableSegment(
                    page_index=0,
                    order_in_page=0,
                    provider_index=0,
                    bbox=ProviderBBox(100, 100, 900, 800),
                    page_local_html="<table></table>",
                    crop_artifact_role=None,
                    logical_stream_status="retained",
                    raw_segment_json="{}",
                    raw_segment_sha256=_SHA,
                ),
            ),
        )

        outline = build_document_outline(
            document,
            negative_hints=(_negative_hint(document, 2, "page_continuation"),),
        )

        self.assertEqual(
            [item.disposition for item in outline.candidates],
            ["demoted", "demoted", "demoted", "accepted"],
        )
        self.assertEqual(
            [item.disposition_reason for item in outline.candidates],
            ["table_contained", "checkbox_selector", "page_continuation", "accepted"],
        )
        self.assertEqual([heading.source_index for heading in outline.headings], [3])
        self.assertEqual(outline.units[0].block_source_indices, (0, 1, 2))
        self.assertEqual(outline.units[1].block_source_indices, (3, 4))

    def test_no_heading_has_one_preamble_and_empty_stub_is_not_dropped(self) -> None:
        document = _document(
            _block(0, "正文", annotation="paragraph"),
            _block(1, "", provider_type="table", annotation="table"),
        )

        first = build_document_outline(document)
        second = build_document_outline(document)

        self.assertEqual(first, second)
        self.assertEqual(first.headings, ())
        self.assertEqual(len(first.units), 1)
        self.assertEqual(first.units[0].block_source_indices, (0, 1))

    def test_duplicate_heading_text_and_private_use_glyph_remain_distinct(self) -> None:
        document = _document(
            _block(0, "一、标题\uf052", level=2),
            _block(1, "一、标题\uf052", level=2),
        )

        outline = build_document_outline(document)

        self.assertEqual(len(outline.headings), 2)
        self.assertNotEqual(
            outline.headings[0].heading_id, outline.headings[1].heading_id
        )
        self.assertEqual(outline.headings[0].text, outline.headings[1].text)
        self.assertIn("\uf052", outline.headings[0].text)

    def test_invalid_duplicate_or_stale_hints_fail_closed(self) -> None:
        document = _document(_block(0, "标题", annotation="paragraph"))

        with self.assertRaises(ValueError):
            HeadingNegativeHint(_SHA, 0, _SHA, "unknown")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            build_document_outline(
                document,
                level_hints=(HeadingLevelHint(_SHA, 4, _SHA, "bookmark", 1),),
            )
        with self.assertRaises(ValueError):
            build_document_outline(
                document,
                level_hints=(
                    _level_hint(document, 0, "bookmark", 1),
                    _level_hint(document, 0, "bookmark", 2),
                ),
            )
        with self.assertRaises(ValueError):
            build_document_outline(
                document,
                level_hints=(
                    HeadingLevelHint(
                        "sha256:" + "c" * 64,
                        0,
                        document.blocks[0].raw_item_sha256,
                        "bookmark",
                        1,
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            build_document_outline(
                document,
                level_hints=(
                    HeadingLevelHint(
                        document.source_pdf_sha256,
                        0,
                        "sha256:" + "c" * 64,
                        "bookmark",
                        1,
                    ),
                ),
            )

    def test_common_numbering_families_form_a_nested_stack(self) -> None:
        document = _document(
            _block(0, "第一章 总则", level=2),
            _block(1, "第一节 范围", level=2),
            _block(2, "一、境内", level=2),
            _block(3, "（一）业务", level=2),
            _block(4, "1、事项", level=2),
            _block(5, "（1）说明", level=2),
            _block(6, "2、另一事项", level=2),
            _block(7, "二、其他", level=2),
            _block(8, "第二章 附则", level=2),
        )

        outline = build_document_outline(
            document,
            level_hints=(_level_hint(document, 4, "pdf_style", 1),),
        )

        self.assertEqual(
            [heading.nominal_rank for heading in outline.headings],
            [1, 2, 3, 4, 5, 6, 5, 3, 1],
        )
        self.assertEqual(
            [heading.level for heading in outline.headings],
            [1, 2, 3, 4, 5, 6, 5, 3, 1],
        )
        self.assertEqual(outline.headings[4].placement_source, "numbering")
        self.assertEqual(
            outline.headings[7].parent_heading_id, outline.headings[1].heading_id
        )
        self.assertIsNone(outline.headings[8].parent_heading_id)

    def test_english_sections_and_roman_ordinals_form_sibling_levels(self) -> None:
        document = _document(
            _block(0, "V. Other related information", level=2),
            _block(1, "Section III Management Discussion and Analysis", level=2),
            _block(2, "I. Major businesses", level=2),
            _block(3, "II. Industry situation", level=2),
            _block(4, "1. Consumption field", level=2),
            _block(5, "2. Industrial field", level=2),
            _block(6, "III. Core competence analysis", level=2),
            _block(7, "Section IV Corporate Governance", level=2),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [heading.nominal_rank for heading in outline.headings],
            [2, 1, 2, 2, 5, 5, 2, 1],
        )
        self.assertEqual(
            outline.headings[3].headpath,
            (
                "Section III Management Discussion and Analysis",
                "II. Industry situation",
            ),
        )
        self.assertEqual(
            outline.headings[5].headpath,
            (
                "Section III Management Discussion and Analysis",
                "II. Industry situation",
                "2. Industrial field",
            ),
        )
        self.assertEqual(
            outline.headings[6].headpath,
            (
                "Section III Management Discussion and Analysis",
                "III. Core competence analysis",
            ),
        )
        self.assertIsNone(outline.headings[7].parent_heading_id)

    def test_lowercase_lettered_lists_are_not_roman_sections(self) -> None:
        document = _document(
            _block(0, "Section III Risk analysis", level=2),
            _block(1, "a) Market risk", level=2),
            _block(2, "b) Credit risk", level=2),
            _block(3, "c) Interest-rate risk", level=2),
            _block(4, "d) Liquidity risk", level=2),
            _block(5, "i. Global market development", level=2),
            _block(6, "iii. Domestic market development", level=2),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [heading.nominal_rank for heading in outline.headings],
            [1, 6, 6, 6, 6, 6, 6],
        )
        self.assertTrue(
            all(
                heading.parent_heading_id == outline.headings[0].heading_id
                for heading in outline.headings[1:]
            )
        )

    def test_provider_only_title_is_a_leaf_and_does_not_capture_numbered_sibling(
        self,
    ) -> None:
        document = _document(
            _block(0, "第一章 总则", level=2),
            _block(1, "未经结构证明的粗体说明", level=2),
            _block(2, "一、范围", level=2),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            outline.headings[1].parent_heading_id, outline.headings[0].heading_id
        )
        self.assertEqual(
            outline.headings[2].parent_heading_id, outline.headings[0].heading_id
        )
        self.assertNotEqual(
            outline.headings[2].parent_heading_id, outline.headings[1].heading_id
        )

    def test_dotted_numbered_siblings_escape_intervening_style_subheads(self) -> None:
        document = _document(
            _block(0, "4.6 资本充足率", level=2),
            _block(1, "高级法下资本充足率", level=2),
            _block(2, "权重法下资本充足率", level=2),
            _block(3, "4.7 其他重要业务指标", level=2),
        )
        outline = build_document_outline(
            document,
            level_hints=(
                _level_hint(document, 1, "pdf_style", 2),
                _level_hint(document, 2, "pdf_style", 2),
            ),
        )

        headings = {heading.text: heading for heading in outline.headings}
        capital = headings["4.6 资本充足率"]
        self.assertEqual(
            headings["高级法下资本充足率"].parent_heading_id,
            capital.heading_id,
        )
        self.assertEqual(
            headings["权重法下资本充足率"].parent_heading_id,
            capital.heading_id,
        )
        self.assertIsNone(headings["4.7 其他重要业务指标"].parent_heading_id)
        self.assertEqual(
            headings["4.7 其他重要业务指标"].headpath,
            ("4.7 其他重要业务指标",),
        )

    def test_repeated_single_column_indentation_can_reset_a_stale_numbered_parent(
        self,
    ) -> None:
        document = _paged_document(
            (
                _block(0, "前部大标题", level=2, bbox=ProviderBBox(150, 100, 300, 120)),
                _block(1, "前部小标题", level=2, bbox=ProviderBBox(190, 150, 340, 166)),
            ),
            (
                _block(2, "中部大标题", level=2, bbox=ProviderBBox(151, 100, 301, 120)),
                _block(3, "中部小标题", level=2, bbox=ProviderBBox(191, 150, 341, 166)),
                _block(4, "（三）旧编号父级", level=2),
            ),
            (
                _block(5, "后部大标题", level=2, bbox=ProviderBBox(149, 100, 299, 120)),
                _block(6, "后部小标题", level=2, bbox=ProviderBBox(189, 150, 339, 166)),
            ),
        )

        outline = build_document_outline(document)

        headings = {heading.text: heading for heading in outline.headings}
        self.assertEqual(headings["后部大标题"].placement_source, "provider_style")
        self.assertEqual(headings["后部大标题"].nominal_rank, 2)
        self.assertIsNone(headings["后部大标题"].parent_heading_id)
        self.assertEqual(
            headings["后部小标题"].parent_heading_id,
            headings["后部大标题"].heading_id,
        )

    def test_two_column_spacing_does_not_become_a_style_hierarchy(self) -> None:
        document = _paged_document(
            (
                _block(0, "左栏一", level=2, bbox=ProviderBBox(100, 100, 220, 120)),
                _block(1, "右栏一", level=2, bbox=ProviderBBox(500, 100, 620, 120)),
            ),
            (
                _block(2, "左栏二", level=2, bbox=ProviderBBox(101, 100, 221, 120)),
                _block(3, "右栏二", level=2, bbox=ProviderBBox(501, 100, 621, 120)),
            ),
            (
                _block(4, "左栏三", level=2, bbox=ProviderBBox(99, 100, 219, 120)),
                _block(5, "右栏三", level=2, bbox=ProviderBBox(499, 100, 619, 120)),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [heading.placement_source for heading in outline.headings],
            ["provider"] * 6,
        )
        self.assertTrue(
            all(heading.parent_heading_id is None for heading in outline.headings)
        )

    def test_equal_line_height_indent_clusters_remain_provider_leaves(self) -> None:
        document = _paged_document(
            (
                _block(0, "外层一", level=2, bbox=ProviderBBox(150, 100, 270, 120)),
                _block(1, "内层一", level=2, bbox=ProviderBBox(190, 150, 310, 170)),
            ),
            (
                _block(2, "外层二", level=2, bbox=ProviderBBox(151, 100, 271, 120)),
                _block(3, "内层二", level=2, bbox=ProviderBBox(191, 150, 311, 170)),
            ),
            (
                _block(4, "外层三", level=2, bbox=ProviderBBox(149, 100, 269, 120)),
                _block(5, "内层三", level=2, bbox=ProviderBBox(189, 150, 309, 170)),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [heading.placement_source for heading in outline.headings],
            ["provider"] * 6,
        )

    def test_symbol_only_provider_titles_are_demoted_without_dropping_text(
        self,
    ) -> None:
        document = _document(
            _block(0, "®", level=2),
            _block(1, "<sup>®</sup>▼", level=2),
            _block(2, "® BAT1706", level=2),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [candidate.disposition_reason for candidate in outline.candidates],
            ["non_semantic_glyph", "non_semantic_glyph", "accepted"],
        )
        self.assertEqual([heading.text for heading in outline.headings], ["® BAT1706"])
        self.assertEqual(outline.units[0].block_source_indices, (0, 1))

    def test_weak_short_title_conflicting_with_body_is_demoted_but_centered_title_survives(
        self,
    ) -> None:
        document = _document(
            _block(
                0,
                "无",
                annotation="title",
                level=2,
                bbox=ProviderBBox(90, 100, 112, 114),
            ),
            _block(
                1, "无", annotation="paragraph", bbox=ProviderBBox(90, 150, 112, 164)
            ),
            _block(
                2,
                "目录",
                annotation="title",
                level=2,
                bbox=ProviderBBox(460, 200, 520, 224),
            ),
            _block(
                3, "目录", annotation="paragraph", bbox=ProviderBBox(90, 250, 150, 274)
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [
                (item.source_index, item.disposition_reason)
                for item in outline.candidates
            ],
            [(0, "body_text_conflict"), (2, "accepted")],
        )
        self.assertEqual([heading.text for heading in outline.headings], ["目录"])
        self.assertEqual(outline.units[0].block_source_indices, (0, 1))

    def test_authoritative_hint_overrides_weak_body_text_conflict(self) -> None:
        document = _document(
            _block(
                0,
                "无",
                annotation="title",
                level=2,
                bbox=ProviderBBox(90, 100, 112, 114),
            ),
            _block(
                1, "无", annotation="paragraph", bbox=ProviderBBox(90, 150, 112, 164)
            ),
        )

        outline = build_document_outline(
            document,
            level_hints=(_level_hint(document, 0, "bookmark", 1),),
        )

        self.assertEqual(outline.candidates[0].disposition, "accepted")
        self.assertEqual(outline.headings[0].placement_source, "bookmark")

    def test_terminal_right_aligned_signature_followed_by_date_is_demoted(self) -> None:
        document = _document(
            _block(
                0,
                "示例股份有限公司董事会",
                annotation="title",
                level=1,
                bbox=ProviderBBox(541, 126, 811, 146),
            ),
            _block(
                1,
                "2025 年 4 月 22 日",
                annotation="paragraph",
                bbox=ProviderBBox(603, 154, 774, 173),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(outline.candidates[0].disposition_reason, "terminal_signature")
        self.assertEqual(outline.headings, ())
        self.assertEqual(outline.units[0].block_source_indices, (0, 1))

    def test_numbered_paragraph_can_only_complete_a_proved_sequence(self) -> None:
        document = _document(
            _block(
                0,
                "1、第一项",
                annotation="title",
                level=2,
                bbox=ProviderBBox(90, 100, 240, 116),
            ),
            _block(
                1,
                "2、第二项",
                annotation="title",
                level=2,
                bbox=ProviderBBox(90, 150, 240, 166),
            ),
            _block(
                2,
                "3、第三项",
                annotation="paragraph",
                bbox=ProviderBBox(88, 200, 240, 215),
            ),
            _block(
                3,
                "4、其他",
                annotation="paragraph",
                bbox=ProviderBBox(87, 250, 161, 265),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [item.source_index for item in outline.candidates], [0, 1, 2, 3]
        )
        self.assertEqual(
            [item.placement_source for item in outline.candidates],
            ["numbering", "numbering", "numbering", "numbering"],
        )
        self.assertEqual([heading.text for heading in outline.headings][-1], "4、其他")

    def test_numbered_body_list_or_style_mismatch_does_not_create_headings(
        self,
    ) -> None:
        plain_list = _document(
            _block(0, "1、事实一", annotation="paragraph"),
            _block(1, "2、事实二", annotation="paragraph"),
            _block(2, "3、事实三", annotation="paragraph"),
        )
        style_mismatch = _document(
            _block(
                0,
                "1、第一项",
                annotation="title",
                level=2,
                bbox=ProviderBBox(90, 100, 240, 116),
            ),
            _block(
                1,
                "2、第二项",
                annotation="title",
                level=2,
                bbox=ProviderBBox(90, 150, 240, 166),
            ),
            _block(
                2,
                "3、正文列表",
                annotation="paragraph",
                bbox=ProviderBBox(180, 200, 400, 240),
            ),
        )

        self.assertEqual(build_document_outline(plain_list).headings, ())
        mismatch_outline = build_document_outline(style_mismatch)
        self.assertEqual(
            [heading.source_index for heading in mismatch_outline.headings], [0, 1]
        )

    def test_page_leading_numbered_paragraph_between_siblings_is_admitted(
        self,
    ) -> None:
        document = _paged_document(
            (
                _block(0, "四、前一节", annotation="title", level=2),
                _block(1, "第四节正文", annotation="paragraph"),
            ),
            (
                _block(
                    2,
                    "五、这是一个换行后被 provider 降成 paragraph 的长标题",
                    annotation="paragraph",
                    bbox=ProviderBBox(90, 90, 900, 210),
                ),
                _block(3, "第五节正文", annotation="paragraph"),
            ),
            (
                _block(4, "六、后一节", annotation="title", level=2),
                _block(5, "第六节正文", annotation="paragraph"),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [heading.text for heading in outline.headings],
            [
                "四、前一节",
                "五、这是一个换行后被 provider 降成 paragraph 的长标题",
                "六、后一节",
            ],
        )
        self.assertEqual(outline.headings[1].placement_source, "numbering")

    def test_bracketed_numbered_body_is_not_admitted_without_page_leading_geometry(
        self,
    ) -> None:
        document = _paged_document(
            (_block(0, "四、前一节", annotation="title", level=2),),
            (
                _block(1, "页首正文", annotation="paragraph"),
                _block(2, "五、正文列表项", annotation="paragraph"),
            ),
            (_block(3, "六、后一节", annotation="title", level=2),),
        )

        outline = build_document_outline(document)

        self.assertEqual([heading.source_index for heading in outline.headings], [0, 3])

    def test_centered_page_leading_front_matter_resets_before_first_chapter(
        self,
    ) -> None:
        document = _paged_document(
            (
                _block(
                    0,
                    "文档封面标题",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(400, 300, 600, 324),
                ),
            ),
            (
                _block(
                    1,
                    "释义",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(430, 90, 570, 112),
                ),
                _block(2, "一、普通术语", annotation="title", level=2),
            ),
            (
                _block(
                    3,
                    "重大事项提示",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(410, 90, 590, 112),
                ),
                _block(4, "提示正文", annotation="paragraph"),
            ),
            (
                _block(5, "第一节 正文", annotation="title", level=2),
                _block(6, "一、主题", annotation="title", level=2),
            ),
            (
                _block(
                    7,
                    "财务报表名称",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(410, 90, 590, 112),
                ),
            ),
        )

        outline = build_document_outline(document)
        headings = {heading.text: heading for heading in outline.headings}

        self.assertEqual(
            headings["一、普通术语"].parent_heading_id,
            headings["释义"].heading_id,
        )
        self.assertIsNone(headings["重大事项提示"].parent_heading_id)
        self.assertEqual(
            headings["重大事项提示"].placement_source,
            "provider_style",
        )
        self.assertIsNone(headings["第一节 正文"].parent_heading_id)
        self.assertEqual(
            headings["财务报表名称"].parent_heading_id,
            headings["一、主题"].heading_id,
        )
        self.assertEqual(
            headings["财务报表名称"].placement_source,
            "provider",
        )

    def test_section_remains_below_chapter_when_higher_division_exists(
        self,
    ) -> None:
        outline = build_document_outline(
            _document(
                _block(0, "第一章 总则", annotation="title", level=2),
                _block(1, "第一节 范围", annotation="title", level=2),
                _block(2, "第二节 原则", annotation="title", level=2),
            )
        )
        headings = {heading.text: heading for heading in outline.headings}

        self.assertEqual(
            headings["第一节 范围"].parent_heading_id,
            headings["第一章 总则"].heading_id,
        )
        self.assertEqual(
            headings["第二节 原则"].parent_heading_id,
            headings["第一章 总则"].heading_id,
        )

    def test_whitespace_can_stand_for_a_dropped_numbering_separator(self) -> None:
        document = _document(
            _block(0, "2001 年年度报告", level=1),
            _block(1, "第二节 数据摘要", level=2),
            _block(2, "三 审计差异", level=2),
            _block(3, "1 境内审计数", level=2),
            _block(4, "2 境外审计数", level=2),
            _block(5, "第三节 业务数据", level=2),
            _block(6, "(一)前三年数据", level=2),
            _block(7, "二 年均指标", level=2),
            _block(8, "三 分支机构", level=2),
        )

        outline = build_document_outline(document)
        headings = {heading.text: heading for heading in outline.headings}

        self.assertEqual(headings["2001 年年度报告"].placement_source, "provider")
        self.assertEqual(
            headings["1 境内审计数"].headpath,
            ("第二节 数据摘要", "三 审计差异", "1 境内审计数"),
        )
        self.assertEqual(
            headings["2 境外审计数"].headpath,
            ("第二节 数据摘要", "三 审计差异", "2 境外审计数"),
        )
        self.assertEqual(
            headings["二 年均指标"].headpath,
            ("第三节 业务数据", "二 年均指标"),
        )
        self.assertEqual(
            headings["三 分支机构"].headpath,
            ("第三节 业务数据", "三 分支机构"),
        )

    def test_repeated_page_leading_provider_title_is_page_furniture(self) -> None:
        repeated = "公司对会计报表相关的内部控制"
        document = _paged_document(
            (
                _block(0, repeated, level=1, bbox=ProviderBBox(200, 50, 500, 90)),
                _block(1, "第一页正文", annotation="paragraph"),
            ),
            (
                _block(2, repeated, level=1, bbox=ProviderBBox(201, 51, 501, 91)),
                _block(3, "第二页正文", annotation="paragraph"),
            ),
            (
                _block(4, repeated, level=1, bbox=ProviderBBox(199, 49, 499, 89)),
                _block(5, "第三页正文", annotation="paragraph"),
            ),
            (
                _block(6, repeated, level=1, bbox=ProviderBBox(200, 400, 500, 440)),
                _block(7, "真正章节正文", annotation="paragraph"),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [candidate.disposition_reason for candidate in outline.candidates],
            [
                "accepted",
                "repeated_page_header",
                "repeated_page_header",
                "accepted",
            ],
        )
        self.assertEqual([heading.source_index for heading in outline.headings], [0, 6])
        self.assertEqual(outline.units[0].block_source_indices, (0, 1, 2, 3, 4, 5))

    def test_two_similar_page_leading_titles_are_not_enough_for_demotion(
        self,
    ) -> None:
        document = _paged_document(
            (_block(0, "独立标题", level=1, bbox=ProviderBBox(200, 50, 500, 90)),),
            (_block(1, "独立标题", level=1, bbox=ProviderBBox(201, 51, 501, 91)),),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [candidate.disposition_reason for candidate in outline.candidates],
            ["accepted", "accepted"],
        )

    def test_repeated_titles_below_normalized_top_quarter_are_not_headers(
        self,
    ) -> None:
        document = _paged_document(
            (
                _block(
                    0,
                    "页面中部的重复标题",
                    level=1,
                    bbox=ProviderBBox(200, 260, 500, 300),
                ),
            ),
            (
                _block(
                    1,
                    "页面中部的重复标题",
                    level=1,
                    bbox=ProviderBBox(201, 261, 501, 301),
                ),
            ),
            (
                _block(
                    2,
                    "页面中部的重复标题",
                    level=1,
                    bbox=ProviderBBox(199, 259, 499, 299),
                ),
            ),
            page_size=(600.0, 2_000.0),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [candidate.disposition_reason for candidate in outline.candidates],
            ["accepted", "accepted", "accepted"],
        )

    def test_repeated_standalone_cover_titles_remain_headings(self) -> None:
        pages: list[tuple[ProviderBlock, ...]] = [() for _ in range(11)]
        for source_index, page_index in enumerate((0, 5, 10)):
            pages[page_index] = (
                _block(
                    source_index,
                    "同一机构的独立文件封面",
                    level=1,
                    bbox=ProviderBBox(200, 50, 500, 90),
                ),
            )
        document = _paged_document(*pages)

        outline = build_document_outline(document)

        self.assertEqual(
            [candidate.disposition_reason for candidate in outline.candidates],
            ["accepted", "accepted", "accepted"],
        )

    def test_outline_record_rejects_missing_or_inconsistent_resolved_heading(
        self,
    ) -> None:
        outline = build_document_outline(
            _document(
                _block(0, "第一章 总则", level=2),
                _block(1, "一、范围", level=2),
            )
        )

        with self.assertRaises(ValueError):
            replace(outline, headings=outline.headings[:1])
        with self.assertRaises(ValueError):
            replace(
                outline,
                headings=(
                    outline.headings[0],
                    replace(outline.headings[1], level=1),
                ),
            )
        forged_heading = replace(
            outline.headings[1],
            text="伪造标题",
            headpath=(*outline.headings[0].headpath, "伪造标题"),
        )
        forged_unit = replace(
            outline.units[1],
            title="伪造标题",
            headpath=forged_heading.headpath,
        )
        with self.assertRaises(ValueError):
            replace(
                outline,
                headings=(outline.headings[0], forged_heading),
                units=(outline.units[0], forged_unit),
            )
        with self.assertRaises(ValueError):
            replace(
                outline,
                units=(
                    replace(
                        outline.units[0],
                        block_source_indices=(0, 1),
                    ),
                ),
            )


def _block(
    source_index: int,
    text: str,
    *,
    annotation: str | None = None,
    level: int | None = None,
    provider_type: str = "text",
    bbox: ProviderBBox | None = None,
) -> ProviderBlock:
    return ProviderBlock(
        source_index=source_index,
        page_index=0,
        order_in_page=source_index,
        provider_type=provider_type,
        typed_annotation=annotation,
        provider_level=level,
        bbox=bbox
        or ProviderBBox(100, 100 + source_index * 50, 900, 140 + source_index * 50),
        payloads=() if not text else (ProviderPayload("text", None, text),),
        referenced_artifact_roles=(),
        raw_item_json="{}",
        raw_item_sha256=_SHA,
    )


def _document(
    *blocks: ProviderBlock,
    segments: tuple[ProviderPhysicalTableSegment, ...] = (),
) -> ProviderDocument:
    return _paged_document(tuple(blocks), segments=segments)


def _paged_document(
    *page_blocks: tuple[ProviderBlock, ...],
    segments: tuple[ProviderPhysicalTableSegment, ...] = (),
    page_size: tuple[float, float] = (600.0, 800.0),
) -> ProviderDocument:
    pages = tuple(
        ProviderPage(
            page_index=page_index,
            page_size=page_size,
            blocks=tuple(
                replace(
                    block,
                    page_index=page_index,
                    order_in_page=order,
                )
                for order, block in enumerate(blocks)
            ),
        )
        for page_index, blocks in enumerate(page_blocks)
    )
    return ProviderDocument(
        source_pdf_sha256=_SHA,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=pages,
        physical_table_segments=segments,
        artifacts=(),
        bundle_sha256=_BUNDLE_SHA,
    )


def _level_hint(
    document: ProviderDocument,
    source_index: int,
    source: HeadingHintSource,
    level: int,
) -> HeadingLevelHint:
    return HeadingLevelHint(
        document.source_pdf_sha256,
        source_index,
        document.blocks[source_index].raw_item_sha256,
        source,
        level,
    )


def _negative_hint(
    document: ProviderDocument,
    source_index: int,
    reason: HeadingNegativeReason,
) -> HeadingNegativeHint:
    return HeadingNegativeHint(
        document.source_pdf_sha256,
        source_index,
        document.blocks[source_index].raw_item_sha256,
        reason,
    )


if __name__ == "__main__":
    unittest.main()
