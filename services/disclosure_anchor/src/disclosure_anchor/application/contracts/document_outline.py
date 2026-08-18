"""Small DB-free records for the greenfield document outline."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from disclosure_anchor.application.contracts.provider_document import ProviderBBox


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

HeadingHintSource = Literal["bookmark", "printed_toc", "pdf_style"]
HeadingNegativeReason = Literal["page_continuation"]
HeadingDisposition = Literal["accepted", "demoted"]
HeadingDispositionReason = Literal[
    "accepted",
    "table_contained",
    "checkbox_selector",
    "selector_statement",
    "page_continuation",
    "body_text_conflict",
    "terminal_signature",
    "non_semantic_glyph",
    "repeated_page_header",
]
HeadingPlacementSource = Literal[
    "bookmark",
    "printed_toc",
    "numbering",
    "pdf_style",
    "provider",
    "provider_style",
    "flattened",
]


@dataclass(frozen=True, slots=True)
class HeadingLevelHint:
    """A level signal already bound to one exact provider block."""

    source_pdf_sha256: str
    source_index: int
    raw_block_sha256: str
    source: HeadingHintSource
    level: int

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.source_pdf_sha256):
            raise ValueError("heading hint source PDF hash must be canonical")
        if self.source_index < 0:
            raise ValueError("heading hint source index cannot be negative")
        if not _SHA256_RE.fullmatch(self.raw_block_sha256):
            raise ValueError("heading hint block hash must be canonical")
        if self.source not in {"bookmark", "printed_toc", "pdf_style"}:
            raise ValueError("heading hint source is unsupported")
        if self.level < 1:
            raise ValueError("heading hint level must be positive")


@dataclass(frozen=True, slots=True)
class HeadingNegativeHint:
    """A hard-negative signal already bound to one exact provider block."""

    source_pdf_sha256: str
    source_index: int
    raw_block_sha256: str
    reason: HeadingNegativeReason

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.source_pdf_sha256):
            raise ValueError("heading negative source PDF hash must be canonical")
        if self.source_index < 0:
            raise ValueError("heading negative source index cannot be negative")
        if not _SHA256_RE.fullmatch(self.raw_block_sha256):
            raise ValueError("heading negative block hash must be canonical")
        if self.reason != "page_continuation":
            raise ValueError("heading negative reason is unsupported")


@dataclass(frozen=True, slots=True)
class HeadingCandidate:
    """One source occurrence considered for the outline."""

    heading_id: str
    source_index: int
    payload_ordinal: int
    page_index: int
    bbox: ProviderBBox | None
    text: str
    raw_block_sha256: str
    provider_level: int | None
    numbering_family: str | None
    nominal_rank: int | None
    disposition: HeadingDisposition
    disposition_reason: HeadingDispositionReason
    placement_source: HeadingPlacementSource | None

    def __post_init__(self) -> None:
        if not self.heading_id:
            raise ValueError("heading candidate id must be non-empty")
        if self.source_index < 0 or self.payload_ordinal < 0 or self.page_index < 0:
            raise ValueError("heading candidate indices cannot be negative")
        if not self.text:
            raise ValueError("heading candidate text must be non-empty")
        if not _SHA256_RE.fullmatch(self.raw_block_sha256):
            raise ValueError("heading candidate block hash must be canonical")
        if self.provider_level is not None and self.provider_level < 0:
            raise ValueError("heading candidate provider level cannot be negative")
        if self.nominal_rank is not None and self.nominal_rank < 1:
            raise ValueError("heading candidate nominal rank must be positive")
        if self.disposition_reason not in {
            "accepted",
            "table_contained",
            "checkbox_selector",
            "selector_statement",
            "page_continuation",
            "body_text_conflict",
            "terminal_signature",
            "non_semantic_glyph",
            "repeated_page_header",
        }:
            raise ValueError("heading candidate disposition reason is unsupported")
        if self.disposition == "accepted":
            if self.disposition_reason != "accepted":
                raise ValueError("accepted heading has a demotion reason")
            if self.nominal_rank is None or self.placement_source is None:
                raise ValueError("accepted heading has no placement")
        elif self.nominal_rank is not None or self.placement_source is not None:
            raise ValueError("demoted heading cannot have a placement")


@dataclass(frozen=True, slots=True)
class ResolvedHeading:
    """One accepted heading placed by an ordered monotonic parent stack.

    ``nominal_rank`` retains the winning source signal. A provider-only leaf does
    not enter the reliable parent stack, so this value is not an asserted tree
    depth; ``level`` and ``parent_heading_id`` describe the resolved placement.
    """

    heading_id: str
    source_index: int
    payload_ordinal: int
    page_index: int
    bbox: ProviderBBox | None
    text: str
    nominal_rank: int
    level: int
    parent_heading_id: str | None
    headpath: tuple[str, ...]
    placement_source: HeadingPlacementSource

    def __post_init__(self) -> None:
        if not self.heading_id or not self.text:
            raise ValueError("resolved heading identity must be complete")
        if self.source_index < 0 or self.payload_ordinal < 0 or self.page_index < 0:
            raise ValueError("resolved heading indices cannot be negative")
        if self.nominal_rank < 1 or self.level < 1:
            raise ValueError("resolved heading levels must be positive")
        if not self.headpath or self.headpath[-1] != self.text:
            raise ValueError("resolved heading path must include itself")


@dataclass(frozen=True, slots=True)
class CoarseUnit:
    """One ordered diagnostic grouping; no retrieval or publication claim."""

    unit_index: int
    heading_id: str | None
    title: str | None
    headpath: tuple[str, ...]
    block_source_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.unit_index < 0:
            raise ValueError("coarse unit index cannot be negative")
        if not self.block_source_indices:
            raise ValueError("coarse unit cannot be empty")
        if tuple(sorted(self.block_source_indices)) != self.block_source_indices:
            raise ValueError("coarse unit blocks must preserve source order")
        if len(set(self.block_source_indices)) != len(self.block_source_indices):
            raise ValueError("coarse unit cannot repeat a source block")
        if self.heading_id is None:
            if self.title is not None or self.headpath:
                raise ValueError("preamble unit cannot claim a heading")
        elif self.title is None or not self.headpath:
            raise ValueError("headed unit must carry its display path")


@dataclass(frozen=True, slots=True)
class DocumentOutline:
    """Complete DB-free result of deterministic outline and coarse grouping."""

    source_pdf_sha256: str
    provider_bundle_sha256: str
    block_count: int
    candidates: tuple[HeadingCandidate, ...]
    headings: tuple[ResolvedHeading, ...]
    units: tuple[CoarseUnit, ...]

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.source_pdf_sha256):
            raise ValueError("outline source PDF hash must be canonical")
        if not _SHA256_RE.fullmatch(self.provider_bundle_sha256):
            raise ValueError("outline provider bundle hash must be canonical")
        if self.block_count < 0:
            raise ValueError("outline block count cannot be negative")
        candidate_ids = [candidate.heading_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("outline candidate ids must be unique")
        candidate_indices = [candidate.source_index for candidate in self.candidates]
        if candidate_indices != sorted(candidate_indices):
            raise ValueError("outline candidates must preserve source order")
        if any(index >= self.block_count for index in candidate_indices):
            raise ValueError("outline candidate source index is out of range")
        heading_ids = [heading.heading_id for heading in self.headings]
        if len(heading_ids) != len(set(heading_ids)):
            raise ValueError("outline heading ids must be unique")
        accepted_ids = [
            candidate.heading_id
            for candidate in self.candidates
            if candidate.disposition == "accepted"
        ]
        if heading_ids != accepted_ids:
            raise ValueError("outline headings must match accepted candidates in order")
        accepted_by_id = {
            candidate.heading_id: candidate
            for candidate in self.candidates
            if candidate.disposition == "accepted"
        }
        seen_headings: set[str] = set()
        heading_by_id: dict[str, ResolvedHeading] = {}
        for heading in self.headings:
            candidate = accepted_by_id[heading.heading_id]
            if (
                heading.source_index != candidate.source_index
                or heading.payload_ordinal != candidate.payload_ordinal
                or heading.page_index != candidate.page_index
                or heading.bbox != candidate.bbox
                or heading.text != candidate.text
                or heading.nominal_rank != candidate.nominal_rank
                or heading.placement_source != candidate.placement_source
            ):
                raise ValueError(
                    "resolved heading must preserve its accepted source candidate"
                )
            if (
                heading.parent_heading_id is not None
                and heading.parent_heading_id not in seen_headings
            ):
                raise ValueError("outline parent must precede its child")
            parent = (
                None
                if heading.parent_heading_id is None
                else heading_by_id[heading.parent_heading_id]
            )
            expected_level = 1 if parent is None else parent.level + 1
            expected_headpath = (
                (heading.text,) if parent is None else (*parent.headpath, heading.text)
            )
            if heading.level != expected_level or heading.headpath != expected_headpath:
                raise ValueError("outline heading placement is internally inconsistent")
            seen_headings.add(heading.heading_id)
            heading_by_id[heading.heading_id] = heading
        if tuple(unit.unit_index for unit in self.units) != tuple(
            range(len(self.units))
        ):
            raise ValueError("coarse unit indices must be contiguous")
        preamble_indices = [
            index for index, unit in enumerate(self.units) if unit.heading_id is None
        ]
        if preamble_indices not in ([], [0]):
            raise ValueError("a coarse outline may have only one leading preamble")
        unit_heading_ids = [
            unit.heading_id for unit in self.units if unit.heading_id is not None
        ]
        if unit_heading_ids != heading_ids:
            raise ValueError("each accepted heading must open exactly one coarse unit")
        partition = tuple(
            source_index
            for unit in self.units
            for source_index in unit.block_source_indices
        )
        if partition != tuple(range(self.block_count)):
            raise ValueError(
                "coarse units must partition every provider block in order"
            )
        for unit in self.units:
            if unit.heading_id is None:
                continue
            unit_heading = heading_by_id.get(unit.heading_id)
            if unit_heading is None:
                raise ValueError("coarse unit references an unknown heading")
            if unit_heading.source_index not in unit.block_source_indices:
                raise ValueError("headed unit does not contain its heading block")
            if (
                unit.block_source_indices[0] != unit_heading.source_index
                and unit.unit_index != 0
            ):
                raise ValueError(
                    "only the first headed unit may retain leading source blocks"
                )
            if (
                unit.title != unit_heading.text
                or unit.headpath != unit_heading.headpath
            ):
                raise ValueError("coarse unit heading display differs from the outline")


__all__ = [
    "CoarseUnit",
    "DocumentOutline",
    "HeadingCandidate",
    "HeadingDisposition",
    "HeadingDispositionReason",
    "HeadingHintSource",
    "HeadingLevelHint",
    "HeadingNegativeHint",
    "HeadingNegativeReason",
    "HeadingPlacementSource",
    "ResolvedHeading",
]
