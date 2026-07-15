"""Pure S1-S7 document_unit builder stages."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import hashlib
import re
import unicodedata
from typing import Any, Callable, Iterable

from disclosure_anchor.adapters.unit_builder import rules
from disclosure_anchor.adapters.unit_builder.table_grid import (
    drop_blank_table_rows,
    merge_table_grids,
)


ImageBytesResolver = Callable[[str], bytes]
HeadingStackEntry = tuple[int, str, int | None, float | None]


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
    # Complete S2 stack used only inside the builder. ``heading_path`` remains
    # the capped public breadcrumb; merge/group stages must not use that lossy
    # projection as a section identity.
    structural_path: list[str] = field(default_factory=list)
    title: str | None = None
    qa_question_boundary: bool = False


@dataclass(frozen=True)
class UnitDraft:
    payload_kind: str
    payload: dict[str, Any]
    source_order: int
    intra_order: int = 0
    heading_path: list[str] = field(default_factory=list)
    structural_path: list[str] = field(default_factory=list)
    title: str | None = None
    semantic_key: str | None = None
    semantic_keys: list[str] | None = None
    quality_status: str = "ok"
    applicability: str | None = None
    artifact_locator: dict[str, Any] | None = None
    qa_question_boundaries: list[str] = field(default_factory=list)


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
    native_text_qa_pairs_recovered: int = 0
    native_text_carriers_suppressed: int = 0
    native_text_table_rows_suppressed: int = 0
    qa_form_carriers_replaced: int = 0
    inferred_page_furniture: int = 0
    recovered_statement_captions: int = 0
    recovered_section_furniture_headings: int = 0
    recovered_split_note_headings: int = 0
    recovered_sandwiched_note_ordinals: int = 0
    recovered_statement_orphan_rows: int = 0
    recovered_table_group_rows: int = 0
    recovered_parameter_list_items: int = 0
    merged_cover_title_fragments: int = 0
    merged_announcement_header_units: int = 0
    deduplicated_announcement_header_units: int = 0
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
            "native_text_qa_pairs_recovered": self.native_text_qa_pairs_recovered,
            "native_text_carriers_suppressed": (
                self.native_text_carriers_suppressed
            ),
            "native_text_table_rows_suppressed": (
                self.native_text_table_rows_suppressed
            ),
            "qa_form_carriers_replaced": self.qa_form_carriers_replaced,
            "inferred_page_furniture": self.inferred_page_furniture,
            "recovered_statement_captions": self.recovered_statement_captions,
            "recovered_section_furniture_headings": (
                self.recovered_section_furniture_headings
            ),
            "recovered_split_note_headings": self.recovered_split_note_headings,
            "recovered_sandwiched_note_ordinals": (
                self.recovered_sandwiched_note_ordinals
            ),
            "recovered_statement_orphan_rows": (self.recovered_statement_orphan_rows),
            "recovered_table_group_rows": self.recovered_table_group_rows,
            "recovered_parameter_list_items": self.recovered_parameter_list_items,
            "merged_cover_title_fragments": self.merged_cover_title_fragments,
            "merged_announcement_header_units": (
                self.merged_announcement_header_units
            ),
            "deduplicated_announcement_header_units": (
                self.deduplicated_announcement_header_units
            ),
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
    leading_text: str | None = None
    leading_needs_review: bool = False
    # Corrupt source spans quarantined before the QA at the given zero-based
    # index.  This keeps a bad middle pair in evidence order without either
    # fabricating QA from it or hiding independently proven later pairs.
    review_spans: list[tuple[int, str]] = field(default_factory=list)
    trailing_text: str | None = None


@dataclass(frozen=True)
class _NativeSection:
    title: str
    body: str
    ordinal: int
    start_page_no: int
    end_page_no: int


@dataclass(frozen=True)
class _NativeQaPair:
    ordinal: int
    question: str
    answer: str
    raw_text: str
    start_page_no: int
    end_page_no: int


@dataclass(frozen=True)
class _NativeDirectFragmentMatch:
    positions: tuple[int, ...]
    final_remainder: str = ""


@dataclass(frozen=True)
class _QaFormRecovery:
    elements: list[dict[str, Any]]
    section_count: int = 0
    replaced_carriers: int = 0


@dataclass(frozen=True)
class _SplitNoteHeadingRecovery:
    prefix_index: int
    title_index: int
    insertion_index: int
    text: str
    visual_top: float


@dataclass(frozen=True)
class _SectionBoundary:
    """Internal grouping identity separated from the public breadcrumb."""

    identity: tuple[str, ...]
    heading_path: tuple[str, ...]
    title: str | None
    reanchor: bool = False
    local_heading_root: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _QaLogicalCarrier:
    """One physical source order reconstructed for transcript-only parsing."""

    source_order: int
    text: str
    context: UnitDraft
    question_boundaries: tuple[str, ...] = ()
    has_existing_qa: bool = False
    ends_run: bool = False


@dataclass(frozen=True)
class _SourceHeadingAnchor:
    """Last real source heading, for bounded same-style sibling recovery."""

    effective_level: int
    pattern_level: int | None
    source_level: int | None
    left: float | None


@dataclass
class _SingleDotSequence:
    """Document-local ``1.``/``2.`` outline run that may be temporarily closed.

    Some bank reports insert a locally numbered hotspot below chapter ``8.``;
    those intervening headings evict the open chapter stack before chapter
    ``9.`` resumes.  A proven run of at least three same-left siblings keeps
    its parent snapshot so the next exact ordinal can safely reopen it.
    """

    ordinal: int
    effective_level: int
    parent_stack: tuple[HeadingStackEntry, ...]
    left: float
    length: int
    last_order: int


@dataclass
class _LocalRomanSequence:
    """One Roman sibling run proven below a document-local Latin parent.

    Page-top parent continuations such as ``(b) ... (续)`` legitimately close
    the active Roman child in the stack.  Keeping the exact Latin-parent
    snapshot lets the following ``(ii)``/``(iii)`` reopen that local run
    without assigning a global Latin > Roman hierarchy.
    """

    ordinal: int
    effective_level: int
    parent_stack: tuple[HeadingStackEntry, ...]
    title: str
    left: float | None
    last_order: int


@dataclass
class _LocalNumericSequence:
    """One parenthesized-numeric run below a proven local Roman node."""

    ordinal: int
    effective_level: int
    parent_stack: tuple[HeadingStackEntry, ...]
    title: str
    last_order: int


@dataclass
class _ParenthesizedHeadingSequence:
    """One locally proven ``(1)``/``(2)`` parser-heading sibling run."""

    ordinal: int
    effective_level: int
    parent_stack: tuple[HeadingStackEntry, ...]
    title: str
    left: float
    source_level: int
    length: int
    last_order: int


def s1_preprocess_elements(
    elements: Iterable[dict[str, Any]],
    *,
    image_bytes_resolver: ImageBytesResolver | None = None,
) -> Stage1Result:
    stats = BuildStats()
    prepared: list[PreparedElement] = []
    previous_non_furniture: PreparedElement | None = None
    raw_elements, recovered_orphan_rows = _recover_statement_orphan_rows(list(elements))
    stats.recovered_statement_orphan_rows = recovered_orphan_rows
    raw_elements, recovered_group_rows = _recover_long_term_investment_group_rows(
        raw_elements
    )
    stats.recovered_table_group_rows = recovered_group_rows
    parameter_list_evidence = _parameter_value_list_indices(raw_elements)
    stats.recovered_parameter_list_items = len(parameter_list_evidence)
    inferred_page_furniture = _inferred_repeated_page_furniture(raw_elements)
    structural_furniture_headings = _structural_furniture_heading_indices(raw_elements)
    recovered_statement_captions = _statement_furniture_captions(raw_elements)
    split_note_headings = _split_note_heading_fragments(raw_elements)
    split_headings_by_insertion: dict[int, list[_SplitNoteHeadingRecovery]] = {}
    for recovery in split_note_headings:
        split_headings_by_insertion.setdefault(recovery.insertion_index, []).append(
            recovery
        )
    for recoveries in split_headings_by_insertion.values():
        recoveries.sort(
            key=lambda recovery: (recovery.visual_top, recovery.title_index)
        )
    consumed_split_prefixes = {
        recovery.prefix_index for recovery in split_note_headings
    }
    consumed_split_titles = {recovery.title_index for recovery in split_note_headings}

    for element_index, element in enumerate(raw_elements):
        kind = str(element.get("kind", "unknown"))
        order_index = int(element.get("order_index", len(prepared)))
        raw_kind = str(element.get("raw_kind", kind))
        page_no = _int_or_none(element.get("page_no"))
        if element_index in structural_furniture_headings:
            text = _clean_text(_element_text(element))
            item = PreparedElement(
                kind="heading",
                order_index=order_index,
                raw_kind="recovered_section_page_furniture",
                page_no=page_no,
                heading_level=1,
                text=text,
                artifact_locator=_artifact_locator(element),
            )
            prepared.append(item)
            previous_non_furniture = item
            stats.recovered_section_furniture_headings += 1
            continue
        for split_recovery in split_headings_by_insertion.get(element_index, []):
            title_element = raw_elements[split_recovery.title_index]
            prefix_element = raw_elements[split_recovery.prefix_index]
            locator = _artifact_locator(title_element)
            locator["source_order_span"] = sorted(
                [
                    int(prefix_element.get("order_index", split_recovery.prefix_index)),
                    int(title_element.get("order_index", split_recovery.title_index)),
                ]
            )
            item = PreparedElement(
                kind="heading",
                order_index=order_index,
                raw_kind="recovered_split_note_heading",
                page_no=_int_or_none(title_element.get("page_no")),
                heading_level=_int_or_none(title_element.get("heading_level")) or 2,
                text=split_recovery.text,
                artifact_locator=locator,
            )
            prepared.append(item)
            previous_non_furniture = item
            stats.recovered_split_note_headings += 1
        if element_index in consumed_split_prefixes:
            stats.dropped_by_kind["split_note_heading_fragment"] += 1
            continue
        if element_index in consumed_split_titles:
            continue
        if element_index in inferred_page_furniture:
            stats.dropped_by_kind["page_furniture"] += 1
            stats.inferred_page_furniture += 1
            continue
        if kind == "page_furniture":
            stats.dropped_by_kind[kind] += 1
            continue
        if kind in {"text", "heading", "equation"}:
            text = _clean_text(_element_text(element))
            if not text:
                stats.dropped_by_kind[kind] += 1
                continue
            marker: str | None = None
            if kind != "equation":
                text, marker = rules.split_trailing_applicability_marker(text)
            recovered_parameter_item = element_index in parameter_list_evidence
            output_kind = (
                "text" if kind == "equation" or recovered_parameter_item else kind
            )
            item = PreparedElement(
                kind=output_kind,
                order_index=order_index,
                raw_kind=(
                    "recovered_parameter_list_item"
                    if recovered_parameter_item
                    else raw_kind
                ),
                page_no=page_no,
                heading_level=(
                    None
                    if recovered_parameter_item
                    else _int_or_none(element.get("heading_level"))
                ),
                text=text,
                artifact_locator=_artifact_locator(element),
            )
            prepared.append(item)
            if marker is not None:
                prepared.append(
                    PreparedElement(
                        kind="text",
                        order_index=order_index,
                        raw_kind="synthetic_applicability_marker",
                        page_no=page_no,
                        text=marker,
                        artifact_locator=_artifact_locator(element),
                    )
                )
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
            recovered_caption = recovered_statement_captions.get(element_index)
            if recovered_caption is not None:
                captions.append(recovered_caption)
                stats.recovered_statement_captions += 1
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
                table_footnote=[
                    str(item) for item in element.get("table_footnote") or []
                ],
                table_html=element.get("table_html"),
                table_parse_failed=bool(element.get("table_parse_failed")),
                artifact_locator=_artifact_locator(element),
            )
            prepared.append(item)
            previous_non_furniture = item
            continue
        stats.dropped_by_kind[kind] += 1

    prepared, recovered_ordinals = _recover_sandwiched_note_ordinals(prepared)
    stats.recovered_sandwiched_note_ordinals = recovered_ordinals
    return Stage1Result(elements=prepared, stats=stats)


def _recover_sandwiched_note_ordinals(
    elements: list[PreparedElement],
) -> tuple[list[PreparedElement], int]:
    """Restore one MinerU-dropped integer note prefix from local sequence proof.

    Bank statements sometimes preserve ``6. ...`` and ``8. ...`` but crop the
    left-margin ``7.`` from the intervening controlled title.  Recovery is
    deliberately closed: both numbered anchors must be exact controlled note
    labels, their ordinals must differ by two, and exactly one unnumbered exact
    controlled heading between them must share their source heading level and
    left edge.  The candidate must also sit no more than two pages before the
    right anchor.  Ambiguous intervals remain untouched.
    """

    anchors: list[tuple[int, int, PreparedElement]] = []
    for index, element in enumerate(elements):
        if element.kind != "heading":
            continue
        title = (element.text or "").strip()
        ordinal = _single_dot_ordinal(title)
        if ordinal is None or rules.exact_note_key_for_title(title) is None:
            continue
        anchors.append((index, ordinal, element))

    replacements: dict[int, PreparedElement] = {}
    for (left_index, left_ordinal, left), (
        right_index,
        right_ordinal,
        right,
    ) in zip(anchors, anchors[1:]):
        if right_ordinal != left_ordinal + 2:
            continue
        source_levels = {
            level
            for level in (left.heading_level, right.heading_level)
            if level is not None
        }
        if len(source_levels) > 1:
            continue
        anchor_lefts = [_heading_left(item) for item in (left, right)]
        if any(value is None for value in anchor_lefts):
            continue
        concrete_anchor_lefts = [value for value in anchor_lefts if value is not None]

        candidates: list[tuple[int, PreparedElement]] = []
        for candidate_index in range(left_index + 1, right_index):
            candidate = elements[candidate_index]
            title = (candidate.text or "").strip()
            if (
                candidate.kind != "heading"
                or not title
                or _pattern_heading_level(title) is not None
                or rules.exact_note_key_for_title(title) is None
            ):
                continue
            if source_levels and candidate.heading_level not in source_levels:
                continue
            candidate_left = _heading_left(candidate)
            if candidate_left is None or any(
                abs(candidate_left - anchor_left) > 8
                for anchor_left in concrete_anchor_lefts
            ):
                continue
            if (
                candidate.page_no is not None
                and right.page_no is not None
                and not 0 <= right.page_no - candidate.page_no <= 2
            ):
                continue
            candidates.append((candidate_index, candidate))
        if len(candidates) != 1:
            continue

        candidate_index, candidate = candidates[0]
        locator = dict(candidate.artifact_locator or {})
        locator["source_text"] = candidate.text
        replacements[candidate_index] = replace(
            candidate,
            text=f"{left_ordinal + 1}. {(candidate.text or '').strip()}",
            raw_kind="recovered_sandwiched_note_ordinal",
            artifact_locator=locator,
        )

    if not replacements:
        return elements, 0
    return (
        [replacements.get(index, element) for index, element in enumerate(elements)],
        len(replacements),
    )


_STATEMENT_ORPHAN_ROW_LABEL = "(二)稀释每股收益(元/股)"
_STATEMENT_ORPHAN_PREVIOUS_LABELS = (
    "七、每股收益:",
    "(一)基本每股收益(元/股)",
)
_STATEMENT_SIGNATORY_PREFIXES = (
    "公司负责人:",
    "主管会计工作负责人:",
    "会计机构负责人:",
)


def _recover_statement_orphan_rows(
    elements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Recover a proven cross-page statutory-statement row misread as title.

    MinerU can classify the final row of a continued income statement as a
    heading even though it is visibly inside the table border.  The repair is
    intentionally narrower than a generic EPS-title downgrade: the preceding
    page must end with the exact two-row earnings-per-share sequence, the
    orphan must be the four-column empty diluted-EPS row at the next page top,
    and the three statutory signatories plus the next cash-flow statement must
    immediately follow.  The orphan becomes a one-row table carrier so S5 can
    merge it with the preceding table while preserving its own page locator.
    """

    recovered = list(elements)
    recovered_count = 0
    for index, candidate in enumerate(elements):
        if not _is_statement_orphan_row_candidate(candidate):
            continue
        previous_index = _previous_business_element_index(elements, index)
        if previous_index is None:
            continue
        previous = elements[previous_index]
        if not _preceding_statement_table_proves_orphan(previous, candidate):
            continue
        following = _following_business_element_indices(elements, index, limit=4)
        if len(following) != 4 or not _following_statement_boundary_proves_orphan(
            elements, following, candidate
        ):
            continue

        converted = dict(candidate)
        converted.pop("text", None)
        converted["kind"] = "table"
        converted["raw_kind"] = "recovered_statement_orphan_row"
        converted["heading_level"] = None
        converted["table"] = {
            "headers": [],
            "rows": [[str(candidate.get("text") or ""), "", "", ""]],
        }
        converted["table_caption"] = []
        converted["table_footnote"] = []
        converted.pop("table_html", None)
        converted.pop("table_parse_failed", None)
        recovered[index] = converted
        recovered_count += 1
    return recovered, recovered_count


_LONG_TERM_INVESTMENT_GROUP_ROW_RE = re.compile(
    r"^(?P<ordinal>[一二三四五六七八九十]+)、"
    r"(?P<label>合营企业|联营企业)$"
)


def _recover_long_term_investment_group_rows(
    elements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Fold one page-bottom VLM title back into its continued note table.

    The repair is closed to the statutory long-term-investment table shape:
    a group row mislabelled as a heading must sit directly below a same-page
    table, while the next page starts with the consecutive group row as an
    explicit full-width merged table cell of the same column count.
    """

    recovered = list(elements)
    count = 0
    for index, candidate in enumerate(elements):
        candidate_text = _clean_text(_element_text(candidate))
        candidate_match = _LONG_TERM_INVESTMENT_GROUP_ROW_RE.fullmatch(
            candidate_text
        )
        if (
            candidate_match is None
            or str(candidate.get("kind") or "") != "heading"
        ):
            continue
        previous_index = _previous_business_element_index(elements, index)
        if previous_index is None:
            continue
        previous = elements[previous_index]
        owner_index = _previous_business_element_index(elements, previous_index)
        if owner_index is None:
            continue
        owner = elements[owner_index]
        if (
            str(previous.get("kind") or "") != "table"
            or str(owner.get("kind") or "") != "heading"
            or rules.exact_note_key_for_title(_element_text(owner))
            != "long_term_equity_investment"
        ):
            continue
        following = _following_business_element_indices(elements, index, limit=1)
        if not following:
            continue
        next_table = elements[following[0]]
        previous_columns = _raw_table_column_count(previous)
        next_columns = _raw_table_column_count(next_table)
        next_group = _raw_full_span_group_row(next_table)
        candidate_ordinal = _cn_ordinal(candidate_match.group("ordinal"))
        if (
            str(next_table.get("kind") or "") != "table"
            or previous_columns is None
            or previous_columns < 2
            or next_columns != previous_columns
            or next_group is None
            or candidate_ordinal is None
            or next_group[0]
            != candidate_ordinal + 1
        ):
            continue
        candidate_page = _int_or_none(candidate.get("page_no"))
        previous_page = _int_or_none(previous.get("page_no"))
        next_page = _int_or_none(next_table.get("page_no"))
        candidate_bbox = _element_bbox(candidate)
        previous_bbox = _element_bbox(previous)
        next_bbox = _element_bbox(next_table)
        if (
            candidate_page is None
            or previous_page != candidate_page
            or next_page != candidate_page + 1
            or candidate_bbox is None
            or previous_bbox is None
            or next_bbox is None
            or not -4 <= candidate_bbox[1] - previous_bbox[3] <= 20
            or candidate_bbox[3] < 850
            or next_bbox[1] > 160
            or not previous_bbox[0] <= candidate_bbox[0] <= previous_bbox[2]
        ):
            continue

        converted = dict(candidate)
        converted.pop("text", None)
        converted["kind"] = "table"
        converted["raw_kind"] = "recovered_table_group_row"
        converted["heading_level"] = None
        converted["table"] = {
            "headers": [],
            "rows": [[candidate_text] * previous_columns],
            "merged_cells": [
                {
                    "row": 0,
                    "col": 0,
                    "rowspan": 1,
                    "colspan": previous_columns,
                }
            ],
        }
        converted["table_caption"] = []
        converted["table_footnote"] = []
        converted.pop("table_html", None)
        converted.pop("table_parse_failed", None)
        recovered[index] = converted
        count += 1
    return recovered, count


def _raw_table_column_count(element: dict[str, Any]) -> int | None:
    if str(element.get("kind") or "") != "table":
        return None
    table = element.get("table")
    if not isinstance(table, dict):
        return None
    headers = table.get("headers")
    if isinstance(headers, list) and headers:
        return len(headers)
    rows = table.get("rows")
    if not isinstance(rows, list):
        return None
    first = next((row for row in rows if isinstance(row, list) and row), None)
    return len(first) if first is not None else None


def _raw_full_span_group_row(element: dict[str, Any]) -> tuple[int, str] | None:
    columns = _raw_table_column_count(element)
    table = element.get("table")
    if columns is None or not isinstance(table, dict):
        return None
    rows = table.get("rows")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        return None
    first = [str(value).strip() for value in rows[0]]
    if len(first) != columns or not first or len(set(first)) != 1:
        return None
    match = _LONG_TERM_INVESTMENT_GROUP_ROW_RE.fullmatch(first[0])
    if match is None:
        return None
    ordinal = _cn_ordinal(match.group("ordinal"))
    if ordinal is None:
        return None
    merged_cells = table.get("merged_cells")
    if not isinstance(merged_cells, list) or not any(
        isinstance(cell, dict)
        and _int_or_none(cell.get("row")) == 0
        and _int_or_none(cell.get("col")) == 0
        and _int_or_none(cell.get("rowspan")) == 1
        and _int_or_none(cell.get("colspan")) == columns
        for cell in merged_cells
    ):
        return None
    return ordinal, match.group("label")


_PARAMETER_VALUE_ITEM_RE = re.compile(
    r"^\s*(?P<ordinal>\d{1,2})、"
    r"(?P<label>[^\n：:]{1,40})[：:]\s*(?P<value>\d[^\n]{0,220})$"
)


def _parameter_value_list_indices(elements: list[dict[str, Any]]) -> set[int]:
    """Find a consecutive numeric parameter list misread as headings.

    A ``1、label：numeric value`` heading followed by text-kind 2/3/… items
    is evidence, not an outline: leaving the mixed source classifications in
    S2 can silently consume the first values as empty structure. Recovery
    requires an adjacent business sequence beginning at 1, at least three
    consecutive ordinals, a numeric value immediately after every colon, and
    both heading and text source kinds. Page furniture may separate pages.
    """

    business = [
        (index, element)
        for index, element in enumerate(elements)
        if str(element.get("kind") or "") != "page_furniture"
    ]
    recovered: set[int] = set()
    position = 0
    while position < len(business):
        _index, element = business[position]
        first = _PARAMETER_VALUE_ITEM_RE.fullmatch(
            _clean_text(_element_text(element))
        )
        if (
            first is None
            or int(first.group("ordinal")) != 1
            or str(element.get("kind") or "") not in {"heading", "text"}
        ):
            position += 1
            continue
        group: list[tuple[int, dict[str, Any]]] = []
        expected = 1
        cursor = position
        while cursor < len(business):
            candidate_index, candidate = business[cursor]
            if str(candidate.get("kind") or "") not in {"heading", "text"}:
                break
            match = _PARAMETER_VALUE_ITEM_RE.fullmatch(
                _clean_text(_element_text(candidate))
            )
            if match is None or int(match.group("ordinal")) != expected:
                break
            group.append((candidate_index, candidate))
            expected += 1
            cursor += 1
        source_kinds = {
            str(candidate.get("kind") or "") for _, candidate in group
        }
        if len(group) >= 3 and source_kinds == {"heading", "text"}:
            recovered.update(candidate_index for candidate_index, _ in group)
            position = cursor
            continue
        position += 1
    return recovered


def _is_statement_orphan_row_candidate(element: dict[str, Any]) -> bool:
    if (
        str(element.get("kind") or "") != "heading"
        or str(element.get("raw_kind") or "") != "text"
        or _int_or_none(element.get("heading_level")) != 1
        or _normalized_statement_cell(_element_text(element))
        != _STATEMENT_ORPHAN_ROW_LABEL
    ):
        return False
    bbox = _element_bbox(element)
    return bbox is not None and bbox[1] <= 150


def _preceding_statement_table_proves_orphan(
    previous: dict[str, Any], candidate: dict[str, Any]
) -> bool:
    previous_page = _int_or_none(previous.get("page_no"))
    candidate_page = _int_or_none(candidate.get("page_no"))
    if (
        str(previous.get("kind") or "") != "table"
        or previous_page is None
        or candidate_page != previous_page + 1
    ):
        return False
    table = previous.get("table")
    if not isinstance(table, dict):
        return False
    rows = table.get("rows")
    if not isinstance(rows, list) or len(rows) < 2:
        return False
    tail = rows[-2:]
    if any(not isinstance(row, list) or len(row) != 4 for row in tail):
        return False
    labels = tuple(_normalized_statement_cell(str(row[0])) for row in tail)
    return labels == _STATEMENT_ORPHAN_PREVIOUS_LABELS and all(
        not str(cell).strip() for row in tail for cell in row[1:]
    )


def _following_statement_boundary_proves_orphan(
    elements: list[dict[str, Any]],
    indices: list[int],
    candidate: dict[str, Any],
) -> bool:
    page_no = _int_or_none(candidate.get("page_no"))
    if page_no is None:
        return False
    signatories = [elements[index] for index in indices[:3]]
    next_statement = elements[indices[3]]
    if any(
        str(item.get("kind") or "") not in {"text", "heading"}
        or _int_or_none(item.get("page_no")) != page_no
        or not _normalized_statement_cell(_element_text(item)).startswith(prefix)
        for item, prefix in zip(signatories, _STATEMENT_SIGNATORY_PREFIXES, strict=True)
    ):
        return False
    if (
        str(next_statement.get("kind") or "") not in {"text", "heading"}
        or _int_or_none(next_statement.get("page_no")) != page_no
        or _structural_statement_key(_element_text(next_statement))
        not in {"cash_flow_statement", "cash_flow_statement_parent"}
    ):
        return False

    candidate_bbox = _element_bbox(candidate)
    signatory_bboxes = [_element_bbox(item) for item in signatories]
    statement_bbox = _element_bbox(next_statement)
    if (
        candidate_bbox is None
        or statement_bbox is None
        or any(bbox is None for bbox in signatory_bboxes)
    ):
        return False
    concrete_signatory_bboxes = [bbox for bbox in signatory_bboxes if bbox is not None]
    return (
        candidate_bbox[3] <= min(bbox[1] for bbox in concrete_signatory_bboxes) + 4
        and max(bbox[3] for bbox in concrete_signatory_bboxes) <= statement_bbox[1] + 4
    )


def _previous_business_element_index(
    elements: list[dict[str, Any]], index: int
) -> int | None:
    for candidate_index in range(index - 1, -1, -1):
        if str(elements[candidate_index].get("kind") or "") != "page_furniture":
            return candidate_index
    return None


def _following_business_element_indices(
    elements: list[dict[str, Any]], index: int, *, limit: int
) -> list[int]:
    indices: list[int] = []
    for candidate_index in range(index + 1, len(elements)):
        if str(elements[candidate_index].get("kind") or "") == "page_furniture":
            continue
        indices.append(candidate_index)
        if len(indices) == limit:
            break
    return indices


def _normalized_statement_cell(value: str) -> str:
    return re.sub(
        r"\s+",
        "",
        value.translate(str.maketrans("（）：／", "():/")),
    )


def _split_note_heading_fragments(
    elements: list[dict[str, Any]],
) -> list[_SplitNoteHeadingRecovery]:
    """Recover a statutory note number detached into a left-margin glyph.

    MinerU can classify the number as ``page_number``/``aside_text`` while
    keeping the title as a heading (or even page furniture).  Pairing is
    deliberately geometry- and vocabulary-gated: one 1–3 digit fragment must
    align vertically with one exact controlled title on the same page, sit to
    its left with a bounded gap, and have no competing match.  Bottom page
    numbers and ordinary marginal annotations therefore cannot participate.
    """

    numeric_candidates: list[tuple[int, int, int, tuple[float, ...]]] = []
    for index, element in enumerate(elements):
        kind = str(element.get("kind"))
        raw_kind = str(element.get("raw_kind") or kind)
        if not (
            (kind == "page_furniture" and raw_kind == "page_number")
            or (kind == "unknown" and raw_kind == "aside_text")
        ):
            continue
        value = _clean_text(_element_text(element))
        page_no = _int_or_none(element.get("page_no"))
        bbox = _element_bbox(element)
        if (
            page_no is None
            or bbox is None
            or not re.fullmatch(r"\d{1,3}", value)
            or not 1 <= int(value) <= 200
            or bbox[0] >= 200
        ):
            continue
        numeric_candidates.append((index, page_no, int(value), bbox))

    proposals: dict[int, list[tuple[int, str]]] = {}
    prefix_owners: Counter[int] = Counter()
    for title_index, element in enumerate(elements):
        if str(element.get("kind")) not in {"heading", "page_furniture"}:
            continue
        title = _clean_text(_element_text(element))
        if (
            not title
            or _pattern_heading_level(title) is not None
            or rules.exact_note_key_for_title(title) is None
        ):
            continue
        page_no = _int_or_none(element.get("page_no"))
        title_bbox = _element_bbox(element)
        if page_no is None or title_bbox is None or title_bbox[0] >= 350:
            continue
        matches: list[tuple[int, str]] = []
        title_center = (title_bbox[1] + title_bbox[3]) / 2
        for prefix_index, prefix_page, number, prefix_bbox in numeric_candidates:
            prefix_center = (prefix_bbox[1] + prefix_bbox[3]) / 2
            gap = title_bbox[0] - prefix_bbox[2]
            if (
                prefix_page == page_no
                and 0 <= gap <= 80
                and abs(prefix_center - title_center) <= 4
            ):
                matches.append((prefix_index, f"{number} {title}"))
        if len(matches) == 1:
            proposals[title_index] = matches
            prefix_owners[matches[0][0]] += 1

    recoveries: list[_SplitNoteHeadingRecovery] = []
    for title_index, matches in proposals.items():
        prefix_index, recovered_text = matches[0]
        if prefix_owners[prefix_index] != 1:
            continue
        title_element = elements[title_index]
        title_bbox = _element_bbox(title_element)
        page_no = _int_or_none(title_element.get("page_no"))
        if title_bbox is None or page_no is None:
            continue
        below_candidates: list[tuple[float, float, int]] = []
        for candidate_index, candidate in enumerate(elements):
            if candidate_index in {prefix_index, title_index} or str(
                candidate.get("kind")
            ) not in {"heading", "text", "table", "image", "equation"}:
                continue
            if _int_or_none(candidate.get("page_no")) != page_no:
                continue
            candidate_bbox = _element_bbox(candidate)
            if candidate_bbox is None or candidate_bbox[1] < title_bbox[3] - 2:
                continue
            below_candidates.append(
                (candidate_bbox[1], candidate_bbox[0], candidate_index)
            )
        insertion_index = title_index
        if below_candidates:
            visual_target = min(below_candidates)[2]
            if visual_target < title_index:
                insertion_index = visual_target
        recoveries.append(
            _SplitNoteHeadingRecovery(
                prefix_index=prefix_index,
                title_index=title_index,
                insertion_index=insertion_index,
                text=recovered_text,
                visual_top=title_bbox[1],
            )
        )
    return recoveries


def _statement_furniture_captions(
    elements: list[dict[str, Any]],
) -> dict[int, str]:
    """Recover a statement caption MinerU emitted as page furniture.

    Recovery is deliberately geometric and exact.  A controlled statement
    title must be visibly above the table on the same page; an ambiguous page
    or a table that already has a controlled caption is left untouched.
    Source order is not used because MinerU can serialize the table before its
    visually preceding title (observed in 1222948914).
    """

    candidates_by_page: dict[int, list[tuple[str, str, tuple[float, ...]]]] = {}
    for element in elements:
        if str(element.get("kind")) != "page_furniture":
            continue
        page_no = _int_or_none(element.get("page_no"))
        bbox = _element_bbox(element)
        title = _clean_text(_element_text(element))
        key = _structural_statement_key(title)
        if page_no is None or bbox is None or key not in rules.FINANCIAL_STATEMENT_KEYS:
            continue
        normalized = re.sub(r"\s+", "", _statement_stack_title(title))
        candidates_by_page.setdefault(page_no, []).append((normalized, title, bbox))

    recovered: dict[int, str] = {}
    for index, element in enumerate(elements):
        if str(element.get("kind")) != "table":
            continue
        if any(
            _structural_statement_key(str(caption)) is not None
            for caption in element.get("table_caption") or []
        ):
            continue
        page_no = _int_or_none(element.get("page_no"))
        table_bbox = _element_bbox(element)
        if page_no is None or table_bbox is None:
            continue
        distinct: dict[str, str] = {}
        for normalized, title, title_bbox in candidates_by_page.get(page_no, []):
            visibly_above = title_bbox[3] <= table_bbox[1]
            horizontally_overlaps = max(title_bbox[0], table_bbox[0]) <= min(
                title_bbox[2], table_bbox[2]
            )
            if visibly_above and horizontally_overlaps:
                distinct.setdefault(normalized, title)
        if len(distinct) == 1:
            recovered[index] = next(iter(distinct.values()))
    return recovered


def _element_bbox(element: dict[str, Any]) -> tuple[float, ...] | None:
    bbox = element.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        return tuple(float(value) for value in bbox[:4])
    except (TypeError, ValueError):
        return None


def _structural_furniture_heading_indices(
    elements: list[dict[str, Any]],
) -> set[int]:
    """Promote the first exact section banner that MinerU labelled furniture.

    Repeated running headers remain furniture.  The closed title set is kept
    in rules.py; no fuzzy note-key or substring match may create structure.
    """

    recovered: set[int] = set()
    seen: set[str] = set()
    for index, element in enumerate(elements):
        if str(element.get("kind")) != "page_furniture":
            continue
        title = _normalized_title(_clean_text(_element_text(element)))
        if title not in rules.STRUCTURAL_PAGE_FURNITURE_TITLES or title in seen:
            continue
        seen.add(title)
        recovered.add(index)
    return recovered


def _inferred_repeated_page_furniture(
    elements: list[dict[str, Any]],
) -> set[int]:
    """Find short repeated top-of-page headings MinerU failed to tag.

    The gate is document-local and geometric: exact normalized text plus a
    stable top y-position, then either a repeated ``(续)`` heading or matching
    page-furniture labels already recognized on other pages. A body occurrence
    of the same text at another y-position is retained.
    """

    groups: dict[tuple[str, int], list[tuple[int, int]]] = {}
    declared_pages: dict[str, set[int]] = {}
    top_heading_runs: dict[str, list[tuple[int, int]]] = {}
    for index, element in enumerate(elements):
        kind = str(element.get("kind"))
        if kind not in {"heading", "text", "page_furniture"}:
            continue
        text = _normalized_title(_clean_text(_element_text(element)))
        page_no = _int_or_none(element.get("page_no"))
        bbox = element.get("bbox")
        if (
            not text
            or len(text) > 100
            or page_no is None
            or not isinstance(bbox, list)
            or len(bbox) < 4
        ):
            continue
        try:
            top = float(bbox[1])
            bottom = float(bbox[3])
        except (TypeError, ValueError):
            continue
        if top < 0 or bottom > 220:
            continue
        key = (text, int(top // 50))
        if kind == "page_furniture":
            # MinerU often emits the declared page-furniture copy and the
            # leaked heading copy in different top bands on the same page.
            # Geometry still gates candidates to the top region, but declared
            # corroboration is text-level rather than y-bucket-level.
            declared_pages.setdefault(text, set()).add(page_no)
            continue
        if kind == "text" and element.get("heading_level") is None:
            continue
        groups.setdefault(key, []).append((index, page_no))
        top_heading_runs.setdefault(text, []).append((index, page_no))

    inferred: set[int] = set()
    for key, members in groups.items():
        candidate_pages = {page_no for _, page_no in members}
        continued = bool(re.search(r"[（(]\s*续\s*[）)]", key[0]))
        corroborated = len(declared_pages.get(key[0], set())) >= 2
        if (continued and len(candidate_pages) >= 3) or corroborated:
            inferred.update(index for index, _ in members)
    for text, members in top_heading_runs.items():
        if len({page_no for _, page_no in members}) < 3:
            continue
        note_key = rules.note_key_for_title(text)
        plausible_running_header = note_key in {
            "financial_report_chapter",
            "financial_statements_section",
            "consolidated_notes",
            "parent_company_notes",
        } or bool(
            re.fullmatch(
                r"[\u3400-\u9fffA-Za-z0-9·（）()]{2,50}"
                r"(?:股份有限公司|有限责任公司)",
                text,
            )
        )
        if not plausible_running_header:
            continue
        # Running issuer/report/notes banners are structural only once. Keep
        # the earliest top-of-page anchor and drop later copies; a same-text
        # body occurrence below the geometry gate never enters this run.
        ordered = sorted(members)
        inferred.update(index for index, _ in ordered[1:])
    return inferred


_ANNOUNCEMENT_SIGNING_DATE_RE = re.compile(
    r"^[二〇○零一二三四五六七八九十]{4}年"
    r"[一二三四五六七八九十]{1,3}月"
    r"[一二三四五六七八九十]{1,3}日$"
)

_EXPLICIT_QA_SECTION_MARKER_RE = re.compile(
    r"^(?:(?:第[一二三四五六七八九十百]+[章节])|"
    r"(?:[一二三四五六七八九十百]+、)|(?:\d{1,3}[、.．]))?"
    r"(?:主要)?(?:交流问题|问答(?:环节)?|提问(?:环节)?)$"
)


def _is_explicit_qa_section_marker(element: PreparedElement) -> bool:
    """Identify a short structural boundary that opens the Q&A transcript.

    Some investor-relations documents contain numbered strategy or product
    sections before a later explicit ``问答环节``.  Relaxing every numbered
    heading across the whole document turns those ordinary sections into fake
    questions.  This closed title family lets S2 keep the preamble structural
    and switch to QA heading handling only at the declared transcript boundary.
    """

    if element.kind not in {"heading", "text"}:
        return False
    title = _normalized_title(element.text or "")
    return bool(title and _EXPLICIT_QA_SECTION_MARKER_RE.fullmatch(title))


def _prepared_bbox(
    element: PreparedElement,
) -> tuple[float, float, float, float] | None:
    bbox = (element.artifact_locator or {}).get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        return (
            float(bbox[0]),
            float(bbox[1]),
            float(bbox[2]),
            float(bbox[3]),
        )
    except (TypeError, ValueError):
        return None


def _terminal_attachment_table_indices(
    elements: list[PreparedElement],
) -> set[int]:
    """Prove a post-signature, new-page, table-only attachment suffix."""

    proven: set[int] = set()
    for index, element in enumerate(elements):
        if element.kind != "table" or not element.table_caption:
            continue
        first_caption = element.table_caption[0].strip()
        if rules.ATTACHMENT_CAPTION_RE.match(first_caption) is None:
            continue
        bbox = _prepared_bbox(element)
        if element.page_no is None or bbox is None or bbox[1] > 200 or index == 0:
            continue
        previous = elements[index - 1]
        if previous.page_no is None or previous.page_no >= element.page_no:
            continue
        suffix = elements[index + 1 :]
        if any(item.kind != "table" for item in suffix):
            continue
        preceding_text = [
            (item.text or "").strip()
            for item in elements[max(0, index - 8) : index]
            if item.kind in {"heading", "text"} and (item.text or "").strip()
        ]
        if not any(rules.is_closing_formula_line(value) for value in preceding_text):
            continue
        if not any(
            _ANNOUNCEMENT_SIGNING_DATE_RE.fullmatch(value)
            for value in preceding_text
        ):
            continue
        proven.add(index)
    return proven


def s2_apply_heading_tree(
    elements: Iterable[PreparedElement],
    *,
    qa_heading_mode: bool = False,
) -> list[PreparedElement]:
    element_list = list(elements)
    has_supplement_root = any(
        _normalized_title(element.text or "") == rules.SUPPLEMENTAL_FINANCIAL_INFO_TITLE
        for element in element_list
        if element.kind == "heading"
    )
    # (level, title, ordinal, left) — ordinal and indentation repair parser
    # heading-level flattening without hard-coding one numbering style.
    stack: list[HeadingStackEntry] = []
    placed: list[PreparedElement] = []
    source_heading_anchor: _SourceHeadingAnchor | None = None
    content_since_source_heading = False
    single_dot_sequences: list[_SingleDotSequence] = []
    local_roman_sequences: list[_LocalRomanSequence] = []
    local_numeric_sequences: list[_LocalNumericSequence] = []
    parenthesized_heading_sequences: list[_ParenthesizedHeadingSequence] = []
    terminal_attachment_tables = _terminal_attachment_table_indices(element_list)
    has_explicit_qa_section = qa_heading_mode and any(
        _is_explicit_qa_section_marker(element) for element in element_list
    )
    qa_section_active = not has_explicit_qa_section
    for element_index, element in enumerate(element_list):
        text = (element.text or "").strip()
        explicit_qa_section_marker = (
            qa_heading_mode and _is_explicit_qa_section_marker(element)
        )
        if explicit_qa_section_marker:
            qa_section_active = True
        inside_skipped_section = any(
            _skip_section_title(title) is not None for _, title, _, _ in stack
        )
        toc_page_reference = inside_skipped_section and _is_toc_page_reference(text)
        if (
            inside_skipped_section
            and element.kind == "heading"
            and not toc_page_reference
        ):
            # A real parser heading is the only high-confidence exit from a
            # TOC/definitions trap. A heading-shaped TOC row with a terminal
            # page reference is not an exit: MinerU promotes those rows to h1
            # on several real reports. Reset before normal level inference so
            # a real style such as "1. 释义" does not remain below "目录".
            stack = []
            source_heading_anchor = None
            content_since_source_heading = False
            inside_skipped_section = False
        if _is_repeated_controlled_ancestor_heading(element, stack):
            # Issuers often repeat an identical ``2025 ...财务报表附注``
            # banner as a MinerU heading on every page.  Reopening the same
            # controlled ancestor creates an ever-deeper stack; the next real
            # major section then attaches below the stale 税项/会计政策 scope.
            # The first open ancestor remains authoritative, so later exact
            # duplicates are page furniture for structure purposes.
            continue
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
            source_heading_anchor = None
            content_since_source_heading = False
        if element.kind == "table" and element.table_caption:
            first_caption = str(element.table_caption[0]).strip()
            if rules.ATTACHMENT_CAPTION_RE.match(first_caption) and (
                qa_heading_mode or element_index in terminal_attachment_tables
            ):
                # In official forms an attachment is a top-level sibling. In
                # narrative announcements the same reset is safe only for a
                # proven post-signature, new-page, table-only terminal suffix;
                # ordinary in-body attachments keep their surrounding scope.
                if qa_heading_mode:
                    stack = [(1, first_caption, None, _heading_left(element))]
                else:
                    root = stack[:1]
                    caption_level = root[0][0] + 1 if root else 1
                    stack = [
                        *root,
                        (
                            caption_level,
                            first_caption,
                            None,
                            _heading_left(element),
                        ),
                    ]
                source_heading_anchor = None
        if element.kind == "table" and element.table_caption:
            statement_caption = next(
                (
                    str(caption).strip()
                    for caption in element.table_caption
                    if _structural_statement_key(str(caption).strip()) is not None
                ),
                None,
            )
            if statement_caption is not None:
                statement_level = _financial_statement_sibling_level(stack)
                stack = [entry for entry in stack if entry[0] < statement_level]
                stack.append(
                    (
                        statement_level,
                        _statement_stack_title(statement_caption),
                        None,
                        _heading_left(element),
                    )
                )
                source_heading_anchor = None
        if (
            qa_heading_mode
            and element.kind == "heading"
            and placed
            and placed[-1].kind == "text"
            and placed[-1].qa_question_boundary
            and not (placed[-1].text or "").rstrip().endswith(("?", "？"))
            and not _numbered_line(text)
            and element.heading_level == placed[-1].heading_level
            and 0 < element.order_index - placed[-1].order_index <= 2
            and any(mark in text for mark in ("?", "？"))
        ):
            # MinerU can split one long numbered investor question into two
            # consecutive same-level headings. Merge the physical fragments
            # before S3 so the continuation is not mistaken for either an
            # answer or a new structural section (1217897311 Q4).
            previous = placed[-1]
            locator = dict(previous.artifact_locator or {})
            locator["source_order_span"] = sorted(
                [previous.order_index, element.order_index]
            )
            placed[-1] = PreparedElement(
                **{
                    **previous.__dict__,
                    "text": _join_wrapped_lines([previous.text or "", text]),
                    "artifact_locator": locator,
                }
            )
            content_since_source_heading = True
            continue
        if (
            qa_heading_mode
            and element.kind == "heading"
            and rules.ATTACHMENT_CAPTION_RE.match(text)
        ):
            # The same official attachment boundary can arrive as a heading
            # instead of a table caption. It is a sibling of the narrative
            # sections, even when MinerU reports heading_level=2.
            stack = [(1, text, None, _heading_left(element))]
            source_heading_anchor = _SourceHeadingAnchor(
                effective_level=1,
                pattern_level=_pattern_heading_level(text),
                source_level=element.heading_level,
                left=_heading_left(element),
            )
            content_since_source_heading = False
            continue
        if element.kind == "table" and element.table_caption:
            # A caption that continues an already-proven local numeric family
            # is structural even when its business label is not in the
            # semantic vocabulary.  CMB note 61 has headings ``(1)/(2)`` and
            # then emits ``(3)/(4)`` only as table captions; waiting for a
            # controlled key until ``(5) 客户存款`` would lose the sequence and
            # reanchor that later table at the global digit-paren level.
            local_numbered_caption = next(
                (
                    str(caption).strip()
                    for caption in element.table_caption
                    if _numeric_under_local_roman_level(stack, str(caption).strip())
                    is not None
                    or _closed_local_numeric_sequence(
                        local_numeric_sequences,
                        stack=stack,
                        title=str(caption).strip(),
                        order_index=element.order_index,
                    )
                    is not None
                ),
                None,
            )
            controlled_caption = local_numbered_caption or next(
                (
                    str(caption).strip()
                    for caption in element.table_caption
                    if _pattern_heading_level(str(caption).strip()) is not None
                    and _is_controlled_boundary_title(str(caption).strip())
                    and _structural_statement_key(str(caption).strip()) is None
                ),
                None,
            )
            if controlled_caption is not None:
                # A numbered controlled table caption is a real section
                # boundary, not metadata local to the table. Update S2's
                # structural state so caption-less prose/tables that follow
                # do not fall back to the preceding sibling (full-corpus
                # examples: notes 10→11, 59→60, governance 二→三).
                stack = _stack_reanchored_to_numbered_caption(
                    stack,
                    title=controlled_caption,
                    left=_heading_left(element),
                    order_index=element.order_index,
                    local_numeric_sequences=local_numeric_sequences,
                )
                source_heading_anchor = None
        if (
            qa_heading_mode
            and qa_section_active
            and not explicit_qa_section_marker
            and element.kind in {"heading", "text"}
            and (
                _numbered_line(element.text or "")
                or _qa_physical_question_heading(element)
            )
        ):
            heading_path = _public_heading_path(stack)
            structural_path = _internal_heading_path(stack)
            placed.append(
                PreparedElement(
                    **{
                        **element.__dict__,
                        "kind": "text",
                        "heading_path": heading_path,
                        "structural_path": structural_path,
                        "title": element.title
                        or (heading_path[-1] if heading_path else None),
                        # Preserve the physical MinerU heading signal after
                        # demotion. S4 may use it as a strong interaction
                        # boundary even when an investor statement/suggestion
                        # is not grammatically phrased as a question.
                        "qa_question_boundary": (
                            element.kind == "heading"
                            or _qa_physical_question_heading(element)
                        ),
                    }
                )
            )
            content_since_source_heading = True
            continue
        if _is_statement_signatory_heading(element, stack):
            # Bank statements often promote two-to-four-character signer names
            # to h1. They are statement content, not structural breadcrumbs.
            heading_path = _public_heading_path(stack)
            structural_path = _internal_heading_path(stack)
            placed.append(
                PreparedElement(
                    **{
                        **element.__dict__,
                        "kind": "text",
                        "heading_path": heading_path,
                        "structural_path": structural_path,
                        "title": stack[-1][1] if stack else None,
                    }
                )
            )
            content_since_source_heading = True
            continue
        # TOC entries often look exactly like real numbered headings. While
        # an explicit skip section is open, a text-kind candidate stays under
        # that section; only an explicit parser heading can close the trap.
        level = (
            None
            if inside_skipped_section
            and (element.kind == "text" or toc_page_reference)
            else _heading_level_for(element)
        )
        if level is None and not inside_skipped_section:
            level = _overlong_text_digit_close_sibling_level(
                parenthesized_heading_sequences,
                stack=stack,
                element=element,
            )
        if level is None and element.kind == "text" and not inside_skipped_section:
            # MinerU sometimes emits an exact continuation/next member of a
            # locally proven Latin/Roman family as plain text.  Promote only a
            # bounded heading-shaped line; substantive numbered prose remains
            # evidence and is handled by the sibling-close rule below.
            local_text_latin_level = _local_latin_sibling_level(stack, text)
            local_text_roman_level = (
                None
                if local_text_latin_level is not None
                else _local_roman_level(stack, text)
            )
            local_text_roman_history = _closed_local_roman_sequence(
                local_roman_sequences,
                stack=stack,
                title=text,
                left=_heading_left(element),
                order_index=element.order_index,
            )
            local_text_numeric_level = _numeric_under_local_roman_level(stack, text)
            local_text_numeric_history = _closed_local_numeric_sequence(
                local_numeric_sequences,
                stack=stack,
                title=text,
                order_index=element.order_index,
            )
            local_text_level = next(
                (
                    candidate
                    for candidate in (
                        local_text_latin_level,
                        local_text_roman_level,
                        (
                            local_text_roman_history.effective_level
                            if local_text_roman_history is not None
                            else None
                        ),
                        local_text_numeric_level,
                        (
                            local_text_numeric_history.effective_level
                            if local_text_numeric_history is not None
                            else None
                        ),
                    )
                    if candidate is not None
                ),
                None,
            )
            if local_text_level is not None and _text_heading_candidate(text):
                level = local_text_level
            elif (
                local_text_numeric_level is not None
                or local_text_numeric_history is not None
            ):
                # ``(7) ...。`` can be a substantive list paragraph rather
                # than a heading.  It still proves that the preceding ``(6)``
                # leaf ended, so retain the paragraph under the Roman parent
                # instead of leaking the old numeric title into it and its
                # following table.
                if local_text_numeric_level is not None:
                    numeric_level = local_text_numeric_level
                else:
                    assert local_text_numeric_history is not None
                    numeric_level = local_text_numeric_history.effective_level
                if local_text_numeric_history is not None:
                    stack = list(local_text_numeric_history.parent_stack)
                else:
                    stack = [entry for entry in stack if entry[0] < numeric_level]
                _remember_local_numeric_sequence(
                    local_numeric_sequences,
                    title=text,
                    effective_level=numeric_level,
                    parent_stack=stack,
                    order_index=element.order_index,
                    reopened=local_text_numeric_history,
                )
        if level is not None:
            text = (element.text or "").strip()
            pattern_level = _pattern_heading_level(text)
            glued_controlled_level = _glued_controlled_note_sibling_level(
                stack, text
            )
            if pattern_level is None and glued_controlled_level is not None:
                pattern_level = 5
            ordinal = _heading_ordinal(text)
            if ordinal is None and glued_controlled_level is not None:
                ordinal = _glued_controlled_note_ordinal(text)
            decimal_outline = _decimal_outline_parts(text)
            statement_heading = _structural_statement_key(text) is not None
            repeated_controlled_level = _same_exact_controlled_ancestor_level(
                element, stack
            )
            controlled_note_sibling = False
            source_heading_sibling = False
            supplement_after_approval = bool(
                has_supplement_root
                and _single_dot_ordinal(text) == 1
                and any(
                    rules.exact_note_key_for_title(open_title)
                    == "financial_statement_approval"
                    for _, open_title, _, _ in stack
                )
            )
            if supplement_after_approval:
                stack = [
                    (
                        1,
                        rules.SUPPLEMENTAL_FINANCIAL_INFO_TITLE,
                        None,
                        _heading_left(element),
                    )
                ]
                history_sequence = None
            else:
                history_sequence = _closed_single_dot_sequence(
                    single_dot_sequences,
                    stack=stack,
                    title=text,
                    left=_heading_left(element),
                )
            local_latin_level = _local_latin_sibling_level(stack, text)
            local_roman_level = (
                None
                if local_latin_level is not None
                else _local_roman_level(stack, text)
            )
            local_roman_history = (
                None
                if local_latin_level is not None
                else _closed_local_roman_sequence(
                    local_roman_sequences,
                    stack=stack,
                    title=text,
                    left=_heading_left(element),
                    order_index=element.order_index,
                )
            )
            local_roman_numeric_level = _numeric_under_local_roman_level(stack, text)
            exact_controlled_digit_close_level = (
                _digit_close_under_exact_controlled_parent_level(stack, text)
            )
            proven_digit_close_level = (
                _digit_close_under_proven_parenthesized_sequence_level(
                    parenthesized_heading_sequences,
                    stack=stack,
                    title=text,
                    order_index=element.order_index,
                )
            )
            proven_parenthesized_sequence = (
                _proven_parenthesized_heading_sequence(
                    parenthesized_heading_sequences,
                    stack=stack,
                    element=element,
                    title=text,
                )
            )
            local_numeric_history = _closed_local_numeric_sequence(
                local_numeric_sequences,
                stack=stack,
                title=text,
                order_index=element.order_index,
            )
            local_numbered_sibling = any(
                value is not None
                for value in (
                    local_latin_level,
                    local_roman_level,
                    local_roman_history,
                    local_roman_numeric_level,
                    exact_controlled_digit_close_level,
                    proven_digit_close_level,
                    proven_parenthesized_sequence,
                    local_numeric_history,
                )
            )
            history_numbered_sibling = history_sequence is not None
            if history_sequence is not None:
                # Reopen only the proven sequence's parent snapshot.  Keeping
                # the current stack would retain the local hotspot headings
                # that closed the chapter run in the first place.
                stack = list(history_sequence.parent_stack)
            if local_roman_history is not None:
                # A repeated Latin parent banner may have closed the prior
                # Roman child.  Reopen only the exact remembered parent; no
                # unrelated document-global numbering assumption is used.
                stack = list(local_roman_history.parent_stack)
            if local_numeric_history is not None:
                stack = list(local_numeric_history.parent_stack)
            statement_exit_level = _statement_block_exit_level(
                stack,
                title=text,
                ordinal=ordinal,
                pattern_level=pattern_level,
            )
            if repeated_controlled_level is not None:
                # A genuine source heading that reopens an exact controlled
                # root is a same-level boundary, not page furniture and not a
                # child with the same name. Only the separately proven
                # recovered supplement banner is swallowed above.
                level = repeated_controlled_level
                controlled_note_sibling = True
            elif supplement_after_approval:
                level = 2
                controlled_note_sibling = True
            elif history_sequence is not None:
                level = history_sequence.effective_level
            elif statement_exit_level is not None:
                level = statement_exit_level
            elif statement_heading:
                level = _financial_statement_sibling_level(stack)
            elif (
                rules.exact_note_key_for_title(text) == "company_profile"
                and (notes_level := _open_financial_notes_level(stack)) is not None
            ):
                # ``公司基本情况`` is a genuine top-level root in unnumbered
                # bank/H-share reports, hence its FIXED_L1 classification.
                # Inside an already-open statutory notes tree, however, it is
                # the first notes section (the detached ``一`` is sometimes
                # absent from MinerU output).  Context must win over the global
                # fixed-root rule or every later note inherits company_profile.
                level = notes_level + 1
                controlled_note_sibling = True
            elif (
                pattern_level == 2
                and re.match(r"^[一二三四五六七八九十]+\s+", text)
                and (notes_level := _open_financial_notes_level(stack)) is not None
            ):
                # ``一 公司基本情况`` / ``二 主要会计政策`` are children
                # of an unnumbered 财务报表附注 root, not roots themselves.
                level = notes_level + 1
            elif decimal_outline is not None:
                level = _repair_decimal_outline_level(
                    stack,
                    level,
                    decimal_outline,
                    left=_heading_left(element),
                )
            elif local_latin_level is not None:
                level = local_latin_level
                source_heading_sibling = True
            elif local_roman_level is not None:
                level = local_roman_level
                source_heading_sibling = True
            elif local_roman_history is not None:
                level = local_roman_history.effective_level
                source_heading_sibling = True
            elif local_roman_numeric_level is not None:
                level = local_roman_numeric_level
                source_heading_sibling = True
            elif exact_controlled_digit_close_level is not None:
                level = exact_controlled_digit_close_level
                source_heading_sibling = True
            elif proven_digit_close_level is not None:
                level = proven_digit_close_level
                source_heading_sibling = True
            elif proven_parenthesized_sequence is not None:
                level = proven_parenthesized_sequence.effective_level
                source_heading_sibling = True
            elif local_numeric_history is not None:
                level = local_numeric_history.effective_level
                source_heading_sibling = True
            elif (
                pattern_level is not None
                or _normalized_title(text) in rules.FIXED_L1_TITLES
            ):
                continuity_level = _repair_level_by_continuity(
                    stack, level, ordinal, pattern_level
                )
                controlled_level = _controlled_note_sibling_level(stack, text)
                controlled_note_sibling = controlled_level is not None
                level = (
                    controlled_level
                    if controlled_level is not None
                    else continuity_level
                )
            elif (
                element.heading_level == 1
                and not any(
                    _pattern_heading_level(title) is not None
                    for _, title, _, _ in stack
                )
                and not _has_open_controlled_statement_structure(stack)
            ):
                # An explicit MinerU level-1 heading is a document-top block
                # only before numbered structure begins. Inside a numbered
                # report MinerU also emits note leaf labels as level 1; making
                # those roots poisoned every later section's heading_path.
                level = 1
            elif (
                sibling_level := _same_source_unnumbered_sibling_level(
                    element,
                    source_heading_anchor,
                    content_since_source_heading=content_since_source_heading,
                )
            ) is not None:
                # MinerU often flattens sibling Latin/unnumbered note labels to
                # one heading_level.  Equal source level + equal left edge +
                # intervening content is enough to close the preceding leaf;
                # consecutive heading-only label chains still nest.
                level = sibling_level
                source_heading_sibling = True
            else:
                # Unnumbered MinerU heading (sub-label like 安全生产费): its
                # raw heading_level is unreliable (flattened to 2 on real
                # filings) and used to evict the numbered parent — nest under
                # the current context instead (Codex round5).
                level = (stack[-1][0] if stack else 0) + 1
            if (
                pattern_level != 1
                and _normalized_title(text) not in rules.FIXED_L1_TITLES
                and not statement_heading
                and statement_exit_level is None
                and not controlled_note_sibling
                and not source_heading_sibling
                and not history_numbered_sibling
                and not local_numbered_sibling
            ):
                level = _repair_level_by_indentation(
                    stack,
                    level,
                    ordinal,
                    pattern_level,
                    _heading_left(element),
                )
                level = _repair_level_by_open_parent_pattern(
                    stack, level, ordinal, pattern_level
                )
            # Keep the complete internal tree even when its public breadcrumb
            # is capped at four levels.  The former depth guard also stopped
            # fifth-level headings from entering the stack; subsequent
            # ordinal continuity then lost its parent sequence and nested
            # later note headings under the wrong sibling (1217717242).
            stack = [entry for entry in stack if entry[0] < level]
            # ``(续)`` is page-continuation metadata, not a new business node.
            # Canonicalizing every structural heading here prevents repeated
            # pages from consuming public-depth slots or nesting later siblings.
            stack_title = _statement_stack_title(element.text or "")
            stack.append((level, stack_title, ordinal, _heading_left(element)))
            _remember_parenthesized_heading_sequence(
                parenthesized_heading_sequences,
                element=element,
                title=stack_title,
                effective_level=level,
                parent_stack=stack[:-1],
            )
            if local_roman_level is not None or local_roman_history is not None:
                _remember_local_roman_sequence(
                    local_roman_sequences,
                    title=stack_title,
                    effective_level=level,
                    parent_stack=stack[:-1],
                    left=_heading_left(element),
                    order_index=element.order_index,
                    reopened=local_roman_history,
                )
            if (
                local_roman_numeric_level is not None
                or local_numeric_history is not None
            ):
                _remember_local_numeric_sequence(
                    local_numeric_sequences,
                    title=stack_title,
                    effective_level=level,
                    parent_stack=stack[:-1],
                    order_index=element.order_index,
                    reopened=local_numeric_history,
                )
            _remember_single_dot_sequence(
                single_dot_sequences,
                title=stack_title,
                effective_level=level,
                parent_stack=stack[:-1],
                left=_heading_left(element),
                order_index=element.order_index,
                reopened=history_sequence,
            )
            source_heading_anchor = _SourceHeadingAnchor(
                effective_level=level,
                pattern_level=pattern_level,
                source_level=element.heading_level,
                left=_heading_left(element),
            )
            content_since_source_heading = False
            continue
        heading_path = _public_heading_path(stack)
        structural_path = _internal_heading_path(stack)
        # Public breadcrumbs remain capped at four levels, but title is the
        # deepest internal leaf. Otherwise fifth-level siblings collapse in S3
        # and their controlled note keys disappear from retrieval.
        title = element.title or (stack[-1][1] if stack else None)
        element_values = dict(element.__dict__)
        if element.kind == "heading":
            element_values["kind"] = "text"
        placed.append(
            PreparedElement(
                **{
                    **element_values,
                    "heading_path": heading_path,
                    "structural_path": structural_path,
                    "title": title,
                }
            )
        )
        content_since_source_heading = True
    return placed


def _public_heading_path(stack: list[HeadingStackEntry]) -> list[str]:
    """Project the full internal heading tree to the 1-4 level contract.

    Historically S2 kept only the first four stack entries, so content below
    that depth inherited those same four public ancestors.  Preserve that
    external shape while retaining deeper entries internally for correct
    sibling/sequence repair.
    """

    return [title for _, title, _, _ in stack[:4]]


def _internal_heading_path(stack: list[HeadingStackEntry]) -> list[str]:
    """Return the complete structural stack for internal boundary identity."""

    return [title for _, title, _, _ in stack]


def _same_source_unnumbered_sibling_level(
    element: PreparedElement,
    anchor: _SourceHeadingAnchor | None,
    *,
    content_since_source_heading: bool,
) -> int | None:
    """Recover same-style unnumbered siblings without flattening label chains.

    This is intentionally source- and geometry-gated.  A heading-only chain
    can still express parent → child, while two MinerU headings of the same
    raw level and left edge separated by real content are sibling sections.
    """

    if (
        element.kind != "heading"
        or element.heading_level is None
        or anchor is None
        or anchor.pattern_level is not None
        or anchor.source_level != element.heading_level
        or not content_since_source_heading
    ):
        return None
    left = _heading_left(element)
    if left is None or anchor.left is None or abs(left - anchor.left) > 8:
        return None
    return anchor.effective_level


def _is_repeated_controlled_ancestor_heading(
    element: PreparedElement, stack: list[HeadingStackEntry]
) -> bool:
    if (
        element.kind != "heading"
        or element.raw_kind != "recovered_section_page_furniture"
    ):
        return False
    title = (element.text or "").strip()
    key = rules.exact_note_key_for_title(title)
    if key != "supplementary_financial_information":
        return False
    normalized = _normalized_title(title)
    return any(
        _normalized_title(open_title) == normalized
        and rules.exact_note_key_for_title(open_title) == key
        for _, open_title, _, _ in stack
    )


def _same_exact_controlled_ancestor_level(
    element: PreparedElement, stack: list[HeadingStackEntry]
) -> int | None:
    if element.kind != "heading":
        return None
    title = (element.text or "").strip()
    key = rules.exact_note_key_for_title(title)
    if key is None:
        # Dated statutory roots such as ``2025 年上半年度财务报表附注``
        # intentionally cannot live in the exact label map.  The broader
        # matcher is safe here only for the two financial-notes root
        # families, because reopening still requires an exact normalized
        # title already present in the active stack.
        inferred_key = rules.note_key_for_title(title)
        if inferred_key in {"consolidated_notes", "parent_company_notes"}:
            key = inferred_key
    if key not in {
        "financial_report_chapter",
        "financial_statements_section",
        "consolidated_notes",
        "parent_company_notes",
        "supplementary_financial_information",
    }:
        return None
    normalized = _normalized_title(title)
    return next(
        (
            level
            for level, open_title, _, _ in reversed(stack)
            if _normalized_title(open_title) == normalized
            and (
                rules.exact_note_key_for_title(open_title) == key
                or (
                    key in {"consolidated_notes", "parent_company_notes"}
                    and rules.note_key_for_title(open_title) == key
                )
            )
        ),
        None,
    )


def _structural_statement_key(title: str | None) -> str | None:
    """Return a statutory-statement key only for a bounded full title.

    ``note_key_for_title`` deliberately supports longest-substring retrieval,
    but that is unsafe for structure: prose such as ``于合并利润表内确认的金额``
    and TOC rows containing several statement names were promoted to fake
    statement roots. Structural recovery accepts an exact controlled label or
    a bounded full-title grammar: an audit/period/issuer prefix plus only a
    statutory date/unit/standards suffix. Both sides are anchored, so prose
    such as ``于合并利润表内确认`` and ``合并利润表重大项目变化情况`` stay out.
    """

    return rules.structural_statement_key(title)


def _stack_reanchored_to_numbered_caption(
    stack: list[HeadingStackEntry],
    *,
    title: str,
    left: float | None,
    order_index: int,
    local_numeric_sequences: list[_LocalNumericSequence],
) -> list[HeadingStackEntry]:
    """Open a numbered controlled table caption as the current sibling."""

    pattern_level = _pattern_heading_level(title)
    if pattern_level is None:  # pragma: no cover - caller invariant
        return stack
    decimal_outline = _decimal_outline_parts(title)
    local_latin_level = _local_latin_sibling_level(stack, title)
    local_roman_level = (
        None if local_latin_level is not None else _local_roman_level(stack, title)
    )
    local_numeric_level = _numeric_under_local_roman_level(stack, title)
    local_numeric_history = _closed_local_numeric_sequence(
        local_numeric_sequences,
        stack=stack,
        title=title,
        order_index=order_index,
    )
    if local_numeric_history is not None:
        stack = list(local_numeric_history.parent_stack)
    local_level = next(
        (
            candidate
            for candidate in (
                local_latin_level,
                local_roman_level,
                local_numeric_level,
                (
                    local_numeric_history.effective_level
                    if local_numeric_history is not None
                    else None
                ),
            )
            if candidate is not None
        ),
        None,
    )
    if local_level is not None:
        # A table caption participates in the same already-proven local family
        # as a source heading.  This is what keeps CMB note 61's ``(3)/(4)``
        # tables below ``(b) > (i)`` instead of reanchoring them globally.
        effective_level = local_level
    elif decimal_outline is not None:
        # Decimal outline depth is document-local.  ``7.1`` has a nominal
        # depth of two, but that must never make it a sibling of a Chinese
        # level-2 root such as ``四、财务报表主要项目注释``.  Resolve it against
        # the open ``7.``/``7.x`` numeric family just as an ordinary heading
        # is resolved; otherwise one table caption can evict the notes root
        # and poison every following note (observed on the ICBC annual).
        effective_level = _repair_decimal_outline_level(
            stack,
            pattern_level,
            decimal_outline,
            left=left,
        )
    else:
        effective_level = pattern_level
        for stack_level, stack_title, _, _ in reversed(stack):
            if (
                _decimal_outline_parts(stack_title) is None
                and _pattern_heading_level(stack_title) == pattern_level
            ):
                effective_level = stack_level
                break
    anchored = [entry for entry in stack if entry[0] < effective_level]
    anchored.append((effective_level, title, _heading_ordinal(title), left))
    if local_numeric_level is not None or local_numeric_history is not None:
        _remember_local_numeric_sequence(
            local_numeric_sequences,
            title=title,
            effective_level=effective_level,
            parent_stack=anchored[:-1],
            order_index=order_index,
            reopened=local_numeric_history,
        )
    return anchored


def _financial_statement_sibling_level(
    stack: list[HeadingStackEntry],
) -> int:
    statement_levels = [
        stack_level
        for stack_level, stack_title, _, _ in stack
        if _structural_statement_key(stack_title) is not None
    ]
    if statement_levels:
        return statement_levels[-1]
    # Prefer an explicit statutory parent, but only its exact controlled title:
    # contains matching would misread ``注册会计师对财务报表审计的责任`` as the
    # parent and recreate the poisoned audit-responsibility chain.
    for stack_level, stack_title, _, _ in reversed(stack):
        if (
            rules.exact_note_key_for_title(stack_title)
            == "financial_statements_section"
        ):
            return stack_level + 1
    # Without an explicit parent, the first statement replaces the latest
    # level-2 topic (normally the auditor-responsibility heading) as its sibling.
    for stack_level, stack_title, _, _ in reversed(stack):
        if _pattern_heading_level(stack_title) == 2:
            return stack_level
    for stack_level, stack_title, _, _ in reversed(stack):
        if rules.note_key_for_title(stack_title) == "financial_report_chapter":
            return stack_level + 1
    # No trustworthy parent exists. Starting a fresh root is safer than
    # nesting a statutory statement below arbitrary MinerU h1 labels such as
    # signatory names; later statement captions then remain siblings.
    return 1


def _has_open_controlled_statement_structure(stack: list[HeadingStackEntry]) -> bool:
    """Protect an open statutory statement tree from arbitrary MinerU h1s."""

    for _, stack_title, _, _ in stack:
        key = rules.note_key_for_title(stack_title)
        if _structural_statement_key(stack_title) is not None or key in {
            "financial_report_chapter",
            "financial_statements_section",
            "consolidated_notes",
            "parent_company_notes",
        }:
            return True
    return False


def _is_statement_signatory_heading(
    element: PreparedElement, stack: list[HeadingStackEntry]
) -> bool:
    text = (element.text or "").strip()
    return bool(
        element.kind == "heading"
        and element.heading_level == 1
        and re.fullmatch(r"[\u3400-\u9fff·]{2,4}", text)
        and any(
            _structural_statement_key(stack_title) is not None
            for _, stack_title, _, _ in stack
        )
    )


def _open_financial_notes_level(stack: list[HeadingStackEntry]) -> int | None:
    for level, title, _, _ in reversed(stack):
        if rules.note_key_for_title(title) in {
            "consolidated_notes",
            "parent_company_notes",
        }:
            return level
    return None


def _controlled_note_sibling_level(
    stack: list[HeadingStackEntry], title: str
) -> int | None:
    """Skipped ordinals do not nest one exact controlled note below another."""

    pattern_level = _controlled_note_family_level(title)
    if pattern_level is None:
        return None

    continuation_base = _statement_stack_title(title)
    if continuation_base != title.strip():
        normalized_base = _normalized_title(continuation_base)
        for stack_level, stack_title, _, _ in reversed(stack):
            if (
                _controlled_note_family_level(stack_title) == pattern_level
                and _normalized_title(_statement_stack_title(stack_title))
                == normalized_base
            ):
                # Explicit continuation pages reopen the same numbered node,
                # including issuer-specific combined labels outside the exact
                # note vocabulary (e.g. 递延所得税资产和负债).
                return stack_level

    exact_key = rules.exact_note_key_for_title(title)
    if exact_key is None:
        return None

    if (
        pattern_level == 5
        and re.match(r"^\s*\d{1,3}\s+\S", title)
        and (ordinal := _heading_ordinal(title)) is not None
    ):
        for stack_level, stack_title, _, _ in reversed(stack):
            outline = _decimal_outline_parts(stack_title)
            if outline is not None and ordinal == outline[0] + 1:
                # Annual-report summaries sometimes omit an explicit ``4``
                # parent and expose only 4.1..4.4, then resume with controlled
                # ``5 公司债券情况``. The integer heading is the decimal run's
                # sibling, never its child.
                return stack_level
    for stack_level, stack_title, _, _ in reversed(stack):
        if (
            _controlled_note_family_level(stack_title) == pattern_level
            and rules.exact_note_key_for_title(stack_title) is not None
        ):
            return stack_level
    return None


_GLUED_CONTROLLED_NOTE_RE = re.compile(
    r"^\s*(?P<ordinal>\d{1,3})(?P<label>[\u3400-\u9fff][^\n]{0,100})$"
)


def _glued_controlled_note_ordinal(title: str) -> int | None:
    match = _GLUED_CONTROLLED_NOTE_RE.fullmatch(title)
    if match is None or rules.exact_note_key_for_title(title) is None:
        return None
    return int(match.group("ordinal"))


def _controlled_note_family_level(title: str) -> int | None:
    level = _pattern_heading_level(title)
    if level is not None:
        return level
    return 5 if _glued_controlled_note_ordinal(title) is not None else None


def _glued_controlled_note_sibling_level(
    stack: list[HeadingStackEntry], title: str
) -> int | None:
    """Recover a missing separator only beside a proven controlled sibling."""

    if _glued_controlled_note_ordinal(title) is None:
        return None
    return next(
        (
            stack_level
            for stack_level, stack_title, _, _ in reversed(stack)
            if _controlled_note_family_level(stack_title) == 5
            and rules.exact_note_key_for_title(stack_title) is not None
        ),
        None,
    )


def _statement_block_exit_level(
    stack: list[HeadingStackEntry],
    *,
    title: str,
    ordinal: int | None,
    pattern_level: int | None,
) -> int | None:
    """Close an unnumbered statement run when the numbered notes restart."""

    note_key = rules.note_key_for_title(title)
    controlled_notes_start = note_key in {
        "consolidated_notes",
        "parent_company_notes",
    }
    controlled_first_note = ordinal == 1 and rules.exact_note_key_for_title(title) in {
        "company_profile",
        "basis_of_preparation",
        "accounting_policies",
        "accounting_policy",
    }
    if (
        not controlled_notes_start
        and not controlled_first_note
        and (pattern_level != 2 or ordinal != 1)
    ):
        return None
    statement_levels = [
        stack_level
        for stack_level, stack_title, _, _ in stack
        if _structural_statement_key(stack_title) is not None
    ]
    if not statement_levels:
        return None
    for stack_level, stack_title, _, _ in reversed(stack):
        if (
            rules.exact_note_key_for_title(stack_title)
            == "financial_statements_section"
        ):
            return stack_level
    return statement_levels[-1]


_STATEMENT_CONTINUATION_RE = re.compile(r"\s*(?:[（(]\s*续\s*[）)]|[-—–－]\s*续)\s*$")


def _statement_stack_title(title: str) -> str:
    """Keep continuation pages under the base statutory statement title."""

    return _STATEMENT_CONTINUATION_RE.sub("", title).strip()


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
    re.compile(r"^([一二三四五六七八九十百]+)(?:、|\s+)"),
    re.compile(r"^[（(]([一二三四五六七八九十百]+)[）)]"),
    re.compile(r"^[（(](\d+)[）)]"),
    re.compile(r"^(\d+)[.、．)）\s]"),
)

_DECIMAL_OUTLINE_RE = re.compile(r"^(?P<number>\d{1,3}(?:[.．]\d{1,3}){1,3})(?=\s)")
_SINGLE_DOT_OUTLINE_RE = re.compile(r"^(?P<number>\d{1,3})[.．](?=\s)")
_LATIN_PAREN_OUTLINE_RE = re.compile(r"^\(([a-z])\)\s*", re.IGNORECASE)
_ROMAN_PAREN_OUTLINE_RE = re.compile(
    r"^\((i{1,3}|iv|v|vi{0,3}|ix|x|xi|xii)\)\s*", re.IGNORECASE
)
_DECIMAL_AMOUNT_TAIL_RE = re.compile(
    r"^(?:[%％]|(?:人民币)?(?:元|千元|万元|百万元|亿元)|"
    r"美元|港(?:币|元)|欧元|日元|英镑|个?百分点|倍|"
    r"吨|万吨|千克|公斤|平方米|万平方米|千瓦|兆瓦)"
)


def _decimal_outline_parts(text: str) -> tuple[int, ...] | None:
    """Parse a bounded N.N[.N] heading, never a decimal amount.

    Requiring whitespace after the outline keeps values such as ``1.5亿元``
    in body text while recognizing bank-report headings such as ``3.9.1 关于``.
    """

    match = _DECIMAL_OUTLINE_RE.match(text)
    if match is None:
        return None
    tail = text[match.end() :].lstrip()
    if _DECIMAL_AMOUNT_TAIL_RE.match(tail):
        return None
    return tuple(int(part) for part in re.split(r"[.．]", match.group("number")))


def _outline_parts_for_stack_title(text: str) -> tuple[int, ...] | None:
    decimal = _decimal_outline_parts(text)
    if decimal is not None:
        return decimal
    match = _SINGLE_DOT_OUTLINE_RE.match(text)
    if match is None:
        return None
    return (int(match.group("number")),)


def _single_dot_ordinal(text: str) -> int | None:
    match = _SINGLE_DOT_OUTLINE_RE.match(text)
    return int(match.group("number")) if match is not None else None


def _latin_parenthesized_ordinal(text: str) -> int | None:
    match = _LATIN_PAREN_OUTLINE_RE.match(text)
    if match is None:
        return None
    return ord(match.group(1).lower()) - ord("a") + 1


def _roman_parenthesized_ordinal(text: str) -> int | None:
    match = _ROMAN_PAREN_OUTLINE_RE.match(text)
    if match is None:
        return None
    token = match.group(1).lower()
    values = {"i": 1, "v": 5, "x": 10}
    total = 0
    previous = 0
    for char in reversed(token):
        value = values[char]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total


def _same_continuation_node(current: str, opened: str) -> bool:
    return _normalized_title(_statement_stack_title(current)) == _normalized_title(
        _statement_stack_title(opened)
    )


def _local_latin_sibling_level(
    stack: list[HeadingStackEntry], title: str
) -> int | None:
    """Confirm a local Latin family only from same-node or a→b continuity."""

    ordinal = _latin_parenthesized_ordinal(title)
    if ordinal is None:
        return None
    for level, open_title, _, _ in reversed(stack):
        open_ordinal = _latin_parenthesized_ordinal(open_title)
        if open_ordinal is None:
            continue
        if ordinal == open_ordinal and _same_continuation_node(title, open_title):
            return level
        if ordinal == open_ordinal + 1:
            return level
    return None


def _open_local_latin_parent(
    stack: list[HeadingStackEntry], *, before_level: int | None = None
) -> HeadingStackEntry | None:
    candidates = [
        entry
        for entry in stack
        if (before_level is None or entry[0] < before_level)
        and _latin_parenthesized_ordinal(entry[1]) is not None
    ]
    for entry in reversed(candidates):
        level, title, _, _ = entry
        # ``(i)`` is both Latin 9 and Roman 1.  Once another Latin node is
        # open below it, the deeper token is the Roman child, not a new parent.
        if _roman_parenthesized_ordinal(title) is not None and any(
            candidate[0] < level for candidate in candidates
        ):
            continue
        return entry
    return None


def _local_roman_level(stack: list[HeadingStackEntry], title: str) -> int | None:
    """Resolve Roman children only inside a locally proven Latin parent.

    ``(i)`` remains ambiguous globally.  It is Roman when a Latin parent is
    still open, except when the same token is the proven Latin successor of
    ``(h)`` (handled first by ``_local_latin_sibling_level``).
    """

    ordinal = _roman_parenthesized_ordinal(title)
    if ordinal is None:
        return None
    parent = _open_local_latin_parent(stack)
    if parent is None:
        return None
    parent_level = parent[0]
    for level, open_title, _, _ in reversed(stack):
        if level <= parent_level:
            break
        open_ordinal = _roman_parenthesized_ordinal(open_title)
        if open_ordinal is None:
            continue
        if ordinal == open_ordinal and _same_continuation_node(title, open_title):
            return level
        if ordinal == open_ordinal + 1:
            return level
    return parent_level + 1 if ordinal == 1 else None


def _numeric_under_local_roman_level(
    stack: list[HeadingStackEntry], title: str
) -> int | None:
    ordinal = _numeric_parenthesized_ordinal(title)
    if ordinal is None:
        return None
    for level, open_title, _, _ in reversed(stack):
        if _roman_parenthesized_ordinal(open_title) is None:
            continue
        if _open_local_latin_parent(stack, before_level=level) is not None:
            for child_level, child_title, _, _ in reversed(stack):
                if child_level <= level:
                    break
                open_ordinal = _numeric_parenthesized_ordinal(child_title)
                if open_ordinal is not None and ordinal in {
                    open_ordinal,
                    open_ordinal + 1,
                }:
                    return child_level
            return level + 1 if ordinal == 1 else None
    return None


def _numeric_parenthesized_ordinal(text: str) -> int | None:
    match = re.match(r"^[（(](\d{1,3})[）)]\s*", text)
    return int(match.group(1)) if match is not None else None


def _digit_close_ordinal(text: str) -> int | None:
    match = re.match(r"^\s*(\d{1,3})[)）]\s*", text)
    return int(match.group(1)) if match is not None else None


def _digit_close_under_exact_controlled_parent_level(
    stack: list[HeadingStackEntry], title: str
) -> int | None:
    """Nest ``1)``-style roles below an exact ``(N)`` controlled note.

    MinerU flattens both punctuation families to one source level.  They are
    not globally parent/child, so the repair is admitted only when the open
    full-parenthesis node has an exact controlled-note label.  This covers
    official note layouts such as ``(3) 关联租赁情况 > 1) 本公司作为出租方``
    while leaving contains-only or free-form headings untouched.  The first
    visible child need not start at one because some filings number table
    captions and role labels in one continuous local sequence.
    """

    ordinal = _digit_close_ordinal(title)
    if ordinal is None:
        return None
    for parent_index in range(len(stack) - 1, -1, -1):
        parent_level, parent_title, _, _ = stack[parent_index]
        if (
            _numeric_parenthesized_ordinal(parent_title) is None
            or rules.exact_note_key_for_title(parent_title) is None
        ):
            continue
        for child_level, child_title, _, _ in reversed(stack[parent_index + 1 :]):
            open_ordinal = _digit_close_ordinal(child_title)
            if (
                child_level > parent_level
                and open_ordinal is not None
                and ordinal in {open_ordinal, open_ordinal + 1}
            ):
                return child_level
        return parent_level + 1
    return None


def _remember_parenthesized_heading_sequence(
    sequences: list[_ParenthesizedHeadingSequence],
    *,
    element: PreparedElement,
    title: str,
    effective_level: int,
    parent_stack: list[HeadingStackEntry],
) -> None:
    """Remember only a source- and geometry-proven full-parenthesis run."""

    ordinal = _numeric_parenthesized_ordinal(title)
    left = _heading_left(element)
    source_level = element.heading_level
    if (
        element.kind != "heading"
        or ordinal is None
        or left is None
        or source_level is None
    ):
        return
    parent_signature = _stack_signature(parent_stack)
    if ordinal == 1:
        same_node = [
            sequence
            for sequence in sequences
            if sequence.ordinal == 1
            and sequence.effective_level == effective_level
            and _stack_signature(sequence.parent_stack) == parent_signature
            and sequence.source_level == source_level
            and abs(sequence.left - left) <= 12
            and _same_continuation_node(sequence.title, title)
            and 0 <= element.order_index - sequence.last_order <= 500
        ]
        if same_node:
            latest = max(same_node, key=lambda sequence: sequence.last_order)
            latest.left = left
            latest.last_order = element.order_index
            return
        sequences.append(
            _ParenthesizedHeadingSequence(
                ordinal=1,
                effective_level=effective_level,
                parent_stack=tuple(parent_stack),
                title=title,
                left=left,
                source_level=source_level,
                length=1,
                last_order=element.order_index,
            )
        )
        return

    predecessors = [
        sequence
        for sequence in sequences
        if sequence.ordinal == ordinal - 1
        and sequence.effective_level == effective_level
        and _stack_signature(sequence.parent_stack) == parent_signature
        and sequence.source_level == source_level
        and abs(sequence.left - left) <= 12
        and 0 <= element.order_index - sequence.last_order <= 500
    ]
    identities = {
        (
            sequence.effective_level,
            _stack_signature(sequence.parent_stack),
            sequence.source_level,
        )
        for sequence in predecessors
    }
    if not predecessors or len(identities) != 1:
        return
    latest = max(predecessors, key=lambda sequence: sequence.last_order)
    latest.ordinal = ordinal
    latest.title = title
    latest.left = left
    latest.length += 1
    latest.last_order = element.order_index


def _proven_parenthesized_heading_sequence(
    sequences: list[_ParenthesizedHeadingSequence],
    *,
    stack: list[HeadingStackEntry],
    element: PreparedElement,
    title: str,
) -> _ParenthesizedHeadingSequence | None:
    """Return a proven outer sibling hidden by an open ``1）`` child."""

    ordinal = _numeric_parenthesized_ordinal(title)
    left = _heading_left(element)
    source_level = element.heading_level
    if (
        element.kind != "heading"
        or ordinal is None
        or left is None
        or source_level is None
    ):
        return None
    current_signature = _stack_signature(stack)
    candidates = [
        sequence
        for sequence in sequences
        if sequence.length >= 2
        and ordinal == sequence.ordinal + 1
        and sequence.source_level == source_level
        and abs(sequence.left - left) <= 12
        and 0 <= element.order_index - sequence.last_order <= 500
        and current_signature[: len(_stack_signature(sequence.parent_stack))]
        == _stack_signature(sequence.parent_stack)
        and any(
            level == sequence.effective_level
            and _numeric_parenthesized_ordinal(open_title) == sequence.ordinal
            and _same_continuation_node(open_title, sequence.title)
            for level, open_title, _, _ in stack
        )
    ]
    identities = {
        (sequence.effective_level, _stack_signature(sequence.parent_stack))
        for sequence in candidates
    }
    if not candidates or len(identities) != 1:
        return None
    return max(candidates, key=lambda sequence: sequence.last_order)


def _digit_close_under_proven_parenthesized_sequence_level(
    sequences: list[_ParenthesizedHeadingSequence],
    *,
    stack: list[HeadingStackEntry],
    title: str,
    order_index: int,
    min_parent_order_gap: int = 0,
    max_parent_order_gap: int = 500,
) -> int | None:
    """Nest ``1）`` below a locally proven ``(1)``/``(2)`` outer run."""

    if _digit_close_ordinal(title) is None:
        return None
    for parent_index in range(len(stack) - 1, -1, -1):
        parent_level, parent_title, _, _ = stack[parent_index]
        parent_ordinal = _numeric_parenthesized_ordinal(parent_title)
        if parent_ordinal is None:
            continue
        parent_signature = _stack_signature(stack[:parent_index])
        candidates = [
            sequence
            for sequence in sequences
            if sequence.length >= 2
            and sequence.ordinal == parent_ordinal
            and sequence.effective_level == parent_level
            and _stack_signature(sequence.parent_stack) == parent_signature
            and _same_continuation_node(sequence.title, parent_title)
            and min_parent_order_gap
            <= order_index - sequence.last_order
            <= max_parent_order_gap
        ]
        identities = {
            (sequence.effective_level, _stack_signature(sequence.parent_stack))
            for sequence in candidates
        }
        if candidates and len(identities) == 1:
            return parent_level + 1
    return None


def _overlong_text_digit_close_sibling_level(
    sequences: list[_ParenthesizedHeadingSequence],
    *,
    stack: list[HeadingStackEntry],
    element: PreparedElement,
) -> int | None:
    """Recover one long ``n)`` leaf only inside a locally proven sequence.

    The general text-heading gate intentionally stops at 40 characters.  A
    small corpus family has genuine 41--80 character ``n)`` labels, however,
    and flattening them into the preceding ``n-1)`` leaf loses their narrower
    semantic key.  Admit only the exact local continuation: a proven outer
    ``(1)/(2)`` run, the immediately open ``n-1)`` sibling, matching left edge,
    a short parent-order gap, and a single visual line without sentence/KV
    punctuation.  Ordinary long numbered prose remains content.
    """

    title = (element.text or "").strip()
    bbox = _prepared_bbox(element)
    ordinal = _digit_close_ordinal(title)
    if (
        element.kind != "text"
        or element.raw_kind != "text"
        or element.heading_level is not None
        or ordinal is None
        or not 41 <= len(title) <= 80
        or "\n" in title
        or bbox is None
        or not 0 < bbox[3] - bbox[1] <= 32
        or title.endswith(("。", "；", "，", ",", "：", ":", "？", "?", "！", "!"))
        or any(marker in title for marker in ("：", ":"))
        or rules.is_declaration_line(title)
        or rules.is_standalone_noise(title)
        or rules.FOOTNOTE_LINE_RE.match(title)
        or not stack
    ):
        return None
    level = _digit_close_under_proven_parenthesized_sequence_level(
        sequences,
        stack=stack,
        title=title,
        order_index=element.order_index,
        min_parent_order_gap=1,
        max_parent_order_gap=12,
    )
    if level is None:
        return None
    sibling_level, sibling_title, _, sibling_left = stack[-1]
    sibling_ordinal = _digit_close_ordinal(sibling_title)
    if (
        sibling_level != level
        or sibling_ordinal is None
        or ordinal != sibling_ordinal + 1
        or sibling_left is None
        or abs(bbox[0] - sibling_left) > 8
    ):
        return None
    return level


def _open_local_roman_parent(
    stack: list[HeadingStackEntry],
) -> HeadingStackEntry | None:
    for entry in reversed(stack):
        level, title, _, _ = entry
        if (
            _roman_parenthesized_ordinal(title) is not None
            and _open_local_latin_parent(stack, before_level=level) is not None
        ):
            return entry
    return None


def _closed_local_numeric_sequence(
    sequences: list[_LocalNumericSequence],
    *,
    stack: list[HeadingStackEntry],
    title: str,
    order_index: int,
) -> _LocalNumericSequence | None:
    """Recover a numeric sibling only below the same proven Roman parent."""

    ordinal = _numeric_parenthesized_ordinal(title)
    parent = _open_local_roman_parent(stack)
    if ordinal is None or parent is None:
        return None
    current_signature = _stack_signature(stack)
    candidates = [
        sequence
        for sequence in sequences
        if current_signature[: len(_stack_signature(sequence.parent_stack))]
        == _stack_signature(sequence.parent_stack)
        and sequence.parent_stack
        and _same_continuation_node(parent[1], sequence.parent_stack[-1][1])
        and (
            ordinal == sequence.ordinal + 1
            or (
                ordinal == sequence.ordinal
                and _same_continuation_node(title, sequence.title)
            )
        )
        and 0 <= order_index - sequence.last_order <= 500
    ]
    if not candidates:
        return None
    identities = {
        (sequence.effective_level, _stack_signature(sequence.parent_stack))
        for sequence in candidates
    }
    if len(identities) != 1:
        return None
    return max(candidates, key=lambda sequence: sequence.last_order)


def _remember_local_numeric_sequence(
    sequences: list[_LocalNumericSequence],
    *,
    title: str,
    effective_level: int,
    parent_stack: list[HeadingStackEntry],
    order_index: int,
    reopened: _LocalNumericSequence | None,
) -> None:
    ordinal = _numeric_parenthesized_ordinal(title)
    if ordinal is None:
        return
    if reopened is not None:
        reopened.ordinal = ordinal
        reopened.title = title
        reopened.last_order = order_index
        return
    parent_signature = _stack_signature(parent_stack)
    continuations = [
        sequence
        for sequence in sequences
        if sequence.effective_level == effective_level
        and _stack_signature(sequence.parent_stack) == parent_signature
        and (
            ordinal == sequence.ordinal + 1
            or (
                ordinal == sequence.ordinal
                and _same_continuation_node(title, sequence.title)
            )
        )
    ]
    if continuations:
        latest = max(continuations, key=lambda sequence: sequence.last_order)
        latest.ordinal = ordinal
        latest.title = title
        latest.last_order = order_index
        return
    sequences.append(
        _LocalNumericSequence(
            ordinal=ordinal,
            effective_level=effective_level,
            parent_stack=tuple(parent_stack),
            title=title,
            last_order=order_index,
        )
    )


def _closed_local_roman_sequence(
    sequences: list[_LocalRomanSequence],
    *,
    stack: list[HeadingStackEntry],
    title: str,
    left: float | None,
    order_index: int,
) -> _LocalRomanSequence | None:
    """Recover a Roman sibling only below the same remembered Latin parent."""

    ordinal = _roman_parenthesized_ordinal(title)
    parent = _open_local_latin_parent(stack)
    if ordinal is None or parent is None:
        return None
    current_signature = _stack_signature(stack)
    candidates: list[_LocalRomanSequence] = []
    for sequence in sequences:
        parent_signature = _stack_signature(sequence.parent_stack)
        same_parent = (
            current_signature[: len(parent_signature)] == parent_signature
            and sequence.parent_stack
            and _same_continuation_node(parent[1], sequence.parent_stack[-1][1])
        )
        same_or_next = ordinal == sequence.ordinal + 1 or (
            ordinal == sequence.ordinal
            and _same_continuation_node(title, sequence.title)
        )
        same_geometry = (
            left is None or sequence.left is None or abs(left - sequence.left) <= 12
        )
        if (
            same_parent
            and same_or_next
            and same_geometry
            and 0 <= order_index - sequence.last_order <= 500
        ):
            candidates.append(sequence)
    if not candidates:
        return None
    identities = {
        (sequence.effective_level, _stack_signature(sequence.parent_stack))
        for sequence in candidates
    }
    if len(identities) != 1:
        return None
    return max(candidates, key=lambda sequence: sequence.last_order)


def _remember_local_roman_sequence(
    sequences: list[_LocalRomanSequence],
    *,
    title: str,
    effective_level: int,
    parent_stack: list[HeadingStackEntry],
    left: float | None,
    order_index: int,
    reopened: _LocalRomanSequence | None,
) -> None:
    ordinal = _roman_parenthesized_ordinal(title)
    if ordinal is None:
        return
    if reopened is not None:
        reopened.ordinal = ordinal
        reopened.title = title
        reopened.left = left
        reopened.last_order = order_index
        return
    parent_signature = _stack_signature(parent_stack)
    continuations = [
        sequence
        for sequence in sequences
        if sequence.effective_level == effective_level
        and _stack_signature(sequence.parent_stack) == parent_signature
        and (
            ordinal == sequence.ordinal + 1
            or (
                ordinal == sequence.ordinal
                and _same_continuation_node(title, sequence.title)
            )
        )
    ]
    if continuations:
        latest = max(continuations, key=lambda sequence: sequence.last_order)
        latest.ordinal = ordinal
        latest.title = title
        latest.left = left
        latest.last_order = order_index
        return
    sequences.append(
        _LocalRomanSequence(
            ordinal=ordinal,
            effective_level=effective_level,
            parent_stack=tuple(parent_stack),
            title=title,
            left=left,
            last_order=order_index,
        )
    )


def _stack_signature(
    stack: Iterable[HeadingStackEntry],
) -> tuple[tuple[int, str], ...]:
    return tuple((level, _normalized_title(title)) for level, title, _, _ in stack)


def _closed_single_dot_sequence(
    sequences: list[_SingleDotSequence],
    *,
    stack: list[HeadingStackEntry],
    title: str,
    left: float | None,
) -> _SingleDotSequence | None:
    """Return a proven closed ``N.`` sibling run, never a guessed outline.

    Active-stack continuity remains authoritative.  Reopening is allowed only
    when at least three prior consecutive siblings established the sequence
    and the current heading has the same left edge (within normal PDF jitter).
    This recovers long top-level bank chapter runs without generic outdent
    promotion, which is unsafe for centered report roots.
    """

    ordinal = _single_dot_ordinal(title)
    if (
        ordinal is None
        or ordinal <= 1
        or left is None
        or not _is_controlled_boundary_title(title)
    ):
        return None
    for _, open_title, _, _ in reversed(stack):
        if _single_dot_ordinal(open_title) in {ordinal - 1, ordinal}:
            # Active structure is authoritative even when a page boundary
            # changes its left margin. The predecessor is normal continuity;
            # the same ordinal is a continuation page. Neither may be
            # displaced by an older document-local history snapshot.
            return None
    current_signature = _stack_signature(stack)
    candidates = [
        sequence
        for sequence in sequences
        if sequence.ordinal == ordinal - 1
        and sequence.length >= 3
        and abs(left - sequence.left) <= 12
        and current_signature[: len(_stack_signature(sequence.parent_stack))]
        == _stack_signature(sequence.parent_stack)
    ]
    if not candidates:
        return None
    identities = {
        (
            sequence.effective_level,
            _stack_signature(sequence.parent_stack),
        )
        for sequence in candidates
    }
    if len(identities) != 1:
        # The same ordinal can occur in several note subtrees. Never guess
        # which closed run owns a later controlled title.
        return None
    return max(candidates, key=lambda sequence: sequence.last_order)


def _remember_single_dot_sequence(
    sequences: list[_SingleDotSequence],
    *,
    title: str,
    effective_level: int,
    parent_stack: list[HeadingStackEntry],
    left: float | None,
    order_index: int,
    reopened: _SingleDotSequence | None,
) -> None:
    ordinal = _single_dot_ordinal(title)
    if ordinal is None or left is None:
        return
    parent_signature = _stack_signature(parent_stack)
    if reopened is not None:
        reopened.ordinal = ordinal
        reopened.left = left
        reopened.length += 1
        reopened.last_order = order_index
        return
    same_node = [
        sequence
        for sequence in sequences
        if sequence.ordinal == ordinal
        and sequence.effective_level == effective_level
        and _stack_signature(sequence.parent_stack) == parent_signature
        and abs(left - sequence.left) <= 12
    ]
    if same_node:
        latest = max(same_node, key=lambda sequence: sequence.last_order)
        latest.left = left
        latest.last_order = order_index
        return
    predecessors = [
        sequence
        for sequence in sequences
        if sequence.ordinal == ordinal - 1
        and sequence.effective_level == effective_level
        and _stack_signature(sequence.parent_stack) == parent_signature
        and abs(left - sequence.left) <= 12
    ]
    if predecessors:
        latest = max(predecessors, key=lambda sequence: sequence.last_order)
        latest.ordinal = ordinal
        latest.left = left
        latest.length += 1
        latest.last_order = order_index
        return
    sequences.append(
        _SingleDotSequence(
            ordinal=ordinal,
            effective_level=effective_level,
            parent_stack=tuple(parent_stack),
            left=left,
            length=1,
            last_order=order_index,
        )
    )


def _heading_ordinal(text: str) -> int | None:
    decimal_outline = _decimal_outline_parts(text)
    if decimal_outline is not None:
        return decimal_outline[-1]
    for pattern in _ORDINAL_RES:
        match = pattern.match(text)
        if match:
            token = match.group(1)
            return int(token) if token.isdigit() else _cn_ordinal(token)
    return None


def _pattern_heading_level(text: str) -> int | None:
    decimal_outline = _decimal_outline_parts(text)
    if decimal_outline is not None:
        return len(decimal_outline)
    for level, pattern in rules.HEADING_PATTERNS:
        if pattern.match(text):
            return level
    return None


def _repair_decimal_outline_level(
    stack: list[HeadingStackEntry],
    level: int,
    outline: tuple[int, ...],
    *,
    left: float | None,
) -> int:
    """Place a decimal outline relative to its open sibling or parent.

    ``8.1`` can sit below an open single-dot ``8. 讨论与分析`` whose global
    Chinese-report pattern level is 5, while a standalone bank ``3.8`` below
    an unnumbered section root uses its natural outline depth (2). Matching
    the numeric prefix makes both layouts document-local and deterministic.
    """

    for effective_level, title, _, _ in reversed(stack):
        open_outline = _outline_parts_for_stack_title(title)
        if open_outline is None:
            continue
        if len(open_outline) == len(outline) and open_outline[:-1] == outline[:-1]:
            return effective_level
        if len(open_outline) + 1 == len(outline) and outline[:-1] == open_outline:
            return effective_level + 1

    # Annual-report summaries commonly use a Chinese chapter number followed
    # by decimal children (``二、公司基本情况 > 2.1 公司简介``).
    # MinerU flattens both to heading_level=1 and the global pattern table puts
    # both at effective level 2, so the decimal child used to evict its proven
    # chapter parent.  Matching the decimal's first component to the open
    # Chinese ordinal is bounded document-local evidence; a mismatched outline
    # keeps the existing behavior.
    if len(outline) >= 2:
        for effective_level, title, open_ordinal, _ in reversed(stack):
            if (
                _pattern_heading_level(title) == 2
                and open_ordinal == outline[0]
            ):
                return effective_level + len(outline) - 1

    # ``28、其他应付款 > 28.1/28.2`` uses a dot outline as a child of the
    # comma-numbered note. Natural outline depth (2) used to lift it above the
    # surrounding ``(五) 合并财务报表项目注释`` root and poison every later
    # major note with ``other_payables``. Numeric identity is stronger than
    # the globally assigned punctuation level.
    if len(outline) >= 2:
        for effective_level, title, open_ordinal, _ in reversed(stack):
            if re.match(r"^\s*\d{1,3}\s*、", title) and open_ordinal == outline[0]:
                return effective_level + 1

    # A first decimal outline inside an indented, controlled major note must
    # remain below that note. Standalone bank-report outlines have no such
    # numbered controlled ancestor and retain their natural levels.
    if left is not None:
        for effective_level, title, _, title_left in reversed(stack):
            if (
                _pattern_heading_level(title) in {2, 3}
                and rules.exact_note_key_for_title(title) is not None
                and title_left is not None
                and left >= title_left + 24
            ):
                return max(level, effective_level + 1)
    return level


def _repair_level_by_continuity(
    stack: list[HeadingStackEntry],
    level: int,
    ordinal: int | None,
    pattern_level: int | None,
) -> int:
    """Re-level a numbered heading whose ordinal breaks its own sequence.

    Real filings misnumber: the 江海 annual prints 三、（市场风险） where
    （三）市场风险 was meant, and the L2-style prefix used to evict the open
    十二、金融工具风险 section. If the ordinal does not continue the open
    sequence at its pattern level but exactly continues another OPEN level's
    sequence, the heading belongs there. Ordinal 1 always starts a fresh
    sequence at its own level. Cross-family repair is DEMOTION-ONLY: a heading
    may sink into a deeper open sequence, never rise above its pattern level —
    the 附注科目 chain (9、…44、) must not latch onto 第八节's ordinal 8 at
    level 1. An exact continuation in the *same numbering family* is stronger:
    it returns to the open sibling's effective level even when a controlled
    heading previously promoted that family. Otherwise ``十、`` promoted to a
    root leaves ``十一、`` nested below it, and ``9.`` followed by ``10.``
    leaves later bank-report chapters below a stale hotspot subsection.
    """

    if ordinal is None or ordinal <= 1:
        return level
    # Effective levels can be shifted by document-local indentation. Match a
    # continuing sequence by its original numbering family first; otherwise
    # a demoted ``1、`` at effective level 6 is confused with its ``(1)``
    # child, which also has nominal level 6.
    if pattern_level is not None:
        for effective_level, title, open_ordinal, _ in reversed(stack):
            if (
                _pattern_heading_level(title) == pattern_level
                and open_ordinal is not None
                and ordinal == open_ordinal + 1
            ):
                return effective_level
    # Preserve the older cross-family recovery for malformed prefixes such as
    # ``三、`` continuing an open ``（二）`` sequence. It may only demote.
    for lvl, _, ord_, _ in reversed(stack):
        if lvl > level and ord_ is not None and ordinal == ord_ + 1:
            return lvl
    return level


def _repair_level_by_open_parent_pattern(
    stack: list[HeadingStackEntry],
    level: int,
    ordinal: int | None,
    pattern_level: int | None,
) -> int:
    """Keep a nominal child below a document-locally demoted parent.

    When indentation moves ``1、`` from nominal level 4 to effective level 6,
    its ``(1)`` child (nominal level 6) must move below it rather than replace
    it at the same effective level. The first visible child need not be ordinal
    1 because a long ``(1)`` line may correctly remain body text. A confirmed
    continuation of an older same-family sequence wins over the open parent,
    so ``11.`` can still close ``10.`` after the latter's ``7、`` child.
    """

    if pattern_level is None or not stack:
        return level
    if ordinal is not None and any(
        effective_level == level
        and _pattern_heading_level(title) == pattern_level
        and open_ordinal is not None
        and ordinal == open_ordinal + 1
        for effective_level, title, open_ordinal, _ in stack
    ):
        return level
    parent_level, parent_title, _, _ = stack[-1]
    parent_pattern_level = _pattern_heading_level(parent_title)
    if (
        parent_pattern_level is not None
        and pattern_level > parent_pattern_level
        and level <= parent_level
    ):
        return parent_level + 1
    return level


def _repair_level_by_indentation(
    stack: list[HeadingStackEntry],
    level: int,
    ordinal: int | None,
    pattern_level: int | None,
    left: float | None,
) -> int:
    """Use a clear indent as a document-local hierarchy signal.

    Numbering styles are not globally stable: one issuer uses ``6.`` for a
    policy and indented ``1.`` for its child, while another uses dot-numbered
    children at the same left edge. Geometry may demote any clearly indented
    heading because a source can omit the first child and begin at ``2、``.
    Later siblings follow the opened sequence through ordinal continuity.
    Generic outdent repair is deliberately forbidden: centered chapter titles
    and ordinary layout drift otherwise collapse whole reports into the wrong
    branch.
    """

    if left is None or not stack:
        return level
    # A consecutive heading in the same numbering family is a stronger
    # sibling signal than page geometry. Real reports can change the left
    # margin at a page boundary (1217717242: 三、 x=83 then 四、 x=146).
    if (
        ordinal is not None
        and pattern_level is not None
        and any(
            _pattern_heading_level(title) == pattern_level
            and open_ordinal is not None
            and ordinal == open_ordinal + 1
            for _, title, open_ordinal, _ in stack
        )
    ):
        return level
    last_left = stack[-1][3]
    if last_left is None:
        return level
    if left >= last_left + 24:
        return max(level, stack[-1][0] + 1)
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
            quality = (
                "needs_review"
                if any(item.quality_status == "needs_review" for item in buffer)
                else "ok"
            )
            units.append(
                UnitDraft(
                    payload_kind="text",
                    payload={"text": text},
                    source_order=buffer[0].order_index,
                    heading_path=list(buffer[0].heading_path),
                    structural_path=list(buffer[0].structural_path),
                    title=buffer[0].title,
                    quality_status=quality,
                    applicability=applicability,
                    artifact_locator=buffer[0].artifact_locator,
                    qa_question_boundaries=[
                        item.text or ""
                        for item in buffer
                        if item.qa_question_boundary and item.text
                    ],
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
                    structural_path=list(buffer[0].structural_path),
                    title=buffer[0].title,
                    applicability="applicable",
                )
            )
        buffer.clear()

    for element in elements:
        if element.kind == "text" and element.payload is None:
            if buffer and (
                element.qa_question_boundary
                or element.heading_path != buffer[-1].heading_path
                or element.structural_path != buffer[-1].structural_path
                or element.title != buffer[-1].title
            ):
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
                        structural_path=list(element.structural_path),
                        title=element.title,
                        quality_status=element.quality_status,
                        artifact_locator=element.artifact_locator,
                    )
                )
    flush()
    return units


def s4_build_qa_units(
    text: str,
    *,
    source: UnitDraft,
    require_explicit_answer: bool = False,
) -> QaParseResult:
    lines = _qa_lines(text)
    native_source = (source.artifact_locator or {}).get("source") == "native_text"
    bracket_transcript = any(
        rules.BRACKET_QUESTION_START_RE.match(line.strip()) for line in lines
    )

    def answer_start(line: str) -> re.Match[str] | None:
        explicit = rules.ANSWER_START_RE.match(line)
        if explicit is not None:
            return explicit
        if bracket_transcript:
            return rules.BRACKET_SPEAKER_ANSWER_RE.match(line)
        return None

    strong_heading_boundaries = {
        _comparison_text(value) for value in source.qa_question_boundaries if value
    }
    allow_numbered_question = any(answer_start(line.strip()) for line in lines)
    if require_explicit_answer and not allow_numbered_question:
        # Periodic and ordinary announcement prose contains many numbered
        # sentences phrased as questions (audit duties, declarations, form
        # fields). Without an explicit answer marker they are not a proven QA
        # transcript and must remain ordinary evidence.
        return QaParseResult(units=[])
    current_question_lines: list[str] = []
    current_ordinal: int | None = None
    current_unlabelled_response_proven = False
    answer_lines: list[str] = []
    raw_lines: list[str] = []
    seen_answer = False
    units: list[UnitDraft] = []
    ordinals: list[int] = []
    unstable = False
    leading_lines: list[str] = []
    leading_needs_review = False
    review_spans: list[tuple[int, str]] = []
    recovery_lines: list[str] | None = None
    trailing_text: str | None = None

    def next_line_is_answer(index: int) -> bool:
        return next(
            (
                bool(answer_start(candidate.strip()))
                for candidate in lines[index + 1 :]
                if candidate.strip()
            ),
            False,
        )

    def next_line_is_unlabelled_company_response(index: int) -> bool:
        for candidate in lines[index + 1 :]:
            following = candidate.strip()
            if not following:
                continue
            # The response cue must be the immediately following logical
            # line. Looking through another numbered/question boundary turns
            # answer-side 1/2/3 lists into fabricated outer questions.
            if (
                _qa_numbered_line(following)
                or rules.QUESTION_START_RE.match(following)
                or rules.EXPLICIT_QUESTION_START_RE.match(following)
            ):
                return False
            return bool(rules.UNLABELLED_COMPANY_RESPONSE_START_RE.match(following))
        return False

    def answer_before_next_outer_question(
        index: int, candidate_ordinal: int | None
    ) -> bool:
        """Prove that a numbered line owns a later explicit answer.

        A long outer question may contain numbered subquestions (Q10 followed
        by 1/2/3/4), so smaller ordinals do not close the candidate.  A same
        or larger ordinal before ``答`` does: this is the key distinction from
        answer-side numbered lists that happen to begin with the next number.
        """

        for candidate in lines[index + 1 :]:
            following = candidate.strip()
            if not following:
                continue
            if answer_start(following):
                return True
            if rules.EXPLICIT_QUESTION_START_RE.match(
                following
            ) and _strip_question_prefix(following):
                return False
            following_ordinal = _qa_ordinal(following)
            if (
                following_ordinal is not None
                and candidate_ordinal is not None
                and following_ordinal >= candidate_ordinal
            ):
                return False
        return False

    def answer_before_next_numbered(index: int) -> bool:
        """Strict resync gate after an orphan answer.

        Unlike the compound-question lookahead above, every intervening
        numbered line closes this candidate.  This prevents answer subpoint 3
        from looking through the real outer Q2 and stealing Q2's answer.
        """

        for candidate in lines[index + 1 :]:
            following = candidate.strip()
            if not following:
                continue
            if answer_start(following):
                return True
            if (
                (
                    rules.QUESTION_START_RE.match(following)
                    or rules.EXPLICIT_QUESTION_START_RE.match(following)
                )
                and _strip_question_prefix(following)
            ) or _qa_numbered_line(following):
                return False
        return False

    def proven_resync_question(index: int, stripped: str) -> bool:
        """Require a strong outer question with its own explicit answer."""

        if not _strip_question_prefix(stripped):
            return False
        strong_heading_boundary = _comparison_text(
            stripped
        ) in strong_heading_boundaries and _qa_numbered_line(stripped)
        strong_question_shape = bool(
            rules.EXPLICIT_QUESTION_START_RE.match(stripped)
            or rules.QUESTION_START_RE.match(stripped)
            or rules.QA_COMPOUND_QUESTION_INTRO_RE.match(stripped)
            or _QA_YEAR_PREFIXED_ORDINAL_RE.match(stripped)
            or strong_heading_boundary
        )
        return strong_question_shape and answer_before_next_numbered(index)

    def quarantine_before_next_qa(values: list[str]) -> None:
        text = "\n".join(value for value in values if value.strip()).strip()
        if not text:
            return
        before_index = len(units)
        if review_spans and review_spans[-1][0] == before_index:
            review_spans[-1] = (
                before_index,
                "\n".join([review_spans[-1][1], text]),
            )
        else:
            review_spans.append((before_index, text))

    def starts_question(index: int, stripped: str) -> bool:
        if rules.EXPLICIT_QUESTION_START_RE.match(stripped):
            # A labelled Q boundary is stronger than same-carrier answer
            # lookahead.  When a page/table seam strands Q5 without its
            # answer, it must close Q4 and survive as review text rather than
            # being silently appended to Q4's answer.
            return bool(_strip_question_prefix(stripped))
        strong_heading_boundary = _comparison_text(
            stripped
        ) in strong_heading_boundaries and _qa_numbered_line(stripped)
        if strong_heading_boundary:
            if not _strip_question_prefix(stripped):
                return False
            if current_question_lines:
                return seen_answer or bool(
                    answer_lines
                    and not native_source
                    and not require_explicit_answer
                    and not allow_numbered_question
                )
            # In relaxed IR/performance-briefing mode the physical MinerU
            # heading is itself the boundary proof. Such transcripts often
            # use an unlabelled company-response paragraph, so requiring a
            # later 答 marker would defeat the provenance we preserved.
            if not native_source and not require_explicit_answer:
                return True
            # In strict logical-run parsing the heading flag is additional
            # provenance, not a veto. Fall through so an explicit-answer
            # candidate can still be proven by the ordinary numbered-Q gate.
        matches_question_pattern = bool(
            rules.QUESTION_START_RE.match(stripped)
            or rules.QA_COMPOUND_QUESTION_INTRO_RE.match(stripped)
            or _QA_YEAR_PREFIXED_ORDINAL_RE.match(stripped)
        )
        candidate_ordinal = _qa_ordinal(stripped)
        if candidate_ordinal is None:
            return matches_question_pattern
        if not allow_numbered_question:
            # Legacy unlabelled-answer transcripts still use punctuation and
            # request cues.  Their ambiguity is contained to relaxed filing
            # types; periodic/ordinary filings set require_explicit_answer.
            if matches_question_pattern:
                return True
            return bool(
                current_question_lines
                and current_ordinal is not None
                and candidate_ordinal == current_ordinal + 1
                and answer_lines
                and not native_source
                and not require_explicit_answer
                and next_line_is_unlabelled_company_response(index)
            )
        if current_question_lines and not seen_answer:
            # Before the first explicit answer, numbered lines belong to one
            # compound investor question (observed 1/2/3 subquestions and
            # Q10's four recommendations), not four fabricated QA pairs.
            return False
        if current_question_lines and current_ordinal is not None:
            if candidate_ordinal == current_ordinal + 1:
                return answer_before_next_outer_question(index, candidate_ordinal)
            # Documents occasionally jump from Q2 to Q10.  Keep that valid
            # shape only when a strong question pattern is immediately proven
            # by an answer; do not promote a long answer-list fragment.
            return matches_question_pattern and next_line_is_answer(index)
        if (
            leading_lines
            and not leading_needs_review
            and candidate_ordinal > 1
            and any(mark in "\n".join(leading_lines) for mark in ("?", "？"))
        ):
            # The MinerU table→text seam can strand the tail of compound Q1
            # before a numbered subquestion (observed Q1's third subquestion
            # arriving as ``3. ...?``).  Do not fabricate Q3 from that tail.
            # Its following orphan 答 marks the prefix damaged, after which the
            # real outer Q2 is allowed to resynchronize normally.
            return False
        if leading_needs_review:
            # After an orphan 答, bare numbered lines are overwhelmingly its
            # answer subpoints.  Resynchronize only at a strong interrogative /
            # request-shaped outer question whose own answer arrives before
            # *any* further numbered candidate, never at ``3、依法披露。``.
            return matches_question_pattern and answer_before_next_numbered(index)
        if matches_question_pattern:
            return True
        if (
            require_explicit_answer
            or not allow_numbered_question
            or not _qa_numbered_line(stripped)
        ):
            return False
        topic = _strip_question_prefix(stripped)
        if not topic or len(topic) > 160:
            return False
        return answer_before_next_outer_question(index, candidate_ordinal)

    def relaxed_question_is_complete() -> bool:
        """Tell hard-wrapped questions from their unlabelled answers."""

        question = _join_wrapped_lines(current_question_lines)
        if not question:
            return False
        if current_unlabelled_response_proven:
            return True
        if _comparison_text(question) in {
            _comparison_text(_strip_question_prefix(boundary))
            for boundary in source.qa_question_boundaries
            if boundary
        }:
            # A MinerU heading is already a physical question boundary.  It
            # need not end in punctuation (``5、海外业务布局``); the following
            # carrier is its unlabelled company response, not another wrapped
            # question line.
            return True
        # An earlier sub-question mark does not close a line that ends in a
        # torn word (observed ``...领先地位？韩国一家企业公`` at a page seam).
        if re.search(
            r"[？?](?:\s*(?:谢谢|感谢(?:管理层|公司)?)[。.!！]?)?\s*$",
            question,
        ):
            return True
        return bool(
            re.search(r"[。；;！!]\s*$", question)
            and re.search(
                r"(?:请|能否|可否|如何|怎样|怎么样|什么|多少|是否|为何|"
                r"为什么|吗|呢|介绍|说明|回复|回答)",
                question,
            )
        )

    def standalone_ask_marker(index: int, stripped: str) -> bool:
        """Drop split ``提``/``问``/``问题`` only before a proven outer Q."""

        if stripped not in {"提", "问", "问题"}:
            return False
        before = next(
            (
                candidate.strip()
                for candidate in reversed(lines[:index])
                if candidate.strip()
            ),
            "",
        )
        after = [
            candidate.strip() for candidate in lines[index + 1 :] if candidate.strip()
        ]

        def outer_question(value: str) -> bool:
            return bool(
                rules.EXPLICIT_QUESTION_START_RE.match(value)
                or rules.QUESTION_START_RE.match(value)
                or rules.QA_COMPOUND_QUESTION_INTRO_RE.match(value)
                or _QA_YEAR_PREFIXED_ORDINAL_RE.match(value)
                or re.match(r"^\d{1,3}[：:]\s*【", value)
            )

        if stripped == "问题":
            # ``问题 3：...`` is occasionally split by the numeric-colon
            # tokenizer into a naked label plus the intact outer question.
            # Do not change that tokenizer's established carrier selection:
            # dropping only this exact immediately-followed fragment removed
            # 20 polluted answers without losing Q1/Q2 in three complex real
            # transcripts.
            return bool(after and outer_question(after[0]))
        if stripped == "提":
            return bool(
                after
                and (
                    outer_question(after[0])
                    or (
                        after[0] == "问" and len(after) > 1 and outer_question(after[1])
                    )
                )
            )
        return before == "提" and bool(after and outer_question(after[0]))

    def emit(*, final: bool = False) -> None:
        nonlocal current_question_lines, current_ordinal
        nonlocal answer_lines, raw_lines, seen_answer, unstable, trailing_text
        nonlocal current_unlabelled_response_proven
        if not current_question_lines:
            return
        question = _join_wrapped_lines(current_question_lines)
        answer = (
            _join_wrapped_lines(answer_lines)
            if native_source
            else "\n".join(line for line in answer_lines if line.strip()).strip()
        )
        if (
            not question
            or not answer
            or (
                (native_source or require_explicit_answer or allow_numbered_question)
                and not seen_answer
            )
        ):
            if final and units:
                # A page/table boundary can truncate only the last question.
                # Keep every complete QA before it and preserve the tail for
                # review; an incomplete first/only question still fails the
                # whole candidate closed.
                trailing_text = "\n".join(raw_lines).strip() or question or None
                current_question_lines = []
                current_ordinal = None
                current_unlabelled_response_proven = False
                answer_lines = []
                raw_lines = []
                seen_answer = False
                return
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
                structural_path=list(source.structural_path),
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
        current_unlabelled_response_proven = False
        answer_lines = []
        raw_lines = []
        seen_answer = False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if recovery_lines is not None:
            # A second answer marker proves that the current question/answer
            # pairing is corrupt.  Preserve every line until a later outer
            # question proves its *own* answer before any intervening numbered
            # boundary; ordinary question detection is intentionally bypassed
            # while this quarantine state is active.
            if proven_resync_question(index, stripped):
                quarantine_before_next_qa(recovery_lines)
                recovery_lines = None
                current_question_lines = [_strip_question_prefix(stripped)]
                current_ordinal = _qa_ordinal(stripped)
                current_unlabelled_response_proven = False
                raw_lines = [stripped]
                answer_lines = []
                seen_answer = False
            else:
                recovery_lines.append(stripped)
            continue
        if standalone_ask_marker(index, stripped):
            continue
        if starts_question(index, stripped):
            if current_question_lines:
                emit()
                if unstable:
                    if units:
                        # A malformed later pair must not erase earlier QA
                        # that were already independently proven. Preserve the
                        # damaged suffix verbatim for review and stop here.
                        trailing_text = "\n".join(
                            [
                                *raw_lines,
                                *(
                                    item.strip()
                                    for item in lines[index:]
                                    if item.strip()
                                ),
                            ]
                        ).strip()
                        current_question_lines = []
                        current_ordinal = None
                        answer_lines = []
                        raw_lines = []
                        seen_answer = False
                        unstable = False
                    break
            current_question_lines = [_strip_question_prefix(stripped)]
            current_ordinal = _qa_ordinal(stripped)
            current_unlabelled_response_proven = bool(
                not native_source
                and not require_explicit_answer
                and next_line_is_unlabelled_company_response(index)
            )
            raw_lines = [stripped]
            answer_lines = []
            seen_answer = False
            continue
        answer_match = answer_start(stripped)
        if answer_match is not None:
            if not current_question_lines:
                # A page/table boundary can leave the tail of the preceding
                # answer at the start of this text slice. Preserve that damaged
                # prefix for review and resynchronize at the next proven Q+A,
                # instead of hiding every complete question that follows.
                leading_lines.append(stripped)
                leading_needs_review = True
                continue
            bracket_speaker = rules.BRACKET_SPEAKER_ANSWER_RE.match(stripped)
            if seen_answer and bracket_speaker is not None:
                # One compound question can be answered by several named
                # managers. In a proven bracket transcript this is answer
                # continuation, not a duplicate anonymous answer marker.
                raw_lines.append(stripped)
                answer_lines.append(stripped)
                continue
            if seen_answer:
                # Do not emit the current pair: the duplicate marker means a
                # question boundary was missed somewhere inside it.  Enter a
                # quarantine state and keep scanning for a later independently
                # proven Q+A instead of discarding the whole remaining block.
                recovery_lines = [*raw_lines, stripped]
                current_question_lines = []
                current_ordinal = None
                current_unlabelled_response_proven = False
                answer_lines = []
                raw_lines = []
                seen_answer = False
                continue
            seen_answer = True
            raw_lines.append(stripped)
            answer_lines.append(
                stripped
                if bracket_speaker is not None
                else _strip_answer_prefix(stripped)
            )
            continue
        if current_question_lines:
            raw_lines.append(stripped)
            if seen_answer:
                answer_lines.append(stripped)
            elif native_source or require_explicit_answer or allow_numbered_question:
                # Native PDF text preserves hard line wraps.  Until the first
                # 答/回复 marker, every wrapped/nested line is still part of
                # the same compound question.  In an explicitly marked block,
                # treating a pre-answer subquestion as an unlabelled answer is
                # the exact false-pair failure this state machine prevents.
                current_question_lines.append(stripped)
            elif not relaxed_question_is_complete():
                # Relaxed IR transcripts still wrap one question over
                # physical lines/pages.  Wait for a terminal boundary before
                # treating the next unlabelled line as the company answer.
                current_question_lines.append(stripped)
            else:
                # Existing MinerU contract: a line after an explicit question
                # may be an unlabelled answer.
                answer_lines.append(stripped)
        else:
            leading_lines.append(stripped)

    if recovery_lines is not None:
        recovery_text = "\n".join(recovery_lines).strip()
        if units:
            trailing_text = recovery_text or None
        else:
            unstable = True
    if not unstable:
        emit(final=True)
    if leading_needs_review and not units:
        unstable = True
    if unstable:
        return QaParseResult(units=[], unstable=True, ordinals=[])
    leading_text = "\n".join(leading_lines).strip() or None
    return QaParseResult(
        units=units,
        unstable=False,
        ordinals=ordinals,
        leading_text=leading_text,
        leading_needs_review=leading_needs_review,
        review_spans=review_spans,
        trailing_text=trailing_text,
    )


def replace_text_units_with_qa_where_stable(
    units: Iterable[UnitDraft],
    *,
    require_explicit_answer: bool = False,
) -> list[UnitDraft]:
    output: list[UnitDraft] = []
    for unit in units:
        if unit.payload_kind != "text" or "text" not in unit.payload:
            output.append(unit)
            continue
        result = s4_build_qa_units(
            str(unit.payload["text"]),
            source=unit,
            require_explicit_answer=require_explicit_answer,
        )
        native_qa_section = (unit.artifact_locator or {}).get(
            "source"
        ) == "native_text" and any(
            rules.QA_FORM_QA_SECTION_RE.search(title)
            for title in [unit.title or "", *unit.heading_path]
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
            pieces: list[UnitDraft] = []
            if result.leading_text:
                # S2/S3 can legitimately place an answer tail, meeting
                # preamble, or form metadata before the first recognized
                # question. Keep that prefix as evidence and still expose the
                # complete QA suffix; the former all-or-nothing fallback hid
                # every later question in the block (1371-document audit).
                pieces.append(
                    UnitDraft(
                        **{
                            **unit.__dict__,
                            "payload": {"text": result.leading_text},
                            "quality_status": (
                                "needs_review"
                                if result.leading_needs_review
                                else unit.quality_status
                            ),
                        }
                    )
                )
            review_by_index: dict[int, list[str]] = {}
            for before_index, review_text in result.review_spans:
                review_by_index.setdefault(before_index, []).append(review_text)
            for qa_index in range(len(result.units) + 1):
                for review_text in review_by_index.get(qa_index, []):
                    pieces.append(
                        UnitDraft(
                            **{
                                **unit.__dict__,
                                "payload": {"text": review_text},
                                "quality_status": "needs_review",
                            }
                        )
                    )
                if qa_index < len(result.units):
                    pieces.append(result.units[qa_index])
            if result.trailing_text:
                pieces.append(
                    UnitDraft(
                        **{
                            **unit.__dict__,
                            "payload": {"text": result.trailing_text},
                            "quality_status": "needs_review",
                        }
                    )
                )
            output.extend(
                UnitDraft(
                    **{
                        **piece.__dict__,
                        "intra_order": unit.intra_order + offset,
                    }
                )
                for offset, piece in enumerate(pieces)
            )
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
    has_qa_marker = bool(rules.QA_TABLE_MARKER_RE.search(text))
    long_shredded_transcript = (
        len(text) >= rules.QA_TABLE_CONTENT_MIN_CHARS and has_qa_marker
    )
    # 1220145222: the first-page form table ends halfway through Q1 at only
    # 454 characters.  The old length-only gate called it ``ok``, even though
    # the continuation/answer lives in a later needs_review text carrier.
    # Keep the broad threshold for generic tables, but a proven official-form
    # transcript cue plus an unterminated tail is sufficient to fail closed.
    short_truncated_form_transcript = bool(
        has_qa_marker
        and rules.QA_FORM_TRANSCRIPT_CUE_RE.search(text)
        and not re.search(r"[。！？!?；;：:…）)】\]”’\"'」』]$", text.rstrip())
    )
    if not long_shredded_transcript and not short_truncated_form_transcript:
        return unit
    return UnitDraft(**{**unit.__dict__, "quality_status": "needs_review"})


def _downgrade_qa_before_shredded_table(units: list[UnitDraft]) -> list[UnitDraft]:
    """A Q&A cut at a text→table page boundary is not a complete `ok` QA.

    The continuation table does not always contain a later ``Q/答`` marker:
    MinerU may put only the rest of the current answer into a long one-cell
    header.  In that shape, an answer ending in a bare word/character is also
    a high-confidence truncation signal.  We preserve both units and merely
    downgrade the incomplete QA instead of silently claiming completeness.
    """

    out = list(units)
    for index, unit in enumerate(out):
        if unit.payload_kind != "table":
            continue
        previous = index - 1
        if previous < 0:
            continue
        candidate = out[previous]
        if candidate.payload_kind != "qa":
            continue
        table_text = _main_text(unit)
        marker_shredded = unit.quality_status == "needs_review" and bool(
            rules.QA_TABLE_MARKER_RE.search(table_text)
        )
        headers = [
            str(value).strip()
            for value in unit.payload.get("headers", [])
            if str(value).strip()
        ]
        long_narrative_header = bool(headers and len(headers[0]) >= 40)
        rows = unit.payload.get("rows") or []
        footer_firsts = [
            str(row[0]).strip() for row in rows if row and str(row[0]).strip()
        ]
        reanchored_footer_overflow = bool(
            unit.quality_status == "needs_review"
            and headers
            and footer_firsts
            and all(
                rules.QA_FORM_FOOTER_FIELD_RE.match(first) for first in footer_firsts
            )
        )
        if (
            candidate.heading_path != unit.heading_path
            and not reanchored_footer_overflow
        ):
            continue
        answer = str(candidate.payload.get("answer") or "").rstrip()
        answer_looks_cut = bool(answer) and not re.search(
            r"[。！？!?；;：:…）)】\]”’\"'」』]$", answer
        )
        if marker_shredded or (
            answer_looks_cut and (long_narrative_header or reanchored_footer_overflow)
        ):
            out[previous] = UnitDraft(
                **{**candidate.__dict__, "quality_status": "needs_review"}
            )

    # Symmetric table→text seam: a QA derived from a table row/header can end
    # mid-word while the following MinerU text element carries its tail.  Only
    # the last QA from that physical table can be adjacent to the continuation.
    for index, candidate in enumerate(out[:-1]):
        if candidate.payload_kind != "qa":
            continue
        answer = str(candidate.payload.get("answer") or "").rstrip()
        if not answer or re.search(r"[。！？!?；;：:…）)】\]”’\"'」』]$", answer):
            continue
        has_source_table = any(
            earlier.payload_kind == "table"
            and earlier.source_order == candidate.source_order
            for earlier in out[:index]
        )
        if not has_source_table:
            continue
        follower = out[index + 1]
        if (
            follower.payload_kind != "text"
            or follower.source_order <= candidate.source_order
        ):
            continue
        continuation = _main_text(follower).strip()
        if (
            not continuation
            or rules.QUESTION_START_RE.match(continuation)
            or rules.ANSWER_START_RE.match(continuation)
            or rules.ATTACHMENT_CAPTION_RE.match(continuation)
        ):
            continue
        out[index] = UnitDraft(
            **{**candidate.__dict__, "quality_status": "needs_review"}
        )
        # The follower is the other half of the same broken carrier.  Calling
        # its orphan answer tail ``ok`` lets L2 consume a contextless fragment
        # even though we already proved the preceding table QA is truncated
        # (phase00 IR Q1: ``此外，美的`` → ``系微波炉…``).
        out[index + 1] = UnitDraft(
            **{**follower.__dict__, "quality_status": "needs_review"}
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
        and not (header_first and rules.QA_FORM_FOOTER_FIELD_RE.match(header_first))
    )
    return UnitDraft(
        **{
            **unit.__dict__,
            "heading_path": [document_title],
            "structural_path": [document_title],
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
    headers: list[str],
    rows: list[list[str]],
    merged_cells: list[dict[str, int]],
    stats: BuildStats | None,
) -> tuple[list[list[str]], list[dict[str, int]]]:
    """Drop blank data rows while preserving full-grid merge coordinates.

    ``merged_cells.row`` is always relative to ``[headers, *rows]`` when a
    header exists, otherwise to ``rows``.  Keeping one coordinate system from
    mapper through published unit is required for an anchor-only renderer.
    """

    kept, adjusted, dropped = drop_blank_table_rows(
        headers=headers,
        rows=rows,
        merged_cells=merged_cells,
    )
    if stats is not None:
        stats.dropped_blank_table_rows += dropped
    return kept, adjusted


def s5_build_table_units(
    elements: Iterable[PreparedElement], stats: BuildStats
) -> list[UnitDraft]:
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
            if candidate.kind == "table" and _can_merge_continued_table(
                group[-1], candidate
            ):
                group.append(candidate)
                stats.merged_tables += 1
                index += 1
                continue
            break
        previous_text = _previous_text_before(items, element.order_index)
        units.append(
            _table_group_to_unit(group, previous_text=previous_text, stats=stats)
        )
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
    if filing_type in {"investor_relations", "performance_briefing"}:
        # Filing classification is controlled upstream and is more reliable
        # than issuer-specific title wording as a broad retrieval route.
        event_keys = tuple(sorted({*event_keys, "investor_communication"}))
    finalized: list[UnitDraft] = []
    for unit in units:
        note_keys = _note_keys_for_unit(unit, filing_type=filing_type)
        matched_keys = semantic_keys_for_unit(unit, filing_type=filing_type)
        market_risk_definition = _has_market_risk_definition_evidence(
            unit, note_keys=note_keys
        )
        semantic_key = (
            "market_risk"
            if market_risk_definition
            else unit.semantic_key or (matched_keys[0] if matched_keys else None)
        )
        keys = set(unit.semantic_keys or ())
        keys.update(event_keys)
        keys.update(matched_keys)
        if market_risk_definition:
            keys.add("market_risk")
        if semantic_key:
            keys.add(semantic_key)
        keys.update(note_keys)
        if semantic_key is None and note_keys:
            semantic_key = note_keys[0]
        if semantic_key is None and event_keys:
            semantic_key = event_keys[0]
        sorted_keys = sorted(keys)
        if semantic_key is None and sorted_keys:
            # Keep the scalar compatibility route consistent with the richer
            # array. This covers keys inherited by a pre-grouped mixed unit,
            # where neither the final title nor the document event supplies a
            # scalar candidate.
            semantic_key = sorted_keys[0]
        if semantic_key is None:
            semantic_key = rules.SEMANTIC_FALLBACK_KEY
            sorted_keys = [rules.SEMANTIC_FALLBACK_KEY]
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
                    # New output always has at least the controlled
                    # document_content fallback, so scalar and array expose
                    # one consistent non-empty retrieval state.
                    "semantic_keys": sorted_keys,
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
        if qa_check.unstable or not qa_check.units or qa_check.review_spans:
            return []
    return sections


def _native_direct_question_match(
    line: str, *, allow_alt_numbered: bool = False
) -> tuple[str, int, str] | None:
    explicit = rules.QA_DIRECT_EXPLICIT_QUESTION_RE.match(line)
    if explicit is not None:
        return (
            "explicit",
            int(explicit.group("ordinal")),
            explicit.group("question").strip(),
        )
    numbered = rules.QA_DIRECT_NUMBERED_QUESTION_RE.match(line)
    if numbered is None and allow_alt_numbered:
        numbered = rules.QA_DIRECT_ALT_NUMBERED_QUESTION_RE.match(line)
    if numbered is None:
        return None
    return (
        "numbered",
        int(numbered.group("ordinal")),
        numbered.group("question").strip(),
    )


_NATIVE_DIRECT_MAIN_LABEL_FRAGMENT_SEQUENCES: tuple[tuple[str, ...], ...] = (
    ("投资者关系活动", "主要内容介绍"),
    ("投资者关系活动主要内容", "介绍"),
    ("投资者关系活", "动主要内容介", "绍"),
    ("投资者关系活动主要", "内容介绍"),
    ("投资者关系活动主", "要内容介绍"),
)
_NATIVE_DIRECT_CATEGORY_FRAGMENT_SEQUENCES: tuple[tuple[str, ...], ...] = (
    ("投资者关系活动类", "别"),
    ("投资者关系活", "动类别"),
)


def _native_direct_fragment_remainder(line: str, fragment: str) -> str | None:
    """Return text after one anchored compact prefix without rewriting it."""

    target = _comparison_text(fragment)
    if not _comparison_text(line).startswith(target):
        return None
    consumed = ""
    for index, character in enumerate(line):
        if character.isspace():
            continue
        consumed += unicodedata.normalize("NFKC", character).casefold()
        if consumed == target:
            return line[index + 1 :].strip()
        if not target.startswith(consumed):
            return None
    return None


def _native_direct_fragment_sequence_match(
    lines: list[tuple[int, str]],
    *,
    start_index: int,
    end_index: int,
    first_end_index: int | None = None,
    sequences: tuple[tuple[str, ...], ...],
    allow_all_suffixes: bool = False,
    allow_final_suffix: bool = False,
    max_step_delta: int = 3,
    max_total_delta: int = 4,
) -> _NativeDirectFragmentMatch | None:
    """Match one bounded same-page form-label split.

    ``first_end_index`` constrains only the first fragment. This admits one
    observed PDF draw-order shape where a three-part outer label is interleaved
    with Q2's wrapped prompt, without accepting a label that first appears in
    Q2's answer.
    """

    for sequence in sequences:
        first_stop = min(end_index, first_end_index or end_index)
        for first_index in range(start_index, first_stop):
            first_page, first_line = lines[first_index]
            first_remainder = _native_direct_fragment_remainder(
                first_line, sequence[0]
            )
            if first_remainder is None or (
                first_remainder and not allow_all_suffixes
            ):
                continue
            positions = [first_index]
            final_remainder = first_remainder
            cursor = first_index + 1
            for fragment_index, fragment in enumerate(sequence[1:], 1):
                is_final = fragment_index == len(sequence) - 1
                search_stop = min(end_index, positions[-1] + max_step_delta + 1)
                found = next(
                    (
                        (index, remainder)
                        for index in range(cursor, search_stop)
                        if lines[index][0] == first_page
                        and (
                            remainder := _native_direct_fragment_remainder(
                                lines[index][1], fragment
                            )
                        )
                        is not None
                        and (
                            not remainder
                            or allow_all_suffixes
                            or (is_final and allow_final_suffix)
                        )
                    ),
                    None,
                )
                if found is None:
                    break
                position, final_remainder = found
                positions.append(position)
                cursor = position + 1
            if len(positions) != len(sequence):
                continue
            if positions[-1] - positions[0] <= max_total_delta:
                return _NativeDirectFragmentMatch(
                    positions=tuple(positions),
                    final_remainder=final_remainder,
                )
    return None


def _native_direct_layout_noise(line: str) -> bool:
    """Return whether one native line is a split outer-form label/footer."""

    compact = _comparison_text(line)
    return compact in {
        "投资者关系活动主要内容",
        "投资者关系活动主内容",
        "投资者关系活动主要内容介绍",
        "主要内容介绍",
        "交流内容及具体",
        "问答",
    } or bool(re.fullmatch(r"\d{1,3}", compact))


def _native_direct_question_answer_start(
    lines: list[tuple[int, str]],
    *,
    question_index: int,
    end_index: int,
    skipped_positions: frozenset[int],
) -> int | None:
    """Locate Q2's first answer boundary under the direct-QA state rules."""

    matched = _native_direct_question_match(
        lines[question_index][1], allow_alt_numbered=True
    )
    if matched is None:
        return None
    mode = matched[0]
    cursor = question_index + 1
    if mode == "numbered":
        question_lines = [matched[2]]
        continuation_count = 0
        while not _join_wrapped_lines(question_lines).endswith(("?", "？")):
            if cursor >= end_index or continuation_count >= 3:
                return None
            if cursor in skipped_positions:
                cursor += 1
                continue
            line = lines[cursor][1]
            if (
                _native_direct_question_match(line, allow_alt_numbered=True)
                is not None
                or rules.ANSWER_START_RE.match(line)
                or rules.QA_DIRECT_FORM_END_RE.match(line)
            ):
                return None
            question_lines.append(line)
            continuation_count += 1
            cursor += 1
        while cursor < end_index and cursor in skipped_positions:
            cursor += 1
        return cursor

    continuation_count = 0
    while cursor < end_index:
        if cursor in skipped_positions:
            cursor += 1
            continue
        line = lines[cursor][1]
        if rules.ANSWER_START_RE.match(line):
            return cursor
        if (
            _native_direct_question_match(line) is not None
            or rules.QA_DIRECT_FORM_END_RE.match(line)
            or continuation_count >= 3
        ):
            return None
        continuation_count += 1
        cursor += 1
    return None


def _native_direct_main_label_fragment_match(
    lines: list[tuple[int, str]], *, question_index: int, end_index: int
) -> _NativeDirectFragmentMatch | None:
    """Return a proven split outer-form label and any answer suffix."""

    second_question_index = next(
        (
            index
            for index in range(question_index + 1, end_index)
            if (
                (matched := _native_direct_question_match(
                    lines[index][1], allow_alt_numbered=True
                ))
                is not None
                and matched[1] == 2
            )
        ),
        None,
    )
    if second_question_index is None:
        return None
    matched_fragments = _native_direct_fragment_sequence_match(
        lines,
        start_index=question_index + 1,
        end_index=end_index,
        first_end_index=second_question_index,
        sequences=_NATIVE_DIRECT_MAIN_LABEL_FRAGMENT_SEQUENCES,
        allow_final_suffix=True,
    )
    if matched_fragments is None:
        return None
    positions = frozenset(matched_fragments.positions)
    answer_start = _native_direct_question_answer_start(
        lines,
        question_index=second_question_index,
        end_index=end_index,
        skipped_positions=positions,
    )
    if answer_start is None or matched_fragments.positions[-1] >= answer_start:
        return None
    remainder = matched_fragments.final_remainder
    if remainder:
        if matched_fragments.positions[-1] >= second_question_index:
            return None
        if (
            _native_direct_question_match(remainder, allow_alt_numbered=True)
            is not None
            or rules.ANSWER_START_RE.match(remainder)
            or rules.QA_DIRECT_FORM_END_RE.match(remainder)
        ):
            return None
    return matched_fragments


def _native_direct_main_label_evidence(
    lines: list[tuple[int, str]], *, question_index: int, end_index: int
) -> bool:
    """Prove the official main-field label despite PDF table draw order.

    A complete label before Q1 is sufficient.  When the PDF text layer draws
    the left-hand label after Q1, accept only exact split-label lines on Q1's
    page and before Q2, with at most two intervening physical lines.  This
    admits the real outer-form layout without treating narrative occurrences
    of ``主要内容介绍`` later in the transcript as form evidence.
    """

    complete_labels = {
        "投资者关系活动主要内容介绍",
        "主要内容介绍",
    }
    if any(
        _comparison_text(line) in complete_labels
        for _, line in lines[:question_index]
    ):
        return True

    if any(
        lines[index][0] == lines[question_index][0]
        and _comparison_text(lines[index][1])
        == "投资者关系活动主要内容介绍"
        for index in range(question_index + 1, end_index)
    ):
        return True
    return (
        _native_direct_main_label_fragment_match(
            lines, question_index=question_index, end_index=end_index
        )
        is not None
    )


def _native_direct_form_evidence(
    lines: list[tuple[int, str]], *, question_index: int, end_index: int
) -> bool:
    """Require an official IR form and transcript cue before parsing QA."""

    preamble = _comparison_text("\n".join(line for _, line in lines[:question_index]))
    if "投资者关系活动记录表" not in preamble:
        return False
    category_evidence = "投资者关系活动类别" in preamble or (
        "投资者关系活动" in preamble and "类别" in preamble
    ) or (
        _native_direct_fragment_sequence_match(
            lines,
            start_index=0,
            end_index=question_index,
            sequences=_NATIVE_DIRECT_CATEGORY_FRAGMENT_SEQUENCES,
            allow_all_suffixes=True,
        )
        is not None
    )
    if not category_evidence:
        return False
    # Count mutually exclusive field families, not one exact rendering per
    # field. Native extraction can split ``参与单位名称 ... 及人员姓名`` and
    # ``上市公司接待 ... 人员姓名`` across cells/lines; the compact preamble
    # still preserves each official label stem. Each family contributes at
    # most one vote, and the existing >=3-form-fields gate remains unchanged.
    field_evidence = sum(
        (
            any(
                token in preamble
                for token in (
                    "参与单位名称及人员姓名",
                    "参与单位名称",
                    "活动参与人员",
                )
            ),
            any(
                token in preamble for token in ("上市公司接待人员姓名", "上市公司接待")
            ),
            "时间" in preamble,
            "地点" in preamble,
            "形式" in preamble,
        )
    )
    if field_evidence < 3:
        return False
    transcript = _comparison_text("\n".join(line for _, line in lines[:end_index]))
    official_main_label = _native_direct_main_label_evidence(
        lines,
        question_index=question_index,
        end_index=end_index,
    )
    return bool(
        rules.QA_DIRECT_TRANSCRIPT_CUE_RE.search(transcript)
        or ("交流内容及具体" in transcript and "问答" in transcript)
        # Some official templates use only the main narrative field label.
        # The helper admits its exact, bounded PDF draw-order variant beside Q1.
        # The caller still requires a complete footer plus a consecutive 1..N
        # sequence of at least two fully closed questions before emitting QA.
        or official_main_label
    )


def _native_direct_qa_pairs(native_text: Any) -> list[_NativeQaPair]:
    """Parse a strict official-form direct transcript from native PDF text.

    This family is intentionally separate from generic S4 parsing.  It is
    allowed to infer an unlabelled answer only after proving the official form,
    its transcript cue, a complete footer boundary, and a consecutive 1..N
    question sequence.  Any ambiguity rejects the whole recovery.
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
        lines.extend(
            (page_no, line.strip())
            for line in str(page.get("text") or "").splitlines()
            if line.strip()
        )
    if not lines:
        return []

    start_index: int | None = None
    mode: str | None = None
    for index, (_, line) in enumerate(lines):
        match = _native_direct_question_match(line)
        if match is not None and match[1] == 1:
            start_index = index
            mode = match[0]
            break
    if start_index is None:
        # Preserve the established dot/Q-prefixed baseline exactly.  Only a
        # document with no old-family Q1 may opt into the alternate Chinese
        # ``1、`` / ``1：`` family; once selected it remains isolated from the
        # explicit-Q state machine so answer-side numbered lists cannot become
        # outer questions.
        for index, (_, line) in enumerate(lines):
            match = _native_direct_question_match(line, allow_alt_numbered=True)
            if match is not None and match[0] == "numbered" and match[1] == 1:
                start_index = index
                mode = match[0]
                break
    if start_index is None or mode is None:
        return []
    end_index = next(
        (
            index
            for index, (_, line) in enumerate(lines[start_index + 1 :], start_index + 1)
            if rules.QA_DIRECT_FORM_END_RE.match(line)
        ),
        None,
    )
    if end_index is None or not _native_direct_form_evidence(
        lines, question_index=start_index, end_index=end_index
    ):
        return []

    label_match = _native_direct_main_label_fragment_match(
        lines, question_index=start_index, end_index=end_index
    )
    label_replacements: dict[int, str] = {}
    if label_match is not None:
        label_replacements.update(
            {position: "" for position in label_match.positions}
        )
        label_replacements[label_match.positions[-1]] = (
            label_match.final_remainder
        )

    def content_line(index: int) -> str | None:
        if index in label_replacements:
            return label_replacements[index] or None
        line = lines[index][1]
        return None if _native_direct_layout_noise(line) else line

    pairs: list[_NativeQaPair] = []
    cursor = start_index
    expected = 1
    while cursor < end_index:
        while cursor < end_index and content_line(cursor) is None:
            cursor += 1
        if cursor >= end_index:
            break
        matched = _native_direct_question_match(
            lines[cursor][1], allow_alt_numbered=mode == "numbered"
        )
        if matched is None or matched[0] != mode or matched[1] != expected:
            return []
        question_start = cursor
        question_lines = [matched[2]]
        cursor += 1

        inline_answer = ""
        inline_answer_page: int | None = None
        if mode == "explicit":
            # Q-prefixed transcripts prove the boundary with an explicit
            # 回复/答 marker. A bounded continuation supports wrapped prompts
            # without looking arbitrarily far into the answer.
            continuation_count = 0
            while cursor < end_index:
                current_line = content_line(cursor)
                if current_line is None:
                    cursor += 1
                    continue
                answer_match = rules.ANSWER_START_RE.match(current_line)
                if answer_match is not None:
                    inline_answer = _strip_answer_prefix(current_line)
                    if inline_answer:
                        inline_answer_page = lines[cursor][0]
                    cursor += 1
                    break
                if (
                    _native_direct_question_match(current_line) is not None
                    or rules.QA_DIRECT_FORM_END_RE.match(current_line)
                    or continuation_count >= 3
                ):
                    return []
                question_lines.append(current_line)
                continuation_count += 1
                cursor += 1
            else:
                return []
        else:
            # Unlabelled-answer transcripts must print a complete interrogative
            # prompt.  Wrapped prompts are bounded to four native lines.
            continuation_count = 0
            while not _join_wrapped_lines(question_lines).endswith(("?", "？")):
                if cursor >= end_index or continuation_count >= 3:
                    return []
                current_line = content_line(cursor)
                if current_line is None:
                    cursor += 1
                    continue
                if (
                    _native_direct_question_match(
                        current_line, allow_alt_numbered=True
                    )
                    is not None
                    or rules.ANSWER_START_RE.match(current_line)
                    or rules.QA_DIRECT_FORM_END_RE.match(current_line)
                ):
                    return []
                question_lines.append(current_line)
                continuation_count += 1
                cursor += 1

        answer_lines = [inline_answer] if inline_answer else []
        raw_answer_lines = [inline_answer] if inline_answer else []
        explicit_answer_seen = bool(inline_answer)
        last_answer_page = inline_answer_page
        while cursor < end_index:
            page_no = lines[cursor][0]
            current_line = content_line(cursor)
            if current_line is None:
                cursor += 1
                continue
            following = _native_direct_question_match(
                current_line, allow_alt_numbered=mode == "numbered"
            )
            if following is not None:
                if following[0] != mode or following[1] != expected + 1:
                    return []
                break
            answer_match = rules.ANSWER_START_RE.match(current_line)
            if answer_match is not None:
                # Numbered direct transcripts may still print 答/回复. It is
                # valid only once, at the beginning of this answer.
                if answer_lines or explicit_answer_seen:
                    return []
                stripped_answer = _strip_answer_prefix(current_line)
                explicit_answer_seen = True
                if stripped_answer:
                    answer_lines.append(stripped_answer)
                    raw_answer_lines.append(current_line)
                    last_answer_page = page_no
                cursor += 1
                continue
            answer_lines.append(current_line)
            raw_answer_lines.append(current_line)
            last_answer_page = page_no
            cursor += 1

        question = _join_wrapped_lines(question_lines)
        answer = _join_wrapped_lines(answer_lines)
        if not question or not answer or last_answer_page is None:
            return []
        raw_text = "\n".join([question, *raw_answer_lines]).strip()
        pairs.append(
            _NativeQaPair(
                ordinal=expected,
                question=question,
                answer=answer,
                raw_text=raw_text,
                start_page_no=lines[question_start][0],
                end_page_no=last_answer_page,
            )
        )
        expected += 1

    if len(pairs) < 2 or [pair.ordinal for pair in pairs] != list(
        range(1, len(pairs) + 1)
    ):
        return []
    return pairs


def _native_direct_qa_units(
    normalized_ir: dict[str, Any],
    *,
    filing_type: str | None,
    document_title: str | None,
) -> list[UnitDraft]:
    if filing_type not in {"investor_relations", "performance_briefing"}:
        return []
    pairs = _native_direct_qa_pairs(normalized_ir.get("native_text"))
    if not pairs:
        return []
    raw_elements = [dict(item) for item in normalized_ir.get("elements", [])]
    max_order = max(
        (
            _int_or_none(element.get("order_index")) or index
            for index, element in enumerate(raw_elements)
        ),
        default=0,
    )
    native_hash = str(
        (normalized_ir.get("native_text") or {}).get("content_hash") or ""
    )
    anchor = document_title or "投资者关系活动记录表"
    units: list[UnitDraft] = []
    for pair in pairs:
        question_identity = _comparison_text(pair.question)
        source_order = next(
            (
                _int_or_none(element.get("order_index")) or index
                for index, element in enumerate(raw_elements)
                if question_identity
                and question_identity in _comparison_text(_raw_element_text(element))
            ),
            None,
        )
        if source_order is None:
            source_order = next(
                (
                    _int_or_none(element.get("order_index")) or index
                    for index, element in enumerate(raw_elements)
                    if _int_or_none(element.get("page_no")) == pair.start_page_no
                ),
                max_order + pair.ordinal,
            )
        locator: dict[str, Any] = {
            "source": "native_text",
            "page_span": [pair.start_page_no, pair.end_page_no],
        }
        if native_hash:
            locator["native_text_hash"] = native_hash
        units.append(
            UnitDraft(
                payload_kind="qa",
                payload={
                    "question": pair.question,
                    "answer": pair.answer,
                    "raw_text": pair.raw_text,
                },
                source_order=source_order,
                intra_order=pair.ordinal,
                heading_path=[anchor],
                structural_path=[anchor],
                title=pair.question,
                artifact_locator=locator,
            )
        )
    return units


def _qa_question_identity(unit: UnitDraft) -> str:
    if unit.payload_kind != "qa":
        return ""
    identity = _comparison_text(str(unit.payload.get("question") or ""))
    return re.sub(r"[?？。.!！:：]+$", "", identity)


def _merge_direct_native_qa_units(
    units: list[UnitDraft], native_units: list[UnitDraft]
) -> list[UnitDraft]:
    """Prefer complete native pairs over duplicate MinerU-derived QA leaves."""

    native_by_question: dict[str, UnitDraft] = {}
    for unit in native_units:
        identity = _qa_question_identity(unit)
        if identity and identity not in native_by_question:
            native_by_question[identity] = unit
    if not native_by_question:
        return units
    kept = [
        unit
        for unit in units
        if not (
            unit.payload_kind == "qa"
            and _qa_question_identity(unit) in native_by_question
        )
    ]
    return sorted([*kept, *native_by_question.values()], key=_unit_sort_key)


def _native_qa_overlap_text(value: Any) -> str:
    """Canonicalize only for proven native/MinerU duplicate suppression."""

    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized)


def _suppress_direct_native_qa_carriers(
    units: list[UnitDraft],
    native_units: list[UnitDraft],
    *,
    stats: BuildStats,
) -> list[UnitDraft]:
    """Remove MinerU fragments already covered by a proven native QA run.

    The strict native parser has already proved the official form, footer and
    complete consecutive question sequence before this function is called.
    Suppression is still narrow: a non-QA text carrier must be a long exact
    substring after punctuation normalization, while tables are trimmed only
    at matching row/cell granularity so metadata and footer rows survive.
    """

    native_qa_units = [unit for unit in native_units if unit.payload_kind == "qa"]
    coverage = "".join(
        _native_qa_overlap_text(unit.payload.get("raw_text") or "")
        for unit in native_qa_units
    )
    numbered_coverage = "".join(
        _native_qa_overlap_text(
            f"{unit.intra_order}{unit.payload.get('raw_text') or ''}"
        )
        for unit in native_qa_units
    )
    questions_by_ordinal = {
        unit.intra_order: _native_qa_overlap_text(unit.payload.get("question") or "")
        for unit in native_qa_units
        if unit.intra_order > 0
    }
    if not coverage or not numbered_coverage:
        return units

    def covered(value: Any) -> bool:
        raw = str(value)
        raw_candidate = _native_qa_overlap_text(raw)
        candidates = [raw_candidate]
        without_ordinal = re.sub(
            r"^\s*(?:Q\s*)?\d{1,3}\s*[.．、：:]\s*",
            "",
            raw,
            count=1,
            flags=re.IGNORECASE,
        )
        if without_ordinal != raw:
            candidates.append(_native_qa_overlap_text(without_ordinal))
        if any(
            len(candidate) >= 16
            and (candidate in coverage or candidate in numbered_coverage)
            for candidate in candidates
        ):
            return True

        # A question-only MinerU shard can be shorter than the long-carrier
        # floor. Remove it only when the whole physical value is a strict
        # numbered question and both its ordinal and normalized question are
        # identical to one member of the already-proven native 1..N run.
        question_match = _native_direct_question_match(
            raw, allow_alt_numbered=True
        )
        if question_match is None:
            return False
        _, ordinal, question = question_match
        return bool(
            questions_by_ordinal.get(ordinal)
            and _native_qa_overlap_text(question) == questions_by_ordinal[ordinal]
        )

    kept: list[UnitDraft] = []
    for unit in units:
        if unit.payload_kind == "qa":
            kept.append(unit)
            continue
        if (
            unit.payload_kind == "text"
            and "image_ref" not in unit.payload
            and covered(unit.payload.get("text") or "")
        ):
            stats.native_text_carriers_suppressed += 1
            continue
        if unit.payload_kind != "table":
            kept.append(unit)
            continue

        rows = unit.payload.get("rows")
        headers = unit.payload.get("headers")
        if not isinstance(rows, list) or not isinstance(headers, list):
            kept.append(unit)
            continue
        covered_headers = bool(
            headers and any(covered(cell) for cell in headers if str(cell).strip())
        )
        filtered_rows = [
            row
            for row in rows
            if not (
                isinstance(row, list)
                and any(covered(cell) for cell in row if str(cell).strip())
            )
        ]
        removed = len(rows) - len(filtered_rows) + int(covered_headers)
        if not removed:
            kept.append(unit)
            continue
        stats.native_text_table_rows_suppressed += removed
        payload = {
            **unit.payload,
            "headers": [] if covered_headers else headers,
            "rows": filtered_rows,
        }
        if not any(
            (
                payload.get("headers"),
                payload.get("rows"),
                payload.get("caption"),
                payload.get("notes"),
            )
        ):
            stats.native_text_carriers_suppressed += 1
            continue
        kept.append(UnitDraft(**{**unit.__dict__, "payload": payload}))
    return kept


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


def _performance_briefing_has_transcript_evidence(
    elements: list[dict[str, Any]], *, document_title: str | None
) -> bool:
    """Require document-local QA evidence before relaxing heading parsing.

    A provider filing type describes the event, not the carrier.  The same
    ``performance_briefing`` class contains both verbatim Q&A transcripts and
    ordinary numbered press releases.  Only the former may demote numbered
    headings into unlabelled question boundaries.
    """

    values = [document_title or ""]
    values.extend(_raw_element_text(element) for element in elements)
    transcript = "\n".join(value for value in values if value.strip())
    if not transcript:
        return False
    if (
        rules.QA_DIRECT_TRANSCRIPT_CUE_RE.search(transcript)
        or rules.QA_FORM_TRANSCRIPT_CUE_RE.search(transcript)
        or rules.QA_FORM_MAIN_SECTION_RE.search(transcript)
    ):
        return True
    official_form = "投资者关系活动记录表" in transcript
    narrative_field = "投资者关系活动主要内容介绍" in transcript
    question_ordinals: list[int] = []
    for element in elements:
        if str(element.get("kind") or "") not in {"heading", "text"}:
            continue
        match = re.fullmatch(
            r"\s*(\d{1,3})[.．、]\s*[^\n]{2,2000}[？?]\s*",
            str(element.get("text") or ""),
        )
        if match is not None:
            question_ordinals.append(int(match.group(1)))
    if official_form and narrative_field and any(
        right == left + 1
        for left, right in zip(question_ordinals, question_ordinals[1:])
    ):
        return True
    lines = [line.strip() for line in transcript.splitlines() if line.strip()]
    has_explicit_question = any(
        rules.EXPLICIT_QUESTION_START_RE.match(line) for line in lines
    )
    has_explicit_answer = any(
        rules.ANSWER_START_RE.match(line)
        or rules.BRACKET_SPEAKER_ANSWER_RE.match(line)
        for line in lines
    )
    return has_explicit_question and has_explicit_answer


def _marker_start(text: str, marker: str) -> int | None:
    compact = re.sub(r"\s+", "", marker)
    if not compact:
        return None
    match = re.search(r"\s*".join(re.escape(char) for char in compact), text)
    return match.start() if match is not None else None


def _element_contains_marker(element: dict[str, Any], marker: str) -> bool:
    return _marker_start(_raw_element_text(element), marker) is not None


def _raw_start_table_is_safe_narrative(element: dict[str, Any], marker: str) -> bool:
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
    table["rows"] = [[trim(cell) for cell in row] for row in table.get("rows") or []]
    if not seen:
        return None
    return {**element, "table": table}


def _raw_table_is_empty(element: dict[str, Any]) -> bool:
    table = element.get("table") or {}
    return not any(
        str(cell).strip() for cell in table.get("headers") or []
    ) and not any(str(cell).strip() for row in table.get("rows") or [] for cell in row)


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
        column for row in grids for column, cell in enumerate(row) if str(cell).strip()
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
        (
            line.strip()
            for line in str(element.get("text") or "").splitlines()
            if line.strip()
        ),
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
        sum(bool(str(cell).strip()) for cell in row) > 1 for row in pre_footer_grids
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
    footer_grid_start = (1 if table.get("headers") else 0) + first_footer
    for cell in table.get("merged_cells") or []:
        row = int(cell["row"])
        source_end = row + int(cell["rowspan"])
        kept_start = max(row, footer_grid_start)
        if source_end <= kept_start:
            continue
        merged_cells.append(
            {
                **cell,
                "row": kept_start - footer_grid_start,
                "rowspan": source_end - kept_start,
            }
        )
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
    if not _raw_start_table_is_safe_narrative(raw_elements[start_index], first_title):
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

    native_hash = str(
        (normalized_ir.get("native_text") or {}).get("content_hash") or ""
    )
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
    recovery = _replace_qa_form_narrative(normalized_ir, filing_type=filing_type)
    relaxed_qa_mode = filing_type == "investor_relations" or (
        filing_type == "performance_briefing"
        and _performance_briefing_has_transcript_evidence(
            recovery.elements,
            document_title=document_title,
        )
    )
    native_direct_qa = (
        []
        if recovery.section_count
        else _native_direct_qa_units(
            normalized_ir,
            filing_type=filing_type,
            document_title=document_title,
        )
    )
    s1 = s1_preprocess_elements(
        recovery.elements,
        image_bytes_resolver=image_bytes_resolver,
    )
    s1.stats.native_text_sections_recovered = recovery.section_count
    s1.stats.native_text_qa_pairs_recovered = len(native_direct_qa)
    s1.stats.qa_form_carriers_replaced = recovery.replaced_carriers
    elements = _merge_registered_cover_title_fragments(
        s1.elements,
        document_title=document_title,
        stats=s1.stats,
    )
    # Preserve substantive pre-section evidence. Only an exact page-1 date
    # beneath a proven periodic-report cover and before a later structural
    # root is metadata; the former bulk-prefix truncation is forbidden.
    elements = _drop_periodic_cover_date(
        elements,
        filing_type=filing_type,
        stats=s1.stats,
    )
    placed = s2_apply_heading_tree(
        elements,
        qa_heading_mode=relaxed_qa_mode,
    )
    text_units = replace_text_units_with_qa_where_stable(
        s3_build_text_units(placed, stats=s1.stats),
        require_explicit_answer=not relaxed_qa_mode,
    )
    table_units = s5_build_table_units(placed, s1.stats)
    table_units = [
        _clean_periodic_table_banner_captions(
            unit, filing_type=filing_type, stats=s1.stats
        )
        for unit in table_units
    ]
    if filing_type in {"investor_relations", "performance_briefing"}:
        table_units = [_flag_shredded_qa_table(unit) for unit in table_units]
        table_units = [
            _reanchor_qa_form_footer(unit, document_title=document_title)
            for unit in table_units
        ]
    table_qa_units = _qa_units_from_tables(table_units)
    units = sorted([*text_units, *table_units, *table_qa_units], key=_unit_sort_key)
    if filing_type in {"investor_relations", "performance_briefing"}:
        units = _recover_qa_across_table_text_seams(units)
        units = sorted(units, key=_unit_sort_key)
        # Let the strict two-carrier seam rules consume their exact physical
        # shapes first. The broader bounded logical-run recovery must not
        # replace a complete text→table answer with a truncated projection.
        units = _recover_qa_across_logical_carrier_runs(units)
        units = sorted(units, key=_unit_sort_key)
        units = _recover_official_form_unlabelled_q1_q2(
            units,
            raw_elements=recovery.elements,
        )
        units = sorted(units, key=_unit_sort_key)
        units = _recover_final_unlabelled_qa_footer(units)
        units = sorted(units, key=_unit_sort_key)
        units = _downgrade_qa_before_shredded_table(units)
        units = _recover_qa_answer_text_sandwiches(units)
        units = sorted(units, key=_unit_sort_key)
        units = _merge_direct_native_qa_units(units, native_direct_qa)
        units = _suppress_direct_native_qa_carriers(
            units,
            native_direct_qa,
            stats=s1.stats,
        )
    units = _sink_leading_applicable(units)
    kept = s6_filter_units(units, s1.stats)
    kept = _anchor_headerless_units(kept, document_title=document_title, stats=s1.stats)
    kept = _mark_native_shadow_fallback_needs_review(
        kept,
        normalized_ir=normalized_ir,
        filing_type=filing_type,
    )
    kept = s8_group_semantic_units(
        kept, filing_type=filing_type, document_title=document_title, stats=s1.stats
    )
    kept = _drop_periodic_cover_metadata_units(
        kept, filing_type=filing_type, stats=s1.stats
    )
    kept = _merge_announcement_number_carriers(kept, stats=s1.stats)
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


def _mark_native_shadow_fallback_needs_review(
    units: list[UnitDraft],
    *,
    normalized_ir: dict[str, Any],
    filing_type: str | None,
) -> list[UnitDraft]:
    """Fail closed when a form-scoped native-text cross-check was unavailable."""

    if filing_type not in {"investor_relations", "performance_briefing"}:
        return units
    diagnostics = normalized_ir.get("parser_diagnostics")
    if not isinstance(diagnostics, dict):
        return units
    shadow = diagnostics.get("native_text_shadow")
    if not isinstance(shadow, dict) or shadow.get("status") not in {
        "empty",
        "unavailable",
    }:
        return units
    return [
        (
            unit
            if unit.quality_status == "unusable"
            else replace(unit, quality_status="needs_review")
        )
        for unit in units
    ]


def _merge_registered_cover_title_fragments(
    elements: list[PreparedElement],
    *,
    document_title: str | None,
    stats: BuildStats,
) -> list[PreparedElement]:
    """Join only a page-1 heading chain proven by the registered title.

    MinerU can turn visual line wrapping on an announcement cover into a false
    parent/child tree (for example ``...补`` + ``充公告``). We do not infer a
    join from typography or length. A contiguous range inside the opening
    page-1 heading run must concatenate exactly to the registered title or its
    issuer-prefix suffix after whitespace/NFKC normalization.
    """

    if not document_title:
        return elements
    run_start = 0
    while run_start < len(elements):
        element = elements[run_start]
        if (
            element.kind != "text"
            or element.page_no != 1
            or not element.text
            or rules.strip_header_kv_line(element.text) != ""
        ):
            break
        run_start += 1
    prefix_end = run_start
    while prefix_end < len(elements):
        element = elements[prefix_end]
        if element.kind != "heading" or element.page_no != 1 or not element.text:
            break
        prefix_end += 1
    if prefix_end - run_start < 2:
        return elements

    targets = [(document_title, _comparison_text(document_title))]
    issuer_split = re.split(r"[：:]", document_title, maxsplit=1)
    if len(issuer_split) == 2 and issuer_split[1].strip():
        suffix = issuer_split[1].strip()
        targets.append((suffix, _comparison_text(suffix)))

    match: tuple[int, int, str] | None = None
    for width in range(prefix_end, 1, -1):
        for start in range(run_start, prefix_end - width + 1):
            end = start + width
            joined = _comparison_text(
                "".join(str(element.text or "") for element in elements[start:end])
            )
            display = next(
                (value for value, normalized in targets if joined == normalized),
                None,
            )
            if display is not None:
                match = (start, end, display)
                break
        if match is not None:
            break
    if match is None:
        return elements

    start, end, display = match
    merged = replace(
        elements[start],
        text=display,
        heading_level=1,
        title=None,
    )
    stats.merged_cover_title_fragments += end - start - 1
    return [*elements[:start], merged, *elements[end:]]


def _drop_periodic_cover_date(
    elements: list[PreparedElement],
    *,
    filing_type: str | None,
    stats: BuildStats,
) -> list[PreparedElement]:
    """Drop only a proven periodic-report cover's exact publication date."""

    if filing_type not in rules.PERIODIC_REPORT_FILING_TYPES:
        return elements
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
    if first_structural is None or first_structural == 0:
        return elements

    dropped: set[int] = set()
    for index, element in enumerate(elements[:first_structural]):
        if (
            element.kind not in {"heading", "text"}
            or element.page_no != 1
            or rules.PERIODIC_COVER_DATE_ONLY_RE.fullmatch(
                (element.text or "").strip()
            )
            is None
        ):
            continue
        preceding = [
            candidate
            for candidate in elements[:index]
            if candidate.kind in {"heading", "text", "table", "image"}
        ]
        if (
            preceding
            and all(candidate.kind == "heading" for candidate in preceding)
            and any(
                rules.PERIODIC_REPORT_TITLE_RE.search(candidate.text or "")
                for candidate in preceding
            )
        ):
            dropped.add(index)

    if not dropped:
        return elements
    stats.dropped_cover_prelude += len(dropped)
    return [element for index, element in enumerate(elements) if index not in dropped]


def _drop_periodic_cover_metadata_units(
    units: list[UnitDraft], *, filing_type: str | None, stats: BuildStats
) -> list[UnitDraft]:
    """Drop only page-one units proven to contain cover metadata alone.

    Periodic covers can be parsed as ``issuer heading + report-title/month``
    or ``brand/company headings + stock-code/report-title``.  Removing a
    broad prefix would risk substantive letters and introductions, so this
    gate requires an early page-one text unit, one exact report-title line,
    and no line outside the closed stock-code/date metadata families.
    """

    if filing_type not in rules.PERIODIC_REPORT_FILING_TYPES:
        return units
    kept: list[UnitDraft] = []
    for unit in units:
        page_no = (unit.artifact_locator or {}).get("page_no")
        lines = [
            line.strip()
            for line in str(unit.payload.get("text", "")).splitlines()
            if line.strip()
        ]
        has_report_title = any(
            rules.PERIODIC_COVER_REPORT_TITLE_LINE_RE.fullmatch(line)
            for line in lines
        )
        metadata_only = bool(lines) and all(
            rules.PERIODIC_COVER_REPORT_TITLE_LINE_RE.fullmatch(line)
            or any(pattern.fullmatch(line) for pattern in rules.PERIODIC_COVER_AUXILIARY_LINE_RES)
            for line in lines
        )
        if (
            unit.payload_kind == "text"
            and "image_ref" not in unit.payload
            and page_no == 1
            and unit.source_order <= 3
            and has_report_title
            and metadata_only
        ):
            stats.dropped_by_kind["periodic_cover_metadata"] += 1
            continue
        kept.append(unit)
    return kept


def _merge_announcement_number_carriers(
    units: list[UnitDraft], *, stats: BuildStats
) -> list[UnitDraft]:
    """Attach unique announcement numbers to the immediately following content.

    The provider/document contract has no extracted announcement-number field.
    A number-only unit is therefore not noise, but it is also a poor retrieval
    fragment. Consecutive, closed page-one cover carriers and any immediately
    following cover-only metadata units are folded into the first substantive
    text/mixed unit while that unit's real title/path/semantic keys remain the
    owner. Without a safe target, every source unit is retained.
    """

    output: list[UnitDraft] = []
    index = 0
    while index < len(units):
        carriers: list[UnitDraft] = []
        number_lines: list[str] = []
        dropped_metadata_lines = 0
        cursor = index
        while cursor < len(units):
            parsed = _announcement_number_carrier(units[cursor])
            if parsed is None:
                break
            numbers, metadata_count = parsed
            carriers.append(units[cursor])
            number_lines.extend(numbers)
            dropped_metadata_lines += metadata_count
            cursor += 1

        metadata_units: list[UnitDraft] = []
        while carriers and cursor < len(units):
            continuation_count = _announcement_cover_metadata_unit_line_count(
                units[cursor]
            )
            if continuation_count is None:
                break
            metadata_units.append(units[cursor])
            dropped_metadata_lines += continuation_count
            cursor += 1
        if not carriers or cursor >= len(units):
            output.append(units[index])
            index += 1
            continue
        target = units[cursor]
        if not _announcement_number_merge_target(target):
            output.append(units[index])
            index += 1
            continue

        existing_lines = {
            line.strip() for line in _main_text(target).splitlines() if line.strip()
        }
        unique_numbers: list[str] = []
        seen = set(existing_lines)
        for line in number_lines:
            if line not in seen:
                unique_numbers.append(line)
                seen.add(line)
        deduplicated = len(unique_numbers) < len(number_lines)
        output.append(
            _prepend_announcement_numbers(
                target,
                carriers=[*carriers, *metadata_units],
                number_lines=unique_numbers,
            )
        )
        stats.merged_announcement_header_units += len(carriers)
        if deduplicated:
            stats.deduplicated_announcement_header_units += len(carriers)
        stats.dropped_by_kind[
            "announcement_cover_metadata_line"
        ] += dropped_metadata_lines
        index = cursor + 1
    return output


def _announcement_number_carrier(
    unit: UnitDraft,
) -> tuple[list[str], int] | None:
    if unit.payload_kind != "text" or "image_ref" in unit.payload:
        return None
    lines = [
        line.strip()
        for line in str(unit.payload.get("text", "")).splitlines()
        if line.strip()
    ]
    number_lines = [
        line for line in lines if rules.ANNOUNCEMENT_NUMBER_LINE_RE.fullmatch(line)
    ]
    if len(number_lines) != 1:
        return None
    metadata_lines = [line for line in lines if line not in number_lines]
    page_no = (unit.artifact_locator or {}).get("page_no")
    if page_no not in {None, 1} or unit.source_order > 4:
        return None
    if metadata_lines and not all(
        _closed_announcement_cover_line(line) for line in metadata_lines
    ):
        return None
    return number_lines, len(metadata_lines)


def _closed_announcement_cover_line(line: str) -> bool:
    return bool(
        rules.HEADER_KV_LINE_RE.fullmatch(line)
        or rules.PERIODIC_COVER_REPORT_TITLE_LINE_RE.fullmatch(line)
        or rules.PERIODIC_COVER_DATE_ONLY_RE.fullmatch(line)
        or any(
            pattern.fullmatch(line)
            for pattern in rules.PERIODIC_COVER_AUXILIARY_LINE_RES
        )
    )


def _announcement_cover_metadata_unit_line_count(unit: UnitDraft) -> int | None:
    """Recognize a bounded metadata continuation only after a number carrier.

    Chinese dates are valid evidence in ordinary body sections, so this helper
    is deliberately not a global noise predicate. Its caller reaches it only
    through a page-one announcement number at source order <= 4, and the
    continuation itself must remain early and strictly adjacent.
    """

    if unit.payload_kind != "text" or "image_ref" in unit.payload:
        return None
    lines = [
        line.strip()
        for line in str(unit.payload.get("text", "")).splitlines()
        if line.strip()
    ]
    page_no = (unit.artifact_locator or {}).get("page_no")
    if (
        not lines
        or page_no not in {None, 1}
        or unit.source_order > 10
        or not all(_closed_announcement_cover_line(line) for line in lines)
    ):
        return None
    return len(lines)


def _announcement_number_merge_target(unit: UnitDraft) -> bool:
    if unit.payload_kind == "text":
        if "image_ref" in unit.payload or "text" not in unit.payload:
            return False
    elif unit.payload_kind != "mixed" or not isinstance(
        unit.payload.get("parts"), list
    ):
        return False
    lines = [line.strip() for line in _main_text(unit).splitlines() if line.strip()]
    return bool(lines) and any(
        not _closed_announcement_cover_line(line) for line in lines
    )


def _prepend_announcement_numbers(
    target: UnitDraft,
    *,
    carriers: list[UnitDraft],
    number_lines: list[str],
) -> UnitDraft:
    payload = dict(target.payload)
    prefix = "\n".join(number_lines)
    if prefix and target.payload_kind == "text":
        text = str(payload.get("text", ""))
        payload["text"] = f"{prefix}\n{text}" if text else prefix
    elif prefix:
        parts = list(payload.get("parts") or [])
        payload["parts"] = [
            {
                "kind": "text",
                "order": min(unit.source_order for unit in carriers),
                "text": prefix,
            },
            *parts,
        ]
    members = [*carriers, target]
    quality_status = (
        "unusable"
        if target.quality_status == "unusable"
        else _worst_quality(members)
    )
    # The merged unit is still the substantive target.  Starting from the
    # target locator preserves its body bbox/page instead of making the public
    # unit appear to point at the small announcement-number carrier.  The
    # source span and reason retain the otherwise-lost merge provenance.
    artifact_locator = _merged_locator([target, *carriers]) or {}
    artifact_locator["source_order_span"] = [
        min(unit.source_order for unit in members),
        max(unit.source_order for unit in members),
    ]
    artifact_locator["merge_reason"] = "announcement_number_carrier"
    return replace(
        target,
        payload=payload,
        source_order=min(unit.source_order for unit in members),
        intra_order=min(unit.intra_order for unit in members),
        quality_status=quality_status,
        artifact_locator=artifact_locator,
    )


def _clean_periodic_table_banner_captions(
    unit: UnitDraft,
    *,
    filing_type: str | None,
    stats: BuildStats,
) -> UnitDraft:
    """Remove a prior-page report banner misattached as a table caption."""

    if (
        filing_type not in rules.PERIODIC_REPORT_FILING_TYPES
        or unit.payload_kind != "table"
    ):
        return unit
    captions = [str(value) for value in unit.payload.get("caption", [])]
    removed = [
        caption
        for caption in captions
        if rules.PERIODIC_REPORT_BANNER_RE.fullmatch(caption.strip())
    ]
    if not removed:
        return unit
    path = unit.structural_path or unit.heading_path
    fallback_title = path[-1] if path else None
    if not fallback_title or rules.PERIODIC_REPORT_BANNER_RE.fullmatch(
        fallback_title.strip()
    ):
        # Without an independent structural owner the banner may be the only
        # surviving table identity; fail closed rather than invent a title.
        return unit
    payload = dict(unit.payload)
    payload["caption"] = [caption for caption in captions if caption not in removed]
    title = unit.title
    if title is not None and title in removed:
        title = fallback_title
    stats.dropped_by_kind["periodic_report_banner_caption"] += len(removed)
    return UnitDraft(**{**unit.__dict__, "payload": payload, "title": title})


def _is_structural_l1(element: PreparedElement) -> bool:
    text = (element.text or "").strip()
    if not text:
        return False
    if _normalized_title(text) in rules.FIXED_L1_TITLES:
        return True
    level_one_pattern = rules.HEADING_PATTERNS[0][1]
    return bool(level_one_pattern.match(text))


def _anchor_headerless_units(
    units: list[UnitDraft], *, document_title: str | None = None, stats: BuildStats
) -> list[UnitDraft]:
    """Anchor pre-first-heading units under a stable synthetic heading.

    Announcement-header remnants and letterhead sit before the
    first in-document heading and used to surface with heading_path=[], which
    breaks L2 retrieval and replay anchoring (round3 P0#4). Fully flat
    documents (no headings anywhere) are left untouched — inventing structure
    there would be worse than none.
    """

    fully_flat = not any(unit.heading_path for unit in units)
    if fully_flat and not document_title:
        return units
    qa_form_anchor = _headerless_qa_form_anchor(units)
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
        # Pre-heading pure letterhead remnants drop HERE, before the
        # short-document collapse can absorb them as mixed-unit parts where
        # the late standalone-noise stage cannot see them. 公告编号 is unique
        # source evidence, so it is excluded from the noise family and merged
        # losslessly into the next content unit later. Long real content that
        # merely sits before the first heading also keeps anchoring.
        if (
            unit.payload_kind == "text"
            and "image_ref" not in unit.payload
            and rules.is_standalone_noise(str(unit.payload.get("text", "")))
        ):
            stats.dropped_by_kind["standalone_noise"] += 1
            continue
        stats.anchored_header_units += 1
        if unit.payload_kind == "qa":
            # A QA title is the leaf question, never its own ancestor. Prefer
            # the official form's narrative label when it is present in the
            # preserved table; otherwise the registered document title is the
            # only truthful document-level address.
            anchor = qa_form_anchor or document_title or rules.DOCUMENT_HEADER_ANCHOR
        else:
            anchor = unit.title or document_title or rules.DOCUMENT_HEADER_ANCHOR
        out.append(
            UnitDraft(
                **{
                    **unit.__dict__,
                    "heading_path": [anchor],
                    "structural_path": [anchor],
                    "title": unit.title or anchor,
                }
            )
        )
    return out


def _headerless_qa_form_anchor(units: list[UnitDraft]) -> str | None:
    for unit in units:
        values: list[Any] = []
        if unit.payload_kind == "table":
            values.extend(unit.payload.get("headers") or [])
            values.extend(
                value for row in unit.payload.get("rows") or [] for value in row
            )
        elif unit.payload_kind == "text":
            values.append(unit.payload.get("text") or "")
        for value in values:
            for line in str(value).splitlines():
                if rules.QA_FORM_NARRATIVE_LABEL_RE.fullmatch(line.strip()):
                    return "投资者关系活动主要内容介绍"
    return None


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
            "parts": [_unit_part(member, include_heading=False) for member in members],
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
                "parts": [_unit_part(unit, include_heading=True) for unit in real],
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
    """Merge technical slices inside a stable business boundary.

    Structure chooses the boundary; size only limits it.  The former
    shallowest-subtree-under-8k heuristic merged explicit sibling topics into
    whole chapters and changed boundaries abruptly when one year's text crossed
    8k.  Keep at least a second-level business heading when present, descend to
    the deepest controlled note heading, and use 8k solely as a hard cap.
    ``qa``/already-``mixed`` units remain atomic.
    """

    out: list[UnitDraft] = []
    group: list[UnitDraft] = []
    group_boundary: _SectionBoundary | None = None
    group_chars = 0

    def close() -> None:
        nonlocal group, group_boundary, group_chars
        if group:
            if group_boundary is None:  # pragma: no cover - internal invariant
                raise AssertionError("section group has no boundary")
            out.extend(
                _section_group_to_units(
                    group,
                    group_boundary,
                    filing_type=filing_type,
                    stats=stats,
                )
            )
            group = []
            group_boundary = None
            group_chars = 0

    for unit in units:
        boundary = _section_boundary_for_unit(unit)
        if boundary is None:
            close()
            out.append(unit)
            continue
        chars = len(_main_text(unit))
        # An already-atomic oversized slice cannot be made safer by wrapping it
        # in a mixed unit.  Preserve it for L2 windowing and never let adjacent
        # slices push the group further past the cap.
        if chars > rules.SECTION_GROUP_MAX_CHARS:
            close()
            out.append(_reanchor_section_unit(unit, boundary))
            continue
        if group_boundary is None or boundary.identity != group_boundary.identity:
            close()
            group_boundary = boundary
        elif group and (
            _starts_repeated_business_instance(unit)
            or group_chars + (1 if group_chars and chars else 0) + chars
            > rules.SECTION_GROUP_MAX_CHARS
            or len(group) >= rules.SECTION_GROUP_MAX_PARTS
        ):
            close()
            group_boundary = boundary
        separator_chars = 1 if group and group_chars and chars else 0
        group.append(unit)
        group_chars += separator_chars + chars
    close()
    return out


def _section_boundary_for_unit(unit: UnitDraft) -> _SectionBoundary | None:
    """Choose a semantic boundary without conflating it with public depth.

    A controlled table caption can be a sibling of the stale heading_path
    inherited from the preceding table. Reanchor that caption deterministically.
    A text leaf deeper than the four-level public path remains part of the
    internal identity so adjacent deep topics never merge back together in S8.
    """

    if (
        unit.payload_kind in {"qa", "mixed"}
        or unit.quality_status != "ok"
        or not (unit.heading_path or unit.structural_path)
    ):
        return None
    original_path = list(unit.heading_path)
    structural_path = list(unit.structural_path or original_path)
    policy_boundary = _accounting_policy_subject_boundary(
        original_path=original_path,
        structural_path=structural_path,
    )
    if policy_boundary is not None:
        return policy_boundary
    structural_title = structural_path[-1] if structural_path else None
    hidden_structural_title = (
        structural_title
        if structural_title and len(structural_path) > len(original_path)
        else None
    )
    if hidden_structural_title is not None:
        # Every carrier under a leaf hidden by the four-level public cap must
        # share the complete internal identity.  Choosing this only for tables
        # made adjacent text/table slices from one deep note look like separate
        # sections; checking title membership also failed when an ancestor and
        # the hidden leaf happened to have the same text.
        effective_path = list(original_path)
        hidden_controlled_title = next(
            (
                title
                for title in reversed(structural_path[len(original_path) :])
                if _is_controlled_boundary_title(title)
            ),
            None,
        )
        if hidden_controlled_title is not None:
            # If the deepest leaf is a local role (for example ``1) 本公司作为
            # 出租方``), project its exact controlled hidden parent into the
            # public four-slot breadcrumb.  The leaf remains the unit title and
            # complete internal identity, so no sibling detail is collapsed.
            effective_path = _path_reanchored_to_controlled_title(
                original_path, hidden_controlled_title
            )
        common_prefix_length = 0
        for structural_segment, public_segment in zip(
            structural_path, effective_path
        ):
            if _normalized_title(structural_segment) != _normalized_title(
                public_segment
            ):
                break
            common_prefix_length += 1
        return _SectionBoundary(
            identity=tuple(
                _normalized_title(_statement_stack_title(title))
                for title in structural_path
            ),
            heading_path=tuple(effective_path),
            title=hidden_structural_title,
            reanchor=(
                effective_path != original_path or unit.title != hidden_structural_title
            ),
            # When a five-level structural leaf is projected into public slot
            # four, the displaced controlled parent must remain literal local
            # evidence. Start local headings at the last common raw ancestor,
            # rather than using the projected path that is not a source prefix.
            local_heading_root=tuple(
                structural_path[:common_prefix_length]
            ),
        )

    title_candidate = unit.title
    controlled_title = (
        title_candidate if _is_controlled_boundary_title(title_candidate) else None
    )
    effective_path = list(original_path)
    reanchor = False
    if controlled_title is not None:
        effective_path = _path_reanchored_to_controlled_title(
            original_path, controlled_title
        )
        reanchor = effective_path != original_path or unit.title != controlled_title

    depth = min(2, len(effective_path))
    for index, path_title in enumerate(effective_path):
        if rules.note_key_for_title(path_title) is not None:
            depth = max(depth, index + 1)
    boundary_path = effective_path[:depth]
    boundary_title = boundary_path[-1] if boundary_path else unit.title
    identity = tuple(_normalized_title(title) for title in boundary_path)

    if controlled_title is not None:
        boundary_title = controlled_title

    return _SectionBoundary(
        identity=identity,
        heading_path=tuple(boundary_path),
        title=boundary_title,
        reanchor=reanchor,
    )


def _accounting_policy_subject_boundary(
    *,
    original_path: list[str],
    structural_path: list[str],
) -> _SectionBoundary | None:
    """Group policy sub-items at their first exact subject parent.

    Retrieval labels are intentionally richer than structural boundaries. A
    descendant such as ``2. 借款费用资本化期间`` may contain the same mapped
    phrase as ``26、借款费用``; promoting that substring hit to a new boundary
    made a vocabulary-only upgrade fragment the evidence. Inside the explicit
    accounting-policy chapter, the first exact controlled subject owns all
    deeper text/table slices. The normal 8k/24-part caps still apply, and mixed
    parts retain their local headings.
    """

    policy_index = next(
        (
            index
            for index, title in enumerate(structural_path)
            if rules.is_accounting_policy_section_title(title)
        ),
        None,
    )
    if policy_index is None:
        return None
    subject_index = next(
        (
            index
            for index in range(policy_index + 1, len(structural_path))
            if rules.exact_note_key_for_title(structural_path[index]) is not None
            and not rules.is_accounting_policy_section_title(
                structural_path[index]
            )
        ),
        None,
    )
    if subject_index is None:
        return None

    subject_path = structural_path[: subject_index + 1]
    subject_title = subject_path[-1]
    if subject_index < 4:
        public_path = subject_path
    else:
        # Match the existing four-slot controlled-title projection: keep the
        # first three document ancestors and expose the hidden business owner.
        public_path = [*subject_path[:3], subject_title]
    return _SectionBoundary(
        identity=tuple(_normalized_title(title) for title in subject_path),
        heading_path=tuple(public_path),
        title=subject_title,
        # A single child remains independently addressable. Reanchoring is
        # needed only when two or more slices actually become a mixed section.
        reanchor=False,
        # The public path may project a hidden subject into slot four.  Parts
        # still need the complete internal root so deeper (d)/(e) headings are
        # retained as local evidence instead of disappearing on prefix mismatch.
        local_heading_root=tuple(subject_path),
    )


def _is_controlled_boundary_title(title: str | None) -> bool:
    if not title:
        return False
    if rules.exact_note_key_for_title(title) is not None:
        return True
    # A numbered caption with a bounded vocabulary hit is a structural sibling
    # even when the label carries an issuer suffix such as ``情况简介``. Free-
    # form captions still require exact matching and remain inside their section.
    return bool(
        _pattern_heading_level(title) is not None
        and rules.note_key_for_title(title) is not None
    )


def _path_reanchored_to_controlled_title(path: list[str], title: str) -> list[str]:
    """Replace a stale numbered sibling or append a controlled caption."""

    normalized = _normalized_title(title)
    if any(_normalized_title(path_title) == normalized for path_title in path):
        return list(path)
    statement_key = _structural_statement_key(title)
    if statement_key is not None and any(
        _structural_statement_key(path_title) == statement_key for path_title in path
    ):
        # A continuation caption is the same statutory statement, not a new
        # child below its base title (``母公司股东权益变动表 - 续``).
        return list(path)

    pattern_level = _pattern_heading_level(title)
    if pattern_level is not None:
        target_outline = _outline_parts_for_stack_title(title)
        if target_outline is not None:
            # Decimal/dot outlines form their own document-local family.  A
            # nominal level-2 ``3.6`` must never replace a Chinese level-2
            # root such as ``五、财务报表附注`` merely because both regexes map
            # to the same pattern depth.
            for index in range(len(path) - 1, -1, -1):
                candidate_outline = _outline_parts_for_stack_title(path[index])
                if candidate_outline is None:
                    continue
                if (
                    len(candidate_outline) == len(target_outline)
                    and candidate_outline[:-1] == target_outline[:-1]
                ):
                    return [*path[:index], title]
                if target_outline[: len(candidate_outline)] == candidate_outline:
                    candidate = [*path[: index + 1], title]
                    if len(candidate) <= 4:
                        return candidate
                    # The public contract is capped at four levels.  When the
                    # numeric parent already occupies slot four, retain the
                    # first three ancestors and project the hidden leaf into
                    # the final slot instead of emitting a five-level path.
                    return [*path[:-1], title]
        else:
            for index in range(len(path) - 1, -1, -1):
                if (
                    _outline_parts_for_stack_title(path[index]) is None
                    and _pattern_heading_level(path[index]) == pattern_level
                ):
                    return [*path[:index], title]

    if len(path) < 4:
        return [*path, title]
    return [*path[:-1], title]


def _reanchor_section_unit(unit: UnitDraft, boundary: _SectionBoundary) -> UnitDraft:
    if not boundary.reanchor:
        return unit
    return UnitDraft(
        **{
            **unit.__dict__,
            "heading_path": list(boundary.heading_path),
            "title": boundary.title,
        }
    )


def _starts_repeated_business_instance(unit: UnitDraft) -> bool:
    return bool(
        unit.payload_kind == "text"
        and rules.GOODWILL_ASSET_GROUP_START_RE.match(str(unit.payload.get("text", "")))
    )


def _section_group_to_units(
    members_all: list[UnitDraft],
    boundary: _SectionBoundary,
    *,
    filing_type: str | None,
    stats: BuildStats,
) -> list[UnitDraft]:
    members = [unit for unit in members_all if not _is_blank_text_unit(unit)]
    if not members:
        return list(members_all)
    if len(members) == 1:
        return [_reanchor_section_unit(members[0], boundary)]
    stats.grouped_section_units += 1
    first = members[0]
    key = list(boundary.heading_path)
    local_heading_root = list(boundary.local_heading_root or boundary.heading_path)
    return [
        UnitDraft(
            payload_kind="mixed",
            payload={
                "semantic_type": "section",
                "parts": [
                    _unit_part(
                        member,
                        include_heading=False,
                        relative_to=local_heading_root,
                    )
                    for member in members
                ],
            },
            source_order=first.source_order,
            intra_order=first.intra_order,
            heading_path=list(key),
            title=boundary.title or (key[-1] if key else first.title),
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

    keys: set[str] = set()
    for member in members:
        keys.update(member.semantic_keys or ())
        if member.semantic_key:
            keys.add(member.semantic_key)
        keys.update(semantic_keys_for_unit(member, filing_type=filing_type))
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
    relative_path: list[str] | None = None
    if relative_to is not None:
        if len(unit.heading_path) >= len(relative_to) and all(
            _normalized_title(actual) == _normalized_title(expected)
            for actual, expected in zip(unit.heading_path, relative_to)
        ):
            relative_path = unit.heading_path
        elif len(unit.structural_path) >= len(relative_to) and all(
            _normalized_title(actual) == _normalized_title(expected)
            for actual, expected in zip(unit.structural_path, relative_to)
        ):
            relative_path = unit.structural_path
    if relative_to is not None:
        local = (
            relative_path[len(relative_to) :]
            if relative_path is not None
            else []
        )
        # Grouping must never make a previously addressable source title
        # disappear. A controlled table caption can truthfully reanchor a
        # member even when its inherited heading/structural path is stale and
        # therefore cannot share the group's exact prefix. Preserve that title
        # as local evidence; normalized boundary titles are still deduplicated.
        if unit.title and _normalized_title(unit.title) not in {
            _normalized_title(title) for title in [*relative_to, *local]
        }:
            local = [*local, unit.title]
        if local:
            part["local_heading"] = local
    if unit.applicability:
        part["applicability"] = unit.applicability
    if unit.quality_status != "ok":
        part["quality_status"] = unit.quality_status
    if unit.artifact_locator:
        # A mixed unit can contain several tables.  Keeping only the first
        # member's top-level locator loses each later table's merged-cell map
        # and makes lossless compact rendering impossible.  Per-part locators
        # are query/provenance annotations (excluded from content identity).
        part["artifact_locator"] = dict(unit.artifact_locator)
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
        for value in ((unit.artifact_locator or {}).get("page_no") for unit in units)
        if isinstance(value, int)
    ]
    if pages and min(pages) != max(pages):
        locator["page_span"] = [min(pages), max(pages)]
    return locator or None


def _unit_sort_key(unit: UnitDraft) -> tuple[int, int]:
    return (unit.source_order, unit.intra_order)


def _note_keys_for_unit(unit: UnitDraft, *, filing_type: str | None) -> list[str]:
    """章节键（含祖先继承，round13 用户裁决）：标题优先，然后沿 heading_path
    自深向浅逐级取键——"(1) 明细情况" 这类无科目语义的叶子从最近的科目祖先
    继承（19、其他非流动金融资产 → other_noncurrent_financial_assets），
    章级键（第八节 财务报告 → financial_report_chapter）一并入组。返回按
    深度有序去重的列表，首个即最具体键。"""

    # 词表键对全部文档类型开放（round13：审计报告等 'other' 文档同样承载
    # 报表与附注结构；标题匹配有界，误配风险可控且被类扫描看护）。
    keys: list[str] = []
    candidates = [
        unit.title,
        *reversed(unit.structural_path or unit.heading_path),
        *reversed(unit.heading_path),
    ]
    for candidate in candidates:
        key = rules.note_key_for_title(candidate)
        if key and key not in keys:
            keys.append(key)
    return keys


def _note_key_for_unit(unit: UnitDraft, *, filing_type: str | None) -> str | None:
    keys = _note_keys_for_unit(unit, filing_type=filing_type)
    return keys[0] if keys else None


def semantic_keys_for_unit(unit: UnitDraft, *, filing_type: str | None) -> list[str]:
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
    keys: list[str] = []
    for rule in rules.SEMANTIC_KEY_RULES:
        if (
            rule.filing_type_limited
            and filing_type not in rules.SEMANTIC_LIMITED_FILING_TYPES
        ):
            continue
        if all(token in text for token in rule.required) and (
            not rule.any_required or any(token in text for token in rule.any_required)
        ):
            if rule.semantic_key not in keys:
                keys.append(rule.semantic_key)
    note_keys = _note_keys_for_unit(unit, filing_type=filing_type)
    if (
        _has_market_risk_definition_evidence(unit, note_keys=note_keys)
        and "market_risk" not in keys
    ):
        keys.append("market_risk")
    return keys


_MARKET_RISK_DEFINITION_PREFIXES = (
    "金融工具的市场风险，是指",
    "市场风险，是指金融工具",
)


def _has_market_risk_definition_evidence(
    unit: UnitDraft, *, note_keys: Iterable[str]
) -> bool:
    """Recover a lost market-risk route from its statutory definition.

    Some source PDFs misnumber the market-risk heading, so S2 truthfully keeps
    the printed hierarchy but cannot infer the intended ordinal.  The first
    body sentence remains authoritative: only a financial-instrument-risk
    ancestor plus an exact definition prefix may add the narrower key.
    Ordinary narrative mentions never qualify.
    """

    if "financial_instrument_risk" not in set(note_keys):
        return False
    text = _main_text(unit).lstrip()
    return any(text.startswith(prefix) for prefix in _MARKET_RISK_DEFINITION_PREFIXES)


def semantic_key_for_unit(unit: UnitDraft, *, filing_type: str | None) -> str | None:
    keys = semantic_keys_for_unit(unit, filing_type=filing_type)
    return keys[0] if keys else None


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
    for key in (
        "source",
        "source_order_index",
        "page_span",
        "native_text_hash",
        "page_bboxes",
        "model_table_indices",
        "continuation_source_item_indices",
        "table_locator_algorithm",
    ):
        if element.get(key) is not None:
            locator[key] = element.get(key)
    return locator


def _heading_left(element: PreparedElement) -> float | None:
    bbox = (element.artifact_locator or {}).get("bbox")
    if not isinstance(bbox, (list, tuple)) or not bbox:
        return None
    try:
        return float(bbox[0])
    except (TypeError, ValueError):
        return None


def _heading_level_for(element: PreparedElement) -> int | None:
    text = (element.text or "").strip()
    if not text:
        return None
    if element.raw_kind == "recovered_parameter_list_item":
        return None
    if text.endswith(("?", "？")) or rules.QUESTION_START_RE.match(text):
        return None
    # MinerU occasionally tags applicability markers or yes/no checkbox
    # answers with text_level>=1; a declaration line must never enter the
    # heading tree (observed polluting heading_path/title in the real annual
    # corpus, 2026-07-06).
    if rules.is_declaration_line(text):
        return None
    if _is_explicit_qa_section_marker(element):
        # The generic bare-label noise family also matches short labels such
        # as ``二、问答环节：``.  Here the label is an explicit transcript
        # boundary: keep it in the structural tree so it flushes the preamble
        # and gives subsequent derived QA a stable owner.
        for level, pattern in rules.HEADING_PATTERNS:
            if pattern.match(text):
                return level
        if element.kind == "heading" and element.heading_level is not None:
            return max(1, min(5, element.heading_level))
    # A MinerU h1 such as ``2024年度`` is still standalone noise.  Let the
    # later counted filter drop its unit, but never let it replace the open
    # financial-statement branch first.
    if rules.is_standalone_noise(text):
        return None
    # Table footnotes ([注1] …, 注：…) belong to the preceding table and must
    # never become headings (Codex round5: promoted to a unit title).
    if rules.FOOTNOTE_LINE_RE.match(text):
        return None
    if _structural_statement_key(text) is not None and _text_heading_candidate(text):
        return 1
    decimal_outline = _decimal_outline_parts(text)
    if decimal_outline is not None:
        if element.kind == "text" and not _text_heading_candidate(text):
            return None
        return len(decimal_outline)
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
    if re.match(r"^(?:[一二三四五六七八九十百]{1,3}|\d{1,3})\s+\S", stripped) and (
        rules.exact_note_key_for_title(stripped) is None
        and _structural_statement_key(stripped) is None
    ):
        # A plain space is too weak to promote text-kind evidence: both
        # ``一 持续加强研发`` and ``1 亿元投资额`` are common prose. Preserve
        # these as content unless the complete label is in the controlled
        # structural vocabulary. Parser-declared headings remain unaffected,
        # and real ``5 公司债券情况`` is admitted by its exact key.
        return False
    return (
        "\n" not in stripped
        and len(stripped) <= 40
        and not stripped.endswith(("。", "；", "，", ","))
    )


def _normalized_title(text: str) -> str:
    return re.sub(r"\s+", "", text).rstrip("：:")


_SKIP_SECTION_PREFIX_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百]+[节章]|"
    r"[一二三四五六七八九十百]+、|"
    r"\d{1,3}[、.．]|"
    r"[（(](?:\d{1,3}|[一二三四五六七八九十百]+)[）)])"
)

_TOC_PAGE_REFERENCE_RE = re.compile(
    r"^(?=.{2,160}$)(?:"
    r".+?(?:\.{3,}|…{2,}|·{3,})\s*\d{1,4}|"
    r"第[一二三四五六七八九十百0-9]+[章节]\s+.{1,100}\s+\d{1,4}"
    r")\s*$"
)


def _is_toc_page_reference(text: str) -> bool:
    """Return true only for a complete TOC row ending in a page number.

    This shape is structural noise only while an explicit skip section is
    open. Outside that context the caller must preserve the source verbatim.
    """

    return "\n" not in text and _TOC_PAGE_REFERENCE_RE.fullmatch(text) is not None


def _skip_section_title(text: str) -> str | None:
    normalized = _normalized_title(text)
    core = _SKIP_SECTION_PREFIX_RE.sub("", normalized).lstrip("、.．)）")
    if core in rules.SKIP_SECTION_TITLES:
        return core
    if normalized in rules.SKIP_SECTION_TITLES:
        return normalized
    return None


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
        if applicability is None and index == 0 and rules.is_pure_marker_line(line):
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


_QA_YEAR_PREFIXED_ORDINAL_RE = re.compile(
    r"^\s*(?P<ordinal>\d{1,3})\."
    r"(?=(?:19|20)\d{2}\s*年[^\n]{2,180}[？?])"
)


def _qa_lines(text: str) -> list[str]:
    # PDF extraction often inserts horizontal spacing inside a single label
    # (``问题 2、...``).  Keep that marker+ordinal atomic before the generic
    # numbered-list splitter; otherwise it creates a naked ``问题`` line that
    # is later mistaken for the preceding answer.  Do not join across real
    # line breaks: ``以下问题\n1、成本高`` is an answer list, not proven QA.
    prepared = re.sub(
        r"(?P<label>问题|问|投资者提问|提问)[ \t\u3000]+"
        r"(?=\d+(?:[、．：:]|\.(?!\d)))",
        r"\g<label>",
        text,
    )
    prepared = re.sub(
        # ``43.2024年……？`` is an outer ordinal in this proven QA shape,
        # while 15.7% / 2.0 remain decimals. Insert only a logical line break;
        # never rewrite the source delimiter stored in payload.raw_text.
        r"(?<!^)(?<![A-Za-z0-9])"
        r"(?=\d{1,3}\.(?=(?:19|20)\d{2}\s*年[^\n]{2,180}[？?]))",
        "\n",
        prepared,
    )
    prepared = re.sub(
        # Never split identifiers such as Q4/P4/V12.  The former pattern only
        # protected a preceding digit, producing both false questions and a
        # naked ``Q`` suffix in the preceding answer.
        r"(?<!^)(?<![A-Za-z\d])(?<!问题)(?<!问)(?<!\d[.．])"
        r"(?=\d+(?:[、．]|\.(?!\d))\s*)",
        "\n",
        prepared,
    )
    prepared = re.sub(
        # A bare numeric colon is a question boundary only when the whole
        # bounded line is interrogative; this excludes times such as 15:30.
        r"(?<!^)(?<![A-Za-z\d])(?<!问题)(?<!问)"
        r"(?=\d{1,3}[：:]\s*(?:【|[^\n]{2,180}[？?]\s*(?:\n|$)))",
        "\n",
        prepared,
    )
    prepared = re.sub(
        r"(?<!^)(?=(?:Q|q)\s*\d+[、．：:]|(?:Q|q)\s*\d+\.(?!\d))",
        "\n",
        prepared,
    )
    prepared = re.sub(
        r"(?<!^)(?=(?:问题|问|投资者提问|提问)\s*\d+\s*[、.．：:])",
        "\n",
        prepared,
    )
    prepared = re.sub(
        r"(?<!^)(?=【\s*(?:提问|问题)\s*\d{1,3}"
        r"[^】\n]{0,100}】\s*[：:])",
        "\n",
        prepared,
    )
    prepared = re.sub(
        r"(?<!^)(?<![A-Za-z0-9\u4e00-\u9fff])"
        r"(?=(?:(?:Q|A)\s*\d*|问题\s*\d*|问|投资者提问|提问|"
        r"答|回复|公司回复)\s*[：:])",
        "\n",
        prepared,
        flags=re.IGNORECASE,
    )
    prepared = re.sub(
        r"([？?])(?=\s*(答|回复|公司回复|A\d*)\s*[：:])",
        "\\1\n",
        prepared,
    )
    prepared = rules.INLINE_ANSWER_BOUNDARY_RE.sub("\n", prepared)
    return prepared.splitlines()


def _join_wrapped_lines(lines: list[str]) -> str:
    """Join PDF hard wraps without inventing spaces inside Chinese words."""

    joined = ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if (
            joined
            and re.search(r"[A-Za-z0-9]$", joined)
            and re.match(r"^[A-Za-z0-9]", line)
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
    stripped = text.strip()
    return bool(
        re.match(r"^\s*\d+(?:[、．]|\.(?!\d))\s*", stripped)
        or _QA_YEAR_PREFIXED_ORDINAL_RE.match(stripped)
    )


def _qa_physical_question_heading(element: PreparedElement) -> bool:
    """Recognize two complete QA-only physical boundary shapes.

    A spaced exact ``问题 N：...？`` text carrier is itself a physical MinerU
    boundary even when no heading level survived.  Bare numeric-colon syntax
    is narrower: it must be an original parser heading.  Ordinary prose and
    time/ratio-like numeric colons therefore remain untouched.
    """

    if element.raw_kind != "text":
        return False
    text = (element.text or "").strip()
    if re.fullmatch(
        r"问题[ \t\u3000]+\d{1,3}\s*[：:]\s*[^\n]{2,2000}[？?]",
        text,
    ):
        return True
    return bool(
        element.kind == "heading"
        and element.heading_level is not None
        and re.fullmatch(
            r"\d{1,3}\s*[：:]\s*(?!\d)[^\n]{2,2000}[？?]",
            text,
        )
    )


def _qa_numbered_line(text: str) -> bool:
    stripped = text.strip()
    return bool(
        re.match(
            r"^\s*(?:Q\s*)?\d+(?:[、．]|\.(?!\d))\s*",
            stripped,
            re.IGNORECASE,
        )
        or re.match(r"^\s*\d+[：:]\s*(?:【|[^\n]{2,180}[？?]\s*$)", stripped)
        or _QA_YEAR_PREFIXED_ORDINAL_RE.match(stripped)
    )


def _qa_ordinal(text: str) -> int | None:
    bracketed = rules.BRACKET_QUESTION_START_RE.match(text.strip())
    if bracketed is not None:
        return int(bracketed.group("ordinal"))
    labelled = re.match(
        r"^\s*(?:问题|问|投资者提问|提问)\s*(\d+)"
        r"(?:[、．]|\.(?!\d)|[：:])",
        text.strip(),
    )
    if labelled is not None:
        return int(labelled.group(1))
    match = re.match(
        r"^\s*Q\s*(\d+)(?:[、．]|\.(?!\d)|[：:])",
        text.strip(),
        re.IGNORECASE,
    )
    if match is not None:
        return int(match.group(1))
    bare_colon = re.match(
        r"^\s*(\d+)[：:]\s*(?:【|[^\n]{2,180}[？?]\s*$)", text.strip()
    )
    if bare_colon is not None:
        return int(bare_colon.group(1))
    year_prefixed = _QA_YEAR_PREFIXED_ORDINAL_RE.match(text.strip())
    if year_prefixed is not None:
        return int(year_prefixed.group("ordinal"))
    return _heading_ordinal(text)


def _strip_question_prefix(text: str) -> str:
    bracketed = rules.BRACKET_QUESTION_START_RE.match(text.strip())
    if bracketed is not None:
        return bracketed.group("question").strip()
    text = _QA_YEAR_PREFIXED_ORDINAL_RE.sub("", text, count=1)
    text = re.sub(
        r"^\s*(?:问题|问|投资者提问|提问)\s*\d+\s*[、.．]\s*",
        "",
        text,
    )
    text = re.sub(r"^\s*(问题|问|Q\d*|投资者提问|提问)\s*\d*\s*[：:]\s*", "", text)
    text = re.sub(r"^\s*Q\s*\d+\s*[、.．]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\d+[、.．]\s*", "", text)
    text = re.sub(r"^\s*\d+[：:]\s*(?=(?:【|[^\n]{2,180}[？?]\s*$))", "", text)
    return text.strip()


def _strip_answer_prefix(text: str) -> str:
    text = rules.BRACKET_SPEAKER_ANSWER_RE.sub("", text, count=1)
    return re.sub(r"^\s*(答|回复|公司回复|A\d*)\s*[：:]\s*", "", text).strip()


def _is_empty_table_element(element: PreparedElement) -> bool:
    if element.kind != "table":
        return False
    table = element.table or {}
    return (
        not table.get("headers")
        and not table.get("rows")
        and not (element.table_html or "")
    )


def _column_count(element: PreparedElement) -> int | None:
    table = element.table or {}
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    if headers:
        return len(headers)
    if rows:
        return len(rows[0])
    return None


def _can_merge_continued_table(
    previous: PreparedElement, current: PreparedElement
) -> bool:
    continued_statement_caption = bool(
        current.table_caption
        and _STATEMENT_CONTINUATION_RE.search(current.table_caption[0])
        and _structural_statement_key(current.table_caption[0]) is not None
    )
    if current.table_caption and not continued_statement_caption:
        return False
    # A page-spillover continuation never crosses a section boundary. Note
    # tables share one shape (项目/本期/上期), so column count alone merged
    # adjacent DIFFERENT notes once cn_a_v6 let their headings enter the
    # stack (no text element left between the tables) — 3. 销售费用's table
    # was absorbed into 1. 营业收入 and the heading vanished from every path
    # (ub-2026.07-18, swallowed-heading audit).
    if _prepared_section_identity(previous) != _prepared_section_identity(current):
        return False
    previous_count = _column_count(previous)
    current_count = _column_count(current)
    return previous_count is not None and previous_count == current_count


def _prepared_section_identity(element: PreparedElement) -> tuple[str, ...]:
    """Return the uncapped structural identity used for table continuation."""

    path = list(element.structural_path or element.heading_path)
    title = (element.title or "").strip()
    if title and (
        not path
        or _normalized_title(_statement_stack_title(path[-1]))
        != _normalized_title(_statement_stack_title(title))
    ):
        path.append(title)
    return tuple(
        _normalized_title(_statement_stack_title(part)) for part in path if part.strip()
    )


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
            structural_path=list(first.structural_path),
            title=_table_title(first),
            quality_status="needs_review",
            artifact_locator=first.artifact_locator,
        )

    headers, rows, merged_cells = _merged_table_grid(group)
    rows, merged_cells = _drop_blank_rows_adjusting(
        headers, rows, merged_cells, stats
    )
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
        structural_path=list(first.structural_path),
        title=_table_title(first),
        quality_status="ok",
        artifact_locator=locator or None,
    )


def _merged_table_grid(
    group: list[PreparedElement],
) -> tuple[list[str], list[list[str]], list[dict[str, int]]]:
    return merge_table_grids([element.table or {} for element in group])


def _merged_notes(group: list[PreparedElement]) -> list[str]:
    notes: list[str] = []
    for element in group:
        notes.extend(element.table_footnote)
    return notes


def _table_title(element: PreparedElement) -> str | None:
    captions = [caption.strip() for caption in element.table_caption if caption.strip()]
    for caption in captions:
        if not rules.is_unit_declaration_line(caption):
            return caption
    # A unit/currency declaration is table metadata, not its business title.
    # Preferring it erased the deepest structural leaf from mixed payloads
    # (招商银行: ``(b) 损失准备变动情况`` became ``单位：人民币百万元``),
    # weakening both lexical and embedding retrieval.  The declaration stays
    # losslessly in payload.caption/unit; use it as title only when no heading
    # exists at all.
    return element.title or (captions[0] if captions else None)


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
        if title is not None and (skip_title := _skip_section_title(title)) is not None:
            return skip_title
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
        return str(unit.payload.get("question") or "") + str(
            unit.payload.get("answer") or ""
        )
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
            filter(
                None, (str(part.get("caption") or ""), str(part.get("context") or ""))
            )
        )
    return str(part.get("text") or "")


def _table_caption_first(unit: UnitDraft) -> str:
    caption = unit.payload.get("caption") if unit.payload_kind == "table" else None
    if isinstance(caption, list) and caption:
        return str(caption[0])
    return ""


def _qa_table_grid_text(values: Iterable[Any]) -> str:
    """Linearize one table grid once, collapsing merged-cell expansion.

    MinerU repeats a rowspan/colspan value into every covered column.  Feeding
    three identical packed transcripts to S4 makes a valid trailing question
    collide with the first question of the duplicate copy and invalidates the
    row.  Exact per-grid deduplication is safe for the derived QA view; the
    original table payload remains byte-for-byte preserved.
    """

    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        kept.append(text)
    return "\n".join(kept)


def _qa_units_from_tables(table_units: Iterable[UnitDraft]) -> list[UnitDraft]:
    units: list[UnitDraft] = []
    for table in table_units:
        if table.payload_kind != "table" or table.quality_status == "unusable":
            continue
        text_blocks = []
        headers = table.payload.get("headers", [])
        if headers:
            # MinerU promotes a continued one-cell narrative row to table
            # headers.  Ignoring headers hid packed Q25/Q26 and Q4/Q5 in real
            # IR forms even though both carried explicit answer markers.
            text_blocks.append(_qa_table_grid_text(headers))
        text_blocks.extend(
            _qa_table_grid_text(row) for row in table.payload.get("rows", [])
        )
        for offset, text in enumerate(text_blocks, start=100):
            result = s4_build_qa_units(
                text,
                source=table,
                # Tables are structurally lossy: adjacent investor-question
                # cells can look like an unlabelled Q+A pair.  Only an explicit
                # 答/回复 marker can authorize a derived table QA.
                require_explicit_answer=True,
            )
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


_OFFICIAL_FORM_Q1_Q2_LABEL_ORDER = (
    ("投资者关系活动类别",),
    ("活动参与人员", "参与单位名称及人员姓名", "参与单位名称"),
    ("时间",),
    ("地点",),
    ("形式", "交流形式"),
    ("投资者关系活动主要内容介绍",),
)
_OFFICIAL_FORM_UNLABELLED_Q1_Q2_RE = re.compile(
    r"^\s*问题\s*1\s*[：:]\s*(?P<q1>.+?[？?])"
    r"(?P<a1>感谢您的提问。.+?[。！？!?])"
    r"问题\s*2\s*[：:]\s*(?P<q2_prefix>.+?)\s*$",
    re.DOTALL,
)


def _recover_official_form_unlabelled_q1_q2(
    units: list[UnitDraft], *, raw_elements: list[dict[str, Any]]
) -> list[UnitDraft]:
    """Recover one strictly proven official-form Q1/Q2 page-loss shape.

    MinerU can attach the visual second-page narrative to the first-page form
    table, emit that second page as an empty table ghost, and resume Q2 in a
    later text carrier.  This repair requires the complete ordered form-field
    family, Q1/Q2 with the exact unlabelled courtesy opener, one intervening
    empty page table, and a following physical Q3.  It adds a derived QA view
    only; every source table/text carrier remains published for audit.
    """

    existing_ordinals = {
        ordinal
        for unit in units
        if unit.payload_kind == "qa"
        if (ordinal := _qa_raw_ordinal(unit)) is not None
    }
    if 1 in existing_ordinals or 2 in existing_ordinals:
        return units

    ordered_units = sorted(units, key=_unit_sort_key)
    ordered_raw = sorted(
        raw_elements, key=lambda item: int(item.get("order_index", 0))
    )
    for table_index, table_unit in enumerate(ordered_units):
        if table_unit.payload_kind != "table":
            continue
        grids = [
            [str(value) for value in table_unit.payload.get("headers") or []],
            *[
                [str(value) for value in row]
                for row in table_unit.payload.get("rows") or []
            ],
        ]
        if len(grids) != len(_OFFICIAL_FORM_Q1_Q2_LABEL_ORDER) or any(
            len(row) != 2 for row in grids
        ):
            continue
        labels = [_comparison_text(row[0]) for row in grids]
        if any(
            label not in {_comparison_text(value) for value in allowed}
            for label, allowed in zip(
                labels, _OFFICIAL_FORM_Q1_Q2_LABEL_ORDER, strict=True
            )
        ):
            continue
        narrative = grids[-1][1].strip()
        match = _OFFICIAL_FORM_UNLABELLED_Q1_Q2_RE.fullmatch(narrative)
        if match is None:
            continue
        q1 = match.group("q1").strip()
        a1 = match.group("a1").strip()
        q2_prefix = match.group("q2_prefix").strip()
        if (
            sum(q1.count(mark) for mark in ("?", "？")) != 1
            or not 20 <= len(_comparison_text(q1)) <= 500
            or not 30 <= len(_comparison_text(a1)) <= 4000
            or not a1.startswith("感谢您的提问。")
            or not re.search(r"[。！？!?]$", a1)
            or not 20 <= len(_comparison_text(q2_prefix)) <= 500
            or any(mark in q2_prefix for mark in ("?", "？"))
            or re.search(r"[。！？!?；;：:]$", q2_prefix)
        ):
            continue

        follower = next(
            (
                unit
                for unit in ordered_units[table_index + 1 :]
                if unit.source_order > table_unit.source_order
                and not _is_blank_text_unit(unit)
                and not (
                    unit.payload_kind == "table"
                    and _table_payload_is_empty(unit.payload)
                )
            ),
            None,
        )
        if follower is None or follower.payload_kind != "text":
            continue
        follower_lines = [
            line.strip()
            for line in _main_text(follower).splitlines()
            if line.strip()
        ]
        if len(follower_lines) < 2:
            continue
        q2_tail = follower_lines[0]
        a2 = "\n".join(follower_lines[1:]).strip()
        q2 = _join_wrapped_lines([q2_prefix, q2_tail])
        if (
            not q2.endswith(("?", "？"))
            or sum(q2.count(mark) for mark in ("?", "？")) != 1
            or not a2.startswith("感谢您的提问。")
            or not 30 <= len(_comparison_text(a2)) <= 6000
            or not re.search(r"[。！？!?]$", a2)
        ):
            continue
        next_business = next(
            (
                unit
                for unit in ordered_units
                if unit.source_order > follower.source_order
                and not _is_blank_text_unit(unit)
                and not (
                    unit.payload_kind == "table"
                    and _table_payload_is_empty(unit.payload)
                )
            ),
            None,
        )
        if (
            next_business is None
            or next_business.payload_kind != "qa"
            or _qa_raw_ordinal(next_business) != 3
            or not 0 < next_business.source_order - follower.source_order <= 64
        ):
            continue

        between = [
            element
            for element in ordered_raw
            if table_unit.source_order
            < int(element.get("order_index", -1))
            < follower.source_order
        ]
        empty_tables = [
            element
            for element in between
            if str(element.get("kind") or "") == "table"
            and not str(element.get("table_html") or "").strip()
            and not element.get("table_parse_failed")
            and _raw_table_is_empty(element)
        ]
        if len(empty_tables) != 1 or any(
            str(element.get("kind") or "") != "page_furniture"
            and element not in empty_tables
            for element in between
        ):
            continue
        table_page = _locator_page_no(table_unit)
        empty_page = _int_or_none(empty_tables[0].get("page_no"))
        follower_page = _locator_page_no(follower)
        if (
            table_page is None
            or empty_page != table_page + 1
            or follower_page != empty_page + 1
        ):
            continue

        answer_elements = [
            element
            for element in ordered_raw
            if follower.source_order
            <= int(element.get("order_index", -1))
            < next_business.source_order
        ]
        if not answer_elements or any(
            str(element.get("kind") or "") not in {"text", "page_furniture"}
            for element in answer_elements
        ):
            continue
        raw_answer_text = "\n".join(
            str(element.get("text") or "")
            for element in answer_elements
            if str(element.get("kind") or "") == "text"
        )
        if _comparison_text(raw_answer_text) != _comparison_text(_main_text(follower)):
            continue
        answer_orders = [
            int(element.get("order_index", -1))
            for element in answer_elements
            if str(element.get("kind") or "") == "text"
        ]
        answer_pages = [
            page
            for element in answer_elements
            if str(element.get("kind") or "") == "text"
            if (page := _int_or_none(element.get("page_no"))) is not None
        ]
        if not answer_orders or not answer_pages:
            continue

        q1_locator = dict(table_unit.artifact_locator or {})
        q1_locator["page_span"] = [table_page, empty_page]
        q1_locator["source_order_span"] = [
            table_unit.source_order,
            int(empty_tables[0].get("order_index", table_unit.source_order)),
        ]
        q1_locator["merge_reason"] = "official_form_unlabelled_q1_page_ghost"
        q2_locator = dict(_merged_locator([table_unit, follower]) or {})
        q2_locator["page_span"] = [table_page, max(answer_pages)]
        q2_locator["source_order_span"] = [
            table_unit.source_order,
            max(answer_orders),
        ]
        q2_locator["merge_reason"] = "official_form_unlabelled_q2_page_seam"

        table_position = ordered_units.index(table_unit)
        follower_position = ordered_units.index(follower)
        ordered_units[table_position] = UnitDraft(
            **{**table_unit.__dict__, "quality_status": "needs_review"}
        )
        ordered_units[follower_position] = UnitDraft(
            **{**follower.__dict__, "quality_status": "needs_review"}
        )
        recovered = [
            UnitDraft(
                payload_kind="qa",
                payload={
                    "question": q1,
                    "answer": a1,
                    "raw_text": f"问题1：{q1}\n{a1}",
                },
                source_order=table_unit.source_order,
                intra_order=6000,
                heading_path=list(table_unit.heading_path),
                structural_path=list(table_unit.structural_path),
                title=q1,
                quality_status="needs_review",
                artifact_locator=q1_locator,
            ),
            UnitDraft(
                payload_kind="qa",
                payload={
                    "question": q2,
                    "answer": a2,
                    "raw_text": f"问题2：{q2}\n{a2}",
                },
                source_order=table_unit.source_order,
                intra_order=6001,
                heading_path=list(table_unit.heading_path),
                structural_path=list(table_unit.structural_path),
                title=q2,
                quality_status="needs_review",
                artifact_locator=q2_locator,
            ),
        ]
        return [*ordered_units, *recovered]
    return units


def _qa_logical_table_text(table: UnitDraft) -> tuple[str, bool]:
    """Return the narrative part of one official-form table.

    The public table unit remains unchanged.  This projection exists only for
    cross-carrier QA parsing and stops before the exchange form's footer rows,
    which otherwise look like answer prose after a page-spill header.
    """

    captions = [
        str(value).strip() for value in table.payload.get("caption") or [] if value
    ]
    if any(rules.ATTACHMENT_CAPTION_RE.match(value) for value in captions):
        return "", True
    if any(
        rules.ATTACHMENT_CAPTION_RE.match(value)
        for value in [table.title or "", *table.heading_path]
        if value
    ):
        return "", True

    blocks: list[str] = []
    headers = table.payload.get("headers") or []
    if headers:
        header_text = _qa_table_grid_text(headers)
        if header_text:
            blocks.append(header_text)

    ends_run = False
    for row in table.payload.get("rows") or []:
        first = next((str(value).strip() for value in row if str(value).strip()), "")
        if first and rules.QA_FORM_FOOTER_FIELD_RE.fullmatch(first):
            ends_run = True
            break
        row_text = _qa_table_grid_text(row)
        if row_text:
            blocks.append(row_text)
    return "\n".join(blocks), ends_run


def _qa_text_before_form_boundary(text: str) -> tuple[str, bool]:
    kept: list[str] = []
    ends_run = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and (
            rules.QA_FORM_FOOTER_FIELD_RE.fullmatch(stripped)
            or rules.ATTACHMENT_CAPTION_RE.match(stripped)
        ):
            ends_run = True
            break
        kept.append(line)
    return "\n".join(kept).strip(), ends_run


def _qa_logical_carriers(units: list[UnitDraft]) -> list[_QaLogicalCarrier]:
    """Reconstruct one transcript-only carrier per physical source order."""

    grouped: dict[int, list[UnitDraft]] = {}
    for unit in sorted(units, key=_unit_sort_key):
        grouped.setdefault(unit.source_order, []).append(unit)

    carriers: list[_QaLogicalCarrier] = []
    for source_order, source_units in grouped.items():
        usable = [unit for unit in source_units if unit.quality_status != "unusable"]
        if not usable:
            continue
        table = next((unit for unit in usable if unit.payload_kind == "table"), None)
        boundaries = tuple(
            dict.fromkeys(
                value
                for unit in usable
                for value in unit.qa_question_boundaries
                if value
            )
        )
        if table is not None:
            text, ends_run = _qa_logical_table_text(table)
            context = table
        else:
            context = next(
                (unit for unit in usable if unit.payload_kind != "qa"), usable[0]
            )
            pieces: list[str] = []
            seen: set[str] = set()
            for unit in usable:
                if unit.payload_kind == "qa":
                    value = str(unit.payload.get("raw_text") or "").strip()
                elif unit.payload_kind == "text":
                    value = _main_text(unit).strip()
                else:
                    continue
                key = _comparison_text(value)
                if value and key and key not in seen:
                    seen.add(key)
                    pieces.append(value)
            text = "\n".join(pieces)

            # A MinerU heading can be the middle fragment of a question. S2
            # correctly keeps it as structure for ordinary documents, so add
            # it only to this private transcript projection when it is visibly
            # interrogative and absent from the following answer carrier.
            title = (context.title or "").strip()
            if (
                title
                and title.endswith(("?", "？"))
                and _comparison_text(title) not in _comparison_text(text)
            ):
                text = "\n".join(filter(None, [title, text]))
            text, ends_run = _qa_text_before_form_boundary(text)

        if not text and not ends_run:
            continue
        carriers.append(
            _QaLogicalCarrier(
                source_order=source_order,
                text=text,
                context=context,
                question_boundaries=boundaries,
                has_existing_qa=any(unit.payload_kind == "qa" for unit in usable),
                ends_run=ends_run,
            )
        )
    return carriers


def _qa_logical_carrier_has_signal(carrier: _QaLogicalCarrier) -> bool:
    if carrier.has_existing_qa or carrier.question_boundaries:
        return True
    return any(
        rules.ANSWER_START_RE.match(line.strip())
        or rules.EXPLICIT_QUESTION_START_RE.match(line.strip())
        or rules.QUESTION_START_RE.match(line.strip())
        or rules.QA_COMPOUND_QUESTION_INTRO_RE.match(line.strip())
        for line in _qa_lines(carrier.text)
        if line.strip()
    )


def _locator_page_no(unit: UnitDraft, *, end: bool = False) -> int | None:
    locator = unit.artifact_locator or {}
    span = locator.get("page_span")
    if (
        isinstance(span, list)
        and len(span) == 2
        and all(isinstance(value, int) for value in span)
    ):
        return int(span[1 if end else 0])
    page_no = locator.get("page_no")
    return int(page_no) if isinstance(page_no, int) else None


def _qa_unlabelled_numbered_blocks(text: str) -> list[tuple[int, str, str]]:
    """Split exact ``N、question？ answer`` blocks without guessing prose.

    This projection is used only after a surrounding physical-heading/table
    sequence has been proven by :func:`_qa_logical_unlabelled_pair_ordinals`.
    Requiring one and only one question mark keeps compound questions and
    rhetorical answer prose fail-closed.
    """

    blocks: list[tuple[int, str, str]] = []
    for raw_line in _qa_lines(text):
        line = raw_line.strip()
        ordinal = _qa_ordinal(line)
        if ordinal is None or not _qa_numbered_line(line):
            continue
        question_marks = [
            index for index, char in enumerate(line) if char in {"?", "？"}
        ]
        if len(question_marks) != 1:
            return []
        end = question_marks[0] + 1
        question = line[:end].strip()
        answer = line[end:].strip()
        if len(_comparison_text(_strip_question_prefix(question))) < 3:
            return []
        if len(_comparison_text(answer)) < 8:
            return []
        if any(
            marker.match(candidate.strip())
            for candidate in _qa_lines(answer)
            if candidate.strip()
            for marker in (
                rules.ANSWER_START_RE,
                rules.EXPLICIT_QUESTION_START_RE,
                rules.QUESTION_START_RE,
            )
        ):
            return []
        blocks.append((ordinal, question, answer))
    return blocks


def _qa_logical_unlabelled_pair_ordinals(
    previous: _QaLogicalCarrier, current: _QaLogicalCarrier
) -> list[int] | None:
    """Prove the observed heading→single-cell unlabelled-QA page spill.

    CATL 1218099701 places Q8 as a physical bottom-of-page heading, then its
    answer plus Q9/Q10/Q11 and their unlabelled answers in one next-page table
    cell.  MinerU preserved every character but lost the semantic row labels.
    The conjunction below is intentionally much narrower than "numbered text
    in a table": adjacent pages, same structural branch, exact heading
    provenance, one logical cell, and at least three consecutive question /
    answer blocks.
    """

    if (
        previous.has_existing_qa
        or previous.context.payload_kind != "text"
        or current.context.payload_kind != "table"
        or previous.context.structural_path != current.context.structural_path
        or not previous.question_boundaries
    ):
        return None
    previous_page = _locator_page_no(previous.context, end=True)
    current_page = _locator_page_no(current.context)
    if (
        previous_page is None
        or current_page is None
        or current_page != previous_page + 1
    ):
        return None

    previous_lines = [line.strip() for line in _qa_lines(previous.text) if line.strip()]
    if len(previous_lines) != 1:
        return None
    first_question = previous_lines[0]
    first_ordinal = _qa_ordinal(first_question)
    if (
        first_ordinal is None
        or not first_question.endswith(("?", "？"))
        or _comparison_text(first_question)
        not in {_comparison_text(boundary) for boundary in previous.question_boundaries}
    ):
        return None

    table = current.context.payload
    cells = [str(value).strip() for value in table.get("headers") or []]
    cells.extend(str(value).strip() for row in table.get("rows") or [] for value in row)
    nonempty_cells = [value for value in cells if value]
    if len(nonempty_cells) != 1:
        return None
    table_lines = [line.strip() for line in _qa_lines(current.text) if line.strip()]
    first_numbered = next(
        (index for index, line in enumerate(table_lines) if _qa_numbered_line(line)),
        None,
    )
    if first_numbered != 1:
        return None
    leading_answer = table_lines[0]
    if len(_comparison_text(leading_answer)) < 20 or not re.search(
        r"[。！？!?；;）)】\]”’\"'」』]$", leading_answer
    ):
        return None

    blocks = _qa_unlabelled_numbered_blocks(current.text)
    ordinals = [ordinal for ordinal, _, _ in blocks]
    if len(blocks) < 3 or ordinals != list(
        range(first_ordinal + 1, first_ordinal + 1 + len(blocks))
    ):
        return None
    if any(
        not re.search(r"[。！？!?；;）)】\]”’\"'」』]$", answer)
        for _, _, answer in blocks[:-1]
    ):
        return None
    return [first_ordinal, *ordinals]


def _qa_expand_unlabelled_numbered_blocks(text: str) -> str:
    blocks = {
        ordinal: (question, answer)
        for ordinal, question, answer in _qa_unlabelled_numbered_blocks(text)
    }
    expanded: list[str] = []
    for raw_line in _qa_lines(text):
        line = raw_line.strip()
        ordinal = _qa_ordinal(line)
        block = blocks.get(ordinal) if ordinal is not None else None
        if block is None:
            if line:
                expanded.append(line)
            continue
        expanded.extend(block)
    return "\n".join(expanded)


def _qa_logical_official_compound_start(carrier: _QaLogicalCarrier) -> bool:
    if carrier.context.payload_kind != "table":
        return False
    compact = _comparison_text(carrier.text)
    if not rules.QA_FORM_TRANSCRIPT_CUE_RE.search(compact):
        return False
    return any(
        rules.QA_COMPOUND_QUESTION_INTRO_RE.match(line.strip())
        for line in _qa_lines(carrier.text)
        if line.strip()
    )


def _qa_logical_run_break(
    previous: _QaLogicalCarrier, current: _QaLogicalCarrier
) -> bool:
    if current.source_order - previous.source_order > 64:
        return True
    if not current.has_existing_qa and any(
        _qa_numbered_line(boundary) for boundary in current.question_boundaries
    ):
        # A complete physical numbered-question heading starts a new logical
        # transcript segment.  Keeping the preceding already-complete QA in
        # the run hid the exact Q8-heading→next-page-table proof; an unnumbered
        # question fragment (used by table→heading seam recovery) still joins.
        return True
    if not _qa_logical_carrier_has_signal(current):
        # Arbitrary prose and ordinary business tables are never answer
        # continuations merely because they share a heading path.  The sole
        # unlabelled exception is the fully proven physical-heading / one-cell
        # consecutive-QA spill above.
        return _qa_logical_unlabelled_pair_ordinals(previous, current) is None
    if previous.context.structural_path == current.context.structural_path:
        return False

    # False question-fragment headings and reanchored footer-overflow tables
    # are precisely the cross-path forms this recovery exists for. A normal
    # numbered section change remains a hard boundary (and protects ordinary
    # prose from an earlier unanswered table question).
    current_title = (current.context.title or "").strip()
    if current_title.endswith(("?", "？")):
        return False
    if current.context.payload_kind == "table" and current.ends_run:
        return not _qa_logical_carrier_has_signal(current)
    return True


def _qa_logical_runs(
    carriers: list[_QaLogicalCarrier],
) -> list[list[_QaLogicalCarrier]]:
    runs: list[list[_QaLogicalCarrier]] = []
    current: list[_QaLogicalCarrier] = []
    for carrier in carriers:
        if current and _qa_logical_run_break(current[-1], carrier):
            if len(current) > 1:
                runs.append(current)
            current = []
        if not current:
            # Continuous recovery needs a locally proven pair as its anchor.
            # Starting from a lone unanswered question can bridge arbitrary
            # prose to a later unrelated ``答`` marker.  The one exception is
            # an official-form table with a cue-complete compound-question
            # intro; its answer is known to spill into the following carrier.
            if not (
                carrier.has_existing_qa
                or _qa_logical_official_compound_start(carrier)
                or carrier.question_boundaries
            ):
                continue
            current = [carrier]
        else:
            current.append(carrier)
        if carrier.ends_run:
            if len(current) > 1:
                runs.append(current)
            current = []
    if len(current) > 1:
        runs.append(current)
    return runs


def _qa_match_variants(text: str) -> list[str]:
    stripped = text.strip()
    values = [
        stripped,
        _strip_question_prefix(stripped),
        _strip_answer_prefix(stripped),
        re.sub(r"^\s*\d+[、.．]\s*", "", stripped),
    ]
    variants: list[str] = []
    for value in values:
        key = _comparison_text(value)
        if len(key) >= 3 and key not in variants:
            variants.append(key)
    return sorted(variants, key=len, reverse=True)


def _qa_logical_unit_span(
    qa: UnitDraft, carriers: list[_QaLogicalCarrier]
) -> tuple[int, int] | None:
    carrier_texts = [_comparison_text(carrier.text) for carrier in carriers]
    raw_lines = [
        line.strip()
        for line in _qa_lines(str(qa.payload.get("raw_text") or ""))
        if line.strip()
    ]
    if not raw_lines:
        raw_lines = [
            str(qa.payload.get("question") or ""),
            str(qa.payload.get("answer") or ""),
        ]

    matched: list[int] = []
    cursor = 0
    for raw_line in raw_lines:
        variants = _qa_match_variants(raw_line)
        found: int | None = None
        for index in range(cursor, len(carrier_texts)):
            if any(value in carrier_texts[index] for value in variants):
                found = index
                break
        if found is None:
            continue
        matched.append(found)
        cursor = found
    if not matched:
        return None
    return min(matched), max(matched)


def _qa_existing_match(
    units: list[UnitDraft], *, question_key: str, source_order: int
) -> int | None:
    candidates: list[tuple[int, int]] = []
    for index, unit in enumerate(units):
        if unit.payload_kind != "qa":
            continue
        existing = _comparison_text(str(unit.payload.get("question") or ""))
        if not existing:
            continue
        exact = existing == question_key
        prefix = min(len(existing), len(question_key)) >= 12 and (
            existing.startswith(question_key) or question_key.startswith(existing)
        )
        if exact or prefix:
            candidates.append((abs(unit.source_order - source_order), index))
    return min(candidates)[1] if candidates else None


def _qa_candidate_improves_existing(
    *,
    candidate: UnitDraft,
    existing: UnitDraft,
    next_question_key: str | None,
    candidate_crosses_carriers: bool,
) -> bool:
    candidate_answer = _comparison_text(str(candidate.payload.get("answer") or ""))
    existing_answer = _comparison_text(str(existing.payload.get("answer") or ""))
    if not candidate_answer or candidate_answer == existing_answer:
        return False
    if candidate_answer.startswith(existing_answer):
        return candidate_crosses_carriers
    if not existing_answer.startswith(candidate_answer):
        return False
    tail = existing_answer[len(candidate_answer) :]
    if not next_question_key:
        return False
    # A locally parsed answer may contain only the first half of the next
    # question; the logical run supplies its continuation from a later
    # heading.  Requiring the whole next question would preserve exactly that
    # polluted prefix (1223090921 Q2→Q3).
    overlap = next_question_key[: min(8, len(next_question_key))]
    return len(overlap) >= 8 and overlap in tail


def _qa_recovered_question_has_boundary(
    qa: UnitDraft, run: list[_QaLogicalCarrier]
) -> bool:
    question = str(qa.payload.get("question") or "").strip()
    if question.endswith(("?", "？")):
        return True
    question_key = _comparison_text(question)
    if any(
        question_key
        and (
            question_key in _comparison_text(boundary)
            or _comparison_text(boundary) in question_key
        )
        for carrier in run
        for boundary in carrier.question_boundaries
    ):
        return True
    first = next(
        (
            line.strip()
            for line in _qa_lines(str(qa.payload.get("raw_text") or ""))
            if line.strip()
        ),
        "",
    )
    return bool(
        rules.EXPLICIT_QUESTION_START_RE.match(first)
        or rules.QUESTION_START_RE.match(first)
    )


def _mark_qa_span_carriers_needs_review(
    units: list[UnitDraft], *, start_order: int, end_order: int
) -> None:
    for index, unit in enumerate(units):
        if (
            start_order <= unit.source_order <= end_order
            and unit.payload_kind != "qa"
            and unit.quality_status != "unusable"
        ):
            units[index] = UnitDraft(
                **{**unit.__dict__, "quality_status": "needs_review"}
            )


def _recover_qa_across_logical_carrier_runs(
    units: list[UnitDraft],
) -> list[UnitDraft]:
    """Derive complete QA pairs from bounded multi-carrier form transcripts.

    This private continuous view fixes table→heading→text and text→table→text
    splits without replacing or mutating the original evidence carriers.
    Recovered or corrected pairs are marked ``needs_review`` and carry a
    source-order span whenever more than one physical carrier contributed.
    """

    out = list(units)
    recovered: list[UnitDraft] = []
    for run in _qa_logical_runs(_qa_logical_carriers(out)):
        unlabelled_ordinals = (
            _qa_logical_unlabelled_pair_ordinals(run[0], run[1])
            if len(run) == 2
            else None
        )
        source_units = [carrier.context for carrier in run]
        locator = dict(_merged_locator(source_units) or {})
        carrier_texts = [carrier.text for carrier in run]
        if unlabelled_ordinals is not None:
            carrier_texts[1] = _qa_expand_unlabelled_numbered_blocks(carrier_texts[1])
        source = UnitDraft(
            payload_kind="text",
            payload={"text": "\n".join(text for text in carrier_texts if text)},
            source_order=run[0].source_order,
            heading_path=list(run[0].context.heading_path),
            structural_path=list(run[0].context.structural_path),
            title=run[0].context.title,
            quality_status=_worst_quality(source_units),
            artifact_locator=locator or None,
            qa_question_boundaries=[
                value for carrier in run for value in carrier.question_boundaries
            ],
        )
        parsed = s4_build_qa_units(
            str(source.payload["text"]),
            source=source,
            require_explicit_answer=unlabelled_ordinals is None,
        )
        if (
            parsed.unstable
            or not parsed.units
            or (
                unlabelled_ordinals is not None
                and parsed.ordinals != unlabelled_ordinals
            )
        ):
            continue

        for qa_index, qa in enumerate(parsed.units):
            question_key = _comparison_text(str(qa.payload.get("question") or ""))
            if not question_key:
                continue
            span = _qa_logical_unit_span(qa, run)
            if span is None:
                continue
            start_index, end_index = span
            if (
                end_index - start_index > 2
                or run[end_index].source_order - run[start_index].source_order > 64
            ):
                continue
            contributors = [
                carrier.context for carrier in run[start_index : end_index + 1]
            ]
            qa_locator = dict(_merged_locator(contributors) or {})
            if start_index != end_index:
                qa_locator["source_order_span"] = [
                    run[start_index].source_order,
                    run[end_index].source_order,
                ]
            candidate = UnitDraft(
                **{
                    **qa.__dict__,
                    "source_order": run[start_index].source_order,
                    "intra_order": 3000 + qa_index + len(recovered),
                    "heading_path": list(run[start_index].context.heading_path),
                    "structural_path": list(run[start_index].context.structural_path),
                    "title": str(qa.payload.get("question") or "") or qa.title,
                    "quality_status": "needs_review",
                    "artifact_locator": qa_locator or None,
                }
            )
            match_index = _qa_existing_match(
                out,
                question_key=question_key,
                source_order=candidate.source_order,
            )
            next_question_key = (
                _comparison_text(
                    str(parsed.units[qa_index + 1].payload.get("question") or "")
                )
                if qa_index + 1 < len(parsed.units)
                else None
            )
            if match_index is None:
                proven_single_table_pair = bool(
                    unlabelled_ordinals is not None
                    and start_index == end_index == 1
                    and _qa_ordinal(
                        str(qa.payload.get("raw_text") or "").splitlines()[0]
                    )
                    in unlabelled_ordinals[1:]
                )
                if (
                    start_index == end_index and not proven_single_table_pair
                ) or not _qa_recovered_question_has_boundary(qa, run):
                    continue
                _mark_qa_span_carriers_needs_review(
                    out,
                    start_order=run[start_index].source_order,
                    end_order=run[end_index].source_order,
                )
                recovered.append(candidate)
                continue
            existing = out[match_index]
            if _qa_candidate_improves_existing(
                candidate=candidate,
                existing=existing,
                next_question_key=next_question_key,
                candidate_crosses_carriers=start_index != end_index,
            ):
                _mark_qa_span_carriers_needs_review(
                    out,
                    start_order=run[start_index].source_order,
                    end_order=run[end_index].source_order,
                )
                out[match_index] = UnitDraft(
                    **{
                        **candidate.__dict__,
                        "source_order": existing.source_order,
                        "intra_order": existing.intra_order,
                    }
                )
    return [*out, *recovered]


def _qa_raw_ordinal(unit: UnitDraft) -> int | None:
    raw = str(unit.payload.get("raw_text") or "")
    first = next((line.strip() for line in _qa_lines(raw) if line.strip()), "")
    return _qa_ordinal(first or unit.title or "")


def _qa_effective_end_order(unit: UnitDraft) -> int:
    span = (unit.artifact_locator or {}).get("source_order_span")
    if (
        isinstance(span, list)
        and len(span) == 2
        and all(isinstance(value, int) for value in span)
    ):
        return int(span[1])
    return unit.source_order


def _locator_bbox(unit: UnitDraft) -> tuple[float, float, float, float] | None:
    bbox = (unit.artifact_locator or {}).get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        left, top, right, bottom = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _next_physical_business_unit(
    units: list[UnitDraft], *, after_order: int
) -> tuple[int, UnitDraft] | None:
    for index, candidate in enumerate(units):
        if candidate.source_order <= after_order or candidate.payload_kind == "qa":
            continue
        if _is_blank_text_unit(candidate):
            continue
        if candidate.payload_kind == "table" and _table_payload_is_empty(
            candidate.payload
        ):
            continue
        return index, candidate
    return None


def _next_physical_qa_unit(
    units: list[UnitDraft], *, after_order: int
) -> tuple[int, UnitDraft] | None:
    return next(
        (
            (index, candidate)
            for index, candidate in enumerate(units)
            if candidate.source_order > after_order and candidate.payload_kind == "qa"
        ),
        None,
    )


def _recover_qa_answer_text_sandwiches(units: list[UnitDraft]) -> list[UnitDraft]:
    """Join a page-top plain-text answer tail before the next physical Q.

    The recovery needs all three pieces: a review QA whose answer visibly ends
    mid-token, a top-of-next-page plain text carrier, and the immediately
    following consecutive question heading on that same page.  This fixes the
    MinerU table→text page seams in 1223071887 Q1/Q11 and 1218099701 Q11 while
    keeping arbitrary prose and non-consecutive transcripts fail-closed.
    """

    out = sorted(list(units), key=_unit_sort_key)
    consumed_tail_orders: set[int] = set()
    for qa_index, candidate in enumerate(list(out)):
        if candidate.payload_kind != "qa":
            continue
        answer_end_order = _qa_effective_end_order(candidate)
        source_table_evidence = any(
            unit.payload_kind == "table"
            and candidate.source_order <= unit.source_order <= answer_end_order
            for unit in out
        )
        if candidate.quality_status != "needs_review" and not source_table_evidence:
            continue
        ordinal = _qa_raw_ordinal(candidate)
        answer = str(candidate.payload.get("answer") or "").rstrip()
        if (
            ordinal is None
            or not answer
            or not re.search(r"[A-Za-z0-9\u4e00-\u9fff]$", answer)
            or re.search(r"[。！？!?；;：:…）)】\]”’\"'」』]$", answer)
        ):
            continue
        follower_result = _next_physical_business_unit(
            out, after_order=answer_end_order
        )
        if follower_result is None:
            continue
        follower_index, follower = follower_result
        if (
            follower.source_order in consumed_tail_orders
            or follower.payload_kind != "text"
            or not 0 < follower.source_order - answer_end_order <= 64
        ):
            continue
        continuation = _main_text(follower).strip()
        continuation_lines = [
            line.strip() for line in _qa_lines(continuation) if line.strip()
        ]
        if (
            not 8 <= len(_comparison_text(continuation)) <= 2000
            or not continuation_lines
            or not re.search(r"[。！？!?；;）)】\]”’\"'」』]$", continuation)
            or any(
                _qa_numbered_line(line)
                or rules.ANSWER_START_RE.match(line)
                or rules.EXPLICIT_QUESTION_START_RE.match(line)
                or rules.QUESTION_START_RE.match(line)
                for line in continuation_lines
            )
        ):
            continue
        next_result = _next_physical_qa_unit(out, after_order=follower.source_order)
        if next_result is None:
            continue
        _, next_question = next_result
        intervening_business = any(
            unit.payload_kind != "qa"
            and follower.source_order < unit.source_order < next_question.source_order
            and not _is_blank_text_unit(unit)
            and not (
                unit.payload_kind == "table" and _table_payload_is_empty(unit.payload)
            )
            for unit in out
        )
        if (
            intervening_business
            or _qa_raw_ordinal(next_question) != ordinal + 1
            or not 0 < next_question.source_order - follower.source_order <= 16
        ):
            continue

        candidate_page = _locator_page_no(candidate, end=True)
        follower_page = _locator_page_no(follower)
        next_page = _locator_page_no(next_question)
        follower_bbox = _locator_bbox(follower)
        next_bbox = _locator_bbox(next_question)
        if (
            follower_page is None
            or next_page != follower_page
            or follower_bbox is None
            or next_bbox is None
            or follower_bbox[1] > 320
            or next_bbox[1] <= follower_bbox[3]
            or (
                not source_table_evidence
                and (candidate_page is None or follower_page != candidate_page + 1)
            )
        ):
            continue

        payload = dict(candidate.payload)
        payload["answer"] = _join_wrapped_lines([answer, continuation])
        payload["raw_text"] = "\n".join(
            filter(None, [str(payload.get("raw_text") or "").strip(), continuation])
        )
        locator = dict(_merged_locator([candidate, follower]) or {})
        existing_span = (candidate.artifact_locator or {}).get("source_order_span")
        start_order = (
            int(existing_span[0])
            if isinstance(existing_span, list)
            and len(existing_span) == 2
            and isinstance(existing_span[0], int)
            else candidate.source_order
        )
        locator["source_order_span"] = [start_order, follower.source_order]
        out[qa_index] = UnitDraft(
            **{
                **candidate.__dict__,
                "payload": payload,
                "quality_status": "needs_review",
                "artifact_locator": locator,
            }
        )
        out[follower_index] = UnitDraft(
            **{**follower.__dict__, "quality_status": "needs_review"}
        )
        consumed_tail_orders.add(follower.source_order)
    return out


def _qa_footer_overflow_answer(table: UnitDraft) -> str | None:
    headers = [str(value).strip() for value in table.payload.get("headers") or []]
    rows = [
        [str(value).strip() for value in row] for row in table.payload.get("rows") or []
    ]
    if len(headers) < 2 or headers[0] or not headers[1]:
        return None
    if any(headers[index] for index in range(2, len(headers))):
        return None
    first_cells = [row[0] for row in rows if row and row[0]]
    if len(first_cells) < 2 or not all(
        rules.QA_FORM_FOOTER_FIELD_RE.fullmatch(value) for value in first_cells
    ):
        return None
    answer = headers[1]
    if (
        len(_comparison_text(answer)) < 10
        or not re.search(r"[。！？!?；;）)】\]”’\"'」』]$", answer)
        or any(
            _qa_numbered_line(line)
            or rules.ANSWER_START_RE.match(line)
            or rules.EXPLICIT_QUESTION_START_RE.match(line)
            or rules.QUESTION_START_RE.match(line)
            for line in _qa_lines(answer)
            if line.strip()
        )
    ):
        return None
    return answer


_EXTENDED_QA_FOOTER_FIELD_KEYS = (
    _comparison_text("关于本次活动是否涉及"),
    _comparison_text("应披露重大信息的说明"),
    _comparison_text(
        "活动过程中所使用的演示文稿、提供的文档等附件(如有,可作为附件)"
    ),
)


def _strict_extended_qa_footer_overflow(table: UnitDraft) -> bool:
    """Recognize the one ordered three-field exchange-form footer family."""

    headers = [str(value).strip() for value in table.payload.get("headers") or []]
    rows = [
        [str(value).strip() for value in row]
        for row in table.payload.get("rows") or []
    ]
    firsts = [row[0] for row in rows if row and row[0]]
    if (
        len(headers) != 2
        or headers[0]
        or not headers[1]
        or len(rows) != 3
        or tuple(_comparison_text(value) for value in firsts)
        != _EXTENDED_QA_FOOTER_FIELD_KEYS
    ):
        return False
    return _qa_footer_overflow_answer(table) is not None


def _recover_final_unlabelled_qa_footer(units: list[UnitDraft]) -> list[UnitDraft]:
    """Recover one trailing numbered Q from the official footer overflow.

    S4 can preserve Q15 as review text after Q14 while MinerU puts its answer
    into the next page's outer-form header.  Requiring the text to be the
    consecutive ordinal, to share Q14's source carrier, and to be followed by
    an exact attachment/date footer makes this a lossless form repair rather
    than generic table flattening.
    """

    out = sorted(list(units), key=_unit_sort_key)
    existing_questions = {
        _comparison_text(str(unit.payload.get("question") or ""))
        for unit in out
        if unit.payload_kind == "qa"
    }
    recovered: list[UnitDraft] = []
    for index, candidate in enumerate(list(out)):
        if (
            candidate.payload_kind != "text"
            or candidate.quality_status != "needs_review"
        ):
            continue
        lines = [
            line.strip() for line in _qa_lines(_main_text(candidate)) if line.strip()
        ]
        if len(lines) != 1 or not _qa_numbered_line(lines[0]):
            continue
        ordinal = _qa_ordinal(lines[0])
        question = _strip_question_prefix(lines[0])
        question_key = _comparison_text(question)
        if (
            ordinal is None
            or not question.endswith(("?", "？"))
            or not question_key
            or question_key in existing_questions
        ):
            continue
        previous = next(
            (unit for unit in reversed(out[:index]) if unit.payload_kind == "qa"),
            None,
        )
        if previous is None or _qa_raw_ordinal(previous) != ordinal - 1:
            continue
        previous_raw = next(
            (
                line.strip()
                for line in _qa_lines(str(previous.payload.get("raw_text") or ""))
                if line.strip()
            ),
            "",
        )
        candidate_boundaries = {
            _comparison_text(boundary) for boundary in candidate.qa_question_boundaries
        }
        same_carrier_tail = bool(
            previous.source_order == candidate.source_order
            and _comparison_text(previous_raw) in candidate_boundaries
        )
        physical_next_heading = bool(
            0 < candidate.source_order - previous.source_order <= 16
            and _comparison_text(lines[0]) in candidate_boundaries
        )
        if not same_carrier_tail and not physical_next_heading:
            continue
        follower_result = _next_physical_business_unit(
            out, after_order=candidate.source_order
        )
        if follower_result is None:
            continue
        follower_index, table = follower_result
        answer = (
            _qa_footer_overflow_answer(table) if table.payload_kind == "table" else None
        )
        if answer is None or not 0 < table.source_order - candidate.source_order <= 64:
            continue
        candidate_page = _locator_page_no(candidate, end=True)
        table_page = _locator_page_no(table)
        table_bbox = _locator_bbox(table)
        if (
            candidate_page is None
            or table_page != candidate_page + 1
            or table_bbox is None
            or table_bbox[1] > 320
        ):
            continue

        locator = dict(_merged_locator([candidate, table]) or {})
        locator["source_order_span"] = [candidate.source_order, table.source_order]
        recovered.append(
            UnitDraft(
                payload_kind="qa",
                payload={
                    "question": question,
                    "answer": answer,
                    "raw_text": f"{lines[0]}\n{answer}",
                },
                source_order=candidate.source_order,
                intra_order=4000 + len(recovered),
                heading_path=list(previous.heading_path),
                structural_path=list(previous.structural_path),
                title=question,
                quality_status="needs_review",
                artifact_locator=locator,
            )
        )
        out[index] = UnitDraft(
            **{**candidate.__dict__, "quality_status": "needs_review"}
        )
        out[follower_index] = UnitDraft(
            **{**table.__dict__, "quality_status": "needs_review"}
        )
        existing_questions.add(question_key)
    return [*out, *recovered]


def _qa_table_blocks(table: UnitDraft) -> list[str]:
    """Return distinct table grids in physical order for seam inspection."""

    grids = [table.payload.get("headers") or []]
    grids.extend(table.payload.get("rows") or [])
    seen: set[str] = set()
    blocks: list[str] = []
    for grid in grids:
        block = _qa_table_grid_text(grid)
        key = _comparison_text(block)
        if not key or key in seen:
            continue
        seen.add(key)
        blocks.append(block)
    return blocks


def _last_unanswered_explicit_table_question(table: UnitDraft) -> list[str] | None:
    """Return only the table's final explicit, unanswered question tail.

    A question in an earlier grid is not physically adjacent to the following
    text carrier.  Likewise, a later answer marker means the question was not
    split at this seam.  Both shapes fail closed.
    """

    blocks = _qa_table_blocks(table)
    if not blocks:
        return None
    lines = [line.strip() for line in _qa_lines(blocks[-1]) if line.strip()]
    question_indexes = [
        index
        for index, line in enumerate(lines)
        if rules.EXPLICIT_QUESTION_START_RE.match(line) and _strip_question_prefix(line)
    ]
    if not question_indexes:
        return None
    question_index = question_indexes[-1]
    if any(rules.ANSWER_START_RE.match(line) for line in lines[question_index + 1 :]):
        return None
    return lines[question_index:]


def _first_following_business_carrier(
    units: list[UnitDraft], *, table_index: int, table_order: int
) -> UnitDraft | None:
    """Find the next physical carrier, ignoring derived QA and empty grids."""

    for candidate in units[table_index + 1 :]:
        if candidate.source_order <= table_order:
            continue
        if candidate.payload_kind == "qa":
            continue
        if _is_blank_text_unit(candidate):
            continue
        if candidate.payload_kind == "table" and _table_payload_is_empty(
            candidate.payload
        ):
            continue
        return candidate
    return None


def _recover_qa_across_table_text_seams(units: list[UnitDraft]) -> list[UnitDraft]:
    """Recover explicit QA split across adjacent table/text carriers.

    This is deliberately narrow: only the table's final explicit unanswered
    question may cross into the very next business carrier in the same
    heading path.  A complete table question requires an immediate answer;
    an incomplete one may take continuation text only when the combined
    question ends in ``?``/``？`` before that answer.  The reverse direction
    additionally requires the official-form overflow shape: an incomplete
    labelled review-text question followed by an empty-leading-cell table
    header containing one narrative continuation and an explicit answer.
    Original carriers remain untouched; recovered QA is additionally marked
    needs_review because its locator spans two MinerU carriers.
    """

    out = list(units)
    existing_questions = {
        _comparison_text(str(unit.payload.get("question") or ""))
        for unit in out
        if unit.payload_kind == "qa"
    }
    recovered: list[UnitDraft] = []
    for table_index, table in enumerate(out):
        if table.payload_kind != "table" or table.quality_status == "unusable":
            continue
        question_tail = _last_unanswered_explicit_table_question(table)
        if question_tail is None:
            continue
        follower = _first_following_business_carrier(
            out, table_index=table_index, table_order=table.source_order
        )
        if (
            follower is None
            or follower.payload_kind != "text"
            or follower.quality_status != "needs_review"
            or follower.heading_path != table.heading_path
        ):
            continue
        follower_text = _main_text(follower)
        follower_lines = [
            line.strip() for line in _qa_lines(follower_text) if line.strip()
        ]
        answer_index = next(
            (
                index
                for index, line in enumerate(follower_lines)
                if rules.ANSWER_START_RE.match(line)
            ),
            None,
        )
        if answer_index is None:
            continue
        question_continuation = follower_lines[:answer_index]
        if any(
            rules.QUESTION_START_RE.match(line)
            or rules.EXPLICIT_QUESTION_START_RE.match(line)
            for line in question_continuation
        ):
            continue
        table_question = _join_wrapped_lines(question_tail)
        table_question_complete = table_question.endswith(("?", "？"))
        if table_question_complete and question_continuation:
            continue
        if not table_question_complete and not question_continuation:
            continue
        combined_question = _join_wrapped_lines(
            [*question_tail, *question_continuation]
        )
        if not combined_question.endswith(("?", "？")):
            continue

        expected_question = _strip_question_prefix(combined_question)
        locator = dict(_merged_locator([table, follower]) or {})
        locator["source_order_span"] = [
            table.source_order,
            follower.source_order,
        ]
        source = UnitDraft(
            payload_kind="text",
            payload={"text": "\n".join([*question_tail, *follower_lines])},
            source_order=table.source_order,
            heading_path=list(table.heading_path),
            title=table.title or follower.title,
            quality_status="needs_review",
            artifact_locator=locator,
        )
        result = s4_build_qa_units(
            str(source.payload["text"]),
            source=source,
            require_explicit_answer=True,
        )
        if result.unstable or not result.units:
            continue
        qa_unit = result.units[0]
        question_key = _comparison_text(str(qa_unit.payload.get("question") or ""))
        if (
            not question_key
            or question_key != _comparison_text(expected_question)
            or question_key in existing_questions
        ):
            continue
        existing_questions.add(question_key)
        recovered.append(
            UnitDraft(
                **{
                    **qa_unit.__dict__,
                    "intra_order": 1000 + len(recovered),
                    "quality_status": "needs_review",
                }
            )
        )

    # Symmetric text→table seam.  Official investor-relations forms commonly
    # continue the final Q&A in the next page's outer-table header
    # (1217929537 / 71829602155).  Inspect only the observed spill shape, not
    # arbitrary table rows, so normal business tables cannot supply answers.
    for text_index, text_unit in enumerate(out):
        if (
            text_unit.payload_kind != "text"
            or text_unit.quality_status != "needs_review"
        ):
            continue
        text_value = _main_text(text_unit).strip()
        text_lines = [line.strip() for line in _qa_lines(text_value) if line.strip()]
        if not any(
            rules.EXPLICIT_QUESTION_START_RE.match(line)
            and _strip_question_prefix(line)
            for line in text_lines
        ) or any(rules.ANSWER_START_RE.match(line) for line in text_lines):
            continue
        follower = _first_following_business_carrier(
            out, table_index=text_index, table_order=text_unit.source_order
        )
        if (
            follower is None
            or follower.payload_kind != "table"
            or follower.source_order - text_unit.source_order > 16
        ):
            continue
        headers = [str(value) for value in follower.payload.get("headers") or []]
        nonempty_headers = [value.strip() for value in headers if value.strip()]
        if len(headers) < 2 or headers[0].strip() or len(nonempty_headers) != 1:
            continue
        rows = follower.payload.get("rows") or []
        footer_firsts = [
            str(row[0]).strip() for row in rows if row and str(row[0]).strip()
        ]
        reanchored_footer_overflow = bool(
            footer_firsts
            and all(
                rules.QA_FORM_FOOTER_FIELD_RE.match(first) for first in footer_firsts
            )
        )
        if (
            follower.heading_path != text_unit.heading_path
            and not reanchored_footer_overflow
        ):
            continue
        table_continuation = nonempty_headers[0]
        table_lines = [
            line.strip() for line in _qa_lines(table_continuation) if line.strip()
        ]
        answer_index = next(
            (
                index
                for index, line in enumerate(table_lines)
                if rules.ANSWER_START_RE.match(line)
            ),
            None,
        )
        if answer_index is None:
            continue
        question_continuation = table_lines[:answer_index]
        if any(
            rules.QUESTION_START_RE.match(line)
            or rules.EXPLICIT_QUESTION_START_RE.match(line)
            for line in question_continuation
        ):
            continue
        text_question_complete = text_value.endswith(("?", "？"))
        if text_question_complete and question_continuation:
            continue
        if not text_question_complete and not question_continuation:
            continue
        combined_question = _join_wrapped_lines([text_value, *question_continuation])
        if not combined_question.endswith(("?", "？")):
            continue
        expected_question = _strip_question_prefix(combined_question)

        locator = dict(_merged_locator([text_unit, follower]) or {})
        locator["source_order_span"] = [
            text_unit.source_order,
            follower.source_order,
        ]
        source = UnitDraft(
            payload_kind="text",
            payload={"text": f"{text_value}\n{table_continuation}"},
            source_order=text_unit.source_order,
            heading_path=list(text_unit.heading_path),
            title=text_unit.title,
            quality_status="needs_review",
            artifact_locator=locator,
        )
        result = s4_build_qa_units(
            str(source.payload["text"]),
            source=source,
            require_explicit_answer=True,
        )
        if (
            result.unstable
            or len(result.units) != 1
            or result.leading_text
            or result.review_spans
            or result.trailing_text
        ):
            continue
        qa_unit = result.units[0]
        question_key = _comparison_text(str(qa_unit.payload.get("question") or ""))
        if (
            not question_key
            or question_key != _comparison_text(expected_question)
            or question_key in existing_questions
        ):
            continue
        existing_questions.add(question_key)
        follower_index = next(
            index
            for index, candidate in enumerate(out[text_index + 1 :], text_index + 1)
            if candidate is follower
        )
        out[follower_index] = UnitDraft(
            **{**follower.__dict__, "quality_status": "needs_review"}
        )
        recovered.append(
            UnitDraft(
                **{
                    **qa_unit.__dict__,
                    "intra_order": 2000 + len(recovered),
                    "quality_status": "needs_review",
                }
            )
        )

    # A completed question can have only the tail of its unlabelled answer
    # moved into the next page's official-form outer table. Unlike the
    # unanswered seam above, this updates the already-derived final QA only
    # when the answer visibly ends mid-token and the table has exactly one
    # narrative header plus either an empty leading cell or exact footer rows.
    for table_index, table in enumerate(out):
        if table.payload_kind != "table" or table_index == 0:
            continue
        candidate = out[table_index - 1]
        if candidate.payload_kind != "qa":
            continue
        if not 0 < table.source_order - candidate.source_order <= 64:
            continue
        answer = str(candidate.payload.get("answer") or "").rstrip()
        strict_extended_footer = _strict_extended_qa_footer_overflow(table)
        if not answer or (
            re.search(r"[。！？!?；;：:…）)】\]”’\"'」』]$", answer)
            and not strict_extended_footer
        ):
            continue
        if strict_extended_footer:
            ordinals = [
                ordinal
                for unit in out
                if unit.payload_kind == "qa"
                if (ordinal := _qa_raw_ordinal(unit)) is not None
            ]
            candidate_page = _locator_page_no(candidate, end=True)
            table_page = _locator_page_no(table)
            table_bbox = _locator_bbox(table)
            if (
                not ordinals
                or _qa_raw_ordinal(candidate) != max(ordinals)
                or candidate_page is None
                or table_page != candidate_page + 1
                or table_bbox is None
                or table_bbox[1] > 320
            ):
                continue
        raw_headers = [str(value) for value in table.payload.get("headers") or []]
        nonempty_headers = [value.strip() for value in raw_headers if value.strip()]
        if len(nonempty_headers) != 1 or len(nonempty_headers[0]) < 10:
            continue
        rows = table.payload.get("rows") or []
        footer_firsts = [
            str(row[0]).strip() for row in rows if row and str(row[0]).strip()
        ]
        exact_footer_shape = bool(
            footer_firsts
            and all(
                rules.QA_FORM_FOOTER_FIELD_RE.match(first) for first in footer_firsts
            )
        )
        empty_leading_header = bool(
            len(raw_headers) >= 2 and not raw_headers[0].strip()
        )
        if not exact_footer_shape and not empty_leading_header:
            continue
        if candidate.heading_path != table.heading_path and not exact_footer_shape:
            continue
        continuation = re.split(
            r"(?:接待|调研)过程中\s*[,，]?\s*公司严格按照|附件清单|"
            r"[（(]\s*二\s*[）)]\s*对于公告征集问题的回复",
            nonempty_headers[0],
            maxsplit=1,
        )[0].strip()
        if len(continuation) < 10 or any(
            rules.QUESTION_START_RE.match(line)
            or rules.EXPLICIT_QUESTION_START_RE.match(line)
            or rules.ANSWER_START_RE.match(line)
            for line in _qa_lines(continuation)
            if line.strip()
        ):
            continue
        payload = dict(candidate.payload)
        payload["answer"] = _join_wrapped_lines([answer, continuation])
        payload["raw_text"] = "\n".join(
            filter(None, [str(payload.get("raw_text") or "").strip(), continuation])
        )
        locator = dict(_merged_locator([candidate, table]) or {})
        locator["source_order_span"] = [
            candidate.source_order,
            table.source_order,
        ]
        out[table_index - 1] = UnitDraft(
            **{
                **candidate.__dict__,
                "payload": payload,
                "quality_status": "needs_review",
                "artifact_locator": locator,
            }
        )
        out[table_index] = UnitDraft(
            **{**table.__dict__, "quality_status": "needs_review"}
        )
    return [*out, *recovered]


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
