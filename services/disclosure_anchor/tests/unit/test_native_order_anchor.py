"""Publication order and section attribution of native gap runs.

Both behaviours are decided by private builder passes that these cases never
import.  Each case drives the real composition instead — canonical occurrence
stream, native stream drafts, S1-S7 assembly and the independent document audit
— and asserts the published sequence, the placement record and a clean audit
report, so the passes stay covered by observable publication behaviour only.
"""

from __future__ import annotations

import hashlib
import unittest
from typing import Any

from disclosure_anchor.application.contracts.document_structure import (
    DOCUMENT_STRUCTURE_ALGORITHM,
    DOCUMENT_STRUCTURE_VERSION,
    carrier_set_sha256,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    validate_current_normalized_ir_for_write,
)
from disclosure_anchor.application.contracts.source_evidence import (
    SourceEvidenceProof,
)
from disclosure_anchor.application.services.document_unit_audit import (
    AuditDocumentMetadata,
    AuditUnitView,
    DocumentAuditReport,
    audit_document,
)
from disclosure_anchor.application.services.unit_builder.builder import (
    BuildStats,
    UnitDraft,
)
from disclosure_anchor.application.services.unit_preparation import (
    prepare_and_audit_units,
)
from tests.unit.test_canonical_occurrence import (
    SOURCE_PDF_SHA256,
    Element,
    MappedAtom,
    NativeAtom,
    build_case,
)

_DOCUMENT_ID = "doc_native_order"
_ARTIFACT_ROLES = (
    "content_list",
    "content_list_v2",
    "middle",
    "model",
    "pdf_structure",
    "source_evidence",
    "visual_semantics",
)
# v4 derives the carrier ``kind`` from its provider ``raw_kind``; only the
# roles these cases need are mapped here.
_CARRIER_KINDS = {"text": "text", "list": "text", "header": "page_furniture"}


def carrier(
    *,
    text: str,
    raw_kind: str = "text",
    heading: bool = False,
) -> dict[str, Any]:
    """Describe one NormalizedIR carrier by its provider role and text."""

    fields: dict[str, Any] = {"raw_kind": raw_kind, "text": text}
    if heading:
        fields["text_level"] = 1
    if raw_kind == "list":
        fields["list_items"] = text.split("\n")
    return fields


def heading_node(
    node_id: int,
    source_index: int,
    *,
    text: str,
    section_end: int,
) -> dict[str, Any]:
    """Prove one top-level section owning carriers ``source_index..end``."""

    return {
        "node_id": node_id,
        "parent_node_id": None,
        "heading_level": 1,
        "propagates": True,
        "evidence_kinds": ["mineru_v2_title"],
        "section_span": [source_index, section_end],
        "source_refs": [
            {
                "source_item_index": source_index,
                "field": "text",
                "text_span": [0, len(text)],
            }
        ],
    }


def document_ir(
    normalized_ir: dict[str, Any],
    *,
    carriers: dict[int, dict[str, Any]],
    headings: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Complete one canonical-stream case into a write-valid v4 document.

    ``build_case`` states only the physical facts an ordering case is about;
    publication additionally needs the closed v4 envelope, so the remaining
    fields are filled in here and validated against the write contract.
    """

    elements = [
        {
            **element,
            "kind": _CARRIER_KINDS[
                str(carriers[element["source_item_index"]]["raw_kind"])
            ],
            "source_item_sha256": "sha256:"
            + hashlib.sha256(
                f"carrier:{element['source_item_index']}".encode()
            ).hexdigest(),
            **carriers[element["source_item_index"]],
        }
        for element in normalized_ir["elements"]
    ]
    page_count = normalized_ir["source_pdf_page_count"]
    source_evidence_sha256 = normalized_ir["parser_artifacts"]["files"][
        "source_evidence"
    ]["sha256"]
    document: dict[str, Any] = {
        "contract_version": "normalized_ir.v4",
        "created_at": "2026-07-29T00:00:00Z",
        "document_id": _DOCUMENT_ID,
        "source_pdf": "raw/native_order.pdf",
        "source_pdf_sha256": SOURCE_PDF_SHA256,
        "source_pdf_page_count": page_count,
        "title": "排序样本",
        "parser": {
            "name": "MinerU",
            "package_version": "3.4.0",
            "backend": "pipeline",
            "method": "auto",
            "language": "ch",
            "formula": False,
            "table": True,
            "effort": None,
            "image_analysis": False,
            "full_pdf": True,
            "start_page": None,
            "end_page": None,
            "runtime_bundle_identity_sha256": "sha256:" + "c" * 64,
            "inline_equation_left": "$",
            "inline_equation_right": "$",
            "target_contract_version": "parser-target.v1",
        },
        "parser_artifacts": {
            "artifact_root_relpath": "parser/native_order",
            "files": {
                role: {
                    "availability": "present",
                    "relpath": f"parser/native_order/{role}.json",
                    "sha256": (
                        source_evidence_sha256
                        if role == "source_evidence"
                        else "sha256:" + "b" * 64
                    ),
                    "size_bytes": 1,
                }
                for role in _ARTIFACT_ROLES
            },
        },
        "parsed_pages": {
            "start_page_no": 1,
            "end_page_no": page_count,
            "full_pdf": True,
        },
        "parser_diagnostics": {
            "table_reconciliation": {
                "algorithm_version": "mineru-page-local-table-closure.v6",
                "model_hash": "sha256:" + "b" * 64,
                "content_tables": 0,
                "model_tables": 0,
                "matched_tables": 0,
                "page_local_closed": True,
            },
            "visual_semantics": {
                "contract_version": "visual-semantics.v1",
                "artifact_role": "visual_semantics",
                "artifact_sha256": "sha256:" + "b" * 64,
                "disposition_count": 0,
                "status_counts": {
                    "semantic_text": 0,
                    "guard_only": 0,
                    "unresolved": 0,
                },
            },
        },
        "elements": elements,
        "structure_proof": {
            "contract_version": DOCUMENT_STRUCTURE_VERSION,
            "algorithm_version": DOCUMENT_STRUCTURE_ALGORITHM,
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_pdf_page_count": page_count,
            "carrier_set_sha256": carrier_set_sha256(elements),
            "native": {"status": "untagged", "artifact_role": "pdf_structure"},
            "headings": list(headings),
            "page_frames": [],
            "conflicts": [],
            "coverage": {
                "heading_nodes": len(headings),
                "page_frame_groups": 0,
            },
        },
    }
    validate_current_normalized_ir_for_write(document)
    return document


def publish(
    normalized_ir: dict[str, Any],
    proof: SourceEvidenceProof,
) -> tuple[list[UnitDraft], BuildStats, DocumentAuditReport]:
    """Assemble and audit through the composition publication itself uses."""

    return prepare_and_audit_units(
        normalized_ir=normalized_ir,
        filing_type="other",
        metadata=AuditDocumentMetadata(
            document_id=_DOCUMENT_ID,
            title="排序样本",
            filing_type="other",
        ),
        image_artifact_resolver=None,
        image_hash_provider=dict,
        source_proof=proof,
    )


def physical_context(draft: UnitDraft) -> dict[str, Any] | None:
    graph = (draft.artifact_locator or {}).get("source_projection")
    context = graph.get("physical_context") if isinstance(graph, dict) else None
    return context if isinstance(context, dict) else None


def published_shapes(drafts: list[UnitDraft]) -> list[tuple[str, Any]]:
    """Render publication as ordered carrier/gap identities.

    A carrier unit is identified by the source order it publishes at, a native
    gap run by the physical position its own placement record proves.
    """

    shapes: list[tuple[str, Any]] = []
    for draft in drafts:
        context = physical_context(draft)
        if context is None:
            shapes.append(("carrier", draft.source_order))
            continue
        shapes.append(
            ("gap", (context["page_no"], context["word_order_span"][0]))
        )
    return shapes


def gap_texts(draft: UnitDraft) -> list[str]:
    return [part["text"] for part in draft.payload["parts"]]


class PublicationCase(unittest.TestCase):
    def assert_audit_ok(self, report: DocumentAuditReport) -> None:
        self.assertEqual(
            [finding.as_dict() for finding in report.findings],
            [],
        )
        self.assertTrue(report.ok)


class NativeGapPublicationOrderTests(PublicationCase):
    def test_attested_page_publishes_each_gap_after_its_own_carrier(
        self,
    ) -> None:
        # One conflicting mapped atom attests the page, so the carriers
        # publish in provider order while the native words run the other way.
        # Each gap must follow the carrier that physically precedes it, not
        # the page-global native word order.
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=1, page=0, word=0, block=1),
                NativeAtom(text="原生甲", page=0, word=1),
                MappedAtom(
                    carrier=0,
                    page=0,
                    word=2,
                    block=0,
                    order_state="conflict",
                ),
                NativeAtom(text="原生乙", page=0, word=3),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={
                0: carrier(text="第一节 甲", heading=True),
                1: carrier(text="第二节 乙", heading=True),
            },
            headings=(
                heading_node(1, 0, text="第一节 甲", section_end=0),
                heading_node(2, 1, text="第二节 乙", section_end=1),
            ),
        )

        drafts, stats, report = publish(document, proof)

        self.assertEqual(
            published_shapes(drafts),
            [
                ("carrier", 0),
                ("gap", (1, 3)),
                ("carrier", 1),
                ("gap", (1, 1)),
            ],
        )
        self.assertEqual(
            [gap_texts(drafts[1]), gap_texts(drafts[3])],
            [["原生乙"], ["原生甲"]],
        )
        self.assertEqual(stats.provider_attested_pages, 1)
        self.assert_audit_ok(report)

    def test_document_leading_gap_publishes_before_every_carrier(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0),),
            atoms=(
                NativeAtom(text="文首原生", page=0, word=0),
                MappedAtom(carrier=0, page=0, word=1, block=0),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={0: carrier(text="正文载体")},
        )

        drafts, _stats, report = publish(document, proof)

        self.assertEqual(
            published_shapes(drafts),
            [("gap", (1, 0)), ("carrier", 0)],
        )
        self.assertEqual(
            physical_context(drafts[0])["relation"],
            "page_prefix",
        )
        self.assert_audit_ok(report)

    def test_page_prefix_gap_follows_the_previous_page_last_carrier(
        self,
    ) -> None:
        normalized_ir, proof = build_case(
            page_count=2,
            elements=(Element(index=0, page=0), Element(index=1, page=1)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="次页页首原生", page=1, word=0),
                MappedAtom(carrier=1, page=1, word=1, block=1),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={
                0: carrier(text="第一节 甲", heading=True),
                1: carrier(text="第二节 乙", heading=True),
            },
            headings=(
                heading_node(1, 0, text="第一节 甲", section_end=0),
                heading_node(2, 1, text="第二节 乙", section_end=1),
            ),
        )

        drafts, _stats, report = publish(document, proof)

        # The gap opens page 2 and still publishes between the two carriers:
        # its anchor is the last carrier of page 1, not the page it sits on.
        self.assertEqual(
            published_shapes(drafts),
            [("carrier", 0), ("gap", (2, 0)), ("carrier", 1)],
        )
        self.assertEqual(
            physical_context(drafts[1])["relation"],
            "page_prefix",
        )
        self.assert_audit_ok(report)

    def test_gaps_sharing_one_anchor_publish_in_native_word_order(
        self,
    ) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0),),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="被包含原生", page=0, word=1),
                MappedAtom(carrier=0, page=0, word=2, block=0),
                NativeAtom(text="页尾原生", page=0, word=3),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={0: carrier(text="正文载体")},
        )

        drafts, _stats, report = publish(document, proof)

        self.assertEqual(
            published_shapes(drafts),
            [("carrier", 0), ("gap", (1, 1)), ("gap", (1, 3))],
        )
        self.assertEqual(
            [
                physical_context(draft)["relation"]
                for draft in drafts[1:]
            ],
            ["bounded_by_same_source", "page_suffix"],
        )
        self.assert_audit_ok(report)

    def test_gap_inside_a_section_trails_that_section_container(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(
                Element(index=0, page=0),
                Element(index=1, page=0),
                Element(index=2, page=0),
            ),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                MappedAtom(carrier=1, page=0, word=1, block=1),
                NativeAtom(text="节内原生", page=0, word=2),
                MappedAtom(carrier=2, page=0, word=3, block=2),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={
                0: carrier(text="第一节 概览", heading=True),
                1: carrier(text="一、甲事项\n二、乙事项", raw_kind="list"),
                2: carrier(text="三、丙事项\n四、丁事项", raw_kind="list"),
            },
            headings=(heading_node(1, 0, text="第一节 概览", section_end=2),),
        )

        drafts, _stats, report = publish(document, proof)

        # The run sits between the two section members physically, but a
        # native run may never split a proven section occurrence, so it is
        # held and published right after the whole container.
        container, gap = drafts
        self.assertEqual(
            published_shapes(drafts),
            [("carrier", 1), ("gap", (1, 2))],
        )
        self.assertEqual(container.payload["semantic_type"], "section")
        self.assertEqual(container.heading_path, ["第一节 概览"])
        self.assertEqual(
            [part["text"] for part in container.payload["parts"]],
            ["一、甲事项\n二、乙事项", "三、丙事项\n四、丁事项"],
        )
        self.assertEqual(gap_texts(gap), ["节内原生"])
        self.assert_audit_ok(report)


class NativeGapSectionAttributionTests(PublicationCase):
    """The placement record carries the anchor's published section path.

    A native recovery never claims a heading path of its own, so its section
    is proven transitively: whichever published unit owns the anchor carrier
    already carries the proven path.  The recovery therefore stays locatable
    without inventing projections or proof ancestry it does not have.
    """

    def test_gap_carries_the_section_path_of_its_anchor_carrier(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                MappedAtom(carrier=1, page=0, word=1, block=1),
                NativeAtom(text="节末原生", page=0, word=2),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={
                0: carrier(text="第一节 概览", heading=True),
                1: carrier(text="一、甲事项\n二、乙事项", raw_kind="list"),
            },
            headings=(heading_node(1, 0, text="第一节 概览", section_end=1),),
        )

        drafts, _stats, report = publish(document, proof)

        section, gap = drafts
        self.assertEqual(section.heading_path, ["第一节 概览"])
        self.assertEqual(
            physical_context(gap)["anchor_heading_path"],
            section.heading_path,
        )
        # The recovery is located inside the section without claiming it: the
        # public heading contract stays empty for an unproven unit.
        self.assertEqual(gap.heading_path, [])
        self.assertEqual(gap_texts(gap), ["节末原生"])
        self.assert_audit_ok(report)

    def test_anchor_inside_a_container_attributes_the_container_path(
        self,
    ) -> None:
        # The anchor is an unproven page header, detached on its own but
        # swallowed as a part of the section container that surrounds it.
        # Attribution is published-unit scope, so the recovery inherits the
        # container path rather than the part's empty one.
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(
                Element(index=0, page=0),
                Element(index=1, page=0),
                Element(index=2, page=0),
            ),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                MappedAtom(carrier=1, page=0, word=1, block=1),
                MappedAtom(carrier=2, page=0, word=2, block=2),
                NativeAtom(text="页眉后原生", page=0, word=3),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={
                0: carrier(text="第一节 概览", heading=True),
                1: carrier(text="一、甲事项\n二、乙事项", raw_kind="list"),
                2: carrier(text="某某股份有限公司2024年年度报告", raw_kind="header"),
            },
            headings=(heading_node(1, 0, text="第一节 概览", section_end=2),),
        )

        drafts, _stats, report = publish(document, proof)

        container, gap = drafts
        self.assertEqual(container.payload["semantic_type"], "section")
        self.assertEqual(container.heading_path, ["第一节 概览"])
        self.assertEqual(
            [part["text"] for part in container.payload["parts"]],
            ["一、甲事项\n二、乙事项", "某某股份有限公司2024年年度报告"],
        )
        self.assertNotIn("heading_path", container.payload["parts"][1])
        self.assertEqual(
            physical_context(gap)["predecessor"]["source"]["source_item_index"],
            2,
        )
        self.assertEqual(
            physical_context(gap)["anchor_heading_path"],
            container.heading_path,
        )
        self.assertEqual(gap.heading_path, [])
        self.assert_audit_ok(report)

    def test_unanchored_gap_never_borrows_the_section_that_follows(
        self,
    ) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                NativeAtom(text="文首原生", page=0, word=0),
                MappedAtom(carrier=0, page=0, word=1, block=0),
                MappedAtom(carrier=1, page=0, word=2, block=1),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={
                0: carrier(text="第一节 概览", heading=True),
                1: carrier(text="一、甲事项\n二、乙事项", raw_kind="list"),
            },
            headings=(heading_node(1, 0, text="第一节 概览", section_end=1),),
        )

        drafts, _stats, report = publish(document, proof)

        gap, section = drafts
        self.assertEqual(section.heading_path, ["第一节 概览"])
        self.assertIsNone(physical_context(gap)["predecessor"])
        self.assertEqual(physical_context(gap)["anchor_heading_path"], [])
        self.assert_audit_ok(report)


class NativeGapPunctuationSuppressionTests(PublicationCase):
    """Pure leader/placeholder runs stay in the ledger, not in units.

    TOC dot leaders and empty-cell dashes are marks the provider omitted
    deliberately; publishing them as recovery units adds retrieval noise
    with no evidence value. The audit re-derives the predicate from its
    own partition, so absence is enforced from both sides.
    """

    def test_leader_punctuation_run_is_suppressed(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0),),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="……——……", page=0, word=1),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={0: carrier(text="一、甲事项\n二、乙事项", raw_kind="list")},
        )

        drafts, stats, report = publish(document, proof)

        self.assertEqual([draft.payload_kind for draft in drafts], ["text"])
        self.assertEqual(stats.punctuation_only_native_runs, 1)
        self.assert_audit_ok(report)

    def test_a_run_with_any_content_character_still_publishes(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0),),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="-7,835.44", page=0, word=1),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={0: carrier(text="一、甲事项\n二、乙事项", raw_kind="list")},
        )

        drafts, stats, report = publish(document, proof)

        self.assertEqual(stats.punctuation_only_native_runs, 0)
        gap = drafts[-1]
        self.assertEqual(gap_texts(gap), ["-7,835.44"])
        self.assert_audit_ok(report)


class NativeGapCoverageFlagTests(PublicationCase):
    """A containment-proven recovery denies its owner a silent ``ok``.

    Words proven to fall inside a carrier element's span without being
    covered by its payload mean that carrier lost content there — e.g. a
    dropped wrapped minus sign — so the owning unit must publish marked
    for review.  Anchors proving only page adjacency say nothing about
    the carrier's interior and must not flag it.
    """

    def _containment_case(
        self,
    ) -> tuple[dict[str, Any], SourceEvidenceProof]:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0), Element(index=1, page=0)),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="表内失落", page=0, word=1),
                MappedAtom(carrier=0, page=0, word=2, block=0),
                MappedAtom(carrier=1, page=0, word=3, block=1),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={
                0: carrier(text="一、甲事项\n二、乙事项", raw_kind="list"),
                1: carrier(text="三、丙事项\n四、丁事项", raw_kind="list"),
            },
        )
        return document, proof

    def test_containment_gap_marks_its_owner_unit_for_review(self) -> None:
        document, proof = self._containment_case()

        drafts, _stats, report = publish(document, proof)

        owner, gap, neighbor = drafts
        self.assertEqual(physical_context(gap)["relation"], "bounded_by_same_source")
        self.assertEqual(physical_context(gap)["containment_owner"], 0)
        self.assertEqual(owner.quality_status, "needs_review")
        # Only the proven-lossy carrier is flagged, not the page's neighbors.
        self.assertEqual(neighbor.quality_status, "ok")
        self.assert_audit_ok(report)

    def test_page_adjacency_anchor_leaves_its_carrier_ok(self) -> None:
        normalized_ir, proof = build_case(
            page_count=1,
            elements=(Element(index=0, page=0),),
            atoms=(
                MappedAtom(carrier=0, page=0, word=0, block=0),
                NativeAtom(text="页尾原生", page=0, word=1),
            ),
        )
        document = document_ir(
            normalized_ir,
            carriers={0: carrier(text="一、甲事项\n二、乙事项", raw_kind="list")},
        )

        drafts, _stats, report = publish(document, proof)

        anchor_carrier, gap = drafts
        self.assertEqual(physical_context(gap)["relation"], "page_suffix")
        self.assertIsNone(physical_context(gap)["containment_owner"])
        self.assertEqual(anchor_carrier.quality_status, "ok")
        self.assert_audit_ok(report)

    def test_forged_ok_on_a_coverage_gap_owner_fails_the_audit(self) -> None:
        document, proof = self._containment_case()
        drafts, stats, report = publish(document, proof)
        self.assert_audit_ok(report)
        self.assertEqual(drafts[0].quality_status, "needs_review")

        forged = audit_document(
            normalized_ir=document,
            units=(
                AuditUnitView(
                    order_index=index,
                    payload_kind=draft.payload_kind,
                    payload=draft.payload,
                    title=draft.title,
                    heading_path=draft.heading_path,
                    semantic_key=draft.semantic_key,
                    semantic_keys=draft.semantic_keys,
                    quality_status="ok" if index == 1 else draft.quality_status,
                    applicability=draft.applicability,
                    artifact_locator=draft.artifact_locator,
                )
                for index, draft in enumerate(drafts, start=1)
            ),
            metadata=AuditDocumentMetadata(
                document_id=_DOCUMENT_ID,
                title="排序样本",
                filing_type="other",
            ),
            source_proof=proof,
            source_dispositions=stats.source_dispositions,
            image_hashes={},
        )

        self.assertFalse(forged.ok)
        self.assertIn(
            "source_coverage_gap_unflagged",
            [finding.code for finding in forged.findings],
        )


if __name__ == "__main__":
    unittest.main()
