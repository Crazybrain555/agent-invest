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
    native_text_sections_recovered: int = 0
    qa_form_carriers_replaced: int = 0
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
            "native_text_sections_recovered": self.native_text_sections_recovered,
            "qa_form_carriers_replaced": self.qa_form_carriers_replaced,
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
    ordinals: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class _NativeSection:
    title: str
    body: str
    ordinal: int
    start_page_no: int
    end_page_no: int


@dataclass(frozen=True)
class _QaFormRecovery:
    elements: list[dict[str, Any]]
    section_count: int = 0
    replaced_carriers: int = 0


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
        text = (element.text or "").strip()
        if (
            qa_heading_mode
            and element.kind == "heading"
            and (element.artifact_locator or {}).get("source") == "native_text"
            and rules.QA_FORM_MAIN_SECTION_RE.match(text)
            and _heading_ordinal(text) == 1
        ):
            # A recovered official-form section run is a sibling of the form
            # metadata, not a child of MinerU's optional level-1 document
            # title.  Reset only at the proven native first section so normal
            # filing heading trees and ordinary QA documents are untouched.
            stack = []
        if qa_heading_mode and element.kind == "table" and element.table_caption:
            first_caption = str(element.table_caption[0]).strip()
            if rules.ATTACHMENT_CAPTION_RE.match(first_caption):
                # 附件是正文的兄弟节点(round17, 语料 11 例全部投关表单):
                # caption 命中即开新顶层分支, 本表与其后的延续元素都归属
                # 附件。仅表单模式——叙事文档的文中附件重置栈会错挂后续标题。
                stack = [(1, first_caption, None)]
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
    native_source = (source.artifact_locator or {}).get("source") == "native_text"
    allow_numbered_question = any(
        rules.ANSWER_START_RE.match(line.strip()) for line in lines
    )
    current_question_lines: list[str] = []
    current_ordinal: int | None = None
    answer_lines: list[str] = []
    raw_lines: list[str] = []
    seen_answer = False
    units: list[UnitDraft] = []
    ordinals: list[int] = []
    unstable = False

    def emit() -> None:
        nonlocal current_question_lines, current_ordinal
        nonlocal answer_lines, raw_lines, seen_answer, unstable
        if not current_question_lines:
            return
        question = _join_wrapped_lines(current_question_lines)
        answer = (
            _join_wrapped_lines(answer_lines)
            if native_source
            else "\n".join(line for line in answer_lines if line.strip()).strip()
        )
        if not question or not answer or (native_source and not seen_answer):
            unstable = True
            return
        units.append(
            UnitDraft(
                payload_kind="qa",
                payload={
                    "question": question,
                    "answer": answer,
                    "raw_text": "\n".join(raw_lines).strip(),
                },
                source_order=source.source_order,
                intra_order=source.intra_order + len(units),
                heading_path=list(source.heading_path),
                # A QA leaf is addressed by its question; the surrounding
                # section remains in heading_path ("三、主要交流问题").
                title=question,
                quality_status=source.quality_status,
                artifact_locator=source.artifact_locator,
            )
        )
        if current_ordinal is not None:
            ordinals.append(current_ordinal)
        current_question_lines = []
        current_ordinal = None
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
            if current_question_lines:
                emit()
                if unstable:
                    break
            current_question_lines = [_strip_question_prefix(stripped)]
            current_ordinal = _heading_ordinal(stripped)
            raw_lines = [stripped]
            answer_lines = []
            seen_answer = False
            continue
        if rules.ANSWER_START_RE.match(stripped):
            if not current_question_lines:
                unstable = True
                break
            if seen_answer:
                unstable = True
                break
            seen_answer = True
            raw_lines.append(stripped)
            answer_lines.append(_strip_answer_prefix(stripped))
            continue
        if current_question_lines:
            raw_lines.append(stripped)
            if seen_answer:
                answer_lines.append(stripped)
            elif native_source:
                # Native PDF text preserves hard line wraps.  Until the first
                # 答/回复 marker, a wrapped line is still part of the question,
                # not an answer fragment.
                current_question_lines.append(stripped)
            else:
                # Existing MinerU contract: a line after an explicit question
                # may be an unlabelled answer.
                answer_lines.append(stripped)

    if not unstable:
        emit()
    if unstable:
        return QaParseResult(units=[], unstable=True, ordinals=[])
    return QaParseResult(units=units, unstable=False, ordinals=ordinals)


def replace_text_units_with_qa_where_stable(units: Iterable[UnitDraft]) -> list[UnitDraft]:
    output: list[UnitDraft] = []
    for unit in units:
        if unit.payload_kind != "text" or "text" not in unit.payload:
            output.append(unit)
            continue
        result = s4_build_qa_units(str(unit.payload["text"]), source=unit)
        native_qa_section = (
            (unit.artifact_locator or {}).get("source") == "native_text"
            and any(
                rules.QA_FORM_QA_SECTION_RE.search(title)
                for title in [unit.title or "", *unit.heading_path]
            )
        )
        native_sequence_unstable = native_qa_section and (
            not result.units
            or len(result.ordinals) != len(result.units)
            or result.ordinals != list(range(1, len(result.ordinals) + 1))
        )
        if result.unstable or native_sequence_unstable:
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


def _drop_standalone_noise_units(
    units: list[UnitDraft], *, stats: BuildStats
) -> list[UnitDraft]:
    """Drop whole units that are bare labels or year fragments (round10).

    Only fires on standalone text units whose entire content matches the
    closed noise patterns; counted, never silent (D9). The raw artifact keeps
    the line for reprocessing.
    """

    kept: list[UnitDraft] = []
    for unit in units:
        if (
            unit.payload_kind == "text"
            and "image_ref" not in unit.payload
            and rules.is_standalone_noise(str(unit.payload.get("text", "")))
        ):
            stats.dropped_by_kind["standalone_noise"] += 1
            continue
        kept.append(unit)
    return kept


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


def _downgrade_qa_before_shredded_table(units: list[UnitDraft]) -> list[UnitDraft]:
    """A Q&A cut at a text→table page boundary is not a complete `ok` QA."""

    out = list(units)
    for index, unit in enumerate(out):
        if (
            unit.payload_kind != "table"
            or unit.quality_status != "needs_review"
            or not rules.QA_TABLE_MARKER_RE.search(_main_text(unit))
        ):
            continue
        previous = index - 1
        if previous < 0:
            continue
        candidate = out[previous]
        if candidate.payload_kind == "qa" and candidate.heading_path == unit.heading_path:
            out[previous] = UnitDraft(
                **{**candidate.__dict__, "quality_status": "needs_review"}
            )
    return out


def _reanchor_qa_form_footer(
    unit: UnitDraft, *, document_title: str | None
) -> UnitDraft:
    """投关记录表单尾字段表(附件清单/日期)归属文档本身, 不是最后一个叙事
    小节(round17 语料: 72 张表错挂在「三、主要交流问题」类标题下)。首列
    全部命中官方模板字段词表才判定, 叙事小节里的业务表格不受影响。"""

    if document_title is None or unit.payload_kind != "table":
        # 无注册标题时不发明合成锚——否则全平文档会因此失去
        # 「fully flat 不造结构」的守卫。
        return unit
    rows = unit.payload.get("rows") or []
    firsts = [str(row[0]).strip() for row in rows if row and str(row[0]).strip()]
    # 空首格行是跨页溢出残行(1217576500 表#18), 不参与判定; 判定本身
    # 要求每个非空首格整格命中官方模板字段。
    if not firsts or not all(
        rules.QA_FORM_FOOTER_FIELD_RE.match(first) for first in firsts
    ):
        return unit
    headers = [str(cell).strip() for cell in unit.payload.get("headers") or []]
    header_first = headers[0] if headers else ""
    carries_narrative_overflow = bool(
        any(headers)
        and not (
            header_first
            and rules.QA_FORM_FOOTER_FIELD_RE.match(header_first)
        )
    )
    return UnitDraft(
        **{
            **unit.__dict__,
            "heading_path": [document_title],
            "title": document_title,
            # Without a native-text recovery channel, a blank first header can
            # hide the tail of the final answer (1217576500 page 7).  Keep the
            # content but fail closed instead of calling the footer clean.
            "quality_status": (
                "needs_review" if carries_narrative_overflow else unit.quality_status
            ),
        }
    )


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
    document_title: str | None = None,
    stats: BuildStats,
) -> list[UnitDraft]:
    # Document-level event keys (round12): "what happened" is its own facet;
    # derived from the announcement title, unioned into every unit's keys.
    event_keys = rules.event_keys_for_document_title(document_title)
    finalized: list[UnitDraft] = []
    for unit in units:
        semantic_key = unit.semantic_key or semantic_key_for_unit(unit, filing_type=filing_type)
        keys = set(unit.semantic_keys or ())
        keys.update(event_keys)
        if semantic_key:
            keys.add(semantic_key)
        note_keys = _note_keys_for_unit(unit, filing_type=filing_type)
        keys.update(note_keys)
        if semantic_key is None and note_keys:
            semantic_key = note_keys[0]
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


def _native_qa_form_sections(native_text: Any) -> list[_NativeSection]:
    """Recover consecutive top-level form sections from the PDF text layer.

    The native channel stays parser-neutral; this is the business-aware gate.
    We require a consecutive run beginning at 一、 and containing a Q&A section,
    then stop before the official form footer/attachment.  Anything less
    certain falls back to the MinerU elements unchanged.
    """

    if not isinstance(native_text, dict) or native_text.get("status") != "ok":
        return []
    pages = native_text.get("pages")
    if not isinstance(pages, list):
        return []

    lines: list[tuple[int, str]] = []
    for page in pages:
        if not isinstance(page, dict):
            return []
        page_no = _int_or_none(page.get("page_no"))
        if page_no is None:
            return []
        for raw_line in str(page.get("text") or "").splitlines():
            line = raw_line.strip()
            if line:
                lines.append((page_no, line))
    if not lines:
        return []

    end_index = next(
        (
            index
            for index, (_, line) in enumerate(lines)
            if rules.QA_FORM_NARRATIVE_END_RE.match(line)
        ),
        len(lines),
    )
    candidates: list[tuple[int, str, int]] = []
    for index, (_, line) in enumerate(lines[:end_index]):
        match = rules.QA_FORM_MAIN_SECTION_RE.match(line)
        if match is None:
            continue
        title = match.group(1).strip()
        ordinal = _heading_ordinal(title)
        if ordinal is not None:
            candidates.append((index, title, ordinal))

    runs: list[list[tuple[int, str, int]]] = []
    for start, candidate in enumerate(candidates):
        if candidate[2] != 1:
            continue
        run = [candidate]
        expected = 2
        for following in candidates[start + 1 :]:
            if following[2] != expected:
                break
            run.append(following)
            expected += 1
        if len(run) >= 2 and any(
            rules.QA_FORM_QA_SECTION_RE.search(title) for _, title, _ in run
        ):
            runs.append(run)
    if not runs:
        return []
    run = max(runs, key=len)

    sections: list[_NativeSection] = []
    for offset, (line_index, title, ordinal) in enumerate(run):
        next_index = run[offset + 1][0] if offset + 1 < len(run) else end_index
        body_lines = [
            line
            for _, line in lines[line_index + 1 : next_index]
            if not rules.QA_FORM_NARRATIVE_LABEL_RE.match(line)
        ]
        if not body_lines:
            return []
        page_numbers = [page_no for page_no, _ in lines[line_index:next_index]]
        sections.append(
            _NativeSection(
                title=title,
                body="\n".join(body_lines),
                ordinal=ordinal,
                start_page_no=min(page_numbers),
                end_page_no=max(page_numbers),
            )
        )
    for section in sections:
        if not rules.QA_FORM_QA_SECTION_RE.search(section.title):
            continue
        qa_check = s4_build_qa_units(
            section.body,
            source=UnitDraft(
                payload_kind="text",
                payload={"text": section.body},
                source_order=0,
                heading_path=[section.title],
                artifact_locator={"source": "native_text"},
            ),
        )
        if qa_check.unstable or not qa_check.units:
            return []
    return sections


def _raw_table_text(element: dict[str, Any]) -> str:
    table = element.get("table") or {}
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    return " ".join(
        [str(item) for item in element.get("table_caption") or []]
        + [str(cell) for cell in headers]
        + [str(cell) for row in rows for cell in row]
    )


def _raw_element_text(element: dict[str, Any]) -> str:
    if str(element.get("kind")) == "table":
        return _raw_table_text(element)
    return str(element.get("text") or "")


def _marker_start(text: str, marker: str) -> int | None:
    compact = re.sub(r"\s+", "", marker)
    if not compact:
        return None
    match = re.search(r"\s*".join(re.escape(char) for char in compact), text)
    return match.start() if match is not None else None


def _element_contains_marker(element: dict[str, Any], marker: str) -> bool:
    return _marker_start(_raw_element_text(element), marker) is not None


def _raw_start_table_is_safe_narrative(
    element: dict[str, Any], marker: str
) -> bool:
    if str(element.get("kind")) != "table":
        return True
    table = element.get("table") or {}
    marker_seen = False
    for row in [table.get("headers") or [], *(table.get("rows") or [])]:
        narrative_cells: list[str] = []
        marker_in_row = False
        for cell in row:
            text = str(cell)
            marker_start = _marker_start(text, marker)
            if marker_start is not None:
                marker_seen = True
                marker_in_row = True
                narrative_cells.append(_comparison_text(text[marker_start:]))
            elif marker_seen:
                narrative_cells.append(_comparison_text(text))
        if marker_seen and not marker_in_row and any(narrative_cells):
            # Even a sparse one-nonempty-cell row can be one column of a real
            # table.  The only proven first-carrier shape has the narrative in
            # the final non-empty row, so any later content makes recovery
            # structurally ambiguous and must fall back.
            return False
        distinct = {cell for cell in narrative_cells if cell}
        if len(distinct) > 1:
            # The first form carrier contains a real multi-column table after
            # the narrative marker.  Text coverage cannot authorize dropping
            # its row/column structure.
            return False
    return marker_seen


def _trim_raw_table_before_marker(
    element: dict[str, Any], marker: str
) -> dict[str, Any] | None:
    if str(element.get("kind")) != "table" or element.get("table_parse_failed"):
        return None
    table = dict(element.get("table") or {})
    seen = False

    def trim(cell: Any) -> str:
        nonlocal seen
        text = str(cell)
        start = _marker_start(text, marker)
        if start is not None:
            seen = True
            return text[:start].rstrip()
        return "" if seen else text

    table["headers"] = [trim(cell) for cell in table.get("headers") or []]
    table["rows"] = [
        [trim(cell) for cell in row] for row in table.get("rows") or []
    ]
    if not seen:
        return None
    return {**element, "table": table}


def _raw_table_is_empty(element: dict[str, Any]) -> bool:
    table = element.get("table") or {}
    return not any(str(cell).strip() for cell in table.get("headers") or []) and not any(
        str(cell).strip() for row in table.get("rows") or [] for cell in row
    )


def _raw_table_is_qa_prose_carrier(element: dict[str, Any]) -> bool:
    if str(element.get("kind")) != "table":
        return False
    table = element.get("table") or {}
    rows = table.get("rows") or []
    grids = [table.get("headers") or [], *rows]
    nonempty_columns = max(
        (sum(bool(str(cell).strip()) for cell in row) for row in grids),
        default=0,
    )
    occupied_columns = {
        column
        for row in grids
        for column, cell in enumerate(row)
        if str(cell).strip()
    }
    text = _raw_table_text(element)
    return (
        not element.get("table_caption")
        and nonempty_columns <= 1
        and len(occupied_columns) <= 1
        and len(text) >= rules.QA_TABLE_CONTENT_MIN_CHARS
        and bool(rules.QA_TABLE_MARKER_RE.search(text))
    )


def _raw_attachment_table(element: dict[str, Any]) -> bool:
    if str(element.get("kind")) != "table":
        return False
    captions = [str(item).strip() for item in element.get("table_caption") or []]
    return bool(captions and rules.ATTACHMENT_CAPTION_RE.match(captions[0]))


def _raw_attachment_boundary(element: dict[str, Any]) -> bool:
    if _raw_attachment_table(element):
        return True
    if str(element.get("kind")) not in {"text", "heading"}:
        return False
    first_line = next(
        (line.strip() for line in str(element.get("text") or "").splitlines() if line.strip()),
        "",
    )
    return bool(rules.ATTACHMENT_CAPTION_RE.match(first_line))


def _normalized_attachment_boundary(
    element: dict[str, Any],
) -> dict[str, Any] | None:
    if _raw_attachment_table(element):
        return element
    lines = [
        line.strip()
        for line in str(element.get("text") or "").splitlines()
        if line.strip()
    ]
    if len(lines) != 1 or not rules.ATTACHMENT_CAPTION_RE.match(lines[0]):
        return None
    return {
        **element,
        "kind": "heading",
        "heading_level": 1,
        "text": lines[0],
    }


def _raw_footer_only(element: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    if str(element.get("kind")) != "table" or element.get("table_parse_failed"):
        return None
    table = dict(element.get("table") or {})
    rows = [[str(cell) for cell in row] for row in table.get("rows") or []]
    first_footer = next(
        (
            index
            for index, row in enumerate(rows)
            if row and rules.QA_FORM_FOOTER_FIELD_RE.match(row[0].strip())
        ),
        None,
    )
    if first_footer is None:
        return None
    pre_footer_grids = [table.get("headers") or [], *rows[:first_footer]]
    if any(
        sum(bool(str(cell).strip()) for cell in row) > 1
        for row in pre_footer_grids
    ):
        # A structured multi-column table immediately before the template
        # footer is business content, not narrative overflow.  Recovery must
        # keep the original MinerU structure instead of flattening it.
        return None
    occupied_columns = {
        column
        for row in pre_footer_grids
        for column, cell in enumerate(row)
        if str(cell).strip()
    }
    if len(occupied_columns) > 1:
        # A sparse table may have only one non-empty cell per row while values
        # alternate across real columns.  Narrative overflow stays in one
        # fixed outer-form cell; cross-column content is structural.
        return None
    footer_rows = rows[first_footer:]
    first_cells = [row[0].strip() for row in footer_rows if row and row[0].strip()]
    if not first_cells or not all(
        rules.QA_FORM_FOOTER_FIELD_RE.match(cell) for cell in first_cells
    ):
        return None
    merged_cells = []
    for cell in table.get("merged_cells") or []:
        row = int(cell["row"])
        if row < first_footer:
            continue
        merged_cells.append({**cell, "row": row - first_footer})
    table["headers"] = []
    table["rows"] = footer_rows
    if merged_cells:
        table["merged_cells"] = merged_cells
    else:
        table.pop("merged_cells", None)
    return {**element, "table": table}, first_footer


def _comparison_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    # MinerU's Markdown text escapes a literal tilde as ``\~`` while the
    # native PDF text layer contains ``~``.  The backslash is serialization
    # syntax, not source punctuation; keep the tilde itself so ranges such as
    # ``1~2 年`` still participate in strict fact-preserving comparison.
    normalized = normalized.replace(r"\~", "~")
    # Only PDF hard-wrap whitespace is layout noise.  Preserve punctuation,
    # decimals, percent signs and +/- because they carry financial meaning.
    return re.sub(r"\s+", "", normalized)


def _native_covers_replaced_narrative(
    *,
    raw_elements: list[dict[str, Any]],
    start_index: int,
    attachment_index: int,
    sections: list[_NativeSection],
) -> bool:
    first_title = sections[0].title
    fragments: list[str] = []

    def add_fragment(value: Any) -> None:
        compact = _comparison_text(str(value))
        if compact and (not fragments or compact != fragments[-1]):
            fragments.append(compact)

    first_element = raw_elements[start_index]
    if str(first_element.get("kind")) == "table":
        table = first_element.get("table") or {}
        marker_seen = False
        for row in [table.get("headers") or [], *(table.get("rows") or [])]:
            for cell in row:
                text = str(cell)
                marker = _marker_start(text, first_title)
                if marker is not None:
                    marker_seen = True
                    add_fragment(text[marker:])
                elif marker_seen:
                    add_fragment(text)
        if not marker_seen:
            return False
    else:
        first_text = _raw_element_text(first_element)
        first_marker = _marker_start(first_text, first_title)
        if first_marker is None:
            return False
        add_fragment(first_text[first_marker:])

    for element in raw_elements[start_index + 1 : attachment_index]:
        kind = str(element.get("kind"))
        if kind in {"text", "heading"}:
            add_fragment(_raw_element_text(element))
            continue
        if kind != "table":
            continue
        table = element.get("table") or {}
        footer_result = _raw_footer_only(element)
        if footer_result is not None:
            headers = table.get("headers") or []
            rows = table.get("rows") or []
            first_footer = footer_result[1]
            for cell in headers:
                add_fragment(cell)
            for row in rows[:first_footer]:
                for cell in row:
                    add_fragment(cell)
        elif _raw_table_is_qa_prose_carrier(element):
            for cell in table.get("headers") or []:
                add_fragment(cell)
            for row in table.get("rows") or []:
                for cell in row:
                    add_fragment(cell)

    native_text = _comparison_text(
        "\n".join(f"{section.title}\n{section.body}" for section in sections)
    )
    if not fragments or not native_text:
        return False
    cursor = 0
    for fragment in fragments:
        position = native_text.find(fragment, cursor)
        if position < 0:
            return False
        cursor = position + len(fragment)
    return True


def _replace_qa_form_narrative(
    normalized_ir: dict[str, Any], *, filing_type: str | None
) -> _QaFormRecovery:
    raw_elements = [dict(item) for item in normalized_ir.get("elements", [])]
    if filing_type not in {"investor_relations", "performance_briefing"}:
        return _QaFormRecovery(elements=raw_elements)
    sections = _native_qa_form_sections(normalized_ir.get("native_text"))
    if not sections:
        return _QaFormRecovery(elements=raw_elements)

    first_title = sections[0].title
    start_index = next(
        (
            index
            for index, element in enumerate(raw_elements)
            if _element_contains_marker(element, first_title)
        ),
        None,
    )
    if start_index is None:
        return _QaFormRecovery(elements=raw_elements)
    if not _raw_start_table_is_safe_narrative(
        raw_elements[start_index], first_title
    ):
        return _QaFormRecovery(elements=raw_elements)
    attachment_index = next(
        (
            index
            for index, element in enumerate(raw_elements[start_index:], start_index)
            if _raw_attachment_boundary(element)
        ),
        len(raw_elements),
    )
    attachment_suffix = list(raw_elements[attachment_index:])
    if attachment_suffix:
        normalized_boundary = _normalized_attachment_boundary(attachment_suffix[0])
        if normalized_boundary is None:
            return _QaFormRecovery(elements=raw_elements)
        attachment_suffix[0] = normalized_boundary
    if not _native_covers_replaced_narrative(
        raw_elements=raw_elements,
        start_index=start_index,
        attachment_index=attachment_index,
        sections=sections,
    ):
        return _QaFormRecovery(elements=raw_elements)

    prefix = list(raw_elements[:start_index])
    trimmed = _trim_raw_table_before_marker(raw_elements[start_index], first_title)
    if trimmed is not None and not _raw_table_is_empty(trimmed):
        prefix.append(trimmed)
    elif str(raw_elements[start_index].get("kind")) != "table":
        # A plain text/heading carrier is fully replaced by native text.
        trimmed = {}
    else:
        return _QaFormRecovery(elements=raw_elements)

    footer: list[dict[str, Any]] = []
    replaced_carriers = 1
    for element in raw_elements[start_index + 1 : attachment_index]:
        kind = str(element.get("kind"))
        if kind in {"text", "heading", "page_furniture"}:
            continue
        if kind == "unknown" and not _clean_text(_element_text(element)):
            # Mirror S1 for legacy persisted IR whose mapper emitted an empty
            # unknown.  New MinerU string-list items are preserved as text by
            # mapper_to_ir and therefore pass through the strict coverage
            # comparison instead of relying on this compatibility branch.
            continue
        if kind == "table":
            footer_result = _raw_footer_only(element)
            if footer_result is not None:
                footer_element, overflow_rows = footer_result
                footer.append(footer_element)
                replaced_carriers += int(overflow_rows > 0)
                continue
            if _raw_table_is_empty(element):
                continue
            if _raw_table_is_qa_prose_carrier(element):
                replaced_carriers += 1
                continue
        # Do not silently flatten a real business table/image/equation.  This
        # form is outside the high-confidence recovery family; keep MinerU and
        # its needs_review behavior unchanged.
        return _QaFormRecovery(elements=raw_elements)

    native_hash = str((normalized_ir.get("native_text") or {}).get("content_hash") or "")
    recovered: list[dict[str, Any]] = []
    for section in sections:
        locator = {
            "source": "native_text",
            "page_span": [section.start_page_no, section.end_page_no],
        }
        if native_hash:
            locator["native_text_hash"] = native_hash
        recovered.extend(
            [
                {
                    "kind": "heading",
                    "raw_kind": "native_text",
                    "heading_level": 2,
                    "text": section.title,
                    "page_no": section.start_page_no,
                    **locator,
                },
                {
                    "kind": "text",
                    "raw_kind": "native_text",
                    "text": (
                        section.body
                        if rules.QA_FORM_QA_SECTION_RE.search(section.title)
                        else _join_native_prose(section.body)
                    ),
                    "page_no": section.start_page_no,
                    **locator,
                },
            ]
        )

    combined = [*prefix, *recovered, *footer, *attachment_suffix]
    reindexed: list[dict[str, Any]] = []
    for order_index, element in enumerate(combined):
        values = dict(element)
        if "order_index" in values:
            values["source_order_index"] = values["order_index"]
        values["order_index"] = order_index
        reindexed.append(values)
    return _QaFormRecovery(
        elements=reindexed,
        section_count=len(sections),
        replaced_carriers=replaced_carriers,
    )


def build_unit_drafts_s1_s7(
    normalized_ir: dict[str, Any],
    *,
    filing_type: str | None,
    document_title: str | None = None,
    image_bytes_resolver: ImageBytesResolver | None = None,
) -> tuple[list[UnitDraft], BuildStats]:
    recovery = _replace_qa_form_narrative(
        normalized_ir, filing_type=filing_type
    )
    s1 = s1_preprocess_elements(
        recovery.elements,
        image_bytes_resolver=image_bytes_resolver,
    )
    s1.stats.native_text_sections_recovered = recovery.section_count
    s1.stats.qa_form_carriers_replaced = recovery.replaced_carriers
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
        table_units = [
            _reanchor_qa_form_footer(unit, document_title=document_title)
            for unit in table_units
        ]
    table_qa_units = _qa_units_from_tables(table_units)
    units = sorted([*text_units, *table_units, *table_qa_units], key=_unit_sort_key)
    if filing_type in {"investor_relations", "performance_briefing"}:
        units = _downgrade_qa_before_shredded_table(units)
    units = _sink_leading_applicable(units)
    kept = s6_filter_units(units, s1.stats)
    kept = _anchor_headerless_units(kept, document_title=document_title, stats=s1.stats)
    kept = s8_group_semantic_units(
        kept, filing_type=filing_type, document_title=document_title, stats=s1.stats
    )
    kept = _drop_standalone_noise_units(kept, stats=s1.stats)
    return (
        s7_finalize_units(
            kept,
            filing_type=filing_type,
            document_title=document_title,
            stats=s1.stats,
        ),
        s1.stats,
    )


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
    # 首标题前的内容属于文档本身(round17, 取代 round3 的合成锚常态):
    # 自带 caption 的单元锚到自身标题, 其余锚到文档注册标题——
    # 「公告头信息」只作 document_title 缺失时的最后兜底。被困在表单
    # 单元格里的小节标题(一、…)与正文粘连无分隔, 任何切分都会产出脏
    # 锚点, 按宁漏勿脏不做抽取。
    out: list[UnitDraft] = []
    for unit in units:
        if unit.heading_path:
            out.append(unit)
            continue
        # Pre-heading letterhead remnants (whole-unit 公告编号 line etc.,
        # round16 corpus: 68 live 公告头信息 units) drop HERE, before the
        # short-document collapse can absorb them as mixed-unit parts where
        # the late standalone-noise stage cannot see them. Long real content
        # that merely sits before the first heading never matches the closed
        # family and keeps anchoring.
        if (
            unit.payload_kind == "text"
            and "image_ref" not in unit.payload
            and rules.is_standalone_noise(str(unit.payload.get("text", "")))
        ):
            stats.dropped_by_kind["standalone_noise"] += 1
            continue
        stats.anchored_header_units += 1
        anchor = (
            unit.title
            or document_title
            or rules.DOCUMENT_HEADER_ANCHOR
        )
        out.append(
            UnitDraft(
                **{
                    **unit.__dict__,
                    "heading_path": [anchor],
                    "title": unit.title or anchor,
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
    mixed unit. QA leaves are already semantic and are never regrouped; the
    non-QA prelude/sections inside an IR filing still use normal section
    grouping.
    """

    if filing_type in {"investor_relations", "performance_briefing"}:
        return _group_section_units(units, filing_type=filing_type, stats=stats)
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
            title, same_line_remainder = _split_proposal_anchor_title(lines[0])
            remainder_lines = lines[1:]
            if same_line_remainder:
                remainder_lines = [same_line_remainder, *remainder_lines]
            remainder = "\n".join(remainder_lines).strip()
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
            title, _ = _split_proposal_anchor_title(first)
            return (
                "table_caption",
                title,
                _strip_anchor_suffix(unit.heading_path),
                [unit],
            )
    path = unit.heading_path
    if path and rules.match_proposal_anchor(path[-1].strip()):
        title, _ = _split_proposal_anchor_title(path[-1])
        return ("heading", title, _strip_anchor_suffix(path), [unit])
    return None


_PROPOSAL_TITLE_TRAILING_RE = re.compile(
    r"\s*(?P<tail>(?:审议结果|表决情况)\s*[：:].*)$"
)


def _split_proposal_anchor_title(line: str) -> tuple[str, str]:
    stripped = line.strip()
    match = _PROPOSAL_TITLE_TRAILING_RE.search(stripped)
    if match is None:
        return stripped, ""
    return stripped[: match.start()].rstrip(), match.group("tail").strip()


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
    for member in members:
        keys.update(_note_keys_for_unit(member, filing_type=filing_type))
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


def _note_keys_for_unit(
    unit: UnitDraft, *, filing_type: str | None
) -> list[str]:
    """章节键（含祖先继承，round13 用户裁决）：标题优先，然后沿 heading_path
    自深向浅逐级取键——"(1) 明细情况" 这类无科目语义的叶子从最近的科目祖先
    继承（19、其他非流动金融资产 → other_noncurrent_financial_assets），
    章级键（第八节 财务报告 → financial_report_chapter）一并入组。返回按
    深度有序去重的列表，首个即最具体键。"""

    # 词表键对全部文档类型开放（round13：审计报告等 'other' 文档同样承载
    # 报表与附注结构；标题匹配有界，误配风险可控且被类扫描看护）。
    keys: list[str] = []
    candidates = [unit.title, *reversed(unit.heading_path)]
    for candidate in candidates:
        key = rules.note_key_for_title(candidate)
        if key and key not in keys:
            keys.append(key)
    return keys


def _note_key_for_unit(unit: UnitDraft, *, filing_type: str | None) -> str | None:
    keys = _note_keys_for_unit(unit, filing_type=filing_type)
    return keys[0] if keys else None


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
    for key in ("source", "source_order_index", "page_span", "native_text_hash"):
        if element.get(key) is not None:
            locator[key] = element.get(key)
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
        if rules.is_unit_declaration_line(line):
            if stats is not None:
                stats.dropped_unit_declarations += 1
            continue
        if rules.BOILERPLATE_GUARANTEE_RE.match(line) or rules.is_closing_formula_line(
            line
        ):
            # Fixed legalese/closing formulas (§3.5 稳定噪声, user-authorized).
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
        r"(?<!^)(?<!\d)(?=\d+(?:[、．]|\.(?!\d))\s*)",
        "\n",
        text,
    )
    prepared = re.sub(
        r"([？?])(?=\s*(答|回复|公司回复|A\d*)\s*[：:])",
        "\\1\n",
        prepared,
    )
    return prepared.splitlines()


def _join_wrapped_lines(lines: list[str]) -> str:
    """Join PDF hard wraps without inventing spaces inside Chinese words."""

    joined = ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if joined and re.search(r"[A-Za-z0-9]$", joined) and re.match(
            r"^[A-Za-z0-9]", line
        ):
            joined += " "
        joined += line
    return joined.strip()


def _join_native_prose(text: str) -> str:
    """Remove PDF hard wraps while preserving numbered-list boundaries."""

    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _numbered_line(stripped) and current:
            paragraphs.append(_join_wrapped_lines(current))
            current = []
        current.append(stripped)
    if current:
        paragraphs.append(_join_wrapped_lines(current))
    return "\n".join(paragraph for paragraph in paragraphs if paragraph)


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
    # A page-spillover continuation never crosses a section boundary. Note
    # tables share one shape (项目/本期/上期), so column count alone merged
    # adjacent DIFFERENT notes once cn_a_v6 let their headings enter the
    # stack (no text element left between the tables) — 3. 销售费用's table
    # was absorbed into 1. 营业收入 and the heading vanished from every path
    # (ub-2026.07-18, swallowed-heading audit).
    if list(previous.heading_path) != list(current.heading_path):
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
    # A header-skeleton table (headers, zero data rows) is original content:
    # 分部信息-class sections disclose an empty template, and dropping the
    # unit swallowed the whole heading branch from every path
    # (ub-2026.07-18 swallowed-heading audit).
    if any(str(cell).strip() for cell in payload.get("headers") or []):
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
        if table.payload_kind != "table" or table.quality_status != "ok":
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
