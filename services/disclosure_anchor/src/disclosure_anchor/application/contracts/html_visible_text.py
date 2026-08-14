"""Deterministic visible-text projection for parser-owned HTML evidence."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from typing import Literal


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
        if normalized == "thead":
            if self._table_depth != 1:
                self.invalid = True
            self._thead_depth += 1
            return
        if normalized == "tr":
            if self._table_depth != 1 or self._row is not None:
                self.invalid = True
            self._row = []
            return
        if normalized not in {"td", "th"}:
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
        if normalized == "thead":
            if self._thead_depth <= 0:
                self.invalid = True
            else:
                self._thead_depth -= 1
            return
        if normalized == "table":
            if self._table_depth != 1 or self._row is not None:
                self.invalid = True
            self._table_depth = max(0, self._table_depth - 1)

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
            or self._row is not None
            or self._cell_tag is not None
        ):
            self.invalid = True

    def _span(
        self,
        attrs: list[tuple[str, str | None]],
        name: str,
    ) -> int:
        raw = next((value for key, value in attrs if key.lower() == name), None)
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
    visible = html_visible_text_segments(value)
    if tuple(cell.text for cell in cells) != visible or any(
        not cell.text for cell in cells
    ):
        return ()

    roles: list[HtmlTableSemanticRole] = ["table_text"] * len(cells)
    if _is_two_column_field_form(parser.rows):
        offset = 0
        for row in parser.rows:
            roles[offset] = "table_field_label"
            offset += len(row)
    else:
        offset = 0
        implicit_header_row = _implicit_column_header_row(parser.rows)
        for row_index, row in enumerate(parser.rows):
            for cell_index, cell in enumerate(row):
                if cell.tag == "th" or cell.in_thead:
                    roles[offset + cell_index] = "table_column_header"
            if row_index == implicit_header_row:
                for cell_index in range(len(row)):
                    roles[offset + cell_index] = "table_column_header"
            offset += len(row)
    return tuple(
        HtmlTableSemanticSegment(text=cell.text, role=role)
        for cell, role in zip(cells, roles, strict=True)
    )


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
    if len(rows) < 2:
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
        cell.colspan != 1
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


__all__ = [
    "HtmlTableSemanticSegment",
    "html_table_semantic_segments",
    "html_visible_text",
    "html_visible_text_segments",
]
