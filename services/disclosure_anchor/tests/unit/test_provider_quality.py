"""Literal reason evidence, unchanged predicate scope, and complete-build assessment."""

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.document_outline import HeadingSourceFragment
from disclosure_anchor.application.contracts.provider_quality import (
    EncodedTextOccurrence, SourceFindingOccurrence, TruncatedTitleOccurrence, UnboundTableOccurrence,
    ordered_quality_occurrences, quality_occurrence_from_payload, quality_occurrence_to_payload,
)
from disclosure_anchor.application.contracts.provider_table_projection import ProviderTablePartRef, UnboundProviderTablePart
from disclosure_anchor.application.contracts.provider_unit import ProviderUnitLocator, ProviderUnitSourceQualityFinding
from disclosure_anchor.application.contracts.provider_source_semantics import ProviderSourceSemantics
from disclosure_anchor.application.services.provider_quality import assess_provider_unit_quality, assess_source_build_quality
from disclosure_anchor.application.services.provider_source_semantics import derive_source_semantics
from disclosure_anchor.application.services.provider_unit_builder import build_source_provider_units
from tests._provider_source_semantics_fixture import block, document, observation, segment, target
from tests._source_semantic_record_fixture import literal_record, sha


ENCODED = "0123456789!@#$%^&*()_+-=[]{}"


def locator(**changes):
    return ProviderUnitLocator(**{
        "provider_document_sha256": sha("quality structural record"), "unit_index": 0,
        "heading_chain": (), "parts": (), "evidence_only_block_source_indices": (),
        "unbound_table_parts": (), "evidence_artifacts": (), "search_targets": (), **changes,
    })


def source_build(semantics):
    return build_source_provider_units(semantics, semantic_record_sha256=sha("quality structural record"),
                                       target_identity=target())


def literal_occurrences():
    finding = dict(literal_record()["source_quality_findings"][0])
    fragments = [
        {"source_index": 2, "payload_ordinal": 0, "page_index": 0, "text": "<sup>", "raw_block_sha256": sha("fragment 2")},
        {"source_index": 3, "payload_ordinal": 0, "page_index": 1, "text": "®</sup>", "raw_block_sha256": sha("fragment 3")},
    ]
    return (
        (SourceFindingOccurrence(0, ProviderUnitSourceQualityFinding(**finding)),
         {"kind": "source_finding", "reason_id": "source_finding:source_pdf_native_text_quality.v2:cjk_bracket_omission",
          "unit_index": 0, "finding": finding}),
        (UnboundTableOccurrence(None, UnboundProviderTablePart(ProviderTablePartRef(None, 2), "page_table_count_mismatch"),
                                None, None, 1, sha("segment 2")),
         {"kind": "table_unbound", "reason_id": "table_unbound:page_table_count_mismatch", "unit_index": None,
          "part": {"part": {"block_source_index": None, "physical_segment_index": 2}, "reason": "page_table_count_mismatch"},
          "block_page_index": None, "raw_block_sha256": None, "segment_page_index": 1, "raw_segment_sha256": sha("segment 2")}),
        (EncodedTextOccurrence(2, 10, sha("encoded block")),
         {"kind": "encoded_text", "reason_id": "suspected_encoded_text", "unit_index": 2, "source_index": 10,
          "raw_block_sha256": sha("encoded block")}),
        (TruncatedTitleOccurrence(3, "heading:00000002", tuple(HeadingSourceFragment(**f) for f in fragments)),
         {"kind": "truncated_title", "reason_id": "suspected_truncated_markup_title", "unit_index": 3,
          "heading_id": "heading:00000002", "source_fragments": fragments}),
    )


class ProviderQualityTests(unittest.TestCase):
    def test_all_four_occurrence_payloads_are_literal_closed_direct_roundtrips(self):
        for occurrence, expected in literal_occurrences():
            with self.subTest(kind=expected["kind"]):
                self.assertEqual(quality_occurrence_to_payload(occurrence), expected)
                self.assertEqual(quality_occurrence_from_payload(expected), occurrence)
                self.assertEqual(quality_occurrence_from_payload(quality_occurrence_to_payload(occurrence)), occurrence)

    def test_occurrence_wire_rejects_reason_drift_extra_fields_and_scalar_coercion(self):
        for _, value in literal_occurrences():
            for change in ({"reason_id": "invented_reason"}, {"extra": True}, {"unit_index": True}):
                with self.subTest(kind=value["kind"], change=change), self.assertRaises(ValueError):
                    quality_occurrence_from_payload({**value, **change})
        title = literal_occurrences()[3][1]
        for fragments in ([], [{**title["source_fragments"][0], "page_index": True}]):
            with self.subTest(fragments=fragments), self.assertRaises(ValueError):
                quality_occurrence_from_payload({**title, "source_fragments": fragments})

    def test_table_wire_requires_null_unit_only_for_complete_segment_only_identity(self):
        value = literal_occurrences()[1][1]
        for change in ({"unit_index": 0}, {"block_page_index": 0}, {"raw_block_sha256": sha("invented block")},
                       {"segment_page_index": None}, {"raw_segment_sha256": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                quality_occurrence_from_payload({**value, **change})

    def test_order_is_numeric_null_first_and_deduplicates_only_identical_occurrences(self):
        finding, unassigned, encoded, title = (item for item, _ in literal_occurrences())
        e2 = EncodedTextOccurrence(2, 2, sha("same index raw A"))
        e10 = EncodedTextOccurrence(2, 10, sha("same index raw A"))
        e_other = EncodedTextOccurrence(2, 2, sha("same index raw B"))
        u10 = EncodedTextOccurrence(10, 1, sha("later Unit"))
        attached = UnboundTableOccurrence(0, UnboundProviderTablePart(ProviderTablePartRef(0, None), "page_table_count_mismatch"),
                                          0, sha("attached block"), None, None)
        ordered = ordered_quality_occurrences((attached, u10, e10, e2, e2, unassigned, title, finding, e_other))
        self.assertEqual(ordered[0], finding)
        encoded_values = [v for v in ordered if isinstance(v, EncodedTextOccurrence)]
        self.assertEqual([(v.unit_index, v.source_index) for v in encoded_values], [(2, 2), (2, 2), (2, 10), (10, 1)])
        self.assertEqual(len([v for v in encoded_values if v == e2]), 1)
        self.assertIn(e_other, encoded_values)
        self.assertEqual([v.unit_index for v in ordered if isinstance(v, UnboundTableOccurrence)], [None, 0])
        self.assertEqual(len(ordered), 8)
        self.assertEqual(encoded.reason_id, "suspected_encoded_text")

    def test_encoded_text_predicate_and_unit_local_scope_preserve_existing_thresholds(self):
        self.assertEqual(len(ENCODED), 28)
        doc = document(((block(0, 0, 0, ENCODED), block(1, 0, 1, "正常正文")),))
        assessment = assess_provider_unit_quality(document=doc, unit_sources=frozenset({0}), heading=None, locator=locator())
        self.assertEqual(assessment.quality_status, "needs_review")
        self.assertEqual(assessment.occurrences, (EncodedTextOccurrence(0, 0, doc.blocks[0].raw_item_sha256),))
        self.assertFalse(hasattr(assessment.occurrences[0], "payload_ordinal"))
        unaffected = assess_provider_unit_quality(document=doc, unit_sources=frozenset({1}), heading=None, locator=locator())
        self.assertEqual((unaffected.quality_status, unaffected.occurrences), ("ok", ()))
        for text in (ENCODED[:23], ENCODED + "中文", "!" * 40):
            adjacent = document(((block(0, 0, 0, text),),))
            result = assess_provider_unit_quality(document=adjacent, unit_sources=frozenset({0}), heading=None, locator=locator())
            with self.subTest(text=text):
                self.assertEqual((result.quality_status, result.occurrences), ("ok", ()))

    def test_multiple_findings_and_encoded_occurrences_do_not_inflate_unit_counts(self):
        items = (block(0, 0, 0, "支付元。"), block(1, 0, 1, ENCODED))
        semantics = derive_source_semantics(document=document((items,)), observations=(observation(items[0], "支付利息20元。"),))
        build = source_build(semantics)
        self.assertEqual(len(build.units), 1)
        self.assertEqual([u.quality_status for u in build.units], ["needs_review"])
        occurrences = assess_source_build_quality(semantics, build)
        self.assertEqual([o.reason_id for o in occurrences],
                         ["source_finding:source_pdf_native_text_quality.v1:native_text_omission", "suspected_encoded_text"])
        self.assertEqual([o.unit_index for o in occurrences], [0, 0])
        self.assertEqual(len(build.units), 1)

    def test_each_existing_table_reason_is_projected_without_new_reason_or_cell_repair(self):
        html = "<table><tr><td>17</td></tr></table>"
        doc = document(((block(0, 0, 0, html, kind="table"),),), (segment(0, 0, html, "unbound"),))
        for reason in ("page_table_count_mismatch", "retained_without_payload", "deleted_with_payload",
                       "provider_status_unbound", "continuation_without_owner", "continuation_not_next_page",
                       "continuation_not_page_boundary"):
            part = UnboundProviderTablePart(ProviderTablePartRef(0, 0), reason)
            result = assess_provider_unit_quality(document=doc, unit_sources=frozenset({0}), heading=None,
                                                   locator=locator(unbound_table_parts=(part,)))
            with self.subTest(reason=reason):
                self.assertEqual(result.quality_status, "needs_review")
                occurrence, = result.occurrences
                self.assertEqual(occurrence.reason_id, "table_unbound:" + reason)
                self.assertEqual((occurrence.block_page_index, occurrence.segment_page_index), (0, 0))
                self.assertEqual(occurrence.raw_block_sha256, doc.blocks[0].raw_item_sha256)
                self.assertEqual(occurrence.raw_segment_sha256, doc.physical_table_segments[0].raw_segment_sha256)
                self.assertEqual(doc.blocks[0].payloads[0].text, html)

    def test_unassigned_segment_retains_reason_with_zero_units_and_empty_build_has_none(self):
        part = segment(1, 0, "<table><tr><td>17</td></tr></table>", "unbound")
        semantics = ProviderSourceSemantics(document(((), ()), (part,)))
        build = source_build(semantics)
        self.assertEqual(build.units, ())
        occurrence, = assess_source_build_quality(semantics, build)
        self.assertIsNone(occurrence.unit_index)
        self.assertEqual(occurrence.reason_id, "table_unbound:page_table_count_mismatch")
        self.assertEqual((occurrence.block_page_index, occurrence.raw_block_sha256), (None, None))
        self.assertEqual((occurrence.segment_page_index, occurrence.raw_segment_sha256), (1, part.raw_segment_sha256))
        empty = ProviderSourceSemantics(document(((), ())))
        self.assertEqual(source_build(empty).units, ())
        self.assertEqual(assess_source_build_quality(empty, source_build(empty)), ())

    def test_repeated_truncated_title_flags_each_leaf_occurrence_and_preserves_fragments(self):
        items = (block(0, 0, 0, "<sup>®</sup>A", heading=True), block(1, 0, 1, "说明甲"),
                 block(2, 1, 0, "<sup>®</sup>A", heading=True), block(3, 1, 1, "说明乙"))
        semantics = ProviderSourceSemantics(document((items[:2], items[2:])))
        build = source_build(semantics)
        occurrences = assess_source_build_quality(semantics, build)
        titles = [o for o in occurrences if isinstance(o, TruncatedTitleOccurrence)]
        self.assertEqual([o.unit_index for o in titles], [0, 1])
        self.assertEqual([o.heading_id for o in titles], ["heading:00000000", "heading:00000002"])
        self.assertEqual([o.source_fragments[0].source_index for o in titles], [0, 2])
        self.assertEqual([o.source_fragments[0].raw_block_sha256 for o in titles], [items[0].raw_item_sha256, items[2].raw_item_sha256])
        glyph = ProviderSourceSemantics(document(((block(0, 0, 0, "<sup>®</sup>", heading=True),),)))
        self.assertEqual(assess_source_build_quality(glyph, source_build(glyph)), ())
        adjacent = ProviderSourceSemantics(document(((block(0, 0, 0, "名称<sup>®</sup>", heading=True),),)))
        self.assertEqual(assess_source_build_quality(adjacent, source_build(adjacent)), ())

    def test_failed_incomplete_or_unknown_status_build_is_not_converted_to_zero_quality(self):
        semantics = ProviderSourceSemantics(document(((block(0, 0, 0, "正文"),),)))
        build = source_build(semantics)
        for changed in (replace(build, units=()),
                        replace(build, units=(replace(build.units[0], quality_status="future_unknown"),)),
                        replace(build, units=(replace(build.units[0], quality_status="needs_review"),))):
            with self.subTest(build=changed), self.assertRaises(ValueError):
                assess_source_build_quality(semantics, changed)


if __name__ == "__main__":
    unittest.main()
