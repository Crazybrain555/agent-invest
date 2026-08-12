"""Deterministic heading placement and content-conserving coarse units."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
import re
from statistics import median

from disclosure_anchor.application.contracts.html_visible_text import (
    html_visible_text,
)
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
_CHINESE_ORDINAL_RE = re.compile(
    rf"^(?:[{_CHINESE_NUMBER}]+、|[{_CHINESE_NUMBER}]{{1,3}}\s+)"
)
_CHINESE_SEQUENCE_RE = re.compile(rf"^(?P<ordinal>[{_CHINESE_NUMBER}]+)(?:、|\s+)")
_PAREN_CHINESE_RE = re.compile(rf"^[（(][{_CHINESE_NUMBER}]+[）)]")
_DOTTED_ARABIC_RE = re.compile(r"^(?P<number>[0-9]+(?:\.[0-9]+)+)(?:[、.]|\s|$)")
_ARABIC_ORDINAL_RE = re.compile(r"^(?P<ordinal>[0-9]{1,3})(?:[、.]|\s+)")
_PAREN_ARABIC_RE = re.compile(r"^(?:[（(][0-9]+[）)]|[0-9]+[）)])")
_ENGLISH_SECTION_RE = re.compile(
    r"^Section\s+(?:[IVXLCDM]+|[0-9]+)(?:[.:)]|\s|$)",
    re.IGNORECASE,
)
_ROMAN_ORDINAL_RE = re.compile(r"^[IVXLCDM]+[.)](?:\s|$)")
_LOWER_ROMAN_ORDINAL_RE = re.compile(r"^[ivxlcdm]+[.)](?:\s|$)")
_LETTER_ORDINAL_RE = re.compile(r"^[A-Za-z][.)、]")
_BOX_MARKERS = ("□", "☐", "☑", "\uf052")
_CHECKBOX_MARKERS = (*_BOX_MARKERS, "√")
_TABLE_CONTAINMENT_TOLERANCE = 1.0
_HINT_PRIORITY = {"bookmark": 0, "printed_toc": 1, "pdf_style": 2}
_DATE_LINE_RE = re.compile(
    r"^(?:(?:19|20)[0-9]{2}\s*年\s*[0-9]{1,2}\s*月\s*[0-9]{1,2}"
    r"|[〇零一二三四五六七八九十]{4}\s*年\s*[〇零一二三四五六七八九十]{1,3}"
    r"\s*月\s*[〇零一二三四五六七八九十]{1,3})\s*日$"
)
_SHORT_BODY_CONFLICT_MAX_CHARS = 2
_CENTERED_X0 = 400.0
_CENTERED_X1 = 600.0
_RIGHT_ALIGNED_CENTER_X = 600.0
_SEQUENCE_X0_TOLERANCE = 4.0
_SEQUENCE_HEIGHT_TOLERANCE = 2.0
_STYLE_X0_CLUSTER_TOLERANCE = 8.0
_STYLE_MAX_INDENT_STEP = 80.0
_STYLE_MIN_CLUSTER_OCCURRENCES = 3
_STYLE_MIN_HEIGHT_STEP = 2.0
_REPEATED_HEADER_MIN_PAGES = 3
_REPEATED_HEADER_MAX_PAGE_GAP = 3
_REPEATED_HEADER_TOP_FRACTION = 0.25
_REPEATED_HEADER_BBOX_TOLERANCE = 4.0
_NORMALIZED_PAGE_AXIS = 1_000.0


def build_document_outline(
    document: ProviderDocument,
    *,
    level_hints: Iterable[HeadingLevelHint] = (),
    negative_hints: Iterable[HeadingNegativeHint] = (),
) -> DocumentOutline:
    """Resolve only source-bound candidates and partition every provider block."""

    level_by_index = _resolved_level_hints(document, level_hints)
    negative_indices = _negative_hint_indices(document, negative_hints)
    body_text_conflicts = _body_text_conflicts(document)
    repeated_page_headers = _repeated_page_header_indices(document)
    initial_candidates = tuple(
        candidate
        for block in document.blocks
        if (
            candidate := _heading_candidate(
                document,
                block,
                level_hint=level_by_index.get(block.source_index),
                externally_negative=block.source_index in negative_indices,
                body_text_conflicts=body_text_conflicts,
                repeated_page_headers=repeated_page_headers,
                sequence_admitted=False,
            )
        )
        is not None
    )
    sequence_admissions = _numbered_sequence_admissions(
        document,
        initial_candidates,
        externally_negative=negative_indices,
    )
    if sequence_admissions:
        candidates = tuple(
            candidate
            for block in document.blocks
            if (
                candidate := _heading_candidate(
                    document,
                    block,
                    level_hint=level_by_index.get(block.source_index),
                    externally_negative=block.source_index in negative_indices,
                    body_text_conflicts=body_text_conflicts,
                    repeated_page_headers=repeated_page_headers,
                    sequence_admitted=block.source_index in sequence_admissions,
                )
            )
            is not None
        )
    else:
        candidates = initial_candidates
    candidates = _apply_document_numbering_scale(candidates)
    candidates = _apply_front_matter_root_placements(document, candidates)
    candidates = _apply_provider_style_placements(document, candidates)
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
    body_text_conflicts: frozenset[str],
    repeated_page_headers: frozenset[int],
    sequence_admitted: bool,
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
    if (
        not outline_hint
        and not provider_title
        and not provider_level_fallback
        and not sequence_admitted
    ):
        return None
    text = _block_text(block)
    if not text.strip():
        return None
    heading_id = f"heading:{block.source_index:08d}"
    numbering_family, numbering_rank = _numbering(text)
    weak_provider_only = (
        not sequence_admitted
        and not outline_hint
        and numbering_family is None
        and (provider_title or provider_level_fallback)
    )
    provider_only_signal = (
        not sequence_admitted
        and not outline_hint
        and (provider_title or provider_level_fallback)
    )
    disposition_reason = _demotion_reason(
        document,
        block,
        text=text,
        externally_negative=externally_negative,
        body_text_conflicts=body_text_conflicts,
        repeated_page_header=(
            provider_only_signal and block.source_index in repeated_page_headers
        ),
        weak_provider_only=weak_provider_only,
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
    body_text_conflicts: frozenset[str],
    repeated_page_header: bool,
    weak_provider_only: bool,
) -> HeadingDispositionReason | None:
    if externally_negative:
        return "page_continuation"
    if _inside_physical_table(document, block):
        return "table_contained"
    box_count = sum(text.count(marker) for marker in _BOX_MARKERS)
    marker_count = sum(text.count(marker) for marker in _CHECKBOX_MARKERS)
    if box_count >= 1 and marker_count >= 2:
        return "checkbox_selector"
    if repeated_page_header:
        return "repeated_page_header"
    if weak_provider_only:
        if not any(character.isalnum() for character in html_visible_text(text)):
            return "non_semantic_glyph"
        normalized = _normalized_text(text)
        if (
            0 < len(normalized) <= _SHORT_BODY_CONFLICT_MAX_CHARS
            and normalized in body_text_conflicts
            and not _is_horizontally_centered(block)
        ):
            return "body_text_conflict"
        if _is_terminal_signature(document, block):
            return "terminal_signature"
    return None


def _body_text_conflicts(document: ProviderDocument) -> frozenset[str]:
    return frozenset(
        normalized
        for block in document.blocks
        if not _has_provider_heading_signal(block)
        and (normalized := _normalized_text(_block_text(block)))
    )


def _repeated_page_header_indices(document: ProviderDocument) -> frozenset[int]:
    """Find provider titles that repeat as page-leading furniture.

    This is intentionally occurrence- and geometry-bound.  Repeating a phrase
    elsewhere in the document is not enough; the same unnumbered provider
    title must occupy the same top-page frame on at least three distinct pages.
    """

    grouped: dict[str, list[ProviderBlock]] = {}
    for block in document.blocks:
        if (
            not _has_provider_heading_signal(block)
            or block.provider_type.casefold() != "text"
            or block.bbox is None
            or block.bbox.y1 > _NORMALIZED_PAGE_AXIS * _REPEATED_HEADER_TOP_FRACTION
        ):
            continue
        normalized = _normalized_text(_block_text(block))
        if normalized:
            grouped.setdefault(normalized, []).append(block)

    repeated: set[int] = set()
    for occurrences in grouped.values():
        clusters: list[list[ProviderBlock]] = []
        for block in occurrences:
            for cluster in clusters:
                anchor = cluster[0]
                assert anchor.bbox is not None and block.bbox is not None
                if _bbox_nearly_equal(anchor.bbox, block.bbox):
                    cluster.append(block)
                    break
            else:
                clusters.append([block])
        for cluster in clusters:
            ordered = sorted(cluster, key=lambda item: item.page_index)
            page_indices = [block.page_index for block in ordered]
            if len(set(page_indices)) >= _REPEATED_HEADER_MIN_PAGES and all(
                later - earlier <= _REPEATED_HEADER_MAX_PAGE_GAP
                for earlier, later in zip(page_indices, page_indices[1:], strict=False)
            ):
                repeated.update(
                    block.source_index
                    for block in sorted(cluster, key=lambda item: item.source_index)[1:]
                )
    return frozenset(repeated)


def _bbox_nearly_equal(first: ProviderBBox, second: ProviderBBox) -> bool:
    tolerance = _REPEATED_HEADER_BBOX_TOLERANCE
    return all(
        abs(left - right) <= tolerance
        for left, right in zip(
            (first.x0, first.y0, first.x1, first.y1),
            (second.x0, second.y0, second.x1, second.y1),
            strict=True,
        )
    )


def _has_provider_heading_signal(block: ProviderBlock) -> bool:
    return (block.typed_annotation or "").casefold() == "title" or (
        block.typed_annotation is None
        and block.provider_level is not None
        and block.provider_level > 0
    )


def _normalized_text(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _is_horizontally_centered(block: ProviderBlock) -> bool:
    if block.bbox is None:
        return False
    center = (block.bbox.x0 + block.bbox.x1) / 2.0
    return _CENTERED_X0 <= center <= _CENTERED_X1


def _is_terminal_signature(
    document: ProviderDocument,
    block: ProviderBlock,
) -> bool:
    if block.page_index != len(document.pages) - 1 or block.bbox is None:
        return False
    block_center = (block.bbox.x0 + block.bbox.x1) / 2.0
    if block_center < _RIGHT_ALIGNED_CENTER_X:
        return False
    next_index = block.source_index + 1
    if next_index >= len(document.blocks):
        return False
    following = document.blocks[next_index]
    if following.page_index != block.page_index or following.bbox is None:
        return False
    following_center = (following.bbox.x0 + following.bbox.x1) / 2.0
    return (
        following_center >= _RIGHT_ALIGNED_CENTER_X
        and following.bbox.y0 >= block.bbox.y0
        and _DATE_LINE_RE.fullmatch(_block_text(following).strip()) is not None
    )


def _numbered_sequence_admissions(
    document: ProviderDocument,
    initial_candidates: tuple[HeadingCandidate, ...],
    *,
    externally_negative: set[int],
) -> frozenset[int]:
    """Admit only a missing member of an already source-proved heading sequence."""

    initial_by_source = {
        candidate.source_index: candidate for candidate in initial_candidates
    }
    accepted_history: list[tuple[ProviderBlock, str, int, int]] = []
    admitted: set[int] = set()
    for block in document.blocks:
        existing = initial_by_source.get(block.source_index)
        if existing is not None:
            if existing.disposition == "accepted":
                signature = _numbering_signature(existing.text)
                if signature is None:
                    accepted_history.append((block, "", 0, 0))
                else:
                    family, rank, ordinal = signature
                    accepted_history.append((block, family, rank, ordinal))
            continue
        if (block.typed_annotation or "").casefold() != "paragraph":
            continue
        signature = _numbering_signature(_block_text(block))
        if signature is None or len(accepted_history) < 2:
            continue
        family, rank, ordinal = signature
        previous_two = accepted_history[-2:]
        if not (
            previous_two[0][1:] == (family, rank, ordinal - 2)
            and previous_two[1][1:] == (family, rank, ordinal - 1)
            and _matching_heading_geometry(
                block,
                previous_two[0][0],
                previous_two[1][0],
            )
        ):
            continue
        if (
            _demotion_reason(
                document,
                block,
                text=_block_text(block),
                externally_negative=block.source_index in externally_negative,
                body_text_conflicts=frozenset(),
                repeated_page_header=False,
                weak_provider_only=False,
            )
            is not None
        ):
            continue
        admitted.add(block.source_index)
        accepted_history.append((block, family, rank, ordinal))
    admitted.update(
        _bracketed_numbered_sequence_admissions(
            document,
            initial_candidates,
            externally_negative=externally_negative,
        )
    )
    return frozenset(admitted)


def _bracketed_numbered_sequence_admissions(
    document: ProviderDocument,
    initial_candidates: tuple[HeadingCandidate, ...],
    *,
    externally_negative: set[int],
) -> frozenset[int]:
    """Recover only a page-leading sibling bracketed by N-1 and N+1.

    Long headings are often emitted as paragraphs because they wrap over
    several lines, so line-height parity is not a safe requirement.  The
    two-sided sibling sequence and the physical page-leading position supply
    the missing positive evidence; any intervening peer-or-higher heading
    makes the relation ambiguous and leaves the paragraph as body text.
    """

    accepted = tuple(
        candidate
        for candidate in initial_candidates
        if candidate.disposition == "accepted"
        and _numbering_signature(candidate.text) is not None
    )
    page_leading_sources = _page_leading_source_indices(document)
    admitted: set[int] = set()
    for block in document.blocks:
        if (
            block.typed_annotation or ""
        ).casefold() != "paragraph" or block.source_index not in page_leading_sources:
            continue
        signature = _numbering_signature(_block_text(block))
        if signature is None:
            continue
        family, rank, ordinal = signature
        previous = next(
            (
                candidate
                for candidate in reversed(accepted)
                if candidate.source_index < block.source_index
                and _numbering_signature(candidate.text) == (family, rank, ordinal - 1)
            ),
            None,
        )
        following = next(
            (
                candidate
                for candidate in accepted
                if candidate.source_index > block.source_index
                and _numbering_signature(candidate.text) == (family, rank, ordinal + 1)
            ),
            None,
        )
        if previous is None or following is None:
            continue
        if any(
            previous.source_index < candidate.source_index < following.source_index
            and candidate.nominal_rank is not None
            and candidate.nominal_rank <= rank
            for candidate in accepted
        ):
            continue
        if (
            _demotion_reason(
                document,
                block,
                text=_block_text(block),
                externally_negative=block.source_index in externally_negative,
                body_text_conflicts=frozenset(),
                repeated_page_header=False,
                weak_provider_only=False,
            )
            is None
        ):
            admitted.add(block.source_index)
    return frozenset(admitted)


def _apply_front_matter_root_placements(
    document: ProviderDocument,
    candidates: tuple[HeadingCandidate, ...],
) -> tuple[HeadingCandidate, ...]:
    """Treat centered page-leading front matter as roots before chapter one.

    Prospectuses and restructuring reports commonly place declarations,
    definitions, major notices, and major risks on their own centered opening
    pages before the first numbered chapter.  MinerU flattens all of them to a
    weak level-2 leaf, which otherwise leaves later notices underneath the
    final numbered definition subsection.  The bound is deliberately narrow:
    it stops at the first ``第X章/节``-family heading and never changes a
    provider title embedded in the numbered body.
    """

    first_chapter_source = next(
        (
            candidate.source_index
            for candidate in candidates
            if candidate.disposition == "accepted"
            and candidate.numbering_family
            in {"第X编", "第X篇", "第X部", "第X章", "第X节"}
        ),
        None,
    )
    if first_chapter_source is None:
        return candidates
    page_leading_sources = _page_leading_source_indices(document)
    return tuple(
        replace(
            candidate,
            nominal_rank=1,
            placement_source="provider_style",
        )
        if candidate.disposition == "accepted"
        and candidate.source_index < first_chapter_source
        and candidate.page_index > 0
        and candidate.source_index in page_leading_sources
        and candidate.numbering_family is None
        and candidate.placement_source in {"provider", "flattened"}
        and _is_horizontally_centered(document.blocks[candidate.source_index])
        else candidate
        for candidate in candidates
    )


def _apply_document_numbering_scale(
    candidates: tuple[HeadingCandidate, ...],
) -> tuple[HeadingCandidate, ...]:
    """Make ``第X节`` top-level when the document has no higher division.

    Chinese reports use ``第X节`` both beneath a numbered chapter and as the
    document's highest numbered division.  Treating every section as rank 2
    lets a weak front-matter title (for example a contents page) own the whole
    report.  The document-wide presence of an accepted 编/篇/部/章 is the
    bounded signal that distinguishes the two numbering scales.
    """

    has_higher_division = any(
        candidate.disposition == "accepted"
        and candidate.numbering_family in {"第X编", "第X篇", "第X部", "第X章"}
        for candidate in candidates
    )
    if has_higher_division:
        return candidates
    return tuple(
        replace(candidate, nominal_rank=1)
        if candidate.disposition == "accepted"
        and candidate.numbering_family == "第X节"
        and candidate.placement_source == "numbering"
        else candidate
        for candidate in candidates
    )


def _apply_provider_style_placements(
    document: ProviderDocument,
    candidates: tuple[HeadingCandidate, ...],
) -> tuple[HeadingCandidate, ...]:
    """Promote only repeated single-column indentation into a weak hierarchy.

    MinerU Medium commonly flattens every title to level 2.  A single bbox is
    not enough to repair that, but repeated document-wide indentation is a
    mechanical style signal.  Requiring two supported clusters and rejecting
    column-sized gaps keeps one-off alignment and multi-column layouts on the
    provider-leaf fallback.
    """

    eligible = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.disposition == "accepted"
            and candidate.numbering_family is None
            and candidate.placement_source in {"provider", "flattened"}
            and candidate.bbox is not None
        ),
        key=lambda candidate: candidate.bbox.x0 if candidate.bbox else 0.0,
    )
    clusters: list[list[HeadingCandidate]] = []
    for candidate in eligible:
        assert candidate.bbox is not None
        if not clusters:
            clusters.append([candidate])
            continue
        center = sum(
            item.bbox.x0 for item in clusters[-1] if item.bbox is not None
        ) / len(clusters[-1])
        if candidate.bbox.x0 - center <= _STYLE_X0_CLUSTER_TOLERANCE:
            clusters[-1].append(candidate)
        else:
            clusters.append([candidate])
    reliable = [
        cluster
        for cluster in clusters
        if len(cluster) >= _STYLE_MIN_CLUSTER_OCCURRENCES
    ]
    if len(reliable) < 2:
        return candidates
    page_leading_sources = _page_leading_source_indices(document)
    if (
        sum(candidate.source_index in page_leading_sources for candidate in reliable[0])
        < _STYLE_MIN_CLUSTER_OCCURRENCES
    ):
        return candidates
    if (
        _median_heading_height(reliable[0]) - _median_heading_height(reliable[1])
        < _STYLE_MIN_HEIGHT_STEP
    ):
        return candidates
    centers = [
        sum(item.bbox.x0 for item in cluster if item.bbox is not None) / len(cluster)
        for cluster in reliable
    ]
    if any(
        next_center - center > _STYLE_MAX_INDENT_STEP
        for center, next_center in zip(centers, centers[1:], strict=False)
    ):
        return candidates
    rank_by_id = {
        candidate.heading_id: rank
        for rank, cluster in enumerate(reliable, start=2)
        for candidate in cluster
    }
    return tuple(
        replace(
            candidate,
            nominal_rank=rank_by_id[candidate.heading_id],
            placement_source="provider_style",
        )
        if candidate.heading_id in rank_by_id
        else candidate
        for candidate in candidates
    )


def _median_heading_height(cluster: list[HeadingCandidate]) -> float:
    return float(
        median(
            candidate.bbox.y1 - candidate.bbox.y0
            for candidate in cluster
            if candidate.bbox is not None
        )
    )


def _page_leading_source_indices(document: ProviderDocument) -> frozenset[int]:
    result: set[int] = set()
    for page in document.pages:
        for block in page.blocks:
            annotation = (block.typed_annotation or "").casefold()
            if annotation in {
                "page_header",
                "page_footer",
                "page_footnote",
                "page_number",
            }:
                continue
            result.add(block.source_index)
            break
    return frozenset(result)


def _numbering_signature(text: str) -> tuple[str, int, int] | None:
    prefix = text.lstrip()
    chinese_match = _CHINESE_SEQUENCE_RE.match(prefix)
    if chinese_match is not None:
        ordinal = _chinese_ordinal_value(chinese_match.group("ordinal"))
        if ordinal is not None:
            return "中文序号", 3, ordinal
    match = _ARABIC_ORDINAL_RE.match(prefix)
    if match is None:
        return None
    return "阿拉伯序号", 5, int(match.group("ordinal"))


def _chinese_ordinal_value(text: str) -> int | None:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1_000, "万": 10_000}
    total = 0
    section = 0
    number = 0
    for character in text:
        if character in digits:
            number = digits[character]
            continue
        unit = units.get(character)
        if unit is None:
            return None
        if unit == 10_000:
            total += (section + number) * unit
            section = 0
            number = 0
        else:
            section += (number or 1) * unit
            number = 0
    value = total + section + number
    return value if value > 0 else None


def _matching_heading_geometry(
    current: ProviderBlock,
    first: ProviderBlock,
    second: ProviderBlock,
) -> bool:
    if current.bbox is None or first.bbox is None or second.bbox is None:
        return False
    current_bbox = current.bbox
    prior_bboxes = (first.bbox, second.bbox)
    current_height = current_bbox.y1 - current_bbox.y0
    return all(
        abs(current_bbox.x0 - prior_bbox.x0) <= _SEQUENCE_X0_TOLERANCE
        and abs(current_height - (prior_bbox.y1 - prior_bbox.y0))
        <= _SEQUENCE_HEIGHT_TOLERANCE
        for prior_bbox in prior_bboxes
    )


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
    if _ENGLISH_SECTION_RE.match(prefix):
        return "Section X", 1
    if _ROMAN_ORDINAL_RE.match(prefix):
        return "Roman ordinal", 2
    if _LOWER_ROMAN_ORDINAL_RE.match(prefix):
        return "lower Roman ordinal", 6
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
