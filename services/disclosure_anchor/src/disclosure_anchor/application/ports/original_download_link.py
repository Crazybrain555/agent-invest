"""Recorded publisher links, separate from archived-file access capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class OriginalDownloadLinkRecord:
    document_id: str
    provider: str | None
    provider_document_id: str | None
    raw_file_hash: str | None
    source_ref: str | None
    recorded_url: str | None
    recorded_at: datetime | None


class OriginalDownloadLinkReader(Protocol):
    def read(self, document_id: str) -> OriginalDownloadLinkRecord | None: ...
