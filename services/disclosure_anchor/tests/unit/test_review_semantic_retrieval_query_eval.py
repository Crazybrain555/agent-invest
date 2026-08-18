from __future__ import annotations

import unittest

from scripts.review_semantic_retrieval_query_eval import review


_HASH = "sha256:" + "a" * 64
_CONTENT_HASH = "sha256:" + "b" * 64


class ReviewSemanticRetrievalQueryEvalTests(unittest.TestCase):
    def test_graded_review_binds_hashes_and_reports_ablations(self) -> None:
        evaluation = _evaluation()
        gold = {
            "contract_version": "semantic_retrieval_query_gold.v4",
            "_about": "graded test",
            "evaluation_id": "eval",
            "taxonomy_version": "taxonomy",
            "router_version": "router",
            "judged_units": [
                ["pid", 0, _HASH, _CONTENT_HASH],
                ["pid", 1, _HASH, _CONTENT_HASH],
                ["pid", 2, _HASH, _CONTENT_HASH],
                ["pid", 3, _HASH, _CONTENT_HASH],
            ],
            "thresholds": {
                "success_at_5": 1.0,
                "grade3_recall_at_20": 1.0,
                "grade2_recall_at_10": 1.0,
                "grade2_recall_at_20": 1.0,
                "ndcg_at_10": 0.8,
                "narrow_returned_precision_at_5": 0.5,
                "broad_returned_precision_at_10": 0.5,
                "max_grade0_top5": 1,
                "max_mechanical_top10": 0,
            },
            "cases": [
                _case("narrow", "收入", qrels=[["pid", 0, 3]]),
                _case(
                    "broad",
                    "风险",
                    qrels=[["pid", 1, 0], ["pid", 2, 3], ["pid", 3, 2]],
                    semantic_keys_any=["risk_management"],
                    section_keys_any=["risk_management"],
                    neighbor_radius=1,
                ),
            ],
        }

        result = review(
            evaluation=evaluation,
            gold=gold,
            search_rows=_search_rows(),
        )

        self.assertTrue(result["passed"])
        self.assertTrue(result["judgment_hash_bound"])
        self.assertTrue(result["judgment_content_hash_bound"])
        self.assertTrue(result["judgment_pool_complete"])
        self.assertTrue(result["query_hash_bound"])
        self.assertEqual(result["metrics"]["grade3_recall_at_20"], 1.0)
        self.assertEqual(result["metrics"]["narrow_returned_precision_at_5"], 1.0)
        broad = result["results"][1]
        self.assertEqual(broad["top"][0]["unit_index"], 2)
        self.assertNotIn(["pid", 3], broad["ablations"]["without_neighbors"])

    def test_graded_review_rejects_stale_live_projection(self) -> None:
        search_rows = _search_rows()
        search_rows[("pid", 1)]["query_projection_hash"] = "sha256:" + "b" * 64

        with self.assertRaisesRegex(ValueError, "query hashes do not align"):
            review(
                evaluation=_evaluation(),
                gold={
                    "contract_version": "semantic_retrieval_query_gold.v4",
                    "_about": "graded test",
                    "evaluation_id": "eval",
                    "taxonomy_version": "taxonomy",
                    "router_version": "router",
                    "judged_units": [
                        ["pid", 0, _HASH, _CONTENT_HASH],
                        ["pid", 2, _HASH, _CONTENT_HASH],
                    ],
                    "thresholds": {
                        "success_at_5": 0.0,
                        "grade3_recall_at_20": 0.0,
                        "grade2_recall_at_10": 0.0,
                        "grade2_recall_at_20": 0.0,
                        "ndcg_at_10": 0.0,
                        "narrow_returned_precision_at_5": 0.0,
                        "broad_returned_precision_at_10": 0.0,
                        "max_grade0_top5": 5,
                        "max_mechanical_top10": 10,
                    },
                    "cases": [
                        _case("narrow", "收入", qrels=[["pid", 0, 3]]),
                        _case("broad", "风险", qrels=[["pid", 2, 3]]),
                    ],
                },
                search_rows=search_rows,
            )

    def test_graded_review_rejects_stale_judgment_content(self) -> None:
        evaluation = _evaluation()
        search_rows = _search_rows()
        changed_content_hash = "sha256:" + "c" * 64
        evaluation["rows"][0]["content_hash"] = changed_content_hash
        search_rows[("pid", 0)]["content_hash"] = changed_content_hash

        with self.assertRaisesRegex(ValueError, "judged Unit hashes do not match"):
            review(
                evaluation=evaluation,
                gold={
                    "contract_version": "semantic_retrieval_query_gold.v4",
                    "_about": "graded test",
                    "evaluation_id": "eval",
                    "taxonomy_version": "taxonomy",
                    "router_version": "router",
                    "judged_units": [
                        ["pid", 0, _HASH, _CONTENT_HASH],
                        ["pid", 2, _HASH, _CONTENT_HASH],
                    ],
                    "thresholds": {
                        "success_at_5": 0.0,
                        "grade3_recall_at_20": 0.0,
                        "grade2_recall_at_10": 0.0,
                        "grade2_recall_at_20": 0.0,
                        "ndcg_at_10": 0.0,
                        "narrow_returned_precision_at_5": 0.0,
                        "broad_returned_precision_at_10": 0.0,
                        "max_grade0_top5": 5,
                        "max_mechanical_top10": 10,
                    },
                    "cases": [
                        _case("narrow", "收入", qrels=[["pid", 0, 3]]),
                        _case("broad", "风险", qrels=[["pid", 2, 3]]),
                    ],
                },
                search_rows=search_rows,
            )

    def test_graded_review_rejects_unjudged_evaluated_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "contains unjudged Units"):
            review(
                evaluation=_evaluation(),
                gold={
                    "contract_version": "semantic_retrieval_query_gold.v4",
                    "_about": "graded test",
                    "evaluation_id": "eval",
                    "taxonomy_version": "taxonomy",
                    "router_version": "router",
                    "judged_units": [
                        ["pid", 0, _HASH, _CONTENT_HASH],
                        ["pid", 2, _HASH, _CONTENT_HASH],
                        ["pid", 3, _HASH, _CONTENT_HASH],
                    ],
                    "thresholds": _permissive_thresholds(),
                    "cases": [
                        _case("narrow", "收入", qrels=[["pid", 0, 3]]),
                        _case(
                            "broad",
                            "风险",
                            qrels=[["pid", 2, 3], ["pid", 3, 2]],
                            section_keys_any=["risk_management"],
                            neighbor_radius=1,
                        ),
                    ],
                },
                search_rows=_search_rows(),
            )

    def test_source_wide_positive_outside_every_ranking_lowers_recall(self) -> None:
        evaluation = _evaluation()
        evaluation["row_count"] = 5
        rows = evaluation["rows"]
        assert isinstance(rows, list)
        rows.append(
            {
                "provider_document_id": "pid",
                "unit_index": 4,
                "title": "其他正文",
                "semantic_keys": [],
                "section_keys": [],
                "query_projection_hash": _HASH,
                "content_hash": _CONTENT_HASH,
            }
        )
        search_rows = _search_rows()
        search_rows[("pid", 4)] = {
            "contract_version": "document_unit.v2",
            "content_hash": _CONTENT_HASH,
            "query_projection_hash": _HASH,
            "title_text": "其他正文",
            "heading_path_text": "",
            "search_tokens": "其他 正文",
            "atom_text": "",
        }
        gold = {
            "contract_version": "semantic_retrieval_query_gold.v4",
            "_about": "source-wide recall denominator test",
            "evaluation_id": "eval",
            "taxonomy_version": "taxonomy",
            "router_version": "router",
            "judged_units": [
                ["pid", 0, _HASH, _CONTENT_HASH],
                ["pid", 2, _HASH, _CONTENT_HASH],
                ["pid", 4, _HASH, _CONTENT_HASH],
            ],
            "thresholds": _permissive_thresholds(),
            "cases": [
                _case(
                    "narrow",
                    "收入",
                    qrels=[["pid", 0, 3], ["pid", 4, 2]],
                ),
                _case(
                    "broad",
                    "风险管理",
                    qrels=[["pid", 2, 3]],
                    section_keys_any=["risk_management"],
                ),
            ],
        }

        result = review(
            evaluation=evaluation,
            gold=gold,
            search_rows=search_rows,
        )

        self.assertTrue(result["passed"])
        self.assertTrue(result["judgment_pool_complete"])
        self.assertEqual(result["results"][0]["metrics"]["grade2_recall_at_20"], 0.5)
        self.assertEqual(result["results"][0]["top"][0]["unit_index"], 0)

    def test_graded_review_rejects_deprecated_unit_surface(self) -> None:
        search_rows = _search_rows()
        search_rows[("pid", 0)]["contract_version"] = "document_unit.v1"

        with self.assertRaisesRegex(ValueError, "not from document_unit.v2"):
            review(
                evaluation=_evaluation(),
                gold={
                    "contract_version": "semantic_retrieval_query_gold.v4",
                    "_about": "current public Unit surface test",
                    "evaluation_id": "eval",
                    "taxonomy_version": "taxonomy",
                    "router_version": "router",
                    "judged_units": [
                        ["pid", 0, _HASH, _CONTENT_HASH],
                        ["pid", 2, _HASH, _CONTENT_HASH],
                    ],
                    "thresholds": _permissive_thresholds(),
                    "cases": [
                        _case("narrow", "收入", qrels=[["pid", 0, 3]]),
                        _case("broad", "风险", qrels=[["pid", 2, 3]]),
                    ],
                },
                search_rows=search_rows,
            )

    def test_and_lexical_operator_keeps_unit_level_conjunctive_match(self) -> None:
        search_rows = _search_rows()
        search_rows[("pid", 3)]["search_tokens"] = "风险 应对 措施"
        gold = {
            "contract_version": "semantic_retrieval_query_gold.v4",
            "_about": "explicit lexical operator test",
            "evaluation_id": "eval",
            "taxonomy_version": "taxonomy",
            "router_version": "router",
            "judged_units": [
                ["pid", 0, _HASH, _CONTENT_HASH],
                ["pid", 3, _HASH, _CONTENT_HASH],
            ],
            "thresholds": _permissive_thresholds(),
            "cases": [
                _case("narrow", "收入", qrels=[["pid", 0, 3]]),
                _case(
                    "broad",
                    "风险 措施",
                    qrels=[["pid", 3, 3]],
                    lexical_operator="and",
                ),
            ],
        }

        result = review(
            evaluation=_evaluation(),
            gold=gold,
            search_rows=search_rows,
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["results"][1]["top"][0]["unit_index"], 3)
        self.assertEqual(result["results"][1]["top"][0]["reasons"], ["lexical"])

    def test_explicit_lexical_plan_is_distinct_from_display_query(self) -> None:
        search_rows = _search_rows()
        search_rows[("pid", 3)]["search_tokens"] = "销售量 价格变动"
        gold = {
            "contract_version": "semantic_retrieval_query_gold.v4",
            "_about": "explicit lexical plan test",
            "evaluation_id": "eval",
            "taxonomy_version": "taxonomy",
            "router_version": "router",
            "judged_units": [
                ["pid", 0, _HASH, _CONTENT_HASH],
                ["pid", 3, _HASH, _CONTENT_HASH],
            ],
            "thresholds": _permissive_thresholds(),
            "cases": [
                _case("narrow", "收入", qrels=[["pid", 0, 3]]),
                _case(
                    "broad",
                    "销量价格",
                    qrels=[["pid", 3, 3]],
                    lexical_query="销售量 价格变动",
                    lexical_operator="and",
                ),
            ],
        }

        result = review(
            evaluation=_evaluation(),
            gold=gold,
            search_rows=search_rows,
        )

        broad = result["results"][1]
        self.assertEqual(broad["query"], "销量价格")
        self.assertEqual(broad["lexical_query"], "销售量 价格变动")
        self.assertEqual(broad["top"][0]["unit_index"], 3)

    def test_phrase_union_and_filing_preference_rank_related_period_first(self) -> None:
        search_rows = _search_rows()
        search_rows[("pid", 2)]["atom_text"] = "其他综合收益"
        search_rows[("pid", 3)]["atom_text"] = "未分配利润"
        evaluation = _evaluation()
        evaluation["rows"][2]["effective_filing_type"] = "annual_report"
        evaluation["rows"][3]["effective_filing_type"] = "quarterly_report"
        gold = {
            "contract_version": "semantic_retrieval_query_gold.v4",
            "_about": "phrase union and filing preference test",
            "evaluation_id": "eval",
            "taxonomy_version": "taxonomy",
            "router_version": "router",
            "judged_units": [
                ["pid", 0, _HASH, _CONTENT_HASH],
                ["pid", 2, _HASH, _CONTENT_HASH],
                ["pid", 3, _HASH, _CONTENT_HASH],
            ],
            "thresholds": _permissive_thresholds(),
            "cases": [
                _case("narrow", "收入", qrels=[["pid", 0, 3]]),
                _case(
                    "broad",
                    "权益组成",
                    qrels=[["pid", 2, 2], ["pid", 3, 3]],
                    lexical_queries_any=["未分配利润", "其他综合收益"],
                    filing_types_preferred=["quarterly_report"],
                ),
            ],
        }

        result = review(evaluation=evaluation, gold=gold, search_rows=search_rows)

        broad = result["results"][1]
        self.assertEqual(broad["top"][0]["unit_index"], 3)
        self.assertEqual(
            broad["top"][0]["reasons"],
            ["lexical", "filing_type_preference"],
        )
        self.assertEqual(broad["lexical_queries_any"], ["未分配利润", "其他综合收益"])
        self.assertIsNone(broad["lexical_query"])

    def test_phrase_union_rejects_ambiguous_single_query_controls(self) -> None:
        gold = {
            "contract_version": "semantic_retrieval_query_gold.v4",
            "_about": "phrase union validation test",
            "evaluation_id": "eval",
            "taxonomy_version": "taxonomy",
            "router_version": "router",
            "judged_units": [["pid", 0, _HASH, _CONTENT_HASH]],
            "thresholds": _permissive_thresholds(),
            "cases": [
                _case(
                    "narrow",
                    "收入",
                    qrels=[["pid", 0, 3]],
                    lexical_query="收入",
                    lexical_queries_any=["收入", "成本"],
                )
            ],
        }

        with self.assertRaisesRegex(ValueError, "lexical_queries_any conflicts"):
            review(evaluation=_evaluation(), gold=gold, search_rows=_search_rows())


def _case(
    intent: str,
    query: str,
    *,
    qrels: list[list[object]],
    lexical_operator: str | None = None,
    lexical_queries_any: list[str] | None = None,
    lexical_query: str | None = None,
    filing_types_preferred: list[str] | None = None,
    semantic_keys_any: list[str] | None = None,
    section_keys_any: list[str] | None = None,
    neighbor_radius: int = 0,
    mechanical_forbidden: list[list[object]] | None = None,
) -> dict[str, object]:
    result = {
        "id": intent,
        "intent": intent,
        "query": query,
        "semantic_keys_all": ["revenue_and_cost"] if intent == "narrow" else [],
        "section_keys_any": section_keys_any or [],
        "neighbor_radius": neighbor_radius,
        "qrels": qrels,
        "mechanical_forbidden": mechanical_forbidden or [],
    }
    if semantic_keys_any is not None:
        result["semantic_keys_any"] = semantic_keys_any
    if lexical_operator is not None:
        result["lexical_operator"] = lexical_operator
    if lexical_queries_any is not None:
        result["lexical_queries_any"] = lexical_queries_any
    if lexical_query is not None:
        result["lexical_query"] = lexical_query
    if filing_types_preferred is not None:
        result["filing_types_preferred"] = filing_types_preferred
    return result


def _permissive_thresholds() -> dict[str, float | int]:
    return {
        "success_at_5": 0.0,
        "grade3_recall_at_20": 0.0,
        "grade2_recall_at_10": 0.0,
        "grade2_recall_at_20": 0.0,
        "ndcg_at_10": 0.0,
        "narrow_returned_precision_at_5": 0.0,
        "broad_returned_precision_at_10": 0.0,
        "max_grade0_top5": 5,
        "max_mechanical_top10": 10,
    }


def _evaluation() -> dict[str, object]:
    return {
        "contract_version": "semantic_route_model_eval.v1",
        "evaluation_id": "eval",
        "taxonomy_version": "taxonomy",
        "router_version": "router",
        "row_count": 4,
        "rows": [
            {
                "provider_document_id": "pid",
                "unit_index": 0,
                "title": "营业收入",
                "semantic_keys": ["revenue_and_cost"],
                "section_keys": [],
                "query_projection_hash": _HASH,
                "content_hash": _CONTENT_HASH,
            },
            {
                "provider_document_id": "pid",
                "unit_index": 1,
                "title": "目录",
                "semantic_keys": ["table_of_contents"],
                "section_keys": [],
                "query_projection_hash": _HASH,
                "content_hash": _CONTENT_HASH,
            },
            {
                "provider_document_id": "pid",
                "unit_index": 2,
                "title": "风险管理",
                "semantic_keys": ["risk_management"],
                "section_keys": ["risk_management"],
                "query_projection_hash": _HASH,
                "content_hash": _CONTENT_HASH,
            },
            {
                "provider_document_id": "pid",
                "unit_index": 3,
                "title": "风险应对",
                "semantic_keys": [],
                "section_keys": [],
                "query_projection_hash": _HASH,
                "content_hash": _CONTENT_HASH,
            },
        ],
    }


def _search_rows() -> dict[tuple[str, int], dict[str, object]]:
    return {
        ("pid", 0): {
            "contract_version": "document_unit.v2",
            "content_hash": _CONTENT_HASH,
            "query_projection_hash": _HASH,
            "title_text": "营业收入",
            "heading_path_text": "经营情况",
            "search_tokens": "营业 收入",
            "atom_text": "营业收入增长",
        },
        ("pid", 1): {
            "contract_version": "document_unit.v2",
            "content_hash": _CONTENT_HASH,
            "query_projection_hash": _HASH,
            "title_text": "目录",
            "heading_path_text": "",
            "search_tokens": "目录",
            "atom_text": "",
        },
        ("pid", 2): {
            "contract_version": "document_unit.v2",
            "content_hash": _CONTENT_HASH,
            "query_projection_hash": _HASH,
            "title_text": "风险管理",
            "heading_path_text": "风险管理",
            "search_tokens": "风险 管理",
            "atom_text": "",
        },
        ("pid", 3): {
            "contract_version": "document_unit.v2",
            "content_hash": _CONTENT_HASH,
            "query_projection_hash": _HASH,
            "title_text": "应对措施",
            "heading_path_text": "应对措施",
            "search_tokens": "应对 措施",
            "atom_text": "",
        },
    }


if __name__ == "__main__":
    unittest.main()
