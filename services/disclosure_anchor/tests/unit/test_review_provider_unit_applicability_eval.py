from __future__ import annotations

import unittest

from scripts.review_provider_unit_applicability_eval import (
    applicability_projection_sha256,
    review,
)


def _row(*, applicability: str | None = None) -> dict[str, object]:
    return {
        "provider_document_id": "doc",
        "unit_index": 1,
        "title": "标题",
        "heading_path": ["第一节", "标题"],
        "content_hash": "sha256:" + "a" * 64,
        "query_projection_hash": "sha256:" + "b" * 64,
        "applicability": applicability,
    }


def _gold(row: dict[str, object]) -> dict[str, object]:
    return {
        "contract_version": "provider_unit_applicability_gold.v2",
        "evaluations": {
            "source": {
                "coverage_policy": "all_non_null_plus_reviewed_null",
                "evaluation_sha256": "sha256:" + "c" * 64,
                "row_count": 1,
                "projection_sha256": applicability_projection_sha256([row]),
                "applicability_counts": {
                    "null": int(row["applicability"] is None),
                    "applicable": int(row["applicability"] == "applicable"),
                    "not_applicable": int(
                        row["applicability"] == "not_applicable"
                    ),
                },
                "expected_non_null": (
                    [
                        {
                            **row,
                            "expected_applicability": row["applicability"],
                            "rationale": "fixture",
                        }
                    ]
                    if row["applicability"] is not None
                    else []
                ),
                "reviewed_null": (
                    [
                        {
                            **row,
                            "expected_applicability": None,
                            "rationale": "fixture",
                        }
                    ]
                    if row["applicability"] is None
                    else []
                ),
            }
        },
    }


class ProviderUnitApplicabilityReviewTests(unittest.TestCase):
    def test_accepts_hash_bound_projection_and_case(self) -> None:
        row = _row(applicability="applicable")

        report = review(
            evaluation={"rows": [row]},
            evaluation_sha256="sha256:" + "c" * 64,
            gold=_gold(row),
            scope="source",
        )

        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["passed"], 1)

    def test_rejects_projection_and_case_drift(self) -> None:
        row = _row(applicability="applicable")
        changed = {**row, "applicability": None}

        report = review(
            evaluation={"rows": [changed]},
            evaluation_sha256="sha256:" + "c" * 64,
            gold=_gold(row),
            scope="source",
        )

        reasons = {finding["reason"] for finding in report["findings"] if "reason" in finding}
        self.assertIn("projection_sha256_differs", reasons)
        self.assertIn("applicability_counts_differ", reasons)
        self.assertTrue(
            any(
                "applicability_differs" in finding.get("reasons", [])
                for finding in report["findings"]
            )
        )
        self.assertTrue(
            any(
                finding.get("reason") == "expected_non_null_missing"
                for finding in report["findings"]
            )
        )

    def test_rejects_unexpected_non_null_identity(self) -> None:
        row = _row(applicability=None)
        changed = {**row, "applicability": "not_applicable"}

        report = review(
            evaluation={"rows": [changed]},
            evaluation_sha256="sha256:" + "c" * 64,
            gold=_gold(row),
            scope="source",
        )

        self.assertTrue(
            any(
                finding.get("reason") == "unexpected_non_null"
                for finding in report["findings"]
            )
        )

    def test_rejects_heading_path_drift_even_when_projection_is_unchanged(self) -> None:
        row = _row(applicability="applicable")
        changed = {**row, "heading_path": ["错误路径", "标题"]}

        report = review(
            evaluation={"rows": [changed]},
            evaluation_sha256="sha256:" + "c" * 64,
            gold=_gold(row),
            scope="source",
        )

        self.assertTrue(
            any(
                "heading_path_differs" in finding.get("reasons", [])
                for finding in report["findings"]
            )
        )


if __name__ == "__main__":
    unittest.main()
