from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.generate_current_source_replay import (
    _atomic_write_new,
    _evaluation_row,
    _receipt_relpath,
    _replay_guarantee,
)
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION,
)
from disclosure_anchor.application.contracts.document_unit_body_status import (
    derive_document_unit_body_status,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V1,
    SEMANTIC_ROUTE_RECEIPT_VERSION,
)
from disclosure_anchor.domain import entities as e


_CONTENT_HASH = "sha256:" + "a" * 64
_QUERY_HASH = "sha256:" + "b" * 64


def _document(*, filing_type: str | None = "annual_report") -> e.Document:
    return e.Document(
        document_id="doc",
        status="unitized",
        provider_document_id="provider-doc",
        class_filing_type=filing_type,
        current_processing_run_id="run",
    )


def _run(**overrides: object) -> e.ProcessingRun:
    values: dict[str, object] = {
        "processing_run_id": "run",
        "document_id": "doc",
        "artifact_owner_processing_run_id": "owner",
        "run_kind": "rebuild_units",
        "status": "succeeded",
        "is_active": True,
        "document_units_relpath": "derived/doc/run/document_units.v1.jsonl",
    }
    values.update(overrides)
    return e.ProcessingRun(**values)  # type: ignore[arg-type]


def _unit(**overrides: object) -> e.DocumentUnit:
    values: dict[str, object] = {
        "asset_id": "asset",
        "document_id": "doc",
        "processing_run_id": "run",
        "provider_document_id": "provider-doc",
        "payload_kind": "text",
        "order_index": 1,
        "payload": {"text": "完整正文"},
        "content_hash": _CONTENT_HASH,
        "query_projection_hash": _QUERY_HASH,
        "heading_path": ["第一节"],
        "semantic_keys": ["revenue_and_cost"],
        "section_keys": ["revenue_and_cost"],
        "title": "营业收入和成本",
    }
    values.update(overrides)
    return e.DocumentUnit(**values)  # type: ignore[arg-type]


class GenerateCurrentSourceReplayTests(unittest.TestCase):
    def test_replay_guarantee_uses_the_current_builder_identity(self) -> None:
        guarantee = _replay_guarantee()

        self.assertIn(
            f"rebuilt {PROVIDER_UNIT_BUILDER_VERSION}",
            guarantee,
        )
        self.assertNotIn("provider_unit.v18", guarantee)

    def test_body_status_is_derived_without_a_public_view_or_database(self) -> None:
        cases = (
            ("text", {"text": "正文"}, None, "content"),
            ("text", {"text": ""}, "一、标题", "heading_only"),
            ("text", {"text": ""}, None, "empty"),
            ("text", {"text": "", "extra": ""}, None, "content"),
            (
                "mixed",
                {"parts": [{"content": "", "content_artifacts": [{"x": 1}]}]},
                "封面",
                "heading_only",
            ),
            (
                "mixed",
                {"parts": [{"values": ["", None], "content_artifacts": []}]},
                None,
                "empty",
            ),
            ("mixed", {"parts": [{"values": [0]}]}, None, "content"),
            ("mixed", {"parts": [{"content": "可检索正文"}]}, None, "content"),
            ("table", {"rows": []}, None, "content"),
            ("qa", {"question": "", "answer": ""}, None, "content"),
            ("mixed", {"parts": [{"content": "\t"}]}, None, "content"),
            ("mixed", {"parts": [{"values": ["\n"]}]}, "标题", "content"),
            ("mixed", {"parts": [{"content": "\u3000"}]}, None, "content"),
        )
        for payload_kind, payload, title, expected in cases:
            with self.subTest(payload_kind=payload_kind, payload=payload, title=title):
                self.assertEqual(
                    derive_document_unit_body_status(
                        payload_kind=payload_kind,
                        payload=payload,
                        title=title,
                    ),
                    expected,
                )

        with self.assertRaisesRegex(ValueError, "part must be an object"):
            derive_document_unit_body_status(
                payload_kind="mixed",
                payload={"parts": ["not-an-object"]},
                title=None,
            )

    def test_evaluation_row_preserves_current_identity_routes_and_hashes(self) -> None:
        row = _evaluation_row(
            document=_document(),
            run=_run(),
            unit=_unit(),
            decision_source="deterministic",
            body_status="content",
        )

        self.assertEqual(row["provider_document_id"], "provider-doc")
        self.assertEqual(row["unit_index"], 0)
        self.assertEqual(row["effective_filing_type"], "annual_report")
        self.assertEqual(row["semantic_keys"], ["revenue_and_cost"])
        self.assertEqual(row["section_keys"], ["revenue_and_cost"])
        self.assertEqual(row["content_hash"], _CONTENT_HASH)
        self.assertEqual(row["query_projection_hash"], _QUERY_HASH)

    def test_evaluation_row_fails_closed_on_missing_filing_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "effective filing type"):
            _evaluation_row(
                document=_document(filing_type=None),
                run=_run(),
                unit=_unit(),
                decision_source="rule_abstain",
                body_status="content",
            )

    def test_evaluation_row_fails_closed_on_generation_or_hash_drift(self) -> None:
        for unit in (
            _unit(processing_run_id="old-run"),
            _unit(query_projection_hash="not-a-hash"),
        ):
            with self.subTest(unit=unit):
                with self.assertRaises(ValueError):
                    _evaluation_row(
                        document=_document(),
                        run=_run(),
                        unit=unit,
                        decision_source="deterministic",
                        body_status="content",
                    )

    def test_receipt_path_supports_current_v2_and_explicit_legacy_v1(self) -> None:
        current_path, current_version = _receipt_relpath(
            _run(
                semantic_route_receipts_relpath=(
                    "derived/doc/run/semantic_route_receipts.v2.jsonl"
                ),
                semantic_route_receipts_contract_version=(
                    SEMANTIC_ROUTE_RECEIPT_VERSION
                ),
            )
        )
        legacy_path, legacy_version = _receipt_relpath(_run())

        self.assertEqual(
            current_path,
            Path("derived/doc/run/semantic_route_receipts.v2.jsonl"),
        )
        self.assertEqual(current_version, SEMANTIC_ROUTE_RECEIPT_VERSION)
        self.assertEqual(
            legacy_path,
            Path("derived/doc/run/semantic_route_receipts.v1.jsonl"),
        )
        self.assertEqual(legacy_version, SEMANTIC_ROUTE_RECEIPT_V1)

    def test_evidence_write_never_replaces_an_existing_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "receipt.json"
            _atomic_write_new(path, b"first\n")

            with self.assertRaises(FileExistsError):
                _atomic_write_new(path, b"second\n")

            self.assertEqual(path.read_bytes(), b"first\n")


if __name__ == "__main__":
    unittest.main()
