from __future__ import annotations

import copy
from typing import Any
import unittest

from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NativeObjectIssue,
    NativeStructureDiagnostics,
)
from disclosure_anchor.domain.errors import ParserOutputContractError
from scripts.audit_unit_corpus import (
    _exception_failure_family,
    _source_observations,
    _summary,
    _validate_raw_struct_tree_citations,
)

from tests.unit._native_index import native_index, native_node


class _ReasonedError(RuntimeError):
    reason_code = "native_structure_invalid"


class _GenericReasonedError(RuntimeError):
    reason_code = "parser_output_contract_error"


def _fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    ir: dict[str, Any] = {
        "elements": [
            {
                "source_item_index": 0,
                "page_idx": 0,
                "bbox": [90, 90, 310, 140],
                "text": "第 １ 章",
            },
            {
                "source_item_index": 1,
                "page_idx": 0,
                "bbox": [90, 150, 310, 200],
                "text": "第一节",
            },
        ],
        "structure_proof": {
            "headings": [
                {
                    "node_id": 1,
                    "parent_node_id": None,
                    "propagates": True,
                    "evidence_kinds": ["struct_tree"],
                    "native_role": "H1",
                    "native_node_id": 10,
                    "native_segment_id": "native_1",
                    "source_refs": [
                        {
                            "source_item_index": 0,
                            "field": "text",
                            "text_span": [0, 5],
                        }
                    ],
                },
                {
                    "node_id": 2,
                    "parent_node_id": 1,
                    "propagates": True,
                    "evidence_kinds": ["struct_tree"],
                    "native_role": "H2",
                    "native_node_id": 11,
                    "native_segment_id": "native_1",
                    "source_refs": [
                        {
                            "source_item_index": 1,
                            "field": "text",
                            "text_span": [0, 3],
                        }
                    ],
                },
            ]
        },
    }
    native: dict[str, Any] = {
        "nodes": [
            {
                "node_id": 10,
                "segment_id": "native_1",
                "standard_role": "H1",
                "ancestor_roles": [],
                "ancestor_node_ids": [],
                "mcid_refs": [{"page_idx": 0, "mcid": 7}],
            },
            {
                "node_id": 11,
                "segment_id": "native_1",
                "standard_role": "H2",
                "ancestor_roles": ["H1"],
                "ancestor_node_ids": [10],
                "mcid_refs": [{"page_idx": 0, "mcid": 8}],
            },
        ],
        "marked_content": [
            {
                "page_idx": 0,
                "mcid": 7,
                "object_order": 1,
                "bbox": [100, 100, 300, 130],
                "text": "第1章",
            },
            {
                "page_idx": 0,
                "mcid": 8,
                "object_order": 2,
                "bbox": [100, 160, 300, 190],
                "text": "第一节",
            },
        ],
    }
    return ir, native


class RawStructTreeCitationOracleTests(unittest.TestCase):
    def test_exact_citations_and_fail_closed_boundaries(self) -> None:
        ir, native = _fixture()
        self.assertEqual(_validate_raw_struct_tree_citations(ir, native), 2)

        spacing_ir, spacing_native = copy.deepcopy((ir, native))
        spacing_native["nodes"][0]["mcid_refs"].extend(
            [{"page_idx": 0, "mcid": 9}, {"page_idx": 0, "mcid": 10}]
        )
        spacing_native["marked_content"].extend(
            [
                {
                    "page_idx": 0,
                    "mcid": 9,
                    "object_order": 3,
                    "bbox": [120, 100, 121, 101],
                    "text": " ",
                },
                {
                    "page_idx": 0,
                    "mcid": 10,
                    "object_order": 4,
                    "bbox": None,
                    "text": None,
                },
            ]
        )
        self.assertEqual(
            _validate_raw_struct_tree_citations(spacing_ir, spacing_native),
            2,
        )

        cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        toc_ir, toc_native = copy.deepcopy((ir, native))
        toc_native["nodes"][1]["ancestor_roles"].append("TOC")
        cases.append(("TOC ancestry", toc_ir, toc_native))

        segment_ir, segment_native = copy.deepcopy((ir, native))
        segment_ir["structure_proof"]["headings"][1]["native_segment_id"] = "native_2"
        segment_native["nodes"][1]["segment_id"] = "native_2"
        cases.append(("cross-segment parent", segment_ir, segment_native))

        node_ir, node_native = copy.deepcopy((ir, native))
        node_ir["structure_proof"]["headings"][1]["native_node_id"] = 99
        cases.append(("missing native node", node_ir, node_native))

        role_ir, role_native = copy.deepcopy((ir, native))
        role_ir["structure_proof"]["headings"][1]["native_role"] = "H3"
        cases.append(("wrong native role", role_ir, role_native))

        reused_ir, reused_native = copy.deepcopy((ir, native))
        duplicate = copy.deepcopy(reused_ir["structure_proof"]["headings"][1])
        duplicate["node_id"] = 3
        reused_ir["structure_proof"]["headings"].append(duplicate)
        cases.append(("reused native identity", reused_ir, reused_native))

        ambiguous_ir, ambiguous_native = copy.deepcopy((ir, native))
        ambiguous_ir["elements"].append(copy.deepcopy(ambiguous_ir["elements"][1]))
        ambiguous_ir["elements"][2]["source_item_index"] = 2
        ambiguous_ir["structure_proof"]["headings"][1]["source_refs"].append(
            {"source_item_index": 2, "field": "text", "text_span": [0, 3]}
        )
        cases.append(("ambiguous source binding", ambiguous_ir, ambiguous_native))

        unbound_ir, unbound_native = copy.deepcopy((ir, native))
        unbound_native["marked_content"][1]["bbox"] = [400, 400, 500, 430]
        cases.append(("missing source binding", unbound_ir, unbound_native))

        missing_ir, missing_native = copy.deepcopy((ir, native))
        missing_native["marked_content"].pop()
        cases.append(("missing marked content", missing_ir, missing_native))

        for label, failing_ir, failing_native in cases:
            with self.subTest(label), self.assertRaises(ValueError):
                _validate_raw_struct_tree_citations(failing_ir, failing_native)


class CorpusObservabilityTests(unittest.TestCase):
    def test_failure_family_uses_typed_chain_and_never_message_text(self) -> None:
        for explicit_cause in (False, True):
            with self.subTest(explicit_cause=explicit_cause):
                try:
                    raise _ReasonedError("typed root")
                except _ReasonedError as inner:
                    try:
                        if explicit_cause:
                            raise RuntimeError("wrapper changed") from inner
                        raise RuntimeError("wrapper changed")
                    except RuntimeError as outer:
                        self.assertEqual(
                            _exception_failure_family(outer),
                            "native_structure_invalid",
                        )

        self.assertEqual(
            _exception_failure_family(
                RuntimeError("message mentions native_structure_invalid")
            ),
            "audit_execution_error",
        )
        self.assertEqual(
            _exception_failure_family(ParserOutputContractError("generic contract")),
            "parser_output_contract_error",
        )
        try:
            raise _GenericReasonedError("inner generic")
        except _GenericReasonedError as inner:
            try:
                raise _ReasonedError("outer specific") from inner
            except _ReasonedError as outer:
                self.assertEqual(
                    _exception_failure_family(outer),
                    "native_structure_invalid",
                )

    def test_structure_and_retrieval_observations_aggregate_without_guessing(
        self,
    ) -> None:
        observations = _source_observations(
            {
                "structure_proof": {
                    "conflicts": [{"relation": "native_heading_unaligned"}],
                    "coverage": {
                        "native_heading_candidates": 3,
                        "proven_heading_nodes": 2,
                    },
                }
            },
            source_evidence={
                "pages": [],
                "coverage": {"source_atoms": 7},
                "retrieval_runs": [
                    {"boundary_basis": "native_complete_cell"},
                    {"join_algorithm": "legacy-without-basis"},
                ],
            },
            native_structure=native_index(
                native_status="partial",
                pdfium_tagged=True,
                nodes=[
                    native_node(1, "Table"),
                    native_node(2, "TD"),
                    native_node(3, "TD"),
                ],
                diagnostics=NativeStructureDiagnostics(
                    parent_conflicts=1,
                    root_reachable_nodes=3,
                    visible_mcid_anchors=0,
                    marked_content_objects=0,
                    referenced_mcid_refs=0,
                    resolved_mcid_refs=0,
                    unresolved_reasons=(
                        "stream_scoped_mcid",
                        "stream_scoped_mcid",
                    ),
                    unresolved_mcid_refs=((0, 4),),
                    object_issues=(
                        NativeObjectIssue(0, 1, 0, "bbox_unavailable"),
                        NativeObjectIssue(0, 2, 1, "text_unavailable"),
                    ),
                ),
            ),
        )
        self.assertEqual(
            observations["retrieval_boundary_basis"],
            {"native_complete_cell": 1, "unspecified": 1},
        )
        self.assertEqual(
            observations["native_pdf_structure"],
            {
                "status": "partial",
                "pdfium_tagged": True,
                "roles": {"TD": 2, "Table": 1},
                "diagnostics": {
                    "marked_content_objects": 0,
                    "object_issues": 2,
                    "parent_conflicts": 1,
                    "referenced_mcid_refs": 0,
                    "resolved_mcid_refs": 0,
                    "root_reachable_nodes": 3,
                    "unresolved": 2,
                    "unresolved_mcid_refs": 1,
                    "visible_mcid_anchors": 0,
                },
                "unresolved_reasons": {"stream_scoped_mcid": 2},
                "object_issues": {
                    "bbox_unavailable": 1,
                    "text_unavailable": 1,
                },
            },
        )

        summary = _summary(
            [
                {
                    "ok": True,
                    "failure_family": None,
                    "company_name": "甲",
                    "filing_type": "annual_report",
                    "metrics": {"unit_count": 1, "unit_kinds": {"table": 1}},
                    "source_observations": observations,
                    "findings": [],
                },
                {
                    "ok": False,
                    "failure_family": "native_structure_invalid",
                    "company_name": "乙",
                    "filing_type": "annual_report",
                    "metrics": {},
                    "source_observations": {},
                    "findings": [
                        {
                            "code": "audit_execution_error",
                            "severity": "error",
                        }
                    ],
                },
            ],
            manifest_hash="sha256:" + "a" * 64,
            source_replay=True,
        )
        self.assertEqual(
            summary["failure_families"],
            {"native_structure_invalid": 1},
        )
        source_summary = summary["source_observations"]
        self.assertEqual(
            source_summary["structure_coverage"],
            {"native_heading_candidates": 3, "proven_heading_nodes": 2},
        )
        self.assertEqual(
            source_summary["retrieval_boundary_basis"],
            {"native_complete_cell": 1, "unspecified": 1},
        )
        self.assertEqual(
            source_summary["native_pdf_structure"],
            {
                "statuses": {"partial": 1, "unavailable": 1},
                "pdfium_tagged": {"true": 1, "unknown": 1},
                "roles": {"TD": 2, "Table": 1},
                "diagnostics": {
                    "marked_content_objects": 0,
                    "object_issues": 2,
                    "parent_conflicts": 1,
                    "referenced_mcid_refs": 0,
                    "resolved_mcid_refs": 0,
                    "root_reachable_nodes": 3,
                    "unresolved": 2,
                    "unresolved_mcid_refs": 1,
                    "visible_mcid_anchors": 0,
                },
                "unresolved_reasons": {"stream_scoped_mcid": 2},
                "documents_with_object_issues": 1,
                "object_issues": {
                    "bbox_unavailable": 1,
                    "text_unavailable": 1,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
