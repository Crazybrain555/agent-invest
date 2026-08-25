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
    def test_untyped_prompt_selector_does_not_invent_a_heading(self) -> None:
        template = (
            "报告期内公司经营情况的重大变化，以及报告期内发生的对公司"
            "经营情况有重大影响和预计未来会有重大影响的事项"
        )
        document = _document(
            _block(0, "二、分红、回购与增持", annotation="title", level=2),
            _block(1, "公司完成股份回购。", annotation="paragraph"),
            _block(2, template, annotation="paragraph"),
            _block(3, "□适用 √不适用", annotation="paragraph"),
            _block(4, "三、核心竞争力分析", annotation="title", level=2),
            _block(5, "核心竞争力保持稳定。", annotation="paragraph"),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [unit.block_source_indices for unit in outline.units],
            [(0, 1, 2, 3), (4, 5)],
        )
        self.assertEqual(outline.units[0].title, "二、分红、回购与增持")
        self.assertNotIn(
            "statutory_template",
            {heading.placement_source for heading in outline.headings},
        )

        approximate = build_document_outline(
            _document(
                _block(0, "二、分红、回购与增持", annotation="title", level=2),
                _block(1, f"{template}说明", annotation="paragraph"),
                _block(2, "□适用 √不适用", annotation="paragraph"),
                _block(3, "三、核心竞争力分析", annotation="title", level=2),
            )
        )
        self.assertEqual(
            [unit.block_source_indices for unit in approximate.units],
            [(0, 1, 2), (3,)],
        )

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

    def test_same_page_exchange_identity_preamble_joins_first_heading(self) -> None:
        document = _document(
            _block(0, "证券代码：002714", annotation="paragraph"),
            _block(1, "证券简称：牧原股份", annotation="paragraph"),
            _block(2, "公告编号：2026-067", annotation="paragraph"),
            _block(3, "2025年度报告", annotation="title", level=1),
            _block(4, "报告正文", annotation="paragraph"),
        )

        outline = build_document_outline(document)

        self.assertEqual(len(outline.units), 1)
        self.assertEqual(outline.units[0].title, "2025年度报告")
        self.assertEqual(outline.units[0].block_source_indices, (0, 1, 2, 3, 4))

    def test_unknown_or_cross_page_front_matter_remains_a_preamble(self) -> None:
        unknown = build_document_outline(
            _document(
                _block(0, "证券代码相关说明：详见正文", annotation="paragraph"),
                _block(1, "2025年度报告", annotation="title", level=1),
            )
        )
        sentence_value = build_document_outline(
            _document(
                _block(
                    0,
                    "证券代码：002714；本公告以交易所披露为准",
                    annotation="paragraph",
                ),
                _block(1, "2025年度报告", annotation="title", level=1),
            )
        )
        cross_page = build_document_outline(
            _paged_document(
                (_block(0, "证券代码：002714", annotation="paragraph"),),
                (_block(1, "2025年度报告", annotation="title", level=1),),
            )
        )

        self.assertEqual(
            [unit.block_source_indices for unit in unknown.units],
            [(0,), (1,)],
        )
        self.assertEqual(
            [unit.block_source_indices for unit in sentence_value.units],
            [(0,), (1,)],
        )
        self.assertEqual(
            [unit.block_source_indices for unit in cross_page.units],
            [(0,), (1,)],
        )

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

    def test_bare_applicability_statement_is_body_not_a_heading(self) -> None:
        document = _document(
            _block(0, "20、 投资性房地产", annotation="title", level=2),
            _block(1, "不适用", annotation="title", level=3),
            _block(2, "21、 固定资产", annotation="title", level=2),
            _block(3, "适用范围", annotation="title", level=3),
            _block(4, "一、适用", annotation="title", level=3),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [item.disposition_reason for item in outline.candidates],
            [
                "accepted",
                "selector_statement",
                "accepted",
                "accepted",
                "accepted",
            ],
        )
        self.assertEqual(
            [(item.title, item.block_source_indices) for item in outline.units],
            [
                ("20、 投资性房地产", (0, 1)),
                ("21、 固定资产", (2,)),
                ("适用范围", (3,)),
                ("一、适用", (4,)),
            ],
        )

    def test_strong_numbered_table_caption_is_a_source_bound_heading(self) -> None:
        document = _document(
            _block(0, "第四节 公司治理、环境和社会", annotation="title", level=1),
            _table_block(
                1,
                caption=(
                    "四、纳入环境信息依法披露企业名单的上市公司及其主要"
                    "子公司的环境信息情况√适用 □不适用"
                ),
            ),
            _block(
                2,
                "(一) 在报告期内为减少污染物排放所采取的措施",
                annotation="title",
                level=2,
            ),
            segments=(
                ProviderPhysicalTableSegment(
                    page_index=0,
                    order_in_page=0,
                    provider_index=0,
                    bbox=ProviderBBox(100, 150, 900, 190),
                    page_local_html="<table><td>企业数量</td><td>9</td></table>",
                    crop_artifact_role=None,
                    logical_stream_status="retained",
                    raw_segment_json="{}",
                    raw_segment_sha256=_SHA,
                ),
            ),
        )

        outline = build_document_outline(document)

        caption = outline.headings[1]
        child = outline.headings[2]
        self.assertEqual(caption.source_index, 1)
        self.assertEqual(caption.payload_ordinal, 1)
        self.assertEqual(caption.placement_source, "numbering")
        self.assertEqual(caption.parent_heading_id, outline.headings[0].heading_id)
        self.assertEqual(child.parent_heading_id, caption.heading_id)
        self.assertEqual(outline.units[1].block_source_indices, (1,))

    def test_bound_page_continuation_demotes_numbered_table_caption(self) -> None:
        document = _document(
            _table_block(0, caption="四、环境信息（续）"),
            _block(1, "本页继续列示上一页环境数据。", annotation="paragraph"),
        )

        outline = build_document_outline(
            document,
            negative_hints=(_negative_hint(document, 0, "page_continuation"),),
        )

        self.assertEqual(len(outline.candidates), 1)
        self.assertEqual(outline.candidates[0].disposition, "demoted")
        self.assertEqual(
            outline.candidates[0].disposition_reason,
            "page_continuation",
        )
        self.assertFalse(outline.headings)
        self.assertEqual(outline.units[0].block_source_indices, (0, 1))

    def test_ordinary_or_incidental_table_captions_do_not_open_sections(self) -> None:
        document = _document(
            _table_block(0, caption="表4 环境信息"),
            _table_block(1, caption="本表说明四、环境风险"),
            _table_block(2, caption="√适用 □不适用"),
            _table_block(3, caption="(一) 普通表内分组"),
        )

        outline = build_document_outline(document)

        self.assertFalse(outline.candidates)
        self.assertEqual(outline.units[0].block_source_indices, (0, 1, 2, 3))

    def test_repeated_page_leading_table_labels_recover_statement_boundaries(
        self,
    ) -> None:
        document = _paged_document(
            (
                _block(
                    0,
                    "合并资产负债表",
                    annotation="paragraph",
                    bbox=ProviderBBox(420, 80, 580, 104),
                ),
                _block(
                    1,
                    "2025年12月31日",
                    annotation="paragraph",
                    bbox=ProviderBBox(430, 112, 570, 132),
                ),
                _table_block(
                    2,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
            (
                _block(
                    3,
                    "母公司资产负债表",
                    annotation="paragraph",
                    bbox=ProviderBBox(405, 82, 595, 106),
                ),
                _block(
                    4,
                    "单位：人民币元",
                    annotation="paragraph",
                    bbox=ProviderBBox(720, 115, 900, 135),
                ),
                _table_block(
                    5,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
            (
                _block(
                    6,
                    "合并利润表",
                    annotation="paragraph",
                    bbox=ProviderBBox(435, 79, 565, 103),
                ),
                _table_block(
                    7,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [heading.text for heading in outline.headings],
            ["合并资产负债表", "母公司资产负债表", "合并利润表"],
        )
        self.assertTrue(
            all(
                heading.placement_source == "table_label"
                for heading in outline.headings
            )
        )
        self.assertEqual(
            [unit.block_source_indices for unit in outline.units],
            [(0, 1, 2), (3, 4, 5), (6, 7)],
        )

    def test_isolated_or_continuation_table_label_remains_body(self) -> None:
        document = _paged_document(
            (
                _block(
                    0,
                    "单页孤立表名",
                    annotation="paragraph",
                    bbox=ProviderBBox(420, 80, 580, 104),
                ),
                _table_block(
                    1,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
            (
                _block(
                    2,
                    "合并资产负债表（续）",
                    annotation="paragraph",
                    bbox=ProviderBBox(410, 80, 590, 104),
                ),
                _table_block(
                    3,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
            (
                _block(
                    4,
                    "合并资产负债表（续）",
                    annotation="paragraph",
                    bbox=ProviderBBox(410, 80, 590, 104),
                ),
                _table_block(
                    5,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
            (
                _block(
                    6,
                    "合并资产负债表（续）",
                    annotation="paragraph",
                    bbox=ProviderBBox(410, 80, 590, 104),
                ),
                _table_block(
                    7,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
        )

        outline = build_document_outline(document)

        self.assertFalse(outline.headings)
        self.assertEqual(
            outline.units[0].block_source_indices,
            tuple(range(8)),
        )

    def test_source_table_container_closes_stale_parent_and_owns_labels(
        self,
    ) -> None:
        document = _paged_document(
            (
                _block(0, "第九节 财务报告", annotation="title", level=2),
                _block(1, "七、注册会计师审计责任 -续", annotation="title", level=2),
            ),
            (
                _block(2, "财务报表", annotation="paragraph", bbox=ProviderBBox(430, 50, 570, 70)),
                _block(3, "2025年12月31日", annotation="paragraph", bbox=ProviderBBox(430, 75, 570, 95)),
                _block(4, "合并资产负债表", annotation="paragraph", bbox=ProviderBBox(420, 105, 580, 129)),
                _table_block(5, caption="", bbox=ProviderBBox(80, 150, 920, 700)),
            ),
            (
                _block(6, "母公司资产负债表", annotation="paragraph", bbox=ProviderBBox(410, 105, 590, 129)),
                _table_block(7, caption="", bbox=ProviderBBox(80, 150, 920, 700)),
            ),
            (
                _block(8, "合并利润表", annotation="paragraph", bbox=ProviderBBox(430, 105, 570, 129)),
                _table_block(9, caption="", bbox=ProviderBBox(80, 150, 920, 700)),
            ),
            (_block(10, "(一) 公司基本情况", annotation="title", level=2),),
        )

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }

        financial = headings["第九节 财务报告"]
        statements = headings["财务报表"]
        self.assertEqual(statements.parent_heading_id, financial.heading_id)
        self.assertEqual(
            headings["合并资产负债表"].parent_heading_id,
            statements.heading_id,
        )
        self.assertEqual(
            headings["(一) 公司基本情况"].parent_heading_id,
            statements.heading_id,
        )

    def test_table_label_geometry_does_not_pop_an_active_numbered_parent(self) -> None:
        document = _paged_document(
            (
                _block(0, "第一章 业务情况", annotation="title", level=1),
                _block(1, "一、当前章节", annotation="title", level=2),
            ),
            (
                _block(2, "表A", annotation="paragraph", bbox=ProviderBBox(440, 80, 560, 104)),
                _table_block(3, caption="", bbox=ProviderBBox(80, 150, 920, 700)),
            ),
            (
                _block(4, "表B", annotation="paragraph", bbox=ProviderBBox(440, 80, 560, 104)),
                _table_block(5, caption="", bbox=ProviderBBox(80, 150, 920, 700)),
            ),
            (
                _block(6, "表C", annotation="paragraph", bbox=ProviderBBox(440, 80, 560, 104)),
                _table_block(7, caption="", bbox=ProviderBBox(80, 150, 920, 700)),
            ),
        )

        headings = {item.text: item for item in build_document_outline(document).headings}

        for label in ("表A", "表B", "表C"):
            self.assertEqual(
                headings[label].parent_heading_id,
                headings["一、当前章节"].heading_id,
            )

    def test_repeated_financial_statement_carrier_opens_one_container(self) -> None:
        document = _paged_document(
            (
                _block(0, "财务报表", annotation="paragraph", bbox=ProviderBBox(440, 50, 560, 70)),
                _block(1, "合并资产负债表", annotation="paragraph", bbox=ProviderBBox(420, 100, 580, 124)),
                _table_block(2, caption="", bbox=ProviderBBox(80, 150, 920, 700)),
            ),
            (
                _block(3, "财务报表", annotation="paragraph", bbox=ProviderBBox(440, 50, 560, 70)),
                _block(4, "母公司资产负债表", annotation="paragraph", bbox=ProviderBBox(410, 100, 590, 124)),
                _table_block(5, caption="", bbox=ProviderBBox(80, 150, 920, 700)),
            ),
            (
                _block(6, "财务报表", annotation="paragraph", bbox=ProviderBBox(440, 50, 560, 70)),
                _block(7, "合并利润表", annotation="paragraph", bbox=ProviderBBox(430, 100, 570, 124)),
                _table_block(8, caption="", bbox=ProviderBBox(80, 150, 920, 700)),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [heading.source_index for heading in outline.headings if heading.text == "财务报表"],
            [0],
        )
        container = next(heading for heading in outline.headings if heading.source_index == 0)
        self.assertTrue(
            all(
                heading.parent_heading_id == container.heading_id
                for heading in outline.headings
                if heading.source_index in {1, 4, 7}
            )
        )

    def test_post_table_title_matching_typed_footnote_is_demoted(self) -> None:
        document = _paged_document(
            (
                _table_block(
                    0,
                    caption="",
                    footnote="附注为财务报表的组成部分",
                    bbox=ProviderBBox(80, 100, 920, 650),
                ),
                _block(
                    1,
                    "附注为财务报表的组成部分",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(380, 700, 620, 724),
                ),
            ),
            (
                _block(
                    2,
                    "附注为财务报表的组成部分",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(380, 80, 620, 104),
                ),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [candidate.disposition_reason for candidate in outline.candidates],
            ["table_footnote_conflict", "accepted"],
        )
        self.assertEqual([heading.source_index for heading in outline.headings], [2])

    def test_parenthesized_paragraphs_need_source_proved_sequence(self) -> None:
        document = _document(
            _block(0, "一、实施情况概要", annotation="title", level=2),
            _block(
                1,
                "（一）激励计划简介",
                annotation="paragraph",
                bbox=ProviderBBox(194, 150, 500, 168),
            ),
            _block(
                2,
                "（二）首次授予情况",
                annotation="title",
                level=2,
                bbox=ProviderBBox(194, 200, 500, 218),
            ),
            _block(3, "（1）授予日期", annotation="title", level=2),
            _block(
                4,
                "（三）预留授予情况",
                annotation="title",
                level=2,
                bbox=ProviderBBox(194, 300, 500, 318),
            ),
            _block(
                5,
                "（四）数量及价格变动情况",
                annotation="paragraph",
                bbox=ProviderBBox(194, 350, 620, 368),
            ),
        )

        headings = build_document_outline(document).headings

        self.assertEqual([heading.source_index for heading in headings], list(range(6)))
        self.assertEqual(headings[1].parent_heading_id, headings[0].heading_id)
        self.assertEqual(headings[5].parent_heading_id, headings[0].heading_id)

    def test_parenthesized_first_child_does_not_restart_after_existing_sibling(
        self,
    ) -> None:
        document = _document(
            _block(0, "一、父章节", annotation="title", level=2),
            _block(1, "（九）已有子项", annotation="title", level=2),
            _block(
                2,
                "（一）正文内重启枚举",
                annotation="paragraph",
                bbox=ProviderBBox(194, 250, 500, 268),
            ),
            _block(
                3,
                "（二）正文内下一枚举",
                annotation="title",
                level=2,
                bbox=ProviderBBox(194, 300, 500, 318),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual([heading.source_index for heading in outline.headings], [0, 1, 3])
        self.assertNotIn(2, [candidate.source_index for candidate in outline.candidates])

    def test_multi_gap_parenthesized_sequence_is_atomic(self) -> None:
        valid = _document(
            _block(0, "（八）回购义务", annotation="title", level=2),
            _block(1, "（九）影响分析", annotation="paragraph"),
            _block(2, "（十）增减持计划", annotation="paragraph"),
            _block(3, "（十一）审议程序", annotation="title", level=2),
        )
        invalid = _document(
            _block(0, "（八）回购义务", annotation="title", level=2),
            _block(1, "（九）影响分析", annotation="paragraph"),
            _block(2, "（九）重复项", annotation="paragraph"),
            _block(3, "（十一）审议程序", annotation="title", level=2),
        )

        self.assertEqual(
            [heading.source_index for heading in build_document_outline(valid).headings],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [heading.source_index for heading in build_document_outline(invalid).headings],
            [0, 3],
        )

    def test_numbered_complete_sentence_stays_body(self) -> None:
        document = _document(
            _block(0, "（一）预告类型", annotation="title", level=2),
            _block(1, "（二）业绩预告情况", annotation="title", level=2),
            _block(
                2,
                "（三）本次所预计的业绩未经注册会计师审计。",
                annotation="paragraph",
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual([heading.source_index for heading in outline.headings], [0, 1])
        self.assertEqual(outline.units[-1].block_source_indices, (1, 2))

    def test_parenthesized_table_caption_between_siblings_opens_section(self) -> None:
        document = _document(
            _block(0, "（三）投资者保护", annotation="title", level=2),
            _table_block(1, caption="（四）中介机构的情况"),
            _block(2, "报告期内上述机构是否发生变化", annotation="title"),
            _block(3, "（五）募集资金使用情况", annotation="title", level=2),
        )

        outline = build_document_outline(document)
        headings = {heading.text: heading for heading in outline.headings}

        caption = headings["（四）中介机构的情况"]
        self.assertEqual(caption.payload_ordinal, 1)
        self.assertEqual(
            headings["报告期内上述机构是否发生变化"].parent_heading_id,
            caption.heading_id,
        )
        self.assertIsNone(headings["（五）募集资金使用情况"].parent_heading_id)

    def test_dotted_table_caption_requires_predecessor_and_nonempty_title(self) -> None:
        valid = _document(
            _block(0, "3.5 分部经营数据", annotation="title", level=2),
            _block(1, "3.5.1 存贷款情况", annotation="title", level=2),
            _table_block(2, caption="3.5.2 资产质量"),
            segments=(
                ProviderPhysicalTableSegment(
                    page_index=0,
                    order_in_page=0,
                    provider_index=0,
                    bbox=ProviderBBox(100, 200, 900, 300),
                    page_local_html="<table><td>不良贷款率</td><td>1%</td></table>",
                    crop_artifact_role=None,
                    logical_stream_status="retained",
                    raw_segment_json="{}",
                    raw_segment_sha256=_SHA,
                ),
            ),
        )
        bare = _document(
            _block(0, "3.5 分部经营数据", annotation="title", level=2),
            _block(1, "3.5.1 存贷款情况", annotation="title", level=2),
            _table_block(2, caption="3.5.2"),
            segments=valid.physical_table_segments,
        )
        missing_predecessor = _document(
            _block(0, "3.5 分部经营数据", annotation="title", level=2),
            _table_block(1, caption="3.5.2 资产质量"),
            segments=(
                valid.physical_table_segments[0],
            ),
        )

        valid_outline = build_document_outline(valid)
        self.assertIn("3.5.2 资产质量", [item.text for item in valid_outline.headings])
        self.assertNotIn(
            "3.5.2",
            [item.text for item in build_document_outline(bare).headings],
        )
        self.assertNotIn(
            "3.5.2 资产质量",
            [item.text for item in build_document_outline(missing_predecessor).headings],
        )

    def test_wrapped_numbered_title_keeps_both_source_fragments(self) -> None:
        document = _document(
            _block(
                0,
                "（七）担保情况、偿债计划及其他偿债保障措施在报告期内的执行情况及对债",
                annotation="title",
                level=2,
                bbox=ProviderBBox(100, 100, 900, 119),
            ),
            _block(
                1,
                "券投资者权益的影响",
                annotation="paragraph",
                bbox=ProviderBBox(150, 127, 350, 145),
            ),
            _block(
                2,
                "√适用□不适用",
                annotation="paragraph",
                bbox=ProviderBBox(100, 160, 300, 178),
            ),
        )

        outline = build_document_outline(document)

        heading = outline.headings[0]
        self.assertEqual(
            heading.text,
            "（七）担保情况、偿债计划及其他偿债保障措施在报告期内的执行情况及对债券投资者权益的影响",
        )
        self.assertEqual(
            [fragment.source_index for fragment in heading.source_fragments],
            [0, 1],
        )
        self.assertEqual(outline.units[0].block_source_indices, (0, 1, 2))

    def test_wrapped_numbered_title_does_not_consume_a_colon_lead_in(self) -> None:
        for tail in ("投资者应特别关注：", "Investors should note:"):
            with self.subTest(tail=tail):
                document = _document(
                    _block(
                        0,
                        "（一）重大风险提示",
                        annotation="title",
                        level=2,
                        bbox=ProviderBBox(100, 100, 900, 119),
                    ),
                    _block(
                        1,
                        tail,
                        annotation="paragraph",
                        bbox=ProviderBBox(150, 127, 350, 145),
                    ),
                    _block(
                        2,
                        "公司存在流动性压力。",
                        annotation="paragraph",
                        bbox=ProviderBBox(100, 160, 900, 178),
                    ),
                )

                heading = build_document_outline(document).headings[0]

                self.assertEqual(heading.text, "（一）重大风险提示")
                self.assertEqual(
                    [fragment.source_index for fragment in heading.source_fragments],
                    [0],
                )

    def test_wrapped_parenthesized_sibling_needs_immediate_selector(self) -> None:
        def candidate(selector: bool) -> ProviderDocument:
            blocks = [
                _block(0, "（五）募集资金", annotation="title", level=2),
                _block(1, "（六）信用评级", annotation="title", level=2),
                _block(
                    2,
                    "（七）担保情况、偿债计划和保障措施",
                    annotation="paragraph",
                    bbox=ProviderBBox(100, 200, 900, 260),
                ),
            ]
            if selector:
                blocks.append(
                    _block(3, "√适用□不适用", annotation="paragraph")
                )
            return _document(*blocks)

        self.assertEqual(
            [heading.source_index for heading in build_document_outline(candidate(True)).headings],
            [0, 1, 2],
        )
        self.assertEqual(
            [heading.source_index for heading in build_document_outline(candidate(False)).headings],
            [0, 1],
        )

    def test_dotted_sibling_restarts_after_only_repeated_ancestor_carriers(
        self,
    ) -> None:
        document = _paged_document(
            (
                _block(
                    0,
                    "（八）金融工具风险 -续",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(80, 50, 390, 70),
                ),
                _block(
                    1,
                    "1、风险管理目标 -续",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(140, 90, 660, 110),
                ),
                _block(
                    2,
                    "1.2信用风险",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(140, 220, 260, 240),
                ),
            ),
            (
                _block(
                    3,
                    "（八）金融工具风险 -续",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(80, 50, 390, 70),
                ),
                _block(
                    4,
                    "1、风险管理目标 -续",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(140, 90, 660, 110),
                ),
            ),
            (
                _block(
                    5,
                    "（八）金融工具风险 -续",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(80, 50, 390, 70),
                ),
                _block(
                    6,
                    "1、风险管理目标 -续",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(140, 90, 660, 110),
                ),
                _block(
                    7,
                    "1.3流动性风险",
                    annotation="paragraph",
                    bbox=ProviderBBox(140, 220, 280, 240),
                ),
            ),
        )

        outline = build_document_outline(document)

        self.assertIn(7, [heading.source_index for heading in outline.headings])
        self.assertEqual(
            next(heading for heading in outline.headings if heading.source_index == 7).text,
            "1.3流动性风险",
        )

    def test_empty_numbered_container_owns_repeated_table_heading_family(self) -> None:
        document = _paged_document(
            (
                _block(0, "5 财务报表", annotation="title", level=2),
                _block(
                    1,
                    "未经审计合并资产负债表",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(90, 110, 390, 132),
                ),
                _table_block(
                    2,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
            (
                _block(
                    3,
                    "未经审计合并资产负债表（续）",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(91, 110, 410, 132),
                ),
                _table_block(
                    4,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
            (
                _block(
                    5,
                    "未经审计合并利润表",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(89, 110, 340, 132),
                ),
                _table_block(
                    6,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
            (
                _block(
                    7,
                    "未经审计合并现金流量表",
                    annotation="title",
                    level=2,
                    bbox=ProviderBBox(90, 110, 360, 132),
                ),
                _table_block(
                    8,
                    caption="",
                    bbox=ProviderBBox(80, 150, 920, 700),
                ),
            ),
            (_block(9, "6 流动性覆盖率信息", annotation="title", level=2),),
        )

        outline = build_document_outline(document)
        headings = {heading.text: heading for heading in outline.headings}

        container = headings["5 财务报表"]
        self.assertEqual(
            headings["未经审计合并资产负债表"].parent_heading_id,
            container.heading_id,
        )
        self.assertNotIn("未经审计合并资产负债表（续）", headings)
        self.assertEqual(
            next(
                candidate
                for candidate in outline.candidates
                if candidate.source_index == 3
            ).disposition_reason,
            "page_continuation",
        )
        self.assertIsNone(headings["6 流动性覆盖率信息"].parent_heading_id)

    def test_numbered_container_without_repeated_table_family_does_not_lock(self) -> None:
        document = _document(
            _block(0, "5 财务报表", annotation="title", level=2),
            _block(1, "已有正文", annotation="paragraph"),
            _block(2, "临时表名", annotation="title", level=1),
            _table_block(3, caption=""),
            _block(4, "6 其他信息", annotation="title", level=2),
        )

        outline = build_document_outline(document)

        candidate = next(
            item for item in outline.candidates if item.text == "临时表名"
        )
        self.assertEqual(candidate.placement_source, "provider")
        self.assertEqual(candidate.nominal_rank, 1)
        self.assertEqual(candidate.disposition, "accepted")

    def test_delayed_table_notice_stays_inline_with_proved_table_owner(self) -> None:
        document = _document(
            _block(
                0,
                "境内主要项目开发情况和开发计划",
                annotation="title",
                level=2,
            ),
            _block(1, "单位：平方米", annotation="paragraph"),
            _block(2, "特别风险提示", annotation="title", level=2),
            _block(
                3,
                "上述项目开发计划可能因市场变化而调整。",
                annotation="paragraph",
            ),
            _table_block(
                4,
                caption="",
                body=(
                    "<table><tr><td>项目名称</td><td>开发计划</td>"
                    "<td>开工面积</td></tr><tr><td>A项目</td>"
                    "<td>2026年</td><td>100</td></tr></table>"
                ),
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [unit.block_source_indices for unit in outline.units],
            [(0, 1, 2, 3, 4)],
        )
        self.assertEqual(outline.units[0].title, "境内主要项目开发情况和开发计划")
        notice = next(item for item in outline.candidates if item.source_index == 2)
        self.assertEqual(notice.disposition_reason, "interstitial_notice")

    def test_delayed_table_owner_does_not_inherit_prior_numbered_leaf(self) -> None:
        document = _document(
            _block(0, "（七）未来发展展望", annotation="title", level=2),
            _block(1, "5、深化科技应用", annotation="title", level=2),
            _block(2, "境内主要项目开发情况和开发计划", annotation="title", level=2),
            _block(3, "单位：平方米", annotation="paragraph"),
            _block(4, "特别风险提示", annotation="title", level=2),
            _block(5, "上述项目开发计划可能调整。", annotation="paragraph"),
            _table_block(
                6,
                caption="",
                body=(
                    "<table><tr><td>项目名称</td><td>开发计划</td>"
                    "<td>开工面积</td></tr></table>"
                ),
            ),
        )

        outline = build_document_outline(document)
        headings = {item.text: item for item in outline.headings}

        self.assertEqual(
            headings["境内主要项目开发情况和开发计划"].parent_heading_id,
            headings["（七）未来发展展望"].heading_id,
        )
        self.assertNotIn("特别风险提示", headings)
        owner = next(
            unit
            for unit in outline.units
            if unit.title == "境内主要项目开发情况和开发计划"
        )
        self.assertEqual(owner.block_source_indices, (2, 3, 4, 5, 6))

    def test_delayed_table_without_back_reference_keeps_reading_order_owner(
        self,
    ) -> None:
        document = _document(
            _block(0, "项目开发计划", annotation="title", level=2),
            _block(1, "单位：平方米", annotation="paragraph"),
            _block(2, "特别风险提示", annotation="title", level=2),
            _block(3, "宏观市场可能发生变化。", annotation="paragraph"),
            _table_block(
                4,
                caption="",
                body="<table><tr><td>项目</td><td>开发计划</td></tr></table>",
            ),
        )

        outline = build_document_outline(document)

        self.assertEqual(
            [unit.block_source_indices for unit in outline.units],
            [(0, 1), (2, 3, 4)],
        )

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

    def test_weak_label_escapes_completed_numbered_subgroup(self) -> None:
        document = _document(
            _block(0, "第八节 财务报告", level=2),
            _block(1, "9、其他应收款", level=2),
            _block(2, "(6) 本期实际核销的应收利息情况", level=2),
            _block(3, "应收股利", level=2),
            _block(4, "(1) 应收股利", annotation="paragraph"),
            _block(5, "(2) 重要的账龄超过 1 年的应收股利", level=2),
        )

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }

        self.assertEqual(
            headings["应收股利"].headpath,
            ("第八节 财务报告", "9、其他应收款", "应收股利"),
        )
        self.assertEqual(
            headings["(2) 重要的账龄超过 1 年的应收股利"].headpath,
            (
                "第八节 财务报告",
                "9、其他应收款",
                "(2) 重要的账龄超过 1 年的应收股利",
            ),
        )

    def test_distant_restart_does_not_move_an_unrelated_weak_label(self) -> None:
        document = _document(
            _block(0, "第八节 财务报告", level=2),
            _block(1, "9、其他应收款", level=2),
            _block(2, "(6) 本期实际核销情况", level=2),
            _block(3, "补充说明", level=2),
            _block(4, "正文", annotation="paragraph"),
            _block(5, "(1) 新序列", level=2),
        )

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }

        self.assertEqual(
            headings["补充说明"].headpath,
            (
                "第八节 财务报告",
                "9、其他应收款",
                "(6) 本期实际核销情况",
                "补充说明",
            ),
        )

    def test_table_does_not_create_an_unheaded_section_boundary(
        self,
    ) -> None:
        table = replace(
            _block(3, "", annotation="table", provider_type="table"),
            payloads=(ProviderPayload("table_body", None, "<table></table>"),),
        )
        document = _paged_document(
            (
                _block(0, "第四节 公司治理", level=2),
                _block(1, "三、员工激励", level=2),
                _block(2, "(二) 后续进展", level=2),
            ),
            (table,),
            (_block(4, "(一) 环境措施", level=2),),
        )

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }

        self.assertEqual(
            headings["(一) 环境措施"].headpath,
            ("第四节 公司治理", "三、员工激励", "(一) 环境措施"),
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

    def test_dotted_numbering_without_space_preserves_parent_and_full_depth(self) -> None:
        document = _document(
            _block(0, "14、存货", level=2),
            _block(1, "14.1存货类别和计价方法", level=2),
            _block(2, "14.1.1 存货类别", level=2),
            _block(3, "14.1.2发出存货的计价方法", level=2),
            _block(4, "14.2存货跌价准备", level=2),
        )

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }

        self.assertEqual(
            headings["14.1存货类别和计价方法"].headpath,
            ("14、存货", "14.1存货类别和计价方法"),
        )
        self.assertEqual(
            headings["14.1.1 存货类别"].headpath,
            (
                "14、存货",
                "14.1存货类别和计价方法",
                "14.1.1 存货类别",
            ),
        )
        self.assertEqual(
            headings["14.1.2发出存货的计价方法"].headpath,
            (
                "14、存货",
                "14.1存货类别和计价方法",
                "14.1.2发出存货的计价方法",
            ),
        )
        self.assertEqual(
            headings["14.2存货跌价准备"].headpath,
            ("14、存货", "14.2存货跌价准备"),
        )

    def test_mixed_arabic_subgroup_keeps_dotted_parent_and_does_not_leak(self) -> None:
        document = _document(
            _block(0, "三、会计数据和财务指标", annotation="title", level=1),
            _block(1, "3.2 主要会计数据和财务指标", annotation="title", level=2),
            _block(2, "境内外会计准则下会计数据差异", annotation="title", level=2),
            _block(3, "1、国际与中国准则差异", annotation="paragraph"),
            _block(4, "□适用√不适用", annotation="paragraph"),
            _block(5, "2、境外与中国准则差异", annotation="paragraph"),
            _block(6, "□适用√不适用", annotation="paragraph"),
            _block(7, "3、差异原因说明", annotation="title", level=2),
            _block(8, "存贷款情况", annotation="title", level=2),
            _block(9, "3.3 补充财务比率", annotation="title", level=2),
        )

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }
        self.assertEqual(
            headings["3、差异原因说明"].parent_heading_id,
            headings["境内外会计准则下会计数据差异"].heading_id,
        )
        self.assertEqual(
            headings["存贷款情况"].parent_heading_id,
            headings["3.2 主要会计数据和财务指标"].heading_id,
        )
        self.assertEqual(
            headings["3.3 补充财务比率"].parent_heading_id,
            headings["三、会计数据和财务指标"].heading_id,
        )

    def test_decimal_amount_paragraph_is_not_promoted_by_dotted_numbering(self) -> None:
        document = _document(
            _block(0, "14、存货", level=2),
            _block(1, "14.5亿元用于补充流动资金。", annotation="paragraph"),
            _block(2, "后续正文", annotation="paragraph"),
        )

        outline = build_document_outline(document)

        self.assertEqual([heading.text for heading in outline.headings], ["14、存货"])
        self.assertEqual(outline.units[0].block_source_indices, (0, 1, 2))

    def test_indented_cross_family_sequence_preserves_statutory_parent(self) -> None:
        document = _document(
            _block(0, "第六节 重要事项", level=2, bbox=ProviderBBox(390, 100, 610, 140)),
            _block(1, "一、承诺事项履行情况", level=2, bbox=ProviderBBox(90, 150, 310, 190)),
            _block(2, "1、关于保持公司独立性的承诺", level=2, bbox=ProviderBBox(90, 200, 380, 240)),
            _block(3, "“一、上市公司的人员独立", level=2, bbox=ProviderBBox(135, 250, 390, 290)),
            _block(4, "二、上市公司的财务独立", level=2, bbox=ProviderBBox(130, 300, 370, 340)),
            _block(5, "三、上市公司的机构独立", level=2, bbox=ProviderBBox(130, 350, 370, 390)),
            _block(6, "2、关于避免同业竞争的承诺", level=2, bbox=ProviderBBox(90, 400, 370, 440)),
            _block(7, "3、关于规范关联交易的承诺", level=2, bbox=ProviderBBox(90, 450, 370, 490)),
            _block(8, "（二）资产盈利预测履行情况", level=2, bbox=ProviderBBox(90, 500, 500, 540)),
        )

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }

        self.assertEqual(
            headings["“一、上市公司的人员独立"].headpath,
            (
                "第六节 重要事项",
                "一、承诺事项履行情况",
                "1、关于保持公司独立性的承诺",
                "“一、上市公司的人员独立",
            ),
        )
        self.assertEqual(
            headings["二、上市公司的财务独立"].headpath,
            (
                "第六节 重要事项",
                "一、承诺事项履行情况",
                "1、关于保持公司独立性的承诺",
                "二、上市公司的财务独立",
            ),
        )
        self.assertEqual(
            headings["2、关于避免同业竞争的承诺"].headpath,
            (
                "第六节 重要事项",
                "一、承诺事项履行情况",
                "2、关于避免同业竞争的承诺",
            ),
        )
        self.assertEqual(
            headings["（二）资产盈利预测履行情况"].headpath,
            (
                "第六节 重要事项",
                "一、承诺事项履行情况",
                "（二）资产盈利预测履行情况",
            ),
        )

    def test_same_column_ordinal_restart_does_not_invert_numbering_rank(self) -> None:
        document = _document(
            _block(0, "第一节 原章节", level=2, bbox=ProviderBBox(100, 100, 500, 140)),
            _block(1, "1、原子项", level=2, bbox=ProviderBBox(100, 150, 500, 190)),
            _block(2, "一、新章节", level=2, bbox=ProviderBBox(100, 200, 500, 240)),
        )

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }

        self.assertEqual(
            headings["一、新章节"].parent_heading_id,
            headings["第一节 原章节"].heading_id,
        )

    def test_plain_numbered_sibling_escapes_intervening_style_subhead(self) -> None:
        document = _document(
            _block(0, "2 主要财务数据", level=2),
            _block(1, "3.2 优先股", level=2),
            _block(2, "优先股赎回情况", level=2),
            _block(3, "4 管理层讨论与分析", level=2),
        )
        outline = build_document_outline(
            document,
            level_hints=(_level_hint(document, 2, "pdf_style", 2),),
        )

        headings = {heading.text: heading for heading in outline.headings}
        self.assertEqual(
            headings["优先股赎回情况"].parent_heading_id,
            headings["3.2 优先股"].heading_id,
        )
        self.assertIsNone(headings["4 管理层讨论与分析"].parent_heading_id)

    def test_plain_numbered_sibling_recovers_after_style_root_replaces_stack(
        self,
    ) -> None:
        document = _document(
            _block(0, "5 财务报表", level=2),
            _block(1, "未经审计现金流量表（续）", level=2),
            _block(2, "6 流动性覆盖率信息", level=2),
        )
        outline = build_document_outline(
            document,
            level_hints=(_level_hint(document, 1, "pdf_style", 2),),
        )

        headings = {heading.text: heading for heading in outline.headings}
        self.assertIsNone(headings["5 财务报表"].parent_heading_id)
        self.assertIsNone(headings["未经审计现金流量表（续）"].parent_heading_id)
        self.assertIsNone(headings["6 流动性覆盖率信息"].parent_heading_id)

    def test_style_history_does_not_bridge_an_ordinal_restart(self) -> None:
        document = _document(
            _block(0, "5 原序列", level=2),
            _block(1, "样式标题", level=2),
            _block(2, "1 新序列", level=2),
        )
        outline = build_document_outline(
            document,
            level_hints=(_level_hint(document, 1, "pdf_style", 2),),
        )

        headings = {heading.text: heading for heading in outline.headings}
        self.assertEqual(
            headings["1 新序列"].parent_heading_id,
            headings["样式标题"].heading_id,
        )

    def test_style_history_does_not_cross_a_new_numbered_parent(self) -> None:
        document = _document(
            _block(0, "第一节 旧父级", level=2),
            _block(1, "5 旧序列", level=2),
            _block(2, "旧样式标题", level=2),
            _block(3, "第二节 新父级", level=2),
            _block(4, "6 新父级内标题", level=2),
        )
        outline = build_document_outline(
            document,
            level_hints=(_level_hint(document, 2, "pdf_style", 3),),
        )

        headings = {heading.text: heading for heading in outline.headings}
        self.assertEqual(
            headings["6 新父级内标题"].parent_heading_id,
            headings["第二节 新父级"].heading_id,
        )

    def test_repeated_page_titles_can_end_a_stale_numbered_parent(self) -> None:
        pages = [
            (_block(0, "第九节 财务报告", level=2),),
            (_block(1, "七、审计责任 -续", level=2),),
        ]
        pages.extend((_block(index, f"正文{index}", annotation="paragraph"),) for index in range(2, 6))
        pages.append(
            (
                _block(
                    6,
                    "附注为财务报表的组成部分",
                    level=2,
                    bbox=ProviderBBox(50, 500, 220, 525),
                ),
            )
        )
        pages.extend((_block(index, f"正文{index}", annotation="paragraph"),) for index in range(7, 10))
        pages.append(
            (
                _block(
                    10,
                    "附注为财务报表的组成部分",
                    level=2,
                    bbox=ProviderBBox(51, 501, 221, 526),
                ),
            )
        )
        pages.append((_block(11, "正文11", annotation="paragraph"),))
        pages.append((_block(12, "(一) 公司基本情况", level=2),))
        document = _paged_document(*pages)

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }

        self.assertEqual(
            headings["(一) 公司基本情况"].parent_heading_id,
            headings["第九节 财务报告"].heading_id,
        )

    def test_repeated_legal_titles_do_not_end_a_noncontinuation_parent(self) -> None:
        pages = [
            (_block(0, "第一节 风险管理", level=2),),
            (_block(1, "一、风险概况", level=2),),
        ]
        pages.extend(
            (_block(index, f"正文{index}", annotation="paragraph"),)
            for index in range(2, 6)
        )
        pages.append(
            (
                _block(
                    6,
                    "风险管理",
                    level=2,
                    bbox=ProviderBBox(50, 500, 220, 525),
                ),
            )
        )
        pages.extend(
            (_block(index, f"正文{index}", annotation="paragraph"),)
            for index in range(7, 10)
        )
        pages.append(
            (
                _block(
                    10,
                    "风险管理",
                    level=2,
                    bbox=ProviderBBox(51, 501, 221, 526),
                ),
            )
        )
        pages.append((_block(11, "正文11", annotation="paragraph"),))
        pages.append((_block(12, "(一) 市场风险", level=2),))

        headings = {
            heading.text: heading
            for heading in build_document_outline(_paged_document(*pages)).headings
        }

        self.assertEqual(
            headings["(一) 市场风险"].parent_heading_id,
            headings["一、风险概况"].heading_id,
        )

    def test_repeated_titles_at_different_positions_do_not_reset_parent(self) -> None:
        pages = [
            (_block(0, "第九节 财务报告", level=2),),
            (_block(1, "七、审计责任 -续", level=2),),
        ]
        pages.extend(
            (_block(index, f"正文{index}", annotation="paragraph"),)
            for index in range(2, 6)
        )
        pages.append(
            (
                _block(
                    6,
                    "合法同名标题",
                    level=2,
                    bbox=ProviderBBox(50, 200, 220, 225),
                ),
            )
        )
        pages.extend(
            (_block(index, f"正文{index}", annotation="paragraph"),)
            for index in range(7, 10)
        )
        pages.append(
            (
                _block(
                    10,
                    "合法同名标题",
                    level=2,
                    bbox=ProviderBBox(50, 700, 220, 725),
                ),
            )
        )
        pages.append((_block(11, "正文11", annotation="paragraph"),))
        pages.append((_block(12, "(一) 公司基本情况", level=2),))

        headings = {
            heading.text: heading
            for heading in build_document_outline(_paged_document(*pages)).headings
        }

        self.assertEqual(
            headings["(一) 公司基本情况"].parent_heading_id,
            headings["七、审计责任 -续"].heading_id,
        )

    def test_single_page_title_does_not_end_a_numbered_parent(self) -> None:
        pages = [
            (_block(0, "第九节 财务报告", level=2),),
            (_block(1, "七、审计责任 -续", level=2),),
        ]
        pages.extend((_block(index, f"正文{index}", annotation="paragraph"),) for index in range(2, 9))
        pages.append((_block(9, "附注为财务报表的组成部分", level=2),))
        pages.extend((_block(index, f"正文{index}", annotation="paragraph"),) for index in range(10, 12))
        pages.append((_block(12, "(一) 公司基本情况", level=2),))
        document = _paged_document(*pages)

        headings = {
            heading.text: heading for heading in build_document_outline(document).headings
        }

        self.assertEqual(
            headings["(一) 公司基本情况"].parent_heading_id,
            headings["七、审计责任 -续"].heading_id,
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
            source_fragments=(
                replace(outline.headings[1].source_fragments[0], text="伪造标题"),
            ),
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


def _table_block(
    source_index: int,
    *,
    caption: str,
    footnote: str | None = None,
    bbox: ProviderBBox | None = None,
    body: str = "<table><td>正文</td></table>",
) -> ProviderBlock:
    payloads = [
        ProviderPayload("table_body", None, body),
    ]
    if caption:
        payloads.append(ProviderPayload("table_caption", 0, caption))
    if footnote is not None:
        payloads.append(ProviderPayload("table_footnote", 0, footnote))
    return ProviderBlock(
        source_index=source_index,
        page_index=0,
        order_in_page=source_index,
        provider_type="table",
        typed_annotation="table",
        provider_level=None,
        bbox=bbox
        or ProviderBBox(
            100,
            100 + source_index * 50,
            900,
            140 + source_index * 50,
        ),
        payloads=tuple(payloads),
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
