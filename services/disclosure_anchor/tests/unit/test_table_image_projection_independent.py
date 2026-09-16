"""Independent retained crop integrity and private provider-envelope checks."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest

from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelopeError, provider_document_envelope_to_bytes,
    provider_document_envelope_from_bytes,
)
from tests.unit import test_provider_document_envelope as envelope_fixture
from tests.unit.test_table_image_conservation_independent import CROP, URI, SHA


def model():
    return [[{'type': 'table', 'bbox': [0.1, 0.2, 0.7, 0.8], 'content': '<table>[Al]</table>',
              'table_image_unmatched': [{'kind': 'missing', 'token': '[A1]', 'expected': 1, 'actual': 0,
                'image_sha256': SHA, 'image_byte_count': len(CROP), 'image_data_uri': URI}]}]]


def extract(value, expected_total=None):
    from disclosure_anchor.adapters.parsers.mineru_medium.table_image_conservation import extract_table_image_unmatched
    return extract_table_image_unmatched(value, expected_total=expected_total)


class TableImageProjectionIndependentTests(unittest.TestCase):
    def test_verified_original_image_projects_without_duplicate_byte_payload(self):
        original = model()
        frozen = deepcopy(original)
        evidence, = extract(original, expected_total=1)
        self.assertEqual(original, frozen, 'read-only extraction preserves immutable evidence')
        self.assertEqual((evidence.page_index, evidence.model_block_index, evidence.token), (0, 0, '[A1]'))
        self.assertEqual((evidence.image_sha256, evidence.image_byte_count), (SHA, len(CROP)))
        self.assertFalse(hasattr(evidence, 'image_data_uri'), 'original bytes remain in retained model artifact')
        self.assertTrue(type(evidence).__module__.startswith('disclosure_anchor.application.contracts.'))

    def test_missing_evidence_or_wrong_hash_size_count_never_becomes_empty_success(self):
        mutations = [
            lambda m: m[0][0]['table_image_unmatched'][0].update(image_sha256='sha256:'+'0'*64),
            lambda m: m[0][0]['table_image_unmatched'][0].update(image_byte_count=len(CROP)+1),
            lambda m: m[0][0]['table_image_unmatched'][0].update(image_data_uri='data:image/jpeg;base64,%%%'),
            lambda m: m[0][0]['table_image_unmatched'][0].update(kind='invented'),
            lambda m: m[0][0]['table_image_unmatched'][0].pop('token'),
            lambda m: m[0][0].pop('table_image_unmatched'),
        ]
        for mutation in mutations:
            value = model()
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ProviderDocumentEnvelopeError):
                extract(value, expected_total=1)
        with self.assertRaises(ProviderDocumentEnvelopeError):
            extract(model(), expected_total=0)
        for malformed in (None, [], {}, 'missing'):
            value = model()
            value[0][0]['table_image_unmatched'] = malformed
            with self.subTest(present_malformed=malformed), self.assertRaises(ProviderDocumentEnvelopeError):
                extract(value)

    def test_legacy_absence_and_empty_page_have_no_new_finding(self):
        self.assertEqual(extract([[{'type': 'table', 'bbox': [0, 0, 1, 1], 'content': '<table>ok</table>'}]]), ())
        self.assertEqual(extract([[]], expected_total=0), ())

    def test_provider_envelope_roundtrip_preserves_image_identity_and_old_bytes(self):
        original = envelope_fixture._envelope()
        original_bytes = provider_document_envelope_to_bytes(original)
        self.assertEqual(provider_document_envelope_to_bytes(provider_document_envelope_from_bytes(original_bytes)), original_bytes)
        issue, = extract(model(), expected_total=1)
        changed = replace(original, provider_document=replace(original.provider_document, table_image_unmatched=(issue,)))
        raw = provider_document_envelope_to_bytes(changed)
        restored = provider_document_envelope_from_bytes(raw)
        self.assertEqual(restored, changed)
        self.assertEqual(restored.provider_document.table_image_unmatched, (issue,))
        self.assertNotIn(URI, raw.decode(), 'canonical projection does not duplicate image bytes')
        self.assertIn(SHA, raw.decode())
        self.assertNotEqual(raw, original_bytes)
        self.assertIsInstance(json.loads(raw), dict)

    def test_private_value_rejects_impossible_counts_locations_and_duplicate_identity(self):
        issue, = extract(model(), expected_total=1)
        for changes in ({'expected': 2}, {'actual': 1}, {'page_index': True},
                        {'model_block_index': -1}, {'image_byte_count': 0},
                        {'table_bbox': (0.0, 0.0, float('nan'), 1.0)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(issue, **changes)
        doc = envelope_fixture._envelope().provider_document
        for values in ((issue, issue), (replace(issue, page_index=len(doc.pages)),)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(doc, table_image_unmatched=values)

    def test_whole_source_review_survives_effective_view_and_zero_unit_output(self):
        from disclosure_anchor.application.contracts.provider_quality import (
            quality_occurrence_from_payload, quality_occurrence_to_payload,
        )
        from disclosure_anchor.application.contracts.provider_source_semantics import ProviderSourceSemantics
        from disclosure_anchor.application.contracts.m6_document_qualification import qualify_document
        from disclosure_anchor.application.services.provider_quality import assess_source_build_quality
        from disclosure_anchor.application.services.provider_source_semantics import derive_source_semantics
        from tests._provider_source_semantics_fixture import block, document, observation
        from tests.unit.test_provider_quality import source_build
        from tests import m6_support as m6

        issue, = extract(model(), expected_total=1)
        item = block(0, 0, 0, '净利润万元。')
        doc = replace(document(((item,),)), table_image_unmatched=(issue,))
        semantics = derive_source_semantics(document=doc, observations=(observation(item, '净利润25万元。'),))
        self.assertTrue(semantics.source_text_reconciliations, 'exercise the real effective-view reconstruction')
        self.assertEqual(semantics.effective_provider_document.table_image_unmatched, (issue,))
        empty = ProviderSourceSemantics(replace(document(((),)), table_image_unmatched=(issue,)))
        source = m6.entry('missing-crop', pages=1, mode='e2e_publication')
        planned = m6.quality_plan('e2e_publication', ('table_image_unmatched', 'review_required'))
        for case in (semantics, empty):
            build = source_build(case)
            occurrences = assess_source_build_quality(case, build)
            with self.subTest(unit_count=len(build.units)):
                self.assertEqual(len(occurrences), 1)
                occurrence, = occurrences
                self.assertEqual(occurrence.reason_id, 'table_image_unmatched')
                self.assertIsNone(occurrence.unit_index, 'retain whole-document evidence without inventing Unit ownership')
                self.assertEqual(quality_occurrence_from_payload(quality_occurrence_to_payload(occurrence)), occurrence)
                reasons = tuple(sorted({v.reason_id for v in occurrences}))
                evidence = m6.qualification_for(source, 'e2e_publication', 'attempt-crop',
                                                unit_count=len(build.units), review_reasons=reasons)
                for plan in (planned, m6.quality_plan('e2e_publication')):
                    verdict = qualify_document(evidence, plan)
                    self.assertEqual(verdict.verdict, 'review_pending')
                    self.assertIsNone(verdict.scorable_page_count)
        clean = ProviderSourceSemantics(document(((),)))
        self.assertEqual(assess_source_build_quality(clean, source_build(clean)), ())


if __name__ == '__main__':
    unittest.main()
