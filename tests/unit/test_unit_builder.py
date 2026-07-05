"""Unit builder rule and S1-S7 stage tests."""

from __future__ import annotations

import unittest

from disclosure_anchor.adapters.unit_builder import rules
from disclosure_anchor.adapters.unit_builder.builder import (
    BuildStats,
    PreparedElement,
    UnitDraft,
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
)


class UnitBuilderTests(unittest.TestCase):
    def test_rules_version_and_fixed_tables(self) -> None:
        self.assertEqual(rules.RULES_VERSION, "ub-2026.07-1")
        self.assertEqual(rules.HEADING_RULESET_ID, "cn_a_v1")
        self.assertEqual(rules.SKIP_SECTION_TITLES, {"释义", "目录", "备查文件"})
        self.assertEqual(rules.GIBBERISH_RATIO_MAX, 0.30)

    def test_s1_drops_furniture_and_separator_but_records_stats(self) -> None:
        result = s1_preprocess_elements(
            [
                {"kind": "page_furniture", "raw_kind": "header", "order_index": 1},
                {"kind": "text", "raw_kind": "text", "order_index": 2, "text": "---\n正文\u0001"},
                {"kind": "unknown", "raw_kind": "mystery", "order_index": 3},
            ]
        )

        self.assertEqual([item.text for item in result.elements], ["正文"])
        self.assertEqual(result.stats.dropped_by_kind["page_furniture"], 1)
        self.assertEqual(result.stats.dropped_by_kind["unknown"], 1)
        self.assertEqual(result.stats.dropped_unknown_by_raw_kind["mystery"], 1)

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

    def test_s2_heading_tree_excludes_questions(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(
                    kind="heading",
                    order_index=1,
                    text="第一节 重要提示、目录和释义",
                    heading_level=1,
                ),
                PreparedElement(kind="text", order_index=2, text="2.请介绍集团业务矩阵？"),
                PreparedElement(kind="text", order_index=3, text="答:业务覆盖多个领域。"),
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

    def test_s3_splits_many_long_numbered_items(self) -> None:
        long_items = "\n".join(
            f"{idx}、" + "经营情况说明" * 12
            for idx in range(1, 4)
        )
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

        self.assertEqual(len(units), 3)
        self.assertTrue(all(unit.payload_kind == "text" for unit in units))

    def test_full_s1_s7_split_numbered_text_stays_before_following_table(self) -> None:
        long_items = "\n".join(
            f"{idx}、" + "经营情况说明" * 12
            for idx in range(1, 4)
        )

        units, _ = build_unit_drafts_s1_s7(
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

        self.assertEqual(
            [unit.payload_kind for unit in units],
            ["text", "text", "text", "table"],
        )
        self.assertEqual(units[0].payload["text"].split("、", 1)[0], "1")
        self.assertEqual(units[1].payload["text"].split("、", 1)[0], "2")
        self.assertEqual(units[2].payload["text"].split("、", 1)[0], "3")

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
        self.assertEqual(parsed.units[0].payload["question"], "美国加征关税对公司有什么影响？")
        self.assertEqual(parsed.units[0].payload["answer"], "美国收入占比很低。")

        self.assertTrue(s4_build_qa_units("答:没有问题。", source=source).unstable)
        self.assertTrue(
            s4_build_qa_units("问:问题？\n答:一\n回复:二", source=source).unstable
        )
        self.assertTrue(s4_build_qa_units("问:问题？", source=source).unstable)

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

        self.assertEqual(units[0].payload, {"caption": ["失败表"], "raw_html": "<table>", "notes": ["注"]})
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
        self.assertEqual(units[0].payload, {"caption": ["失败表"], "raw_html": "<table>", "notes": ["注"]})
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
        self.assertEqual(set(table_unit.payload), {"caption", "unit", "headers", "rows", "notes"})
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

        self.assertEqual([unit.title for unit in units], ["重要提示", "风险提示"])
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

        self.assertEqual(units, [])
        self.assertEqual(stats.dropped_by_kind["page_furniture"], 2)


if __name__ == "__main__":
    unittest.main()
