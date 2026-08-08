"""Representation-level text normalization shared across builder and audit.

Two carriers are "the same" for exact-duplicate/projection comparison when they
differ only in Unicode representation, letter case, LaTeX tilde escaping, or
whitespace.  Keeping this normalization in the domain layer lets the unit
builder and its independent audit apply byte-identical rules without importing
each other.  Domain code stays free of IO/framework dependencies (stdlib only).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


_SOURCE_GLYPH_EQUIVALENCE = str.maketrans(
    {
        "\uf052": "\u2611",
        "\u2610": "\u25a1",
    }
)


def comparison_text(value: str) -> str:
    """Normalize only representation-level differences for exact comparison."""

    normalized = unicodedata.normalize("NFKC", value).casefold().replace(r"\~", "~")
    return re.sub(r"\s+", "", normalized)


def fold_provider_markup(value: str) -> str:
    """Remove provider-only Markdown/inline-equation serialization marks."""

    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if (
            char == "\\"
            and index + 1 < len(value)
            and not value[index + 1].isalnum()
        ):
            if value[index + 1] != "$":
                output.append(value[index + 1])
            index += 2
            continue
        if char == "$":
            index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def source_occurrence_comparison_text(value: str) -> str:
    """Normalize native/provider source strings without rewriting payloads."""

    return "".join(
        char
        for char in unicodedata.normalize(
            "NFKC",
            fold_provider_markup(value).translate(_SOURCE_GLYPH_EQUIVALENCE),
        )
        if not char.isspace()
    )


def strict_source_comparison_text(value: str) -> str:
    """Fold harmless serialization only; never equate undecoded glyphs.

    This comparison is safe for deciding that an owner already supplies a
    search surface.  It deliberately does not case-fold and does not apply the
    glyph-equivalence candidate map: neither operation proves occurrence
    identity or a font-bound semantic decode.
    """

    return "".join(
        char
        for char in unicodedata.normalize("NFKC", fold_provider_markup(value))
        if not char.isspace()
    )


def source_carrier_search_surfaces(
    element: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return separate semantic fields that can route search to one owner.

    Surfaces remain separate so a residual cannot match by crossing a cell,
    caption, footnote, or field boundary.  A match is only an owner-level
    search redirect; it is never an assertion that two extractor outputs are
    the same physical occurrence.
    """

    values: list[str] = []
    text = element.get("text")
    if isinstance(text, str):
        values.append(text)
    if element.get("kind") == "table":
        for field in ("table_caption", "table_footnote"):
            raw = element.get(field)
            if isinstance(raw, Sequence) and not isinstance(
                raw, (str, bytes, bytearray)
            ):
                values.extend(str(item) for item in raw)
        values.extend(_nested_scalar_text(element.get("table")))
    return tuple(
        normalized
        for value in values
        if (normalized := strict_source_comparison_text(value))
    )


def source_carrier_comparison_text(element: Mapping[str, Any]) -> str:
    """Return the exact-comparison surface of one NormalizedIR carrier."""

    values: list[str] = []
    text = element.get("text")
    if isinstance(text, str):
        values.append(text)
    if element.get("kind") == "table":
        table_html = element.get("table_html")
        if isinstance(table_html, str):
            values.append(table_html)
        for field in ("table_caption", "table_footnote"):
            raw = element.get(field)
            if isinstance(raw, Sequence) and not isinstance(
                raw, (str, bytes, bytearray)
            ):
                values.extend(str(item) for item in raw)
        if not isinstance(table_html, str) or not table_html:
            values.extend(_nested_scalar_text(element.get("table")))
    return source_occurrence_comparison_text("\n".join(values))


def _nested_scalar_text(value: object) -> list[str]:
    if isinstance(value, Mapping):
        output: list[str] = []
        for item in value.values():
            output.extend(_nested_scalar_text(item))
        return output
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        output = []
        for item in value:
            output.extend(_nested_scalar_text(item))
        return output
    return [str(value)] if value is not None else []
