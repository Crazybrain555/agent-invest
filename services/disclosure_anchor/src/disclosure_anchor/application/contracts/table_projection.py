"""Reader-visible table projection: the closed comparison domain for tables.

``reader-visible-table-projection.v1`` states exactly what two renderings
of one table must agree on: the caption/cell/note/footnote domains, each
with its order and multiplicity, and inside every cell the ordered
sequence of visible text runs and opaque media occurrences. Markup,
styling, hidden attributes, media bytes/paths/alt/title, provenance, and
evidence-only observations are outside the domain and can never affect
equality.

The producer's reconciler and the independent audit both build
projections from raw artifacts through this module. Sharing the closed
schema, the whitespace normalization, and the canonical hash primitive is
deliberate; results (projections, matchings, equality verdicts) are never
shared between them.

Text normalization is exactly: HTML entity decoding plus deterministic
whitespace collapse. No NFKC, punctuation, digit, case, or PUA
normalization is ever applied. ``<br>`` becomes a stable single-space
boundary, so ``甲<br>乙`` can never equal ``甲乙``. Only the HTML boolean
``hidden`` attribute hides content; CSS is opaque markup and is not
interpreted.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import json
import re

READER_VISIBLE_TABLE_PROJECTION_VERSION = "reader-visible-table-projection.v1"
_PROJECTION_DOMAIN_TAG = "disclosure-anchor.reader-visible-table-projection.v1"

_INVISIBLE_CONTENT_TAGS = frozenset({"script", "style", "template", "noscript"})
_WS_RE = re.compile(r"\s+")


class TableProjectionError(ValueError):
    """The table cannot be projected without structural loss."""


@dataclass(frozen=True, slots=True)
class VisibleItem:
    """One visible occurrence inside a cell: a text run or a media marker.

    A media occurrence is opaque by design: its position and multiplicity
    are reader-visible facts, while its path, bytes, alt, and title are
    artifact facts owned by other closures.
    """

    kind: str  # "text" | "media_marker"
    value: str | None  # collapsed text for "text"; None for "media_marker"


@dataclass(frozen=True, slots=True)
class VisibleCell:
    ordinal: int
    row: int
    col: int
    rowspan: int
    colspan: int
    role: str  # "header" | "body"
    items: tuple[VisibleItem, ...]


@dataclass(frozen=True, slots=True)
class VisibleTableProjection:
    caption: tuple[str, ...]
    cells: tuple[VisibleCell, ...]
    notes: tuple[str, ...]
    footnotes: tuple[str, ...]

    def body(self) -> "VisibleTableProjection":
        """The model-comparable part: cells only.

        A model rendering observes the table body; captions, notes, and
        footnotes are content-side domains. Comparing bodies never
        interprets an unobserved domain as empty — it simply does not
        claim it.
        """

        return VisibleTableProjection(
            caption=(),
            cells=self.cells,
            notes=(),
            footnotes=(),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "projection_contract_version": (
                READER_VISIBLE_TABLE_PROJECTION_VERSION
            ),
            "caption": list(self.caption),
            "cells": [
                {
                    "ordinal": cell.ordinal,
                    "row": cell.row,
                    "col": cell.col,
                    "rowspan": cell.rowspan,
                    "colspan": cell.colspan,
                    "role": cell.role,
                    "items": [
                        {"kind": item.kind, "value": item.value}
                        for item in cell.items
                    ],
                }
                for cell in self.cells
            ],
            "notes": list(self.notes),
            "footnotes": list(self.footnotes),
        }

    def sha256(self) -> str:
        preimage = json.dumps(
            {
                "domain": _PROJECTION_DOMAIN_TAG,
                "projection": self.canonical_payload(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return "sha256:" + hashlib.sha256(
            preimage.encode("utf-8")
        ).hexdigest()


@dataclass(slots=True)
class _RawCell:
    rowspan: int
    colspan: int
    is_header: bool
    in_tfoot: bool
    items: list[VisibleItem]
    pending_text: list[str]


class _ProjectionParser(HTMLParser):
    """Parse exactly one non-nested table into visible facts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.captions: list[str] = []
        self.rows: list[list[_RawCell]] = []
        self.tfoot_rows: list[list[_RawCell]] = []
        self._saw_table = False
        self._in_table = False
        self._in_tfoot = False
        self._caption_parts: list[str] | None = None
        self._current_row: list[_RawCell] | None = None
        self._cell: _RawCell | None = None
        self._invisible_depth = 0
        self._hidden_depth = 0
        # Tags whose close must pop the hidden/invisible state, tracked as
        # a stack of (tag, was_invisible, was_hidden) frames.
        self._element_stack: list[tuple[str, bool, bool]] = []

    # -- element visibility bookkeeping ---------------------------------

    def _push_element(self, tag: str, attrs: dict[str, str | None]) -> None:
        invisible = tag in _INVISIBLE_CONTENT_TAGS
        hidden = "hidden" in attrs
        self._element_stack.append((tag, invisible, hidden))
        if invisible:
            self._invisible_depth += 1
        if hidden:
            self._hidden_depth += 1

    def _pop_element(self, tag: str) -> None:
        while self._element_stack:
            top_tag, invisible, hidden = self._element_stack.pop()
            if invisible:
                self._invisible_depth -= 1
            if hidden:
                self._hidden_depth -= 1
            if top_tag == tag:
                return

    def _content_visible(self) -> bool:
        return not self._invisible_depth and not self._hidden_depth

    # -- parser events ---------------------------------------------------

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        attr_map = _unique_attributes(attrs)
        if tag == "table":
            if self._in_table or self._saw_table:
                raise TableProjectionError(
                    "table carrier must contain exactly one non-nested table"
                )
            self._saw_table = True
            self._in_table = True
            return
        if not self._in_table:
            return
        if tag == "br":
            if self._cell is not None and self._content_visible():
                self._cell.pending_text.append(" ")
            elif self._caption_parts is not None and self._content_visible():
                self._caption_parts.append(" ")
            return
        self._push_element(tag, attr_map)
        if tag == "caption":
            if self._current_row is not None or self._cell is not None:
                raise TableProjectionError(
                    "table caption is nested inside a row or cell"
                )
            if self._caption_parts is not None:
                raise TableProjectionError("table caption is nested")
            self._caption_parts = []
            return
        if tag == "tfoot":
            if self._in_tfoot:
                raise TableProjectionError("table tfoot is nested")
            self._in_tfoot = True
            return
        if tag == "tr":
            if self._current_row is not None or self._cell is not None:
                raise TableProjectionError("table rows are nested or unclosed")
            self._current_row = []
            return
        if tag in {"td", "th"}:
            if self._current_row is None or self._cell is not None:
                raise TableProjectionError(
                    "table cell is outside a row or nested"
                )
            self._cell = _RawCell(
                rowspan=_span_value(attr_map.get("rowspan")),
                colspan=_span_value(attr_map.get("colspan")),
                is_header=tag == "th",
                in_tfoot=self._in_tfoot,
                items=[],
                pending_text=[],
            )
            return
        if tag == "img":
            if self._cell is None:
                raise TableProjectionError(
                    "embedded table image is outside a logical cell"
                )
            if self._cell.in_tfoot:
                raise TableProjectionError(
                    "media inside tfoot notes is unsupported"
                )
            if self._content_visible():
                _flush_text(self._cell)
                self._cell.items.append(
                    VisibleItem(kind="media_marker", value=None)
                )

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        if tag in {"img", "br"}:
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self._in_table or not self._content_visible():
            return
        if self._cell is not None:
            self._cell.pending_text.append(data)
        elif self._caption_parts is not None:
            self._caption_parts.append(data)
        elif data.strip():
            raise TableProjectionError(
                "visible table content exists outside a logical cell"
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._in_table:
            return
        if tag == "table":
            if (
                self._current_row is not None
                or self._cell is not None
                or self._caption_parts is not None
                or self._in_tfoot
            ):
                raise TableProjectionError("table closing tag is unmatched")
            self._in_table = False
            return
        if tag == "caption":
            if self._caption_parts is None:
                raise TableProjectionError("caption closing tag is unmatched")
            self.captions.append(_collapse(" ".join(self._caption_parts)))
            self._caption_parts = None
            self._pop_element(tag)
            return
        if tag == "tfoot":
            if not self._in_tfoot or self._current_row is not None:
                raise TableProjectionError("tfoot closing tag is unmatched")
            self._in_tfoot = False
            self._pop_element(tag)
            return
        if tag in {"td", "th"}:
            if self._cell is None or self._current_row is None:
                raise TableProjectionError(
                    "table cell closing tag is unmatched"
                )
            _flush_text(self._cell)
            self._current_row.append(self._cell)
            self._cell = None
            self._pop_element(tag)
            return
        if tag == "tr":
            if self._current_row is None or self._cell is not None:
                raise TableProjectionError("table row closing tag is unmatched")
            if self._in_tfoot:
                self.tfoot_rows.append(self._current_row)
            else:
                self.rows.append(self._current_row)
            self._current_row = None
            self._pop_element(tag)
            return
        self._pop_element(tag)

    def finish(self) -> None:
        self.close()
        if (
            not self._saw_table
            or self._in_table
            or self._current_row is not None
            or self._cell is not None
        ):
            raise TableProjectionError(
                "table carrier has no complete logical cells"
            )


def _flush_text(cell: _RawCell) -> None:
    text = _collapse("".join(cell.pending_text))
    cell.pending_text = []
    if not text:
        return
    if cell.items and cell.items[-1].kind == "text":
        merged = f"{cell.items[-1].value} {text}"
        cell.items[-1] = VisibleItem(kind="text", value=_collapse(merged))
        return
    cell.items.append(VisibleItem(kind="text", value=text))


def project_table_html(
    value: str,
    *,
    extra_captions: tuple[str, ...] = (),
    extra_footnotes: tuple[str, ...] = (),
) -> VisibleTableProjection:
    """Project one HTML table carrier into its reader-visible facts.

    ``extra_captions``/``extra_footnotes`` carry the provider's out-of-band
    ``table_caption``/``table_footnote`` values in their stored order; they
    join the same domains as in-HTML ``<caption>``/``<tfoot>`` content.
    """

    if not isinstance(value, str) or not value.strip():
        raise TableProjectionError("table HTML must be non-empty text")
    parser = _ProjectionParser()
    parser.feed(value)
    parser.finish()

    cells = _expand_cells(parser.rows)
    notes: list[str] = []
    for row in parser.tfoot_rows:
        for raw in row:
            text_items = [
                item.value for item in raw.items if item.kind == "text"
            ]
            note = _collapse(" ".join(part for part in text_items if part))
            if note:
                notes.append(note)
    captions = [
        _collapse(caption)
        for caption in (*extra_captions, *parser.captions)
    ]
    footnotes = [_collapse(note) for note in extra_footnotes]
    if any(not caption for caption in captions) or any(
        not note for note in footnotes
    ):
        raise TableProjectionError(
            "caption and footnote entries must be non-empty text"
        )
    return VisibleTableProjection(
        caption=tuple(captions),
        cells=cells,
        notes=tuple(notes),
        footnotes=tuple(footnotes),
    )


def _expand_cells(rows: list[list[_RawCell]]) -> tuple[VisibleCell, ...]:
    occupied: set[tuple[int, int]] = set()
    cells: list[VisibleCell] = []
    any_cell = False
    for row_index, row in enumerate(rows):
        col_index = 0
        for raw in row:
            any_cell = True
            while (row_index, col_index) in occupied:
                col_index += 1
            targets = {
                (row_index + dr, col_index + dc)
                for dr in range(raw.rowspan)
                for dc in range(raw.colspan)
            }
            if targets & occupied:
                raise TableProjectionError("table cell spans overlap")
            occupied.update(targets)
            cells.append(
                VisibleCell(
                    ordinal=len(cells),
                    row=row_index,
                    col=col_index,
                    rowspan=raw.rowspan,
                    colspan=raw.colspan,
                    role="header" if raw.is_header else "body",
                    items=tuple(raw.items),
                )
            )
            col_index += raw.colspan
    if not any_cell:
        raise TableProjectionError("table carrier has no logical cells")
    return tuple(cells)


def _unique_attributes(
    attrs: list[tuple[str, str | None]],
) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for raw_name, value in attrs:
        name = raw_name.lower()
        if name in result:
            raise TableProjectionError(
                f"table HTML attribute is duplicated: {name}"
            )
        result[name] = value
    return result


def _span_value(value: str | None) -> int:
    if value is None:
        return 1
    if not value.isdigit() or (parsed := int(value)) < 1:
        raise TableProjectionError("table rowspan/colspan must be positive")
    return parsed


def _collapse(value: str) -> str:
    return _WS_RE.sub(" ", value).strip()


def collect_table_media_sources(value: str) -> tuple[str, ...]:
    """Collect every declared media source of one table carrier, in order.

    Integrity closures need the raw sources (registered files, data URIs)
    even though reader-visible equality treats occurrences as opaque; a
    hidden occurrence still names an artifact that must be well-formed.
    """

    class _Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.sources: list[str] = []

        def handle_starttag(
            self,
            tag: str,
            attrs: list[tuple[str, str | None]],
        ) -> None:
            if tag.lower() != "img":
                return
            for name, attr_value in attrs:
                if name.lower() == "src" and attr_value is not None:
                    self.sources.append(attr_value)

        def handle_startendtag(
            self,
            tag: str,
            attrs: list[tuple[str, str | None]],
        ) -> None:
            self.handle_starttag(tag, attrs)

    collector = _Collector()
    collector.feed(value)
    collector.close()
    return tuple(collector.sources)


__all__ = [
    "READER_VISIBLE_TABLE_PROJECTION_VERSION",
    "collect_table_media_sources",
    "TableProjectionError",
    "VisibleCell",
    "VisibleItem",
    "VisibleTableProjection",
    "project_table_html",
]
