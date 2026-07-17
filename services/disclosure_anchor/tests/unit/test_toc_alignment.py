"""Unit tests for TOC parsing/matching (shared module + audit matcher)."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

from disclosure_anchor.adapters.unit_builder.toc_outline import (
    parse_toc_titles,
    strip_section_enumerator,
    toc_declared_root_keys,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from audit_toc_alignment import match_titles_to_tree, normalize_heading  # noqa: E402


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
