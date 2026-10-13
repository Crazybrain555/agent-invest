"""Read rough native text of one archived original, page by page.

Read-only: it never enqueues, writes, OCRs, or stands in for formal Units. The
archived original is located from the document's own trusted record and its
recorded raw hash is the extraction identity; no caller path or URL exists.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from disclosure_anchor.application.ports.processing_requests import (
    OriginalLocatorReader,
    QuickTextExtractor,
)
from disclosure_anchor.domain.errors import DisclosureAnchorError


TEXT_CONTRACT_VERSION = "disclosure-quick-text.v1"
TEXT_KIND = "quick_native_pdf_text"
DEFAULT_MAX_CHARS = 20000
MAX_CHARS = 40000
DEFAULT_MAX_PAGES = 20
MAX_PAGES = 100
MAX_CURSOR_CHARS = 4096


class DocumentTextNotFound(DisclosureAnchorError):
    pass


class OriginalTextUnavailable(DisclosureAnchorError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"archived original unavailable: {reason}")


class OriginalTextIntegrityError(DisclosureAnchorError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"archived original failed verification: {reason}")


class ReadQuickDocumentText:
    def __init__(
        self,
        *,
        locator: OriginalLocatorReader,
        extractor: QuickTextExtractor,
        original_root: Path,
    ) -> None:
        if not original_root.is_absolute():
            raise ValueError("original_root must be absolute")
        self._locator = locator
        self._extractor = extractor
        self._original_root = original_root

    def execute(
        self,
        *,
        document_id: str,
        cursor: str | None,
        start_page: int | None,
        max_chars: int,
        max_pages: int,
    ) -> dict[str, Any]:
        located = self._locator.locate(document_id)
        if located is None:
            raise DocumentTextNotFound(f"document {document_id} not found")
        if not located.raw_file_hash or not located.raw_file_relpath:
            raise OriginalTextUnavailable("no_archived_original")
        result = self._extractor.extract(
            root=self._original_root,
            relpath=Path(located.raw_file_relpath),
            expected_sha256=located.raw_file_hash,
            cursor=cursor,
            start_page=1 if start_page is None else start_page,
            char_offset=0,
            max_chars=max_chars,
            max_pages=max_pages,
        )
        if result.source_sha256 != located.raw_file_hash:
            raise OriginalTextIntegrityError("source_hash_mismatch")
        return {
            "contract_version": TEXT_CONTRACT_VERSION,
            "document_id": located.document_id,
            "raw_file_hash": located.raw_file_hash,
            "source_byte_count": result.source_byte_count,
            "document_status": located.status,
            "formal_units_available": located.status == "published",
            "text_kind": TEXT_KIND,
            "extractor": {
                "name": result.extractor,
                "version": result.extractor_version,
                "ocr": False,
            },
            "total_pages": result.total_pages,
            "chunks": [asdict(chunk) for chunk in result.chunks],
            "coverage": {
                "pages_scanned": result.pages_scanned,
                "next_page": result.next_page,
                "next_char_offset": result.next_char_offset,
                "complete": result.complete,
                "full_document_traversed": result.full_document_traversed,
            },
            "next_cursor": result.next_cursor,
            "limitations": list(result.limitations),
            "citation": {
                "document_id": located.document_id,
                "provider": located.provider,
                "provider_document_id": located.provider_document_id,
                "security_code": located.security_code,
                "exchange": located.exchange,
                "title": located.title,
                "announcement_date": (
                    None
                    if located.announcement_date is None
                    else located.announcement_date.isoformat()
                ),
                "raw_file_hash": located.raw_file_hash,
                "source_ref": located.source_ref,
                "company_ref": located.company_ref,
                "security_ref": located.security_ref,
                "text_kind": TEXT_KIND,
            },
        }
