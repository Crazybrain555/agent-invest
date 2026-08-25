from __future__ import annotations

import unittest

from scripts.review_semantic_route_eval import review


class ReviewSemanticRouteEvalTests(unittest.TestCase):
    def test_reports_direct_section_partitions_cardinality_and_concentration(
        self,
    ) -> None:
        rows = []
        for unit_index, direct, sections in (
            (0, ["a", "b"], []),
            (1, [], ["chapter"]),
            (2, ["a"], ["chapter", "leaf"]),
            (3, [], []),
        ):
            rows.append(
                {
                    "decision_source": "deterministic" if direct else "fallback",
                    "filing_type": "annual_report",
                    "heading_path": [],
                    "provider_document_id": "pid",
                    "section_keys": sections,
                    "semantic_keys": direct,
                    "title": f"标题{unit_index}",
                    "unit_index": unit_index,
                }
            )

        result = review(
            evaluation={
                "contract_version": "semantic_route_model_eval.v1",
                "rows": rows,
            },
            gold={
                "contract_version": "semantic_route_gold.v1",
                "cases": [
                    {
                        "provider_document_id": "pid",
                        "unit_index": 0,
                        "title": "标题0",
                        "expected_keys": ["a", "b"],
                    }
                ],
            },
        )

        coverage = result["coverage"]
        self.assertEqual(
            coverage["partition"],
            {"direct_only": 1, "section_only": 1, "both": 1, "neither": 1},
        )
        self.assertEqual(coverage["direct"]["assignments"], 3)
        self.assertEqual(coverage["direct"]["multi_key_rows"], 1)
        self.assertEqual(coverage["direct"]["distinct_keys"], 2)
        self.assertEqual(coverage["section"]["assignments"], 3)
        self.assertEqual(coverage["section"]["distinct_arrays"], 2)
        self.assertEqual(coverage["section"]["top_key_row_share"], 0.5)
        self.assertEqual(
            coverage["section"]["top_keys"][0],
            {"key": "chapter", "rows": 2},
        )

    def test_heading_path_only_gold_preserves_order_and_repeated_labels(self) -> None:
        result = review(
            evaluation={
                "contract_version": "semantic_route_model_eval.v1",
                "rows": [
                    {
                        "decision_source": "fallback",
                        "filing_type": "annual_report",
                        "heading_path": ["附注", "附注"],
                        "provider_document_id": "pid",
                        "section_keys": [],
                        "semantic_keys": [],
                        "title": "附注",
                        "unit_index": 0,
                    }
                ],
            },
            gold={
                "contract_version": "semantic_route_gold.v1",
                "cases": [
                    {
                        "provider_document_id": "pid",
                        "unit_index": 0,
                        "title": "附注",
                        "expected_heading_path": ["附注", "附注"],
                        "rationale": "Repeated source headings remain ordered evidence.",
                    }
                ],
            },
        )

        self.assertEqual(result["passed"], 1)
        self.assertEqual(result["failed"], 0)

    def test_duplicate_gold_identity_is_rejected(self) -> None:
        evaluation = {
            "contract_version": "semantic_route_model_eval.v1",
            "rows": [
                {
                    "decision_source": "fallback",
                    "filing_type": "annual_report",
                    "heading_path": [],
                    "provider_document_id": "pid",
                    "section_keys": [],
                    "semantic_keys": [],
                    "title": "标题",
                    "unit_index": 0,
                }
            ],
        }
        case = {
            "provider_document_id": "pid",
            "unit_index": 0,
            "title": "标题",
            "expected_keys": [],
        }

        with self.assertRaisesRegex(ValueError, "semantic gold repeats case"):
            review(
                evaluation=evaluation,
                gold={
                    "contract_version": "semantic_route_gold.v1",
                    "cases": [case, dict(case)],
                },
            )

    def test_exact_route_and_section_expectations_preserve_order(self) -> None:
        result = review(
            evaluation={
                "contract_version": "semantic_route_model_eval.v1",
                "rows": [
                    {
                        "decision_source": "deterministic",
                        "filing_type": "annual_report",
                        "heading_path": [],
                        "provider_document_id": "pid",
                        "section_keys": ["section_b", "section_a"],
                        "semantic_keys": ["route_b", "route_a"],
                        "title": "标题",
                        "unit_index": 0,
                    }
                ],
            },
            gold={
                "contract_version": "semantic_route_gold.v1",
                "cases": [
                    {
                        "provider_document_id": "pid",
                        "unit_index": 0,
                        "title": "标题",
                        "expected_keys": ["route_a", "route_b"],
                        "expected_section_keys": ["section_a", "section_b"],
                    }
                ],
            },
        )

        self.assertEqual(result["passed"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            result["findings"][0]["reasons"],
            ["exact_route_order_differs", "exact_section_order_differs"],
        )


if __name__ == "__main__":
    unittest.main()
