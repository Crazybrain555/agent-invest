"""Expose a recorded official URL without downloading or scheduling work."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from disclosure_anchor.application.ports.original_download_link import (
    OriginalDownloadLinkReader,
)
from disclosure_anchor.domain.errors import DisclosureAnchorError


class DownloadLinkDocumentNotFound(DisclosureAnchorError):
    pass


def supported_publisher_url(provider: str | None, url: str) -> bool:
    """Validate the recorded source; never manufacture or rewrite a URL."""
    if provider != "cninfo" or len(url) > 4096 or any(ord(c) <= 32 for c in url):
        return False
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in {"http", "https"}
            and parts.hostname == "static.cninfo.com.cn"
            and parts.port is None
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and "\\" not in url
            and parts.path.lower().endswith(".pdf")
        )
    except ValueError:
        return False


class ReadOriginalDownloadLink:
    def __init__(self, reader: OriginalDownloadLinkReader) -> None:
        self._reader = reader

    def execute(self, document_id: str) -> dict[str, Any]:
        record = self._reader.read(document_id)
        if record is None:
            raise DownloadLinkDocumentNotFound("document not found")
        url = record.recorded_url
        reason = None
        if not url:
            reason = "no_matching_recorded_url"
        elif not supported_publisher_url(record.provider, url):
            reason = "unsupported_publisher_url"
        available = reason is None
        return {
            "contract_version": "disclosure-download-link.v1",
            "document_id": record.document_id,
            "provider": record.provider,
            "provider_document_id": record.provider_document_id,
            "raw_file_hash": record.raw_file_hash,
            "source_ref": record.source_ref,
            "url": url if available else None,
            "url_source": "recorded_official_index" if available else None,
            "recorded_at": (
                record.recorded_at.isoformat()
                if available and record.recorded_at is not None else None
            ),
            "unavailable_reason": reason,
            "link_live_checked": False,
            "hash_verification_required": record.raw_file_hash is not None,
            "notes": [
                "Official content may change or the link may expire. Verify downloaded "
                "bytes against raw_file_hash before calling it the same archived original.",
                "Native PDF tools may inspect images and layout; quick text and formal "
                "Units remain separate representations.",
            ],
        }
