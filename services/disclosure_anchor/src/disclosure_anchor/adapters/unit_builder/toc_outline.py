"""In-document TOC parsing shared by the unit builder and the TOC audit.

A table-of-contents page is the document's own outline declaration. In the
degenerate parser regime it is often the only in-document evidence that an
unnumbered body heading ("管理层讨论与分析") is a top-level section whose
statutory form ("第三章 管理层讨论与分析") appears nowhere else but the TOC
and page furniture.
"""

from __future__ import annotations

import re
from typing import Iterable

_TRAILING_PAGE_RE = re.compile(
    r"^(?P<title>.+?)[\s.·…‥、_-]*(?P<page>\d{1,4})[\s\-–－]*$"
)
_LEADING_PAGE_RE = re.compile(r"^(?P<page>\d{1,4})\s+(?P<title>\S.*?)\s*$")
_NUMERIC_ONLY_RE = re.compile(r"^[\d\s./·…-]*$")
_SECTION_ENUMERATOR_RE = re.compile(r"^第\s*[一二三四五六七八九十百]+\s*[节章]\s*")

# A block is TOC-shaped only when it declares this many entries; shorter
# lists (release schedules, cross-references) must not define the outline.
_MIN_TOC_TITLES = 5


def strip_section_enumerator(text: str) -> str:
    """Drop a statutory 第X章/第X节 prefix; body openers often omit it."""

    return _SECTION_ENUMERATOR_RE.sub("", text.strip())


def normalize_section_title(text: str) -> str:
    return re.sub(r"[\s.·…‥、_（）()：:]+", "", text)


def _candidate_lines(toc_text: str) -> list[str]:
    lines = []
    for raw in toc_text.splitlines():
        line = raw.strip()
        if len(line) >= 2 and not _NUMERIC_ONLY_RE.match(line):
            lines.append(line)
    return lines


def _join_wrapped_lines(lines: list[str]) -> list[str]:
    """Repair a long TOC entry wrapped onto two physical lines.

    A wrapped head carries no page number of its own; joining it with the
    following line yields one parseable entry ("四、本次权益变动…不超过公 /
    司已发行股份 5% 的情况....8"). Only an unparseable head followed by a
    line whose concatenation parses is joined, so normal entries never merge.
    """

    joined: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        head_parses = bool(
            _TRAILING_PAGE_RE.match(line) or _LEADING_PAGE_RE.match(line)
        )
        if not head_parses and index + 1 < len(lines):
            candidate = line + lines[index + 1]
            if _TRAILING_PAGE_RE.match(candidate) or _LEADING_PAGE_RE.match(
                candidate
            ):
                joined.append(candidate)
                index += 2
                continue
        joined.append(line)
        index += 1
    return joined


def parse_toc_titles(toc_text: str) -> tuple[list[str], int]:
    """Extract section titles from a TOC page's text.

    Real TOCs come in two line grammars — "title …… page" and "page title" —
    and one document sticks to one of them, so the majority grammar wins per
    document. Returns (titles, unparsed_line_count).
    """

    lines = _candidate_lines(toc_text)
    lines = _join_wrapped_lines(lines)
    trailing = [_TRAILING_PAGE_RE.match(line) for line in lines]
    leading = [_LEADING_PAGE_RE.match(line) for line in lines]

    def _titles(matches: list[re.Match[str] | None]) -> list[str]:
        found = []
        for match in matches:
            if match is None:
                continue
            title = match.group("title").strip()
            if len(title) >= 2 and not _NUMERIC_ONLY_RE.match(title):
                found.append(title)
        return found

    trailing_titles = _titles(trailing)
    leading_titles = _titles(leading)
    titles = (
        leading_titles
        if len(leading_titles) > len(trailing_titles)
        else trailing_titles
    )
    return titles, len(lines) - len(titles)


_SUB_ENTRY_PREFIX_RE = re.compile(r"^[\d(（]")

# A block harvests unprefixed entries only when it proves itself a statutory
# TOC by carrying this many 第X章/第X节 entries; page-numbered feature boxes
# ("热点问题一…") mimic the line grammar but never carry the enumerators.
_MIN_ENUMERATED_ENTRIES = 2


def toc_declared_root_keys(text_blocks: Iterable[str]) -> frozenset[str]:
    """Normalized top-level section names declared by TOC-shaped blocks.

    Enumerator-prefixed entries (第X章/第X节 …) are top-level by statute and
    are returned enumerator-stripped so prefix-less body openers can match.
    Unprefixed entries without sub-entry numbering (释义, 附表：…) count as
    declared only inside a block that also carries the statutory enumerators;
    numbered sub-entries (3.1 …, (1) …) never do.
    """

    keys: set[str] = set()
    for block in text_blocks:
        titles, _unparsed = parse_toc_titles(block)
        if len(titles) < _MIN_TOC_TITLES:
            continue
        prefixed = [
            title
            for title in titles
            if strip_section_enumerator(title) != title.strip()
        ]
        if len(prefixed) < _MIN_ENUMERATED_ENTRIES:
            continue
        for title in titles:
            stripped = strip_section_enumerator(title)
            if stripped == title.strip() and _SUB_ENTRY_PREFIX_RE.match(
                stripped
            ):
                continue
            key = normalize_section_title(stripped)
            if len(key) >= 2:
                keys.add(key)
    return frozenset(keys)
