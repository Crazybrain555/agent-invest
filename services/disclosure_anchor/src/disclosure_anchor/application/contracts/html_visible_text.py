"""Deterministic visible-text projection for parser-owned HTML evidence."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from typing import Literal
import unicodedata


_NON_VISIBLE_ELEMENTS = frozenset(
    {"script", "style", "template", "noscript"}
)

HtmlTableSemanticRole = Literal[
    "table_text",
    "table_field_label",
    "table_column_header",
]


@dataclass(frozen=True, slots=True)
class HtmlTableSemanticSegment:
    """One visible cell with a narrowly derived structural role."""

    text: str
    role: HtmlTableSemanticRole


@dataclass(frozen=True, slots=True)
class HtmlQARowAtom:
    """One mechanically closed question/answer row from provider HTML."""

    source_row_index: int
    row_text: str


@dataclass(frozen=True, slots=True)
class _TableCell:
    text: str
    tag: str
    colspan: int
    rowspan: int
    in_thead: bool


class _VisibleTextParser(HTMLParser):
    """Collect character data without leaking markup into retrieval text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0
        self._hard_parts: list[str] = []
        self._hard_fragments: list[str] = []
        self._cell_stack: list[str] = []

    def _flush_hard_segment(self) -> None:
        value = " ".join(self._hard_fragments)
        if value:
            self._hard_parts.append(value)
        self._hard_fragments = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        normalized = tag.lower()
        if self._suppressed_depth:
            if normalized in _NON_VISIBLE_ELEMENTS:
                self._suppressed_depth += 1
            return
        if normalized in _NON_VISIBLE_ELEMENTS:
            self._suppressed_depth += 1
            return
        if normalized in {"caption", "td", "th"}:
            self._flush_hard_segment()
            self._cell_stack.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if self._suppressed_depth:
            if normalized in _NON_VISIBLE_ELEMENTS:
                self._suppressed_depth -= 1
            return
        if self._cell_stack and normalized == self._cell_stack[-1]:
            self._flush_hard_segment()
            self._cell_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._suppressed_depth:
            return
        collapsed = " ".join(data.split())
        if collapsed:
            self.parts.append(collapsed)
            self._hard_fragments.append(collapsed)

    def close(self) -> None:
        super().close()
        self._flush_hard_segment()

    def hard_segments(self) -> tuple[str, ...]:
        return tuple(self._hard_parts)


class _TableStructureParser(HTMLParser):
    """Read only explicit HTML rows/cells; malformed structure has no roles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[_TableCell, ...]] = []
        self.invalid = False
        self._table_depth = 0
        self._table_count = 0
        self._thead_depth = 0
        self._section_tag: str | None = None
        self._last_section_rank = -1
        self._seen_thead = False
        self._seen_tbody = False
        self._seen_tfoot = False
        self._seen_direct_row = False
        self._suppressed_depth = 0
        self._row: list[_TableCell] | None = None
        self._cell_tag: str | None = None
        self._cell_fragments: list[str] = []
        self._cell_colspan = 1
        self._cell_rowspan = 1
        self._cell_in_thead = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.lower()
        if self._suppressed_depth:
            if normalized in _NON_VISIBLE_ELEMENTS:
                self._suppressed_depth += 1
            return
        if normalized in _NON_VISIBLE_ELEMENTS:
            self._suppressed_depth += 1
            return
        if normalized == "table":
            self._table_count += 1
            self._table_depth += 1
            if self._table_count != 1 or self._table_depth != 1:
                self.invalid = True
            return
        if normalized in {"thead", "tbody", "tfoot"}:
            rank = {"thead": 0, "tbody": 1, "tfoot": 2}[normalized]
            if (
                self._table_depth != 1
                or self._row is not None
                or self._cell_tag is not None
                or self._section_tag is not None
                or rank < self._last_section_rank
                or (normalized == "thead" and (self._seen_thead or self.rows))
                or (normalized == "tbody" and self._seen_direct_row)
                or (normalized == "tfoot" and self._seen_tfoot)
            ):
                self.invalid = True
                return
            self._section_tag = normalized
            self._last_section_rank = rank
            if normalized == "thead":
                self._seen_thead = True
                self._thead_depth = 1
            elif normalized == "tbody":
                self._seen_tbody = True
            else:
                self._seen_tfoot = True
            return
        if normalized == "tr":
            if (
                self._table_depth != 1
                or self._row is not None
                or (
                    self._section_tag is None
                    and (self._seen_tbody or self._seen_tfoot)
                )
            ):
                self.invalid = True
            if self._section_tag is None:
                self._seen_direct_row = True
            self._row = []
            return
        if normalized not in {"td", "th"}:
            if self._table_depth and self._cell_tag is None:
                self.invalid = True
            return
        if self._table_depth != 1 or self._row is None or self._cell_tag is not None:
            self.invalid = True
            return
        self._cell_tag = normalized
        self._cell_fragments = []
        self._cell_colspan = self._span(attrs, "colspan")
        self._cell_rowspan = self._span(attrs, "rowspan")
        self._cell_in_thead = self._thead_depth > 0

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if self._suppressed_depth:
            if normalized in _NON_VISIBLE_ELEMENTS:
                self._suppressed_depth -= 1
            return
        if normalized in {"td", "th"}:
            if self._cell_tag != normalized or self._row is None:
                self.invalid = True
                return
            self._row.append(
                _TableCell(
                    text=" ".join(self._cell_fragments),
                    tag=normalized,
                    colspan=self._cell_colspan,
                    rowspan=self._cell_rowspan,
                    in_thead=self._cell_in_thead,
                )
            )
            self._cell_tag = None
            self._cell_fragments = []
            return
        if normalized == "tr":
            if self._row is None or self._cell_tag is not None or not self._row:
                self.invalid = True
                self._row = None
                return
            self.rows.append(tuple(self._row))
            self._row = None
            return
        if normalized in {"thead", "tbody", "tfoot"}:
            if (
                self._section_tag != normalized
                or self._row is not None
                or self._cell_tag is not None
            ):
                self.invalid = True
            else:
                self._section_tag = None
                if normalized == "thead":
                    self._thead_depth = 0
            return
        if normalized == "table":
            if self._table_depth != 1 or self._row is not None:
                self.invalid = True
            self._table_depth = max(0, self._table_depth - 1)
            return
        if self._table_depth and self._cell_tag is None:
            self.invalid = True

    def handle_data(self, data: str) -> None:
        if self._suppressed_depth:
            return
        collapsed = " ".join(data.split())
        if not collapsed:
            return
        if self._cell_tag is None:
            if self._table_depth:
                self.invalid = True
            return
        self._cell_fragments.append(collapsed)

    def close(self) -> None:
        super().close()
        if (
            self._table_count != 1
            or self._table_depth
            or self._thead_depth
            or self._section_tag is not None
            or self._row is not None
            or self._cell_tag is not None
        ):
            self.invalid = True

    def _span(
        self,
        attrs: list[tuple[str, str | None]],
        name: str,
    ) -> int:
        matches = [value for key, value in attrs if key.lower() == name]
        if len(matches) > 1:
            self.invalid = True
            return 1
        raw = matches[0] if matches else None
        if raw is None:
            return 1
        try:
            value = int(raw)
        except (TypeError, ValueError):
            self.invalid = True
            return 1
        if value < 1 or value > 1000:
            self.invalid = True
            return 1
        return value


def html_visible_text(value: str) -> str:
    """Return only human-visible HTML text in deterministic source order.

    This is a representation projection, not a second table parser.  It is
    used when the typed grid parser failed but the immutable HTML carrier may
    still contain facts that L2 must be able to retrieve.  Element names,
    attributes, comments, and non-visible script/style/template contents are
    never emitted.
    """

    if not value.strip():
        return ""
    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    return " ".join(parser.parts)


def html_visible_text_segments(value: str) -> tuple[str, ...]:
    """Return table-cell segments whose boundaries cannot be crossed.

    Inline tags and whitespace inside one cell remain one segment.  Separate
    ``caption``/``th``/``td`` cells are independent source fields for exact
    occurrence matching, even though ``html_visible_text`` presents them as
    one reader-facing string.
    """

    if not value.strip():
        return ()
    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    return parser.hard_segments()


def html_table_semantic_segments(
    value: str,
) -> tuple[HtmlTableSemanticSegment, ...]:
    """Classify only mechanically closed table labels and column headers.

    Raw HTML remains the content authority. This projection returns no typed
    roles for nested, malformed, caption-bearing, or otherwise ambiguous HTML.
    MinerU 3.4.4 emits ``td`` cells without semantic header tags, so a strict
    two-column form and a rectangular, non-numeric first row are the only
    service-side fallbacks.
    """

    if not value.strip():
        return ()
    parser = _TableStructureParser()
    parser.feed(value)
    parser.close()
    if parser.invalid or not parser.rows:
        return ()
    cells = tuple(cell for row in parser.rows for cell in row)
    nonempty_cells = tuple(cell for cell in cells if cell.text)
    visible = html_visible_text_segments(value)
    if tuple(cell.text for cell in nonempty_cells) != visible:
        return ()

    roles: list[HtmlTableSemanticRole] = ["table_text"] * len(cells)
    grid_width = _rectangular_grid_width(parser.rows)
    if grid_width is None:
        pass
    elif _is_two_column_field_form(parser.rows):
        offset = 0
        for row in parser.rows:
            roles[offset] = "table_field_label"
            offset += len(row)
    else:
        offset = 0
        implicit_header_row = _implicit_column_header_row(parser.rows)
        multirow_header_rows = _implicit_multirow_column_header_rows(parser.rows)
        typed_data_labels = _typed_data_label_cells(parser.rows)
        for row_index, row in enumerate(parser.rows):
            for cell_index, cell in enumerate(row):
                if cell.tag == "th" or cell.in_thead:
                    roles[offset + cell_index] = "table_column_header"
                if (row_index, cell_index) in typed_data_labels:
                    roles[offset + cell_index] = "table_field_label"
            if row_index == implicit_header_row or row_index in multirow_header_rows:
                for cell_index in range(len(row)):
                    roles[offset + cell_index] = "table_column_header"
            offset += len(row)
    return tuple(
        HtmlTableSemanticSegment(text=cell.text, role=role)
        for cell, role in zip(cells, roles, strict=True)
        if cell.text
    )


def html_qa_row_atoms(value: str) -> tuple[HtmlQARowAtom, ...]:
    """Project only an explicit three-column Q&A table into row atoms.

    This is deliberately narrower than generic table parsing.  The table may
    have one leading three-column title row, but it must then contain the exact
    ``序号 / 提问内容 / 回复内容`` header and contiguous integer data rows.
    Spans, nesting, missing cells, duplicate/gapped ordinals, or any visible
    text mismatch make the complete table ineligible; callers retain the
    ordinary parent/leaf search channels.
    """

    if not value.strip():
        return ()
    parser = _TableStructureParser()
    parser.feed(value)
    parser.close()
    if parser.invalid or not parser.rows:
        return ()
    cells = tuple(cell for row in parser.rows for cell in row)
    if tuple(cell.text for cell in cells) != html_visible_text_segments(value):
        return ()
    header_index = 0
    first = parser.rows[0]
    if (
        len(first) == 1
        and first[0].colspan == 3
        and first[0].rowspan == 1
        and first[0].text
    ):
        header_index = 1
    if header_index >= len(parser.rows) - 1:
        return ()
    header = parser.rows[header_index]
    if (
        len(header) != 3
        or any(cell.colspan != 1 or cell.rowspan != 1 for cell in header)
        or tuple(_compact_cell_text(cell.text) for cell in header)
        != ("序号", "提问内容", "回复内容")
    ):
        return ()
    atoms: list[HtmlQARowAtom] = []
    for expected_ordinal, source_row_index in enumerate(
        range(header_index + 1, len(parser.rows)),
        start=1,
    ):
        row = parser.rows[source_row_index]
        if (
            len(row) != 3
            or any(
                not cell.text or cell.colspan != 1 or cell.rowspan != 1
                for cell in row
            )
            or _compact_cell_text(row[0].text) != str(expected_ordinal)
        ):
            return ()
        atoms.append(
            HtmlQARowAtom(
                source_row_index=source_row_index,
                row_text=" ".join(cell.text for cell in row),
            )
        )
    return tuple(atoms)


def _compact_cell_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def _is_two_column_field_form(rows: list[tuple[_TableCell, ...]]) -> bool:
    if len(rows) < 2:
        return False
    labels: list[str] = []
    for row in rows:
        if len(row) != 2 or any(
            cell.colspan != 1 or cell.rowspan != 1 for cell in row
        ):
            return False
        label, value = row
        if not label.text or not value.text or len(label.text) > 128:
            return False
        labels.append("".join(label.text.split()).casefold())
    return len(labels) == len(set(labels))


def _implicit_column_header_row(
    rows: list[tuple[_TableCell, ...]],
) -> int | None:
    if len(rows) < 2 or _rectangular_grid_width(rows) is None:
        return None
    header_index = 0
    if (
        len(rows) >= 3
        and len(rows[0]) == 1
        and rows[0][0].colspan >= 2
        and rows[0][0].rowspan == 1
        and sum(cell.colspan for cell in rows[1]) == rows[0][0].colspan
    ):
        # Common provider shape: one spanning table title followed by the
        # actual column labels.  The title stays ordinary visible text; only
        # the rectangular second row gains header semantics.
        header_index = 1
    first = rows[header_index]
    if len(first) < 2:
        return None
    if any(
        not cell.text
        or cell.colspan != 1
        or cell.rowspan != 1
        or re.search(r"[0-9０-９%％]", cell.text)
        for cell in first
    ):
        return None
    width = len(first)
    if any(
        sum(cell.colspan for cell in row) == width
        for row in rows[header_index + 1 :]
    ):
        return header_index
    return None


def _implicit_multirow_column_header_rows(
    rows: list[tuple[_TableCell, ...]],
) -> frozenset[int]:
    """Recognize one closed two-row header band with explicit spans."""

    if len(rows) < 3 or _rectangular_grid_width(rows) is None:
        return frozenset()
    first, second, data = rows[0], rows[1], rows[2]
    if (
        (len(first) == 1 and first[0].colspan > 1 and first[0].rowspan == 1)
        or not any(cell.colspan > 1 or cell.rowspan > 1 for cell in first)
        or any(
            not cell.text or re.search(r"[0-9０-９%％]", cell.text)
            for cell in (*first, *second)
        )
        or not any(re.search(r"[0-9０-９]", cell.text) for cell in data)
    ):
        return frozenset()
    return frozenset({0, 1})


def _typed_data_label_cells(
    rows: list[tuple[_TableCell, ...]],
) -> frozenset[tuple[int, int]]:
    """Return source cells that a closed table schema proves are fact labels."""

    if (
        len(rows) < 2
        or _rectangular_grid_width(rows) is None
        or any(
            cell.rowspan != 1 or cell.colspan != 1
            for row in rows[1:]
            for cell in row
        )
    ):
        return frozenset()
    header = tuple(_compact_cell_text(cell.text) for cell in rows[0])
    positions: set[tuple[int, int]] = set()
    if (
        len(header) >= 4
        and header[0] == "科目"
        and sum(
            any(marker in item for marker in ("本期", "上年同期", "变动"))
            for item in header[1:]
        )
        >= 2
    ):
        for row_index, row in enumerate(rows[1:], start=1):
            if row[0].text and any(
                re.search(r"[0-9０-９]", cell.text) for cell in row[1:]
            ):
                positions.add((row_index, 0))
    related_party_headers = {
        "关联方",
        "关联交易内容",
        "本期发生额",
        "上期发生额",
    }
    if related_party_headers.issubset(set(header)):
        topic_index = header.index("关联交易内容")
        amount_indices = tuple(
            index
            for index, item in enumerate(header)
            if item in {"本期发生额", "上期发生额"}
        )
        for row_index, row in enumerate(rows[1:], start=1):
            if (
                topic_index < len(row)
                and row[topic_index].text
                and any(
                    index < len(row)
                    and re.search(r"[0-9０-９]", row[index].text)
                    for index in amount_indices
                )
            ):
                positions.add((row_index, topic_index))
    return frozenset(positions)


def _rectangular_grid_width(rows: list[tuple[_TableCell, ...]]) -> int | None:
    """Validate one non-overlapping rowspan/colspan grid and return its width."""

    active: dict[int, int] = {}
    widths: list[int] = []
    for row in rows:
        occupied = set(active)
        next_active = {
            column: remaining - 1
            for column, remaining in active.items()
            if remaining > 1
        }
        column = 0
        for cell in row:
            while column in occupied:
                column += 1
            span = range(column, column + cell.colspan)
            if any(item in occupied for item in span):
                return None
            for item in span:
                occupied.add(item)
                if cell.rowspan > 1:
                    next_active[item] = cell.rowspan - 1
            column += cell.colspan
        if not occupied or occupied != set(range(max(occupied) + 1)):
            return None
        widths.append(max(occupied) + 1)
        active = next_active
    if active or len(set(widths)) != 1:
        return None
    return widths[0]


__all__ = [
    "HtmlQARowAtom",
    "HtmlTableSemanticSegment",
    "html_qa_row_atoms",
    "html_table_semantic_segments",
    "html_visible_text",
    "html_visible_text_segments",
]
