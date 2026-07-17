"""06R search projection: tokenizer + deterministic row computation (no DB).

Covers the pure, DB-free surface of milestone 06R: the pinned jieba tokenizer
(determinism + dictionary-drift rejection) and the projection row computation
(body linearization per payload_kind incl. raw_html exclusion, and the
header_row_candidate diagnostic).
"""

from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest import mock

from disclosure_anchor.adapters.retrieval import tokenizer
from disclosure_anchor.application.use_cases.build_search_projection import (
    compute_search_projection_row,
    header_row_candidate,
    linearize_body,
)

_BUILT_AT = datetime(2026, 7, 17, tzinfo=timezone.utc)


class TokenizerTests(unittest.TestCase):
    def test_tokenize_is_deterministic(self) -> None:
        first = tokenizer.tokenize("应收账款账龄分析")
        second = tokenizer.tokenize("应收账款账龄分析")
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_tokenize_normalizes_width_and_case_and_empty(self) -> None:
        # NFKC folds full-width forms; casefold lowercases; blank -> "".
        self.assertEqual(tokenizer.tokenize("１２３"), tokenizer.tokenize("123"))
        self.assertIn("abc", tokenizer.tokenize("ABC def").split())
        self.assertEqual(tokenizer.tokenize("   "), "")

    def test_tokenize_rejects_dictionary_drift(self) -> None:
        # Reset the module cache so the pinned-fingerprint check re-runs, then
        # force a sha mismatch: the tokenizer must fail loudly, not segment.
        tokenizer._tokenizer = None
        try:
            with mock.patch.object(tokenizer, "_DICT_SHA256", "0" * 64):
                with self.assertRaises(tokenizer.RetrievalDictionaryError):
                    tokenizer.tokenize("应收账款")
        finally:
            # Drop the (unbuilt) cache so later tests reload against the real sha.
            tokenizer._tokenizer = None


class QuerySynonymTests(unittest.TestCase):
    def test_shipped_synonym_file_loads_against_pinned_tokenizer(self) -> None:
        expansion = tokenizer._load_synonyms()
        self.assertIn("年报", expansion)
        self.assertIn("年度报告", expansion["年报"])

    def test_equivalence_group_expands_both_directions(self) -> None:
        self.assertEqual(
            tokenizer.build_search_tsquery("分红"),
            "('分红' | '利润分配' | '分派')",
        )
        self.assertIn("'分红'", tokenizer.build_search_tsquery("利润分配"))

    def test_directional_rule_is_one_way(self) -> None:
        expanded = tokenizer.build_search_tsquery("派息")
        self.assertIn("'分红'", expanded)
        self.assertNotIn("'派息'", tokenizer.build_search_tsquery("分红"))

    def test_multi_lexeme_query_terms_and_conjunction(self) -> None:
        # 商誉减值 segments to two lexemes with no aliases: plain AND, which
        # already matches 商誉减值准备 through the shared lexemes.
        self.assertEqual(
            tokenizer.build_search_tsquery("商誉减值"), "'商誉' & '减值'"
        )
        self.assertEqual(tokenizer.build_search_tsquery("  "), "")

    def test_synonym_loading_is_lock_order_independent(self) -> None:
        # Regression: loading synonyms before
        # any tokenize call must not deadlock on the tokenizer lock.
        import threading

        tokenizer._synonyms = None
        tokenizer._tokenizer = None
        loaded: list[dict] = []
        worker = threading.Thread(
            target=lambda: loaded.append(tokenizer._load_synonyms()),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "synonym loading deadlocked")
        self.assertTrue(loaded and loaded[0])

    def test_parse_rejects_multi_lexeme_and_degenerate_rules(self) -> None:
        with self.assertRaises(tokenizer.RetrievalSynonymError):
            tokenizer.parse_synonyms("回购, 股份回购\n")
        with self.assertRaises(tokenizer.RetrievalSynonymError):
            tokenizer.parse_synonyms("年报\n")
        with self.assertRaises(tokenizer.RetrievalSynonymError):
            tokenizer.parse_synonyms("年报, 季报 => 季度\n")
        with self.assertRaises(tokenizer.RetrievalSynonymError):
            tokenizer.parse_synonyms(
                "\n".join(f"年报, 年度报告  # {index}" for index in range(41))
            )


class LinearizeBodyTests(unittest.TestCase):
    def test_text_body_is_payload_text(self) -> None:
        self.assertEqual(linearize_body("text", {"text": "货币资金明细"}), "货币资金明细")

    def test_table_body_excludes_raw_html(self) -> None:
        body = linearize_body(
            "table",
            {
                "caption": ["应收账款账龄"],
                "unit": "元",
                "headers": ["账龄", "金额"],
                "rows": [["1年以内", "100"], ["1-2年", "50"]],
                "notes": ["注1"],
                "raw_html": "<table><td>RAWHTMLSENTINEL</td></table>",
            },
        )
        for token in ["应收账款账龄", "元", "账龄", "金额", "1年以内", "100", "注1"]:
            self.assertIn(token, body)
        self.assertNotIn("RAWHTMLSENTINEL", body)
        self.assertNotIn("<table>", body)

    def test_mixed_parts_linearize_in_order_recursively(self) -> None:
        body = linearize_body(
            "mixed",
            {
                "semantic_type": "section",
                "parts": [
                    {"kind": "text", "text": "文本部分ALPHA"},
                    {
                        "kind": "table",
                        "headers": ["列"],
                        "rows": [["值BETA"]],
                        "raw_html": "<b>RAWSENTINEL</b>",
                    },
                    {"kind": "image", "caption": "图注GAMMA", "context": "上下文DELTA"},
                ],
            },
        )
        for token in ["文本部分ALPHA", "列", "值BETA", "图注GAMMA", "上下文DELTA"]:
            self.assertIn(token, body)
        self.assertNotIn("RAWSENTINEL", body)

    def test_unknown_payload_kind_is_empty(self) -> None:
        self.assertEqual(linearize_body("qa", {"parts": [{"text": "x"}]}), "")


class HeaderRowCandidateTests(unittest.TestCase):
    def test_positive_td_only_numeric_table(self) -> None:
        self.assertTrue(
            header_row_candidate(
                "table",
                {
                    "headers": [],
                    "rows": [
                        ["应收账款", "期末余额", "期初余额"],
                        ["1年以内", "100", "90"],
                        ["1-2年", "50.5", "40"],
                    ],
                },
            )
        )

    def test_negative_kv_form_first_row_has_numeric_value(self) -> None:
        self.assertFalse(
            header_row_candidate(
                "table",
                {
                    "headers": [],
                    "rows": [["证券代码", "600000"], ["注册资本", "1000"]],
                },
            )
        )

    def test_negative_table_with_real_headers(self) -> None:
        self.assertFalse(
            header_row_candidate(
                "table",
                {
                    "headers": ["科目", "金额"],
                    "rows": [["货币资金", "100"], ["应收账款", "50"]],
                },
            )
        )

    def test_negative_single_row_table(self) -> None:
        self.assertFalse(
            header_row_candidate("table", {"headers": [], "rows": [["应收账款", "账龄"]]})
        )

    def test_negative_all_text_table_has_no_numeric_data(self) -> None:
        self.assertFalse(
            header_row_candidate(
                "table",
                {"headers": [], "rows": [["项目", "说明"], ["政策", "描述"]]},
            )
        )

    def test_negative_non_table_kind(self) -> None:
        self.assertFalse(header_row_candidate("text", {"rows": [["a"], ["1"]]}))


class ComputeRowTests(unittest.TestCase):
    def test_row_fields_exclude_generated_tsv_and_exclude_raw_html(self) -> None:
        row = compute_search_projection_row(
            asset_id="ua_x",
            title="现金流量表",
            heading_path=["第八节 财务报告", "现金流量表补充资料"],
            payload_kind="table",
            payload={
                "caption": ["现金流量表补充资料"],
                "unit": "元",
                "headers": ["项目"],
                "rows": [["经营活动", "100"]],
                "notes": [],
                "raw_html": "<x>NOPE</x>",
            },
            semantic_keys=["cash_flow_statement", "financial_report_chapter"],
            built_at=_BUILT_AT,
        )
        self.assertEqual(row["asset_id"], "ua_x")
        self.assertEqual(row["title_text"], "现金流量表")
        self.assertEqual(row["heading_path_text"], "第八节 财务报告 > 现金流量表补充资料")
        self.assertEqual(row["retrieval_rules_version"], tokenizer.RETRIEVAL_RULES_VERSION)
        # Semantic keys are joined untokenized (controlled ASCII tokens).
        self.assertEqual(
            row["key_tokens"], "cash_flow_statement financial_report_chapter"
        )
        self.assertFalse(row["header_row_candidate"])
        self.assertEqual(row["built_at"], _BUILT_AT)
        self.assertNotIn("search_tsv", row)
        self.assertTrue(row["title_tokens"])
        self.assertNotIn("nope", row["body_tokens"])

    def test_empty_unit_yields_legal_empty_strings(self) -> None:
        row = compute_search_projection_row(
            asset_id="ua_empty",
            title=None,
            heading_path=[],
            payload_kind="text",
            payload={"text": ""},
            semantic_keys=None,
            built_at=_BUILT_AT,
        )
        self.assertEqual(row["title_text"], "")
        self.assertEqual(row["heading_path_text"], "")
        self.assertEqual(row["title_tokens"], "")
        self.assertEqual(row["path_tokens"], "")
        self.assertEqual(row["body_tokens"], "")
        self.assertEqual(row["key_tokens"], "")
        self.assertEqual(row["retrieval_rules_version"], tokenizer.RETRIEVAL_RULES_VERSION)


if __name__ == "__main__":
    unittest.main()
