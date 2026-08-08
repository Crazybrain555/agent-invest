"""Materialization of canonical-stream native runs and visual page binding.

Placement itself is decided by the canonical occurrence stream and is proved in
``tests/unit/test_canonical_occurrence.py``; this module only asserts what the
draft materializer adds on top of an already-decided position.
"""

from __future__ import annotations

import unittest

from disclosure_anchor.application.contracts.canonical_occurrence import (
    canonical_occurrence_stream,
)
from disclosure_anchor.application.contracts.source_evidence import (
    SourceEvidenceProof,
    SourcePageProof,
    SourceProofIdentity,
    VerifiedVisualArtifact,
    VisualArtifactProof,
    VisualBindingProof,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    empty_projection_graph,
    search_text_values,
)
from disclosure_anchor.application.services.unit_builder.builder import (
    UnitDraft,
)
from disclosure_anchor.application.services.unit_builder.source_native_fallback import (
    bind_visual_page_evidence,
    native_stream_unit_drafts,
)
from tests.unit.test_canonical_occurrence import (
    Element,
    MappedAtom,
    NativeAtom,
    build_case,
)


class SourceVisualBindingTests(unittest.TestCase):
    def test_visual_occurrence_precedes_provider_bytes_on_unit_and_part(self) -> None:
        occurrence = {
            "artifact_role": "source_visual_occurrence_000007",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 321,
            "pixel_width": 100,
            "pixel_height": 200,
            "media_type": "image/png",
        }
        provider = {
            "artifact_role": "evidence_image_000007",
            "sha256": "sha256:" + "d" * 64,
            "size_bytes": 123,
            "media_type": "image/png",
        }
        locator = {
            "evidence_artifacts": [provider],
            "source_projection": {
                "version": "unit-source-projection.v4",
                "payload": {
                    "kind": "image_identity",
                    "sources": [
                        {
                            "source": {
                                "kind": "normalized_ir_element",
                                "ir_id": "ir_0007",
                                "source_item_index": 7,
                                "order_index": 7,
                                "page_no": 2,
                            },
                            "field": {"kind": "image"},
                        }
                    ],
                    "target_field": "payload.image_ref",
                    "transform": "sha256_bytes.v1",
                },
                "heading_path": [],
                "structured": [],
                "provenance": [],
                "search_targets": [],
                "search_atoms": [],
                "physical_context": None,
            },
        }
        proof = _visual_proof(
            VisualBindingProof(
                source_item_index=7,
                page_idx=1,
                kind="occurrence_crop",
                artifact=_visual_artifact(occurrence),
            )
        )
        part = {
            "kind": "image",
            "order": 7,
            "image_ref": "images/" + "d" * 64 + ".png",
            "caption": "",
            "visual_kind": "image",
            "artifact_locator": locator,
        }
        draft = UnitDraft(
            payload_kind="mixed",
            payload={"parts": [part], "semantic_type": "section"},
            source_order=7,
            artifact_locator=locator,
            quality_status="needs_review",
        )

        (bound,) = bind_visual_page_evidence([draft], proof)

        self.assertEqual(
            [
                item["artifact_role"]
                for item in bound.artifact_locator["evidence_artifacts"]
            ],
            [
                "source_visual_occurrence_000007",
                "evidence_image_000007",
            ],
        )
        bound_part = bound.payload["parts"][0]
        self.assertEqual(
            [
                item["artifact_role"]
                for item in bound_part["artifact_locator"]["evidence_artifacts"]
            ],
            [
                "source_visual_occurrence_000007",
                "evidence_image_000007",
            ],
        )

    def test_visual_page_is_bound_to_its_searchable_carrier(self) -> None:
        visual = {
            "artifact_role": "source_page_visual_000002",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 321,
            "pixel_width": 100,
            "pixel_height": 200,
            "media_type": "image/png",
        }
        proof = _visual_proof(
            VisualBindingProof(
                source_item_index=7,
                page_idx=1,
                kind="carrier_guard",
                artifact=_visual_artifact(visual),
            )
        )
        draft = UnitDraft(
            payload_kind="text",
            payload={"text": "OCR正文"},
            source_order=8,
            artifact_locator={
                "source_projection": {
                    "version": "unit-source-projection.v4",
                    "payload": {
                        "kind": "text_identity",
                        "sources": [
                            {
                                "source": {
                                    "kind": "normalized_ir_element",
                                    "ir_id": "ir_0007",
                                    "source_item_index": 7,
                                    "order_index": 8,
                                    "page_no": 2,
                                },
                                "field": {"kind": "text"},
                            }
                        ],
                        "target_field": "payload.text",
                        "transform": "clean_text.v1",
                    },
                    "heading_path": [],
                    "structured": [],
                    "provenance": [],
                    "search_targets": ["payload.text"],
                    "search_atoms": [],
                    "physical_context": None,
                }
            },
        )

        (bound,) = bind_visual_page_evidence([draft], proof)

        self.assertEqual(bound.artifact_locator["evidence_artifacts"], [visual])

    def test_visual_binder_does_not_invent_a_carrier(self) -> None:
        self.assertEqual(bind_visual_page_evidence([], _visual_proof()), [])


class NativeStreamUnitDraftTests(unittest.TestCase):
    """Every native gap becomes one detached draft at its stream position."""

    def _drafts(
        self,
        normalized_ir: dict[str, object],
        proof: SourceEvidenceProof,
    ) -> tuple[list[int], list[UnitDraft]]:
        stream = canonical_occurrence_stream(normalized_ir, proof)
        gap_orders = [
            entry.stream_order
            for entry in stream.entries
            if entry.kind == "native_gap_run"
        ]
        elements = normalized_ir.get("elements")
        element_orders = {
            element["source_item_index"]: element["order_index"]
            for element in (elements if isinstance(elements, list) else ())
        }
        return gap_orders, native_stream_unit_drafts(
            stream,
            element_orders=element_orders,
        )

    def test_every_gap_materializes_once_at_its_stream_position(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="缺口甲", page=0, word=1),
                MappedAtom(carrier=1, page=0, word=2, block=1),
                NativeAtom(text="缺口乙", page=0, word=3),
            ),
        )

        gap_orders, drafts = self._drafts(normalized_ir, proof)

        self.assertEqual([draft.source_order for draft in drafts], gap_orders)
        self.assertEqual(
            [part["text"] for draft in drafts for part in draft.payload["parts"]],
            ["缺口甲", "缺口乙"],
        )
        for draft in drafts:
            # The grouper must never read a native run as a section boundary.
            self.assertTrue(draft.detached_from_section)
            self.assertIsNone(draft.title)
            self.assertEqual(draft.heading_path, [])
            self.assertEqual(draft.section_path, [])
            self.assertEqual(draft.payload_kind, "mixed")
            self.assertEqual(draft.payload["semantic_type"], "document")

    def test_physical_context_records_the_proven_placement_only(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="被包含", page=0, word=1),
                MappedAtom(carrier=0, page=0, word=2, block=0),
                NativeAtom(text="自由缺口", page=0, word=3),
                MappedAtom(carrier=1, page=0, word=4, block=1),
            ),
        )

        _gap_orders, drafts = self._drafts(normalized_ir, proof)

        contexts = [
            draft.artifact_locator["source_projection"]["physical_context"]
            for draft in drafts
        ]
        self.assertEqual(
            [context["version"] for context in contexts],
            ["source-native-placement.v2"] * 2,
        )
        self.assertEqual(
            [
                (context["order_basis"], context["containment_owner"])
                for context in contexts
            ],
            [("containment_proven", 0), ("native_proven", None)],
        )
        self.assertEqual(
            [context["relation"] for context in contexts],
            ["bounded_by_same_source", "between_mapped_sources"],
        )
        self.assertEqual(
            [context["word_order_span"] for context in contexts],
            [[1, 2], [3, 4]],
        )
        for context in contexts:
            # v1 carried a review lane inside the placement record; v2 proves
            # the position instead, so neither key may come back.
            self.assertNotIn("linearization", context)
        for draft in drafts:
            self.assertNotIn("review_reason", draft.artifact_locator)

    def test_quality_status_tracks_occurrence_review_only(self) -> None:
        normalized_ir, proof = build_case(
            page_count=2,
            elements=(Element(index=0, page=1),),
            atoms=(
                MappedAtom(carrier=0, page=1, word=0, block=0),
                NativeAtom(text="纯文本缺口", page=1, word=1),
            ),
            visual_pages=(0,),
        )

        _gap_orders, drafts = self._drafts(normalized_ir, proof)

        visual, text = drafts
        self.assertEqual(visual.quality_status, "needs_review")
        self.assertEqual(
            [part["kind"] for part in visual.payload["parts"]],
            ["image"],
        )
        self.assertEqual(text.quality_status, "ok")
        self.assertEqual(
            [part.get("quality_status") for part in text.payload["parts"]],
            [None],
        )

    def test_one_retrieval_run_split_across_atoms_stays_one_search_atom(
        self,
    ) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="股", page=0, word=1),
                NativeAtom(text="份变动", page=0, word=2),
                MappedAtom(carrier=1, page=0, word=3, block=1),
            ),
        )

        _gap_orders, (draft,) = self._drafts(normalized_ir, proof)

        graph = draft.artifact_locator["source_projection"]
        self.assertEqual(graph["search_atoms"], [])
        self.assertEqual(len(draft.payload["parts"]), 1)
        part = draft.payload["parts"][0]
        self.assertEqual(part["text"], "股份变动")
        part_graph = part["artifact_locator"]["source_projection"]
        self.assertEqual(part_graph["payload"]["kind"], "text_concat")
        self.assertEqual(part_graph["payload"]["transform"], "exact_concat.v1")
        self.assertEqual(len(part_graph["payload"]["sources"]), 2)
        self.assertEqual(part_graph["search_targets"], ["payload.text"])
        self.assertEqual(part_graph["search_atoms"], [])
        self.assertEqual(
            search_text_values(
                payload_kind=draft.payload_kind,
                payload=draft.payload,
                artifact_locator=draft.artifact_locator,
            ),
            ("股份变动",),
        )

    def test_container_projection_owns_no_source_of_its_own(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0),),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="页尾缺口", page=0, word=1),
            ),
        )

        _gap_orders, (draft,) = self._drafts(normalized_ir, proof)

        graph = draft.artifact_locator["source_projection"]
        self.assertEqual(graph["payload"]["sources"], [])
        self.assertEqual(graph["payload"]["target_field"], "payload.parts")
        self.assertEqual(graph["heading_path"], [])
        self.assertEqual(graph["structured"], [])
        self.assertEqual(graph["provenance"], [])
        self.assertEqual(graph["search_targets"], [])
        self.assertEqual(
            empty_projection_graph()["version"],
            graph["version"],
        )
        (part,) = draft.payload["parts"]
        part_graph = part["artifact_locator"]["source_projection"]
        self.assertEqual(part_graph["search_targets"], ["payload.text"])
        self.assertEqual(part_graph["search_atoms"], [])
        self.assertIsNone(part_graph["physical_context"])
        self.assertEqual(
            part_graph["payload"]["sources"][0]["source"]["kind"],
            "source_evidence_atom",
        )

def _visual_artifact(value: dict[str, object]) -> VisualArtifactProof:
    size_bytes = value["size_bytes"]
    pixel_width = value["pixel_width"]
    pixel_height = value["pixel_height"]
    assert isinstance(size_bytes, int)
    assert isinstance(pixel_width, int)
    assert isinstance(pixel_height, int)
    return VisualArtifactProof(
        artifact_role=str(value["artifact_role"]),
        sha256=str(value["sha256"]),
        size_bytes=size_bytes,
        pixel_width=pixel_width,
        pixel_height=pixel_height,
        media_type="image/png",
    )


def _visual_proof(
    *bindings: VisualBindingProof,
) -> SourceEvidenceProof:
    return SourceEvidenceProof(
        identity=SourceProofIdentity(
            source_evidence_sha256="sha256:" + "b" * 64,
            source_pdf_sha256="sha256:" + "a" * 64,
            page_count=2,
        ),
        pages=(
            SourcePageProof(page_idx=0, events=()),
            SourcePageProof(page_idx=1, events=()),
        ),
        retrieval_runs=(),
        visual_bindings=bindings,
        verified_visuals=tuple(
            VerifiedVisualArtifact(
                artifact_role=binding.artifact.artifact_role,
                sha256=binding.artifact.sha256,
            )
            for binding in bindings
        ),
    )


if __name__ == "__main__":
    unittest.main()
