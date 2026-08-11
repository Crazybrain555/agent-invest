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
