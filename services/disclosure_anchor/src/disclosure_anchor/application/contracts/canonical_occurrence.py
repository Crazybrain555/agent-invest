"""Page-major canonical occurrence stream, decided once before unit grouping.

Design authority: docs/implementation/design/canonical-occurrence-stream.md.
Every NormalizedIR carrier and every native gap run receives exactly one
position in a single page-major sequence. Position proof is layered:
``native_proven`` (page-linear order witnessed by native word order),
``containment_proven`` (gap bounded by mapped events of one carrier), or
``provider_attested`` (deterministic provider block order at page scope).
Contradictory page identity fails loud; there is no needs-review lane.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from disclosure_anchor.application.contracts.source_evidence import (
    MappedSourceEvent,
    SourceEvidenceProof,
)
from disclosure_anchor.application.contracts.source_evidence_projection import (
    NativeEvidenceGap,
    SourceEvidenceProjectionError,
    native_evidence_gaps,
)

OrderBasis = Literal["native_proven", "containment_proven", "provider_attested"]
PageOrderBasis = Literal["native_proven", "provider_attested"]


@dataclass(frozen=True, slots=True)
class CanonicalStreamEntry:
    kind: Literal["mineru_carrier", "native_gap_run"]
    page_idx: int
    stream_order: int
    order_basis: OrderBasis
    native_span: tuple[int, int] | None
    provider_order: int | None
    source_item_index: int | None
    containment_owner: int | None
    gap: NativeEvidenceGap | None


@dataclass(frozen=True, slots=True)
class PageOrderResolution:
    page_idx: int
    order_basis: PageOrderBasis
    span_overlap_count: int
    order_conflict_count: int


@dataclass(frozen=True, slots=True)
class CanonicalOccurrenceStream:
    entries: tuple[CanonicalStreamEntry, ...]
    pages: tuple[PageOrderResolution, ...]


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    kind: Literal["mineru_carrier", "native_gap_run"]
    order_basis: OrderBasis
    native_span: tuple[int, int] | None
    provider_order: int | None
    source_item_index: int | None
    containment_owner: int | None
    gap: NativeEvidenceGap | None


def canonical_occurrence_stream(
    normalized_ir: Mapping[str, Any],
    proof: SourceEvidenceProof,
) -> CanonicalOccurrenceStream:
    """Order every carrier and native gap run exactly once, page-major."""

    element_pages = _element_pages(normalized_ir)
    gaps = native_evidence_gaps(normalized_ir, proof)
    gaps_by_page: dict[int, list[NativeEvidenceGap]] = {}
    for gap in gaps:
        gaps_by_page.setdefault(gap.page_idx, []).append(gap)

    proof_page_indices = {page.page_idx for page in proof.pages}
    carriers_by_page: dict[int, list[int]] = {}
    for source_index in sorted(element_pages):
        carriers_by_page.setdefault(element_pages[source_index], []).append(
            source_index
        )

    entries: list[CanonicalStreamEntry] = []
    pages: list[PageOrderResolution] = []
    stream_order = 0
    all_page_indices = sorted(
        proof_page_indices | set(carriers_by_page) | set(gaps_by_page)
    )
    events_by_page = {page.page_idx: page.events for page in proof.pages}
    for page_idx in all_page_indices:
        mapped: dict[int, list[MappedSourceEvent]] = {}
        conflict_count = 0
        for event in events_by_page.get(page_idx, ()):
            if not isinstance(event, MappedSourceEvent):
                continue
            mapped.setdefault(event.source_item_index, []).append(event)
            if event.order_state == "conflict":
                conflict_count += 1
        spans: dict[int, tuple[int, int]] = {}
        provider_orders: dict[int, int] = {}
        # Page identity between a mapped event and its NormalizedIR carrier is
        # enforced once upstream (mapped_source_anchor inside
        # native_evidence_gaps); this loop deliberately re-checks nothing.
        for source_index, events in mapped.items():
            orders = {event.carrier_order for event in events}
            # A real ledger derives carrier_order and the selector's
            # source_item_index from the same carrier identity, so the two are
            # equal by construction and this set always holds one value; the
            # guard exists for proof bytes rewritten outside that writer.
            if len(orders) != 1:
                raise SourceEvidenceProjectionError(
                    f"carrier {source_index} has inconsistent provider "
                    f"orders: {sorted(orders)}"
                )
            provider_orders[source_index] = orders.pop()
            word_orders = [event.word_order for event in events]
            spans[source_index] = (min(word_orders), max(word_orders) + 1)
        overlap_count = _span_overlap_count(spans)
        page_basis: PageOrderBasis = (
            "provider_attested"
            if overlap_count or conflict_count
            else "native_proven"
        )
        page_gaps = gaps_by_page.get(page_idx, [])
        contained, free_gaps = _split_contained_gaps(page_gaps, spans)
        pending = _page_sequence(
            page_idx=page_idx,
            page_basis=page_basis,
            spans=spans,
            provider_orders=provider_orders,
            unmapped_carriers=[
                source_index
                for source_index in carriers_by_page.get(page_idx, [])
                if source_index not in spans
            ],
            contained=contained,
            free_gaps=free_gaps,
        )
        for item in pending:
            entries.append(
                CanonicalStreamEntry(
                    kind=item.kind,
                    page_idx=page_idx,
                    stream_order=stream_order,
                    order_basis=item.order_basis,
                    native_span=item.native_span,
                    provider_order=item.provider_order,
                    source_item_index=item.source_item_index,
                    containment_owner=item.containment_owner,
                    gap=item.gap,
                )
            )
            stream_order += 1
        pages.append(
            PageOrderResolution(
                page_idx=page_idx,
                order_basis=page_basis,
                span_overlap_count=overlap_count,
                order_conflict_count=conflict_count,
            )
        )

    _validate_conservation(entries, element_pages=element_pages, gaps=gaps)
    return CanonicalOccurrenceStream(entries=tuple(entries), pages=tuple(pages))


def _element_pages(normalized_ir: Mapping[str, Any]) -> dict[int, int]:
    elements = normalized_ir.get("elements")
    if not isinstance(elements, list):
        raise SourceEvidenceProjectionError(
            "NormalizedIR elements are missing or not a list"
        )
    pages: dict[int, int] = {}
    for element in elements:
        if not isinstance(element, Mapping):
            raise SourceEvidenceProjectionError(
                "NormalizedIR element is not a mapping"
            )
        source_index = element.get("source_item_index")
        page_no = element.get("page_no")
        if (
            not isinstance(source_index, int)
            or isinstance(source_index, bool)
            or not isinstance(page_no, int)
            or isinstance(page_no, bool)
            or page_no < 1
        ):
            raise SourceEvidenceProjectionError(
                "NormalizedIR element lacks integer source identity or page"
            )
        if source_index in pages:
            raise SourceEvidenceProjectionError(
                f"NormalizedIR carrier {source_index} appears twice"
            )
        pages[source_index] = page_no - 1
    return pages


def _span_overlap_count(spans: Mapping[int, tuple[int, int]]) -> int:
    ordered = sorted(spans.values())
    return sum(
        1
        for previous, current in zip(ordered, ordered[1:], strict=False)
        if current[0] < previous[1]
    )


def _split_contained_gaps(
    page_gaps: list[NativeEvidenceGap],
    spans: Mapping[int, tuple[int, int]],
) -> tuple[dict[int, list[NativeEvidenceGap]], list[NativeEvidenceGap]]:
    contained: dict[int, list[NativeEvidenceGap]] = {}
    free_gaps: list[NativeEvidenceGap] = []
    for gap in page_gaps:
        if (
            gap.relation == "bounded_by_same_source"
            and gap.predecessor is not None
            and gap.predecessor.source_item_index in spans
        ):
            contained.setdefault(
                gap.predecessor.source_item_index, []
            ).append(gap)
        else:
            free_gaps.append(gap)
    return contained, free_gaps


def _page_sequence(
    *,
    page_idx: int,
    page_basis: PageOrderBasis,
    spans: Mapping[int, tuple[int, int]],
    provider_orders: Mapping[int, int],
    unmapped_carriers: list[int],
    contained: Mapping[int, list[NativeEvidenceGap]],
    free_gaps: list[NativeEvidenceGap],
) -> list[_PendingEntry]:
    # Carriers always keep the provider content sequence; native word order
    # anchors only where gap runs interleave between them. Reordering the
    # carriers themselves by native span is a wider behavioral decision that
    # stays out of scope until corpus replay evidence justifies it.
    # provider_orders[index] == index for ledger-written proof, so this is the
    # same sequence unmapped carriers are woven into further down.
    carriers = sorted(spans, key=lambda index: provider_orders[index])
    sequence: list[_PendingEntry] = []
    prefix = [gap for gap in free_gaps if gap.relation == "page_prefix"]
    for gap in sorted(prefix, key=lambda gap: gap.word_order_span):
        sequence.append(_gap_entry(gap, page_basis))
    after_carrier: dict[int, list[NativeEvidenceGap]] = {}
    trailing: list[NativeEvidenceGap] = []
    for gap in free_gaps:
        if gap.relation == "page_prefix":
            continue
        predecessor = gap.predecessor
        if predecessor is not None and predecessor.source_item_index in spans:
            after_carrier.setdefault(
                predecessor.source_item_index, []
            ).append(gap)
        else:
            trailing.append(gap)
    for source_index in carriers:
        sequence.append(
            _PendingEntry(
                kind="mineru_carrier",
                order_basis=page_basis,
                native_span=spans[source_index],
                provider_order=provider_orders[source_index],
                source_item_index=source_index,
                containment_owner=None,
                gap=None,
            )
        )
        for gap in sorted(
            after_carrier.get(source_index, []),
            key=lambda gap: gap.word_order_span,
        ):
            sequence.append(_gap_entry(gap, page_basis))
    for gap in sorted(trailing, key=lambda gap: gap.word_order_span):
        sequence.append(_gap_entry(gap, page_basis))

    sequence = _weave_contained(sequence, contained)
    return _weave_unmapped_carriers(
        sequence,
        page_idx=page_idx,
        unmapped_carriers=unmapped_carriers,
    )


def _gap_entry(gap: NativeEvidenceGap, order_basis: OrderBasis) -> _PendingEntry:
    return _PendingEntry(
        kind="native_gap_run",
        order_basis=order_basis,
        native_span=gap.word_order_span,
        provider_order=None,
        source_item_index=None,
        containment_owner=None,
        gap=gap,
    )


def _weave_contained(
    sequence: list[_PendingEntry],
    contained: Mapping[int, list[NativeEvidenceGap]],
) -> list[_PendingEntry]:
    if not contained:
        return sequence
    output: list[_PendingEntry] = []
    seen_owner: set[int] = set()
    for entry in sequence:
        output.append(entry)
        owner = entry.source_item_index
        if (
            entry.kind != "mineru_carrier"
            or owner is None
            or owner not in contained
        ):
            continue
        seen_owner.add(owner)
        for gap in sorted(
            contained[owner], key=lambda gap: gap.word_order_span
        ):
            output.append(
                _PendingEntry(
                    kind="native_gap_run",
                    order_basis="containment_proven",
                    native_span=gap.word_order_span,
                    provider_order=None,
                    source_item_index=None,
                    containment_owner=owner,
                    gap=gap,
                )
            )
    missing = set(contained) - seen_owner
    if missing:
        raise SourceEvidenceProjectionError(
            f"contained gap owners are absent from the page: {sorted(missing)}"
        )
    return output


def _weave_unmapped_carriers(
    sequence: list[_PendingEntry],
    *,
    page_idx: int,
    unmapped_carriers: list[int],
) -> list[_PendingEntry]:
    if not unmapped_carriers:
        return sequence
    mapped_positions = [
        (entry.provider_order, position)
        for position, entry in enumerate(sequence)
        if entry.kind == "mineru_carrier" and entry.provider_order is not None
    ]
    insertions: dict[int, list[_PendingEntry]] = {}
    for source_index in sorted(unmapped_carriers):
        entry = _PendingEntry(
            kind="mineru_carrier",
            order_basis="provider_attested",
            native_span=None,
            provider_order=source_index,
            source_item_index=source_index,
            containment_owner=None,
            gap=None,
        )
        slot = len(sequence)
        for provider_order, position in mapped_positions:
            if provider_order > source_index:
                slot = position
                break
        insertions.setdefault(slot, []).append(entry)
    output: list[_PendingEntry] = []
    for position in range(len(sequence) + 1):
        output.extend(insertions.get(position, ()))
        if position < len(sequence):
            output.append(sequence[position])
    return output


def _validate_conservation(
    entries: list[CanonicalStreamEntry],
    *,
    element_pages: Mapping[int, int],
    gaps: tuple[NativeEvidenceGap, ...],
) -> None:
    carrier_entries = [
        entry.source_item_index
        for entry in entries
        if entry.kind == "mineru_carrier"
    ]
    if sorted(
        index for index in carrier_entries if index is not None
    ) != sorted(element_pages):
        raise SourceEvidenceProjectionError(
            "canonical stream does not cover every carrier exactly once"
        )
    gap_keys = sorted(
        (entry.gap.page_idx, entry.gap.word_order_span, entry.gap.relation)
        for entry in entries
        if entry.gap is not None
    )
    expected_keys = sorted(
        (gap.page_idx, gap.word_order_span, gap.relation) for gap in gaps
    )
    if gap_keys != expected_keys:
        raise SourceEvidenceProjectionError(
            "canonical stream does not cover every native gap exactly once"
        )
    orders = [entry.stream_order for entry in entries]
    if orders != list(range(len(entries))):
        raise SourceEvidenceProjectionError(
            "canonical stream orders are not dense"
        )
