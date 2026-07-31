"""Deterministic visible-text projection for parser-owned HTML evidence."""

from __future__ import annotations

from html.parser import HTMLParser


_NON_VISIBLE_ELEMENTS = frozenset(
    {"script", "style", "template", "noscript"}
)


class _VisibleTextParser(HTMLParser):
    """Collect character data without leaking markup into retrieval text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0
        self._cell_parts: list[str] = []
        self._cell_fragments: list[str] | None = None
        self._saw_cell = False

    def _flush_cell(self) -> None:
        if self._cell_fragments is None:
            return
        value = " ".join(self._cell_fragments)
        if value:
            self._cell_parts.append(value)
        self._cell_fragments = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.lower() in _NON_VISIBLE_ELEMENTS:
            self._suppressed_depth += 1
            return
        if tag.lower() in {"caption", "td", "th"}:
            self._flush_cell()
            self._cell_fragments = []
            self._saw_cell = True

    def handle_endtag(self, tag: str) -> None:
        if (
            tag.lower() in _NON_VISIBLE_ELEMENTS
            and self._suppressed_depth > 0
        ):
            self._suppressed_depth -= 1
            return
        if tag.lower() in {"caption", "td", "th"}:
            self._flush_cell()

    def handle_data(self, data: str) -> None:
        if self._suppressed_depth:
            return
        collapsed = " ".join(data.split())
        if collapsed:
            self.parts.append(collapsed)
            if self._cell_fragments is not None:
                self._cell_fragments.append(collapsed)

    def close(self) -> None:
        super().close()
        self._flush_cell()

    def hard_segments(self) -> tuple[str, ...]:
        if self._saw_cell:
            return tuple(self._cell_parts)
        text = " ".join(self.parts)
        return (text,) if text else ()


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
