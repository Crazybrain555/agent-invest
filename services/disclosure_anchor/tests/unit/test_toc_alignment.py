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

    def test_strip_section_enumerator(self) -> None:
        self.assertEqual(strip_section_enumerator("第三章 管理层讨论与分析"), "管理层讨论与分析")
        self.assertEqual(strip_section_enumerator("第十节 财务报告"), "财务报告")
        self.assertEqual(strip_section_enumerator("重要提示"), "重要提示")

    def test_toc_declared_root_keys_prefixed_entries_only(self) -> None:
        toc = "\n".join(
            [
                "2 释义",
                "10 第一章 公司简介",
                "14 第二章 会计数据和财务指标摘要",
                "19 第三章 管理层讨论与分析",
                "72 第四章 环境、社会与治理(ESG)",
                "80 第五章 公司治理",
            ]
        )
        keys = toc_declared_root_keys([toc])
        self.assertIn("管理层讨论与分析", keys)
        self.assertIn("公司简介", keys)
        # Unprefixed TOC lines do not define roots.
        self.assertNotIn("释义", keys)
        # Short lists never define the outline.
        self.assertEqual(toc_declared_root_keys(["10 第一章 公司简介"]), frozenset())

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
