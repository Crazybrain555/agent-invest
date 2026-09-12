"""Output-equivalence snapshots; independent semantic truth lives in sibling tests."""

from dataclasses import replace
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts.provider_document_admission import (
    ProviderDocumentAdmissionError, SourcePdfObservation,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    provider_document_envelope_from_bytes, provider_document_envelope_from_payload,
    provider_document_envelope_to_bytes, provider_document_envelope_to_payload,
)
from disclosure_anchor.application.ports.provider_document_source import ProviderDocumentSourceError
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units, replay_provider_unit_search_binding,
    replay_provider_unit_search_binding_source_text,
)
from tests._provider_source_semantics_fixture import (
    SOURCE_BYTES, admit, admission_inputs, cases, json_value, sha,
)


SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "fixtures/provider_source_semantics_head_compatibility.json"


def snapshot():
    return json.loads(SNAPSHOT_PATH.read_text())


class ProviderSourceCompatibilityTests(unittest.TestCase):
    def test_public_admitted_signatures_keep_positional_and_hint_contract(self):
        signatures = snapshot()["signatures"]
        for function in (build_provider_units, replay_provider_unit_search_binding,
                         replay_provider_unit_search_binding_source_text):
            with self.subTest(function=function.__name__):
                self.assertEqual(str(inspect.signature(function)), signatures[function.__name__])

    def test_exact_pre_extraction_envelope_semantics_build_hashes_and_both_replays(self):
        snapshots = snapshot()["cases"]
        for name, (doc, observations) in cases().items():
            with self.subTest(case=name):
                expected = snapshots[name]
                admitted = admit(doc, observations)
                encoded = provider_document_envelope_to_bytes(admitted.envelope)
                self.assertEqual(encoded.hex(), expected["envelope_bytes_hex"])
                self.assertEqual(provider_document_envelope_to_payload(admitted.envelope), expected["envelope"])
                self.assertEqual(provider_document_envelope_from_bytes(encoded), admitted.envelope)
                self.assertEqual([json_value(x) for x in admitted.source_text_reconciliations], expected["source_text_reconciliations"])
                self.assertEqual([json_value(x) for x in admitted.source_quality_findings], expected["source_quality_findings"])
                self.assertEqual(json_value(admitted.effective_provider_document), expected["effective_provider_document"])
                result = build_provider_units(admitted)
                self.assertEqual(json_value(result), expected["build"])
                replayed = [
                    [draft.unit_index, binding.source.target_id,
                     list(replay_provider_unit_search_binding(admitted, draft, binding)),
                     replay_provider_unit_search_binding_source_text(admitted, draft, binding)]
                    for draft in result.units for binding in draft.locator.search_targets
                ]
                self.assertEqual(replayed, expected["replay"])

    def test_pre_extraction_codec_error_classes_messages_and_validation_order(self):
        original = snapshot()["cases"]["plain"]["envelope"]
        for name, mutate in (
            ("extra_envelope_field", lambda p: p.update(unexpected=True)),
            ("missing_document_field", lambda p: p["provider_document"].pop("effort")),
            ("bool_page_count", lambda p: p.update(source_pdf_page_count=True)),
            ("target_profile", lambda p: p["parser_target_identity"].update(language="en")),
            ("raw_block_hash", lambda p: p["provider_document"]["pages"][0]["blocks"][0].update(raw_item_sha256="sha256:" + "0" * 64)),
            ("raw_noncanonical", lambda p: p["provider_document"]["pages"][0]["blocks"][0].update(raw_item_json="{\"x\": 1}")),
            ("wrong_required_media", lambda p: p["provider_document"]["artifacts"][0].update(media_type="text/plain")),
            ("wrong_owner_path", lambda p: p.update(artifact_owner_processing_run_id="other-owner")),
        ):
            with self.subTest(case=name):
                payload = json.loads(json.dumps(original))
                mutate(payload)
                with self.assertRaises(ValueError) as raised:
                    provider_document_envelope_from_payload(payload)
                self.assertEqual({"class": type(raised.exception).__name__, "message": str(raised.exception)}, snapshot()["errors"][name])
        # An owner/path failure must remain earlier than content/profile failure.
        original["artifact_owner_processing_run_id"] = "other-owner"
        original["provider_document"]["parser_version"] = "3.4.5"
        with self.assertRaisesRegex(ValueError, "parser artifact root does not bind"):
            provider_document_envelope_from_payload(original)

    def test_materialized_and_postpublication_admission_preserve_same_semantic_result(self):
        doc, observations = cases()["repair_and_finding"]
        service, entity, run, source = admission_inputs(doc, observations)
        postpublication = service.admit(document=entity, run=run, artifact_owner=run, security_code="009999")
        materialized = service.admit_materialized(
            document=entity, envelope=source.envelope,
            provider_document_sha256=sha(provider_document_envelope_to_bytes(source.envelope)),
            expected_source_byte_count=len(SOURCE_BYTES), security_code="009999",
        )
        self.assertEqual(materialized, postpublication)
        self.assertEqual([r.source_index for r in materialized.source_text_reconciliations], [0])
        self.assertEqual([f.source_index for f in materialized.source_quality_findings], [1])

    def test_admission_still_checks_document_owner_record_source_and_rebuilt_projection(self):
        doc, _ = cases()["plain"]
        for family, modify, reason in (
            ("run_document", lambda entity, run, source: setattr(run, "document_id", "other-document"), "parse_owner_invalid"),
            ("owner", lambda entity, run, source: setattr(run, "artifact_owner_processing_run_id", "other-owner"), "parse_owner_invalid"),
            ("record_hash", lambda entity, run, source: setattr(run, "artifact_hash", sha("other record")), "provider_document_hash_mismatch"),
            ("source", lambda entity, run, source: setattr(source, "source_identity", SourcePdfObservation(sha("other source"), len(SOURCE_BYTES), 1)), "source_pdf_identity_mismatch"),
            ("projection", lambda entity, run, source: setattr(source, "rebuilt", replace(doc, ocr_enabled=True)), "provider_document_projection_mismatch"),
        ):
            with self.subTest(family=family):
                service, entity, run, source = admission_inputs(doc)
                modify(entity, run, source)
                with self.assertRaises(ProviderDocumentAdmissionError) as raised:
                    service.admit(document=entity, run=run, artifact_owner=run, security_code="009999")
                self.assertEqual(raised.exception.reason_code, reason)
        service, entity, _, source = admission_inputs(doc)
        for receipt, byte_count, expected in (
            (sha("different materialized receipt"), len(SOURCE_BYTES), "provider_document_hash_mismatch"),
            (sha(provider_document_envelope_to_bytes(source.envelope)), len(SOURCE_BYTES) + 1, "source_pdf_identity_mismatch"),
        ):
            with self.subTest(expected=expected), self.assertRaises(ProviderDocumentAdmissionError) as raised:
                service.admit_materialized(document=entity, envelope=source.envelope,
                    provider_document_sha256=receipt, expected_source_byte_count=byte_count, security_code="009999")
            self.assertEqual(raised.exception.reason_code, expected)

    def test_native_source_error_retains_reason_retryability_and_cause(self):
        doc, _ = cases()["plain"]
        service, entity, run, source = admission_inputs(doc)
        failure = ProviderDocumentSourceError("native_fixture_unavailable", "literal source read failure", retryable=True)
        with patch.object(source, "observe_source_pdf_text", side_effect=failure):
            with self.assertRaises(ProviderDocumentAdmissionError) as raised:
                service.admit(document=entity, run=run, artifact_owner=run, security_code="009999")
        self.assertEqual(raised.exception.reason_code, "native_fixture_unavailable")
        self.assertTrue(raised.exception.retryable)
        self.assertIs(raised.exception.__cause__, failure)


if __name__ == "__main__":
    unittest.main()
