"""Deterministic heading placement and content-conserving coarse units."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
import re
from statistics import median

from disclosure_anchor.application.contracts.applicability_selector import (
    is_closed_applicability_selector,
)
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
    HeadingSourceFragment,
    ResolvedHeading,
)
from disclosure_anchor.application.contracts.provider_document import (
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
)
from disclosure_anchor.application.services.provider_table_projection import (
    build_provider_table_projection,
)


_CHINESE_NUMBER = "一二三四五六七八九十百千万零〇两"
_CHAPTER_RE = re.compile(rf"^第[{_CHINESE_NUMBER}0-9]+(?P<kind>[编篇部章节条])")
_CHINESE_ORDINAL_RE = re.compile(
    rf"^(?:[{_CHINESE_NUMBER}]+、|[{_CHINESE_NUMBER}]{{1,3}}\s+)"
)
_CHINESE_SEQUENCE_RE = re.compile(rf"^(?P<ordinal>[{_CHINESE_NUMBER}]+)(?:、|\s+)")
_NUMBERING_LEADING_QUOTES_RE = re.compile(r'^["\'“‘「『]+')
_PAREN_CHINESE_RE = re.compile(rf"^[（(][{_CHINESE_NUMBER}]+[）)]")
_DOTTED_ARABIC_RE = re.compile(
    r"^(?P<number>[0-9]+(?:\.[0-9]+)+)"
    r"(?:[、.]|\s|$|(?=[A-Za-z\u3400-\u9fff]))"
)
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
_BARE_APPLICABILITY_STATEMENTS = frozenset({"适用", "不适用"})
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
_NESTED_NUMBERING_MIN_INDENT = 12.0
_STYLE_MIN_CLUSTER_OCCURRENCES = 3
_STYLE_MIN_HEIGHT_STEP = 2.0
_REPEATED_HEADER_MIN_PAGES = 3
_REPEATED_HEADER_MAX_PAGE_GAP = 3
_REPEATED_HEADER_TOP_FRACTION = 0.25
_REPEATED_HEADER_BBOX_TOLERANCE = 4.0
_NORMALIZED_PAGE_AXIS = 1_000.0
_STALE_NUMBERED_PARENT_MIN_PAGE_GAP = 8
_CONTINUATION_MARKER_RE = re.compile(r"(?:^|[\s（(\-—–])续[）)]?\s*$")
_FRONT_MATTER_IDENTITY_LABEL_RE = re.compile(
    r"(?:公司|证券|股票|债券|可转债|[ABH]\s*股|优先股)\s*(?:代码|简称)"
    r"|公告编号"
)
_FRONT_MATTER_IDENTITY_VALUE_MAX_CHARS = 40
_FRONT_MATTER_IDENTITY_SENTENCE_MARKS = frozenset("。；;！？!?：:")
_TABLE_LABEL_MAX_NORMALIZED_CHARS = 40
_TABLE_LABEL_MAX_Y_FRACTION = 0.30
_TABLE_LABEL_MIN_DOCUMENT_OCCURRENCES = 3
_TABLE_LABEL_TERMINAL_MARKS = frozenset("。；;！？!?;:：")
_SEQUENCE_HEADING_TERMINAL_MARKS = frozenset("。；;！？!?;")
_TABLE_LABEL_CENTER_TOLERANCE = 70.0
_TABLE_LABEL_Y_TOLERANCE = 70.0
_TABLE_LABEL_HEIGHT_TOLERANCE = 12.0
_TABLE_LABEL_MAX_INTERSTITIAL_BLOCKS = 3
_TABLE_LABEL_CARRIER_RE = re.compile(
    r"^(?:"
    r"单位(?:[:：].*)?|币种(?:[:：].*)?|"
    r"编制单位(?:[:：].*)?|人民币(?:元|千元|万元|百万元)?|"
    r"财务报表|"
    r"[（(]?除特别注明外[，,]?货币单位.*列示[）)]?|"
    r"(?:19|20)[0-9]{2}\s*年\s*[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日"
    r"(?:\s*止年度)?"
    r")$"
)
_INTERSTITIAL_TABLE_HEADING_RE = re.compile(r"(?:风险|提示|说明)")
_FIRST_TABLE_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
_DELAYED_OWNER_REFERENCE_MARKERS = ("上述", "前述", "该")
_DELAYED_OWNER_MAX_TEXT_BLOCKS = 8
_OWNER_BIGRAM_STOP = frozenset({"情况", "主要", "信息", "提示", "年度", "报告"})
_WRAPPED_HEADING_RIGHT_EDGE_TOLERANCE = 24.0
_WRAPPED_HEADING_MIN_HANGING_INDENT = 12.0
_WRAPPED_HEADING_MAX_HANGING_INDENT = 180.0
_WRAPPED_HEADING_MAX_VERTICAL_GAP = 18.0
_WRAPPED_HEADING_HEIGHT_TOLERANCE = 5.0
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
    table_label_admissions = _page_table_label_admissions(document)
    table_container_admissions = _page_table_container_admissions(
        document,
        table_label_admissions=table_label_admissions,
    )
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
                table_label_admitted=(
                    block.source_index in table_label_admissions
                ),
                table_container_admitted=(
                    block.source_index in table_container_admissions
                ),
            )
        )
        is not None
    )
    initial_candidates = _apply_wrapped_heading_fragments(
        document,
        initial_candidates,
    )
    sequence_admissions = _numbered_sequence_admissions(
        document,
        initial_candidates,
        externally_negative=negative_indices,
    )
    sequence_admissions = frozenset(
        (*sequence_admissions, *_dotted_table_caption_admissions(document, initial_candidates))
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
                    table_label_admitted=(
                        block.source_index in table_label_admissions
                    ),
                    table_container_admitted=(
                        block.source_index in table_container_admissions
                    ),
                )
            )
            is not None
        )
        candidates = _apply_wrapped_heading_fragments(document, candidates)
    else:
        candidates = initial_candidates
    candidates = _apply_document_numbering_scale(candidates)
    candidates = _apply_front_matter_root_placements(document, candidates)
    candidates = _apply_provider_style_placements(document, candidates)
    candidates = _apply_numbered_table_container_placements(document, candidates)
    headings = _resolve_headings(candidates, blocks=document.blocks)
    units = _coarse_units(document, headings)
    delayed_notice_pairs = _delayed_table_notice_heading_pairs(document, units)
    if delayed_notice_pairs:
        notice_ids = {notice_id for notice_id, _owner_id in delayed_notice_pairs}
        owner_ids = {owner_id for _notice_id, owner_id in delayed_notice_pairs}
        resolved_by_id = {heading.heading_id: heading for heading in headings}
        candidate_by_id = {
            candidate.heading_id: candidate for candidate in candidates
        }
        owner_rank_overrides: dict[str, int] = {}
        for owner_id in owner_ids:
            owner = resolved_by_id[owner_id]
            if owner.parent_heading_id is None:
                continue
            parent = resolved_by_id[owner.parent_heading_id]
            owner_candidate = candidate_by_id[owner_id]
            parent_candidate = candidate_by_id[parent.heading_id]
            if (
                parent_candidate.numbering_family is not None
                and owner_candidate.provider_level is not None
                and owner_candidate.provider_level == parent_candidate.provider_level
            ):
                owner_rank_overrides[owner_id] = parent.nominal_rank
        candidates = tuple(
            replace(
                candidate,
                nominal_rank=None,
                disposition="demoted",
                disposition_reason="interstitial_notice",
                placement_source=None,
            )
            if candidate.heading_id in notice_ids
            else (
                replace(
                    candidate,
                    nominal_rank=owner_rank_overrides[candidate.heading_id],
                    placement_source="provider_style",
                )
                if candidate.heading_id in owner_rank_overrides
                else candidate
            )
            for candidate in candidates
        )
        headings = _resolve_headings(candidates, blocks=document.blocks)
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
    table_label_admitted: bool,
    table_container_admitted: bool,
) -> HeadingCandidate | None:
    caption_heading = _numbered_table_caption(
        block,
        allow_parenthesized=sequence_admitted,
    )
    if block.provider_type.casefold() != "text" and caption_heading is None:
        return None
    if caption_heading is not None:
        payload_ordinal, text = caption_heading
        numbering_family, numbering_rank = _numbering(text)
        assert numbering_family is not None and numbering_rank is not None
        if externally_negative:
            return HeadingCandidate(
                heading_id=f"heading:{block.source_index:08d}:{payload_ordinal:04d}",
                source_index=block.source_index,
                payload_ordinal=payload_ordinal,
                page_index=block.page_index,
                bbox=block.bbox,
                text=text,
                raw_block_sha256=block.raw_item_sha256,
                provider_level=None,
                numbering_family=numbering_family,
                nominal_rank=None,
                disposition="demoted",
                disposition_reason="page_continuation",
                placement_source=None,
                source_fragments=(
                    _heading_source_fragment(block, payload_ordinal, text),
                ),
            )
        return HeadingCandidate(
            heading_id=f"heading:{block.source_index:08d}:{payload_ordinal:04d}",
            source_index=block.source_index,
            payload_ordinal=payload_ordinal,
            page_index=block.page_index,
            bbox=block.bbox,
            text=text,
            raw_block_sha256=block.raw_item_sha256,
            provider_level=None,
            numbering_family=numbering_family,
            nominal_rank=numbering_rank,
            disposition="accepted",
            disposition_reason="accepted",
            placement_source="numbering",
            source_fragments=(
                _heading_source_fragment(block, payload_ordinal, text),
            ),
        )
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
        and not table_label_admitted
        and not table_container_admitted
    ):
        return None
    payload_ordinal, text = _block_heading_payload(block)
    if not text.strip():
        return None
    heading_id = f"heading:{block.source_index:08d}"
    numbering_family, numbering_rank = _numbering(text)
    weak_provider_only = (
        not sequence_admitted
        and not table_label_admitted
        and not table_container_admitted
        and not outline_hint
        and numbering_family is None
        and (provider_title or provider_level_fallback)
    )
    provider_only_signal = (
        not sequence_admitted
        and not table_label_admitted
        and not table_container_admitted
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
            payload_ordinal=payload_ordinal,
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
            source_fragments=(
                _heading_source_fragment(block, payload_ordinal, text),
            ),
        )
    nominal_rank: int
    placement_source: HeadingPlacementSource
    if table_container_admitted:
        nominal_rank, placement_source = 3, "table_container"
    elif table_label_admitted:
        nominal_rank, placement_source = 4, "table_label"
    else:
        nominal_rank, placement_source = _placement(
            block,
            level_hint=level_hint,
            numbering_rank=numbering_rank,
        )
    return HeadingCandidate(
        heading_id=heading_id,
        source_index=block.source_index,
        payload_ordinal=payload_ordinal,
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
        source_fragments=(
            _heading_source_fragment(block, payload_ordinal, text),
        ),
    )


def _heading_source_fragment(
    block: ProviderBlock,
    payload_ordinal: int,
    text: str,
) -> HeadingSourceFragment:
    return HeadingSourceFragment(
        source_index=block.source_index,
        payload_ordinal=payload_ordinal,
        page_index=block.page_index,
        text=text,
        raw_block_sha256=block.raw_item_sha256,
    )


def _apply_wrapped_heading_fragments(
    document: ProviderDocument,
    candidates: tuple[HeadingCandidate, ...],
) -> tuple[HeadingCandidate, ...]:
    """Join one geometry-proved wrapped title tail without losing either source."""

    candidate_sources = {candidate.source_index for candidate in candidates}
    replacements: dict[str, HeadingCandidate] = {}
    for candidate in candidates:
        if (
            candidate.disposition != "accepted"
            or candidate.numbering_family
            not in {
                "第X编",
                "第X篇",
                "第X部",
                "第X章",
                "第X节",
                "第X条",
                "中文序号",
                "括号中文序号",
            }
            or len(candidate.source_fragments) != 1
        ):
            continue
        block = document.blocks[candidate.source_index]
        if (
            (block.typed_annotation or "").casefold() != "title"
            or block.bbox is None
            or candidate.source_index + 1 >= len(document.blocks)
        ):
            continue
        following = document.blocks[candidate.source_index + 1]
        if (
            following.source_index in candidate_sources
            or following.page_index != block.page_index
            or following.provider_type.casefold() != "text"
            or (following.typed_annotation or "").casefold() != "paragraph"
            or following.bbox is None
        ):
            continue
        payload_ordinal, tail = _block_heading_payload(following)
        if (
            not tail.strip()
            or _numbering(tail)[0] is not None
            or tail.rstrip()[-1] in _SEQUENCE_HEADING_TERMINAL_MARKS
            or tail.rstrip().endswith((":", "："))
            or sum(tail.count(marker) for marker in _CHECKBOX_MARKERS) >= 2
        ):
            continue
        right_edges = tuple(
            item.bbox.x1
            for item in document.pages[block.page_index].blocks
            if item.bbox is not None
            and item.provider_type.casefold() in {"text", "table"}
            and item.bbox.x1 >= 600.0
        )
        if not right_edges:
            continue
        learned_right_edge = median(right_edges)
        primary_height = block.bbox.y1 - block.bbox.y0
        following_height = following.bbox.y1 - following.bbox.y0
        hanging_indent = following.bbox.x0 - block.bbox.x0
        vertical_gap = following.bbox.y0 - block.bbox.y1
        if not (
            abs(block.bbox.x1 - learned_right_edge)
            <= _WRAPPED_HEADING_RIGHT_EDGE_TOLERANCE
            and _WRAPPED_HEADING_MIN_HANGING_INDENT
            <= hanging_indent
            <= _WRAPPED_HEADING_MAX_HANGING_INDENT
            and -1.0 <= vertical_gap <= _WRAPPED_HEADING_MAX_VERTICAL_GAP
            and abs(primary_height - following_height)
            <= _WRAPPED_HEADING_HEIGHT_TOLERANCE
        ):
            continue
        fragment = _heading_source_fragment(following, payload_ordinal, tail)
        replacements[candidate.heading_id] = replace(
            candidate,
            text=candidate.text + tail,
            source_fragments=(*candidate.source_fragments, fragment),
        )
    return tuple(replacements.get(item.heading_id, item) for item in candidates)


def _block_heading_payload(block: ProviderBlock) -> tuple[int, str]:
    for payload_ordinal, payload in enumerate(block.payloads):
        if payload.field in {"text", "content"} and payload.text:
            return payload_ordinal, payload.text
    return 0, ""


def _block_text(block: ProviderBlock) -> str:
    return _block_heading_payload(block)[1]


def _numbered_table_caption(
    block: ProviderBlock,
    *,
    allow_parenthesized: bool = False,
) -> tuple[int, str] | None:
    """Expose one strong, provider-typed caption occurrence as a heading.

    MinerU 3.4.4 occasionally embeds a source section heading in the table's
    dedicated ``table_caption`` field rather than emitting a separate text
    block.  This is not inferred from HTML or cell text: the exact payload
    ordinal remains the source occurrence and only strong root-style numbering
    may open a section.  Ordinary captions, ``表4`` labels, parenthesized table
    notes, and incidental numbering stay table payload.
    """

    if block.provider_type.casefold() != "table":
        return None
    captions = tuple(
        (payload_ordinal, payload.text)
        for payload_ordinal, payload in enumerate(block.payloads)
        if payload.field == "table_caption" and payload.text.strip()
    )
    if len(captions) != 1:
        return None
    payload_ordinal, text = captions[0]
    family, _rank = _numbering(text)
    allowed_families = {
        "第X编",
        "第X篇",
        "第X部",
        "第X章",
        "第X节",
        "中文序号",
        "阿拉伯序号",
    }
    if allow_parenthesized:
        allowed_families.update(
            {"括号中文序号", "括号阿拉伯序号", "点分阿拉伯序号"}
        )
    if family not in allowed_families:
        return None
    return payload_ordinal, text


def _dotted_table_caption_admissions(
    document: ProviderDocument,
    candidates: tuple[HeadingCandidate, ...],
) -> set[int]:
    """Admit one retained dotted caption only after its exact predecessor."""

    retained_owners = {
        table.owner.block_source_index
        for table in build_provider_table_projection(document).logical_tables
    }
    accepted_dotted: list[tuple[int, tuple[int, ...], str]] = []
    for candidate in candidates:
        if (
            candidate.disposition != "accepted"
            or candidate.numbering_family != "点分阿拉伯序号"
        ):
            continue
        components = _dotted_components(candidate.text)
        if components is not None:
            accepted_dotted.append((candidate.source_index, components, candidate.text))
    accepted_texts = {text for _source_index, _components, text in accepted_dotted}
    admitted: set[int] = set()
    for block in document.blocks:
        if block.source_index not in retained_owners:
            continue
        captions = tuple(
            payload.text
            for payload in block.payloads
            if payload.field == "table_caption" and payload.text.strip()
        )
        if len(captions) != 1 or _CONTINUATION_MARKER_RE.search(captions[0]):
            continue
        caption = captions[0]
        components = _dotted_components(caption)
        if (
            components is None
            or len(components) < 3
            or components[-1] <= 1
            or not _dotted_caption_has_title(caption)
        ):
            continue
        if caption in accepted_texts:
            continue
        predecessor = (*components[:-1], components[-1] - 1)
        matches = tuple(
            source_index
            for source_index, prior_components, _text in accepted_dotted
            if source_index < block.source_index and prior_components == predecessor
        )
        if len(matches) != 1:
            continue
        prior_index = matches[0]
        competing = tuple(
            candidate
            for candidate in candidates
            if (
                candidate.disposition == "accepted"
                and prior_index < candidate.source_index < block.source_index
                and candidate.nominal_rank is not None
                and candidate.nominal_rank <= len(components) + 4
            )
        )
        if competing:
            continue
        admitted.add(block.source_index)
    return admitted


def _dotted_components(text: str) -> tuple[int, ...] | None:
    match = _DOTTED_ARABIC_RE.match(_numbering_prefix(text))
    if match is None:
        return None
    return tuple(int(value) for value in match.group("number").split("."))


def _dotted_caption_has_title(text: str) -> bool:
    prefix = _numbering_prefix(text)
    match = _DOTTED_ARABIC_RE.match(prefix)
    if match is None:
        return False
    return bool(prefix[match.end() :].strip(" 、.．:："))


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
    if _table_footnote_title_conflict(document, block, text=text):
        return "table_footnote_conflict"
    if weak_provider_only:
        if _normalized_text(text) in _BARE_APPLICABILITY_STATEMENTS:
            return "selector_statement"
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


def _page_table_label_admissions(document: ProviderDocument) -> frozenset[int]:
    """Admit repeated page-leading labels that mechanically own a table.

    MinerU can flatten statement names to paragraphs while preserving the
    exact source occurrence and the following table.  A single centered line
    is not enough evidence.  The label must be page-leading, short, centered,
    followed on the same page only by closed carrier fields and a provider
    table, and belong to a document-wide geometry cluster seen on at least
    three pages.  The text is never synthesized from table cells.
    """

    eligible = tuple(
        block
        for block in document.blocks
        if _is_page_table_label_candidate(document, block)
    )
    clusters: list[list[ProviderBlock]] = []
    for block in eligible:
        assert block.bbox is not None
        for cluster in clusters:
            anchor = cluster[0]
            assert anchor.bbox is not None
            block_center = (block.bbox.x0 + block.bbox.x1) / 2.0
            anchor_center = (anchor.bbox.x0 + anchor.bbox.x1) / 2.0
            if (
                abs(block_center - anchor_center)
                <= _TABLE_LABEL_CENTER_TOLERANCE
                and abs(block.bbox.y0 - anchor.bbox.y0)
                <= _TABLE_LABEL_Y_TOLERANCE
                and abs(
                    (block.bbox.y1 - block.bbox.y0)
                    - (anchor.bbox.y1 - anchor.bbox.y0)
                )
                <= _TABLE_LABEL_HEIGHT_TOLERANCE
            ):
                cluster.append(block)
                break
        else:
            clusters.append([block])
    return frozenset(
        block.source_index
        for cluster in clusters
        if len({block.page_index for block in cluster})
        >= _TABLE_LABEL_MIN_DOCUMENT_OCCURRENCES
        for block in cluster
    )


def _page_table_container_admissions(
    document: ProviderDocument,
    *,
    table_label_admissions: frozenset[int],
) -> frozenset[int]:
    """Admit one exact source container immediately before a proved table family."""

    page_leading = _page_leading_source_indices(document)
    eligible: list[int] = []
    for block in document.blocks:
        if (
            block.source_index not in page_leading
            or block.provider_type.casefold() != "text"
            or (block.typed_annotation or "").casefold() != "paragraph"
            or _normalized_text(_block_text(block)) != "财务报表"
        ):
            continue
        following = tuple(
            item
            for item in document.blocks
            if item.page_index == block.page_index
            and item.source_index > block.source_index
        )
        for offset, item in enumerate(following):
            if offset > _TABLE_LABEL_MAX_INTERSTITIAL_BLOCKS:
                break
            if item.source_index in table_label_admissions:
                eligible.append(block.source_index)
                break
            annotation = (item.typed_annotation or "").casefold()
            if annotation in {"page_header", "page_footer", "page_number"}:
                continue
            if not _is_table_label_carrier(item):
                break
    # One repeated page label denotes the same source section, not a fresh
    # container on every statement page.  Conservatively open the earliest
    # source occurrence; later identical carriers remain payload.
    return frozenset(eligible[:1])


def _is_page_table_label_candidate(
    document: ProviderDocument,
    block: ProviderBlock,
) -> bool:
    if (
        block.provider_type.casefold() != "text"
        or (block.typed_annotation or "").casefold() != "paragraph"
        or not _page_table_label_prefix_is_mechanical(document, block)
        or block.bbox is None
        or block.bbox.y1
        > _NORMALIZED_PAGE_AXIS * _TABLE_LABEL_MAX_Y_FRACTION
        or not _is_horizontally_centered(block)
    ):
        return False
    text = " ".join(_block_text(block).split())
    normalized = _normalized_text(text)
    if (
        not normalized
        or len(normalized) > _TABLE_LABEL_MAX_NORMALIZED_CHARS
        or text[-1] in _TABLE_LABEL_TERMINAL_MARKS
        or _numbering(text)[0] is not None
        or _CONTINUATION_MARKER_RE.search(text) is not None
    ):
        return False
    following = tuple(
        item
        for item in document.blocks
        if item.source_index > block.source_index
        and item.page_index == block.page_index
    )
    for offset, item in enumerate(following):
        if offset > _TABLE_LABEL_MAX_INTERSTITIAL_BLOCKS:
            return False
        annotation = (item.typed_annotation or "").casefold()
        if annotation in {"page_header", "page_footer", "page_number"}:
            continue
        if item.provider_type.casefold() == "table":
            return True
        if not _is_table_label_carrier(item):
            return False
    return False


def _page_table_label_prefix_is_mechanical(
    document: ProviderDocument,
    block: ProviderBlock,
) -> bool:
    for item in document.blocks:
        if item.page_index != block.page_index or item.source_index >= block.source_index:
            continue
        annotation = (item.typed_annotation or "").casefold()
        if annotation in {"page_header", "page_footer", "page_number"}:
            continue
        if not _is_table_label_carrier(item):
            return False
    return True


def _is_table_label_carrier(block: ProviderBlock) -> bool:
    if block.provider_type.casefold() != "text":
        return False
    text = " ".join(_block_text(block).split())
    return bool(text) and _TABLE_LABEL_CARRIER_RE.fullmatch(text) is not None


def _table_footnote_title_conflict(
    document: ProviderDocument,
    block: ProviderBlock,
    *,
    text: str,
) -> bool:
    """Demote a post-table title already typed elsewhere as a footnote."""

    if (
        (block.typed_annotation or "").casefold() != "title"
        or block.source_index in _page_leading_source_indices(document)
    ):
        return False
    normalized = _normalized_text(text)
    if not normalized or not any(
        payload.field == "table_footnote"
        and _normalized_text(payload.text) == normalized
        for item in document.blocks
        for payload in item.payloads
    ):
        return False
    return any(
        item.page_index == block.page_index
        and item.source_index < block.source_index
        and item.provider_type.casefold() == "table"
        for item in document.blocks
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
                signature = _admission_numbering_signature(existing.text)
                if signature is None:
                    accepted_history.append((block, "", 0, 0))
                else:
                    family, rank, ordinal = signature
                    accepted_history.append((block, family, rank, ordinal))
            continue
        if (block.typed_annotation or "").casefold() != "paragraph":
            continue
        text = _block_text(block)
        signature = _admission_numbering_signature(text)
        if signature is None or not _sequence_heading_shape(text):
            continue
        family, rank, ordinal = signature
        previous_two = [
            item
            for item in accepted_history
            if item[1:3] == (family, rank) and item[3] < ordinal
        ][-2:]
        if not (
            len(previous_two) == 2
            and previous_two[0][1:] == (family, rank, ordinal - 2)
            and previous_two[1][1:] == (family, rank, ordinal - 1)
            and not _has_peer_or_higher_boundary(
                accepted_history,
                after_source=previous_two[0][0].source_index,
                before_source=block.source_index,
                rank=rank,
                allowed_sources={
                    previous_two[1][0].source_index,
                },
            )
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
        _first_parenthesized_child_admissions(
            document,
            initial_candidates,
            externally_negative=externally_negative,
        )
    )
    admitted.update(
        _multi_gap_numbered_sequence_admissions(
            document,
            initial_candidates,
            externally_negative=externally_negative,
        )
    )
    admitted.update(
        _bracketed_numbered_sequence_admissions(
            document,
            initial_candidates,
            externally_negative=externally_negative,
        )
    )
    admitted.update(
        _bracketed_table_caption_sequence_admissions(
            document,
            initial_candidates,
            externally_negative=externally_negative,
        )
    )
    admitted.update(
        _long_parenthesized_selector_sequence_admissions(
            document,
            initial_candidates,
            externally_negative=externally_negative,
        )
    )
    admitted.update(
        _dotted_restart_after_repeated_carriers_admissions(
            document,
            initial_candidates,
            externally_negative=externally_negative,
        )
    )
    return frozenset(admitted)


def _admission_numbering_signature(text: str) -> tuple[str, int, int] | None:
    signature = _numbering_signature(text)
    if signature is not None:
        return signature
    parenthesized = _parenthesized_signature(text)
    if parenthesized is None:
        return None
    family, ordinal = parenthesized
    rank = 4 if family == "括号中文序号" else 6
    return family, rank, ordinal


def _sequence_heading_shape(text: str) -> bool:
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] not in _SEQUENCE_HEADING_TERMINAL_MARKS


def _has_peer_or_higher_boundary(
    history: list[tuple[ProviderBlock, str, int, int]],
    *,
    after_source: int,
    before_source: int,
    rank: int,
    allowed_sources: set[int] | frozenset[int] = frozenset(),
) -> bool:
    return any(
        after_source < item[0].source_index < before_source
        and item[0].source_index not in allowed_sources
        and item[2] > 0
        and item[2] <= rank
        for item in history
    )


def _paragraph_admission_is_safe(
    document: ProviderDocument,
    block: ProviderBlock,
    *,
    externally_negative: set[int],
) -> bool:
    text = _block_text(block)
    return _sequence_heading_shape(text) and _demotion_reason(
        document,
        block,
        text=text,
        externally_negative=block.source_index in externally_negative,
        body_text_conflicts=frozenset(),
        repeated_page_header=False,
        weak_provider_only=False,
    ) is None


def _first_parenthesized_child_admissions(
    document: ProviderDocument,
    initial_candidates: tuple[HeadingCandidate, ...],
    *,
    externally_negative: set[int],
) -> frozenset[int]:
    accepted = tuple(
        candidate
        for candidate in initial_candidates
        if candidate.disposition == "accepted"
    )
    admitted: set[int] = set()
    for block in document.blocks:
        if (block.typed_annotation or "").casefold() != "paragraph":
            continue
        signature = _admission_numbering_signature(_block_text(block))
        if (
            signature is None
            or signature[0] not in {"括号中文序号", "括号阿拉伯序号"}
            or signature[2] != 1
            or not _paragraph_admission_is_safe(
                document,
                block,
                externally_negative=externally_negative,
            )
        ):
            continue
        family, rank, _ordinal = signature
        parent = next(
            (
                candidate
                for candidate in reversed(accepted)
                if candidate.source_index < block.source_index
                and candidate.nominal_rank == rank - 1
            ),
            None,
        )
        following = next(
            (
                candidate
                for candidate in accepted
                if candidate.source_index > block.source_index
                and _admission_numbering_signature(candidate.text)
                == (family, rank, 2)
            ),
            None,
        )
        if parent is None or following is None:
            continue
        intervening = tuple(
            candidate
            for candidate in accepted
            if parent.source_index < candidate.source_index < following.source_index
        )
        if any(
            candidate.nominal_rank is not None
            and candidate.nominal_rank <= rank
            for candidate in intervening
        ):
            continue
        if not _matching_heading_geometry_pair(
            block,
            document.blocks[following.source_index],
        ):
            continue
        admitted.add(block.source_index)
    return frozenset(admitted)


def _multi_gap_numbered_sequence_admissions(
    document: ProviderDocument,
    initial_candidates: tuple[HeadingCandidate, ...],
    *,
    externally_negative: set[int],
) -> frozenset[int]:
    accepted = tuple(
        candidate
        for candidate in initial_candidates
        if candidate.disposition == "accepted"
        and _admission_numbering_signature(candidate.text) is not None
    )
    result: set[int] = set()
    for previous in accepted:
        previous_signature = _admission_numbering_signature(previous.text)
        assert previous_signature is not None
        family, rank, previous_ordinal = previous_signature
        later_same_family = tuple(
            candidate
            for candidate in accepted
            if candidate.source_index > previous.source_index
            and (
                signature := _admission_numbering_signature(candidate.text)
            )
            is not None
            and signature[0:2] == (family, rank)
        )
        if not later_same_family:
            continue
        following = later_same_family[0]
        following_signature = _admission_numbering_signature(following.text)
        assert following_signature is not None
        if following_signature[2] - previous_ordinal < 3:
            continue
        if _has_peer_or_higher_candidate_boundary(
            accepted,
            after_source=previous.source_index,
            before_source=following.source_index,
            rank=rank,
        ):
            continue
        candidates_by_ordinal: dict[int, list[ProviderBlock]] = {}
        for block in document.blocks:
            if not (previous.source_index < block.source_index < following.source_index):
                continue
            if (block.typed_annotation or "").casefold() != "paragraph":
                continue
            signature = _admission_numbering_signature(_block_text(block))
            if signature is None or signature[0:2] != (family, rank):
                continue
            candidates_by_ordinal.setdefault(signature[2], []).append(block)
        missing_ordinals = range(previous_ordinal + 1, following_signature[2])
        missing = tuple(
            candidates_by_ordinal.get(ordinal, []) for ordinal in missing_ordinals
        )
        if not missing or any(len(items) != 1 for items in missing):
            continue
        blocks = tuple(items[0] for items in missing)
        if tuple(block.source_index for block in blocks) != tuple(
            sorted(block.source_index for block in blocks)
        ):
            continue
        if not all(
            _paragraph_admission_is_safe(
                document,
                block,
                externally_negative=externally_negative,
            )
            for block in blocks
        ):
            continue
        result.update(block.source_index for block in blocks)
    return frozenset(result)


def _has_peer_or_higher_candidate_boundary(
    accepted: tuple[HeadingCandidate, ...],
    *,
    after_source: int,
    before_source: int,
    rank: int,
) -> bool:
    return any(
        after_source < candidate.source_index < before_source
        and candidate.nominal_rank is not None
        and candidate.nominal_rank <= rank
        for candidate in accepted
    )


def _matching_heading_geometry_pair(
    first: ProviderBlock,
    second: ProviderBlock,
) -> bool:
    if first.bbox is None or second.bbox is None:
        return False
    return (
        abs(first.bbox.x0 - second.bbox.x0) <= _SEQUENCE_X0_TOLERANCE
        and abs(
            (first.bbox.y1 - first.bbox.y0)
            - (second.bbox.y1 - second.bbox.y0)
        )
        <= _SEQUENCE_HEIGHT_TOLERANCE
    )


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
        and _admission_numbering_signature(candidate.text) is not None
    )
    page_leading_sources = _page_leading_source_indices(document)
    admitted: set[int] = set()
    for block in document.blocks:
        if (
            block.typed_annotation or ""
        ).casefold() != "paragraph" or block.source_index not in page_leading_sources:
            continue
        text = _block_text(block)
        signature = _admission_numbering_signature(text)
        if signature is None or not _sequence_heading_shape(text):
            continue
        family, rank, ordinal = signature
        previous = next(
            (
                candidate
                for candidate in reversed(accepted)
                if candidate.source_index < block.source_index
                and _admission_numbering_signature(candidate.text)
                == (family, rank, ordinal - 1)
            ),
            None,
        )
        following = next(
            (
                candidate
                for candidate in accepted
                if candidate.source_index > block.source_index
                and _admission_numbering_signature(candidate.text)
                == (family, rank, ordinal + 1)
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


def _bracketed_table_caption_sequence_admissions(
    document: ProviderDocument,
    initial_candidates: tuple[HeadingCandidate, ...],
    *,
    externally_negative: set[int],
) -> frozenset[int]:
    """Admit one parenthesized caption only between proved sibling ordinals."""

    accepted = tuple(
        candidate
        for candidate in initial_candidates
        if candidate.disposition == "accepted"
        and _admission_numbering_signature(candidate.text) is not None
    )
    admitted: set[int] = set()
    for block in document.blocks:
        if (
            block.provider_type.casefold() != "table"
            or block.source_index in externally_negative
        ):
            continue
        captions = tuple(
            payload.text
            for payload in block.payloads
            if payload.field == "table_caption" and payload.text.strip()
        )
        if len(captions) != 1:
            continue
        text = captions[0]
        signature = _admission_numbering_signature(text)
        if (
            signature is None
            or signature[0] not in {"括号中文序号", "括号阿拉伯序号"}
            or not _sequence_heading_shape(text)
            or sum(text.count(marker) for marker in _CHECKBOX_MARKERS) >= 2
        ):
            continue
        family, rank, ordinal = signature
        previous = next(
            (
                candidate
                for candidate in reversed(accepted)
                if candidate.source_index < block.source_index
                and _admission_numbering_signature(candidate.text)
                == (family, rank, ordinal - 1)
            ),
            None,
        )
        following = next(
            (
                candidate
                for candidate in accepted
                if candidate.source_index > block.source_index
                and _admission_numbering_signature(candidate.text)
                == (family, rank, ordinal + 1)
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
        admitted.add(block.source_index)
    return frozenset(admitted)


def _long_parenthesized_selector_sequence_admissions(
    document: ProviderDocument,
    initial_candidates: tuple[HeadingCandidate, ...],
    *,
    externally_negative: set[int],
) -> frozenset[int]:
    """Admit one wrapped sibling only when an owned selector follows it."""

    accepted = tuple(
        candidate
        for candidate in initial_candidates
        if candidate.disposition == "accepted"
    )
    admitted: set[int] = set()
    for block in document.blocks:
        if (
            (block.typed_annotation or "").casefold() != "paragraph"
            or block.source_index + 1 >= len(document.blocks)
            or not _paragraph_admission_is_safe(
                document,
                block,
                externally_negative=externally_negative,
            )
        ):
            continue
        signature = _admission_numbering_signature(_block_text(block))
        if signature is None or signature[0] not in {
            "括号中文序号",
            "括号阿拉伯序号",
        }:
            continue
        family, rank, ordinal = signature
        previous_two = tuple(
            candidate
            for candidate in accepted
            if candidate.source_index < block.source_index
            and _admission_numbering_signature(candidate.text)
            in {
                (family, rank, ordinal - 2),
                (family, rank, ordinal - 1),
            }
        )[-2:]
        if (
            len(previous_two) != 2
            or _admission_numbering_signature(previous_two[0].text)
            != (family, rank, ordinal - 2)
            or _admission_numbering_signature(previous_two[1].text)
            != (family, rank, ordinal - 1)
            or any(
                previous_two[0].source_index
                < candidate.source_index
                < block.source_index
                and candidate.source_index != previous_two[1].source_index
                and candidate.nominal_rank is not None
                and candidate.nominal_rank <= rank
                for candidate in accepted
            )
        ):
            continue
        following = document.blocks[block.source_index + 1]
        selector = _block_text(following)
        if (
            following.page_index != block.page_index
            or not is_closed_applicability_selector(selector)
            or block.bbox is None
            or previous_two[-1].bbox is None
            or abs(block.bbox.x0 - previous_two[-1].bbox.x0) > 20.0
        ):
            continue
        admitted.add(block.source_index)
    return frozenset(admitted)


def _dotted_restart_after_repeated_carriers_admissions(
    document: ProviderDocument,
    initial_candidates: tuple[HeadingCandidate, ...],
    *,
    externally_negative: set[int],
) -> frozenset[int]:
    """Recover a dotted sibling after source-proved repeated ancestor carriers."""

    by_source = {candidate.source_index: candidate for candidate in initial_candidates}
    accepted = tuple(
        candidate
        for candidate in initial_candidates
        if candidate.disposition == "accepted"
    )
    accepted_texts = {
        _normalized_text(_continuation_base(candidate.text) or candidate.text)
        for candidate in accepted
    }
    admitted: set[int] = set()
    for block in document.blocks:
        if (
            (block.typed_annotation or "").casefold() != "paragraph"
            or not _paragraph_admission_is_safe(
                document,
                block,
                externally_negative=externally_negative,
            )
            or _CONTINUATION_MARKER_RE.search(_block_text(block)) is not None
        ):
            continue
        dotted = _dotted_stem_and_ordinal(_block_text(block))
        if dotted is None:
            continue
        stem, ordinal = dotted
        previous = next(
            (
                candidate
                for candidate in reversed(accepted)
                if candidate.source_index < block.source_index
                and _dotted_stem_and_ordinal(candidate.text) == (stem, ordinal - 1)
            ),
            None,
        )
        if previous is None or not _matching_heading_geometry_pair(
            block,
            document.blocks[previous.source_index],
        ):
            continue
        preceding = tuple(
            item
            for item in document.pages[block.page_index].blocks
            if item.source_index < block.source_index
        )
        repeated: list[HeadingCandidate] = []
        valid = True
        for item in preceding:
            annotation = (item.typed_annotation or "").casefold()
            if annotation in {
                "page_header",
                "page_footer",
                "page_footnote",
                "page_number",
            }:
                continue
            candidate = by_source.get(item.source_index)
            if (
                candidate is None
                or candidate.disposition_reason != "repeated_page_header"
            ):
                valid = False
                break
            base = _continuation_base(candidate.text)
            if base is None or _normalized_text(base) not in accepted_texts:
                valid = False
                break
            repeated.append(candidate)
        if valid and repeated:
            admitted.add(block.source_index)
    return frozenset(admitted)


def _dotted_stem_and_ordinal(text: str) -> tuple[tuple[int, ...], int] | None:
    match = _DOTTED_ARABIC_RE.match(_numbering_prefix(text))
    if match is None:
        return None
    components = tuple(int(item) for item in match.group("number").split("."))
    return components[:-1], components[-1]


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


def _apply_numbered_table_container_placements(
    document: ProviderDocument,
    candidates: tuple[HeadingCandidate, ...],
) -> tuple[HeadingCandidate, ...]:
    """Keep a repeated table-heading family under its empty numbered parent.

    Provider style ranks are document-wide and may be numerically stronger
    than a local numbered container.  The override is allowed only when the
    numbered heading is immediately followed by at least three unnumbered,
    geometry-consistent headings that each mechanically own a same-page
    table, and a later consecutive numbered sibling closes the run.  Explicit
    continuation labels are conserved as body carriers rather than separate
    Units.
    """

    accepted = tuple(
        candidate for candidate in candidates if candidate.disposition == "accepted"
    )
    replacements: dict[str, HeadingCandidate] = {}
    for container in accepted:
        signature = _numbering_signature(container.text)
        if signature is None or container.nominal_rank is None:
            continue
        family, rank, ordinal = signature
        closing = next(
            (
                candidate
                for candidate in accepted
                if candidate.source_index > container.source_index
                and (
                    following_signature := _numbering_signature(candidate.text)
                )
                is not None
                and following_signature == (family, rank, ordinal + 1)
            ),
            None,
        )
        if closing is None:
            continue
        run = tuple(
            candidate
            for candidate in accepted
            if container.source_index < candidate.source_index < closing.source_index
        )
        if (
            len(run) < _TABLE_LABEL_MIN_DOCUMENT_OCCURRENCES
            or run[0].source_index != container.source_index + 1
            or any(
                candidate.numbering_family is not None
                or candidate.placement_source
                not in {"provider", "flattened", "pdf_style", "provider_style"}
                or not _heading_immediately_owns_table(
                    document,
                    document.blocks[candidate.source_index],
                )
                for candidate in run
            )
            or not _same_table_heading_geometry(run)
        ):
            continue
        prior_base: str | None = None
        for candidate in run:
            base = _continuation_base(candidate.text)
            if base is not None and prior_base == _normalized_text(base):
                replacements[candidate.heading_id] = replace(
                    candidate,
                    nominal_rank=None,
                    disposition="demoted",
                    disposition_reason="page_continuation",
                    placement_source=None,
                )
                continue
            prior_base = _normalized_text(candidate.text)
            replacements[candidate.heading_id] = replace(
                candidate,
                nominal_rank=container.nominal_rank + 1,
                placement_source="provider_style",
            )
    return tuple(replacements.get(item.heading_id, item) for item in candidates)


def _heading_immediately_owns_table(
    document: ProviderDocument,
    block: ProviderBlock,
) -> bool:
    following = tuple(
        item
        for item in document.blocks
        if item.source_index > block.source_index
        and item.page_index == block.page_index
    )
    for offset, item in enumerate(following):
        if offset > _TABLE_LABEL_MAX_INTERSTITIAL_BLOCKS:
            return False
        annotation = (item.typed_annotation or "").casefold()
        if annotation in {"page_header", "page_footer", "page_number"}:
            continue
        if item.provider_type.casefold() == "table":
            return True
        if not _is_table_label_carrier(item):
            return False
    return False


def _same_table_heading_geometry(
    candidates: tuple[HeadingCandidate, ...],
) -> bool:
    bboxes = tuple(candidate.bbox for candidate in candidates)
    if any(bbox is None for bbox in bboxes):
        return False
    typed = tuple(bbox for bbox in bboxes if bbox is not None)
    anchor = typed[0]
    anchor_height = anchor.y1 - anchor.y0
    return all(
        abs(bbox.x0 - anchor.x0) <= _STYLE_X0_CLUSTER_TOLERANCE
        and abs((bbox.y1 - bbox.y0) - anchor_height)
        <= _TABLE_LABEL_HEIGHT_TOLERANCE
        for bbox in typed[1:]
    )


def _continuation_base(text: str) -> str | None:
    if _CONTINUATION_MARKER_RE.search(text) is None:
        return None
    return _CONTINUATION_MARKER_RE.sub("", text).strip()


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
    prefix = _numbering_prefix(text)
    chinese_match = _CHINESE_SEQUENCE_RE.match(prefix)
    if chinese_match is not None:
        ordinal = _chinese_ordinal_value(chinese_match.group("ordinal"))
        if ordinal is not None:
            return "中文序号", 3, ordinal
    dotted_match = _DOTTED_ARABIC_RE.match(prefix)
    if dotted_match is not None:
        components = dotted_match.group("number").split(".")
        rank = min(len(components) + 4, 9)
        return "点分阿拉伯序号", rank, int(components[-1])
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
    prefix = _numbering_prefix(text)
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
    *,
    blocks: tuple[ProviderBlock, ...],
) -> tuple[ResolvedHeading, ...]:
    resolved: list[ResolvedHeading] = []
    stack: list[ResolvedHeading] = []
    candidate_by_heading_id = {
        candidate.heading_id: candidate for candidate in candidates
    }
    accepted = tuple(
        candidate for candidate in candidates if candidate.disposition == "accepted"
    )
    for candidate in accepted:
        nominal_rank = candidate.nominal_rank
        placement_source = candidate.placement_source
        if nominal_rank is None or placement_source is None:
            raise ValueError("accepted heading has no placement")
        sequence_subgroup_parent = _source_sequence_subgroup_parent(
            candidate,
            blocks=blocks,
            stack=stack,
            resolved=resolved,
            candidate_by_heading_id=candidate_by_heading_id,
        )
        if sequence_subgroup_parent is not None:
            heading = ResolvedHeading(
                heading_id=candidate.heading_id,
                source_index=candidate.source_index,
                payload_ordinal=candidate.payload_ordinal,
                page_index=candidate.page_index,
                bbox=candidate.bbox,
                text=candidate.text,
                nominal_rank=nominal_rank,
                level=sequence_subgroup_parent.level + 1,
                parent_heading_id=sequence_subgroup_parent.heading_id,
                headpath=(*sequence_subgroup_parent.headpath, candidate.text),
                placement_source=placement_source,
                source_fragments=candidate.source_fragments,
            )
            resolved.append(heading)
            # This is the closing member of a source-proved local 1..N list,
            # not a new global numbered ancestor.  Keep the dotted parent on
            # the stack so the following 3.x sibling can close the subgroup.
            continue
        if _weak_heading_starts_new_numbered_subgroup(
            candidate,
            blocks=blocks,
            stack=stack,
            resolved=resolved,
            candidate_by_heading_id=candidate_by_heading_id,
        ):
            stack.pop()
        if placement_source == "table_label":
            while stack and stack[-1].nominal_rank >= nominal_rank:
                stack.pop()
        parent_eligible = placement_source not in {
            "provider",
            "flattened",
            "table_label",
        }
        if parent_eligible:
            historical_parent_stack = _numbered_history_parent_stack(
                candidate,
                resolved=resolved,
                candidate_by_heading_id=candidate_by_heading_id,
            )
            recovered_numbered_sibling = historical_parent_stack is not None
            if historical_parent_stack is not None:
                stack[:] = historical_parent_stack
            indented_numbered_subgroup = (
                not recovered_numbered_sibling
                and _starts_indented_numbered_subgroup(
                    candidate,
                    stack=stack,
                    candidate_by_heading_id=candidate_by_heading_id,
                )
            )
            if not recovered_numbered_sibling and not indented_numbered_subgroup:
                # Dotted siblings such as 4.6/4.7 are structurally stronger than
                # inferred style-only subheads between them.  Apply this only when
                # an earlier dotted sibling of the same depth is still on the
                # stack; other numbering families keep their existing front-matter
                # and style-reset behavior.
                has_dotted_sibling_ancestor = (
                    candidate.numbering_family == "点分阿拉伯序号"
                    and any(
                        candidate_by_heading_id[item.heading_id].numbering_family
                        == "点分阿拉伯序号"
                        and item.nominal_rank == nominal_rank
                        for item in stack
                    )
                )
                if has_dotted_sibling_ancestor:
                    while stack and stack[-1].placement_source in {
                        "pdf_style",
                        "provider_style",
                    }:
                        stack.pop()
                sibling_ancestor_index = _numbered_sibling_ancestor_index(
                    candidate,
                    stack=stack,
                    candidate_by_heading_id=candidate_by_heading_id,
                )
                if sibling_ancestor_index is not None:
                    del stack[sibling_ancestor_index + 1 :]
                if _starts_after_stale_numbered_parent(
                    candidate,
                    stack=stack,
                    candidates=candidates,
                    candidate_by_heading_id=candidate_by_heading_id,
                ):
                    stack.pop()
                while stack and stack[-1].nominal_rank >= nominal_rank:
                    if (
                        placement_source in {"pdf_style", "provider_style"}
                        and candidate_by_heading_id[
                            stack[-1].heading_id
                        ].numbering_family
                        == "点分阿拉伯序号"
                    ):
                        break
                    stack.pop()
        parent = stack[-1] if stack else None
        heading = ResolvedHeading(
            heading_id=candidate.heading_id,
            source_index=candidate.source_index,
            payload_ordinal=candidate.payload_ordinal,
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
            source_fragments=candidate.source_fragments,
        )
        resolved.append(heading)
        if parent_eligible:
            stack.append(heading)
    return tuple(resolved)


def _source_sequence_subgroup_parent(
    candidate: HeadingCandidate,
    *,
    blocks: tuple[ProviderBlock, ...],
    stack: list[ResolvedHeading],
    resolved: list[ResolvedHeading],
    candidate_by_heading_id: dict[str, HeadingCandidate],
) -> ResolvedHeading | None:
    """Keep a complete local Arabic 1..N list under its unnumbered subgroup."""

    signature = _numbering_signature(candidate.text)
    if (
        signature is None
        or signature[0] != "阿拉伯序号"
        or signature[2] <= 1
        or not stack
        or not resolved
    ):
        return None
    subgroup = resolved[-1]
    subgroup_candidate = candidate_by_heading_id[subgroup.heading_id]
    if (
        subgroup.parent_heading_id != stack[-1].heading_id
        or subgroup_candidate.numbering_family is not None
        or subgroup.placement_source
        not in {"provider", "flattened", "pdf_style", "provider_style"}
        or candidate.bbox is None
    ):
        return None
    accepted_sources = {
        item.source_index
        for item in candidate_by_heading_id.values()
        if item.disposition == "accepted" and item.numbering_family is not None
    }
    sequence: list[int] = []
    for block in blocks:
        if not (subgroup.source_index < block.source_index < candidate.source_index):
            continue
        if block.source_index in accepted_sources:
            return None
        block_signature = _numbering_signature(_block_text(block))
        if block_signature is None:
            continue
        if (
            block_signature[:2] != signature[:2]
            or block.bbox is None
            or abs(block.bbox.x0 - candidate.bbox.x0) > _SEQUENCE_X0_TOLERANCE
        ):
            return None
        sequence.append(block_signature[2])
    if sequence != list(range(1, signature[2])):
        return None
    return subgroup


def _weak_heading_starts_new_numbered_subgroup(
    candidate: HeadingCandidate,
    *,
    blocks: tuple[ProviderBlock, ...],
    stack: list[ResolvedHeading],
    resolved: list[ResolvedHeading],
    candidate_by_heading_id: dict[str, HeadingCandidate],
) -> bool:
    """Detach a weak leaf only when the next source block restarts at one.

    MinerU sometimes marks the ordinal-one line after a weak subgroup label as
    a paragraph, so accepted-candidate lookahead is not source-complete.  The
    immediate provider block is still a bounded structural signal.  A later
    or merely smaller ordinal is not enough to move the weak label.
    """

    if (
        candidate.numbering_family is not None
        or candidate.placement_source
        not in {"provider", "flattened", "pdf_style", "provider_style"}
        or not stack
        or not resolved
        or resolved[-1].heading_id != stack[-1].heading_id
    ):
        return False
    prior = candidate_by_heading_id[stack[-1].heading_id]
    prior_signature = _parenthesized_signature(prior.text)
    if prior_signature is None or prior_signature[1] <= 1:
        return False
    following = next(
        (block for block in blocks if block.source_index > candidate.source_index),
        None,
    )
    if following is None:
        return False
    following_signature = _parenthesized_signature(_block_text(following))
    return following_signature == (prior_signature[0], 1)


def _parenthesized_signature(text: str) -> tuple[str, int] | None:
    family, _rank = _numbering(text)
    if family not in {"括号中文序号", "括号阿拉伯序号"}:
        return None
    ordinal = _parenthesized_ordinal(text)
    if ordinal is None:
        return None
    return family, ordinal


def _numbered_history_parent_stack(
    candidate: HeadingCandidate,
    *,
    resolved: list[ResolvedHeading],
    candidate_by_heading_id: dict[str, HeadingCandidate],
) -> list[ResolvedHeading] | None:
    """Recover a numbered sibling displaced only by weak style headings.

    A run of provider/PDF style table titles can pop a numbered heading from
    the monotonic stack even though the next consecutive ordinal is its
    sibling.  Historical recovery is deliberately narrower than ordinary
    stack placement: the ordinal must be consecutive and every accepted
    heading since the prior sibling must be an unnumbered weak style heading.
    Any new numbered/outline boundary or ordinal restart leaves the current
    stack untouched.
    """

    signature = _numbering_signature(candidate.text)
    if signature is None:
        return None
    family, rank, ordinal = signature
    if ordinal <= 1:
        return None
    prior_index: int | None = None
    for index in range(len(resolved) - 1, -1, -1):
        prior = candidate_by_heading_id[resolved[index].heading_id]
        if _numbering_signature(prior.text) == (family, rank, ordinal - 1):
            prior_index = index
            break
    if prior_index is None:
        return None
    for intervening in resolved[prior_index + 1 :]:
        intervening_candidate = candidate_by_heading_id[intervening.heading_id]
        if (
            intervening.placement_source not in {"pdf_style", "provider_style"}
            or intervening_candidate.numbering_family is not None
        ):
            return None

    resolved_by_id = {heading.heading_id: heading for heading in resolved}
    parent_id = resolved[prior_index].parent_heading_id
    reversed_parents: list[ResolvedHeading] = []
    while parent_id is not None:
        parent = resolved_by_id[parent_id]
        reversed_parents.append(parent)
        parent_id = parent.parent_heading_id
    return list(reversed(reversed_parents))


def _starts_indented_numbered_subgroup(
    candidate: HeadingCandidate,
    *,
    stack: list[ResolvedHeading],
    candidate_by_heading_id: dict[str, HeadingCandidate],
) -> bool:
    """Let source indentation prove an otherwise rank-inverted subgroup.

    Some statutory templates place a Chinese-numbered sequence inside an
    Arabic-numbered commitment item.  Static numbering ranks alone would pop
    the Arabic parent.  Only ordinal one may establish this inversion, and
    both source boxes must prove a real indent; consecutive siblings then
    reuse the exact parent recovered from resolved history.
    """

    signature = _numbering_signature(candidate.text)
    if signature is None or signature[2] != 1 or not stack:
        return False
    parent = stack[-1]
    parent_candidate = candidate_by_heading_id[parent.heading_id]
    parent_signature = _numbering_signature(parent_candidate.text)
    if (
        parent_signature is None
        or parent_signature[0] == signature[0]
        or parent.nominal_rank is None
        or candidate.nominal_rank is None
        or parent.nominal_rank <= candidate.nominal_rank
        or parent_candidate.bbox is None
        or candidate.bbox is None
    ):
        return False
    indent = candidate.bbox.x0 - parent_candidate.bbox.x0
    return _NESTED_NUMBERING_MIN_INDENT <= indent <= _STYLE_MAX_INDENT_STEP


def _numbered_sibling_ancestor_index(
    candidate: HeadingCandidate,
    *,
    stack: list[ResolvedHeading],
    candidate_by_heading_id: dict[str, HeadingCandidate],
) -> int | None:
    """Restore ordinary sibling semantics across weak inferred subheads.

    A same-rank numbered ancestor would already be popped by the monotonic
    stack if every intervening heading had a faithful rank.  MinerU style
    hints can instead assign one intervening leaf a numerically stronger rank;
    an increasing source ordinal is the bounded evidence that the incoming
    heading is still a sibling of the earlier numbered occurrence.
    """

    signature = _numbering_signature(candidate.text)
    if signature is None:
        return None
    family, rank, ordinal = signature
    for index in range(len(stack) - 1, -1, -1):
        ancestor = candidate_by_heading_id[stack[index].heading_id]
        ancestor_signature = _numbering_signature(ancestor.text)
        if ancestor_signature is None:
            continue
        ancestor_family, ancestor_rank, ancestor_ordinal = ancestor_signature
        if (
            ancestor_family == family
            and ancestor_rank == rank
            and ancestor_ordinal < ordinal
        ):
            return index
    return None


def _starts_after_stale_numbered_parent(
    candidate: HeadingCandidate,
    *,
    stack: list[ResolvedHeading],
    candidates: tuple[HeadingCandidate, ...],
    candidate_by_heading_id: dict[str, HeadingCandidate],
) -> bool:
    """Detect a new ordinal-one sequence after repeated page-level furniture.

    Page distance alone is never a boundary.  The reset requires an ordinal-one
    child, a long gap from its numbered parent, and the same unnumbered provider
    title repeated on at least two distinct intervening pages.  This handles a
    provider-missed section boundary without naming any report or heading.
    """

    if not stack or candidate.numbering_family not in {
        "括号中文序号",
        "括号阿拉伯序号",
    }:
        return False
    ordinal = _parenthesized_ordinal(candidate.text)
    if ordinal != 1:
        return False
    parent = stack[-1]
    parent_candidate = candidate_by_heading_id[parent.heading_id]
    if (
        parent_candidate.numbering_family not in {"中文序号", "阿拉伯序号"}
        or _CONTINUATION_MARKER_RE.search(parent_candidate.text) is None
        or candidate.page_index - parent.page_index
        < _STALE_NUMBERED_PARENT_MIN_PAGE_GAP
    ):
        return False
    candidates_by_text: dict[str, list[HeadingCandidate]] = {}
    for item in candidates:
        if not (parent.source_index < item.source_index < candidate.source_index):
            continue
        if (
            item.disposition != "accepted"
            or item.numbering_family is not None
            or item.placement_source not in {"provider", "flattened"}
            or item.bbox is None
        ):
            continue
        candidates_by_text.setdefault(_normalized_text(item.text), []).append(
            item
        )
    for repeated in candidates_by_text.values():
        for index, first in enumerate(repeated):
            for second in repeated[index + 1 :]:
                if (
                    first.page_index != second.page_index
                    and first.bbox is not None
                    and second.bbox is not None
                    and all(
                        abs(left - right) <= _REPEATED_HEADER_BBOX_TOLERANCE
                        for left, right in zip(
                            first.bbox.as_tuple(),
                            second.bbox.as_tuple(),
                            strict=True,
                        )
                    )
                ):
                    return True
    return False


def _parenthesized_ordinal(text: str) -> int | None:
    prefix = _numbering_prefix(text)
    chinese = re.match(rf"^[（(](?P<ordinal>[{_CHINESE_NUMBER}]+)[）)]", prefix)
    if chinese is not None:
        return _chinese_ordinal_value(chinese.group("ordinal"))
    arabic = re.match(r"^[（(](?P<ordinal>[0-9]+)[）)]", prefix)
    if arabic is not None:
        return int(arabic.group("ordinal"))
    return None


def _numbering_prefix(text: str) -> str:
    return _NUMBERING_LEADING_QUOTES_RE.sub("", text.lstrip())


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
            if not _mergeable_leading_front_matter(
                document,
                pending_indices=tuple(pending_indices),
                heading=heading,
                has_prior_units=bool(units),
                pending_heading=pending_heading,
            ):
                flush()
            pending_heading = heading
        pending_indices.append(block.source_index)
    flush()
    return tuple(units)


def _delayed_table_notice_heading_pairs(
    document: ProviderDocument,
    units: tuple[CoarseUnit, ...],
) -> tuple[tuple[str, str], ...]:
    """Find a bounded notice that must remain inline with its table owner.

    Some layouts place a short risk/notice paragraph between a table's real
    heading and the table itself.  Reading-order partitioning would make the
    notice own every following table page.  We demote the notice heading only
    when the previous heading is otherwise empty, the notice
    explicitly refers back to it, the first table row has at least two exact
    non-generic bigram anchors from that heading, and the remainder contains
    only the one table stream plus page furniture.  This keeps one contiguous,
    source-conserving owner Unit; ambiguity preserves the ordinary partition.
    """

    result: list[tuple[str, str]] = []
    for index, unit in enumerate(units):
        prior = units[index - 1] if index else None
        if _delayed_table_split_source(
            document,
            prior=prior,
            current=unit,
        ) is not None:
            assert unit.heading_id is not None
            assert prior is not None and prior.heading_id is not None
            result.append((unit.heading_id, prior.heading_id))
    return tuple(result)


def _delayed_table_split_source(
    document: ProviderDocument,
    *,
    prior: CoarseUnit | None,
    current: CoarseUnit,
) -> int | None:
    if (
        prior is None
        or prior.heading_id is None
        or current.heading_id is None
        or current.title is None
        or _INTERSTITIAL_TABLE_HEADING_RE.search(current.title) is None
        or not _unit_has_only_heading_and_carriers(document, prior)
    ):
        return None
    current_blocks = tuple(
        document.blocks[source_index] for source_index in current.block_source_indices
    )
    table_offset = next(
        (
            index
            for index, block in enumerate(current_blocks)
            if block.provider_type.casefold() == "table"
        ),
        None,
    )
    if table_offset is None or not 1 < table_offset <= _DELAYED_OWNER_MAX_TEXT_BLOCKS:
        return None
    before_table = current_blocks[:table_offset]
    after_table = current_blocks[table_offset:]
    if any(
        block.provider_type.casefold() != "text"
        for block in before_table
    ) or any(not _is_table_or_page_furniture(block) for block in after_table):
        return None
    prior_text = " ".join(
        _block_text(document.blocks[source_index])
        for source_index in prior.block_source_indices
    )
    notice_text = " ".join(_block_text(block) for block in before_table)
    if not any(marker in notice_text for marker in _DELAYED_OWNER_REFERENCE_MARKERS):
        return None
    prior_anchors = _owner_bigrams(prior_text)
    if not (prior_anchors & _owner_bigrams(notice_text)):
        return None
    table_payload = next(
        (
            payload.text
            for payload in after_table[0].payloads
            if payload.field == "table_body" and payload.text.strip()
        ),
        "",
    )
    first_row_match = _FIRST_TABLE_ROW_RE.search(table_payload)
    if first_row_match is None:
        return None
    table_anchors = _owner_bigrams(html_visible_text(first_row_match.group(0)))
    if len(prior_anchors & table_anchors) < 2:
        return None
    return after_table[0].source_index


def _unit_has_only_heading_and_carriers(
    document: ProviderDocument,
    unit: CoarseUnit,
) -> bool:
    if not unit.block_source_indices:
        return False
    return all(
        source_index == unit.block_source_indices[0]
        or _is_table_label_carrier(document.blocks[source_index])
        for source_index in unit.block_source_indices
    )


def _is_table_or_page_furniture(block: ProviderBlock) -> bool:
    return block.provider_type.casefold() == "table" or (
        block.typed_annotation or ""
    ).casefold() in {"page_header", "page_footer", "page_number"}


def _owner_bigrams(text: str) -> frozenset[str]:
    normalized = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", text).casefold()
    return frozenset(
        token
        for token in (
            normalized[index : index + 2]
            for index in range(max(0, len(normalized) - 1))
        )
        if token not in _OWNER_BIGRAM_STOP
        and not token.isdigit()
    )


def _mergeable_leading_front_matter(
    document: ProviderDocument,
    *,
    pending_indices: tuple[int, ...],
    heading: ResolvedHeading,
    has_prior_units: bool,
    pending_heading: ResolvedHeading | None,
) -> bool:
    """Attach only same-page mechanical cover carriers to the first heading.

    A logo or exchange identity line has no useful standalone retrieval
    boundary, but it remains source content and evidence.  Merging it into the
    first titled Unit preserves every block and artifact without inventing a
    title or deleting the carrier.  Any substantive text, unknown label, page
    boundary, or prior Unit keeps the ordinary leading preamble.
    """

    if (
        not pending_indices
        or has_prior_units
        or pending_heading is not None
    ):
        return False
    blocks = tuple(document.blocks[index] for index in pending_indices)
    if any(block.page_index != heading.page_index for block in blocks):
        return False
    return all(_is_mechanical_front_matter_block(block) for block in blocks)


def _is_mechanical_front_matter_block(block: ProviderBlock) -> bool:
    nonblank_payloads = tuple(
        payload.text.strip() for payload in block.payloads if payload.text.strip()
    )
    if block.referenced_artifact_roles and not nonblank_payloads:
        return True
    return bool(nonblank_payloads) and all(
        _is_exchange_identity_text(text) for text in nonblank_payloads
    )


def _is_exchange_identity_text(text: str) -> bool:
    """Accept a complete sequence of closed exchange identity label/value pairs."""

    normalized = " ".join(text.split())
    labels = tuple(_FRONT_MATTER_IDENTITY_LABEL_RE.finditer(normalized))
    if not labels or normalized[: labels[0].start()].strip():
        return False
    for index, label in enumerate(labels):
        value_start = label.end()
        if value_start >= len(normalized) or normalized[value_start] not in {":", "："}:
            return False
        value_end = labels[index + 1].start() if index + 1 < len(labels) else len(normalized)
        value = normalized[value_start + 1 : value_end].strip()
        if (
            not value
            or len(value) > _FRONT_MATTER_IDENTITY_VALUE_MAX_CHARS
            or any(mark in value for mark in _FRONT_MATTER_IDENTITY_SENTENCE_MARKS)
        ):
            return False
    return True


__all__ = ["build_document_outline"]
