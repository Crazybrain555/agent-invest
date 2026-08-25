"""Native PDF text observations constrained to provider-owned rectangles."""

from __future__ import annotations

import math
from pathlib import Path

import pypdfium2 as pdfium

from disclosure_anchor.adapters.parsers.pdfium_runtime import PDFIUM_LOCK
from disclosure_anchor.application.contracts.provider_document import (
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfTextObservation,
)


_PAGE_SHAPE_REL_TOLERANCE = 0.01
_AMBIGUOUS_BBOX_COVERAGE = 0.9


def observe_pdf_text_rectangles(
    path: Path,
    *,
    document: ProviderDocument,
) -> tuple[SourcePdfTextObservation, ...]:
    """Read native text only within exact MinerU text-block rectangles."""

    result: list[SourcePdfTextObservation] = []
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            if len(pdf) != len(document.pages):
                raise ValueError("source PDF page count differs from provider document")
            for provider_page in document.pages:
                page = pdf[provider_page.page_index]
                try:
                    if page.get_rotation() != 0:
                        continue
                    left, bottom, right, top = page.get_bbox()
                    width = right - left
                    height = top - bottom
                    provider_width, provider_height = provider_page.page_size
                    if (
                        not all(
                            math.isfinite(value)
                            for value in (left, bottom, right, top, width, height)
                        )
                        or width <= 0
                        or height <= 0
                        or not math.isclose(
                            width / height,
                            provider_width / provider_height,
                            rel_tol=_PAGE_SHAPE_REL_TOLERANCE,
                        )
                    ):
                        continue
                    eligible = _eligible_text_blocks(provider_page.blocks)
                    ambiguous = _ambiguous_source_indices(
                        eligible,
                        blockers=provider_page.blocks,
                    )
                    text_page = page.get_textpage()
                    try:
                        for block, payload_ordinal in eligible:
                            if block.source_index in ambiguous:
                                continue
                            assert block.bbox is not None
                            bbox = block.bbox
                            text = text_page.get_text_bounded(
                                left=left + bbox.x0 / 1_000.0 * width,
                                bottom=top - bbox.y1 / 1_000.0 * height,
                                right=left + bbox.x1 / 1_000.0 * width,
                                top=top - bbox.y0 / 1_000.0 * height,
                            )
                            if text.strip():
                                result.append(
                                    SourcePdfTextObservation(
                                        source_index=block.source_index,
                                        page_index=block.page_index,
                                        payload_ordinal=payload_ordinal,
                                        raw_block_sha256=block.raw_item_sha256,
                                        text=text,
                                    )
                                )
                    finally:
                        text_page.close()
                finally:
                    page.close()
        finally:
            pdf.close()
    return tuple(result)


def _eligible_text_blocks(
    blocks: tuple[ProviderBlock, ...],
) -> tuple[tuple[ProviderBlock, int], ...]:
    eligible: list[tuple[ProviderBlock, int]] = []
    for block in blocks:
        if block.provider_type not in {"text", "table"} or block.bbox is None:
            continue
        expected_field = "text" if block.provider_type == "text" else "table_body"
        ordinals = tuple(
            ordinal
            for ordinal, payload in enumerate(block.payloads)
            if payload.field == expected_field
        )
        if len(ordinals) == 1:
            eligible.append((block, ordinals[0]))
    return tuple(eligible)


def _ambiguous_source_indices(
    blocks: tuple[tuple[ProviderBlock, int], ...],
    *,
    blockers: tuple[ProviderBlock, ...],
) -> frozenset[int]:
    ambiguous: set[int] = set()
    for left, _ordinal in blocks:
        assert left.bbox is not None
        for right in blockers:
            if (
                right.source_index == left.source_index
                or right.provider_type not in {"text", "table"}
                or right.bbox is None
                or _bbox_coverage(left.bbox, right.bbox)
                < _AMBIGUOUS_BBOX_COVERAGE
            ):
                continue
            if (
                left.provider_type == right.provider_type
                or left.provider_type == "text"
            ):
                ambiguous.add(left.source_index)
    return frozenset(ambiguous)


def _bbox_coverage(left: ProviderBBox, right: ProviderBBox) -> float:
    intersection_width = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    intersection_height = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
    intersection = intersection_width * intersection_height
    left_area = (left.x1 - left.x0) * (left.y1 - left.y0)
    right_area = (right.x1 - right.x0) * (right.y1 - right.y0)
    return intersection / min(left_area, right_area)


__all__ = ["observe_pdf_text_rectangles"]
