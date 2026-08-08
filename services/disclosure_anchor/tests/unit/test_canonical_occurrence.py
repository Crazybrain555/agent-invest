from __future__ import annotations

import hashlib
import unittest
from dataclasses import dataclass
from typing import Any, Literal

from disclosure_anchor.application.contracts.canonical_occurrence import (
    CanonicalOccurrenceStream,
    PageOrderResolution,
    canonical_occurrence_stream,
)
from disclosure_anchor.application.contracts.source_evidence import (
    MappedSourceEvent,
    NativeTextEvent,
    RetrievalRunProof,
    SourceEvidenceProof,
    SourcePageEvent,
    SourcePageProof,
    SourceProofIdentity,
    VerifiedVisualArtifact,
    VisualArtifactProof,
    VisualPageFallback,
)
from disclosure_anchor.application.contracts.source_evidence_projection import (
    SourceEvidenceProjectionError,
)

SOURCE_PDF_SHA256 = "sha256:" + "a" * 64
SOURCE_EVIDENCE_SHA256 = "sha256:" + "b" * 64


@dataclass(frozen=True)
class Element:
    """One NormalizedIR carrier element on a physical page."""

    index: int
    page: int


@dataclass(frozen=True)
class MappedAtom:
    """One native atom MinerU claims for a carrier; ``block`` is carrier_order."""

    carrier: int
    page: int
    word: int
    block: int
    order_state: Literal["monotonic", "conflict"] = "monotonic"


@dataclass(frozen=True)
class NativeAtom:
    """One native atom no carrier claims."""

    text: str
    page: int
    word: int


def text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def build_case(
    *,
    page_count: int,
    elements: tuple[Element, ...] = (),
    atoms: tuple[MappedAtom | NativeAtom, ...] = (),
    visual_pages: tuple[int, ...] = (),
) -> tuple[dict[str, Any], SourceEvidenceProof]:
    """Build one NormalizedIR/proof pair from a page-major physical script.

    Atom indices, page-local word order and retrieval-run membership are
    derived here so a case only states the physical facts it is about.
    """

    normalized_ir: dict[str, Any] = {
        "source_pdf_sha256": SOURCE_PDF_SHA256,
        "source_pdf_page_count": page_count,
        "parser_artifacts": {
            "files": {
                "source_evidence": {
                    "availability": "present",
                    "sha256": SOURCE_EVIDENCE_SHA256,
                }
            }
        },
        "elements": [
            {
                "ir_id": f"ir_{element.index}",
                "source_item_index": element.index,
                "order_index": element.index,
                "page_idx": element.page,
                "page_no": element.page + 1,
                "bbox": [0, 0, 100, 20],
            }
            for element in elements
        ],
    }
    page_events: dict[int, list[SourcePageEvent]] = {
        page_idx: [] for page_idx in range(page_count)
    }
    ordered = sorted(atoms, key=lambda atom: (atom.page, atom.word))
    for atom_index, atom in enumerate(ordered):
        if isinstance(atom, MappedAtom):
            selector_value = f"载体{atom.carrier}"
            page_events[atom.page].append(
                MappedSourceEvent(
                    atom_index=atom_index,
                    word_order=atom.word,
                    source_item_index=atom.carrier,
                    order_state=atom.order_state,
                    selector_field="text",
                    selector_index=None,
                    selector_char_span=(0, len(selector_value)),
                    selector_value_sha256=text_sha256(selector_value),
                    carrier_order=atom.block,
                    carrier_bbox=(0.0, 0.0, 100.0, 20.0),
                    atom_bbox=atom_bbox(atom.word),
                    native_layout_path=(0, atom.block, 0, atom.word),
                )
            )
            continue
        page_events[atom.page].append(
            NativeTextEvent(
                atom_index=atom_index,
                word_order=atom.word,
                text=atom.text,
                text_sha256=text_sha256(atom.text),
                bbox=atom_bbox(atom.word),
                char_span=(atom.word, atom.word + 1),
                layout_path=(0, atom.word, 0, 0),
            )
        )
    visual_artifacts = {
        page_idx: VisualArtifactProof(
            artifact_role=f"source_page_visual_{page_idx:06d}",
            sha256=text_sha256(f"page-visual-{page_idx}"),
            size_bytes=321,
            pixel_width=100,
            pixel_height=200,
            media_type="image/png",
        )
        for page_idx in visual_pages
    }
    proof = SourceEvidenceProof(
        identity=SourceProofIdentity(
            source_evidence_sha256=SOURCE_EVIDENCE_SHA256,
            source_pdf_sha256=SOURCE_PDF_SHA256,
            page_count=page_count,
        ),
        pages=tuple(
            SourcePageProof(
                page_idx=page_idx,
                events=tuple(page_events[page_idx]),
                visual_only=(
                    VisualPageFallback(
                        visual_artifact=visual_artifacts[page_idx]
                    )
                    if page_idx in visual_artifacts
                    else None
                ),
            )
            for page_idx in range(page_count)
        ),
        retrieval_runs=retrieval_runs(page_events),
        visual_bindings=(),
        verified_visuals=tuple(
            VerifiedVisualArtifact(
                artifact_role=artifact.artifact_role,
                sha256=artifact.sha256,
            )
            for artifact in visual_artifacts.values()
        ),
    )
    return normalized_ir, proof


def atom_bbox(word_order: int) -> tuple[float, float, float, float]:
    return (float(word_order * 10), 0.0, float(word_order * 10 + 10), 10.0)


def retrieval_runs(
    page_events: dict[int, list[SourcePageEvent]],
) -> tuple[RetrievalRunProof, ...]:
    """Close every native atom into its maximal consecutive page-local run."""

    runs: list[RetrievalRunProof] = []
    for page_idx, events in page_events.items():
        groups: list[list[NativeTextEvent]] = []
        pending: list[NativeTextEvent] = []
        for event in events:
            if isinstance(event, NativeTextEvent):
                pending.append(event)
                continue
            if pending:
                groups.append(pending)
                pending = []
        if pending:
            groups.append(pending)
        for run_index, members in enumerate(groups):
            runs.append(
                RetrievalRunProof(
                    page_idx=page_idx,
                    run_index=run_index,
                    atom_indices=tuple(item.atom_index for item in members),
                    text_sha256=text_sha256(
                        "".join(item.text for item in members)
                    ),
                )
            )
    return tuple(runs)


def entry_shapes(
    stream: CanonicalOccurrenceStream,
) -> list[tuple[str, Any, str]]:
    """Render the stream as ordered (kind, identity, order_basis) triples."""

    shapes: list[tuple[str, Any, str]] = []
    for entry in stream.entries:
        if entry.kind == "mineru_carrier":
            shapes.append(
                ("carrier", entry.source_item_index, entry.order_basis)
            )
            continue
        assert entry.gap is not None
        shapes.append(("gap", entry.gap.word_order_span, entry.order_basis))
    return shapes


def gap_relations(stream: CanonicalOccurrenceStream) -> list[str]:
    return [
        entry.gap.relation
        for entry in stream.entries
        if entry.gap is not None
    ]


def assert_conservation(
    case: unittest.TestCase,
    stream: CanonicalOccurrenceStream,
    *,
    carriers: tuple[int, ...],
    gaps: tuple[tuple[int, tuple[int, int]], ...],
) -> None:
    """Every carrier and every native gap holds exactly one dense position."""

    case.assertEqual(
        sorted(
            entry.source_item_index
            for entry in stream.entries
            if entry.kind == "mineru_carrier"
        ),
        sorted(carriers),
    )
    case.assertEqual(
        sorted(
            (entry.page_idx, entry.gap.word_order_span)
            for entry in stream.entries
            if entry.gap is not None
        ),
        sorted(gaps),
    )
    for entry in stream.entries:
        case.assertEqual(
            entry.gap is not None, entry.kind == "native_gap_run"
        )
    case.assertEqual(
        [entry.stream_order for entry in stream.entries],
        list(range(len(stream.entries))),
    )
    case.assertEqual(
        [entry.page_idx for entry in stream.entries],
        sorted(entry.page_idx for entry in stream.entries),
    )


class NativeProvenOrderTests(unittest.TestCase):
    def test_native_word_order_interleaves_carriers_and_gaps(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="原生甲", page=0, word=1),
                MappedAtom(carrier=1, page=0, word=2, block=1),
                NativeAtom(text="原生乙", page=0, word=3),
            ),
        )

        stream = canonical_occurrence_stream(normalized_ir, proof)

        self.assertEqual(
            entry_shapes(stream),
            [
                ("carrier", 0, "native_proven"),
                ("gap", (1, 2), "native_proven"),
                ("carrier", 1, "native_proven"),
                ("gap", (3, 4), "native_proven"),
            ],
        )
        self.assertEqual(
            [entry.native_span for entry in stream.entries],
            [(0, 1), (1, 2), (2, 3), (3, 4)],
        )
        self.assertEqual(
            stream.pages,
            (
                PageOrderResolution(
                    page_idx=0,
                    order_basis="native_proven",
                    span_overlap_count=0,
                    order_conflict_count=0,
                ),
            ),
        )
        assert_conservation(
            self,
            stream,
            carriers=(0, 1),
            gaps=((0, (1, 2)), (0, (3, 4))),
        )

    def test_gap_bounded_by_one_carrier_is_contained_by_that_carrier(
        self,
    ) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="被包含", page=0, word=1),
                MappedAtom(carrier=0, page=0, word=2, block=0),
                MappedAtom(carrier=1, page=0, word=3, block=1),
            ),
        )

        stream = canonical_occurrence_stream(normalized_ir, proof)

        self.assertEqual(
            entry_shapes(stream),
            [
                ("carrier", 0, "native_proven"),
                ("gap", (1, 2), "containment_proven"),
                ("carrier", 1, "native_proven"),
            ],
        )
        self.assertEqual(gap_relations(stream), ["bounded_by_same_source"])
        self.assertEqual(stream.entries[1].containment_owner, 0)
        self.assertEqual(
            stream.entries[0].source_item_index,
            stream.entries[1].containment_owner,
        )
        self.assertEqual(stream.pages[0].order_basis, "native_proven")
        assert_conservation(
            self,
            stream,
            carriers=(0, 1),
            gaps=((0, (1, 2)),),
        )

    def test_unmapped_carrier_is_woven_by_its_provider_index(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(
                Element(index=0, page=0),
                Element(index=1, page=0),
                Element(index=2, page=0),
            ),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                MappedAtom(carrier=2, page=0, word=1, block=2),
            ),
        )

        stream = canonical_occurrence_stream(normalized_ir, proof)

        self.assertEqual(
            entry_shapes(stream),
            [
                ("carrier", 0, "native_proven"),
                ("carrier", 1, "provider_attested"),
                ("carrier", 2, "native_proven"),
            ],
        )
        (visual,) = [
            entry
            for entry in stream.entries
            if entry.source_item_index == 1
        ]
        self.assertIsNone(visual.native_span)
        self.assertEqual(visual.provider_order, 1)
        self.assertEqual(stream.pages[0].order_basis, "native_proven")
        assert_conservation(self, stream, carriers=(0, 1, 2), gaps=())

    def test_stream_is_page_major_and_densely_ordered(self) -> None:
        normalized_ir, proof = build_case(
            page_count=2,
            elements=(Element(index=0, page=0), Element(index=1, page=1)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="第一页尾", page=0, word=1),
                NativeAtom(text="第二页首", page=1, word=0),
                MappedAtom(carrier=1, page=1, word=1, block=1),
            ),
        )

        stream = canonical_occurrence_stream(normalized_ir, proof)

        self.assertEqual(
            entry_shapes(stream),
            [
                ("carrier", 0, "native_proven"),
                ("gap", (1, 2), "native_proven"),
                ("gap", (0, 1), "native_proven"),
                ("carrier", 1, "native_proven"),
            ],
        )
        self.assertEqual(
            [(entry.page_idx, entry.stream_order) for entry in stream.entries],
            [(0, 0), (0, 1), (1, 2), (1, 3)],
        )
        self.assertEqual(
            [page.page_idx for page in stream.pages],
            [0, 1],
        )
        assert_conservation(
            self,
            stream,
            carriers=(0, 1),
            gaps=((0, (1, 2)), (1, (0, 1))),
        )

    def test_visual_only_page_is_its_single_stream_entry(self) -> None:
        normalized_ir, proof = build_case(
            page_count=2,
            elements=(Element(index=0, page=1),),
            atoms=(MappedAtom(carrier=0, page=1, word=0, block=0),),
            visual_pages=(0,),
        )

        stream = canonical_occurrence_stream(normalized_ir, proof)

        self.assertEqual(
            entry_shapes(stream),
            [
                ("gap", (0, 0), "native_proven"),
                ("carrier", 0, "native_proven"),
            ],
        )
        page_entries = [
            entry for entry in stream.entries if entry.page_idx == 0
        ]
        self.assertEqual(len(page_entries), 1)
        assert page_entries[0].gap is not None
        self.assertEqual(page_entries[0].gap.relation, "page_only")
        assert_conservation(
            self,
            stream,
            carriers=(0,),
            gaps=((0, (0, 0)),),
        )


class ProviderAttestedOrderTests(unittest.TestCase):
    def test_overlapping_native_spans_switch_the_page_to_provider_order(
        self,
    ) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=5),
                MappedAtom(carrier=1, page=0, word=1, block=2),
                MappedAtom(carrier=1, page=0, word=2, block=2),
                MappedAtom(carrier=0, page=0, word=3, block=5),
            ),
        )

        stream = canonical_occurrence_stream(normalized_ir, proof)

        self.assertEqual(
            entry_shapes(stream),
            [
                ("carrier", 1, "provider_attested"),
                ("carrier", 0, "provider_attested"),
            ],
        )
        self.assertEqual(
            [entry.provider_order for entry in stream.entries],
            [2, 5],
        )
        page = stream.pages[0]
        self.assertEqual(page.order_basis, "provider_attested")
        self.assertGreaterEqual(page.span_overlap_count, 1)
        self.assertEqual(page.order_conflict_count, 0)
        assert_conservation(self, stream, carriers=(0, 1), gaps=())

    def test_order_conflict_attests_the_page_without_any_gap(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                MappedAtom(
                    carrier=1,
                    page=0,
                    word=1,
                    block=1,
                    order_state="conflict",
                ),
            ),
        )

        stream = canonical_occurrence_stream(normalized_ir, proof)

        self.assertEqual(
            entry_shapes(stream),
            [
                ("carrier", 0, "provider_attested"),
                ("carrier", 1, "provider_attested"),
            ],
        )
        page = stream.pages[0]
        self.assertEqual(page.order_basis, "provider_attested")
        self.assertEqual(page.span_overlap_count, 0)
        self.assertGreaterEqual(page.order_conflict_count, 1)
        assert_conservation(self, stream, carriers=(0, 1), gaps=())

    def test_free_gaps_keep_deterministic_slots_on_an_attested_page(
        self,
    ) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                NativeAtom(text="页首缺口", page=0, word=0),
                MappedAtom(carrier=0, page=0, word=1, block=7),
                NativeAtom(text="中段缺口", page=0, word=2),
                MappedAtom(carrier=1, page=0, word=3, block=3),
                MappedAtom(carrier=0, page=0, word=4, block=7),
                NativeAtom(text="页尾缺口", page=0, word=5),
            ),
        )

        stream = canonical_occurrence_stream(normalized_ir, proof)

        self.assertEqual(
            entry_shapes(stream),
            [
                ("gap", (0, 1), "provider_attested"),
                ("carrier", 1, "provider_attested"),
                ("carrier", 0, "provider_attested"),
                ("gap", (2, 3), "provider_attested"),
                ("gap", (5, 6), "provider_attested"),
            ],
        )
        self.assertEqual(
            gap_relations(stream),
            ["page_prefix", "between_mapped_sources", "page_suffix"],
        )
        page = stream.pages[0]
        self.assertEqual(page.order_basis, "provider_attested")
        self.assertGreaterEqual(page.span_overlap_count, 1)
        assert_conservation(
            self,
            stream,
            carriers=(0, 1),
            gaps=((0, (0, 1)), (0, (2, 3)), (0, (5, 6))),
        )


class ContradictorySourceIdentityTests(unittest.TestCase):
    def test_carrier_element_page_must_match_its_mapped_atoms(self) -> None:
        normalized_ir, proof = build_case(
            page_count=2,
            elements=(Element(index=0, page=1),),
            atoms=(MappedAtom(carrier=0, page=0, word=0, block=0),),
        )

        with self.assertRaises(SourceEvidenceProjectionError) as raised:
            canonical_occurrence_stream(normalized_ir, proof)

        self.assertIn(
            "differs from its NormalizedIR page", str(raised.exception)
        )

    def test_one_carrier_cannot_own_mapped_atoms_on_two_pages(self) -> None:
        normalized_ir, proof = build_case(
            page_count=2,
            elements=(Element(index=0, page=0),),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                MappedAtom(carrier=0, page=1, word=0, block=0),
            ),
        )

        with self.assertRaises(SourceEvidenceProjectionError) as raised:
            canonical_occurrence_stream(normalized_ir, proof)

        self.assertIn(
            "differs from its NormalizedIR page", str(raised.exception)
        )

    def test_carrier_provider_order_must_be_consistent(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0),),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=3),
                MappedAtom(carrier=0, page=0, word=1, block=4),
            ),
        )

        with self.assertRaises(SourceEvidenceProjectionError) as raised:
            canonical_occurrence_stream(normalized_ir, proof)

        self.assertIn("inconsistent provider orders", str(raised.exception))

    def test_native_atom_outside_every_retrieval_run_fails_loud(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0),),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="失联原生", page=0, word=1),
            ),
        )
        # The proof type closes retrieval runs over every native atom, so the
        # projection guard is only reachable by forging that closed state.
        object.__setattr__(proof, "retrieval_runs", ())

        with self.assertRaises(SourceEvidenceProjectionError) as raised:
            canonical_occurrence_stream(normalized_ir, proof)

        self.assertIn(
            "native text atom 1 belongs to no retrieval run",
            str(raised.exception),
        )

    def test_normalized_ir_elements_need_unique_identity_and_a_page(
        self,
    ) -> None:
        with self.subTest("duplicate source_item_index"):
            normalized_ir, proof = build_case(
                page_count=1,
                elements=(Element(index=0, page=0),),
                atoms=(MappedAtom(carrier=0, page=0, word=0, block=0),),
            )
            elements = normalized_ir["elements"]
            elements.append(dict(elements[0]))

            with self.assertRaises(SourceEvidenceProjectionError) as raised:
                canonical_occurrence_stream(normalized_ir, proof)

            self.assertIn("appears twice", str(raised.exception))

        with self.subTest("missing page_no"):
            normalized_ir, proof = build_case(
                page_count=1,
                elements=(Element(index=0, page=0),),
                atoms=(MappedAtom(carrier=0, page=0, word=0, block=0),),
            )
            del normalized_ir["elements"][0]["page_no"]

            with self.assertRaises(SourceEvidenceProjectionError) as raised:
                canonical_occurrence_stream(normalized_ir, proof)

            self.assertIn(
                "lacks integer source identity or page",
                str(raised.exception),
            )


if __name__ == "__main__":
    unittest.main()
