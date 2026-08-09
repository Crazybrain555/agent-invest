"""Generated JSON Schema contracts for Simple95 acceptance receipts."""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from scripts.simple95_acceptance_receipts import (
    DIFF_RECEIPT_SCHEMA,
    RUN_RECEIPT_SCHEMA,
    ReceiptContractError,
    build_run_receipt,
    diff_run_receipts,
    export_receipt_schemas,
    validate_diff_receipt,
    validate_run_receipt,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED_ROOT = REPO_ROOT / "contracts/acceptance"
_DIGEST = "sha256:" + "a" * 64
_BINDING = {
    "code_commit_sha": "1" * 40,
    "corpus_manifest_sha256": _DIGEST,
    "document_id": "doc_1",
    "provider": "fixture",
    "provider_document_id": "provider_doc_1",
    "source_pdf_sha256": _DIGEST,
    "processing_run_id": "run_1",
    "parser_target_sha256": _DIGEST,
    "parse_receipt_sha256": _DIGEST,
    "normalized_ir_sha256": _DIGEST,
    "source_evidence_sha256": _DIGEST,
    "provider_artifact_hashes": {
        role: {"sha256": _DIGEST, "size_bytes": 2}
        for role in (
            "content_list",
            "content_list_v2",
            "middle",
            "model",
            "pdf_structure",
            "source_evidence",
            "visual_semantics",
            "parse_receipt",
        )
    },
}
_GATE = {
    "contract_version": "publication-gate.v1",
    "capability": "source-evidence-bounded-content-conservation",
    "decision": "publish",
    "checks": {
        "metric_shape_closed": True,
        "audit_ok": True,
        "error_count_zero": True,
        "coverage_closed": True,
        "primary_search_closed": True,
    },
    "diagnostics": {
        "error_count": 0,
        "coverage_uncovered": 0,
        "primary_search_missing": 0,
    },
}


def _empty_receipt() -> dict[str, object]:
    return build_run_receipt(
        binding=_BINDING,
        unit_inputs=[],
        publication_gate=_GATE,
        findings=[],
        hierarchy_status="flattened_unresolved",
    )


class Simple95AcceptanceReceiptContractTests(unittest.TestCase):
    def test_exported_schemas_match_fresh_export_byte_for_byte(self) -> None:
        for schema in (RUN_RECEIPT_SCHEMA, DIFF_RECEIPT_SCHEMA):
            Draft202012Validator.check_schema(schema)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            written = export_receipt_schemas(output_root)
            self.assertEqual(len(written), 2)
            for fresh in written:
                tracked = TRACKED_ROOT / fresh.name
                self.assertTrue(tracked.is_file(), tracked)
                self.assertEqual(fresh.read_bytes(), tracked.read_bytes())

    def test_schemas_are_closed_and_semantic_replay_rejects_tampering(self) -> None:
        before = _empty_receipt()
        after = _empty_receipt()
        diff = diff_run_receipts(before, after)
        self.assertEqual(list(Draft202012Validator(RUN_RECEIPT_SCHEMA).iter_errors(before)), [])
        self.assertEqual(list(Draft202012Validator(DIFF_RECEIPT_SCHEMA).iter_errors(diff)), [])
        validate_run_receipt(before)
        validate_diff_receipt(diff, before=before, after=after)

        extra = copy.deepcopy(before)
        extra["generated_at"] = "2026-08-09T00:00:00Z"
        self.assertTrue(
            list(Draft202012Validator(RUN_RECEIPT_SCHEMA).iter_errors(extra))
        )
        with self.assertRaises(ReceiptContractError):
            validate_run_receipt(extra)

        stale_diff = copy.deepcopy(diff)
        stale_diff["content_delta"] = True
        with self.assertRaisesRegex(ReceiptContractError, "does not replay"):
            validate_diff_receipt(stale_diff, before=before, after=after)


if __name__ == "__main__":
    unittest.main()
