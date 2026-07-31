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


def comparison_text(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize(
            "NFKC", value.translate(_GLYPH_EQUIVALENCE)
        )
        if not char.isspace()
    )
