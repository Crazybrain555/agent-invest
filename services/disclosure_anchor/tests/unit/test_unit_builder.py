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
        self.assertEqual(rules.RULES_VERSION, "ub-2026.07-9")
        self.assertEqual(rules.HEADING_RULESET_ID, "cn_a_v4")
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

    def test_s3_keeps_numbered_enumeration_as_one_block(self) -> None:
        # ub-2026.07-5: enumerated lines are one business block — splitting
        # them into per-line units was the round3 over-fragmentation defect.
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

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload_kind, "text")
        self.assertEqual(units[0].payload["text"], long_items)

    def test_full_s1_s7_short_other_doc_collapses_to_document_unit(self) -> None:
        long_items = "\n".join(
            f"{idx}、" + "经营情况说明" * 12
            for idx in range(1, 4)
        )

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
                    {"kind": "text", "raw_kind": "text", "order_index": 3, "text": filler},
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
                        "table": {"headers": ["项目", "金额"], "rows": [["费用化", "100"]]},
                    },
                    {
                        "kind": "table",
                        "raw_kind": "table",
                        "order_index": 7,
                        "table_caption": ["研发人员情况"],
                        "table": {"headers": ["类别", "人数"], "rows": [["硕士", "30"]]},
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
                    {"kind": "heading", "raw_kind": "text", "order_index": 1,
                     "heading_level": 1, "text": "第三节 管理层讨论与分析"},
                    {"kind": "heading", "raw_kind": "text", "order_index": 2,
                     "heading_level": 2, "text": "一、业务概况"},
                    {"kind": "text", "raw_kind": "text", "order_index": 3, "text": filler},
                    {"kind": "heading", "raw_kind": "text", "order_index": 4,
                     "heading_level": 2, "text": "二、主营业务分析"},
                    {"kind": "text", "raw_kind": "text", "order_index": 5,
                     "text": "报告期内经营情况如下。"},
                    {"kind": "table", "raw_kind": "table", "order_index": 6,
                     "table_caption": ["营业收入构成"],
                     "table": {"headers": ["项目", "金额"], "rows": [["主营", "100"]]}},
                    {"kind": "table", "raw_kind": "table", "order_index": 7,
                     "table_caption": ["存货分类构成"],
                     "table": {"headers": ["类别", "金额"], "rows": [["原材料", "10"]]}},
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
        self.assertEqual(
            section.semantic_keys, ["inventory_breakdown", "revenue_breakdown"]
        )

    def test_collapsed_document_title_uses_registry_title(self) -> None:
        # Codex round4 P1#4: the in-PDF document-name line is often dropped as
        # cover prelude, so 第一章 must not become the document unit's title.
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {"kind": "heading", "raw_kind": "text", "order_index": 1,
                     "heading_level": 1, "text": "第一章 总则"},
                    {"kind": "text", "raw_kind": "text", "order_index": 2,
                     "text": "第一条 为完善治理结构，制定本办法。"},
                    {"kind": "heading", "raw_kind": "text", "order_index": 3,
                     "heading_level": 1, "text": "第二章 附则"},
                    {"kind": "text", "raw_kind": "text", "order_index": 4,
                     "text": "第二条 本办法自发布之日起施行。"},
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
                PreparedElement(kind="heading", order_index=1, heading_level=1,
                                text="第八节 财务报告"),
                PreparedElement(kind="heading", order_index=2, heading_level=2,
                                text="42、其他重要的会计政策和会计估计"),
                PreparedElement(kind="heading", order_index=3, heading_level=2,
                                text="与回购公司股份相关的会计处理方法"),
                PreparedElement(kind="text", order_index=4,
                                text="按实际支付的金额作为库存股处理。"),
                PreparedElement(kind="heading", order_index=5, heading_level=2,
                                text="43、重要会计政策和会计估计变更"),
                PreparedElement(kind="text", order_index=6, text="无变更。"),
            ]
        )

        self.assertEqual(
            placed[0].heading_path,
            ["第八节 财务报告", "42、其他重要的会计政策和会计估计",
             "与回购公司股份相关的会计处理方法"],
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
                PreparedElement(kind="heading", order_index=1, heading_level=1,
                                text="第八节 财务报告"),
                PreparedElement(kind="heading", order_index=2, heading_level=2,
                                text="十二、与金融工具相关的风险"),
                PreparedElement(kind="heading", order_index=3, heading_level=2,
                                text="(一) 信用风险"),
                PreparedElement(kind="text", order_index=4, text="信用风险管理。"),
                PreparedElement(kind="heading", order_index=5, heading_level=2,
                                text="(二) 流动性风险"),
                PreparedElement(kind="text", order_index=6, text="流动性管理。"),
                PreparedElement(kind="heading", order_index=7, heading_level=2,
                                text="三、（市场风险）"),
                PreparedElement(kind="text", order_index=8, text="市场风险说明。"),
            ]
        )

        market = placed[-1]
        self.assertEqual(
            market.heading_path,
            ["第八节 财务报告", "十二、与金融工具相关的风险", "三、（市场风险）"],
        )

    def test_s2_footnote_line_never_becomes_heading(self) -> None:
        placed = s2_apply_heading_tree(
            [
                PreparedElement(kind="heading", order_index=1, heading_level=1,
                                text="十一、关联方及关联交易"),
                PreparedElement(kind="heading", order_index=2, heading_level=2,
                                text="[注] 该金额系双方 2025 年 1-2 月交易金额"),
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
                    {"kind": "heading", "raw_kind": "text", "order_index": 1,
                     "heading_level": 1, "text": "重要提示"},
                    {"kind": "text", "raw_kind": "text", "order_index": 2,
                     "text": ("本公司董事会及全体董事保证本公告内容不存在任何虚假记载、"
                              "误导性陈述或者重大遗漏，并对其内容的真实性、准确性和完整性"
                              "承担法律责任。\n公司存在退市风险，请投资者注意。")},
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
                        "merged_cells": [{"row": 2, "col": 0, "rowspan": 1, "colspan": 2}],
                    },
                )
            ],
            stats,
        )

        self.assertEqual(units[0].payload["rows"], [["收入", "100"], ["成本", "60"]])
        self.assertEqual(stats.dropped_blank_table_rows, 1)
        locator = units[0].artifact_locator or {}
        self.assertEqual(locator["merged_cells"], [{"row": 1, "col": 0, "rowspan": 1, "colspan": 2}])

    def test_board_resolution_approval_style_proposals_group(self) -> None:
        # Codex round7 平安董事会决议: "一、审议通过了《…议案》" style + 表决行
        # must become one proposal unit each, not one blob.
        units, stats = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {"kind": "text", "raw_kind": "text", "order_index": 1,
                     "text": ("本行第十三届董事会第五次会议以书面传签方式召开。\n"
                              "本次会议审议通过了如下议案：\n"
                              "一、审议通过了《关于修订董事会专门委员会工作细则的议案》。\n"
                              "本议案同意票12票，反对票0票，弃权票0票。\n"
                              "二、审议通过了《关于修订商业行为和道德守则的议案》。\n"
                              "本议案同意票12票，反对票0票，弃权票0票。")},
                ]
            },
            filing_type="other",
        )

        proposals = [u for u in units if u.payload_kind == "mixed"
                     and u.payload.get("semantic_type") == "meeting_proposal"]
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
                    {"kind": "heading", "raw_kind": "text", "order_index": 1,
                     "heading_level": 1, "text": "二、议案审议情况"},
                    {"kind": "text", "raw_kind": "text", "order_index": 2,
                     "text": "7. 议案名称：关于选举董事的议案\n审议结果：通过"},
                    {"kind": "table", "raw_kind": "table", "order_index": 3,
                     "table": {"headers": ["同意"], "rows": [["99%"]]}},
                    {"kind": "table", "raw_kind": "table", "order_index": 4,
                     "table_caption": ["8. 议案名称：关于选举监事的议案"],
                     "table": {"headers": ["同意"], "rows": [["98%"]]}},
                ]
            },
            filing_type="other",
        )

        titles = [u.title for u in units if u.payload_kind == "mixed"]
        self.assertIn("7. 议案名称：关于选举董事的议案", titles)
        self.assertIn("8. 议案名称：关于选举监事的议案", titles)

    def test_flat_document_units_anchor_under_document_title(self) -> None:
        # Codex round7 美的 IR: form-table filings have no headings at all —
        # units anchored under the registry title, never heading_path=[].
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {"kind": "table", "raw_kind": "table", "order_index": 1,
                     "table": {"headers": ["活动类别"], "rows": [["特定对象调研"]]}},
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
        shredded = "机系列销量超56万套。户数量持续增长。" * 30 + "3. 公司业务当前的进展？"
        units, _ = build_unit_drafts_s1_s7(
            {
                "elements": [
                    {"kind": "table", "raw_kind": "table", "order_index": 1,
                     "table": {"headers": [shredded], "rows": [["答：进展顺利。"]]}},
                ]
            },
            filing_type="investor_relations",
            document_title="投资者关系活动记录表",
        )

        self.assertEqual(units[0].payload_kind, "table")
        self.assertEqual(units[0].quality_status, "needs_review")

    def test_full_s1_s7_oversized_leaf_still_merges_whole(self) -> None:
        # Splitting one topic by payload kind is the defect — an oversized
        # leaf section still becomes one mixed unit.
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
                        "text": "经营情况说明。" * 700,
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

        self.assertEqual([unit.payload_kind for unit in units], ["mixed"])
        self.assertEqual(units[0].heading_path, ["第一节 经营情况"])

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
                        "table": {"headers": ["股东类型", "同意"], "rows": [["A股", "99%"]]},
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
                        "table": {"headers": ["股东类型", "同意"], "rows": [["A股", "98%"]]},
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
        self.assertIn("会议决定，聘请天健会计师事务所。", first.payload["parts"][2]["text"])
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

    def test_full_s1_s7_headerless_prefix_anchors_under_stable_heading(self) -> None:
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

        self.assertEqual(stats.anchored_header_units, 1)
        by_path = {tuple(unit.heading_path): unit for unit in units}
        header = by_path[(rules.DOCUMENT_HEADER_ANCHOR,)]
        self.assertEqual(header.payload["text"], "公告编号：临 2026-026")

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
                        "table": {"headers": ["年度", "毛利率"], "rows": [["2025", "30%"]]},
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

    def test_cover_prelude_dropped_before_first_structural_section(self) -> None:
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

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].heading_path, ["第一节 重要提示"])
        self.assertEqual(stats.dropped_cover_prelude, 3)

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

        # Tiny sibling sections group into one 第五节 unit (ub-2026.07-5);
        # per-section flags live on the parts, and the unit-level column stays
        # None because the merged sections disagree.
        self.assertEqual([unit.payload_kind for unit in units], ["mixed"])
        section = units[0]
        self.assertEqual(section.heading_path, ["第五节 重要事项"])
        self.assertIsNone(section.applicability)
        bankruptcy, litigation = section.payload["parts"]
        self.assertEqual(bankruptcy["local_heading"], ["一、破产重整相关事项"])
        self.assertEqual(bankruptcy["applicability"], "not_applicable")
        self.assertIn("不适用", bankruptcy["text"])
        self.assertEqual(litigation["local_heading"], ["二、重大诉讼事项"])
        self.assertEqual(litigation["applicability"], "applicable")
        # The leading marker line is stripped; the prose remains.
        self.assertEqual(litigation["text"], "公司报告期内存在如下诉讼。")

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
