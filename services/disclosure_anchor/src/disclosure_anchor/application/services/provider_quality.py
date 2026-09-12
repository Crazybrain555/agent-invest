"""Existing provider quality predicates with complete source-bound occurrences."""

from __future__ import annotations

import string

from disclosure_anchor.application.contracts.document_outline import ResolvedHeading
from disclosure_anchor.application.contracts.html_visible_text import html_visible_text
from disclosure_anchor.application.contracts.provider_document import ProviderBlock, ProviderDocument
from disclosure_anchor.application.contracts.provider_source_semantics import ProviderSourceSemantics
from disclosure_anchor.application.contracts.provider_table_projection import UnboundProviderTablePart
from disclosure_anchor.application.contracts.provider_unit import ProviderUnitBuildResult, ProviderUnitLocator
from disclosure_anchor.application.contracts.provider_quality import (
    EncodedTextOccurrence, ProviderQualityOccurrence, ProviderUnitQualityAssessment,
    SourceFindingOccurrence, TruncatedTitleOccurrence, UnboundTableOccurrence,
    ordered_quality_occurrences,
)
from disclosure_anchor.application.services.document_outline import build_document_outline


def assess_provider_unit_quality(
    *, document: ProviderDocument, unit_sources: frozenset[int],
    heading: ResolvedHeading | None, locator: ProviderUnitLocator,
) -> ProviderUnitQualityAssessment:
    """Preserve the old needs-review disjunction and its Unit-local scope."""
    occurrences: list[ProviderQualityOccurrence] = [
        SourceFindingOccurrence(locator.unit_index, finding)
        for finding in locator.source_quality_findings
    ]
    occurrences.extend(
        _table_occurrence(document, locator.unit_index, part)
        for part in locator.unbound_table_parts
    )
    if _has_suspected_truncated_markup_title(heading):
        assert heading is not None
        occurrences.append(TruncatedTitleOccurrence(
            locator.unit_index, heading.heading_id, heading.source_fragments,
        ))
    blocks = document.blocks
    for source_index in sorted(unit_sources):
        if _has_suspected_encoded_text(blocks[source_index]):
            occurrences.append(EncodedTextOccurrence(
                locator.unit_index, source_index, blocks[source_index].raw_item_sha256,
            ))
    return ProviderUnitQualityAssessment(
        "needs_review" if occurrences else "ok",
        ordered_quality_occurrences(tuple(occurrences)),
    )


def assess_source_build_quality(
    semantics: ProviderSourceSemantics, build: ProviderUnitBuildResult,
) -> tuple[ProviderQualityOccurrence, ...]:
    """Assess a full source build with the H2 runtime's explicitly empty hints.

    These are data-level occurrences; this function proves no actual file read.
    It never substitutes a candidate build or invents zero counts on failure.
    """
    document = semantics.effective_provider_document
    outline = build_document_outline(document)
    if len(build.units) != len(outline.units):
        raise ValueError("source quality build is incomplete for its outline")
    headings = {item.heading_id: item for item in outline.headings}
    occurrences: list[ProviderQualityOccurrence] = []
    for draft, coarse in zip(build.units, outline.units, strict=True):
        assessment = assess_provider_unit_quality(
            document=document, unit_sources=frozenset(coarse.block_source_indices),
            heading=None if coarse.heading_id is None else headings[coarse.heading_id],
            locator=draft.locator,
        )
        if draft.quality_status != assessment.quality_status:
            raise ValueError("source quality status differs from existing assessment")
        occurrences.extend(assessment.occurrences)
    occurrences.extend(
        _table_occurrence(document, None, part) for part in build.unassigned_table_parts
    )
    return ordered_quality_occurrences(tuple(occurrences))


def _table_occurrence(
    document: ProviderDocument, unit_index: int | None, part: UnboundProviderTablePart,
) -> UnboundTableOccurrence:
    block_index, segment_index = part.part.block_source_index, part.part.physical_segment_index
    block = None if block_index is None else document.blocks[block_index]
    segment = None if segment_index is None else document.physical_table_segments[segment_index]
    return UnboundTableOccurrence(
        unit_index, part,
        None if block is None else block.page_index,
        None if block is None else block.raw_item_sha256,
        None if segment is None else segment.page_index,
        None if segment is None else segment.raw_segment_sha256,
    )


def _has_suspected_encoded_text(block: ProviderBlock) -> bool:
    """Flag improbable ASCII-glyph maps without replacing their source text."""

    for payload in block.payloads:
        if payload.field not in {"text", "content"}:
            continue
        visible = "".join(html_visible_text(payload.text).split())
        if len(visible) < 24 or any("\u4e00" <= char <= "\u9fff" for char in visible):
            continue
        punctuation = [char for char in visible if char in string.punctuation]
        if len(punctuation) / len(visible) >= 0.45 and len(set(punctuation)) >= 12:
            return True
    return False


def _has_suspected_truncated_markup_title(
    heading: ResolvedHeading | None,
) -> bool:
    """Flag a provider title reduced to inline markup and non-CJK residue.

    MinerU can preserve a superscript trademark while dropping the adjacent
    Chinese drug name.  The source scalar remains untouched; this only makes
    the unresolved provider damage visible to downstream review.
    """

    if heading is None:
        return False
    folded = heading.text.casefold()
    if "<sup" not in folded and "<sub" not in folded:
        return False
    visible = html_visible_text(heading.text)
    return not any("\u4e00" <= character <= "\u9fff" for character in visible)
