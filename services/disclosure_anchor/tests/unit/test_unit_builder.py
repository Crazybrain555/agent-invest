"""Unit builder rule and S1-S7 stage tests."""

from __future__ import annotations

import unittest

from disclosure_anchor.adapters.unit_builder import rules
from disclosure_anchor.adapters.unit_builder.builder import (
    BuildStats,
    PreparedElement,
    UnitDraft,
    _worst_quality,
    build_unit_drafts_s1_s7,
    replace_text_units_with_qa_where_stable,
    s1_preprocess_elements,
    s2_apply_heading_tree,
    s3_build_text_units,
    s4_build_qa_units,
    s5_build_table_units,
    s6_filter_units,
    s7_finalize_units,
    semantic_key_for_unit,
    semantic_keys_for_unit,
)


class UnitBuilderTests(unittest.TestCase):
    def test_rules_version_and_fixed_tables(self) -> None:
        self.assertEqual(rules.RULES_VERSION, "ub-2026.07-55")
        self.assertEqual(rules.HEADING_RULESET_ID, "cn_a_v6")
        self.assertEqual(rules.SKIP_SECTION_TITLES, set())
        self.assertEqual(rules.GIBBERISH_RATIO_MAX, 0.30)

    def test_short_note_labels_do_not_match_unrelated_substrings(self) -> None:
        self.assertIsNone(rules.note_key_for_title("库存货物管理情况"))
        self.assertIsNone(rules.note_key_for_title("公司商誉体系建设"))
        self.assertIsNone(rules.note_key_for_title("融资租赁业务发展"))
        self.assertEqual(rules.note_key_for_title("存货分类构成"), "inventory")

    def test_every_registered_note_label_reaches_its_declared_keys(self) -> None:
        exact, _ = rules._note_key_tables()
        for label, expected_keys in exact.items():
            with self.subTest(label=label):
                self.assertTrue(
                    set(expected_keys).issubset(rules.note_keys_for_title(label)),
                    (label, expected_keys, rules.note_keys_for_title(label)),
                )

    def test_note_number_stripping_requires_structural_punctuation(self) -> None:
        self.assertEqual(
            rules.note_key_for_title("一年内到期的非流动资产"),
            "noncurrent_due_within_one_year",
        )
        self.assertEqual(rules.note_key_for_title("一般风险准备"), "surplus_reserve")
        self.assertIsNone(rules.note_key_for_title("12存货"))
        self.assertEqual(rules.note_key_for_title("12、存货"), "inventory")

    def test_s1_keeps_one_repeated_furniture_carrier_with_all_provenance(
        self,
    ) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "page_furniture",
                    "raw_kind": "header",
                    "order_index": 1,
                    "page_no": 1,
                    "text": "重复页眉",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "header",
                    "order_index": 2,
                    "page_no": 2,
                    "text": "重复页眉",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "header",
                    "order_index": 3,
                    "page_no": 2,
                    "text": "合并资产负债表",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 4,
                    "text": "---\n正文\u0001",
                },
                {"kind": "unknown", "raw_kind": "mystery", "order_index": 5},
            ]
        )

        self.assertEqual(
            [item.text for item in result.elements],
            ["重复页眉", "合并资产负债表", "正文"],
        )
        self.assertEqual(
            result.stats.dropped_by_kind["page_furniture_exact_duplicate"], 1
        )
        self.assertEqual(result.stats.source_dispositions, [])
        self.assertEqual(
            result.elements[0].artifact_locator["derivation"]["kind"],
            "exact_duplicate_carriers",
        )
        self.assertEqual(
            len(result.elements[0].artifact_locator["source_locators"]), 2
        )
        self.assertEqual(result.stats.dropped_by_kind["unknown"], 1)
        self.assertEqual(result.stats.dropped_unknown_by_raw_kind["mystery"], 1)

    def test_s1_deduplicates_only_authoritative_page_number_metadata(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "page_furniture",
                    "raw_kind": "page_number",
                    "order_index": 1,
                    "page_no": 7,
                    "text": "第 7 页",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "header",
                    "order_index": 2,
                    "page_no": 7,
                    "text": "7",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "page_number",
                    "order_index": 3,
                    "page_no": 7,
                    "text": "8",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "page_number",
                    "order_index": 4,
                    "page_no": 7,
                    "text": "7 / 24",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "page_number",
                    "order_index": 5,
                    "page_no": 7,
                    "text": "8 / 24",
                },
            ]
        )

        self.assertEqual([item.text for item in result.elements], ["7"])
        self.assertEqual(result.stats.deduplicated_page_number_lines, 4)
        self.assertEqual(
            [item["reason"] for item in result.stats.source_dispositions],
            ["exact_page_number"] * 4,
        )

    def test_s1_overlapping_furniture_groups_have_one_unsuppressed_canonical(
        self,
    ) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "page_no": 1,
                    "text": "重复页眉",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "header",
                    "order_index": 2,
                    "page_no": 2,
                    "text": "重复页眉",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "header",
                    "order_index": 3,
                    "page_no": 3,
                    "text": "重复页眉",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "footer",
                    "order_index": 4,
                    "page_no": 4,
                    "text": "重复页眉",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "footer",
                    "order_index": 5,
                    "page_no": 5,
                    "text": "重复页眉",
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "header",
                    "order_index": 6,
                    "page_no": 6,
                    "text": "重复页眉",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 7,
                    "page_no": 6,
                    "text": "重复页眉",
                },
            ]
        )

        self.assertEqual([item.kind for item in result.elements], ["heading"])
        locator = result.elements[0].artifact_locator or {}
        self.assertEqual(locator["order_index"], 1)
        self.assertEqual(
            {item["order_index"] for item in locator["source_locators"]},
            set(range(1, 8)),
        )
        self.assertEqual(
            result.stats.dropped_by_kind["page_furniture_exact_duplicate"],
            6,
        )

    def test_retained_page_furniture_does_not_inherit_business_heading(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "一、经营情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "经营情况稳定。",
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "footer",
                        "order_index": 3,
                        "page_no": 1,
                        "text": "公告编号：2026-001",
                    },
                ]
            },
            filing_type="other",
            document_title="测试公告",
        )

        furniture = next(
            unit for unit in units if "公告编号" in unit.payload.get("text", "")
        )
        self.assertEqual(furniture.heading_path, ["测试公告"])
        self.assertNotIn(
            "heading_source_locators", furniture.artifact_locator or {}
        )

    def test_repeated_page_furniture_is_published_once_with_duplicate_lineage(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "一、经营情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "经营情况稳定。",
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "header",
                        "order_index": 3,
                        "page_no": 2,
                        "text": "测试公司 2026 年年度报告",
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "header",
                        "order_index": 4,
                        "page_no": 3,
                        "text": "测试公司 2026 年年度报告",
                    },
                ]
            },
            filing_type="annual_report",
            document_title="测试公司 2026 年年度报告",
        )

        furniture = [
            unit
            for unit in units
            if "测试公司" in unit.payload.get("text", "")
        ]
        self.assertEqual(len(furniture), 1)
        self.assertEqual(
            furniture[0].artifact_locator["derivation"]["kind"],
            "exact_duplicate_carriers",
        )

    def test_s1_visual_payload_preserves_structured_source_fields(self) -> None:
        digest = "a" * 64
        content = "| 指标 | 数值 |\n| --- | --- |\n| 收入 | 10 |"
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "source_item_index": 1,
                    "ir_id": "ir_0001",
                    "page_no": 5,
                    "text": "股权结构图",
                    "heading_level": 2,
                },
                {
                    "kind": "image",
                    "raw_kind": "chart",
                    "order_index": 2,
                    "source_item_index": 2,
                    "ir_id": "ir_0002",
                    "page_no": 5,
                    "image_path": f"images/{digest}.jpg",
                    "text": content,
                    "image_caption": ["收入结构", "按期末数"],
                    "image_footnote": ["注：未经审计"],
                },
                {
                    "kind": "image",
                    "raw_kind": "image",
                    "order_index": 3,
                    "source_item_index": 3,
                    "ir_id": "ir_0003",
                    "page_no": 6,
                    "image_path": f"images/{'b' * 64}.jpg",
                },
            ]
        )

        image_units = [item for item in result.elements if item.payload]
        self.assertEqual(len(image_units), 2)
        self.assertEqual(image_units[0].payload["image_ref"], f"images/{digest}.jpg")
        self.assertEqual(image_units[0].payload["caption"], "收入结构\n按期末数")
        self.assertEqual(image_units[0].payload["context"], "股权结构图")
        self.assertEqual(image_units[0].payload["content"], content)
        self.assertEqual(image_units[0].payload["notes"], ["注：未经审计"])
        self.assertEqual(image_units[0].quality_status, "needs_review")
        locator = image_units[0].artifact_locator or {}
        projection = locator["source_projection"]
        self.assertEqual(
            [item["target_field"] for item in projection["structured"]],
            [
                "payload.caption",
                "payload.content",
                "payload.notes.0",
                "payload.context",
            ],
        )
        self.assertEqual(
            image_units[1].payload["image_ref"], f"images/{'b' * 64}.jpg"
        )
        self.assertEqual(image_units[1].payload["caption"], "")
        self.assertEqual(image_units[1].payload["context"], "")
        self.assertEqual(result.stats.dropped_by_kind["image"], 0)

    def test_s1_preserves_fact_caption_when_image_path_is_missing(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "image",
                    "raw_kind": "image",
                    "order_index": 1,
                    "caption": "控股股东持股比例为51%",
                }
            ]
        )

        self.assertEqual(len(result.elements), 1)
        self.assertIsNone(result.elements[0].text)
        self.assertEqual(
            result.elements[0].payload["text"],
            "控股股东持股比例为51%",
        )
        self.assertEqual(result.elements[0].quality_status, "needs_review")
        self.assertEqual(
            result.elements[0].artifact_locator["derivation"]["kind"],
            "image_caption_without_image",
        )

    def test_s2_heading_tree_excludes_questions(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="第一节 重要提示、目录和释义",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="text", order_index=2, text="2.请介绍集团业务矩阵？"
                ),
                PreparedElement(
                    kind="text", order_index=3, text="答:业务覆盖多个领域。"
                ),
            ]
        )

        self.assertEqual(len(placed), 2)
        self.assertEqual(placed[0].text, "2.请介绍集团业务矩阵？")
        self.assertEqual(placed[0].kind, "text")
        self.assertEqual(placed[0].heading_path, ["第一节 重要提示、目录和释义"])
        self.assertNotIn("2.请介绍集团业务矩阵？", placed[1].heading_path)

    def test_s2_qa_heading_mode_does_not_demote_declarative_numbering(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="6.请公司讲一下，2025年重点工作",
                    heading_level=2,
                ),
                PreparedElement(kind="text", order_index=2, text="答:重点是海外业务。"),
            ],
            qa_heading_mode=True,
        )

        self.assertEqual(placed[0].kind, "text")
        parsed = replace_text_units_with_qa_where_stable(s3_build_text_units(placed))
        self.assertEqual([unit.payload_kind for unit in parsed], ["text"])
        self.assertEqual(parsed[0].title, "6.请公司讲一下，2025年重点工作")

    def test_numbered_business_sentences_remain_searchable_without_fragmenting(
        self,
    ) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="text", order_index=1, text="1、公司不存在重大诉讼"
                ),
                PreparedElement(
                    kind="text", order_index=2, text="2、公司不存在违规担保"
                ),
            ]
        )

        units = s3_build_text_units(placed)

        self.assertEqual(len(units), 1)
        self.assertIn("重大诉讼", units[0].payload["text"])
        self.assertIn("违规担保", units[0].payload["text"])

    def test_terminal_heading_remains_publicly_searchable(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "一、核心事实：收入增长20%",
                    }
                ]
            },
            filing_type="other",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload["text"], "一、核心事实：收入增长20%")
        self.assertEqual(stats.heading_only_carriers_preserved, 1)
        self.assertEqual(stats.needs_review_count, 0)

    def test_unnumbered_parser_headings_with_same_level_are_siblings(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="经营情况", heading_level=1
                ),
                PreparedElement(
                    kind="heading", order_index=2, text="主营业务", heading_level=2
                ),
                PreparedElement(kind="text", order_index=3, text="业务事实。"),
                PreparedElement(
                    kind="heading", order_index=4, text="核心竞争力", heading_level=2
                ),
                PreparedElement(kind="text", order_index=5, text="竞争力事实。"),
                PreparedElement(
                    kind="heading", order_index=6, text="经营计划", heading_level=2
                ),
                PreparedElement(kind="text", order_index=7, text="计划事实。"),
            ]
        )

        self.assertEqual(
            [item.structural_path for item in placed],
            [
                ["经营情况", "主营业务"],
                ["经营情况", "核心竞争力"],
                ["经营情况", "经营计划"],
            ],
        )

    def test_s3_keeps_numbered_enumeration_as_one_block(self) -> None:
        # ub-2026.07-5: enumerated lines are one business block — splitting
        # them into per-line units was the round3 over-fragmentation defect.
        long_items = "\n".join(f"{idx}、" + "经营情况说明" * 12 for idx in range(1, 4))
        units = s3_build_text_units(
            [
                PreparedElement(
                    kind="text",
                    order_index=1,
                    text=long_items,
                    heading_path=["重要提示"],
                    title="重要提示",
                )
            ]
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload_kind, "text")
        self.assertEqual(units[0].payload["text"], long_items)

    def test_s3_coalescing_preserves_each_source_locator(self) -> None:
        units = s3_build_text_units(
            [
                PreparedElement(
                    kind="text",
                    order_index=1,
                    page_no=1,
                    text="第一段。",
                    structural_path=["经营情况"],
                    artifact_locator={"order_index": 1, "page_no": 1, "bbox": [0, 0, 1, 1]},
                ),
                PreparedElement(
                    kind="text",
                    order_index=2,
                    page_no=2,
                    text="第二段。",
                    structural_path=["经营情况"],
                    artifact_locator={"order_index": 2, "page_no": 2, "bbox": [0, 0, 1, 1]},
                ),
            ]
        )

        self.assertEqual(len(units), 1)
        locator = units[0].artifact_locator
        self.assertEqual(locator["source_order_span"], [1, 2])
        self.assertEqual(locator["page_span"], [1, 2])
        self.assertEqual(len(locator["source_locators"]), 2)

    def test_s4_preserves_prose_before_first_question(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": "placeholder"},
            source_order=1,
            heading_path=["交流情况"],
            structural_path=["交流情况"],
        )

        units = replace_text_units_with_qa_where_stable(
            [
                UnitDraft(
                    **{
                        **source.__dict__,
                        "payload": {
                            "text": "活动背景：本次交流围绕年度经营。\n问：收入为何增长？\n答：主要系销量提升。"
                        },
                    }
                )
            ]
        )

        self.assertEqual([unit.payload_kind for unit in units], ["text", "qa"])
        self.assertEqual(
            units[0].payload["text"], "活动背景：本次交流围绕年度经营。"
        )
        self.assertEqual(units[0].quality_status, "needs_review")
        self.assertEqual(units[1].payload["answer"], "主要系销量提升。")

    def test_s2_persists_full_source_hierarchy_on_public_path(self) -> None:
        elements = [
            PreparedElement(
                kind="heading",
                order_index=index,
                text=title,
                heading_level=index,
            )
            for index, title in enumerate(
                ["一级", "二级", "三级", "四级", "五级"], start=1
            )
        ]
        elements.append(
            PreparedElement(kind="text", order_index=6, text="完整层级下的事实。")
        )

        placed = s2_apply_heading_tree(elements)

        self.assertEqual(
            placed[0].heading_path,
            ["一级", "二级", "三级", "四级", "五级"],
        )
        self.assertEqual(
            placed[0].structural_path,
            ["一级", "二级", "三级", "四级", "五级"],
        )

    def test_numbering_never_discards_parser_proven_parent(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="管理层讨论", heading_level=1
                ),
                PreparedElement(
                    kind="heading", order_index=2, text="经营分析", heading_level=2
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="一、分产品分析",
                    heading_level=3,
                ),
                PreparedElement(kind="text", order_index=4, text="产品事实。"),
            ]
        )

        self.assertEqual(
            placed[0].heading_path,
            ["管理层讨论", "经营分析", "一、分产品分析"],
        )

    def test_repeated_same_title_sections_keep_distinct_occurrence_identity(
        self,
    ) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="项目情况", heading_level=1
                ),
                PreparedElement(kind="text", order_index=2, text="甲项目事实。"),
                PreparedElement(
                    kind="heading", order_index=3, text="项目情况", heading_level=1
                ),
                PreparedElement(kind="text", order_index=4, text="乙项目事实。"),
            ]
        )

        self.assertEqual([item.heading_path for item in placed], [["项目情况"]] * 2)
        self.assertNotEqual(placed[0].section_path, placed[1].section_path)
        units = s3_build_text_units(placed)
        self.assertEqual(
            [unit.payload["text"] for unit in units],
            ["甲项目事实。", "乙项目事实。"],
        )

    def test_short_structured_document_keeps_explicit_chapter_boundaries(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第一章 总则",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "第一条 为完善治理结构，制定本办法。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "第二章 附则",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "第二条 本办法自发布之日起施行。",
                    },
                ]
            },
            filing_type="other",
            document_title="贵州茅台：董事、高级管理人员考核和薪酬管理办法",
        )

        self.assertEqual(
            [unit.heading_path for unit in units],
            [["第一章 总则"], ["第二章 附则"]],
        )
        self.assertIn("完善治理结构", units[0].payload["text"])
        self.assertIn("发布之日起施行", units[1].payload["text"])

    def test_s2_preserves_parser_level_for_ambiguous_unnumbered_heading(
        self,
    ) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    heading_level=1,
                    text="第八节 财务报告",
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    heading_level=2,
                    text="42、其他重要的会计政策和会计估计",
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    heading_level=2,
                    text="与回购公司股份相关的会计处理方法",
                ),
                PreparedElement(
                    kind="text", order_index=4, text="按实际支付的金额作为库存股处理。"
                ),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    heading_level=2,
                    text="43、重要会计政策和会计估计变更",
                ),
                PreparedElement(kind="text", order_index=6, text="无变更。"),
            ]
        )

        body = next(item for item in placed if item.text == "按实际支付的金额作为库存股处理。")
        change = next(item for item in placed if item.text == "无变更。")
        orphan = next(
            item for item in placed if item.text == "42、其他重要的会计政策和会计估计"
        )
        self.assertEqual(
            body.heading_path,
            ["第八节 财务报告", "与回购公司股份相关的会计处理方法"],
        )
        self.assertEqual(
            change.heading_path,
            ["第八节 财务报告", "43、重要会计政策和会计估计变更"],
        )
        self.assertEqual(orphan.heading_path[-1], orphan.text)

    def test_s2_does_not_reparent_from_one_ordinal_coincidence(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    heading_level=1,
                    text="第八节 财务报告",
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    heading_level=2,
                    text="十二、与金融工具相关的风险",
                ),
                PreparedElement(
                    kind="heading", order_index=3, heading_level=2, text="(一) 信用风险"
                ),
                PreparedElement(kind="text", order_index=4, text="信用风险管理。"),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    heading_level=2,
                    text="(二) 流动性风险",
                ),
                PreparedElement(kind="text", order_index=6, text="流动性管理。"),
                PreparedElement(
                    kind="heading",
                    order_index=7,
                    heading_level=2,
                    text="三、（市场风险）",
                ),
                PreparedElement(kind="text", order_index=8, text="市场风险说明。"),
            ]
        )

        market = placed[-1]
        self.assertEqual(
            market.heading_path,
            ["第八节 财务报告", "三、（市场风险）"],
        )

        counterexample = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, heading_level=1, text="第八节"
                ),
                PreparedElement(
                    kind="heading", order_index=2, heading_level=2, text="二、业务概况"
                ),
                PreparedElement(
                    kind="heading", order_index=3, heading_level=3, text="3、子项"
                ),
                PreparedElement(
                    kind="heading", order_index=4, heading_level=2, text="四、独立事项"
                ),
                PreparedElement(kind="text", order_index=5, text="独立事实。"),
            ]
        )
        self.assertEqual(
            counterexample[-1].heading_path,
            ["第八节", "四、独立事项"],
        )

    def test_s2_dot_subitem_nests_under_dunhao_note_heading(self) -> None:
        # cn_a_v6 (round14): 、-numbered 科目 headings and .-numbered sub-items
        # are different levels. On the real 江海 annual, "1. 存货的分类" used to
        # evict "17、存货" from the stack, so every 存货 policy sub-item lost its
        # 科目 ancestor from heading_path (text-kind and heading-kind alike:
        # MinerU tags "1. 共同控制、重大影响的判断" as heading_level=2).
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    heading_level=1,
                    text="第八节 财务报告",
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    heading_level=2,
                    text="五、重要会计政策及会计估计",
                ),
                PreparedElement(
                    kind="heading", order_index=3, heading_level=2, text="17、存货"
                ),
                PreparedElement(kind="text", order_index=4, text="1. 存货的分类"),
                PreparedElement(
                    kind="text", order_index=5, text="存货包括产成品、在产品和原材料。"
                ),
                PreparedElement(
                    kind="text", order_index=6, text="2. 发出存货的计价方法"
                ),
                PreparedElement(
                    kind="text", order_index=7, text="发出存货采用加权平均法。"
                ),
                PreparedElement(
                    kind="heading",
                    order_index=8,
                    heading_level=2,
                    text="18、持有待售资产",
                ),
                PreparedElement(
                    kind="heading",
                    order_index=9,
                    heading_level=2,
                    text="1. 共同控制、重大影响的判断",
                ),
                PreparedElement(kind="text", order_index=10, text="按照相关约定判断。"),
            ]
        )

        by_text = {item.text: item for item in placed}
        self.assertEqual(
            by_text["存货包括产成品、在产品和原材料。"].heading_path,
            ["第八节 财务报告", "五、重要会计政策及会计估计", "17、存货"],
        )
        self.assertEqual(
            by_text["发出存货采用加权平均法。"].heading_path,
            ["第八节 财务报告", "五、重要会计政策及会计估计", "17、存货"],
        )
        # The 、-chain continues past the sub-items (18、 ordinal follows 17、),
        # and a heading-kind dot item nests instead of evicting.
        self.assertEqual(
            by_text["按照相关约定判断。"].heading_path,
            [
                "第八节 财务报告",
                "五、重要会计政策及会计估计",
                "18、持有待售资产",
                "1. 共同控制、重大影响的判断",
            ],
        )

    def test_s2_dotted_outline_recovers_flattened_parser_hierarchy(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "heading_level": 1,
                    "text": "财务报表附注",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 2,
                    "heading_level": 1,
                    "text": "3. 重要会计政策和会计估计",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 3,
                    "heading_level": 1,
                    "text": "3.1 总体经营情况",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 4,
                    "heading_level": 1,
                    "text": "3.1.1 外部环境",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 5,
                    "text": "外部环境保持复杂。",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 6,
                    "heading_level": 1,
                    "text": "3.2 利润表分析",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 7,
                    "text": "利润表分析正文。",
                },
            ]
        )

        placed = s2_apply_heading_tree(result.elements)
        body = {item.text: item for item in placed if item.text}
        self.assertEqual(
            body["外部环境保持复杂。"].heading_path,
            [
                "财务报表附注",
                "3. 重要会计政策和会计估计",
                "3.1 总体经营情况",
                "3.1.1 外部环境",
            ],
        )
        self.assertEqual(
            body["利润表分析正文。"].heading_path,
            [
                "财务报表附注",
                "3. 重要会计政策和会计估计",
                "3.2 利润表分析",
            ],
        )

    def test_s2_latin_and_roman_outline_recovers_flattened_hierarchy(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "heading_level": 1,
                    "text": "3. 重要会计政策和会计估计",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 2,
                    "heading_level": 1,
                    "text": "(a) 遵循企业会计准则的声明",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 3,
                    "heading_level": 1,
                    "text": "(i) 计量基础",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 4,
                    "text": "采用权责发生制。",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 5,
                    "heading_level": 1,
                    "text": "(b) 重要会计政策",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 6,
                    "text": "政策保持一致。",
                },
            ]
        )

        placed = s2_apply_heading_tree(result.elements)
        body = {item.text: item for item in placed if item.text}
        self.assertEqual(
            body["采用权责发生制。"].heading_path,
            [
                "3. 重要会计政策和会计估计",
                "(a) 遵循企业会计准则的声明",
                "(i) 计量基础",
            ],
        )
        self.assertEqual(
            body["政策保持一致。"].heading_path,
            ["3. 重要会计政策和会计估计", "(b) 重要会计政策"],
        )

    def test_parenthesized_i_continues_a_proven_latin_sibling_run(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "heading_level": 1,
                    "text": "3. 现金流量表附注",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 2,
                    "heading_level": 1,
                    "text": "(h) 现金流量表补充资料",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 3,
                    "heading_level": 1,
                    "text": "(i) 现金及现金等价物的构成",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 4,
                    "text": "构成明细如下。",
                },
            ]
        )

        placed = s2_apply_heading_tree(result.elements)
        body = next(item for item in placed if item.text == "构成明细如下。")
        self.assertEqual(
            body.heading_path,
            ["3. 现金流量表附注", "(i) 现金及现金等价物的构成"],
        )

    def test_direct_roman_run_is_sibling_children_of_non_latin_parent(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "heading_level": 1,
                    "text": "1.1 信用风险管理",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 2,
                    "heading_level": 1,
                    "text": "(i) 发放贷款",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 3,
                    "text": "贷款风险说明。",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 4,
                    "heading_level": 1,
                    "text": "(ii) 债券",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 5,
                    "text": "债券风险说明。",
                },
            ]
        )

        placed = s2_apply_heading_tree(result.elements)
        body = {item.text: item for item in placed if item.text}
        self.assertEqual(
            body["贷款风险说明。"].heading_path,
            ["1.1 信用风险管理", "(i) 发放贷款"],
        )
        self.assertEqual(
            body["债券风险说明。"].heading_path,
            ["1.1 信用风险管理", "(ii) 债券"],
        )

    def test_roman_outline_supports_ordinals_beyond_ten(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "heading_level": 1,
                    "text": "58. 风险管理",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 2,
                    "heading_level": 1,
                    "text": "(a) 信用风险",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 3,
                    "heading_level": 1,
                    "text": "(xi) 本金变动",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 4,
                    "heading_level": 1,
                    "text": "(xii) 信用质量分析",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 5,
                    "text": "信用质量正文。",
                },
            ]
        )

        placed = s2_apply_heading_tree(result.elements)
        body = next(item for item in placed if item.text == "信用质量正文。")
        self.assertEqual(
            body.heading_path,
            ["58. 风险管理", "(a) 信用风险", "(xii) 信用质量分析"],
        )

    def test_latin_clause_depth_is_relative_to_digit_paren_parent(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "heading_level": 1,
                    "text": "(17) 无形资产",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 2,
                    "heading_level": 1,
                    "text": "(d) 使用寿命",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 3,
                    "text": "使用寿命按年限确定。",
                },
            ]
        )

        placed = s2_apply_heading_tree(result.elements)
        body = next(item for item in placed if item.text == "使用寿命按年限确定。")
        self.assertEqual(body.heading_path, ["(17) 无形资产", "(d) 使用寿命"])

    def test_dotted_outline_never_uses_a_different_numeric_prefix_as_parent(
        self,
    ) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "heading_level": 1,
                    "text": "第一章 总览",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 2,
                    "heading_level": 1,
                    "text": "1. 已知分支",
                },
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 3,
                    "heading_level": 1,
                    "text": "2.1 缺失父标题的分支",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 4,
                    "text": "证据正文。",
                },
            ]
        )

        placed = s2_apply_heading_tree(result.elements)
        body = next(item for item in placed if item.text == "证据正文。")
        self.assertEqual(
            body.heading_path,
            ["第一章 总览", "2.1 缺失父标题的分支"],
        )

    def test_root_heading_only_siblings_without_document_title_are_not_grouped(
        self,
    ) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "风险一",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "风险二",
                    },
                ]
            },
            filing_type="other",
            document_title=None,
        )

        self.assertEqual([unit.payload_kind for unit in units], ["text", "text"])
        self.assertEqual([unit.title for unit in units], ["风险一", "风险二"])
        self.assertEqual(stats.heading_outline_units_generated, 0)

    def test_headerless_captioned_table_uses_its_local_caption_anchor(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 1,
                        "table_caption": ["募集资金使用表"],
                        "table": {"headers": ["项目"], "rows": [["研发"]]},
                    }
                ]
            },
            filing_type="other",
            document_title=None,
        )

        self.assertEqual(units[0].heading_path, ["募集资金使用表"])

    def test_s2_colon_lead_in_nests_under_note_instead_of_evicting(self) -> None:
        # Round14 companion defect: "2. 当公司…下列项目：" (heading candidate)
        # used to evict "8、合营安排…" while its sibling "1. …。" stayed body
        # text — the same note's children split across two ancestries.
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    heading_level=2,
                    text="8、合营安排分类及共同经营会计处理方法",
                ),
                PreparedElement(
                    kind="text",
                    order_index=2,
                    text="1. 合营安排分为共同经营和合营企业。",
                ),
                PreparedElement(
                    kind="text",
                    order_index=3,
                    text="2. 当公司为共同经营的合营方时，确认下列项目：",
                ),
                PreparedElement(
                    kind="text", order_index=4, text="确认单独所持有的资产。"
                ),
            ]
        )

        self.assertEqual(
            placed[0].heading_path, ["8、合营安排分类及共同经营会计处理方法"]
        )
        self.assertEqual(
            placed[-1].heading_path,
            ["8、合营安排分类及共同经营会计处理方法"],
        )

    def test_s2_decimal_amount_line_is_not_a_heading(self) -> None:
        # cn_a_v6: the dot class carries (?!\d) — "1.5亿元…" is an amount
        # sentence, not a numbered heading.
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    heading_level=2,
                    text="一、募集资金使用情况",
                ),
                PreparedElement(
                    kind="text", order_index=2, text="1.5亿元用于产能建设项目"
                ),
            ]
        )

        self.assertEqual(placed[0].text, "1.5亿元用于产能建设项目")
        self.assertEqual(placed[0].heading_path, ["一、募集资金使用情况"])

    def test_s2_footnote_line_never_becomes_heading(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    heading_level=1,
                    text="十一、关联方及关联交易",
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    heading_level=2,
                    text="[注] 该金额系双方 2025 年 1-2 月交易金额",
                ),
                PreparedElement(kind="text", order_index=3, text="承租情况如下。"),
            ]
        )

        self.assertEqual(placed[0].text, "[注] 该金额系双方 2025 年 1-2 月交易金额")
        self.assertEqual(placed[0].kind, "text")
        self.assertEqual(placed[0].heading_path, ["十一、关联方及关联交易"])
        self.assertEqual(placed[1].heading_path, ["十一、关联方及关联交易"])

    def test_boilerplate_guarantee_line_is_preserved_as_source_evidence(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "重要提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": (
                            "本公司董事会及全体董事保证本公告内容不存在任何虚假记载、"
                            "误导性陈述或者重大遗漏，并对其内容的真实性、准确性和完整性"
                            "承担法律责任。\n公司存在退市风险，请投资者注意。"
                        ),
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 1)
        self.assertIn("董事会及全体董事保证", units[0].payload["text"])
        self.assertIn("公司存在退市风险", units[0].payload["text"])

    def test_guarantee_like_line_with_business_fact_is_never_dropped(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": (
                            "公司保证本公告内容真实，2025年收入增长20%，"
                            "不存在重大遗漏。"
                        ),
                    }
                ]
            },
            filing_type="other",
        )

        self.assertIn("收入增长20%", str(units[0].payload))

    def test_blank_rows_and_merged_cells_preserve_source_grid(self) -> None:
        stats = BuildStats()
        units = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=1,
                    table={
                        "headers": ["项目", "金额"],
                        "rows": [["收入", "100"], ["", " "], ["成本", "60"]],
                        "merged_cells": [
                            {"row": 3, "col": 0, "rowspan": 1, "colspan": 2}
                        ],
                    },
                )
            ],
            stats,
        )

        self.assertEqual(
            units[0].payload["rows"],
            [["收入", "100"], ["", " "], ["成本", "60"]],
        )
        self.assertEqual(stats.dropped_blank_table_rows, 0)
        self.assertEqual(
            units[0].payload["merged_cells"],
            [{"row": 3, "col": 0, "rowspan": 1, "colspan": 2}],
        )
        locator = units[0].artifact_locator or {}
        self.assertNotIn("merged_cells", locator)

    def test_flat_document_units_anchor_under_document_title(self) -> None:
        # Codex round7 美的 IR: form-table filings have no headings at all —
        # units anchored under the registry title, never heading_path=[].
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 1,
                        "table": {"headers": ["活动类别"], "rows": [["特定对象调研"]]},
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="美的集团股份有限公司投资者关系活动记录表",
        )

        self.assertEqual(
            units[0].heading_path,
            ["美的集团股份有限公司投资者关系活动记录表"],
        )
        self.assertIsNotNone(units[0].title)

    def test_qa_vocabulary_alone_does_not_downgrade_table_quality(self) -> None:
        shredded = (
            "机系列销量超56万套。户数量持续增长。" * 30 + "3. 公司业务当前的进展？"
        )
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 1,
                        "table": {"headers": [shredded], "rows": [["答：进展顺利。"]]},
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="投资者关系活动记录表",
        )

        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertEqual(table.quality_status, "ok")
        self.assertIn("进展顺利", str(table.payload))

    def test_year_line_never_becomes_numbered_heading(self) -> None:
        # "2025 年度" matched the ^\d+\s numbered-heading pattern (user-found
        # bug: it became a heading node under 财务报表附注 and stranded a
        # 金额单位 line as its own unit).
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, heading_level=1, text="财务报表附注"
                ),
                PreparedElement(
                    kind="heading", order_index=2, heading_level=2, text="2025 年度"
                ),
                PreparedElement(kind="text", order_index=3, text="接上表。"),
            ]
        )

        # The year line nests as an unnumbered sub-label, never a numbered
        # top-level node; and it must not evict 财务报表附注.
        self.assertEqual(placed[-1].heading_path[0], "财务报表附注")

    def test_plain_text_numeric_quantities_never_become_headings(self) -> None:
        for text in ("1 年内到期债务为100万元", "2 个项目已完成验收"):
            with self.subTest(text=text):
                placed = s2_apply_heading_tree(
                    [
                        PreparedElement(kind="text", order_index=1, text=text),
                        PreparedElement(kind="text", order_index=2, text="后续事实。"),
                    ]
                )
                self.assertEqual(placed[0].heading_path, [])
                self.assertEqual(placed[1].heading_path, [])

    def test_unit_declaration_family_generalizes(self) -> None:
        # Round11 (user directive 泛化能力): the declaration is a pattern
        # FAMILY across filing formats; substantive sentences never match.
        strip = [
            "单位：元",
            "金额单位：人民币元",
            "货币单位：万元",
            "币种：人民币",
            "除特别注明外，本财务报表附注均以人民币元列示。",
            "本报告中如无特殊说明，货币单位均为人民币元。",
        ]
        keep = [
            "财务附注中报表的单位为：元",
            "营业收入单位：万元",
            "单位：",
            "金额单位为",
            "币种：",
            "公司记账本位币为人民币。",
            "境外子公司以美元为记账本位币，折算方法见会计政策。",
            "本报告中如无特殊说明，均指合并口径的经营数据及相关分析。",
        ]
        for line in strip:
            self.assertTrue(rules.is_unit_declaration_line(line), line)
        for line in keep:
            self.assertFalse(rules.is_unit_declaration_line(line), line)

    def test_amount_unit_declaration_stays_with_narrative_values(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "财务报表附注",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "金额单位：人民币元\n应收账款期末余额如下。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(
            units[0].payload["text"],
            "金额单位：人民币元\n应收账款期末余额如下。",
        )

    def test_expanded_semantic_vocabulary_samples(self) -> None:
        cases = [
            ("4、研发投入", "研发费用及人员构成如下", "rd_investment"),
            ("前五名客户销售情况", "", "customer_concentration"),
            ("现金流量表主要项目", "", "cash_flow"),
            ("利润分配方案", "", "dividend"),
            ("回购股份实施结果", "", "share_buyback"),
        ]
        for title, text, expected in cases:
            with self.subTest(key=expected):
                unit = UnitDraft(
                    payload_kind="text",
                    payload={"text": text or title},
                    source_order=1,
                    heading_path=[title],
                    title=title,
                )
                self.assertEqual(
                    semantic_key_for_unit(unit, filing_type="annual_report"),
                    expected,
                )

    def test_semantic_labels_use_complete_source_hierarchy(self) -> None:
        unit = UnitDraft(
            payload_kind="text",
            payload={"text": "本期测试结果如下。"},
            source_order=1,
            heading_path=["第八节 财务报告", "其他"],
            structural_path=[
                "第八节 财务报告",
                "七、资产减值",
                "商誉减值测试",
                "其他",
            ],
            title="其他",
        )

        self.assertIn(
            "goodwill_impairment",
            semantic_keys_for_unit(unit, filing_type="annual_report"),
        )

    def test_document_title_does_not_inject_event_keys_into_units(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "公司日常经营正常。" * 40,
                    },
                ]
            },
            filing_type="other",
            document_title="贵州茅台：关于回购股份实施结果暨股份变动的公告",
        )

        for unit in units:
            self.assertNotIn("share_buyback_event", unit.semantic_keys or [])

    def test_negated_document_title_is_not_published_as_confirmed_event(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "该项目对本期经营无重大影响。",
                    }
                ]
            },
            filing_type="other",
            document_title="关于项目未中标的公告",
        )

        self.assertTrue(units)
        self.assertTrue(
            all("contract_award" not in (unit.semantic_keys or []) for unit in units)
        )

    def test_note_vocabulary_keys_notes_sections(self) -> None:
        # design/retrieval-and-semantic-keys.md §4: 附注标题是法定受控词表
        # （编报规则第15号），标题剥编号后三级匹配派生 note key。
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第八节 财务报告",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "七、合并财务报表项目注释",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 2,
                        "text": "75、其他综合收益",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "本期其他综合收益变动如下。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 5,
                        "heading_level": 2,
                        "text": "八、研发支出",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 6,
                        "text": "研发支出按性质列示。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        # The tiny doc groups into one section unit; member note keys must
        # surface on the aggregated semantic_keys column.
        all_keys = {key for unit in units for key in (unit.semantic_keys or [])}
        self.assertIn("other_comprehensive_income", all_keys)
        self.assertIn("rd_expenses", all_keys)

    def test_generic_leaf_inherits_ancestor_note_key(self) -> None:
        # Round13 用户裁决: "(1) 明细情况" 类无科目语义标题必须从最近的科目
        # 祖先继承键——L2 除 heading 外就靠 semantic_keys 检索。
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第八节 财务报告",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "19、其他非流动金融资产",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 2,
                        "text": "(1) 明细情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "其他非流动金融资产明细如下表所示。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        target = next(u for u in units if "明细" in str(u.payload))
        self.assertIn("other_noncurrent_financial_assets", target.semantic_keys or [])
        self.assertIn("financial_report_chapter", target.semantic_keys or [])
        # 最具体键作为单值 semantic_key
        self.assertEqual(target.semantic_key, "other_noncurrent_financial_assets")

    def test_note_vocabulary_applies_to_other_filing_types(self) -> None:
        # Round13: 审计报告等 'other' 文档同样承载报表/附注结构，词表键开放。
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "其他综合收益",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "临时公告正文。" * 1200,
                    },
                ]
            },
            filing_type="other",
        )
        self.assertTrue(
            any("other_comprehensive_income" in (u.semantic_keys or []) for u in units)
        )

    def test_full_s1_s7_sse_spaced_announce_no_dropped_as_noise(self) -> None:
        # round17 语料：沪市信头「公告编号：临 2026-026」编号带内部空格，
        # 旧模式漏放行，整段残片曾挂在合成锚下入库。
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "公告编号：临 2026-026",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "关于聘任董事会秘书的公告",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "董事会决定聘任张三为董事会秘书。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(stats.dropped_by_kind.get("standalone_noise", 0), 0)
        self.assertEqual(stats.anchored_header_units, 0)
        paths = {part for unit in units for part in unit.heading_path}
        self.assertNotIn("公告头信息", paths)
        self.assertIn("公告编号：临 2026-026", units[0].payload["text"])

    def test_full_s1_s7_headerless_prefix_prefers_document_title(self) -> None:
        # round17：首标题前的真内容属于文档本身——有注册标题用它做锚，
        # 「公告头信息」只在 document_title 缺失时兜底。表单类文档的被困
        # 标题与正文粘连无分隔，按宁漏勿脏不抽取，锚到文档标题即根本解法。
        elements = [
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 1,
                "text": "截至本公告披露日，公司回购专用账户持有股份 1,200,000 股。",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 2,
                "heading_level": 1,
                "text": "一、回购进展",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 3,
                "text": "回购按计划推进。",
            },
        ]

        units, stats = build_unit_drafts_s1_s7(
            {"elements": elements},
            filing_type="annual_report",
            document_title="某公司关于回购股份进展的公告",
        )
        self.assertEqual(stats.anchored_header_units, 1)
        by_path = {tuple(unit.heading_path): unit for unit in units}
        header = by_path[("某公司关于回购股份进展的公告",)]
        self.assertIn("回购专用账户", str(header.payload))

        units2, _ = build_unit_drafts_s1_s7(
            {"elements": elements}, filing_type="annual_report"
        )
        by_path2 = {tuple(unit.heading_path): unit for unit in units2}
        self.assertIn((), by_path2)
        self.assertNotIn(("公告头信息",), by_path2)

    def test_table_qa_text_is_not_published_as_duplicate_peer_unit(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 1,
                        "table_caption": ["投关问答"],
                        "table": {
                            "headers": ["内容"],
                            "rows": [["问:产能如何？\n答:产能稳定。"]],
                        },
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "后续正文。",
                    },
                ]
            },
            filing_type="investor_relations",
        )

        self.assertEqual(
            [unit.payload_kind for unit in units],
            ["table", "text"],
        )
        self.assertIn("问:产能如何？", str(units[0].payload))
        self.assertEqual(units[1].payload["text"], "后续正文。")

    def test_s4_parses_chinese_qa_and_rejects_unstable_boundaries(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            title="投资者关系活动主要内容介绍",
        )
        parsed = s4_build_qa_units(
            "1.美国加征关税对公司有什么影响？答:美国收入占比很低。",
            source=source,
        )
        self.assertFalse(parsed.unstable)
        self.assertEqual(
            parsed.units[0].payload["question"], "美国加征关税对公司有什么影响？"
        )
        self.assertEqual(parsed.units[0].payload["answer"], "美国收入占比很低。")

        self.assertTrue(s4_build_qa_units("答:没有问题。", source=source).unstable)
        self.assertTrue(
            s4_build_qa_units("问:问题？\n答:一\n回复:二", source=source).unstable
        )
        self.assertTrue(s4_build_qa_units("问:问题？", source=source).unstable)

    def test_explicit_answer_mode_keeps_wrapped_lines_in_question(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["交流问题"],
        )

        parsed = s4_build_qa_units(
            "问：第一行问题\n问题续行？\n答：真实回答",
            source=source,
            require_explicit_answer=True,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.units[0].payload["question"], "第一行问题问题续行？")
        self.assertEqual(parsed.units[0].payload["answer"], "真实回答")

    def test_s4_numbered_prose_is_not_promoted_by_a_later_answer_marker(
        self,
    ) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )

        parsed = s4_build_qa_units(
            "1、公司基本情况\n公司成立于2000年。\n问：收入如何？\n答：收入增长。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 1)
        self.assertEqual(parsed.units[0].payload["question"], "收入如何？")
        self.assertEqual(parsed.leading_text, "1、公司基本情况\n公司成立于2000年。")

        unlabelled = s4_build_qa_units("问：收入如何？\n公司收入稳定。", source=source)
        self.assertTrue(unlabelled.unstable)
        self.assertEqual(unlabelled.units, [])

    def test_stable_qa_conversion_preserves_leading_business_fact(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={
                "text": (
                    "交流前，公司说明本季度收入同比增长20%。\n"
                    "问：毛利率为何提升？\n答：产品结构改善。"
                )
            },
            source_order=1,
            heading_path=["交流问题"],
            structural_path=["交流问题"],
        )

        converted = replace_text_units_with_qa_where_stable([source])

        self.assertEqual([unit.payload_kind for unit in converted], ["text", "qa"])
        self.assertIn("收入同比增长20%", converted[0].payload["text"])
        self.assertEqual(converted[0].quality_status, "needs_review")
        self.assertEqual(converted[1].payload["answer"], "产品结构改善。")

    def test_s4_unstable_text_block_becomes_needs_review_text(self) -> None:
        units = replace_text_units_with_qa_where_stable(
            [
                UnitDraft(
                    payload_kind="text",
                    payload={"text": "答:没有问题。"},
                    source_order=1,
                )
            ]
        )

        self.assertEqual(units[0].payload_kind, "text")
        self.assertEqual(units[0].quality_status, "needs_review")

    def test_s5_does_not_infer_continuation_from_shape_and_page(self) -> None:
        stats = BuildStats()
        elements = [
            PreparedElement(
                kind="table",
                order_index=1,
                page_no=10,
                table={"headers": [], "rows": [["项目", "金额"], ["收入", "10"]]},
                table_footnote=["含追溯调整。"],
                title="应收账款",
            ),
            PreparedElement(
                kind="table",
                order_index=2,
                page_no=11,
                table={"headers": [], "rows": []},
                table_html="",
            ),
            PreparedElement(
                kind="table",
                order_index=3,
                page_no=11,
                table={"headers": [], "rows": [["项目", "金额"], ["成本", "8"]]},
                table_caption=[],
                title="应收账款",
            ),
        ]

        units = s5_build_table_units(elements, stats)

        self.assertEqual(len(units), 2)
        self.assertEqual(units[0].payload["headers"], [])
        self.assertEqual(
            units[0].payload["rows"], [["项目", "金额"], ["收入", "10"]]
        )
        self.assertEqual(
            units[1].payload["rows"], [["项目", "金额"], ["成本", "8"]]
        )
        self.assertEqual(units[0].payload["notes"], ["含追溯调整。"])
        self.assertEqual(stats.dropped_by_kind["table_empty"], 1)

    def test_s5_preserves_headerless_first_row_as_data(self) -> None:
        units = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=1,
                    table={
                        "headers": [],
                        "rows": [["2024", "100"], ["2025", "200"]],
                    },
                )
            ],
            BuildStats(),
        )

        self.assertEqual(units[0].payload["headers"], [])
        self.assertEqual(
            units[0].payload["rows"],
            [["2024", "100"], ["2025", "200"]],
        )

    def test_caption_and_note_only_table_remains_searchable(self) -> None:
        units = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=1,
                    table_caption=["重大合同履行情况"],
                    table_footnote=["注：截至期末尚未履行金额为1亿元。"],
                    table={"headers": [], "rows": []},
                )
            ],
            BuildStats(),
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].quality_status, "needs_review")
        self.assertIn("1亿元", str(units[0].payload))

    def test_truly_empty_table_carrier_is_still_dropped(self) -> None:
        stats = BuildStats()
        units = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=1,
                    table={"headers": [], "rows": []},
                )
            ],
            stats,
        )

        self.assertEqual(units, [])
        self.assertEqual(stats.dropped_by_kind["table_empty"], 1)

    def test_table_checkbox_caption_is_preserved_without_becoming_title(self) -> None:
        for caption, applicability in (
            ("√是 □否", None),
            ("√适用 □不适用", "applicable"),
        ):
            with self.subTest(caption=caption):
                units, _ = build_unit_drafts_s1_s7(
                    {
                        "elements": [
                            {
                                "kind": "table",
                                "raw_kind": "table",
                                "order_index": 1,
                                "table_caption": [caption],
                                "table": {
                                    "headers": ["项目"],
                                    "rows": [["事实"]],
                                },
                            }
                        ]
                    },
                    filing_type="annual_report",
                )

                table = units[0]
                self.assertEqual(table.payload["caption"], [caption])
                self.assertIsNone(table.title)
                self.assertEqual(table.applicability, applicability)

    def test_s5_never_merges_tables_across_section_boundary(self) -> None:
        # Audit-report expense notes share one 3-column shape; once cn_a_v6
        # let their headings enter the stack, the tables became adjacent and
        # column count alone merged 3. 销售费用 into 1. 营业收入 — the heading
        # vanished from every path (ub-2026.07-18 swallowed-heading audit).
        stats = BuildStats()
        base = ["财务报表附注", "五、合并财务报表项目注释", "(二) 合并利润表项目注释"]
        elements = [
            PreparedElement(
                kind="table",
                order_index=1,
                page_no=20,
                table={
                    "headers": ["项目", "本期", "上期"],
                    "rows": [["工资", "1", "2"]],
                },
                heading_path=[*base, "3. 销售费用"],
                title="3. 销售费用",
            ),
            PreparedElement(
                kind="table",
                order_index=2,
                page_no=20,
                table={
                    "headers": ["项目", "本期", "上期"],
                    "rows": [["折旧", "3", "4"]],
                },
                heading_path=[*base, "4. 管理费用"],
                title="4. 管理费用",
            ),
        ]

        units = s5_build_table_units(elements, stats)

        self.assertEqual(len(units), 2)
        self.assertEqual(units[0].title, "3. 销售费用")
        self.assertEqual(units[1].title, "4. 管理费用")

    def test_s5_never_merges_independent_same_page_tables_by_shape(self) -> None:
        stats = BuildStats()
        path = ["一、经营情况"]
        elements = [
            PreparedElement(
                kind="table",
                order_index=1,
                page_no=20,
                table={"headers": ["项目", "金额"], "rows": [["收入", "10"]]},
                heading_path=path,
                structural_path=path,
            ),
            PreparedElement(
                kind="table",
                order_index=2,
                page_no=20,
                table={"headers": ["项目", "金额"], "rows": [["成本", "8"]]},
                heading_path=path,
                structural_path=path,
            ),
        ]

        units = s5_build_table_units(elements, stats)

        self.assertEqual(len(units), 2)

    def test_s5_table_parse_failed_uses_raw_html_payload(self) -> None:
        stats = BuildStats()
        units = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=1,
                    table={"headers": [], "rows": []},
                    table_caption=["失败表"],
                    table_footnote=["注"],
                    table_html="<table>",
                    table_parse_failed=True,
                )
            ],
            stats,
        )

        self.assertEqual(
            units[0].payload,
            {"caption": ["失败表"], "raw_html": "<table>", "notes": ["注"]},
        )
        self.assertEqual(units[0].quality_status, "needs_review")

    def test_s5_empty_grid_with_html_fails_closed_even_without_upstream_flag(
        self,
    ) -> None:
        units = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=1,
                    table={"headers": [], "rows": []},
                    table_html="<table><tr><td>收入</td></tr></table>",
                    table_parse_failed=False,
                )
            ],
            BuildStats(),
        )

        self.assertEqual(len(units), 1)
        self.assertIn("收入", units[0].payload["raw_html"])
        self.assertEqual(units[0].quality_status, "needs_review")

    def test_full_s1_s7_preserves_table_parse_failed_raw_html(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 1,
                        "table_caption": ["失败表"],
                        "table_footnote": ["注"],
                        "table_html": "<table>",
                        "table_parse_failed": True,
                        "table": {"headers": [], "rows": []},
                    }
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload_kind, "table")
        self.assertEqual(
            units[0].payload,
            {"caption": ["失败表"], "raw_html": "<table>", "notes": ["注"]},
        )
        self.assertEqual(units[0].quality_status, "needs_review")
        self.assertEqual(stats.generated_by_kind["table"], 1)
        self.assertEqual(stats.dropped_by_kind["table_empty"], 0)

    def test_payload_inner_key_contracts_by_kind(self) -> None:
        text_unit = s3_build_text_units(
            [PreparedElement(kind="text", order_index=1, text="正文")]
        )[0]
        qa_unit = s4_build_qa_units(
            "问:问题？\n答:答案",
            source=UnitDraft(payload_kind="text", payload={"text": ""}, source_order=2),
        ).units[0]
        image_unit = s1_preprocess_elements(
            [
                {
                    "kind": "image",
                    "raw_kind": "image",
                    "order_index": 3,
                    "image_path": f"images/{'c' * 64}.jpg",
                    "caption": "股权结构图",
                }
            ]
        ).elements[0]
        table_unit = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=4,
                    table_caption=["应收账款账龄"],
                    table_footnote=["含追溯调整。"],
                    table={"headers": ["账龄"], "rows": [["合计"]]},
                )
            ],
            BuildStats(),
        )[0]
        failed_table = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=5,
                    table_caption=["失败表"],
                    table_footnote=["注"],
                    table={"headers": [], "rows": []},
                    table_html="<table>",
                    table_parse_failed=True,
                )
            ],
            BuildStats(),
        )[0]

        self.assertEqual(set(text_unit.payload), {"text"})
        self.assertEqual(set(qa_unit.payload), {"question", "answer", "raw_text"})
        self.assertEqual(set(image_unit.payload), {"image_ref", "caption", "context"})
        self.assertEqual(image_unit.quality_status, "needs_review")
        self.assertEqual(
            set(table_unit.payload),
            {"caption", "unit", "headers", "rows", "merged_cells", "notes"},
        )
        self.assertIn("追溯调整", table_unit.payload["notes"][0])
        self.assertEqual(set(failed_table.payload), {"caption", "raw_html", "notes"})

    def test_s6_preserves_vocabulary_named_sections(self) -> None:
        stats = BuildStats()
        kept = s6_filter_units(
            [
                UnitDraft(
                    payload_kind="text",
                    payload={"text": "释义内容"},
                    source_order=1,
                    heading_path=["释义"],
                    title="释义",
                ),
                UnitDraft(
                    payload_kind="text",
                    payload={"text": "存在退市风险"},
                    source_order=2,
                    heading_path=["重要提示"],
                    title="重要提示",
                ),
                UnitDraft(
                    payload_kind="text",
                    payload={"text": "存在风险"},
                    source_order=3,
                    heading_path=["风险提示"],
                    title="风险提示",
                ),
            ],
            stats,
        )

        self.assertEqual([unit.title for unit in kept], ["释义", "重要提示", "风险提示"])
        self.assertEqual(stats.skipped_sections, [])

    def test_s7_semantic_key_and_quality(self) -> None:
        stats = BuildStats()
        units = s7_finalize_units(
            [
                UnitDraft(
                    payload_kind="table",
                    payload={
                        "caption": ["应收账款账龄披露"],
                        "unit": "元",
                        "headers": ["账龄"],
                        "rows": [["合计"]],
                        "notes": [],
                    },
                    source_order=1,
                    title="应收账款账龄披露",
                ),
                UnitDraft(
                    payload_kind="text",
                    payload={"text": "\ufffd" * 4 + "ab"},
                    source_order=2,
                    title="乱码",
                ),
            ],
            filing_type="annual_report",
            stats=stats,
        )

        self.assertEqual(units[0].semantic_key, "receivable_aging")
        self.assertEqual(units[1].quality_status, "unusable")
        self.assertEqual(stats.generated_by_kind["table"], 1)
        self.assertEqual(stats.unusable_count, 1)

    def test_semantic_key_tariff_not_filing_type_limited(self) -> None:
        unit = UnitDraft(
            payload_kind="text",
            payload={"text": "关税影响"},
            source_order=1,
            title="关税影响",
        )

        self.assertEqual(
            semantic_key_for_unit(unit, filing_type="other"),
            "tariff_exposure",
        )

    def test_cover_prelude_is_not_bulk_deleted_before_first_section(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "某某股份有限公司",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "2025 年年度报告",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "股票代码：000000 股票简称：某某股份",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 4,
                        "heading_level": 1,
                        "text": "第一节 重要提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 5,
                        "text": "公司存在退市风险，请投资者注意。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 3)
        by_path = {tuple(unit.heading_path): unit for unit in units}
        self.assertIn(("某某股份有限公司",), by_path)
        self.assertEqual(
            by_path[("某某股份有限公司",)].payload["text"],
            "某某股份有限公司",
        )
        self.assertIn("股票代码：000000", by_path[("2025 年年度报告",)].payload["text"])
        self.assertEqual(
            by_path[("第一节 重要提示",)].payload["text"],
            "公司存在退市风险，请投资者注意。",
        )

    def test_cover_prelude_inactive_without_structural_sections(self) -> None:
        # Short announcements have no 第X节 structure; nothing may be dropped.
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "某某股份有限公司董事会决议公告",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "本公司董事会于近日审议通过如下议案。",
                    },
                ]
            },
            filing_type="other",
        )

        self.assertEqual(len(units), 1)

    def test_standalone_unit_declaration_is_preserved_with_its_table(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第二节 主要财务指标",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "单位：元",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "table": {"headers": ["项目"], "rows": [["营业收入"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual([unit.payload_kind for unit in units], ["text", "table"])
        self.assertEqual(units[0].payload["text"], "单位：元")
        self.assertEqual(units[1].payload["unit"], "元")
        self.assertEqual(units[0].heading_path, units[1].heading_path)

    def test_table_unit_declaration_never_crosses_source_sections(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "一、金额表",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "单位：万元",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "二、人数表",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 4,
                        "table": {
                            "headers": ["部门", "人数"],
                            "rows": [["研发", "10"]],
                        },
                    },
                ]
            },
            filing_type="annual_report",
        )

        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertIsNone(table.payload["unit"])

    def test_responsible_organization_is_not_parsed_as_measurement_unit(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="text",
                    order_index=1,
                    text="负责单位：上海事业部",
                    structural_path=["项目情况"],
                ),
                PreparedElement(
                    kind="table",
                    order_index=2,
                    table={"headers": ["项目"], "rows": [["扩产"]]},
                    structural_path=["项目情况"],
                ),
            ]
        )

        table = s5_build_table_units(placed, BuildStats())[0]

        self.assertIsNone(table.payload["unit"])

    def test_quality_aggregation_never_upgrades_unusable_members(self) -> None:
        members = [
            UnitDraft(
                payload_kind="text",
                payload={"text": "损坏"},
                source_order=1,
                quality_status="unusable",
            ),
            UnitDraft(
                payload_kind="text",
                payload={"text": "正常正文"},
                source_order=2,
                quality_status="ok",
            ),
        ]
        self.assertEqual(_worst_quality(members), "unusable")

    def test_unit_declaration_merged_with_content_is_kept(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "单位：元\n下表列示了主要科目变动。",
                    },
                ]
            },
            filing_type="other",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload["text"], "单位：元\n下表列示了主要科目变动。")

    def test_single_line_header_combo_keeps_announce_no(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "证券代码：600519 证券简称：贵州茅台 公告编号：临 2026-027",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "分红实施公告正文。",
                    },
                ]
            },
            filing_type="other",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(
            units[0].payload["text"],
            "证券代码：600519 证券简称：贵州茅台 公告编号：临 2026-027\n"
            "分红实施公告正文。",
        )

    def test_text_units_carry_page_no_via_locator(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 3,
                        "ir_id": "x_ir_0001",
                        "artifact_locator": {"page_no": 3},
                        "text": "第三页的正文内容。",
                    },
                ]
            },
            filing_type="other",
        )

        self.assertEqual(len(units), 1)
        locator = units[0].artifact_locator or {}
        self.assertEqual(locator.get("page_no"), 3)

    def test_marker_line_never_becomes_heading(self) -> None:
        # MinerU sometimes tags the marker with text_level>=1 (kind=heading);
        # it must stay out of the heading tree and strip like normal text.
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 2,
                        "text": "二、非经常性损益",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 3,
                        "text": "□适用 √不适用",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "公司不存在其他符合非经常性损益定义的损益项目。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].heading_path, ["二、非经常性损益"])
        self.assertNotIn("适用", units[0].title or "")
        self.assertEqual(units[0].applicability, "not_applicable")
        self.assertIn("非经常性损益", units[0].payload["text"])

    def test_heading_trailing_applicability_marker_uses_clean_section_path(
        self,
    ) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "一、募集资金 √适用 □不适用",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 2,
                        "table": {
                            "headers": ["项目", "金额"],
                            "rows": [["募投", "100"]],
                        },
                    },
                ]
            },
            filing_type="annual_report",
        )

        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertEqual(table.heading_path, ["一、募集资金"])
        self.assertEqual(table.applicability, "applicable")
        self.assertEqual(stats.stripped_marker_lines, 1)

    def test_heading_trailing_not_applicable_marker_keeps_disclosure(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "二、对外担保 □适用 √不适用",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "公司不存在对外担保。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(units[0].heading_path, ["二、对外担保"])
        self.assertEqual(units[0].applicability, "not_applicable")

    def test_header_kv_lines_stripped_but_announce_no_kept(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "证券代码：600519\n证券简称：贵州茅台\n公告编号：临 2026-006",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "本公司董事会保证公告内容真实、准确、完整。",
                    },
                ]
            },
            filing_type="other",
        )

        # 同属空 heading 的两段被 S3 正常合并为一个 unit。
        self.assertEqual(len(units), 1)
        # 代码/简称是 document 元数据的重复；公告编号是独有信息，必须保留。
        self.assertEqual(
            units[0].payload["text"],
            "证券代码：600519\n证券简称：贵州茅台\n公告编号：临 2026-006\n"
            "本公司董事会保证公告内容真实、准确、完整。",
        )
        # 正文中出现的"被担保人证券代码"这类行不受影响。
        units2, stats2 = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "被担保人证券代码：600000，担保金额如下。",
                    },
                ]
            },
            filing_type="other",
        )
        self.assertIn("被担保人证券代码", units2[0].payload["text"])
        self.assertIsNone(
            rules.strip_header_kv_line("证券代码：000002 公司发生重大诉讼")
        )

        units3, stats3 = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 3,
                        "heading_level": 1,
                        "text": "二、交易标的",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "page_no": 3,
                        "text": "证券代码：123456\n该代码为交易标的证券代码。",
                    },
                ]
            },
            filing_type="other",
        )
        self.assertIn("证券代码：123456", str(units3[0].payload))

    def test_ambiguous_spaced_security_name_is_preserved(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "优先股代码：140002\n优先股简称：平银优 01",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "本行第十二届董事会审议通过了关联交易议案。",
                    },
                ]
            },
            filing_type="other",
        )

        self.assertEqual(len(units), 1)
        self.assertIn("优先股代码：140002", units[0].payload["text"])
        self.assertIn("优先股简称：平银优 01", units[0].payload["text"])

    def test_fragment_labels_are_preserved_as_source_evidence(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "营业收入：",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "2025年度",
                    },
                ]
            },
            filing_type="other",
        )

        payload = "\n".join(str(unit.payload) for unit in units)
        self.assertIn("营业收入：", payload)
        self.assertIn("2025年度", payload)

    def test_standalone_announcement_number_is_losslessly_deduplicated(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "公告编号：2023-026",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "一、交易概述",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "公告编号：2023-026\n公司拟与关联方发生交易，金额为人民币一亿元。",
                    },
                ]
            },
            filing_type="other",
        )

        titles = [unit.title for unit in units]
        self.assertNotIn("公告头信息", titles)
        self.assertEqual(stats.dropped_by_kind.get("standalone_noise", 0), 0)
        body = next(u for u in units if "交易概述" in (u.title or ""))
        self.assertIn("公告编号：2023-026", body.payload["text"])
        self.assertEqual(body.payload["text"].count("公告编号：2023-026"), 1)

    def test_long_preheading_content_still_anchored(self) -> None:
        # 31 个存量『公告头信息』unit 是真内容（首标题出现晚），必须继续锚定
        # 而不是被噪声规则误杀。
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "实现营业收入 4.80 亿元，同比增长 53.58%，毛利率 44.18%，"
                        "该板块毛利率下降主要受并表影响；医药板块实现营业收入 2.06 亿元。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "一、经营情况讨论",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "报告期内公司经营稳健。",
                    },
                ]
            },
            filing_type="other",
        )

        # 首标题前的真内容应作为独立 L1 证据原子保留。
        blob = " ".join(str(u.payload) for u in units)
        self.assertIn("营业收入", blob)
        self.assertNotIn(
            "公告头信息", blob + " ".join(str(u.heading_path) for u in units)
        )

    def test_attachment_caption_opens_top_level_scope(self) -> None:
        # 附件重置必须由唯一、完整的交易所投关表单结构证明；不能仅凭
        # caption 词面把普通叙事表移出其解析器小节。
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "document_id": "doc_form",
                        "ir_id": "ir_0000",
                        "source_item_index": 0,
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 0,
                        "table_caption": [],
                        "table_footnote": [],
                        "table": {
                            "headers": [],
                            "merged_cells": [],
                            "rows": [
                                ["投资者关系活动类别", "特定对象调研"],
                                ["参与单位名称及人员姓名", "见附件"],
                                ["时间", "2026-07-16"],
                                ["地点", "电话会议"],
                                ["上市公司接待人员姓名", "董事会秘书"],
                                [
                                    "投资者关系活动主要内容介绍",
                                    "一、经营情况\n经营保持稳健。",
                                ],
                            ],
                        },
                    },
                    {
                        "document_id": "doc_form",
                        "ir_id": "ir_0001",
                        "source_item_index": 1,
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "三、主要交流问题",
                    },
                    {
                        "document_id": "doc_form",
                        "ir_id": "ir_0002",
                        "source_item_index": 2,
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "问：公司下半年增长压力如何？答：经营保持稳健。",
                    },
                    {
                        "document_id": "doc_form",
                        "ir_id": "ir_0003",
                        "source_item_index": 3,
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "table_caption": ["附件 1：《参与机构名单》"],
                        "table": {
                            "headers": ["机构名称"],
                            "rows": [["某某基金"], ["某某证券"]],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司：投资者关系活动记录表",
        )

        paths = [tuple(unit.heading_path) for unit in units]
        self.assertIn(("附件 1：《参与机构名单》",), paths)
        for unit in units:
            if unit.heading_path and unit.heading_path[0] == "三、主要交流问题":
                self.assertNotIn("机构名称", str(unit.payload))

    def test_captioned_table_before_first_heading_anchors_to_caption(self) -> None:
        # round17：首标题前自带 caption 的表（投关记录表单头）锚到自身
        # 标题，而不是文档标题或「公告头信息」。
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 1,
                        "table_caption": [
                            "华测检测认证集团股份有限公司投资者关系活动记录表"
                        ],
                        "table": {
                            "headers": [],
                            "rows": [
                                ["投资者关系活动类别", "特定对象调研"],
                                ["时间", "2023年8月11日"],
                            ],
                        },
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "一、公司基本情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "公司主营检验检测服务。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="华测检测：投资者关系活动记录表",
        )

        form = next(u for u in units if "投资者关系活动类别" in str(u.payload))
        self.assertEqual(
            form.heading_path,
            ["华测检测认证集团股份有限公司投资者关系活动记录表"],
        )

    def test_qa_form_footer_table_keeps_parser_section_ownership(self) -> None:
        # 字段名看起来像表单尾部不足以证明它属于文档根。
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "三、主要交流问题",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "问：竞争格局如何？答：每个细分领域有不同的竞争者。",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "table": {
                            "headers": [],
                            "rows": [
                                ["附件清单（如有）", "《参会机构名单》"],
                                ["日期", "2023-08-11~2023-08-17"],
                            ],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="华测检测：投资者关系活动记录表",
        )

        footer = next(u for u in units if "附件清单" in str(u.payload))
        self.assertEqual(footer.heading_path, ["三、主要交流问题"])
        self.assertEqual(footer.title, "三、主要交流问题")
        # 叙事小节里的业务表格不受影响——首列不是模板尾字段。
        units2, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "二、经营数据",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 2,
                        "table": {
                            "headers": ["日期", "营业收入"],
                            "rows": [["2023-06-30", "4.8亿元"]],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="华测检测：投资者关系活动记录表",
        )
        data_table = next(u for u in units2 if "营业收入" in str(u.payload))
        self.assertEqual(data_table.heading_path[0], "二、经营数据")
        # 「日期安排」类业务标签是前缀命中而非整格命中，不得触发重锚
        # （复审 Major#2：尾字段词表整格精确匹配）。
        units3, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "二、回购安排",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 2,
                        "table": {
                            "headers": [],
                            "rows": [
                                ["日期安排", "2026年7月至12月"],
                                ["日期变更", "无"],
                            ],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司：投资者关系活动记录表",
        )
        biz_table = next(u for u in units3 if "日期变更" in str(u.payload))
        self.assertEqual(biz_table.heading_path[0], "二、回购安排")

    def test_attachment_caption_ignored_outside_qa_mode(self) -> None:
        # 复审 Major#1：附件栈重置仅限表单模式（语料 11 例全部投关）。
        # 叙事文档的文中附件若重置栈，其后的正文标题会错挂进附件分支。
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "一、审议事项",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 2,
                        "table_caption": ["附件1：《股东名单》"],
                        "table": {
                            "headers": ["股东名称"],
                            "rows": [["某某投资"]],
                        },
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "二、表决结果",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "议案获得通过。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        vote = next(u for u in units if "议案获得通过" in str(u.payload))
        self.assertEqual(vote.heading_path[0], "二、表决结果")
        roster = next(u for u in units if "股东名称" in str(u.payload))
        self.assertEqual(roster.heading_path[0], "一、审议事项")

    def test_applicability_marker_becomes_payload_flag(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第五节 重要事项",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "一、破产重整相关事项",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "□适用 √不适用",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 4,
                        "heading_level": 2,
                        "text": "二、重大诉讼事项",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 5,
                        "text": "√适用 □不适用\n公司报告期内存在如下诉讼。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual([unit.payload_kind for unit in units], ["text", "text"])
        bankruptcy, litigation = units
        self.assertEqual(bankruptcy.applicability, "not_applicable")
        self.assertIn("不适用", bankruptcy.payload["text"])
        self.assertEqual(litigation.applicability, "applicable")
        # The leading marker line is stripped; the prose remains.
        self.assertEqual(litigation.payload["text"], "公司报告期内存在如下诉讼。")

    def test_dangling_applicable_marker_sinks_onto_following_table(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 2,
                        "text": "4、研发投入",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "√适用 □不适用",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "table": {"headers": ["项目"], "rows": [["研发投入金额"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        # The bare marker must not survive as its own unit (user decision).
        self.assertEqual([unit.payload_kind for unit in units], ["table"])
        self.assertEqual(units[0].applicability, "applicable")
        self.assertEqual(stats.stripped_marker_lines, 1)
        self.assertEqual(
            (units[0].artifact_locator or {})["applicability_source_locator"][
                "order_index"
            ],
            2,
        )

    def test_applicability_is_parsed_per_source_carrier_before_coalescing(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "重大事项",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "适用 □不适用\n第一项说明。",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "适用 □不适用\n第二项说明。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(
            [unit.payload["text"] for unit in units],
            ["第一项说明。", "第二项说明。"],
        )
        self.assertTrue(all(unit.applicability == "applicable" for unit in units))
        self.assertTrue(all("适用" not in unit.payload["text"] for unit in units))

    def test_dangling_applicable_marker_sinks_onto_following_text(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 2,
                        "text": "五、重大合同",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "√适用 □不适用",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "公司与某客户签署了重大销售合同。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        # Text followers receive the flag exactly like tables do.
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload_kind, "text")
        self.assertEqual(units[0].applicability, "applicable")
        self.assertEqual(units[0].payload["text"], "公司与某客户签署了重大销售合同。")
        self.assertEqual(
            (units[0].artifact_locator or {})["applicability_source_locator"][
                "order_index"
            ],
            2,
        )

    def test_dangling_applicable_marker_preserves_following_image_payload(self) -> None:
        digest = "a" * 64
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 2,
                        "text": "六、股权结构",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "√适用 □不适用",
                    },
                    {
                        "kind": "image",
                        "raw_kind": "image",
                        "order_index": 3,
                        "image_path": f"images/{digest}.jpg",
                        "caption": "股权结构图",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload["image_ref"], f"images/{digest}.jpg")
        self.assertEqual(units[0].applicability, "applicable")
        self.assertNotIn("text", units[0].payload)
        self.assertEqual(
            (units[0].artifact_locator or {})["applicability_source_locator"][
                "order_index"
            ],
            2,
        )

    def test_dangling_applicable_marker_sinks_into_child_section(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 2,
                        "text": "十六、募集资金使用情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "√适用 □不适用",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 3,
                        "text": "（一） 募集资金总体使用情况",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 4,
                        "table": {"headers": ["项目"], "rows": [["募集总额"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        # The follower opens a CHILD heading — still this section's content.
        self.assertEqual([unit.payload_kind for unit in units], ["table"])
        self.assertEqual(units[0].applicability, "applicable")

    def test_dangling_applicable_without_sibling_keeps_declaration(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 2,
                        "text": "一、孤例小节",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "√适用 □不适用",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload["text"], "√适用 □不适用")
        self.assertEqual(units[0].applicability, "applicable")

    def test_label_then_marker_composite_is_flagged_but_untouched(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "主要客户其他情况说明\n□适用 √不适用",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].applicability, "not_applicable")
        self.assertIn("主要客户其他情况说明", units[0].payload["text"])
        self.assertIn("不适用", units[0].payload["text"])

    def test_full_s1_s7_redline_important_tip(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "page_furniture",
                        "raw_kind": "header",
                        "order_index": 1,
                        "text": "重要提示",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "重要提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "公司存在退市风险，请投资者注意。",
                    },
                ]
            },
            filing_type="other",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].title, "重要提示")
        self.assertIn("退市风险", units[0].payload["text"])
        self.assertEqual(stats.dropped_by_kind["page_furniture_exact_duplicate"], 1)
        heading_sources = (units[0].artifact_locator or {})[
            "heading_source_locators"
        ]
        self.assertEqual(heading_sources[0]["heading_text"], "重要提示")
        self.assertEqual(len(heading_sources[0]["source_locators"]), 2)

    def test_full_s1_s7_redline_important_and_risk_tip_positive(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "重要提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "公司存在退市风险，请投资者注意。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "风险提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "原材料价格波动可能影响公司业绩。",
                    },
                ]
            },
            filing_type="other",
        )

        self.assertEqual(
            [unit.heading_path for unit in units],
            [["重要提示"], ["风险提示"]],
        )
        self.assertIn("退市风险", units[0].payload["text"])
        self.assertIn("原材料价格波动", units[1].payload["text"])

    def test_full_s1_s7_redline_important_tip_repeated_header_negative(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "page_furniture",
                        "raw_kind": "header",
                        "order_index": 1,
                        "text": "重要提示",
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "header",
                        "order_index": 2,
                        "text": "风险提示",
                    },
                ]
            },
            filing_type="other",
        )

        self.assertEqual(len(units), 1)
        self.assertIn("重要提示", units[0].payload["text"])
        self.assertIn("风险提示", units[0].payload["text"])
        self.assertEqual(units[0].quality_status, "needs_review")
        self.assertEqual(stats.dropped_by_kind["page_furniture"], 0)


if __name__ == "__main__":
    unittest.main()
