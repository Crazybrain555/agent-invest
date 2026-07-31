"""Source-bound payload annotations that never decide document structure."""

from __future__ import annotations

import re
import unicodedata


GIBBERISH_RATIO_MAX = 0.30

_CHECKED = "√☑✓"
_UNCHECKED = "□☐"
_APPLICABLE_RE = re.compile(
    rf"[{_CHECKED}]\s*适\s*用\s*[{_UNCHECKED}]\s*不\s*适\s*用\s*[。.]?\s*$"
)
_NOT_APPLICABLE_RE = re.compile(
    rf"[{_UNCHECKED}]\s*适\s*用\s*[{_CHECKED}]\s*不\s*适\s*用\s*[。.]?\s*$"
)
_UNIT_DECLARATION_LABEL = (
    r"(?:(?:数\s*量|金\s*额|货\s*币|计\s*量)\s*)?单\s*位|币\s*种"
)
_UNIT_DECLARATION_RE = re.compile(
    rf"(?P<label>{_UNIT_DECLARATION_LABEL})"
    r"\s*(?:均\s*)?(?:为|是|指|以)?\s*[：:]\s*"
)


def parse_unit_declarations(line: str) -> tuple[tuple[str, str], ...]:
    """Parse complete unit/currency declarations without a value vocabulary."""

    text = unicodedata.normalize("NFKC", line).strip()
    if text.startswith(("(", "（")) and text.endswith((")", "）")):
        text = text[1:-1].strip()
    text = re.sub(r"^本\s*表\s*", "", text, count=1)
    text = re.sub(r"[。.]$", "", text).strip()
    matches = list(_UNIT_DECLARATION_RE.finditer(text))
    if not matches or text[: matches[0].start()].strip():
        return ()
    output: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip()
        if (
            not value
            or "\n" in value
            or "\r" in value
            or re.search(r"[，,；;。:：]", value)
        ):
            return ()
        output.append((re.sub(r"\s+", "", match.group("label")), value))
    return tuple(output)


def classify_marker_line(line: str) -> str | None:
    """Classify a complete applicability declaration as a payload annotation."""

    if _APPLICABLE_RE.search(line):
        return "applicable"
    if _NOT_APPLICABLE_RE.search(line):
        return "not_applicable"
    return None


def is_pure_marker_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    match = _APPLICABLE_RE.search(stripped) or _NOT_APPLICABLE_RE.search(stripped)
    return match is not None and match.start() == 0


__all__ = [
    "GIBBERISH_RATIO_MAX",
    "classify_marker_line",
    "is_pure_marker_line",
    "parse_unit_declarations",
]
