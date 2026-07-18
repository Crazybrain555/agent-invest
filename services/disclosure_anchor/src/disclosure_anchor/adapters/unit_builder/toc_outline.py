"""In-document TOC parsing shared by the unit builder and the TOC audit.

A table-of-contents page is the document's own outline declaration. In the
degenerate parser regime it is often the only in-document evidence that a
body heading is a top-level section.

Layout accidents the grammar must survive (per the MinerU output contract):
TOC titles may arrive as text or heading blocks; entry page numbers may sit
on the same line or in a detached page-number column; top-level entries may
be enumerated with statutory 第X章/第X节 prefixes, Chinese ordinals (一、),
or single-level arabic numbering (1. / 1、/ "1 ").  What defines a TOC is
its enumeration structure, not where the page numbers were typeset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_TRAILING_PAGE_RE = re.compile(
    r"^(?P<title>.+?)[\s.·…‥、_|｜-]*(?P<page>\d{1,4})[\s\-–－]*$"
)
_LEADING_PAGE_RE = re.compile(
    r"^(?P<page>\d{1,4})[\s|｜]+(?P<title>\S.*?)\s*$"
)
_NUMERIC_ONLY_RE = re.compile(r"^[\d\s./·…-]*$")
_SECTION_ENUMERATOR_RE = re.compile(
    r"^第\s*(?P<ord>[一二三四五六七八九十百]+)\s*[节章]\s*"
)
_CHINESE_ORDINAL_PREFIX_RE = re.compile(
    r"^(?P<ord>[一二三四五六七八九十]{1,3})\s*[、.．]\s*"
)
# Single-level arabic top entries: "1. 释义", "2. 2023 年…", "1、意见",
# "1 财务摘要".  Multi-level dotted numbering ("3.1 财务回顾") never
# matches: without a space after the dot the next char must not be a digit.
_ARABIC_TOP_PREFIX_RE = re.compile(
    r"^(?P<ord>\d{1,2})(?:[.．、]\s+|[.．、](?=[^\d\s])|\s+(?=[^\d\s]))"
)
_DOTTED_SUB_RE = re.compile(r"^\d{1,3}[.．]\d")
_PAREN_SUB_RE = re.compile(r"^[（(]")
_SENTENCE_PUNCT_RE = re.compile(r"[。；！？]")

# A block is TOC-shaped only when it declares this many entries; shorter
# lists (release schedules, cross-references) must not define the outline.
_MIN_TOC_TITLES = 5

# A block harvests unprefixed entries only when it proves itself a TOC by
# carrying this many statutory 第X章/第X节 entries; page-numbered feature
# boxes ("热点问题一…") mimic the line grammar but never carry them.
_MIN_ENUMERATED_ENTRIES = 2

# Weaker enumeration families (一、 / 1.) qualify a marker-anchored block
# only as an ascending run at least this long; a TOC enumerates in order.
_MIN_ASCENDING_RUN = 3

# A marker page whose lines ALL parse as "title + page" (no residue, no
# bare lines) is a clean miniature TOC even below the general threshold:
# inquiry replies genuinely index 3-5 questions.
_MIN_CLEAN_MINI_TOC = 3

# Highest-ranked family present in a block defines its top level; lower
# families are that TOC's sub-structure (第X章 > 一、 > 1. by convention).
_FAMILY_RANK = {"statutory": 0, "chinese": 1, "arabic": 2}

_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _chinese_ordinal_value(token: str) -> int | None:
    if not token:
        return None
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(token)


@dataclass(frozen=True)
class TocEntry:
    title: str
    page: int | None
    family: str  # "statutory" | "chinese" | "arabic" | "sub" | "plain"
    ordinal: int | None


@dataclass(frozen=True)
class TocBlockAnalysis:
    qualified: bool
    keys: frozenset[str]


def strip_section_enumerator(text: str) -> str:
    """Drop a statutory 第X章/第X节 prefix; body openers often omit it."""

    return _SECTION_ENUMERATOR_RE.sub("", text.strip())


def strip_outline_enumerator(text: str) -> str:
    """Drop any single top-level enumerator (第X章 / 一、 / 1.) prefix.

    Multi-level dotted numbering ("3.1 …") is sub-structure and stays
    intact, so sub-headings never collide with declared top-level keys.
    """

    stripped = text.strip()
    for regex in (
        _SECTION_ENUMERATOR_RE,
        _CHINESE_ORDINAL_PREFIX_RE,
        _ARABIC_TOP_PREFIX_RE,
    ):
        candidate = regex.sub("", stripped, count=1)
        if candidate != stripped:
            return candidate.strip()
    return stripped


def normalize_section_title(text: str) -> str:
    return re.sub(r"[\s.·…‥、_（）()：:]+", "", text)


_LATIN_FILLER_RE = re.compile(r"[A-Za-z\s]+")


def is_toc_marker(text: str) -> bool:
    """True for a line that announces the TOC page.

    Designed (often bilingual) reports write "目录 Contents" or space the
    characters out; the marker is the CJK content, not the exact string.
    Longer titles that merely end in 目录 (备查文件目录) never count.
    """

    return normalize_section_title(_LATIN_FILLER_RE.sub("", text)) == "目录"


def _classify_title(title: str) -> tuple[str, int | None]:
    statutory = _SECTION_ENUMERATOR_RE.match(title)
    if statutory:
        return "statutory", _chinese_ordinal_value(statutory.group("ord"))
    if _DOTTED_SUB_RE.match(title) or _PAREN_SUB_RE.match(title):
        return "sub", None
    chinese = _CHINESE_ORDINAL_PREFIX_RE.match(title)
    if chinese:
        return "chinese", _chinese_ordinal_value(chinese.group("ord"))
    arabic = _ARABIC_TOP_PREFIX_RE.match(title)
    if arabic:
        return "arabic", int(arabic.group("ord"))
    return "plain", None


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


def _valid_title(title: str) -> bool:
    return len(title) >= 2 and not _NUMERIC_ONLY_RE.match(title)


def _bare_entry(line: str) -> TocEntry | None:
    family, ordinal = _classify_title(line)
    if family != "plain":
        return TocEntry(title=line, page=None, family=family, ordinal=ordinal)
    # An outline title is a short nominal phrase: sentence punctuation or a
    # trailing colon marks body text / label lines, never TOC entries.
    if (
        len(line) <= 40
        and not _SENTENCE_PUNCT_RE.search(line)
        and not line.endswith(("：", ":"))
        and not is_toc_marker(line)
    ):
        return TocEntry(title=line, page=None, family="plain", ordinal=None)
    return None


def parse_toc_entries(
    toc_text: str, *, include_bare: bool = False
) -> tuple[list[TocEntry], int]:
    """Parse a block into TOC entries, preserving reading order.

    Real TOCs use one page-number line grammar — "title …… page" or
    "page title" — so the majority grammar wins per block.  With
    ``include_bare`` (the block is known/anchored TOC territory), lines
    without any page number also count: enumerated lines always, plain
    lines only in short title shape.  Returns (entries, unparsed_count).
    """

    lines = _join_wrapped_lines(_candidate_lines(toc_text))
    trailing = [_TRAILING_PAGE_RE.match(line) for line in lines]
    leading = [_LEADING_PAGE_RE.match(line) for line in lines]

    def _paged_count(matches: list[re.Match[str] | None]) -> int:
        return sum(
            1
            for match in matches
            if match is not None and _valid_title(match.group("title").strip())
        )

    paged = (
        leading if _paged_count(leading) > _paged_count(trailing) else trailing
    )

    entries: list[TocEntry] = []
    for line, match in zip(lines, paged):
        if match is not None:
            title = match.group("title").strip()
            if _valid_title(title):
                family, ordinal = _classify_title(title)
                entries.append(
                    TocEntry(
                        title=title,
                        page=int(match.group("page")),
                        family=family,
                        ordinal=ordinal,
                    )
                )
                continue
        if include_bare:
            bare = _bare_entry(line)
            if bare is not None:
                entries.append(bare)
    return entries, len(lines) - len(entries)


def parse_toc_titles(
    toc_text: str, *, include_bare: bool = False
) -> tuple[list[str], int]:
    """Extract section titles from a TOC page's text."""

    entries, unparsed = parse_toc_entries(toc_text, include_bare=include_bare)
    return [entry.title for entry in entries], unparsed


def _top_family(entries: list[TocEntry]) -> str | None:
    present = {
        entry.family for entry in entries if entry.family in _FAMILY_RANK
    }
    if not present:
        return None
    return min(present, key=lambda family: _FAMILY_RANK[family])


def _has_ascending_run(values: list[int], minimum: int) -> bool:
    streak = 1
    for previous, current in zip(values, values[1:]):
        streak = streak + 1 if current > previous else 1
        if streak >= minimum:
            return True
    return minimum <= 1


def _qualifies(
    entries: list[TocEntry], *, marker_anchored: bool, unparsed: int = 1
) -> bool:
    if len(entries) < _MIN_TOC_TITLES:
        paged = sum(1 for entry in entries if entry.page is not None)
        return (
            marker_anchored
            and unparsed == 0
            and paged == len(entries)
            and paged >= _MIN_CLEAN_MINI_TOC
        )
    statutory = sum(1 for entry in entries if entry.family == "statutory")
    if statutory >= _MIN_ENUMERATED_ENTRIES:
        return True
    if not marker_anchored:
        return False
    # A page anchored by its own 目录 marker whose lines read "title +
    # page number" is a TOC by the document's own declaration, even with
    # no enumeration anywhere (designed reports may number nothing).
    paged = sum(1 for entry in entries if entry.page is not None)
    if paged >= _MIN_TOC_TITLES:
        return True
    top = _top_family(entries)
    if top not in ("chinese", "arabic"):
        return False
    ordinals = [
        entry.ordinal
        for entry in entries
        if entry.family == top and entry.ordinal is not None
    ]
    return len(ordinals) >= _MIN_ASCENDING_RUN and _has_ascending_run(
        ordinals, _MIN_ASCENDING_RUN
    )


def _declared_keys(entries: list[TocEntry]) -> frozenset[str]:
    top = _top_family(entries)
    keys: set[str] = set()
    # None → no enumerated entry seen yet; front matter before the first
    # top-level entry (释义, 董事长致辞) is itself top-level by position.
    last_enumerated_was_top: bool | None = None
    for entry in entries:
        if entry.family == top:
            key = normalize_section_title(strip_outline_enumerator(entry.title))
            if len(key) >= 2:
                keys.add(key)
            last_enumerated_was_top = True
        elif entry.family in ("statutory", "chinese", "arabic", "sub"):
            last_enumerated_was_top = False
        elif last_enumerated_was_top is not False:
            # A plain entry is top-level only while the TOC has not
            # descended into sub-structure (专栏/热点 features nest inside
            # a chapter; 附表 after the last chapter stays top-level).
            key = normalize_section_title(entry.title)
            if len(key) >= 2:
                keys.add(key)
    return frozenset(keys)


def analyze_toc_block(
    toc_text: str, *, marker_anchored: bool = False
) -> TocBlockAnalysis:
    """Judge one page-joined block: is it a TOC, and what does it declare?

    Bare (page-number-less) entries and the weaker enumeration gates apply
    only to marker-anchored blocks (a page carrying its own 目录 marker);
    un-anchored blocks keep the strict statutory inline-page contract.
    """

    entries, unparsed = parse_toc_entries(
        toc_text, include_bare=marker_anchored
    )
    if not _qualifies(
        entries, marker_anchored=marker_anchored, unparsed=unparsed
    ):
        return TocBlockAnalysis(qualified=False, keys=frozenset())
    return TocBlockAnalysis(qualified=True, keys=_declared_keys(entries))


def is_page_annotated_entry(line: str) -> bool:
    """True when a single line reads as "title + page number" (either
    grammar) — the shape of a TOC entry, never of a real body heading."""

    stripped = line.strip()
    if not stripped or "\n" in stripped:
        return False
    match = _TRAILING_PAGE_RE.match(stripped) or _LEADING_PAGE_RE.match(
        stripped
    )
    return match is not None and _valid_title(match.group("title").strip())


def toc_declared_root_keys(text_blocks: Iterable[str]) -> frozenset[str]:
    """Normalized top-level section names declared by TOC-shaped blocks.

    Compatibility path without marker context: only the strict statutory
    inline-page gate applies. Marker-aware callers use
    :func:`analyze_toc_block` per page instead.
    """

    keys: set[str] = set()
    for block in text_blocks:
        analysis = analyze_toc_block(block, marker_anchored=False)
        keys.update(analysis.keys)
    return frozenset(keys)
