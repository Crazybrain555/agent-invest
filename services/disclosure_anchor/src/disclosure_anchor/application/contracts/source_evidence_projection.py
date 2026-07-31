"""Pure projection of parser-neutral source proof into unit evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from disclosure_anchor.application.contracts.source_evidence import (
    GeometryIssueEvent,
    MappedSourceEvent,
    NativeTextEvent,
    SourceEvidenceProof,
)
from disclosure_anchor.application.contracts.source_evidence_occurrence import (
    SourceMappedAnchor as NativeMappedAnchor,
    SourceNativeOccurrence as NativeEvidenceOccurrence,
    SourceOccurrenceIdentityError,
    geometry_issue_occurrence,
    mapped_source_anchor,
    native_text_occurrence,
    visual_page_occurrence,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    SOURCE_EVIDENCE_VISUAL_PAGE_KIND,
)


class SourceEvidenceProjectionError(ValueError):
    """Typed source facts cannot be projected onto public evidence units."""


@dataclass(frozen=True, slots=True)
class NativeEvidenceGap:
    page_idx: int
    word_order_span: tuple[int, int]
    occurrences: tuple[NativeEvidenceOccurrence, ...]
    predecessor: NativeMappedAnchor | None
    successor: NativeMappedAnchor | None
    relation: Literal[
        "between_mapped_sources",
        "bounded_by_same_source",
        "page_prefix",
        "page_suffix",
        "page_only",
    ]


def native_gap_search_atoms(
    gap: NativeEvidenceGap,
) -> list[dict[str, Any]]:
    """Project only retrieval runs proved by the typed source proof."""

    entries: list[dict[str, Any]] = []
    closed_runs: set[tuple[int, int]] = set()
    active_run: tuple[int, int] | None = None
    for part_index, occurrence in enumerate(gap.occurrences):
        if occurrence.text is None:
            continue
        run_identity = occurrence.retrieval_run
        boundary: dict[str, Any] = {
            "kind": "source_occurrence_singleton"
        }
        if run_identity is not None:
            page_idx, run_index = run_identity
            boundary = {
                "kind": "source_evidence_run",
                "source_evidence_sha256": occurrence.source_ref[
                    "source_evidence_sha256"
                ],
                "page_idx": page_idx,
                "run_index": run_index,
            }
        target = f"payload.parts.{part_index}.text"
        if run_identity is not None and run_identity == active_run:
            target_fields = entries[-1]["target_fields"]
            if not isinstance(target_fields, list):
                raise SourceEvidenceProjectionError(
                    "native search target projection is invalid"
                )
            target_fields.append(target)
            continue
        if active_run is not None:
            closed_runs.add(active_run)
        if run_identity is not None and run_identity in closed_runs:
            raise SourceEvidenceProjectionError(
                "one retrieval run is split across native gap parts"
            )
        active_run = run_identity
        entries.append(
            {
                "boundary": boundary,
                "target_fields": [target],
                "transform": "exact_concat.v1",
            }
        )
    return entries


def native_gap_physical_context(
    gap: NativeEvidenceGap,
    *,
    order_basis: str,
    containment_owner: int | None,
    page_order_basis: str,
) -> dict[str, Any]:
    """Serialize the proven canonical-stream placement of one native gap."""

    first_ref = gap.occurrences[0].source_ref
    return {
        "version": "source-native-placement.v2",
        "scope": "native_gap",
        "source_evidence_sha256": first_ref["source_evidence_sha256"],
        "source_pdf_sha256": first_ref["source_pdf_sha256"],
        "page_idx": gap.page_idx,
        "page_no": gap.page_idx + 1,
        "word_order_span": list(gap.word_order_span),
        "predecessor": (
            gap.predecessor.as_dict()
            if gap.predecessor is not None
            else None
        ),
        "successor": (
            gap.successor.as_dict()
            if gap.successor is not None
            else None
        ),
        "relation": gap.relation,
        "order_basis": order_basis,
        "containment_owner": containment_owner,
        "page_order_basis": page_order_basis,
    }



def native_evidence_occurrences(
    normalized_ir: Mapping[str, Any],
    proof: SourceEvidenceProof,
) -> tuple[NativeEvidenceOccurrence, ...]:
    """Return every typed physical occurrence needing a public payload edge."""

    _validate_proof_identity(normalized_ir, proof)
    run_membership = _retrieval_run_membership(proof)
    output: list[NativeEvidenceOccurrence] = []
    for page in proof.pages:
        for event in page.events:
            if isinstance(event, MappedSourceEvent):
                continue
            if isinstance(event, NativeTextEvent):
                retrieval_run = run_membership.get(event.atom_index)
                if retrieval_run is None:
                    raise SourceEvidenceProjectionError(
                        f"native text atom {event.atom_index} belongs to no "
                        "retrieval run"
                    )
                output.append(
                    native_text_occurrence(
                        proof.identity,
                        page_idx=page.page_idx,
                        event=event,
                        retrieval_run=retrieval_run,
                    )
                )
                continue
            assert isinstance(event, GeometryIssueEvent)
            output.append(
                geometry_issue_occurrence(
                    proof.identity,
                    page_idx=page.page_idx,
                    event=event,
                )
            )
        if page.visual_only is None:
            continue
        artifact = page.visual_only.visual_artifact
        output.append(
            visual_page_occurrence(
                proof.identity,
                page_idx=page.page_idx,
                artifact=artifact,
                semantic_text=page.visual_only.semantic_text,
                semantic_text_sha256=page.visual_only.semantic_text_sha256,
            )
        )
    return tuple(output)


def native_evidence_gaps(
    normalized_ir: Mapping[str, Any],
    proof: SourceEvidenceProof,
) -> tuple[NativeEvidenceGap, ...]:
    """Partition each page into maximal consecutive unmapped event gaps."""

    occurrences = native_evidence_occurrences(normalized_ir, proof)
    occurrence_by_event = {
        (occurrence.page_idx, occurrence.word_order): occurrence
        for occurrence in occurrences
        if occurrence.kind != SOURCE_EVIDENCE_VISUAL_PAGE_KIND
    }
    if len(occurrence_by_event) != sum(
        occurrence.kind != SOURCE_EVIDENCE_VISUAL_PAGE_KIND
        for occurrence in occurrences
    ):
        raise SourceEvidenceProjectionError(
            "source-native occurrence page/order is duplicated"
        )
    elements = _elements_by_source(normalized_ir)
    gaps: list[NativeEvidenceGap] = []
    for page in proof.pages:
        predecessor: NativeMappedAnchor | None = None
        pending: list[NativeEvidenceOccurrence] = []

        def flush(successor: NativeMappedAnchor | None) -> None:
            if not pending:
                return
            gaps.append(
                NativeEvidenceGap(
                    page_idx=page.page_idx,
                    word_order_span=(
                        pending[0].word_order,
                        pending[-1].word_order + 1,
                    ),
                    occurrences=tuple(pending),
                    predecessor=predecessor,
                    successor=successor,
                    relation=_gap_relation(predecessor, successor),
                )
            )
            pending.clear()

        for event in page.events:
            if isinstance(event, MappedSourceEvent):
                anchor = _mapped_anchor(
                    page_idx=page.page_idx,
                    event=event,
                    elements=elements,
                )
                flush(anchor)
                predecessor = anchor
                continue
            occurrence = occurrence_by_event.get(
                (page.page_idx, event.word_order)
            )
            if occurrence is None:
                raise SourceEvidenceProjectionError(
                    "typed source event has no projected occurrence"
                )
            pending.append(occurrence)
        flush(None)
        if page.visual_only is not None:
            visual = [
                occurrence
                for occurrence in occurrences
                if occurrence.page_idx == page.page_idx
                and occurrence.kind == SOURCE_EVIDENCE_VISUAL_PAGE_KIND
            ]
            if len(visual) != 1:
                raise SourceEvidenceProjectionError(
                    "visual-only page does not have one physical occurrence"
                )
            gaps.append(
                NativeEvidenceGap(
                    page_idx=page.page_idx,
                    word_order_span=(0, 0),
                    occurrences=(visual[0],),
                    predecessor=None,
                    successor=None,
                    relation="page_only",
                )
            )
    if sum(len(gap.occurrences) for gap in gaps) != len(occurrences):
        raise SourceEvidenceProjectionError(
            "source-native gaps do not exactly cover occurrences"
        )
    return tuple(gaps)



def _validate_proof_identity(
    normalized_ir: Mapping[str, Any],
    proof: SourceEvidenceProof,
) -> None:
    if (
        normalized_ir.get("source_pdf_sha256")
        != proof.identity.source_pdf_sha256
        or normalized_ir.get("source_pdf_page_count")
        != proof.identity.page_count
    ):
        raise SourceEvidenceProjectionError(
            "typed source proof differs from NormalizedIR PDF identity"
        )


def _elements_by_source(
    normalized_ir: Mapping[str, Any],
) -> dict[int, Mapping[str, Any]]:
    raw_elements = normalized_ir.get("elements")
    if not isinstance(raw_elements, list):
        raise SourceEvidenceProjectionError(
            "NormalizedIR elements are not an array"
        )
    output: dict[int, Mapping[str, Any]] = {}
    for raw_element in raw_elements:
        if not isinstance(raw_element, Mapping):
            raise SourceEvidenceProjectionError(
                "NormalizedIR element is invalid"
            )
        source_index = raw_element.get("source_item_index")
        if not _index(source_index) or source_index in output:
            raise SourceEvidenceProjectionError(
                "NormalizedIR source item identity is invalid"
            )
        output[cast(int, source_index)] = raw_element
    return output


def _mapped_anchor(
    *,
    page_idx: int,
    event: MappedSourceEvent,
    elements: Mapping[int, Mapping[str, Any]],
) -> NativeMappedAnchor:
    element = elements.get(event.source_item_index)
    if element is None:
        raise SourceEvidenceProjectionError(
            "mapped source event has no NormalizedIR carrier"
        )
    try:
        return mapped_source_anchor(
            page_idx=page_idx,
            atom_index=event.atom_index,
            word_order=event.word_order,
            source_item_index=event.source_item_index,
            order_state=event.order_state,
            element=element,
        )
    except SourceOccurrenceIdentityError as exc:
        raise SourceEvidenceProjectionError(str(exc)) from exc


def _retrieval_run_membership(
    proof: SourceEvidenceProof,
) -> dict[int, tuple[int, int]]:
    output: dict[int, tuple[int, int]] = {}
    for run in proof.retrieval_runs:
        for atom_index in run.atom_indices:
            output[atom_index] = (run.page_idx, run.run_index)
    return output


def _gap_relation(
    predecessor: NativeMappedAnchor | None,
    successor: NativeMappedAnchor | None,
) -> Literal[
    "between_mapped_sources",
    "bounded_by_same_source",
    "page_prefix",
    "page_suffix",
    "page_only",
]:
    if predecessor is not None and successor is not None:
        if predecessor.source_ref == successor.source_ref:
            return "bounded_by_same_source"
        return "between_mapped_sources"
    if successor is not None:
        return "page_prefix"
    if predecessor is not None:
        return "page_suffix"
    return "page_only"


def _index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = [
    "NativeEvidenceGap",
    "NativeEvidenceOccurrence",
    "NativeMappedAnchor",
    "SourceEvidenceProjectionError",
    "native_evidence_gaps",
    "native_evidence_occurrences",
    "native_gap_physical_context",
    "native_gap_search_atoms",
]
