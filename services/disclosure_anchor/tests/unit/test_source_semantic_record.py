"""Hand-derived closed source records and strict/untrusted decoder distinction."""

from dataclasses import FrozenInstanceError, replace
import json
import unittest

from disclosure_anchor.application.services.source_semantic_record import (
    DecodedSourceSemanticRecord, UntrustedSourceSemanticCandidate,
    decode_source_semantic_record, encode_source_semantic_record, parse_source_semantic_candidate,
)
from disclosure_anchor.application.services.provider_unit_builder import build_provider_units, build_source_provider_units
from tests._source_semantic_record_fixture import canonical, encoder_arguments, literal_record, sha


class SourceSemanticRecordTests(unittest.TestCase):
    def setUp(self):
        self.record = literal_record()
        self.raw = canonical(self.record)
        self.arguments = encoder_arguments(self.record)
        self.limit = len(self.raw)

    def changed(self, mutation):
        record = json.loads(self.raw)
        mutation(record)
        return canonical(record)

    def test_encoder_matches_hand_assembled_complete_canonical_preimage(self):
        encoded = encode_source_semantic_record(**self.arguments, maximum_bytes=self.limit)
        self.assertEqual(encoded, self.raw)
        self.assertNotIn("record_sha256", json.loads(encoded))
        self.assertEqual(self.record["provider_document"]["pages"][1]["blocks"], [])

    def test_strict_decoder_returns_exact_original_and_derived_values_with_byte_hash(self):
        value = decode_source_semantic_record(self.raw, maximum_bytes=self.limit)
        self.assertIs(type(value), DecodedSourceSemanticRecord)
        self.assertEqual(value.record_sha256, sha(self.raw))
        self.assertEqual(value.source_observation, self.arguments["source_observation"])
        self.assertEqual(value.target_identity, self.arguments["target_identity"])
        self.assertEqual(value.provider_document, self.arguments["provider_document"])
        self.assertEqual(value.native_observations, self.arguments["native_observations"])
        repair, = value.source_text_reconciliations
        finding, = value.source_quality_findings
        self.assertEqual((repair.source_index, repair.source_kind, repair.source_text),
                         (0, "source_pdf_native_numeric.v1", "金额5元。"))
        self.assertEqual((finding.source_index, finding.source_kind, finding.reason),
                         (1, "source_pdf_native_text_quality.v2", "cjk_bracket_omission"))
        self.assertEqual([b.payloads[0].text for b in value.semantics.provider_document.blocks], ["金额元。", "公告2026〕7号"])
        self.assertEqual([b.payloads[0].text for b in value.semantics.effective_provider_document.blocks], ["金额5元。", "公告2026〕7号"])
        with self.assertRaises(FrozenInstanceError):
            value.record_sha256 = sha("changed")

    def test_distinct_candidate_and_decoded_data_types_do_not_authorize_production(self):
        candidate = parse_source_semantic_candidate(self.raw, maximum_bytes=self.limit)
        decoded = decode_source_semantic_record(self.raw, maximum_bytes=self.limit)
        self.assertIs(type(candidate), UntrustedSourceSemanticCandidate)
        self.assertNotIsInstance(candidate, DecodedSourceSemanticRecord)
        self.assertFalse(hasattr(candidate, "semantics"))
        self.assertEqual(candidate.record_sha256, sha(self.raw))
        for value in (candidate, decoded):
            with self.subTest(type=type(value).__name__), self.assertRaises(TypeError):
                build_provider_units(value)
            with self.subTest(type=type(value).__name__), self.assertRaises(TypeError):
                build_source_provider_units(value, semantic_record_sha256=value.record_sha256,
                                            target_identity=value.target_identity)
        with self.assertRaises(FrozenInstanceError):
            candidate.record_sha256 = sha("changed")
        with self.assertRaises(TypeError):
            decode_source_semantic_record(self.raw, maximum_bytes=self.limit, verify=False)

    def test_candidate_preserves_cross_source_page_and_native_reference_differences(self):
        for name, mutation in (
            ("source_hash", lambda r: r["source_observation"].update(sha256=sha("other source"))),
            ("physical_pages", lambda r: r["source_observation"].update(page_count=3)),
            ("native_index", lambda r: r["native_observations"][1].update(source_index=99)),
            ("native_page", lambda r: r["native_observations"][1].update(page_index=1)),
            ("native_raw", lambda r: r["native_observations"][1].update(raw_block_sha256=sha("different raw"))),
        ):
            with self.subTest(case=name):
                raw = self.changed(mutation)
                candidate = parse_source_semantic_candidate(raw, maximum_bytes=len(raw))
                self.assertIs(type(candidate), UntrustedSourceSemanticCandidate)
                self.assertEqual(candidate.record_sha256, sha(raw))
                if name == "source_hash":
                    self.assertEqual(candidate.source_observation.sha256, sha("other source"))
                if name == "native_index":
                    self.assertEqual(candidate.native_observations[1].source_index, 99)
                with self.assertRaises(ValueError):
                    decode_source_semantic_record(raw, maximum_bytes=len(raw))

    def test_candidate_preserves_divergent_claims_while_strict_decode_rederives(self):
        for name, mutation in (
            ("missing_repair", lambda r: r.update(source_text_reconciliations=[])),
            ("missing_finding", lambda r: r.update(source_quality_findings=[])),
            ("repair_kind", lambda r: r["source_text_reconciliations"][0].update(source_kind="source_pdf_native_identifier.v1")),
            ("repair_preimage", lambda r: r["source_text_reconciliations"][0].update(provider_text_sha256=sha("other provider text"))),
            ("repair_text", lambda r: r["source_text_reconciliations"][0].update(source_text="金额6元。", source_text_sha256=sha("金额6元。"))),
            ("finding_kind", lambda r: r["source_quality_findings"][0].update(source_kind="source_pdf_native_text_quality.v1", reason="native_text_omission")),
        ):
            with self.subTest(case=name):
                raw = self.changed(mutation)
                candidate = parse_source_semantic_candidate(raw, maximum_bytes=len(raw))
                self.assertEqual(candidate.record_sha256, sha(raw))
                self.assertEqual(len(candidate.source_text_reconciliations), len(json.loads(raw)["source_text_reconciliations"]))
                self.assertEqual(len(candidate.source_quality_findings), len(json.loads(raw)["source_quality_findings"]))
                with self.assertRaises(ValueError):
                    decode_source_semantic_record(raw, maximum_bytes=len(raw))

    def test_both_parsers_reject_bad_versions_unknown_fields_and_malformed_provider_content(self):
        for name, mutation in (
            ("version", lambda r: r.update(contract_version="m6.source-semantic-record.v2")),
            ("policy", lambda r: r.update(source_semantics_version="provider_source_semantics.v2")),
            ("mode", lambda r: r.update(mode="e2e_publication")),
            ("owner", lambda r: r.update(processing_run_id="fabricated-owner")),
            ("self_hash", lambda r: r.update(record_sha256=sha("self"))),
            ("missing", lambda r: r.pop("native_observations")),
            ("raw_hash", lambda r: r["provider_document"]["pages"][0]["blocks"][0].update(raw_item_sha256=sha("wrong preimage"))),
            ("raw_duplicate", lambda r: r["provider_document"]["pages"][0]["blocks"][0].update(raw_item_json='{"x":1,"x":2}', raw_item_sha256=sha('{"x":1,"x":2}'))),
            ("profile", lambda r: r["target_identity"].update(language="en")),
            ("nested_extra", lambda r: r["native_observations"][0].update(verified=True)),
            ("bad_text_hash", lambda r: r["source_text_reconciliations"][0].update(source_text_sha256=sha("different text"))),
        ):
            raw = self.changed(mutation)
            for parser in (parse_source_semantic_candidate, decode_source_semantic_record):
                with self.subTest(case=name, parser=parser.__name__), self.assertRaises(ValueError):
                    parser(raw, maximum_bytes=len(raw))

    def test_both_parsers_require_exact_scalar_types_for_all_index_families(self):
        for section, field in (("native_observations", "source_index"), ("native_observations", "page_index"),
                               ("native_observations", "payload_ordinal"), ("source_text_reconciliations", "source_index"),
                               ("source_text_reconciliations", "payload_ordinal"), ("source_quality_findings", "source_index"),
                               ("source_quality_findings", "payload_ordinal")):
            for wrong in (True, "0", 0.0, -1):
                raw = self.changed(lambda r: r[section][0].update({field: wrong}))
                for parser in (parse_source_semantic_candidate, decode_source_semantic_record):
                    with self.subTest(section=section, field=field, wrong=wrong), self.assertRaises(ValueError):
                        parser(raw, maximum_bytes=len(raw))
        for path, field, wrong in (("source_observation", "byte_count", True),
                                   ("source_observation", "page_count", True),
                                   ("target_identity", "formula", 1),
                                   ("provider_document", "ocr_enabled", 0)):
            raw = self.changed(lambda r: r[path].update({field: wrong}))
            for parser in (parse_source_semantic_candidate, decode_source_semantic_record):
                with self.subTest(path=path, field=field), self.assertRaises(ValueError):
                    parser(raw, maximum_bytes=len(raw))
        for section, field in (("native_observations", "text"),
                               ("native_observations", "raw_block_sha256"),
                               ("source_text_reconciliations", "source_kind"),
                               ("source_text_reconciliations", "source_text"),
                               ("source_quality_findings", "reason"),
                               ("source_quality_findings", "source_text_sha256")):
            for wrong in (None, True, 0, ["coercible-looking"]):
                raw = self.changed(lambda r: r[section][0].update({field: wrong}))
                for parser in (parse_source_semantic_candidate, decode_source_semantic_record):
                    with self.subTest(section=section, field=field, wrong=wrong), self.assertRaises(ValueError):
                        parser(raw, maximum_bytes=len(raw))

    def test_encoder_rejects_cross_source_and_existing_dto_bool_index_loophole(self):
        class TextSubclass(str):
            pass

        changed_source = replace(self.arguments["source_observation"], sha256=sha("other source"))
        bool_index = replace(self.arguments["native_observations"][0], source_index=False)
        subclass_text = replace(self.arguments["native_observations"][0], text=TextSubclass("金额5元。"))
        for changes in ({"source_observation": changed_source},
                        {"native_observations": (bool_index, self.arguments["native_observations"][1])},
                        {"native_observations": (subclass_text, self.arguments["native_observations"][1])}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                encode_source_semantic_record(**{**self.arguments, **changes}, maximum_bytes=self.limit)

    def test_encoder_and_decoders_enforce_exact_byte_budget_and_canonical_wire(self):
        with self.assertRaises(ValueError):
            encode_source_semantic_record(**self.arguments, maximum_bytes=self.limit - 1)
        for parser in (parse_source_semantic_candidate, decode_source_semantic_record):
            with self.subTest(parser=parser.__name__), self.assertRaises(ValueError):
                parser(self.raw, maximum_bytes=self.limit - 1)
            for raw in (self.raw + b"\n", json.dumps(self.record, ensure_ascii=False).encode(),
                        self.raw.replace(b'"mode":"service_diagnostic"', b'"mode":"service_diagnostic","mode":"service_diagnostic"')):
                with self.subTest(parser=parser.__name__, size=len(raw)), self.assertRaises(ValueError):
                    parser(raw, maximum_bytes=len(raw))

    def test_strict_decoder_rejects_duplicate_and_reordered_native_observations(self):
        for mutation in (lambda r: r["native_observations"].reverse(),
                         lambda r: r["native_observations"].append(r["native_observations"][0])):
            raw = self.changed(mutation)
            with self.assertRaises(ValueError):
                decode_source_semantic_record(raw, maximum_bytes=len(raw))


if __name__ == "__main__":
    unittest.main()
