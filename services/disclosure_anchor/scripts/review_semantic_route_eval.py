#!/usr/bin/env python3
"""Score one offline semantic-route replay against source-reviewed sentinels."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


_GOLD_KEYS = {
    "provider_document_id",
    "unit_index",
    "title",
    "expected_keys",
    "required_keys",
    "forbidden_keys",
    "expected_section_keys",
    "required_section_keys",
    "forbidden_section_keys",
    "rationale",
}


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} repeats a key")
    return value


def review(*, evaluation: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    if evaluation.get("contract_version") not in {
        "semantic_route_model_eval.v1",
        "heldout_provider_unit_semantic_review.v1",
    }:
        raise ValueError("semantic route evaluation contract is unsupported")
    if gold.get("contract_version") != "semantic_route_gold.v1":
        raise ValueError("semantic route gold contract is unsupported")
    cases = gold.get("cases")
    rows = evaluation.get("rows")
    if not isinstance(cases, list) or not isinstance(rows, list):
        raise ValueError("semantic evaluation or gold rows are missing")

    row_by_identity: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("semantic evaluation row is not an object")
        provider_document_id = row.get("provider_document_id")
        unit_index = row.get("unit_index")
        if not isinstance(provider_document_id, str) or not isinstance(unit_index, int):
            raise ValueError("semantic evaluation row identity is invalid")
        identity = (provider_document_id, unit_index)
        if identity in row_by_identity:
            raise ValueError(f"semantic evaluation repeats row {identity}")
        row_by_identity[identity] = row

    decision_sources: Counter[str] = Counter()
    route_cardinality: Counter[int] = Counter()
    section_cardinality: Counter[int] = Counter()
    coverage_by_type: dict[str, Counter[str]] = defaultdict(Counter)
    for row in row_by_identity.values():
        source = row.get("decision_source")
        filing_type = row.get("effective_filing_type", row.get("filing_type"))
        keys = _string_list(row.get("semantic_keys"), field="semantic_keys")
        section_keys = _string_list(
            row.get("section_keys", []), field="section_keys"
        )
        if not isinstance(source, str) or not isinstance(filing_type, str):
            raise ValueError("semantic evaluation row coverage fields are invalid")
        decision_sources[source] += 1
        route_cardinality[len(keys)] += 1
        section_cardinality[len(section_keys)] += 1
        coverage_by_type[filing_type]["rows"] += 1
        coverage_by_type[filing_type]["routed" if keys else "null"] += 1
        coverage_by_type[filing_type][
            "section_routed" if section_keys else "section_null"
        ] += 1
        coverage_by_type[filing_type][
            "retrievable" if keys or section_keys else "unrouted"
        ] += 1

    findings: list[dict[str, object]] = []
    passed = 0
    for raw_case in cases:
        if not isinstance(raw_case, dict) or not set(raw_case).issubset(_GOLD_KEYS):
            raise ValueError("semantic gold case has unknown fields")
        provider_document_id = raw_case.get("provider_document_id")
        unit_index = raw_case.get("unit_index")
        title = raw_case.get("title")
        if (
            not isinstance(provider_document_id, str)
            or not isinstance(unit_index, int)
            or not isinstance(title, str)
        ):
            raise ValueError("semantic gold identity is invalid")
        expected = (
            _string_list(raw_case["expected_keys"], field="expected_keys")
            if "expected_keys" in raw_case
            else None
        )
        required = _string_list(
            raw_case.get("required_keys", []), field="required_keys"
        )
        forbidden = _string_list(
            raw_case.get("forbidden_keys", []), field="forbidden_keys"
        )
        expected_sections = (
            _string_list(
                raw_case["expected_section_keys"],
                field="expected_section_keys",
            )
            if "expected_section_keys" in raw_case
            else None
        )
        required_sections = _string_list(
            raw_case.get("required_section_keys", []),
            field="required_section_keys",
        )
        forbidden_sections = _string_list(
            raw_case.get("forbidden_section_keys", []),
            field="forbidden_section_keys",
        )
        if expected is None and expected_sections is None and not (
            required or forbidden or required_sections or forbidden_sections
        ):
            raise ValueError("semantic gold case has no acceptance assertion")
        row = row_by_identity.get((provider_document_id, unit_index))
        reasons: list[str] = []
        actual: list[str] = []
        actual_sections: list[str] = []
        if row is None:
            reasons.append("row_missing")
        else:
            if row.get("title") != title:
                reasons.append("title_drift")
            actual = _string_list(row.get("semantic_keys"), field="semantic_keys")
            actual_sections = _string_list(
                row.get("section_keys", []), field="section_keys"
            )
            actual_set = set(actual)
            actual_section_set = set(actual_sections)
            if expected is not None and actual_set != set(expected):
                reasons.append("exact_route_set_differs")
            if not set(required).issubset(actual_set):
                reasons.append("required_route_missing")
            if set(forbidden) & actual_set:
                reasons.append("forbidden_route_selected")
            if (
                expected_sections is not None
                and actual_section_set != set(expected_sections)
            ):
                reasons.append("exact_section_set_differs")
            if not set(required_sections).issubset(actual_section_set):
                reasons.append("required_section_missing")
            if set(forbidden_sections) & actual_section_set:
                reasons.append("forbidden_section_selected")
        if reasons:
            findings.append(
                {
                    "actual_keys": actual,
                    "actual_section_keys": actual_sections,
                    "provider_document_id": provider_document_id,
                    "reasons": reasons,
                    "unit_index": unit_index,
                }
            )
        else:
            passed += 1

    routed = sum(count for size, count in route_cardinality.items() if size > 0)
    section_routed = sum(
        count for size, count in section_cardinality.items() if size > 0
    )
    retrievable = sum(
        1
        for row in row_by_identity.values()
        if row.get("semantic_keys") or row.get("section_keys")
    )
    row_count = len(row_by_identity)
    return {
        "contract_version": "semantic_route_gold_review.v1",
        "evaluation_id": evaluation.get("evaluation_id"),
        "router_version": evaluation.get("router_version"),
        "taxonomy_version": evaluation.get("taxonomy_version"),
        "case_count": len(cases),
        "passed": passed,
        "failed": len(findings),
        "findings": findings,
        "coverage": {
            "rows": row_count,
            "routed": routed,
            "null": row_count - routed,
            "routed_rate": round(routed / row_count, 6) if row_count else 0.0,
            "section_routed": section_routed,
            "section_routed_rate": (
                round(section_routed / row_count, 6) if row_count else 0.0
            ),
            "retrievable_by_route_or_section": retrievable,
            "retrievable_rate": (
                round(retrievable / row_count, 6) if row_count else 0.0
            ),
            "decision_sources": dict(sorted(decision_sources.items())),
            "route_cardinality": {
                str(size): count for size, count in sorted(route_cardinality.items())
            },
            "section_cardinality": {
                str(size): count
                for size, count in sorted(section_cardinality.items())
            },
            "by_effective_filing_type": {
                filing_type: {
                    **dict(sorted(counts.items())),
                    "routed_rate": round(
                        counts["routed"] / counts["rows"],
                        6,
                    ),
                    "section_routed_rate": round(
                        counts["section_routed"] / counts["rows"],
                        6,
                    ),
                    "retrievable_rate": round(
                        counts["retrievable"] / counts["rows"],
                        6,
                    ),
                }
                for filing_type, counts in sorted(coverage_by_type.items())
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evaluation", type=Path)
    parser.add_argument(
        "--gold",
        type=Path,
        default=Path("docs/implementation/checks/semantic-route-gold.v1.json"),
    )
    args = parser.parse_args()
    evaluation = _load_object(args.evaluation)
    gold_bytes = args.gold.read_bytes()
    gold = json.loads(gold_bytes)
    if not isinstance(gold, dict):
        raise ValueError("semantic route gold root must be an object")
    result = review(evaluation=evaluation, gold=gold)
    result["gold_sha256"] = "sha256:" + hashlib.sha256(gold_bytes).hexdigest()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
