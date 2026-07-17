"""Unit tests for the TOC-alignment audit's pure parsing/matching functions."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from audit_toc_alignment import (  # noqa: E402
    match_titles_to_tree,
    normalize_heading,
    parse_toc_titles,
)


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

    def test_matching_exact_and_prefix_but_not_fragments(self) -> None:
        segments = {
            normalize_heading("第九节 财务报告"),
            normalize_heading("第一章 公司简介"),
        }
        matched, missing = match_titles_to_tree(
            ["第九节 财务报告", "第一章 公司简介（续）", "第四节 董事会报告"],
            segments,
        )
        self.assertIn("第九节 财务报告", matched)
        self.assertIn("第一章 公司简介（续）", matched)
        self.assertEqual(missing, ["第四节 董事会报告"])


if __name__ == "__main__":
    unittest.main()
