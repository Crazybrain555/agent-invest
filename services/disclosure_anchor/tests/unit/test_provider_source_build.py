"""Source-only build/replay authority and source conservation boundaries."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from disclosure_anchor.application.contracts.document_outline import HeadingLevelHint, HeadingNegativeHint
from disclosure_anchor.application.contracts.provider_document_admission import AdmittedProviderDocument
from disclosure_anchor.application.contracts.provider_document import provider_artifact_bundle_sha256
from disclosure_anchor.application.contracts.provider_document_envelope import ProviderDocumentEnvelopeError
from disclosure_anchor.application.contracts.provider_source_semantics import ProviderSourceSemantics
from disclosure_anchor.application.services.provider_source_semantics import derive_source_semantics
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units, build_source_provider_units,
    replay_provider_unit_search_binding, replay_provider_unit_search_binding_source_text,
    replay_source_provider_unit_search_binding, replay_source_provider_unit_search_binding_source_text,
)
from tests._provider_source_semantics_fixture import admit, block, cases, document, sha, target


REFERENCE = sha("H1 structural test reference; no runtime evidence authority")


class ProviderSourceBuildTests(unittest.TestCase):
    def setUp(self):
        self.doc, observations = cases()["plain"]
        self.semantics = derive_source_semantics(document=self.doc, observations=observations)
        self.admitted = admit(self.doc, observations)
        self.result = self.build(self.semantics)
        self.draft, = self.result.units
        self.binding, = self.draft.locator.search_targets

    def build(self, semantics, **changes):
        return build_source_provider_units(semantics, **{
            "semantic_record_sha256": REFERENCE, "target_identity": target(), **changes,
        })

    def source_calls(self, semantics, **changes):
        kwargs = {"semantic_record_sha256": REFERENCE, "target_identity": target(), **changes}
        return (
            lambda: build_source_provider_units(semantics, **kwargs),
            lambda: replay_source_provider_unit_search_binding(semantics, self.draft, self.binding, **kwargs),
            lambda: replay_source_provider_unit_search_binding_source_text(semantics, self.draft, self.binding, **kwargs),
        )

    def test_production_interfaces_reject_source_values_ducks_and_subclasses(self):
        class AdmittedSubclass(AdmittedProviderDocument):
            pass

        subclass = AdmittedSubclass(self.admitted.provider_document_relpath,
                                     self.admitted.provider_document_sha256, self.admitted.envelope)
        duck = SimpleNamespace(effective_provider_document=self.doc,
                               provider_document_sha256=self.admitted.provider_document_sha256,
                               source_text_reconciliations=(), source_quality_findings=())
        for unadmitted in (self.semantics, self.doc, self.admitted.envelope, duck, subclass):
            for function, extra in ((build_provider_units, ()),
                                    (replay_provider_unit_search_binding, (self.draft, self.binding)),
                                    (replay_provider_unit_search_binding_source_text, (self.draft, self.binding))):
                with self.subTest(type=type(unadmitted).__name__, function=function.__name__), self.assertRaises(TypeError):
                    function(unadmitted, *extra)

    def test_source_interfaces_require_exact_semantics_and_target_types(self):
        class SourceSubclass(ProviderSourceSemantics):
            pass

        class TargetSubclass(type(target())):
            pass

        wrong_semantics = (self.doc, self.admitted, SourceSubclass(self.doc),
                           SimpleNamespace(provider_document=self.doc, effective_provider_document=self.doc,
                                           source_text_reconciliations=(), source_quality_findings=()))
        for value in wrong_semantics:
            for call in self.source_calls(value):
                with self.subTest(type=type(value).__name__), self.assertRaises(TypeError):
                    call()
        wrong_targets = (None, target().to_payload(), TargetSubclass(**target().to_payload()))
        for value in wrong_targets:
            for call in self.source_calls(self.semantics, target_identity=value):
                with self.subTest(target=value), self.assertRaises(TypeError):
                    call()

    def test_source_reference_is_canonical_and_bound_by_both_replay_helpers(self):
        for digest in (None, True, 1, "", "sha256:" + "A" * 64, "0" * 64, REFERENCE + "\n"):
            for call in self.source_calls(self.semantics, semantic_record_sha256=digest):
                with self.subTest(digest=digest), self.assertRaises(ValueError):
                    call()
        other = sha("another structural reference")
        rebuilt = self.build(self.semantics, semantic_record_sha256=other)
        self.assertEqual(rebuilt.provider_document_sha256, other)
        self.assertEqual(rebuilt.units[0].locator.provider_document_sha256, other)
        self.assertEqual(rebuilt.units[0].content_hash, self.draft.content_hash)
        self.assertEqual(rebuilt.units[0].query_projection_hash, self.draft.query_projection_hash)
        self.assertEqual(rebuilt.units[0].structure_hash, self.draft.structure_hash)
        for call in self.source_calls(self.semantics, semantic_record_sha256=other)[1:]:
            with self.assertRaisesRegex(ValueError, "different document"):
                call()

    def test_source_and_admitted_builds_share_complete_outputs_and_replay(self):
        for name, (doc, observations) in cases().items():
            with self.subTest(case=name):
                admitted = admit(doc, observations)
                semantics = derive_source_semantics(document=doc, observations=observations)
                kwargs = {"semantic_record_sha256": admitted.provider_document_sha256, "target_identity": target()}
                source_result = build_source_provider_units(semantics, **kwargs)
                production_result = build_provider_units(admitted)
                self.assertEqual(source_result, production_result)
                for draft in source_result.units:
                    for binding in draft.locator.search_targets:
                        self.assertEqual(replay_source_provider_unit_search_binding(semantics, draft, binding, **kwargs),
                                         replay_provider_unit_search_binding(admitted, draft, binding))
                        self.assertEqual(replay_source_provider_unit_search_binding_source_text(semantics, draft, binding, **kwargs),
                                         replay_provider_unit_search_binding_source_text(admitted, draft, binding))

    def test_hints_are_forwarded_as_source_bound_values(self):
        doc, _ = cases()["multipage"]
        admitted = admit(doc)
        semantics = ProviderSourceSemantics(doc)
        first, _, repeated, _, _ = doc.blocks
        level = HeadingLevelHint(doc.source_pdf_sha256, first.source_index, first.raw_item_sha256, "bookmark", 2)
        negative = HeadingNegativeHint(doc.source_pdf_sha256, repeated.source_index, repeated.raw_item_sha256, "page_continuation")
        for level_hints, negative_hints in (((level,), ()), ((), (negative,)), ((level,), (negative,))):
            with self.subTest(level_hints=level_hints, negative_hints=negative_hints):
                expected = build_provider_units(admitted, level_hints=level_hints, negative_hints=negative_hints)
                actual = self.build(semantics, semantic_record_sha256=admitted.provider_document_sha256,
                                    level_hints=iter(level_hints), negative_hints=iter(negative_hints))
                self.assertEqual(actual, expected)
        with self.assertRaises(ValueError):
            self.build(semantics, level_hints=(replace(level, source_pdf_sha256=sha("other source")),))

    def test_all_source_interfaces_validate_original_raw_profile_and_media(self):
        item = self.doc.blocks[0]
        bad_block = replace(item, raw_item_sha256=sha("different preimage"))
        bad_page = replace(self.doc.pages[0], blocks=(bad_block,))
        artifacts = tuple(replace(a, media_type="text/plain") if a.role == "content_list" else a
                          for a in self.doc.artifacts)
        missing = tuple(a for a in self.doc.artifacts if a.role != "model_json")
        bad_documents = (
            replace(self.doc, pages=(bad_page,)), replace(self.doc, parser_version="3.4.5"),
            replace(self.doc, artifacts=artifacts),
            replace(self.doc, artifacts=missing, bundle_sha256=provider_artifact_bundle_sha256(missing)),
        )
        for doc in bad_documents:
            for call in self.source_calls(ProviderSourceSemantics(doc)):
                with self.subTest(document=doc), self.assertRaises(ProviderDocumentEnvelopeError):
                    call()
        for call in self.source_calls(self.semantics, target_identity=replace(target(), language="en")):
            with self.assertRaises(ProviderDocumentEnvelopeError):
                call()
        # Repaired text legitimately differs from original raw JSON. Validation
        # must concern the original record, while replay uses effective text.
        doc, observations = cases()["repair_and_finding"]
        result = self.build(derive_source_semantics(document=doc, observations=observations))
        self.assertEqual(result.units[0].payload["parts"][0]["text"], "营业收入12万元，同比增长3%。")

    def test_both_source_replays_reject_wrong_payload_and_foreign_binding(self):
        damaged = replace(self.draft, payload={"text": "另一段正文。"})
        foreign_doc = document(((block(0, 0, 0, "其他源文本"),),))
        foreign = self.build(ProviderSourceSemantics(foreign_doc)).units[0].locator.search_targets[0]
        for replay in (replay_source_provider_unit_search_binding, replay_source_provider_unit_search_binding_source_text):
            for draft, binding in ((damaged, self.binding), (self.draft, foreign)):
                with self.subTest(replay=replay.__name__), self.assertRaises(ValueError):
                    replay(self.semantics, draft, binding, semantic_record_sha256=REFERENCE, target_identity=target())

    def test_blank_pages_repeated_heading_and_heading_only_occurrences_are_conserved(self):
        doc, observations = cases()["multipage"]
        result = self.build(derive_source_semantics(document=doc, observations=observations))
        self.assertEqual([page.page_index for page in doc.pages], [0, 1, 2, 3])
        self.assertEqual(doc.pages[1].blocks, ())
        self.assertEqual([u.title for u in result.units], ["业务情况", "业务情况", "其他事项"])
        self.assertEqual([u.page_no for u in result.units], [1, 3, 4])
        self.assertEqual([u.locator.heading_chain[-1].source_index for u in result.units], [0, 2, 4])
        self.assertEqual([i for u in result.units for part in u.locator.parts for i in part.block_source_indices], [1, 3])
        self.assertEqual(result.units[2].payload, {"text": ""})
        self.assertEqual([b.source.source_index for u in result.units for b in u.locator.search_targets], [0, 1, 2, 3, 4])
        unassigned, = result.unassigned_table_parts
        self.assertEqual((unassigned.part.block_source_index, unassigned.part.physical_segment_index), (None, 0))
        self.assertEqual(unassigned.reason, "page_table_count_mismatch")

    def test_continued_table_replays_owner_once_and_preserves_both_physical_parts(self):
        doc, _ = cases()["continued_table"]
        semantics = ProviderSourceSemantics(doc)
        draft, = self.build(semantics).units
        part, = draft.locator.parts
        self.assertEqual(part.block_source_indices, (0, 1))
        self.assertEqual(part.physical_table_segment_indices, (0, 1))
        binding, = draft.locator.search_targets
        self.assertEqual(replay_source_provider_unit_search_binding(semantics, draft, binding,
                         semantic_record_sha256=REFERENCE, target_identity=target()), ("项目", "金额", "甲", "17"))
        self.assertEqual(replay_source_provider_unit_search_binding_source_text(semantics, draft, binding,
                         semantic_record_sha256=REFERENCE, target_identity=target()), doc.blocks[0].payloads[0].text)

    def test_empty_and_visual_only_builds_do_not_require_text_or_positive_unit_counts(self):
        empty, _ = cases()["empty"]
        result = self.build(ProviderSourceSemantics(empty))
        self.assertEqual(result.units, ())
        self.assertEqual(result.unassigned_table_parts, ())
        visual, _ = cases()["visual_only"]
        draft, = self.build(ProviderSourceSemantics(visual)).units
        self.assertEqual(draft.payload_kind, "mixed")
        self.assertEqual(draft.quality_status, "ok")
        self.assertEqual(draft.locator.search_targets, ())
        self.assertEqual(draft.locator.parts[0].block_source_indices, (0,))
        evidence, = draft.locator.evidence_artifacts
        self.assertEqual(evidence.sha256, next(a.sha256 for a in visual.artifacts if a.role == "figure"))
        self.assertEqual(draft.payload["parts"][0]["content_artifacts"][0]["sha256"], evidence.sha256)


if __name__ == "__main__":
    unittest.main()
