"""Mutation and determinism tests for Simple95 acceptance receipts."""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.parse_receipt import build_parse_receipt
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.services.document_unit_audit import (
    DocumentAuditReport,
)
from disclosure_anchor.application.services.unit_builder.builder import UnitDraft
from scripts.audit_unit_corpus import ManifestEntry, ReceiptAuditObservation
from scripts.simple95_acceptance_receipts import (
    ReceiptContractError,
    build_run_receipt,
    build_run_receipt_from_observation,
    canonical_receipt_bytes,
    diff_run_receipts,
    load_run_receipts,
    main as receipt_main,
    unit_inputs_from_drafts,
    validate_diff_receipt,
    validate_run_receipt,
)
from tests.unit._current_ir import write_text_ir_bundle


_DIGEST = "sha256:" + "a" * 64
_PASS_GATE = {
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


def _binding(*, processing_run_id: str = "run_1") -> dict[str, object]:
    return {
        "code_commit_sha": "1" * 40,
        "corpus_manifest_sha256": _DIGEST,
        "document_id": "doc_1",
        "provider": "fixture",
        "provider_document_id": "provider_doc_1",
        "source_pdf_sha256": _DIGEST,
        "processing_run_id": processing_run_id,
        "parser_target_sha256": _DIGEST,
        "parse_receipt_sha256": _DIGEST,
        "normalized_ir_sha256": _DIGEST,
        "source_evidence_sha256": _DIGEST,
        "provider_artifact_hashes": {
            role: {"sha256": _DIGEST, "size_bytes": 10}
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


def _locator(*, transform: str = "clean_text.v1") -> dict[str, object]:
    return {
        "source_projection": {
            "version": "unit-source-projection.v4",
            "payload": {
                "kind": "text_identity",
                "sources": [],
                "target_field": "payload.text",
                "transform": transform,
            },
            "heading_path": [],
            "structured": [],
            "provenance": [],
            "search_targets": ["payload.text"],
            "search_atoms": [],
            "physical_context": None,
        }
    }


def _draft(
    text: str = "甲\n乙",
    *,
    heading_path: tuple[str, ...] = ("第一节",),
    section_path: tuple[int, ...] = (1,),
    source_order: int = 0,
    transform: str = "clean_text.v1",
    native_order_anchor: tuple[int, int, int] | None = None,
) -> UnitDraft:
    return UnitDraft(
        payload_kind="text",
        payload={"text": text},
        source_order=source_order,
        heading_path=list(heading_path),
        section_path=list(section_path),
        title=heading_path[-1] if heading_path else None,
        semantic_key="document_content",
        semantic_keys=["document_content"],
        artifact_locator=_locator(transform=transform),
        native_order_anchor=native_order_anchor,
    )


def _mixed_draft(*, grouped: bool) -> UnitDraft:
    parts = [
        {
            "kind": "text",
            "text": text,
            "artifact_locator": {
                "source_projection": {
                    "version": "unit-source-projection.v4",
                    "payload": {
                        "kind": "text_identity_exact",
                        "sources": [{"source": {}, "field": {}}],
                        "target_field": "payload.text",
                        "transform": "test",
                    },
                    "heading_path": [],
                    "structured": [],
                    "provenance": [],
                    "search_targets": ["payload.text"],
                    "search_atoms": [],
                    "physical_context": None,
                }
            },
        }
        for text in ("股", "份变动")
    ]
    atoms = (
        [
            {
                "boundary": {
                    "kind": "source_evidence_run",
                    "source_evidence_sha256": _DIGEST,
                    "page_idx": 0,
                    "run_index": 0,
                },
                "target_fields": [
                    "payload.parts.0.text",
                    "payload.parts.1.text",
                ],
                "transform": "exact_concat.v1",
            }
        ]
        if grouped
        else []
    )
    return UnitDraft(
        payload_kind="mixed",
        payload={"semantic_type": "document", "parts": parts},
        source_order=0,
        semantic_key="document_content",
        semantic_keys=["document_content"],
        artifact_locator={
            "source_projection": {
                "version": "unit-source-projection.v4",
                "payload": {
                    "kind": "container",
                    "sources": [],
                    "target_field": "payload.parts",
                    "transform": "test",
                },
                "heading_path": [],
                "structured": [],
                "provenance": [],
                "search_targets": [],
                "search_atoms": atoms,
                "physical_context": None,
            }
        },
    )


def _receipt(
    drafts: list[UnitDraft],
    *,
    binding: dict[str, object] | None = None,
    gate: dict[str, object] | None = None,
    asset_ids: list[str] | None = None,
    stored_hash_overrides: list[dict[str, str] | None] | None = None,
    retrieval_rules_version: str = "rp-2026.07-5",
) -> dict[str, object]:
    return build_run_receipt(
        binding=binding or _binding(),
        unit_inputs=unit_inputs_from_drafts(
            drafts,
            asset_ids=asset_ids,
            stored_hash_overrides=stored_hash_overrides,
        ),
        publication_gate=gate or _PASS_GATE,
        findings=[],
        hierarchy_status="flattened_unresolved",
        retrieval_rules_version=retrieval_rules_version,
    )


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _run_observation(
    root: Path,
    *,
    target_version: str = "parser-target.v2",
    include_parse_receipt: bool = True,
) -> ReceiptAuditObservation:
    data = root / "data"
    data.mkdir(parents=True)
    pdf = b"%PDF-1.7\nfixture"
    (data / "raw.pdf").write_bytes(pdf)
    target = ParserTargetIdentity(
        name="MinerU",
        package_version="3.4.0",
        backend="pipeline",
        method="auto",
        language="ch",
        formula=True,
        table=True,
        runtime_bundle_identity_sha256="sha256:" + "b" * 64,
        target_contract_version=target_version,
    ).to_payload()
    artifact_payloads = {
        "content_list": b"[]",
        "content_list_v2": b"[]",
        "middle": b"{}",
        "model": b"[]",
        "pdf_structure": b"{}",
        "source_evidence": b"{}",
        "visual_semantics": b"{}",
    }
    if include_parse_receipt:
        artifact_payloads["parse_receipt"] = json.dumps(
            build_parse_receipt(
                source_pdf_sha256=_sha256(pdf),
                parser_target_payload=target,
                server_url=None,
                http_request_concurrency=None,
                timeout_seconds=None,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    files: dict[str, dict[str, object]] = {}
    for role, raw in artifact_payloads.items():
        path = data / "parser" / f"{role}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        files[role] = {
            "availability": "present",
            "relpath": str(path.relative_to(data)),
            "sha256": _sha256(raw),
            "size_bytes": len(raw),
        }
    frozen = {
        "contract_version": "normalized_ir.v4",
        "document_id": "doc_1",
        "source_pdf": "raw.pdf",
        "source_pdf_sha256": _sha256(pdf),
        "parser": target,
        "parser_artifacts": {"artifact_root_relpath": "parser", "files": files},
    }
    frozen_bytes = json.dumps(
        frozen,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    entry = ManifestEntry(
        document_id="doc_1",
        provider="fixture",
        provider_document_id="provider_doc_1",
        processing_run_id="run_1",
        security_code="000001",
        security_name=None,
        company_name=None,
        title="公告",
        filing_type="other",
        normalized_ir_relpath="normalized_ir.v4.json",
        normalized_ir_sha256=_sha256(frozen_bytes),
    )
    report = DocumentAuditReport(
        document_id="doc_1",
        metrics={
            "hierarchy_capability": {"status": "flattened_unresolved"},
            "coverage": {"uncovered": 0},
            "primary_search": {"missing_carriers": 0},
            "error_count": 0,
        },
    )
    return ReceiptAuditObservation(
        entry=entry,
        data_root=root,
        frozen_normalized_ir_bytes=frozen_bytes,
        frozen_normalized_ir=frozen,
        replayed_normalized_ir=frozen,
        drafts=(_draft(),),
        report=report,
        audit_result={},
    )


class Simple95AcceptanceReceiptTests(unittest.TestCase):
    def test_four_delta_families_are_distinct_and_explained(self) -> None:
        baseline = _receipt([_draft()])
        blocked_gate = copy.deepcopy(_PASS_GATE)
        blocked_gate["diagnostics"]["coverage_uncovered"] = 1
        blocked_gate["checks"]["coverage_closed"] = False
        blocked_gate["decision"] = "block"
        cases = {
            "payload": (
                baseline,
                _receipt([_draft("甲\n丙")]),
                (True, False, False, False),
            ),
            "owner": (
                baseline,
                _receipt([_draft(section_path=(2,))]),
                (False, True, False, False),
            ),
            "source_order": (
                baseline,
                _receipt([_draft(source_order=9)]),
                (False, True, False, False),
            ),
            "native_before_first_carrier": (
                baseline,
                _receipt([_draft(native_order_anchor=(-1, 0, 0))]),
                (False, True, False, False),
            ),
            "published_order": (
                _receipt([_draft("甲", source_order=0), _draft("乙", source_order=1)]),
                _receipt([_draft("乙", source_order=1), _draft("甲", source_order=0)]),
                (False, True, False, False),
            ),
            "content_owner_order_pairing": (
                _receipt([_draft("甲", source_order=0), _draft("乙", source_order=1)]),
                _receipt([_draft("乙", source_order=0), _draft("甲", source_order=1)]),
                (False, True, False, False),
            ),
            "nfkc_equivalent_content_owner_order_pairing": (
                _receipt([_draft("Ａ", source_order=0), _draft("A", source_order=1)]),
                _receipt([_draft("A", source_order=0), _draft("Ａ", source_order=1)]),
                (False, True, False, False),
            ),
            "heading_path": (
                baseline,
                _receipt([_draft(heading_path=("第二节",))]),
                (False, True, True, False),
            ),
            "search_plan": (
                baseline,
                _receipt([_draft(transform="safe_text.v1")]),
                (False, False, True, False),
            ),
            "search_grouping": (
                _receipt([_mixed_draft(grouped=False)]),
                _receipt([_mixed_draft(grouped=True)]),
                (False, False, True, False),
            ),
            "publication": (
                baseline,
                _receipt([_draft()], gate=blocked_gate),
                (False, False, False, True),
            ),
        }
        fields = (
            "content_delta",
            "structure_order_owner_delta",
            "query_search_plan_delta",
            "publication_outcome_delta",
        )
        for name, (before, changed, expected) in cases.items():
            with self.subTest(name=name):
                diff = diff_run_receipts(before, changed)
                self.assertEqual(tuple(diff[field] for field in fields), expected)
                self.assertEqual(diff["unexplained_deltas"], [])
                if "owner_order_pairing" in name:
                    self.assertEqual(
                        diff["changed_fields"]["structure_order_owner"][
                            "content_occurrence_owner_order_pairing"
                        ],
                        2,
                    )
                validate_diff_receipt(diff, before=before, after=changed)

    def test_asset_churn_and_version_labels_do_not_change_content(self) -> None:
        drafts = [_draft(), _draft("丙")]
        first = _receipt(drafts, asset_ids=["asset_a", "asset_b"])
        second = _receipt(drafts, asset_ids=["asset_x", "asset_y"])
        self.assertEqual(canonical_receipt_bytes(first), canonical_receipt_bytes(second))
        target_v2_binding = _binding()
        target_v2_binding["parser_target_sha256"] = "sha256:" + "b" * 64
        target_version_changed = _receipt(drafts, binding=target_v2_binding)
        target_diff = diff_run_receipts(first, target_version_changed)
        self.assertFalse(target_diff["content_delta"])
        self.assertEqual(
            target_diff["changed_fields"]["run_binding"]["parser_target_sha256"],
            1,
        )
        version_changed = _receipt(drafts, retrieval_rules_version="rp-2026.08-1")
        diff = diff_run_receipts(first, version_changed)
        self.assertFalse(diff["content_delta"])
        self.assertTrue(diff["query_search_plan_delta"])

        other_provider_binding = _binding()
        other_provider_binding["provider"] = "other_provider"
        other_provider = _receipt(drafts, binding=other_provider_binding)
        self.assertNotEqual(
            canonical_receipt_bytes(first),
            canonical_receipt_bytes(other_provider),
        )
        with self.assertRaisesRegex(
            ReceiptContractError, "different provider values"
        ):
            diff_run_receipts(first, other_provider)

    def test_duplicate_multiplicity_is_detected(self) -> None:
        before = _receipt([_draft()])
        after = _receipt([_draft(), _draft()])
        diff = diff_run_receipts(before, after)
        self.assertTrue(diff["content_delta"])
        self.assertEqual(
            diff["changed_fields"]["content"]["content_hash_multiset"], 1
        )

    def test_stale_stored_hash_and_tampered_root_are_rejected(self) -> None:
        with self.assertRaisesRegex(ReceiptContractError, "stored unit hashes"):
            _receipt(
                [_draft()],
                stored_hash_overrides=[{"content_hash": "sha256:" + "0" * 64}],
            )
        receipt = _receipt([_draft()])
        receipt["content_multiset_root"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ReceiptContractError, "roots do not replay"):
            validate_run_receipt(receipt)

        contradictory_gate = _receipt([_draft()])
        contradictory_gate["publication_gate"]["diagnostics"]["error_count"] = 7
        with self.assertRaisesRegex(
            ReceiptContractError, "error count differs from audit findings"
        ):
            validate_run_receipt(contradictory_gate)

        error_finding = {
            "code": "TEST_ERROR",
            "severity": "error",
            "message": "synthetic receipt validation mutation",
            "source_ref": None,
            "unit_order": 1,
        }
        with self.assertRaisesRegex(
            ReceiptContractError, "error count differs from audit findings"
        ):
            build_run_receipt(
                binding=_binding(),
                unit_inputs=unit_inputs_from_drafts([_draft()]),
                publication_gate=_PASS_GATE,
                findings=[error_finding],
                hierarchy_status="flattened_unresolved",
            )

    def test_run_binding_requires_current_target_and_parse_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            current = _run_observation(Path(temp_dir) / "current")
            receipt = build_run_receipt_from_observation(
                current,
                code_commit_sha="1" * 40,
                corpus_manifest_sha256=_DIGEST,
            )
            self.assertEqual(receipt["provider"], "fixture")
            self.assertEqual(receipt["parse_receipt_sha256"], receipt[
                "provider_artifact_hashes"
            ]["parse_receipt"]["sha256"])

            legacy = _run_observation(
                Path(temp_dir) / "legacy", target_version="parser-target.v1"
            )
            with self.assertRaisesRegex(ReceiptContractError, "parser-target.v2"):
                build_run_receipt_from_observation(
                    legacy,
                    code_commit_sha="1" * 40,
                    corpus_manifest_sha256=_DIGEST,
                )

            missing = _run_observation(
                Path(temp_dir) / "missing", include_parse_receipt=False
            )
            with self.assertRaisesRegex(ReceiptContractError, "required roles"):
                build_run_receipt_from_observation(
                    replace(missing),
                    code_commit_sha="1" * 40,
                    corpus_manifest_sha256=_DIGEST,
                )

    def test_current_v2_audit_cli_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "service"
            data = data_root / "data"
            ir_relpath = Path(
                "derived/normalized_ir/fixture/000001/provider_doc_1/"
                "run_1/normalized_ir.v4.json"
            )
            write_text_ir_bundle(
                data,
                ir_relpath,
                source_pdf="raw.pdf",
                source_pdf_bytes=b"%PDF-1.7\nsource-replay-fixture",
            )
            ir_bytes = (data / ir_relpath).read_bytes()
            manifest = root / "manifest.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "document_id": "doc_1",
                        "provider": "fixture",
                        "provider_document_id": "provider_doc_1",
                        "processing_run_id": "run_1",
                        "security_code": "000001",
                        "security_name": None,
                        "company_name": "Fixture Co",
                        "title": "公告",
                        "filing_type": "other",
                        "normalized_ir_relpath": str(ir_relpath),
                        "normalized_ir_sha256": _sha256(ir_bytes),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            outputs = [root / "receipt-1.jsonl", root / "receipt-2.jsonl"]
            for output in outputs:
                with mock.patch(
                    "scripts.simple95_acceptance_receipts."
                    "_require_current_clean_commit"
                ):
                    self.assertEqual(
                        receipt_main(
                            [
                                "build-run",
                                "--manifest",
                                str(manifest),
                                "--data-root",
                                str(data_root),
                                "--out",
                                str(output),
                                "--code-commit-sha",
                                "1" * 40,
                            ]
                        ),
                        0,
                    )
                self.assertEqual(len(load_run_receipts(output)), 1)
            self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())

    def test_run_and_diff_receipts_are_byte_identical_for_same_input(self) -> None:
        first = _receipt([_draft(), _draft("丙")])
        second = _receipt([_draft(), _draft("丙")])
        self.assertEqual(canonical_receipt_bytes(first), canonical_receipt_bytes(second))
        changed = _receipt([_draft(), _draft("丁")])
        self.assertEqual(
            canonical_receipt_bytes(diff_run_receipts(first, changed)),
            canonical_receipt_bytes(diff_run_receipts(second, changed)),
        )

    def test_receipt_is_observer_only_and_does_not_mutate_inputs(self) -> None:
        drafts = [_draft()]
        unit_inputs = unit_inputs_from_drafts(drafts)
        original_units = copy.deepcopy(unit_inputs)
        gate = copy.deepcopy(_PASS_GATE)
        original_gate = copy.deepcopy(gate)
        build_run_receipt(
            binding=_binding(),
            unit_inputs=unit_inputs,
            publication_gate=gate,
            findings=[],
            hierarchy_status="flattened_unresolved",
        )
        self.assertEqual(unit_inputs, original_units)
        self.assertEqual(gate, original_gate)
        source_root = Path(__file__).resolve().parents[2] / "src/disclosure_anchor"
        reverse_imports = [
            path
            for path in source_root.rglob("*.py")
            if "simple95_acceptance_receipts" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(reverse_imports, [])


if __name__ == "__main__":
    unittest.main()
