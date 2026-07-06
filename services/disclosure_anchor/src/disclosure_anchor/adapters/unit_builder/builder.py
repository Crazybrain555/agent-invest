"""Pure S1-S7 document_unit builder stages."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import re
import unicodedata
from typing import Any, Callable, Iterable

from disclosure_anchor.adapters.unit_builder import rules


ImageBytesResolver = Callable[[str], bytes]


@dataclass(frozen=True)
class PreparedElement:
    kind: str
    order_index: int
    text: str | None = None
    raw_kind: str | None = None
    page_no: int | None = None
    heading_level: int | None = None
    table: dict[str, Any] | None = None
    table_caption: list[str] = field(default_factory=list)
    table_footnote: list[str] = field(default_factory=list)
    table_html: str | None = None
    table_parse_failed: bool = False
    image_path: str | None = None
    payload: dict[str, Any] | None = None
    quality_status: str = "ok"
    artifact_locator: dict[str, Any] | None = None
    heading_path: list[str] = field(default_factory=list)
    title: str | None = None


@dataclass(frozen=True)
class UnitDraft:
    payload_kind: str
    payload: dict[str, Any]
    source_order: int
    intra_order: int = 0
    heading_path: list[str] = field(default_factory=list)
    title: str | None = None
    semantic_key: str | None = None
    semantic_keys: list[str] | None = None
    quality_status: str = "ok"
    applicability: str | None = None
    artifact_locator: dict[str, Any] | None = None


@dataclass
class BuildStats:
    generated_by_kind: Counter[str] = field(default_factory=Counter)
    dropped_by_kind: Counter[str] = field(default_factory=Counter)
    dropped_unknown_by_raw_kind: Counter[str] = field(default_factory=Counter)
    skipped_sections: list[str] = field(default_factory=list)
    dropped_cover_prelude: int = 0
    dropped_unit_declarations: int = 0
    stripped_marker_lines: int = 0
    stripped_header_lines: int = 0
    merged_tables: int = 0
    dropped_blank_table_rows: int = 0
    dropped_boilerplate_lines: int = 0
    grouped_proposal_units: int = 0
    grouped_section_units: int = 0
    collapsed_documents: int = 0
    anchored_header_units: int = 0
    needs_review_count: int = 0
    unusable_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_by_kind": dict(self.generated_by_kind),
            "dropped_by_kind": dict(self.dropped_by_kind),
            "dropped_unknown_by_raw_kind": dict(self.dropped_unknown_by_raw_kind),
            "skipped_sections": list(self.skipped_sections),
            "dropped_cover_prelude": self.dropped_cover_prelude,
            "dropped_unit_declarations": self.dropped_unit_declarations,
            "stripped_marker_lines": self.stripped_marker_lines,
            "stripped_header_lines": self.stripped_header_lines,
            "merged_tables": self.merged_tables,
            "dropped_blank_table_rows": self.dropped_blank_table_rows,
            "dropped_boilerplate_lines": self.dropped_boilerplate_lines,
            "grouped_proposal_units": self.grouped_proposal_units,
            "grouped_section_units": self.grouped_section_units,
            "collapsed_documents": self.collapsed_documents,
            "anchored_header_units": self.anchored_header_units,
            "needs_review_count": self.needs_review_count,
            "unusable_count": self.unusable_count,
        }


@dataclass(frozen=True)
class Stage1Result:
    elements: list[PreparedElement]
    stats: BuildStats


@dataclass(frozen=True)
class QaParseResult:
    units: list[UnitDraft]
    unstable: bool = False


def s1_preprocess_elements(
    elements: Iterable[dict[str, Any]],
    *,
    image_bytes_resolver: ImageBytesResolver | None = None,
) -> Stage1Result:
    stats = BuildStats()
    prepared: list[PreparedElement] = []
    previous_non_furniture: PreparedElement | None = None

    for element in elements:
        kind = str(element.get("kind", "unknown"))
        order_index = int(element.get("order_index", len(prepared)))
        raw_kind = str(element.get("raw_kind", kind))
        page_no = _int_or_none(element.get("page_no"))
        if kind == "page_furniture":
            stats.dropped_by_kind[kind] += 1
            continue
        if kind in {"text", "heading", "equation"}:
            text = _clean_text(_element_text(element))
            if not text:
                stats.dropped_by_kind[kind] += 1
                continue
            output_kind = "text" if kind == "equation" else kind
            item = PreparedElement(
                kind=output_kind,
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                heading_level=_int_or_none(element.get("heading_level")),
                text=text,
                artifact_locator=_artifact_locator(element),
            )
            prepared.append(item)
            previous_non_furniture = item
            continue
        if kind == "unknown":
            text = _clean_text(_element_text(element))
            if not text:
                stats.dropped_by_kind[kind] += 1
                stats.dropped_unknown_by_raw_kind[raw_kind] += 1
                continue
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                heading_level=_int_or_none(element.get("heading_level")),
                text=text,
                quality_status="needs_review",
                artifact_locator=_artifact_locator(element),
            )
            prepared.append(item)
            previous_non_furniture = item
            continue
        if kind == "image":
            caption = _clean_text(_caption_text(element))
            context = _image_context(previous_non_furniture, page_no)
            if caption or context:
                image_ref = _content_addressed_image_ref(
                    str(element.get("image_path") or ""),
                    image_bytes_resolver=image_bytes_resolver,
                )
                item = PreparedElement(
                    kind="text",
                    order_index=order_index,
                    raw_kind=raw_kind,
                    page_no=page_no,
                    payload={
                        "image_ref": image_ref,
                        "caption": caption,
                        "context": context,
                    },
                    title=context or caption or None,
                    quality_status="needs_review",
                    artifact_locator=_artifact_locator(element),
                )
                prepared.append(item)
                previous_non_furniture = item
            else:
                stats.dropped_by_kind[kind] += 1
            continue
        if kind == "table":
            # MinerU misattaches checkbox declarations ("是 □否") as table
            # captions; they would leak into unit titles.
            captions: list[str] = []
            for caption in element.get("table_caption") or []:
                if rules.is_declaration_line(str(caption)):
                    stats.stripped_marker_lines += 1
                    continue
                captions.append(str(caption))
            item = PreparedElement(
                kind="table",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                heading_level=_int_or_none(element.get("heading_level")),
                table=dict(element.get("table") or {"headers": [], "rows": []}),
                table_caption=captions,
                table_footnote=[str(item) for item in element.get("table_footnote") or []],
                table_html=element.get("table_html"),
                table_parse_failed=bool(element.get("table_parse_failed")),
                artifact_locator=_artifact_locator(element),
            )
            prepared.append(item)
            previous_non_furniture = item
            continue
        stats.dropped_by_kind[kind] += 1

    return Stage1Result(elements=prepared, stats=stats)


def s2_apply_heading_tree(
    elements: Iterable[PreparedElement],
    *,
    qa_heading_mode: bool = False,
) -> list[PreparedElement]:
    # (level, title, ordinal) — the ordinal drives numbering-continuity repair.
    stack: list[tuple[int, str, int | None]] = []
    placed: list[PreparedElement] = []
    for element in elements:
        if qa_heading_mode and element.kind == "heading" and _numbered_line(element.text or ""):
            heading_path = [title for _, title, _ in stack]
            placed.append(
                PreparedElement(
                    **{
                        **element.__dict__,
                        "kind": "text",
                        "heading_path": heading_path,
                        "title": element.title or (heading_path[-1] if heading_path else None),
                    }
                )
            )
            continue
        level = _heading_level_for(element)
        if level is not None:
            text = (element.text or "").strip()
            ordinal = _heading_ordinal(text)
            if _pattern_heading_level(text) is not None or _normalized_title(text) in rules.FIXED_L1_TITLES:
                level = _repair_level_by_continuity(stack, level, ordinal)
            elif element.heading_level == 1:
                # An explicit MinerU level-1 heading is a document-top block
                # (title page, 重要提示-class): keep it top-level.
                level = 1
            else:
                # Unnumbered MinerU heading (sub-label like 安全生产费): its
                # raw heading_level is unreliable (flattened to 2 on real
                # filings) and used to evict the numbered parent — nest under
                # the current context instead (Codex round5).
                level = (stack[-1][0] if stack else 0) + 1
            candidate_path = [title for lvl, title, _ in stack if lvl < level] + [element.text or ""]
            if len(candidate_path) <= 4:
                stack = [entry for entry in stack if entry[0] < level]
                stack.append((level, element.text or "", ordinal))
                continue
        heading_path = [title for _, title, _ in stack]
        title = element.title or (heading_path[-1] if heading_path else None)
        element_values = dict(element.__dict__)
        if element.kind == "heading":
            element_values["kind"] = "text"
        placed.append(
            PreparedElement(
                **{
                    **element_values,
                    "heading_path": heading_path,
                    "title": title,
                }
            )
        )
    return placed


_CN_NUMERALS = {c: i for i, c in enumerate("零一二三四五六七八九", start=0)}


def _cn_ordinal(text: str) -> int | None:
    """Parse 一/十/十二/二十一/百-scale Chinese ordinals."""

    if not text:
        return None
    total, unit_value, digit = 0, 0, 0
    for char in text:
        if char in _CN_NUMERALS:
            digit = _CN_NUMERALS[char]
        elif char == "十":
            unit_value = (digit or 1) * 10
            total += unit_value
            digit = 0
        elif char == "百":
            total += (digit or 1) * 100
            digit = 0
        else:
            return None
    return total + digit or None


_ORDINAL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^第([一二三四五六七八九十百]+)[节章]"),
    re.compile(r"^([一二三四五六七八九十百]+)、"),
    re.compile(r"^[（(]([一二三四五六七八九十百]+)[）)]"),
    re.compile(r"^[（(](\d+)[）)]"),
    re.compile(r"^(\d+)[.、．)）\s]"),
)


def _heading_ordinal(text: str) -> int | None:
    for pattern in _ORDINAL_RES:
        match = pattern.match(text)
        if match:
            token = match.group(1)
            return int(token) if token.isdigit() else _cn_ordinal(token)
    return None


def _pattern_heading_level(text: str) -> int | None:
    for level, pattern in rules.HEADING_PATTERNS:
        if pattern.match(text):
            return level
    return None


def _repair_level_by_continuity(
    stack: list[tuple[int, str, int | None]], level: int, ordinal: int | None
) -> int:
    """Re-level a numbered heading whose ordinal breaks its own sequence.

    Real filings misnumber: the 江海 annual prints 三、（市场风险） where
    （三）市场风险 was meant, and the L2-style prefix used to evict the open
    十二、金融工具风险 section. If the ordinal does not continue the open
    sequence at its pattern level but exactly continues another OPEN level's
    sequence, the heading belongs there. Ordinal 1 always starts a fresh
    sequence at its own level. Repair is DEMOTION-ONLY: a heading may sink
    into a deeper open sequence, never rise above its pattern level — the
    附注科目 chain (9、…44、) would otherwise latch onto 第八节's ordinal 8
    at level 1 and evict the whole tree (observed on the real 江海 annual).
    """

    if ordinal is None or ordinal <= 1:
        return level
    at_level = {lvl: ord_ for lvl, _, ord_ in stack}
    own = at_level.get(level)
    if own is not None and ordinal == own + 1:
        return level
    for lvl, _, ord_ in reversed(stack):
        if lvl > level and ord_ is not None and ordinal == ord_ + 1:
            return lvl
    return level


def s3_build_text_units(
    elements: Iterable[PreparedElement], *, stats: BuildStats | None = None
) -> list[UnitDraft]:
    units: list[UnitDraft] = []
    buffer: list[PreparedElement] = []

    def flush() -> None:
        if not buffer:
            return
        text = "\n".join(item.text or "" for item in buffer if item.text).strip()
        text, applicability = _strip_declaration_lines(text, stats=stats)
        if text:
            quality = "needs_review" if any(item.quality_status == "needs_review" for item in buffer) else "ok"
            units.append(
                UnitDraft(
                    payload_kind="text",
                    payload={"text": text},
                    source_order=buffer[0].order_index,
                    heading_path=list(buffer[0].heading_path),
                    title=buffer[0].title,
                    quality_status=quality,
                    applicability=applicability,
                    artifact_locator=buffer[0].artifact_locator,
                )
            )
        elif applicability == "applicable":
            # Placeholder so _sink_leading_applicable can move the flag onto
            # the section content that follows (usually a table).
            units.append(
                UnitDraft(
                    payload_kind="text",
                    payload={"text": ""},
                    source_order=buffer[0].order_index,
                    heading_path=list(buffer[0].heading_path),
                    title=buffer[0].title,
                    applicability="applicable",
                )
            )
        buffer.clear()

    for element in elements:
        if element.kind == "text" and element.payload is None:
            if buffer and element.heading_path != buffer[-1].heading_path:
                flush()
            buffer.append(element)
        else:
            flush()
            if element.kind == "text" and element.payload is not None:
                units.append(
                    UnitDraft(
                        payload_kind="text",
                        payload=element.payload,
                        source_order=element.order_index,
                        heading_path=list(element.heading_path),
                        title=element.title,
                        quality_status=element.quality_status,
                        artifact_locator=element.artifact_locator,
                    )
                )
    flush()
    return units


def s4_build_qa_units(text: str, *, source: UnitDraft) -> QaParseResult:
    lines = _qa_lines(text)
    allow_numbered_question = any(
        rules.ANSWER_START_RE.match(line.strip()) for line in lines
    )
    current_question: str | None = None
    answer_lines: list[str] = []
    raw_lines: list[str] = []
    seen_answer = False
    units: list[UnitDraft] = []
    unstable = False

    def emit() -> None:
        nonlocal current_question, answer_lines, raw_lines, seen_answer, unstable
        if current_question is None:
            return
        answer = "\n".join(line for line in answer_lines if line.strip()).strip()
        if not answer:
            unstable = True
            return
        units.append(
            UnitDraft(
                payload_kind="qa",
                payload={
                    "question": current_question.strip(),
                    "answer": answer,
                    "raw_text": "\n".join(raw_lines).strip(),
                },
                source_order=source.source_order,
                intra_order=source.intra_order + len(units),
                heading_path=list(source.heading_path),
                title=source.title,
                quality_status=source.quality_status,
                artifact_locator=source.artifact_locator,
            )
        )
        current_question = None
        answer_lines = []
        raw_lines = []
        seen_answer = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if rules.QUESTION_START_RE.match(stripped) or (
            allow_numbered_question and _numbered_line(stripped)
        ):
            if current_question is not None:
                emit()
                if unstable:
                    break
            current_question = _strip_question_prefix(stripped)
            raw_lines = [stripped]
            answer_lines = []
            seen_answer = False
            continue
        if rules.ANSWER_START_RE.match(stripped):
            if current_question is None:
                unstable = True
                break
            if seen_answer:
                unstable = True
                break
            seen_answer = True
            raw_lines.append(stripped)
            answer_lines.append(_strip_answer_prefix(stripped))
            continue
        if current_question is not None:
            raw_lines.append(stripped)
            if seen_answer:
                answer_lines.append(stripped)
            else:
                answer_lines.append(stripped)

    if not unstable:
        emit()
    if unstable:
        return QaParseResult(units=[], unstable=True)
    return QaParseResult(units=units, unstable=False)


def replace_text_units_with_qa_where_stable(units: Iterable[UnitDraft]) -> list[UnitDraft]:
    output: list[UnitDraft] = []
    for unit in units:
        if unit.payload_kind != "text" or "text" not in unit.payload:
            output.append(unit)
            continue
        result = s4_build_qa_units(str(unit.payload["text"]), source=unit)
        if result.unstable:
            output.append(
                UnitDraft(
                    **{
                        **unit.__dict__,
                        "quality_status": "needs_review",
                    }
                )
            )
        elif result.units:
            output.extend(result.units)
        else:
            output.append(unit)
    return output


def _flag_shredded_qa_table(unit: UnitDraft) -> UnitDraft:
    """Flag Q&A transcripts that MinerU mis-detected as a table.

    Form-style 投资者关系记录表 sometimes arrive with the transcript shredded
    across cells (sentences split mid-way, question titles inside headers) —
    unrecoverable at build time. Mark needs_review so L2 skips the soup; the
    raw artifact stays reprocessable (§3.5).
    """

    if unit.payload_kind != "table" or unit.quality_status != "ok":
        return unit
    text = _main_text(unit)
    if len(text) < rules.QA_TABLE_CONTENT_MIN_CHARS:
        return unit
    if not rules.QA_TABLE_MARKER_RE.search(text):
        return unit
    return UnitDraft(**{**unit.__dict__, "quality_status": "needs_review"})


def _drop_blank_rows_adjusting(
    rows: list[list[str]],
    merged_cells: list[dict[str, int]],
    stats: BuildStats | None,
) -> tuple[list[list[str]], list[dict[str, int]]]:
    """Drop all-blank rows; merged_cells row indices shift with the kept rows."""

    kept: list[list[str]] = []
    index_map: dict[int, int] = {}
    for index, row in enumerate(rows):
        if any(str(cell).strip() for cell in row):
            index_map[index] = len(kept)
            kept.append(row)
    if stats is not None:
        stats.dropped_blank_table_rows += len(rows) - len(kept)
    if len(kept) == len(rows):
        return rows, merged_cells
    adjusted = [
        {**cell, "row": index_map[int(cell["row"])]}
        for cell in merged_cells
        if int(cell["row"]) in index_map
    ]
    return kept, adjusted


def s5_build_table_units(elements: Iterable[PreparedElement], stats: BuildStats) -> list[UnitDraft]:
    items = list(elements)
    units: list[UnitDraft] = []
    index = 0
    while index < len(items):
        element = items[index]
        if element.kind != "table":
            index += 1
            continue
        if _is_empty_table_element(element):
            stats.dropped_by_kind["table_empty"] += 1
            index += 1
            continue
        group = [element]
        index += 1
        while index < len(items):
            candidate = items[index]
            if _is_empty_table_element(candidate):
                stats.dropped_by_kind["table_empty"] += 1
                index += 1
                continue
            if candidate.kind == "table" and _can_merge_continued_table(group[-1], candidate):
                group.append(candidate)
                stats.merged_tables += 1
                index += 1
                continue
            break
        previous_text = _previous_text_before(items, element.order_index)
        units.append(_table_group_to_unit(group, previous_text=previous_text, stats=stats))
    return units


def s6_filter_units(units: Iterable[UnitDraft], stats: BuildStats) -> list[UnitDraft]:
    kept: list[UnitDraft] = []
    for unit in units:
        skip_title = _matching_skip_title(unit)
        if skip_title is not None:
            stats.skipped_sections.append(skip_title)
            continue
        if unit.payload_kind == "table" and _table_payload_is_empty(unit.payload):
            stats.dropped_by_kind["table_empty"] += 1
            continue
        kept.append(unit)
    return kept


def s7_finalize_units(
    units: Iterable[UnitDraft],
    *,
    filing_type: str | None,
    stats: BuildStats,
) -> list[UnitDraft]:
    finalized: list[UnitDraft] = []
    for unit in units:
        semantic_key = unit.semantic_key or semantic_key_for_unit(unit, filing_type=filing_type)
        keys = set(unit.semantic_keys or ())
        if semantic_key:
            keys.add(semantic_key)
        quality_status = _final_quality_status(unit)
        if quality_status == "needs_review":
            stats.needs_review_count += 1
        if quality_status == "unusable":
            stats.unusable_count += 1
        stats.generated_by_kind[unit.payload_kind] += 1
        finalized.append(
            UnitDraft(
                **{
                    **unit.__dict__,
                    "semantic_key": semantic_key,
                    "semantic_keys": sorted(keys) or None,
                    "quality_status": quality_status,
                }
            )
        )
    return finalized


def build_unit_drafts_s1_s7(
    normalized_ir: dict[str, Any],
    *,
    filing_type: str | None,
    document_title: str | None = None,
    image_bytes_resolver: ImageBytesResolver | None = None,
) -> tuple[list[UnitDraft], BuildStats]:
    s1 = s1_preprocess_elements(
        normalized_ir.get("elements", []),
        image_bytes_resolver=image_bytes_resolver,
    )
    elements = _drop_cover_prelude(s1.elements, stats=s1.stats)
    placed = s2_apply_heading_tree(
        elements,
        qa_heading_mode=filing_type in {"investor_relations", "performance_briefing"},
    )
    text_units = replace_text_units_with_qa_where_stable(
        s3_build_text_units(placed, stats=s1.stats)
    )
    table_units = s5_build_table_units(placed, s1.stats)
    if filing_type in {"investor_relations", "performance_briefing"}:
        table_units = [_flag_shredded_qa_table(unit) for unit in table_units]
    table_qa_units = _qa_units_from_tables(table_units)
    units = sorted([*text_units, *table_units, *table_qa_units], key=_unit_sort_key)
    units = _sink_leading_applicable(units)
    kept = s6_filter_units(units, s1.stats)
    kept = _anchor_headerless_units(kept, document_title=document_title, stats=s1.stats)
    kept = s8_group_semantic_units(
        kept, filing_type=filing_type, document_title=document_title, stats=s1.stats
    )
    return s7_finalize_units(kept, filing_type=filing_type, stats=s1.stats), s1.stats


def _anchor_headerless_units(
    units: list[UnitDraft], *, document_title: str | None = None, stats: BuildStats
) -> list[UnitDraft]:
    """Anchor pre-first-heading units under a stable synthetic heading.

    Announcement-header remnants (公告编号 lines, letterhead) sit before the
    first in-document heading and used to surface with heading_path=[], which
    breaks L2 retrieval and replay anchoring (round3 P0#4). Fully flat
    documents (no headings anywhere) are left untouched — inventing structure
    there would be worse than none.
    """

    fully_flat = not any(unit.heading_path for unit in units)
    if fully_flat and not document_title:
        return units
    # Pre-first-heading remnants anchor under 公告头信息; a fully flat document
    # (MinerU form-table filings) anchors under its registry title instead of
    # inventing structure (Codex round7: IR units with heading_path=[]).
    anchor = document_title if fully_flat else rules.DOCUMENT_HEADER_ANCHOR
    out: list[UnitDraft] = []
    for unit in units:
        if unit.heading_path:
            out.append(unit)
            continue
        stats.anchored_header_units += 1
        out.append(
            UnitDraft(
                **{
                    **unit.__dict__,
                    "heading_path": [anchor],
                    "title": unit.title or (anchor if fully_flat else None),
                }
            )
        )
    return out


def s8_group_semantic_units(
    units: list[UnitDraft],
    *,
    filing_type: str | None,
    document_title: str | None = None,
    stats: BuildStats,
) -> list[UnitDraft]:
    """Regroup technical slices into business-semantic units (round3 P0#1).

    L2-facing units must express a complete business fact: a meeting proposal
    (审议结果 + 表决表格 + 会议决定) becomes ONE mixed unit with ordered parts,
    and a short filing without proposal structure becomes ONE document-level
    mixed unit. QA-mode filings are already semantic and are never regrouped.
    """

    if filing_type in {"investor_relations", "performance_briefing"}:
        return units
    grouped, made_proposals = _group_proposal_units(
        _split_units_at_proposal_anchors(units), filing_type=filing_type, stats=stats
    )
    if made_proposals:
        # Proposals are done; the surrounding units (会议召开情况, 律师意见)
        # still deserve section grouping — mixed units never regroup.
        return _group_section_units(grouped, filing_type=filing_type, stats=stats)
    collapsed = _collapse_short_document(
        grouped, filing_type=filing_type, document_title=document_title, stats=stats
    )
    if collapsed is not None:
        return collapsed
    return _group_section_units(grouped, filing_type=filing_type, stats=stats)


def _split_units_at_proposal_anchors(units: list[UnitDraft]) -> list[UnitDraft]:
    """Split text units at in-body proposal starts (\\d+.议案名称：…).

    The next proposal's title often begins inside the previous proposal's last
    text block (observed on the real 贵州茅台 股东会决议公告); without the split
    the following proposal inherits the wrong heading attribution.
    """

    out: list[UnitDraft] = []
    for unit in units:
        if unit.payload_kind != "text" or "text" not in unit.payload:
            out.append(unit)
            continue
        lines = str(unit.payload["text"]).splitlines()
        inner_anchors = [
            index
            for index, line in enumerate(lines)
            if index > 0 and rules.match_proposal_anchor(line.strip())
        ]
        if not inner_anchors:
            out.append(unit)
            continue
        boundaries = [0, *inner_anchors, len(lines)]
        for offset, start in enumerate(boundaries[:-1]):
            text = "\n".join(lines[start : boundaries[offset + 1]]).strip()
            if not text:
                continue
            out.append(
                UnitDraft(
                    **{
                        **unit.__dict__,
                        "payload": {"text": text},
                        "intra_order": unit.intra_order + offset,
                        "applicability": unit.applicability if offset == 0 else None,
                    }
                )
            )
    return out


def _group_proposal_units(
    units: list[UnitDraft], *, filing_type: str | None, stats: BuildStats
) -> tuple[list[UnitDraft], bool]:
    out: list[UnitDraft] = []
    current: dict[str, Any] | None = None
    seen_titles: set[str] = set()
    made = False

    def close() -> None:
        nonlocal current
        if current is not None:
            out.append(_proposal_to_unit(current, filing_type=filing_type, stats=stats))
            current = None

    for unit in units:
        anchor = _proposal_anchor(unit)
        if anchor is not None:
            anchor_kind, title, parent_path, members = anchor
            if current is not None and title == current["title"]:
                current["members"].append(unit)
                continue
            if (
                anchor_kind == "heading"
                and title in seen_titles
                and current is not None
            ):
                # Stale heading attribution: units after an in-text proposal
                # start still carry the previous proposal's heading. They
                # belong to the currently open proposal.
                current["members"].append(unit)
                continue
            close()
            made = True
            seen_titles.add(title)
            current = {
                "title": title,
                "parent_path": parent_path,
                "members": members,
                "first": unit,
            }
            continue
        if current is not None and _joins_proposal(unit, current["parent_path"]):
            current["members"].append(unit)
            continue
        close()
        out.append(unit)
    close()
    return out, made


def _proposal_anchor(
    unit: UnitDraft,
) -> tuple[str, str, list[str], list[UnitDraft]] | None:
    """Detect a proposal start: (kind, title, parent_path, initial members)."""

    if unit.payload_kind == "text" and "text" in unit.payload:
        lines = str(unit.payload["text"]).splitlines()
        if lines and rules.match_proposal_anchor(lines[0].strip()):
            title = lines[0].strip()
            remainder = "\n".join(lines[1:]).strip()
            members: list[UnitDraft] = []
            if remainder:
                members.append(
                    UnitDraft(**{**unit.__dict__, "payload": {"text": remainder}})
                )
            return ("text", title, _strip_anchor_suffix(unit.heading_path), members)
    if unit.payload_kind == "table":
        caption = unit.payload.get("caption") or []
        first = str(caption[0]).strip() if caption else ""
        if first and rules.match_proposal_anchor(first):
            return (
                "table_caption",
                first,
                _strip_anchor_suffix(unit.heading_path),
                [unit],
            )
    path = unit.heading_path
    if path and rules.match_proposal_anchor(path[-1].strip()):
        return ("heading", path[-1].strip(), _strip_anchor_suffix(path), [unit])
    return None


def _strip_anchor_suffix(path: list[str]) -> list[str]:
    parent = list(path)
    while parent and rules.match_proposal_anchor(parent[-1].strip()):
        parent.pop()
    return parent


def _joins_proposal(unit: UnitDraft, parent_path: list[str]) -> bool:
    normalized = _strip_anchor_suffix(unit.heading_path)
    return normalized[: len(parent_path)] == parent_path


def _proposal_to_unit(
    current: dict[str, Any], *, filing_type: str | None, stats: BuildStats
) -> UnitDraft:
    members = [
        member for member in current["members"] if not _is_blank_text_unit(member)
    ]
    first: UnitDraft = current["first"]
    title: str = current["title"]
    heading_path = list(current["parent_path"]) or [title]
    if not members:
        return UnitDraft(
            payload_kind="text",
            payload={"text": title},
            source_order=first.source_order,
            intra_order=first.intra_order,
            heading_path=heading_path,
            title=title,
            quality_status=first.quality_status,
            artifact_locator=first.artifact_locator,
        )
    stats.grouped_proposal_units += 1
    return UnitDraft(
        payload_kind="mixed",
        payload={
            "semantic_type": "meeting_proposal",
            "parts": [
                _unit_part(member, include_heading=False) for member in members
            ],
        },
        source_order=first.source_order,
        intra_order=first.intra_order,
        heading_path=heading_path,
        title=title,
        semantic_keys=_member_semantic_keys(members, filing_type=filing_type),
        quality_status=_worst_quality(members),
        artifact_locator=_merged_locator(members),
    )


def _collapse_short_document(
    units: list[UnitDraft],
    *,
    filing_type: str | None,
    document_title: str | None = None,
    stats: BuildStats,
) -> list[UnitDraft] | None:
    """Collapse a short filing into one document-level unit, or None to decline."""

    if filing_type not in rules.COLLAPSIBLE_FILING_TYPES:
        return None
    real = [unit for unit in units if not _is_blank_text_unit(unit)]
    if len(real) < 2:
        return None
    if sum(len(_main_text(unit)) for unit in real) > rules.SHORT_DOC_CONTENT_CHARS:
        return None
    title = document_title or next(
        (
            unit.heading_path[0]
            for unit in real
            if unit.heading_path
            and unit.heading_path[0] != rules.DOCUMENT_HEADER_ANCHOR
        ),
        None,
    )
    stats.collapsed_documents += 1
    first = real[0]
    return [
        UnitDraft(
            payload_kind="mixed",
            payload={
                "semantic_type": "document",
                "parts": [
                    _unit_part(unit, include_heading=True) for unit in real
                ],
            },
            source_order=first.source_order,
            intra_order=first.intra_order,
            heading_path=[title] if title else [rules.DOCUMENT_HEADER_ANCHOR],
            title=title,
            semantic_keys=_member_semantic_keys(real, filing_type=filing_type),
            quality_status=_worst_quality(real),
            artifact_locator=_merged_locator(real),
        )
    ]


def _group_section_units(
    units: list[UnitDraft], *, filing_type: str | None, stats: BuildStats
) -> list[UnitDraft]:
    """Merge each business section's text/table/image slices into one unit.

    The grouping node is the shallowest heading whose subtree stays within
    rules.SECTION_GROUP_MAX_CHARS: 研发投入 (intro text + expense table +
    personnel table) becomes ONE mixed unit instead of a text/table/table
    scatter (round3 P0#1, 长年报 clause). qa units are already complete
    business units and never join a group; a single-member group keeps its
    original payload_kind untouched.
    """

    sizes: dict[tuple[str, ...], int] = {}
    for unit in units:
        chars = len(_main_text(unit))
        path = tuple(unit.heading_path)
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            sizes[prefix] = sizes.get(prefix, 0) + chars

    def key_for(unit: UnitDraft) -> tuple[str, ...] | None:
        # qa units are complete; mixed units are already grouped (no nesting).
        if unit.payload_kind in {"qa", "mixed"}:
            return None
        path = tuple(unit.heading_path)
        if not path:
            return None
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            if sizes[prefix] <= rules.SECTION_GROUP_MAX_CHARS:
                return prefix
        # Oversized leaf: still one business topic — merge at the leaf.
        return path

    out: list[UnitDraft] = []
    group: list[UnitDraft] = []
    group_key: tuple[str, ...] = ()

    def close() -> None:
        nonlocal group
        if group:
            out.extend(
                _section_group_to_units(
                    group, list(group_key), filing_type=filing_type, stats=stats
                )
            )
            group = []

    for unit in units:
        key = key_for(unit)
        if key is None:
            close()
            out.append(unit)
            continue
        if key != group_key:
            close()
            group_key = key
        group.append(unit)
    close()
    return out


def _section_group_to_units(
    members_all: list[UnitDraft],
    key: list[str],
    *,
    filing_type: str | None,
    stats: BuildStats,
) -> list[UnitDraft]:
    members = [unit for unit in members_all if not _is_blank_text_unit(unit)]
    if not members:
        return list(members_all)
    if len(members) == 1:
        return [members[0]]
    stats.grouped_section_units += 1
    first = members[0]
    return [
        UnitDraft(
            payload_kind="mixed",
            payload={
                "semantic_type": "section",
                "parts": [
                    _unit_part(member, include_heading=False, relative_to=key)
                    for member in members
                ],
            },
            source_order=first.source_order,
            intra_order=first.intra_order,
            heading_path=list(key),
            title=key[-1] if key else first.title,
            semantic_keys=_member_semantic_keys(members, filing_type=filing_type),
            quality_status=_worst_quality(members),
            applicability=_uniform_applicability(members),
            artifact_locator=_merged_locator(members),
        )
    ]


def _member_semantic_keys(
    members: list[UnitDraft], *, filing_type: str | None
) -> list[str] | None:
    """Recall keys of the grouped members — column-bound, never in payload.

    Embedding keys in parts would push a rules-derived value into content_hash
    (U2 forbids rule upgrades masquerading as content changes).
    """

    keys = {
        key
        for member in members
        if (
            key := member.semantic_key
            or semantic_key_for_unit(member, filing_type=filing_type)
        )
    }
    return sorted(keys) or None


def _uniform_applicability(members: list[UnitDraft]) -> str | None:
    """Unit-level flag only when the merged sections agree; parts keep detail."""

    flags = {unit.applicability for unit in members if unit.applicability}
    if len(flags) == 1:
        return next(iter(flags))
    return None


def _unit_part(
    unit: UnitDraft,
    *,
    include_heading: bool,
    relative_to: list[str] | None = None,
) -> dict[str, Any]:
    part: dict[str, Any] = {"kind": unit.payload_kind, "order": unit.source_order}
    if unit.payload_kind == "text" and "image_ref" in unit.payload:
        part["kind"] = "image"
    part.update(unit.payload)
    if include_heading and unit.heading_path:
        part["heading_path"] = list(unit.heading_path)
    if relative_to is not None and unit.heading_path[: len(relative_to)] == relative_to:
        local = unit.heading_path[len(relative_to) :]
        if local:
            part["local_heading"] = local
    if unit.applicability:
        part["applicability"] = unit.applicability
    if unit.quality_status != "ok":
        part["quality_status"] = unit.quality_status
    return part


def _is_blank_text_unit(unit: UnitDraft) -> bool:
    return (
        unit.payload_kind == "text"
        and "image_ref" not in unit.payload
        and not str(unit.payload.get("text", "")).strip()
    )


def _worst_quality(units: list[UnitDraft]) -> str:
    return (
        "needs_review"
        if any(unit.quality_status == "needs_review" for unit in units)
        else "ok"
    )


def _merged_locator(units: list[UnitDraft]) -> dict[str, Any] | None:
    locator = dict(units[0].artifact_locator or {})
    pages = [
        value
        for value in (
            (unit.artifact_locator or {}).get("page_no") for unit in units
        )
        if isinstance(value, int)
    ]
    if pages and min(pages) != max(pages):
        locator["page_span"] = [min(pages), max(pages)]
    return locator or None


def _drop_cover_prelude(
    elements: list[PreparedElement], *, stats: BuildStats
) -> list[PreparedElement]:
    """Drop cover-page text/headings before the first structural L1 section.

    Cover identity (company name, report title, announcement date, stock code)
    is already document metadata inherited by every unit, so repeating it as
    units is pure noise (protocol §3.5 稳定噪声). Only active when the document
    has a structural L1 heading at all — short announcements without 第X节
    structure are never touched. Drops are counted, never silent (D9).
    """

    # MinerU sometimes tags real structural headings (第一章 总则) as plain
    # text; requiring kind=='heading' here dropped whole opening chapters as
    # cover prelude (observed on 贵州茅台薪酬管理办法, 2026-07-06). A text
    # element counts when it would enter the heading tree via the same gate
    # _heading_level_for applies.
    first_structural = next(
        (
            index
            for index, element in enumerate(elements)
            if _is_structural_l1(element)
            and (
                element.kind == "heading"
                or _text_heading_candidate(element.text or "")
            )
        ),
        None,
    )
    if not first_structural:
        return elements
    kept: list[PreparedElement] = []
    for index, element in enumerate(elements):
        if index < first_structural and element.kind in {"heading", "text"}:
            stats.dropped_cover_prelude += 1
            continue
        kept.append(element)
    return kept


def _is_structural_l1(element: PreparedElement) -> bool:
    text = (element.text or "").strip()
    if not text:
        return False
    if _normalized_title(text) in rules.FIXED_L1_TITLES:
        return True
    level_one_pattern = rules.HEADING_PATTERNS[0][1]
    return bool(level_one_pattern.match(text))


def _unit_sort_key(unit: UnitDraft) -> tuple[int, int]:
    return (unit.source_order, unit.intra_order)


def semantic_key_for_unit(unit: UnitDraft, *, filing_type: str | None) -> str | None:
    text = " ".join(
        part
        for part in [
            unit.title or "",
            " ".join(unit.heading_path[-2:]),
            _table_caption_first(unit),
            str(unit.payload.get("question", "")) if unit.payload_kind == "qa" else "",
        ]
        if part
    )
    for rule in rules.SEMANTIC_KEY_RULES:
        if (
            rule.filing_type_limited
            and filing_type not in rules.SEMANTIC_LIMITED_FILING_TYPES
        ):
            continue
        if all(token in text for token in rule.required) and (
            not rule.any_required or any(token in text for token in rule.any_required)
        ):
            return rule.semantic_key
    return None


def _clean_text(value: str) -> str:
    cleaned = "".join(
        char
        for char in value
        if not (unicodedata.category(char) == "Cc" and char not in "\n\t")
    )
    lines = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if rules.NOISE_SEPARATOR_RE.match(stripped):
            continue
        if any(pattern.match(stripped) for pattern in rules.NOISE_LINE_PATTERNS):
            continue
        lines.append(line.strip())
    return "\n".join(line for line in lines if line).strip()


def _element_text(element: dict[str, Any]) -> str:
    for key in ("text", "latex", "content"):
        value = element.get(key)
        if value is not None:
            return str(value)
    return ""


def _caption_text(element: dict[str, Any]) -> str:
    for key in ("caption", "image_caption", "text"):
        value = element.get(key)
        if value is not None:
            if isinstance(value, list):
                return " ".join(str(item) for item in value)
            return str(value)
    return ""


def _image_context(previous: PreparedElement | None, page_no: int | None) -> str:
    if previous is None or previous.kind != "heading":
        return ""
    if previous.page_no != page_no:
        return ""
    return previous.text or ""


def _content_addressed_image_ref(
    image_path: str,
    *,
    image_bytes_resolver: ImageBytesResolver | None,
) -> str:
    filename = image_path.rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0]
    if re.fullmatch(r"[0-9a-fA-F]{64}", stem):
        return f"images/{filename}"
    if image_bytes_resolver is None:
        raise ValueError(f"image bytes required for non-hash image name: {image_path}")
    suffix = ""
    if "." in filename:
        suffix = "." + filename.rsplit(".", 1)[1]
    digest = hashlib.sha256(image_bytes_resolver(image_path)).hexdigest()
    return f"images/{digest}{suffix}"


def _artifact_locator(element: dict[str, Any]) -> dict[str, Any]:
    locator: dict[str, Any] = {"order_index": element.get("order_index")}
    if element.get("page_no") is not None:
        locator["page_no"] = element.get("page_no")
    if element.get("bbox") is not None:
        locator["bbox"] = element.get("bbox")
    return locator


def _heading_level_for(element: PreparedElement) -> int | None:
    text = (element.text or "").strip()
    if not text:
        return None
    if text.endswith(("?", "？")) or rules.QUESTION_START_RE.match(text):
        return None
    # MinerU occasionally tags applicability markers or yes/no checkbox
    # answers with text_level>=1; a declaration line must never enter the
    # heading tree (observed polluting heading_path/title in the real annual
    # corpus, 2026-07-06).
    if rules.is_declaration_line(text):
        return None
    # Table footnotes ([注1] …, 注：…) belong to the preceding table and must
    # never become headings (Codex round5: promoted to a unit title).
    if rules.FOOTNOTE_LINE_RE.match(text):
        return None
    normalized_title = _normalized_title(text)
    if normalized_title in rules.FIXED_L1_TITLES:
        return 1
    for level, pattern in rules.HEADING_PATTERNS:
        if pattern.match(text):
            if element.kind == "text" and not _text_heading_candidate(text):
                return None
            return level
    if element.kind == "heading" and element.heading_level is not None:
        return max(1, min(5, element.heading_level))
    return None


def _text_heading_candidate(text: str) -> bool:
    stripped = text.strip()
    return "\n" not in stripped and len(stripped) <= 40 and not stripped.endswith(
        ("。", "；", "，", ",")
    )


def _normalized_title(text: str) -> str:
    return re.sub(r"\s+", "", text).rstrip("：:")


def _strip_declaration_lines(
    text: str, *, stats: BuildStats | None
) -> tuple[str, str | None]:
    """Strip unit-declaration lines and a leading applicability marker.

    Returns the remaining text and the section applicability. A leading
    not_applicable marker with no other content keeps the marker line as the
    unit text (that IS the section's disclosure); a leading applicable marker
    with no other content returns empty text — the flag is sunk onto the next
    unit of the same section by _sink_leading_applicable.

    A marker on line two directly after a short label line (…说明 style) flags
    the unit without stripping, so composite declarations keep their prose.
    """

    if not text:
        return text, None
    lines = text.splitlines()
    kept: list[str] = []
    applicability: str | None = None
    for index, line in enumerate(lines):
        if rules.UNIT_DECLARATION_RE.fullmatch(line):
            if stats is not None:
                stats.dropped_unit_declarations += 1
            continue
        if rules.BOILERPLATE_GUARANTEE_RE.match(line):
            # Fixed board-guarantee legalese (§3.5 稳定噪声, user-authorized).
            if stats is not None:
                stats.dropped_boilerplate_lines += 1
            continue
        header_replacement = rules.strip_header_kv_line(line)
        if header_replacement is not None:
            if stats is not None:
                stats.stripped_header_lines += 1
            if header_replacement:
                kept.append(header_replacement)
            continue
        if (
            applicability is None
            and index == 0
            and rules.is_pure_marker_line(line)
        ):
            applicability = rules.classify_marker_line(line)
            if stats is not None:
                stats.stripped_marker_lines += 1
            if applicability == "not_applicable":
                kept.append(line.strip())
            continue
        if (
            applicability is None
            and index == 1
            and rules.is_pure_marker_line(line)
            and len(lines[0].strip()) <= 24
            and not lines[0].strip().endswith(("。", "；"))
        ):
            # Label-then-marker composite: flag it, keep the text intact.
            applicability = rules.classify_marker_line(line)
            kept.append(line)
            continue
        kept.append(line)
    return "\n".join(kept).strip(), applicability


def _same_or_child_section(candidate: list[str], section: list[str]) -> bool:
    """Child headings still belong to the declared section (prefix match)."""

    return candidate[: len(section)] == section


def _sink_leading_applicable(units: list[UnitDraft]) -> list[UnitDraft]:
    """Attach dangling √适用 declarations to the section content they open.

    An applicable marker with no prose means "content follows" (usually a
    table); the marker line itself must not survive as a unit (user decision
    2026-07-06) — the flag moves onto the immediately following unit of the
    same section. With no such sibling the declaration stays as its own unit.
    """

    out = list(units)
    dropped: set[int] = set()
    for index, unit in enumerate(out):
        dangling = (
            unit.payload_kind == "text"
            and unit.applicability == "applicable"
            and not str(unit.payload.get("text", "")).strip()
        )
        if not dangling:
            continue
        follower = index + 1
        if follower < len(out) and _same_or_child_section(
            out[follower].heading_path, unit.heading_path
        ):
            if out[follower].applicability is None:
                out[follower] = UnitDraft(
                    **{**out[follower].__dict__, "applicability": "applicable"}
                )
            dropped.add(index)
        else:
            out[index] = UnitDraft(**{**unit.__dict__, "payload": {"text": "适用"}})
    return [unit for index, unit in enumerate(out) if index not in dropped]


def _qa_lines(text: str) -> list[str]:
    prepared = re.sub(
        r"(?<!^)(?=\d+(?:[、．]|\.(?!\d))\s*)",
        "\n",
        text,
    )
    prepared = re.sub(
        r"([？?])(?=(答|回复|公司回复|A\d*)\s*[：:])",
        "\\1\n",
        prepared,
    )
    return prepared.splitlines()


def _numbered_line(text: str) -> bool:
    return bool(re.match(r"^\s*\d+(?:[、．]|\.(?!\d))\s*", text.strip()))


def _strip_question_prefix(text: str) -> str:
    text = re.sub(r"^\s*(问题|问|Q\d*|投资者提问|提问)\s*\d*\s*[：:]\s*", "", text)
    text = re.sub(r"^\s*\d+[、.．]\s*", "", text)
    return text.strip()


def _strip_answer_prefix(text: str) -> str:
    return re.sub(r"^\s*(答|回复|公司回复|A\d*)\s*[：:]\s*", "", text).strip()


def _is_empty_table_element(element: PreparedElement) -> bool:
    if element.kind != "table":
        return False
    table = element.table or {}
    return not table.get("headers") and not table.get("rows") and not (element.table_html or "")


def _column_count(element: PreparedElement) -> int | None:
    table = element.table or {}
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    if headers:
        return len(headers)
    if rows:
        return len(rows[0])
    return None


def _can_merge_continued_table(previous: PreparedElement, current: PreparedElement) -> bool:
    if current.table_caption:
        return False
    previous_count = _column_count(previous)
    current_count = _column_count(current)
    return previous_count is not None and previous_count == current_count


def _previous_text_before(elements: list[PreparedElement], order_index: int) -> str:
    for element in reversed(elements):
        if element.order_index >= order_index:
            continue
        if element.kind == "text" and element.text:
            return element.text
    return ""


def _table_group_to_unit(
    group: list[PreparedElement], *, previous_text: str, stats: BuildStats | None = None
) -> UnitDraft:
    first = group[0]
    if first.table_parse_failed:
        return UnitDraft(
            payload_kind="table",
            payload={
                "caption": list(first.table_caption),
                "raw_html": first.table_html or "",
                "notes": list(first.table_footnote),
            },
            source_order=first.order_index,
            heading_path=list(first.heading_path),
            title=_table_title(first),
            quality_status="needs_review",
            artifact_locator=first.artifact_locator,
        )

    headers, rows, merged_cells = _merged_table_grid(group)
    rows, merged_cells = _drop_blank_rows_adjusting(rows, merged_cells, stats)
    payload = {
        "caption": list(first.table_caption),
        "unit": _detect_unit(first, headers=headers, previous_text=previous_text),
        "headers": headers,
        "rows": rows,
        "notes": _merged_notes(group),
    }
    locator = dict(first.artifact_locator or {})
    if len(group) > 1:
        locator["merge_reason"] = "continued_table"
        page_numbers = [item.page_no for item in group if item.page_no is not None]
        if page_numbers:
            locator["page_span"] = [min(page_numbers), max(page_numbers)]
    if merged_cells:
        locator["merged_cells"] = merged_cells
    return UnitDraft(
        payload_kind="table",
        payload=payload,
        source_order=first.order_index,
        heading_path=list(first.heading_path),
        title=_table_title(first),
        quality_status="ok",
        artifact_locator=locator or None,
    )


def _merged_table_grid(group: list[PreparedElement]) -> tuple[list[str], list[list[str]], list[dict[str, int]]]:
    first_table = group[0].table or {}
    headers = [str(item) for item in first_table.get("headers") or []]
    rows = [[str(cell) for cell in row] for row in first_table.get("rows") or []]
    merged_cells = list(first_table.get("merged_cells") or [])
    header_candidate = headers or (rows[0] if rows else [])

    for element in group[1:]:
        table = element.table or {}
        next_rows = [[str(cell) for cell in row] for row in table.get("rows") or []]
        if header_candidate and next_rows and _same_cells(next_rows[0], header_candidate):
            next_rows = next_rows[1:]
        row_offset = len(rows)
        for merged_cell in table.get("merged_cells") or []:
            adjusted = dict(merged_cell)
            adjusted["row"] = int(adjusted["row"]) + row_offset
            merged_cells.append(adjusted)
        rows.extend(next_rows)

    if not headers and rows:
        headers = rows[0]
        rows = rows[1:]
    return headers, rows, merged_cells


def _same_cells(left: list[str], right: list[str]) -> bool:
    return [cell.strip() for cell in left] == [cell.strip() for cell in right]


def _merged_notes(group: list[PreparedElement]) -> list[str]:
    notes: list[str] = []
    for element in group:
        notes.extend(element.table_footnote)
    return notes


def _table_title(element: PreparedElement) -> str | None:
    return element.table_caption[0] if element.table_caption else element.title


def _detect_unit(
    element: PreparedElement,
    *,
    headers: list[str],
    previous_text: str,
) -> str | None:
    candidates = [*element.table_caption, *headers, previous_text]
    for candidate in candidates:
        match = re.search(r"单位[：:]\s*([^\s，。,；;）)]+)", candidate)
        if match:
            return match.group(1)
    return None


def _matching_skip_title(unit: UnitDraft) -> str | None:
    for title in [unit.title, *unit.heading_path]:
        if title is not None and _normalized_title(title) in rules.SKIP_SECTION_TITLES:
            return _normalized_title(title)
    return None


def _table_payload_is_empty(payload: dict[str, Any]) -> bool:
    if str(payload.get("raw_html") or "").strip():
        return False
    rows = payload.get("rows") or []
    if not rows:
        return True
    return all(not "".join(str(cell).strip() for cell in row) for row in rows)


def _final_quality_status(unit: UnitDraft) -> str:
    if _main_text_is_unusable(unit):
        return "unusable"
    if unit.quality_status == "needs_review":
        return "needs_review"
    return "ok"


def _main_text_is_unusable(unit: UnitDraft) -> bool:
    text = _main_text(unit)
    if not re.sub(r"\s+", "", text):
        return True
    total = len(text)
    bad = sum(
        1
        for char in text
        if char == "\ufffd"
        or (unicodedata.category(char).startswith("C") and char not in "\n\t\r")
    )
    return total > 0 and bad / total > rules.GIBBERISH_RATIO_MAX


def _main_text(unit: UnitDraft) -> str:
    if unit.payload_kind == "mixed":
        return " ".join(
            filter(None, (_part_text(part) for part in unit.payload.get("parts", [])))
        )
    if unit.payload_kind == "text":
        if "text" in unit.payload:
            return str(unit.payload.get("text") or "")
        return " ".join(str(value) for value in unit.payload.values() if value)
    if unit.payload_kind == "qa":
        return str(unit.payload.get("question") or "") + str(unit.payload.get("answer") or "")
    if unit.payload_kind == "table":
        if "raw_html" in unit.payload:
            return str(unit.payload.get("raw_html") or "")
        rows = unit.payload.get("rows") or []
        headers = unit.payload.get("headers") or []
        return " ".join(
            [str(cell) for cell in headers]
            + [str(cell) for row in rows for cell in row]
        )
    return ""


def _part_text(part: dict[str, Any]) -> str:
    kind = str(part.get("kind", "text"))
    if kind == "table":
        rows = part.get("rows") or []
        headers = part.get("headers") or []
        cells = [str(cell) for cell in headers] + [
            str(cell) for row in rows for cell in row
        ]
        return " ".join(cells) or str(part.get("raw_html") or "")
    if kind == "qa":
        return str(part.get("question") or "") + str(part.get("answer") or "")
    if kind == "image":
        return " ".join(
            filter(None, (str(part.get("caption") or ""), str(part.get("context") or "")))
        )
    return str(part.get("text") or "")


def _table_caption_first(unit: UnitDraft) -> str:
    caption = unit.payload.get("caption") if unit.payload_kind == "table" else None
    if isinstance(caption, list) and caption:
        return str(caption[0])
    return ""


def _qa_units_from_tables(table_units: Iterable[UnitDraft]) -> list[UnitDraft]:
    units: list[UnitDraft] = []
    for table in table_units:
        if table.payload_kind != "table":
            continue
        text_blocks = ["\n".join(str(cell) for cell in row) for row in table.payload.get("rows", [])]
        for offset, text in enumerate(text_blocks, start=100):
            result = s4_build_qa_units(text, source=table)
            for unit in result.units:
                units.append(
                    UnitDraft(
                        **{
                            **unit.__dict__,
                            "source_order": table.source_order,
                            "intra_order": table.intra_order + offset + len(units),
                        }
                    )
                )
    return units


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
