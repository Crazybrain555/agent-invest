"""Unit builder rule and S1-S7 stage tests."""

from __future__ import annotations

import copy
import unittest

from disclosure_anchor.adapters.unit_builder import rules
from disclosure_anchor.adapters.unit_builder.builder import (
    BuildStats,
    PreparedElement,
    UnitDraft,
    _main_text,
    _merge_announcement_number_carriers,
    _proposal_anchor,
    _recover_qa_across_logical_carrier_runs,
    _unit_part,
    build_unit_drafts_s1_s7,
    replace_text_units_with_qa_where_stable,
    s1_preprocess_elements,
    s2_apply_heading_tree,
    s3_build_text_units,
    s4_build_qa_units,
    s5_build_table_units,
    s6_filter_units,
    s7_finalize_units,
    s8_group_semantic_units,
    semantic_key_for_unit,
    semantic_keys_for_unit,
)


class UnitBuilderTests(unittest.TestCase):
    def test_rules_version_and_fixed_tables(self) -> None:
        self.assertEqual(rules.RULES_VERSION, "ub-2026.07-52")
        self.assertEqual(
            rules.TABLE_BUILDER_SEMANTICS_VERSION,
            "table-builder-semantics.v2",
        )
        self.assertEqual(rules.HEADING_RULESET_ID, "cn_a_v6")
        self.assertEqual(
            rules.SKIP_SECTION_TITLES, {"释义", "目录", "备查文件", "备查文件目录"}
        )
        self.assertEqual(rules.GIBBERISH_RATIO_MAX, 0.30)
        self.assertEqual(rules.NOTE_KEY_MAP_VERSION, "2026-07-r16")
        self.assertEqual(rules.NOTE_KEY_MAP_KEY_COUNT, 173)
        self.assertEqual(len(rules._note_key_tables()[0]), 389)
        self.assertEqual(rules.EVENT_KEY_MAP_VERSION, "2026-07-r2")
        self.assertEqual(rules.EVENT_KEY_MAP_EVENT_COUNT, 35)
        self.assertEqual(
            sum(len(patterns) for _, patterns in rules._event_key_table()),
            109,
        )

    def test_s1_drops_furniture_and_separator_but_records_stats(self) -> None:
        result = s1_preprocess_elements(
            [
                {"kind": "page_furniture", "raw_kind": "header", "order_index": 1},
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 2,
                    "text": "---\n正文\u0001",
                },
                {"kind": "unknown", "raw_kind": "mystery", "order_index": 3},
            ]
        )

        self.assertEqual([item.text for item in result.elements], ["正文"])
        self.assertEqual(result.stats.dropped_by_kind["page_furniture"], 1)
        self.assertEqual(result.stats.dropped_by_kind["unknown"], 1)
        self.assertEqual(result.stats.dropped_unknown_by_raw_kind["mystery"], 1)

    def test_s1_recovers_exact_statement_furniture_above_same_page_table(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "table",
                    "raw_kind": "table",
                    "order_index": 1,
                    "page_no": 206,
                    "bbox": [40, 100, 550, 760],
                    "table_caption": ["2024年12月31日"],
                    "table": {"headers": ["项目"], "rows": [["资产"]]},
                },
                {
                    "kind": "page_furniture",
                    "raw_kind": "header",
                    "order_index": 2,
                    "page_no": 206,
                    "bbox": [120, 45, 430, 80],
                    "text": "合并及公司资产负债表",
                },
            ]
        )

        table = result.elements[0]
        self.assertEqual(
            table.table_caption,
            ["合并及公司资产负债表", "2024年12月31日"],
        )
        self.assertEqual(result.stats.recovered_statement_captions, 1)
        self.assertEqual(result.stats.dropped_by_kind["page_furniture"], 1)

    def test_s1_does_not_guess_ambiguous_or_nonexact_statement_furniture(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "page_furniture",
                    "order_index": 1,
                    "page_no": 1,
                    "bbox": [20, 20, 300, 40],
                    "text": "合并资产负债表",
                },
                {
                    "kind": "page_furniture",
                    "order_index": 2,
                    "page_no": 1,
                    "bbox": [20, 45, 400, 65],
                    "text": "注册会计师对财务报表审计的责任",
                },
                {
                    "kind": "page_furniture",
                    "order_index": 3,
                    "page_no": 1,
                    "bbox": [20, 70, 300, 90],
                    "text": "公司利润表",
                },
                {
                    "kind": "table",
                    "order_index": 4,
                    "page_no": 1,
                    "bbox": [20, 100, 500, 700],
                    "table": {"headers": ["项目"], "rows": [["金额"]]},
                },
            ]
        )

        table = next(item for item in result.elements if item.kind == "table")
        self.assertEqual(table.table_caption, [])
        self.assertEqual(result.stats.recovered_statement_captions, 0)

    def test_s1_recovers_proven_cross_page_income_statement_orphan_row(self) -> None:
        elements = [
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 1,
                "page_no": 54,
                "heading_level": 1,
                "text": "第八节 财务报告",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 2,
                "page_no": 54,
                "heading_level": 2,
                "text": "二、财务报表",
            },
            {
                "kind": "table",
                "raw_kind": "table",
                "order_index": 3,
                "page_no": 55,
                "bbox": [137, 87, 902, 902],
                "table_caption": ["母公司利润表"],
                "table": {
                    "headers": [],
                    "rows": [
                        ["六、综合收益总额", "", "16", "23"],
                        ["七、每股收益:", "", "", ""],
                        ["(一)基本每股收益(元/股)", "", "", ""],
                    ],
                },
            },
            {
                "kind": "page_furniture",
                "raw_kind": "header",
                "order_index": 4,
                "page_no": 55,
                "text": "某公司2025年半年度报告",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 5,
                "page_no": 56,
                "heading_level": 1,
                "bbox": [189, 90, 396, 105],
                "text": "（二）稀释每股收益(元/股)",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 6,
                "page_no": 56,
                "bbox": [147, 121, 310, 137],
                "text": "公司负责人：甲",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 7,
                "page_no": 56,
                "bbox": [386, 121, 618, 137],
                "text": "主管会计工作负责人：乙",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 8,
                "page_no": 56,
                "bbox": [677, 121, 875, 137],
                "text": "会计机构负责人：丙",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 9,
                "page_no": 56,
                "bbox": [455, 170, 584, 185],
                "text": "合并现金流量表",
            },
            {
                "kind": "table",
                "raw_kind": "table",
                "order_index": 10,
                "page_no": 56,
                "bbox": [137, 219, 905, 904],
                "table": {
                    "headers": [],
                    "rows": [["一、经营活动产生的现金流量", "", "84", "-8"]],
                },
            },
        ]

        s1 = s1_preprocess_elements(elements)
        self.assertEqual(s1.stats.recovered_statement_orphan_rows, 1)
        recovered = next(
            item
            for item in s1.elements
            if item.raw_kind == "recovered_statement_orphan_row"
        )
        self.assertEqual(recovered.kind, "table")
        self.assertEqual(
            recovered.table["rows"],
            [["（二）稀释每股收益(元/股)", "", "", ""]],
        )

        units, stats = build_unit_drafts_s1_s7(
            {"elements": elements}, filing_type="semiannual_report"
        )
        self.assertEqual(stats.recovered_statement_orphan_rows, 1)
        profit = next(unit for unit in units if unit.title == "母公司利润表")
        self.assertIn("（二）稀释每股收益(元/股)", str(profit.payload))
        self.assertFalse(
            any(
                "稀释每股收益" in (unit.title or "")
                or any("稀释每股收益" in part for part in unit.heading_path)
                for unit in units
            )
        )
        cash = next(unit for unit in units if unit.title == "合并现金流量表")
        self.assertNotIn("母公司利润表", cash.heading_path)

        missing_boundary = s1_preprocess_elements(elements[:-2])
        self.assertEqual(missing_boundary.stats.recovered_statement_orphan_rows, 0)
        self.assertTrue(
            any(
                item.kind == "heading" and item.text == "（二）稀释每股收益(元/股)"
                for item in missing_boundary.elements
            )
        )

    def test_s1_recovers_mixed_heading_parameter_value_list_as_text(self) -> None:
        elements = [
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 1,
                "page_no": 10,
                "heading_level": 2,
                "text": "（一）第二类限制性股票的公允价值计算方法",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 2,
                "page_no": 10,
                "text": "公司采用期权定价模型确定授予日公允价值。",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 3,
                "page_no": 10,
                "heading_level": 1,
                "text": "1、标的股价：222.94元/股",
            },
            {
                "kind": "page_furniture",
                "raw_kind": "page_number",
                "order_index": 4,
                "page_no": 10,
                "text": "10",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 5,
                "page_no": 11,
                "text": "2、有效期分别为：1年、2年和3年",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 6,
                "page_no": 11,
                "text": "3、历史波动率：23.04%",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 7,
                "page_no": 11,
                "text": "4、无风险利率：1.50%",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 8,
                "page_no": 11,
                "text": "5、股息率：0.00%",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 9,
                "page_no": 11,
                "heading_level": 2,
                "text": "（二）预计费用影响",
            },
        ]

        s1 = s1_preprocess_elements(elements)
        recovered = [
            item
            for item in s1.elements
            if item.raw_kind == "recovered_parameter_list_item"
        ]
        self.assertEqual(s1.stats.recovered_parameter_list_items, 5)
        self.assertEqual(len(recovered), 5)
        self.assertTrue(all(item.kind == "text" for item in recovered))
        self.assertTrue(all(item.heading_level is None for item in recovered))

        units, stats = build_unit_drafts_s1_s7(
            {"elements": elements}, filing_type="annual_report"
        )
        self.assertEqual(stats.recovered_parameter_list_items, 5)
        parameter_unit = next(
            unit
            for unit in units
            if unit.title == "（一）第二类限制性股票的公允价值计算方法"
        )
        for ordinal in range(1, 6):
            self.assertIn(f"{ordinal}、", str(parameter_unit.payload))
        self.assertFalse(
            any(
                unit.title and unit.title.startswith(tuple(f"{n}、" for n in range(1, 6)))
                for unit in units
            )
        )

    def test_s1_does_not_flatten_numbered_business_headings(self) -> None:
        elements = [
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": index,
                "page_no": 1,
                "heading_level": 2,
                "text": text,
            }
            for index, text in enumerate(
                [
                    "1、业务目标：稳步增长",
                    "2、经营计划：提质增效",
                    "3、风险提示：加强管控",
                ],
                start=1,
            )
        ]

        s1 = s1_preprocess_elements(elements)
        self.assertEqual(s1.stats.recovered_parameter_list_items, 0)
        self.assertTrue(all(item.kind == "heading" for item in s1.elements))
        self.assertFalse(
            any(
                item.raw_kind == "recovered_parameter_list_item"
                for item in s1.elements
            )
        )

    def test_s1_image_shell_requires_context_or_caption(self) -> None:
        digest = "a" * 64
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "page_no": 5,
                    "text": "股权结构图",
                    "heading_level": 2,
                },
                {
                    "kind": "image",
                    "raw_kind": "image",
                    "order_index": 2,
                    "page_no": 5,
                    "image_path": f"images/{digest}.jpg",
                },
                {
                    "kind": "image",
                    "raw_kind": "image",
                    "order_index": 3,
                    "page_no": 6,
                    "image_path": f"images/{'b' * 64}.jpg",
                },
            ]
        )

        image_units = [item for item in result.elements if item.payload]
        self.assertEqual(len(image_units), 1)
        self.assertEqual(image_units[0].payload["image_ref"], f"images/{digest}.jpg")
        self.assertEqual(image_units[0].payload["context"], "股权结构图")
        self.assertEqual(image_units[0].quality_status, "needs_review")
        self.assertEqual(result.stats.dropped_by_kind["image"], 1)

    def test_s1_infers_repeated_top_heading_as_page_furniture(self) -> None:
        repeated = [
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": page,
                "page_no": page,
                "heading_level": 1,
                "bbox": [431, 159 if page < 4 else 174, 554, 175 if page < 4 else 190],
                "text": "审计报告（续）",
            }
            for page in range(1, 4)
        ]
        result = s1_preprocess_elements(
            [
                *repeated,
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 4,
                    "page_no": 4,
                    "heading_level": 1,
                    "bbox": [100, 420, 300, 440],
                    "text": "审计报告（续）",
                },
                {
                    "kind": "text",
                    "raw_kind": "text",
                    "order_index": 5,
                    "page_no": 4,
                    "bbox": [100, 470, 700, 500],
                    "text": "这是正文中的同名内容。",
                },
            ]
        )

        self.assertEqual(result.stats.inferred_page_furniture, 3)
        self.assertEqual(result.stats.dropped_by_kind["page_furniture"], 3)
        self.assertEqual(
            [element.text for element in result.elements],
            ["审计报告（续）", "这是正文中的同名内容。"],
        )

    def test_s1_declared_furniture_corroborates_different_top_band(self) -> None:
        result = s1_preprocess_elements(
            [
                *[
                    {
                        "kind": "page_furniture",
                        "order_index": page,
                        "page_no": page,
                        "bbox": [80, 42, 520, 62],
                        "text": "四、财务报表主要项目注释（续）",
                    }
                    for page in (1, 2)
                ],
                *[
                    {
                        "kind": "heading",
                        "order_index": page + 10,
                        "page_no": page,
                        "heading_level": 1,
                        "bbox": [80, 142, 520, 166],
                        "text": "四、财务报表主要项目注释（续）",
                    }
                    for page in (3, 4)
                ],
                {
                    "kind": "heading",
                    "order_index": 20,
                    "page_no": 4,
                    "heading_level": 1,
                    "bbox": [80, 420, 520, 444],
                    "text": "四、财务报表主要项目注释（续）",
                },
            ]
        )

        self.assertEqual(result.stats.inferred_page_furniture, 2)
        self.assertEqual(
            [element.text for element in result.elements],
            ["四、财务报表主要项目注释（续）"],
        )

    def test_s1_splits_applicability_marker_glued_to_heading(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 1,
                    "page_no": 1,
                    "heading_level": 2,
                    "text": "(3). 财务报表 √适用 □不适用",
                }
            ]
        )

        self.assertEqual(
            [(element.kind, element.text) for element in result.elements],
            [("heading", "(3). 财务报表"), ("text", "√适用 □不适用")],
        )

    def test_s1_keeps_repeated_top_template_leaf_without_furniture_evidence(
        self,
    ) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": page,
                    "page_no": page,
                    "heading_level": 1,
                    "bbox": [120, 84, 430, 102],
                    "text": "1）预计未来现金流量的现值",
                }
                for page in range(1, 5)
            ]
        )

        self.assertEqual(result.stats.inferred_page_furniture, 0)
        self.assertEqual(len(result.elements), 4)

    def test_s1_drops_repeated_issuer_header_but_keeps_first_and_body_copy(
        self,
    ) -> None:
        result = s1_preprocess_elements(
            [
                *[
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": page,
                        "page_no": page,
                        "heading_level": 1,
                        "bbox": [120, 84, 430, 102],
                        "text": "示例股份有限公司",
                    }
                    for page in range(1, 4)
                ],
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": 4,
                    "page_no": 4,
                    "heading_level": 1,
                    "bbox": [120, 420, 430, 442],
                    "text": "示例股份有限公司",
                },
            ]
        )

        self.assertEqual(result.stats.inferred_page_furniture, 2)
        self.assertEqual(
            [element.text for element in result.elements],
            ["示例股份有限公司", "示例股份有限公司"],
        )

    def test_s1_drops_repeated_financial_report_running_header(self) -> None:
        result = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "raw_kind": "text",
                    "order_index": page,
                    "page_no": page,
                    "heading_level": 1,
                    "bbox": [120, 84, 430, 102],
                    "text": "财务报告",
                }
                for page in range(1, 4)
            ]
        )

        self.assertEqual(result.stats.inferred_page_furniture, 2)
        self.assertEqual([element.text for element in result.elements], ["财务报告"])

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

    def test_s2_qa_heading_mode_demotes_numbered_headings(self) -> None:
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
        self.assertEqual(parsed[0].payload_kind, "qa")
        self.assertEqual(parsed[0].payload["question"], "请公司讲一下，2025年重点工作")

    def test_s2_qa_mode_demotes_split_numbered_text_question(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="投资者关系活动记录表",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="text",
                    order_index=2,
                    text="2、近年来竞争激烈，请问公司在维持竞争力方",
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="面有哪些核心优势和创新策略？",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=4, text="答：持续创新。"),
            ],
            qa_heading_mode=True,
        )

        parsed = replace_text_units_with_qa_where_stable(s3_build_text_units(placed))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].payload_kind, "qa")
        self.assertEqual(
            parsed[0].payload["question"],
            "近年来竞争激烈，请问公司在维持竞争力方面有哪些核心优势和创新策略？",
        )

    def test_s2_qa_mode_merges_consecutive_heading_question_fragments(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="4、请问收购后经营业绩改善了",
                    heading_level=2,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="吗？获取订单的能力改善没有？",
                    heading_level=2,
                ),
                PreparedElement(
                    kind="text", order_index=3, text="尊敬的投资者，经营稳步改善。"
                ),
            ],
            qa_heading_mode=True,
        )

        self.assertEqual(len(placed), 2)
        self.assertTrue(placed[0].qa_question_boundary)
        parsed = replace_text_units_with_qa_where_stable(s3_build_text_units(placed))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].payload_kind, "qa")
        self.assertEqual(
            parsed[0].payload["question"],
            "请问收购后经营业绩改善了吗？获取订单的能力改善没有？",
        )

    def test_s2_qa_period_ended_numbered_heading_keeps_question_and_answer(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "text": "三、主要交流问题",
                        "heading_level": 1,
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "text": "4. 请介绍一下信用卡业务的整体经营情况。",
                        "heading_level": 1,
                    },
                    {
                        "kind": "text",
                        "order_index": 3,
                        "text": "信用卡业务保持稳健增长。",
                    },
                    {
                        "kind": "text",
                        "order_index": 4,
                        "text": "资产质量总体稳定。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某银行投资者关系活动记录表",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload_kind, "qa")
        self.assertEqual(
            units[0].payload["question"], "请介绍一下信用卡业务的整体经营情况。"
        )
        self.assertEqual(
            units[0].payload["answer"],
            "信用卡业务保持稳健增长。\n资产质量总体稳定。",
        )

    def test_s2_repeated_notes_banner_cannot_hide_next_major_section(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第十节 财务报告",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "2025 年上半年度财务报表附注",
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "三 税项",
                    },
                    {
                        "kind": "heading",
                        "order_index": 4,
                        "heading_level": 1,
                        "text": "(1) 主要税种及税率",
                    },
                    {
                        "kind": "heading",
                        "order_index": 5,
                        "heading_level": 1,
                        "text": "某某股份有限公司",
                    },
                    {
                        "kind": "heading",
                        "order_index": 6,
                        "heading_level": 1,
                        "text": "2025 年上半年度财务报表附注",
                    },
                    {
                        "kind": "heading",
                        "order_index": 7,
                        "heading_level": 1,
                        "text": "四 合并财务报表项目附注",
                    },
                    {
                        "kind": "heading",
                        "order_index": 8,
                        "heading_level": 1,
                        "text": "(1) 货币资金",
                    },
                    {
                        "kind": "table",
                        "order_index": 9,
                        "table": {"headers": ["项目"], "rows": [["银行存款"]]},
                    },
                ]
            },
            filing_type="semiannual_report",
        )

        cash = next(unit for unit in units if unit.title == "(1) 货币资金")
        self.assertEqual(
            cash.heading_path,
            [
                "第十节 财务报告",
                "2025 年上半年度财务报表附注",
                "四 合并财务报表项目附注",
                "(1) 货币资金",
            ],
        )
        self.assertNotIn("tax_items", cash.semantic_keys)

    def test_glued_number_exact_controlled_note_reopens_sibling(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "财务报表附注",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "22 其他非流动资产",
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "(1) 资产明细",
                    },
                    {
                        "kind": "table",
                        "order_index": 4,
                        "table": {"headers": ["项目"], "rows": [["旧资产"]]},
                    },
                    {
                        "kind": "heading",
                        "order_index": 5,
                        "heading_level": 1,
                        "text": "23短期借款",
                    },
                    {
                        "kind": "heading",
                        "order_index": 6,
                        "heading_level": 1,
                        "text": "(1) 短期借款分类",
                    },
                    {
                        "kind": "table",
                        "order_index": 7,
                        "table": {"headers": ["项目"], "rows": [["信用借款"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        borrowing = next(unit for unit in units if "信用借款" in str(unit.payload))
        self.assertIn("23短期借款", borrowing.heading_path)
        self.assertNotIn("22 其他非流动资产", borrowing.heading_path)
        self.assertIn("short_term_borrowings", borrowing.semantic_keys or [])
        self.assertNotIn("other_noncurrent_assets", borrowing.semantic_keys or [])

    def test_s1_recovers_geometry_split_statutory_note_headings(self) -> None:
        elements = [
            {
                "kind": "heading",
                "order_index": 1,
                "heading_level": 1,
                "text": "公司基本情况",
                "page_no": 1,
                "bbox": [55, 50, 240, 70],
            },
            {
                "kind": "heading",
                "order_index": 2,
                "heading_level": 1,
                "text": "五 合并财务报表项目附注",
                "page_no": 1,
                "bbox": [57, 90, 300, 110],
            },
            {
                "kind": "heading",
                "order_index": 3,
                "heading_level": 2,
                "text": "31 其他流动负债",
                "page_no": 1,
                "bbox": [57, 130, 250, 150],
            },
            {
                "kind": "table",
                "order_index": 4,
                "page_no": 1,
                "bbox": [57, 160, 850, 300],
                "table": {"headers": ["项目"], "rows": [["待转销项税"]]},
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 5,
                "heading_level": 2,
                "text": "长期借款",
                "page_no": 2,
                "bbox": [129, 98, 208, 114],
            },
            {
                "kind": "page_furniture",
                "raw_kind": "page_number",
                "order_index": 6,
                "text": "32",
                "page_no": 2,
                "bbox": [58, 98, 84, 112],
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 7,
                "heading_level": 2,
                "text": "长期借款分类",
                "page_no": 2,
                "bbox": [129, 134, 243, 151],
            },
            {
                "kind": "table",
                "order_index": 8,
                "page_no": 2,
                "bbox": [57, 170, 850, 500],
                "table": {"headers": ["种类"], "rows": [["银行借款"]]},
            },
            {
                "kind": "page_furniture",
                "raw_kind": "header",
                "order_index": 9,
                "text": "应付债券",
                "page_no": 3,
                "bbox": [91, 137, 147, 159],
            },
            {
                "kind": "page_furniture",
                "raw_kind": "page_number",
                "order_index": 10,
                "text": "33",
                "page_no": 3,
                "bbox": [40, 139, 59, 156],
            },
            {
                "kind": "table",
                "order_index": 11,
                "page_no": 3,
                "bbox": [40, 180, 850, 500],
                "table": {"headers": ["债券"], "rows": [["中期票据"]]},
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 12,
                "heading_level": 2,
                "text": "预计负债",
                "page_no": 4,
                "bbox": [129, 500, 208, 519],
            },
            {
                "kind": "unknown",
                "raw_kind": "aside_text",
                "order_index": 13,
                "text": "34",
                "page_no": 4,
                "bbox": [58, 501, 82, 517],
            },
            {
                "kind": "table",
                "order_index": 14,
                "page_no": 4,
                "bbox": [57, 530, 850, 650],
                "table": {"headers": ["项目"], "rows": [["补偿准备"]]},
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 15,
                "heading_level": 2,
                "text": "资本公积",
                "page_no": 5,
                "bbox": [129, 606, 208, 624],
            },
            {
                "kind": "unknown",
                "raw_kind": "aside_text",
                "order_index": 16,
                "text": "38",
                "page_no": 5,
                "bbox": [58, 607, 82, 623],
            },
            {
                "kind": "table",
                "order_index": 17,
                "page_no": 5,
                "bbox": [57, 640, 850, 800],
                "table": {"headers": ["项目"], "rows": [["股本溢价"]]},
            },
            {
                "kind": "page_furniture",
                "raw_kind": "page_number",
                "order_index": 18,
                "text": "152",
                "page_no": 5,
                "bbox": [500, 954, 526, 967],
            },
        ]

        s1 = s1_preprocess_elements(elements)
        self.assertEqual(s1.stats.recovered_split_note_headings, 4)
        self.assertEqual(
            [item.text for item in s1.elements if item.kind == "heading"],
            [
                "公司基本情况",
                "五 合并财务报表项目附注",
                "31 其他流动负债",
                "32 长期借款",
                "长期借款分类",
                "33 应付债券",
                "34 预计负债",
                "38 资本公积",
            ],
        )

        units, _ = build_unit_drafts_s1_s7(
            {"elements": elements},
            filing_type="semiannual_report",
        )

        provisions = next(unit for unit in units if unit.title == "34 预计负债")
        capital = next(unit for unit in units if unit.title == "38 资本公积")
        self.assertEqual(provisions.heading_path[-1], "34 预计负债")
        self.assertEqual(capital.heading_path[-1], "38 资本公积")
        self.assertNotIn("other_current_liabilities", provisions.semantic_keys)
        self.assertNotIn("long_term_borrowings", provisions.semantic_keys)
        self.assertNotIn("other_current_liabilities", capital.semantic_keys)

    def test_s1_recovers_late_serialized_revenue_and_cost_note_number(self) -> None:
        elements = [
            {
                "kind": "heading",
                "order_index": 1,
                "heading_level": 1,
                "text": "五 合并财务报表项目附注",
                "page_no": 1,
                "bbox": [57, 96, 317, 112],
            },
            {
                "kind": "heading",
                "order_index": 2,
                "heading_level": 2,
                "text": "41 未分配利润",
                "page_no": 1,
                "bbox": [57, 550, 226, 568],
            },
            {
                "kind": "table",
                "order_index": 3,
                "page_no": 1,
                "bbox": [131, 583, 907, 771],
                "table": {"headers": ["项目"], "rows": [["期末未分配利润"]]},
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 4,
                "heading_level": 2,
                "text": "营业收入及成本",
                "page_no": 2,
                "bbox": [129, 96, 263, 112],
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 5,
                "heading_level": 2,
                "text": "(1) 营业收入及成本",
                "page_no": 2,
                "bbox": [57, 133, 263, 151],
            },
            {
                "kind": "table",
                "order_index": 6,
                "page_no": 2,
                "bbox": [129, 166, 905, 262],
                "table": {"headers": ["收入", "成本"], "rows": [["10", "8"]]},
            },
            {
                # MinerU serialized this left-margin glyph after the page's
                # business carriers; visual order must still recover note 42.
                "kind": "page_furniture",
                "raw_kind": "page_number",
                "order_index": 7,
                "text": "42",
                "page_no": 2,
                "bbox": [57, 97, 84, 111],
            },
        ]

        s1 = s1_preprocess_elements(elements)
        self.assertEqual(s1.stats.recovered_split_note_headings, 1)
        self.assertIn("42 营业收入及成本", [item.text for item in s1.elements])

        units, _ = build_unit_drafts_s1_s7(
            {"elements": elements}, filing_type="semiannual_report"
        )
        revenue = next(unit for unit in units if unit.title == "(1) 营业收入及成本")
        self.assertIn("42 营业收入及成本", revenue.heading_path)
        self.assertNotIn("41 未分配利润", revenue.heading_path)
        self.assertIn("revenue_and_cost", revenue.semantic_keys)

    def test_s1_recovers_late_borrowing_cost_number_and_closes_stale_child(
        self,
    ) -> None:
        elements = [
            {
                "kind": "heading",
                "order_index": 1,
                "heading_level": 1,
                "text": "三 公司重要会计政策、会计估计",
                "page_no": 84,
                "bbox": [57, 96, 317, 112],
            },
            {
                "kind": "heading",
                "order_index": 2,
                "heading_level": 2,
                "text": "14 生物资产",
                "page_no": 84,
                "bbox": [57, 140, 208, 158],
            },
            {
                "kind": "heading",
                "order_index": 3,
                "heading_level": 2,
                "text": "(2) 生产性生物资产",
                "page_no": 84,
                "bbox": [57, 180, 263, 198],
            },
            {
                "kind": "text",
                "order_index": 4,
                "text": "生产性生物资产正文。",
                "page_no": 84,
                "bbox": [126, 220, 897, 260],
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 5,
                "heading_level": 2,
                "text": "借款费用",
                "page_no": 85,
                "bbox": [129, 96, 208, 112],
            },
            {
                "kind": "text",
                "order_index": 6,
                "text": "借款费用正文。",
                "page_no": 85,
                "bbox": [126, 134, 897, 174],
            },
            {
                # Real MinerU shape: the left-margin heading number is emitted
                # after the page's business content as raw_kind=page_number.
                "kind": "page_furniture",
                "raw_kind": "page_number",
                "order_index": 7,
                "text": "15",
                "page_no": 85,
                "bbox": [58, 97, 84, 111],
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 8,
                "heading_level": 2,
                "text": "无形资产",
                "page_no": 86,
                "bbox": [129, 96, 208, 112],
            },
            {
                "kind": "text",
                "order_index": 9,
                "text": "无形资产正文。",
                "page_no": 86,
                "bbox": [126, 134, 897, 174],
            },
            {
                "kind": "page_furniture",
                "raw_kind": "page_number",
                "order_index": 10,
                "text": "16",
                "page_no": 86,
                "bbox": [58, 97, 84, 111],
            },
        ]

        s1 = s1_preprocess_elements(elements)
        self.assertEqual(s1.stats.recovered_split_note_headings, 2)
        self.assertIn("15 借款费用", [item.text for item in s1.elements])

        units, _ = build_unit_drafts_s1_s7(
            {"elements": elements}, filing_type="semiannual_report"
        )
        borrowing = next(unit for unit in units if unit.title == "15 借款费用")
        intangible = next(unit for unit in units if unit.title == "16 无形资产")
        self.assertEqual(borrowing.heading_path[-1], "15 借款费用")
        self.assertNotIn("14 生物资产", borrowing.heading_path)
        self.assertNotIn("(2) 生产性生物资产", borrowing.heading_path)
        self.assertIn("borrowing_costs", borrowing.semantic_keys)
        self.assertEqual(intangible.heading_path[-1], "16 无形资产")
        self.assertNotIn("borrowing_costs", intangible.semantic_keys)

    def test_s1_recovers_one_sandwiched_controlled_note_ordinal(self) -> None:
        def heading(order: int, text: str, page: int) -> dict[str, object]:
            return {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": order,
                "heading_level": 2,
                "text": text,
                "page_no": page,
                "bbox": [109, 175, 360, 192],
            }

        elements = [
            heading(1, "三、 财务报表主要项目附注", 10),
            heading(2, "6. 发放贷款和垫款", 10),
            heading(3, "6.6 贷款减值准备变动", 11),
            {
                "kind": "table",
                "order_index": 4,
                "page_no": 11,
                "bbox": [157, 206, 892, 696],
                "table": {"headers": ["项目"], "rows": [["贷款减值"]]},
            },
            heading(5, "交易性金融资产", 12),
            {
                "kind": "table",
                "order_index": 6,
                "page_no": 12,
                "bbox": [157, 206, 892, 696],
                "table": {"headers": ["项目"], "rows": [["政府债券"]]},
            },
            heading(7, "8. 债权投资", 13),
            {
                "kind": "table",
                "order_index": 8,
                "page_no": 13,
                "bbox": [157, 206, 892, 696],
                "table": {"headers": ["项目"], "rows": [["企业债券"]]},
            },
        ]

        s1 = s1_preprocess_elements(elements)
        self.assertEqual(s1.stats.recovered_sandwiched_note_ordinals, 1)
        recovered = next(
            item for item in s1.elements if item.raw_kind == "recovered_sandwiched_note_ordinal"
        )
        self.assertEqual(recovered.text, "7. 交易性金融资产")
        self.assertEqual(
            (recovered.artifact_locator or {}).get("source_text"),
            "交易性金融资产",
        )

        units, stats = build_unit_drafts_s1_s7(
            {"elements": elements}, filing_type="semiannual_report"
        )
        trading = next(unit for unit in units if unit.title == "7. 交易性金融资产")
        self.assertEqual(stats.recovered_sandwiched_note_ordinals, 1)
        self.assertEqual(
            trading.heading_path,
            ["三、 财务报表主要项目附注", "7. 交易性金融资产"],
        )
        self.assertIn("trading_financial_assets", trading.semantic_keys)
        self.assertNotIn("bank_loan_loss_allowance", trading.semantic_keys)

    def test_s1_does_not_guess_ambiguous_sandwiched_note_ordinal(self) -> None:
        elements = [
            {
                "kind": "heading",
                "order_index": 1,
                "heading_level": 2,
                "text": "6. 发放贷款和垫款",
                "page_no": 1,
                "bbox": [109, 100, 300, 120],
            },
            {
                "kind": "heading",
                "order_index": 2,
                "heading_level": 2,
                "text": "交易性金融资产",
                "page_no": 2,
                "bbox": [109, 100, 300, 120],
            },
            {
                "kind": "heading",
                "order_index": 3,
                "heading_level": 2,
                "text": "其他债权投资",
                "page_no": 2,
                "bbox": [109, 140, 300, 160],
            },
            {
                "kind": "heading",
                "order_index": 4,
                "heading_level": 2,
                "text": "8. 债权投资",
                "page_no": 3,
                "bbox": [109, 100, 300, 120],
            },
        ]

        s1 = s1_preprocess_elements(elements)
        self.assertEqual(s1.stats.recovered_sandwiched_note_ordinals, 0)
        self.assertIn("交易性金融资产", [item.text for item in s1.elements])
        self.assertNotIn("7. 交易性金融资产", [item.text for item in s1.elements])

    def test_s1_inserts_split_note_heading_before_visually_lower_table(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "五 合并财务报表项目附注",
                        "page_no": 1,
                        "bbox": [50, 60, 320, 82],
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "32 长期借款",
                        "page_no": 1,
                        "bbox": [50, 100, 240, 122],
                    },
                    {
                        "kind": "table",
                        "order_index": 3,
                        "page_no": 2,
                        "bbox": [50, 180, 850, 420],
                        "table": {"headers": ["债券"], "rows": [["中期票据"]]},
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "header",
                        "order_index": 4,
                        "text": "应付债券",
                        "page_no": 2,
                        "bbox": [91, 137, 147, 159],
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "page_number",
                        "order_index": 5,
                        "text": "33",
                        "page_no": 2,
                        "bbox": [40, 139, 59, 156],
                    },
                    {
                        "kind": "table",
                        "order_index": 6,
                        "page_no": 3,
                        "bbox": [50, 200, 850, 420],
                        "table": {"headers": ["项目"], "rows": [["普通股"]]},
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "header",
                        "order_index": 7,
                        "text": "股本",
                        "page_no": 3,
                        "bbox": [91, 157, 147, 179],
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "page_number",
                        "order_index": 8,
                        "text": "36",
                        "page_no": 3,
                        "bbox": [40, 159, 59, 176],
                    },
                    {
                        "kind": "table",
                        "order_index": 9,
                        "page_no": 4,
                        "bbox": [50, 220, 850, 440],
                        "table": {"headers": ["项目"], "rows": [["税后净额"]]},
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "header",
                        "order_index": 10,
                        "text": "其他综合收益",
                        "page_no": 4,
                        "bbox": [91, 177, 190, 199],
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "page_number",
                        "order_index": 11,
                        "text": "39",
                        "page_no": 4,
                        "bbox": [40, 179, 59, 196],
                    },
                ]
            },
            filing_type="semiannual_report",
        )

        bonds = next(unit for unit in units if unit.title == "33 应付债券")
        capital = next(unit for unit in units if unit.title == "36 股本")
        oci = next(unit for unit in units if unit.title == "39 其他综合收益")
        self.assertIn("bonds_payable", bonds.semantic_keys)
        self.assertIn("share_capital", capital.semantic_keys)
        self.assertIn("other_comprehensive_income", oci.semantic_keys)
        self.assertLess(bonds.source_order, capital.source_order)
        self.assertLess(capital.source_order, oci.source_order)
        self.assertNotIn("long_term_borrowings", bonds.semantic_keys)
        self.assertNotIn("share_capital", oci.semantic_keys)

    def test_split_income_statement_supplement_stays_separate_from_eps_table(
        self,
    ) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "五 合并财务报表项目附注",
                        "page_no": 1,
                        "bbox": [57, 60, 320, 82],
                    },
                    {
                        # Real MinerU emits this statutory note title as text.
                        # The exact controlled alias permits S2 promotion
                        # without admitting arbitrary numeric-space prose.
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "53 基本每股收益和稀释每股收益的计算过程",
                        "page_no": 1,
                        "bbox": [57, 400, 220, 420],
                    },
                    {
                        "kind": "table",
                        "order_index": 3,
                        "page_no": 1,
                        "bbox": [131, 620, 907, 740],
                        "table": {
                            "headers": [],
                            "rows": [
                                ["项目", "本期金额", "上期金额"],
                                ["普通股加权平均数", "100", "90"],
                            ],
                        },
                    },
                    {
                        "kind": "table",
                        "order_index": 4,
                        "page_no": 2,
                        "bbox": [131, 128, 907, 363],
                        "table": {
                            "headers": [],
                            "rows": [
                                ["项目", "本期金额", "上期金额"],
                                ["营业收入", "200", "180"],
                                ["营业利润", "20", "18"],
                            ],
                        },
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 5,
                        "heading_level": 2,
                        "text": "55 现金流量表项目",
                        "page_no": 2,
                        "bbox": [57, 381, 262, 399],
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "page_number",
                        "order_index": 6,
                        "text": "54",
                        "page_no": 2,
                        "bbox": [58, 98, 82, 111],
                    },
                    {
                        "kind": "page_furniture",
                        "raw_kind": "header",
                        "order_index": 7,
                        "text": "利润表补充资料",
                        "page_no": 2,
                        "bbox": [129, 96, 262, 112],
                    },
                ]
            },
            filing_type="semiannual_report",
        )

        self.assertEqual(stats.recovered_split_note_headings, 1)
        eps = next(
            unit
            for unit in units
            if unit.title == "53 基本每股收益和稀释每股收益的计算过程"
        )
        supplement = next(unit for unit in units if unit.title == "54 利润表补充资料")
        self.assertIn("普通股加权平均数", str(eps.payload))
        self.assertNotIn("营业收入", str(eps.payload))
        self.assertIn("earnings_per_share", eps.semantic_keys or [])
        self.assertIn("营业收入", str(supplement.payload))
        self.assertIn("income_statement_supplement", supplement.semantic_keys or [])

    def test_s2_controlled_integer_heading_exits_decimal_run(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第二节 公司基本情况",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "4.4 报告期末公司优先股股东情况",
                    },
                    {"kind": "text", "order_index": 3, "text": "不适用。"},
                    {
                        "kind": "heading",
                        "order_index": 4,
                        "heading_level": 2,
                        "text": "5 公司债券情况",
                    },
                    {"kind": "text", "order_index": 5, "text": "不适用。"},
                ]
            },
            filing_type="annual_report",
        )

        bonds = next(unit for unit in units if unit.title == "5 公司债券情况")
        self.assertEqual(
            bonds.heading_path,
            ["第二节 公司基本情况", "5 公司债券情况"],
        )
        self.assertIn("bonds_section", bonds.semantic_keys)
        self.assertNotIn("other_equity_instruments", bonds.semantic_keys)

    def test_s2_decimal_major_heading_exits_its_previous_decimal_run(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第三节 经营情况讨论与分析",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "3. 委托理财总体情况",
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "heading_level": 3,
                        "text": "3.2 委托理财业务",
                    },
                    {"kind": "text", "order_index": 4, "text": "无。"},
                    {
                        "kind": "heading",
                        "order_index": 5,
                        "heading_level": 2,
                        "text": "4. 或有事项",
                    },
                    {"kind": "text", "order_index": 6, "text": "无。"},
                ]
            },
            filing_type="annual_report",
        )

        contingencies = next(unit for unit in units if unit.title == "4. 或有事项")
        self.assertEqual(
            contingencies.heading_path,
            ["第三节 经营情况讨论与分析", "4. 或有事项"],
        )

    def test_s4_never_silently_drops_prose_before_first_question(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": "会议背景说明。\n1、收入如何？\n答：收入保持增长。"},
            source_order=1,
            heading_path=["主要交流问题"],
            title="主要交流问题",
        )

        units = replace_text_units_with_qa_where_stable([source])

        self.assertEqual(len(units), 2)
        self.assertEqual(units[0].payload_kind, "text")
        self.assertEqual(units[0].quality_status, "ok")
        self.assertEqual(units[0].payload["text"], "会议背景说明。")
        self.assertEqual(units[1].payload_kind, "qa")
        self.assertEqual(units[1].payload["question"], "收入如何？")
        self.assertEqual(units[1].payload["answer"], "收入保持增长。")

    def test_s4_table_qa_suffix_survives_form_metadata_prefix(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": ["投资者关系活动主要内容介绍"],
                            "rows": [
                                [
                                    "会议时间：2026年7月15日\n"
                                    "1.收入如何？\n答：收入增长。\n"
                                    "2.产能如何？\n答：产能稳定。"
                                ]
                            ],
                        },
                    }
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["收入如何？", "产能如何？"],
        )

    def test_ir_filing_type_adds_broad_retrieval_key_to_every_unit(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "order_index": 1,
                        "text": "问：收入如何？\n答：收入保持增长。",
                    },
                    {
                        "kind": "table",
                        "order_index": 2,
                        "table": {
                            "headers": ["项目", "金额"],
                            "rows": [["收入", "100"]],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            # Deliberately does not rely on event_key_map title wording.
            document_title="某公司调研纪要",
        )

        self.assertTrue(units)
        self.assertTrue(
            all("investor_communication" in unit.semantic_keys for unit in units)
        )

    def test_s4_keeps_complete_qas_before_truncated_final_question(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={
                "text": (
                    "1、收入如何？\n收入增长。\n"
                    "2、产能如何？\n产能稳定。\n"
                    "3、海外工厂进展如何？政策的"
                )
            },
            source_order=1,
            heading_path=["二、问答环节"],
            title="二、问答环节",
        )

        units = replace_text_units_with_qa_where_stable([source])

        self.assertEqual(
            [unit.payload_kind for unit in units],
            ["qa", "qa", "text"],
        )
        self.assertEqual(units[-1].quality_status, "needs_review")
        self.assertEqual(units[-1].payload["text"], "3、海外工厂进展如何？政策的")

    def test_s4_strong_q_boundary_never_leaks_into_previous_answer(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["主要交流问题"],
        )

        parsed = s4_build_qa_units(
            "Q4、分红情况如何？\n回复：维持稳定分红。\nQ5、知识产权体系是什么样的体",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 1)
        self.assertEqual(parsed.units[0].payload["question"], "分红情况如何？")
        self.assertEqual(parsed.units[0].payload["answer"], "维持稳定分红。")
        self.assertEqual(parsed.trailing_text, "Q5、知识产权体系是什么样的体")

    def test_s4_keeps_spaced_problem_ordinals_atomic(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["主要交流问题"],
        )

        parsed = s4_build_qa_units(
            "问题 2、原材料价格如何？\n答：总体稳定。\n"
            "问题 3、客户结构如何？\n答：较为分散。\n"
            "问题 4、库存策略如何？\n答：保持适度库存。\n"
            "问题 5、未来分红如何？",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.ordinals, [2, 3, 4])
        self.assertEqual(
            [unit.payload["answer"] for unit in parsed.units],
            ["总体稳定。", "较为分散。", "保持适度库存。"],
        )
        self.assertEqual(parsed.trailing_text, "问题5、未来分红如何？")

        colon = s4_build_qa_units(
            "问题 2：原材料价格如何？\n感谢您的提问。总体稳定。\n"
            "问题 3：客户结构如何？\n感谢您的提问。较为分散。",
            source=source,
        )
        self.assertFalse(colon.unstable)
        self.assertEqual(colon.ordinals, [2, 3])
        self.assertEqual(
            [unit.payload["question"] for unit in colon.units],
            ["原材料价格如何？", "客户结构如何？"],
        )

        packed = s4_build_qa_units(
            "会议背景介绍。问题1、业绩为什么下滑？答：主要受原料价格影响。",
            source=source,
            require_explicit_answer=True,
        )
        self.assertFalse(packed.unstable)
        self.assertEqual(len(packed.units), 1)
        self.assertEqual(packed.units[0].payload["question"], "业绩为什么下滑？")

        answer_list = s4_build_qa_units(
            "问：主要风险是什么？\n答：主要有以下\n问题\n1、成本较高。\n2、交付较慢。",
            source=source,
        )
        self.assertFalse(answer_list.unstable)
        self.assertEqual(len(answer_list.units), 1)
        self.assertIn("问题\n1、成本较高", answer_list.units[0].payload["answer"])

    def test_s4_splits_glued_answer_labels_without_splitting_answer_words(
        self,
    ) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["主要交流问题"],
        )
        parsed = s4_build_qa_units(
            "问题1、收入变化希望答:收入增长。"
            "问题2、成本会下降吗答：会逐步下降。"
            "问题3、海外进展如何谢谢公司回复:进展顺利。",
            source=source,
            require_explicit_answer=True,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 3)
        self.assertEqual(
            [unit.payload["answer"] for unit in parsed.units],
            ["收入增长。", "会逐步下降。", "进展顺利。"],
        )

        lexical = s4_build_qa_units(
            "问题1、如何理解问答：栏目并回答：收入问题？答：正常栏目。"
            "问题2、应答：和作答：是否属于标签？答：不属于。",
            source=source,
            require_explicit_answer=True,
        )
        self.assertFalse(lexical.unstable)
        self.assertEqual(len(lexical.units), 2)
        self.assertIn("问答：栏目并回答：", lexical.units[0].payload["question"])
        self.assertIn("应答：和作答：", lexical.units[1].payload["question"])

    def test_s4_year_prefixed_outer_ordinal_is_not_treated_as_decimal(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["主要交流问题"],
        )
        parsed = s4_build_qa_units(
            "42.请问海外业务情况谢谢答:海外业务稳定增长。"
            "43.2024年家电行业表现如何？答:行业保持韧性。",
            source=source,
            require_explicit_answer=True,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.ordinals, [42, 43])
        self.assertEqual(
            [unit.payload["question"] for unit in parsed.units],
            ["请问海外业务情况谢谢", "2024年家电行业表现如何？"],
        )
        self.assertEqual(
            [unit.payload["answer"] for unit in parsed.units],
            ["海外业务稳定增长。", "行业保持韧性。"],
        )
        self.assertTrue(parsed.units[1].payload["raw_text"].startswith("43.2024年"))
        self.assertNotIn("43、2024年", parsed.units[1].payload["raw_text"])

    def test_s4_unlabelled_response_lookahead_stops_at_numbered_answer_list(
        self,
    ) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["主要交流问题"],
        )
        parsed = s4_build_qa_units(
            "1、主要风险是什么？\n"
            "公司回复如下：\n"
            "2、运营推进。\n"
            "3、渠道推进。\n"
            "尊敬的投资者，公司将稳步推进。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 1)
        self.assertIn("2、运营推进", parsed.units[0].payload["answer"])
        self.assertIn("3、渠道推进", parsed.units[0].payload["answer"])

    def test_s4_later_malformed_pair_preserves_earlier_complete_qa(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["主要交流问题"],
        )
        parsed = s4_build_qa_units(
            "问题1、收入如何？\n答：收入增长。\n"
            "问题2、成本如何？\n"
            "问题3、订单如何？\n答：订单稳定。",
            source=source,
            require_explicit_answer=True,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 1)
        self.assertEqual(parsed.units[0].payload["question"], "收入如何？")
        self.assertEqual(
            parsed.trailing_text,
            "问题2、成本如何？\n问题3、订单如何？\n答：订单稳定。",
        )

    def test_qa_mode_numbered_mineru_headings_are_strong_boundaries(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "text": "三、主要交流问题",
                        "heading_level": 1,
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "text": "5、海外业务布局",
                        "heading_level": 2,
                    },
                    {"kind": "text", "order_index": 3, "text": "稳步推进。"},
                    {
                        "kind": "heading",
                        "order_index": 4,
                        "text": "6、建议增加分红频次。",
                        "heading_level": 2,
                    },
                    {
                        "kind": "text",
                        "order_index": 5,
                        "text": "尊敬的投资者，感谢您的建议。",
                    },
                    {
                        "kind": "text",
                        "order_index": 6,
                        "text": "7、建议增加回购频次。",
                    },
                    {
                        "kind": "text",
                        "order_index": 7,
                        "text": "尊敬的投资者，公司会综合考虑。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["海外业务布局", "建议增加分红频次。", "建议增加回购频次。"],
        )
        self.assertEqual(
            [unit.payload["answer"] for unit in qa_units],
            [
                "稳步推进。",
                "尊敬的投资者，感谢您的建议。",
                "尊敬的投资者，公司会综合考虑。",
            ],
        )

    def test_non_qa_filing_requires_explicit_answer_markers(self) -> None:
        periodic_units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "text": "审计师职责",
                        "heading_level": 1,
                    },
                    {
                        "kind": "text",
                        "order_index": 2,
                        "text": "1、评价财务报表是否公允反映？",
                    },
                    {
                        "kind": "text",
                        "order_index": 3,
                        "text": "我们还会评价列报、结构和内容。",
                    },
                ]
            },
            filing_type="annual_report",
        )
        self.assertFalse(any(unit.payload_kind == "qa" for unit in periodic_units))
        self.assertTrue(all(unit.quality_status == "ok" for unit in periodic_units))

        explicit_units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "order_index": 1,
                        "text": "问：收入如何？\n答：收入保持增长。",
                    }
                ]
            },
            filing_type="annual_report",
        )
        self.assertEqual([unit.payload_kind for unit in explicit_units], ["qa"])

    def test_s2_text_toc_entries_cannot_escape_skip_section(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="目录", heading_level=1
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="第三章 管理层讨论与分析....23",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="第四章 公司治理、环境和社会 57",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=4,
                    text="第一章 公司简介",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=5, text="公司主营业务稳定。"),
            ]
        )

        self.assertEqual(placed[0].heading_path, ["目录"])
        self.assertEqual(placed[1].heading_path, ["目录"])
        self.assertEqual(placed[2].heading_path, ["第一章 公司简介"])

    def test_full_builder_drops_heading_toc_rows_but_keeps_real_sections(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "目录",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "第三章 管理层讨论与分析 …… 17",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "第四章 公司治理、环境和社会 57",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 4,
                        "heading_level": 1,
                        "text": "第一节 重要提示、目录和释义",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 5,
                        "text": "公司保证报告内容真实、准确、完整。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 6,
                        "heading_level": 1,
                        "text": "第三章 管理层讨论与分析",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 7,
                        "text": "公司总体经营保持稳健。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 8,
                        "heading_level": 1,
                        "text": "业务进展....2025",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 9,
                        "text": "目录外的原始点线标题必须保留。",
                    },
                ]
            },
            filing_type="semiannual_report",
        )

        titles = [unit.title for unit in units]
        self.assertIn("第一节 重要提示、目录和释义", titles)
        self.assertIn("第三章 管理层讨论与分析", titles)
        self.assertIn("业务进展....2025", titles)
        self.assertFalse(any(title and "…… 17" in title for title in titles))
        self.assertNotIn("第四章 公司治理、环境和社会 57", titles)

    def test_s2_unnumbered_level_one_leaf_does_not_poison_numbered_tree(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="第八节 财务报告",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="七、合并财务报表项目注释",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="48、长期应付款",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=4,
                    text="- 成本法转公允价值计量",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=5, text="转换说明。"),
                PreparedElement(
                    kind="heading",
                    order_index=6,
                    text="49、长期应付职工薪酬",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=7, text="职工薪酬说明。"),
                PreparedElement(
                    kind="heading",
                    order_index=8,
                    text="十四、关联方及关联交易",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=9, text="关联交易说明。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["职工薪酬说明。"].heading_path,
            ["第八节 财务报告", "七、合并财务报表项目注释", "49、长期应付职工薪酬"],
        )
        self.assertEqual(
            by_text["关联交易说明。"].heading_path,
            ["第八节 财务报告", "十四、关联方及关联交易"],
        )
        self.assertNotIn(
            "- 成本法转公允价值计量",
            by_text["职工薪酬说明。"].heading_path,
        )

    def test_s2_statutory_financial_statements_are_unnumbered_siblings(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="第十节 财务报告",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading", order_index=2, text="二、财务报表", heading_level=1
                ),
                PreparedElement(kind="text", order_index=3, text="合并资产负债表"),
                PreparedElement(kind="text", order_index=4, text="合并资产。"),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    text="母公司资产负债表",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=6, text="母公司资产。"),
                PreparedElement(
                    kind="heading", order_index=7, text="合并利润表", heading_level=1
                ),
                PreparedElement(kind="text", order_index=8, text="合并利润。"),
                PreparedElement(
                    kind="table",
                    order_index=9,
                    table_caption=["母公司利润表", "2023年1—6月"],
                    table={"headers": ["项目"], "rows": [["收入"]]},
                ),
            ]
        )

        by_text = {element.text: element for element in placed}
        for body, statement in (
            ("合并资产。", "合并资产负债表"),
            ("母公司资产。", "母公司资产负债表"),
            ("合并利润。", "合并利润表"),
        ):
            self.assertEqual(
                by_text[body].heading_path,
                ["第十节 财务报告", "二、财务报表", statement],
            )
        statement_table = next(element for element in placed if element.kind == "table")
        self.assertEqual(
            statement_table.heading_path,
            ["第十节 财务报告", "二、财务报表", "母公司利润表"],
        )

    def test_s2_first_statement_replaces_auditor_responsibility_sibling(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="第十章 财务报告",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="六、注册会计师对财务报表审计的责任",
                    heading_level=2,
                ),
                PreparedElement(kind="text", order_index=3, text="审计责任正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=4,
                    text="合并及银行资产负债表",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=5, text="资产合计。"),
            ]
        )

        asset_body = next(item for item in placed if item.text == "资产合计。")
        self.assertEqual(
            asset_body.heading_path,
            ["第十章 财务报告", "合并及银行资产负债表"],
        )
        self.assertNotIn("注册会计师", " ".join(asset_body.heading_path))

    def test_year_prefixed_statement_caption_stays_separate_from_prior_statement(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "text": "第十节 财务报告",
                        "heading_level": 1,
                    },
                    {
                        "kind": "table",
                        "order_index": 2,
                        "table_caption": ["合并及公司资产负债表"],
                        "table": {"headers": ["资产"], "rows": [["货币资金"]]},
                    },
                    {
                        "kind": "text",
                        "order_index": 3,
                        "text": "后附财务报表附注为财务报表的组成部分。",
                    },
                    {
                        "kind": "table",
                        "order_index": 4,
                        "table_caption": ["2024 年度合并及公司利润表"],
                        "table": {"headers": ["项目"], "rows": [["营业收入"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        balance = next(unit for unit in units if unit.title == "合并及公司资产负债表")
        income = next(
            unit for unit in units if unit.title == "2024 年度合并及公司利润表"
        )
        self.assertIn("balance_sheet", balance.semantic_keys or [])
        self.assertNotIn("income_statement", balance.semantic_keys or [])
        self.assertIn("income_statement", income.semantic_keys or [])
        self.assertNotIn("balance_sheet", income.semantic_keys or [])

    def test_s2_bounded_issuer_and_half_year_statement_titles_reanchor(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="财务报告", heading_level=1
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="合并及公司资产负债表",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=3, text="资产正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=4,
                    text="2024 年半年度合并及公司利润表",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=5, text="利润正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=6,
                    text="平安银行股份有限公司合并现金流量表",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=7, text="现金流正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=8,
                    text="2023年半年度公司股东权益变动表（续）",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=9, text="权益正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=10,
                    text="合并利润表-按中国会计准则编制（续）",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=11, text="准则利润正文。"),
            ]
        )

        by_text = {item.text: item for item in placed}
        self.assertEqual(
            by_text["利润正文。"].heading_path,
            ["财务报告", "2024 年半年度合并及公司利润表"],
        )
        self.assertEqual(
            by_text["现金流正文。"].heading_path,
            ["财务报告", "平安银行股份有限公司合并现金流量表"],
        )
        self.assertEqual(
            by_text["权益正文。"].heading_path,
            ["财务报告", "2023年半年度公司股东权益变动表"],
        )
        self.assertEqual(
            by_text["准则利润正文。"].heading_path,
            ["财务报告", "合并利润表-按中国会计准则编制"],
        )

    def test_s2_statement_suffix_grammar_rejects_analysis_heading(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="经营情况", heading_level=1
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="1、合并资产负债表重大项目变化情况",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=3, text="变化分析正文。"),
            ]
        )

        body = next(item for item in placed if item.text == "变化分析正文。")
        self.assertEqual(
            body.heading_path,
            ["经营情况", "1、合并资产负债表重大项目变化情况"],
        )

    def test_s2_numbered_notes_exit_statement_run_and_year_does_not_replace_it(
        self,
    ) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="财务报告", heading_level=1
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="合并股东权益变动表",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading", order_index=3, text="2024年度", heading_level=1
                ),
                PreparedElement(kind="text", order_index=4, text="权益变动。"),
                PreparedElement(
                    kind="heading", order_index=5, text="一、公司简介", heading_level=2
                ),
                PreparedElement(kind="text", order_index=6, text="公司简介正文。"),
            ]
        )

        by_text = {item.text: item for item in placed}
        self.assertEqual(
            by_text["权益变动。"].heading_path,
            ["财务报告", "合并股东权益变动表"],
        )
        self.assertEqual(
            by_text["公司简介正文。"].heading_path,
            ["财务报告", "一、公司简介"],
        )

    def test_s2_signatory_h1s_cannot_evict_open_statement_tree(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="财务报告", heading_level=1
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="合并及银行资产负债表",
                    heading_level=1,
                ),
                *[
                    PreparedElement(
                        kind="heading",
                        order_index=index + 3,
                        text=name,
                        heading_level=1,
                    )
                    for index, name in enumerate(("缪建民", "王良", "彭家文", "李俐"))
                ],
                PreparedElement(
                    kind="table",
                    order_index=7,
                    table_caption=["合并及银行利润表"],
                    table={"headers": ["项目"], "rows": [["营业收入"]]},
                ),
            ]
        )

        table = next(element for element in placed if element.kind == "table")
        self.assertEqual(table.heading_path, ["财务报告", "合并及银行利润表"])
        self.assertFalse(any(name in table.heading_path for name in ("缪建民", "王良")))
        self.assertFalse(
            any(
                name in element.heading_path
                for element in placed
                for name in ("缪建民", "王良", "彭家文", "李俐")
            )
        )

    def test_s2_controlled_notes_heading_exits_statement_after_signatories(
        self,
    ) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="财务报告", heading_level=1
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="公司股东权益变动表",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="美的集团股份有限公司",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=4,
                    text="2025年度财务报表附注",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    text="一 公司基本情况",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=6, text="公司基本情况正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=7,
                    text="二 主要会计政策和会计估计",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=8, text="会计政策正文。"),
            ]
        )

        by_text = {item.text: item for item in placed}
        body = by_text["公司基本情况正文。"]
        self.assertEqual(
            body.heading_path,
            ["财务报告", "2025年度财务报表附注", "一 公司基本情况"],
        )
        self.assertNotIn("公司股东权益变动表", body.heading_path)
        self.assertEqual(
            by_text["会计政策正文。"].heading_path,
            ["财务报告", "2025年度财务报表附注", "二 主要会计政策和会计估计"],
        )

    def test_s2_company_profile_is_child_of_open_financial_notes(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="第九节 财务报告",
                    heading_level=2,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="万科企业股份有限公司财务报表附注(除特别注明外，金额单位为人民币元)",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading", order_index=3, text="公司基本情况", heading_level=2
                ),
                PreparedElement(kind="text", order_index=4, text="公司沿革正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    text="二 财务报表编制基础",
                    heading_level=2,
                ),
                PreparedElement(kind="text", order_index=6, text="编制基础正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=7,
                    text="五 合并财务报表项目附注",
                    heading_level=2,
                ),
                PreparedElement(
                    kind="heading", order_index=8, text="1 货币资金", heading_level=2
                ),
                PreparedElement(kind="text", order_index=9, text="银行存款正文。"),
            ]
        )

        by_text = {item.text: item for item in placed}
        self.assertEqual(
            by_text["公司沿革正文。"].heading_path,
            [
                "第九节 财务报告",
                "万科企业股份有限公司财务报表附注(除特别注明外，金额单位为人民币元)",
                "公司基本情况",
            ],
        )
        self.assertNotIn("公司基本情况", by_text["编制基础正文。"].heading_path)
        self.assertNotIn("公司基本情况", by_text["银行存款正文。"].heading_path)
        self.assertEqual(
            by_text["编制基础正文。"].heading_path,
            [
                "第九节 财务报告",
                "万科企业股份有限公司财务报表附注(除特别注明外，金额单位为人民币元)",
                "二 财务报表编制基础",
            ],
        )
        self.assertEqual(
            by_text["银行存款正文。"].heading_path,
            [
                "第九节 财务报告",
                "万科企业股份有限公司财务报表附注(除特别注明外，金额单位为人民币元)",
                "五 合并财务报表项目附注",
                "1 货币资金",
            ],
        )

        standalone = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="管理层讨论与分析",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading", order_index=2, text="公司基本情况", heading_level=1
                ),
                PreparedElement(kind="text", order_index=3, text="银行概况正文。"),
            ]
        )
        self.assertEqual(standalone[-1].heading_path, ["公司基本情况"])

    def test_s2_dash_continued_statement_exits_at_parenthesized_notes_start(
        self,
    ) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="第九节 财务报告",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="table",
                    order_index=2,
                    table_caption=["母公司股东权益变动表 - 续"],
                    table={"headers": ["项目"], "rows": [["资本公积"]]},
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="(一) 公司基本情况",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=4, text="公司基本情况正文。"),
            ]
        )

        body = next(item for item in placed if item.text == "公司基本情况正文。")
        self.assertEqual(
            body.heading_path,
            ["第九节 财务报告", "(一) 公司基本情况"],
        )
        self.assertNotIn("母公司股东权益变动表 - 续", body.heading_path)

    def test_full_builder_dash_statement_continuation_is_not_nested(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "text": "第九节 财务报告",
                        "heading_level": 1,
                    },
                    {
                        "kind": "table",
                        "order_index": 2,
                        "table_caption": ["母公司股东权益变动表"],
                        "table": {"headers": ["项目"], "rows": [["股本"]]},
                    },
                    {
                        "kind": "table",
                        "order_index": 3,
                        "table_caption": ["母公司股东权益变动表 - 续"],
                        "table": {"headers": ["项目"], "rows": [["资本公积"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertTrue(units)
        for unit in units:
            self.assertEqual(unit.heading_path.count("母公司股东权益变动表"), 1)
            self.assertNotIn("母公司股东权益变动表 - 续", unit.heading_path)

    def test_s2_statement_name_inside_prose_or_toc_never_opens_statement(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="财务报告", heading_level=1
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="29. 员工福利计划",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="text",
                    order_index=3,
                    text="于合并利润表内确认的金额如下：",
                ),
                PreparedElement(
                    kind="text",
                    order_index=4,
                    text="未经审计合并及公司资产负债表 88 利润表 92 现金流量表 96",
                ),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    text="30. 应交税费",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=6, text="税费正文。"),
            ]
        )

        body = next(item for item in placed if item.text == "税费正文。")
        self.assertEqual(body.heading_path, ["财务报告", "30. 应交税费"])
        self.assertFalse(any("利润表内确认" in title for title in body.heading_path))

    def test_s2_skipped_controlled_note_ordinal_stays_a_sibling(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="四、财务报表主要项目注释",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="42. 其他综合收益",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="42.1 资产负债表中归属于母公司股东的其他综合收益情况",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=4, text="综合收益正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    text="44. 现金流量表补充资料",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=6, text="现金流补充正文。"),
            ]
        )

        cash = next(item for item in placed if item.text == "现金流补充正文。")
        self.assertEqual(
            cash.heading_path,
            ["四、财务报表主要项目注释", "44. 现金流量表补充资料"],
        )

    def test_s2_decimal_outline_cannot_escape_controlled_major_note(self) -> None:
        def heading(order: int, text: str, left: int) -> PreparedElement:
            return PreparedElement(
                kind="heading",
                order_index=order,
                text=text,
                heading_level=1,
                artifact_locator={"bbox": [left, 100, left + 300, 120]},
            )

        placed = s2_apply_heading_tree(
            [
                heading(1, "第九节 财务报告", 40),
                heading(2, "(三) 重要会计政策和会计估计", 78),
                heading(3, "35.1 租赁负债", 139),
                PreparedElement(kind="text", order_index=4, text="政策正文。"),
                heading(5, "(四) 税项", 78),
                PreparedElement(kind="text", order_index=6, text="税项正文。"),
                heading(7, "(五) 合并财务报表项目注释", 78),
                heading(8, "28、其他应付款", 139),
                heading(9, "28.1 项目列示", 139),
                heading(10, "28.2 其他应付款", 139),
                PreparedElement(kind="text", order_index=11, text="应付款正文。"),
                heading(12, "(六) 合并范围的变更", 78),
                PreparedElement(kind="text", order_index=13, text="合并范围正文。"),
                heading(14, "(八) 与金融工具相关的风险", 78),
                heading(15, "1、风险管理目标、政策和程序", 139),
                heading(16, "1.1 市场风险", 139),
                heading(17, "1.2 信用风险", 139),
                heading(18, "1.3 流动性风险", 139),
                PreparedElement(kind="text", order_index=19, text="流动性正文。"),
                heading(20, "(九) 公允价值的披露", 78),
                PreparedElement(kind="text", order_index=21, text="公允价值正文。"),
                heading(22, "(十) 关联方及关联方交易", 78),
                PreparedElement(kind="text", order_index=23, text="关联方正文。"),
            ]
        )

        by_text = {item.text: item for item in placed}
        self.assertEqual(
            by_text["政策正文。"].heading_path,
            [
                "第九节 财务报告",
                "(三) 重要会计政策和会计估计",
                "35.1 租赁负债",
            ],
        )
        self.assertEqual(
            by_text["税项正文。"].heading_path,
            ["第九节 财务报告", "(四) 税项"],
        )
        self.assertEqual(
            by_text["应付款正文。"].heading_path,
            [
                "第九节 财务报告",
                "(五) 合并财务报表项目注释",
                "28、其他应付款",
                "28.2 其他应付款",
            ],
        )
        self.assertEqual(
            by_text["合并范围正文。"].heading_path,
            ["第九节 财务报告", "(六) 合并范围的变更"],
        )
        self.assertEqual(
            by_text["公允价值正文。"].heading_path,
            ["第九节 财务报告", "(九) 公允价值的披露"],
        )
        self.assertNotIn(
            "流动性风险", " > ".join(by_text["公允价值正文。"].heading_path)
        )
        self.assertEqual(
            by_text["关联方正文。"].heading_path,
            ["第九节 财务报告", "(十) 关联方及关联方交易"],
        )

    def test_s2_indent_repairs_document_local_dot_numbering_style(self) -> None:
        def heading(order: int, text: str, left: int) -> PreparedElement:
            return PreparedElement(
                kind="heading",
                order_index=order,
                text=text,
                heading_level=1,
                artifact_locator={"bbox": [left, 100, left + 300, 120]},
            )

        placed = s2_apply_heading_tree(
            [
                heading(1, "第十节 财务报告", 90),
                heading(2, "五、重要会计政策及会计估计", 90),
                heading(3, "6. 合并财务报表的编制方法", 90),
                heading(4, "1. 合并范围", 132),
                PreparedElement(kind="text", order_index=5, text="合并范围说明。"),
                heading(6, "7. 合营安排分类及共同经营会计处理方法", 90),
                heading(7, "1. 合营安排的分类", 132),
                PreparedElement(kind="text", order_index=8, text="合营安排说明。"),
                heading(9, "11. 应收票据", 90),
                PreparedElement(kind="text", order_index=10, text="应收票据说明。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["合并范围说明。"].heading_path,
            [
                "第十节 财务报告",
                "五、重要会计政策及会计估计",
                "6. 合并财务报表的编制方法",
                "1. 合并范围",
            ],
        )
        self.assertEqual(
            by_text["合营安排说明。"].heading_path,
            [
                "第十节 财务报告",
                "五、重要会计政策及会计估计",
                "7. 合营安排分类及共同经营会计处理方法",
                "1. 合营安排的分类",
            ],
        )
        self.assertEqual(
            by_text["应收票据说明。"].heading_path,
            ["第十节 财务报告", "五、重要会计政策及会计估计", "11. 应收票据"],
        )

    def test_s2_tracks_deep_internal_tree_without_exceeding_public_cap(self) -> None:
        def heading(order: int, text: str, left: int) -> PreparedElement:
            return PreparedElement(
                kind="heading",
                order_index=order,
                text=text,
                heading_level=1,
                artifact_locator={"bbox": [left, 100, left + 300, 120]},
            )

        placed = s2_apply_heading_tree(
            [
                heading(1, "第十节 财务报告", 90),
                heading(2, "五、重要会计政策及会计估计", 90),
                heading(3, "（一）政策组", 90),
                heading(4, "5. 企业合并", 90),
                # The source starts this child sequence at 2; indentation,
                # not ordinal 1, is the reliable parent signal.
                heading(5, "2、同一控制下企业合并", 132),
                heading(6, "（2）购买日计量", 140),
                PreparedElement(kind="text", order_index=7, text="购买日说明。"),
                heading(8, "6. 合并财务报表的编制方法", 90),
                PreparedElement(kind="text", order_index=9, text="合并报表说明。"),
                heading(10, "1、合并范围", 132),
                heading(11, "7. 合营安排", 90),
                PreparedElement(kind="text", order_index=12, text="合营安排说明。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["购买日说明。"].heading_path,
            [
                "第十节 财务报告",
                "五、重要会计政策及会计估计",
                "（一）政策组",
                "5. 企业合并",
            ],
        )
        self.assertEqual(
            by_text["合并报表说明。"].heading_path,
            [
                "第十节 财务报告",
                "五、重要会计政策及会计估计",
                "（一）政策组",
                "6. 合并财务报表的编制方法",
            ],
        )
        self.assertEqual(
            by_text["合营安排说明。"].heading_path,
            [
                "第十节 财务报告",
                "五、重要会计政策及会计估计",
                "（一）政策组",
                "7. 合营安排",
            ],
        )
        self.assertTrue(all(len(element.heading_path) <= 4 for element in placed))

    def test_deep_controlled_leaf_siblings_remain_distinct_units(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "text": "第十节 财务报告",
                        "heading_level": 1,
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "text": "五、财务报表附注",
                        "heading_level": 2,
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "text": "（一）资产项目",
                        "heading_level": 3,
                    },
                    {
                        "kind": "heading",
                        "order_index": 4,
                        "text": "1、主题",
                        "heading_level": 4,
                    },
                    {
                        "kind": "heading",
                        "order_index": 5,
                        "text": "1. 应收账款",
                        "heading_level": 5,
                    },
                    {"kind": "text", "order_index": 6, "text": "应收账款正文A。"},
                    {
                        "kind": "table",
                        "order_index": 7,
                        "table": {"headers": ["账龄"], "rows": [["一年以内"]]},
                    },
                    {
                        "kind": "heading",
                        "order_index": 8,
                        "text": "2. 存货",
                        "heading_level": 5,
                    },
                    {"kind": "text", "order_index": 9, "text": "存货正文B。"},
                    {
                        "kind": "table",
                        "order_index": 10,
                        "table": {"headers": ["类别"], "rows": [["原材料"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual([unit.title for unit in units], ["1. 应收账款", "2. 存货"])
        self.assertEqual([unit.payload_kind for unit in units], ["mixed", "mixed"])
        self.assertEqual(
            [[part["kind"] for part in unit.payload["parts"]] for unit in units],
            [["text", "table"], ["text", "table"]],
        )
        self.assertTrue(all(len(unit.heading_path) <= 4 for unit in units))
        self.assertEqual(
            [unit.heading_path[-1] for unit in units], ["1. 应收账款", "2. 存货"]
        )

    def test_hidden_decimal_leaf_reanchor_preserves_chinese_ancestors(self) -> None:
        public_path = [
            "第十节 财务报告",
            "五、财务报表附注",
            "（一）资产项目",
            "1、主题",
        ]
        units = s8_group_semantic_units(
            [
                UnitDraft(
                    payload_kind="text",
                    payload={"text": "分部正文。"},
                    source_order=1,
                    heading_path=public_path,
                    structural_path=[*public_path, "3.6 分部经营业绩"],
                    title="3.6 分部经营业绩",
                )
            ],
            filing_type="annual_report",
            stats=BuildStats(),
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(
            units[0].heading_path,
            [
                "第十节 财务报告",
                "五、财务报表附注",
                "（一）资产项目",
                "3.6 分部经营业绩",
            ],
        )

    def test_hidden_numeric_child_reanchor_stays_within_public_depth_cap(self) -> None:
        public_path = [
            "第十节 财务报告",
            "3. 风险管理",
            "3.3 信用风险",
            "3.3.1 客户贷款",
        ]
        units = s8_group_semantic_units(
            [
                UnitDraft(
                    payload_kind="text",
                    payload={"text": "贷款和垫款正文。"},
                    source_order=1,
                    heading_path=public_path,
                    structural_path=[*public_path, "3.3.1.1 贷款和垫款"],
                    title="3.3.1.1 贷款和垫款",
                )
            ],
            filing_type="annual_report",
            stats=BuildStats(),
        )

        self.assertEqual(
            units[0].heading_path,
            [
                "第十节 财务报告",
                "3. 风险管理",
                "3.3 信用风险",
                "3.3.1.1 贷款和垫款",
            ],
        )
        self.assertLessEqual(len(units[0].heading_path), 4)

    def test_s2_numbering_continuity_beats_page_margin_shift(self) -> None:
        def heading(order: int, text: str, left: int) -> PreparedElement:
            return PreparedElement(
                kind="heading",
                order_index=order,
                text=text,
                heading_level=1,
                artifact_locator={"bbox": [left, 100, left + 300, 120]},
            )

        placed = s2_apply_heading_tree(
            [
                heading(1, "第六节 重要事项", 90),
                heading(2, "三、违规担保情况", 83),
                PreparedElement(kind="text", order_index=3, text="担保说明。"),
                # Same-level siblings straddle a PDF page/margin change.
                heading(4, "四、半年报审计情况", 146),
                PreparedElement(kind="text", order_index=5, text="审计说明。"),
                heading(6, "五、非标准审计意见", 146),
                PreparedElement(kind="text", order_index=7, text="意见说明。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["担保说明。"].heading_path,
            ["第六节 重要事项", "三、违规担保情况"],
        )
        self.assertEqual(
            by_text["审计说明。"].heading_path,
            ["第六节 重要事项", "四、半年报审计情况"],
        )
        self.assertEqual(
            by_text["意见说明。"].heading_path,
            ["第六节 重要事项", "五、非标准审计意见"],
        )

    def test_s2_same_family_continuity_can_return_to_promoted_chinese_root(
        self,
    ) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="合并资产负债表",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="一、公司基本情况",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="九、关联方关系及交易",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=4, text="关联方正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    text="十、资产负债表日后事项",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=6, text="期后事项正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=7,
                    text="十一、比较数据",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=8, text="比较数据正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=9,
                    text="十二、财务报表的批准",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=10, text="批准正文。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["期后事项正文。"].heading_path, ["十、资产负债表日后事项"]
        )
        self.assertEqual(by_text["比较数据正文。"].heading_path, ["十一、比较数据"])
        self.assertEqual(by_text["批准正文。"].heading_path, ["十二、财务报表的批准"])

    def test_s2_recovers_supplement_root_from_first_exact_page_banner(self) -> None:
        s1 = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "order_index": 1,
                    "heading_level": 1,
                    "text": "十二、财务报表的批准",
                },
                {"kind": "text", "order_index": 2, "text": "本财务报表已经批准。"},
                {
                    "kind": "heading",
                    "order_index": 3,
                    "heading_level": 1,
                    "text": "1. 非经常性损益明细表",
                },
                {"kind": "text", "order_index": 4, "text": "非经常性损益正文。"},
                {
                    "kind": "heading",
                    "order_index": 5,
                    "heading_level": 1,
                    "text": "2. 会计准则差异说明",
                },
                {"kind": "text", "order_index": 6, "text": "准则差异正文。"},
                {
                    "kind": "page_furniture",
                    "order_index": 7,
                    "page_no": 2,
                    "text": "未经审计财务报表补充资料",
                },
                {
                    "kind": "heading",
                    "order_index": 8,
                    "heading_level": 1,
                    "text": "3. 净资产收益率及每股收益",
                },
                {"kind": "text", "order_index": 9, "text": "收益率正文。"},
                {
                    "kind": "page_furniture",
                    "order_index": 10,
                    "page_no": 3,
                    "text": "未经审计财务报表补充资料",
                },
                {
                    "kind": "heading",
                    "order_index": 11,
                    "heading_level": 1,
                    "text": "4. 监管资本项目",
                },
                {"kind": "text", "order_index": 12, "text": "监管资本正文。"},
            ]
        )
        placed = s2_apply_heading_tree(s1.elements)

        self.assertEqual(s1.stats.recovered_section_furniture_headings, 1)
        by_text = {element.text: element for element in placed}
        root = "未经审计财务报表补充资料"
        self.assertEqual(
            by_text["非经常性损益正文。"].heading_path,
            [root, "1. 非经常性损益明细表"],
        )
        self.assertEqual(
            by_text["准则差异正文。"].heading_path,
            [root, "2. 会计准则差异说明"],
        )
        self.assertEqual(
            by_text["收益率正文。"].heading_path,
            [root, "3. 净资产收益率及每股收益"],
        )
        self.assertEqual(
            by_text["监管资本正文。"].heading_path,
            [root, "4. 监管资本项目"],
        )
        self.assertEqual(
            rules.exact_note_key_for_title(root),
            "supplementary_financial_information",
        )

    def test_s2_native_repeated_controlled_root_reopens_same_level(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    raw_kind="text",
                    order_index=1,
                    heading_level=1,
                    text="财务报表附注",
                ),
                PreparedElement(kind="text", order_index=2, text="第一页正文。"),
                PreparedElement(
                    kind="heading",
                    raw_kind="text",
                    order_index=3,
                    heading_level=1,
                    text="财务报表附注",
                ),
                PreparedElement(kind="text", order_index=4, text="第二页正文。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["第一页正文。"].heading_path,
            ["财务报表附注"],
        )
        self.assertEqual(
            by_text["第二页正文。"].heading_path,
            ["财务报表附注"],
        )

    def test_s2_same_family_continuity_closes_stale_bank_hotspot(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="8. 讨论与分析", heading_level=1
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="8.7 资本市场关注热点问题",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="热点问题五：扎实推进智能化风控",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=4, text="热点正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    text="9. 股本变动及主要股东持股情况",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=6, text="股本正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=7,
                    text="10. 董事、监事及高级管理人员情况",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=8, text="人员正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=9,
                    text="11. 公司治理报告",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=10, text="治理正文。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["热点正文。"].heading_path,
            [
                "8. 讨论与分析",
                "8.7 资本市场关注热点问题",
                "热点问题五：扎实推进智能化风控",
            ],
        )
        self.assertEqual(
            by_text["股本正文。"].heading_path,
            ["9. 股本变动及主要股东持股情况"],
        )
        self.assertEqual(
            by_text["人员正文。"].heading_path,
            ["10. 董事、监事及高级管理人员情况"],
        )
        self.assertEqual(by_text["治理正文。"].heading_path, ["11. 公司治理报告"])

    def test_s2_proven_single_dot_history_reopens_controlled_bank_chapter(
        self,
    ) -> None:
        def heading(order: int, text: str, left: int) -> PreparedElement:
            return PreparedElement(
                kind="heading",
                order_index=order,
                text=text,
                heading_level=1,
                artifact_locator={"bbox": [left, 100, 800, 130]},
            )

        placed = s2_apply_heading_tree(
            [
                heading(1, "5. 主章五", 144),
                PreparedElement(kind="text", order_index=2, text="五。"),
                heading(3, "6. 主章六", 144),
                PreparedElement(kind="text", order_index=4, text="六。"),
                heading(5, "7. 主章七", 144),
                PreparedElement(kind="text", order_index=6, text="七。"),
                heading(7, "8. 讨论与分析", 144),
                heading(8, "8.7 资本市场关注热点问题", 181),
                heading(9, "热点问题五：扎实推进智能化风控", 200),
                heading(10, "一、局部专题条目", 186),
                PreparedElement(kind="text", order_index=11, text="局部正文。"),
                heading(12, "9. 股本变动及主要股东持股情况", 144),
                PreparedElement(kind="text", order_index=13, text="股本正文。"),
            ]
        )

        body = next(element for element in placed if element.text == "股本正文。")
        self.assertEqual(body.heading_path, ["9. 股本变动及主要股东持股情况"])

    def test_s2_closed_single_dot_history_cannot_reopen_under_uncontrolled_title(
        self,
    ) -> None:
        def heading(order: int, text: str) -> PreparedElement:
            return PreparedElement(
                kind="heading",
                order_index=order,
                text=text,
                heading_level=1,
                artifact_locator={"bbox": [100, 100, 600, 130]},
            )

        placed = s2_apply_heading_tree(
            [
                heading(1, "第一节 旧父"),
                heading(2, "1. 旧子一"),
                PreparedElement(kind="text", order_index=3, text="一。"),
                heading(4, "2. 旧子二"),
                PreparedElement(kind="text", order_index=5, text="二。"),
                heading(6, "3. 旧子三"),
                PreparedElement(kind="text", order_index=7, text="三。"),
                heading(8, "管理层讨论与分析"),
                PreparedElement(kind="text", order_index=9, text="新根正文。"),
                heading(10, "4. 新根子四"),
                PreparedElement(kind="text", order_index=11, text="新子正文。"),
            ]
        )

        child = next(element for element in placed if element.text == "新子正文。")
        self.assertNotIn("第一节 旧父", child.heading_path)
        self.assertIn("管理层讨论与分析", child.heading_path)

    def test_s2_local_latin_roman_numeric_families_do_not_leak_siblings(
        self,
    ) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="61. 关联方关系及交易",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="(a) 主要关联方概况",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=3,
                    text="(i) 主要股东及其母公司",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=4, text="股东概况正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    text="(b) 重大关联方交易款项余额",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=6,
                    text="(i) 与本集团关联公司的交易余额",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading", order_index=7, text="(1) 拆出资金", heading_level=1
                ),
                PreparedElement(kind="text", order_index=8, text="拆出资金正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=9,
                    text="(2) 贷款和垫款",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=10, text="贷款正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=11,
                    text="(ii) 与本行关联公司的交易余额",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=12, text="本行余额正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=13,
                    text="(c) 重大关联方交易发生额",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=14, text="发生额正文。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["股东概况正文。"].structural_path,
            [
                "61. 关联方关系及交易",
                "(a) 主要关联方概况",
                "(i) 主要股东及其母公司",
            ],
        )
        self.assertEqual(
            by_text["拆出资金正文。"].structural_path,
            [
                "61. 关联方关系及交易",
                "(b) 重大关联方交易款项余额",
                "(i) 与本集团关联公司的交易余额",
                "(1) 拆出资金",
            ],
        )
        self.assertEqual(
            by_text["贷款正文。"].structural_path[-2:],
            ["(i) 与本集团关联公司的交易余额", "(2) 贷款和垫款"],
        )
        self.assertEqual(
            by_text["本行余额正文。"].structural_path,
            [
                "61. 关联方关系及交易",
                "(b) 重大关联方交易款项余额",
                "(ii) 与本行关联公司的交易余额",
            ],
        )
        self.assertEqual(
            by_text["发生额正文。"].structural_path,
            ["61. 关联方关系及交易", "(c) 重大关联方交易发生额"],
        )

    def test_s2_ambiguous_i_continues_confirmed_latin_h_family(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading", order_index=1, text="3. 附注", heading_level=1
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="(h) 现金流量表补充资料",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=3, text="补充资料正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=4,
                    text="(i) 现金及现金等价物",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=5, text="现金正文。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["现金正文。"].structural_path,
            ["3. 附注", "(i) 现金及现金等价物"],
        )
        self.assertNotIn(
            "(h) 现金流量表补充资料", by_text["现金正文。"].structural_path
        )

    def test_s2_local_families_survive_parent_continuations_and_table_captions(
        self,
    ) -> None:
        def heading(order: int, text: str) -> PreparedElement:
            return PreparedElement(
                kind="heading",
                order_index=order,
                text=text,
                heading_level=1,
                artifact_locator={"bbox": [90, 100, 500, 120]},
            )

        def table(order: int, caption: str) -> PreparedElement:
            return PreparedElement(
                kind="table",
                order_index=order,
                table={"headers": ["项目"], "rows": [[str(order)]]},
                table_caption=[caption],
                artifact_locator={"bbox": [184, 140, 900, 300]},
            )

        placed = s2_apply_heading_tree(
            [
                heading(1, "61. 关联方关系及交易"),
                heading(2, "(a) 主要关联方概况"),
                heading(3, "(b) 重大关联方交易款项余额"),
                heading(4, "(i) 与本集团关联公司的交易余额"),
                heading(5, "(1) 拆出资金"),
                heading(6, "(2) 贷款和垫款"),
                heading(7, "(b) 重大关联方交易款项余额(续)"),
                heading(8, "(i) 与本集团关联公司的交易余额(续)"),
                table(9, "(3) 金融投资"),
                table(10, "(4) 同业和其他金融机构存放款项"),
                heading(11, "(b) 重大关联方交易款项余额(续)"),
                heading(12, "(ii) 与本行关联公司的交易余额"),
                table(13, "(1) 拆出资金"),
                heading(14, "(b) 重大关联方交易款项余额(续)"),
                PreparedElement(
                    kind="text",
                    order_index=15,
                    text="(ii) 与本行关联公司的交易余额(续)",
                    artifact_locator={"bbox": [90, 100, 500, 120]},
                ),
                table(16, "(2) 贷款和垫款"),
                PreparedElement(
                    kind="text",
                    order_index=17,
                    text="(3) 本段是需要保留的实质性说明。",
                    artifact_locator={"bbox": [137, 100, 700, 140]},
                ),
            ]
        )

        by_order = {element.order_index: element for element in placed}
        common = [
            "61. 关联方关系及交易",
            "(b) 重大关联方交易款项余额",
            "(i) 与本集团关联公司的交易余额",
        ]
        self.assertEqual(by_order[9].structural_path, [*common, "(3) 金融投资"])
        self.assertEqual(
            by_order[10].structural_path,
            [*common, "(4) 同业和其他金融机构存放款项"],
        )
        roman_two = [
            "61. 关联方关系及交易",
            "(b) 重大关联方交易款项余额",
            "(ii) 与本行关联公司的交易余额",
        ]
        self.assertEqual(by_order[13].structural_path, [*roman_two, "(1) 拆出资金"])
        self.assertEqual(
            by_order[16].structural_path,
            [*roman_two, "(2) 贷款和垫款"],
        )
        self.assertEqual(by_order[17].structural_path, roman_two)
        self.assertEqual(by_order[17].text, "(3) 本段是需要保留的实质性说明。")

    def test_s2_decimal_outline_and_unnumbered_major_roots(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="管理层讨论与分析",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="3.8 发展战略实施情况",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=3, text="战略说明。"),
                PreparedElement(
                    kind="heading",
                    order_index=4,
                    text="3.9 经营中关注的重点问题",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=5,
                    text="3.9.1 关于净利息收益率",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=6, text="息差说明。"),
                PreparedElement(
                    kind="heading",
                    order_index=7,
                    text="财务报告",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=8, text="报告说明。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["战略说明。"].heading_path,
            ["管理层讨论与分析", "3.8 发展战略实施情况"],
        )
        self.assertEqual(
            by_text["息差说明。"].heading_path,
            [
                "管理层讨论与分析",
                "3.9 经营中关注的重点问题",
                "3.9.1 关于净利息收益率",
            ],
        )
        self.assertEqual(by_text["报告说明。"].heading_path, ["财务报告"])

    def test_s2_decimal_outline_stays_below_matching_chinese_chapter(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="二、公司基本情况",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    text="2.1 公司简介",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=3, text="公司正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=4,
                    text="2.2 主要业务简介",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=5, text="业务正文。"),
                PreparedElement(
                    kind="heading",
                    order_index=6,
                    text="三、主要会计数据和财务指标",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="heading",
                    order_index=7,
                    text="3.1 关键指标",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=8, text="指标正文。"),
            ]
        )

        by_text = {element.text: element for element in placed}
        self.assertEqual(
            by_text["公司正文。"].heading_path,
            ["二、公司基本情况", "2.1 公司简介"],
        )
        self.assertEqual(
            by_text["业务正文。"].heading_path,
            ["二、公司基本情况", "2.2 主要业务简介"],
        )
        self.assertEqual(
            by_text["指标正文。"].heading_path,
            ["三、主要会计数据和财务指标", "3.1 关键指标"],
        )

    def test_s2_decimal_amount_is_not_an_outline_heading(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="管理层讨论与分析",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="text", order_index=2, text="1.5亿元投资已经完成。"
                ),
            ]
        )

        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0].text, "1.5亿元投资已经完成。")
        self.assertEqual(placed[0].heading_path, ["管理层讨论与分析"])

    def test_s2_spaced_decimal_amount_and_percent_are_not_outline_headings(
        self,
    ) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="管理层讨论与分析",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=2, text="1.5 亿元投资。"),
                PreparedElement(kind="text", order_index=3, text="3.2 %的增长率。"),
            ]
        )

        self.assertEqual(len(placed), 2)
        self.assertTrue(
            all(item.heading_path == ["管理层讨论与分析"] for item in placed)
        )

    def test_s2_space_delimited_enumeration_and_amount_stay_body_text(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="管理层讨论与分析",
                    heading_level=1,
                ),
                PreparedElement(
                    kind="text", order_index=2, text="一 是持续加强研发投入"
                ),
                PreparedElement(kind="text", order_index=3, text="1 亿元投资额"),
                PreparedElement(kind="text", order_index=4, text="一 持续加强研发投入"),
                PreparedElement(
                    kind="text", order_index=5, text="一 公司持续加强研发投入"
                ),
                PreparedElement(kind="text", order_index=6, text="二 项目建设稳步推进"),
                PreparedElement(kind="text", order_index=7, text="1 持续加强研发投入"),
                PreparedElement(kind="text", order_index=8, text="2 项目建设稳步推进"),
                PreparedElement(
                    kind="text",
                    order_index=9,
                    text="1 所得税费用增加导致净利润下降",
                ),
            ]
        )

        self.assertEqual(
            [item.text for item in placed],
            [
                "一 是持续加强研发投入",
                "1 亿元投资额",
                "一 持续加强研发投入",
                "一 公司持续加强研发投入",
                "二 项目建设稳步推进",
                "1 持续加强研发投入",
                "2 项目建设稳步推进",
                "1 所得税费用增加导致净利润下降",
            ],
        )
        self.assertTrue(
            all(item.heading_path == ["管理层讨论与分析"] for item in placed)
        )

        controlled = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="第二节 公司基本情况",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=2, text="5 公司债券情况"),
                PreparedElement(kind="text", order_index=3, text="不适用。"),
            ]
        )
        self.assertEqual(controlled[-1].heading_path[-1], "5 公司债券情况")

    def test_s2_promotes_only_exact_corpus_numeric_space_note_aliases(self) -> None:
        cases = {
            "3 净资产收益率及每股收益": "return_on_equity",
            "11 长期股权投资及共同经营": "long_term_equity_investment",
            "53 基本每股收益和稀释每股收益的计算过程": "earnings_per_share",
            "5 同一控制下和非同一控制下企业合并的会计处理方法": ("accounting_policies"),
            "1 债券偿还": "bonds_section",
        }

        for title, expected_key in cases.items():
            with self.subTest(title=title):
                self.assertEqual(rules.exact_note_key_for_title(title), expected_key)
                placed = s2_apply_heading_tree(
                    [
                        PreparedElement(
                            kind="heading",
                            order_index=1,
                            text="财务报表附注",
                            heading_level=1,
                        ),
                        PreparedElement(kind="text", order_index=2, text=title),
                        PreparedElement(
                            kind="text", order_index=3, text="本节披露正文。"
                        ),
                    ]
                )
                self.assertEqual(placed[-1].heading_path[-1], title)

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

    def test_full_s1_s7_short_other_doc_collapses_to_document_unit(self) -> None:
        long_items = "\n".join(f"{idx}、" + "经营情况说明" * 12 for idx in range(1, 4))

        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": long_items,
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 2,
                        "table_caption": ["收入表"],
                        "table": {"headers": ["项目"], "rows": [["收入"]]},
                    },
                ]
            },
            filing_type="other",
        )

        self.assertEqual([unit.payload_kind for unit in units], ["mixed"])
        self.assertEqual(units[0].payload["semantic_type"], "document")
        self.assertEqual(
            [part["kind"] for part in units[0].payload["parts"]],
            ["text", "table"],
        )
        self.assertEqual(units[0].payload["parts"][0]["text"], long_items)
        self.assertEqual(units[0].payload["parts"][1]["caption"], ["收入表"])
        self.assertEqual(stats.collapsed_documents, 1)

    def test_full_s1_s7_annual_section_groups_text_and_tables(self) -> None:
        # 研发投入 shape (round3 P0#1 长年报 clause): intro text + two tables
        # under one business heading must be ONE unit, not three slices. The
        # oversized sibling forces grouping below the 节 level.
        filler = "业务概况说明。" * 1300  # > SECTION_GROUP_MAX_CHARS
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第三节 管理层讨论与分析",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "一、业务概况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": filler,
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 4,
                        "heading_level": 2,
                        "text": "二、研发投入",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 5,
                        "text": "报告期内公司持续加大研发投入。",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 6,
                        "table_caption": ["费用化研发投入"],
                        "table": {
                            "headers": ["项目", "金额"],
                            "rows": [["费用化", "100"]],
                        },
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 7,
                        "table_caption": ["研发人员情况"],
                        "table": {
                            "headers": ["类别", "人数"],
                            "rows": [["硕士", "30"]],
                        },
                    },
                ]
            },
            filing_type="annual_report",
        )

        by_path = {tuple(unit.heading_path): unit for unit in units}
        overview = by_path[("第三节 管理层讨论与分析", "一、业务概况")]
        self.assertEqual(overview.payload_kind, "text")  # single member: untouched
        rnd = by_path[("第三节 管理层讨论与分析", "二、研发投入")]
        self.assertEqual(rnd.payload_kind, "mixed")
        self.assertEqual(rnd.payload["semantic_type"], "section")
        self.assertEqual(
            [part["kind"] for part in rnd.payload["parts"]],
            ["text", "table", "table"],
        )
        self.assertEqual(rnd.title, "二、研发投入")
        self.assertEqual(stats.grouped_section_units, 1)

    def test_mixed_section_aggregates_part_semantic_keys(self) -> None:
        # Codex round4 P1#1: grouping must not swallow recall keys — parts
        # keep their own semantic_key and the unit exposes the full set.
        filler = "业务概况说明。" * 1300
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第三节 管理层讨论与分析",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "一、业务概况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": filler,
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 4,
                        "heading_level": 2,
                        "text": "二、主营业务分析",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 5,
                        "text": "报告期内经营情况如下。",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 6,
                        "table_caption": ["营业收入构成"],
                        "table": {
                            "headers": ["项目", "金额"],
                            "rows": [["主营", "100"]],
                        },
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 7,
                        "table_caption": ["存货分类构成"],
                        "table": {
                            "headers": ["类别", "金额"],
                            "rows": [["原材料", "10"]],
                        },
                    },
                ]
            },
            filing_type="annual_report",
        )

        by_path = {tuple(unit.heading_path): unit for unit in units}
        section = by_path[("第三节 管理层讨论与分析", "二、主营业务分析")]
        self.assertEqual(section.payload_kind, "mixed")
        # Payload stays pure: no rules-derived keys inside parts (U2).
        self.assertTrue(
            all("semantic_key" not in part for part in section.payload["parts"])
        )
        # Rule keys and note-vocabulary keys coexist on the unit.
        self.assertEqual(
            section.semantic_keys,
            # Ancestor inheritance (round13): the chapter key joins the set.
            [
                "business_review",
                "inventory",
                "inventory_breakdown",
                "main_business_analysis",
                "revenue_and_cost",
                "revenue_breakdown",
            ],
        )

    def test_accounting_policy_children_group_at_exact_subject_parent(self) -> None:
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
                        "text": "五、重要会计政策及会计估计",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 2,
                        "text": "17、存货",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 4,
                        "heading_level": 2,
                        "text": "1. 存货的分类",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 5,
                        "text": "存货包括原材料和产成品。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 6,
                        "heading_level": 2,
                        "text": "2. 发出存货的计价方法",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 7,
                        "text": "发出存货采用加权平均法。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 8,
                        "heading_level": 2,
                        "text": "3. 存货可变现净值的确定依据",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 9,
                        "text": "可变现净值按预计售价减相关成本确定。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 10,
                        "heading_level": 2,
                        "text": "18、持有待售资产",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 11,
                        "text": "符合条件的资产分类为持有待售。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 2)
        inventory = units[0]
        self.assertEqual(inventory.payload_kind, "mixed")
        self.assertEqual(inventory.title, "17、存货")
        self.assertEqual(
            inventory.heading_path,
            ["第八节 财务报告", "五、重要会计政策及会计估计", "17、存货"],
        )
        self.assertEqual(
            [part["local_heading"] for part in inventory.payload["parts"]],
            [
                ["1. 存货的分类"],
                ["2. 发出存货的计价方法"],
                ["3. 存货可变现净值的确定依据"],
            ],
        )
        self.assertTrue(
            {"accounting_policies", "inventory", "inventory_breakdown"}
            <= set(inventory.semantic_keys or [])
        )
        self.assertEqual(units[1].title, "18、持有待售资产")

    def test_accounting_policy_hidden_subject_projects_into_public_path(self) -> None:
        parent = [
            "财务报告",
            "2025年度财务报表附注",
            "附注说明",
            "二 主要会计政策和会计估计",
            "(11) 存货",
        ]
        units = [
            UnitDraft(
                payload_kind="text",
                payload={"text": "采用永续盘存制。"},
                source_order=1,
                heading_path=parent[:4],
                structural_path=[*parent, "(d) 盘存制度"],
                title="(d) 盘存制度",
            ),
            UnitDraft(
                payload_kind="text",
                payload={"text": "低值易耗品一次摊销。"},
                source_order=2,
                heading_path=parent[:4],
                structural_path=[*parent, "(e) 低值易耗品的摊销方法"],
                title="(e) 低值易耗品的摊销方法",
            ),
        ]

        grouped = s8_group_semantic_units(
            units,
            filing_type="annual_report",
            stats=BuildStats(),
        )

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].payload_kind, "mixed")
        self.assertEqual(grouped[0].title, "(11) 存货")
        self.assertEqual(
            grouped[0].heading_path,
            ["财务报告", "2025年度财务报表附注", "附注说明", "(11) 存货"],
        )
        self.assertEqual(
            [part["local_heading"] for part in grouped[0].payload["parts"]],
            [
                ["(d) 盘存制度"],
                ["(e) 低值易耗品的摊销方法"],
            ],
        )

    def test_section_part_preserves_title_when_inherited_path_is_stale(self) -> None:
        member = UnitDraft(
            payload_kind="text",
            payload={"text": "采用成本法核算对子公司的长期股权投资。"},
            source_order=3,
            heading_path=["第八节 财务报告", "旧页眉"],
            structural_path=["第八节 财务报告", "旧页眉"],
            title="16.3.1 按成本法核算的长期股权投资",
        )

        part = _unit_part(
            member,
            include_heading=False,
            relative_to=[
                "第八节 财务报告",
                "五、重要会计政策及会计估计",
                "16 长期股权投资",
            ],
        )

        self.assertEqual(
            part["local_heading"],
            ["16.3.1 按成本法核算的长期股权投资"],
        )

    def test_section_group_local_headings_tolerate_source_whitespace_drift(
        self,
    ) -> None:
        compact_root = [
            "第八节 财务报告",
            "(三)重要会计政策和会计估计",
            "16、长期股权投资",
        ]
        spaced_root = [
            "第八节 财务报告",
            "(三) 重要会计政策和会计估计",
            "16、长期股权投资",
        ]
        paths = [
            [*compact_root, "16.1 共同控制、重要影响的判断标准"],
            [*spaced_root, "16.2 初始投资成本的确定"],
            [
                *spaced_root,
                "16.3 后续计量及损益确认方法",
                "16.3.1 按成本法核算的长期股权投资",
            ],
            [
                *spaced_root,
                "16.3 后续计量及损益确认方法",
                "16.3.2 按权益法核算的长期股权投资",
            ],
            [*spaced_root, "16.4 长期股权投资处置"],
        ]
        members = [
            UnitDraft(
                payload_kind="text",
                payload={"text": f"长期股权投资政策正文 {index}。"},
                source_order=index,
                heading_path=path[:4],
                structural_path=path,
                title=path[-1],
            )
            for index, path in enumerate(paths, start=1)
        ]

        grouped = s8_group_semantic_units(
            members,
            filing_type="semiannual_report",
            stats=BuildStats(),
        )

        self.assertEqual(len(grouped), 1)
        self.assertEqual(
            [part["local_heading"] for part in grouped[0].payload["parts"]],
            [
                ["16.1 共同控制、重要影响的判断标准"],
                ["16.2 初始投资成本的确定"],
                [
                    "16.3 后续计量及损益确认方法",
                    "16.3.1 按成本法核算的长期股权投资",
                ],
                [
                    "16.3 后续计量及损益确认方法",
                    "16.3.2 按权益法核算的长期股权投资",
                ],
                ["16.4 长期股权投资处置"],
            ],
        )

    def test_hidden_leaf_group_keeps_displaced_controlled_parent_local(self) -> None:
        parent = [
            "第九节 财务报告",
            "万科企业股份有限公司财务报表附注(除特别注明外，金额单位为人民币元)",
            "三 公司重要会计政策、会计估计",
            "37 主要会计政策和会计估计变更",
        ]
        leaf = "(1) 会计政策变更的内容及原因"
        members = [
            UnitDraft(
                payload_kind="text",
                payload={"text": "本期变更会计政策。"},
                source_order=1,
                heading_path=parent,
                structural_path=[*parent, leaf],
                title=leaf,
            ),
            UnitDraft(
                payload_kind="table",
                payload={
                    "headers": ["项目", "影响金额"],
                    "rows": [["资产", "100"]],
                    "caption": [],
                },
                source_order=2,
                heading_path=parent,
                structural_path=[*parent, leaf],
                title="(i) 变更对合并资产负债表的影响",
            ),
        ]

        grouped = s8_group_semantic_units(
            members,
            filing_type="semiannual_report",
            stats=BuildStats(),
        )

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].heading_path[-1], leaf)
        local_headings = [
            part["local_heading"] for part in grouped[0].payload["parts"]
        ]
        self.assertTrue(
            all("37 主要会计政策和会计估计变更" in value for value in local_headings)
        )
        self.assertEqual(
            local_headings[1][-1], "(i) 变更对合并资产负债表的影响"
        )

    def test_collapsed_document_title_uses_registry_title(self) -> None:
        # Codex round4 P1#4: the in-PDF document-name line is often dropped as
        # cover prelude, so 第一章 must not become the document unit's title.
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

        self.assertEqual([unit.payload_kind for unit in units], ["mixed"])
        self.assertEqual(
            units[0].title, "贵州茅台：董事、高级管理人员考核和薪酬管理办法"
        )
        self.assertEqual(
            [part["heading_path"] for part in units[0].payload["parts"]],
            [["第一章 总则"], ["第二章 附则"]],
        )

    def test_s2_unnumbered_heading_nests_under_numbered_parent(self) -> None:
        # Codex round5: "与回购公司股份相关的会计处理方法" (MinerU heading,
        # level 2, no numbering) must stay inside 42、其他重要的会计政策, and
        # the following numbered sibling 43、 must return to the 42、 level.
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

        self.assertEqual(
            placed[0].heading_path,
            [
                "第八节 财务报告",
                "42、其他重要的会计政策和会计估计",
                "与回购公司股份相关的会计处理方法",
            ],
        )
        self.assertEqual(
            placed[1].heading_path,
            ["第八节 财务报告", "43、重要会计政策和会计估计变更"],
        )

    def test_s2_ordinal_continuity_repairs_mislevelled_heading(self) -> None:
        # Codex round5 / real 江海 annual p.187: the filing itself prints
        # 三、（市场风险） where （三）市场风险 was meant. Ordinal 3 continues
        # the open (一)(二) sequence, so it must not evict 十二、金融工具风险.
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
            ["第八节 财务报告", "十二、与金融工具相关的风险", "三、（市场风险）"],
        )

    def test_exact_controlled_parent_nests_digit_close_role_children(self) -> None:
        elements = [
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
                "heading_level": 1,
                "text": "2025年度财务报表附注",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 3,
                "heading_level": 1,
                "text": "十一、关联方及关联交易",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 4,
                "heading_level": 1,
                "text": "5、关联交易情况",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 5,
                "heading_level": 1,
                "text": "（3）关联租赁情况",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 6,
                "text": "3) 本公司作为出租方:",
            },
            {
                "kind": "table",
                "raw_kind": "table",
                "order_index": 7,
                "table": {
                    "headers": ["关联方", "租赁资产种类"],
                    "rows": [["甲公司", "房屋"]],
                },
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 8,
                "heading_level": 1,
                "text": "4) 本公司作为承租方：",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 9,
                "text": "无。",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 10,
                "heading_level": 1,
                "text": "（4）关联担保情况",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 11,
                "text": "无。",
            },
        ]

        s2 = s2_apply_heading_tree(s1_preprocess_elements(elements).elements)
        table = next(item for item in s2 if item.order_index == 7)
        self.assertEqual(
            table.structural_path[-2:],
            ["（3）关联租赁情况", "3) 本公司作为出租方:"],
        )
        tenant = next(item for item in s2 if item.order_index == 9)
        self.assertEqual(
            tenant.structural_path[-2:],
            ["（3）关联租赁情况", "4) 本公司作为承租方："],
        )
        guarantee = next(item for item in s2 if item.order_index == 11)
        self.assertNotIn("（3）关联租赁情况", guarantee.structural_path)

        units, _ = build_unit_drafts_s1_s7(
            {"elements": elements}, filing_type="annual_report"
        )
        lease_units = [unit for unit in units if unit.source_order in {7, 9}]
        self.assertEqual(len(lease_units), 2)
        self.assertTrue(
            all("lease_note" in (unit.semantic_keys or []) for unit in lease_units)
        )
        self.assertTrue(
            all(
                "（3）关联租赁情况" in unit.heading_path
                for unit in lease_units
            )
        )
        guarantee_unit = next(unit for unit in units if unit.source_order == 11)
        self.assertNotIn("lease_note", guarantee_unit.semantic_keys or [])

    def test_digit_close_child_does_not_nest_under_contains_only_parent(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    heading_level=1,
                    text="（3）关联租赁业务发展情况",
                ),
                PreparedElement(
                    kind="heading",
                    order_index=2,
                    heading_level=1,
                    text="1) 本公司作为出租方:",
                ),
                PreparedElement(kind="text", order_index=3, text="业务正常开展。"),
            ]
        )

        self.assertNotIn(
            "（3）关联租赁业务发展情况", placed[0].structural_path
        )

    def test_proven_parenthesized_run_nests_digit_close_policy_children(
        self,
    ) -> None:
        def heading(order: int, text: str, left: float = 126) -> PreparedElement:
            return PreparedElement(
                kind="heading",
                raw_kind="text",
                order_index=order,
                heading_level=1,
                text=text,
                artifact_locator={"bbox": [left, order * 20, left + 300, order * 20 + 10]},
            )

        def text(order: int, value: str, left: float = 126) -> PreparedElement:
            return PreparedElement(
                kind="text",
                raw_kind="text",
                order_index=order,
                text=value,
                artifact_locator={"bbox": [left, order * 20, left + 300, order * 20 + 10]},
            )

        placed = s2_apply_heading_tree(
            [
                heading(1, "第十节 财务报告", 80),
                heading(2, "2. 金融资产和金融负债", 100),
                heading(3, "(1) 金融资产的分类"),
                text(4, "分类原则正文。"),
                heading(5, "(2) 金融资产的初始计量"),
                heading(6, "1）第一种计量方法", 124),
                text(7, "第一种方法正文。"),
                text(8, "2）第二种计量方法", 124),
                text(9, "第二种方法正文。"),
                heading(10, "(3) 金融负债的后续计量方法"),
                heading(11, "1）摊余成本计量", 124),
                text(12, "摊余成本正文。"),
                heading(13, "(4) 金融资产和金融负债的终止确认"),
                text(14, "1）终止确认条件", 124),
                text(15, "终止确认正文。"),
                heading(16, "3. 金融资产转移", 100),
                text(17, "转移部分正文。"),
            ]
        )
        by_order = {element.order_index: element for element in placed}

        self.assertEqual(
            by_order[7].structural_path[-2:],
            ["(2) 金融资产的初始计量", "1）第一种计量方法"],
        )
        self.assertEqual(
            by_order[9].structural_path[-2:],
            ["(2) 金融资产的初始计量", "2）第二种计量方法"],
        )
        self.assertEqual(
            by_order[12].structural_path[-2:],
            ["(3) 金融负债的后续计量方法", "1）摊余成本计量"],
        )
        self.assertEqual(
            by_order[15].structural_path[-2:],
            ["(4) 金融资产和金融负债的终止确认", "1）终止确认条件"],
        )
        self.assertNotIn(
            "(4) 金融资产和金融负债的终止确认",
            by_order[17].structural_path,
        )
        self.assertEqual(by_order[17].structural_path[-1], "3. 金融资产转移")

    def test_unproven_parenthesized_run_does_not_nest_digit_close_child(
        self,
    ) -> None:
        def heading(
            order: int,
            text: str,
            *,
            left: float = 126,
            source_level: int = 1,
        ) -> PreparedElement:
            return PreparedElement(
                kind="heading",
                raw_kind="text",
                order_index=order,
                heading_level=source_level,
                text=text,
                artifact_locator={"bbox": [left, order * 20, left + 300, order * 20 + 10]},
            )

        variants = (
            [heading(1, "(1) 自由标题"), heading(2, "1）普通子项")],
            [
                heading(1, "(1) 自由标题"),
                heading(2, "(3) 跳号标题"),
                heading(3, "1）普通子项"),
            ],
            [
                heading(1, "(1) 自由标题"),
                heading(2, "(2) 左边距不同", left=160),
                heading(3, "1）普通子项", left=160),
            ],
            [
                heading(1, "(1) 自由标题"),
                heading(2, "(2) 来源层级不同", source_level=2),
                heading(3, "1）普通子项", source_level=2),
            ],
        )
        for elements in variants:
            with self.subTest(elements=[element.text for element in elements]):
                placed = s2_apply_heading_tree(
                    [
                        *elements,
                        PreparedElement(
                            kind="text",
                            raw_kind="text",
                            order_index=10,
                            text="普通子项正文。",
                        ),
                    ]
                )
                self.assertFalse(
                    any(
                        (title or "").startswith("(")
                        for title in placed[-1].structural_path[:-1]
                    )
                )

    def test_proven_sequence_recovers_one_overlong_digit_close_leaf(self) -> None:
        def item(
            order: int,
            kind: str,
            text: str,
            left: float,
            *,
            raw_kind: str = "text",
        ) -> PreparedElement:
            return PreparedElement(
                kind=kind,
                raw_kind=raw_kind,
                order_index=order,
                heading_level=1 if kind == "heading" else None,
                text=text,
                artifact_locator={
                    "bbox": [left, order * 20, left + 500, order * 20 + 16]
                },
            )

        long_leaf = (
            "2) Derivative investment for speculative purposes during the report period"
        )
        placed = s2_apply_heading_tree(
            [
                item(1, "heading", "(1) Financial assets", 81),
                item(2, "text", "Policy text.", 81),
                item(3, "heading", "(2) Derivative investments", 81),
                item(4, "heading", "1) Hedging derivatives", 81),
                item(5, "text", "Hedging policy text.", 81),
                item(6, "text", long_leaf, 89),
                item(7, "text", "Speculative derivative policy text.", 89),
            ]
        )

        self.assertEqual(
            placed[-1].structural_path[-2:],
            ["(2) Derivative investments", long_leaf],
        )
        self.assertEqual(placed[-1].title, long_leaf)

    def test_overlong_digit_close_recovery_fails_closed(self) -> None:
        def item(
            order: int,
            kind: str,
            text: str,
            left: float,
            *,
            raw_kind: str = "text",
            height: float = 16,
            with_bbox: bool = True,
        ) -> PreparedElement:
            locator = (
                {"bbox": [left, order * 20, left + 500, order * 20 + height]}
                if with_bbox
                else None
            )
            return PreparedElement(
                kind=kind,
                raw_kind=raw_kind,
                order_index=order,
                heading_level=1 if kind == "heading" else None,
                text=text,
                artifact_locator=locator,
            )

        valid = "2) " + "A" * 42
        variants = (
            {"text": "3) " + "A" * 42},
            {"text": valid + "。"},
            {"text": "2) key: " + "A" * 42},
            {"text": "2) " + "A" * 78},
            {"text": valid, "left": 90},
            {"text": valid, "height": 33},
            {"text": valid, "with_bbox": False},
            {"text": valid, "raw_kind": "list"},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                candidate = item(
                    6,
                    "text",
                    str(variant["text"]),
                    float(variant.get("left", 81)),
                    raw_kind=str(variant.get("raw_kind", "text")),
                    height=float(variant.get("height", 16)),
                    with_bbox=bool(variant.get("with_bbox", True)),
                )
                placed = s2_apply_heading_tree(
                    [
                        item(1, "heading", "(1) Financial assets", 81),
                        item(2, "text", "Policy text.", 81),
                        item(3, "heading", "(2) Derivative investments", 81),
                        item(4, "heading", "1) Hedging derivatives", 81),
                        item(5, "text", "Hedging policy text.", 81),
                        candidate,
                        item(7, "text", "Following evidence.", 81),
                    ]
                )
                self.assertEqual(
                    placed[-1].structural_path[-1], "1) Hedging derivatives"
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
            [
                "第八节 财务报告",
                "五、重要会计政策及会计估计",
                "17、存货",
                "1. 存货的分类",
            ],
        )
        self.assertEqual(
            by_text["发出存货采用加权平均法。"].heading_path,
            [
                "第八节 财务报告",
                "五、重要会计政策及会计估计",
                "17、存货",
                "2. 发出存货的计价方法",
            ],
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
            [
                "8、合营安排分类及共同经营会计处理方法",
                "2. 当公司为共同经营的合营方时，确认下列项目：",
            ],
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

    def test_boilerplate_guarantee_line_is_dropped_and_counted(self) -> None:
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
        self.assertEqual(units[0].payload["text"], "公司存在退市风险，请投资者注意。")
        self.assertEqual(stats.dropped_boilerplate_lines, 1)

    def test_blank_rows_dropped_and_merged_cells_reindexed(self) -> None:
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

        self.assertEqual(units[0].payload["rows"], [["收入", "100"], ["成本", "60"]])
        self.assertEqual(stats.dropped_blank_table_rows, 1)
        locator = units[0].artifact_locator or {}
        self.assertEqual(
            locator["merged_cells"], [{"row": 2, "col": 0, "rowspan": 1, "colspan": 2}]
        )

    def test_merged_cells_keep_full_grid_coordinates_across_continuations(
        self,
    ) -> None:
        stats = BuildStats()
        units = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=1,
                    page_no=1,
                    heading_path=["一、明细"],
                    table={
                        "headers": [],
                        "rows": [
                            ["项目", "金额"],
                            ["", ""],
                            ["甲", "1"],
                        ],
                        "merged_cells": [
                            {"row": 0, "col": 0, "rowspan": 1, "colspan": 2},
                            {"row": 2, "col": 0, "rowspan": 1, "colspan": 2},
                        ],
                    },
                ),
                PreparedElement(
                    kind="table",
                    order_index=2,
                    page_no=2,
                    heading_path=["一、明细"],
                    table={
                        "headers": [],
                        "rows": [["项目", "金额"], ["乙", "2"]],
                        "merged_cells": [
                            {"row": 0, "col": 0, "rowspan": 1, "colspan": 2},
                            {"row": 1, "col": 0, "rowspan": 1, "colspan": 2},
                        ],
                    },
                ),
            ],
            stats,
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload["headers"], ["项目", "金额"])
        self.assertEqual(units[0].payload["rows"], [["甲", "1"], ["乙", "2"]])
        self.assertEqual(
            (units[0].artifact_locator or {})["merged_cells"],
            [
                {"row": 0, "col": 0, "rowspan": 1, "colspan": 2},
                {"row": 1, "col": 0, "rowspan": 1, "colspan": 2},
                {"row": 2, "col": 0, "rowspan": 1, "colspan": 2},
            ],
        )

    def test_mixed_part_preserves_its_own_table_locator(self) -> None:
        locator = {
            "order_index": 9,
            "page_no": 3,
            "merged_cells": [
                {"row": 0, "col": 0, "rowspan": 1, "colspan": 2}
            ],
        }
        part = _unit_part(
            UnitDraft(
                payload_kind="table",
                payload={"headers": ["项目", "金额"], "rows": [["甲", "1"]]},
                source_order=9,
                artifact_locator=locator,
            ),
            include_heading=False,
        )

        self.assertEqual(part["artifact_locator"], locator)

    def test_long_term_investment_table_group_row_cannot_poison_note_tree(
        self,
    ) -> None:
        columns = 13
        elements = [
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 0,
                "page_no": 187,
                "heading_level": 1,
                "text": "第十节 财务报告",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 1,
                "page_no": 187,
                "heading_level": 2,
                "text": "七、合并财务报表项目注释",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 2,
                "page_no": 187,
                "heading_level": 2,
                "text": "18、长期股权投资",
            },
            {
                "kind": "table",
                "raw_kind": "table",
                "order_index": 3,
                "page_no": 187,
                "bbox": [82, 771, 897, 885],
                "table": {
                    "headers": [],
                    "rows": [
                        ["被投资单位"] * columns,
                        ["期初余额"] * columns,
                    ],
                },
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 4,
                "page_no": 187,
                "bbox": [90, 887, 186, 901],
                "heading_level": 1,
                "text": "一、合营企业",
            },
            {
                "kind": "page_furniture",
                "raw_kind": "page_number",
                "order_index": 5,
                "page_no": 187,
                "text": "187",
            },
            {
                "kind": "table",
                "raw_kind": "table",
                "order_index": 6,
                "page_no": 188,
                "bbox": [82, 83, 895, 904],
                "table": {
                    "headers": [],
                    "rows": [
                        ["二、联营企业"] * columns,
                        ["某联营企业", "1200"] + [""] * (columns - 2),
                    ],
                    "merged_cells": [
                        {"row": 0, "col": 0, "rowspan": 1, "colspan": columns}
                    ],
                },
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 7,
                "page_no": 189,
                "heading_level": 2,
                "text": "19、投资性房地产",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 8,
                "page_no": 189,
                "text": "投资性房地产采用成本模式计量。",
            },
        ]

        units, stats = build_unit_drafts_s1_s7(
            {"elements": elements},
            filing_type="annual_report",
        )

        self.assertEqual(stats.recovered_table_group_rows, 1)
        long_term = next(unit for unit in units if unit.source_order == 3)
        self.assertEqual(
            long_term.heading_path,
            ["第十节 财务报告", "七、合并财务报表项目注释", "18、长期股权投资"],
        )
        self.assertIn("consolidated_notes", long_term.semantic_keys or [])
        self.assertIn("long_term_equity_investment", long_term.semantic_keys or [])
        main_text = _main_text(long_term)
        self.assertLess(main_text.index("一、合营企业"), main_text.index("二、联营企业"))
        self.assertFalse(
            any("一、合营企业" in unit.heading_path for unit in units)
        )

        broken = [dict(element) for element in elements]
        broken[6] = {
            **broken[6],
            "table": {
                **broken[6]["table"],
                "merged_cells": [],
            },
        }
        s1 = s1_preprocess_elements(broken)
        self.assertEqual(s1.stats.recovered_table_group_rows, 0)
        self.assertTrue(
            any(
                element.kind == "heading" and element.text == "一、合营企业"
                for element in s1.elements
            )
        )

    def test_board_resolution_approval_style_proposals_group(self) -> None:
        # Codex round7 平安董事会决议: "一、审议通过了《…议案》" style + 表决行
        # must become one proposal unit each, not one blob.
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": (
                            "本行第十三届董事会第五次会议以书面传签方式召开。\n"
                            "本次会议审议通过了如下议案：\n"
                            "一、审议通过了《关于修订董事会专门委员会工作细则的议案》。\n"
                            "本议案同意票12票，反对票0票，弃权票0票。\n"
                            "二、审议通过了《关于修订商业行为和道德守则的议案》。\n"
                            "本议案同意票12票，反对票0票，弃权票0票。"
                        ),
                    },
                ]
            },
            filing_type="other",
        )

        proposals = [
            u
            for u in units
            if u.payload_kind == "mixed"
            and u.payload.get("semantic_type") == "meeting_proposal"
        ]
        self.assertEqual(len(proposals), 2)
        self.assertEqual(stats.grouped_proposal_units, 2)
        self.assertTrue(proposals[0].title.startswith("一、审议通过了"))
        self.assertIn("同意票12票", proposals[0].payload["parts"][0]["text"])
        self.assertTrue(proposals[1].title.startswith("二、审议通过了"))

    def test_table_caption_proposal_anchor_starts_new_unit(self) -> None:
        # Codex round7 招商股东会决议: MinerU attaches "8. 议案名称：…" as the
        # vote table's caption — it must start proposal 8, not join proposal 7.
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "二、议案审议情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "7. 议案名称：关于选举董事的议案\n审议结果：通过",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "table": {"headers": ["同意"], "rows": [["99%"]]},
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 4,
                        "table_caption": [
                            "8. 议案名称：关于选举监事的议案审议结果：通过"
                        ],
                        "table": {"headers": ["同意"], "rows": [["98%"]]},
                    },
                ]
            },
            filing_type="other",
        )

        proposals = [u for u in units if u.payload_kind == "mixed"]
        titles = [u.title for u in proposals]
        self.assertIn("7. 议案名称：关于选举董事的议案", titles)
        self.assertIn("8. 议案名称：关于选举监事的议案", titles)
        self.assertNotIn("8. 议案名称：关于选举监事的议案审议结果：通过", titles)
        self.assertEqual(
            [proposal.heading_path for proposal in proposals],
            [["二、议案审议情况"], ["二、议案审议情况"]],
        )

    def test_text_proposal_anchor_splits_same_line_result_from_title(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "二、议案审议情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "8. 议案名称：关于选举监事的议案审议结果：通过\n表决情况：",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "table": {"headers": ["同意"], "rows": [["98%"]]},
                    },
                ]
            },
            filing_type="other",
        )

        proposal = next(u for u in units if u.payload_kind == "mixed")
        self.assertEqual(proposal.title, "8. 议案名称：关于选举监事的议案")
        self.assertEqual(proposal.heading_path, ["二、议案审议情况"])
        self.assertEqual(
            proposal.payload["parts"][0]["text"], "审议结果：通过\n表决情况："
        )

    def test_proposal_anchor_preserves_existing_body_blank_lines(self) -> None:
        anchor = _proposal_anchor(
            UnitDraft(
                payload_kind="text",
                payload={
                    "text": "8. 议案名称：关于选举监事的议案审议结果：通过\n\n表决情况："
                },
                source_order=1,
                heading_path=["二、议案审议情况"],
            )
        )

        self.assertIsNotNone(anchor)
        _, title, parent_path, members = anchor
        self.assertEqual(title, "8. 议案名称：关于选举监事的议案")
        self.assertEqual(parent_path, ["二、议案审议情况"])
        self.assertEqual(members[0].payload["text"], "审议结果：通过\n\n表决情况：")

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

    def test_shredded_qa_table_is_flagged_needs_review(self) -> None:
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

        self.assertEqual(units[0].payload_kind, "table")
        self.assertEqual(units[0].quality_status, "needs_review")

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

    def test_unit_declaration_family_generalizes(self) -> None:
        # Round11 (user directive 泛化能力): the declaration is a pattern
        # FAMILY across filing formats; substantive sentences never match.
        strip = [
            "单位：元",
            "金额单位：人民币元",
            "财务附注中报表的单位为：元",
            "货币单位：万元",
            "币种：人民币",
            "除特别注明外，本财务报表附注均以人民币元列示。",
            "本报告中如无特殊说明，货币单位均为人民币元。",
        ]
        keep = [
            "公司记账本位币为人民币。",
            "境外子公司以美元为记账本位币，折算方法见会计政策。",
            "本报告中如无特殊说明，均指合并口径的经营数据及相关分析。",
        ]
        for line in strip:
            self.assertTrue(rules.is_unit_declaration_line(line), line)
        for line in keep:
            self.assertFalse(rules.is_unit_declaration_line(line), line)

    def test_unit_caption_does_not_replace_structural_table_title(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "6. 存放同业和其他金融机构款项",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "(b) 损失准备变动情况",
                    },
                    {
                        "kind": "table",
                        "order_index": 3,
                        "table_caption": ["单位：人民币百万元"],
                        "table": {
                            "headers": ["项目", "2025年"],
                            "rows": [["年末余额", "830"]],
                        },
                    },
                ]
            },
            filing_type="annual_report",
        )

        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertEqual(table.title, "(b) 损失准备变动情况")
        self.assertEqual(table.payload["caption"], ["单位：人民币百万元"])
        self.assertEqual(table.payload["unit"], "人民币百万元")

    def test_amount_unit_declaration_variant_is_stripped(self) -> None:
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
        self.assertEqual(units[0].payload["text"], "应收账款期末余额如下。")
        self.assertEqual(stats.dropped_unit_declarations, 1)

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

    def test_event_keys_from_document_title_union_into_all_units(self) -> None:
        # Round12 (研究落地): 事件键是独立 facet，从公告标题派生并入全部单元。
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": "回购方案实施完毕，累计回购股份 1,200 万股。" * 40,
                    },
                ]
            },
            filing_type="other",
            document_title="贵州茅台：关于回购股份实施结果暨股份变动的公告",
        )

        for unit in units:
            self.assertIn("share_buyback_event", unit.semantic_keys or [])

    def test_event_keys_precise_on_composite_titles(self) -> None:
        keys = rules.event_keys_for_document_title(
            "江海股份：关于部分董事、高级管理人员减持股份预披露的公告"
        )
        self.assertEqual(set(keys), {"holding_decrease"})
        self.assertEqual(rules.event_keys_for_document_title("2025年年度报告"), ())

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

    def test_full_s1_s7_section_grouping_uses_size_as_hard_cap(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第一节 经营情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "经营情况说明。" * 1000,
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "table": {
                            "headers": ["项目", "金额"],
                            "rows": [["收入" * 400, "100" * 400]],
                        },
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual([unit.payload_kind for unit in units], ["text", "table"])
        self.assertGreater(
            len(units[0].payload["text"])
            + sum(len(str(cell)) for row in units[1].payload["rows"] for cell in row),
            rules.SECTION_GROUP_MAX_CHARS,
        )

    def test_full_s1_s7_section_grouping_caps_repeated_parts(self) -> None:
        tables = [
            {
                "kind": "table",
                "order_index": index + 2,
                "table_caption": [f"分组{index}"],
                "table": {"headers": ["项目"], "rows": [[str(index)]]},
            }
            for index in range(rules.SECTION_GROUP_MAX_PARTS + 1)
        ]
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "商誉减值测试",
                    },
                    *tables,
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 2)
        self.assertEqual(len(units[0].payload["parts"]), rules.SECTION_GROUP_MAX_PARTS)
        self.assertEqual(units[1].payload_kind, "table")

    def test_full_s1_s7_section_grouping_does_not_cross_business_siblings(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第三节 管理层讨论与分析",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "一、业务概况",
                    },
                    {"kind": "text", "order_index": 3, "text": "业务说明。"},
                    {
                        "kind": "table",
                        "order_index": 4,
                        "table": {"headers": ["项目"], "rows": [["收入"]]},
                    },
                    {
                        "kind": "heading",
                        "order_index": 5,
                        "heading_level": 2,
                        "text": "二、研发投入",
                    },
                    {"kind": "text", "order_index": 6, "text": "研发说明。"},
                    {
                        "kind": "table",
                        "order_index": 7,
                        "table": {"headers": ["项目"], "rows": [["研发费用"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(
            [unit.heading_path for unit in units],
            [
                ["第三节 管理层讨论与分析", "一、业务概况"],
                ["第三节 管理层讨论与分析", "二、研发投入"],
            ],
        )
        self.assertEqual([unit.payload_kind for unit in units], ["mixed", "mixed"])

    def test_full_s1_s7_section_grouping_descends_to_controlled_note_boundary(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第十节 财务报告",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "十七、母公司财务报表主要项目注释",
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "heading_level": 3,
                        "text": "1、应收账款",
                    },
                    {"kind": "text", "order_index": 4, "text": "应收账款说明。"},
                    {
                        "kind": "table",
                        "order_index": 5,
                        "table": {"headers": ["账龄"], "rows": [["一年以内"]]},
                    },
                    {
                        "kind": "heading",
                        "order_index": 6,
                        "heading_level": 3,
                        "text": "2、其他应收款",
                    },
                    {"kind": "text", "order_index": 7, "text": "其他应收款说明。"},
                    {
                        "kind": "table",
                        "order_index": 8,
                        "table": {"headers": ["性质"], "rows": [["往来款"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(
            [unit.title for unit in units],
            ["1、应收账款", "2、其他应收款"],
        )
        self.assertEqual([unit.payload_kind for unit in units], ["mixed", "mixed"])

    def test_controlled_numbered_table_caption_reanchors_stale_sibling_path(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "text": "第十节 财务报告",
                        "heading_level": 1,
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "text": "七、合并财务报表项目注释",
                        "heading_level": 2,
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "text": "20、应付职工薪酬",
                        "heading_level": 3,
                    },
                    {"kind": "text", "order_index": 4, "text": "职工薪酬说明。"},
                    {
                        "kind": "table",
                        "order_index": 5,
                        "table_caption": ["21、应交税费"],
                        "table": {"headers": ["税种"], "rows": [["增值税"]]},
                    },
                    {"kind": "text", "order_index": 6, "text": "税费变动说明。"},
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 2)
        self.assertEqual(units[0].heading_path[-1], "20、应付职工薪酬")
        self.assertEqual(units[1].heading_path[-1], "21、应交税费")
        self.assertEqual(units[1].title, "21、应交税费")
        self.assertEqual(units[1].payload_kind, "mixed")
        self.assertIn("税费变动说明。", str(units[1].payload))

    def test_decimal_table_caption_preserves_chinese_notes_root(self) -> None:
        s1 = s1_preprocess_elements(
            [
                {
                    "kind": "heading",
                    "order_index": 1,
                    "text": "四、财务报表主要项目注释",
                    "heading_level": 1,
                },
                {
                    "kind": "heading",
                    "order_index": 2,
                    "text": "7. 金融投资",
                    "heading_level": 2,
                },
                {
                    "kind": "table",
                    "order_index": 3,
                    "table_caption": [
                        "7.1 以公允价值计量且其变动计入当期损益的金融投资"
                    ],
                    "table": {"headers": ["项目"], "rows": [["债券"]]},
                },
                {
                    "kind": "heading",
                    "order_index": 4,
                    "text": "7.2 以公允价值计量且其变动计入其他综合收益的金融投资",
                    "heading_level": 2,
                },
                {
                    "kind": "table",
                    "order_index": 5,
                    "table": {"headers": ["项目"], "rows": [["贷款"]]},
                },
                {
                    "kind": "heading",
                    "order_index": 6,
                    "text": "8. 长期股权投资",
                    "heading_level": 2,
                },
                {"kind": "text", "order_index": 7, "text": "投资说明。"},
            ]
        )
        placed = s2_apply_heading_tree(s1.elements)

        first = next(element for element in placed if element.order_index == 3)
        later = next(element for element in placed if element.order_index == 7)
        self.assertEqual(
            first.heading_path,
            [
                "四、财务报表主要项目注释",
                "7. 金融投资",
                "7.1 以公允价值计量且其变动计入当期损益的金融投资",
            ],
        )
        self.assertEqual(
            later.heading_path,
            ["四、财务报表主要项目注释", "8. 长期股权投资"],
        )
        self.assertNotIn(
            "7.2 以公允价值计量且其变动计入其他综合收益的金融投资",
            later.heading_path,
        )

    def test_numbered_controlled_caption_allows_bounded_label_suffix(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "text": "第四节 公司治理",
                        "heading_level": 1,
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "text": "二、股东和股东大会",
                        "heading_level": 2,
                    },
                    {"kind": "text", "order_index": 3, "text": "股东情况说明。"},
                    {
                        "kind": "table",
                        "order_index": 4,
                        "table_caption": ["三、股东大会情况简介"],
                        "table": {"headers": ["届次"], "rows": [["年度股东大会"]]},
                    },
                    {"kind": "text", "order_index": 5, "text": "股东大会情况说明。"},
                ]
            },
            filing_type="annual_report",
        )

        meeting = next(unit for unit in units if unit.title == "三、股东大会情况简介")
        self.assertEqual(meeting.title, "三、股东大会情况简介")
        self.assertEqual(
            meeting.heading_path,
            ["第四节 公司治理", "三、股东大会情况简介"],
        )
        self.assertIn("股东大会情况说明。", str(meeting.payload))

    def test_section_group_char_cap_counts_mixed_part_separator(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "text": "第一节 经营情况",
                        "heading_level": 1,
                    },
                    {"kind": "text", "order_index": 2, "text": "甲" * 4001},
                    {
                        "kind": "table",
                        "order_index": 3,
                        "table": {"headers": [], "rows": [["乙" * 3999]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual([unit.payload_kind for unit in units], ["text", "table"])

    def test_full_s1_s7_goodwill_asset_groups_are_distinct_instances(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第十节 财务报告",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "七、合并财务报表项目注释",
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "heading_level": 3,
                        "text": "1、商誉减值测试过程、关键参数及商誉减值损失的确认方法",
                    },
                    {
                        "kind": "heading",
                        "order_index": 4,
                        "heading_level": 4,
                        "text": "1）预计未来现金流量的现值",
                    },
                    {"kind": "text", "order_index": 5, "text": "第一资产组说明。"},
                    {
                        "kind": "table",
                        "order_index": 6,
                        "table_caption": ["第一资产组参数"],
                        "table": {"headers": ["参数"], "rows": [["增长率"]]},
                    },
                    {
                        "kind": "text",
                        "order_index": 7,
                        "text": "（2）为商誉减值测试的目的，公司对第二资产组的可收回金额单独进行估值并记录关键假设。",
                    },
                    {
                        "kind": "heading",
                        "order_index": 8,
                        "heading_level": 4,
                        "text": "1）预计未来现金流量的现值",
                    },
                    {"kind": "text", "order_index": 9, "text": "第二资产组说明。"},
                    {
                        "kind": "table",
                        "order_index": 10,
                        "table_caption": ["第二资产组参数"],
                        "table": {"headers": ["参数"], "rows": [["折现率"]]},
                    },
                ]
            },
            filing_type="annual_report",
        )

        goodwill = [
            unit
            for unit in units
            if unit.title == "1、商誉减值测试过程、关键参数及商誉减值损失的确认方法"
        ]
        self.assertEqual(len(goodwill), 2)
        self.assertEqual([len(unit.payload["parts"]) for unit in goodwill], [2, 2])
        self.assertTrue(goodwill[1].payload["parts"][0]["text"].startswith("（2）"))

    def test_full_s1_s7_meeting_proposals_group_with_votes(self) -> None:
        # 股东会决议 shape (round3 P0#1): 审议结果 + 表决表格 + 会议决定 are
        # ONE proposal unit, and the next proposal starting mid-text must not
        # stay attributed to the previous proposal's heading.
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "二、议案审议情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "3.议案名称：《关于聘请审计机构的议案》\n审议结果：通过\n表决情况：",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "table": {
                            "headers": ["股东类型", "同意"],
                            "rows": [["A股", "99%"]],
                        },
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "会议决定，聘请天健会计师事务所。\n4.议案名称：《关于利润分配方案的议案》\n审议结果：通过\n表决情况：",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 5,
                        "table": {
                            "headers": ["股东类型", "同意"],
                            "rows": [["A股", "98%"]],
                        },
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 6,
                        "text": "会议决定，通过利润分配方案。",
                    },
                ]
            },
            filing_type="other",
        )

        proposals = [unit for unit in units if unit.payload_kind == "mixed"]
        self.assertEqual(len(proposals), 2)
        self.assertEqual(stats.grouped_proposal_units, 2)
        first, second = proposals
        self.assertEqual(first.title, "3.议案名称：《关于聘请审计机构的议案》")
        self.assertEqual(first.payload["semantic_type"], "meeting_proposal")
        self.assertEqual(
            [part["kind"] for part in first.payload["parts"]],
            ["text", "table", "text"],
        )
        self.assertIn(
            "会议决定，聘请天健会计师事务所。", first.payload["parts"][2]["text"]
        )
        self.assertEqual(second.title, "4.议案名称：《关于利润分配方案的议案》")
        self.assertEqual(
            [part["kind"] for part in second.payload["parts"]],
            ["text", "table", "text"],
        )
        # Acceptance A: no text carries the NEXT proposal's title mid-body.
        for unit in units:
            for part in unit.payload.get("parts", []):
                text = str(part.get("text", ""))
                self.assertNotRegex(text, r"\n\d+\.议案名称：")

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
        self.assertEqual(stats.merged_announcement_header_units, 1)
        # The prefix is briefly anchored so it cannot become addressless, then
        # the late coalescer removes that carrier from the final unit set.
        self.assertEqual(stats.anchored_header_units, 1)
        searchable_text = "\n".join(
            str(unit.payload.get("text") or "") for unit in units
        )
        self.assertIn("公告编号：临 2026-026", searchable_text)
        paths = {part for unit in units for part in unit.heading_path}
        self.assertNotIn(rules.DOCUMENT_HEADER_ANCHOR, paths)

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
        self.assertIn((rules.DOCUMENT_HEADER_ANCHOR,), by_path2)

    def test_full_s1_s7_qa_units_never_join_section_groups(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "问询回复",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "问题1：公司毛利率为何下滑？\n回复：主要系原材料涨价。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 2,
                        "text": "一、数据说明",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "具体数据说明如下。",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 5,
                        "table": {
                            "headers": ["年度", "毛利率"],
                            "rows": [["2025", "30%"]],
                        },
                    },
                ]
            },
            filing_type="inquiry_reply",
        )

        kinds = [unit.payload_kind for unit in units]
        self.assertEqual(kinds, ["qa", "mixed"])
        self.assertEqual(
            [part["kind"] for part in units[1].payload["parts"]],
            ["text", "table"],
        )

    def test_full_s1_s7_table_qa_stays_before_following_text(self) -> None:
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
            ["table", "qa", "text"],
        )
        self.assertEqual(units[1].payload["question"], "产能如何？")
        self.assertEqual(units[2].payload["text"], "后续正文。")

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

        unlabelled = s4_build_qa_units("问：收入如何？\n公司收入稳定。", source=source)
        self.assertFalse(unlabelled.unstable)
        self.assertEqual(unlabelled.units[0].payload["answer"], "公司收入稳定。")

        native_source = UnitDraft(
            **{
                **source.__dict__,
                "artifact_locator": {"source": "native_text"},
            }
        )
        self.assertTrue(
            s4_build_qa_units(
                "问：收入如何？\n公司收入稳定。", source=native_source
            ).unstable
        )

    def test_s4_parses_bracketed_question_and_management_speaker_turns(
        self,
    ) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=10,
            heading_path=["问答环节实录"],
        )
        parsed = s4_build_qa_units(
            "【提问 1 中信证券分析师甲】：请介绍战略方\n"
            "向，谢谢。\n"
            "【王行长】：公司将坚持既定战略。\n"
            "【李副行长】：我补充一点，执行路径保持稳定。\n"
            "【问题 2 某机构分析师乙】：净息差怎么看？\n"
            "【王行长】：净息差总体保持韧性。",
            source=source,
            require_explicit_answer=True,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.ordinals, [1, 2])
        self.assertEqual(
            [unit.payload["question"] for unit in parsed.units],
            ["请介绍战略方向，谢谢。", "净息差怎么看？"],
        )
        self.assertEqual(
            [unit.payload["answer"] for unit in parsed.units],
            [
                "【王行长】：公司将坚持既定战略。\n"
                "【李副行长】：我补充一点，执行路径保持稳定。",
                "【王行长】：净息差总体保持韧性。",
            ],
        )

        legacy = s4_build_qa_units(
            "提问1：【分析师甲】：资产质量怎么看？\n"
            "【王行长】：资产质量总体稳定。",
            source=source,
        )
        self.assertFalse(legacy.unstable)
        self.assertEqual(len(legacy.units), 1)
        self.assertIn("资产质量怎么看？", legacy.units[0].payload["question"])
        self.assertEqual(
            legacy.units[0].payload["answer"],
            "【王行长】：资产质量总体稳定。",
        )

    def test_performance_briefing_press_release_keeps_numbered_headings(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 0,
                        "heading_level": 1,
                        "text": "二、本公司经营情况分析",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 2,
                        "text": "1. 动态调整资产配置，信贷规模稳定增长",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "本集团灵活配置资产，持续优化资产结构。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 2,
                        "text": "2. 负债结构不断优化，客户存款量增质优",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "客户存款保持稳定增长。",
                    },
                ]
            },
            filing_type="performance_briefing",
            document_title="2022年度业绩交流会新闻稿",
        )

        self.assertFalse(any(unit.payload_kind == "qa" for unit in units))
        self.assertEqual(
            [unit.title for unit in units],
            [
                "1. 动态调整资产配置，信贷规模稳定增长",
                "2. 负债结构不断优化，客户存款量增质优",
            ],
        )
        self.assertTrue(
            all("二、本公司经营情况分析" in unit.heading_path for unit in units)
        )

    def test_performance_briefing_official_form_keeps_unlabelled_qa_mode(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 0,
                        "table_caption": ["某公司投资者关系活动记录表"],
                        "table": {
                            "headers": [],
                            "rows": [
                                ["投资者关系活动类别", "√业绩说明会"],
                                [
                                    "投资者关系活动主要内容介绍",
                                    "1. 公司的业务结构及未来规划？\n公司持续优化业务结构。",
                                ],
                            ],
                        },
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "2. 公司的技术优势体现在哪些方面？",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "公司持续完善研发体系并加大研发投入。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "3. 公司未来的分红政策？",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "公司力争保持稳定持续的分红政策。",
                    },
                ]
            },
            filing_type="performance_briefing",
            document_title="某公司年度业绩说明会投资者关系活动记录表",
        )

        questions = [
            str(unit.payload["question"])
            for unit in units
            if unit.payload_kind == "qa"
        ]
        self.assertIn("公司的技术优势体现在哪些方面？", questions)
        self.assertIn("公司未来的分红政策？", questions)

    def test_s4_preserves_wrapped_questions_and_multi_digit_ordinals(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["三、主要交流问题"],
            artifact_locator={"source": "native_text"},
        )
        text = "\n".join(
            [
                "1、第一问跨行",
                "继续吗？ 答：第一答。",
                "2、第二问？",
                "答：第二答跨",
                "页完整。",
                "10、第十问不会拆成第零问吗？",
                "答：第十答。",
            ]
        )

        parsed = s4_build_qa_units(text, source=source)

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.ordinals, [1, 2, 10])
        self.assertEqual(len(parsed.units), 3)
        self.assertEqual(parsed.units[0].payload["question"], "第一问跨行继续吗？")
        self.assertEqual(
            parsed.units[2].payload["question"], "第十问不会拆成第零问吗？"
        )
        self.assertEqual(parsed.units[2].title, "第十问不会拆成第零问吗？")
        self.assertEqual(parsed.units[1].payload["answer"], "第二答跨页完整。")

        native_source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["三、主要交流问题"],
            artifact_locator={"source": "native_text"},
        )
        recovered = replace_text_units_with_qa_where_stable(
            [
                UnitDraft(
                    **{
                        **native_source.__dict__,
                        "payload": {
                            "text": "1、第一问？答：第一答。\n问：无序号问？答：无序号答。"
                        },
                    }
                )
            ]
        )
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].payload_kind, "text")
        self.assertEqual(recovered[0].quality_status, "needs_review")

    def test_s4_keeps_numbered_answer_subpoints_inside_answer(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
            heading_path=["二、主要交流问题"],
        )
        parsed = s4_build_qa_units(
            "1、合作如何？\n"
            "答：1、投资端推进。2、运营端推进。\n"
            "2、订单如何？\n"
            "答：订单稳定。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 2)
        self.assertEqual(parsed.ordinals, [1, 2])
        self.assertIn("1、投资端推进", parsed.units[0].payload["answer"])
        self.assertIn("2、运营端推进", parsed.units[0].payload["answer"])

    def test_s4_long_numbered_answer_fragment_does_not_become_question(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        answer_fragment = "3、" + "产品能力持续升级并投入市场。" * 10
        parsed = s4_build_qa_units(
            "1、产品进展如何？\n答：第一阶段完成。\n"
            f"{answer_fragment}\n清洁能力继续提升。\n"
            "2、订单如何？\n答：订单稳定。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(
            [unit.payload["question"] for unit in parsed.units],
            ["产品进展如何？", "订单如何？"],
        )
        self.assertIn(answer_fragment, parsed.units[0].payload["answer"])

    def test_s4_orphan_answer_prefix_resynchronizes_later_complete_qa(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": "答：上一问被表格截断。\n2、订单如何？\n答：订单稳定。"},
            source_order=1,
        )

        parsed = s4_build_qa_units(str(source.payload["text"]), source=source)
        self.assertFalse(parsed.unstable)
        self.assertTrue(parsed.leading_needs_review)
        self.assertEqual(parsed.leading_text, "答：上一问被表格截断。")
        self.assertEqual(len(parsed.units), 1)
        self.assertEqual(parsed.units[0].payload["question"], "订单如何？")

        recovered = replace_text_units_with_qa_where_stable([source])
        self.assertEqual([unit.payload_kind for unit in recovered], ["text", "qa"])
        self.assertEqual(recovered[0].quality_status, "needs_review")
        self.assertEqual(recovered[1].quality_status, "ok")

    def test_s4_duplicate_answer_quarantines_pair_and_resynchronizes_q3(
        self,
    ) -> None:
        text = (
            "问题1、第一问如何？\n"
            "答：第一答。\n"
            "答：未知问题的孤立回答。\n"
            "问题3、第三问如何？\n"
            "答：第三答。"
        )
        source = UnitDraft(
            payload_kind="text",
            payload={"text": text},
            source_order=1,
            intra_order=7,
        )

        parsed = s4_build_qa_units(
            text,
            source=source,
            require_explicit_answer=True,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.ordinals, [3])
        self.assertEqual(len(parsed.units), 1)
        self.assertEqual(parsed.units[0].payload["question"], "第三问如何？")
        self.assertEqual(parsed.units[0].payload["answer"], "第三答。")
        self.assertEqual(
            parsed.review_spans,
            [(0, "问题1、第一问如何？\n答：第一答。\n答：未知问题的孤立回答。")],
        )
        self.assertIsNone(parsed.leading_text)
        self.assertIsNone(parsed.trailing_text)

        recovered = replace_text_units_with_qa_where_stable(
            [source], require_explicit_answer=True
        )
        self.assertEqual(
            [unit.payload_kind for unit in recovered],
            ["text", "qa"],
        )
        self.assertEqual(
            [unit.quality_status for unit in recovered],
            ["needs_review", "ok"],
        )
        self.assertEqual([unit.intra_order for unit in recovered], [7, 8])
        self.assertEqual(
            recovered[0].payload["text"],
            "问题1、第一问如何？\n答：第一答。\n答：未知问题的孤立回答。",
        )
        self.assertEqual(
            recovered[1].payload["raw_text"],
            "问题3、第三问如何？\n答：第三答。",
        )

    def test_s4_duplicate_answer_preserves_committed_q1_and_quarantines_q2(
        self,
    ) -> None:
        text = (
            "问题1、第一问如何？\n答：第一答。\n"
            "问题2、第二问如何？\n答：第二答。\n"
            "答：未知问题的孤立回答。\n"
            "2、依法披露。\n"
            "问题3、第三问如何？\n答：第三答。"
        )
        source = UnitDraft(
            payload_kind="text",
            payload={"text": text},
            source_order=1,
            intra_order=3,
        )

        parsed = s4_build_qa_units(
            text,
            source=source,
            require_explicit_answer=True,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.ordinals, [1, 3])
        self.assertEqual(
            [unit.payload["question"] for unit in parsed.units],
            ["第一问如何？", "第三问如何？"],
        )
        self.assertEqual(
            parsed.review_spans,
            [
                (
                    1,
                    "问题2、第二问如何？\n答：第二答。\n"
                    "答：未知问题的孤立回答。\n2、依法披露。",
                )
            ],
        )
        recovered = replace_text_units_with_qa_where_stable(
            [source], require_explicit_answer=True
        )
        self.assertEqual(
            [unit.payload_kind for unit in recovered],
            ["qa", "text", "qa"],
        )
        self.assertEqual(
            [unit.quality_status for unit in recovered],
            ["ok", "needs_review", "ok"],
        )
        self.assertEqual([unit.intra_order for unit in recovered], [3, 4, 5])
        self.assertEqual(recovered[0].payload["answer"], "第一答。")
        self.assertEqual(recovered[2].payload["answer"], "第三答。")
        self.assertNotIn(
            "第二问如何？",
            "\n".join(
                str(unit.payload.get("raw_text") or "")
                for unit in recovered
                if unit.payload_kind == "qa"
            ),
        )

    def test_s4_short_topic_with_immediate_answer_remains_relaxed_qa(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "1、能源+AI\n答：业务持续推进。\n2、机器人\n答：已有产品落地。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(
            [unit.payload["question"] for unit in parsed.units],
            ["能源+AI", "机器人"],
        )

    def test_s4_keeps_q_prefixed_ordinals_and_product_codes_intact(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "Q2、公司新品规划？\n"
            "回复：嫩冻凝鲜2.0、-38℃超冻及P4实验室持续推进。"
            "Q3、后续投入如何？\n"
            "回复：V12产品继续升级。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.ordinals, [2, 3])
        self.assertEqual(len(parsed.units), 2)
        self.assertNotIn("\nQ", parsed.units[0].payload["answer"])
        self.assertIn("P4实验室", parsed.units[0].payload["answer"])
        self.assertIn("V12产品", parsed.units[1].payload["answer"])

    def test_s4_recovers_hard_wrapped_outer_question(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "2、近年来竞争激烈，请问公司在维持竞争力方\n"
            "面有哪些核心优势和创新策略？\n"
            "答：持续研发创新。\n"
            "3、未来研发方向如何？\n"
            "答：聚焦智慧能源。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 2)
        self.assertEqual(
            parsed.units[0].payload["question"],
            "近年来竞争激烈，请问公司在维持竞争力方面有哪些核心优势和创新策略？",
        )

    def test_s4_unlabelled_question_waits_for_terminal_boundary(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "4、公司技术领先，怎么看下一代技术，是否还能领先？韩国一家企业公\n"
            "布了后续规划，我们如何定义下一代技术？\n"
            "公司持续推进多条技术路线。\n"
            "5、海外进展如何？\n"
            "海外项目按计划推进。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 2)
        self.assertEqual(
            parsed.units[0].payload["question"],
            "公司技术领先，怎么看下一代技术，是否还能领先？韩国一家企业公布了后续规划，我们如何定义下一代技术？",
        )
        self.assertEqual(
            parsed.units[0].payload["answer"],
            "公司持续推进多条技术路线。",
        )

    def test_s4_drops_split_ask_marker_and_joins_wrapped_labelled_question(
        self,
    ) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "问1：第一问？\n管理层回答第一问。\n"
            "提\n"
            "问2：请问公司致力于打造哪些核心竞争力，是资产获取还是风险定\n"
            "价的平衡，还是产品创新？谢谢。\n"
            "管理层回答第二问。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 2)
        self.assertNotIn("提", parsed.units[0].payload["answer"])
        self.assertEqual(
            parsed.units[1].payload["question"],
            "请问公司致力于打造哪些核心竞争力，是资产获取还是风险定价的平衡，还是产品创新？谢谢。",
        )

        long_context = "行业背景复杂。" * 45
        split_three_ways = s4_build_qa_units(
            "1：【分析师甲】：第一问？\n【管理层】：第一答。\n"
            "提\n问\n"
            f"2：【分析师乙】：{long_context}公司致力于打造哪些核心竞争力，是资产获取还是风险定\n"
            "价的平衡，还是产品创新？谢谢。\n"
            "【管理层】：第二答。",
            source=source,
        )
        self.assertFalse(split_three_ways.unstable)
        self.assertEqual(len(split_three_ways.units), 2)
        self.assertNotIn("\n提", split_three_ways.units[0].payload["answer"])
        self.assertNotIn("\n问", split_three_ways.units[0].payload["answer"])

    def test_s4_colon_ordinal_tail_cannot_pollute_previous_answer(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "14、单位盈利水平如何？\n单位盈利保持稳定。\n15：碳酸锂矿端的情况？",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 1)
        self.assertEqual(parsed.units[0].payload["answer"], "单位盈利保持稳定。")
        self.assertEqual(parsed.trailing_text, "15：碳酸锂矿端的情况？")

    def test_s4_damaged_compound_prefix_resynchronizes_at_real_q2(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "何？下步重点在哪些方面？目前订单如何？\n"
            "3、控股股东有没有长期发展规划或者承诺呢？\n"
            "答：1、聚焦主业。2、经营承压。3、依法披露。\n"
            "2、近年来竞争激烈，请问公司在维持竞争力方\n"
            "面有哪些核心优势和创新策略？\n"
            "答：持续研发创新。\n"
            "3、未来研发方向如何？\n"
            "答：聚焦智慧能源。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertTrue(parsed.leading_needs_review)
        self.assertIn("控股股东", parsed.leading_text or "")
        self.assertEqual(parsed.ordinals, [2, 3])
        self.assertEqual(len(parsed.units), 2)

    def test_s4_compound_question_keeps_nested_ordinals_before_shared_answer(
        self,
    ) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "1、请管理层回应以下事项：\n"
            "1、未来重点在哪些方面？\n"
            "2、在手订单如何？\n"
            "3、全年业绩目标如何？\n"
            "答：三个事项统一回复。\n"
            "2、公司的核心优势如何？\n"
            "答：核心优势稳定。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 2)
        self.assertIn("在手订单如何？", parsed.units[0].payload["question"])
        self.assertIn("全年业绩目标如何？", parsed.units[0].payload["question"])
        self.assertEqual(parsed.units[0].payload["answer"], "三个事项统一回复。")

    def test_s4_compound_intro_ignores_narrative_replied_label(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "公司就投资者提出的问题进行了回复:\n"
            "1、三个问题，请针对性按题作答，谢谢!\n"
            "1.公司如何提振信心？\n"
            "2.未来发展重点如何？\n"
            "3.大股东是否有长期规划？\n"
            "答：三个事项统一回复。",
            source=source,
            require_explicit_answer=True,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(len(parsed.units), 1)
        self.assertIn("未来发展重点如何？", parsed.units[0].payload["question"])
        self.assertEqual(parsed.units[0].payload["answer"], "三个事项统一回复。")

    def test_official_table_compound_question_recovers_across_text_carrier(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": [],
                            "rows": [
                                [
                                    "投资者关系活动主要内容介绍",
                                    "投资者提出的问题及公司回复情况"
                                    "公司就投资者在本次说明会中提出的问题进行了回复:"
                                    "1、三个问题，请针对性按题作答，谢谢!"
                                    "1.公司如何提振信心？"
                                    "2.未来发展重点如",
                                ]
                            ],
                        },
                    },
                    {
                        "kind": "text",
                        "order_index": 2,
                        "text": "何？\n3.大股东是否有长期规划？",
                    },
                    {
                        "kind": "text",
                        "order_index": 3,
                        "text": "答：三个事项统一回复。",
                    },
                    {
                        "kind": "heading",
                        "order_index": 4,
                        "heading_level": 1,
                        "text": "2、下一问如何？",
                    },
                    {"kind": "text", "order_index": 5, "text": "答：下一答。"},
                ]
            },
            filing_type="performance_briefing",
            document_title="某公司业绩说明会投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(len(qa_units), 2)
        self.assertIn("未来发展重点如何？", qa_units[0].payload["question"])
        self.assertIn("大股东是否有长期规划？", qa_units[0].payload["question"])
        self.assertEqual(qa_units[0].payload["answer"], "三个事项统一回复。")
        self.assertEqual(qa_units[0].quality_status, "needs_review")

    def test_s4_long_recommendation_with_subpoints_is_one_outer_question(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "9、公司一季度业绩怎么样？\n答：请关注公告。\n"
            "10、感谢公司取得良好业绩。在股东回报方面有几点建议，请管理层参考：\n"
            "1、制定明确规划。\n2、加大回购金额。\n"
            "3、尽快发布公告。\n4、其余库存股注销。\n"
            "答：感谢您的建议。\n"
            "11、下一季度展望如何？\n答：经营稳定。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.ordinals, [9, 10, 11])
        self.assertEqual(len(parsed.units), 3)
        self.assertIn("其余库存股注销", parsed.units[1].payload["question"])
        self.assertEqual(parsed.units[1].payload["answer"], "感谢您的建议。")

    def test_s4_bare_wrapped_question_marker_does_not_hide_outer_q23(self) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "22、分红有何预期？\n答：将持续回报股东。\n"
            "23、公司未来三年AI投入较大，目前研发支出全部费用化。我想\n"
            "问：\n"
            "1、未来AI投入是否仍全部费用化？\n"
            "2、公司计划如何应对利润影响？\n"
            "答：公司将坚持研发投入。\n"
            "24、楼宇科技表现如何？\n答：保持增长。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(parsed.ordinals, [22, 23, 24])
        self.assertNotIn("23、", parsed.units[0].payload["answer"])
        self.assertTrue(parsed.units[1].payload["question"].startswith("公司未来三年"))
        self.assertIn("问：1、未来AI投入", parsed.units[1].payload["question"])

    def test_s4_split_problem_label_before_colon_question_does_not_pollute_answer(
        self,
    ) -> None:
        source = UnitDraft(
            payload_kind="text",
            payload={"text": ""},
            source_order=1,
        )
        parsed = s4_build_qa_units(
            "问题 1：第一问如何？\n答：第一答完整。\n"
            "问题 2：第二问如何？\n答：第二答完整。",
            source=source,
        )

        self.assertFalse(parsed.unstable)
        self.assertEqual(
            [unit.payload["question"] for unit in parsed.units],
            ["第一问如何？", "第二问如何？"],
        )
        self.assertEqual(parsed.units[0].payload["answer"], "第一答完整。")
        self.assertNotIn("问题", parsed.units[0].payload["answer"])

    def test_table_qa_requires_explicit_answer_marker(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": ["内容"],
                            "rows": [
                                [
                                    "1、公司股价下跌，有哪些提振举措？\n"
                                    "2、公司上市以来经营业绩下滑明显，情况如"
                                ]
                            ],
                        },
                    }
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        self.assertFalse(any(unit.payload_kind == "qa" for unit in units))

    def test_table_qa_parses_narrative_header_and_propagates_review_status(
        self,
    ) -> None:
        continuation = "上一问答案的跨页续文。" * 45
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": [
                                continuation
                                + "Q4、第四问如何？\n答：第四答。"
                                + "\n5、第五问如何？\n答：第五答。"
                            ],
                            "rows": [],
                        },
                    }
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["第四问如何？", "第五问如何？"],
        )
        self.assertTrue(all(unit.quality_status == "needs_review" for unit in qa_units))

    def test_table_qa_splits_inline_q_a_and_deduplicates_merged_cells(self) -> None:
        packed = (
            "Q:第一问如何?A:第一答持续稳定。"
            "Q:第二问如何?A:第二答持续增长。"
            "Q:第三问跨表未完?"
        )
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": ["活动内容", "问答记录", "问答记录"],
                            # Expanded merged cells repeat the exact same
                            # transcript across covered columns.
                            "rows": [["主要内容", packed, packed, packed]],
                        },
                    }
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["第一问如何?", "第二问如何?"],
        )

    def test_table_text_seam_recovers_only_explicit_cross_carrier_qa(self) -> None:
        packed = "Q:第一问如何?A:第一答。Q:第二问的前半段如何"
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": ["活动内容"],
                            "rows": [["主要内容", packed, packed]],
                        },
                    },
                    {
                        "kind": "text",
                        "order_index": 2,
                        "text": (
                            "补充部分？\nA:第二答。\n3、第三问如何？\n答：第三答。"
                        ),
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["第一问如何?", "第二问的前半段如何补充部分？", "第三问如何？"],
        )
        seam = next(
            unit for unit in qa_units if unit.payload["question"].startswith("第二问")
        )
        self.assertEqual(seam.quality_status, "needs_review")

    def test_text_table_form_seam_recovers_only_explicit_overflow_qa(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "order_index": 1,
                        "text": (
                            "Q4、分红情况如何？\n回复：保持稳定分红。\n"
                            "Q5、知识产权体系是什么样的体"
                        ),
                    },
                    {
                        "kind": "table",
                        "order_index": 10,
                        "table": {
                            "headers": [
                                "",
                                "系？回复：已建立全生命周期知识产权体系。",
                            ],
                            "rows": [
                                ["附件清单(如有)", "无"],
                                ["日期", "2026年7月15日"],
                            ],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["分红情况如何？", "知识产权体系是什么样的体系？"],
        )
        seam = qa_units[-1]
        self.assertEqual(seam.payload["answer"], "已建立全生命周期知识产权体系。")
        self.assertEqual(seam.quality_status, "needs_review")
        self.assertEqual(
            seam.artifact_locator["source_order_span"],
            [1, 10],
        )

        rejected, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "order_index": 1,
                        "text": "Q5、普通业务表是否能补答案？",
                    },
                    {
                        "kind": "table",
                        "order_index": 2,
                        "table": {
                            "headers": ["项目", "答：不能作为跨页答案。"],
                            "rows": [["收入", "100"]],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )
        self.assertFalse(
            any(
                unit.payload_kind == "qa"
                and unit.payload.get("question") == "普通业务表是否能补答案？"
                for unit in rejected
            )
        )

    def test_logical_qa_run_at_eof_is_recovered_once(self) -> None:
        path = ["二、问答环节"]
        units = [
            UnitDraft(
                payload_kind="text",
                payload={"text": "问题1：公司今年收入增长如何？"},
                source_order=1,
                heading_path=path,
                structural_path=path,
                qa_question_boundaries=["问题1：公司今年收入增长如何？"],
            ),
            UnitDraft(
                payload_kind="text",
                payload={
                    "text": "答：公司今年收入保持稳健增长，并持续改善盈利质量。"
                },
                source_order=2,
                heading_path=path,
                structural_path=path,
            ),
        ]

        recovered = _recover_qa_across_logical_carrier_runs(units)
        qa_units = [unit for unit in recovered if unit.payload_kind == "qa"]

        self.assertEqual(len(qa_units), 1)
        self.assertEqual(qa_units[0].payload["question"], "公司今年收入增长如何？")
        self.assertEqual(
            qa_units[0].payload["answer"],
            "公司今年收入保持稳健增长，并持续改善盈利质量。",
        )
        self.assertEqual(
            qa_units[0].artifact_locator["source_order_span"],
            [1, 2],
        )

    def test_explicit_qa_section_keeps_numbered_preamble_structural(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "（三）战略转型",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "1、能源+AI",
                    },
                    {
                        "kind": "text",
                        "order_index": 3,
                        "text": "公司围绕能源场景推进人工智能应用。",
                    },
                    {
                        "kind": "heading",
                        "order_index": 4,
                        "heading_level": 1,
                        "text": "2、能源+机器人",
                    },
                    {
                        "kind": "text",
                        "order_index": 5,
                        "text": "公司开发面向能源运维场景的机器人。",
                    },
                    {
                        "kind": "heading",
                        "order_index": 6,
                        "heading_level": 1,
                        "text": "二、问答环节：",
                    },
                    {
                        "kind": "text",
                        "order_index": 7,
                        "text": "1、公司在机器人研发方面的进展如何？",
                    },
                    {
                        "kind": "text",
                        "order_index": 8,
                        "text": "答：公司已完成原型开发并进入场景验证。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["公司在机器人研发方面的进展如何？"],
        )
        self.assertEqual(qa_units[0].heading_path[-1], "二、问答环节：")
        self.assertFalse(
            any(
                unit.payload_kind == "qa"
                and unit.payload.get("question") in {"能源+AI", "能源+机器人"}
                for unit in units
            )
        )
        evidence = "\n".join(_main_text(unit) for unit in units)
        self.assertIn("能源场景推进人工智能应用", evidence)
        self.assertIn("能源运维场景的机器人", evidence)

    def test_logical_qa_run_recovers_heading_table_text_question(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "三、主要交流问题",
                    },
                    {
                        "kind": "heading",
                        "order_index": 2,
                        "heading_level": 2,
                        "text": "10.上一问如何？",
                    },
                    {"kind": "text", "order_index": 3, "text": "答:上一答。"},
                    {
                        "kind": "heading",
                        "order_index": 4,
                        "heading_level": 2,
                        "text": "11.在新能源方面的发展情况？",
                    },
                    {
                        "kind": "table",
                        "order_index": 6,
                        "table": {
                            "headers": ["答:绿色能源答复前半"],
                            "rows": [],
                        },
                    },
                    {
                        "kind": "text",
                        "order_index": 8,
                        "text": "后半。\n12.下一问如何？\n答:下一答。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["上一问如何？", "在新能源方面的发展情况？", "下一问如何？"],
        )
        self.assertEqual(qa_units[0].payload["answer"], "上一答。")
        self.assertEqual(
            "".join(qa_units[1].payload["answer"].split()),
            "绿色能源答复前半后半。",
        )
        self.assertEqual(qa_units[1].quality_status, "needs_review")
        self.assertEqual(qa_units[1].artifact_locator["source_order_span"], [4, 8])

    def test_logical_qa_run_extends_answer_into_footer_table_header(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 2,
                        "text": "14.服务器电源业务进展怎么样？",
                    },
                    {
                        "kind": "text",
                        "order_index": 2,
                        "text": "答:2026年将是公司在该领域规模发展的元",
                    },
                    {
                        "kind": "table",
                        "order_index": 30,
                        "table": {
                            "headers": [
                                "年。谢谢!15.原材料上涨如何应对？答:加强成本管控。"
                                "16.公司还会并购吗？答:持续关注机会。"
                            ],
                            "rows": [
                                ["附件清单(如有)", "无"],
                                ["日期", "2026年7月15日"],
                            ],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            [
                "服务器电源业务进展怎么样？",
                "原材料上涨如何应对？",
                "公司还会并购吗？",
            ],
        )
        self.assertEqual(
            "".join(qa_units[0].payload["answer"].split()),
            "2026年将是公司在该领域规模发展的元年。谢谢!",
        )
        self.assertEqual(qa_units[0].quality_status, "needs_review")
        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertEqual(table.quality_status, "needs_review")
        self.assertEqual(table.payload["rows"][0][0], "附件清单(如有)")

    def test_logical_qa_run_recovers_proven_unlabelled_single_cell_sequence(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 21,
                        "heading_level": 2,
                        "page_no": 3,
                        "bbox": [349, 749, 667, 769],
                        "text": "7、上一问如何？",
                    },
                    {
                        "kind": "text",
                        "order_index": 22,
                        "page_no": 3,
                        "bbox": [310, 790, 840, 840],
                        "text": "上一问回答完整。",
                    },
                    {
                        "kind": "heading",
                        "order_index": 23,
                        "heading_level": 2,
                        "page_no": 3,
                        "bbox": [349, 885, 687, 906],
                        "text": "8、欧洲市场明年的展望？",
                    },
                    {
                        "kind": "table",
                        "order_index": 25,
                        "page_no": 4,
                        "bbox": [147, 83, 852, 907],
                        "table": {
                            "headers": [],
                            "rows": [
                                [
                                    "欧洲市场受宏观环境影响有些波动，但长期趋势向好。 "
                                    "9、复合集流体进展？ 已具备自研自制能力，并在产品中应用。 "
                                    "10、超充电池进展如何？ 已经发布产品并获得多家客户合作意向。 "
                                    "11、储能产品策略？ 公司坚持高质量产品并努力打造核电"
                                ]
                            ],
                        },
                    },
                    {
                        "kind": "text",
                        "order_index": 27,
                        "page_no": 5,
                        "bbox": [310, 91, 489, 112],
                        "text": "级安全储能产品。",
                    },
                    {
                        "kind": "heading",
                        "order_index": 28,
                        "heading_level": 2,
                        "page_no": 5,
                        "bbox": [351, 147, 757, 167],
                        "text": "12、下一问如何？",
                    },
                    {
                        "kind": "text",
                        "order_index": 29,
                        "page_no": 5,
                        "bbox": [310, 180, 840, 220],
                        "text": "下一问回答完整。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            [
                "上一问如何？",
                "欧洲市场明年的展望？",
                "复合集流体进展？",
                "超充电池进展如何？",
                "储能产品策略？",
                "下一问如何？",
            ],
        )
        q11 = next(
            unit for unit in qa_units if unit.payload["question"] == "储能产品策略？"
        )
        self.assertTrue(q11.payload["answer"].endswith("核电级安全储能产品。"))
        self.assertEqual(q11.artifact_locator["source_order_span"], [25, 27])
        self.assertEqual(q11.quality_status, "needs_review")

    def test_unlabelled_single_cell_sequence_requires_three_consecutive_blocks(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 2,
                        "page_no": 1,
                        "bbox": [300, 880, 700, 905],
                        "text": "1、悬空问题如何？",
                    },
                    {
                        "kind": "table",
                        "order_index": 2,
                        "page_no": 2,
                        "bbox": [140, 80, 850, 500],
                        "table": {
                            "headers": [],
                            "rows": [
                                [
                                    "这是一段长度足够且以句号结束的表格叙事回答。 "
                                    "2、表内第二问？ 第二问的无标记回答完整。 "
                                    "3、表内第三问？ 第三问的无标记回答完整。"
                                ]
                            ],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        self.assertFalse(any(unit.payload_kind == "qa" for unit in units))

    def test_final_unlabelled_question_recovers_only_from_exact_form_footer(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 33,
                        "heading_level": 2,
                        "page_no": 5,
                        "bbox": [310, 585, 842, 633],
                        "text": "14、上一问如何？",
                    },
                    {
                        "kind": "text",
                        "order_index": 34,
                        "page_no": 5,
                        "bbox": [310, 650, 842, 760],
                        "text": "上一问回答完整。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 35,
                        "heading_level": 2,
                        "page_no": 5,
                        "bbox": [352, 885, 574, 904],
                        "text": "15：矿端情况如何？",
                    },
                    {
                        "kind": "table",
                        "order_index": 37,
                        "page_no": 6,
                        "bbox": [149, 83, 848, 254],
                        "table": {
                            "headers": [],
                            "rows": [
                                ["", "项目陆续投产，后续产量将明显提升。"],
                                ["附件清单(如有)", "无"],
                                ["日期", "2026年7月15日"],
                            ],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["上一问如何？", "矿端情况如何？"],
        )
        self.assertEqual(
            qa_units[-1].payload["answer"], "项目陆续投产，后续产量将明显提升。"
        )
        self.assertEqual(
            qa_units[-1].artifact_locator["source_order_span"],
            [35, 37],
        )
        self.assertEqual(qa_units[-1].artifact_locator["page_span"], [5, 6])
        self.assertEqual(
            qa_units[-1].artifact_locator["bbox"],
            [352, 885, 574, 904],
        )

    def test_official_form_recovers_only_proven_unlabelled_q1_q2_page_loss(
        self,
    ) -> None:
        narrative = (
            "问题1:公司未来将采取哪些关键举措保障项目按期交付?"
            "感谢您的提问。公司将强化全周期进度管理与供应链保障，"
            "持续提升项目交付质量并切实兑现对业主的承诺。"
            "问题2:公司今年仍有较大规模公开债务将在未来数月集中"
        )
        elements = [
            {
                "kind": "table",
                "raw_kind": "table",
                "order_index": 1,
                "page_no": 1,
                "bbox": [137, 181, 858, 828],
                "table": {
                    "headers": [],
                    "rows": [
                        ["投资者关系活动类别", "分析师会议"],
                        ["活动参与人员", "某证券分析师及公司管理层"],
                        ["时间", "2026年3月31日"],
                        ["地点", "公司会议室"],
                        ["形式", "现场会议"],
                        ["投资者关系活动主要内容介绍", narrative],
                    ],
                },
            },
            {
                "kind": "page_furniture",
                "raw_kind": "header",
                "order_index": 2,
                "page_no": 1,
                "text": "某公司投资者关系活动记录表",
            },
            {
                "kind": "page_furniture",
                "raw_kind": "page_number",
                "order_index": 3,
                "page_no": 1,
                "text": "1 / 4",
            },
            {
                "kind": "table",
                "raw_kind": "table",
                "order_index": 4,
                "page_no": 2,
                "bbox": [139, 83, 858, 912],
                "table_html": "",
                "table": {"headers": [], "rows": []},
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 5,
                "page_no": 3,
                "bbox": [302, 93, 847, 181],
                "text": "到期，管理层将采取哪些化解策略？",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 6,
                "page_no": 3,
                "text": (
                    "感谢您的提问。公司将保持坦诚沟通，积极争取风险化解"
                    "的时间与空间，并切实维护全体债权人的长远利益。"
                ),
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 7,
                "page_no": 4,
                "text": "公司也将持续改善经营质量，推动恢复健康经营。",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 10,
                "page_no": 4,
                "bbox": [302, 275, 847, 431],
                "text": "问题 3：公司如何保障开发业务可持续发展？",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 11,
                "page_no": 4,
                "text": "感谢您的提问。公司将盘活存量资源并提升经营效率。",
            },
        ]

        units, _ = build_unit_drafts_s1_s7(
            {"elements": elements},
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )
        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            [
                "公司未来将采取哪些关键举措保障项目按期交付?",
                "公司今年仍有较大规模公开债务将在未来数月集中到期，管理层将采取哪些化解策略？",
                "公司如何保障开发业务可持续发展？",
            ],
        )
        self.assertEqual(
            qa_units[0].artifact_locator["source_order_span"], [1, 4]
        )
        self.assertEqual(qa_units[0].artifact_locator["page_span"], [1, 2])
        self.assertEqual(
            qa_units[1].artifact_locator["source_order_span"], [1, 7]
        )
        self.assertEqual(qa_units[1].artifact_locator["page_span"], [1, 4])
        self.assertTrue(
            all(unit.quality_status == "needs_review" for unit in qa_units[:2])
        )
        carriers = [
            unit
            for unit in units
            if unit.source_order in {1, 5} and unit.payload_kind != "qa"
        ]
        self.assertTrue(carriers)
        self.assertTrue(
            all(unit.quality_status == "needs_review" for unit in carriers)
        )

        missing_ghost = copy.deepcopy(elements)
        missing_ghost[3]["table"] = {
            "headers": ["项目", "金额"],
            "rows": [["债务", "100"]],
        }
        rejected, _ = build_unit_drafts_s1_s7(
            {"elements": missing_ghost},
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )
        self.assertFalse(
            any(
                unit.payload_kind == "qa"
                and unit.artifact_locator
                and str(unit.artifact_locator.get("merge_reason", "")).startswith(
                    "official_form_unlabelled"
                )
                for unit in rejected
            )
        )

    def test_extended_form_footer_completes_only_last_qa_answer(self) -> None:
        elements = [
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 57,
                "page_no": 13,
                "bbox": [302, 609, 850, 731],
                "text": "问题 8：围绕价值提升，公司有哪些具体工作思路？",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 58,
                "page_no": 13,
                "text": "感谢您的提问。公司将持续改善经营质量。",
            },
            {
                "kind": "table",
                "raw_kind": "table",
                "order_index": 59,
                "page_no": 14,
                "bbox": [137, 83, 858, 903],
                "table": {
                    "headers": [],
                    "rows": [
                        [
                            "",
                            "公司还将优化资产负债结构，并为长期价值修复创造更扎实的条件。",
                        ],
                        ["关于本次活动是否涉及", "本次活动符合规范要求。"],
                        ["应披露重大信息的说明", ""],
                        [
                            "活动过程中所使用的演示文稿、提供的文档等附件"
                            "(如有,可作为附件)",
                            "无",
                        ],
                    ],
                },
            },
        ]
        units, _ = build_unit_drafts_s1_s7(
            {"elements": elements},
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )
        qa = next(unit for unit in units if unit.payload_kind == "qa")
        self.assertEqual(
            qa.payload["answer"],
            "感谢您的提问。公司将持续改善经营质量。"
            "公司还将优化资产负债结构，并为长期价值修复创造更扎实的条件。",
        )
        self.assertEqual(qa.artifact_locator["source_order_span"], [57, 59])
        self.assertEqual(qa.artifact_locator["page_span"], [13, 14])
        self.assertEqual(qa.quality_status, "needs_review")

        reordered = copy.deepcopy(elements)
        reordered[2]["table"]["rows"][1:3] = list(
            reversed(reordered[2]["table"]["rows"][1:3])
        )
        rejected, _ = build_unit_drafts_s1_s7(
            {"elements": reordered},
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )
        rejected_qa = next(
            unit for unit in rejected if unit.payload_kind == "qa"
        )
        self.assertEqual(
            rejected_qa.payload["answer"],
            "感谢您的提问。公司将持续改善经营质量。",
        )

    def test_footer_overflow_stops_before_embedded_followup_section(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 78,
                        "page_no": 15,
                        "text": "问题 11：资产退出计划如何考虑？",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 79,
                        "page_no": 15,
                        "text": "感谢您的提问。公司正在持续建设资产退出能",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 82,
                        "page_no": 16,
                        "table": {
                            "headers": [],
                            "rows": [
                                [
                                    "",
                                    "力，并相信未来交易会做得更好，谢谢。"
                                    "(二) 对于公告征集问题的回复"
                                    "会前征集问题已在问答环节覆盖。",
                                ],
                                ["附件清单(如有)", "无"],
                                ["日期", "2024年4月1日"],
                            ],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )
        qa = next(unit for unit in units if unit.payload_kind == "qa")
        self.assertEqual(
            qa.payload["answer"],
            "感谢您的提问。公司正在持续建设资产退出能力，并相信未来交易会做得更好，谢谢。",
        )
        self.assertNotIn("公告征集问题", qa.payload["answer"])

    def test_logical_qa_run_recovers_table_heading_text_question(self) -> None:
        packed = (
            "1.第一问如何？答:第一答。"
            "2.第二问如何？答:第二答。"
            "3.超级电容器阶段性成果体现"
        )
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {"headers": ["问答记录"], "rows": [[packed]]},
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "heading_level": 2,
                        "text": "在哪？未来市场计划如何？",
                    },
                    {
                        "kind": "text",
                        "order_index": 4,
                        "text": "答:第三答。\n4.第四问如何？\n答:第四答。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            [
                "第一问如何？",
                "第二问如何？",
                "超级电容器阶段性成果体现在哪？未来市场计划如何？",
                "第四问如何？",
            ],
        )
        self.assertEqual(qa_units[1].payload["answer"], "第二答。")
        self.assertEqual(qa_units[2].payload["answer"], "第三答。")
        self.assertEqual(qa_units[2].quality_status, "needs_review")

    def test_headerless_qa_uses_form_section_not_question_as_path(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": ["投资者关系活动主要内容介绍"],
                            "rows": [["1.第一问如何？答:第一答。"]],
                        },
                    }
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa = next(unit for unit in units if unit.payload_kind == "qa")
        self.assertEqual(qa.heading_path, ["投资者关系活动主要内容介绍"])
        self.assertEqual(qa.title, "第一问如何？")

    def test_table_text_seam_does_not_cross_intervening_prose(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": ["内容"],
                            "rows": [["Q:悬空问题如何?"]],
                        },
                    },
                    {
                        "kind": "text",
                        "order_index": 2,
                        "text": "这是完全无关的独立正文。",
                    },
                    {
                        "kind": "text",
                        "order_index": 3,
                        "text": "A:这是另一段答案。\nQ:后续问题?\nA:后续答案。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        questions = {
            str(unit.payload.get("question") or "")
            for unit in units
            if unit.payload_kind == "qa"
        }
        self.assertNotIn("悬空问题如何?这是完全无关的独立正文。", questions)
        self.assertNotIn("悬空问题如何?", questions)

    def test_table_text_seam_does_not_cross_heading_boundary(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "一、原章节",
                    },
                    {
                        "kind": "table",
                        "order_index": 2,
                        "table": {
                            "headers": ["内容"],
                            "rows": [["Q:悬空问题如何?"]],
                        },
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "二、新章节",
                    },
                    {
                        "kind": "text",
                        "order_index": 4,
                        "text": "A:这是另一章答案。\nQ:后续问题?\nA:后续答案。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        questions = {
            str(unit.payload.get("question") or "")
            for unit in units
            if unit.payload_kind == "qa"
        }
        self.assertNotIn("悬空问题如何?", questions)

    def test_text_table_seam_recovers_only_adjacent_same_section_overflow(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "一、交流问答",
                    },
                    {
                        "kind": "text",
                        "order_index": 2,
                        "text": "Q:公司如何建立知识产权体",
                    },
                    {
                        "kind": "table",
                        "order_index": 3,
                        "table": {
                            "headers": ["", "系？回复：已建立完整管理体系。"],
                            "rows": [],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        recovered = next(
            unit
            for unit in units
            if unit.payload_kind == "qa"
            and unit.payload["question"] == "公司如何建立知识产权体系？"
        )
        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertEqual(recovered.quality_status, "needs_review")
        self.assertEqual(
            recovered.artifact_locator["source_order_span"],
            [2, 3],
        )
        self.assertEqual(table.quality_status, "needs_review")

    def test_text_table_seam_does_not_cross_heading_boundary(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "一、原章节",
                    },
                    {
                        "kind": "text",
                        "order_index": 2,
                        "text": "Q:悬空问题如何?",
                    },
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "二、新章节",
                    },
                    {
                        "kind": "table",
                        "order_index": 4,
                        "table": {
                            "headers": ["", "A:这是另一章答案。"],
                            "rows": [],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        questions = {
            str(unit.payload.get("question") or "")
            for unit in units
            if unit.payload_kind == "qa"
        }
        self.assertNotIn("悬空问题如何?", questions)

    def test_short_truncated_form_table_is_needs_review(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": ["投资者关系活动主要内容介绍"],
                            "rows": [
                                [
                                    "投资者提出的问题及公司回复情况："
                                    "1、公司经营情况如何？2、后续业绩情况如"
                                ]
                            ],
                        },
                    }
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertEqual(table.quality_status, "needs_review")

    def test_text_qa_cut_before_narrative_table_is_needs_review(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "二、主要交流问题",
                    },
                    {
                        "kind": "text",
                        "order_index": 2,
                        "text": "1、合作进展如何？\n答：已经覆盖能源及电网",
                    },
                    {
                        "kind": "table",
                        "order_index": 3,
                        "table": {
                            "headers": ["全链条高度融合，后续稳步推进。"],
                            "rows": [
                                ["附件清单（如有）", "无"],
                                ["日期", "2026年7月15日"],
                            ],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_unit = next(unit for unit in units if unit.payload_kind == "qa")
        self.assertEqual(qa_unit.quality_status, "needs_review")
        self.assertEqual(
            qa_unit.payload["answer"],
            "已经覆盖能源及电网全链条高度融合，后续稳步推进。",
        )
        self.assertEqual(qa_unit.artifact_locator["source_order_span"], [2, 3])
        footer = next(unit for unit in units if unit.payload_kind == "table")
        self.assertEqual(footer.heading_path, ["某公司投资者关系活动记录表"])

        ordinary, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "order_index": 1,
                        "text": "1、合作进展如何？\n答：已经覆盖能源及电网",
                    },
                    {
                        "kind": "table",
                        "order_index": 2,
                        "table": {
                            "headers": ["项目", "金额"],
                            "rows": [["收入", "100"]],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司调研纪要",
        )
        ordinary_qa = next(unit for unit in ordinary if unit.payload_kind == "qa")
        self.assertEqual(ordinary_qa.payload["answer"], "已经覆盖能源及电网")

    def test_qa_heading_boundary_preserves_late_source_order_for_table_seam(
        self,
    ) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "1. 第一问？",
                    },
                    {"kind": "text", "order_index": 2, "text": "答：第一答。"},
                    {
                        "kind": "heading",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "2. 第二问？",
                    },
                    {
                        "kind": "text",
                        "order_index": 4,
                        "text": "答：第二答前半",
                    },
                    {
                        "kind": "table",
                        "order_index": 5,
                        "table": {
                            "headers": [],
                            "rows": [
                                ["", "后半内容已经完整结束。"],
                                ["附件清单（如有）", "无"],
                                ["日期", "2026年7月15日"],
                            ],
                        },
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual([unit.source_order for unit in qa_units], [1, 3])
        self.assertEqual(
            qa_units[1].payload["answer"],
            "第二答前半后半内容已经完整结束。",
        )
        self.assertEqual(
            (qa_units[1].artifact_locator or {}).get("source_order_span"),
            [3, 5],
        )
        self.assertEqual(qa_units[1].quality_status, "needs_review")

    def test_table_qa_cut_before_text_marks_both_sides_needs_review(self) -> None:
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": ["活动内容"],
                            "rows": [["Q:经营情况如何?A:经营总体保"]],
                        },
                    },
                    {
                        "kind": "text",
                        "order_index": 2,
                        "text": "持稳定并持续改善。",
                    },
                ]
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa = next(unit for unit in units if unit.payload_kind == "qa")
        tail = next(
            unit
            for unit in units
            if unit.payload_kind == "text" and "持稳定" in unit.payload["text"]
        )
        self.assertEqual(qa.quality_status, "needs_review")
        self.assertEqual(tail.quality_status, "needs_review")

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

    def test_s5_merges_continued_tables_after_empty_fragment(self) -> None:
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

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload["headers"], ["项目", "金额"])
        self.assertEqual(units[0].payload["rows"], [["收入", "10"], ["成本", "8"]])
        self.assertEqual(units[0].payload["notes"], ["含追溯调整。"])
        self.assertEqual(units[0].artifact_locator["merge_reason"], "continued_table")
        self.assertEqual(units[0].artifact_locator["page_span"], [10, 11])
        self.assertEqual(stats.dropped_by_kind["table_empty"], 1)
        self.assertEqual(stats.merged_tables, 1)

    def test_s5_merges_exact_statement_continuation_caption(self) -> None:
        stats = BuildStats()
        path = ["财务报告", "合并及公司资产负债表"]
        units = s5_build_table_units(
            [
                PreparedElement(
                    kind="table",
                    order_index=1,
                    page_no=206,
                    table_caption=["合并及公司资产负债表"],
                    table={"headers": ["项目", "金额"], "rows": [["资产", "1"]]},
                    heading_path=path,
                ),
                PreparedElement(
                    kind="table",
                    order_index=2,
                    page_no=207,
                    table_caption=["合并及公司资产负债表（续）"],
                    table={"headers": ["项目", "金额"], "rows": [["负债", "1"]]},
                    heading_path=path,
                ),
            ],
            stats,
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload["rows"], [["资产", "1"], ["负债", "1"]])
        self.assertEqual(stats.merged_tables, 1)

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
        self.assertEqual(stats.merged_tables, 0)

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
            set(table_unit.payload), {"caption", "unit", "headers", "rows", "notes"}
        )
        self.assertIn("追溯调整", table_unit.payload["notes"][0])
        self.assertEqual(set(failed_table.payload), {"caption", "raw_html", "notes"})

    def test_s6_skips_only_closed_skip_sections(self) -> None:
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

        self.assertEqual([unit.title for unit in kept], ["重要提示", "风险提示"])
        self.assertEqual(stats.skipped_sections, ["释义"])

    def test_s6_skips_numbered_skip_section_but_keeps_next_section(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "text": "第一节 释义",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "本公司：指测试股份有限公司",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 1,
                        "text": "第二节 公司简介",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "公司主营业务稳定。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].heading_path, ["第二节 公司简介"])
        self.assertEqual(units[0].payload["text"], "公司主营业务稳定。")
        self.assertEqual(stats.skipped_sections, ["释义"])

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

    def test_semantic_rules_keep_all_matches_with_stable_scalar_priority(self) -> None:
        unit = UnitDraft(
            payload_kind="text",
            payload={"text": "关联交易及重大担保情况"},
            source_order=1,
            title="关联交易及重大担保情况",
        )

        self.assertEqual(
            semantic_keys_for_unit(unit, filing_type="annual_report"),
            ["guarantee", "related_party"],
        )
        finalized = s7_finalize_units(
            [unit], filing_type="annual_report", stats=BuildStats()
        )[0]
        self.assertEqual(finalized.semantic_key, "guarantee")
        self.assertEqual(finalized.semantic_keys, ["guarantee", "related_party"])

    def test_market_risk_definition_recovers_narrow_key_without_rewriting_source(
        self,
    ) -> None:
        definition = (
            "金融工具的市场风险，是指金融工具的公允价值或未来现金流量因"
            "市场价格变动而发生波动的风险。"
        )
        unit = UnitDraft(
            payload_kind="mixed",
            payload={
                "semantic_type": "section",
                "parts": [
                    {
                        "kind": "text",
                        "order": 1,
                        "text": definition,
                        "local_heading": ["（四） 汇率风险"],
                    }
                ],
            },
            source_order=1,
            heading_path=["第八节 财务报告", "十、与金融工具相关的风险"],
            structural_path=["第八节 财务报告", "十、与金融工具相关的风险"],
            title="十、与金融工具相关的风险",
        )

        finalized = s7_finalize_units(
            [unit], filing_type="annual_report", stats=BuildStats()
        )[0]
        self.assertEqual(finalized.semantic_key, "market_risk")
        self.assertIn("market_risk", finalized.semantic_keys or [])
        self.assertEqual(
            finalized.payload["parts"][0]["local_heading"],
            ["（四） 汇率风险"],
        )

        no_financial_ancestor = UnitDraft(
            payload_kind="text",
            payload={"text": definition},
            source_order=2,
            heading_path=["第三节 管理层讨论与分析"],
            title="经营风险",
        )
        ordinary_mention = UnitDraft(
            payload_kind="text",
            payload={"text": "公司持续关注市场风险并完善日常管理。"},
            source_order=3,
            heading_path=["第八节 财务报告", "十、与金融工具相关的风险"],
            title="十、与金融工具相关的风险",
        )
        self.assertNotIn(
            "market_risk",
            semantic_keys_for_unit(
                no_financial_ancestor, filing_type="annual_report"
            ),
        )
        self.assertNotIn(
            "market_risk",
            semantic_keys_for_unit(ordinary_mention, filing_type="annual_report"),
        )

    def test_semantic_precision_for_goodwill_and_fundraising(self) -> None:
        goodwill = UnitDraft(
            payload_kind="text",
            payload={"text": "商誉期末余额"},
            source_order=1,
            title="28、商誉",
        )
        impaired = UnitDraft(
            payload_kind="text",
            payload={"text": "商誉减值测试"},
            source_order=2,
            title="商誉减值测试",
        )
        fundraising = UnitDraft(
            payload_kind="text",
            payload={"text": "募集资金使用情况"},
            source_order=3,
            title="募集资金使用情况",
        )

        self.assertNotIn(
            "goodwill_impairment",
            semantic_keys_for_unit(goodwill, filing_type="annual_report"),
        )
        self.assertIn(
            "goodwill_impairment",
            semantic_keys_for_unit(impaired, filing_type="annual_report"),
        )
        self.assertNotIn(
            "capex_projects",
            semantic_keys_for_unit(fundraising, filing_type="annual_report"),
        )

        future_cash_flow = UnitDraft(
            payload_kind="text",
            payload={"text": "预计未来现金流量现值用于商誉减值测试"},
            source_order=4,
            title="预计未来现金流量现值",
        )
        self.assertNotIn(
            "cash_flow",
            semantic_keys_for_unit(future_cash_flow, filing_type="annual_report"),
        )

    def test_s7_scalar_is_present_when_semantic_keys_are_present(self) -> None:
        unit = UnitDraft(
            payload_kind="mixed",
            payload={"semantic_type": "section", "parts": []},
            source_order=1,
            semantic_keys=["inventory", "accounts_receivable"],
        )

        finalized = s7_finalize_units(
            [unit], filing_type="annual_report", stats=BuildStats()
        )[0]

        self.assertEqual(finalized.semantic_keys, ["accounts_receivable", "inventory"])
        self.assertEqual(finalized.semantic_key, "accounts_receivable")

    def test_note_labels_are_unique_and_financial_statements_is_reachable(self) -> None:
        self.assertEqual(
            rules.note_key_for_title("第八节 财务报告"),
            "financial_report_chapter",
        )
        self.assertEqual(
            rules.note_key_for_title("财务报表"),
            "financial_statements_section",
        )
        self.assertIsNone(
            rules.note_key_for_title("六、注册会计师对财务报表审计的责任")
        )
        self.assertIsNone(
            rules.exact_note_key_for_title("注册会计师对财务报表审计的责任")
        )
        self.assertEqual(
            rules.exact_note_key_for_title("合并及公司现金流量表（续）"),
            "cash_flow_statement",
        )
        self.assertEqual(
            rules.exact_note_key_for_title("现金及现金等价物"),
            "cash_flow_note",
        )
        self.assertEqual(
            rules.note_key_for_title("合并及银行股东权益变动表"),
            "equity_statement",
        )
        self.assertEqual(
            rules.note_key_for_title("会计数据和财务指标摘要"),
            "company_profile_metrics",
        )
        self.assertEqual(
            rules.note_key_for_title("4.1 环境、社会与治理情况综述"),
            "environment_social",
        )
        self.assertEqual(
            rules.note_key_for_title("4.2 环境信息"),
            "environment_social",
        )
        self.assertEqual(
            rules.note_key_for_title("4.4 治理信息"),
            "governance",
        )
        with self.assertRaisesRegex(ValueError, "duplicate note label"):
            rules._unique_note_label_table(  # type: ignore[attr-defined]
                {
                    "first": {"names": ["重复"], "aliases": []},
                    "second": {"names": ["重复"], "aliases": []},
                }
            )

    def test_bank_report_titles_use_controlled_note_keys(self) -> None:
        cases = {
            "3.2.5 净利息收入": "bank_net_interest_income",
            "3.3.1.1 贷款和垫款": "bank_loans_advances",
            "3.5.3 资本充足率情况": "bank_capital_adequacy",
            "1. 信用风险（续）": "credit_risk",
            "4. 银行账簿利率风险": "banking_book_interest_rate_risk",
            "3.6 分部经营业绩": "segment_information",
        }

        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(rules.note_key_for_title(title), expected)

    def test_investor_document_event_is_truthful_scalar_fallback(self) -> None:
        units = s7_finalize_units(
            [
                UnitDraft(
                    payload_kind="qa",
                    payload={"question": "公司经营情况如何？", "answer": "经营稳定。"},
                    source_order=1,
                    title="公司经营情况如何？",
                )
            ],
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
            stats=BuildStats(),
        )

        self.assertEqual(units[0].semantic_key, "investor_communication")
        self.assertEqual(units[0].semantic_keys, ["investor_communication"])

    def test_regulatory_title_families_have_truthful_event_fallbacks(self) -> None:
        cases = {
            "万科A：2025年6月销售及近期新增项目情况简报": "operating_data_event",
            "华测检测：2025年度业绩预告": "performance_forecast_event",
            "招商银行：2025年度业绩快报公告": "performance_flash_event",
            "平安银行：日常关联交易公告": "related_party_transaction_event",
        }

        for document_title, expected in cases.items():
            with self.subTest(document_title=document_title):
                unit = s7_finalize_units(
                    [
                        UnitDraft(
                            payload_kind="text",
                            payload={"text": "公告正文。"},
                            source_order=1,
                            title="公告正文",
                        )
                    ],
                    filing_type="other",
                    document_title=document_title,
                    stats=BuildStats(),
                )[0]
                self.assertEqual(unit.semantic_key, expected)
                self.assertIn(expected, unit.semantic_keys or [])

    def test_semantic_keys_use_truthful_document_content_fallback(self) -> None:
        units = s7_finalize_units(
            [
                UnitDraft(
                    payload_kind="text",
                    payload={"text": "普通说明"},
                    source_order=1,
                    title="普通说明",
                )
            ],
            filing_type="other",
            stats=BuildStats(),
        )

        self.assertEqual(units[0].semantic_key, "document_content")
        self.assertEqual(units[0].semantic_keys, ["document_content"])

    def test_substantive_prelude_is_preserved_before_first_structural_section(
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
                        "text": "某某股份有限公司",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "text": "董事长致股东信",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "text": "过去一年，公司坚持长期主义并持续投入研发。",
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
            document_title="某某股份：2025年年度报告",
        )

        self.assertEqual(len(units), 2)
        self.assertEqual(units[0].heading_path, ["董事长致股东信"])
        self.assertIn("长期主义", str(units[0].payload))
        self.assertEqual(units[1].heading_path, ["第一节 重要提示"])
        self.assertEqual(stats.dropped_cover_prelude, 0)

    def test_periodic_cover_exact_date_is_dropped_without_bulk_truncation(
        self,
    ) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 0,
                        "heading_level": 1,
                        "page_no": 1,
                        "text": "南通江海电容器股份有限公司",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "page_no": 1,
                        "text": "2025 年年度报告",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "page_no": 1,
                        "text": "【2026 年 4 月 10 日】",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 1,
                        "page_no": 2,
                        "text": "第一节 重要提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "page_no": 2,
                        "text": "公司声明本报告真实、准确、完整。",
                    },
                ]
            },
            filing_type="annual_report",
            document_title="江海股份：2025年年度报告",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].heading_path, ["第一节 重要提示"])
        self.assertNotIn("2026 年 4 月 10 日", str(units[0].payload))
        self.assertEqual(stats.dropped_cover_prelude, 1)

    def test_registered_page_one_title_fragments_merge_by_exact_proof(self) -> None:
        registered = (
            "关于部分募集资金投资项目结项并将节余募集资金"
            "永久补充流动资金的公告"
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "title",
                        "order_index": 0,
                        "heading_level": 1,
                        "page_no": 1,
                        "text": "关于部分募集资金投资项目结项并将节余募集资金",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "title",
                        "order_index": 1,
                        "heading_level": 2,
                        "page_no": 1,
                        "text": "永久补充流动资金的公",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "title",
                        "order_index": 2,
                        "heading_level": 2,
                        "page_no": 1,
                        "text": "告",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "page_no": 1,
                        "text": "本次项目已经结项，节余资金永久补流。",
                    },
                ]
            },
            filing_type="major_contract",
            document_title=f"某公司：{registered}",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].title, registered)
        self.assertEqual(units[0].heading_path, [registered])
        self.assertEqual(stats.merged_cover_title_fragments, 2)

    def test_registered_title_merge_skips_only_page_one_header_kv(self) -> None:
        registered = "2024年限制性股票激励计划(草案)摘要"
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 0,
                        "page_no": 1,
                        "text": "证券代码：688001",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 1,
                        "text": "证券简称：某公司",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "title",
                        "order_index": 2,
                        "heading_level": 1,
                        "page_no": 1,
                        "text": "2024年限制性股票激励计划",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "title",
                        "order_index": 3,
                        "heading_level": 2,
                        "page_no": 1,
                        "text": "(草案)摘要",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "page_no": 1,
                        "text": "本激励计划的考核条件如下。",
                    },
                ]
            },
            filing_type="equity_incentive",
            document_title=f"某公司：{registered}",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].title, registered)
        self.assertEqual(units[0].heading_path, [registered])
        self.assertEqual(stats.merged_cover_title_fragments, 1)
        self.assertEqual(stats.stripped_header_lines, 2)

    def test_unproven_page_one_heading_fragments_are_not_merged(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "title",
                        "order_index": 0,
                        "heading_level": 1,
                        "page_no": 1,
                        "text": "关于项目进展的",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "title",
                        "order_index": 1,
                        "heading_level": 2,
                        "page_no": 1,
                        "text": "补充公告",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "page_no": 1,
                        "text": "项目按计划推进。",
                    },
                ]
            },
            filing_type="major_contract",
            document_title="某公司：关于另一项目的公告",
        )

        self.assertEqual(stats.merged_cover_title_fragments, 0)
        self.assertFalse(any(unit.title == "关于项目进展的补充公告" for unit in units))

    def test_periodic_cover_metadata_unit_is_dropped_only_when_closed_set(
        self,
    ) -> None:
        gree_elements = [
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 1,
                "heading_level": 1,
                "page_no": 1,
                "text": "珠海格力电器股份有限公司",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 2,
                "page_no": 1,
                "text": "2024 年年度报告",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 3,
                "page_no": 1,
                "text": "二〇二五年四月",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 4,
                "heading_level": 1,
                "page_no": 2,
                "text": "第一节 重要提示",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 5,
                "page_no": 2,
                "text": "公司保证报告内容真实、准确、完整。",
            },
        ]
        gree, stats = build_unit_drafts_s1_s7(
            {"elements": gree_elements},
            filing_type="annual_report",
            document_title="格力电器：2024年年度报告",
        )
        self.assertEqual([unit.title for unit in gree], ["第一节 重要提示"])
        self.assertEqual(stats.dropped_by_kind["periodic_cover_metadata"], 1)

        icbc_elements = [
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 0,
                "heading_level": 1,
                "page_no": 1,
                "text": "ICBC 中国工商银行",
            },
            {
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 1,
                "heading_level": 1,
                "page_no": 1,
                "text": "中国工商银行股份有限公司",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 2,
                "page_no": 1,
                "text": "(股票代码: 601398)",
            },
            {
                "kind": "text",
                "raw_kind": "text",
                "order_index": 3,
                "page_no": 1,
                "text": "2023 年度报告",
            },
            *gree_elements[3:],
        ]
        icbc, icbc_stats = build_unit_drafts_s1_s7(
            {"elements": icbc_elements},
            filing_type="annual_report",
            document_title="工商银行：工商银行2023年度报告",
        )
        self.assertEqual([unit.title for unit in icbc], ["第一节 重要提示"])
        self.assertEqual(
            icbc_stats.dropped_by_kind["periodic_cover_metadata"], 1
        )

        substantive = copy.deepcopy(gree_elements)
        substantive[2]["text"] = "二〇二五年四月\n董事长致股东：公司坚持长期主义。"
        preserved, preserved_stats = build_unit_drafts_s1_s7(
            {"elements": substantive}, filing_type="annual_report"
        )
        self.assertTrue(any("长期主义" in str(unit.payload) for unit in preserved))
        self.assertEqual(
            preserved_stats.dropped_by_kind["periodic_cover_metadata"], 0
        )

    def test_periodic_table_banner_caption_falls_back_to_structural_leaf(
        self,
    ) -> None:
        def build(filing_type: str, caption: str) -> list[UnitDraft]:
            return build_unit_drafts_s1_s7(
                {
                    "elements": [
                        {
                            "kind": "heading",
                            "raw_kind": "text",
                            "order_index": 1,
                            "heading_level": 1,
                            "text": "二、公司基本情况",
                        },
                        {
                            "kind": "heading",
                            "raw_kind": "text",
                            "order_index": 2,
                            "heading_level": 2,
                            "text": "3、主要会计数据和财务指标",
                        },
                        {
                            "kind": "heading",
                            "raw_kind": "text",
                            "order_index": 3,
                            "heading_level": 2,
                            "text": "(2) 分季度主要会计数据",
                        },
                        {
                            "kind": "table",
                            "raw_kind": "table",
                            "order_index": 4,
                            "table_caption": [caption],
                            "table": {
                                "headers": ["第一季度", "第二季度"],
                                "rows": [["10", "20"]],
                            },
                        },
                    ]
                },
                filing_type=filing_type,
            )[0]

        banner = "上海能辉科技股份有限公司 2024 年年度报告摘要"
        periodic = build("annual_report", banner)
        self.assertEqual(len(periodic), 1)
        self.assertEqual(periodic[0].title, "(2) 分季度主要会计数据")
        self.assertEqual(periodic[0].payload["caption"], [])

        nonperiodic = build("other", banner)
        self.assertEqual(nonperiodic[0].title, banner)
        self.assertEqual(nonperiodic[0].payload["caption"], [banner])

        business_caption = "上海能辉科技股份有限公司2024年度报告分部信息"
        preserved = build("annual_report", business_caption)
        self.assertEqual(preserved[0].title, business_caption)
        self.assertEqual(preserved[0].payload["caption"], [business_caption])

    def test_periodic_date_after_substantive_prelude_is_preserved(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 0,
                        "heading_level": 1,
                        "page_no": 1,
                        "text": "某某股份有限公司",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 1,
                        "page_no": 1,
                        "text": "2025 年年度报告",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "heading_level": 1,
                        "page_no": 1,
                        "text": "董事长致股东信",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "page_no": 1,
                        "text": "过去一年，公司坚持长期主义。",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "page_no": 1,
                        "text": "2026 年 4 月 10 日",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 5,
                        "heading_level": 1,
                        "page_no": 2,
                        "text": "第一节 重要提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 6,
                        "page_no": 2,
                        "text": "公司声明本报告真实、准确、完整。",
                    },
                ]
            },
            filing_type="annual_report",
        )

        self.assertTrue(
            any("2026 年 4 月 10 日" in str(unit.payload) for unit in units)
        )
        self.assertEqual(stats.dropped_cover_prelude, 0)

    def test_periodic_body_date_and_short_document_date_are_preserved(self) -> None:
        body_units, body_stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 0,
                        "heading_level": 1,
                        "page_no": 2,
                        "text": "第一节 重要提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 4,
                        "text": "2026 年 4 月 10 日",
                    },
                ]
            },
            filing_type="annual_report",
        )
        short_units, short_stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 0,
                        "page_no": 1,
                        "text": "2026 年 4 月 10 日",
                    }
                ]
            },
            filing_type="other",
        )

        self.assertIn("2026 年 4 月 10 日", str(body_units[0].payload))
        self.assertIn("2026 年 4 月 10 日", str(short_units[0].payload))
        self.assertEqual(body_stats.dropped_cover_prelude, 0)
        self.assertEqual(short_stats.dropped_cover_prelude, 0)

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
        self.assertEqual(stats.dropped_cover_prelude, 0)

    def test_standalone_unit_declaration_is_dropped_and_counted(self) -> None:
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

        kinds = [unit.payload_kind for unit in units]
        self.assertEqual(kinds, ["table"])
        self.assertEqual(stats.dropped_unit_declarations, 1)
        # The declaration still reaches the table payload via the element stream.
        self.assertEqual(units[0].payload["unit"], "元")

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
        # -4: declaration lines are stripped line-level even inside merged text.
        self.assertEqual(units[0].payload["text"], "下表列示了主要科目变动。")
        self.assertEqual(stats.dropped_unit_declarations, 1)

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
            units[0].payload["text"], "公告编号：临 2026-027\n分红实施公告正文。"
        )
        self.assertEqual(stats.stripped_header_lines, 1)

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
            "公告编号：临 2026-006\n本公司董事会保证公告内容真实、准确、完整。",
        )
        self.assertEqual(stats.stripped_header_lines, 2)
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
        self.assertEqual(stats2.stripped_header_lines, 0)

    def test_preferred_stock_kv_lines_stripped(self) -> None:
        # round16 语料：平安银行信头「优先股代码/简称」不在旧 KV 模式里，
        # 残片曾以『公告头信息』unit 形态入库（4 个存量实例）。
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
        self.assertNotIn("优先股", units[0].payload["text"])
        self.assertEqual(stats.stripped_header_lines, 2)

    def test_a_share_prefixed_cover_metadata_does_not_fragment_number(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 1,
                        "text": (
                            "A 股证券代码：000002、299903\n"
                            "A股证券简称：万科 A、万科 H 代\n"
                            "公告编号：〈万〉2025-116\n"
                            "二〇二五年八月"
                        ),
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "page_no": 1,
                        "heading_level": 1,
                        "text": "重要提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "page_no": 1,
                        "text": "本报告已经董事会审议通过。",
                    },
                ]
            },
            filing_type="semiannual_report",
            document_title="万科A：2025年半年度报告",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].title, "重要提示")
        self.assertIn("公告编号：〈万〉2025-116", _main_text(units[0]))
        self.assertNotIn("证券代码", _main_text(units[0]))
        self.assertNotIn("证券简称", _main_text(units[0]))
        self.assertNotIn("二〇二五年八月", _main_text(units[0]))
        self.assertEqual(stats.stripped_header_lines, 2)
        self.assertEqual(stats.merged_announcement_header_units, 1)

    def test_announcement_number_skips_closed_date_to_substantive_target(
        self,
    ) -> None:
        number = UnitDraft(
            payload_kind="text",
            payload={"text": "公告编号：2023-043"},
            source_order=0,
            heading_path=["某银行公告"],
            title="某银行公告",
            artifact_locator={
                "page_no": 1,
                "bbox": [1.0, 1.0, 2.0, 2.0],
                "order_index": 0,
            },
        )
        date = UnitDraft(
            payload_kind="text",
            payload={"text": "二〇二三年十月二十五日"},
            source_order=2,
            heading_path=["某银行公告"],
            title="某银行公告",
            artifact_locator={"page_no": 1, "order_index": 2},
        )
        body = UnitDraft(
            payload_kind="mixed",
            payload={
                "semantic_type": "section_group",
                "parts": [
                    {
                        "kind": "text",
                        "order": 11,
                        "text": "1、本行董事会、监事会保证公告内容真实。",
                    }
                ],
            },
            source_order=11,
            heading_path=["重要内容提示"],
            title="重要内容提示",
            artifact_locator={
                "page_no": 1,
                "bbox": [100.0, 100.0, 900.0, 900.0],
                "order_index": 11,
            },
        )
        stats = BuildStats()

        merged = _merge_announcement_number_carriers(
            [number, date, body], stats=stats
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "重要内容提示")
        self.assertIn("公告编号：2023-043", _main_text(merged[0]))
        self.assertNotIn("二〇二三年十月二十五日", _main_text(merged[0]))
        self.assertEqual(
            merged[0].artifact_locator,
            {
                "page_no": 1,
                "bbox": [100.0, 100.0, 900.0, 900.0],
                "order_index": 11,
                "source_order_span": [0, 11],
                "merge_reason": "announcement_number_carrier",
            },
        )
        self.assertEqual(stats.merged_announcement_header_units, 1)
        self.assertEqual(
            stats.dropped_by_kind["announcement_cover_metadata_line"], 1
        )

        fail_closed_stats = BuildStats()
        self.assertEqual(
            _merge_announcement_number_carriers(
                [number, date], stats=fail_closed_stats
            ),
            [number, date],
        )
        self.assertEqual(fail_closed_stats.merged_announcement_header_units, 0)

    def test_standalone_announce_no_merges_into_next_content(self) -> None:
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
                        "text": "公司拟与关联方发生交易，金额为人民币一亿元。",
                    },
                ]
            },
            filing_type="financing",
            document_title="某公司：关联交易公告",
        )

        body = next(u for u in units if "交易概述" in (u.title or ""))
        self.assertIn("公告编号：2023-026", body.payload["text"])
        self.assertTrue(body.payload["text"].endswith("人民币一亿元。"))
        self.assertNotIn("公告头信息", [*body.heading_path, body.title or ""])
        self.assertEqual(stats.merged_announcement_header_units, 1)
        self.assertEqual(stats.deduplicated_announcement_header_units, 0)
        self.assertEqual(stats.dropped_by_kind.get("standalone_noise", 0), 0)

    def test_announcement_number_deduplicates_against_next_content(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 1,
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
                        "text": "公告编号：2023-026\n本公告披露重大交易事实。",
                    },
                ]
            },
            filing_type="financing",
            document_title="某公司：关联交易公告",
        )

        blob = "\n".join(_main_text(unit) for unit in units)
        self.assertEqual(blob.count("公告编号：2023-026"), 1)
        self.assertEqual(stats.merged_announcement_header_units, 1)
        self.assertEqual(stats.deduplicated_announcement_header_units, 1)

    def test_announcement_cover_metadata_merges_into_mixed_body(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 1,
                        "text": (
                            "公告编号：临 2024-018\n"
                            "2024 年半年度报告\n二〇二四年八月"
                        ),
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 2,
                        "page_no": 2,
                        "heading_level": 1,
                        "text": "第一节 重要提示",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "page_no": 2,
                        "text": "本报告真实、准确、完整。",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 4,
                        "page_no": 2,
                        "table": {
                            "headers": ["项目", "金额"],
                            "rows": [["营业收入", "100"]],
                        },
                    },
                ]
            },
            filing_type="semiannual_report",
            document_title="某公司：2024年半年度报告",
        )

        body = next(unit for unit in units if "重要提示" in (unit.title or ""))
        self.assertEqual(body.payload_kind, "mixed")
        self.assertIn("公告编号：临 2024-018", str(body.payload))
        self.assertNotIn("二〇二四年八月", str(body.payload))
        self.assertEqual(body.heading_path, ["第一节 重要提示"])
        self.assertEqual(body.title, "第一节 重要提示")
        self.assertEqual(
            stats.dropped_by_kind["announcement_cover_metadata_line"], 2
        )

    def test_announcement_number_without_safe_neighbor_is_preserved(self) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 1,
                        "text": "公告编号：2023-027",
                    }
                ]
            },
            filing_type="annual_report",
            document_title="某公司：2023年年度报告",
        )

        self.assertIn("公告编号：2023-027", " ".join(map(str, units)))
        self.assertNotIn("公告头信息", units[0].heading_path)
        self.assertEqual(stats.merged_announcement_header_units, 0)

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

        # 短文档可能被 s8 折叠成单个 doc unit——断言内容不丢失且未被
        # 噪声规则误杀（可能以独立 unit 或 mixed parts 形态存在）。
        blob = " ".join(str(u.payload) for u in units)
        self.assertIn("营业收入", blob)
        self.assertIn("公告头信息", blob + " ".join(str(u.heading_path) for u in units))

    def test_attachment_caption_opens_top_level_scope(self) -> None:
        # round17 语料：11 张「附件N：《…》」表错挂在最后一个叙事小节下
        # （1217576500 的《参与机构名单》混进「三、主要交流问题」）。附件
        # 是正文的兄弟节点：caption 命中即在标题树开新顶层分支。
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
                        "text": "问：公司下半年增长压力如何？答：经营保持稳健。",
                    },
                    {
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

    def test_qa_form_footer_table_reanchors_to_document(self) -> None:
        # round17 语料：72 张表单尾字段表（附件清单/日期）错挂在最后一个
        # 叙事小节下——官方模板的固定尾字段归属文档本身。
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
        self.assertEqual(footer.heading_path, ["华测检测：投资者关系活动记录表"])
        self.assertEqual(footer.title, "华测检测：投资者关系活动记录表")
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

    def test_native_text_recovers_qa_form_without_duplicate_prose_table(self) -> None:
        # 1217576500 的真实故障形态：MinerU 把表单内的跨页正文截成
        # text + 单列表格 + 页脚溢出，原生 PDF 文本层却有完整的三个章节。
        long_answer = "第一答完整且保留全部跨页内容。" * 45
        native_text = "\n".join(
            [
                "华测检测认证集团股份有限公司投资者关系活动记录表",
                "投资者关系活动主要内容介绍",
                "一、公司上半年业绩情况",
                "收入和利润均保持增长。",
                # 真实 PDF 的外层表单标签被纵向拆开，尾片落进正文行。
                "要内容介绍",
                "二、公司上半年经营亮点介绍",
                "公司持续推进国际化和精益管理。",
                "三、主要交流问题",
                "1、第一问跨行",
                f"继续吗？ 答：{long_answer}",
                "2、未来 1~2 年规划？",
                "答：第二答完整。",
                "附件清单（如有） 《参与机构名单》",
                "日期 2023-08-11",
            ]
        )
        shredded = (
            f"1、第一问跨行继续吗？答：{long_answer}"
            # MinerU Markdown 对同一个原文波浪号增加转义反斜杠。
            + r"2、未来 1\~2 年规划？"
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "0" * 64,
                    "pages": [
                        {
                            "page_no": 1,
                            "text": native_text,
                            "non_whitespace_chars": len(native_text),
                        }
                    ],
                },
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
                                [
                                    "投资者关系活动主要内容介绍",
                                    "一、公司上半年业绩情况\n收入和利润",
                                    "另一位接待人 一、公司上半年业绩情况\n收入和利润",
                                ],
                            ],
                        },
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "均保持增长。",
                    },
                    {
                        "kind": "unknown",
                        "raw_kind": "list",
                        "order_index": 3,
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 4,
                        "table": {"headers": [shredded], "rows": []},
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 5,
                        "table": {
                            "headers": [],
                            "rows": [
                                ["答：第二答完整。", ""],
                                ["附件清单（如有）", "《参与机构名单》"],
                                ["日期", "2023-08-11"],
                            ],
                        },
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 6,
                        "table": {"headers": [], "rows": [["", ""]]},
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 7,
                        "heading_level": 2,
                        "text": "附件 1：《参与机构名单》",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 8,
                        "table": {
                            "headers": ["机构", "姓名"],
                            "rows": [["某某基金", "张三"]],
                        },
                    },
                ],
            },
            filing_type="investor_relations",
            document_title="华测检测：投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_sections_recovered, 3)
        self.assertEqual(stats.qa_form_carriers_replaced, 3)
        self.assertEqual(stats.needs_review_count, 0)
        self.assertEqual(
            [unit.payload_kind for unit in units],
            ["table", "text", "text", "qa", "qa", "table", "table"],
        )
        blob = " ".join(str(unit.payload) for unit in units)
        self.assertNotIn("第二答残片", blob)
        self.assertIn("另一位接待人", blob)
        self.assertNotIn(rules.DOCUMENT_HEADER_ANCHOR, blob)
        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["第一问跨行继续吗？", "未来 1~2 年规划？"],
        )
        self.assertEqual(
            [unit.title for unit in qa_units],
            ["第一问跨行继续吗？", "未来 1~2 年规划？"],
        )
        self.assertTrue(
            all(unit.heading_path == ["三、主要交流问题"] for unit in qa_units)
        )
        footer = next(unit for unit in units if "附件清单" in str(unit.payload))
        self.assertNotIn("第二答完整", str(footer.payload))
        attachment = next(unit for unit in units if "某某基金" in str(unit.payload))
        self.assertEqual(attachment.heading_path, ["附件 1：《参与机构名单》"])

    def test_direct_native_transcript_recovers_unlabelled_answers_and_deduplicates(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某银行股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 特定对象调研",
                "参与单位名称及人员姓名 某机构",
                "时间 2026年7月15日",
                "地点 深圳",
                "形式 电话会议",
                "介绍公司经营情况，回答投资者提问",
                "1. 第一问跨行",
                "继续吗？",
                "第一答第一行，",
                "第一答第二行。",
                "投资者关系活动主要内容",
                "介绍",
                "2. 第二问？",
                "第二答完整。",
                "关于本次活动是否涉及应披露重大信息的说明 无",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "a" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 0,
                        "page_no": 1,
                        "table": {
                            "headers": ["投资者关系活动类别", "参与机构"],
                            "rows": [["特定对象调研", "某机构"]],
                        },
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 1,
                        "heading_level": 2,
                        "text": "2. 第二问？",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "page_no": 1,
                        "text": "第二答完整。",
                    },
                ],
            },
            filing_type="investor_relations",
            document_title="某银行：调研活动信息",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(stats.native_text_qa_pairs_recovered, 2)
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["第一问跨行继续吗？", "第二问？"],
        )
        self.assertTrue(
            all(
                (unit.artifact_locator or {}).get("source") == "native_text"
                for unit in qa_units
            )
        )
        self.assertTrue(
            all(
                (unit.artifact_locator or {}).get("native_text_hash")
                == "sha256:" + "a" * 64
                for unit in qa_units
            )
        )
        self.assertIn(
            "投资者关系活动类别",
            " ".join(str(unit.payload) for unit in units),
        )

    def test_direct_native_qa_suppresses_only_covered_carriers_and_table_rows(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 业绩说明会",
                "参与单位名称及人员姓名 某机构",
                "上市公司接待人员姓名 董事会秘书",
                "时间 2026年7月15日",
                "地点 珠海",
                "形式 现场会议",
                "投资者关系活动主要内容介绍",
                "1. 第一问如何？",
                "第一答开头，第一答中段持续说明，第一答结尾已经完整。",
                "2. 第二问如何？",
                "第二答开头，第二答中段持续说明，第二答结尾已经完整。",
                "附件清单(如有) 无",
                "日期 2026年7月15日",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "9" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 0,
                        "page_no": 1,
                        "table": {
                            "headers": [],
                            "rows": [
                                ["投资者关系活动类别", "业绩说明会"],
                                [
                                    "投资者关系活动主要内容介绍",
                                    "1. 第一问如何？第一答开头，第一答中段持续说明，",
                                ],
                            ],
                        },
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 1,
                        "text": "第一答中段持续说明，第一答结尾已经完整。",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 2,
                        "page_no": 1,
                        "table": {
                            "headers": [],
                            "rows": [
                                [
                                    "",
                                    "第二答开头，第二答中段持续说明，第二答结尾已经完整。",
                                ],
                                ["附件清单(如有)", "无"],
                                ["日期", "2026年7月15日"],
                            ],
                        },
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "page_no": 1,
                        "table": {
                            "headers": [
                                "1. 第一问如何？第一答开头，第一答中段持续说明，"
                                "第一答结尾已经完整。2. 第二问如何？第二答开头，"
                                "第二答中段持续说明，第二答结尾已经完整。"
                            ],
                            "rows": [["2、第二问如何？"]],
                        },
                    },
                ],
            },
            filing_type="performance_briefing",
            document_title="某公司：业绩说明会投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(len(qa_units), 2)
        non_qa_blob = " ".join(
            str(unit.payload) for unit in units if unit.payload_kind != "qa"
        )
        self.assertNotIn("第一答结尾", non_qa_blob)
        self.assertNotIn("第二答开头", non_qa_blob)
        self.assertNotIn("2、第二问如何", non_qa_blob)
        self.assertIn("投资者关系活动类别", non_qa_blob)
        self.assertIn("附件清单", non_qa_blob)
        self.assertIn("日期", non_qa_blob)
        self.assertEqual(stats.native_text_carriers_suppressed, 2)
        self.assertGreaterEqual(stats.native_text_table_rows_suppressed, 3)

    def test_direct_native_transcript_recovers_explicit_q_reply_across_pages(
        self,
    ) -> None:
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "b" * 64,
                    "pages": [
                        {
                            "page_no": 1,
                            "text": "\n".join(
                                [
                                    "某公司投资者关系活动记录表",
                                    "投资者关系活动",
                                    "类别",
                                    "活动参与人员 董事会秘书",
                                    "时间 2026年7月15日",
                                    "地点 深圳",
                                    "形式 现场会议",
                                    "交流内容及具体",
                                    "Q1：第一问",
                                    "回复：第一答。",
                                    "问答",
                                    "Q2：第二问",
                                    "回复：",
                                ]
                            ),
                        },
                        {
                            "page_no": 2,
                            "text": "\n".join(
                                [
                                    "1、答案子项一。",
                                    "2、答案子项二。",
                                    "关于本次活动是否涉及重大信息 无",
                                ]
                            ),
                        },
                    ],
                },
                "elements": [],
            },
            filing_type="investor_relations",
            document_title="某公司：投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(stats.native_text_qa_pairs_recovered, 2)
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["第一问", "第二问"],
        )
        self.assertIn("答案子项一", qa_units[1].payload["answer"])
        self.assertIn("答案子项二", qa_units[1].payload["answer"])
        self.assertEqual(
            (qa_units[1].artifact_locator or {}).get("page_span"),
            [1, 2],
        )

    def test_direct_native_transcript_accepts_scoped_chinese_numbering_family(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 特定对象调研",
                "参与单位名称 某机构 及人员姓名 张三",
                "上市公司接待 董事会秘书 人员姓名 李四",
                "时间 2026年7月15日",
                "地点 深圳",
                "二、问答环节",
                "1、第一问如何？",
                "第一答完整。",
                "2、第二问如何？",
                "第二答完整。",
                "3：第三问如何？",
                "第三答完整。",
                "附件清单(如有) 无",
                "日期 2026年7月15日",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "d" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [],
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(stats.native_text_qa_pairs_recovered, 3)
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["第一问如何？", "第二问如何？", "第三问如何？"],
        )

    def test_direct_native_dot_sequence_can_continue_with_chinese_delimiter(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 特定对象调研",
                "活动参与人员 某机构",
                "时间 2026年7月15日",
                "地点 深圳",
                "形式 电话会议",
                "回答投资者提问",
                "1. 第一问如何？",
                "第一答完整。",
                "2、第二问如何？",
                "第二答完整。",
                "关于本次活动是否涉及应披露重大信息的说明 无",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [],
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_qa_pairs_recovered, 2)
        self.assertEqual(
            [unit.payload["question"] for unit in units if unit.payload_kind == "qa"],
            ["第一问如何？", "第二问如何？"],
        )

    def test_direct_native_colon_ratio_is_not_a_question_start(self) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 特定对象调研",
                "活动参与人员 某机构",
                "时间 2026年7月15日",
                "地点 深圳",
                "形式 电话会议",
                "回答投资者提问",
                "1:1鲜净感空气机持续升级。",
                "普通业务介绍。",
                "日期 2026年7月15日",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [],
            },
            filing_type="investor_relations",
            document_title="某公司投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_qa_pairs_recovered, 0)
        self.assertFalse(any(unit.payload_kind == "qa" for unit in units))

    def test_direct_native_official_main_label_allows_year_leading_question(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 业绩说明会",
                "参与单位名称及人员姓名 某机构",
                "上市公司接待人员姓名 董事会秘书",
                "时间 2026年7月15日",
                "地点 珠海",
                "形式 现场会议",
                "1. 第一问如何？",
                "第一答开头。",
                # Outer form labels can appear inside the first answer in
                # native drawing order even though they are visually beside it.
                "投资者关系活动",
                "主要内容介绍",
                "第一答完整。",
                "2.2024 年公司净利润增幅如何？",
                "第二答包含 1.1 倍的业务比例，但不会另起问题。",
                "3. 第三问如何？",
                "第三答完整。",
                "附件清单(如有) 无",
                "日期 2026年7月15日",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [],
            },
            filing_type="performance_briefing",
            document_title="某公司业绩说明会投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(stats.native_text_qa_pairs_recovered, 3)
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["第一问如何？", "2024 年公司净利润增幅如何？", "第三问如何？"],
        )
        self.assertIn("1.1 倍", qa_units[1].payload["answer"])

    def test_direct_native_split_main_label_can_cross_wrapped_q2_prompt(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活□新闻发布会□路演活动",
                "动类别□现场参观",
                "参与单位名称及人员姓名 某机构",
                "上市公司接待人员姓名 董事会秘书",
                "时间 2026年7月15日",
                "地点 珠海",
                "形式 现场会议",
                "1. 第一问如何？",
                "第一答完整。",
                "投资者关系活",
                "2. 第二问前半",
                "动主要内容介",
                "后半？",
                "绍",
                "第二答完整。",
                "附件清单(如有) 无",
                "日期 2026年7月15日",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [],
            },
            filing_type="performance_briefing",
            document_title="某公司业绩说明会投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(stats.native_text_qa_pairs_recovered, 2)
        self.assertEqual(
            [unit.payload["question"] for unit in qa_units],
            ["第一问如何？", "第二问前半后半？"],
        )
        blob = " ".join(str(unit.payload) for unit in qa_units)
        self.assertNotIn("动主要内容介", blob)
        self.assertNotIn("投资者关系活", blob)

    def test_direct_native_split_label_preserves_suffix_and_unmatched_answer(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 业绩说明会",
                "参与单位名称及人员姓名 某机构",
                "上市公司接待人员姓名 董事会秘书",
                "时间 2026年7月15日",
                "地点 珠海",
                "形式 现场会议",
                "1. 第一问如何？",
                "第一答开头。",
                "投资者关系活动主要",
                "内容介绍 第一答后缀。",
                "介绍",
                "2. 第二问如何？",
                "第二答完整。",
                "附件清单(如有) 无",
                "日期 2026年7月15日",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [],
            },
            filing_type="performance_briefing",
            document_title="某公司业绩说明会投资者关系活动记录表",
        )

        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(stats.native_text_qa_pairs_recovered, 2)
        first_answer = qa_units[0].payload["answer"]
        self.assertIn("第一答后缀。", first_answer)
        self.assertIn("介绍", first_answer)
        self.assertNotIn("内容介绍", first_answer)
        self.assertNotIn("投资者关系活动主要", first_answer)

    def test_direct_native_split_main_label_that_starts_after_q2_stays_closed(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 业绩说明会",
                "参与单位名称及人员姓名 某机构",
                "上市公司接待人员姓名 董事会秘书",
                "时间 2026年7月15日",
                "地点 珠海",
                "形式 现场会议",
                "1. 第一问如何？",
                "第一答完整。",
                "2. 第二问如何？",
                "第二答开头。",
                "投资者关系活",
                "动主要内容介",
                "绍",
                "第二答完整。",
                "附件清单(如有) 无",
                "日期 2026年7月15日",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [],
            },
            filing_type="performance_briefing",
            document_title="某公司业绩说明会投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_qa_pairs_recovered, 0)
        self.assertFalse(any(unit.payload_kind == "qa" for unit in units))

    def test_direct_native_without_transcript_or_main_label_stays_closed(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 业绩说明会",
                "参与单位名称及人员姓名 某机构",
                "上市公司接待人员姓名 董事会秘书",
                "时间 2026年7月15日",
                "地点 珠海",
                "形式 现场会议",
                "普通经营介绍",
                "1. 第一问如何？",
                "第一答完整。",
                "2. 第二问如何？",
                "第二答完整。",
                "日期 2026年7月15日",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [],
            },
            filing_type="performance_briefing",
            document_title="某公司业绩说明会投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_qa_pairs_recovered, 0)
        self.assertFalse(any(unit.payload_kind == "qa" for unit in units))

    def test_direct_native_main_label_after_second_question_stays_closed(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司股份有限公司投资者关系活动记录表",
                "投资者关系活动类别 业绩说明会",
                "参与单位名称及人员姓名 某机构",
                "上市公司接待人员姓名 董事会秘书",
                "时间 2026年7月15日",
                "地点 珠海",
                "形式 现场会议",
                "1. 第一问如何？",
                "第一答完整。",
                "2. 第二问如何？",
                "第二答中提到投资者关系活动。",
                "主要内容介绍只是第二答的普通措辞。",
                "附件清单(如有) 无",
                "日期 2026年7月15日",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [],
            },
            filing_type="performance_briefing",
            document_title="某公司业绩说明会投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_qa_pairs_recovered, 0)
        self.assertFalse(any(unit.payload_kind == "qa" for unit in units))

    def test_direct_native_transcript_fails_closed_without_scope_or_sequence(
        self,
    ) -> None:
        native_text = "\n".join(
            [
                "某公司投资者关系活动记录表",
                "投资者关系活动类别 特定对象调研",
                "参与单位名称及人员姓名 某机构",
                "时间 2026年7月15日",
                "地点 深圳",
                "形式 电话会议",
                "回答投资者提问",
                "1. 第一问？",
                "第一答。",
                "3. 第三问？",
                "第三答。",
                "日期 2026年7月15日",
            ]
        )
        normalized = {
            "native_text": {
                "status": "ok",
                "content_hash": "sha256:" + "c" * 64,
                "pages": [{"page_no": 1, "text": native_text}],
            },
            "elements": [{"kind": "text", "order_index": 0, "text": "普通年报正文。"}],
        }
        for filing_type in ("annual_report", "investor_relations"):
            with self.subTest(filing_type=filing_type):
                units, stats = build_unit_drafts_s1_s7(
                    normalized,
                    filing_type=filing_type,
                    document_title="某公司文档",
                )
                self.assertEqual(stats.native_text_qa_pairs_recovered, 0)
                self.assertFalse(
                    any(
                        (unit.artifact_locator or {}).get("source") == "native_text"
                        for unit in units
                    )
                )

    def test_native_shadow_unavailable_or_empty_marks_form_fallback_for_review(
        self,
    ) -> None:
        cases = (
            ("investor_relations", "unavailable", True),
            ("performance_briefing", "empty", True),
            ("annual_report", "unavailable", False),
            ("investor_relations", "ok", False),
        )
        for filing_type, status, expected_review in cases:
            with self.subTest(filing_type=filing_type, status=status):
                units, stats = build_unit_drafts_s1_s7(
                    {
                        "parser_diagnostics": {
                            "native_text_shadow": {
                                "status": status,
                                "error_code": (
                                    "timeout" if status == "unavailable" else None
                                ),
                            }
                        },
                        "elements": [
                            {
                                "kind": "text",
                                "raw_kind": "text",
                                "order_index": 1,
                                "text": "经营情况完整。",
                            }
                        ],
                    },
                    filing_type=filing_type,
                    document_title="某公司投资者关系活动记录表",
                )

                self.assertTrue(units)
                self.assertEqual(
                    {unit.quality_status for unit in units},
                    {"needs_review" if expected_review else "ok"},
                )
                self.assertEqual(
                    stats.needs_review_count,
                    len(units) if expected_review else 0,
                )

    def test_native_recovery_normalizes_text_heavy_form_with_mapped_list(self) -> None:
        document_heading = "华测检测认证集团股份有限公司投资者关系活动记录表"
        list_text = "\n".join(
            [
                "1、食农检测业务保持较快增长。",
                "2、汽车检测覆盖国内外知名车企。",
            ]
        )
        native_text = "\n".join(
            [
                document_heading,
                "一、公司上半年业绩情况",
                "收入和利润均",
                "投资者关系活动主",
                "保持增长。",
                "要内容介绍",
                "二、公司上半年经营亮点介绍",
                "公司持续推进国际化和精益管理。",
                list_text,
                "三、主要交流问题",
                "1. 第一问？",
                "答：第一答完整。",
                "2. 未来 1~2 年规划？",
                "答：第二答开头。",
                "第二答尾段。",
                "附件清单（如有） 《参与机构名单》",
                "日期 2023-08-11",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "8" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 0,
                        "heading_level": 1,
                        "text": document_heading,
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 1,
                        "table_caption": [document_heading],
                        "table": {
                            "headers": [],
                            "rows": [
                                ["投资者关系活动类别", "业绩说明会"],
                                [
                                    "投资者关系活动主要内容介绍",
                                    "一、公司上半年业绩情况\n收入和利润均",
                                ],
                            ],
                        },
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "保持增长。",
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 3,
                        "heading_level": 2,
                        "text": "二、公司上半年经营亮点介绍",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "text": "公司持续推进国际化和精益管理。",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "list",
                        "order_index": 5,
                        "text": list_text,
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 6,
                        "heading_level": 2,
                        "text": "三、主要交流问题",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 7,
                        "text": "1. 第一问？",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 8,
                        "text": "答：第一答完整。",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 9,
                        "text": r"2. 未来 1\~2 年规划？",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 10,
                        "text": "答：第二答开头。",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 11,
                        "table": {
                            "headers": [],
                            "rows": [
                                ["", "第二答尾段。"],
                                ["附件清单（如有）", "《参与机构名单》"],
                                ["日期", "2023-08-11"],
                            ],
                        },
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 12,
                        "heading_level": 2,
                        "text": "附件 1：《参与机构名单》",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 13,
                        "table": {
                            "headers": ["机构", "姓名"],
                            "rows": [["某某基金", "张三"]],
                        },
                    },
                ],
            },
            filing_type="investor_relations",
            document_title="华测检测：投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_sections_recovered, 3)
        self.assertEqual(stats.qa_form_carriers_replaced, 2)
        self.assertEqual(stats.needs_review_count, 0)
        self.assertEqual(
            [unit.payload_kind for unit in units],
            ["table", "text", "text", "qa", "qa", "table", "table"],
        )
        self.assertEqual(units[0].heading_path, [document_heading])
        self.assertEqual(units[1].heading_path, ["一、公司上半年业绩情况"])
        self.assertEqual(units[2].heading_path, ["二、公司上半年经营亮点介绍"])
        self.assertIn("汽车检测覆盖国内外知名车企", str(units[2].payload))
        qa_units = [unit for unit in units if unit.payload_kind == "qa"]
        self.assertEqual(
            [unit.heading_path for unit in qa_units],
            [["三、主要交流问题"], ["三、主要交流问题"]],
        )
        self.assertEqual(qa_units[1].payload["answer"], "第二答开头。第二答尾段。")
        footer = next(unit for unit in units if "附件清单" in str(unit.payload))
        self.assertEqual(footer.heading_path, ["华测检测：投资者关系活动记录表"])
        attachment = next(unit for unit in units if "某某基金" in str(unit.payload))
        self.assertEqual(attachment.heading_path, ["附件 1：《参与机构名单》"])

    def test_native_recovery_keeps_nonempty_unknown_element(self) -> None:
        native_text = "\n".join(
            [
                "一、经营情况",
                "公司经营稳定。",
                "二、主要交流问题",
                "1、收入如何？答：收入保持增长。",
                "日期 2026-07-15",
            ]
        )
        mineru_only = "MinerU 未分类但非空的业务内容。"
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "9" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": native_text.replace("\n日期 2026-07-15", ""),
                    },
                    {
                        "kind": "unknown",
                        "raw_kind": "list",
                        "order_index": 2,
                        "content": mineru_only,
                    },
                ],
            },
            filing_type="investor_relations",
            document_title="某公司：投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_sections_recovered, 0)
        self.assertIn(mineru_only, " ".join(str(unit.payload) for unit in units))

    def test_native_recovery_preserves_mineru_only_fact_by_falling_back(self) -> None:
        native_text = "\n".join(
            [
                "一、经营情况",
                "公司经营稳定。",
                "二、主要交流问题",
                "1、收入如何？答：收入保持增长。",
                "日期 2026-07-15",
            ]
        )
        mineru_only = "MinerU独有关键事实：毛利率下降1.25%。"
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "2" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": [],
                            "rows": [
                                [
                                    native_text.replace("\n日期 2026-07-15", "")
                                    + mineru_only
                                ]
                            ],
                        },
                    }
                ],
            },
            filing_type="investor_relations",
            document_title="某公司：投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_sections_recovered, 0)
        self.assertIn(mineru_only, " ".join(str(unit.payload) for unit in units))

    def test_native_recovery_rejects_briefing_notice_without_real_qa(self) -> None:
        notice = "\n".join(
            [
                "一、说明会安排",
                "本次说明会于十五点召开。",
                "二、投资者提问方式",
                "投资者可在网络平台提前提交问题。",
                "日期 2026-07-15",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "3" * 64,
                    "pages": [{"page_no": 1, "text": notice}],
                },
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": notice,
                    }
                ],
            },
            filing_type="performance_briefing",
            document_title="某公司关于召开年度业绩说明会的公告",
        )

        self.assertEqual(stats.native_text_sections_recovered, 0)
        self.assertNotIn(
            "native_text",
            " ".join(str(unit.artifact_locator) for unit in units),
        )

    def test_native_recovery_keeps_multi_column_table_before_footer(self) -> None:
        native_text = "\n".join(
            [
                "一、经营情况",
                "公司经营稳定。",
                "二、主要交流问题",
                "1、收入如何？答：收入保持增长。",
                "日期 2026-07-15",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "4" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": native_text.replace("\n日期 2026-07-15", ""),
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 2,
                        "table": {
                            "headers": [],
                            "rows": [
                                ["指标", ""],
                                ["", "金额"],
                                ["营业收入", ""],
                                ["", "10亿元"],
                                ["日期", "2026-07-15"],
                            ],
                        },
                    },
                ],
            },
            filing_type="investor_relations",
            document_title="某公司：投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_sections_recovered, 0)
        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertIn("营业收入", str(table.payload))

    def test_native_recovery_keeps_multi_column_table_inside_first_form(self) -> None:
        native_text = "\n".join(
            [
                "一、经营情况",
                "指标 金额",
                "营业收入 10亿元",
                "二、主要交流问题",
                "1、收入如何？答：收入保持增长。",
                "日期 2026-07-15",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "5" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 1,
                        "table": {
                            "headers": [],
                            "rows": [
                                ["表单标签", "一、经营情况"],
                                ["指标", "金额"],
                                ["营业收入", "10亿元"],
                                [
                                    "二、主要交流问题",
                                    "1、收入如何？答：收入保持增长。",
                                ],
                            ],
                        },
                    }
                ],
            },
            filing_type="investor_relations",
            document_title="某公司：投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_sections_recovered, 0)
        table = next(unit for unit in units if unit.payload_kind == "table")
        self.assertIn("营业收入", str(table.payload))
        self.assertIn("10亿元", str(table.payload))

    def test_native_recovery_keeps_sparse_multi_column_middle_table(self) -> None:
        first_half = "第一列保留真实结构。" * 35
        second_half = "第二列同样是结构化内容。" * 35
        native_text = "\n".join(
            [
                "一、经营情况",
                "公司经营稳定。",
                "二、主要交流问题",
                f"1、收入如何？答：{first_half}{second_half}",
                "日期 2026-07-15",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "6" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "heading_level": 2,
                        "text": "一、经营情况",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "text": "公司经营稳定。",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 3,
                        "table": {
                            "headers": [],
                            "rows": [
                                [f"二、主要交流问题 1、收入如何？答：{first_half}", ""],
                                ["", second_half],
                            ],
                        },
                    },
                ],
            },
            filing_type="investor_relations",
            document_title="某公司：投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_sections_recovered, 0)
        table_payloads = [
            unit.payload for unit in units if unit.payload_kind == "table"
        ]
        table_payloads.extend(
            part
            for unit in units
            if unit.payload_kind == "mixed"
            for part in unit.payload.get("parts", [])
            if part.get("kind") == "table"
        )
        self.assertTrue(table_payloads)
        self.assertIn("第一列保留真实结构", str(table_payloads))
        self.assertIn("第二列同样是结构化内容", str(table_payloads))

    def test_native_qa_recovery_fails_closed_on_missing_question_ordinal(self) -> None:
        native_text = "\n".join(
            [
                "一、经营情况",
                "经营稳定。",
                "二、主要交流问题",
                "1、第一问？答：第一答。",
                "3、第三问？答：第三答。",
                "日期 2026-07-15",
            ]
        )
        units, stats = build_unit_drafts_s1_s7(
            {
                "native_text": {
                    "status": "ok",
                    "content_hash": "sha256:" + "1" * 64,
                    "pages": [{"page_no": 1, "text": native_text}],
                },
                "elements": [
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 1,
                        "text": native_text.replace("\n日期 2026-07-15", ""),
                    }
                ],
            },
            filing_type="investor_relations",
            document_title="某公司：投资者关系活动记录表",
        )

        self.assertEqual(stats.native_text_sections_recovered, 2)
        qa_section = next(unit for unit in units if "第三问" in str(unit.payload))
        self.assertEqual(qa_section.payload_kind, "text")
        self.assertEqual(qa_section.quality_status, "needs_review")
        self.assertNotIn("日期", str(qa_section.payload))
        self.assertFalse(any(unit.payload_kind == "qa" for unit in units))

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

    def test_terminal_post_signature_attachment_is_document_root_sibling(
        self,
    ) -> None:
        document_title = "关于授权公司及控股子公司对外提供担保的公告"
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 0,
                        "page_no": 1,
                        "heading_level": 1,
                        "text": document_title,
                    },
                    {
                        "kind": "heading",
                        "raw_kind": "text",
                        "order_index": 1,
                        "page_no": 2,
                        "heading_level": 1,
                        "text": "三、转授权安排及授权有效期",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 2,
                        "page_no": 2,
                        "text": "本事项尚需提交股东大会审议。",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 3,
                        "page_no": 2,
                        "text": "特此公告。",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 4,
                        "page_no": 2,
                        "text": "某某股份有限公司",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 5,
                        "page_no": 2,
                        "text": "董事会",
                    },
                    {
                        "kind": "text",
                        "raw_kind": "text",
                        "order_index": 6,
                        "page_no": 2,
                        "text": "二〇二五年三月三十一日",
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 7,
                        "page_no": 3,
                        "bbox": [40, 137, 928, 858],
                        "table_caption": ["附件:", "单位：人民币万元"],
                        "table": {
                            "headers": ["担保对象", "担保额度"],
                            "rows": [["子公司甲", "1000"]],
                        },
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 8,
                        "page_no": 4,
                        "bbox": [40, 58, 928, 833],
                        "table_caption": [],
                        "table": {
                            "headers": [],
                            "rows": [["子公司乙", "2000"]],
                        },
                    },
                ]
            },
            filing_type="financing",
            document_title=document_title,
        )

        attachment = next(unit for unit in units if "子公司甲" in str(unit.payload))
        self.assertEqual(attachment.heading_path, [document_title, "附件:"])
        self.assertNotIn("三、转授权安排及授权有效期", attachment.heading_path)
        self.assertEqual(attachment.title, "附件:")
        self.assertIn("子公司乙", str(attachment.payload))

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

        # Explicit sibling topics stay addressable even when both are tiny.
        self.assertEqual([unit.payload_kind for unit in units], ["text", "text"])
        bankruptcy, litigation = units
        self.assertEqual(
            bankruptcy.heading_path,
            ["第五节 重要事项", "一、破产重整相关事项"],
        )
        self.assertEqual(bankruptcy.applicability, "not_applicable")
        self.assertIn("不适用", bankruptcy.payload["text"])
        self.assertEqual(
            litigation.heading_path,
            ["第五节 重要事项", "二、重大诉讼事项"],
        )
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
        self.assertEqual(units[0].payload["text"], "适用")
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
        self.assertEqual(stats.dropped_by_kind["page_furniture"], 1)

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

        # Short 'other' doc collapses to one document unit; the red line is
        # that both tips stay visible to L2 inside it.
        self.assertEqual([unit.payload_kind for unit in units], ["mixed"])
        parts = units[0].payload["parts"]
        self.assertEqual(
            [part["heading_path"] for part in parts],
            [["重要提示"], ["风险提示"]],
        )
        self.assertIn("退市风险", parts[0]["text"])
        self.assertIn("原材料价格波动", parts[1]["text"])

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

        self.assertEqual(units, [])
        self.assertEqual(stats.dropped_by_kind["page_furniture"], 2)


if __name__ == "__main__":
    unittest.main()
