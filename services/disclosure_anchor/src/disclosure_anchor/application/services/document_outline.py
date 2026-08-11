"""Deterministic heading placement and content-conserving coarse units."""

from __future__ import annotations

from collections.abc import Iterable
import re

from disclosure_anchor.application.contracts.document_outline import (
    CoarseUnit,
    DocumentOutline,
    HeadingCandidate,
    HeadingDispositionReason,
    HeadingLevelHint,
    HeadingNegativeHint,
    HeadingPlacementSource,
    ResolvedHeading,
)
from disclosure_anchor.application.contracts.provider_document import (
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
)


_CHINESE_NUMBER = "一二三四五六七八九十百千万零〇两"
_CHAPTER_RE = re.compile(rf"^第[{_CHINESE_NUMBER}0-9]+(?P<kind>[编篇部章节条])")
_CHINESE_ORDINAL_RE = re.compile(rf"^[{_CHINESE_NUMBER}]+、")
_PAREN_CHINESE_RE = re.compile(rf"^[（(][{_CHINESE_NUMBER}]+[）)]")
_DOTTED_ARABIC_RE = re.compile(r"^(?P<number>[0-9]+(?:\.[0-9]+)+)(?:[、.]|\s|$)")
_ARABIC_ORDINAL_RE = re.compile(r"^[0-9]+[、.]")
_PAREN_ARABIC_RE = re.compile(r"^(?:[（(][0-9]+[）)]|[0-9]+[）)])")
_LETTER_ORDINAL_RE = re.compile(r"^[A-Za-z][.)、]")
_BOX_MARKERS = ("□", "☐", "☑", "\uf052")
_CHECKBOX_MARKERS = (*_BOX_MARKERS, "√")
_TABLE_CONTAINMENT_TOLERANCE = 1.0
_HINT_PRIORITY = {"bookmark": 0, "printed_toc": 1, "pdf_style": 2}


def build_document_outline(
    document: ProviderDocument,
    *,
    level_hints: Iterable[HeadingLevelHint] = (),
    negative_hints: Iterable[HeadingNegativeHint] = (),
) -> DocumentOutline:
    """Resolve only source-bound candidates and partition every provider block."""

    level_by_index = _resolved_level_hints(document, level_hints)
    negative_indices = _negative_hint_indices(document, negative_hints)
    candidates = tuple(
        candidate
        for block in document.blocks
        if (
            candidate := _heading_candidate(
                document,
                block,
                level_hint=level_by_index.get(block.source_index),
                externally_negative=block.source_index in negative_indices,
            )
        )
        is not None
    )
    headings = _resolve_headings(candidates)
    units = _coarse_units(document, headings)
    return DocumentOutline(
        source_pdf_sha256=document.source_pdf_sha256,
        provider_bundle_sha256=document.bundle_sha256,
        block_count=len(document.blocks),
        candidates=candidates,
        headings=headings,
        units=units,
    )


def _resolved_level_hints(
    document: ProviderDocument,
    hints: Iterable[HeadingLevelHint],
) -> dict[int, HeadingLevelHint]:
    blocks_by_index = {block.source_index: block for block in document.blocks}
    by_index: dict[int, dict[str, HeadingLevelHint]] = {}
    for hint in hints:
        block = blocks_by_index.get(hint.source_index)
        if block is None:
            raise ValueError("heading hint references an unknown provider block")
        if (
            hint.source_pdf_sha256 != document.source_pdf_sha256
            or hint.raw_block_sha256 != block.raw_item_sha256
        ):
            raise ValueError("heading hint does not bind the exact source block")
        hints_by_source = by_index.setdefault(hint.source_index, {})
        if hint.source in hints_by_source:
            raise ValueError(
                "provider block has duplicate heading hints from one source"
            )
        hints_by_source[hint.source] = hint
    return {
        source_index: min(
            hints_by_source.values(),
            key=lambda hint: _HINT_PRIORITY[hint.source],
        )
        for source_index, hints_by_source in by_index.items()
    }


def _negative_hint_indices(
    document: ProviderDocument,
    hints: Iterable[HeadingNegativeHint],
) -> set[int]:
    blocks_by_index = {block.source_index: block for block in document.blocks}
    result: set[int] = set()
    for hint in hints:
        block = blocks_by_index.get(hint.source_index)
        if block is None:
            raise ValueError("heading negative references an unknown provider block")
        if (
            hint.source_pdf_sha256 != document.source_pdf_sha256
            or hint.raw_block_sha256 != block.raw_item_sha256
        ):
            raise ValueError("heading negative does not bind the exact source block")
        if hint.source_index in result:
            raise ValueError("provider block has duplicate heading negatives")
        result.add(hint.source_index)
    return result


def _heading_candidate(
    document: ProviderDocument,
    block: ProviderBlock,
    *,
    level_hint: HeadingLevelHint | None,
    externally_negative: bool,
) -> HeadingCandidate | None:
    if block.provider_type.casefold() != "text":
        return None
    annotation = (block.typed_annotation or "").casefold()
    provider_title = annotation == "title"
    provider_level_fallback = (
        block.typed_annotation is None
        and block.provider_level is not None
        and block.provider_level > 0
    )
    outline_hint = level_hint is not None and level_hint.source != "pdf_style"
    if not outline_hint and not provider_title and not provider_level_fallback:
        return None
    text = _block_text(block)
    if not text.strip():
        return None
    heading_id = f"heading:{block.source_index:08d}"
    numbering_family, numbering_rank = _numbering(text)
    disposition_reason = _demotion_reason(
        document,
        block,
        text=text,
        externally_negative=externally_negative,
    )
    if disposition_reason is not None:
        return HeadingCandidate(
            heading_id=heading_id,
            source_index=block.source_index,
            page_index=block.page_index,
            bbox=block.bbox,
            text=text,
            raw_block_sha256=block.raw_item_sha256,
            provider_level=block.provider_level,
            numbering_family=numbering_family,
            nominal_rank=None,
            disposition="demoted",
            disposition_reason=disposition_reason,
            placement_source=None,
        )
    nominal_rank, placement_source = _placement(
        block,
        level_hint=level_hint,
        numbering_rank=numbering_rank,
    )
    return HeadingCandidate(
        heading_id=heading_id,
        source_index=block.source_index,
        page_index=block.page_index,
        bbox=block.bbox,
        text=text,
        raw_block_sha256=block.raw_item_sha256,
        provider_level=block.provider_level,
        numbering_family=numbering_family,
        nominal_rank=nominal_rank,
        disposition="accepted",
        disposition_reason="accepted",
        placement_source=placement_source,
    )


def _block_text(block: ProviderBlock) -> str:
    for payload in block.payloads:
        if payload.field in {"text", "content"} and payload.text:
            return payload.text
    return ""


def _demotion_reason(
    document: ProviderDocument,
    block: ProviderBlock,
    *,
    text: str,
    externally_negative: bool,
) -> HeadingDispositionReason | None:
    if externally_negative:
        return "page_continuation"
    if _inside_physical_table(document, block):
        return "table_contained"
    box_count = sum(text.count(marker) for marker in _BOX_MARKERS)
    marker_count = sum(text.count(marker) for marker in _CHECKBOX_MARKERS)
    if box_count >= 1 and marker_count >= 2:
        return "checkbox_selector"
    return None


def _inside_physical_table(
    document: ProviderDocument,
    block: ProviderBlock,
) -> bool:
    if block.bbox is None:
        return False
    return any(
        segment.page_index == block.page_index
        and segment.bbox is not None
        and _contains(segment.bbox, block.bbox)
        for segment in document.physical_table_segments
    )


def _contains(outer: ProviderBBox, inner: ProviderBBox) -> bool:
    tolerance = _TABLE_CONTAINMENT_TOLERANCE
    return (
        outer.x0 - tolerance <= inner.x0
        and outer.y0 - tolerance <= inner.y0
        and outer.x1 + tolerance >= inner.x1
        and outer.y1 + tolerance >= inner.y1
    )


def _placement(
    block: ProviderBlock,
    *,
    level_hint: HeadingLevelHint | None,
    numbering_rank: int | None,
) -> tuple[int, HeadingPlacementSource]:
    if level_hint is not None and level_hint.source != "pdf_style":
        return level_hint.level, level_hint.source
    if numbering_rank is not None:
        return numbering_rank, "numbering"
    if level_hint is not None:
        return level_hint.level, level_hint.source
    if block.provider_level is not None and block.provider_level > 0:
        return block.provider_level, "provider"
    return 1, "flattened"


def _numbering(text: str) -> tuple[str | None, int | None]:
    prefix = text.lstrip()
    if match := _CHAPTER_RE.match(prefix):
        kind = match.group("kind")
        if kind in {"编", "篇", "部", "章"}:
            return f"第X{kind}", 1
        if kind == "节":
            return "第X节", 2
        return "第X条", 3
    if _CHINESE_ORDINAL_RE.match(prefix):
        return "中文序号", 3
    if _PAREN_CHINESE_RE.match(prefix):
        return "括号中文序号", 4
    if match := _DOTTED_ARABIC_RE.match(prefix):
        depth = min(match.group("number").count(".") + 5, 9)
        return "点分阿拉伯序号", depth
    if _ARABIC_ORDINAL_RE.match(prefix):
        return "阿拉伯序号", 5
    if _PAREN_ARABIC_RE.match(prefix):
        return "括号阿拉伯序号", 6
    if _LETTER_ORDINAL_RE.match(prefix):
        return "字母序号", 6
    return None, None


def _resolve_headings(
    candidates: tuple[HeadingCandidate, ...],
) -> tuple[ResolvedHeading, ...]:
    resolved: list[ResolvedHeading] = []
    stack: list[ResolvedHeading] = []
    for candidate in candidates:
        if candidate.disposition != "accepted":
            continue
        nominal_rank = candidate.nominal_rank
        placement_source = candidate.placement_source
        if nominal_rank is None or placement_source is None:
            raise ValueError("accepted heading has no placement")
        parent_eligible = placement_source not in {"provider", "flattened"}
        if parent_eligible:
            while stack and stack[-1].nominal_rank >= nominal_rank:
                stack.pop()
        parent = stack[-1] if stack else None
        heading = ResolvedHeading(
            heading_id=candidate.heading_id,
            source_index=candidate.source_index,
            page_index=candidate.page_index,
            bbox=candidate.bbox,
            text=candidate.text,
            nominal_rank=nominal_rank,
            level=1 if parent is None else parent.level + 1,
            parent_heading_id=None if parent is None else parent.heading_id,
            headpath=(candidate.text,)
            if parent is None
            else (*parent.headpath, candidate.text),
            placement_source=placement_source,
        )
        resolved.append(heading)
        if parent_eligible:
            stack.append(heading)
    return tuple(resolved)


def _coarse_units(
    document: ProviderDocument,
    headings: tuple[ResolvedHeading, ...],
) -> tuple[CoarseUnit, ...]:
    if not document.blocks:
        return ()
    heading_by_source = {heading.source_index: heading for heading in headings}
    pending_indices: list[int] = []
    pending_heading: ResolvedHeading | None = None
    units: list[CoarseUnit] = []

    def flush() -> None:
        nonlocal pending_indices, pending_heading
        if not pending_indices:
            return
        units.append(
            CoarseUnit(
                unit_index=len(units),
                heading_id=None
                if pending_heading is None
                else pending_heading.heading_id,
                title=None if pending_heading is None else pending_heading.text,
                headpath=() if pending_heading is None else pending_heading.headpath,
                block_source_indices=tuple(pending_indices),
            )
        )
        pending_indices = []

    for block in document.blocks:
        heading = heading_by_source.get(block.source_index)
        if heading is not None:
            flush()
            pending_heading = heading
        pending_indices.append(block.source_index)
    flush()
    return tuple(units)


__all__ = ["build_document_outline"]
