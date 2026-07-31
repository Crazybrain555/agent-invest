"""Shared comparison space for provider/native text matching.

Payloads stay verbatim; every matching layer folds width/compatibility
and evidence-proven glyph variants here, in one place, so builder and
audit derive the same equivalences without importing each other.
"""

from __future__ import annotations

import unicodedata

# Evidence-proven glyph variants only (never payload rewrites):
# U+F052 — Wingdings checked box mapped into the private use area.
# U+2610 — ballot box the native layer yields for the provider's U+25A1.
_GLYPH_EQUIVALENCE = str.maketrans({"\uf052": "\u2611", "\u2610": "\u25a1"})


def fold_provider_markup(value: str) -> str:
    """Fold serializer markup that never reaches the native text layer.

    The provider wraps inline equations in configured ``$`` delimiters and
    escapes markdown punctuation with a backslash (``\\%``, ``\\*``,
    ``\\$``); the native layer carries the bare characters. Folding is a
    comparison-space equivalence only — payloads stay verbatim.
    """

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


def comparison_text(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize(
            "NFKC", fold_provider_markup(value).translate(_GLYPH_EQUIVALENCE)
        )
        if not char.isspace()
    )
