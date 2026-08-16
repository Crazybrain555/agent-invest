from __future__ import annotations

import unittest

from scripts.review_semantic_route_eval import review


class ReviewSemanticRouteEvalTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
