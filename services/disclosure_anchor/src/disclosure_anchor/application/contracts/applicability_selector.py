"""Closed parsing for source-authored applicability selectors."""

from __future__ import annotations

from dataclasses import dataclass
import re


CHECKED_APPLICABILITY_MARKERS = "√☑✓\uf052"
UNCHECKED_APPLICABILITY_MARKERS = "□☐"
ALL_APPLICABILITY_MARKERS = (
    CHECKED_APPLICABILITY_MARKERS + UNCHECKED_APPLICABILITY_MARKERS
)
_HORIZONTAL_SPACE = r"[^\S\r\n]*"
_APPLICABILITY_PAIR_RE = re.compile(
    rf"(?P<applicable>[{ALL_APPLICABILITY_MARKERS}])"
    rf"{_HORIZONTAL_SPACE}适{_HORIZONTAL_SPACE}用"
    rf"{_HORIZONTAL_SPACE}"
    rf"(?P<not_applicable>[{ALL_APPLICABILITY_MARKERS}])"
    rf"{_HORIZONTAL_SPACE}不{_HORIZONTAL_SPACE}适{_HORIZONTAL_SPACE}用"
)


@dataclass(frozen=True, slots=True)
class ApplicabilitySelectorPair:
    """One ordered ``适用 / 不适用`` marker pair from the source text."""

    start: int
    end: int
    applicable_marker: str
    not_applicable_marker: str

    @property
    def applicable_checked(self) -> bool:
        return self.applicable_marker in CHECKED_APPLICABILITY_MARKERS

    @property
    def not_applicable_checked(self) -> bool:
        return self.not_applicable_marker in CHECKED_APPLICABILITY_MARKERS


def applicability_selector_pairs(
    text: str,
) -> tuple[ApplicabilitySelectorPair, ...]:
    """Return only structurally complete ordered selector pairs."""

    return tuple(
        ApplicabilitySelectorPair(
            start=match.start(),
            end=match.end(),
            applicable_marker=match.group("applicable"),
            not_applicable_marker=match.group("not_applicable"),
        )
        for match in _APPLICABILITY_PAIR_RE.finditer(text)
    )


def strip_applicability_selector_pairs(text: str) -> str:
    """Remove complete selector pairs while preserving every other source byte."""

    return _APPLICABILITY_PAIR_RE.sub("", text)


def has_single_applicability_selector_pair(text: str) -> bool:
    """Require one ordered pair and reject every additional marker."""

    pairs = applicability_selector_pairs(text)
    if len(pairs) != 1:
        return False
    residue = strip_applicability_selector_pairs(text)
    return not any(marker in residue for marker in ALL_APPLICABILITY_MARKERS)


def is_closed_applicability_selector(text: str) -> bool:
    """Require exactly one pair and no extra marker or substantive residue."""

    if not has_single_applicability_selector_pair(text):
        return False
    residue = strip_applicability_selector_pairs(text)
    return not any(character.isalnum() for character in residue)


__all__ = [
    "ALL_APPLICABILITY_MARKERS",
    "ApplicabilitySelectorPair",
    "applicability_selector_pairs",
    "has_single_applicability_selector_pair",
    "is_closed_applicability_selector",
    "strip_applicability_selector_pairs",
]
