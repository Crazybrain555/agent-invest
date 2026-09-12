"""Shared provider-content codec and validation boundary, without admission claims."""

from dataclasses import replace
import json
import unittest

from disclosure_anchor.application.contracts import _provider_content as content
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelopeError, provider_document_envelope_from_bytes,
    provider_document_envelope_to_bytes,
)
from tests._provider_source_semantics_fixture import cases, envelope, sha, target
from tests.unit.test_provider_source_compatibility import snapshot


class ProviderSourceContentTests(unittest.TestCase):
    def test_shared_codec_matches_old_literal_payloads_and_error_class_identity(self):
        self.assertIs(content.ProviderDocumentEnvelopeError, ProviderDocumentEnvelopeError)
        for name, (doc, _) in cases().items():
            with self.subTest(case=name):
                expected = snapshot()["cases"][name]["envelope"]["provider_document"]
                self.assertEqual(content.provider_document_to_payload(doc), expected)
                self.assertEqual(content.provider_document_from_payload(expected), doc)
                self.assertIsNone(content.validate_provider_content(doc, target()))

    def test_closed_codec_rejects_extra_missing_wrong_type_and_nonfinite_fields(self):
        doc, _ = cases()["plain"]
        for name, mutate in (
            ("extra", lambda p: p.update(document_id="fabricated-production-owner")),
            ("missing", lambda p: p.pop("bundle_sha256")),
            ("bool_page", lambda p: p["pages"][0].update(page_index=True)),
            ("bool_size", lambda p: p["artifacts"][0].update(size_bytes=True)),
            ("bool_bbox", lambda p: p["pages"][0]["blocks"][0]["bbox"].__setitem__(0, True)),
            ("nonfinite", lambda p: p["pages"][0]["page_size"].__setitem__(0, float("inf"))),
            ("array_kind", lambda p: p.update(pages=tuple(p["pages"]))),
            ("source_hash", lambda p: p.update(source_pdf_sha256="sha256:" + "F" * 64)),
        ):
            with self.subTest(case=name):
                payload = content.provider_document_to_payload(doc)
                mutate(payload)
                with self.assertRaises(ValueError):
                    content.provider_document_from_payload(payload)

    def test_raw_json_preimages_are_canonical_unique_and_hash_bound(self):
        doc, _ = cases()["plain"]
        item = doc.blocks[0]
        for raw, digest in (
            ('{"x":1,"x":2}', None), ('{"x":NaN}', None),
            ('{"x": 1}', None), (item.raw_item_json, sha("wrong block preimage")),
        ):
            with self.subTest(raw=raw):
                bad = replace(item, raw_item_json=raw, raw_item_sha256=digest or sha(raw))
                changed = replace(doc, pages=(replace(doc.pages[0], blocks=(bad,)),))
                with self.assertRaises(ProviderDocumentEnvelopeError):
                    content.validate_provider_content(changed, target())
        table_doc, _ = cases()["continued_table"]
        for raw, digest in (('{"x": 1}', None),
                            (table_doc.physical_table_segments[0].raw_segment_json, sha("wrong segment"))):
            bad = replace(table_doc.physical_table_segments[0], raw_segment_json=raw, raw_segment_sha256=digest or sha(raw))
            changed = replace(table_doc, physical_table_segments=(bad, table_doc.physical_table_segments[1]))
            with self.subTest(segment=raw), self.assertRaises(ProviderDocumentEnvelopeError):
                content.validate_provider_content(changed, target())

    def test_visual_evidence_requires_supported_image_media(self):
        doc, _ = cases()["visual_only"]
        artifacts = tuple(replace(a, media_type="application/pdf") if a.role == "figure" else a for a in doc.artifacts)
        with self.assertRaisesRegex(ProviderDocumentEnvelopeError, "verified image"):
            content.validate_provider_content(replace(doc, artifacts=artifacts), target())

    def test_envelope_bytes_remain_canonical_and_duplicate_keys_cannot_be_decoded(self):
        doc, _ = cases()["plain"]
        encoded = provider_document_envelope_to_bytes(envelope(doc))
        for value in (encoded + b"\n", json.dumps(json.loads(encoded), ensure_ascii=False).encode(),
                      b'{"document_id":"a","document_id":"b"}', b'{"x":NaN}'):
            with self.subTest(value=value[:80]), self.assertRaises(ProviderDocumentEnvelopeError):
                provider_document_envelope_from_bytes(value)


if __name__ == "__main__":
    unittest.main()
