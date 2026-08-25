#!/usr/bin/env python3
"""Verify one applicability replay against a hash-bound reviewed gold."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def applicability_projection_sha256(rows: list[dict[str, Any]]) -> str:
    identities: set[tuple[str, int]] = set()
    projection: list[list[object]] = []
    for row in sorted(
        rows,
        key=lambda item: (item.get("provider_document_id"), item.get("unit_index")),
    ):
        provider_document_id = row.get("provider_document_id")
        unit_index = row.get("unit_index")
        query_projection_hash = row.get("query_projection_hash")
        applicability = row.get("applicability")
        if (
            not isinstance(provider_document_id, str)
            or not isinstance(unit_index, int)
            or not isinstance(query_projection_hash, str)
            or applicability not in {None, "applicable", "not_applicable"}
        ):
            raise ValueError("applicability evaluation row is invalid")
        identity = (provider_document_id, unit_index)
        if identity in identities:
            raise ValueError(f"applicability evaluation repeats row {identity}")
        identities.add(identity)
        content_hash = row.get("content_hash")
        if content_hash is not None and not isinstance(content_hash, str):
            raise ValueError("applicability evaluation content_hash is invalid")
        projection.append(
            [
                provider_document_id,
                unit_index,
                content_hash,
                query_projection_hash,
                applicability,
            ]
        )
    packed = json.dumps(
        projection,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(packed).hexdigest()


def review(
    *,
    evaluation: dict[str, Any],
    evaluation_sha256: str,
    gold: dict[str, Any],
    scope: str,
) -> dict[str, Any]:
    if gold.get("contract_version") != "provider_unit_applicability_gold.v2":
        raise ValueError("applicability gold contract is unsupported")
    evaluations = gold.get("evaluations")
    if not isinstance(evaluations, dict) or not isinstance(evaluations.get(scope), dict):
        raise ValueError(f"applicability gold scope is missing: {scope}")
    expected = evaluations[scope]
    coverage_policy = expected.get("coverage_policy")
    if coverage_policy not in {
        "all_non_null_plus_reviewed_null",
        "reviewed_sentinels_with_projection_lock",
    }:
        raise ValueError("applicability gold coverage policy is unsupported")
    rows = evaluation.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("applicability evaluation rows are missing")
    row_by_identity = {
        (row.get("provider_document_id"), row.get("unit_index")): row for row in rows
    }
    if len(row_by_identity) != len(rows):
        raise ValueError("applicability evaluation repeats an identity")

    findings: list[dict[str, object]] = []
    if evaluation_sha256 != expected.get("evaluation_sha256"):
        findings.append({"reason": "evaluation_sha256_differs"})
    if len(rows) != expected.get("row_count"):
        findings.append({"reason": "row_count_differs", "actual": len(rows)})
    projection_sha256 = applicability_projection_sha256(rows)
    if projection_sha256 != expected.get("projection_sha256"):
        findings.append(
            {"reason": "projection_sha256_differs", "actual": projection_sha256}
        )
    counts = Counter(
        "null" if row.get("applicability") is None else row["applicability"]
        for row in rows
    )
    actual_counts = {
        key: counts.get(key, 0)
        for key in ("null", "applicable", "not_applicable")
    }
    if actual_counts != expected.get("applicability_counts"):
        findings.append(
            {"reason": "applicability_counts_differ", "actual": actual_counts}
        )

    if coverage_policy == "all_non_null_plus_reviewed_null":
        expected_non_null = expected.get("expected_non_null")
        reviewed_null = expected.get("reviewed_null")
        if not isinstance(expected_non_null, list) or not isinstance(
            reviewed_null, list
        ):
            raise ValueError("complete applicability gold cases are missing")
        cases = [*expected_non_null, *reviewed_null]
    else:
        cases = expected.get("reviewed_cases")
        if not isinstance(cases, list):
            raise ValueError("reviewed applicability gold cases are missing")
    seen: set[tuple[str, int]] = set()
    passed = 0
    for case in cases:
        if not isinstance(case, dict) or "expected_applicability" not in case:
            raise ValueError("applicability gold case is invalid")
        provider_document_id = case.get("provider_document_id")
        unit_index = case.get("unit_index")
        if not isinstance(provider_document_id, str) or not isinstance(
            unit_index, int
        ):
            raise ValueError("applicability gold identity is invalid")
        identity = (provider_document_id, unit_index)
        if identity in seen:
            raise ValueError(f"applicability gold repeats case {identity}")
        seen.add(identity)
        row = row_by_identity.get(identity)
        reasons: list[str] = []
        if row is None:
            reasons.append("row_missing")
        else:
            for field in (
                "title",
                "heading_path",
                "content_hash",
                "query_projection_hash",
            ):
                if field in case and row.get(field) != case[field]:
                    reasons.append(f"{field}_differs")
            if row.get("applicability") != case["expected_applicability"]:
                reasons.append("applicability_differs")
        if reasons:
            findings.append(
                {
                    "provider_document_id": identity[0],
                    "unit_index": identity[1],
                    "reasons": reasons,
                }
            )
        else:
            passed += 1

    if coverage_policy == "all_non_null_plus_reviewed_null":
        expected_non_null_identities = {
            (case.get("provider_document_id"), case.get("unit_index"))
            for case in expected_non_null
        }
        actual_non_null_identities = {
            (row.get("provider_document_id"), row.get("unit_index"))
            for row in rows
            if row.get("applicability") is not None
        }
        missing_non_null = sorted(
            expected_non_null_identities - actual_non_null_identities
        )
        unexpected_non_null = sorted(
            actual_non_null_identities - expected_non_null_identities
        )
        if missing_non_null:
            findings.append(
                {
                    "reason": "expected_non_null_missing",
                    "identities": [list(identity) for identity in missing_non_null],
                }
            )
        if unexpected_non_null:
            findings.append(
                {
                    "reason": "unexpected_non_null",
                    "identities": [list(identity) for identity in unexpected_non_null],
                }
            )
    return {
        "contract_version": "provider_unit_applicability_gold_review.v1",
        "scope": scope,
        "coverage_policy": coverage_policy,
        "evaluation_sha256": evaluation_sha256,
        "row_count": len(rows),
        "projection_sha256": projection_sha256,
        "case_count": len(cases),
        "passed": passed,
        "failed": len(findings),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evaluation", type=Path)
    parser.add_argument(
        "--gold",
        type=Path,
        default=Path(
            "docs/implementation/checks/provider-unit-applicability-gold.v2.json"
        ),
    )
    parser.add_argument("--scope", choices=("source", "heldout"), required=True)
    args = parser.parse_args()
    raw = args.evaluation.read_bytes()
    gold_bytes = args.gold.read_bytes()
    report = review(
        evaluation=json.loads(raw),
        evaluation_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        gold=json.loads(gold_bytes),
        scope=args.scope,
    )
    report["gold_sha256"] = "sha256:" + hashlib.sha256(gold_bytes).hexdigest()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
