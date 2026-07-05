"""Disclosure source ports for provider-backed disclosure ingestion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


class DisclosureSourcePort(Protocol):
    """Provider adapter boundary for index search and PDF download."""

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
        categories: Sequence[str] | None = None,
    ) -> list[AnnouncementRef]:
        ...

    def download_pdf(self, ref: AnnouncementRef) -> bytes:
        ...
