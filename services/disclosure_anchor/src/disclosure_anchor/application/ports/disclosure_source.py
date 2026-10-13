"""Disclosure source ports for provider-backed disclosure ingestion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol


@dataclass(frozen=True)
class SourceSecurity:
    """Provider query identity for one listed security."""

    security_code: str
    exchange: str
    security_name: str | None = None


@dataclass(frozen=True)
class DisclosureWindow:
    """Inclusive local-date window used for provider index sync."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("disclosure window end must be on or after start")


@dataclass(frozen=True)
class AnnouncementRef:
    """Standardized announcement candidate returned by a source adapter.

    ``filing_type`` is mapped by the source adapter (provider vocabularies stay
    in the adapter layer); ``None`` means unmapped and consumers fall back to
    ``"other"``.
    """

    provider: str
    provider_document_id: str
    title: str
    download_url: str
    raw_category: str
    announcement_date: date
    security_code: str
    security_name: str | None
    file_size: int | float | str | None
    index_updated_at: datetime | None
    filing_type: str | None = None
    report_period: str | None = None
    # Decoded F006V category names (adapter-resolved); None on the web channel.
    category_names: list[str] | None = None
    object_id: int | str | None = None
    rec_id: str | None = None
    format: str | None = None
    market_code: str | None = None
    market_name: str | None = None
    provider_org_id: str | None = None
    raw_record: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceCompanyProfile:
    """Provider company profile used for subject resolution during sync."""

    security_code: str
    security_name: str
    legal_name: str
    provider_org_id: str | None
    uscc: str | None


class PdfDownloadSink(Protocol):
    """Destination for the body of one logical PDF download.

    The source adapter owns transfer completion; the sink owns the bytes,
    their hash and local capacity. ``begin_attempt`` precedes every attempt's
    first body byte and discards whatever an earlier attempt wrote, so a retry
    never appends to a partial body. ``declared_byte_count`` is the framed
    body length when the response declared one; it is an early hint, never a
    substitute for counting the bytes actually written.
    """

    def begin_attempt(self, *, declared_byte_count: int | None) -> None:
        ...

    def write(self, chunk: bytes) -> None:
        ...


@dataclass(frozen=True)
class CompletedPdfTransfer:
    """The final attempt's body reached EOF within the logical download budget.

    ``byte_count`` is what that attempt wrote into the sink after its
    ``begin_attempt``; ``declared_byte_count`` is the length it announced.
    """

    byte_count: int
    declared_byte_count: int | None

    def __post_init__(self) -> None:
        if self.byte_count < 0:
            raise ValueError("completed transfer byte_count must be non-negative")
        if self.declared_byte_count is not None and self.declared_byte_count < 0:
            raise ValueError("declared_byte_count must be non-negative")


class DisclosureSourcePort(Protocol):
    """Provider adapter boundary for index search and PDF download."""

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
    ) -> list[AnnouncementRef]:
        ...

    def download_pdf_to(
        self, ref: AnnouncementRef, sink: PdfDownloadSink
    ) -> CompletedPdfTransfer:
        """Stream the PDF body into ``sink``; return only after a complete body.

        Provider and deadline failures raise ``SourceRequestError``; errors
        raised by the sink propagate unchanged.
        """
        ...
