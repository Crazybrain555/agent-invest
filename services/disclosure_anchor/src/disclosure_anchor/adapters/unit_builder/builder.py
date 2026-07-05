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
    quality_status: str = "ok"
    artifact_locator: dict[str, Any] | None = None


@dataclass
class BuildStats:
    generated_by_kind: Counter[str] = field(default_factory=Counter)
    dropped_by_kind: Counter[str] = field(default_factory=Counter)
    dropped_unknown_by_raw_kind: Counter[str] = field(default_factory=Counter)
    skipped_sections: list[str] = field(default_factory=list)
    merged_tables: int = 0
    needs_review_count: int = 0
    unusable_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_by_kind": dict(self.generated_by_kind),
            "dropped_by_kind": dict(self.dropped_by_kind),
            "dropped_unknown_by_raw_kind": dict(self.dropped_unknown_by_raw_kind),
            "skipped_sections": list(self.skipped_sections),
            "merged_tables": self.merged_tables,
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
            item = PreparedElement(
                kind="table",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                heading_level=_int_or_none(element.get("heading_level")),
                table=dict(element.get("table") or {"headers": [], "rows": []}),
                table_caption=[str(item) for item in element.get("table_caption") or []],
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
    stack: list[tuple[int, str]] = []
    placed: list[PreparedElement] = []
    for element in elements:
        if qa_heading_mode and element.kind == "heading" and _numbered_line(element.text or ""):
            heading_path = [title for _, title in stack]
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
            candidate_path = [title for _, title in stack if _ < level] + [element.text or ""]
            if len(candidate_path) <= 4:
                stack = [(existing_level, title) for existing_level, title in stack if existing_level < level]
                stack.append((level, element.text or ""))
                continue
        heading_path = [title for _, title in stack]
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


def s3_build_text_units(elements: Iterable[PreparedElement]) -> list[UnitDraft]:
    units: list[UnitDraft] = []
    buffer: list[PreparedElement] = []

    def flush() -> None:
        if not buffer:
            return
        text = "\n".join(item.text or "" for item in buffer if item.text).strip()
        if text:
            quality = "needs_review" if any(item.quality_status == "needs_review" for item in buffer) else "ok"
            units.extend(_split_numbered_text_block(buffer[0], text, quality_status=quality))
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
        units.append(_table_group_to_unit(group, previous_text=previous_text))
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
                    "quality_status": quality_status,
                }
            )
        )
    return finalized


def build_unit_drafts_s1_s7(
    normalized_ir: dict[str, Any],
    *,
    filing_type: str | None,
    image_bytes_resolver: ImageBytesResolver | None = None,
) -> tuple[list[UnitDraft], BuildStats]:
    s1 = s1_preprocess_elements(
        normalized_ir.get("elements", []),
        image_bytes_resolver=image_bytes_resolver,
    )
    placed = s2_apply_heading_tree(
        s1.elements,
        qa_heading_mode=filing_type in {"investor_relations", "performance_briefing"},
    )
    text_units = replace_text_units_with_qa_where_stable(s3_build_text_units(placed))
    table_units = s5_build_table_units(placed, s1.stats)
    table_qa_units = _qa_units_from_tables(table_units)
    units = sorted([*text_units, *table_units, *table_qa_units], key=_unit_sort_key)
    kept = s6_filter_units(units, s1.stats)
    return s7_finalize_units(kept, filing_type=filing_type, stats=s1.stats), s1.stats


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


def _split_numbered_text_block(
    source: PreparedElement,
    text: str,
    *,
    quality_status: str,
) -> list[UnitDraft]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    numbered = [line for line in lines if re.match(r"^\d+[、.．]\s*", line)]
    if len(numbered) >= 3 and len(text) >= 200 and len(numbered) == len(lines):
        return [
            UnitDraft(
                payload_kind="text",
                payload={"text": line},
                source_order=source.order_index,
                intra_order=offset,
                heading_path=list(source.heading_path),
                title=source.title,
                quality_status=quality_status,
            )
            for offset, line in enumerate(lines)
        ]
    return [
        UnitDraft(
            payload_kind="text",
            payload={"text": text},
            source_order=source.order_index,
            heading_path=list(source.heading_path),
            title=source.title,
            quality_status=quality_status,
        )
    ]


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


def _table_group_to_unit(group: list[PreparedElement], *, previous_text: str) -> UnitDraft:
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
