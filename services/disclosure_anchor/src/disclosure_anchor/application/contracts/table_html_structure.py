"""Lossless structural projection for one MinerU HTML table carrier.

The expanded string grid is a retrieval convenience. ``cells`` and
``embedded_media`` retain the source-cell roles and every media occurrence so
consumers never have to infer them back from flattened text.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re

from disclosure_anchor.application.contracts.table_visibility import (
    VOID_TAGS,
    TableVisibilityError,
    VisibilityTracker,
    require_supported_markup,
)


class TableHtmlStructureError(ValueError):
    """The HTML carrier cannot be represented without structural loss."""


@dataclass(frozen=True, slots=True)
class HtmlTableMedia:
    occurrence_index: int
    cell_media_index: int
    row: int
    col: int
    rowspan: int
    colspan: int
    image_path: str
    alt_text: str | None
    title_text: str | None


@dataclass(frozen=True, slots=True)
class HtmlTableCell:
    row: int
    col: int
    rowspan: int
    colspan: int
    text: str
    is_header: bool


@dataclass(frozen=True, slots=True)
class ParsedHtmlTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    cells: tuple[HtmlTableCell, ...]
    embedded_media: tuple[HtmlTableMedia, ...]
    merged_cells: tuple[tuple[int, int, int, int], ...]


@dataclass(frozen=True, slots=True)
class _RawMedia:
    image_path: str
    alt_text: str | None
    title_text: str | None


@dataclass(frozen=True, slots=True)
class _RawCell:
    text: str
    rowspan: int
    colspan: int
    is_header: bool
    media: tuple[_RawMedia, ...]


class _TableParser(HTMLParser):
    """Derive the published grid under the shared visibility policy.

    The published rows/cells and the reader-visible comparison must see
    one visible domain: invisible-content tags and hidden subtrees never
    contribute text or media here either.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_RawCell]] = []
        self._table_depth = 0
        self._saw_table = False
        self._current_row: list[_RawCell] | None = None
        self._cell_attrs: tuple[int, int, bool] | None = None
        self._cell_text: list[str] = []
        self._cell_media: list[_RawMedia] = []
        self._visibility = VisibilityTracker()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        attr_map = _unique_attributes(attrs)
        if tag == "table":
            if self._table_depth or self._saw_table:
                raise TableHtmlStructureError(
                    "table carrier must contain exactly one non-nested table"
                )
            try:
                require_supported_markup(tag, attr_map)
            except TableVisibilityError as exc:
                raise TableHtmlStructureError(str(exc)) from exc
            self._table_depth = 1
            self._saw_table = True
            return
        if not self._table_depth:
            return
        try:
            element_visible = self._visibility.enter(tag, attr_map)
        except TableVisibilityError as exc:
            raise TableHtmlStructureError(str(exc)) from exc
        if tag == "br":
            if element_visible and self._cell_attrs is not None:
                self._cell_text.append(" ")
            return
        if tag in {"caption", "tfoot"}:
            # The published grid and the reader-visible comparison must
            # share one domain assignment. Caption/tfoot content belongs
            # to the caption/note domains, which this grid cannot express;
            # a carrier using them blocks instead of silently diverging.
            raise TableHtmlStructureError(
                f"<{tag}> content cannot be represented in the published "
                "grid without domain loss"
            )
        if tag == "tr":
            if self._current_row is not None or self._cell_attrs is not None:
                raise TableHtmlStructureError("table rows are nested or unclosed")
            self._current_row = []
            return
        if tag in {"td", "th"}:
            if self._current_row is None or self._cell_attrs is not None:
                raise TableHtmlStructureError("table cell is outside a row or nested")
            self._cell_attrs = (
                _span_value(attr_map.get("rowspan")),
                _span_value(attr_map.get("colspan")),
                tag == "th",
            )
            self._cell_text = []
            self._cell_media = []
            return
        if tag == "img":
            if self._cell_attrs is None:
                raise TableHtmlStructureError(
                    "embedded table image is outside a logical cell"
                )
            if not element_visible:
                return
            raw_path = attr_map.get("src")
            if (
                raw_path is None
                or not raw_path
                or raw_path != raw_path.strip()
            ):
                raise TableHtmlStructureError(
                    "embedded table image requires one exact non-empty src"
                )
            self._cell_media.append(
                _RawMedia(
                    image_path=raw_path,
                    alt_text=attr_map.get("alt"),
                    title_text=attr_map.get("title"),
                )
            )

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self._visibility.visible:
            return
        if self._cell_attrs is not None:
            self._cell_text.append(data)
        elif self._table_depth and data.strip():
            raise TableHtmlStructureError(
                "visible table content exists outside a logical cell"
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._table_depth and tag != "table":
            self._visibility.leave(tag)
        if tag in {"td", "th"}:
            if self._cell_attrs is None or self._current_row is None:
                raise TableHtmlStructureError("table cell closing tag is unmatched")
            rowspan, colspan, is_header = self._cell_attrs
            self._current_row.append(
                _RawCell(
                    text=_collapse_text("".join(self._cell_text)),
                    rowspan=rowspan,
                    colspan=colspan,
                    is_header=is_header,
                    media=tuple(self._cell_media),
                )
            )
            self._cell_attrs = None
            self._cell_text = []
            self._cell_media = []
            return
        if tag == "tr":
            if self._current_row is None or self._cell_attrs is not None:
                raise TableHtmlStructureError("table row closing tag is unmatched")
            self.rows.append(self._current_row)
            self._current_row = None
            return
        if tag == "table":
            if (
                not self._table_depth
                or self._current_row is not None
                or self._cell_attrs is not None
            ):
                raise TableHtmlStructureError("table closing tag is unmatched")
            self._table_depth = 0

    def finish(self) -> None:
        self.close()
        if (
            not self._saw_table
            or self._table_depth
            or self._current_row is not None
            or self._cell_attrs is not None
            or not any(self.rows)
        ):
            raise TableHtmlStructureError(
                "table carrier has no complete logical cells"
            )


def parse_table_html_structure(value: str) -> ParsedHtmlTable:
    """Parse exactly one HTML table into a closed structural representation."""

    if not isinstance(value, str) or not value.strip():
        raise TableHtmlStructureError("table HTML must be non-empty text")
    parser = _TableParser()
    parser.feed(value)
    parser.finish()
    return _expand_rows(parser.rows)


def table_media_artifact_role(
    source_item_index: int,
    occurrence_index: int,
) -> str:
    """Return the stable manifest role for one physical HTML media occurrence."""

    if (
        isinstance(source_item_index, bool)
        or not isinstance(source_item_index, int)
        or source_item_index < 0
        or isinstance(occurrence_index, bool)
        or not isinstance(occurrence_index, int)
        or occurrence_index < 0
    ):
        raise ValueError("table media indices must be non-negative integers")
    return (
        f"evidence_table_media_{source_item_index:06d}_"
        f"{occurrence_index:06d}"
    )


def _expand_rows(source_rows: list[list[_RawCell]]) -> ParsedHtmlTable:
    occupied: dict[tuple[int, int], str] = {}
    cells: list[HtmlTableCell] = []
    media: list[HtmlTableMedia] = []
    merged: list[tuple[int, int, int, int]] = []
    max_row = -1
    max_col = -1
    for row_index, row in enumerate(source_rows):
        col_index = 0
        for raw_cell in row:
            while (row_index, col_index) in occupied:
                col_index += 1
            targets = {
                (row_index + row_offset, col_index + col_offset)
                for row_offset in range(raw_cell.rowspan)
                for col_offset in range(raw_cell.colspan)
            }
            if targets & occupied.keys():
                raise TableHtmlStructureError("table cell spans overlap")
            for target in targets:
                occupied[target] = raw_cell.text
                max_row = max(max_row, target[0])
                max_col = max(max_col, target[1])
            cells.append(
                HtmlTableCell(
                    row=row_index,
                    col=col_index,
                    rowspan=raw_cell.rowspan,
                    colspan=raw_cell.colspan,
                    text=raw_cell.text,
                    is_header=raw_cell.is_header,
                )
            )
            if raw_cell.rowspan > 1 or raw_cell.colspan > 1:
                merged.append(
                    (
                        row_index,
                        col_index,
                        raw_cell.rowspan,
                        raw_cell.colspan,
                    )
                )
            for cell_media_index, raw_media in enumerate(raw_cell.media):
                media.append(
                    HtmlTableMedia(
                        occurrence_index=len(media),
                        cell_media_index=cell_media_index,
                        row=row_index,
                        col=col_index,
                        rowspan=raw_cell.rowspan,
                        colspan=raw_cell.colspan,
                        image_path=raw_media.image_path,
                        alt_text=raw_media.alt_text,
                        title_text=raw_media.title_text,
                    )
                )
            col_index += raw_cell.colspan
    if max_row < 0 or max_col < 0:
        raise TableHtmlStructureError("table carrier has no logical cells")

    grid = tuple(
        tuple(
            occupied.get((row_index, col_index), "")
            for col_index in range(max_col + 1)
        )
        for row_index in range(max_row + 1)
    )
    first_row_cells = tuple(cell for cell in cells if cell.row == 0)
    later_header_exists = any(cell.is_header for cell in cells if cell.row > 0)
    one_header_row = (
        bool(first_row_cells)
        and all(cell.is_header for cell in first_row_cells)
        and not later_header_exists
    )
    return ParsedHtmlTable(
        headers=grid[0] if one_header_row else (),
        rows=grid[1:] if one_header_row else grid,
        cells=tuple(cells),
        embedded_media=tuple(media),
        merged_cells=tuple(merged),
    )


def _unique_attributes(
    attrs: list[tuple[str, str | None]],
) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for raw_name, value in attrs:
        name = raw_name.lower()
        if name in result:
            raise TableHtmlStructureError(
                f"table HTML attribute is duplicated: {name}"
            )
        result[name] = value
    return result


def _span_value(value: str | None) -> int:
    if value is None:
        return 1
    if not value.isdigit() or (parsed := int(value)) < 1:
        raise TableHtmlStructureError("table rowspan/colspan must be positive")
    return parsed


def _collapse_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
