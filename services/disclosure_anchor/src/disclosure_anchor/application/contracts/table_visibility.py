"""One visibility policy for every table parsing lane.

The reader-visible comparison and the published grid derivation must see
the same visible domain: invisible-content tags and ``hidden`` subtrees
contribute nothing to either, a hidden void element hides only itself,
and any markup that could change visibility beyond the supported HTML
``hidden`` attribute fails closed instead of being guessed at.
"""

from __future__ import annotations

VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
INVISIBLE_CONTENT_TAGS = frozenset(
    {"script", "style", "template", "noscript"}
)
_STRUCTURAL_TAGS = frozenset(
    {"table", "caption", "thead", "tbody", "tfoot", "tr", "td", "th"}
)
# The closed set of style properties known to never change visibility or
# layout-driven legibility. Anything else — display, visibility,
# content-visibility, opacity, clip, positioning, sizing — fails closed.
_ALLOWED_STYLE_PROPERTIES = frozenset(
    {
        "border",
        "border-bottom",
        "border-collapse",
        "border-color",
        "border-left",
        "border-right",
        "border-style",
        "border-top",
        "border-width",
        "font-family",
        "font-style",
        "font-weight",
        "text-align",
        "text-decoration",
        "vertical-align",
    }
)


class TableVisibilityError(ValueError):
    """Markup could change visibility in a way this contract cannot prove."""


def require_supported_markup(
    tag: str,
    attrs: dict[str, str | None],
) -> None:
    """Fail closed on visibility-affecting markup outside the contract."""

    style = attrs.get("style")
    if style is not None:
        for declaration in style.split(";"):
            name = declaration.split(":", 1)[0].strip().lower()
            if not name:
                continue
            if name not in _ALLOWED_STYLE_PROPERTIES:
                raise TableVisibilityError(
                    "unsupported visibility-relevant style property "
                    f"{name!r} on <{tag}>"
                )
    if "hidden" in attrs and tag in _STRUCTURAL_TAGS:
        raise TableVisibilityError(
            f"hidden structural element <{tag}> is unsupported"
        )


class VisibilityTracker:
    """Track invisible-content and hidden ancestry across one parse.

    Void elements never enter the stack: a hidden void element hides only
    itself and can never swallow following siblings.
    """

    def __init__(self) -> None:
        self._stack: list[tuple[str, bool, bool]] = []
        self._invisible_depth = 0
        self._hidden_depth = 0

    def enter(self, tag: str, attrs: dict[str, str | None]) -> bool:
        """Register a start tag; return True when the ELEMENT is visible.

        For void tags nothing is pushed and the return value covers the
        element itself; for container tags the return value reflects the
        state after entering.
        """

        require_supported_markup(tag, attrs)
        self_hidden = "hidden" in attrs
        if tag in VOID_TAGS:
            return self.visible and not self_hidden
        invisible = tag in INVISIBLE_CONTENT_TAGS
        self._stack.append((tag, invisible, self_hidden))
        if invisible:
            self._invisible_depth += 1
        if self_hidden:
            self._hidden_depth += 1
        return self.visible

    def leave(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        while self._stack:
            top_tag, invisible, hidden = self._stack.pop()
            if invisible:
                self._invisible_depth -= 1
            if hidden:
                self._hidden_depth -= 1
            if top_tag == tag:
                return

    @property
    def visible(self) -> bool:
        return not self._invisible_depth and not self._hidden_depth


__all__ = [
    "INVISIBLE_CONTENT_TAGS",
    "TableVisibilityError",
    "VOID_TAGS",
    "VisibilityTracker",
    "require_supported_markup",
]
