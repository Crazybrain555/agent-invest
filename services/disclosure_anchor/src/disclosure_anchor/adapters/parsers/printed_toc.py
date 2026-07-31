"""Printed table-of-contents witness for outline corroboration.

The document's own printed TOC is intrinsic structure evidence: a group of
lines each declaring ``<title> ...leader... <page number>``. The witness
follows the ICDAR book-structure recipe — recognize TOC-shaped line groups
by their form, fit one logical-to-physical page offset for the whole group,
then validate every entry individually by finding its title on the declared
page. Only individually validated entries corroborate anything; there is no
group-level vote and no vocabulary.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from disclosure_anchor.adapters.parsers.comparison import comparison_text
from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextAtom,
    NativeTextPage,
)
from disclosure_anchor.application.contracts.source_evidence_occurrence import (
    punctuation_only_text,
)

# A leader run is a purely non-alphanumeric atom of at least this many
# characters (dot/dash leaders). Shorter marks are ordinary punctuation.
_MIN_LEADER_LENGTH = 3
# Without a leader, the declared number must sit clearly apart from the
# title: at least this fraction of the page width.
_MIN_NUMBER_GAP_FRACTION = 0.12
# A trustworthy TOC group declares at least this many entries.
_MIN_GROUP_ENTRIES = 3
# Physical location tolerance around ``declared + offset``, in pages.
_PAGE_TOLERANCE = 1
_MAX_PAGE_DIGITS = 4


@dataclass(frozen=True)
class PrintedTocEntry:
    """One individually validated printed-TOC line."""

    toc_page_idx: int
    title_comparison: str
    declared_page: int
    resolved_page_idx: int


@dataclass(frozen=True)
class PrintedTocWitness:
    entries: tuple[PrintedTocEntry, ...]
    page_offset: int

    def corroborates(self, comparison_value: str, page_idx: int) -> bool:
        """True when a validated entry names this carrier on its page."""

        return any(
            entry.title_comparison == comparison_value
            and abs(entry.resolved_page_idx - page_idx) <= _PAGE_TOLERANCE
            for entry in self.entries
        )


@dataclass(frozen=True)
class _TocLine:
    page_idx: int
    title_comparison: str
    declared_page: int


def printed_toc_witness(
    source_pages: tuple[NativeTextPage, ...],
    *,
    carrier_pages_by_comparison: dict[str, tuple[int, ...]],
) -> PrintedTocWitness | None:
    """Recognize, calibrate and validate the document's printed TOC.

    ``carrier_pages_by_comparison`` maps each text carrier's comparison
    value to the physical pages it appears on; entries validate only
    against real carrier occurrences, never against raw page text.
    """

    groups = _toc_line_groups(source_pages)
    best: PrintedTocWitness | None = None
    for group in groups:
        witness = _validated_group(
            group,
            carrier_pages_by_comparison=carrier_pages_by_comparison,
        )
        if witness is None:
            continue
        if best is None or len(witness.entries) > len(best.entries):
            best = witness
    return best


def _toc_line_groups(
    source_pages: tuple[NativeTextPage, ...],
) -> list[list[_TocLine]]:
    groups: list[list[_TocLine]] = []
    current: list[_TocLine] = []
    for page in source_pages:
        lines: dict[tuple[int, int, int], list[NativeTextAtom]] = defaultdict(
            list
        )
        for atom in page.atoms:
            lines[atom.layout.line_ref].append(atom)
        for _line_ref, atoms in sorted(
            lines.items(),
            key=lambda item: min(atom.order for atom in item[1]),
        ):
            entry = _toc_line(page, sorted(atoms, key=lambda atom: atom.order))
            if entry is None:
                if len(current) >= _MIN_GROUP_ENTRIES:
                    groups.append(current)
                current = []
                continue
            current.append(entry)
    if len(current) >= _MIN_GROUP_ENTRIES:
        groups.append(current)
    return groups


def _toc_line(
    page: NativeTextPage,
    atoms: list[NativeTextAtom],
) -> _TocLine | None:
    if len(atoms) < 2:
        return None
    number_atom = atoms[-1]
    digits = number_atom.text
    if (
        not digits
        or len(digits) > _MAX_PAGE_DIGITS
        or not all(char.isdigit() for char in digits)
    ):
        return None
    declared_text = comparison_text(digits)
    if not declared_text or not declared_text.isdigit():
        return None
    declared_page = int(declared_text)
    if declared_page < 1:
        return None
    body_atoms = atoms[:-1]
    leaders = [
        atom
        for atom in body_atoms
        if len(atom.text) >= _MIN_LEADER_LENGTH
        and punctuation_only_text(atom.text)
    ]
    leader_ids = {id(atom) for atom in leaders}
    title_atoms = [atom for atom in body_atoms if id(atom) not in leader_ids]
    if not title_atoms:
        return None
    title = comparison_text("".join(atom.text for atom in title_atoms))
    if not title or punctuation_only_text(title) or title.isdigit():
        return None
    if not leaders:
        gap = number_atom.bbox[0] - max(atom.bbox[2] for atom in title_atoms)
        if gap < _MIN_NUMBER_GAP_FRACTION * page.width:
            return None
    return _TocLine(
        page_idx=page.page_idx,
        title_comparison=title,
        declared_page=declared_page,
    )


def _validated_group(
    group: list[_TocLine],
    *,
    carrier_pages_by_comparison: dict[str, tuple[int, ...]],
) -> PrintedTocWitness | None:
    declared = [line.declared_page for line in group]
    if any(later < earlier for earlier, later in zip(declared, declared[1:])):
        return None
    votes: Counter[int] = Counter()
    for line in group:
        for page_idx in carrier_pages_by_comparison.get(
            line.title_comparison, ()
        ):
            votes[page_idx - line.declared_page] += 1
    if not votes:
        return None
    # One shared calibration parameter for the group; every entry is then
    # accepted or rejected on its own against the fitted offset.
    top = max(votes.values())
    page_offset = min(
        offset for offset, count in votes.items() if count == top
    )
    entries: list[PrintedTocEntry] = []
    for line in group:
        resolved = next(
            (
                page_idx
                for page_idx in carrier_pages_by_comparison.get(
                    line.title_comparison, ()
                )
                if abs(page_idx - (line.declared_page + page_offset))
                <= _PAGE_TOLERANCE
            ),
            None,
        )
        if resolved is None:
            continue
        entries.append(
            PrintedTocEntry(
                toc_page_idx=line.page_idx,
                title_comparison=line.title_comparison,
                declared_page=line.declared_page,
                resolved_page_idx=resolved,
            )
        )
    if len(entries) < _MIN_GROUP_ENTRIES:
        return None
    return PrintedTocWitness(entries=tuple(entries), page_offset=page_offset)
