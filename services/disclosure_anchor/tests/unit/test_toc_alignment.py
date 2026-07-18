"""Unit tests for TOC parsing/matching (shared module + audit matcher)."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

from disclosure_anchor.adapters.unit_builder.toc_outline import (
    analyze_toc_block,
    is_page_annotated_entry,
    parse_toc_titles,
    strip_outline_enumerator,
    strip_section_enumerator,
    toc_declared_root_keys,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from audit_toc_alignment import match_titles_to_tree, normalize_heading  # noqa: E402


class TocEnumerationFamilyTests(unittest.TestCase):
    """Layout-accident robustness: what defines a TOC is its enumeration
    structure, not where page numbers were typeset or the element kind."""

    def test_bare_titles_with_detached_page_column_qualify_when_anchored(
        self,
    ) -> None:
        # Designed annual reports typeset page numbers in a detached column
        # (an aside_text block of digits per the parser contract); titles
        # arrive as bare lines without any page number.
        block = "\n".join(
            [
                "释义",
                "重大风险提示",
                "重要提示",
                "董事长致辞",
                "第一章 公司简介",
                "第二章 会计数据和财务指标摘要",
                "第三章 管理层讨论与分析",
                "19 3.1 总体经营情况分析",
                "19 3.2 利润表分析",
                "第四章 环境、社会与治理(ESG)",
                "第八章 财务报告",
            ]
        )
        anchored = analyze_toc_block(block, marker_anchored=True)
        self.assertTrue(anchored.qualified)
        self.assertIn("公司简介", anchored.keys)
        self.assertIn("释义", anchored.keys)
        self.assertIn("董事长致辞", anchored.keys)
        self.assertNotIn("总体经营情况分析", anchored.keys)
        # Bare lines never count off the marker-anchored TOC page.
        self.assertFalse(analyze_toc_block(block, marker_anchored=False).qualified)

    def test_arabic_numbered_toc_qualifies_only_anchored(self) -> None:
        # Some issuers enumerate top-level sections "1. 释义" with no
        # statutory 第X章 anywhere; ascending single-level numbering on the
        # marker page is that document's declared top level.
        block = "\n".join(
            [
                "1. 释义....5",
                "2. 2023 年主要排名与奖项....7",
                "3. 重要提示....8",
                "4. 公司基本情况简介....9",
                "7.1 经济金融及监管环境....20",
                "专栏：个人财富管理业务取得新突破....47",
                "10. 公司治理报告....133",
            ]
        )
        anchored = analyze_toc_block(block, marker_anchored=True)
        self.assertTrue(anchored.qualified)
        self.assertIn("释义", anchored.keys)
        self.assertIn("2023年主要排名与奖项", anchored.keys)
        self.assertIn("公司治理报告", anchored.keys)
        # Multi-level dotted numbering is sub-structure; a feature line
        # after sub-structure nests inside a chapter and never declares.
        self.assertNotIn("经济金融及监管环境", anchored.keys)
        self.assertNotIn("专栏个人财富管理业务取得新突破", anchored.keys)
        self.assertFalse(analyze_toc_block(block, marker_anchored=False).qualified)

    def test_space_numbered_toc_variant(self) -> None:
        block = "\n".join(
            [
                "释义 5",
                "1 财务摘要 7",
                "2 公司基本情况 9",
                "3 管理层讨论与分析 11",
                "3.1 财务回顾 11",
                "4 公司治理 57",
            ]
        )
        anchored = analyze_toc_block(block, marker_anchored=True)
        self.assertTrue(anchored.qualified)
        self.assertIn("财务摘要", anchored.keys)
        self.assertIn("管理层讨论与分析", anchored.keys)
        self.assertIn("释义", anchored.keys)
        self.assertNotIn("财务回顾", anchored.keys)

    def test_chinese_ordinal_toc_needs_ascending_run(self) -> None:
        entries = [
            "一、董事会和管理层声明 ……1",
            "二、基本情况 …… 2",
            "三、主要指标表 7",
            "四、风险管理能力 ……11",
            "五、风险综合评级 12",
        ]
        anchored = analyze_toc_block("\n".join(entries), marker_anchored=True)
        self.assertTrue(anchored.qualified)
        self.assertIn("董事会和管理层声明", anchored.keys)
        # Without page numbers the ascending run is the only TOC evidence:
        # shuffled bare ordinals are a reference list, not a TOC in order.
        shuffled = "\n".join(
            [
                "三、主要指标表",
                "一、董事会和管理层声明",
                "五、风险综合评级",
                "二、基本情况",
                "四、风险管理能力",
            ]
        )
        self.assertFalse(
            analyze_toc_block(shuffled, marker_anchored=True).qualified
        )

    def test_statutory_top_family_excludes_chinese_sub_entries(self) -> None:
        block = "\n".join(
            [
                "第一章 受托管理的可转换公司债券概况....3",
                "一、发行人主体名称....3",
                "二、公司债券概况....3",
                "第二章 发行人 2024 年度经营及财务状况....4",
                "一、发行人基本情况....4",
                "第三章 发行人募集资金使用及专项账户运作情况....7",
            ]
        )
        analysis = analyze_toc_block(block, marker_anchored=False)
        self.assertTrue(analysis.qualified)
        self.assertIn("受托管理的可转换公司债券概况", analysis.keys)
        self.assertNotIn("发行人主体名称", analysis.keys)

    def test_plain_paged_entries_need_the_marker_page(self) -> None:
        # A page-numbered feature box in the body (no 目录 marker) never
        # declares the outline; the same shape on the marker page is the
        # document's own TOC even with no enumeration anywhere.
        box = "\n".join(
            [
                "21 热点问题一 高质量发展业绩亮点",
                "22 热点问题二 持续提升服务实体经济质效",
                "23 热点问题三 全面风险管理与资产质量",
                "24 热点问题四 基础工程夯实生态化基础",
                "25 热点问题五 数字工行建设持续深化",
            ]
        )
        self.assertFalse(analyze_toc_block(box, marker_anchored=False).qualified)
        self.assertTrue(analyze_toc_block(box, marker_anchored=True).qualified)

    def test_unenumerated_plain_toc_qualifies_on_marker_page(self) -> None:
        # 人保 shape: bare "title page" pairs, zero enumerators anywhere.
        block = "\n".join(
            [
                "重要提示 2",
                "释义 3",
                "核心竞争力与经营亮点 4",
                "财务指标 8",
                "管理层讨论与分析 11",
                "内含价值 45",
                "备查文件目录 58",
                "财务报告 59",
            ]
        )
        anchored = analyze_toc_block(block, marker_anchored=True)
        self.assertTrue(anchored.qualified)
        self.assertIn("内含价值", anchored.keys)
        self.assertIn("管理层讨论与分析", anchored.keys)
        self.assertFalse(analyze_toc_block(block, marker_anchored=False).qualified)

    def test_part_headers_with_leading_page_lists_qualify(self) -> None:
        # 国泰君安 shape: bare part headers ("1 关于我们") between list
        # blocks whose lines carry leading page numbers.
        block = "\n".join(
            [
                "1 关于我们",
                "4 重要提示",
                "6 释义",
                "8 公司简介",
                "16 业绩概览",
                "2 战略与经营分析",
                "21 管理层讨论与分析",
                "3 公司治理",
                "45 公司治理",
                "54 重要事项",
            ]
        )
        anchored = analyze_toc_block(block, marker_anchored=True)
        self.assertTrue(anchored.qualified)
        self.assertIn("关于我们", anchored.keys)
        self.assertIn("管理层讨论与分析", anchored.keys)

    def test_bare_body_prose_is_not_an_entry(self) -> None:
        block = "\n".join(
            [
                "第一章 公司简介",
                "第二章 会计数据",
                "本公司已在本报告中详细描述存在的主要风险及采取的应对措施，详情请参阅第三章有关风险管理的内容。",
                "指定的信息披露媒体和网站：",
            ]
        )
        titles, _ = parse_toc_titles(block, include_bare=True)
        self.assertEqual(titles, ["第一章 公司简介", "第二章 会计数据"])

    def test_strip_outline_enumerator_families(self) -> None:
        self.assertEqual(strip_outline_enumerator("第三章 管理层讨论与分析"), "管理层讨论与分析")
        self.assertEqual(strip_outline_enumerator("一、基本情况"), "基本情况")
        self.assertEqual(strip_outline_enumerator("1. 释义"), "释义")
        self.assertEqual(strip_outline_enumerator("13. 环境和社会责任"), "环境和社会责任")
        self.assertEqual(strip_outline_enumerator("1 财务摘要"), "财务摘要")
        # Sub-structure enumerators stay intact.
        self.assertEqual(strip_outline_enumerator("3.1 财务回顾"), "3.1 财务回顾")
        self.assertEqual(strip_outline_enumerator("（一）保证"), "（一）保证")
        self.assertEqual(strip_outline_enumerator("2023 年度报告"), "2023 年度报告")

    def test_is_page_annotated_entry(self) -> None:
        self.assertTrue(is_page_annotated_entry("第一章 公司简介 5"))
        self.assertTrue(is_page_annotated_entry("释义 5"))
        self.assertTrue(is_page_annotated_entry("第三章 管理层讨论与分析 …… 17"))
        self.assertFalse(is_page_annotated_entry("第一章 公司简介"))
        self.assertFalse(is_page_annotated_entry("重要提示"))


class TocParsingTests(unittest.TestCase):
    def test_trailing_page_grammar(self) -> None:
        text = "\n".join(
            [
                "第一节 重要提示、目录和释义....1",
                "第二节 致股东....3",
                "第四节 董事会报告 …… 10",
                "第九节 财务报告 …… 140",
            ]
        )
        titles, unparsed = parse_toc_titles(text)
        self.assertEqual(unparsed, 0)
        self.assertEqual(titles[0], "第一节 重要提示、目录和释义")
        self.assertEqual(titles[-1], "第九节 财务报告")

    def test_leading_page_grammar_wins_by_majority(self) -> None:
        text = "\n".join(
            [
                "2 释义",
                "3 重要提示",
                "10 第一章 公司简介",
                "18 3.1 总体经营情况分析",
            ]
        )
        titles, _ = parse_toc_titles(text)
        self.assertIn("释义", titles)
        self.assertIn("3.1 总体经营情况分析", titles)
        self.assertIn("第一章 公司简介", titles)

    def test_noise_lines_are_not_titles(self) -> None:
        titles, _ = parse_toc_titles("目\n录\n....\n123\n")
        self.assertEqual(titles, [])

    def test_wrapped_toc_entry_joins_with_next_line(self) -> None:
        text = "\n".join(
            [
                "一、信息披露义务人及其一致行动人....3",
                "四、本次权益变动不超过公",
                "司已发行股份 5% 的情况....8",
                "五、前六个月买卖上市公司股票的情况....12",
            ]
        )
        titles, unparsed = parse_toc_titles(text)
        self.assertIn("四、本次权益变动不超过公司已发行股份 5% 的情况", titles)
        self.assertEqual(unparsed, 0)

    def test_near_match_tolerates_single_character_drift(self) -> None:
        segments = {normalize_heading("第五节 前六个月内买卖上市公司股票的情况")}
        matched, missing = match_titles_to_tree(
            ["第五节 前六个月买卖上市公司股票的情况"], segments
        )
        self.assertEqual(missing, [])
        self.assertEqual(len(matched), 1)

    def test_strip_section_enumerator(self) -> None:
        self.assertEqual(strip_section_enumerator("第三章 管理层讨论与分析"), "管理层讨论与分析")
        self.assertEqual(strip_section_enumerator("第十节 财务报告"), "财务报告")
        self.assertEqual(strip_section_enumerator("重要提示"), "重要提示")

    def test_toc_declared_root_keys_top_level_entries(self) -> None:
        toc = "\n".join(
            [
                "2 释义",
                "10 第一章 公司简介",
                "14 第二章 会计数据和财务指标摘要",
                "18 3.1 总体经营情况分析",
                "72 第四章 环境、社会与治理(ESG)",
                "80 第五章 公司治理",
                "112 附表：简式权益变动报告书",
            ]
        )
        keys = toc_declared_root_keys([toc])
        self.assertIn("公司简介", keys)
        # Unprefixed top-level entries count; numbered sub-entries never do.
        self.assertIn("释义", keys)
        self.assertIn("附表简式权益变动报告书", keys)
        self.assertNotIn("总体经营情况分析", keys)
        self.assertFalse(any("3.1" in key for key in keys))
        # Short lists never define the outline.
        self.assertEqual(toc_declared_root_keys(["10 第一章 公司简介"]), frozenset())

    def test_feature_box_without_enumerators_contributes_nothing(self) -> None:
        # A page-numbered feature box mimics the TOC line grammar but carries
        # no statutory 第X章/节 entries, so it must not declare sections.
        box = "\n".join(
            [
                "21 热点问题一 高质量发展业绩亮点",
                "22 热点问题二 持续提升服务实体经济质效",
                "23 热点问题三 全面风险管理与资产质量",
                "24 热点问题四 基础工程夯实生态化基础",
                "25 热点问题五 数字工行建设持续深化",
            ]
        )
        self.assertEqual(toc_declared_root_keys([box]), frozenset())

    def test_dash_wrapped_page_numbers_parse(self) -> None:
        text = "\n".join(
            [
                "第一节 重要提示、目录和释义 ..... -3 -",
                "第四节 公司治理....-68-",
                "第十节 财务报告....-148-",
            ]
        )
        titles, unparsed = parse_toc_titles(text)
        self.assertEqual(unparsed, 0)
        self.assertEqual(titles[-1], "第十节 财务报告")

    def test_matching_strips_enumerator_on_both_sides(self) -> None:
        segments = {
            normalize_heading("管理层讨论与分析"),
            normalize_heading("第一章 公司简介"),
        }
        matched, missing = match_titles_to_tree(
            ["第三章 管理层讨论与分析", "公司简介", "第四节 董事会报告"],
            segments,
        )
        self.assertIn("第三章 管理层讨论与分析", matched)
        self.assertIn("公司简介", matched)
        self.assertEqual(missing, ["第四节 董事会报告"])


if __name__ == "__main__":
    unittest.main()
